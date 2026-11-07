"""Preliminary evaluation on other recordings with unreviewed original labels."""
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch

from train_smoke import (NEGATIVE, POSITIVE, TinyThermal, excluded_ranges,
                         intersects_exclusions, load_annotations)


def evaluate(folder, model):
    meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    start_ns = int(meta['mono_start_ns'])
    labels, review = load_annotations(folder, meta)
    excluded = excluded_ranges(labels)
    frames = []
    with (folder / 'thermal.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            item = json.loads(line)
            if item.get('valid') is not True or len(item.get('grid_c', [])) != 768:
                continue
            grid = np.asarray(item['grid_c'], dtype=np.float32)
            if np.isfinite(grid).all():
                frames.append((int(item['representative_mono_ns']), grid.reshape(24, 32)))
    if any(a[0] >= b[0] for a, b in zip(frames, frames[1:])):
        raise ValueError('热像时间戳不递增')
    samples, observations = [], []
    for row in labels:
        kind = row['label']
        if kind not in NEGATIVE | POSITIVE:
            continue
        left, right = float(row['start_s']), float(row['end_s'])
        selected = [(t, grid) for t, grid in frames if left <= (t-start_ns)/1e9 < right]
        for index in range(0, len(selected) - 7, 4):
            window = selected[index:index + 8]
            if intersects_exclusions((window[0][0]-start_ns)/1e9,
                                     (window[-1][0]-start_ns)/1e9 + 1e-9, excluded):
                continue
            if any(b[0] - a[0] > 1_000_000_000 for a, b in zip(window, window[1:])):
                continue
            x = np.stack([g for _, g in window])
            samples.append((x - np.median(x)) / 10.0)
            observations.append((row['label_id'], kind, (window[0][0]-start_ns)/1e9,
                                 (window[-1][0]-start_ns)/1e9))
    if not samples:
        raise ValueError('没有可用的标注窗口')
    with torch.no_grad():
        pred = model(torch.from_numpy(np.stack(samples))).argmax(dim=1).numpy()
    counts = Counter((kind, int(value)) for (_, kind, _, _), value in zip(observations, pred))
    hits = defaultdict(list)
    for (identity, kind, left, right), value in zip(observations, pred):
        hits[(identity, kind)].append((int(value), left, right))
    print('\n会话:', meta['session_id'], '有效热像帧:', len(frames), '窗口:', len(samples))
    print('标签来源:', review['revision_id'] if review else '未核对的原始标签',
          '；时间窗可重叠，窗口不是独立事件。')
    for kind in sorted({x[1] for x in observations}):
        a, b = counts[(kind, 0)], counts[(kind, 1)]
        print(f'  {kind}: 判日常 {a} 窗，判模拟下降 {b} 窗')
    for row in labels:
        if row['label'] not in POSITIVE:
            continue
        windows = hits.get((row['label_id'], row['label']), [])
        a=float(row['start_s'])
        b=float(row['end_s'])
        print(f'  模拟下降标签 {a:.2f}～{b:.2f}s: {len(windows)} 窗，'
              f'{sum(z[0] for z in windows)} 窗判为下降'+('（无可用窗口）' if not windows else ''))
    totals = Counter((int(kind in POSITIVE), int(value))
                     for (_, kind, _, _), value in zip(observations, pred))
    positives = []
    for row in labels:
        if row['label'] in POSITIVE:
            windows = hits.get((row['label_id'], row['label']), [])
            positives.append(dict(label_id=row['label_id'], start_s=float(row['start_s']),
                                  end_s=float(row['end_s']), windows=len(windows),
                                  predicted_positive=sum(value for value, _, _ in windows)))
    return dict(session_id=meta['session_id'],
                annotation_source=review['revision_id'] if review else 'unmodified_original_labels',
                windows=len(samples), tn=totals[(0, 0)], fp=totals[(0, 1)],
                fn=totals[(1, 0)], tp=totals[(1, 1)], positive_intervals=positives,
                by_label={kind: dict(predicted_negative=counts[(kind, 0)],
                                     predicted_positive=counts[(kind, 1)])
                          for kind in sorted({item[1] for item in observations})})


def main():
    root = Path(__file__).parent
    model = TinyThermal()
    checkpoint = torch.load(root / 'smoke_model.pt', map_location='cpu', weights_only=True)
    model.load_state_dict(checkpoint['state_dict'])
    model.eval()
    torch.set_num_threads(2)
    print('仅使用 session_01 训练的探索模型；优先采用已保存修订，结果不代表实际识别准确率。')
    for folder in (root / 'data/session_02', root / 'data/session_03'):
        evaluate(folder, model)


if __name__ == '__main__':
    main()
