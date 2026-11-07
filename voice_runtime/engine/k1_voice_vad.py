"""Silero VAD v5.1.2 + existing PipeWire/Pulse capture for the K1 demo.

The ONNX state/context convention follows Silero's MIT-licensed OnnxWrapper:
https://github.com/snakers4/silero-vad/blob/v5.1.2/src/silero_vad/utils_vad.py
Upstream copyright: Copyright (c) 2020-present Silero Team.
See SILERO_LICENSE.txt in the deployment bundle. No PyTorch is needed here.
Audio segmentation, bounded queue and process integration are application code.
"""

from __future__ import annotations

import math
import itertools
import os
import queue
import selectors
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


class SileroOnnx:
    """16 kHz, one channel; recurrent state and 64 samples of left context."""

    def __init__(self, model_path: Path):
        import numpy as np
        import onnxruntime as ort

        if not model_path.is_file():
            raise RuntimeError(f"找不到 VAD 模型：{model_path}，请上传完整部署文件夹。")
        self.np = np
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"],
        )
        if {item.name for item in self.session.get_inputs()} != {"input", "state", "sr"}:
            raise RuntimeError("VAD 模型接口不匹配，请使用部署包附带的 Silero v5.1.2。")
        self.reset()

    def reset(self) -> None:
        self.state = self.np.zeros((2, 1, 128), dtype=self.np.float32)
        self.context = self.np.zeros((1, 64), dtype=self.np.float32)

    def probability(self, pcm: bytes) -> float:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("VAD 每帧必须是 512 个 int16 采样点。")
        np = self.np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32).reshape(1, -1) / 32768.0
        window = np.concatenate((self.context, samples), axis=1)
        output, self.state = self.session.run(
            None, {"input": window, "state": self.state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
        )
        self.context = window[:, -64:].copy()
        probability = float(output.reshape(-1)[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise RuntimeError(f"VAD 返回无效概率：{probability}")
        return probability


@dataclass(frozen=True)
class SpeechSegment:
    pcm: bytes
    reason: str
    endpoint_silence_ms: int
    detected_mono: float


class SpeechSegmenter:
    """Hysteresis + pre-roll + silence endpoint; emits PCM without long silence."""

    def __init__(self, silence_ms: int = 800, threshold: float = 0.5, max_seconds: float = 15):
        self.threshold = threshold
        self.end_threshold = max(0.01, threshold - 0.15)
        self.silence_frames = math.ceil(silence_ms / (FRAME_SECONDS * 1000))
        self.max_frames = math.ceil(max_seconds / FRAME_SECONDS)
        self.pre = deque(maxlen=10)  # 320 ms prevents a clipped first syllable.
        self.tail_frames = 4  # Keep 128 ms after the estimated speech end.
        self.min_voiced_frames = 5  # 160 ms of confident speech.
        self.frames: list[bytes] | None = None
        self.quiet_start: int | None = None
        self.voiced_frames = 0

    def feed(self, pcm: bytes, probability: float, now: float | None = None) -> SpeechSegment | None:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("PCM frame size mismatch")
        self.pre.append(pcm)
        if self.frames is None:
            if probability >= self.threshold:
                self.frames = list(self.pre)
                self.voiced_frames = 1
                self.quiet_start = None
            return None

        self.frames.append(pcm)
        if probability >= self.threshold:
            self.voiced_frames += 1
            self.quiet_start = None
        elif probability < self.end_threshold and self.quiet_start is None:
            self.quiet_start = len(self.frames) - 1

        quiet_frames = 0 if self.quiet_start is None else len(self.frames) - self.quiet_start
        reason = "silence" if quiet_frames >= self.silence_frames else None
        if reason is None and len(self.frames) >= self.max_frames:
            reason = "max_duration"
        if reason is None:
            return None

        result = None
        if self.voiced_frames >= self.min_voiced_frames:
            end = len(self.frames)
            if reason == "silence":
                end = min(end, self.quiet_start + self.tail_frames)
            result = SpeechSegment(
                b"".join(self.frames[:end]), reason,
                round(quiet_frames * FRAME_SECONDS * 1000),
                time.monotonic() if now is None else now,
            )
        self.frames = None
        self.quiet_start = None
        self.voiced_frames = 0
        self.pre.clear()
        return result


def microphone_frames(source: str) -> Iterator[bytes]:
    """One persistent arecord process; selector makes stalls and Ctrl+C visible."""
    environment = os.environ.copy()
    environment["PULSE_SOURCE"] = source
    with tempfile.TemporaryFile(mode="w+b") as errors:
        process = subprocess.Popen(
            ["arecord", "-q", "-D", "plug:pulse", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"],
            env=environment, stdout=subprocess.PIPE, stderr=errors, bufsize=0,
        )
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = bytearray()
            last_audio = time.monotonic()
            while True:
                ready = selector.select(timeout=0.25)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 16384)
                    if not chunk:
                        process.wait(timeout=2)
                        errors.seek(0)
                        detail = errors.read(4096).decode("utf-8", errors="replace")
                        raise RuntimeError(f"录音进程退出（{process.returncode}）：{detail}")
                    last_audio = time.monotonic()
                    pending.extend(chunk)
                    while len(pending) >= FRAME_BYTES:
                        frame = bytes(pending[:FRAME_BYTES])
                        del pending[:FRAME_BYTES]
                        yield frame
                if time.monotonic() - last_audio > 5:
                    raise RuntimeError("麦克风连续 5 秒没有音频数据，请检查音频路由。")
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()


def listen_forever(
    vad: SileroOnnx, source: str, handler: Callable[[Path, dict], None],
    silence_ms: int = 800, threshold: float = 0.5, max_seconds: float = 15,
    debug: bool = False, brief: bool = False, wait_for_current: bool = False,
    session_report: dict | None = None,
) -> None:
    segmenter = SpeechSegmenter(silence_ms, threshold, max_seconds)
    tasks: queue.Queue = queue.Queue(maxsize=4)
    stopping = threading.Event()
    cancelled_ids = []
    processing_errors = []
    active_id = [None]
    submitted_count = 0
    rejected_ids = []

    def worker() -> None:
        while not stopping.is_set():
            try:
                speech_id, segment = tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if stopping.is_set():
                    cancelled_ids.append(speech_id)
                    return
                active_id[0] = speech_id
                started = time.monotonic()
                with tempfile.TemporaryDirectory(prefix="k1_vad_speech_") as directory:
                    path = Path(directory) / "speech.wav"
                    with wave.open(str(path), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(SAMPLE_RATE)
                        audio.writeframes(segment.pcm)
                    metadata = {
                        "speech_id": speech_id,
                        "vad_cut_reason": segment.reason,
                        "vad_estimated_endpoint_silence_ms": segment.endpoint_silence_ms,
                        "queue_wait_ms": round((started - segment.detected_mono) * 1000),
                        "_segment_detected_mono": segment.detected_mono,
                    }
                    handler(path, metadata)
            except Exception as exc:
                processing_errors.append({'speech_id': speech_id, 'error': str(exc)})
                print(f"[语句 {speech_id} 处理失败] {exc}", flush=True)
            finally:
                active_id[0] = None
                tasks.task_done()

    vad.reset()
    capture = microphone_frames(source)
    thread = threading.Thread(target=worker, name="asr-llm-worker", daemon=True)
    speech_id = 0
    debug_at = time.monotonic()
    peak_probability = 0.0
    try:
        first_frame = next(capture)  # Confirm audio arrives before announcing ready.
        thread.start()
        print(f"[监听] 输入源：{source}", flush=True)
        print(f"[就绪] 可以直接说话，无需回车；停顿约 {silence_ms}ms 后提交。Ctrl+C 退出。", flush=True)
        print("[提示] 本轮仅输出文字。先每次说一句，再等结果。", flush=True)
        if brief:
            print('[输出] 每句显示摘要；Ctrl+C 后打印本次完整 JSON。', flush=True)
        for pcm in itertools.chain((first_frame,), capture):
            was_active = segmenter.frames is not None
            probability = vad.probability(pcm)
            peak_probability = max(peak_probability, probability)
            segment = segmenter.feed(pcm, probability)
            if not brief and not was_active and segmenter.frames is not None:
                print("[VAD] 检测到说话。", flush=True)
            if debug and time.monotonic() - debug_at >= 1:
                print(f"[VAD调试] 最近约1秒最高语音概率：{peak_probability:.3f}", flush=True)
                peak_probability = 0.0
                debug_at = time.monotonic()
            if segment is not None:
                speech_id += 1
                duration = len(segment.pcm) / (SAMPLE_RATE * 2)
                if not brief:
                    print(f"[VAD] 语句 {speech_id} 已提交，音频 {duration:.2f}s，原因：{segment.reason}", flush=True)
                try:
                    tasks.put_nowait((speech_id, segment))
                    submitted_count += 1
                except queue.Full as exc:
                    rejected_ids.append(speech_id)
                    raise RuntimeError("处理队列已满（4 句待处理），停止采集；本句未提交，请降低说话频率。") from exc
    finally:
        stopping.set()
        forced_exit = False
        try:
            capture.close()
            if thread.ident is not None:
                if wait_for_current:
                    if active_id[0] is not None:
                        print('[退出] 已停止录音，等待当前语句处理结束后打印 JSON。', flush=True)
                    thread.join()
                else:
                    thread.join(timeout=1)
                    if thread.is_alive():
                        print("[退出] 当前语句尚未处理完毕。", flush=True)
        except KeyboardInterrupt:
            forced_exit = True
            print('[退出] 再次中断，未完成语句将明确标记。', flush=True)
        finally:
            while True:
                try:
                    cancelled_id, _ = tasks.get_nowait()
                except queue.Empty:
                    break
                cancelled_ids.append(cancelled_id)
                tasks.task_done()
            if cancelled_ids:
                print(f'[退出] 未处理的排队语句：{sorted(cancelled_ids)}。', flush=True)
            if session_report is not None:
                session_report.update({
                    'submitted_segments': submitted_count,
                    'cancelled_pending_speech_ids': sorted(cancelled_ids),
                    'rejected_queue_full_speech_ids': rejected_ids,
                    'processing_errors': list(processing_errors),
                    'unfinished_active_speech_id': active_id[0],
                    'forced_exit': forced_exit,
                    'unsubmitted_active_audio_discarded': segmenter.frames is not None,
                })
