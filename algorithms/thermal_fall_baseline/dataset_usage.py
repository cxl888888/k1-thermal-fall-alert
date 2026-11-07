"""User-selected session eligibility; never changes raw data or annotations."""
import json
from pathlib import Path

USAGE_PATH = Path(__file__).with_name('dataset_usage.json')


def load_usage():
    data = json.loads(USAGE_PATH.read_text(encoding='utf-8'))
    if data.get('schema_version') != 'dataset_usage_0.1':
        raise ValueError('不支持的数据使用清单版本')
    entries = data.get('deferred_sessions')
    if not isinstance(entries, dict) or any(
        not isinstance(sid, str) or not sid or not isinstance(row, dict)
        or not isinstance(row.get('reason'), str) or not row['reason'].strip()
        for sid, row in entries.items()
    ):
        raise ValueError('暂缓会话清单格式错误')
    return entries


def deferred_reason(session_id, usage=None):
    entries = load_usage() if usage is None else usage
    return entries.get(session_id, {}).get('reason')


def require_session_enabled(session_id):
    reason = deferred_reason(session_id)
    if reason:
        raise ValueError(f'{session_id}已暂缓使用：{reason}')
