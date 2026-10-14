"""Slow a differential-drive command without changing its turning curvature."""
import math

def limited_twist(linear,angular,linear_limit,angular_limit=.45):
    if not all(math.isfinite(v) for v in [linear,angular,linear_limit,angular_limit]):raise ValueError('nonfinite_motion_command')
    scale=min(1.,linear_limit/abs(linear) if linear else 1.,angular_limit/abs(angular) if angular else 1.)
    return linear*scale,angular*scale
