"""Score thermal frames with the frozen K1 candidate and report episode onsets."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import numpy as np

from portable_forest import PortableForest
from thermal_features import FEATURE_NAMES, thermal_window, warm_trace

MODEL_SHA256 = '91ba210ac9321d7a7a0b55dbf02aa051362157849ec69dd976f06b0af46b417d'


class ThermalDetector:
    def __init__(self, model_path):
        if hashlib.sha256(Path(model_path).read_bytes()).hexdigest() != MODEL_SHA256:
            raise ValueError('热像模型 SHA-256 与冻结基线不一致')
        self.model = PortableForest(model_path)
        config = self.model.provenance['config']
        if (self.model.radar or self.model.provenance['feature_names'] != FEATURE_NAMES
                or config['frames'] != 8 or config['threshold'] != .50):
            raise ValueError('热像模型与当前纯热像验收配置不匹配')
        self.cluster_gap_ns = round(config['cluster_gap_s'] * 1e9)
        self.frames = deque()
        self.last_stamp = None
        self.last_positive = None
        self.last_score = None
        self.threshold = config['threshold']
        self.valid_frames = self.scored_windows = 0

    def push(self, row):
        self.last_score = None
        stamp = row.get('representative_mono_ns')
        mask = row.get('valid_mask', ())
        if (row.get('simulated') is not False or row.get('valid') is not True
                or type(stamp) is not int or stamp < 0 or len(mask) != 768
                or not all(value is True for value in mask)):
            return None
        grid = np.asarray(row.get('grid_c'), dtype=np.float32)
        if grid.shape != (768,) or not np.isfinite(grid).all():
            return None
        if self.last_stamp is not None:
            if stamp <= self.last_stamp:
                raise ValueError('热像采集时间戳未递增')
            if stamp - self.last_stamp > 1_000_000_000:
                self.frames.clear()
        self.last_stamp = stamp
        self.valid_frames += 1
        grid = grid.reshape(24, 32)
        self.frames.append((stamp, grid, warm_trace(grid)))
        # Eight frames at gaps of at most one second, plus four seconds of context.
        while self.frames[0][0] < stamp - 12_000_000_000:
            self.frames.popleft()
        if len(self.frames) < 8:
            return None
        stamps, grids, trace = zip(*self.frames)
        times = (np.asarray(stamps, dtype=np.int64) - stamps[0]) / 1e9
        _, features = thermal_window(np.stack(trace), times, np.stack(grids), len(times)-1)
        score = float(self.model.predict_score(features[None, :])[0])
        self.last_score = score
        self.scored_windows += 1
        if score < self.threshold:
            return None
        new_episode = self.last_positive is None or stamp - self.last_positive > self.cluster_gap_ns
        self.last_positive = stamp
        if new_episode:
            return {'frame_mono_ns': stamp, 'frame_id': row.get('frame_id'), 'score': score}
        return None

    def candidate_frames(self):
        """Take two actual frames from the scoring window, on a live onset only."""
        stamp = self.frames[-1][0]
        previous = next((frame for frame in reversed(self.frames)
                         if frame[0] <= stamp - 750_000_000), self.frames[0])
        frames = []
        for frame_stamp, grid, _ in (previous, self.frames[-1]):
            pixels = np.rint(grid.ravel() * 10)
            if np.any((pixels < -1000) | (pixels > 10000)):
                continue
            frames.append({'offset_ms': (frame_stamp - stamp) // 1_000_000,
                           'pixels_decic': pixels.astype(int).tolist()})
        return frames if frames and frames[-1]['offset_ms'] == 0 else []


def replay(detector, path, on_event, stop):
    """Replay exactly one physical source file. All outputs are historical."""
    session_id = None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if stop.is_set():
                break
            row = json.loads(line)
            if session_id is None:
                session_id = row['session_id']
            if row.get('session_id') != session_id or row.get('simulated') is not False:
                raise ValueError('回放要求单一真实热像会话')
            evidence = detector.push(row)
            if evidence:
                on_event('candidate', dict(evidence, recording_session=session_id))
    if not detector.scored_windows:
        raise ValueError('回放未产生有效的热像判定窗口')


def watch(detector, worker, driver, on_event, stop, failed=False):
    """Reuse the workbench I2C worker and release its lock when listening exits."""
    def fault(reason):
        nonlocal failed
        if not failed and not stop.is_set():
            failed = True
            on_event('fault', {'reason': reason})

    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-B', '-u', str(worker), '--preview', '--driver', str(driver),
             '--duration', 'inf'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        buffer = b''
        valid_deadline = time.monotonic() + 12
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while not stop.is_set():
                if time.monotonic() >= valid_deadline:
                    fault('thermal_valid_frame_timeout')
                if not ready.select(.25):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    fault('thermal_worker_exited')
                    break
                buffer += chunk
                lines = buffer.split(b'\n')
                buffer = lines.pop()
                for line in lines:
                    if stop.is_set():
                        break
                    if not line.startswith(b'WORKBENCH_EVENT '):
                        continue
                    event = json.loads(line.removeprefix(b'WORKBENCH_EVENT '))
                    if event['kind'] == 'error':
                        fault(event['error'])
                    if event['kind'] != 'frame':
                        continue
                    previous = detector.valid_frames
                    evidence = detector.push(event['row'])
                    if detector.valid_frames != previous:
                        valid_deadline = time.monotonic() + 3
                        if failed:
                            on_event('recovered', {'frame_mono_ns': detector.last_stamp})
                            failed = False
                    if evidence:
                        on_event('candidate', dict(evidence, thermal_frames=detector.candidate_frames()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fault(str(exc))
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
