"""Local-only touchscreen collection and PC annotation workbench (Python 3.12+)."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from capture import Capture
from analysis import frame_features, quality_summary
from event_preview import EventPreview
from storage import BASE, LABEL_SCHEMA, Store
from targeted import TARGETED_SCHEMA
from preview import Preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, action='append', help='Original session or parent; repeatable')
    parser.add_argument('--derived-root', type=Path, default=BASE / 'data' / 'reviewed')
    parser.add_argument('--voice-record', type=Path, action='append', default=[], help='Read-only voice state/journal snapshot; repeatable')
    parser.add_argument('--capture', action='store_true', help='Enable explicit real thermal capture on K1')
    parser.add_argument('--capture-output', type=Path, default=Path.home() / 'k1-collection' / 'sessions')
    bundled_driver = BASE / 'drivers/show_thermal_calibrated.py'
    parser.add_argument('--thermal-script', type=Path, default=bundled_driver if bundled_driver.is_file()
                        else Path.home() / 'k1-thermal/show_thermal_calibrated.py')
    parser.add_argument('--radar-port', default='/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0',
                        help='Confirmed UART path; opened only when the session explicitly enables radar')
    parser.add_argument('--radar-baud', type=int, default=260000, help='Host receive setting; no radar configuration write')
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--open', action='store_true', help='Open local browser; never starts collection')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be in 1024..65535')
    if args.capture and sys.platform != 'linux':
        parser.error('Real acquisition is available only on Linux/K1; omit --capture on the PC')
    roots = args.data_root or [BASE / 'data' / 'raw']
    if args.capture and args.capture_output not in roots:
        roots.append(args.capture_output)
    store = Store(roots, args.derived_root)
    event_preview = EventPreview(args.voice_record)
    if not 9600 <= args.radar_baud <= 3000000:
        parser.error('radar-baud outside supported diagnostic range')
    capture = Capture(args.capture, args.capture_output, args.thermal_script,
                      args.radar_port if args.capture else None, args.radar_baud)
    capture.discard_callback=store.recycle_folder
    preview=Preview(args.capture,args.thermal_script)
    lifecycle=threading.RLock()
    def capture_view():return dict(capture.state(),preview=preview.state())
    def require_inactive(key):
        session=store.get(key);live=capture.state()
        if live['state'] in ('starting','recording','stopping') and live['session_id']==session.sid:
            raise ValueError('本数据集仍在录制或收尾，请先结束或取消采集')
        return session
    token = secrets.token_hex(32)
    # SECURITY: loopback only. Host + Origin + per-process CSRF token guard local mutations.
    # This tool is not an authenticated LAN/public service. No --host override is provided.
    hosts = {f'127.0.0.1:{args.port}', f'localhost:{args.port}'}
    origins = {'http://' + host for host in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def reply(self, value, status=200, content_type='application/json; charset=utf-8', extra=None):
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            elif isinstance(value, str):
                value = value.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(value)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(value)

        def guard(self, api=False, mutate=False):
            if self.headers.get('Host') not in hosts:
                self.reply({'error': '仅允许本机地址访问'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in origins:
                self.reply({'error': '不允许跨站访问'}, 403)
                return False
            if api and not hmac.compare_digest(self.headers.get('X-Workbench-Token', ''), token):
                self.reply({'error': '页面授权已过期，请刷新本机页面'}, 403)
                return False
            if mutate and self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.reply({'error': '请求格式须为 JSON'}, 415)
                return False
            return True

        def do_GET(self):
            route = urlsplit(self.path)
            api = route.path.startswith('/api/')
            if not self.guard(api=api):
                return
            query = parse_qs(route.query)
            key = query.get('key', [''])[0]
            try:
                if route.path == '/':
                    page = (BASE / 'web' / 'index.html').read_text(encoding='utf-8')
                    return self.reply(page.replace('__LOCAL_TOKEN__', token), content_type='text/html; charset=utf-8')
                assets = {'/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if route.path in assets:
                    name, mime = assets[route.path]
                    return self.reply((BASE / 'web' / name).read_bytes(), content_type=mime)
                if route.path == '/api/config':
                    return self.reply(dict(label_schema=LABEL_SCHEMA, capture_enabled=args.capture,
                                           targeted_schema=TARGETED_SCHEMA,
                                           derived_root=str(args.derived_root.resolve()),
                                           original_files_read_only=True, radar_mode='not_connected',
                                           radar_configurable=args.capture, radar_host_baud=args.radar_baud))
                if route.path == '/api/sessions':
                    return self.reply(store.scan())
                if route.path == '/api/trash':
                    return self.reply(store.trash.list())
                if route.path == '/api/session':
                    return self.reply(store.details(key))
                if route.path == '/api/frame':
                    return self.reply(store.get(key).frame(int(query.get('n', ['-1'])[0])))
                if route.path == '/api/quality':
                    details = store.details(key)
                    return self.reply(quality_summary(store.get(key), details['labels']))
                if route.path == '/api/features':
                    row = store.get(key).frame(int(query.get('n', ['-1'])[0]))
                    if not row['relative']['valid']:
                        row = dict(row, valid=False)
                    return self.reply(frame_features(row, float(query.get('delta_c', ['2'])[0])))
                if route.path == '/api/radar':
                    return self.reply(store.get(key).radar_at_frame(int(query.get('n', ['-1'])[0])))
                if route.path == '/api/fusion':
                    from fusion import analyze_session
                    return self.reply(analyze_session(store.get(key)))
                if route.path == '/api/revision-download':
                    blob = store.download_revision(key, query.get('revision', [''])[0])
                    return self.reply(blob, content_type='application/zip',
                                      extra={'Content-Disposition': 'attachment; filename="label_revision.zip"'})
                if route.path == '/api/capture':
                    return self.reply(capture_view())
                if route.path == '/api/events-preview':
                    return self.reply(event_preview.snapshot())
                if route.path.startswith('/api/download/'):
                    blob = store.download(route.path.removeprefix('/api/download/'))
                    return self.reply(blob, content_type='application/zip',
                                      extra={'Content-Disposition': 'attachment; filename="label_revision.zip"'})
                return self.reply({'error': '入口不存在'}, 404)
            except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
                return self.reply({'error': str(exc)}, 400)

        def do_POST(self):
            if not self.guard(api=True, mutate=True):
                return
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError('请求为空或过大')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('请求须为对象')
                if route.path == '/api/review':
                    return self.reply(store.save_review(query.get('key', [''])[0], data))
                if route.path == '/api/session/name':
                    return self.reply(store.rename(query.get('key',[''])[0],data))
                if route.path == '/api/session/trash':
                    with lifecycle:
                        key=query.get('key',[''])[0];require_inactive(key)
                        return self.reply(store.recycle(key,data))
                if route.path == '/api/trash/restore':
                    with lifecycle:return self.reply(store.trash.restore(data.get('trash_id')))
                if route.path in ('/api/preview/start', '/api/preview/fall-test'):
                    with lifecycle:
                        if capture.state()['state'] in ('starting','recording','stopping'):
                            raise ValueError('请先结束或取消当前采集，再查看不保存的预览')
                        result = preview.start(fall_test=True) if route.path.endswith('/fall-test') else preview.start()
                        return self.reply(result)
                if route.path == '/api/preview/stop':
                    with lifecycle:return self.reply(preview.stop())
                if route.path == '/api/events-rehearse':
                    return self.reply(event_preview.act(data))
                if route.path == '/api/recover':
                    key = query.get('key', [''])[0]
                    session = store.get(key)
                    live = capture.state()
                    if live['state'] in ('starting', 'recording', 'stopping') and live['folder'] and Path(live['folder']).resolve() == session.path:
                        raise ValueError('本工具仍在采集该会话，请先正常停止并等待保存完成')
                    return self.reply(store.recover(key, data))
                if route.path == '/api/capture/start':
                    with lifecycle:
                        if preview.active():raise ValueError('请先关闭仅预览，再开始正式采集')
                        result=capture.start(data);preview.reset()
                        return self.reply(result)
                if route.path.startswith('/api/capture/'):
                    return self.reply(capture.command(route.path.removeprefix('/api/capture/'), data))
                return self.reply({'error': '操作不存在'}, 404)
            except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
                return self.reply({'error': str(exc)}, 400)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    url = f'http://127.0.0.1:{args.port}/'
    print('采集与标注工作台：' + url, flush=True)
    print('模式：' + ('K1 热像预览、采集与回放；雷达按本场勾选启用' if args.capture else 'PC 离线回放；不访问传感器'), flush=True)
    print('关闭浏览器不会停止采集；请点击“结束采集并保存”。Ctrl+C 退出服务。', flush=True)
    if args.open:
        webbrowser.open(url)
    def interrupt(_number, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print('\n正在保存并退出……', flush=True)
    finally:
        try:
            preview.shutdown()
            capture.shutdown()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
