"""Shared current-pose yielding predicate, independent of velocity messages."""
import math

def people_gate(pose,people):
    p=[pose['x'],pose['y']];nearest=float('inf');front=False
    for person in people:
        dx=person['position'][0]-p[0];dy=person['position'][1]-p[1]
        nearest=min(nearest,math.hypot(dx,dy))
        forward=dx*math.cos(pose['yaw'])+dy*math.sin(pose['yaw'])
        side=abs(-dx*math.sin(pose['yaw'])+dy*math.cos(pose['yaw']))
        front=front or -.8<forward<1.5 and side<.75
    return nearest,nearest<.95 or front

class ContinuousBlock:
    def __init__(self):self.since=None
    def observe(self,blocked,sim_time):
        before=self.since is not None
        if blocked and self.since is None:self.since=sim_time
        if not blocked:self.since=None
        return 'started' if blocked and not before else 'finished' if not blocked and before else None
    def duration(self,sim_time):return 0 if self.since is None else max(0,sim_time-self.since)
