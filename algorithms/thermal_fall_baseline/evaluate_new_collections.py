"""Frozen inference on collection snapshots; pending labels are exploratory only.

Never fits models, changes labels, writes to the board, or controls alarms.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

import numpy as np
import torch

from event_data import SessionData, continuous_samples, warm_trace
from portable_forest import PortableForest
from train_event_models import CONFIG, cnn_scores, evaluate_scores, matrix
from train_smoke import TinyThermal, digest
from dataset_usage import USAGE_PATH, deferred_reason, load_usage, require_session_enabled


def check_sources(folder, snapshot):
    for name in ('metadata.json', 'thermal.jsonl', 'radar.jsonl', 'labels.csv'):
        if digest(folder / name) != snapshot['source_hashes'].get(name):
            raise ValueError(f'{folder.name}/{name}: source snapshot mismatch')


def load_snapshot(folder):
    """Explicit exploratory reader; the strict training reader stays unchanged."""
    folder = Path(folder)
    snapshot = json.loads((folder / 'evaluation_snapshot.json').read_text(encoding='utf-8'))
    if snapshot.get('schema_version') != 'exploratory_evaluation_snapshot_0.1':
        raise ValueError('Expected explicit evaluation snapshot')
    check_sources(folder, snapshot)
    meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    if meta['session_id'] != snapshot['session']['session_id'] or meta['status'] != 'finished':
        raise ValueError('Session identity/status mismatch')
    require_session_enabled(meta['session_id'])
    if meta.get('thermal_simulated', False):
        raise ValueError('Simulated data cannot enter the real-data comparison')
    labels = snapshot['labels']
    duration = (int(meta['mono_end_ns']) - int(meta['mono_start_ns'])) / 1e9
    if len({r['label_id'] for r in labels}) != len(labels):
        raise ValueError('Duplicate label IDs')
    if any(not 0 <= float(r['start_s']) < float(r['end_s']) <= duration for r in labels):
        raise ValueError('Invalid reference boundary')
    base = int(meta['mono_start_ns'])
    frames, rejected = [], 0
    with (folder / 'thermal.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            mask = row.get('valid_mask', [])
            if (row.get('valid') is not True or len(row.get('grid_c', [])) != 768
                    or len(mask) != 768 or not all(v is True for v in mask)):
                rejected += 1
                continue
            grid = np.asarray(row['grid_c'], dtype=np.float32).reshape(24, 32)
            if not np.isfinite(grid).all():
                rejected += 1
                continue
            if row['session_id'] != meta['session_id']:
                raise ValueError('Thermal session mismatch')
            frames.append(((int(row['representative_mono_ns']) - base) / 1e9, grid))
    times = np.asarray([t for t, _ in frames])
    if len(times) < 8 or np.any(np.diff(times) <= 0):
        raise ValueError('Insufficient frames or nonmonotonic thermal time')
    grids = np.stack([g for _, g in frames])
    with (folder / 'radar.jsonl').open(encoding='utf-8') as stream:
        radar = [json.loads(line) for line in stream if line.strip()]
    radar = [r for r in radar if r.get('valid') is True]
    radar_times = np.asarray([(int(r['representative_mono_ns']) - base) / 1e9 for r in radar])
    if np.any(np.diff(radar_times) <= 0):
        raise ValueError('Nonmonotonic radar time')
    return SessionData(folder, meta, labels, times, grids,
                       np.stack([warm_trace(g) for g in grids]), radar, radar_times,
                       dict(invalid_frames=rejected, snapshot=snapshot)), snapshot


def angle_for(label):
    context = label.get('capture_context') or {}
    if isinstance(context, str):
        context = json.loads(context)
    return context.get('body_angle_deg', 'unspecified')


def load_models(root):
    paths = dict(frozen_cnn=root/'two_session_model.pt',
                 event_cnn=root/'event_models/event_cnn.pt',
                 temporal_forest=root/'event_models/temporal_forest.json',
                 temporal_radar_forest=root/'event_models/temporal_radar_forest.json')
    models, training_ids = {}, set()
    for name, path in paths.items():
        if name.endswith('cnn'):
            # Only trusted checkpoints already generated in this project are loaded.
            checkpoint = torch.load(path, map_location='cpu', weights_only=False)
            model = TinyThermal()
            model.load_state_dict(checkpoint['state_dict'])
            models[name] = model.eval()
            provenance = checkpoint.get('provenance', {})
            train = provenance.get('train', checkpoint.get('training', []))
        else:
            models[name] = PortableForest(path)
            train = models[name].provenance.get('train', [])
        training_ids.update(r['session_id'] for r in train)
    return models, paths, training_ids


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=root/'event_models/new_collections')
    args = parser.parse_args()
    usage = load_usage()
    usage_hash = digest(USAGE_PATH)
    folders, skipped = [], []
    for snapshot_path in sorted(args.data_root.glob('*/evaluation_snapshot.json')):
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        sid = snapshot['session']['session_id']
        reason = deferred_reason(sid, usage)
        if reason:
            skipped.append(dict(session_id=sid, reason=reason))
            print('DEFERRED', sid, reason, flush=True)
        else:
            folders.append(snapshot_path.parent)
    if not folders:
        raise ValueError('No eligible evaluation snapshots')
    torch.set_num_threads(2)
    models, paths, training_ids = load_models(root)
    model_hashes = {name: digest(path) for name, path in paths.items()}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = dict(scope='Exploratory frozen-model comparison; pending button labels are not ground truth',
                   official_metrics=None, models=model_hashes, config=CONFIG,
                   training_session_ids=sorted(training_ids), sessions=[], deployable=False,
                   dataset_usage_sha256=usage_hash, deferred_sessions=skipped)
    fields = ['session_id', 'display_name', 'model', 'start_s', 'decision_s', 'score',
              'candidate', 'two_hit_candidate', 'provisional_action', 'provisional_class',
              'label_id', 'angle_deg', 'reference_verified', 'radar_available',
              'warm_region_touches_edge', 'missing_region_frames']
    output = args.output_dir/'predictions.csv'
    with output.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for folder in folders:
            session, snapshot = load_snapshot(folder)
            if session.meta['session_id'] in training_ids:
                raise ValueError('Training/test session overlap')
            rows = continuous_samples(session)
            if not rows:
                raise ValueError(f'{folder.name}: no safe real-frame windows')
            labels = {r['label_id']: r for r in session.labels}
            verified = bool(snapshot['revision_id'] and labels and
                            all(r.get('review_status') == 'human_reviewed' for r in labels.values()))
            item = dict(session_id=session.meta['session_id'],
                        display_name=snapshot['session']['display_name'],
                        seconds=(int(session.meta['mono_end_ns'])-int(session.meta['mono_start_ns']))/1e9,
                        source_hashes=snapshot['source_hashes'], revision_id=snapshot['revision_id'],
                        snapshot_sha256=digest(folder/'evaluation_snapshot.json'),
                        review_status=dict(Counter(r.get('review_status', 'pending') for r in labels.values())),
                        reference_verified=verified, official_metrics=None,
                        labels=session.labels, frames=len(session.times),
                        invalid_frames=session.provenance['invalid_frames'], real_windows=len(rows),
                        provisional_reference_windows=sum(r['reference'] is not None for r in rows),
                        radar_available_windows=sum(bool(r['radar'][0]) for r in rows), models={})
            for name, model in models.items():
                values = (cnn_scores(model, rows) if name.endswith('cnn')
                          else model.predict_score(matrix(rows, model.radar)))
                stats, raw, confirmed = evaluate_scores(rows, values, session.labels)
                angles = defaultdict(lambda: dict(tn=0, fp=0, fn=0, tp=0))
                for row, value, flag, two in zip(rows, values, raw, confirmed):
                    ref = row['reference'] or {}
                    angle = angle_for(labels[ref['group']]) if ref else ''
                    if ref:
                        key = ('tp' if flag else 'fn') if ref['y'] else ('fp' if flag else 'tn')
                        angles[str(angle)][key] += 1
                    writer.writerow(dict(session_id=item['session_id'], display_name=item['display_name'],
                                         model=name, start_s=row['start'], decision_s=row['end'],
                                         score=float(value), candidate=int(flag), two_hit_candidate=int(two),
                                         provisional_action=ref.get('label', ''), provisional_class=ref.get('y', ''),
                                         label_id=ref.get('group', ''), angle_deg=angle,
                                         reference_verified=verified, radar_available=int(bool(row['radar'][0])),
                                         warm_region_touches_edge=int(row['edge_condition']),
                                         missing_region_frames=row['missing_region_frames']))
                item['models'][name] = dict(provisional_button_reference=stats,
                                           provisional_angle_windows=dict(angles),
                                           predicted_positive_windows=int(raw.sum()))
            check_sources(folder, snapshot)
            summary['sessions'].append(item)
            print('EVALUATED', item['display_name'], item['session_id'], len(rows), 'windows', flush=True)
    if model_hashes != {name: digest(path) for name, path in paths.items()}:
        raise RuntimeError('Frozen model changed during evaluation')
    if digest(USAGE_PATH) != usage_hash:
        raise RuntimeError('数据使用清单在评估期间变化，本轮结果不可使用')
    target = args.output_dir/'results.json'
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print('DONE', len(summary['sessions']), 'sessions; frozen model hashes unchanged;', output, flush=True)


if __name__ == '__main__':
    main()
