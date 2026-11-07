"""Causal image-space features for a descent and its observed aftermath."""
import numpy as np


PHASE_NAMES = (
    'phase_descent_visible', 'phase_height_drop', 'phase_relative_height_drop',
    'phase_descent_seconds', 'phase_height_drop_per_second',
    'phase_centroid_y_change', 'phase_width_change', 'phase_axis_change',
    'phase_low_hold_seconds', 'phase_observed_after_trough_seconds',
    'phase_recovery_ratio', 'phase_final_height_ratio',
)


def phase_features(trace, times):
    """Find the largest ordered height drop within a visible run of the window."""
    visible = trace[:, 11] > 0
    height = trace[:, 3]
    missing = np.cumsum(~visible)
    ordered = np.triu(np.ones((len(trace), len(trace)), dtype=bool), k=1)
    continuous = (visible[:, None] & visible[None, :]
                  & (missing[:, None] == missing[None, :]))
    drop = np.where(ordered & continuous, height[:, None] - height[None, :], 0)
    start, trough = np.unravel_index(np.argmax(drop), drop.shape)
    height_drop = float(drop[start, trough])
    if height_drop < .99 / 24:
        return np.zeros(len(PHASE_NAMES), dtype=np.float32)

    duration = times[trough] - times[start]
    missing_after = np.flatnonzero(~visible[trough:])
    stop = trough + int(missing_after[0]) if len(missing_after) else len(trace)
    after = height[trough:stop]
    # A quarter of the observed drop is a feature boundary, not an alarm threshold.
    rising = np.flatnonzero(after > height[trough] + .25 * height_drop)
    last_low = trough + int(rising[0]) - 1 if len(rising) else stop - 1
    axis_change = 1 - np.clip(np.dot(trace[start, 6:8], trace[trough, 6:8]), -1., 1.)
    return np.asarray([
        1., height_drop, height_drop / height[start], duration, height_drop / duration,
        (trace[trough, 1] - trace[start, 1]) / height[start],
        trace[trough, 2] - trace[start, 2], axis_change,
        times[last_low] - times[trough], times[stop-1] - times[trough],
        (after.max() - height[trough]) / height_drop, after[-1] / height[start],
    ], dtype=np.float32)


def attach_phases(session, rows):
    for row in rows:
        end = np.searchsorted(session.times, row['end'])
        window = slice(end-7, end+1)
        row['thermal_phases'] = phase_features(session.trace[window], session.times[window])
