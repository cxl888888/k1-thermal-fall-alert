"""Clock/fact-only regressions for live-session input lifecycle failures."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'engine'))
from safety_policy import decide_response
from interaction import Interaction


class FieldStateRegressions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 100.
        self.created = 0

    def waiting(self):
        self.created += 1
        controller = Interaction(Path(self.folder.name) / ('state_' + str(self.created) + '.db'),
                                 decide_response, timeout=20, clock=lambda: self.now)
        out = controller.facts(['疼痛'], True, started=self.now - 1)
        self.assertTrue(controller.prompt_finished(out['prompt'], True))
        return controller

    def idle(self):
        self.created += 1
        return Interaction(Path(self.folder.name) / ('state_' + str(self.created) + '.db'),
                           decide_response, timeout=20, clock=lambda: self.now)

    def test_daily_request_and_chat_do_not_open_safety_episode(self):
        for events in ([], ['请求帮助'], ['明确安全'], ['请求帮助', '明确安全']):
            with self.subTest(events=events):
                controller = self.idle()
                out = controller.facts(events, True, started=101.)
                self.assertEqual(out['route'], 'normal')
                self.assertIsNone(out['prompt'])
                self.assertFalse(out['answer_eligible'])
                self.assertFalse(out['state']['pending'])
                self.assertFalse(out['state']['needs_review'])
                self.assertFalse(out['state']['red'])
                self.assertEqual(out['state']['prompt_generation'], 0)

    def test_ununderstood_standalone_input_is_diagnostic_not_a_safety_answer(self):
        for status in ('ok', 'service_error', 'invalid_output'):
            with self.subTest(status=status):
                controller = self.idle()
                out = controller.facts([], False, status=status, started=101.)
                self.assertEqual(out['route'], 'uncertain')
                self.assertIsNone(out['prompt'])
                self.assertFalse(out['state']['pending'])
                self.assertFalse(out['state']['needs_review'])
                self.assertFalse(out['effective_understood'])
                with closing(sqlite3.connect(controller.db)) as connection:
                    saved = json.loads(connection.execute(
                        "SELECT payload FROM journal WHERE kind='facts'").fetchone()[0])
                self.assertEqual(saved['status'], status)
                self.assertFalse(saved['understood'])

    def test_standalone_processing_failure_does_not_open_safety_episode(self):
        controller = self.idle()
        controller.input_failed('queue_age_exceeded', 101.)
        self.assertFalse(controller.snapshot()['pending'])
        self.assertFalse(controller.snapshot()['needs_review'])
        self.assertFalse(controller.snapshot()['red'])
        with closing(sqlite3.connect(controller.db)) as connection:
            saved = json.loads(connection.execute(
                "SELECT payload FROM journal WHERE kind='input_failed'").fetchone()[0])
        self.assertEqual({key:saved[key] for key in ('reason','started')},
                         {'reason': 'queue_age_exceeded', 'started': 101.})
        self.assertFalse(saved['state']['red'])
        self.assertFalse(saved['state']['pending'])
        self.assertIn('boot_id',saved)

    def test_old_failed_input_does_not_cancel_queued_question(self):
        controller = self.idle()
        out = controller.facts(['疼痛'], True, started=99.)
        controller.input_failed('queue_age_exceeded', started=98.)
        state = controller.snapshot()
        self.assertEqual(state['prompt_status'], 'requested')
        self.assertEqual(state['prompt_generation'], out['prompt'])
        self.assertTrue(state['pending'])
        self.assertTrue(state['needs_review'])
        self.assertTrue(controller.prompt_finished(out['prompt'], True))
        self.assertEqual(controller.snapshot()['prompt_status'], 'delivered')

    def test_prequestion_failed_input_keeps_answer_window(self):
        controller = self.waiting()
        deadline = controller.deadline
        controller.input_failed('queue_age_exceeded', started=99.)
        self.assertEqual(controller.snapshot()['prompt_status'], 'delivered')
        self.assertEqual(controller.deadline, deadline)
        self.now = deadline
        self.assertTrue(controller.tick())

    def test_previous_answer_failure_does_not_cancel_new_answer_window(self):
        controller = self.waiting()
        controller.speech_started(101.)
        controller.speech_started(102.)
        out = controller.facts([], False, started=101.)
        self.assertEqual(out['prompt_kind'], 'clarify')
        self.now = 103.
        self.assertTrue(controller.prompt_finished(out['prompt'], True))
        deadline = controller.deadline
        controller.input_failed('queue_age_exceeded', started=102.)
        state = controller.snapshot()
        self.assertEqual(state['prompt_status'], 'delivered')
        self.assertEqual(state['processing_inputs'], 0)
        self.assertEqual(controller.answer_after, 103.)
        self.assertEqual(controller.deadline, deadline)
        self.now = deadline
        self.assertTrue(controller.tick())

    def test_current_answer_failure_retains_unresolved_risk(self):
        controller = self.waiting()
        controller.speech_started(101.)
        controller.input_failed('queue_age_exceeded', started=101.)
        state = controller.snapshot()
        self.assertEqual(state['prompt_status'], 'processing_failed_requires_review')
        self.assertEqual(state['processing_inputs'], 0)
        self.assertTrue(state['pending'])
        self.assertTrue(state['needs_review'])
        self.assertFalse(state['red'])
        self.assertIsNone(controller.deadline)
        self.now = 200.
        self.assertFalse(controller.tick())

    def test_global_processing_failure_still_interrupts_answer_window(self):
        controller = self.waiting()
        controller.input_failed('runtime_error')
        state = controller.snapshot()
        self.assertEqual(state['prompt_status'], 'processing_failed_requires_review')
        self.assertTrue(state['pending'])
        self.assertTrue(state['needs_review'])
        self.assertIsNone(controller.deadline)

    def test_body_risk_overrides_assistance_and_safe_words(self):
        for event in ('疼痛', '无法起身', '受伤', '呼吸困难', '站立不稳', '意识异常'):
            with self.subTest(event=event):
                controller = self.idle()
                out = controller.facts([event, '请求帮助', '明确安全'], True, started=101.)
                self.assertIsNotNone(out['prompt'])
                self.assertTrue(out['state']['pending'])
                self.assertTrue(out['state']['needs_review'])
                self.assertFalse(out['state']['red'])
                self.assertNotEqual(out['route'], 'safety_response')
                generation = out['prompt']
                controller.prompt_finished(generation, True)
                again = controller.facts([event], True, started=102.)
                self.assertIsNone(again['prompt'])
                self.assertEqual(again['state']['prompt_generation'], generation)

    def test_daily_input_does_not_hide_sensor_or_fault_evidence(self):
        controller = self.idle()
        controller.sensor('thermal', 'fault')
        out = controller.facts(['请求帮助'], True, started=101.)
        self.assertIsNone(out['prompt'])
        self.assertFalse(out['state']['pending'])
        self.assertTrue(out['state']['needs_review'])
        self.assertEqual(out['state']['faults'], ['thermal'])

    def test_assistance_during_safety_inquiry_is_still_a_followup(self):
        controller = self.waiting()
        out = controller.facts(['请求帮助'], True, started=101.)
        self.assertTrue(out['answer_eligible'])
        self.assertEqual(out['route'], 'needs_assistance')
        self.assertIsNone(out['prompt'])
        self.assertTrue(out['state']['pending'])
        self.assertTrue(out['state']['needs_review'])
        self.assertEqual(out['state']['prompt_status'], 'answered_requires_review')

    def test_old_daily_request_cannot_answer_new_risk_question(self):
        controller = self.waiting()
        deadline = controller.deadline
        out = controller.facts(['请求帮助'], True, started=99.)
        self.assertFalse(out['answer_eligible'])
        self.assertEqual(out['route'], 'normal')
        self.assertEqual(out['state']['prompt_status'], 'delivered')
        self.assertTrue(out['state']['pending'])
        self.assertEqual(controller.deadline, deadline)

    def test_ignored_inputs_never_clear_red_latch(self):
        for events, understood in (([], True), ([], False), (['请求帮助'], True),
                                   (['明确安全'], True)):
            with self.subTest(events=events, understood=understood):
                controller = self.idle()
                controller.facts(['跌倒'], True, started=99.)
                controller.caregiver('ack')
                out = controller.facts(events, understood, started=101.)
                self.assertTrue(out['state']['red'])
                self.assertTrue(out['state']['acknowledged'])
                self.assertTrue(out['state']['needs_review'])
                self.assertEqual(out['state']['prompt_status'], 'red_latched')
                self.assertIsNone(out['prompt'])

    def test_partial_understanding_keeps_explicit_risk_events(self):
        for event in ('求救', '跌倒', '无法起身', '呼吸困难'):
            with self.subTest(event=event):
                controller = self.idle()
                out = controller.facts([event], False, started=101.)
                if event in ('求救', '跌倒'):
                    self.assertTrue(out['state']['red'])
                else:
                    self.assertIsNotNone(out['prompt'])
                    self.assertTrue(out['state']['pending'])

    def test_unclear_followup_then_short_noise_does_not_become_unanswered(self):
        controller = self.waiting()
        controller.speech_started(101.)
        out = controller.facts([], False, started=101., answer_quality='context_only')
        self.assertEqual(out['prompt_kind'], 'clarify')
        self.now = 103.
        controller.prompt_finished(out['prompt'], True)
        controller.speech_started(104.)
        controller.facts([], False, started=104.)
        self.assertEqual(controller.snapshot()['prompt_status'], 'answer_unclear_requires_review')
        controller.speech_started(110.)
        controller.speech_abandoned(110.)
        self.assertEqual(controller.snapshot()['prompt_status'], 'answer_unclear_requires_review')
        self.assertEqual(controller.snapshot()['processing_inputs'], 0)
        self.assertIsNone(controller.deadline)
        self.now = 200.
        self.assertFalse(controller.tick())
        self.assertTrue(controller.snapshot()['pending'])
        self.assertFalse(controller.snapshot()['red'])

    def test_clear_assistance_then_short_noise_keeps_answered_phase(self):
        controller = self.waiting()
        controller.speech_started(101.)
        controller.facts(['请求帮助'], True, started=101.)
        controller.speech_started(105.)
        controller.speech_abandoned(105.)
        self.assertEqual(controller.snapshot()['prompt_status'], 'answered_requires_review')
        self.assertIsNone(controller.deadline)
        self.now = 200.
        self.assertFalse(controller.tick())

    def test_short_noise_while_waiting_restores_original_deadline(self):
        controller = self.waiting()
        original_deadline = controller.deadline
        self.now = 110.
        controller.speech_started(self.now)
        self.assertIsNone(controller.deadline)
        self.now = 115.
        controller.speech_abandoned(110.)
        self.assertEqual(controller.snapshot()['prompt_status'], 'delivered')
        self.assertEqual(controller.deadline, original_deadline)
        self.now = original_deadline - .01
        self.assertFalse(controller.tick())
        self.now = original_deadline
        self.assertTrue(controller.tick())
        self.assertEqual(controller.snapshot()['prompt_status'], 'no_answer_requires_review')
        self.assertTrue(controller.snapshot()['pending'])
        self.assertFalse(controller.snapshot()['red'])

    def test_concurrent_short_inputs_restore_only_after_last_input(self):
        controller = self.waiting()
        original_deadline = controller.deadline
        controller.speech_started(101.)
        controller.speech_started(102.)
        controller.speech_abandoned(101.)
        self.assertEqual(controller.snapshot()['processing_inputs'], 1)
        self.assertEqual(controller.snapshot()['prompt_status'], 'reply_processing')
        self.assertIsNone(controller.deadline)
        controller.speech_abandoned(102.)
        self.assertEqual(controller.snapshot()['processing_inputs'], 0)
        self.assertEqual(controller.snapshot()['prompt_status'], 'delivered')
        self.assertEqual(controller.deadline, original_deadline)

    def test_new_answer_is_not_replaced_by_older_input_restore_point(self):
        for discard_order in ((103., 102.), (102., 103.)):
            with self.subTest(discard_order=discard_order):
                controller = self.waiting()
                controller.speech_started(101.)
                controller.speech_started(102.)
                controller.facts(['请求帮助'], True, started=101.)
                controller.speech_started(103.)
                for started in discard_order:
                    controller.speech_abandoned(started)
                self.assertEqual(controller.snapshot()['prompt_status'], 'answered_requires_review')
                self.assertEqual(controller.snapshot()['processing_inputs'], 0)
                self.assertIsNone(controller.deadline)
                self.now = 200.
                self.assertFalse(controller.tick())
                self.now = 100.

    def test_cancel_clears_multiple_inputs_and_retains_unresolved_evidence(self):
        controller = self.waiting()
        generation = controller.snapshot()['prompt_generation']
        controller.sensor('radar', 'candidate')
        controller.sensor('thermal', 'fault')
        controller.speech_started(101.)
        controller.speech_started(102.)
        out = controller.cancel_pending_inputs('normal_shutdown')
        self.assertEqual(out['cancelled_inputs'], 2)
        state = out['state']
        self.assertEqual(state['processing_inputs'], 0)
        self.assertFalse(controller.inflight)
        self.assertTrue(state['pending'])
        self.assertTrue(state['needs_review'])
        self.assertEqual(state['prompt_status'], 'interrupted_requires_review')
        self.assertEqual(state['sensor_pending'], ['radar'])
        self.assertEqual(state['faults'], ['thermal'])
        self.assertIsNone(controller.deadline)
        self.assertIsNone(controller.answer_after)
        self.assertFalse(controller.prompt_finished(generation, True))
        controller.speech_started(103.)
        self.assertFalse(controller.inflight)
        with closing(sqlite3.connect(controller.db)) as connection:
            stored = json.loads(connection.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            cancelled = json.loads(connection.execute("SELECT payload FROM journal WHERE kind='inputs_cancelled'").fetchone()[0])
        self.assertEqual(stored['processing_inputs'], 0)
        self.assertEqual(cancelled['started'], [101., 102.])
        self.assertEqual(cancelled['reason'], 'normal_shutdown')

    def test_cancel_retains_red_latch_acknowledgement_and_sensor_evidence(self):
        controller = self.waiting()
        controller.sensor('radar', 'candidate')
        controller.sensor('thermal', 'fault')
        controller.speech_started(101.)
        controller.speech_started(102.)
        controller.facts(['跌倒'], True, started=103., source='kws_live')
        controller.caregiver('ack')
        out = controller.cancel_pending_inputs('normal_shutdown')
        self.assertEqual(out['cancelled_inputs'], 2)
        self.assertEqual(out['state']['processing_inputs'], 0)
        self.assertTrue(out['state']['red'])
        self.assertTrue(out['state']['acknowledged'])
        self.assertTrue(out['state']['needs_review'])
        self.assertEqual(out['state']['prompt_status'], 'red_latched')
        self.assertEqual(out['state']['sensor_pending'], ['radar'])
        self.assertEqual(out['state']['faults'], ['thermal'])
        self.assertFalse(controller.tick())
        self.assertEqual(controller.cancel_pending_inputs('already_stopped')['cancelled_inputs'], 0)

    def test_cancel_after_safe_reply_still_marks_remaining_audio_unreviewed(self):
        controller = self.waiting()
        controller.speech_started(101.)
        controller.speech_started(102.)
        controller.facts(['明确安全'], True, started=101.)
        self.assertFalse(controller.snapshot()['pending'])
        self.assertEqual(controller.snapshot()['processing_inputs'], 1)
        out = controller.cancel_pending_inputs('normal_shutdown')
        self.assertEqual(out['cancelled_inputs'], 1)
        self.assertEqual(out['state']['processing_inputs'], 0)
        self.assertTrue(out['state']['needs_review'])
        self.assertEqual(out['state']['prompt_status'], 'interrupted_requires_review')
        self.assertFalse(out['state']['red'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
