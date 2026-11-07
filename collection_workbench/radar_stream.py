"""Read-only LD2410 UART worker and causal replay alignment. No radar commands."""
from __future__ import annotations

import bisect
import json
import math
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from k1_collect.ld2410_protocol import HEADER, FOOTER, decode_payload


class RadarStream:
    def __init__(self, folder, metadata, notify):
        self.folder, self.meta, self.notify = Path(folder), metadata, notify
        self.config = metadata['radar_config']
        self.stop_event = threading.Event()
        self.thread = None
        self.port = None
        self.latest = None
        self.error = None
        self.counts = dict(bytes=0, valid_frames=0, invalid_candidates=0,
                           gaps_over_1s=0, noise_bytes=0, prefix_bytes=0)
        self.modes = {}
        self.last_good_ns = None
        self.started_ns = None
        self.first_good_ns = None
        self.max_gap_ns = 0

    def start(self):
        import serial
        from serial.tools import list_ports
        requested = Path(self.config['port'])
        resolved = requested.resolve()
        ports = [p for p in list_ports.comports() if Path(p.device).resolve() == resolved]
        if len(ports) != 1 or (ports[0].vid, ports[0].pid) != (0x1a86, 0x7523):
            raise ValueError('指定雷达串口未找到或不是已核对的CH340；请重新枚举，不自动选择其他端口')
        if resolved.parent != Path('/dev') or not resolved.name.startswith('ttyUSB'):
            raise ValueError('雷达端口必须是已确认的USB串口或其by-id链接')
        self.port = serial.Serial(port=None, baudrate=self.config['host_baudrate'], timeout=0.05,
                                  bytesize=8, parity='N', stopbits=1, xonxoff=False,
                                  rtscts=False, dsrdtr=False, exclusive=True)
        self.port.port = str(requested)
        self.port.dtr = self.port.rts = False
        try:
            self.port.open()
            self.port.reset_input_buffer()
        except BaseException:
            self.port.close()
            raise
        self.started_ns = time.monotonic_ns()
        from storage import atomic_json
        atomic_json(self.folder/'radar_config.json',dict(self.config, resolved_port=str(resolved),
            usb_vid=ports[0].vid,usb_pid=ports[0].pid,usb_location=ports[0].location,
            usb_serial_number=ports[0].serial_number,pyserial_version=serial.__version__,
            boot_id=self.meta['boot_id'],capture_start_mono_ns=self.started_ns))
        self.thread = threading.Thread(target=self._run, name='radar-read-only', daemon=True)
        self.thread.start()

    def _run(self):
        buffer = bytearray()
        offset = 0
        spans = []
        stale_reported = False
        last_notice = 0
        try:
            with (self.folder / 'radar_raw.bin').open('xb') as raw, \
                 (self.folder / 'radar_reads.jsonl').open('x', encoding='utf-8') as reads, \
                 (self.folder / 'radar.jsonl').open('x', encoding='utf-8') as frames:
                def emit(row):
                    frames.write(json.dumps(dict(session_id=self.meta['session_id'],
                        boot_id=self.meta['boot_id'], simulated=False, **row), ensure_ascii=False) + '\n')

                while not self.stop_event.is_set():
                    begin = time.monotonic_ns()
                    chunk = self.port.read(4096)
                    end = time.monotonic_ns()
                    if chunk:
                        start_byte = self.counts['bytes']
                        raw.write(chunk)
                        reads.write(json.dumps(dict(byte_offset=start_byte, length=len(chunk),
                            read_start_mono_ns=begin, read_end_mono_ns=end)) + '\n')
                        self.counts['bytes'] += len(chunk)
                        spans.append((start_byte, self.counts['bytes'], begin, end))
                        buffer.extend(chunk)
                    while buffer:
                        head = buffer.find(HEADER)
                        if head != 0:
                            drop = head if head > 0 else max(0, len(buffer) - 3)
                            key = 'noise_bytes' if self.counts['valid_frames'] else 'prefix_bytes'
                            self.counts[key] += drop
                            del buffer[:drop]
                            offset += drop
                            if head < 0:
                                break
                        if len(buffer) < 6:
                            break
                        length = int.from_bytes(buffer[4:6], 'little')
                        reason, fields = None, None
                        total = length + 10
                        if not 13 <= length <= 64:
                            reason = 'implausible_payload_length'
                        elif len(buffer) < total:
                            break
                        elif buffer[6 + length:total] != FOOTER:
                            reason = 'outer_footer_mismatch'
                        else:
                            try:
                                fields = decode_payload(bytes(buffer[6:6 + length]))
                            except ValueError as exc:
                                reason = str(exc)
                        if reason:
                            self.counts['invalid_candidates'] += 1
                            emit(dict(status='invalid', valid=False, byte_offset=offset,
                                      received_mono_ns=end, error=reason,
                                      raw_hex=bytes(buffer[:min(total, 74)]).hex()))
                            del buffer[:1]
                            offset += 1
                            continue
                        first = next(s for s in spans if s[0] <= offset < s[1])
                        last = next(s for s in spans if s[0] <= offset + total - 1 < s[1])
                        at = last[3]
                        previous = self.last_good_ns or self.started_ns
                        gap_ns = at - previous
                        self.max_gap_ns = max(self.max_gap_ns, gap_ns)
                        # A stalled read can return valid data immediately on resume.
                        # Detect its preceding silence before resetting last_good_ns.
                        if gap_ns > 1_000_000_000 and not stale_reported:
                            self.counts['gaps_over_1s'] += 1
                            emit(dict(status='gap', valid=False, received_mono_ns=at,
                                      diagnostic_threshold_s=1,
                                      error='no_complete_report_over_1s'))
                        self.first_good_ns = self.first_good_ns or at
                        self.last_good_ns = at
                        stale_reported = False
                        self.counts['valid_frames'] += 1
                        self.modes[fields['mode']] = self.modes.get(fields['mode'], 0) + 1
                        self.latest = dict(status='valid', valid=True, frame_id=self.counts['valid_frames'],
                            byte_offset=offset, wire_length=total, read_start_mono_ns=first[2],
                            read_end_mono_ns=at, representative_mono_ns=at,
                            timestamp_semantics='host_read_completion_not_hardware_sample', **fields)
                        emit(self.latest)
                        del buffer[:total]
                        offset += total
                    spans = [s for s in spans if s[1] > offset]
                    age = (end - (self.last_good_ns or self.started_ns)) / 1e9
                    if age > 1 and not stale_reported:
                        self.counts['gaps_over_1s'] += 1
                        stale_reported = True
                        emit(dict(status='gap', valid=False, received_mono_ns=end,
                                  diagnostic_threshold_s=1, error='no_complete_report_over_1s'))
                    if end - last_notice > 250_000_000:
                        raw.flush(); reads.flush(); frames.flush()
                        self.notify('radar', row=self.latest,
                                    counts=dict(self.counts), state='stale' if age > 1 else 'reading')
                        last_notice = end
                import os
                for stream in (raw, reads, frames):
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.notify('radar', row=self.latest, counts=dict(self.counts), state='error', error=self.error)
        finally:
            self.port.close()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.error = self.error or '雷达读取线程未及时结束；记录不作为完整采集'
        elif self.port:
            self.port.close()
        now = time.monotonic_ns()
        silence = max(self.max_gap_ns, now - (self.last_good_ns or self.started_ns or now),
                      (self.first_good_ns or now) - (self.started_ns or now)) / 1e9
        return dict(**self.counts, observed_modes=dict(self.modes), error=self.error,
                    max_no_report_window_s=silence, configuration_bytes_written=0,
                    delivery_checks_ok=bool(self.counts['valid_frames']) and not self.error
                        and not self.counts['gaps_over_1s'] and silence <= 1,
                    framing_checks_ok=bool(self.counts['valid_frames']) and not self.error
                        and not self.counts['invalid_candidates'] and not self.counts['noise_bytes'],
                    timing_scope='K1_host_read_intervals_not_sensor_hardware_sync')


class RadarReplay:
    """Index only real rows from this session/boot; missing data stays unknown."""
    def __init__(self, folder, metadata):
        self.folder, self.meta = Path(folder), metadata
        self.rows, self.times, self.issues = [], [], []
        self.barriers = []
        path = self.folder / 'radar.jsonl'
        if path.is_symlink():
            raise ValueError('不读取符号链接雷达记录')
        self.signature = (path.stat().st_size, path.stat().st_mtime_ns) if path.is_file() else None
        if self.signature is None:
            return
        previous = None
        with path.open(encoding='utf-8') as stream:
            for line_no, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    if row.get('status') != 'valid' or row.get('valid') is not True:
                        barrier = row.get('received_mono_ns')
                        if isinstance(barrier, int) and not isinstance(barrier, bool):
                            self.barriers.append(barrier)
                        continue
                    at = row['read_end_mono_ns']
                    if isinstance(at, bool) or not isinstance(at, int):
                        raise ValueError('雷达接收时间不是整数')
                    if row.get('session_id') != metadata['session_id'] or row.get('boot_id') != metadata.get('boot_id'):
                        raise ValueError('雷达会话或开机标识不一致')
                    if row.get('simulated') is not False or previous is not None and at < previous:
                        raise ValueError('雷达来源或时间顺序无效')
                    if not metadata['mono_start_ns'] <= at <= metadata['mono_end_ns']:
                        raise ValueError('雷达接收时间越界')
                    if isinstance(row.get('state'),bool) or row.get('state') not in (0, 1, 2, 3, 4, 5, 6):
                        raise ValueError('雷达状态未定义')
                    start=row.get('read_start_mono_ns')
                    if isinstance(start,bool) or not isinstance(start,int) or not metadata['mono_start_ns']<=start<=at:
                        raise ValueError('雷达读取区间无效')
                    for name in ('moving_energy','static_energy'):
                        value=row.get(name)
                        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=100:
                            raise ValueError('雷达能量字段无效')
                    self.rows.append(row)
                    self.times.append(at)
                    previous = at
                except (ValueError, TypeError, KeyError) as exc:
                    self.issues.append(f'雷达第{line_no}行：{exc}')
        if self.signature != (path.stat().st_size, path.stat().st_mtime_ns):
            raise ValueError('雷达索引期间原文件发生变化')
        self.barriers.sort()

    def at(self, mono_ns, max_age_ms=1000):
        path = self.folder / 'radar.jsonl'
        if self.signature and self.signature != (path.stat().st_size, path.stat().st_mtime_ns):
            raise ValueError('雷达记录已变化，请重新打开会话')
        index = bisect.bisect_right(self.times, int(mono_ns)) - 1
        if index < 0:
            return dict(available=False, reason='no_prior_real_report', row=None)
        row = self.rows[index]
        age = (int(mono_ns) - self.times[index]) / 1e6
        barrier = bisect.bisect_right(self.barriers, int(mono_ns)) - 1
        if barrier >= 0 and self.barriers[barrier] >= self.times[index]:
            return dict(available=False, reason='invalid_or_gap_after_report', row=None)
        if self.issues:
            return dict(available=False, reason='radar_record_integrity_issue', row=None)
        if age > max_age_ms:
            return dict(available=False, reason='report_stale', age_ms=age, row=None)
        return dict(available=True, age_ms=age, reason='latest_prior_host_report', row=dict(row))
