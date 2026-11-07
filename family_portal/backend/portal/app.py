"""Authenticated LAN-service foundation. No hardware/control sender is installed."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, or_, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contact_phone import contact_view, current_phone, other_clients, pending_change
from .db import Change, Client, ContactPhoneChange, Credential, Cursor, Database, Device, Incident, Operation, Pairing, SourceEvent, now, stamp
from .hardware_status import HardwareMonitor, latest_thermal_frame
from .schemas import ContactPhoneBody, EventBody, OperationBody, PairBody

COOKIE = 'k1_family_session'


def digest(value):
    # SECURITY: random 256-bit credentials, not human passwords; never persist plaintext.
    return hashlib.sha256(value.encode()).hexdigest()


def payload_digest(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


@dataclass(frozen=True)
class Settings:
    database_url: str
    frontend: Path = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
    allowed_origins: tuple[str, ...] = ('http://127.0.0.1:8794', 'http://localhost:8794',
                                       'http://127.0.0.1:5178', 'http://localhost:5178')
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost', 'testserver')
    secure_cookies: bool = False
    allow_live_ingest: bool = False
    demo_mode: bool = False
    hardware_status_enabled: bool = False
    session_days: int = 7
    contact_phone: str | None = None
    direct_access: bool = False
    allow_device_resolve: bool = False


def authenticate(request, database, role='family'):
    if role == 'producer':
        value = request.headers.get('Authorization', '')
        raw = value[7:] if value.startswith('Bearer ') else ''
    else:
        raw = request.cookies.get(COOKIE, '')
    if not raw or len(raw) > 128:
        raise HTTPException(401, '尚未配对或凭证已失效')
    with database.lock, database.session() as session:
        credential = session.get(Credential, digest(raw))
        if (credential is None or credential.role != role or credential.revoked
                or credential.expires_at is not None and credential.expires_at <= now()):
            raise HTTPException(401, '尚未配对或凭证已失效')
        if role != 'producer':
            client = session.get(Client, credential.client_id)
            if client is None or client.revoked:
                raise HTTPException(401, '该手机的访问权限已撤销')
            return dict(id=client.id, label=client.label)
        return dict(id=credential.digest, label='authenticated_producer')


def voice_page(session, incident_id, before=None):
    voice_entry = func.json_type(SourceEvent.payload, '$.voice_entry')
    transcript = func.json_extract(SourceEvent.payload, '$.transcript')
    source = func.json_extract(SourceEvent.payload, '$.source')
    route = func.json_extract(SourceEvent.payload, '$.route')
    scope = [SourceEvent.incident_id == incident_id, or_(
        voice_entry == 'object', transcript != '',
        func.json_type(SourceEvent.payload, '$.thermal_frames') == 'array',
        and_(source == 'kws_live', route == 'emergency_help'),
    )]
    if before is not None:
        scope.append(SourceEvent.id < before)
    size = 20
    rows = list(session.scalars(select(SourceEvent).where(*scope)
                                .order_by(SourceEvent.id.desc()).limit(size + 1)))
    records = []
    for event in reversed(rows[:size]):
        payload = event.payload
        emergency_keyword = payload.get('source') == 'kws_live' and payload.get('route') == 'emergency_help'
        entry = dict(payload.get('voice_entry') or {})
        if not entry:
            if payload.get('thermal_frames'):
                entry = {'kind': 'thermal_candidate', 'risk': 'thermal_candidate'}
            else:
                entry = {'kind': 'utterance', 'text': payload.get('transcript') or ''}
        if emergency_keyword and not entry.get('risk'):
            entry['risk'] = 'emergency_help'
        if entry['kind'] == 'answer' and payload.get('interaction_state') == 'requested':
            entry['continues_round'] = True
        records.append(dict(entry, id=str(event.id), time=stamp(event.received_at)))
    return dict(voice_records=records, voice_before=rows[size - 1].id if len(rows) > size else None)


def incident_evidence(session, incident, source_events):
    evidence = voice_page(session, incident.id)
    if not source_events and incident.transcript:
        evidence['voice_records'].append(dict(id=incident.id, kind='utterance',
                                              text=incident.transcript, time=stamp(incident.received_at)))
    frames = next((event.payload['thermal_frames'] for event in reversed(source_events)
                   if event.payload.get('thermal_frames')), [])
    if len(source_events) == 100:
        oldest_frame = session.scalar(select(SourceEvent).where(
            SourceEvent.incident_id == incident.id,
            func.json_type(SourceEvent.payload, '$.thermal_frames') == 'array',
        ).order_by(SourceEvent.id).limit(1))
        frames = oldest_frame.payload['thermal_frames'] if oldest_frame else []
    return dict(**evidence, thermal_frames=frames)


def incident_view(session, incident, client_id=None, include_evidence=False):
    scope=Operation.incident_id==incident.id
    operations = list(reversed(list(session.scalars(select(Operation).where(scope)
                                     .order_by(Operation.id.desc()).limit(2000)))))
    def latest(kind, mine=False, current=False):
        query=select(Operation).where(scope,Operation.kind==kind)
        if mine: query=query.where(Operation.client_id==client_id)
        if current: query=query.where(Operation.state_version==incident.version)
        return session.scalar(query.order_by(Operation.id.desc()).limit(1))
    ack,review,request=latest('acknowledge'),latest('review'),latest('resolve_request')
    my_ack,my_review=latest('acknowledge',True,True),latest('review',True,True)
    source_events = list(session.scalars(select(SourceEvent).where(SourceEvent.incident_id == incident.id)
                                        .order_by(SourceEvent.id.desc()).limit(100)))
    first_source = source_events[-1] if source_events else None
    if len(source_events) == 100:
        first_source = session.scalar(select(SourceEvent).where(SourceEvent.incident_id == incident.id)
                                      .order_by(SourceEvent.id).limit(1))
    timeline = [dict(title='设备事件已记录', detail=event.payload.get('reason', ''),
                     time=stamp(event.received_at), source_event_id=event.source_event_id)
                for event in reversed(source_events)]
    titles = dict(received='网页已接收', acknowledge='家属已确认', review='核查说明已保存', resolve_request='解除申请已记录')
    for op in operations:
        client = session.get(Client, op.client_id)
        timeline.append(dict(title=titles.get(op.kind, op.kind),
                             detail=f'{client.label if client else "已撤销客户端"}：{op.payload.get("note", "")}',
                             time=stamp(op.created_at)))
    timeline.sort(key=lambda row: row['time'])
    evidence = incident_evidence(session, incident, source_events) if include_evidence else {}
    return dict(id=incident.id, device_id=incident.device_id, source_incident_id=incident.source_incident_id,
                version=incident.version, origin=incident.origin, boot_id=incident.boot_id,
                updated_sequence=session.scalar(select(func.max(Change.id)).where(Change.incident_id==incident.id)) or 0,
                title=incident.title, route=incident.route, reason=incident.reason,
                transcript=incident.transcript, source=incident.source, red_latched=incident.red_latched,
                trigger_source=(first_source.payload.get('source') if first_source else None) or incident.source,
                archived=incident.archived, occurred_at=incident.occurred_at,
                time_quality=incident.time_quality, received_at=stamp(incident.received_at),
                acknowledged=ack is not None, acknowledged_at=stamp(ack.created_at) if ack else None,
                receipt_count=session.scalar(select(func.count(func.distinct(Operation.client_id))).where(scope,Operation.kind=='received',Operation.state_version==incident.version)),
                review=review.payload if review else None, reviewed_at=stamp(review.created_at) if review else None,
                my_acknowledged=my_ack is not None, my_acknowledged_at=stamp(my_ack.created_at) if my_ack else None,
                my_review=my_review.payload if my_review else None, my_reviewed_at=stamp(my_review.created_at) if my_review else None,
                resolve_request=request.result if request else None, timeline=timeline,
                timeline_truncated=session.scalar(select(func.count()).select_from(Operation).where(scope))>2000
                    or session.scalar(select(func.count()).select_from(SourceEvent).where(SourceEvent.incident_id==incident.id))>100,
                **evidence)


def create_app(settings: Settings, database: Database | None = None):
    db = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app):
        db.check_revision()
        yield
        db.engine.dispose()

    app = FastAPI(title='K1 Family Portal', version='0.2.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database, app.state.settings = db, settings
    hardware_monitor = HardwareMonitor() if settings.hardware_status_enabled else None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    attempts = defaultdict(deque)
    family_role = 'direct_family' if settings.direct_access else 'family'

    def family(request):
        return authenticate(request, db, family_role)

    def limit_session_attempts(request):
        host = request.client.host if request.client else 'unknown'
        moment = time.monotonic()
        queue = attempts[host]
        while queue and queue[0] < moment - 60:
            queue.popleft()
        if len(queue) >= 5:
            raise HTTPException(429, '连接尝试过于频繁，请稍后重试')
        if len(attempts) > 500:
            attempts.clear()
            queue = attempts[host]
        queue.append(moment)

    def new_family_session(session, label):
        client = Client(label=label)
        session.add(client)
        session.flush()
        secret = secrets.token_urlsafe(32)
        session.add(Credential(digest=digest(secret), client_id=client.id, role=family_role,
                               expires_at=now() + timedelta(days=settings.session_days)))
        session.commit()
        result = JSONResponse(dict(client_id=client.id, label=client.label))
        result.set_cookie(COOKIE, secret, httponly=True, samesite='strict', secure=settings.secure_cookies,
                          max_age=settings.session_days * 86400, path='/api')
        return result

    @app.middleware('http')
    async def browser_boundary(request: Request, call_next):
        # SECURITY: same-origin cookie mutations; no wildcard CORS and no credentials in URLs.
        if request.url.path.startswith('/api/'):
            origin = request.headers.get('origin')
            if origin and origin not in settings.allowed_origins:
                return JSONResponse({'detail':'请求来源未授权'}, status_code=403)
            if request.method in ('POST','PUT','PATCH','DELETE'):
                if request.headers.get('content-type','').split(';')[0] != 'application/json':
                    return JSONResponse({'detail':'请求须为JSON'}, status_code=415)
                try:
                    length = int(request.headers.get('content-length','0'))
                except ValueError:
                    length = 0
                if not 0 < length <= 65536:
                    return JSONResponse({'detail':'请求为空或超过64KiB'}, status_code=413)
                if COOKIE in request.cookies and not origin:
                    return JSONResponse({'detail':'带会话的修改请求必须提供授权Origin'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        # Do not echo pairing secrets or health text into validation responses.
        return JSONResponse({'detail':'请求字段无效','fields':[dict(field='.'.join(map(str,e['loc'])),message=e['msg']) for e in exc.errors()]}, status_code=422)

    @app.get('/api/health')
    def health():
        return dict(status='ok', service='family_portal', protocol_version='family_event_0.1',
                    device_control_connected=False, live_ingest_enabled=settings.allow_live_ingest,
                    demo_mode=settings.demo_mode, direct_access=settings.direct_access)

    @app.get('/api/hardware-status')
    def hardware_status(request: Request):
        family(request)
        if hardware_monitor is None:
            raise HTTPException(404, '本服务未启用本地设备状态')
        return hardware_monitor.snapshot()

    @app.get('/api/thermal/live')
    def thermal_live(request: Request):
        family(request)
        frame = latest_thermal_frame() if settings.hardware_status_enabled and not settings.demo_mode else None
        return {'frame': frame}

    @app.post('/api/direct-session')
    def direct_session(request: Request):
        if not settings.direct_access:
            raise HTTPException(404, '此入口不可用')
        if request.headers.get('origin') not in settings.allowed_origins:
            raise HTTPException(403, '请求来源未授权')
        try:
            return family(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
        limit_session_attempts(request)
        with db.lock, db.session() as session:
            return new_family_session(session, '家属手机')

    @app.post('/api/pair')
    def pair(body: PairBody, request: Request):
        if settings.direct_access:
            raise HTTPException(404, '此入口不可用')
        limit_session_attempts(request)
        with db.lock, db.session() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            code = session.get(Pairing,digest(body.code))
            if code is None or code.consumed or code.expires_at <= now():
                raise HTTPException(401,'配对码无效、已使用或已过期')
            code.consumed=True
            return new_family_session(session, code.label)

    @app.get('/api/me')
    def me(request: Request):
        with db.lock:
            client = family(request)
            if settings.direct_access:
                return dict(client, contact_phone=settings.contact_phone)
            with db.session() as session:
                phone = None if settings.demo_mode else current_phone(session, settings.contact_phone)
        return dict(client, contact_phone=phone)

    @app.get('/api/contact')
    def contact(request: Request):
        with db.lock:
            client = family(request)
            if settings.demo_mode or settings.direct_access:
                raise HTTPException(404, '联系电话设置不可用')
            with db.session() as session:
                return contact_view(session, client['id'], settings.contact_phone)

    @app.post('/api/contact/propose')
    def propose_contact(body: ContactPhoneBody, request: Request):
        # SECURITY: authenticate, check roster and mutate under the same lock.
        with db.lock:
            client = family(request)
            if settings.demo_mode or settings.direct_access:
                raise HTTPException(404, '联系电话设置不可用')
            with db.session() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                if not other_clients(session, client['id']):
                    raise HTTPException(409, '需要至少一台其他已配对手机同意修改')
                if pending_change(session):
                    raise HTTPException(409, '已有号码修改申请，请先处理')
                if body.phone == current_phone(session, settings.contact_phone):
                    raise HTTPException(409, '新号码与当前号码相同')
                session.add(ContactPhoneChange(requester_id=client['id'], phone=body.phone))
                session.commit()
                return contact_view(session, client['id'], settings.contact_phone)

    @app.post('/api/contact/{change_id}/{action}')
    def operate_contact(change_id: int, action: Literal['approve', 'apply', 'cancel'], request: Request):
        with db.lock:
            client = family(request)
            if settings.demo_mode or settings.direct_access:
                raise HTTPException(404, '联系电话设置不可用')
            with db.session() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                change = pending_change(session)
                if not change or change.id != change_id:
                    raise HTTPException(409, '这条申请已结束，请刷新')
                client_id = client['id']
                required = other_clients(session, change.requester_id)
                if action == 'approve':
                    if client_id not in required:
                        raise HTTPException(403, '申请人不能同意自己的申请')
                    change.approvals = sorted(set(change.approvals) | {client_id})
                else:
                    if client_id != change.requester_id:
                        raise HTTPException(403, '仅申请人可应用或撤回')
                    if action == 'apply':
                        if not required or not required.issubset(change.approvals):
                            raise HTTPException(409, '还有其他已配对手机未同意')
                        change.applied_at = now()
                        session.add(Change(kind='contact_phone_changed'))
                    else:
                        change.cancelled_at = now()
                session.commit()
                return contact_view(session, client_id, settings.contact_phone)

    @app.post('/api/logout')
    def logout(request: Request):
        family(request)
        with db.lock, db.session() as session:
            credential=session.get(Credential,digest(request.cookies[COOKIE]))
            credential.revoked=True
            session.commit()
        response=JSONResponse(dict(logged_out=True))
        response.delete_cookie(COOKIE,path='/' if settings.demo_mode else '/api')
        return response

    @app.get('/api/incidents')
    def incidents(request: Request, offset: int=Query(0,ge=0,le=100000), limit: int=Query(50,ge=1,le=200),
                  live_only: bool=False):
        client=family(request)
        scope = (Incident.origin == 'live',) if live_only else ()
        with db.lock, db.session() as session:
            rows=list(session.scalars(select(Incident).where(*scope).order_by(Incident.received_at.desc(),Incident.id).offset(offset).limit(limit)))
            count=session.scalar(select(func.count()).select_from(Incident).where(*scope))
            return dict(items=[incident_view(session,x,client['id']) for x in rows],total=count,
                        next_offset=offset+len(rows) if offset+len(rows)<count else None)

    @app.get('/api/incidents/{incident_id}')
    def detail(incident_id: str, request: Request, live_only: bool=False):
        client=family(request)
        with db.lock, db.session() as session:
            row=session.get(Incident,incident_id)
            if row is None or (live_only and row.origin != 'live'): raise HTTPException(404,'事件不存在')
            return incident_view(session,row,client['id'],include_evidence=True)

    @app.get('/api/incidents/{incident_id}/voice')
    def older_voice(incident_id: str, request: Request, before: int = Query(..., gt=0, le=2**63-1),
                    live_only: bool = False):
        # SECURITY: transcript history uses the same family access check as incident detail.
        family(request)
        with db.lock, db.session() as session:
            row = session.get(Incident, incident_id)
            if row is None or (live_only and row.origin != 'live'):
                raise HTTPException(404, '事件不存在')
            return voice_page(session, row.id, before)

    @app.get('/api/devices')
    def devices(request: Request):
        family(request)
        with db.lock, db.session() as session:
            rows=list(session.scalars(select(Device)))
            return dict(items=[dict(device_id=x.device_id,origin=x.origin,boot_id=x.boot_id,
                received_at=stamp(x.received_at),online=x.origin=='live' and not x.source_gap and 0<=(now()-x.received_at).total_seconds()<=15,
                age_s=max(0,(now()-x.received_at).total_seconds()),sensor_status=x.sensor_status,
                interaction_state=x.interaction_state,source_gap=x.source_gap) for x in rows],
                freshness_threshold_s=15,device_control_connected=False)

    @app.get('/api/device/resolve')
    def next_resolution(request: Request, device_id: str = Query(max_length=100), boot_id: str = Query(max_length=100)):
        if not settings.allow_device_resolve or not request.client or request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(404, '板端解除入口不可用')
        authenticate(request, db, 'producer')
        with db.lock, db.session() as session:
            row = session.scalar(select(Operation).join(Incident, Operation.incident_id == Incident.id)
                .join(Client, Operation.client_id == Client.id)
                .where(Incident.device_id == device_id, Incident.boot_id == boot_id, Incident.origin == 'live',
                       Incident.red_latched.is_(True), Incident.archived.is_(False), Client.revoked.is_(False),
                       Operation.kind == 'resolve_request',
                       func.json_extract(Operation.result, '$.status') == 'pending_device_validation',
                       func.json_extract(Operation.result, '$.through_journal_id').is_not(None))
                .order_by(Operation.id.desc()).limit(1))
            if not row:
                return {'command': None}
            return {'command': {'operation_id': row.id, 'through_journal_id': row.result['through_journal_id']}}

    @app.post('/api/ingest')
    def ingest(body: EventBody, request: Request):
        authenticate(request,db,'producer')
        if body.resolution and (not request.client or request.client.host not in ('127.0.0.1', '::1')):
            raise HTTPException(403, '解除回执须由板端本机提交')
        # SECURITY: the unauthenticated-entry LAN demo cannot ingest real or
        # historical personal records, even with a producer credential.
        if settings.demo_mode and body.origin != 'synthetic':
            raise HTTPException(403,'演示服务只接受合成事件')
        if body.origin=='live' and not settings.allow_live_ingest:
            raise HTTPException(403,'离线开发实例未启用真实运行时接入')
        # Preserve the digest of pre-evidence payloads on retry after upgrade.
        absent_evidence = {name: True for name in ('voice_entry', 'thermal_frames', 'resolution') if getattr(body, name) is None}
        if body.voice_entry is not None:
            absent_evidence['voice_entry'] = {name for name in ('risk', 'follow_up')
                                              if getattr(body.voice_entry, name) is None}
        payload=body.model_dump(mode='json',exclude=absent_evidence)
        fingerprint=payload_digest(payload)
        with db.lock, db.session() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            duplicate=session.scalar(select(SourceEvent).where(SourceEvent.device_id==body.device_id,
                SourceEvent.origin==body.origin,SourceEvent.boot_id==body.boot_id,
                SourceEvent.source_event_id==body.source_event_id))
            if duplicate:
                if duplicate.digest!=fingerprint: raise HTTPException(409,'相同事件ID包含不同内容')
                return dict(duplicate=True,incident_id=duplicate.incident_id)
            device=session.scalar(select(Device).where(Device.device_id==body.device_id,Device.origin==body.origin))
            cursor=session.scalar(select(Cursor).where(Cursor.device_id==body.device_id,Cursor.origin==body.origin,Cursor.boot_id==body.boot_id))
            if device and device.boot_id!=body.boot_id and cursor:
                raise HTTPException(409,'拒绝旧开机实例覆盖当前设备状态')
            if cursor and (body.sequence<=cursor.sequence or body.state_version<=cursor.state_version):
                raise HTTPException(409,'源序号或状态版本没有递增')
            # SECURITY / integrity: a later contiguous message must not erase an
            # earlier missing range. Older messages cannot currently be backfilled.
            current_gap=body.sequence!=(cursor.sequence+1 if cursor else 1)
            source_gap=current_gap or bool(device and device.boot_id==body.boot_id and device.source_gap)
            if cursor is None:
                cursor=Cursor(device_id=body.device_id,origin=body.origin,boot_id=body.boot_id,
                              sequence=body.sequence,state_version=body.state_version)
                session.add(cursor)
            else:
                cursor.sequence,cursor.state_version=body.sequence,body.state_version
            if device is None:
                device=Device(device_id=body.device_id,origin=body.origin,boot_id=body.boot_id)
                session.add(device)
            device.boot_id,device.received_at=body.boot_id,now()
            device.sensor_status,device.interaction_state,device.source_gap=body.sensor_status,body.interaction_state,source_gap
            incident=None
            if body.kind=='incident':
                incident=session.scalar(select(Incident).where(Incident.device_id==body.device_id,
                    Incident.origin==body.origin,Incident.source_incident_id==body.incident_id))
                resolved = False
                if body.resolution:
                    operation = session.get(Operation, body.resolution.operation_id)
                    if (not settings.allow_device_resolve or not incident or not incident.red_latched
                            or incident.boot_id != body.boot_id or not operation
                            or operation.incident_id != incident.id or operation.kind != 'resolve_request'
                            or operation.result.get('status') != 'pending_device_validation'
                            or not operation.result.get('through_journal_id')):
                        raise HTTPException(409, '解除回执与原告警不匹配')
                    resolved = body.resolution.outcome == 'resolved'
                    operation.result = dict(operation.result, status=body.resolution.outcome,
                                            red_latched=not resolved, device_command_sent=True,
                                            completed_at=stamp(now()))
                red_reason = body.red_latched and body.route in ('emergency_help', 'fall_or_injury')
                if incident is None:
                    incident=Incident(device_id=body.device_id,source_incident_id=body.incident_id,
                                      origin=body.origin,boot_id=body.boot_id)
                    session.add(incident)
                    was_latched=False
                    incident.version=1
                else:
                    was_latched=incident.red_latched
                    if not was_latched or red_reason or body.voice_entry or body.thermal_frames or body.resolution:
                        incident.version+=1
                # SECURITY: clearing requires a receipt for this incident's authenticated request.
                if not was_latched or (red_reason and incident.route not in ('emergency_help', 'fall_or_injury')):
                    incident.title,incident.route=body.title,body.route
                    if incident.version == 1 or body.reason:
                        incident.reason=body.reason
                    incident.transcript,incident.source=body.transcript,body.source
                incident.red_latched=(was_latched or body.red_latched) and not resolved
                incident.archived=body.archived and not incident.red_latched
                incident.boot_id,incident.received_at=body.boot_id,now()
                incident.occurred_at=body.occurred_at.isoformat() if body.occurred_at else None
                incident.time_quality=body.time_quality
                session.flush()
            session.add(SourceEvent(device_id=body.device_id,origin=body.origin,boot_id=body.boot_id,
                sequence=body.sequence,source_event_id=body.source_event_id,digest=fingerprint,payload=payload,
                incident_id=incident.id if incident else None))
            session.add(Change(kind='source_event',incident_id=incident.id if incident else None))
            session.commit()
            return dict(duplicate=False,incident_id=incident.id if incident else None,source_gap=source_gap)

    @app.post('/api/incidents/{incident_id}/{kind}')
    def operate(incident_id: str, kind: str, body: OperationBody, request: Request):
        client=family(request)
        if kind not in ('received','acknowledge','review','resolve_request'):
            raise HTTPException(404,'操作不存在')
        payload=body.model_dump()
        fingerprint=payload_digest(dict(incident_id=incident_id,kind=kind,**payload))
        with db.lock, db.session() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            previous=session.scalar(select(Operation).where(Operation.client_id==client['id'],Operation.request_id==body.request_id))
            if previous:
                if previous.digest!=fingerprint: raise HTTPException(409,'重复请求ID对应不同操作')
                return previous.result
            row=session.get(Incident,incident_id)
            if row is None: raise HTTPException(404,'事件不存在')
            if row.version!=body.expected_version: raise HTTPException(409,'事件已更新，请刷新后核查')
            # SECURITY: re-check client revocation inside the serialized write transaction.
            current_client=session.get(Client,client['id'])
            if current_client is None or current_client.revoked: raise HTTPException(401,'当前手机权限已撤销')
            def prior(operation_kind):
                return session.scalar(select(Operation).where(Operation.incident_id==incident_id,
                    Operation.client_id==client['id'],Operation.kind==operation_kind,
                    Operation.state_version==row.version).order_by(Operation.id.desc()).limit(1))
            if kind in ('review','resolve_request'):
                if prior('acknowledge') is None:
                    raise HTTPException(409,'请先由当前手机确认收到')
                if not body.note or not body.human_checked:
                    raise HTTPException(422,'需填写核查说明并明确勾选已完成人工核查')
            through_journal_id = None
            if kind=='resolve_request':
                if row.origin!='live' or not row.red_latched or row.archived:
                    raise HTTPException(409,'仅当前真实锁存告警可提交解除申请；演示和历史不能操作设备')
                if prior('review') is None:
                    raise HTTPException(409,'请先保存当前版本的核查说明')
                if settings.allow_device_resolve:
                    event = session.scalar(select(SourceEvent).where(SourceEvent.incident_id == row.id,
                        SourceEvent.boot_id == row.boot_id).order_by(SourceEvent.id.desc()).limit(1))
                    prefix = 'voice.' + row.device_id.removeprefix('k1_voice.') + '.'
                    suffix = event.source_event_id.removeprefix(prefix) if event else ''
                    if (not row.device_id.startswith('k1_voice.') or not event
                            or not event.source_event_id.startswith(prefix)
                            or not suffix.isascii() or not suffix.isdecimal() or int(suffix) <= 0):
                        raise HTTPException(409, '此告警缺少可解除的板端记录')
                    through_journal_id = int(suffix)
                    already_pending = session.scalar(select(Operation).where(Operation.incident_id == row.id,
                        Operation.kind == kind, Operation.state_version == row.version,
                        func.json_extract(Operation.result, '$.status') == 'pending_device_validation',
                        func.json_extract(Operation.result, '$.through_journal_id').is_not(None)).limit(1))
                    if already_pending:
                        return already_pending.result
            if kind in ('received','acknowledge'):
                existing=prior(kind)
                if existing: return existing.result
            result=dict(recorded=True,kind=kind,incident_id=row.id,version=row.version,
                        red_latched=row.red_latched,recorded_at=stamp(now()),actor=client['label'],
                        status='pending_device_validation' if kind=='resolve_request' else 'recorded',
                        device_command_sent=False)
            if through_journal_id:
                result['through_journal_id'] = through_journal_id
            session.add(Operation(client_id=client['id'],request_id=body.request_id,incident_id=row.id,state_version=row.version,
                kind=kind,digest=fingerprint,payload=payload,result=result))
            session.add(Change(kind=kind,incident_id=row.id))
            session.commit()
            return result

    def changes_after(after, limit=200):
        with db.lock, db.session() as session:
            changes=list(session.scalars(select(Change).where(Change.id>after).order_by(Change.id).limit(limit)))
            return [dict(sequence=x.id,kind=x.kind,incident_id=x.incident_id,time=stamp(x.created_at)) for x in changes]

    @app.get('/api/changes')
    def changes(request: Request, after: int=Query(0,ge=0,le=2**63-1), limit: int=Query(200,ge=1,le=500)):
        family(request)
        items=changes_after(after,limit)
        return dict(items=items,cursor=items[-1]['sequence'] if items else after)

    @app.get('/api/stream')
    async def stream(request: Request, after: int=Query(0,ge=0,le=2**63-1)):
        family(request)
        try:
            cursor=max(after,int(request.headers.get('last-event-id','0')))
        except ValueError:
            raise HTTPException(400,'补发游标无效')
        if not 0<=cursor<=2**63-1: raise HTTPException(400,'补发游标无效')

        async def generate():
            nonlocal cursor
            keepalive=time.monotonic()
            while not await request.is_disconnected():
                try:
                    family(request)
                except HTTPException:
                    yield 'event: session_expired\ndata: {}\n\n'
                    return
                items=changes_after(cursor)
                for item in items:
                    yield f'id: {item["sequence"]}\ndata: {json.dumps(item,ensure_ascii=False)}\n\n'
                    cursor=item['sequence']
                if time.monotonic()-keepalive>10:
                    yield ': keepalive\n\n'
                    keepalive=time.monotonic()
                await asyncio.sleep(0.5 if not items else 0)
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    if (settings.frontend/'assets').is_dir():
        app.mount('/assets',StaticFiles(directory=settings.frontend/'assets',follow_symlink=False),name='assets')

    @app.get('/',response_class=HTMLResponse)
    def index(request: Request):
        path=settings.frontend/'index.html'
        if not path.is_file():
            raise HTTPException(503,'前端尚未构建，请先在frontend运行npm run build')
        page=path.read_text(encoding='utf-8').replace('</head>','<meta name="family-api" content="same-origin"></head>')
        if settings.direct_access:
            page = page.replace('</head>', '<meta name="family-access" content="direct"></head>')
        if settings.allow_device_resolve:
            page = page.replace('</head>', '<meta name="family-resolution" content="enabled"></head>')
        if not settings.demo_mode:
            return page
        page=page.replace('</head>','<meta name="family-demo" content="synthetic-events"></head>')
        page=page.replace('<body>', '<body><div class="lan-demo-notice" role="status">局域网演示：事件为模拟；设备页为实时检测。请勿填写个人信息。</div>', 1)
        response=HTMLResponse(page)
        try:
            authenticate(request,db)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            secret=secrets.token_urlsafe(32)
            with db.lock, db.session() as session:
                client=Client(label='体验手机')
                session.add(client)
                session.flush()
                session.add(Credential(digest=digest(secret),client_id=client.id,role='family',
                                       expires_at=now()+timedelta(hours=2)))
                session.commit()
            response.set_cookie(COOKIE,secret,httponly=True,secure=False,samesite='strict',
                                max_age=7200,path='/')
        return response

    return app
