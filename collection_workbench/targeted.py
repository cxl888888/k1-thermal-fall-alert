"""Manual targeted protocols and cycle counts; no automatic action recognition."""
from __future__ import annotations

import copy
import json
import uuid


def phase(label, name, instruction):
    return dict(label=label, name=name, instruction=instruction)


TARGETED_SCHEMA = dict(
    version='targeted_collection_0.1',
    angles=[dict(id=value, name=name) for value, name in [
        ('0', '0° 面向传感器'), ('90', '90° 侧向A'), ('180', '180° 背向传感器'),
        ('270', '270° 侧向B'), ('45', '约45° 斜向')]],
    motion_directions=[dict(id=value, name=name) for value, name in [
        ('not_recorded', '未记录'), ('across_view', '横向移动'), ('toward_sensor', '靠近传感器'),
        ('away_from_sensor', '远离传感器'), ('turning', '转向'), ('mixed', '含不同转向')]],
    visibility_options=[dict(id=value, name=name) for value, name in [
        ('not_recorded', '未记录'), ('full', '完整可见'), ('partial', '部分轮廓'),
        ('uncertain', '无法确认')]],
    targets=[
        dict(id='walk', name='行走（可含自然转向）', controlled=False,
             phases=[phase('walk', '行走', '记录一次连续行走；可包含路线中的自然转向。停止后结束本次，原地转身可单独选专项。')]),
        dict(id='seated_cycle', name='坐下—静坐—起身', controlled=False,
             phases=[phase('sit_down', '坐下', '开始坐下时开始标记；坐稳后结束，下一阶段为静坐。'),
                     phase('sit_still', '静坐', '坐稳后开始，准备起身前结束；时长按现场安排。'),
                     phase('stand_up', '起身', '开始起身时标记，站稳后结束；完成整个周期才计1次。')]),
        dict(id='squat', name='蹲下—保持—恢复', controlled=False,
             phases=[phase('squat', '蹲下周期', '从开始下蹲，经过保持，到恢复站稳，记为一个完整周期；不要求每帧都是蹲姿。')]),
        dict(id='bend', name='弯腰—保持—直起', controlled=False,
             phases=[phase('bend', '弯腰周期', '从开始弯腰，经过保持，到直起站稳，记为一个完整周期。')]),
        dict(id='stand_still', name='站立静止', controlled=False,
             phases=[phase('stand_still', '站立静止', '站稳后开始，移动前结束；一段持续站立记1次，不按帧计次。')]),
        dict(id='turn', name='原地转身', controlled=False,
             phases=[phase('turn', '原地转身', '从开始转身至新朝向稳定；角度记录本次起始朝向，运动方向可选“转向”。')]),
        dict(id='safe_fall_simulation', name='受控模拟下降完整周期', controlled=True,
             phases=[phase('stand_still', '预备站稳', '先确认软垫、保护和视野条件。预备站稳单独标记，不计作下降。'),
                     phase('safe_fall_simulation', '受控下降', '仅记录有保护的受控下降过程；到达低姿态并稳定后结束，不要求失控摔倒或追求速度。'),
                     phase('post_fall_immobile', '低姿态保持', '下降稳定后单独记录保持；重复标记同一次保持不能算多次模拟下降。'),
                     phase('recover_after_fall', '恢复', '按现场安排恢复，站稳后结束；四阶段完整完成才计1次。')]),
        dict(id='recover_after_fall', name='低姿态恢复起身', controlled=True,
             phases=[phase('recover_after_fall', '恢复起身', '从已准备好的低姿态开始恢复，站稳后结束；本专项只计恢复，不计作新的模拟下降。')]),
    ])


def target_for(identity):
    target = next((item for item in TARGETED_SCHEMA['targets'] if item['id'] == identity), None)
    if target is None:
        raise ValueError('请选择有效的专项动作')
    return target


def validate_plan(data, activity_source):
    if not isinstance(data, dict):
        raise ValueError('请设置专项动作、角度与每角度次数')
    target = target_for(data.get('target_id'))
    angles = data.get('angles')
    allowed = {item['id'] for item in TARGETED_SCHEMA['angles']}
    if (not isinstance(angles, list) or not 1 <= len(angles) <= len(allowed)
            or any(not isinstance(item, str) or item not in allowed for item in angles)
            or len(set(angles)) != len(angles)):
        raise ValueError('请至少选择一个角度，不要重复选择')
    count = data.get('repetitions_per_angle')
    if isinstance(count, bool):
        raise ValueError('每角度次数须为1到100的整数')
    if isinstance(count, str) and count.isascii() and count.isdecimal():
        count = int(count)
    if not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError('每角度次数须为1到100的整数')
    if activity_source == 'equipment_only':
        raise ValueError('专项动作采集不能选择仅设备检查')
    if target['controlled'] and activity_source != 'everyday_and_controlled':
        raise ValueError('本专项包含受控模拟阶段，请声明实际模拟方式与保护条件')
    return dict(version=TARGETED_SCHEMA['version'], target_id=target['id'],
                target_snapshot=copy.deepcopy(target), angles=list(angles),
                repetitions_per_angle=count, planned_cycles=len(angles) * count,
                angle_scope='operator-declared approximate initial body heading; not camera mounting angle',
                completion_scope='manual complete cycles; not automatic verification or independent fall counts')


def capture_context(value):
    """Validate and canonicalize the optional CSV/HTTP context column."""
    if value in ('', None):
        return ''
    if not isinstance(value, str) or len(value) > 3000:
        raise ValueError('专项区间条件格式错误')
    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('专项区间条件必须为JSON对象') from exc
    if not isinstance(data, dict) or data.get('version') != TARGETED_SCHEMA['version']:
        raise ValueError('专项区间条件版本无效')
    target = target_for(data.get('target_id'))
    if data.get('body_angle_deg') not in {item['id'] for item in TARGETED_SCHEMA['angles']}:
        raise ValueError('专项角度无效')
    for field, choices in [('motion_direction', 'motion_directions'), ('visibility', 'visibility_options')]:
        if data.get(field) not in {item['id'] for item in TARGETED_SCHEMA[choices]}:
            raise ValueError('专项运动方向或可见性无效')
    if data.get('phase_label') not in {item['label'] for item in target['phases']}:
        raise ValueError('专项阶段无效')
    try:
        uuid.UUID(data.get('cycle_id', ''))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('专项周期ID无效') from exc
    if (isinstance(data.get('cycle_index'), bool) or not isinstance(data.get('cycle_index'), int)
            or not 1 <= data['cycle_index'] <= 20000):
        raise ValueError('专项周期序号无效')
    expected = {'version', 'target_id', 'body_angle_deg', 'motion_direction', 'visibility',
                'phase_label', 'cycle_id', 'cycle_index'}
    if set(data) != expected:
        raise ValueError('专项区间条件包含未知字段')
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class TargetedProgress:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.target = config['target_snapshot']
        self.completed = {angle: 0 for angle in config['angles']}
        self.phase_index = 0
        self.cycle = None

    def snapshot(self):
        return dict(config=self.config, completed=dict(self.completed),
                    completed_cycles=sum(self.completed.values()), phase_index=self.phase_index,
                    expected_phase=self.target['phases'][self.phase_index],
                    cycle=copy.deepcopy(self.cycle))

    def begin(self, label, data):
        expected = self.target['phases'][self.phase_index]
        if label != expected['label']:
            raise ValueError('专项当前应记录：' + expected['name'])
        angle = data.get('body_angle_deg')
        if angle not in self.completed:
            raise ValueError('请选择本场计划中的角度')
        if self.cycle and angle != self.cycle['body_angle_deg']:
            raise ValueError('请先完成或放弃当前周期，再切换角度')
        cycle = self.cycle or dict(cycle_id=uuid.uuid4().hex, body_angle_deg=angle,
                                  cycle_index=self.completed[angle] + 1)
        value = dict(version=TARGETED_SCHEMA['version'], target_id=self.target['id'],
                     body_angle_deg=angle, motion_direction=data.get('motion_direction', 'not_recorded'),
                     visibility=data.get('visibility', 'not_recorded'), phase_label=label,
                     cycle_id=cycle['cycle_id'], cycle_index=cycle['cycle_index'])
        encoded = capture_context(json.dumps(value, ensure_ascii=False))
        self.cycle = cycle
        return encoded

    def finish(self, completed):
        if not self.cycle:
            return None
        if not completed:
            return self.abort()
        if self.phase_index + 1 == len(self.target['phases']):
            result = dict(event='targeted_cycle_complete', **self.cycle)
            self.completed[self.cycle['body_angle_deg']] += 1
            self.phase_index, self.cycle = 0, None
            return result
        self.phase_index += 1
        return None

    def abort(self):
        if not self.cycle:
            return None
        result = dict(event='targeted_cycle_incomplete', **self.cycle)
        self.phase_index, self.cycle = 0, None
        return result
