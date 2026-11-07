"""Explicit stable Pulse node names; never fall back to monitor/default devices."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# Keep each command's original stderr available even when repeated known warnings
# are combined in the terminal. These describe node enumeration, not recordings.
NODE_DIAGNOSTICS = []
_KNOWN_NON_ASCII_WARNING = re.compile(r'Invalid non-ASCII character: 0x[0-9a-fA-F]{2}')
_known_warning_reported = False

PROMPTS = {'general': '请说说您现在的情况。',
 'pain': '哪里疼？现在需要什么帮助？',
 'unable': '发生了什么？需要什么帮助？',
 'assistance': '您希望请人帮什么忙？',
 'sensor': '请说说您现在的情况。',
 'emergency': '我听到您在求助，请先保持安全。',
 'assistance_received': '您的请求已经记下了。还没有联系照护者。',
 'safe_received': '收到您的回答了。',
 'review_received': '您的回答已收到。情况还需要进一步确认。',
 'clarify': '我没听明白，您现在怎么了？'}


def _validate_nodes(rows, kind):
    if not isinstance(rows, list):
        raise ValueError('pactl ' + kind + ' did not return a JSON node list')
    for index, row in enumerate(rows):
        label = 'pactl ' + kind + '[' + str(index) + ']'
        if not isinstance(row, dict):
            raise ValueError(label + ' is not a node object')
        if not isinstance(row.get('name'), str) or not row['name'].strip():
            raise ValueError(label + ' has a missing or invalid name')
        if not isinstance(row.get('mute'), bool):
            raise ValueError(label + ' has a missing or invalid mute flag')
        volume = row.get('volume')
        if not isinstance(volume, dict) or not volume:
            raise ValueError(label + ' has missing or invalid volume channels')
        for channel in volume.values():
            value = channel.get('value') if isinstance(channel, dict) else None
            if type(value) is not int or value < 0:
                raise ValueError(label + ' has a missing or invalid volume value')


def _show_node_stderr(stderr, validated=False):
    global _known_warning_reported
    if not validated:
        sys.stderr.write(stderr)
        sys.stderr.flush()
        return
    lines = stderr.splitlines(keepends=True)
    known = [bool(_KNOWN_NON_ASCII_WARNING.fullmatch(line.rstrip('\r\n'))) for line in lines]
    sys.stderr.write(''.join(line for line, suppress in zip(lines, known) if not suppress))
    if any(known) and not _known_warning_reported:
        print('[音频节点] pactl 非 ASCII 描述警告已合并；原始诊断保存在 audio_io.NODE_DIAGNOSTICS。', file=sys.stderr)
        _known_warning_reported = True
    sys.stderr.flush()


def nodes(kind):
    environment = dict(os.environ, LC_ALL='C.UTF-8')
    result = subprocess.run(['pactl', '-f', 'json', 'list', kind], text=True,
                            encoding='utf-8', capture_output=True, timeout=5, env=environment)
    NODE_DIAGNOSTICS.append({'kind': kind, 'returncode': result.returncode, 'stderr': result.stderr})
    try:
        result.check_returncode()
        rows = json.loads(result.stdout)
        _validate_nodes(rows, kind)
    except (subprocess.CalledProcessError, ValueError):
        _show_node_stderr(result.stderr)
        raise
    _show_node_stderr(result.stderr, validated=True)
    return rows


def resolve_nodes(source=None, sink=None):
    source = source or os.environ.get('K1_AUDIO_SOURCE', '').strip()
    sink = sink or os.environ.get('K1_AUDIO_SINK', '').strip()
    if not source or not sink:
        raise RuntimeError('请用 --source/--sink 或 K1_AUDIO_SOURCE/K1_AUDIO_SINK 指定音频节点；'
                           '用 pactl list short sources/sinks 查看节点名称。')
    sources, sinks = nodes('sources'), nodes('sinks')
    by_source = {x['name']: x for x in sources}
    by_sink = {x['name']: x for x in sinks}
    if source not in by_source or source.endswith('.monitor'):
        raise RuntimeError('未找到指定实际麦克风：' + source)
    if sink not in by_sink:
        raise RuntimeError('未找到指定扬声器：' + sink)
    for label, row in [('麦克风', by_source[source]), ('音箱', by_sink[sink])]:
        if row.get('mute'):
            raise RuntimeError(label + '已静音；请检查音量设置')
        volumes = row.get('volume', {}).values()
        if volumes and all(x.get('value', 0) == 0 for x in volumes):
            raise RuntimeError(label + '音量为零')
    return source, sink


def select_prompt(events):
    if set(events) & {'受伤', '呼吸困难', '站立不稳', '意识异常'}:
        return 'general'
    if '无法起身' in events:
        return 'unable'
    if '疼痛' in events:
        return 'pain'
    if '请求帮助' in events:
        return 'assistance'
    return 'general'


def select_feedback(decision):
    """Only acknowledge answers in an existing risk episode, never idle chatter."""
    state = decision['state']
    if not decision['answer_eligible'] or state['red']:
        return None
    route = decision['route']
    if route == 'needs_assistance':
        return 'assistance_received'
    if route == 'safety_response' and not state['pending']:
        return 'safe_received'
    if state['pending'] and route in {'uncertain', 'continue_safety_confirmation', 'fall_or_injury', 'safety_response'}:
        return 'review_received'
    return None


class Playback:
    """Half-duplex echo guard, NOT AEC or full-duplex/barge-in support."""
    def __init__(self, sink, assets, enabled=False, guard_seconds=.8):
        self.sink, self.assets = sink, Path(assets)
        self.enabled, self.guard_seconds = enabled, guard_seconds
        self.lock = threading.Lock()
        self.playing = threading.Event()
        self.until = 0.0
        self.last_metrics = None

    def blocked(self):
        return self.playing.is_set() or time.monotonic() < self.until

    def play(self, key, response_cue=False):
        if key not in PROMPTS:
            raise ValueError('unknown prompt')
        print('[询问/播报] ' + PROMPTS[key], flush=True)
        if not self.enabled:
            self.last_metrics = dict(enabled=False, succeeded=False)
            print('[播报未启用] 仅显示文字，不开始实际回答倒计时。', flush=True)
            return False
        with self.lock:
            started_ns = time.monotonic_ns()
            metrics = dict(enabled=True, key=key, succeeded=False,
                           request_started_mono_ns=started_ns,
                           speech_process_ms=None, guard_ms=None, cue_process_ms=None,
                           timing_scope='host_process_intervals_not_acoustic_output')
            self.playing.set()
            try:
                path = self.assets / (key + '.wav')
                if not path.is_file():
                    raise RuntimeError('缺少播报文件：' + str(path))
                speech_ns = time.monotonic_ns()
                metrics['speech_process_launch_mono_ns'] = speech_ns
                subprocess.run(['paplay', '--device=' + self.sink, str(path)], check=True, timeout=20)
                metrics['speech_process_ms'] = round((time.monotonic_ns() - speech_ns) / 1e6, 2)
                # Keep capture draining and discard echo/tail during guard period.
                self.until = time.monotonic() + self.guard_seconds
                guard_ns = time.monotonic_ns()
                time.sleep(self.guard_seconds)
                metrics['guard_ms'] = round((time.monotonic_ns() - guard_ns) / 1e6, 2)
                if response_cue:
                    cue = self.assets / 'ready.wav'
                    if not cue.is_file():
                        raise RuntimeError('缺少回答就绪提示音：' + str(cue))
                    # A short non-speech cue follows the echo guard, so the user
                    # need not guess when the silent protection interval ends.
                    cue_ns = time.monotonic_ns()
                    subprocess.run(['paplay', '--device=' + self.sink, str(cue)], check=True, timeout=5)
                    metrics['cue_process_ms'] = round((time.monotonic_ns() - cue_ns) / 1e6, 2)
                metrics['succeeded'] = True
                return True
            except Exception as exc:
                print('[播报失败] ' + str(exc), flush=True)
                return False
            finally:
                metrics['total_ms'] = round((time.monotonic_ns() - started_ns) / 1e6, 2)
                self.last_metrics = metrics
                self.playing.clear()
