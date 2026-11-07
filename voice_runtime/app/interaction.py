"""Persistent demo interaction; consumes facts policy, no GPIO or remote calls."""
from contextlib import closing
import json
import math
import sqlite3
import threading
import time
from pathlib import Path


class Interaction:
    def __init__(self, db, policy, timeout=20, clock=time.monotonic, *, allow_clarification=True):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('response timeout must be positive')
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        self.timeout = timeout
        self.clock = clock
        self.allow_clarification = allow_clarification
        try:
            self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            self.boot_id = None  # Offline tests must not invent a real boot ID.
        self.lock = threading.RLock()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, mono_ns INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')
            row = conn.execute('SELECT payload FROM state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else {
            'red': False, 'acknowledged': False, 'pending': False,
            'sensor_pending': [], 'faults': [], 'needs_review': False,
            'prompt_generation': 0, 'prompt_status': 'none',
        }
        self.state.setdefault('clarification_count', 0)
        self.state.setdefault('processing_inputs', 0)
        self.inflight = set()
        self._reply_resume = None
        self._answered_generation = None
        self.state['processing_inputs'] = 0
        # A process restart cannot reuse the previous process's monotonic deadline.
        self.deadline = None
        self.answer_after = None
        if self.state['pending']:
            self.state['needs_review'] = True
            self.state['prompt_status'] = 'interrupted_requires_review'
        self._save('startup', {'restored_red': self.state['red']})

    def _save(self, kind, payload):
        payload = dict(payload, state=self.snapshot(), boot_id=self.boot_id)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO journal(mono_ns,kind,payload) VALUES(?,?,?)',
                         (time.monotonic_ns(), kind, json.dumps(payload, ensure_ascii=False)))
            conn.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(self.state, ensure_ascii=False),))

    def snapshot(self):
        with self.lock:
            return dict(self.state, sensor_pending=list(self.state['sensor_pending']), faults=list(self.state['faults']))

    def record_output(self, key, stage, details=None):
        """Record delivery diagnostics; never acknowledge or clear an alert."""
        stages = {'queued', 'started', 'process_succeeded', 'process_failed',
                  'cancelled_stale', 'duplicate_red_suppressed'}
        if not isinstance(key, str) or not key or stage not in stages:
            raise ValueError('invalid output diagnostic')
        with self.lock:
            self._save('audio_output', {'key': key, 'stage': stage,
                'audible_confirmation': None, 'details': details or {}})

    def _request(self):
        self.state['pending'] = True
        self.state['needs_review'] = True
        # One automatic prompt per pending episode; no endless retry or timeout escalation.
        if self.state['prompt_status'] != 'none':
            return None
        self.state['prompt_generation'] += 1
        self.state['clarification_count'] = 0
        self.state['prompt_status'] = 'requested'
        return self.state['prompt_generation']

    def facts(self, events, understood, status='ok', started=None, source='llm', answer_quality='clear', transcript=None):
        with self.lock:
            self.inflight.discard(started)
            self.state['processing_inputs'] = len(self.inflight)
            if status != 'ok':
                events, understood = [], False
            if answer_quality != 'clear':
                events, understood = [], False
            question_finished = self.answer_after
            eligible = (self.state['pending'] and self.state['prompt_status'] not in {
                            'none', 'requested', 'playback_failed', 'interrupted_requires_review'}
                        and self.answer_after is not None and started is not None and started >= self.answer_after)
            if eligible:
                self._answered_generation = self.state['prompt_generation']
            decision = self.policy(events, understood, bool(eligible), self.state['red'])
            # Recompute against current latch under lock; never trust stale inference context.
            self.state['red'] = decision.proposed_red_alert_latched
            prompt = None
            if self.state['red']:
                self.state['needs_review'] = True
                self.state['pending'] = False
                self.state['prompt_status'] = 'red_latched'
                self.deadline = None
            elif decision.route == 'safety_response':
                self.deadline = None
                if self.state['sensor_pending'] or self.state['faults']:
                    self.state['needs_review'] = True
                    self.state['prompt_status'] = 'safe_answer_waiting_for_evidence'
                else:
                    self.state.update(pending=False, needs_review=False, prompt_status='none')
                    self.answer_after = None
            elif decision.follow_up_required:
                prompt = self._request()
                if eligible:
                    self.deadline = None
                    unclear = not understood or not events or answer_quality != 'clear'
                    if status != 'ok':
                        self.state['prompt_status'] = 'processing_failed_requires_review'
                    elif unclear and self.allow_clarification and self.state['clarification_count'] < 1:
                        self.state['clarification_count'] += 1
                        self.state['prompt_generation'] += 1
                        self.state['prompt_status'] = 'requested'
                        self.answer_after = None
                        prompt = self.state['prompt_generation']
                    else:
                        self.state['prompt_status'] = 'answer_unclear_requires_review' if unclear else 'answered_requires_review'
            self._restore_reply_phase()
            result = {'route': decision.route, 'reason': decision.reason,
                      'new_red_trigger': decision.new_red_trigger,
                      'prompt': prompt, 'answer_eligible': bool(eligible), 'source': source,
                      'prompt_kind': 'clarify' if prompt and self.state['clarification_count'] else None,
                      'answer_quality': answer_quality,
                      'effective_events': list(events), 'effective_understood': understood,
                      'utterance_started_mono': started, 'question_finished_mono': question_finished,
                      'state': self.snapshot()}
            self._save('facts', dict(result, events=list(events), understood=understood, status=status,
                                     transcript=transcript if isinstance(transcript, str) else None))
            return result

    def speech_started(self, started):
        with self.lock:
            if started in self.inflight:
                return
            if (self.state['pending'] and not self.state['red'] and self.answer_after is not None
                    and started >= self.answer_after and self.state['prompt_status'] != 'requested'):
                previous_phase, previous_deadline = self.state['prompt_status'], self.deadline
                # Concurrent recordings share the last real phase, rather than
                # treating another recording's temporary reply_processing as it.
                if previous_phase != 'reply_processing':
                    self._reply_resume = {'phase': previous_phase, 'deadline': previous_deadline,
                                          'generation': self.state['prompt_generation'],
                                          'answer_after': self.answer_after}
                self.inflight.add(started)
                self.state['processing_inputs'] = len(self.inflight)
                self.deadline = None
                self.state['prompt_status'] = 'reply_processing'
                self._save('reply_started', {'started': started, 'previous_phase': previous_phase,
                                             'previous_deadline': previous_deadline})

    def _restore_reply_phase(self):
        if self.inflight:
            return
        resume, self._reply_resume = self._reply_resume, None
        if (resume is None or not self.state['pending'] or self.state['red']
                or self.state['prompt_status'] != 'reply_processing'):
            return
        self.state['prompt_status'] = resume['phase']
        self.deadline = None
        if (resume['phase'] == 'delivered'
                and resume['generation'] == self.state['prompt_generation']
                and resume['answer_after'] == self.answer_after
                and self._answered_generation != self.state['prompt_generation']):
            # Restore the original window: short noise cannot keep extending it.
            self.deadline = resume['deadline']

    def input_failed(self, reason, started=None):
        with self.lock:
            self.inflight.discard(started)
            self.state['processing_inputs'] = len(self.inflight)
            # A failed standalone input is diagnostic evidence, not a new risk
            # episode. Existing risk must still remain visibly unresolved.
            if self.state['pending'] or self.state['red']:
                self.state['needs_review'] = True
            current_answer_failed = (
                started is not None and self.answer_after is not None
                and started >= self.answer_after
                and self.state['prompt_status'] not in {
                    'none', 'requested', 'playback_failed', 'interrupted_requires_review'})
            # Old queued audio cannot cancel a question that has not played or
            # a newer answer window. A failure without an input timestamp still
            # represents a global processing failure, rather than old audio.
            if self.state['pending'] and (started is None or current_answer_failed):
                self.state['prompt_status'] = 'processing_failed_requires_review'
                self.deadline = None
            self._restore_reply_phase()
            self._save('input_failed', {'reason': reason, 'started': started})

    def speech_abandoned(self, started):
        with self.lock:
            if started not in self.inflight:
                return
            self.inflight.discard(started)
            self.state['processing_inputs'] = len(self.inflight)
            self._restore_reply_phase()
            self._save('short_audio_discarded', {'started': started,
                                                'phase_after_discard': self.state['prompt_status']})

    def cancel_pending_inputs(self, reason='session_stopped'):
        """Call during shutdown after stopping capture/workers; never resolve risk."""
        with self.lock:
            cancelled = sorted(self.inflight)
            previous_phase = self.state['prompt_status']
            self.inflight.clear()
            self._reply_resume = None
            self.state['processing_inputs'] = 0
            self.deadline = self.answer_after = None
            if self.state['pending'] or cancelled:
                self.state['needs_review'] = True
                self.state['prompt_status'] = 'interrupted_requires_review'
            if self.state['red']:
                self.state['needs_review'] = True
                self.state['prompt_status'] = 'red_latched'
            self._save('inputs_cancelled', {'reason': reason, 'started': cancelled,
                                            'previous_phase': previous_phase})
            return {'cancelled_inputs': len(cancelled), 'state': self.snapshot()}

    def prompt_finished(self, generation, succeeded, spoken_text=None):
        with self.lock:
            if (generation != self.state['prompt_generation'] or self.state['red'] or not self.state['pending']
                    or self.state['prompt_status'] == 'interrupted_requires_review'):
                return False
            self.state['prompt_status'] = 'delivered' if succeeded else 'playback_failed'
            self.answer_after = self.clock() if succeeded else None
            self.deadline = self.answer_after + self.timeout if succeeded else None
            if not succeeded:
                self.state['needs_review'] = True
            payload = {'generation': generation, 'succeeded': succeeded}
            if succeeded and spoken_text:
                payload['spoken_text'] = spoken_text
            self._save('prompt_finished', payload)
            return True

    def tick(self):
        with self.lock:
            if self.deadline is None or self.clock() < self.deadline:
                return False
            self.deadline = None
            self.state.update(needs_review=True, prompt_status='no_answer_requires_review')
            self._save('no_answer', {'action': 'retain_pending_no_automatic_red_escalation'})
            return True

    def sensor(self, source, kind, simulated=True, evidence=None):
        if source not in ('radar', 'thermal') or kind not in ('candidate', 'clear', 'fault', 'recovered'):
            raise ValueError('unknown sensor event')
        with self.lock:
            prompt = None
            pending = set(self.state['sensor_pending'])
            faults = set(self.state['faults'])
            if kind == 'candidate':
                pending.add(source)
                if not self.state['red']:
                    prompt = self._request()
            elif kind == 'clear':
                pending.discard(source)
            elif kind == 'fault':
                faults.add(source)
                self.state['needs_review'] = True
            else:
                faults.discard(source)
            self.state['sensor_pending'], self.state['faults'] = sorted(pending), sorted(faults)
            # Clear/recovered data never acts as a resident answer or clears a red latch.
            payload = {'source': source, 'kind': kind, 'simulated': simulated}
            if evidence is not None:
                payload['evidence'] = evidence
            self._save('sensor', payload)
            return {'prompt': prompt, 'state': self.snapshot()}

    def caregiver(self, command):
        with self.lock:
            if command == 'ack':
                self.state['acknowledged'] = self.state['red']
            elif command == 'resolve':
                # Only local operator input reaches this method; not transcript/model text.
                self.state.update(red=False, acknowledged=False, pending=False,
                                  needs_review=bool(self.state['sensor_pending'] or self.state['faults']),
                                  prompt_status='none')
                self.deadline = self.answer_after = None
                self.inflight.clear()
                self._reply_resume = None
                self._answered_generation = None
                self.state['processing_inputs'] = 0
            elif command == 'ask':
                if self.state['red']:
                    return {'prompt': None, 'state': self.snapshot()}
                self.state['prompt_status'] = 'none'
                prompt = self._request()
                self._save('caregiver_ask', {})
                return {'prompt': prompt, 'state': self.snapshot()}
            else:
                raise ValueError('unknown caregiver command')
            self._save('caregiver_' + command, {'actor': 'local_console_operator'})
            return {'prompt': None, 'state': self.snapshot()}

    def resolve_from_family(self, operation_id, through_journal_id):
        with self.lock:
            if not self.state['red'] or self.inflight:
                return
            with closing(sqlite3.connect(self.db)) as conn:
                if conn.execute("SELECT 1 FROM journal WHERE kind='caregiver_resolve' AND "
                                "json_extract(payload,'$.resolution.operation_id')=?",
                                (operation_id,)).fetchone():
                    return
                anchor = conn.execute('SELECT 1 FROM journal WHERE id=?', (through_journal_id,)).fetchone()
                rows = conn.execute("SELECT kind,payload FROM journal WHERE id>? AND kind IN ('facts','sensor')",
                                    (through_journal_id,)).fetchall()
            stale = not anchor
            for kind, payload in rows:
                event = json.loads(payload)
                if (kind == 'facts' and set(event.get('effective_events') or ()) & {'求救', '跌倒'}
                        or kind == 'sensor' and event.get('kind') == 'candidate'):
                    stale = True
                    break
            result = {'actor': 'family_phone', 'resolution': {
                'operation_id': operation_id, 'outcome': 'stale' if stale else 'resolved'}}
            previous = self.snapshot()
            if not stale:
                self.state.update(red=False, acknowledged=False, pending=False, sensor_pending=[],
                                  needs_review=bool(self.state['faults']), prompt_status='none')
            try:
                self._save('caregiver_resolve', result)
            except Exception:
                self.state = previous
                raise
            if not stale:
                self.deadline = self.answer_after = None
                self._reply_resume = self._answered_generation = None
            return result['resolution']['outcome']
