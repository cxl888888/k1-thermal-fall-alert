"""Local operator commands. Pairing/producer secrets are printed once, never logged."""
import argparse
from datetime import timedelta
import ipaddress
import os
from pathlib import Path
import re
import secrets

from sqlalchemy import select

from .app import Settings, create_app, digest
from .db import Client, Credential, Database, Device, Incident, Pairing, SourceEvent, now, stamp


def database_url(path):
    return 'sqlite:///' + str(path.expanduser().resolve()).replace('\\','/')


def server_configuration(db_url, host, port, cert=None, key=None, allow_live_ingest=False,
                         demo_mode=False, contact_phone=None, direct_access=False, *,
                         allow_device_resolve=False, trusted_lan_live=False):
    """Build an explicit local or private-LAN listener without weakening cookies."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError('服务地址须为明确的IPv4地址') from exc
    if not isinstance(address, ipaddress.IPv4Address) or address.is_unspecified:
        raise ValueError('服务地址须为明确的IPv4地址，不能监听所有网卡')
    private_networks = tuple(ipaddress.ip_network(x) for x in
                             ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not address.is_loopback and not any(address in subnet for subnet in private_networks):
        raise ValueError('局域网模式仅允许指定RFC1918私有IPv4地址')
    if bool(cert) != bool(key):
        raise ValueError('TLS证书和私钥必须同时提供')
    if demo_mode and (cert or allow_live_ingest or direct_access):
        raise ValueError('免证书演示不能启用真实事件接入或混用TLS配置')
    if trusted_lan_live and (address.is_loopback or cert or demo_mode or not direct_access or not allow_live_ingest):
        raise ValueError('可信局域网演示需显式指定私有地址、免配对和真实事件接入')
    if allow_device_resolve and (not allow_live_ingest or demo_mode or cert or port != 8795
                                 or not (address.is_loopback or trusted_lan_live)):
        raise ValueError('设备解除需使用 8795 的真实事件入口和本机 HTTP 控制通道')
    if not address.is_loopback and not cert and not demo_mode and not trusted_lan_live:
        raise ValueError('局域网访问需要HTTPS证书和私钥')
    cert_path = Path(cert).expanduser().resolve() if cert else None
    key_path = Path(key).expanduser().resolve() if key else None
    if cert_path and (not cert_path.is_file() or not key_path.is_file()):
        raise ValueError('TLS证书或私钥文件不存在')
    # SECURITY: do not expose a local server key to other OS accounts.
    if key_path and os.name != 'nt' and key_path.stat().st_mode & 0o077:
        raise ValueError('TLS私钥权限过宽，请仅允许所有者访问')
    scheme = 'https' if cert_path else 'http'
    origins = (f'{scheme}://{host}:{port}',)
    if address.is_loopback and not cert_path:
        origins += (f'http://localhost:{port}', 'http://127.0.0.1:5178', 'http://localhost:5178')
    contact_phone = None if demo_mode else (contact_phone or '').strip() or None
    if contact_phone and not re.fullmatch(r'\+?[0-9]{3,20}', contact_phone):
        raise ValueError('K1_FAMILY_CONTACT_PHONE 须为数字号码，可带国际区号前缀 +')
    settings = Settings(database_url=db_url, allowed_origins=origins,
                        allowed_hosts=(host, 'localhost', '127.0.0.1'),
                        secure_cookies=bool(cert_path), allow_live_ingest=allow_live_ingest,
                        demo_mode=demo_mode, hardware_status_enabled=demo_mode or allow_live_ingest,
                        contact_phone=contact_phone, direct_access=direct_access,
                        allow_device_resolve=allow_device_resolve)
    return settings, dict(host=host, port=port, workers=1, access_log=False,
                          proxy_headers=False, ssl_certfile=str(cert_path) if cert_path else None,
                          ssl_keyfile=str(key_path) if key_path else None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=Path(__file__).resolve().parents[2]/'data'/'family.db')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init-db')
    pair=commands.add_parser('pair-code')
    pair.add_argument('--label',required=True,help='Operator-assigned phone label, not a verified person identity')
    commands.add_parser('producer-token')
    commands.add_parser('list-clients')
    revoke=commands.add_parser('revoke-client');revoke.add_argument('client_id')
    serve=commands.add_parser('serve')
    serve.add_argument('--port',type=int,default=8794)
    serve.add_argument('--host',default='127.0.0.1',help='Explicit IPv4; private LAN requires TLS')
    serve.add_argument('--tls-cert',type=Path,help='PEM certificate; phone must trust its issuer')
    serve.add_argument('--tls-key',type=Path,help='PEM private key; owner-only on Linux')
    serve.add_argument('--demo-http',action='store_true',help='LAN demo only: isolated demo DB, synthetic events, automatic guest sessions')
    serve.add_argument('--direct-access',action='store_true',help='Prototype access on a trusted private network without a pairing code')
    serve.add_argument('--allow-live-ingest',action='store_true',help='Explicit runtime-adapter enablement; never enables device commands')
    serve.add_argument('--trusted-lan-live',action='store_true',
                       help='Live HTTP demo on an explicitly trusted private LAN; no pairing or TLS')
    serve.add_argument('--allow-device-resolve',action='store_true',
                       help='Enable device-confirmed alert resolution on the local 8795 control channel')
    args=parser.parse_args()
    if not 1<=getattr(args,'port',8794)<=65535:
        parser.error('端口范围无效')
    if args.command == 'serve' and args.demo_http:
        demo_root = (Path(__file__).resolve().parents[2] / 'data' / 'demo').resolve()
        if not args.db.expanduser().resolve().is_relative_to(demo_root):
            parser.error('免证书演示仅允许使用family_portal/data/demo内的独立数据库')
    args.db.parent.mkdir(parents=True,exist_ok=True)
    if os.name!='nt': args.db.parent.chmod(0o700)
    db=Database(database_url(args.db))
    if args.command=='init-db':
        db.initialize_fresh()
        if os.name!='nt': args.db.chmod(0o600)
        print('空库初始化完成；已有数据库升级请使用Alembic。')
        return
    db.check_revision()
    if args.command=='serve':
        import uvicorn
        if args.demo_http:
            with db.session() as session:
                for model in (Device, Incident, SourceEvent):
                    if session.scalar(select(model.id).where(model.origin != 'synthetic').limit(1)) is not None:
                        parser.error('演示数据库含有非合成数据，拒绝开放')
        try:
            settings, server_options = server_configuration(database_url(args.db), args.host, args.port,
                args.tls_cert, args.tls_key, args.allow_live_ingest, args.demo_http,
                contact_phone=os.environ.get('K1_FAMILY_CONTACT_PHONE'), direct_access=args.direct_access,
                trusted_lan_live=args.trusted_lan_live, allow_device_resolve=args.allow_device_resolve)
        except ValueError as exc:
            parser.error(str(exc))
        app = create_app(settings, db)
        if args.trusted_lan_live:
            import socket
            # SECURITY: only bind the selected LAN address and the local control address.
            with socket.create_server((args.host, args.port)) as lan, \
                    socket.create_server(('127.0.0.1', args.port)) as local:
                uvicorn.Server(uvicorn.Config(app, **server_options)).run(sockets=[lan, local])
        else:
            uvicorn.run(app, **server_options)
        return
    with db.lock,db.session() as session:
        if args.command=='pair-code':
            label=args.label.strip()
            if not 1<=len(label)<=80: parser.error('手机标签需为1至80字')
            code=secrets.token_urlsafe(32)
            expiry=now()+timedelta(minutes=10)
            session.add(Pairing(digest=digest(code),label=label,expires_at=expiry))
            session.commit()
            print(f'一次性配对码（10分钟内有效，仅提供给对应手机）：\n{code}\nUTC到期时间：{stamp(expiry)}')
        elif args.command=='producer-token':
            secret=secrets.token_urlsafe(32)
            session.add(Credential(digest=digest(secret),role='producer',expires_at=now()+timedelta(days=30)))
            session.commit()
            print('事件生产者凭证（30天，仅在本机运行时保存，不放入前端）：\n'+secret)
        elif args.command=='list-clients':
            for client in session.scalars(select(Client).order_by(Client.created_at)):
                print(client.id,client.label,'revoked' if client.revoked else 'active')
        elif args.command=='revoke-client':
            client=session.get(Client,args.client_id)
            if client is None: parser.error('没有该手机客户端')
            client.revoked=True
            session.commit()
            print('该手机的全部会话已撤销；其他手机不受影响。')


if __name__=='__main__':
    main()
