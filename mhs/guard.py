"""Monotonic-clock motion lease. Runs in the ROS process, not the MHS bridge."""
import time

class MotionGuard:
    def __init__(self, stop, clock=time.monotonic):
        self.clock=clock; self.stop=stop; self.last_lease=None
        self.connected=False; self.armed=False; self.reason='not_armed'
    def lease(self, connected):
        self.last_lease=self.clock(); self.connected=bool(connected)
        if not connected: self.trip('hub_disconnected')
    def healthy(self):
        return self.connected and self.last_lease is not None and self.clock()-self.last_lease<=3
    def arm(self, stopped, fresh):
        if not self.healthy(): raise ValueError('motion_lease_unavailable')
        if not stopped or not fresh: raise ValueError('stop_or_pose_not_confirmed')
        self.armed=True; self.reason=None
    def trip(self, reason):
        was_armed=self.armed; self.armed=False; self.reason=reason
        if was_armed: self.stop(reason)
    def tick(self, fresh=True):
        if self.armed and not self.healthy(): self.trip('motion_lease_expired')
        elif self.armed and not fresh: self.trip('telemetry_stale')
        return self.armed
    def state(self):
        return dict(armed=self.armed,connected=self.connected,healthy=self.healthy(),reason=self.reason,
                    age_seconds=None if self.last_lease is None else self.clock()-self.last_lease)
