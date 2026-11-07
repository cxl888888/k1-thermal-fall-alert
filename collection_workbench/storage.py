"""Read original sessions; create independent, immutable label revisions."""
from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import uuid
import zipfile

from analysis import inspect_frame, integer_ns, label_findings
from targeted import capture_context

BASE = Path(__file__).resolve().parent
LABEL_SCHEMA = json.loads((BASE / 'labels.json').read_text(encoding='utf-8'))
LABEL_IDS = {item['id'] for item in LABEL_SCHEMA['labels']}
FIELDS = ['session_id', 'label_id', 'participant_id', 'label', 'start_mono_ns',
          'end_mono_ns', 'end_reason', 'note', 'annotator_id',
          'boundary_uncertainty_ms', 'review_status', 'capture_context']


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def manifest_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def csv_text(value):
    # SECURITY: preserve raw text in review.json; escape spreadsheet formulas only in CSV exports.
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


@contextmanager
def revision_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.save.lock'
    try:
        stream = path.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('另一个实例正在保存修订，或上次保存中断留下锁；请等待并核查后重试') from exc
    try:
        stream.write(json.dumps({'pid': os.getpid(), 'created_at': utc_now()}))
        stream.flush()
        yield
    finally:
        stream.close()
        path.unlink(missing_ok=True)


def text_field(value, name, limit=1000, required=False):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}格式错误或过长')
    value = value.strip()
    if required and not value:
        raise ValueError(f'请填写{name}')
    return value


def seconds_text(ns):
    return format(Decimal(ns) / Decimal(1_000_000_000), 'f')


def seconds_to_ns(value):
    # Decimal keeps original K1 nanoseconds out of JavaScript floating point.
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError('时间必须是十进制秒数字符串')
        result = Decimal(value) * 1_000_000_000
        if not result.is_finite() or result < 0 or result != result.to_integral_value():
            raise ValueError('时间需非负，最多九位小数')
        if result > 10**18:
            raise ValueError('时间超出范围')
        return int(result)
    except InvalidOperation as exc:
        raise ValueError('时间格式错误') from exc


def safe_child(folder, name):
    path = folder / name
    if path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError('会话中包含不支持的符号链接')
    return path


class Session:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.meta = read_json(safe_child(self.path, 'metadata.json'))
        if not isinstance(self.meta, dict):
            raise ValueError('metadata.json 不是对象')
        self.original_meta = dict(self.meta)
        self.display_name = self.meta.get('dataset_name') or self.meta.get('scene') or self.meta.get('session_id')
        self.recovery = None
        self.recovery_signature = None
        self.sid = text_field(self.meta.get('session_id'), '会话ID', 200, True)
        self.base_ns = integer_ns(self.meta['mono_start_ns'])
        self.end_ns = integer_ns(self.meta.get('mono_end_ns', self.base_ns))
        if self.end_ns < self.base_ns:
            raise ValueError('会话时间范围错误')
        self.token = hashlib.sha256(str(self.path).encode()).hexdigest()[:24]
        self.frames = []
        self.offsets = []
        self.issues = []
        self.indexed = False
        self.index_lock = threading.Lock()
        self.review_lock = threading.Lock()
        self.source_stat = None
        self.line_count = 0
        self.radar_index = None

    def describe(self):
        return dict(key=self.token, session_id=self.sid, display_name=self.display_name, scene=self.meta.get('scene'),
                    participant=self.meta.get('participant', self.meta.get('participant_id')),
                    status=self.meta.get('status'), thermal_simulated=self.meta.get('thermal_simulated', False),
                    radar_mode=self.meta.get('radar_mode', 'not_connected'),
                    recovery_id=self.recovery['recovery_id'] if self.recovery else None,
                    seconds=(self.end_ns - self.base_ns) / 1e9, path=str(self.path))

    def manifest_signature(self):
        return {name: (path.stat().st_size, path.stat().st_mtime_ns)
                for name in ('metadata.json', 'thermal.jsonl', 'labels.csv', 'events.jsonl', 'notes.jsonl',
                             'radar.jsonl', 'radar_raw.bin', 'radar_reads.jsonl', 'radar_config.json')
                if (path := safe_child(self.path, name)).is_file()}

    def apply_recovery(self, recovery):
        if (not isinstance(recovery, dict)
                or recovery.get('schema_version') != 'interrupted_recovery_0.1'
                or recovery.get('session_id') != self.sid
                or recovery.get('confirmed_stopped') is not True
                or not isinstance(recovery.get('recovery_id'), str)
                or not isinstance(recovery.get('issues'), list)
                or recovery.get('original_status') != self.original_meta.get('status')):
            raise ValueError('中断恢复记录与原始会话不一致')
        end = recovery.get('mono_end_ns')
        if isinstance(end, bool) or not isinstance(end, int) or end <= self.base_ns:
            raise ValueError('中断恢复记录没有有效结束时间')
        signature = self.manifest_signature()
        if self.manifest() != recovery.get('source_manifest') or self.manifest_signature() != signature:
            raise ValueError('原始文件已变化，中断恢复视图不可继续使用；请重新核查')
        self.recovery, self.recovery_signature = recovery, signature
        self.end_ns = end
        self.meta.update(status='interrupted', mono_end_ns=end,
                         recovery_id=recovery['recovery_id'], fusion_training_ready=False)

    def stat_signature(self):
        file = safe_child(self.path, 'thermal.jsonl')
        st = file.stat()
        return st.st_size, st.st_mtime_ns

    def ensure_index(self):
        with self.index_lock:
            if read_json(safe_child(self.path, 'metadata.json')) != self.original_meta:
                raise ValueError('原始元数据已变化，请刷新列表并重新核对会话')
            if self.recovery and self.manifest_signature() != self.recovery_signature:
                raise ValueError('中断恢复后的原始文件已变化，请刷新会话并重新核查')
            if self.indexed:
                if self.stat_signature() != self.source_stat:
                    raise ValueError('原始温度文件已变化，请刷新会话列表后重新打开')
                return
            if self.meta.get('status') in ('recording', 'starting', 'stopping'):
                raise ValueError('会话尚未结束；请先停止采集再回放')
            before = self.stat_signature()
            rows, offsets = [], []
            issues = list(self.recovery.get('issues', [])) if self.recovery else []
            previous = previous_end = previous_id = None
            with safe_child(self.path, 'thermal.jsonl').open('rb') as stream:
                line_no = 0
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    line_no += 1
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('帧记录不是对象')
                        start = integer_ns(row['read_start_mono_ns'])
                        end = integer_ns(row['read_end_mono_ns'])
                        if start < self.base_ns or start > end or end > self.end_ns:
                            raise ValueError('帧时间超出会话范围')
                        if row.get('session_id') != self.sid:
                            raise ValueError('帧 session_id 不一致')
                        if previous is not None and start < previous:
                            raise ValueError('帧读取时间倒退')
                        quality = inspect_frame(row)
                        frame_id = row.get('frame_id')
                        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
                            quality['errors'].append('帧ID不是非负整数')
                        elif previous_id is not None and frame_id <= previous_id:
                            quality['errors'].append('帧ID重复或倒退')
                        if previous_end is not None and start < previous_end:
                            quality['errors'].append('读取区间与上一帧重叠')
                        if 'representative_mono_ns' in row and integer_ns(row['representative_mono_ns']) != (start+end)//2:
                            quality['errors'].append('代表时间与读取区间中点不一致')
                        issues.extend(f'第{line_no}行：{error}' for error in quality['errors'])
                        rows.append(dict(n=len(rows), frame_id=row.get('frame_id'),
                                         start_s=seconds_text(start - self.base_ns),
                                         end_s=seconds_text(end - self.base_ns),
                                         time_s=((start + end) // 2 - self.base_ns) / 1e9,
                                         valid=quality['valid'] and not quality['errors'],
                                         valid_pixels=row.get('valid_pixels'),
                                         status=row.get('status', 'unknown'),
                                         simulated=bool(row.get('simulated'))))
                        offsets.append(offset)
                        previous = start
                        previous_end = end
                        if isinstance(frame_id, int) and not isinstance(frame_id, bool):
                            previous_id = frame_id
                    except (ValueError, TypeError, KeyError, OverflowError) as exc:
                        issues.append(f'第{line_no}行无法回放：{exc}')
            if self.stat_signature() != before:
                raise ValueError('索引期间原始文件发生变化，请停止写入后重试')
            self.frames, self.offsets, self.issues = rows, offsets, issues
            self.line_count = line_no
            self.source_stat = before
            self.indexed = True

    def frame(self, n):
        self.ensure_index()
        if n < 0 or n >= len(self.offsets):
            raise ValueError('帧序号超出范围')
        with safe_child(self.path, 'thermal.jsonl').open('rb') as stream:
            stream.seek(self.offsets[n])
            row = json.loads(stream.readline())
        # Absolute nanoseconds are strings in HTTP responses, integers on disk.
        for key in ('read_start_mono_ns', 'read_end_mono_ns', 'representative_mono_ns'):
            if key in row:
                row[key] = str(row[key])
        row['relative'] = self.frames[n]
        return row

    def radar_at_frame(self, n):
        self.ensure_index()
        if not 0 <= n < len(self.frames):
            raise ValueError('帧序号超出范围')
        if self.radar_index is None:
            from radar_stream import RadarReplay
            self.radar_index = RadarReplay(self.path, self.meta)
        entry = self.frames[n]
        at = self.base_ns + int((Decimal(entry['start_s']) + Decimal(entry['end_s'])) * 500_000_000)
        result = self.radar_index.at(at)
        result['issues'] = list(self.radar_index.issues)
        if result.get('row'):
            result['row'] = dict(result['row'])
            for key in ('read_start_mono_ns', 'read_end_mono_ns', 'representative_mono_ns'):
                result['row'][key] = str(result['row'][key])
        return result

    def labels(self):
        path = safe_child(self.path, 'labels.csv')
        if not path.exists():
            return []
        result = []
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for i, row in enumerate(csv.DictReader(stream)):
                if row.get('session_id') != self.sid:
                    raise ValueError('原始标签 session_id 不一致')
                try:
                    start, end = int(row['start_mono_ns']), int(row['end_mono_ns'])
                    if not self.base_ns <= start < end <= self.end_ns:
                        raise ValueError('原始标签时间范围无效')
                    if not isinstance(row.get('label'), str) or not row['label']:
                        raise ValueError('标签缺少动作名称')
                except (ValueError, TypeError, KeyError):
                    if not self.recovery:
                        raise
                    issue = f'原始标签第{i+1}条不完整或时间无效，恢复视图未作为完整标签使用；原行保留。'
                    if issue not in self.issues:
                        self.issues.append(issue)
                    continue
                result.append(dict(label_id=row.get('label_id') or f'original_{i+1}',
                                   label=row['label'], start_s=seconds_text(start - self.base_ns),
                                   end_s=seconds_text(end - self.base_ns),
                                   end_reason=row.get('end_reason', ''), note=row.get('note', ''),
                                   annotator_id=row.get('annotator_id', ''),
                                   boundary_uncertainty_ms=row.get('boundary_uncertainty_ms', ''),
                                   review_status=row.get('review_status', 'pending'),
                                   capture_context=capture_context(row.get('capture_context', ''))))
        return result

    def notes(self):
        result = []
        path = safe_child(self.path, 'notes.jsonl')
        if path.exists():
            with path.open(encoding='utf-8-sig') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        row['time_s'] = (int(row['mono_ns']) - self.base_ns) / 1e9
                        row['mono_ns'] = str(row['mono_ns'])
                        result.append(row)
                    except (ValueError, TypeError, KeyError):
                        result.append({'text': '原始备注中有不可解析的行，原文件保留'})
        return result

    def manifest(self):
        return {name: digest(safe_child(self.path, name)) for name in
                ('metadata.json', 'thermal.jsonl', 'labels.csv', 'events.jsonl', 'notes.jsonl',
                 'radar.jsonl', 'radar_raw.bin', 'radar_reads.jsonl', 'radar_config.json')
                if safe_child(self.path, name).is_file()}


class Store:
    def __init__(self, roots, derived):
        self.roots = [Path(p).expanduser().resolve() for p in roots]
        self.derived = Path(derived).expanduser().resolve()
        self.sessions = {}
        self.lock = threading.RLock()
        self.review_tokens = {}
        from session_trash import SessionTrash
        self.trash = SessionTrash(self.roots,self.derived,self.derived.parent/'.trash')

    def scan(self):
        found, issues = {}, []
        for root in self.roots:
            if not root.exists():
                continue
            # Supported roots: a session itself, its parent, or up to two wrapper levels.
            candidates = set()
            for pattern in ('metadata.json', '*/metadata.json', '*/*/metadata.json'):
                candidates.update(root.glob(pattern))
            for path in sorted(candidates):
                if len(found) >= 3000:
                    issues.append('会话过多，请缩小 --data-root 目录')
                    break
                try:
                    if (not path.resolve().is_relative_to(root) or path.is_symlink()
                            or path.resolve().is_relative_to(self.trash.location)):
                        continue
                    session = Session(path.parent)
                    display = self.derived/session.token/'display.json'
                    if display.is_file():
                        saved=read_json(display)
                        if saved.get('session_id')==session.sid:
                            session.display_name=text_field(saved.get('display_name'),'数据集名称',100,True)
                    if not safe_child(session.path, 'thermal.jsonl').is_file():
                        continue
                    if self.derived == session.path or self.derived.is_relative_to(session.path):
                        raise ValueError('修订输出目录不得位于原始会话内部')
                    if session.meta.get('status') in ('recording', 'starting', 'stopping'):
                        for recovery_path in sorted((self.derived / session.token).glob('recovery_*/recovery.json'), reverse=True):
                            try:
                                session.apply_recovery(read_json(recovery_path))
                                break
                            except (OSError, ValueError, TypeError, KeyError) as exc:
                                issues.append(f'{session.sid}：恢复视图未启用：{exc}')
                    found[session.token] = session
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    issues.append(f'{path.parent.name}：{exc}')
        with self.lock:
            self.sessions = found
        return {'sessions': sorted([s.describe() for s in found.values()], key=lambda s: s['session_id'], reverse=True),
                'issues': issues, 'roots': [str(p) for p in self.roots]}

    def get(self, key):
        with self.lock:
            if key not in self.sessions:
                raise ValueError('会话不存在，请刷新列表')
            return self.sessions[key]

    def rename(self,key,data):
        session=self.get(key)
        name=text_field(data.get('display_name'),'数据集名称',100,True)
        with session.review_lock:
            target=self.derived/session.token/'display.json'
            target.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(target,dict(session_id=session.sid,display_name=name,updated_at=utc_now()))
            session.display_name=name
        return session.describe()

    def recycle_folder(self,folder,reason='operator_cancel'):
        session=Session(folder)
        return self.recycle(session.token,dict(confirmed_session_id=session.sid),reason,session)

    def recycle(self,key,data,reason='operator_delete',session=None):
        session=session or self.get(key)
        if data.get('confirmed_session_id')!=session.sid:
            raise ValueError('请确认当前选中的数据集后再移入回收区')
        with self.lock,session.review_lock,session.index_lock:
            result=self.trash.remove(session.path,session.sid,session.token,session.display_name,reason)
            self.sessions.pop(key,None)
            return result

    def recover(self, key, data):
        # Integrity: confirmation is a human assertion, never inferred from a quiet file or PC mode.
        if data.get('confirmed_stopped') is not True:
            raise ValueError('请先确认原采集进程已停止；本工具不会停止或探测传感器进程')
        annotator = text_field(data.get('annotator_id'), '审核人代号', 80, True)
        reason = text_field(data.get('reason'), '中断恢复原因', 2000, True)
        with self.lock:
            session = Session(self.get(key).path)
            if session.original_meta.get('status') not in ('recording', 'starting', 'stopping'):
                raise ValueError('仅支持显式恢复处于采集中状态的中断记录')
            if self.derived == session.path or self.derived.is_relative_to(session.path):
                raise ValueError('恢复输出目录不得位于原始会话内部')
            with revision_lock(self.derived / session.token):
                before = session.manifest()
                if read_json(safe_child(session.path, 'metadata.json')) != session.original_meta:
                    raise ValueError('读取期间原始元数据变化，请停止采集后重试')
                issues = ['中断会话：结束时间为最后一条可解析记录的时间，不代表正常结束；所有标签仍需人工核对。']
                end = session.base_ns
                opened, closed = set(), set()

                def timestamp(value):
                    if isinstance(value, bool) or not isinstance(value, (int, str)):
                        raise ValueError('时间戳不是整数')
                    value = int(value)
                    if value < session.base_ns:
                        raise ValueError('时间戳早于会话开始')
                    return value

                for name in ('thermal.jsonl', 'labels.csv', 'notes.jsonl', 'events.jsonl'):
                    path = safe_child(session.path, name)
                    if not path.is_file():
                        continue
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        rows = csv.DictReader(stream) if name == 'labels.csv' else stream
                        for number, raw in enumerate(rows, 1):
                            try:
                                row = raw if isinstance(raw, dict) else json.loads(raw)
                                if not isinstance(row, dict):
                                    raise ValueError('记录不是对象')
                            except (ValueError, TypeError) as exc:
                                issues.append(f'{name} 第{number}条无法解析，原文保留：{exc}')
                                continue
                            # Thermal/labels require session_id; legacy notes may omit it.
                            if ((name in ('thermal.jsonl', 'labels.csv') and row.get('session_id') != session.sid)
                                    or ('session_id' in row and row['session_id'] != session.sid)):
                                raise ValueError(f'{name} 第{number}条 session_id 不一致，拒绝恢复')
                            try:
                                if name in ('thermal.jsonl', 'labels.csv'):
                                    start = timestamp(row['read_start_mono_ns'] if name == 'thermal.jsonl' else row['start_mono_ns'])
                                    finish = timestamp(row['read_end_mono_ns'] if name == 'thermal.jsonl' else row['end_mono_ns'])
                                    if start > finish:
                                        raise ValueError('区间时间倒退')
                                    end = max(end, finish)
                                    if name == 'labels.csv' and row.get('label_id'):
                                        if not isinstance(row['label_id'], str):
                                            raise ValueError('标签ID格式错误')
                                        closed.add(row['label_id'])
                                else:
                                    end = max(end, timestamp(row['mono_ns']))
                                    if name == 'events.jsonl' and row.get('label_id'):
                                        if not isinstance(row['label_id'], str):
                                            raise ValueError('标签ID格式错误')
                                        if row.get('event') == 'label_start':
                                            opened.add(row['label_id'])
                                        elif row.get('event') == 'label_end':
                                            closed.add(row['label_id'])
                            except (ValueError, TypeError, KeyError) as exc:
                                issues.append(f'{name} 第{number}条无有效时间，原文保留：{exc}')
                if end <= session.base_ns:
                    raise ValueError('没有可用于推导结束时间的有效记录，拒绝恢复')
                unclosed = sorted(opened - closed)
                if unclosed:
                    issues.append(f'有 {len(unclosed)} 个动作开始事件未闭合；未自动生成完整标签，请人工核对。')
                if session.manifest() != before:
                    raise ValueError('恢复检查期间原始文件变化，未提交恢复记录')
                recovery_id = 'recovery_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ_') + uuid.uuid4().hex[:6]
                target = self.derived / session.token / recovery_id
                target.mkdir(parents=True, exist_ok=False)
                recovery = dict(schema_version='interrupted_recovery_0.1', recovery_id=recovery_id,
                                created_at=utc_now(), session_id=session.sid, source_path=str(session.path),
                                source_manifest=before, original_status=session.original_meta.get('status'),
                                confirmed_stopped=True, annotator_id=annotator, reason=reason,
                                mono_end_ns=end, end_time_basis='latest_parseable_source_record',
                                unclosed_label_ids=unclosed, issues=issues,
                                fusion_training_ready=False, review_status='pending')
                atomic_json(target / 'recovery.json', recovery)
                session.apply_recovery(recovery)
                self.sessions[key] = session
                return dict(path=str(target), recovery_id=recovery_id, session_key=key, issues=issues)

    def details(self, key):
        session = self.get(key)
        session.ensure_index()
        source_manifest = session.manifest()
        original = session.labels()
        current = original
        revision_id = None
        revisions = []
        # Restore the most recent saved revision after page reload / PC restart.
        directory = self.derived / session.token
        if directory.is_dir():
            for path in sorted(directory.glob('*/review.json'), reverse=True):
                try:
                    review = read_json(path)
                    revisions.append({'revision_id': review['revision_id'], 'created_at': review['created_at'],
                                      'review_status': review['review_status']})
                    if revision_id is None:
                        if review['source_manifest'] != source_manifest:
                            raise ValueError('原始文件与已有修订的来源不一致，请保留修订并重新核查')
                        if not isinstance(review.get('labels'), list):
                            raise ValueError('已有修订的标签列表格式错误')
                        findings = label_findings(review['labels'], (session.end_ns-session.base_ns)/1e9, check_overlap=False)
                        if findings:
                            raise ValueError('已有修订标签无效：' + '；'.join(findings[:5]))
                        current = review['labels']
                        revision_id = review['revision_id']
                except OSError as exc:
                    raise ValueError('已有修订读取失败') from exc
        meta = dict(session.meta)
        for name in ('mono_start_ns', 'mono_end_ns', 'wall_start_unix_ns'):
            if name in meta:
                meta[name] = str(meta[name])
        notes = session.notes()
        if session.manifest() != source_manifest:
            raise ValueError('读取期间原始文件变化，请重新打开会话')
        recovery = dict(session.recovery) if session.recovery else None
        if recovery:
            if recovery['source_manifest'] != source_manifest:
                raise ValueError('原始文件已变化，请重新核查中断恢复记录')
            recovery['mono_end_ns'] = str(recovery['mono_end_ns'])
        return dict(session=session.describe(), metadata=meta, frames=session.frames,
                    labels=current, original_labels=original, notes=notes,
                    source_manifest_sha256=manifest_digest(source_manifest),
                    issues=session.issues, revision_id=revision_id, revisions=revisions,
                    recovery=recovery,
                    recovery_notice='中断恢复视图；原始文件未修改，未闭合动作未补齐，标签仍需审核。' if recovery else None)

    def save_review(self, key, data):
        session = self.get(key)
        session.ensure_index()
        annotator = text_field(data.get('annotator_id', ''), '标注员代号', 80)
        reason = text_field(data.get('reason', ''), '本次修改或核对说明', 2000)
        if not isinstance(data.get('reviewed', False), bool):
            raise ValueError('人工审核状态必须为布尔值')
        reviewed = data.get('reviewed') is True
        labels = data.get('labels')
        if not isinstance(labels, list) or len(labels) > 5000:
            raise ValueError('标签列表格式或数量不正确')
        result, ids = [], set()
        for row in labels:
            if not isinstance(row, dict) or row.get('label') not in LABEL_IDS:
                raise ValueError('动作名称必须使用标签规则中的枚举')
            lid = text_field(row.get('label_id'), '标签ID', 100, True)
            if lid in ids:
                raise ValueError('标签ID重复')
            ids.add(lid)
            a, b = seconds_to_ns(row.get('start_s')), seconds_to_ns(row.get('end_s'))
            if a >= b or session.base_ns + b > session.end_ns:
                raise ValueError('区间须满足：0 ≤ 开始 < 结束 ≤ 会话时长')
            uncertainty = row.get('boundary_uncertainty_ms', '')
            if uncertainty not in ('', None):
                try:
                    number = float(uncertainty)
                except (ValueError, TypeError) as exc:
                    raise ValueError('边界不确定性须填写毫秒数或留空') from exc
                if not math.isfinite(number) or number < 0 or number > 86_400_000:
                    raise ValueError('边界不确定性超出范围')
                uncertainty = number
            else:
                uncertainty = ''
            result.append(dict(label_id=lid, label=row['label'], start_s=seconds_text(a), end_s=seconds_text(b),
                               end_reason=text_field(row.get('end_reason', 'offline_annotation'), '结束原因', 100),
                               note=text_field(row.get('note', ''), '标签备注', 2000),
                               annotator_id=annotator, boundary_uncertainty_ms=uncertainty,
                               review_status='human_reviewed' if reviewed else 'pending',
                               capture_context=capture_context(row.get('capture_context', ''))))
        result.sort(key=lambda row: Decimal(row['start_s']))
        # SECURITY / integrity: serialize saves even if a concurrent scan replaces Session objects.
        with self.lock, session.review_lock, revision_lock(self.derived / session.token):
            existing = sorted((self.derived / session.token).glob('*/review.json'))
            previous = read_json(existing[-1])['revision_id'] if existing else None
            if data.get('base_revision_id') != previous:
                raise ValueError('另一个窗口已保存修订；请重新打开会话后再修改')
            manifest = session.manifest()
            if data.get('source_manifest_sha256') != manifest_digest(manifest):
                raise ValueError('原始文件自打开后已变化，不能基于旧内容保存；请重新打开并核对')
            original = session.labels()
            revision_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ_') + uuid.uuid4().hex[:6]
            target = self.derived / session.token / revision_id
            target.mkdir(parents=True, exist_ok=False)
            review = dict(schema_version='label_review_0.1', revision_id=revision_id,
                          previous_revision_id=previous, created_at=utc_now(),
                          previous_review_sha256=digest(existing[-1]) if existing else None,
                          session_id=session.sid, source_path=str(session.path), source_manifest=manifest,
                          label_schema_version=LABEL_SCHEMA['version'],
                          label_schema_sha256=digest(BASE / 'labels.json'),
                          annotator_id=annotator, reason=reason,
                          review_status='human_reviewed' if reviewed else 'pending',
                          original_labels=original, labels=result,
                          interrupted_recovery=session.recovery,
                          clock='original K1 monotonic timestamps; no PC clock realignment',
                          fusion_training_ready=False,
                          limitation='Manual reference annotations; no automatic ground-truth certification.')
            try:
                with (target / 'labels_reviewed.csv').open('x', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=FIELDS)
                    writer.writeheader()
                    for row in result:
                        export = {k: v for k, v in row.items() if k in FIELDS}
                        export.update(session_id=session.sid,
                                      participant_id=session.meta.get('participant', session.meta.get('participant_id', '')),
                                      start_mono_ns=session.base_ns + seconds_to_ns(row['start_s']),
                                      end_mono_ns=session.base_ns + seconds_to_ns(row['end_s']))
                        writer.writerow({key: csv_text(value) for key, value in export.items()})
                    stream.flush()
                    os.fsync(stream.fileno())
                if session.manifest() != manifest:
                    raise ValueError('保存期间原始文件发生变化，修订未提交，请重新核查')
                review['labels_reviewed_sha256'] = digest(target / 'labels_reviewed.csv')
                atomic_json(target / 'review.json', review)
            except Exception:
                # Leave an explicit uncommitted folder for diagnosis; only review.json commits a revision.
                raise
            download_key = uuid.uuid4().hex
            with self.lock:
                self.review_tokens[download_key] = target
            return dict(revision_id=revision_id, path=str(target), review_status=review['review_status'],
                        labels=result, download_url='/api/download/' + download_key)

    def download(self, token):
        with self.lock:
            path = self.review_tokens.get(token)
        if path is None:
            raise ValueError('下载不存在，请重新保存或在修订目录中查看')
        return self._revision_zip(path)

    def download_revision(self, key, revision_id):
        # SECURITY: resolve only a committed revision belonging to this indexed session.
        session = self.get(key)
        if not isinstance(revision_id, str) or len(revision_id) > 80 or not all(c.isascii() and (c.isalnum() or c == '_') for c in revision_id):
            raise ValueError('修订编号无效')
        path = safe_child(self.derived / session.token, revision_id)
        review = read_json(safe_child(path, 'review.json'))
        if review.get('session_id') != session.sid or review.get('revision_id') != revision_id:
            raise ValueError('修订不属于当前会话')
        if review.get('source_manifest') != session.manifest():
            raise ValueError('来源已变化，请重新核查后再导出')
        return self._revision_zip(path)

    @staticmethod
    def _revision_zip(path):
        review = read_json(safe_child(path, 'review.json'))
        csv_path = safe_child(path, 'labels_reviewed.csv')
        if review.get('labels_reviewed_sha256') and digest(csv_path) != review['labels_reviewed_sha256']:
            raise ValueError('修订CSV与提交时校验值不一致，拒绝导出')
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in ('review.json', 'labels_reviewed.csv'):
                archive.write(path / name, arcname=name)
        return blob.getvalue()
