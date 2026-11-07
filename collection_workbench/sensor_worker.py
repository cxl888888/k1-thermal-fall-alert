"""Owned child process: thermal acquisition and explicitly enabled read-only radar."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import threading
from contextlib import nullcontext
import signal
from tempfile import NamedTemporaryFile
import uuid

from storage import atomic_json


_OUTPUT_LOCK = threading.Lock()


def notify(kind, **data):
    with _OUTPUT_LOCK:
        print('WORKBENCH_EVENT ' + json.dumps(dict(kind=kind, **data), ensure_ascii=False, allow_nan=False), flush=True)


def publish_latest_thermal(path, grid, read_end_mono_ns):
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', dir=path.parent, prefix='.k1-thermal-',
                                encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'read_end_mono_ns': read_end_mono_ns,
                       'pixels_decic': [round(value * 10) for value in grid]}, stream,
                      separators=(',', ':'))
        temporary.replace(path)
    except OSError:
        # Preview must not interrupt acquisition or risk detection.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--session', type=Path)
    mode.add_argument('--preview',action='store_true')
    parser.add_argument('--driver', type=Path, required=True)
    parser.add_argument('--duration', type=float, required=True)
    args = parser.parse_args()
    import fcntl  # Linux/K1 only; the PC replay service does not import fcntl.

    folder = args.session
    metadata = (dict(session_id='preview_'+uuid.uuid4().hex,radar_config={'enabled':False}) if args.preview
                else json.loads((folder / 'metadata.json').read_text(encoding='utf-8')))
    stop = folder/'stop.request' if folder else None
    stop_event=threading.Event()
    if args.preview:signal.signal(signal.SIGTERM,lambda *_:stop_event.set())
    def stopped():return stop_event.is_set() or bool(stop and stop.exists())
    sensor = adapter = lock = live_path = None
    radar = None
    radar_summary = None
    count = good_count = failure_count = startup = partial = 0
    error = None
    stop_reason = 'duration_complete'
    started = time.monotonic()
    try:
        # Same cooperative lock as the existing collector; not a universal lock for unrelated tools.
        lock = open('/tmp/k1_thermal_collector.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # SECURITY: publish a single replaceable frame under the private per-user runtime directory.
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
        try:
            runtime_info = runtime.stat()
            if runtime.is_dir() and runtime_info.st_uid == os.getuid() and not runtime_info.st_mode & 0o077:
                live_path = runtime / 'k1-family-thermal.json'
                live_path.unlink(missing_ok=True)
        except OSError:
            live_path = None
        if stopped():
            stop_reason = 'operator_stop'
            return 0
        spec = importlib.util.spec_from_file_location('workbench_thermal_driver', args.driver)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        adapter = driver.SmbusI2CAdapter(driver.I2C_BUS)
        sensor = driver.MLX90640(adapter, address=driver.I2C_ADDRESS)
        sensor.refresh_rate = 3
        # Load native thermal dependencies before starting the UART reader. Heavy
        # initialization can otherwise delay its first scheduled read for seconds.
        if metadata.get('radar_config', {}).get('enabled'):
            from radar_stream import RadarStream
            radar = RadarStream(folder, metadata, notify)
            radar.start()
        notify('ready', driver_script_sha256=hashlib.sha256(args.driver.read_bytes()).hexdigest(),
               i2c_bus=driver.I2C_BUS, i2c_address=driver.I2C_ADDRESS,
               i2c_read_len_words=driver.adafruit_mlx90640.I2C_READ_LEN)
        started = time.monotonic()
        with (nullcontext(None) if args.preview else (folder/'thermal.jsonl').open('x',encoding='utf-8',buffering=1)) as stream:
            while time.monotonic() - started < args.duration and not stopped():
                grid = [float('nan')] * 768
                failure = None
                begin = time.monotonic_ns()
                try:
                    adapter.reset_frame_stats()
                    sensor.getFrame(grid)
                except (OSError, RuntimeError, ValueError, ArithmeticError) as exc:
                    failure = f'{type(exc).__name__}: {exc}'
                end = time.monotonic_ns()
                mask = [math.isfinite(x) and -40 < x < 300 for x in grid]
                valid_pixels = sum(mask)
                warming = failure is None and valid_pixels >= 384 and startup < 4
                if warming:
                    startup += 1
                good = failure is None and valid_pixels == 768 and not warming
                count += 1
                good_count += int(good)
                failure_count += int(failure is not None or valid_pixels < 384)
                partial += int(not warming and failure is None and 384 <= valid_pixels < 768)
                row = dict(session_id=metadata['session_id'], frame_id=count, simulated=False,
                           read_start_mono_ns=begin, read_end_mono_ns=end,
                           representative_mono_ns=(begin + end) // 2,
                           timestamp_uncertainty='driver_getFrame_interval_two_subpages',
                           read_ms=(end - begin) / 1e6, valid_pixels=valid_pixels, valid=good,
                           status='error' if failure else 'warmup' if warming else 'valid' if good else 'invalid',
                           error=failure, grid_c=[float(x) if math.isfinite(x) else None for x in grid],
                           valid_mask=mask, i2c_stats=adapter.frame_stats())
                if stream:stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                if stream and count % 20 == 0:
                    stream.flush()
                    os.fsync(stream.fileno())
                notify('frame', row=row, attempts=count, valid_frames=good_count,
                       failure_frames=failure_count, startup_frames=startup,
                       partial_frames=partial,
                       effective_valid_fps=good_count / max(.001, time.monotonic() - started))
                if good and live_path is not None:
                    publish_latest_thermal(live_path, grid, end)
                if failure:
                    time.sleep(.2)
            if stream:
                stream.flush()
                os.fsync(stream.fileno())
        if stopped():
            stop_reason = 'operator_stop'
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
        stop_reason = 'initialization_or_runtime_error'
        notify('error', error=error)
    finally:
        if radar is not None:
            radar_summary = radar.close()
            if (radar_summary.get('error') or not radar_summary.get('framing_checks_ok')
                    or not radar_summary.get('delivery_checks_ok')):
                error = error or radar_summary.get('error') or '雷达存在无有效帧、坏帧或接收断档；保留原始记录，需核查'
        if adapter:
            try:
                adapter.close()
            except OSError as exc:
                error = error or f'关闭I2C失败: {exc}'
        if live_path is not None:
            try:
                live_path.unlink(missing_ok=True)
            except OSError:
                pass
        if lock:
            lock.close()
        summary=dict(attempts=count, valid_frames=good_count,
                    failure_frames=failure_count, startup_frames=startup, partial_frames=partial,
                    error=error, stop_reason=stop_reason, actual_seconds=time.monotonic() - started,
                    radar_summary=radar_summary,
                    worker_end_mono_ns=time.monotonic_ns())
        if not args.preview:atomic_json(folder/'worker_summary.json',summary)
    return 1 if error or not good_count else 0


if __name__ == '__main__':
    raise SystemExit(main())
