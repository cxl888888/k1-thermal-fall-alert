"""Run the family portal on a PC with a separate, empty database."""
import argparse
import ipaddress
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from portal.app import Settings, create_app
from portal.cli import database_url
from portal.db import Database


def main():
    parser = argparse.ArgumentParser(description='在 PC 上启动家属端')
    parser.add_argument('--host', type=ipaddress.IPv4Address, default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8793)
    args = parser.parse_args()
    private_networks = tuple(ipaddress.ip_network(block) for block in
                             ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not args.host.is_loopback and not any(args.host in subnet for subnet in private_networks):
        parser.error('请指定本机回环地址或 Wi-Fi 的私有 IPv4 地址')
    if not 1024 <= args.port <= 65535:
        parser.error('端口范围为 1024 至 65535')
    host = str(args.host)
    address = f'http://{host}:{args.port}'
    with TemporaryDirectory(prefix='k1-family-pc-') as directory:
        url = database_url(Path(directory) / 'family.db')
        db = Database(url)
        try:
            db.initialize_fresh()
            settings = Settings(database_url=url, allowed_hosts=(host,), allowed_origins=(address,),
                                direct_access=True)
            print(f'访问地址 {address}\n按 Ctrl+C 停止。', flush=True)
            uvicorn.run(create_app(settings, db), host=host, port=args.port, access_log=False,
                        proxy_headers=False, timeout_graceful_shutdown=3)
        finally:
            db.engine.dispose()


if __name__ == '__main__':
    main()
