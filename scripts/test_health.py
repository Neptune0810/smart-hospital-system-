import json,tempfile,unittest
from pathlib import Path
from health import SimulationHealth
class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'runs').mkdir();self.now=100;self.alive=True
        self.h=SimulationHealth(self.root,clock=lambda:self.now,alive=lambda:self.alive)
        self.pose()
    def tearDown(self):self.temp.cleanup()
    def pose(self):(self.root/'runs/physical-pose.json').write_text(json.dumps({'wall_time':self.now}))
    def test_process_exit(self):
        self.alive=False
        with self.assertRaisesRegex(RuntimeError,'process_exited'):self.h.check({'sim_time':1})
    def test_stale_pose(self):
        self.now+=11
        with self.assertRaisesRegex(RuntimeError,'pose_stale'):self.h.check({'sim_time':1})
    def test_stalled_clock_despite_fresh_pose(self):
        self.h.check({'sim_time':1});self.now+=21;self.pose()
        with self.assertRaisesRegex(RuntimeError,'clock_stalled'):self.h.check({'sim_time':1})
    def test_advancing_clock(self):
        self.h.check({'sim_time':1});self.now+=21;self.pose();self.h.check({'sim_time':2})
    def test_clock_reversed(self):
        self.h.check({'sim_time':2})
        with self.assertRaisesRegex(RuntimeError,'clock_reversed'):self.h.check({'sim_time':1})
if __name__=='__main__':unittest.main()
