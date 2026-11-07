"""Offline thermal/radar ablation; other sessions retain unreviewed labels."""
import json
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from train_smoke import (NEGATIVE, POSITIVE, digest, excluded_ranges,
                         intersects_exclusions, load_annotations)


def thermal_features(x):
    yy, xx = np.mgrid[:24, :32]
    trace = []
    for grid in x:
        warmth = np.maximum(grid - np.median(grid) - 2, 0)
        total = warmth.sum()
        mask = warmth > .1
        area = mask.mean()
        if total > 0:
            cx, cy = (warmth * xx).sum()/total/31, (warmth * yy).sum()/total/23
            vx = (warmth * (xx/31-cx)**2).sum()/total
            vy = (warmth * (yy/23-cy)**2).sum()/total
        else:
            cx = cy = vx = vy = 0
        trace.append([area, cx, cy, vx, vy, np.percentile(grid, 95)-np.median(grid)])
    a = np.asarray(trace)
    changes = np.abs(np.diff(x, axis=0)).mean(axis=(1, 2))
    return np.r_[a.mean(0), a.std(0), a.max(0)-a.min(0), a[-1]-a[0],
                 np.mean(changes), np.max(changes), np.std(changes)]


def radar_features(rows):
    if not rows:
        return np.zeros(14, dtype=np.float32)
    states = np.array([int(r['state']) for r in rows])
    output = [1., np.mean(states == 0), np.mean((states & 1) > 0),
              np.mean((states & 2) > 0), np.mean(states[1:] != states[:-1]) if len(states)>1 else 0]
    for key in ('moving_energy', 'static_energy', 'detect_dist_cm'):
        values = np.asarray([float(r[key]) for r in rows])
        output.extend([np.median(values), np.percentile(values,75)-np.percentile(values,25), values[-1]-values[0]])
    return np.asarray(output, dtype=np.float32)


def build(folder):
    meta = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
    base = int(meta['mono_start_ns'])
    labels,review=load_annotations(folder,meta)
    excluded=excluded_ranges(labels)
    if review is not None:
        expected=review.get('source_manifest',{}).get('radar.jsonl')
        if not expected or digest(folder/'radar.jsonl')!=expected:
            raise ValueError('radar.jsonl 与修订来源校验值不一致')
    thermal=[]
    with (folder/'thermal.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line)
            if row.get('valid') and len(row.get('grid_c',[]))==768:
                g=np.asarray(row['grid_c'],dtype=np.float32).reshape(24,32)
                if np.isfinite(g).all(): thermal.append(((int(row['representative_mono_ns'])-base)/1e9,g))
    with (folder/'radar.jsonl').open(encoding='utf-8') as stream:
        radar=[json.loads(line) for line in stream if line.strip()]
    radar=[r for r in radar if r.get('valid')]
    result=[]
    for label in labels:
        name=label['label']
        if name not in NEGATIVE|POSITIVE: continue
        selected=[(t,g) for t,g in thermal if float(label['start_s'])<=t<float(label['end_s'])]
        for i in range(0,len(selected)-7,4):
            block=selected[i:i+8];lo,hi=block[0][0],block[-1][0]
            if intersects_exclusions(lo,hi+1e-9,excluded):continue
            if any(b[0]-a[0]>1 for a,b in zip(block,block[1:])):continue
            current=thermal_features(np.stack([g for t,g in block]))
            context=[(t,g) for t,g in thermal if lo-4<=t<lo]
            context_ok=(not intersects_exclusions(lo-4,lo,excluded)
                        and len(context)>=6 and lo-context[-1][0]<=1
                        and not any(b[0]-a[0]>1 for a,b in zip(context,context[1:])))
            prior=thermal_features(np.stack([g for t,g in context])) if context_ok else np.zeros_like(current)
            shape=np.r_[current,prior,current-prior,int(context_ok)]
            rs=[r for r in radar if lo<=(int(r['representative_mono_ns'])-base)/1e9<=hi]
            rf=radar_features(rs)
            result.append(dict(x=shape,r=rf,y=int(name in POSITIVE),label=name,start=lo,end=hi))
    return result


def stats(model, rows, fusion, title):
    features=np.stack([np.r_[r['x'],r['r']] if fusion else r['x'] for r in rows])
    predictions=model.predict(features)
    counts=Counter((r['y'],int(p)) for r,p in zip(rows,predictions))
    print(title,'日常→日常/下降',counts[(0,0)],counts[(0,1)],'模拟→日常/下降',counts[(1,0)],counts[(1,1)],flush=True)
    walk=Counter(int(p) for r,p in zip(rows,predictions) if r['label']=='walk')
    print('  行走窗口判日常/下降',walk[0],walk[1],flush=True)
    # Merge overlapping predicted windows into candidate intervals, not independent alarms.
    intervals=[]
    for r,p in sorted(zip(rows,predictions),key=lambda pair:pair[0]['start']):
        if not p:continue
        if intervals and r['start']<=intervals[-1][1]:intervals[-1][1]=max(intervals[-1][1],r['end'])
        else:intervals.append([r['start'],r['end']])
    print('  标注区间内的合并候选数',len(intervals),'；不等于连续监测误报次数',flush=True)


def main():
    root=Path(__file__).parent/'data'
    sessions=[build(root/f'session_{i:02}') for i in (1,2,3)]
    train=sessions[0];y=np.array([r['y'] for r in train])
    for fusion in (False,True):
        x=np.stack([np.r_[r['x'],r['r']] if fusion else r['x'] for r in train])
        model=RandomForestClassifier(n_estimators=150,max_depth=5,min_samples_leaf=2,
                                     class_weight='balanced',random_state=42,n_jobs=2).fit(x,y)
        for i,rows in enumerate(sessions[1:],2):stats(model,rows,fusion,f'第{i}轮 '+('热像上下文+雷达' if fusion else '热像时序形状+前置上下文'))
    print('仅探索：优先读取已保存修订，无修订时使用未核对的原始标签；雷达参数未读取；不用于正式精度声明。')


if __name__=='__main__':main()
