"""Thermal feature extraction shared by offline evaluation and K1 inference."""
import numpy as np
from scipy import ndimage


GEOMETRY_NAMES = ('width', 'height', 'area', 'aspect', 'axis_cos2', 'axis_sin2',
                  'elongation', 'thermal_contrast')
SEQUENCE_NAMES = ([f'{stat}_{name}' for stat in ('mean', 'std', 'range', 'delta')
                   for name in GEOMETRY_NAMES]
                  + [f'{stat}_{axis}' for stat in ('std', 'range', 'delta') for axis in ('cx', 'cy')]
                  + [f'{stat}_{name}' for stat in ('mean', 'max') for name in ('edge', 'present', 'regions')]
                  + [f'{stat}_rate_{name}' for stat in ('min', 'max', 'mean')
                     for name in ('cx', 'cy', 'width', 'height', 'area')]
                  + ['width_ratio', 'height_ratio', 'area_ratio', 'relative_cx_delta', 'relative_cy_delta',
                     'pixel_rate_mean', 'pixel_rate_max', 'pixel_rate_std', 'span_s'])
FEATURE_NAMES = ([f'{prefix}_{name}' for prefix in ('current', 'previous', 'change')
                  for name in SEQUENCE_NAMES] + ['context_present'])



def warm_trace(grid):
    """Largest 8-connected warm component; not a verified human detector."""
    excess = np.maximum(grid - np.median(grid) - 2., 0)
    components, count = ndimage.label(excess > 0, np.ones((3, 3)))
    contrast = float(np.percentile(grid, 95) - np.median(grid))
    if not count:
        return np.array([0.] * 9 + [contrast, 0., 0., 0.], dtype=np.float32)
    sizes = np.bincount(components.ravel()); sizes[0] = 0
    yy, xx = np.nonzero(components == sizes.argmax())
    weights = excess[yy, xx]; total = weights.sum()
    cx, cy = float((xx*weights).sum()/total/31), float((yy*weights).sum()/total/23)
    dx, dy = xx/31-cx, yy/23-cy
    covariance = np.array([[(dx*dx*weights).sum(), (dx*dy*weights).sum()],
                           [(dx*dy*weights).sum(), (dy*dy*weights).sum()]])/total
    values, vectors = np.linalg.eigh(covariance)
    axis = np.arctan2(vectors[1, -1], vectors[0, -1])
    width, height = (xx.max()-xx.min()+1)/32, (yy.max()-yy.min()+1)/24
    edge = xx.min()==0 or xx.max()==31 or yy.min()==0 or yy.max()==23
    return np.array([cx, cy, width, height, len(xx)/768, width/height,
                     np.cos(2*axis), np.sin(2*axis), np.log1p(values[-1]/(values[0]+1e-6)),
                     contrast, float(edge), 1., float(count)], dtype=np.float32)


def sequence_features(trace, times, grids):
    """No absolute centroid coordinates or physical acceleration estimates."""
    times = np.asarray(times, dtype=np.float64)
    dt = np.diff(times)
    if len(times) < 2 or np.any(dt <= 0) or np.max(dt) > 1:
        raise ValueError('特征序列时间倒退或断档')
    geometry = trace[:, 2:10]
    positions = trace[:, :2]
    flags = trace[:, 10:]
    valid_pairs = (trace[:-1, 11] > 0) & (trace[1:, 11] > 0)
    rates = np.diff(trace[:, :5], axis=0)/dt[:, None]
    rates[~valid_pairs] = 0
    pixel_rates = np.abs(np.diff(grids, axis=0)).mean(axis=(1, 2))/dt
    first = np.maximum(trace[:2, 2:5].mean(0), [1/32, 1/24, 1/768])
    last = trace[-2:, 2:5].mean(0)
    return np.asarray(np.r_[geometry.mean(0), geometry.std(0), np.ptp(geometry, axis=0),
                           geometry[-1]-geometry[0], positions.std(0), np.ptp(positions, axis=0),
                           positions[-1]-positions[0], flags.mean(0), flags.max(0),
                           rates.min(0), rates.max(0), rates.mean(0), last/first,
                           (trace[-1, 0]-trace[0, 0])/max(trace[0, 2], .1),
                           (trace[-1, 1]-trace[0, 1])/max(trace[0, 3], .1),
                           pixel_rates.mean(), pixel_rates.max(), pixel_rates.std(), times[-1]-times[0]],
                      dtype=np.float32)


def thermal_window(trace, times, grids, end_index, exclusions=()):
    """Build the trained feature vector from the current and preceding windows."""
    if end_index < 7:
        return None

    def overlaps(start, end):
        return any(start < right and end > left for left, right in exclusions)

    def safe(indices):
        if len(indices) < 2:
            return False
        block = times[indices]
        dt = np.diff(block)
        return bool(np.all(dt > 0) and np.max(dt) <= 1
                    and not overlaps(block[0], block[-1]+1e-9))

    indices = np.arange(end_index-7, end_index+1)
    if not safe(indices):
        return None
    current = sequence_features(trace[indices], times[indices], grids[indices])
    start = times[indices[0]]
    before = np.arange(np.searchsorted(times, start-4), indices[0])
    context_ok = (len(before) >= 6 and safe(before) and start-times[before[-1]] <= 1
                  and not overlaps(start-4, start))
    previous = (sequence_features(trace[before], times[before], grids[before])
                if context_ok else np.zeros_like(current))
    features = np.r_[current, previous, current-previous, float(context_ok)].astype(np.float32)
    return indices, features
