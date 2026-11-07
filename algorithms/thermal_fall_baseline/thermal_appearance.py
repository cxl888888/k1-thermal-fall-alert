"""Causal appearance and upper-body candidate features for the reviewed experiment."""
import numpy as np
from scipy import ndimage


BODY_FIELDS = ('fill', 'squareness', 'local_contrast', 'spread', 'hotspot_offset',
               'edge_fraction', 'present')
BODY_NAMES = tuple(f'appearance_{stat}_{field}' for stat in ('last', 'delta', 'std')
                   for field in BODY_FIELDS)
HEAD_NAMES = ('head_candidate_fraction', 'head_continuous_pair_fraction',
              'head_dx_relative', 'head_dy_relative', 'head_max_dy_per_second_relative')
SHOULDER_NAMES = ('head_shoulder_fraction', 'head_shoulder_pair_fraction',
                  'head_shoulder_width_gain', 'head_shoulder_dy_relative',
                  'head_shoulder_max_dy_per_second_relative', 'head_shoulder_turn')


def head_shoulder_outline(points, shape, center, expected):
    """Locate a narrow cap followed by a modest widening in the thermal mask."""
    relative = points - center
    _, axes = np.linalg.eigh(relative.T @ relative)
    axis = axes[:, -1]
    if expected is None:
        # Only seed a track when the long axis has a clear upper end.
        width, height = np.ptp(points, axis=0) + 1
        if height < 7 or height < 1.4 * width or abs(axis[1]) < .8:
            return None
        directions = (axis if axis[1] > 0 else -axis,)
    else:
        directions = (axis, -axis)

    best = None
    best_distance = np.inf
    for direction in directions:
        along = relative @ direction
        depth = along - along.min()
        # Scale the crop to the silhouette and limit it to the first few pixels.
        cap_depth = np.clip(np.ptp(along) * .16, 1.4, 2.5)
        cap = depth < cap_depth
        shoulders = (depth >= cap_depth) & (depth < 2 * cap_depth)
        if cap.sum() < 2 or shoulders.sum() < 3:
            continue
        outline = points[cap | shoulders]
        if np.any((outline <= 0) | (outline >= np.asarray(shape[::-1]) - 1)):
            continue
        across = relative @ np.array([-direction[1], direction[0]])
        cap_mid = np.median(across[cap])
        cap_width = np.ptp(across[cap]) + 1
        shoulder_width = np.ptp(across[shoulders]) + 1
        spread = across[shoulders] - cap_mid
        if (shoulder_width - cap_width < .75 or spread.min() >= -.35
                or spread.max() <= .35):
            continue
        anchor = points[cap].mean(axis=0)
        distance = np.linalg.norm(anchor - expected) if expected is not None else 0.
        if distance < best_distance and distance <= max(3., cap_depth * 1.6):
            best = (anchor, direction, (shoulder_width - cap_width) / shoulder_width)
            best_distance = distance
    return best


def appearance_trace(grids, times, exclusions=(), *, with_shoulders=False):
    body = np.zeros((len(grids), len(BODY_FIELDS)), dtype=np.float32)
    head = np.full((len(grids), 3), np.nan, dtype=np.float32)
    shoulders = np.full((len(grids), 6), np.nan, dtype=np.float32) if with_shoulders else None
    last_head = last_body = None
    last_shoulder = last_center = None
    track_id = shoulder_id = 0
    connectivity = np.ones((3, 3), dtype=bool)

    for index, grid in enumerate(grids):
        if index and (times[index] - times[index-1] > 1 or any(
                times[index-1] < right and times[index] >= left for left, right in exclusions)):
            last_head = last_body = None
            last_shoulder = last_center = None
        if any(left <= times[index] < right for left, right in exclusions):
            last_head = last_body = None
            last_shoulder = last_center = None
            continue
        components, count = ndimage.label(grid > np.median(grid) + 2, connectivity)
        if not count:
            last_head = last_body = None
            last_shoulder = last_center = None
            continue
        sizes = np.bincount(components.ravel())
        sizes[0] = 0
        mask = components == sizes.argmax()
        yy, xx = np.nonzero(mask)
        width, height = np.ptp(xx)+1, np.ptp(yy)+1
        center = np.array([xx.mean(), yy.mean()])
        pixels = grid[yy, xx]
        p10, p80, p90 = np.percentile(pixels, [10, 80, 90])
        border = (xx == 0) | (xx == grid.shape[1]-1) | (yy == 0) | (yy == grid.shape[0]-1)
        ring = ndimage.binary_dilation(mask, iterations=2) & (components == 0)
        background = np.median(grid[ring]) if np.any(ring) else np.median(grid)
        hot = pixels >= p80
        hot_center = np.array([xx[hot].mean(), yy[hot].mean()])
        body[index] = (len(xx)/(width*height), min(width, height)/max(width, height),
                       np.median(pixels)-background, p90-p10,
                       np.linalg.norm(hot_center-center)/max(width, height), border.mean(), 1)

        # A local peak is only a candidate. Initialise at an intact upper end of a tall region.
        smoothed = ndimage.uniform_filter(grid, size=3, mode='nearest')
        peak = mask & (smoothed == ndimage.maximum_filter(smoothed, size=3, mode='nearest'))
        py, px = np.nonzero(peak)
        inside = (px > 0) & (px < grid.shape[1]-1) & (py > 0) & (py < grid.shape[0]-1)
        candidates = np.column_stack([px[inside], py[inside]])
        selected = None
        if len(candidates) and last_head is not None:
            expected = last_head + center - last_body
            distances = np.linalg.norm(candidates-expected, axis=1)
            best = int(np.argmin(distances))
            if distances[best] <= max(2.5, min(width, height)*0.6):
                selected = candidates[best]
        elif len(candidates) and height >= 7 and height >= 1.5*width and yy.min() > 0:
            upper = candidates[candidates[:, 1] <= yy.min()+max(1, (height-1)*0.3)]
            if len(upper):
                selected = upper[np.argmax(smoothed[upper[:, 1], upper[:, 0]])]
                track_id += 1
        if selected is not None:
            head[index] = (*selected, track_id)
        last_head = selected
        last_body = center if selected is not None else None

        if shoulders is not None:
            expected = last_shoulder + center - last_center if last_shoulder is not None else None
            outline = head_shoulder_outline(np.column_stack([xx, yy]), grid.shape, center, expected)
            if outline is None:
                last_shoulder = last_center = None
            else:
                anchor, direction, width_gain = outline
                if last_shoulder is None:
                    shoulder_id += 1
                shoulders[index] = (*anchor, *direction, width_gain, shoulder_id)
                last_shoulder, last_center = anchor, center
    return (body, head, shoulders) if with_shoulders else (body, head)


def attach_appearance(session, rows, *, with_shoulders=False):
    traces = appearance_trace(session.grids, session.times, session.exclusions,
                              with_shoulders=with_shoulders)
    body, head = traces[:2]
    for row in rows:
        end = np.searchsorted(session.times, row['end'])
        window = slice(end-7, end+1)
        trace = body[window]
        first, last = trace[:2].mean(0), trace[-2:].mean(0)
        row['appearance'] = np.r_[last, last-first, trace.std(0)]

        track = head[window]
        valid = np.isfinite(track[:, 2])
        matched = valid[1:] & valid[:-1] & (track[1:, 2] == track[:-1, 2])
        delta = np.diff(track[:, :2], axis=0)[matched]
        dt = np.diff(session.times[window])[matched]
        height = max(float(session.trace[end-7, 3]*24), 1)
        dx, dy = delta.sum(0)/height if len(delta) else (0., 0.)
        descent = max(0., float(np.max(delta[:, 1]/dt))/height) if len(delta) else 0.
        row['head_candidate'] = np.asarray([valid.mean(), matched.mean(), dx, dy, descent], dtype=np.float32)
        if with_shoulders:
            outline = traces[2][window]
            present = np.isfinite(outline[:, 5])
            linked = present[1:] & present[:-1] & (outline[1:, 5] == outline[:-1, 5])
            change_y = np.diff(outline[:, 1])[linked] / height
            elapsed = np.diff(session.times[window])[linked]
            turn = 1 - np.sum(outline[1:, 2:4][linked] * outline[:-1, 2:4][linked], axis=1)
            row['head_shoulder'] = np.asarray([
                present.mean(), linked.mean(),
                outline[present, 4].mean() if present.any() else 0.,
                change_y.sum(), max(0., float(np.max(change_y / elapsed))) if linked.any() else 0.,
                np.clip(turn, 0., 2.).mean() if linked.any() else 0.,
            ], dtype=np.float32)
