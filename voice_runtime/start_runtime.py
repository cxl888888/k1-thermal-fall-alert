"""Owned local server + voice application launcher; explicit Ollama rollback.

No system service changes. A file lock prevents duplicate native launches;
occupied ports are refused, never taken over. Only child processes created by
this launcher are stopped. The existing application state DB is not reset.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent


def stop_child(child, graceful=False):
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT if graceful else signal.SIGTERM)
    try:
        child.wait(timeout=40 if graceful else 10)
    except subprocess.TimeoutExpired:
        # This process owns the child's separate session/group.
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--runtime-config', type=Path, default=HERE/'runtime_candidates.json')
    p.add_argument('--llm-backend', choices=('ollama', 'llamacpp'))
    p.add_argument('--llm-api-base')
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--verify-model', action='store_true')
    p.add_argument('--text-only', action='store_true')
    args, extra = p.parse_known_args()
    config = json.loads(args.runtime_config.read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('Unsupported runtime config')
    backend = args.llm_backend or config['default_backend']
    app_path = HERE/'main.py'
    if not app_path.exists():
        app_path = HERE/'app/main.py'
    item = config['variants'][config['selected_variant']]
    expected_base = 'http://127.0.0.1:' + str(item['port']) + '/v1'
    api_base = args.llm_api_base or (expected_base if backend == 'llamacpp' else 'http://127.0.0.1:11434/v1')
    mode = [] if args.text_only else ['--listen', '--speak']
    app_cmd = [sys.executable, '-B', '-u', str(app_path), *mode,
               '--llm-backend', backend, '--llm-api-base', api_base,
               '--silence-ms', str(config['silence_ms']), *extra]
    if '--help' in extra or '-h' in extra:
        return subprocess.call(app_cmd)
    if '--audio-check' in extra:
        os.execv(sys.executable, app_cmd)
    if backend == 'ollama':
        if args.prepare_only:
            print(json.dumps(dict(backend=backend, app_command=app_cmd)), flush=True)
            return 0
        os.execv(sys.executable, app_cmd)
    if api_base != expected_base:
        raise ValueError('Native launcher only manages the configured loopback endpoint; use main.py for custom servers')
    runtime = HERE/'runtime'
    runtime.mkdir(exist_ok=True)
    lock = (runtime/'native_runtime.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('已有原生语音运行实例；不会停止或替换它。')
    model = Path(item['path']).expanduser()
    st = model.stat()
    if st.st_size != item['bytes']:
        raise ValueError('Native model size differs from verified config')
    stamp = runtime/'native_model_verified.json'
    identity = dict(path=str(model), bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                    sha256=item['sha256'])
    try:
        cached = json.loads(stamp.read_text())
    except (OSError, ValueError):
        cached = None
    if args.verify_model or cached != identity:
        print('[初始化] 核对原生模型SHA256；后续启动复用路径/大小/修改时间相同的校验记录。', flush=True)
        with model.open('rb') as f:
            actual = hashlib.file_digest(f, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise ValueError('Native model checksum failed')
        tmp = stamp.with_suffix('.tmp')
        tmp.write_text(json.dumps(identity, ensure_ascii=False))
        tmp.replace(stamp)
    build = Path(config['native_build']).expanduser()
    cmd = [str(build/'build/bin/llama-server'), '-m', str(model), '--host',
           '127.0.0.1', '--port', str(item['port'])] + config['native_arguments'] + item.get('extra_arguments', [])
    if args.prepare_only:
        print(json.dumps(dict(backend=backend, variant=config['selected_variant'],
            model_identity=identity, server_command=cmd, app_command=app_cmd), ensure_ascii=False), flush=True)
        return 0
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1', item['port'])) == 0:
            raise SystemExit('原生端口已占用；不会接管未知服务。')
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = str(build/'build/bin') + ':' + str(build/'tools/usr/lib/riscv64-linux-gnu')
    server = app = None
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        with (runtime/'native_server.log').open('ab', buffering=0) as f:
            server = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        print('[初始化] 原生后端启动：' + config['selected_variant'], flush=True)
        for _ in range(90):
            if server.poll() is not None:
                raise RuntimeError('Native server exited; inspect runtime/native_server.log')
            try:
                with urllib.request.urlopen(expected_base.removesuffix('/v1')+'/health', timeout=1) as r:
                    if json.load(r).get('status') == 'ok':
                        break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Native startup health check timed out')
        app = subprocess.Popen(app_cmd, start_new_session=True)
        return app.wait()
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop_child(app, graceful=True)
        return app.returncode if app and app.returncode is not None else 130
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop_child(app, graceful=True)
        stop_child(server)


if __name__ == '__main__':
    raise SystemExit(main() or 0)
