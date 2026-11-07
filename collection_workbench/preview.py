"""Memory-only thermal preview. It never creates a dataset or opens the radar."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

FALL_RUNTIME = Path(__file__).resolve().parent/'fall_runtime'


class Preview:
    def __init__(self, enabled, driver):
        self.enabled, self.driver = enabled, Path(driver)
        self.lock=threading.RLock()
        self.process=None;self.status='idle';self.latest=None;self.error=None;self.counts={}
        self.started=None;self.ended=None;self.stopping=False;self.monitor=None
        self._reset_test()

    def _reset_test(self):
        self.detector = None
        self.test_error = None
        self.test_events = 0
        self.last_candidate = None
        self.last_score_at = None

    def _test_state(self, now):
        model = FALL_RUNTIME/'thermal_forest.json'
        result = dict(available=self.enabled and model.is_file(), mode='off', score=None,
                      events=self.test_events, last_candidate=self.last_candidate)
        if self.detector is None:
            return result
        result['threshold'] = self.detector.threshold
        if self.error or self.test_error:
            result.update(mode='error', error=self.error or self.test_error)
        elif self.status not in ('starting', 'previewing'):
            result['mode'] = 'stopped'
        elif now - (self.last_score_at or self.started) > (3 if self.last_score_at else 10):
            result['mode'] = 'stale'
        elif self.detector.last_score is None:
            result['mode'] = 'warming'
        else:
            score = self.detector.last_score
            result.update(mode='candidate' if score >= self.detector.threshold else 'monitoring',
                          score=score)
        return result

    def state(self):
        with self.lock:
            row=dict(self.latest) if self.latest else None
            if row:
                for name in ('read_start_mono_ns','read_end_mono_ns','representative_mono_ns'):
                    row[name]=str(row[name])
            now = time.monotonic()
            return dict(state=self.status,latest=row,error=self.error,counts=dict(self.counts),
                        saved=False,elapsed_s=(self.ended or now)-self.started if self.started else 0,
                        fall=self._test_state(now))

    def active(self):
        return self.state()['state'] in ('starting','previewing','stopping')

    def reset(self):
        with self.lock:
            if self.active():raise ValueError('请先关闭预览')
            self.status='idle';self.latest=None;self.error=None;self.started=None;self.ended=None;self.counts={}
            self._reset_test()

    def _spawn(self):
        return subprocess.Popen([sys.executable,'-B','-u',str(Path(__file__).parent/'sensor_worker.py'),
            '--preview','--driver',str(self.driver),'--duration','7200'],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')

    def start(self, fall_test=False):
        if not self.enabled:raise ValueError('本实例仅供回放，不能启动真实热像预览')
        if not self.driver.is_file():raise ValueError('未找到板端热像驱动')
        with self.lock:
            if self.active():raise ValueError('预览已经启动，请先关闭预览')
            detector = None
            if fall_test:
                try:
                    if str(FALL_RUNTIME) not in sys.path:
                        sys.path.insert(0, str(FALL_RUNTIME))
                    from thermal_monitor import ThermalDetector
                    detector = ThermalDetector(FALL_RUNTIME/'thermal_forest.json')
                except (OSError, ImportError, ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f'跌倒测试模型不可用：{exc}') from exc
            self._reset_test()
            self.detector = detector
            self.latest=None;self.error=None;self.counts={};self.stopping=False;self.started=time.monotonic();self.ended=None
            self.status='starting'
            try:self.process=self._spawn()
            except OSError:
                self.status='failed';raise
            self.reader=threading.Thread(target=self._read,args=(self.process,),daemon=True)
            self.monitor=threading.Thread(target=self._watch,args=(self.process,),daemon=True)
            self.reader.start();self.monitor.start()
            return self.state()

    def _read(self,process):
        for line in process.stdout:
            if not line.startswith('WORKBENCH_EVENT '):continue
            try:event=json.loads(line[len('WORKBENCH_EVENT '):])
            except ValueError:continue
            with self.lock:
                if event['kind']=='ready' and not self.stopping:self.status='previewing'
                elif event['kind']=='frame':
                    self.latest=event['row'];self.counts={k:v for k,v in event.items() if k not in ('kind','row')}
                    if self.detector is not None and not self.test_error and not self.stopping:
                        try:
                            candidate = self.detector.push(self.latest)
                            if self.detector.last_score is not None:
                                self.last_score_at = time.monotonic()
                            if candidate:
                                self.test_events += 1
                                self.last_candidate = round(self.last_score_at - self.started, 1)
                        except (ValueError, TypeError, KeyError) as exc:
                            self.test_error = str(exc)
                elif event['kind']=='error':self.error=event['error']

    def _watch(self,process):
        process.wait();self.reader.join(timeout=1)
        if not self.reader.is_alive():process.stdout.close()
        with self.lock:
            if process.returncode and not self.stopping:self.error=self.error or f'预览进程退出码 {process.returncode}'
            self.status='failed' if self.error else 'stopped'
            self.ended=time.monotonic()

    def stop(self):
        with self.lock:
            if not self.active():return self.state()
            self.stopping=True;self.status='stopping';process=self.process
            if process.poll() is None:process.terminate()
            threading.Thread(target=self._ensure_stopped,args=(process,),daemon=True).start()
            return self.state()

    def _ensure_stopped(self,process):
        try:process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=3)

    def shutdown(self):
        self.stop()
        if self.monitor:self.monitor.join(timeout=9)
