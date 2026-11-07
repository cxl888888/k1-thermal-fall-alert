"""Explicit voice-journal -> durable outbox -> authenticated loopback ingestion.

Requires an explicit source journal and origin; never scans old journals. First
launch starts at the tail unless --from-start is explicitly selected. No device
commands, alarm resolution or notification-delivery claims are made here.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from pydantic import ValidationError

from .schemas import EventBody, ThermalFrame


def journal_voice_entry(kind, value):
    state = value.get('state', {})
    follow_up = {'follow_up': True} if state.get('clarification_count') else {}
    if kind == 'prompt_finished':
        if value.get('succeeded') is False:
            return {'kind': 'playback_failed', **follow_up}
        text = value.get('spoken_text')
        if value.get('succeeded') is True and isinstance(text, str) and text.strip():
            return {'kind': 'question', 'text': text[:4000], **follow_up}
    elif kind == 'no_answer':
        return {'kind': 'no_answer'}
    elif (kind == 'sensor' and value.get('source') == 'thermal'
          and value.get('kind') == 'candidate' and value.get('simulated') is False):
        return {'kind': 'thermal_candidate', 'risk': 'thermal_candidate'}
    elif kind == 'facts':
        raw = value.get('transcript')
        text = raw[:4000] if isinstance(raw, str) and raw.strip() else ''
        events = value.get('effective_events') or ()
        risk = ({'risk': 'emergency_help'} if '求救' in events else
                {'risk': 'self_reported_fall'} if '跌倒' in events else {})
        if not text:
            if value.get('source') == 'kws_live' and value.get('new_red_trigger') and risk:
                return {'kind': 'utterance', **risk}
            return None
        if value.get('answer_eligible') is True:
            return {'kind': 'answer', 'text': text, **risk}
        red_follow_up = state.get('red') is True and any(
            event in events for event in ('明确安全', '求救', '跌倒')
        )
        if value.get('new_red_trigger') is True or value.get('prompt') is not None or red_follow_up:
            return {'kind': 'utterance', 'text': text, **risk}
    return None


def journal_thermal_frames(value):
    if value.get('source') != 'thermal' or value.get('kind') != 'candidate' or value.get('simulated') is not False:
        return None
    evidence = value.get('evidence')
    raw = evidence.get('thermal_frames') if isinstance(evidence, dict) else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        return None
    try:
        frames = [ThermalFrame.model_validate(frame) for frame in raw]
    except ValidationError:
        return None
    offsets = [frame.offset_ms for frame in frames]
    return frames if offsets[-1] == 0 and sorted(set(offsets)) == offsets else None


class VoiceOutbox:
    def __init__(self, journal, outbox, source_id, origin, from_start=False):
        self.journal = Path(journal).resolve(strict=True)
        self.path = Path(outbox).resolve()
        if self.path == self.journal:
            raise ValueError('发送队列不能覆盖语音状态库')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', source_id):
            raise ValueError('source-id 需为1至32位字母数字或_.-')
        if origin not in {'live', 'controlled_simulation', 'synthetic'}:
            raise ValueError('明确选择本次语音来源；旧历史使用voice_history工具')
        if origin == 'live' and from_start:
            raise ValueError('真实接入从当前尾部开始；不得把旧记录重新标成实时')
        self.source_id, self.origin = source_id, origin
        self.path.parent.mkdir(parents=True, exist_ok=True)
        signature=json.dumps([str(self.journal),source_id,origin])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS bridge_state (id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT NOT NULL, row_id INTEGER NOT NULL, incident TEXT, boot TEXT, seq INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0)')
            old=db.execute('SELECT signature FROM bridge_state WHERE id=1').fetchone()
            if old and old[0]!=signature:
                raise ValueError('发送队列属于另一数据源或来源类型，拒绝混用')
            if not old:
                with self.source() as src:
                    row_id=0 if from_start else src.execute('SELECT COALESCE(MAX(id),0) FROM journal').fetchone()[0]
                db.execute('INSERT INTO bridge_state VALUES(1,?,?,NULL,NULL,0)',(signature,row_id))
        if os.name!='nt': self.path.chmod(0o600)

    def source(self):
        uri='file:'+quote(self.journal.as_posix(),safe='/:')+'?mode=ro'
        db=sqlite3.connect(uri,uri=True)
        db.execute('PRAGMA query_only=ON')
        return closing(db)

    def collect(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            signature,last,incident,boot,seq=db.execute('SELECT signature,row_id,incident,boot,seq FROM bridge_state WHERE id=1').fetchone()
            with self.source() as src:
                maximum=src.execute('SELECT COALESCE(MAX(id),0) FROM journal').fetchone()[0]
                if maximum<last:raise ValueError('源状态库已被重置，不能继续沿用旧发送游标')
                rows=src.execute('SELECT id,kind,payload FROM journal WHERE id>? ORDER BY id LIMIT 200',(last,)).fetchall()
            count=0
            for ident,kind,raw in rows:
                value=json.loads(raw)
                if kind not in {'facts','startup','prompt_finished','input_failed','no_answer','sensor',
                                'caregiver_ack','caregiver_resolve','caregiver_ask','inputs_cancelled'}:
                    last=ident;continue
                state=value.get('state')
                if self.origin=='live' and (value.get('simulated') is True or value.get('source')=='simulated_facts'):
                    raise ValueError('源记录包含模拟事实，不得以live发送；请使用明确的模拟来源')
                if not isinstance(state,dict):
                    # Old rows cannot be presented as live state.
                    raise ValueError(f'源记录{ident}缺少状态快照；请从新运行尾部接入')
                if any(type(state.get(k)) is not bool for k in ('red','pending','needs_review')):
                    raise ValueError(f'源记录{ident}告警状态不完整，拒绝猜测安全')
                source_boot=value.get('boot_id')
                if not isinstance(source_boot,str) or not source_boot:
                    raise ValueError(f'源记录{ident}没有开机实例，不能猜测')
                if source_boot!=boot: boot,seq=source_boot,0
                red=state.get('red') is True
                pending=state.get('pending') is True or state.get('needs_review') is True
                create=red or pending or value.get('new_red_trigger') is True or value.get('prompt') is not None
                if create and incident is None:incident=f'voice.{self.source_id}.{ident}'
                seq+=1
                resolution = value.get('resolution') if kind == 'caregiver_resolve' and incident else None
                resolved = bool(resolution and resolution['outcome'] == 'resolved')
                voice_entry = journal_voice_entry(kind, value) if incident else None
                thermal_frames = journal_thermal_frames(value) if incident and kind == 'sensor' and self.origin == 'live' else None
                route=value.get('route') or 'uncertain'
                # The voice policy's clarification route is an uncertain result
                # in the family protocol. Its reason and interaction phase carry the detail.
                if route == 'continue_safety_confirmation':
                    route = 'uncertain'
                thermal_candidate = kind == 'sensor' and value.get('source') == 'thermal' and value.get('kind') == 'candidate'
                reason = value.get('reason') or ('热像发现疑似下降，等待安全核查。' if thermal_candidate else
                          '恢复已有锁存告警' if red and kind == 'startup' else '')
                if resolution:
                    reason = '家属已核查，板端已解除告警。' if resolved else '核查后出现新风险，请重新核查。'
                body=EventBody(device_id='k1_voice.'+self.source_id,boot_id=boot,source_event_id=f'voice.{self.source_id}.{ident}',
                    sequence=seq,state_version=seq,origin=self.origin,kind='incident' if incident else 'status',
                    incident_id=incident,time_quality='synthetic' if self.origin=='synthetic' else 'device_unverified',
                    occurred_at=None,title='紧急语音告警' if red else '安全核查' if incident else '语音状态更新',
                    route=route,reason=str(reason),
                    transcript=(voice_entry.get('text') or None) if voice_entry and voice_entry['kind'] in ('utterance', 'answer') else None,
                    source='family_phone' if resolution else str(value.get('source') or 'voice_runtime'),
                    voice_entry=voice_entry, thermal_frames=thermal_frames, resolution=resolution,
                    red_latched=red,archived=resolved or bool(incident and not red and not pending),
                    interaction_state=str(state.get('prompt_status') or 'unknown'),sensor_status={})
                db.execute('INSERT INTO outbox(payload) VALUES(?)',(body.model_dump_json(),));count+=1
                if resolved or (incident and not red and not pending):incident=None
                last=ident
            db.execute('UPDATE bridge_state SET row_id=?,incident=?,boot=?,seq=? WHERE id=1',(last,incident,boot,seq))
            return count

    def drain(self, send):
        # SECURITY: credentials remain in the sender, never the persisted payload.
        with closing(sqlite3.connect(self.path)) as db:
            rows=db.execute('SELECT id,payload FROM outbox WHERE sent=0 ORDER BY id LIMIT 200').fetchall()
        sent=0
        for ident,payload in rows:
            result=send(json.loads(payload))
            if not isinstance(result,dict) or not isinstance(result.get('duplicate'),bool):
                raise ValueError('服务未返回有效入库应答，保留原事件重试')
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE outbox SET sent=1 WHERE id=?',(ident,))
            sent+=1
        return sent


def sender(url, secret):
    parts=urlsplit(url)
    if (parts.scheme!='http' or parts.hostname not in {'127.0.0.1','localhost','::1'}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {'','/'}):
        raise ValueError('此适配器只向K1本机业务服务发送，不将凭证传到其他主机')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*_args,**_kwargs):return None
    opener=build_opener(ProxyHandler({}),NoRedirect)
    def send(body):
        request=Request(url.rstrip('/')+'/api/ingest',data=json.dumps(body,ensure_ascii=False).encode(),
                        headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
        with opener.open(request,timeout=5) as response:return json.load(response)
    return send


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',type=Path,required=True)
    parser.add_argument('--outbox',type=Path,required=True)
    parser.add_argument('--source-id',required=True)
    parser.add_argument('--origin',choices=('live','controlled_simulation','synthetic'),required=True)
    parser.add_argument('--from-start',action='store_true',help='仅用于明确的新建测试库；默认从当前尾部开始')
    parser.add_argument('--url',default='http://127.0.0.1:8794')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    secret=os.environ.get('FAMILY_PRODUCER_TOKEN')
    if not secret:parser.error('缺少本机FAMILY_PRODUCER_TOKEN')
    queue=VoiceOutbox(args.journal,args.outbox,args.source_id,args.origin,args.from_start)
    send=sender(args.url,secret)
    while True:
        try:
            collected=queue.collect();sent=queue.drain(send)
            if collected or sent:print(f'新增{collected}，业务服务已入库应答{sent}；不表示家属已读',flush=True)
        except (OSError,ValueError) as exc:
            # Avoid printing HTTP body/credentials/transcript on retry.
            print('事件适配暂未完成，队列保留：'+type(exc).__name__,flush=True)
            if args.once:raise SystemExit(1)
        if args.once:return
        time.sleep(.5)


if __name__=='__main__':main()
