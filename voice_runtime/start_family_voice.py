"""Start an explicitly selected voice session with independent family delivery.

The bridge starts at the journal tail before voice begins. Network work stays
outside KWS/ASR. Only owned child processes are stopped; no alarm is cleared.
"""
import argparse
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time


def main():
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal',type=Path,default=root/'runtime/interaction.db')
    p.add_argument('--origin',choices=('controlled_simulation','live'),default='controlled_simulation')
    p.add_argument('--source-id',default='voice_main_v1')
    p.add_argument('--outbox',type=Path)
    p.add_argument('--portal-url',default='http://127.0.0.1:8794')
    p.add_argument('--family-resolve',action='store_true',help='启用 8795 本机家属端解除回执')
    p.add_argument('--portal-folder',type=Path,default=root.parent/'family_portal')
    p.add_argument('--bridge-python',type=Path,default=Path.home()/'venv-k1-family/bin/python')
    p.add_argument('--token-file',type=Path)
    p.add_argument('--prepare-only',action='store_true')
    args,extra=p.parse_known_args()
    if '--db' in extra: p.error('使用--journal指定本轮状态库，避免采集与发送读取不同源')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}',args.source_id):p.error('source-id格式无效')
    if args.origin=='live' and any(x in extra for x in ('--text-only','--simulate')):
        p.error('文字和模拟输入不能标成真实收音')
    if args.family_resolve and (args.origin != 'live' or args.portal_url != 'http://127.0.0.1:8795'):
        p.error('家属解除需要真实收音和 http://127.0.0.1:8795')
    if args.family_resolve:
        extra.extend(['--family-device-id', 'k1_voice.' + args.source_id])
    for option, directory in (('--engine-dir', 'engine'), ('--kws-dir', 'kws')):
        if not any(value == option or value.startswith(option + '=') for value in extra):
            extra.extend([option, str(root / directory)])
    folder=args.portal_folder.expanduser().resolve()
    args.journal = args.journal.expanduser().resolve()
    outbox = (args.outbox or folder/'data'/(args.source_id+'.'+args.origin+'.outbox.db')).expanduser().resolve()
    voice=[sys.executable,'-B',str(root/'start_runtime.py'),'--db',str(args.journal),*extra]
    bridge=[str(args.bridge_python.expanduser()),'-B','-m','portal.voice_bridge',
            '--journal',str(args.journal),'--outbox',str(outbox),'--source-id',args.source_id,'--origin',args.origin,'--url',args.portal_url]
    if args.prepare_only:
        print(json.dumps(dict(voice_command=voice,bridge_command=bridge,
            origin=args.origin,credentials='local file; not printed'),ensure_ascii=False));return
    token_file=args.token_file.expanduser() if args.token_file else folder/'data/voice_producer_token'
    secret=token_file.read_text(encoding='utf-8').strip()
    if not secret or token_file.stat().st_mode & 0o077:
        raise ValueError('本机生产者凭证缺失或权限过宽；只允许所有者读取')
    args.journal.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(args.journal) as db:
        db.execute('CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,mono_ns INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)')
    environment=dict(os.environ,FAMILY_PRODUCER_TOKEN=secret)
    child=subprocess.Popen(bridge,cwd=folder/'backend',env=environment,
                           stdout=subprocess.DEVNULL,start_new_session=True)
    app=None
    def interrupted(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    try:
        # Establish the tail before allowing voice to generate new facts.
        for _ in range(50):
            if child.poll() is not None:raise RuntimeError('家属事件适配器未启动')
            try:
                with sqlite3.connect(f'file:{outbox.as_posix()}?mode=ro',uri=True) as db:
                    ready=db.execute('SELECT signature,row_id FROM bridge_state WHERE id=1').fetchone()
                if ready:
                    expected=json.dumps([str(args.journal.resolve()),args.source_id,args.origin])
                    if ready[0]!=expected:raise ValueError('发送队列关联其他数据源，请使用另一队列')
                    break
            except sqlite3.Error:pass
            time.sleep(.1)
        else:raise RuntimeError('发送队列初始化超时')
        app=subprocess.Popen(voice,env=environment if args.family_resolve else None,start_new_session=True)
        bridge_failure_reported=False
        while app.poll() is None:
            if child.poll() is not None and not bridge_failure_reported:
                print('家属事件发送进程已退出，语音预警继续运行；源记录保留供恢复发送。',flush=True)
                bridge_failure_reported=True
            time.sleep(.2)
        time.sleep(.6)  # Allow the final committed status to enter the queue.
    except KeyboardInterrupt:
        pass
    finally:
        # start_runtime owns its native/model children and handles SIGINT cleanup.
        if app and app.poll() is None:
            app.send_signal(signal.SIGINT)
            try:app.wait(timeout=55)
            except subprocess.TimeoutExpired:
                app.terminate();app.wait(timeout=10)
        if child.poll() is None:
            child.terminate()
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if outbox.exists():
            with sqlite3.connect(f'file:{outbox.as_posix()}?mode=ro',uri=True) as db:
                pending=db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0]
            print(f'家属事件待入库队列：{pending}；入库不表示手机已读。',flush=True)
    if app and app.returncode not in (0,130,None):raise SystemExit(app.returncode)


if __name__=='__main__':main()
