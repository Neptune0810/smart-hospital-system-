"""Asynchronous timeline regressions without Kit, GPU or live services."""
import copy
import json
import unittest
from unittest.mock import patch

from .smart_scene import SmartWardWorld, atomic_json
from . import test_smart_scene as scene_tests


class QueuedTimeline:
    def __init__(self):
        self.playing = True; self.commands = []; self.pauses = 0; self.plays = 0
        self.pause_raises = False
    def is_playing(self): return self.playing
    def pause(self):
        self.pauses += 1; self.commands.append(False)
        if self.pause_raises: raise RuntimeError('pause queued then failed')
    def play(self):
        self.plays += 1; self.commands.append(True)
    def update(self):
        for playing in self.commands: self.playing = playing
        self.commands.clear()


class AsyncTimelineTests(unittest.TestCase):
    setUp = scene_tests.ProtocolTests.setUp
    qualify = scene_tests.FaultQualificationTests.qualify

    def prepare(self):
        self.qualify()
        self.timeline = QueuedTimeline(); self.world.faults.timeline = self.timeline
        self.base_wall = self.q['started_wall']

    def submit(self, ident, kind, args, elapsed=0.):
        command = {'run_id': 'run1', 'epoch': 'epoch1', 'qualification_id': self.q['qualification_id'],
            'session_key': self.q['session_key'], 'operation_id': ident, 'kind': kind,
            'arguments': args, 'wall_time': self.base_wall}
        atomic_json(self.directory/'fault-commands'/(ident+'.json'), command)
        self.poll(elapsed)
        return self.directory/'fault-acks'/(ident+'.json')

    def poll(self, elapsed):
        with patch('mhs.smart_scene.time.time', return_value=self.base_wall+elapsed):
            self.world.poll_controls(self.sim)

    def read_ack(self, ident):
        return json.loads((self.directory/'fault-acks'/(ident+'.json')).read_text())

    def pause_and_confirm(self):
        path = self.submit('pause', 'pause_simulation', {'seconds': 8})
        self.assertFalse(path.exists()); self.assertTrue(self.timeline.is_playing())
        self.timeline.update(); self.poll(.1)
        self.assertEqual(self.read_ack('pause')['status'], 'completed')
        self.assertFalse(self.timeline.is_playing())

    def test_delayed_pause_ack_waits_for_actual_false_and_never_reissues_pause(self):
        self.prepare(); before = (self.directory/'observation.json').read_bytes()
        path = self.submit('pause', 'pause_simulation', {'seconds': 8})
        deadline = self.world.faults.state['paused']['resume_wall']
        self.assertFalse(path.exists()); self.assertTrue(self.timeline.is_playing())
        self.poll(.05)
        self.assertFalse(path.exists()); self.assertEqual(self.timeline.pauses, 1)
        self.timeline.update(); self.poll(.1)
        ack = self.read_ack('pause')
        self.assertEqual(ack['status'], 'completed'); self.assertIs(ack['result']['actual_playing'], False)
        self.assertEqual(ack['result']['paused']['resume_wall'], deadline)
        self.assertEqual(ack['wall_time'], self.base_wall+.1)
        self.assertEqual((self.directory/'observation.json').read_bytes(), before)

    def test_delayed_explicit_resume_has_no_success_ack_before_actual_true(self):
        self.prepare(); self.pause_and_confirm()
        path = self.submit('resume', 'resume_simulation', {}, elapsed=.2)
        self.assertFalse(path.exists()); self.assertFalse(self.timeline.is_playing())
        self.poll(.25); self.assertFalse(path.exists()); self.assertEqual(self.timeline.plays, 1)
        self.timeline.update(); self.poll(.3)
        ack = self.read_ack('resume')
        self.assertEqual(ack['status'], 'completed'); self.assertIs(ack['result']['actual_playing'], True)
        self.assertFalse(ack['result']['resumes_navigation']); self.assertIsNone(self.world.faults.state['paused'])
        self.assertTrue(self.world.control['fault_qualification']['simulation_pause_requested'])

    def test_original_wall_limit_fails_unconfirmed_pause_and_still_requests_play(self):
        self.prepare()
        path = self.submit('pause', 'pause_simulation', {'seconds': 8})
        deadline = self.world.faults.state['paused']['resume_wall']
        self.poll(7.99); self.assertFalse(path.exists())
        self.poll(8.01)
        ack = self.read_ack('pause'); original = path.read_bytes()
        self.assertEqual(ack['status'], 'failed')
        self.assertEqual(ack['error'], 'actual_timeline_pause_not_confirmed_before_wall_limit')
        self.assertEqual(self.timeline.plays, 1)
        self.assertEqual(self.world.faults.state['paused']['resume_wall'], deadline)
        self.timeline.update(); self.poll(8.1)
        self.assertTrue(self.timeline.is_playing()); self.assertIsNone(self.world.faults.state['paused'])
        self.assertEqual(path.read_bytes(), original); self.assertEqual(self.timeline.pauses, 1)

    def test_pause_call_failure_retains_deadline_and_auto_restores_queued_pause(self):
        self.prepare(); self.timeline.pause_raises = True
        path = self.submit('pause', 'pause_simulation', {'seconds': 8})
        self.assertEqual(self.read_ack('pause')['status'], 'failed')
        original = path.read_bytes()
        self.timeline.update(); self.assertFalse(self.timeline.is_playing())
        self.poll(8.01); self.assertFalse(self.timeline.is_playing()); self.assertEqual(self.timeline.plays, 1)
        self.timeline.update(); self.poll(8.1)
        self.assertTrue(self.timeline.is_playing()); self.assertIsNone(self.world.faults.state['paused'])
        self.assertEqual(path.read_bytes(), original)

    def test_inactive_close_waits_for_actual_resume_without_raising_or_faking_restore(self):
        self.prepare(); self.pause_and_confirm()
        self.control.update(stop_requested=True, enabled=False); self.q['active'] = False
        atomic_json(self.directory/'control.json', self.control)
        self.poll(.2)
        state = self.world.faults.state
        self.assertFalse(state['active']); self.assertTrue(state['restoration_pending'])
        self.assertIsNotNone(state['paused']); self.assertNotIn('ended_wall', state)
        self.assertFalse(self.timeline.is_playing()); self.assertEqual(self.timeline.plays, 1)
        self.poll(.3); self.assertEqual(self.timeline.plays, 1)
        self.timeline.update(); self.poll(.4)
        self.assertTrue(self.timeline.is_playing()); self.assertIsNone(state['paused'])
        self.assertNotIn('restoration_pending', state); self.assertEqual(state['ended_wall'], self.base_wall+.4)

    def test_pending_operation_conflict_does_not_replace_original_or_repeat_actuator(self):
        self.prepare(); path = self.submit('pause', 'pause_simulation', {'seconds': 8})
        old = copy.deepcopy(self.world.faults.state['processed']['pause']['command'])
        conflict = copy.deepcopy(old); conflict['arguments']['seconds'] = 7
        atomic_json(self.directory/'fault-commands/pause.json', conflict)
        self.poll(.05)
        self.assertFalse(path.exists()); self.assertEqual(self.timeline.pauses, 1)
        self.assertEqual(self.world.faults.state['processed']['pause']['command'], old)
        self.timeline.update(); self.poll(.1)
        self.assertEqual(self.read_ack('pause')['status'], 'completed')
        self.assertEqual(self.read_ack('pause')['result']['paused']['resume_wall'], self.base_wall+8)

    def test_late_resume_readback_fails_original_operation_but_restores_world(self):
        self.prepare(); self.pause_and_confirm()
        path = self.submit('resume', 'resume_simulation', {}, elapsed=5.)
        self.assertFalse(path.exists()); self.timeline.update(); self.poll(8.1)
        ack = self.read_ack('resume')
        self.assertEqual(ack['status'], 'failed')
        self.assertEqual(ack['error'], 'actual_timeline_resume_not_confirmed_before_wall_limit')
        self.assertTrue(self.timeline.is_playing()); self.assertIsNone(self.world.faults.state['paused'])

    def test_restart_reissues_only_saved_recovery_play_when_old_queued_request_was_lost(self):
        self.prepare(); self.pause_and_confirm()
        self.control.update(stop_requested=True, enabled=False); self.q['active'] = False
        atomic_json(self.directory/'control.json', self.control); self.poll(.2)
        self.assertFalse(self.timeline.is_playing()); self.assertEqual(self.timeline.plays, 1)
        # Process death loses the old Kit command queue, not the saved pause or
        # permanent backend stop. A new scene can only restore play, never Nav.
        self.timeline.commands.clear()
        replacement = SmartWardWorld(self.root, self.actuator, lambda: copy.deepcopy(self.actual), self.config, self.timeline)
        with patch('mhs.smart_scene.time.time', return_value=self.base_wall+.3): replacement.poll_controls(self.sim)
        self.assertEqual(self.timeline.pauses, 1); self.assertEqual(self.timeline.plays, 2)
        self.assertIsNotNone(replacement.faults.state['paused'])
        self.timeline.update()
        with patch('mhs.smart_scene.time.time', return_value=self.base_wall+.4): replacement.poll_controls(self.sim)
        self.assertTrue(self.timeline.is_playing()); self.assertIsNone(replacement.faults.state['paused'])
        self.assertFalse(replacement.faults.state['active']); self.assertNotIn('restoration_pending', replacement.faults.state)

    def test_resume_does_not_freshen_old_sample_and_only_advancing_simulation_produces_new_sequences(self):
        self.prepare(); self.pause_and_confirm()
        before = json.loads((self.directory/'observation.json').read_text()); original = (self.directory/'observation.json').read_bytes()
        for elapsed in (1., 3.5, 5.):
            with patch('mhs.smart_scene.time.time', return_value=self.base_wall+elapsed): self.world.tick(self.sim)
            self.assertEqual((self.directory/'observation.json').read_bytes(), original)
        path = self.submit('resume', 'resume_simulation', {}, elapsed=5.05); self.assertFalse(path.exists())
        self.timeline.update(); self.poll(5.1)
        self.assertEqual((self.directory/'observation.json').read_bytes(), original)
        sequences = []
        for elapsed in (6.2, 7.3, 8.4):
            self.sim += .1
            self.actual.update(sim_time=self.sim, wall_time=self.base_wall+elapsed,
                velocity_sim_time=self.sim, velocity_wall_time=self.base_wall+elapsed)
            with patch('mhs.smart_scene.time.time', return_value=self.base_wall+elapsed): self.world.tick(self.sim)
            sample = json.loads((self.directory/'observation.json').read_text()); sequences.append(sample['seq'])
            self.assertGreater(sample['sim_time'], before['sim_time']); self.assertEqual(sample['wall_time'], self.base_wall+elapsed)
        self.assertEqual(len(set(sequences)), 3); self.assertGreater(min(sequences), before['seq'])


if __name__ == '__main__':
    unittest.main()
