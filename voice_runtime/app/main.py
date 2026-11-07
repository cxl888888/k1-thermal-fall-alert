"""K1 interaction entry; independent state DB; old launchers stay intact."""
import argparse
import fcntl
from http.client import HTTPConnection
import importlib
import json
import os
from pathlib import Path
import queue
import selectors
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import wave
from urllib.parse import urlencode

from audio_io import Playback, resolve_nodes, select_prompt, select_feedback, PROMPTS, NODE_DIAGNOSTICS
from interaction import Interaction
from reliability import reply_quality, submit_audio, queue_age

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine-dir', type=Path, default=Path.home() / 'k1_voice_policy_v04_bundle')
    p.add_argument('--kws-dir', type=Path, default=Path.home() / 'k1-kws/scripts')
    p.add_argument('--model', default='k1-safety-v2-r11:latest')
    p.add_argument('--llm-backend', choices=('ollama', 'llamacpp'), default='ollama',
                   help='默认 Ollama；llamacpp 候选需另行启动本机服务并显式指定 --llm-api-base')
    p.add_argument('--llm-api-base',
                   help='服务 API 根地址；llamacpp 候选示例 http://127.0.0.1:11436/v1')
    p.add_argument('--db', type=Path)
    p.add_argument('--listen', action='store_true')
    p.add_argument('--speak', action='store_true')
    p.add_argument('--source')
    p.add_argument('--sink')
    p.add_argument('--llm-timeout', type=float, default=30, help='单次网络读写超时秒数；不做自动重试')
    p.add_argument('--llm-threads', type=int, choices=(1, 2, 3, 4), default=4,
                   help='语义推理线程数；本板性能检查保留4线程')
    p.add_argument('--kws-threads', type=int, choices=(1, 2, 3, 4), default=1,
                   help='常驻关键词模型默认单线程，减少与LLM的资源争抢；可用4作对照')
    p.add_argument('--silence-ms', type=int, default=800,
                   help='VAD句尾等待，默认800ms；缩短前需验证停顿不会被误切')
    p.add_argument('--max-queue-seconds', type=float, default=10, help='超过此排队时长的录音标记未处理，不冒充当前回答')
    p.add_argument('--response-timeout', type=float, default=20,
                   help='演示观察窗口；到期仅保留待核查，不自动升级红色')
    p.add_argument('--no-clarification', action='store_true', help='含糊回答静默保留待核查')
    p.add_argument('--simulate', type=Path, help='仅输入明确标记的事实模拟文件，不冒充 LLM 测试')
    p.add_argument('--audio-check', action='store_true')
    p.add_argument('--family-device-id', help='独立预演家属端的本机设备标识')
    thermal = p.add_mutually_exclusive_group()
    thermal.add_argument('--thermal-worker', type=Path, help='实时读取热像的工作台采集进程')
    thermal.add_argument('--thermal-replay', type=Path, help='离线回放 thermal.jsonl，使用新建测试库')
    p.add_argument('--thermal-model', type=Path)
    bundled_driver = HERE.parents[1] / 'collection_workbench/drivers/show_thermal_calibrated.py'
    p.add_argument('--thermal-driver', type=Path, default=bundled_driver if bundled_driver.is_file()
                   else Path.home()/'k1-thermal/show_thermal_calibrated.py')
    args = p.parse_args()
    family_token = os.environ.get('FAMILY_PRODUCER_TOKEN') if args.family_device_id else None
    if args.family_device_id and (not args.listen or not family_token):
        p.error('家属解除需要联合监听和本机生产者凭证')
    detector = None
    if args.thermal_worker or args.thermal_replay:
        if not args.thermal_model:
            p.error('热像联动需要 --thermal-model')
        if args.thermal_worker and (not args.listen or not args.speak or args.simulate or args.audio_check):
            p.error('实时热像联动需要 --listen --speak')
        if args.thermal_replay and (args.db is None or args.listen or args.speak or args.simulate or args.audio_check):
            p.error('热像回放需要显式的新测试库 --db，且与录音、播报及其他模拟模式互斥')
        from thermal_monitor import ThermalDetector, replay, watch
        detector = ThermalDetector(args.thermal_model)
    args.db = args.db or HERE/'runtime/interaction.db'
    if args.llm_api_base is None:
        if args.llm_backend == 'llamacpp':
            p.error('llamacpp 候选必须显式指定 --llm-api-base，例如 http://127.0.0.1:11436/v1')
        args.llm_api_base = 'http://127.0.0.1:11434/v1'
    import math
    if any(not math.isfinite(x) or x <= 0 for x in (args.llm_timeout, args.max_queue_seconds)):
        p.error('timeout/queue age must be positive finite numbers')
    if args.simulate and (args.listen or args.speak):
        p.error('simulation cannot record or play audio')
    if not 200 <= args.silence_ms <= 3000:
        p.error('silence-ms must be between 200 and 3000')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    lockfile = open(str(args.db) + '.lock', 'w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('此状态库已有运行实例，请先退出旧实例。')
    if args.thermal_replay and args.db.exists():
        p.error('热像回放只允许写入尚不存在的测试库')
    sys.path.insert(0, str(args.engine_dir))
    policy_module = importlib.import_module('safety_policy')
    policy = policy_module.decide_response
    controller = Interaction(args.db, policy, args.response_timeout, allow_clarification=not args.no_clarification)
    results = []
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    playback = None
    kws = None
    jobs = queue.Queue(maxsize=4)
    sound_jobs = queue.Queue(maxsize=4)
    review_feedback_sent = set()
    session = {'mode': 'simulation' if args.simulate else 'audio_probe' if args.audio_check else 'live' if args.listen else 'text',
               'gpio_or_remote_notification': False, 'capture_during_playback': 'discarded_half_duplex_not_AEC',
               'dropped_segments': 0, 'errors': []}
    session['stale_segments'] = 0
    session['product_scope'] = 'emergency_feedback'
    session['policy_version'] = policy_module.POLICY_VERSION
    session['timing_settings'] = dict(silence_ms=args.silence_ms,
                                      llm_threads=args.llm_threads, kws_threads=args.kws_threads,
                                      context_size=1024)
    session['llm_configuration'] = dict(backend=args.llm_backend, api_base=args.llm_api_base,
        runtime_parameters='server launch controls threads/context/template' if args.llm_backend == 'llamacpp'
                           else 'Ollama request options and model parameters')
    session['playback_timings'] = []
    session['playback_failures'] = []

    def show(out):
        state = out['state']
        label = '红色锁存' if state['red'] else '待核查' if state['needs_review'] else '监测'
        print('[状态] ' + label + '；询问=' + state['prompt_status'], flush=True)

    def enqueue_sound(key, generation=None, timing=None):
        feedback_token = (controller.snapshot()['prompt_generation'], key)
        if key == 'review_received' and feedback_token in review_feedback_sent:
            return
        try:
            enqueued_ns = time.monotonic_ns()
            sound_jobs.put_nowait((key, generation, enqueued_ns, timing))
            controller.record_output(key, 'queued', {'enqueued_ns': enqueued_ns,
                                                     'generation': generation, 'pipeline_timing': timing})
            if key == 'review_received':
                review_feedback_sent.add(feedback_token)
        except queue.Full:
            session['errors'].append('playback_queue_full')
            if generation is not None:
                controller.prompt_finished(generation, False)

    def sound_worker():
        while not stop.is_set():
            try:
                key, generation, enqueued_ns, timing = sound_jobs.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                # Stale prompt must not be played after a new red event.
                current = controller.snapshot()
                if generation is not None:
                    if (current['red'] or not current['pending'] or generation != current['prompt_generation']
                            or current['prompt_status'] != 'requested'):
                        controller.record_output(key, 'cancelled_stale', {'enqueued_ns': enqueued_ns})
                        continue
                elif current['red'] and key != 'emergency':
                    controller.record_output(key, 'cancelled_stale', {'enqueued_ns': enqueued_ns})
                    continue
                queue_wait_ms = round((time.monotonic_ns()-enqueued_ns)/1e6, 2)
                controller.record_output(key, 'started', {'enqueued_ns': enqueued_ns,
                                                        'queue_wait_ms': queue_wait_ms})
                succeeded = playback.play(key, response_cue=generation is not None)
                if timing and playback.last_metrics:
                    launch = playback.last_metrics.get('speech_process_launch_mono_ns')
                    speech_end = timing.get('estimated_speech_end_mono_ns')
                    if launch and speech_end:
                        playback.last_metrics['estimated_speech_end_to_process_launch_ms'] = round((launch-speech_end)/1e6, 2)
                session['playback_timings'].append(dict(key=key, generation=generation,
                    queue_wait_ms=queue_wait_ms, metrics=playback.last_metrics, pipeline_timing=timing))
                controller.record_output(key, 'process_succeeded' if succeeded else 'process_failed',
                    {'enqueued_ns': enqueued_ns, 'metrics': playback.last_metrics})
                if not succeeded:
                    session['playback_failures'].append({'key': key, 'generation': generation,
                                                        'enqueued_ns': enqueued_ns})
                    session['errors'].append('audio_process_failed:' + key)
                print('[播放状态] ' + key + ('：进程完成，实际可听需现场确认。' if succeeded
                                               else '：进程失败，保留告警并检查音箱。'), flush=True)
                if generation is not None:
                    accepted = controller.prompt_finished(generation, succeeded, PROMPTS[key])
                    if succeeded and accepted:
                        print('[等待回答] 提示音已结束，现在可以回答；超时只保留待核查。', flush=True)
            finally:
                sound_jobs.task_done()

    def apply_result(events, understood, status, started, raw, origin, quality='clear'):
        transcript = raw.get('transcript') if isinstance(raw, dict) else None
        out = controller.facts(events, understood, status, started, origin, quality, transcript)
        timing = raw.get('pipeline_metrics') if isinstance(raw, dict) else None
        results.append({'input': raw, 'decision': out})
        show(out)
        if out['new_red_trigger'] and playback:
            enqueue_sound('emergency', timing=timing)
        elif (not out['new_red_trigger'] and out['state']['red']
              and set(out['effective_events']) & {'求救', '跌倒'}):
            controller.record_output('emergency', 'duplicate_red_suppressed', {'source': origin})
            print('[告警保持] 已有红色锁存；本次重复危险表达不重复播报。', flush=True)
        elif out['prompt'] is not None:
            if playback:
                enqueue_sound(out.get('prompt_kind') or select_prompt(events), out['prompt'], timing)
            elif args.simulate:
                controller.prompt_finished(out['prompt'], True)
            else:
                print('[文字询问] ' + PROMPTS[out.get('prompt_kind') or select_prompt(events)], flush=True)
                controller.prompt_finished(out['prompt'], True)
        else:
            feedback = select_feedback(out)
            silent_unclear = (args.no_clarification and out['answer_eligible']
                              and out['state']['prompt_status'] == 'answer_unclear_requires_review')
            if playback and feedback and not silent_unclear:
                enqueue_sound(feedback, timing=timing)
        return out

    def apply_sensor(source, kind, simulated, evidence=None):
        out = controller.sensor(source, kind, simulated, evidence)
        summary = {key: value for key, value in evidence.items() if key != 'thermal_frames'} if evidence else evidence
        record = ({'simulated_sensor': [source, kind]} if simulated and evidence is None else
                  {'sensor': {'source': source, 'kind': kind, 'simulated': simulated,
                              'evidence': summary}})
        results.append(dict(record, decision=out))
        show(out)
        if out['prompt']:
            if playback:
                enqueue_sound('sensor', out['prompt'])
            elif not args.thermal_replay:
                controller.prompt_finished(out['prompt'], True)
        return out

    if args.thermal_replay:
        replay(detector, args.thermal_replay, lambda kind, evidence:
               apply_sensor('thermal', kind, True, evidence), stop)
        session.update(mode='thermal_replay', valid_thermal_frames=detector.valid_frames,
                       scored_thermal_windows=detector.scored_windows)
        print(json.dumps({'results': results, 'state': controller.snapshot(), 'session': session},
                         ensure_ascii=False, indent=2))
        return

    if args.simulate:
        for line in args.simulate.read_text(encoding='utf-8-sig').splitlines():
            item = json.loads(line)
            if item.get('command'):
                out = controller.caregiver(item['command'])
                results.append({'simulated': True, 'input': item, 'decision': out})
            else:
                apply_result(item.get('events', []), item.get('understood', True),
                             item.get('status', 'ok'), time.monotonic(), item, 'simulated_facts')
        print(json.dumps({'results': results, 'state': controller.snapshot(), 'session': session}, ensure_ascii=False, indent=2))
        return

    api = importlib.import_module('ai_intent_pipeline')
    original_stdout = sys.stdout
    quiet_output = api._ThreadQuietOutput(original_stdout)
    sys.stdout = quiet_output
    classifier = api.LocalIntentClassifier(api_base=args.llm_api_base, model=args.model, backend=args.llm_backend, compact=True,
        prediction_task='events', timeout_sec=args.llm_timeout, num_threads=args.llm_threads,
        review_unsteadiness=True)

    def classify(text, started, metrics=None):
        controller.speech_started(started)
        snapshot = controller.snapshot()
        classifier.red_alert_before = snapshot['red']
        classifier.awaiting_safety_confirmation = snapshot['pending']
        llm_started_ns = time.monotonic_ns()
        with quiet_output.quiet():
            result = api.classify_transcript(text, classifier)
        llm_finished_ns = time.monotonic_ns()
        if stop.is_set():
            controller.input_failed('shutdown_result_not_applied', started)
            results.append({'cancelled_processing_result': result, 'reason': 'shutdown_requested'})
            return None
        quality = reply_quality(text, controller.snapshot()['pending'])
        result['reply_guard'] = quality
        if metrics:
            detected = metrics.pop('_detected')
            metrics['estimated_speech_end_mono_ns'] = round((detected-metrics['endpoint_silence_ms']/1000)*1e9)
            metrics['llm_started_mono_ns'] = llm_started_ns
            metrics['llm_finished_mono_ns'] = llm_finished_ns
            metrics['estimated_speech_end_to_result_ms'] = (round((time.monotonic() - detected) * 1000)
                                                            + metrics['endpoint_silence_ms'])
            result['pipeline_metrics'] = metrics
        print('[转写] ' + text, flush=True)
        print('[语义] ' + ','.join(result.get('effective_events') or []) + '；' + result['llm_status'], flush=True)
        if quality != 'clear':
            print('[核查] 回答省略上下文，不能作为安全确认。', flush=True)
        return apply_result(result.get('effective_events') or [], bool(result.get('model_understood')),
                            result['llm_status'], started, result, 'llm', quality)

    def command(text):
        if text in ('ack', 'resolve', 'ask'):
            out = controller.caregiver(text)
            results.append({'operator_command': text, 'decision': out})
            show(out)
            if out['prompt']:
                if playback:
                    enqueue_sound('general', out['prompt'])
                else:
                    print('[文字询问] 请告诉我您现在怎么了，需要什么帮助？', flush=True)
                    controller.prompt_finished(out['prompt'], True)
        elif text == 'status':
            print(json.dumps(controller.snapshot(), ensure_ascii=False), flush=True)
        elif text == 'review thermal' and args.thermal_worker:
            apply_sensor('thermal', 'clear', False, {'actor': 'local_console_operator'})
        elif text.startswith('sensor '):
            parts = text.split()
            if len(parts) != 3:
                raise ValueError('用法 sensor radar|thermal candidate|clear|fault|recovered')
            if args.thermal_worker:
                print('[命令] 实时联动使用真实采集；核查完热像可输入 review thermal。', flush=True)
            else:
                print('[模拟传感器，非真实测量]', flush=True)
                apply_sensor(parts[1], parts[2], True)
        elif text:
            if args.listen:
                print('[命令] 监听模式请使用 ask/status/ack/resolve/sensor …/q；请勿将文字命令当作语音回答。', flush=True)
            else:
                classify(text, time.monotonic())

    def tick_worker():
        while not stop.wait(.2):
            if controller.tick():
                print('[未收到有效回答] 保留待核查；未自动判定安全或升级红色。', flush=True)

    def family_resolution_worker():
        path = '/api/device/resolve?' + urlencode(dict(device_id=args.family_device_id, boot_id=controller.boot_id))
        while not stop.wait(.5):
            if not controller.snapshot()['red']:
                continue
            conn = HTTPConnection('127.0.0.1', 8795, timeout=2)
            try:
                conn.request('GET', path, headers={'Authorization': 'Bearer ' + family_token})
                response = conn.getresponse()
                if response.status != 200:
                    continue
                command = json.loads(response.read(4096)).get('command')
                if command and all(type(command.get(key)) is int and command[key] > 0
                                   for key in ('operation_id', 'through_journal_id')):
                    result = controller.resolve_from_family(command['operation_id'], command['through_journal_id'])
                    if result == 'resolved':
                        print('[家属解除] 已解除红色告警，恢复监测。', flush=True)
                    elif result == 'stale':
                        print('[家属解除] 核查后收到新风险，保留告警。', flush=True)
            except (OSError, ValueError, AttributeError, sqlite3.Error):
                continue
            finally:
                conn.close()

    thread = None
    sound_thread = None
    capture = None
    controls = None
    thermal_thread = None
    try:
        threading.Thread(target=tick_worker, daemon=True).start()
        if args.listen or args.speak or args.audio_check:
            source, sink = resolve_nodes(args.source, args.sink)
            print('[路由] 麦克风=' + source + '\n[路由] 音箱=' + sink, flush=True)
            playback = Playback(sink, HERE / 'assets', enabled=args.speak or args.audio_check)
            if args.audio_check:
                session['playback_process_succeeded'] = playback.play('general')
                print('[注意] 播放进程成功不等于人耳已确认有声音。', flush=True)
                return 0 if session['playback_process_succeeded'] else 1
            sound_thread = threading.Thread(target=sound_worker, daemon=True)
            sound_thread.start()
        if not args.listen:
            print('[文字模式] 输入表达；ask/status/ack/resolve；sensor radar candidate 为模拟；q 退出。', flush=True)
            controls = selectors.DefaultSelector()
            controls.register(sys.stdin, selectors.EVENT_READ)
            while not stop.is_set():
                if not controls.select(.25):
                    continue
                line = sys.stdin.readline()
                if not line or line.strip() == 'q':
                    break
                command(line.strip())
            return

        # Loading weights alone does not prime the actual event-extraction prompt.
        # This synthetic startup request must never enter the resident state machine.
        print('[初始化] 正在预热正式语义提示词；此时请先不要说话。', flush=True)
        warm_started = time.monotonic()
        with quiet_output.quiet():
            warm_result = api.classify_transcript('设备启动自检。', classifier)
        session['llm_warmup'] = {
            'status': warm_result['llm_status'],
            'duration_ms': round((time.monotonic() - warm_started) * 1000),
            'error': warm_result.get('llm_error'),
            'applied_to_resident_state': False,
        }
        if warm_result['llm_status'] != 'ok':
            controller.input_failed('llm_warmup_failed')
            print('[初始化提示] 语义模型预热未成功，保留待核查；后续请求会继续尝试。', flush=True)
        else:
            print('[初始化] 语义提示词已预热。', flush=True)
        vad_module = importlib.import_module('k1_voice_vad')
        engine = api.load_asr_engine(Path('/usr/share/spacemit-asr/sensevoice'))
        # Reinstall handlers after native model initialization.
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        vad = vad_module.SileroOnnx(args.engine_dir / 'models/silero_vad_v5.1.2.onnx')
        segmenter = vad_module.SpeechSegmenter(silence_ms=args.silence_ms)

        def worker():
            while not stop.is_set():
                try:
                    task = jobs.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    pcm, started = task['pcm'], task['started']
                    waiting_ms, stale = queue_age(task, time.monotonic(), args.max_queue_seconds)
                    if stale:
                        session['stale_segments'] += 1
                        controller.input_failed('queue_age_exceeded', started)
                        results.append({'unprocessed_audio': 'queue_age_exceeded', 'queue_wait_ms': waiting_ms})
                        print('[积压] 一句录音等待过久，已标记未处理；请完整重说。', flush=True)
                        continue
                    with tempfile.TemporaryDirectory(prefix='k1_interaction_') as folder:
                        path = Path(folder) / 'speech.wav'
                        with wave.open(str(path), 'wb') as audio:
                            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                            audio.writeframes(pcm)
                        # Official ASR wrapper writes progress; keep routine terminal output brief.
                        with quiet_output.quiet():
                            asr = api.transcribe_wav(path, Path('/usr/share/spacemit-asr/sensevoice'), engine)
                        if stop.is_set():
                            continue
                        classify(asr.text, started, {'queue_wait_ms': waiting_ms,
                                                    'asr_processing_ms': asr.processing_time_ms,
                                                    'audio_duration_ms': asr.audio_duration_ms,
                                                    'endpoint_silence_ms': task['endpoint_silence_ms'],
                                                    'audio_cut_reason': task['cut_reason'],
                                                    '_detected': task['detected']})
                except Exception as exc:
                    session['errors'].append(str(exc))
                    if stop.is_set():
                        controller.input_failed('shutdown_processing_error', started)
                        results.append({'cancelled_processing_error': str(exc), 'reason': 'shutdown_requested'})
                    else:
                        apply_result([], False, 'processing_error', started, {'error': str(exc)}, 'pipeline_error')
                finally:
                    jobs.task_done()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # Reserve the immediate KWS path for direct cries for help.
        sys.path.insert(0, str(args.kws_dir))
        kws_class = importlib.import_module('realtime_kws').RealtimeKWS
        os.environ['PULSE_SOURCE'] = source
        kws = kws_class(project_root=str(args.kws_dir), device='plug:pulse',
                        num_threads=args.kws_threads)

        def kws_event(event):
            if playback.blocked():
                return
            if event.keyword in {'救命', '救救我'}:
                apply_result(['求救'], True, 'ok', time.monotonic(), event.to_dict(), 'kws_live')

        kws.start(on_event_callback=kws_event)
        if args.family_device_id:
            threading.Thread(target=family_resolution_worker, daemon=True).start()
        if args.thermal_worker:
            thermal_thread = threading.Thread(
                target=watch, args=(detector, args.thermal_worker, args.thermal_driver,
                                    lambda kind, evidence: apply_sensor('thermal', kind, False, evidence),
                                    stop, 'thermal' in controller.snapshot()['faults']), daemon=True)
            thermal_thread.start()
        capture = vad_module.microphone_frames(source)
        controls = selectors.DefaultSelector()
        if sys.stdin.isatty():
            controls.register(sys.stdin, selectors.EVENT_READ)
        started = None
        blocked_last = False
        print('[就绪] 自动监听；播报期间暂停识别（非全双工 AEC），听到滴声后回答。Ctrl+C 退出。', flush=True)
        for pcm in capture:
            if stop.is_set():
                break
            if kws._process.poll() is not None:
                raise RuntimeError('KWS 快速通道已退出，停止本轮并保留待核查，请检查录音设备。')
            if controls.select(0):
                line = sys.stdin.readline()
                if line.strip() == 'q':
                    break
                command(line.strip())
            # Drain/reset while the question is queued as well as while it plays.
            # This establishes a clean boundary for attributing the next utterance.
            blocked = playback.blocked() or controller.snapshot()['prompt_status'] == 'requested'
            if blocked or blocked_last:
                if started is not None:
                    controller.speech_abandoned(started)
                vad.reset()
                segmenter = vad_module.SpeechSegmenter(silence_ms=args.silence_ms)
                started = None
                blocked_last = blocked
                continue
            before = segmenter.frames is not None
            segment = segmenter.feed(pcm, vad.probability(pcm))
            if not before and segmenter.frames is not None:
                # Pre-roll is already reset at the playback boundary; subtracting
                # it would incorrectly move an immediate answer before the question.
                started = time.monotonic()
                controller.speech_started(started)
            if segment:
                submitted = submit_audio(jobs, segment.pcm, started or time.monotonic(), segment.detected_mono,
                                         controller.snapshot()['prompt_generation'], time.monotonic,
                                         segment.endpoint_silence_ms, segment.reason)
                if not submitted:
                    session['dropped_segments'] += 1
                    controller.input_failed('queue_full', started)
                    results.append({'unprocessed_audio': 'queue_full', 'started_mono': started})
                    print('[积压] 本句未提交，请等待处理完成。', flush=True)
                started = None
            elif before and segmenter.frames is None and started is not None:
                controller.speech_abandoned(started)
                started = None
    except KeyboardInterrupt:
        print('\n[退出] 正在停止监听。', flush=True)
    except Exception as exc:
        if stop.is_set():
            session['termination_note'] = str(exc)
            print('[退出] 终止信号后录音已停止。', flush=True)
        else:
            session['errors'].append(str(exc))
            print('[运行异常] ' + str(exc), flush=True)
            apply_result([], False, 'runtime_error', None, {'error': str(exc)}, 'runtime_error')
    finally:
        stop.set()
        if thermal_thread:
            thermal_thread.join(timeout=6)
            session.update(valid_thermal_frames=detector.valid_frames,
                           scored_thermal_windows=detector.scored_windows)
            if thermal_thread.is_alive():
                session['errors'].append('thermal_worker_did_not_stop')
        if capture:
            capture.close()
        if controls:
            controls.close()
        if kws:
            kws.stop()
        if thread:
            thread.join(timeout=35)
            session['unfinished_processing'] = thread.is_alive()
        if sound_thread:
            sound_thread.join(timeout=28)
            session['unfinished_playback'] = sound_thread.is_alive()
        session['unprocessed_segments'] = jobs.qsize()
        if not ((thread and thread.is_alive()) or (sound_thread and sound_thread.is_alive())):
            cancelled_queue = []
            while True:
                try:
                    task = jobs.get_nowait()
                except queue.Empty:
                    break
                cancelled_queue.append({key: value for key, value in task.items() if key != 'pcm'})
                jobs.task_done()
            session['cancelled_queue_inputs'] = cancelled_queue
            session['remaining_queue_segments'] = jobs.qsize()
            session['input_shutdown'] = controller.cancel_pending_inputs('session_stopped')
        else:
            session['input_shutdown'] = {'cancelled': False, 'reason': 'worker_still_running'}
        if thread and thread.is_alive():
            session['errors'].append('processing_worker_did_not_stop')
        if sound_thread and sound_thread.is_alive():
            session['errors'].append('playback_worker_did_not_stop')
        session['audio_node_diagnostics'] = list(NODE_DIAGNOSTICS)
        sys.stdout = original_stdout
        print('\n[本次完整 JSON]', flush=True)
        print(json.dumps({'results': results, 'state': controller.snapshot(), 'session': session}, ensure_ascii=False, indent=2), flush=True)
    return 1 if session['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main() or 0)
