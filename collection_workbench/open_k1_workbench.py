"""Open/reuse the local K1 workbench. Opening never starts a sensor."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from storage import BASE, atomic_json


def desktop_environment():
    env = os.environ.copy()
    allowed = ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'XAUTHORITY')
    result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', 'lxqt-session'], capture_output=True, text=True)
    for pid in result.stdout.split():
        try:
            fields = (Path('/proc') / pid / 'environ').read_bytes().split(b'\0')
            desktop = dict(part.split(b'=', 1) for part in fields if b'=' in part)
            for key in allowed:
                if key.encode() in desktop:
                    env[key] = desktop[key.encode()].decode('utf-8')
            break
        except (OSError, ValueError, UnicodeError):
            continue
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + env['XDG_RUNTIME_DIR'] + '/bus')
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', action='store_true', help='Prefill temporary codes for a manual short trial')
    parser.add_argument('--stop', action='store_true', help='Gracefully stop only this tool\'s recorded server')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('This entry is for the K1. Use launch_pc.py on Windows.')
    url = 'http://127.0.0.1:8791/'
    pid_file = BASE / 'server.pid'
    if args.stop:
        pid = int(pid_file.read_text())
        command = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
        if str(BASE / 'app.py').encode() not in command or b'--capture' not in command:
            raise RuntimeError('记录的PID不属于本工具，已保留该进程')
        os.kill(pid, signal.SIGTERM)
        print('已请求本工具保存并退出。')
        return
    service_started = False
    try:
        with socket.create_connection(('127.0.0.1', 8791), timeout=1):
            pass
        pid = int(pid_file.read_text())
        command = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
        if str(BASE / 'app.py').encode() not in command or b'--capture' not in command:
            raise RuntimeError('8791端口已有其他服务；保留现状，请核对后再启动')
    except (ConnectionRefusedError, TimeoutError):
        with (BASE / 'server.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-u', str(BASE / 'app.py'), '--capture'],
                cwd=BASE, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        pid = process.pid
        pid_file.write_text(str(pid), encoding='ascii')
        service_started = True
    ready = None
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                page = response.read().decode('utf-8')
            match = re.search(r'<meta name="workbench-token" content="([0-9a-f]+)">', page)
            if not match:
                raise RuntimeError('本地端口返回了其他页面，请保留现状并检查')
            request = Request(url + 'api/capture', headers={'X-Workbench-Token': match[1]})
            with urlopen(request, timeout=2) as response:
                ready = json.load(response)
            break
        except (OSError, ValueError):
            time.sleep(.2)
    if ready is None:
        raise RuntimeError('界面服务尚未就绪，请查看 server.log')
    env = desktop_environment()
    if args.trial:
        url += '?' + urlencode(dict(scene='touch_ui_trial', participant='operator_p01',
                                    annotator_id='operator_a01', duration_minutes='2'))
    browser = ['/usr/bin/chromium-browser', '--no-first-run', '--no-default-browser-check',
               '--disable-background-networking', '--disable-component-update', '--start-maximized',
               '--user-data-dir=' + str(BASE / '.browser-profile'), '--app=' + url]
    if env.get('WAYLAND_DISPLAY'):
        browser.append('--ozone-platform=wayland')
    with (BASE / 'browser.log').open('ab') as log:
        window = subprocess.Popen(browser, cwd=BASE, env=env, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    result = dict(server_pid=pid, server_started=service_started, browser_launcher_pid=window.pid,
                  service_state=ready['state'], capture_active=ready['state'] in ('starting', 'recording', 'stopping'),
                  url='http://127.0.0.1:8791/', temporary_trial_codes=args.trial,
                  note='Browser opening does not start thermal or radar acquisition.')
    atomic_json(BASE / 'startup_status.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
