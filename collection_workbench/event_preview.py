"""Local family-view rehearsal over read-only voice journal snapshots.

No notification provider, live state mutation, or device command is implemented here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading


class EventPreview:
    def __init__(self, sources):
        self.sources = list(dict.fromkeys(Path(path).expanduser().resolve() for path in sources))
        if len(self.sources) > 20:
            raise ValueError('最多指定20份语音历史快照')
        self.lock = threading.RLock()
        self.rehearsals = {}

    def snapshot(self):
        events, issues = [], []
        with self.lock:
            for path in self.sources:
                try:
                    if path.is_symlink() or path.stat().st_size > 20 * 1024 * 1024:
                        raise ValueError('不读取链接或超过20MB的快照')
                    signature = (path.stat().st_size, path.stat().st_mtime_ns)
                    raw = path.read_bytes()
                    data = json.loads(raw.decode('utf-8-sig'))
                    if (path.stat().st_size, path.stat().st_mtime_ns) != signature:
                        raise ValueError('快照正在变化，请停止写入后重新打开')
                    if not isinstance(data, dict) or not isinstance(data.get('journal'), list):
                        raise ValueError('须为包含journal列表的语音状态快照')
                    if len(data['journal']) > 10000:
                        raise ValueError('快照事件过多，请按场次拆分')
                    source_hash = hashlib.sha256(raw).hexdigest()
                    for ordinal, row in enumerate(data['journal']):
                        if not isinstance(row, dict) or row.get('kind') != 'facts':
                            continue
                        payload = row.get('payload')
                        if not isinstance(payload, dict):
                            issues.append(f'{path.parent.name} 第{ordinal+1}条事实不是对象')
                            continue
                        state = payload.get('state')
                        state = state if isinstance(state, dict) else {}
                        key = hashlib.sha256(f'{path}:{source_hash}:{ordinal}'.encode()).hexdigest()[:32]
                        red = state.get('red') if isinstance(state.get('red'), bool) else None
                        facts = payload.get('effective_events', payload.get('events', []))
                        facts = [str(item)[:100] for item in facts] if isinstance(facts, list) else []
                        events.append(dict(event_id=key, source_session=path.parent.name,
                            ordinal=ordinal+1, source_sha256=source_hash,
                            source_type='historical_voice_journal', source=str(payload.get('source', 'unknown'))[:80],
                            mono_ns=str(row.get('mono_ns', '')), route=str(payload.get('route', 'uncertain'))[:100],
                            reason=str(payload.get('reason', '未保存事件原因'))[:2000], events=facts,
                            transcript=payload.get('transcript') if isinstance(payload.get('transcript'), str) else None,
                            red_alert_latched=red, needs_review=state.get('needs_review'),
                            model_status=str(payload.get('status', 'unknown'))[:80],
                            recorded=True, notification_status='not_verified',
                            rehearsal=dict(self.rehearsals.get(key, dict(acknowledged=False, closed=False)))))
                except (OSError, UnicodeError, ValueError, TypeError) as exc:
                    issues.append(f'{path.parent.name}/{path.name}：{exc}')
            current = {row['event_id'] for row in events}
            self.rehearsals = {key: value for key, value in self.rehearsals.items() if key in current}
        return dict(schema_version='family_preview_0.1', mode='offline_rehearsal', events=events, issues=issues,
                    live_connected=False, notifications_connected=False,
                    note='历史事件只读；确认和结束仅用于本进程演练，重启清空，不代表真实家属通知或板端告警解除。')

    def act(self, data):
        # SECURITY: local rehearsal state only. Never call a live caregiver/resolve API.
        action = data.get('action')
        if action not in ('acknowledge', 'close'):
            raise ValueError('仅支持确认演练或结束演练')
        operator, reason = data.get('operator'), data.get('reason')
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 80:
            raise ValueError('请填写演练操作者代号，最多80字')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError('请填写本次演练说明，最多2000字')
        with self.lock:
            found = {row['event_id']: row for row in self.snapshot()['events']}
            key = data.get('event_id')
            if not isinstance(key, str) or key not in found:
                raise ValueError('历史事件不存在或来源已变化，请刷新')
            previous = dict(self.rehearsals.get(key, dict(acknowledged=False, closed=False)))
            if previous['closed']:
                raise ValueError('本事件的演练已结束；历史事实仍保留')
            if action == 'close' and (not previous['acknowledged'] or data.get('human_checked') is not True):
                raise ValueError('先确认演练，并勾选已完成模拟人工核查；确认本身不解除告警')
            previous.update(acknowledged=True, closed=action == 'close', operator=operator.strip(), reason=reason.strip())
            self.rehearsals[key] = previous
            return dict(event_id=key, rehearsal=previous, source_red_alert_latched=found[key]['red_alert_latched'],
                        hardware_action='none', notification_action='none', persisted=False)
