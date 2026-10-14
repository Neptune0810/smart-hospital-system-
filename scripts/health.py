"""Fail promptly when the project's actual simulator disappears or stops advancing."""
import json,time
from pathlib import Path
import manage
class SimulationHealth:
    def __init__(self,root,clock=time.time,alive=None,fresh_seconds=10,stall_seconds=20):
        self.root=Path(root);self.clock=clock;self.alive=alive or manage.live_sim
        self.fresh_seconds=fresh_seconds;self.stall_seconds=stall_seconds
        self.last_sim=None;self.last_progress=None
    def check(self,state=None):
        now=self.clock()
        if not self.alive():raise RuntimeError('project_sim_process_exited')
        try:pose=json.loads((self.root/'runs/physical-pose.json').read_text())
        except (OSError,ValueError):raise RuntimeError('physical_pose_unavailable')
        if now-pose.get('wall_time',0)>self.fresh_seconds:raise RuntimeError('physical_pose_stale')
        if state is None:return
        sim=state['sim_time']
        if self.last_sim is not None and sim<self.last_sim-0.1:raise RuntimeError('simulation_clock_reversed')
        if self.last_sim is None or sim>self.last_sim:
            self.last_sim=sim;self.last_progress=now
        elif now-self.last_progress>self.stall_seconds:raise RuntimeError('simulation_clock_stalled')
