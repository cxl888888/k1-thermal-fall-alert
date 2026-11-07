"""Causal real-frame windows and image-space temporal features.

Supervision means a recent annotated controlled descent, not a safe/unsafe
person state. Body-angle labels, action names and cycle IDs are never inputs.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import cached_property
import json
from pathlib import Path

import numpy as np

from train_smoke import digest, excluded_ranges, intersects_exclusions
from feature_ablation import radar_features
from dataset_usage import require_session_enabled
from thermal_features import FEATURE_NAMES, SEQUENCE_NAMES, thermal_window, warm_trace, sequence_features

POSITIVE = {'safe_fall_simulation'}
NEGATIVE = {'stand_still', 'walk', 'turn', 'sit_down', 'sit_still', 'stand_up',
            'bend', 'squat', 'empty', 'enter_zone', 'exit_zone',
            'post_fall_immobile', 'recover_after_fall'}
OVERRIDE_784 = [('da4c4907d8e74184978eea1715365fe8', 'end_s', '784'),
                ('637d22a6df254eef985a0c84abda9b08', 'start_s', '784')]


def conflict_ranges(labels):
    result = []
    for index, left in enumerate(labels):
        for right in labels[index+1:]:
            a = max(float(left['start_s']), float(right['start_s']))
            b = min(float(left['end_s']), float(right['end_s']))
            if a < b:
                result.append((a, b))
    return result


@dataclass
class SessionData:
    folder: Path
    meta: dict
    labels: list
    times: np.ndarray
    grids: np.ndarray
    trace: np.ndarray
    radar: list
    radar_times: np.ndarray
    provenance: dict

    @cached_property
    def exclusions(self):
        return excluded_ranges(self.labels) + conflict_ranges(self.labels)

    def window(self, end_index):
        window = thermal_window(self.trace, self.times, self.grids, end_index, self.exclusions)
        if window is None:
            return None
        indices, features = window
        times, grids = self.times[indices], self.grids[indices]
        a, b = np.searchsorted(self.radar_times, [times[0], times[-1]], side='left')
        rows = self.radar[a:b]
        if (not rows or len(rows)<2
                or np.max(np.diff(self.radar_times[a:b])) > 1
                or self.radar_times[a]-times[0] > 1 or times[-1]-self.radar_times[b-1] > 1):
            rows = []
        return dict(session=self.meta['session_id'], start=float(times[0]), end=float(times[-1]),
                    x=(grids-np.median(grids))/10.,
                    features=features,
                    radar=radar_features(rows),
                    edge_condition=bool(self.trace[indices, 10].mean() > 0),
                    missing_region_frames=int((self.trace[indices, 11]==0).sum()))

    def assign(self, sample):
        """End time is causal; only explicit intervals supply supervision."""
        a, b = sample['start'], sample['end']
        if intersects_exclusions(a, b+1e-9, self.exclusions):
            return None
        for row in self.labels:
            left, right = float(row['start_s']), float(row['end_s'])
            middle = (left+right)/2
            if row['label'] in POSITIVE and a <= middle <= b <= right+.5:
                return dict(y=1, label=row['label'], group=row['label_id'])
        if any(a < float(r['end_s']) and b >= float(r['start_s']) for r in self.labels if r['label'] in POSITIVE):
            return None
        hits = [row for row in self.labels if row['label'] in NEGATIVE
                and float(row['start_s']) <= a and b < float(row['end_s'])]
        if len(hits) != 1:
            return None
        return dict(y=0, label=hits[0]['label'], group=hits[0]['label_id'])


def load_session(folder, review_path=None, overrides=()):
    folder = Path(folder)
    meta = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
    require_session_enabled(meta['session_id'])
    review_path = Path(review_path) if review_path else folder/'review.json'
    if not review_path.is_file():
        raise ValueError(f'{folder.name}需要明确的修订标签，不自动回退原始标签')
    review = json.loads(review_path.read_text(encoding='utf-8'))
    if review['session_id'] != meta['session_id']:
        raise ValueError('修订与会话不匹配')
    manifest = review['source_manifest']
    radar_path = folder/'radar.jsonl'
    radar_omitted = (meta.get('radar_mode') == 'not_connected'
                     and 'radar.jsonl' not in manifest and not radar_path.exists())
    hashes = {}
    for name in ['metadata.json', 'thermal.jsonl', 'radar.jsonl']:
        if name == 'radar.jsonl' and radar_omitted:
            hashes[name] = None
            continue
        if not (folder/name).is_file() or name not in manifest:
            raise ValueError(name+'缺失或不在修订来源清单中')
        hashes[name] = digest(folder/name)
        if hashes[name] != manifest[name]:
            raise ValueError(name+'与标注来源不匹配')
    labels = copy.deepcopy(review['labels'])
    if len({r['label_id'] for r in labels}) != len(labels):
        raise ValueError('标签ID重复，不能进行事件分组')
    applied = []
    for identity, field, value in overrides:
        if field not in ('start_s', 'end_s'):
            raise ValueError('只允许显式覆盖时间边界')
        row = next(r for r in labels if r['label_id']==identity)
        applied.append(dict(label_id=identity, field=field, before=row[field], after=value))
        row[field] = value
    duration = (int(meta['mono_end_ns'])-int(meta['mono_start_ns']))/1e9
    if any(not 0 <= float(r['start_s']) < float(r['end_s']) <= duration for r in labels):
        raise ValueError('标签时间无效')
    frames, invalid = [], 0
    base = int(meta['mono_start_ns'])
    with (folder/'thermal.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            mask = row.get('valid_mask', [])
            if (row.get('valid') is not True or len(row.get('grid_c', []))!=768
                    or len(mask)!=768 or not all(value is True for value in mask)):
                invalid += 1; continue
            grid = np.asarray(row['grid_c'], dtype=np.float32).reshape(24,32)
            if not np.isfinite(grid).all():
                invalid += 1; continue
            frames.append(((int(row['representative_mono_ns'])-base)/1e9, grid))
    times = np.asarray([t for t,g in frames])
    if len(times)<8 or np.any(np.diff(times)<=0):
        raise ValueError('有效帧不足或时间倒退')
    grids = np.stack([g for t,g in frames])
    radar = []
    if not radar_omitted:
        with radar_path.open(encoding='utf-8') as stream:
            radar = [json.loads(line) for line in stream if line.strip()]
    radar = [r for r in radar if r.get('valid') is True]
    radar_times = np.asarray([(int(r['representative_mono_ns'])-base)/1e9 for r in radar])
    if np.any(np.diff(radar_times)<=0):
        raise ValueError('雷达时间倒退')
    provenance = dict(session_id=meta['session_id'], review_id=review['revision_id'],
                      review_path=str(review_path.resolve()), review_sha256=digest(review_path), source_hashes=hashes,
                      boundary_overrides=applied, verified_downloaded_revision=not bool(applied),
                      invalid_frames=invalid, label_snapshot=labels,
                      participant_id=meta.get('participant'),
                      cross_subject_verified=False)
    return SessionData(folder, meta, labels, times, grids, np.stack([warm_trace(g) for g in grids]),
                       radar, radar_times, provenance)


def training_samples(session, negative_cap=6):
    require_session_enabled(session.meta['session_id'])
    samples, selected = [], set()
    for row in session.labels:
        left, right = float(row['start_s']), float(row['end_s'])
        if row['label'] in POSITIVE:
            # Three real history windows from the latter half of the event;
            # no future frames, padding, interpolation or extra event claims.
            anchors = np.linspace((left+right)/2, right, 3)
            indices = sorted({int(np.searchsorted(session.times, anchors[0], side='left')),
                              *[int(np.searchsorted(session.times, anchor, side='right')-1)
                                for anchor in anchors[1:]]})
        elif row['label'] in NEGATIVE:
            within = np.flatnonzero((session.times>=left)&(session.times<right))
            indices = list(within[7::4])
            if len(indices)>negative_cap:
                indices = [indices[i] for i in np.linspace(0,len(indices)-1,negative_cap,dtype=int)]
        else:
            continue
        for index in indices:
            if index in selected:
                continue
            sample = session.window(index)
            assignment = session.assign(sample) if sample else None
            if assignment is None or assignment['group'] != row['label_id']:
                continue
            selected.add(index); sample.update(assignment); samples.append(sample)
    return samples


def continuous_samples(session):
    samples = []
    for index in range(7,len(session.times)):
        sample = session.window(index)
        if sample:
            sample['reference'] = session.assign(sample)
            samples.append(sample)
    return samples
