"""Experimental temporal warm-region candidates, never ground truth or a red alert."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics

from analysis import frame_features, integer_ns

SOURCE_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class FusionSettings:
    delta_c: float = 2.0
    min_component_pixels: int = 8
    baseline_frames: int = 6
    baseline_span_s: float = 2.0
    baseline_stability: float = 0.12
    max_frame_gap_s: float = 1.5
    max_centroid_jump: float = 0.30
    height_ratio: float = 0.70
    aspect_growth: float = 1.40
    centroid_drop: float = 0.10
    axis_rotation_degrees: float = 40.0
    min_area_ratio: float = 0.50
    max_area_ratio: float = 2.0
    hold_s: float = 2.0
    hold_frames: int = 3
    cooldown_s: float = 10.0

    def __post_init__(self):
        for key,value in asdict(self).items():
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'融合参数{key}必须为有限正数')
        for key in ('min_component_pixels','baseline_frames','hold_frames'):
            if not isinstance(getattr(self,key),int):
                raise ValueError(f'{key}必须为整数')
        if (self.baseline_frames < 2 or self.hold_frames < 2 or self.min_component_pixels > 768
                or self.delta_c > 50 or self.height_ratio >= 1
                or self.min_area_ratio >= self.max_area_ratio or self.axis_rotation_degrees > 90):
            raise ValueError('融合参数范围或顺序无效')


class TemporalFusion:
    VERSION = 'thermal_radar_candidates_0.1_experimental'

    def __init__(self, settings=None):
        self.settings = settings or FusionSettings()
        self.baseline = None
        self.samples = deque(maxlen=max(self.settings.baseline_frames*3,30))
        self.previous = None
        self.last_at = None
        self.transition_start = None
        self.transition_count = 0
        self.last_candidate = None
        self.episode_emitted = False

    def reset_track(self):
        self.baseline = self.previous = None
        self.samples.clear()
        self.transition_start = None
        self.transition_count = 0
        self.episode_emitted = False

    def update(self, row, radar=None):
        cfg = self.settings
        at = integer_ns(row.get('representative_mono_ns', (integer_ns(row['read_start_mono_ns'])+integer_ns(row['read_end_mono_ns']))//2))
        features = frame_features(row,cfg.delta_c)
        out = dict(algorithm_version=self.VERSION, frame_id=row.get('frame_id'), mono_ns=str(at),
                   state='data_unavailable', candidate=False, proposed_red_alert=False,
                   follow_up_recommended=False, radar_support='unavailable', metrics=None,
                   interpretation='探索性温区变化候选，不证明人体或跌倒，不修改人工标签及告警状态')
        gap = (at-self.last_at)/1e9 if self.last_at is not None else None
        self.last_at = at
        if gap is not None and (gap <= 0 or gap > cfg.max_frame_gap_s):
            self.reset_track()
            out['reason'] = '温度时间倒退或断档；连续跟踪重新建立'
            return out
        if not features['usable'] or row.get('relative',{}).get('valid') is False:
            self.reset_track()
            out['reason'] = '预热、无效或结构异常的温度帧'
            return out
        if features['largest_pixels'] < cfg.min_component_pixels or features.get('touches_image_edge'):
            self.reset_track()
            out.update(state='tracking_unavailable',reason='温区过小或接触画面边缘；不把消失解释为安全或跌倒')
            return out
        box,center = features['bbox_px'],features['centroid_px']
        geom = dict(at=at,area=features['largest_pixels'],height=box['height']/24,
                    aspect=box['width']/box['height'],x=center['x']/31,y=center['y']/23,
                    axis=features['principal_axis_degrees'])
        if self.previous:
            jump=math.hypot(geom['x']-self.previous['x'],geom['y']-self.previous['y'])
            if jump > cfg.max_centroid_jump:
                self.reset_track()
                out.update(state='tracking_unavailable',reason='温区跳变，可能换目标；不拼接为同一事件')
                return out
        self.previous=geom
        if self.baseline is None:
            self.samples.append(geom)
            recent=list(self.samples)[-cfg.baseline_frames:]
            stable=(len(recent)>=cfg.baseline_frames and (at-recent[0]['at'])/1e9>=cfg.baseline_span_s
                    and max(x['x'] for x in recent)-min(x['x'] for x in recent)<=cfg.baseline_stability
                    and max(x['y'] for x in recent)-min(x['y'] for x in recent)<=cfg.baseline_stability
                    and max(x['height'] for x in recent)-min(x['height'] for x in recent)<=cfg.baseline_stability)
            if stable:
                self.baseline={key:statistics.median(x[key] for x in recent)
                               for key in ('area','height','aspect','x','y','axis')}
                self.baseline['axis']=math.degrees(math.atan2(
                    statistics.mean(math.sin(math.radians(2*x['axis'])) for x in recent),
                    statistics.mean(math.cos(math.radians(2*x['axis'])) for x in recent)))/2
            out.update(state='baseline_ready' if stable else 'building_baseline',reason='建立同一温区的稳定参考形状；参考不等于站姿真值')
            return out
        base=self.baseline
        rotation=abs((geom['axis']-base['axis']+90)%180-90)
        ratios=dict(height_ratio=geom['height']/base['height'],aspect_growth=geom['aspect']/base['aspect'],
                    area_ratio=geom['area']/base['area'],centroid_drop=geom['y']-base['y'],axis_rotation_degrees=rotation)
        changed=(cfg.min_area_ratio<=ratios['area_ratio']<=cfg.max_area_ratio and
                 ((ratios['height_ratio']<=cfg.height_ratio and ratios['aspect_growth']>=cfg.aspect_growth)
                  or (rotation>=cfg.axis_rotation_degrees and ratios['centroid_drop']>=cfg.centroid_drop)))
        out['metrics']=ratios
        if radar and radar.get('available') and not radar.get('issues'):
            state=radar['row'].get('state')
            out['radar_support']='presence_reported' if state in (1,2,3) else 'no_target_reported' if state==0 else 'diagnostic_state'
        if not changed:
            self.transition_start=None
            self.transition_count=0
            self.episode_emitted=False
            out.update(state='tracking',reason='未达到候选几何变化条件；不代表现场已确认安全')
            return out
        self.transition_start=self.transition_start if self.transition_start is not None else at
        self.transition_count+=1
        held=(at-self.transition_start)/1e9
        out.update(state='shape_change_pending',reason='温区形状已变化，等待持续性条件',hold_seconds=held)
        if held>=cfg.hold_s and self.transition_count>=cfg.hold_frames:
            cooldown=self.last_candidate is not None and (at-self.last_candidate)/1e9<cfg.cooldown_s
            out.update(state='persistent_shape_change',reason='持续形状变化，仅建议语音核查；雷达无人或失联均不能否决候选')
            if not cooldown and not self.episode_emitted:
                self.last_candidate=at
                self.episode_emitted=True
                out.update(candidate=True,follow_up_recommended=True)
        return out


def analyze_session(session, settings=None, include_trace=False):
    settings=settings or FusionSettings()
    session.ensure_index()
    engine=TemporalFusion(settings)
    counts=Counter()
    candidates=[]
    trace=[]
    from radar_stream import RadarReplay
    from storage import safe_child
    before=session.manifest_signature()
    radar=RadarReplay(session.path,session.meta)
    # One read handle for the bounded immutable-session pass; avoid thousands of
    # repeated metadata reads and file opens on large Windows recordings.
    with safe_child(session.path,'thermal.jsonl').open('rb') as stream:
        for index,offset in enumerate(session.offsets):
            stream.seek(offset)
            row=json.loads(stream.readline())
            row['relative']=session.frames[index]
            at=(integer_ns(row['read_start_mono_ns'])+integer_ns(row['read_end_mono_ns']))//2
            evidence=radar.at(at)
            evidence['issues']=radar.issues
            result=engine.update(row,evidence)
            result['time_s']=row['relative']['time_s']
            counts[result['state']]+=1
            if result['candidate']:
                candidates.append(result)
            if include_trace:
                trace.append(result)
    if before!=session.manifest_signature():
        raise ValueError('分析期间原始会话发生变化，结果未提交')
    output=dict(algorithm_version=engine.VERSION,settings=asdict(settings),session_id=session.sid,
                software_sha256=SOURCE_SHA256,
                processed_frames=len(session.frames),state_counts=dict(counts),candidates=candidates,
                action_labels_used_for_prediction=False,accuracy_computed=False,
                limitations=['未验证的探索阈值，不是已训练模型或真实跌倒准确率',
                    '图像坐标受安装角度影响；温区不保证为人体，背景热源及换目标会造成偏差',
                    '参考形状需稳定建立，进入前已处于低位的目标可能不产生变化候选',
                    '没有精确动作标注及正常躺卧对照时，不能评估检出率或误报率',
                    '同板接收时钟对齐不等于传感器硬件同步；雷达没有坐标或跌倒真值'])
    if include_trace:
        output['trace']=trace
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session',type=Path)
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--trace',action='store_true')
    args=parser.parse_args()
    from storage import Session
    settings=FusionSettings(**json.loads(args.settings.read_text(encoding='utf-8'))) if args.settings else FusionSettings()
    print(json.dumps(analyze_session(Session(args.session),settings,args.trace),ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
