"""Sensor-free targeted protocol, persistence and legacy compatibility checks."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture import Capture, collection_context
from storage import FIELDS, Store, atomic_json, manifest_digest
from targeted import TargetedProgress, capture_context, validate_plan


def plan(target='safe_fall_simulation', angles=None, count=2):
    return validate_plan(dict(target_id=target, angles=angles or ['0', '90'],
                              repetitions_per_angle=count), 'everyday_and_controlled')


class FixtureCapture(Capture):
    """Explicitly simulated test fixture; never opens a sensor or child process."""
    def start(self, data):
        context = collection_context(data)
        if self.meta and self.meta['status'] == 'recording':
            raise ValueError('Test capture already active')
        now = time.monotonic_ns()
        self.folder = self.root / ('simulated_targeted_test_' + uuid.uuid4().hex[:8])
        self.folder.mkdir(parents=True)
        self.meta = dict(session_id=self.folder.name, mono_start_ns=now, scene=data.get('scene','test_fixture'),
                         dataset_name=data.get('dataset_name','模拟测试数据'),
                         participant='test_p01', annotator_id='', status='recording',
                         thermal_simulated=True, radar_mode='not_connected', **context)
        self.active, self.seen, self.label_count = None, {}, 0
        self.targeted = TargetedProgress(context['targeted_collection']) if context.get('targeted_collection') else None
        self.labels = (self.folder / 'labels.csv').open('w', encoding='utf-8', newline='')
        self.events = (self.folder / 'events.jsonl').open('w', encoding='utf-8')
        self.notes = (self.folder / 'notes.jsonl').open('w', encoding='utf-8')
        self._streams = [self.labels, self.events, self.notes]
        self.writer = csv.DictWriter(self.labels, fieldnames=FIELDS)
        self.writer.writeheader()
        self.latest = dict(session_id=self.meta['session_id'], frame_id=0,
                           read_start_mono_ns=now+1000, read_end_mono_ns=now+3000,
                           representative_mono_ns=now+2000, valid=True, valid_pixels=768,
                           grid_c=[24.] * 768, valid_mask=[True] * 768, status='ok', simulated=True)
        (self.folder / 'thermal.jsonl').write_text(json.dumps(self.latest) + '\n', encoding='utf-8')
        atomic_json(self.folder / 'metadata.json', self.meta)
        self._event('session_start', now)
        return self.state()

    def command(self, kind, data):
        if kind != 'stop':
            return super().command(kind, data)
        if not self.meta or data.get('session_id') != self.meta['session_id']:
            raise ValueError('Wrong fixture session')
        if self.meta['status'] != 'recording':
            return self.state()
        end = time.monotonic_ns()
        if self.active:
            self._close_interval(end, 'recording_stopped_truncated')
        if self.targeted:
            event = self.targeted.abort()
            if event:
                self._event(event.pop('event'), end, **event)
            self.meta['targeted_progress'] = self.targeted.snapshot()
        self.meta.update(status='finished', mono_end_ns=end, labels=self.label_count,
                         actual_seconds=(end-self.meta['mono_start_ns'])/1e9)
        self._event('session_end', end, stop_reason='operator_stop')
        for stream in self._streams:
            stream.close()
        self._streams = []
        atomic_json(self.folder / 'metadata.json', self.meta)
        return self.state()

    def shutdown(self):
        if self.meta and self.meta['status'] == 'recording':
            self.command('stop', dict(session_id=self.meta['session_id'], request_id=uuid.uuid4().hex))


class PlanTests(unittest.TestCase):
    def test_invalid_plan(self):
        for extra in [dict(angles=[]), dict(angles=['0', '0']), dict(angles=['360']),
                      dict(repetitions_per_angle=True), dict(repetitions_per_angle=1.5),
                      dict(repetitions_per_angle=0), dict(repetitions_per_angle=101)]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_plan(dict(target_id='walk', angles=['0'], repetitions_per_angle=2, **{}) | extra,
                              'everyday_only')

    def test_controlled_source_required(self):
        with self.assertRaises(ValueError):
            validate_plan(dict(target_id='safe_fall_simulation', angles=['0'], repetitions_per_angle=1),
                          'everyday_only')

    def test_phase_order_and_angle_lock(self):
        progress = TargetedProgress(plan())
        with self.assertRaises(ValueError):
            progress.begin('recover_after_fall', dict(body_angle_deg='0'))
        context = json.loads(progress.begin('stand_still', dict(body_angle_deg='0')))
        self.assertEqual(context['visibility'], 'not_recorded')
        self.assertEqual(progress.finish(True), None)
        with self.assertRaises(ValueError):
            progress.begin('safe_fall_simulation', dict(body_angle_deg='90'))
        self.assertEqual(progress.snapshot()['completed_cycles'], 0)

    def test_one_complete_cycle_not_four_events(self):
        progress = TargetedProgress(plan(count=1))
        for iteration in range(2):
            cycle_ids = set()
            for stage in progress.target['phases']:
                context = json.loads(progress.begin(stage['label'], dict(body_angle_deg='0')))
                cycle_ids.add(context['cycle_id'])
                event = progress.finish(True)
            self.assertEqual(len(cycle_ids), 1)
            self.assertEqual(event['event'], 'targeted_cycle_complete')
            self.assertEqual(progress.completed['0'], iteration+1)
        self.assertEqual(progress.completed['90'], 0)

    def test_interruption_never_counts_as_completion(self):
        progress = TargetedProgress(plan())
        progress.begin('stand_still', dict(body_angle_deg='0'))
        progress.finish(True)
        event = progress.abort()
        self.assertEqual(event['event'], 'targeted_cycle_incomplete')
        self.assertEqual(progress.snapshot()['completed_cycles'], 0)
        self.assertEqual(progress.phase_index, 0)

    def test_context_is_validated(self):
        progress = TargetedProgress(plan('walk'))
        value = progress.begin('walk', dict(body_angle_deg='90', motion_direction='mixed', visibility='partial'))
        self.assertEqual(capture_context(value), value)
        with self.assertRaises(ValueError):
            progress.begin('walk', dict(body_angle_deg='90', visibility='invented'))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='k1_targeted_tests_')
        self.root = Path(self.tmp.name)
        self.capture = FixtureCapture(True, self.root/'raw', self.root/'unused_driver.py')

    def tearDown(self):
        self.capture.shutdown()
        self.tmp.cleanup()

    def start(self, target='safe_fall_simulation'):
        data = dict(recording_profile='targeted', collection_purpose='functional_trial',
                    activity_source='everyday_and_controlled' if target=='safe_fall_simulation' else 'everyday_only',
                    installation_id='test_only', simulation_protocol='simulated fixture only' if target=='safe_fall_simulation' else '',
                    targeted_collection=dict(target_id=target, angles=['0', '90'], repetitions_per_angle=2))
        return self.capture.start(data)

    def command(self, kind, **data):
        request = dict(session_id=self.capture.meta['session_id'], request_id=uuid.uuid4().hex, **data)
        return self.capture.command(kind, request), request

    def phase(self, label, angle='0'):
        self.command('label_start', label=label, body_angle_deg=angle,
                     motion_direction='not_recorded', visibility='full')
        return self.command('label_end', label_id=self.capture.active['label_id'])

    def test_idempotent_completion_and_serialized_context(self):
        self.start()
        for stage in self.capture.targeted.target['phases']:
            state, request = self.phase(stage['label'])
            self.capture.command('label_end', request)
        self.assertEqual(state['targeted']['completed_cycles'], 1)
        self.assertEqual(self.capture.label_count, 4)
        self.command('stop')
        with (self.capture.folder/'labels.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len({json.loads(row['capture_context'])['cycle_id'] for row in rows}), 1)
        events = [json.loads(line) for line in (self.capture.folder/'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(event['event']=='targeted_cycle_complete' for event in events), 1)

    def test_stop_active_or_between_phases_is_incomplete(self):
        for active in [True, False]:
            self.start()
            self.command('label_start', label='stand_still', body_angle_deg='0')
            if not active:
                self.command('label_end', label_id=self.capture.active['label_id'])
            self.command('stop')
            self.assertEqual(self.capture.targeted.snapshot()['completed_cycles'], 0)
            events = [json.loads(line) for line in (self.capture.folder/'events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(event['event']=='targeted_cycle_incomplete' for event in events), 1)

    def test_review_export_retains_angle_context_without_identity(self):
        self.start('walk')
        self.phase('walk', '90')
        self.command('stop')
        store = Store([self.root/'raw'], self.root/'reviewed')
        key = store.scan()['sessions'][0]['key']
        details = store.details(key)
        labels = details['labels']
        expected = labels[0]['capture_context']
        labels[0]['note'] = 'test timestamp review'
        result = store.save_review(key, dict(labels=labels, base_revision_id=None,
                                            source_manifest_sha256=details['source_manifest_sha256']))
        saved = json.loads((Path(result['path'])/'review.json').read_text())
        self.assertEqual(saved['labels'][0]['capture_context'], expected)
        self.assertEqual(saved['annotator_id'], '')
        with (Path(result['path'])/'labels_reviewed.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['capture_context'], expected)

    def test_context_absent_in_legacy_csv_is_supported(self):
        self.start('walk')
        self.phase('walk')
        self.command('stop')
        path = self.capture.folder/'labels.csv'
        with path.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        fields = [name for name in FIELDS if name!='capture_context']
        with path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
            writer.writeheader(); writer.writerows(rows)
        store = Store([self.root/'raw'], self.root/'reviewed')
        key = store.scan()['sessions'][0]['key']
        self.assertEqual(store.details(key)['labels'][0]['capture_context'], '')


if __name__ == '__main__':
    unittest.main()
