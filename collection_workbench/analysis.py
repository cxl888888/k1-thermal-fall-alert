"""Read-only thermal quality checks and exploratory features; output goes to stdout."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from decimal import Decimal
import json
import math
from pathlib import Path
import statistics


def integer_ns(value):
    if isinstance(value, bool):
        raise ValueError('时间戳不能是布尔值')
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError('时间戳必须是整数纳秒')


def inspect_frame(row):
    """One shared validity definition for playback, quality checks and features."""
    if not isinstance(row, dict):
        raise ValueError('帧记录不是对象')
    grid, mask = row.get('grid_c'), row.get('valid_mask')
    errors = []
    shape_ok = isinstance(grid, list) and len(grid) == 768 and isinstance(mask, list) and len(mask) == 768
    count = 0
    if not shape_ok:
        errors.append('矩阵或有效像素标记不完整')
    else:
        mask_ok = True
        for value, flag in zip(grid, mask):
            physical = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and -40 < value < 300
            if not isinstance(flag, bool) or flag != physical:
                mask_ok = False
            count += int(flag is True and physical)
        if not mask_ok:
            errors.append('温度值与有效像素标记不一致')
        if isinstance(row.get('valid_pixels'), bool) or row.get('valid_pixels') != count:
            errors.append('有效像素计数不一致')
    if not isinstance(row.get('valid'), bool):
        errors.append('valid 必须为布尔值')
    if not isinstance(row.get('simulated', False), bool):
        errors.append('simulated 必须为布尔值')
    declared = row.get('valid') is True
    if declared and (count != 768 or row.get('status') in ('warmup', 'error', 'invalid') or row.get('error')):
        errors.append('声明有效与像素、预热或读取状态冲突')
    return dict(valid=declared and count == 768 and not errors, valid_pixels=count, errors=errors)


def label_findings(labels, duration, check_overlap=True):
    findings, intervals, ids = [], [], set()
    from storage import LABEL_IDS
    for index, row in enumerate(labels, 1):
        try:
            lid = row['label_id']
            if not isinstance(lid, str) or not lid or lid in ids:
                raise ValueError('标签ID缺失或重复')
            ids.add(lid)
            if row.get('label') not in LABEL_IDS:
                raise ValueError('动作不在当前枚举中')
            start, end = Decimal(row['start_s']), Decimal(row['end_s'])
            if not start.is_finite() or not end.is_finite() or not 0 <= start < end <= Decimal(str(duration)):
                raise ValueError('动作区间越界或起止无效')
            intervals.append((start, end, index))
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            findings.append(f'标签第{index}段：{exc}')
    intervals.sort()
    furthest = None
    for start, end, index in intervals:
        if check_overlap and furthest is not None and start < furthest:
            findings.append(f'标签第{index}段与之前区间重叠')
        furthest = max(end, furthest) if furthest is not None else end
    return findings


def quality_summary(session, labels=None, gap_seconds=3.0):
    if not math.isfinite(gap_seconds) or gap_seconds <= 0:
        raise ValueError('断档提示阈值需为正数')
    session.ensure_index()
    rows = session.frames
    labels = session.labels() if labels is None else labels
    interval_ms = [(b['time_s'] - a['time_s']) * 1000 for a, b in zip(rows, rows[1:])]
    reads = [float(Decimal(row['end_s']) - Decimal(row['start_s'])) * 1000 for row in rows]
    gaps = [dict(after_frame=a['frame_id'], before_frame=b['frame_id'], seconds=round((b['time_s'] - a['time_s']), 6))
            for a, b in zip(rows, rows[1:]) if b['time_s'] - a['time_s'] > gap_seconds]
    label_errors = label_findings(labels, (session.end_ns - session.base_ns) / 1e9)
    warnings = []
    if not labels:
        warnings.append('没有动作标签；可用于读取与回放核对，不能评估动作识别准确率。')
    if any(row.get('review_status') != 'human_reviewed' for row in labels):
        warnings.append('包含待人工审核的标签。')
    if any(row.get('label') in ('unknown', 'invalid') for row in labels):
        warnings.append('不确定或无效区间应从相应监督学习目标中排除，不能当正常负样本。')
    if session.meta.get('thermal_simulated') or any(row['simulated'] for row in rows):
        warnings.append('包含模拟热像，不得当作真实采集或现场泛化证据。')
    source = session.meta.get('activity_source')
    purpose = session.meta.get('collection_purpose')
    controlled = any(row.get('label') in ('safe_fall_simulation', 'post_fall_immobile', 'recover_after_fall')
                     for row in labels)
    if source is None or purpose is None:
        warnings.append('历史记录未单独声明动作来源或采集目的；真实温度不证明动作是真实意外跌倒。')
    if controlled:
        warnings.append('标签包含受控模拟；只能评估该模拟条件，不能当真实意外跌倒的检出效果。')
        if source is not None and source != 'everyday_and_controlled':
            warnings.append('模拟标签与采集时声明的动作来源不一致；保留原声明，需人工核对来源。')
    if session.meta.get('recording_profile') == 'preliminary' or controlled:
        if not any(row.get('label') == 'lie_still_normal' for row in labels):
            warnings.append('本会话未标注正常躺卧对照；不能评估正常躺卧导致的误触发。')
    if purpose in ('functional_trial', 'device_check'):
        warnings.append('本场声明为功能试采或设备检查；不能自动标为训练就绪，需另行审核数据用途。')
    if session.meta.get('radar_mode', 'not_connected') != 'real':
        warnings.append('没有经核验的同步真实雷达；不能用于双传感器融合效果评估。')
    else:
        radar=session.meta.get('radar_summary') or {}
        silence=radar.get('max_no_report_window_s')
        boundary_gap=isinstance(silence,(int,float)) and (not math.isfinite(silence) or silence>1)
        if (not radar.get('framing_checks_ok') or radar.get('gaps_over_1s')
                or radar.get('delivery_checks_ok') is False or boundary_gap):
            warnings.append('雷达存在未完成核验、坏帧或断档，不能直接视为完整双路数据。')
        if (session.meta.get('radar_config') or {}).get('module_configuration_status')=='not_queried':
            warnings.append('雷达固件与内部阈值尚未查询；主机串口设置不替代模块配置快照。')
    installation = session.meta.get('installation') or {}
    if not installation.get('configuration_id'):
        warnings.append('安装配置代号未记录；不能自动认定与其他场次属于同一安装条件。')
    if installation.get('height_m') is None or installation.get('orientation') in (None, '', 'not_recorded'):
        warnings.append('安装高度或朝向未记录；后续动作采集需补充实际条件。')
    span = rows[-1]['time_s'] - rows[0]['time_s'] if len(rows) > 1 else 0
    valid = sum(row['valid'] for row in rows)
    return dict(schema_version='thermal_quality_0.1', session_id=session.sid,
                indexed_frames=len(rows), source_lines=getattr(session, 'line_count', len(rows)),
                valid_frames=valid, invalid_or_warmup_frames=len(rows)-valid,
                valid_fraction=valid / len(rows) if rows else None,
                status_counts=dict(Counter(row['status'] for row in rows)),
                radar_summary=session.meta.get('radar_summary'),
                observed_fps=(len(rows)-1)/span if span > 0 else None,
                read_ms=dict(median=statistics.median(reads) if reads else None, maximum=max(reads, default=None)),
                midpoint_interval_ms=dict(median=statistics.median(interval_ms) if interval_ms else None,
                                          maximum=max(interval_ms, default=None)),
                gap_threshold_seconds=gap_seconds, gap_count=len(gaps), gaps=gaps[:100],
                gaps_truncated=len(gaps) > 100, label_count=len(labels), label_issues=label_errors,
                frame_issues=list(session.issues), warnings=warnings,
                data_checks_ok=bool(rows) and not session.issues and not label_errors,
                note='数据结构检查不代表动作真值、识别精度、测温标定或融合训练已经达标。')


def frame_features(row, delta_c=2.0):
    """Largest 4-connected warm component relative to this frame's median.

    The threshold is an exploratory display setting, never a human/fall decision.
    """
    if not math.isfinite(delta_c) or not 0 < delta_c <= 50:
        raise ValueError('温差参数需大于0且不超过50°C')
    quality = inspect_frame(row)
    result = dict(algorithm_version='warm_region_features_0.1', usable=quality['valid'],
                  threshold_delta_c=delta_c, errors=quality['errors'],
                  interpretation='候选温区几何特征；不证明有人、姿态或跌倒，不生成告警。')
    if not quality['valid']:
        return result
    grid = row['grid_c']
    background = statistics.median(grid)
    hot = {i for i, temperature in enumerate(grid) if temperature > background + delta_c}
    components, seen = [], set()
    for seed in sorted(hot):
        if seed in seen:
            continue
        queue, component = deque([seed]), []
        seen.add(seed)
        while queue:
            pixel = queue.popleft()
            component.append(pixel)
            y, x = divmod(pixel, 32)
            for yy, xx in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                other = yy * 32 + xx
                if 0 <= yy < 24 and 0 <= xx < 32 and other in hot and other not in seen:
                    seen.add(other)
                    queue.append(other)
        components.append(component)
    largest = max(components, key=len, default=[])
    result.update(background_median_c=background, threshold_c=background+delta_c,
                  minimum_c=min(grid), maximum_c=max(grid), warm_pixels=len(hot),
                  component_count=len(components), largest_pixels=len(largest), centroid_px=None, bbox_px=None)
    if largest:
        xs, ys = [i % 32 for i in largest], [i // 32 for i in largest]
        result.update(centroid_px=dict(x=statistics.mean(xs), y=statistics.mean(ys)),
                      bbox_px=dict(left=min(xs), top=min(ys), width=max(xs)-min(xs)+1, height=max(ys)-min(ys)+1))
        cx, cy = statistics.mean(xs), statistics.mean(ys)
        xx = statistics.mean((x-cx)**2 for x in xs)
        yy = statistics.mean((y-cy)**2 for y in ys)
        xy = statistics.mean((x-cx)*(y-cy) for x,y in zip(xs,ys))
        root = math.sqrt((xx-yy)**2 + 4*xy*xy)
        result.update(principal_axis_degrees=math.degrees(math.atan2(2*xy,xx-yy)/2),
                      principal_variance=(xx+yy+root)/2,
                      minor_variance=max(0,(xx+yy-root)/2),
                      touches_image_edge=min(xs)==0 or max(xs)==31 or min(ys)==0 or max(ys)==23)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path, help='Completed session directory; files remain read-only')
    parser.add_argument('--mode', choices=('quality', 'features'), default='quality')
    parser.add_argument('--delta-c', type=float, default=2.0)
    parser.add_argument('--gap-seconds', type=float, default=3.0)
    args = parser.parse_args()
    from storage import Session
    session = Session(args.session)
    session.ensure_index()
    if args.mode == 'quality':
        print(json.dumps(quality_summary(session, gap_seconds=args.gap_seconds), ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for index in range(len(session.frames)):
            row = session.frame(index)
            if not row['relative']['valid']:
                row = dict(row, valid=False)
            features = frame_features(row, args.delta_c)
            print(json.dumps(dict(frame_id=row.get('frame_id'), time_s=row['relative']['time_s'], features=features),
                             ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
