"""Single-session thermal training smoke test; never a deployment or accuracy claim."""
import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from decimal import Decimal
from pathlib import Path

import numpy as np
import torch
from torch import nn
from dataset_usage import require_session_enabled

NEGATIVE = {'stand_still', 'walk', 'turn', 'sit_down', 'sit_still',
            'stand_up', 'bend', 'squat'}
POSITIVE = {'safe_fall_simulation'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_annotations(folder, meta):
    """Prefer the saved revision; never silently evaluate superseded raw labels."""
    require_session_enabled(meta['session_id'])
    path = folder / 'review.json'
    if path.exists():
        review = json.loads(path.read_text(encoding='utf-8'))
        if review['session_id'] != meta['session_id']:
            raise ValueError('标签修订与会话不匹配')
        for name in ('metadata.json', 'thermal.jsonl'):
            expected = review.get('source_manifest', {}).get(name)
            if not expected or digest(folder / name) != expected:
                raise ValueError(f'{name} 与修订来源校验值不一致')
        return review['labels'], review
    base = int(meta['mono_start_ns'])
    with (folder / 'labels.csv').open(encoding='utf-8-sig', newline='') as stream:
        labels = [dict(row,
                       start_s=str(Decimal(int(row['start_mono_ns']) - base) / 1_000_000_000),
                       end_s=str(Decimal(int(row['end_mono_ns']) - base) / 1_000_000_000))
                  for row in csv.DictReader(stream)]
    return labels, None


def excluded_ranges(labels):
    return [(float(row['start_s']), float(row['end_s'])) for row in labels
            if row['label'] in {'invalid', 'unknown', 'occlusion'}]


def intersects_exclusions(start, end, excluded):
    return any(start < right and end > left for left, right in excluded)


def load_data(folder, window=8, stride=4):
    meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    labels, review = load_annotations(folder, meta)
    if review is None:
        raise ValueError('训练流程自检需要已保存的标签修订')
    excluded = excluded_ranges(labels)
    base = int(meta['mono_start_ns'])
    frames, invalid = [], 0
    with (folder / 'thermal.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('valid') is not True or len(row.get('grid_c', [])) != 768:
                invalid += 1
                continue
            grid = np.asarray(row['grid_c'], dtype=np.float32)
            if not np.isfinite(grid).all():
                invalid += 1
                continue
            frames.append(((int(row['representative_mono_ns']) - base) / 1e9, grid.reshape(24, 32)))
    if any(a[0] >= b[0] for a, b in zip(frames, frames[1:])):
        raise ValueError('热像帧时间戳未严格递增')
    samples, tags, events = [], [], []
    for label in labels:
        name = label['label']
        if name not in NEGATIVE | POSITIVE:
            continue
        start, end = float(label['start_s']), float(label['end_s'])
        selected = [(t, g) for t, g in frames if start <= t < end]
        for i in range(0, len(selected) - window + 1, stride):
            block = selected[i:i + window]
            if intersects_exclusions(block[0][0], block[-1][0] + 1e-9, excluded):
                continue
            if any(b[0] - a[0] > 1.0 for a, b in zip(block, block[1:])):
                continue
            stack = np.stack([g for _, g in block])
            # A single window-level offset preserves temporal change.
            stack = (stack - np.median(stack)) / 10.0
            samples.append(stack)
            tags.append(int(name in POSITIVE))
            events.append(label['label_id'])
    if not samples or set(tags) != {0, 1}:
        raise ValueError('没有足够的有效窗口或缺少正、负类别')
    return np.stack(samples), np.array(tags, dtype=np.int64), events, invalid, review


class TinyThermal(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(nn.Conv2d(8, 12, 3, padding=1), nn.ReLU(),
                                 nn.MaxPool2d(2), nn.Conv2d(12, 16, 3, padding=1),
                                 nn.ReLU(), nn.AdaptiveAvgPool2d(1), nn.Flatten(),
                                 nn.Linear(16, 2))

    def forward(self, x):
        return self.net(x)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path(__file__).parent / 'data/session_01')
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--steps', type=int, default=100)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'smoke_model.pt')
    args = parser.parse_args()
    random.seed(42); np.random.seed(42); torch.manual_seed(42)
    if args.device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA 不可用；不自动冒充 GPU 运行')
    torch.set_num_threads(2)
    x, y, event_ids, invalid, review = load_data(args.data)
    counts = Counter(y.tolist())
    by_class = {c: np.flatnonzero(y == c) for c in (0, 1)}
    print(f'会话 {review["session_id"]}，修订 {review["revision_id"]}')
    print(f'排除无效帧 {invalid}；负类窗口 {counts[0]}，模拟下降候选窗口 {counts[1]}')
    print('正类标注区间数', len(set(e for e, label in zip(event_ids, y) if label == 1)),
          '；仅表示标签数量，不视为独立跌倒次数')
    print('这是同一会话的训练流程自检，没有独立测试集，不能推算识别准确率。')
    model = TinyThermal().to(args.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    loss_fn = nn.CrossEntropyLoss()
    for step in range(args.steps):
        indices = np.concatenate([np.random.choice(by_class[c], 8, replace=True) for c in (0, 1)])
        np.random.shuffle(indices)
        batch = torch.from_numpy(x[indices]).to(args.device)
        target = torch.from_numpy(y[indices]).to(args.device)
        optimizer.zero_grad(set_to_none=True)
        loss = loss_fn(model(batch), target)
        if not torch.isfinite(loss):
            raise RuntimeError('损失非有限值')
        loss.backward(); optimizer.step()
        if step in (0, args.steps - 1):
            print(f'训练步 {step + 1}/{args.steps}，批次损失 {loss.item():.4f}')
    torch.save({'state_dict': model.cpu().state_dict(), 'kind': 'non_deployable_smoke_test',
                'session_id': review['session_id'], 'revision_id': review['revision_id'],
                'note': 'single session only; no independent evaluation; thermal only'}, args.output)
    print('流程自检权重：', args.output)


if __name__ == '__main__':
    main()
