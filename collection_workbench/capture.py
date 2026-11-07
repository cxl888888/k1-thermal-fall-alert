"""Touch events stay in the server process while getFrame runs in an owned child."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from storage import BASE, FIELDS, LABEL_IDS, LABEL_SCHEMA, atomic_json, read_json, text_field
from targeted import TargetedProgress, validate_plan


PURPOSES = {'functional_trial', 'dataset_collection', 'device_check'}
ACTIVITY_SOURCES = {'everyday_only', 'everyday_and_controlled', 'equipment_only'}
CONTROLLED_LABELS = {'safe_fall_simulation', 'post_fall_immobile', 'recover_after_fall'}
QUALITY_LABELS = {'occlusion', 'unknown', 'invalid'}


def collection_context(data):
    """Validate declared collection conditions before creating any session files."""
    profile_id = data.get('recording_profile')
    profile = next((p for p in LABEL_SCHEMA['recording_profiles'] if p['id'] == profile_id), None)
    if profile is None:
        raise ValueError('请选择采集计划；旧页面请刷新后重新填写')
    purpose, source = data.get('collection_purpose'), data.get('activity_source')
    if (not isinstance(purpose, str) or not isinstance(source, str)
            or purpose not in PURPOSES or source not in ACTIVITY_SOURCES):
        raise ValueError('请选择采集目的和动作来源')
    if (purpose == 'device_check') != (source == 'equipment_only'):
        raise ValueError('设备检查请搭配“仅设备检查”；有动作的试采请选择功能试采或数据集采集')
    installation_id = text_field(data.get('installation_id', ''), '安装配置代号', 80,
                                 purpose != 'device_check')
    protocol = text_field(data.get('simulation_protocol', ''), '模拟方式与保护条件', 2000,
                          source == 'everyday_and_controlled')
    if protocol and source != 'everyday_and_controlled':
        raise ValueError('填写了模拟方式，请同步选择“日常动作＋受控模拟”，或清空该说明')
    targeted = None
    planned = list(profile['label_ids'])
    if profile_id == 'targeted':
        targeted = validate_plan(data.get('targeted_collection'), source)
        planned = list(dict.fromkeys(item['label'] for item in targeted['target_snapshot']['phases']))
    return dict(context_version='collection_context_0.2', collection_purpose=purpose,
                activity_source=source, simulation_protocol=protocol or None,
                recording_profile=profile_id, recording_profile_version=profile['version'],
                recording_profile_snapshot=dict(profile),
                recording_plan_snapshot=[dict(p) for p in LABEL_SCHEMA['recording_plan']
                                         if p['id'] in profile['label_ids']],
                planned_label_ids=planned,
                deferred_label_ids=[p['id'] for p in LABEL_SCHEMA['recording_plan']
                                    if p['id'] not in planned],
                dataset_assignment='unassigned', installation_id=installation_id or None,
                targeted_collection=targeted)


class Capture:
    def __init__(self, enabled, root, driver, radar_port=None, radar_baud=260000):
        self.enabled = enabled
        self.root = Path(root).expanduser().resolve()
        self.driver = Path(driver).expanduser().resolve()
        self.radar_port, self.radar_baud = radar_port, radar_baud
        self.radar_latest, self.radar_counts, self.radar_state = None, {}, 'disabled'
        self.radar_error = None
        self.lock = threading.RLock()
        self.folder = self.process = self.meta = self.latest = self.active = None
        self.monitor = None
        self.seen = {}
        self.counts = {}
        self.label_count = 0
        self.last_error = None
        self._streams = []
        self.finalized = threading.Event()
        self.finalized.set()
        self.targeted = None
        self.discard_callback = None
        self.cancel_requested = False
        self.cancel_result = None
        self.stop_requested_ns = None

    def state(self):
        with self.lock:
            result = dict(enabled=self.enabled, state=self.meta['status'] if self.meta else 'idle',
                          session_id=self.meta['session_id'] if self.meta else None,
                          folder=str(self.folder) if self.folder else None,
                          active=dict(self.active) if self.active else None, labels=self.label_count,
                          counts=dict(self.counts), error=self.last_error,
                          dataset_name=self.meta.get('dataset_name') if self.meta else None,
                          cancel_result=self.cancel_result,
                          stopping_elapsed_s=(time.monotonic_ns()-self.stop_requested_ns)/1e9
                              if self.stop_requested_ns and self.meta and self.meta['status']=='stopping' else 0,
                          radar_mode=self.meta.get('radar_mode', 'not_connected') if self.meta else 'not_connected', latest=self.latest,
                          radar_configurable=bool(self.radar_port),
                          radar=dict(state=self.radar_state, counts=dict(self.radar_counts),
                                     latest=dict(self.radar_latest) if self.radar_latest else None,
                                     error=self.radar_error),
                          collection_context={key: self.meta.get(key) for key in
                              ('collection_purpose', 'activity_source', 'simulation_protocol',
                               'recording_profile', 'recording_profile_version', 'targeted_collection')}
                              if self.meta else None,
                          targeted=self.targeted.snapshot() if self.targeted else None,
                          elapsed_s=(time.monotonic_ns() - self.meta['mono_start_ns']) / 1e9
                          if self.meta and self.meta['status'] in ('starting', 'recording', 'stopping')
                          else self.meta.get('actual_seconds', 0) if self.meta else 0)
            if result['active']:
                result['active']['elapsed_s'] = (time.monotonic_ns() - result['active'].pop('start_mono_ns')) / 1e9
            if self.latest:
                result['latest_age_s'] = (time.monotonic_ns() - self.latest['read_end_mono_ns']) / 1e9
                result['latest'] = dict(self.latest)
                for key in ('read_start_mono_ns', 'read_end_mono_ns', 'representative_mono_ns'):
                    if key in result['latest']:
                        result['latest'][key] = str(result['latest'][key])
            if result['radar']['latest']:
                row = result['radar']['latest']
                result['radar']['age_s'] = (time.monotonic_ns() - int(row['read_end_mono_ns'])) / 1e9
                if self.meta and self.meta['status'] in ('starting', 'recording') and result['radar']['age_s'] > 1:
                    result['radar']['state'] = 'stale'
                for key in ('read_start_mono_ns', 'read_end_mono_ns', 'representative_mono_ns'):
                    row[key] = str(row[key])
            return result

    def _emit(self, stream, row):
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())

    def _event(self, kind, at, **extra):
        self._emit(self.events, dict(session_id=self.meta['session_id'], event=kind,
                   mono_ns=at, clock='server_receive_monotonic_ns', **extra))

    def start(self, data):
        if not self.enabled or sys.platform != 'linux':
            raise ValueError('本实例仅供回放；真实采集需在 K1 显式启用 --capture')
        scene = text_field(data.get('scene'), '场景代号', 100, True)
        dataset_name=text_field(data.get('dataset_name',''),'数据集名称',100) or scene
        participant = text_field(data.get('participant'), '参与者代号', 80, True)
        annotator = text_field(data.get('annotator_id', ''), '标注员代号', 80)
        context = collection_context(data)
        radar_enabled = data.get('radar_enabled', False)
        if not isinstance(radar_enabled, bool) or radar_enabled and not self.radar_port:
            raise ValueError('雷达采集选项无效；需由启动配置提供已核对的串口')
        protocol_path=BASE.parent/'k1_collect'/'ld2410_protocol.py'
        if radar_enabled and not protocol_path.is_file():
            raise ValueError('缺少配套k1_collect/ld2410_protocol.py，不能启动真实雷达采集')
        orientation = text_field(data.get('orientation', ''), '朝向', 200)
        note = text_field(data.get('note', ''), '采集备注', 2000)
        try:
            duration = float(data.get('duration_minutes', 10)) * 60
            height = None if data.get('height_m') in ('', None) else float(data['height_m'])
        except (ValueError, TypeError) as exc:
            raise ValueError('时长或安装高度格式不正确') from exc
        if not math.isfinite(duration) or not 10 <= duration <= 7200:
            raise ValueError('采集时长需在 10 秒到 120 分钟之间')
        if height is not None and (not math.isfinite(height) or height <= 0):
            raise ValueError('安装高度需为正数或留空')
        if context['collection_purpose'] == 'dataset_collection':
            if height is None or orientation in ('', 'not_recorded'):
                raise ValueError('数据集采集需填写实际安装高度和朝向/覆盖范围；未知时可先选择功能试采')
            if participant == 'equipment_only':
                raise ValueError('数据集采集请填写真实匿名参与者代号，不能使用 equipment_only')
        if not self.driver.is_file():
            raise ValueError('未找到板端热像驱动，请检查 --thermal-script')
        with self.lock:
            if self.meta and self.meta['status'] in ('starting', 'recording', 'stopping'):
                raise ValueError('已有采集，请先结束并保存')
            sid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]
            self.folder = self.root / sid
            self.folder.mkdir(parents=True, exist_ok=False)
            now = time.monotonic_ns()
            self.meta = dict(schema_version='collection_0.5', session_id=sid,
                scene=scene, dataset_name=dataset_name, participant=participant, annotator_id=annotator,
                installation=dict(configuration_id=context.pop('installation_id'),
                                  height_m=height, orientation=orientation or 'not_recorded'), note=note,
                **context,
                thermal_simulated=False, radar_mode='real' if radar_enabled else 'not_connected', fusion_training_ready=False,
                radar_config=dict(enabled=radar_enabled, port=self.radar_port if radar_enabled else None,
                    host_baudrate=self.radar_baud if radar_enabled else None, bytesize=8, parity='N', stopbits=1,
                    read_only=True, configuration_bytes_written=0, module_configuration_status='not_queried',
                    kernel=platform.release(),
                    protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest() if radar_enabled else None,
                    host_rate_scope='Current K1/CH340 combination only; radar output configuration unchanged'),
                activity_provenance_by_label={item['id']: item['activity_provenance']
                                              for item in LABEL_SCHEMA['labels']},
                shape=[24, 32], grid_order='row_major_celsius', mono_start_ns=now,
                wall_start_unix_ns=time.time_ns(), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                subpage_refresh_code=3, subpage_target_hz=4,
                frame_validity='all_768_pixels_valid_after_4_successful_warmup_reads',
                clock='K1 time.monotonic_ns; representative frame time=read interval midpoint; not hardware exposure',
                annotation_clock='server handling time; browser/OS/human latency not removed',
                label_schema_version=LABEL_SCHEMA['version'],
                label_schema_sha256=hashlib.sha256((BASE / 'labels.json').read_bytes()).hexdigest(),
                software_sha256={name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                                 for name in ('capture.py', 'sensor_worker.py', 'radar_stream.py', 'fusion.py', 'storage.py', 'app.py', 'analysis.py', 'event_preview.py', 'targeted.py', 'preview.py', 'session_trash.py')},
                duration_limit_seconds=duration, status='starting')
            self.latest = self.active = self.last_error = None
            self.cancel_requested=False;self.cancel_result=None;self.stop_requested_ns=None
            self.targeted = TargetedProgress(self.meta['targeted_collection']) if self.meta.get('targeted_collection') else None
            self.radar_latest, self.radar_counts = None, {}
            self.radar_state, self.radar_error = ('starting' if radar_enabled else 'disabled'), None
            self.counts, self.seen = {}, {}
            self.label_count = 0
            try:
                self.labels = (self.folder / 'labels.csv').open('x', encoding='utf-8', newline='')
                self.events = (self.folder / 'events.jsonl').open('x', encoding='utf-8')
                self.notes = (self.folder / 'notes.jsonl').open('x', encoding='utf-8')
                self._streams = [self.labels, self.events, self.notes]
                self.writer = csv.DictWriter(self.labels, fieldnames=FIELDS)
                self.writer.writeheader()
                self.labels.flush()
                os.fsync(self.labels.fileno())
                atomic_json(self.folder / 'metadata.json', self.meta)
                self._event('session_start', now)
                self.process = subprocess.Popen([sys.executable, '-B', '-u', str(BASE / 'sensor_worker.py'),
                    '--session', str(self.folder), '--driver', str(self.driver), '--duration', str(duration)],
                    cwd=str(BASE), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
            except Exception as exc:
                self.meta.update(status='failed', mono_end_ns=time.monotonic_ns(), error=str(exc))
                atomic_json(self.folder / 'metadata.json', self.meta)
                for stream in self._streams:
                    stream.close()
                self._streams = []
                raise
            self.reader = threading.Thread(target=self._read_output, args=(self.process, self.folder), daemon=True)
            self.reader.start()
            self.finalized.clear()
            self.monitor = threading.Thread(target=self._monitor_owned, args=(self.process, self.folder, duration), daemon=True)
            self.monitor.start()
            return self.state()

    def _read_output(self, process, folder):
        with (folder / 'capture.log').open('x', encoding='utf-8', buffering=1) as log:
            for line in process.stdout:
                if not line.startswith('WORKBENCH_EVENT '):
                    log.write(line)
                    continue
                try:
                    event = json.loads(line[len('WORKBENCH_EVENT '):])
                    with self.lock:
                        if event['kind'] == 'ready':
                            if self.meta['status'] == 'starting':
                                self.meta['status'] = 'recording'
                            self.meta.update({k: v for k, v in event.items() if k != 'kind'})
                            atomic_json(folder / 'metadata.json', self.meta)
                        elif event['kind'] == 'frame':
                            self.latest = event['row']
                            self.counts = {k: v for k, v in event.items() if k not in ('kind', 'row')}
                        elif event['kind'] == 'error':
                            self.last_error = event['error']
                            log.write(event['error'] + '\n')
                        elif event['kind'] == 'radar':
                            self.radar_latest = event.get('row')
                            self.radar_counts = event.get('counts', {})
                            self.radar_state = event.get('state', 'unknown')
                            self.radar_error = event.get('error')
                except (ValueError, KeyError, OSError) as exc:
                    log.write('状态信息处理失败：' + str(exc) + '\n')

    def _close_interval(self, at, reason):
        row = dict(self.active)
        row.update(session_id=self.meta['session_id'], participant_id=self.meta['participant'],
                   end_mono_ns=at, end_reason=reason, review_status='pending')
        self.writer.writerow({k: row.get(k, '') for k in FIELDS})
        self.labels.flush()
        os.fsync(self.labels.fileno())
        self.active = None
        self.label_count += 1
        self._event('label_end', at, label_id=row['label_id'], end_reason=reason)
        if self.targeted:
            event = self.targeted.finish(reason == 'operator_end')
            if event:
                self._event(event.pop('event'), at, **event)
            self.meta['targeted_progress'] = self.targeted.snapshot()
            atomic_json(self.folder / 'metadata.json', self.meta)

    def _monitor_owned(self, process, folder, duration):
        try:
            self._watch(process, folder, duration)
        except Exception as exc:
            with self.lock:
                self.last_error = f'结束保存失败：{type(exc).__name__}: {exc}'
                self.meta.update(status='failed', error=self.last_error, mono_end_ns=time.monotonic_ns())
                for stream in self._streams:
                    try:
                        stream.close()
                    except OSError:
                        pass
                self._streams = []
                try:
                    atomic_json(folder / 'metadata.json', self.meta)
                except OSError as write_error:
                    print(f'[保存异常] {self.last_error}; 元数据写入失败：{write_error}', file=sys.stderr, flush=True)
        finally:
            self.finalized.set()

    def _watch(self, process, folder, duration):
        watchdog = False
        try:
            process.wait(timeout=duration + 90)
        except subprocess.TimeoutExpired:
            watchdog = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.reader.join(timeout=3)
        with self.lock:
            end = time.monotonic_ns()
            if self.active:
                self._close_interval(end, 'recording_stopped_truncated')
            if self.targeted:
                event = self.targeted.abort()
                if event:
                    self._event(event.pop('event'), end, **event)
                self.meta['targeted_progress'] = self.targeted.snapshot()
            try:
                summary = read_json(folder / 'worker_summary.json') if (folder / 'worker_summary.json').exists() else {}
            except (ValueError, OSError):
                summary = {'error': '子进程结束摘要未完整写入；原始帧和按钮事件保留，需核对'}
            reason = summary.get('stop_reason', 'worker_terminated_or_crashed')
            failure = summary.get('error') or self.last_error
            if watchdog:
                reason, failure = 'worker_watchdog_timeout', '热像子进程超时，已终止本工具创建的进程'
            elif process.returncode != 0 and not failure:
                failure = f'热像子进程退出码 {process.returncode}'
            self._event('session_end', end, stop_reason=reason)
            for stream in self._streams:
                stream.close()
            self._streams = []
            self.meta.update(summary)
            if self.meta.get('radar_mode') == 'real':
                self.radar_state = 'error' if (summary.get('radar_summary') or {}).get('error') else 'stopped'
            self.meta.update(status='failed' if failure else 'finished', error=failure,
                             stop_reason=reason, mono_end_ns=end, labels=self.label_count,
                             actual_seconds=(end - self.meta['mono_start_ns']) / 1e9)
            self.last_error = failure
            if self.cancel_requested:
                self.meta.update(status='cancelled',stop_reason='operator_cancel')
            atomic_json(folder / 'metadata.json', self.meta)
            if self.cancel_requested:
                if not self.discard_callback:
                    raise RuntimeError('回收区未配置；本次文件仍保留在原目录')
                self.cancel_result=self.discard_callback(folder,'operator_cancel')
                self.folder=None
                self.latest=None

    def command(self, kind, data):
        with self.lock:
            if not self.meta or data.get('session_id') != self.meta['session_id']:
                raise ValueError('采集会话已变化，请刷新状态')
            request_id = text_field(data.get('request_id'), '请求ID', 100, True)
            if request_id in self.seen:
                return self.state()
            if len(self.seen) >= 20000 and kind not in ('stop','cancel'):
                raise ValueError('本会话操作数量过多，请结束本次采集后新建会话')
            at = time.monotonic_ns()
            if self.meta['status'] != 'recording' and kind not in ('stop','cancel'):
                raise ValueError('当前尚未就绪或正在停止，请等待状态更新')
            if kind == 'label_start':
                if self.active:
                    raise ValueError('请先结束当前动作，避免重叠')
                if data.get('label') not in LABEL_IDS:
                    raise ValueError('请选择有效动作')
                if data['label'] not in self.meta['planned_label_ids']:
                    raise ValueError('本动作在当前计划中暂缓；如需采集，请结束本场后选择完整参考计划')
                source = self.meta['activity_source']
                if data['label'] in CONTROLLED_LABELS and source != 'everyday_and_controlled':
                    raise ValueError('本场未声明受控模拟；请结束后新建会话，填写实际动作来源与模拟方式')
                if source == 'equipment_only' and data['label'] not in QUALITY_LABELS | {'empty'}:
                    raise ValueError('设备检查不记录人体动作；请使用功能试采或数据集采集')
                note = text_field(data.get('note', ''), '备注', 2000)
                context = self.targeted.begin(data['label'], data) if self.targeted else ''
                lid = uuid.uuid4().hex
                self._event('label_start', at, label_id=lid, label=data['label'], note=note,
                            annotator_id=self.meta['annotator_id'], request_id=request_id,
                            capture_context=context)
                self.active = dict(label_id=lid, label=data['label'], start_mono_ns=at,
                                   note=note, annotator_id=self.meta['annotator_id'],
                                   capture_context=context)
            elif kind == 'label_end':
                if not self.active or data.get('label_id') != self.active['label_id']:
                    raise ValueError('当前动作已变化，请刷新状态后重试')
                self._close_interval(at, 'operator_end')
            elif kind == 'note':
                text = text_field(data.get('text'), '备注', 2000, True)
                self._emit(self.notes, dict(mono_ns=at, text=text, annotator_id=self.meta['annotator_id']))
                self._event('note', at, request_id=request_id)
            elif kind == 'targeted_reset':
                if not self.targeted:
                    raise ValueError('当前不是专项采集')
                if self.active:
                    raise ValueError('请先结束当前阶段，再放弃本次周期；已录阶段仍保留')
                event = self.targeted.abort()
                if event:
                    self._event(event.pop('event'), at, request_id=request_id, **event)
                self.meta['targeted_progress'] = self.targeted.snapshot()
                atomic_json(self.folder / 'metadata.json', self.meta)
            elif kind in ('stop','cancel'):
                if kind=='cancel':
                    if self.meta['status'] not in ('starting','recording','stopping'):
                        raise ValueError('本次采集已结束；可在回放列表中移入回收区')
                    if data.get('confirm_discard') is not True:
                        raise ValueError('请先确认取消本次采集')
                    if not self.discard_callback:raise ValueError('回收区尚未配置，不能取消录制')
                    self.cancel_requested=True
                if self.meta['status'] in ('starting', 'recording'):
                    self._event('cancel_requested' if kind=='cancel' else 'stop_requested', at, request_id=request_id)
                    (self.folder / 'stop.request').touch(exist_ok=True)
                    self.meta['status'] = 'stopping'
                    self.stop_requested_ns=at
                    atomic_json(self.folder / 'metadata.json', self.meta)
                    threading.Thread(target=self._stop_owned, args=(self.process,), daemon=True).start()
            else:
                raise ValueError('不支持的采集操作')
            self.seen[request_id] = kind
            return self.state()

    def _stop_owned(self, process):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def shutdown(self):
        with self.lock:
            process = self.process
            monitor = self.monitor
            running = process is not None and process.poll() is None
            if running:
                (self.folder / 'stop.request').touch(exist_ok=True)
        if running:
            self._stop_owned(process)
        # A finished child can still have pending label/metadata writes in the monitor.
        if monitor and monitor is not threading.current_thread():
            monitor.join(timeout=20)
            if monitor.is_alive() or not self.finalized.is_set():
                print('[保存异常] 最终保存尚未完成；请保留会话并使用中断检查入口核对。', file=sys.stderr, flush=True)
