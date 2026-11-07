"""Convert an explicitly selected voice journal to historical portal events.

This is an offline, read-only adapter. Its output is always historical_replay;
it never monitors a microphone, posts events, or controls/clears an alert.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .schemas import EventBody


def historical_events(path: Path, source_id: str, device_id: str):
    """Yield deterministic incident events for explicitly recorded prompt/red facts.

    Journal rows do not include the utterance transcript, wall clock, hardware
    boot identity, or a reliable long-lived incident identity. Do not infer them.
    Each trigger is an independent historical event, not an episode reconstruction.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', source_id):
        raise ValueError('source_id must be 1-48 ASCII letters, digits, _ or -')
    file = path.expanduser().resolve(strict=True)
    if not file.is_file():
        raise ValueError('journal path must be an existing file')
    # SECURITY: URI read-only mode: importing history cannot modify source state.
    uri = 'file:' + quote(file.as_posix(), safe='/:') + '?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')  # A consistent snapshot while a source process runs.
        rows = con.execute("SELECT id, payload FROM journal WHERE kind='facts' ORDER BY id")
        sequence = 0
        for row_id, raw in rows:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get('state'), dict):
                raise ValueError(f'invalid facts row {row_id}; stop rather than silently omit it')
            state = payload['state']
            red_trigger = payload.get('new_red_trigger') is True
            prompt = payload.get('prompt') is not None
            # Follow-up clarification belongs to the same question, so it does
            # not create a second incident; no reconstruction of episode IDs.
            clarification = payload.get('prompt_kind') == 'clarify'
            if not red_trigger and (not prompt or clarification):
                continue
            if red_trigger and state.get('red') is not True:
                raise ValueError(f'inconsistent red state in row {row_id}')
            sequence += 1
            ident = f'voice.{source_id}.{row_id}'
            route = payload.get('route', 'uncertain')
            event = EventBody(
                device_id=device_id, boot_id=f'history.{source_id}',
                source_event_id=ident, sequence=sequence, state_version=sequence,
                origin='historical_replay', kind='incident', incident_id=ident,
                occurred_at=None, time_quality='historical',
                title='历史紧急语音事件' if red_trigger else '历史语音安全核查',
                route=route, reason=payload.get('reason', ''), transcript=None,
                source=str(payload.get('source') or 'unknown'),
                red_latched=red_trigger, archived=False,
                interaction_state=str(state.get('prompt_status') or 'unknown'),
                sensor_status={},
            )
            yield event
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path, help='Explicit voice SQLite journal, opened read-only')
    parser.add_argument('--source-id', required=True, help='Stable identifier for this exact source journal')
    parser.add_argument('--device-id', default='voice_history', help='Historical source label')
    args = parser.parse_args()
    for event in historical_events(args.journal, args.source_id, args.device_id):
        print(event.model_dump_json())


if __name__ == '__main__':
    main()
