"""Fixed offline development comparison. No sensor access or alarm deployment."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import random

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
import torch
from torch import nn

from event_data import (FEATURE_NAMES, OVERRIDE_784, POSITIVE, continuous_samples,
                        load_session, training_samples)
from train_smoke import TinyThermal, digest
from portable_forest import PortableForest, export_forest

CONFIG = dict(version='causal_event_development_0.1', frames=8, negative_cap_per_interval=6,
              positive_anchors_per_interval=3, steps=200, seed=42, threshold=.5,
              forest_trees=150, forest_max_depth=5, forest_min_leaf=2,
              confirmation_hits=2, confirmation_max_gap_s=.8, cluster_gap_s=1.1,
              event_match_tail_s=.5, deployable=False)


def ratio(a, b):
    return a/b if b else None


def balanced_interval_weights(rows):
    """Equal total weight per class, then per annotated interval within class."""
    counts = Counter((r['y'], r['session'], r['group']) for r in rows)
    groups = Counter(key[0] for key in counts)
    if set(groups) != {0, 1}:
        raise ValueError('训练样本必须同时有正类和负类')
    return np.asarray([1/(2*groups[r['y']]*counts[(r['y'], r['session'], r['group'])])
                       for r in rows]) * len(rows)


def train_cnn(rows):
    random.seed(CONFIG['seed']); np.random.seed(CONFIG['seed']); torch.manual_seed(CONFIG['seed'])
    model = TinyThermal()
    x = torch.from_numpy(np.stack([r['x'] for r in rows]))
    y = torch.tensor([r['y'] for r in rows], dtype=torch.int64)
    weights = balanced_interval_weights(rows)
    class_indices = {kind: np.flatnonzero(y.numpy()==kind) for kind in (0, 1)}
    rng = np.random.default_rng(CONFIG['seed'])
    optimizer = torch.optim.Adam(model.parameters(), lr=.001)
    for step in range(CONFIG['steps']):
        batch = []
        for kind in (0, 1):
            subset = class_indices[kind]
            batch.extend(rng.choice(subset, 8, replace=True, p=weights[subset]/weights[subset].sum()))
        rng.shuffle(batch)
        optimizer.zero_grad(set_to_none=True)
        loss = nn.functional.cross_entropy(model(x[batch]), y[batch])
        if not torch.isfinite(loss):
            raise RuntimeError('训练损失不是有限值')
        loss.backward(); optimizer.step()
        if step in (0, CONFIG['steps']-1):
            print(f'CNN {step+1}/{CONFIG["steps"]} loss={loss.item():.5f}', flush=True)
    return model.eval()


def matrix(rows, radar=False):
    return np.stack([np.r_[r['features'], r['radar']] if radar else r['features'] for r in rows])


def train_forest(rows, radar=False):
    model = RandomForestClassifier(n_estimators=CONFIG['forest_trees'],
                                   max_depth=CONFIG['forest_max_depth'],
                                   min_samples_leaf=CONFIG['forest_min_leaf'],
                                   random_state=CONFIG['seed'], n_jobs=2)
    return model.fit(matrix(rows, radar), [r['y'] for r in rows],
                     sample_weight=balanced_interval_weights(rows))


def cnn_scores(model, rows):
    result = []
    with torch.no_grad():
        for start in range(0, len(rows), 64):
            batch = torch.from_numpy(np.stack([r['x'] for r in rows[start:start+64]]))
            result.extend(model(batch).softmax(1)[:, 1].tolist())
    return np.asarray(result)


def confirmed_flags(times, flags, hits=2, max_gap=.8):
    """Causal consecutive decisions; an absent/negative decision breaks the run."""
    result, run, previous = [], 0, None
    for t, flag in zip(times, flags):
        if not flag or (previous is not None and t-previous > max_gap):
            run = 0
        run = run+1 if flag else 0
        result.append(run >= hits)
        previous = t
    return np.asarray(result, dtype=bool)


def candidate_clusters(rows, flags):
    result = []
    for index, (row, flag) in enumerate(zip(rows, flags)):
        if not flag:
            continue
        if result and row['end']-result[-1]['end_s'] <= CONFIG['cluster_gap_s']:
            result[-1]['end_s'] = row['end']; result[-1]['indices'].append(index)
        else:
            result.append(dict(start_s=row['end'], end_s=row['end'], indices=[index]))
    return result


def decision_stats(rows, flags, labels):
    counts, by_label = Counter(), {}
    for row, flag in zip(rows, flags):
        ref = row['reference']
        if ref is None:
            continue
        counts[(ref['y'], int(flag))] += 1
        stats = by_label.setdefault(ref['label'], dict(windows=0, candidate_windows=0))
        stats['windows'] += 1; stats['candidate_windows'] += int(flag)
    tn, fp, fn, tp = [counts[key] for key in ((0,0), (0,1), (1,0), (1,1))]
    clusters = candidate_clusters(rows, flags)
    events = [r for r in labels if r['label'] in POSITIVE]
    event_hits = []
    matched_clusters = set()
    for event in events:
        lo, hi = float(event['start_s']), float(event['end_s'])+CONFIG['event_match_tail_s']
        # Match a decision at its real emission time, never at the historical window start.
        matches = [j for j, cluster in enumerate(clusters) if lo <= cluster['start_s'] <= hi]
        matched_clusters.update(matches)
        assessable = any(r['reference'] and r['reference']['group']==event['label_id'] for r in rows)
        first_candidate = clusters[matches[0]]['start_s'] if matches else None
        event_hits.append(dict(label_id=event['label_id'], assessable=assessable, hit=bool(matches),
                               first_candidate_s=first_candidate))
    false_clusters, unknown_clusters, positive_unmatched = [], [], []
    for j, cluster in enumerate(clusters):
        if j in matched_clusters:
            continue
        negative = [rows[i]['reference']['label'] for i in cluster['indices']
                    if rows[i]['reference'] and rows[i]['reference']['y']==0]
        clean = {k: v for k,v in cluster.items() if k!='indices'}
        clean['known_negative_actions'] = sorted(set(negative))
        if negative:
            false_clusters.append(clean)
        elif any(rows[i]['reference'] for i in cluster['indices']):
            # An early ongoing candidate is not a fresh detection of a later event.
            positive_unmatched.append(clean)
        else:
            unknown_clusters.append(clean)
    return dict(tn=tn, fp=fp, fn=fn, tp=tp,
                window_accuracy=ratio(tn+tp,tn+fp+fn+tp),
                precision=ratio(tp,tp+fp), recall=ratio(tp,tp+fn),
                f1=ratio(2*tp,2*tp+fp+fn),
                balanced_accuracy=(ratio(tp,tp+fn)+ratio(tn,tn+fp))/2 if tp+fn and tn+fp else None,
                all_negative_accuracy=ratio(tn+fp,tn+fp+fn+tp),
                by_action=by_label,
                labelled_events=len(events), events_with_scorable_windows=sum(e['assessable'] for e in event_hits),
                event_hits=sum(e['hit'] for e in event_hits), event_details=event_hits,
                total_candidate_clusters=len(clusters),
                unmatched_clusters_with_known_negatives=false_clusters,
                unmatched_clusters_with_positive_reference=positive_unmatched,
                unknown_only_unmatched_clusters=unknown_clusters)


def evaluate_scores(rows, scores, labels):
    raw = np.asarray(scores)>=CONFIG['threshold']
    confirmed = confirmed_flags([r['end'] for r in rows], raw,
                                CONFIG['confirmation_hits'], CONFIG['confirmation_max_gap_s'])
    return dict(raw=decision_stats(rows, raw, labels),
                two_hit=decision_stats(rows, confirmed, labels)), raw, confirmed


def source_still_matches(session):
    for name, expected in session.provenance['source_hashes'].items():
        path = session.folder/name
        if expected is None:
            if path.exists():
                return False
        elif not path.is_file() or digest(path) != expected:
            return False
    review_path = Path(session.provenance['review_path'])
    return review_path.is_file() and digest(review_path) == session.provenance['review_sha256']


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=root/'data')
    parser.add_argument('--review03', required=True, type=Path,
                        help='明确指定第三轮修订快照；不静默回退原始标签')
    parser.add_argument('--confirmed-784-boundaries', action='store_true',
                        help='仅适用用户已确认的第三轮784秒边界修正，来源记入模型')
    parser.add_argument('--output-dir', type=Path, default=root/'event_models')
    args = parser.parse_args()
    torch.set_num_threads(2)
    print('固定方案：前两轮训练，第三轮开发对照；同一志愿者，非独立泛化验收。', flush=True)
    sessions = [load_session(args.data_root/f'session_{i:02}') for i in (1,2)]
    test = load_session(args.data_root/'session_03', args.review03,
                        OVERRIDE_784 if args.confirmed_784_boundaries else ())
    if len({s.meta['session_id'] for s in sessions+[test]}) != 3:
        raise ValueError('训练和对照会话不得重合')
    training = []
    for session in sessions:
        selected = training_samples(session, CONFIG['negative_cap_per_interval'])
        training.extend(selected)
        print('TRAIN', json.dumps(dict(session=session.meta['session_id'],
                                      windows=Counter(str(r['y']) for r in selected),
                                      descent_intervals=len({r['group'] for r in selected if r['y']})), ensure_ascii=False), flush=True)
    if not training:
        raise ValueError('无训练窗口')
    test_rows = continuous_samples(test)
    print('DEV', json.dumps(dict(real_windows=len(test_rows),
                                 scorable=Counter(str(r['reference']['y']) for r in test_rows if r['reference']),
                                 context_present=sum(bool(r['features'][-1]) for r in test_rows),
                                 feature_dimensions=len(FEATURE_NAMES)), ensure_ascii=False), flush=True)
    # Predetermine all configurations; no parameter search on the third session.
    cnn = train_cnn(training)
    thermal = train_forest(training)
    fusion = train_forest(training, radar=True)
    original_path = root/'two_session_model.pt'
    original_digest = digest(original_path)
    original = TinyThermal()
    original.load_state_dict(torch.load(original_path, map_location='cpu', weights_only=False)['state_dict'])
    original.eval()
    args.output_dir.mkdir(exist_ok=True, parents=True)
    provenance = dict(config=CONFIG, train=[s.provenance for s in sessions],
                      development=test.provenance, development_previously_inspected=True,
                      scope='三轮同一志愿者的受控模拟开发对照；非真实跌倒准确率。',
                      feature_names=FEATURE_NAMES)
    # Freeze artifacts before development predictions. These never control live alarms.
    torch.save(dict(state_dict=cnn.state_dict(), provenance=provenance), args.output_dir/'event_cnn.pt')
    joblib.dump(dict(model=thermal, provenance=provenance, radar=False), args.output_dir/'temporal_forest.joblib')
    joblib.dump(dict(model=fusion, provenance=provenance, radar=True), args.output_dir/'temporal_radar_forest.joblib')
    for name, model, radar in [('temporal_forest',thermal,False), ('temporal_radar_forest',fusion,True)]:
        export_forest(model,args.output_dir/(name+'.json'),provenance,radar)
    # Verify saved artifacts can reproduce predictions.
    restored = TinyThermal()
    restored.load_state_dict(torch.load(args.output_dir/'event_cnn.pt', weights_only=False)['state_dict'])
    restored.eval()
    if not np.allclose(cnn_scores(cnn, test_rows[:10]), cnn_scores(restored, test_rows[:10])):
        raise RuntimeError('CNN重新载入结果不一致')
    for name, model, radar in [('temporal_forest',thermal,False), ('temporal_radar_forest',fusion,True)]:
        loaded = joblib.load(args.output_dir/(name+'.joblib'))['model']
        if not np.array_equal(model.predict_proba(matrix(test_rows[:10],radar)),
                              loaded.predict_proba(matrix(test_rows[:10],radar))):
            raise RuntimeError('时序模型重新载入结果不一致')
        portable = PortableForest(args.output_dir/(name+'.json'))
        if not np.allclose(model.predict_proba(matrix(test_rows,radar))[:,1],
                           portable.predict_score(matrix(test_rows,radar)),atol=1e-7,rtol=0):
            raise RuntimeError('可移植JSON模型输出不一致')
    scores = dict(frozen_cnn=cnn_scores(original,test_rows), event_cnn=cnn_scores(cnn,test_rows),
                  temporal_forest=thermal.predict_proba(matrix(test_rows))[:,1],
                  temporal_radar_forest=fusion.predict_proba(matrix(test_rows,True))[:,1])
    output = args.output_dir/'session03_comparison.csv'
    with output.open('w',encoding='utf-8-sig',newline='') as stream:
        fields = ['model','session_id','start_s','decision_s','reference','reference_class',
                  'score','candidate','two_hit_candidate','warm_region_touches_edge','missing_region_frames']
        writer = csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for name, values in scores.items():
            stats, raw, confirmed = evaluate_scores(test_rows,values,test.labels)
            print('RESULT', name, json.dumps(stats,ensure_ascii=False), flush=True)
            for row, score, flag, two in zip(test_rows, values, raw, confirmed):
                ref = row['reference'] or {}
                writer.writerow(dict(model=name,session_id=row['session'],start_s=row['start'],
                                     decision_s=row['end'], reference=ref.get('label',''),
                                     reference_class=ref.get('y',''),score=float(score),candidate=int(flag),
                                     two_hit_candidate=int(two),warm_region_touches_edge=int(row['edge_condition']),
                                     missing_region_frames=row['missing_region_frames']))
    if not all(source_still_matches(s) for s in sessions+[test]) or digest(original_path)!=original_digest:
        raise RuntimeError('来源或原模型发生变化，本轮结果不可使用')
    print('SOURCE_CHECK 原始数据与原模型校验值保持一致。',flush=True)
    print('PREDICTIONS', output, flush=True)
    print('未部署；雷达参数未修改；分数未经概率校准；两次连续确认可能漏掉短事件。',flush=True)


if __name__=='__main__':
    main()
