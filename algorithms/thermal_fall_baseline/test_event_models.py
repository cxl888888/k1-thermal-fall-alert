"""Protect time causality, supervision boundaries and the reporting contract."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from event_data import (FEATURE_NAMES, SEQUENCE_NAMES, SessionData, load_session,
                        sequence_features, training_samples, warm_trace)
from train_event_models import (balanced_interval_weights, confirmed_flags,
                                decision_stats, source_still_matches)
from portable_forest import PortableForest, export_forest
from sklearn.ensemble import RandomForestClassifier
from dataset_usage import deferred_reason, load_usage
from train_smoke import digest, load_annotations


def label(name, left, right, identity='example'):
    return dict(label=name,start_s=str(left),end_s=str(right),label_id=identity)


def fixture(labels=None):
    times = np.arange(0,30,.5)
    grids = np.full((len(times),24,32),20,dtype=np.float32)
    for i in range(len(grids)):
        x = 3+i%12
        grids[i,8:18,x:x+4] = 29
    return SessionData(Path('.'),{'session_id':'synthetic'},labels or [],times,grids,
                       np.stack([warm_trace(g) for g in grids]),[],np.array([]),{})


class EventDataTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        usage = Path(folder.name) / 'dataset_usage.json'
        usage.write_text(json.dumps({
            'schema_version': 'dataset_usage_0.1',
            'deferred_sessions': {'synthetic-deferred': {'reason': 'test fixture'}},
        }), encoding='utf-8')
        scoped_usage = patch('dataset_usage.USAGE_PATH', usage)
        scoped_usage.start()
        self.addCleanup(scoped_usage.stop)

    def test_deferred_session_is_not_a_training_source(self):
        session = fixture([label('walk', 0, 29)])
        for sid in load_usage():
            with self.subTest(session=sid), tempfile.TemporaryDirectory() as directory:
                session.meta['session_id'] = sid
                with self.assertRaisesRegex(ValueError, '已暂缓使用'):
                    training_samples(session)
                folder = Path(directory)
                (folder/'metadata.json').write_text(json.dumps({'session_id': sid}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '已暂缓使用'):
                    load_session(folder)
                with self.assertRaisesRegex(ValueError, '已暂缓使用'):
                    load_annotations(folder, session.meta)

    def test_usage_matches_full_identity_and_keeps_other_sessions(self):
        sid = next(iter(load_usage()))
        self.assertTrue(deferred_reason(sid))
        self.assertIsNone(deferred_reason('unrelated_'+sid[-8:]))
        self.assertIsNone(deferred_reason('synthetic-eligible'))

    def test_exploratory_reader_also_rejects_deferred_session(self):
        from evaluate_new_collections import load_snapshot
        from train_smoke import digest
        sid = next(iter(load_usage()))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'metadata.json').write_text(json.dumps({'session_id': sid, 'status': 'finished'}), encoding='utf-8')
            for name in ('thermal.jsonl', 'radar.jsonl', 'labels.csv'):
                (folder/name).write_text('', encoding='utf-8')
            snapshot = dict(schema_version='exploratory_evaluation_snapshot_0.1',
                            session={'session_id': sid},
                            source_hashes={name: digest(folder/name) for name in
                                           ('metadata.json', 'thermal.jsonl', 'radar.jsonl', 'labels.csv')})
            (folder/'evaluation_snapshot.json').write_text(json.dumps(snapshot), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '已暂缓使用'):
                load_snapshot(folder)

    def test_short_event_uses_only_real_history(self):
        session = fixture([label('stand_still',0,10,'n'),label('safe_fall_simulation',10,11,'p')])
        rows = training_samples(session)
        positive = [r for r in rows if r['y']]
        self.assertGreaterEqual(len(positive),1)
        self.assertTrue(any(r['start'] < 10 for r in positive))
        for row in positive:
            end = np.searchsorted(session.times,row['end'])
            block = session.grids[end-7:end+1]
            np.testing.assert_allclose(row['x'],(block-np.median(block))/10)
            self.assertLessEqual(row['end'],11.5)

    def test_future_frames_and_labels_are_not_model_inputs(self):
        session = fixture([label('walk',0,29)])
        before = session.window(25)
        changed = copy.deepcopy(session)
        changed.grids[26:] = 100
        changed.trace[26:] = 0
        changed.meta.update(orientation='270',participant='another',scene='later')
        changed.labels[0]['note'] = 'different heading and human identity'
        after = changed.window(25)
        for key in ('x','features','radar'):
            np.testing.assert_array_equal(before[key],after[key])

    def test_rates_use_actual_seconds(self):
        session = fixture()
        a = sequence_features(session.trace[:8],session.times[:8],session.grids[:8])
        b = sequence_features(session.trace[:8],session.times[:8]*1.5,session.grids[:8])
        for key in ('mean_rate_cx','pixel_rate_mean'):
            index = SEQUENCE_NAMES.index(key)
            self.assertNotEqual(a[index],0)
            self.assertAlmostEqual(float(a[index]/b[index]),1.5,places=5)
        self.assertEqual(len(a),len(SEQUENCE_NAMES))
        self.assertEqual(len(session.window(20)['features']),len(FEATURE_NAMES))

    def test_unknown_gap_is_not_negative(self):
        session = fixture([label('stand_still',0,5,'a'),label('walk',12,20,'b')])
        self.assertIsNone(session.assign(session.window(20)))
        self.assertEqual(session.assign(session.window(31))['label'],'walk')

    def test_invalid_context_does_not_supply_previous_features(self):
        session = fixture([label('invalid',5,7,'bad'),label('stand_still',8,25,'n')])
        sample = session.window(25)  # current 9..12.5; previous 5..8.5 includes excluded data.
        self.assertIsNotNone(sample)
        self.assertEqual(sample['features'][-1],0)
        self.assertIsNone(session.window(15))

    def test_overlaps_are_unscored(self):
        session = fixture([label('walk',0,15,'a'),label('safe_fall_simulation',14,18,'b')])
        self.assertIsNone(session.window(30))

    def test_missing_frames_are_not_silently_resampled(self):
        session = fixture()
        session.times[20:] += 2
        self.assertIsNone(session.window(23))

    def test_source_change_rejected_before_training(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'metadata.json').write_text(json.dumps({'session_id':'test'}))
            (root/'review.json').write_text(json.dumps(dict(session_id='test',source_manifest={'metadata.json':'wrong'})))
            with self.assertRaisesRegex(ValueError,'metadata.json'):
                load_session(root)

    def test_radar_absence_requires_matching_capture_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meta_path = root/'metadata.json'
            thermal_path = root/'thermal.jsonl'
            review_path = root/'review.json'
            meta = dict(session_id='thermal-only', mono_start_ns=0, mono_end_ns=5_000_000_000,
                        radar_mode='not_connected')
            frames = [dict(valid=True, valid_mask=[True]*768, grid_c=[25.0]*768,
                           representative_mono_ns=i*500_000_000) for i in range(9)]
            thermal_path.write_text('\n'.join(json.dumps(frame) for frame in frames), encoding='utf-8')
            manifest = {'thermal.jsonl': digest(thermal_path)}

            def write_capture():
                meta_path.write_text(json.dumps(meta), encoding='utf-8')
                manifest['metadata.json'] = digest(meta_path)
                review = dict(session_id=meta['session_id'], revision_id='test',
                              labels=[], source_manifest=manifest)
                review_path.write_text(json.dumps(review), encoding='utf-8')

            write_capture()
            session = load_session(root)
            self.assertEqual(session.radar, [])
            self.assertEqual(session.window(7)['radar'][0], 0)
            self.assertTrue(source_still_matches(session))
            (root/'radar.jsonl').touch()
            self.assertFalse(source_still_matches(session))
            with self.assertRaisesRegex(ValueError, 'radar.jsonl'):
                load_session(root)
            (root/'radar.jsonl').unlink()
            manifest['radar.jsonl'] = 'recorded-digest'
            write_capture()
            with self.assertRaisesRegex(ValueError, 'radar.jsonl'):
                load_session(root)
            del manifest['radar.jsonl']
            meta['radar_mode'] = 'live'
            write_capture()
            with self.assertRaisesRegex(ValueError, 'radar.jsonl'):
                load_session(root)

    def test_interval_balancing(self):
        rows = [dict(y=0,session='a',group='long')]*8 + [dict(y=0,session='a',group='short')]
        rows += [dict(y=1,session='a',group='event')]*3
        weights = balanced_interval_weights(rows)
        self.assertAlmostEqual(sum(weights[:8]),weights[8])
        self.assertAlmostEqual(sum(weights[:9]),sum(weights[9:]))

    def test_confirmation_breaks_on_negative_or_gap(self):
        flags = confirmed_flags([0,.5,1,1.5,3,3.5],[True,True,False,True,True,True])
        self.assertEqual(flags.tolist(),[False,True,False,False,False,True])

    def test_event_detection_uses_decision_time(self):
        rows = [dict(start=7,end=10.5,reference=None),
                dict(start=10,end=13.5,reference=dict(y=0,label='walk',group='n')),
                dict(start=15,end=18.5,reference=None)]
        stats = decision_stats(rows,[True,True,True],[label('safe_fall_simulation',8,10,'p')])
        self.assertEqual(stats['event_hits'],1)
        self.assertEqual(len(stats['unmatched_clusters_with_known_negatives']),1)
        self.assertEqual(len(stats['unknown_only_unmatched_clusters']),1)

    def test_portable_forest_matches_estimator_at_thresholds(self):
        x = np.array([[0,1],[1,0],[2,1],[3,0],[4,1],[5,0]],dtype=np.float32)
        model = RandomForestClassifier(n_estimators=5,random_state=42,max_depth=3).fit(x,[0,0,1,0,1,1])
        thresholds = [t for e in model.estimators_ for t,f in zip(e.tree_.threshold,e.tree_.feature) if f==0]
        probe = np.vstack([x]+[np.array([[t,0],[np.nextafter(np.float32(t),np.float32(np.inf)),0]],dtype=np.float32)
                               for t in thresholds])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'forest.json'
            export_forest(model,path,{},False)
            loaded = PortableForest(path)
            np.testing.assert_allclose(loaded.predict_score(probe),model.predict_proba(probe)[:,1],atol=1e-7)
            with self.assertRaises(ValueError):
                loaded.predict_score([[float('nan'),0]])
            content = json.loads(path.read_text())
            content['trees'][0]['left'][0] = 0
            path.write_text(json.dumps(content))
            with self.assertRaisesRegex(ValueError,'越界或包含环'):
                PortableForest(path)

    def test_continuous_early_candidate_is_not_a_later_event_detection(self):
        rows = [dict(start=t-3.5,end=t,reference=dict(y=0,label='walk',group='n') if t<8 else None)
                for t in np.arange(4,12,.5)]
        stats = decision_stats(rows,[True]*len(rows),[label('safe_fall_simulation',8,10,'p')])
        self.assertEqual(stats['event_hits'],0)
        self.assertEqual(len(stats['unmatched_clusters_with_known_negatives']),1)


if __name__=='__main__':
    unittest.main()
