import codecs
import datetime
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from speech_event import SpeechEvent

class RealtimeKWS:
    DEDUP_WINDOW_SEC = 3.0  # 改成 3 秒去重

    def __init__(self, project_root: str = None, device: str = "plug:pulse", num_threads: int = 4):
        if type(num_threads) is not int or not 1 <= num_threads <= 4:
            raise ValueError('KWS num_threads must be an integer in 1..4')
        self.num_threads = num_threads
        if project_root is None:
            project_root = Path(__file__).resolve().parents[1]
        self.root = Path(project_root)
        self.bin_path = Path.home() / "install/bin/sherpa-onnx-keyword-spotter-alsa"
        self.model_dir = Path.home() / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        self.keywords_file = Path(__file__).with_name("keywords.txt")
        if not self.keywords_file.is_file():
            self.keywords_file = Path.home() / "k1-kws" / "scripts" / "keywords.txt"
        self.device = device
        self._process = None
        self._thread = None
        self._stop = threading.Event()
        self._last_emit = {}
        self.on_event = None

    def start(self, on_event_callback):
        self.on_event = on_event_callback
        self._stop.clear()

        # 启动前校验依赖文件，缺失时给出明确报错（避免子进程静默失败）
        required = [self.bin_path, self.keywords_file]
        required += [
            self.model_dir / "tokens.txt",
            self.model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            self.model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            self.model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        ]
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            raise FileNotFoundError("KWS 依赖文件缺失:\n  " + "\n  ".join(missing))

        if self._process is not None and self._process.poll() is None:
            raise RuntimeError("KWS 已在运行，请勿重复 start()")

        cmd = [
            str(self.bin_path),
            f"--tokens={self.model_dir / 'tokens.txt'}",
            f"--encoder={self.model_dir / 'encoder-epoch-12-avg-2-chunk-16-left-64.onnx'}",
            f"--decoder={self.model_dir / 'decoder-epoch-12-avg-2-chunk-16-left-64.onnx'}",
            f"--joiner={self.model_dir / 'joiner-epoch-12-avg-2-chunk-16-left-64.onnx'}",
            f"--keywords-file={self.keywords_file}",
            f"--num-threads={self.num_threads}",
            self.device
        ]
        print(f"[KWS] 正在启动麦克风监听: {self.device}")
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        # Linux 版 sherpa-onnx 的 Display 会用回车符刷新终端，并且 JSON
        # 结果后可能没有换行。不能用 readline() 等换行，否则结果会拖到
        # 下一次关键词输出时才交给 Python。这里按字节流读取，JSON 闭合即解析。
        decoder = json.JSONDecoder()
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending_json = ""
        pending_log = ""

        def emit_log(text: str, flush: bool = False):
            nonlocal pending_log
            pending_log += text
            parts = re.split(r"[\r\n]", pending_log)
            if flush:
                ready = parts
                pending_log = ""
            else:
                ready = parts[:-1]
                pending_log = parts[-1]

            for item in ready:
                item = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", item).strip()
                if not item or re.fullmatch(r"\d+:", item):
                    continue
                print(f"[KWS] {item}")

        while not self._stop.is_set():
            try:
                chunk = os.read(self._process.stdout.fileno(), 4096)
            except OSError as exc:
                if not self._stop.is_set():
                    print(f"[KWS] 读取识别器输出失败: {exc}")
                break
            if not chunk:
                break

            pending_json += utf8_decoder.decode(chunk)
            while True:
                json_start = pending_json.find("{")
                if json_start < 0:
                    emit_log(pending_json)
                    pending_json = ""
                    break

                # 结果序号和终端控制字符只作为日志处理，不参与 JSON 解析。
                emit_log(pending_json[:json_start], flush=True)
                try:
                    obj, json_end = decoder.raw_decode(pending_json, json_start)
                except json.JSONDecodeError:
                    pending_json = pending_json[json_start:]
                    if len(pending_json) > 65536:
                        print("[KWS] JSON 结果超过 64 KiB，丢弃不完整结果")
                        pending_json = ""
                    break

                pending_json = pending_json[json_end:]
                if not isinstance(obj, dict):
                    print("[KWS] 忽略非对象形式的 JSON 结果")
                    continue

                keyword = obj.get("keyword")
                if not keyword:
                    continue

                now = time.monotonic()
                last_emit = self._last_emit.get(keyword, 0.0)
                if now - last_emit < self.DEDUP_WINDOW_SEC:
                    print(f"[KWS] 同关键词去重 {self.DEDUP_WINDOW_SEC:.1f}s: {keyword}")
                    continue

                self._last_emit[keyword] = now
                timestamps = obj.get("timestamps") or []
                try:
                    audio_offset_ms = int(float(timestamps[0]) * 1000) if timestamps else -1
                except (TypeError, ValueError):
                    audio_offset_ms = -1

                received_at = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(
                    f"[KWS-TRACE {received_at}] keyword={keyword!r} "
                    f"audio_offset_ms={audio_offset_ms}"
                )
                # 注意: input_mode 必须为 "mic_live"，否则状态机不会触发告警
                event = SpeechEvent(
                    keyword=keyword,
                    source="sherpa-onnx-k1",
                    input_mode="mic_live",
                    audio_offset_ms=audio_offset_ms,
                )
                if self.on_event:
                    try:
                        self.on_event(event)
                    except Exception as exc:
                        # 回调异常不能杀死监听线程，否则系统静默瘫痪
                        print(f"[KWS] 事件回调异常: {exc}")

        emit_log("", flush=True)

        # 读循环结束 = 子进程退出。非主动停止时给出明确提示
        if not self._stop.is_set() and self._process is not None:
            rc = self._process.poll()
            if rc is None:
                rc = self._process.wait()
            print(f"[KWS] 子进程意外退出（返回码 {rc}），语音监测已停止！")

    def stop(self):
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._thread:
            self._thread.join(timeout=2)
