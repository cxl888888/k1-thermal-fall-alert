"""Passive, bounded device-status observations; never open sensor devices."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener


RADAR_ADAPTER = Path('/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0')
THERMAL_BUS = Path('/dev/i2c-4')


def latest_thermal_frame():
    if not hasattr(os, 'getuid'):
        return None
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    try:
        # SECURITY: the reader uses a fixed private path and only accepts the writer's own file.
        fd = os.open(runtime / 'k1-family-thermal.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        frame = json.loads(raw)
        sampled_ns = frame.get('read_end_mono_ns') if isinstance(frame, dict) else None
        if type(sampled_ns) is not int or not 0 <= time.monotonic_ns() - sampled_ns <= 3_000_000_000:
            return None
        pixels = frame.get('pixels_decic')
        if not isinstance(pixels, list) or len(pixels) != 768 or any(
            type(value) is not int or not -400 <= value <= 3000 for value in pixels
        ):
            return None
        return {'pixels_decic': pixels}
    except (OSError, ValueError):
        return None


def audio_nodes(kind):
    """Read PipeWire's node inventory; idle/SUSPENDED is not a fault."""
    try:
        result = subprocess.run(['pactl', '-f', 'json', 'list', kind], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=2)
        if result.returncode:
            return None
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            return None
        return rows
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def capture_observation():
    """Consult the local workbench only; neither start capture nor access I2C/UART."""
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:8791/', timeout=1) as response:
            page = response.read(100_000).decode('utf-8')
        match = re.search(r'<meta name="workbench-token" content="([0-9a-f]+)">', page)
        if not match:
            return None
        req = Request('http://127.0.0.1:8791/api/capture',
                      headers={'X-Workbench-Token': match[1]})
        with opener.open(req, timeout=1) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _node_status(rows, expected, kind):
    name = '麦克风' if kind == 'microphone' else '音箱'
    if not expected:
        return {'state': 'unknown', 'detail': f'尚未配置{name}节点。'}
    if rows is None:
        return {'state': 'unknown', 'detail': '无法读取音频系统状态，请检查音频服务。'}
    node = next((item for item in rows if item.get('name') == expected
                 and (kind != 'microphone' or not expected.endswith('.monitor'))), None)
    if node is None:
        return {'state': 'not_detected', 'detail': f'未识别到配置的{name}节点。'}
    if node.get('mute') is True:
        return {'state': 'muted', 'detail': f'{name}已识别，但当前已静音。'}
    if node.get('mute') is not False:
        return {'state': 'unknown', 'detail': f'{name}节点已识别，静音状态未核验。'}
    volume = node.get('volume') or {}
    values = [v.get('value') for v in volume.values() if isinstance(v, dict) and type(v.get('value')) is int]
    if values and max(values) == 0:
        return {'state': 'muted', 'detail': f'{name}已识别，但当前音量为零。'}
    if node.get('state') == 'RUNNING':
        detail = ('音频节点正在被使用，仍需确认实际拾音。' if kind == 'microphone'
                  else '音频节点正在输出，仍需确认音箱实际有声。')
        return {'state': 'active', 'detail': detail}
    detail = ('已识别麦克风，当前空闲，实际拾音尚未核验。' if kind == 'microphone'
              else '已识别音箱且未静音，当前空闲，实际声音尚未核验。')
    return {'state': 'connected', 'detail': detail}


def build_snapshot(sources, sinks, thermal_bus, radar_adapter, capture):
    microphone = _node_status(sources, os.environ.get('K1_AUDIO_SOURCE', '').strip(), 'microphone')
    speaker = _node_status(sinks, os.environ.get('K1_AUDIO_SINK', '').strip(), 'speaker')
    thermal = ({'state': 'unknown', 'detail': '热像总线可访问；没有近期温度帧，传感器工作状态待核验。'}
               if thermal_bus else {'state': 'not_detected', 'detail': '热像总线不可访问，请检查连接和权限。'})
    radar = ({'state': 'connected', 'detail': '已识别USB串口小板；没有近期雷达帧，雷达本体待核验。'}
             if radar_adapter else {'state': 'not_detected', 'detail': '未识别到项目使用的USB串口小板。'})
    if isinstance(capture, dict) and (capture.get('state') == 'failed' or capture.get('error')):
        thermal = {'state': 'error', 'detail': '采集器报告热像采集失败，请检查连接与工作台。'}
        r = capture.get('radar') or {}
        if r.get('error') or r.get('state') == 'error':
            radar = {'state': 'error', 'detail': '雷达采集报告异常，请检查工作台。'}
    if isinstance(capture, dict) and not capture.get('error') and capture.get('state') in ('starting', 'recording'):
        latest = capture.get('latest') or {}
        age = capture.get('latest_age_s')
        if isinstance(age, (int, float)) and not isinstance(age, bool) and 0 <= age <= 3:
            if latest.get('valid') is True and latest.get('valid_pixels') == 768:
                thermal = {'state': 'active', 'detail': '采集器近期收到完整768像素温度帧。'}
            elif latest.get('status') == 'warmup':
                thermal = {'state': 'waiting', 'detail': '采集器正在预热，尚不能判断稳定工作。'}
            else:
                thermal = {'state': 'error', 'detail': '采集器近期帧无效或读取异常，请检查工作台。'}
        elif type(age) in (int, float) and age > 3 and latest:
            thermal = {'state': 'stale', 'detail': '已有热像采集记录，最近一帧已超过 3 秒。'}
        elif capture.get('state') == 'recording' and thermal_bus:
            thermal = {'state': 'waiting', 'detail': '采集中，但暂无近期热像帧，请检查工作台。'}
        r = capture.get('radar') or {}
        if r.get('error') or r.get('state') == 'error':
            radar = {'state': 'error', 'detail': '雷达采集报告异常，请检查工作台。'}
        elif r.get('state') == 'reading':
            age = r.get('age_s')
            valid_frames = (r.get('counts') or {}).get('valid_frames', 0)
            if type(age) in (int, float) and 0 <= age <= 2 and valid_frames > 0:
                radar = {'state': 'active', 'detail': '采集器近期收到有效雷达串口帧。'}
            elif type(age) in (int, float) and age > 2 and valid_frames > 0:
                radar = {'state': 'stale', 'detail': '已有雷达采集记录，最近一帧已超过 2 秒。'}
            else:
                radar = {'state': 'waiting', 'detail': '雷达读取已启动，但暂无近期有效帧。'}
    return {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'source': 'k1_local_observation', 'items': {'microphone': microphone, 'speaker': speaker,
            'thermal': thermal, 'radar': radar}}


class HardwareMonitor:
    def __init__(self, cache_seconds=3):
        self.cache_seconds = cache_seconds
        self.lock = threading.Lock()
        self.previous = None
        self.previous_at = 0.0

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            if self.previous is not None and now - self.previous_at < self.cache_seconds:
                return self.previous
            self.previous = build_snapshot(audio_nodes('sources'), audio_nodes('sinks'),
                THERMAL_BUS.exists() and os.access(THERMAL_BUS, os.R_OK | os.W_OK),
                RADAR_ADAPTER.exists(), capture_observation())
            if latest_thermal_frame() is not None:
                self.previous['items']['thermal'] = {'state': 'active', 'detail': '正在接收热像画面。'}
            self.previous_at = time.monotonic()
            return self.previous
