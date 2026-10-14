"""Meaningful protocol/geometry regressions; no GPU or simulator launch."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
import random
import re
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from .scene_io import SceneIOConflict, publish_command, scene_path
from .smart_map import bounds_to_pixels, load_map_metadata, write_overlay_map
from .smart_scene import (SmartWardWorld, SmartWardScene, atomic_json, choose_cabinet_position,
                          geometry_inventory_decision, mesh_face_bounds,
                          CABINETS, usd_device_identifier, ObstacleIndex, boxes_intersect)


def isolated_directory():
    workspace = Path(__file__).resolve().parents[1]
    cache = workspace/'.cache/smart-scene-tests'
    cache.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='test-', dir=cache)
    if not Path(temporary.name).resolve().is_relative_to(workspace):
        raise RuntimeError('test_directory_outside_workspace')
    return temporary


class Actuator:
    def __init__(self):
        self.lights = {}; self.doors = {}
    def set_light(self, room, value): self.lights[room] = float(value)
    def read_light(self, room): return self.lights[room]
    def set_door(self, ident, value): self.doors[ident] = value
    def sync_packages(self, state): pass


class Timeline:
    def __init__(self): self.playing = True; self.pauses = 0; self.plays = 0
    def is_playing(self): return self.playing
    def pause(self): self.playing = False; self.pauses += 1
    def play(self): self.playing = True; self.plays += 1


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = isolated_directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        atomic_json(self.root/'configs/stations.json', {'station_b': {'x': 1., 'y': 2.}, 'station_c': {'x': 3., 'y': 4.}, 'lab': {'x': 5., 'y': 6.}})
        self.directory = self.root/'runs/smart-ward-v1/scene'
        atomic_json(self.directory/'control.json', {'run_id': 'run1', 'epoch': 'epoch1'})
        self.actuator = Actuator()
        self.actual = {'x': 1., 'y': 2., 'available': True, 'stopped_samples': 3, 'wall_time': time.time(),
                       'sim_time': 0., 'speed_mps': 0., 'angular_speed_rad_s': 0., 'velocity_available': True,
                       'velocity_wall_time': time.time(), 'velocity_sim_time': 0.}
        self.config = {'sampling_hz': 1, 'lights': {'normal_percent': 60}}
        self.world = SmartWardWorld(self.root, self.actuator, lambda: copy.deepcopy(self.actual), self.config)
        self.sim = 0.
        self.world.tick(self.sim)

    def command(self, ident, kind, args, epoch='epoch1'):
        value = {'run_id': 'run1', 'epoch': epoch, 'operation_id': ident, 'kind': kind, 'arguments': args, 'wall_time': time.time()}
        publish_command(self.directory, value)
        return value

    def tick(self, delta=.10):
        self.sim += delta
        self.actual['wall_time'] = time.time()
        self.actual.update(sim_time=self.sim, velocity_wall_time=time.time(), velocity_sim_time=self.sim)
        self.world.tick(self.sim)

    def normal_path(self, ident, channel='acks', run_id='run1', epoch='epoch1'):
        return scene_path(self.directory, channel, run_id, epoch, ident)

    def ack(self, ident):
        return json.loads(self.normal_path(ident).read_text())

    def specimen(self, ident='insert1'):
        self.command(ident, 'insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'})
        self.tick()
        return self.ack(ident)['result']['specimen_id']

    def handoff(self, sid, ident='pickup1', transfer='transfer1', direction='pickup'):
        cabinet, station = ('hospital-cabinet-b', 'station_b') if direction == 'pickup' else ('hospital-cabinet-lab', 'lab')
        return self.command(ident, 'handoff', {'transfer_id': transfer, 'order_id': 'order1', 'specimen_id': sid,
                    'cabinet_id': cabinet, 'direction': direction, 'station_id': station,
                    'arrival_proof': {'pose': {'x': self.actual['x'], 'y': self.actual['y']},
                                      'physical_pose': {'position': [self.actual['x'], self.actual['y'], 0]}, 'wall_time': time.time()}})

    def complete_animation(self):
        self.tick(); self.tick(.8); self.tick(.8)

    def test_insert_is_durable_and_lost_ack_replay_does_not_duplicate(self):
        sid = self.specimen()
        self.normal_path('insert1').unlink()
        replacement = SmartWardWorld(self.root, self.actuator, lambda: self.actual, self.config)
        replacement.tick(self.sim+.1)
        self.assertEqual(self.ack('insert1')['result']['specimen_id'], sid)
        self.assertEqual(len(replacement.state['specimens']), 1)
        original_ack = self.normal_path('insert1').read_bytes()
        # Immutable same-scope publication refuses the conflicting payload
        # before the scene can execute it or replace the successful original.
        with self.assertRaisesRegex(SceneIOConflict, 'scene_same_scope_payload_conflict'):
            self.command('insert1', 'insert_specimen', {'cabinet_id': 'hospital-cabinet-c', 'priority': 'urgent'})
        replacement.tick(self.sim+.2)
        self.assertEqual(self.normal_path('insert1').read_bytes(), original_ack)
        self.assertEqual(len(replacement.state['specimens']), 1)

    def test_source_capacity_and_epoch_are_enforced(self):
        self.specimen('insert1'); self.specimen('insert2')
        self.command('insert3', 'insert_specimen', {'cabinet_id': 'hospital-cabinet-b'})
        self.tick()
        self.assertEqual(self.ack('insert3')['error'], 'source_cabinet_full')
        self.command('old', 'insert_specimen', {'cabinet_id': 'hospital-cabinet-c'}, epoch='old-epoch')
        self.tick()
        self.assertFalse(self.normal_path('old', epoch='old-epoch').exists())
        self.assertFalse(self.normal_path('old').exists())  # Another epoch is not consumed or ACKed.
        self.assertEqual(len(self.world.state['specimens']), 2)

    def test_transfer_ack_waits_for_actual_change_and_door_close(self):
        sid = self.specimen()
        self.handoff(sid)
        self.tick()
        self.assertFalse(self.normal_path('pickup1').exists())
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')
        self.tick(.8)
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')
        self.assertFalse(self.normal_path('pickup1').exists())
        self.tick(.8)
        ack = self.ack('pickup1')
        self.assertEqual(ack['status'], 'completed')
        self.assertEqual(ack['result']['location'], 'robot')
        self.assertEqual(self.actuator.doors['hospital-cabinet-b'], 0)
        # A new operation for the same logical transfer returns the original proof.
        self.handoff(sid, ident='pickup_retry')
        self.tick()
        self.assertTrue(self.ack('pickup_retry')['result']['replayed'])
        self.assertEqual(self.ack('pickup_retry')['result']['operation_id'], 'pickup1')
        self.assertEqual(len(self.world.state['transfers']), 1)

    def test_pose_or_stop_failure_does_not_move_specimen(self):
        sid = self.specimen()
        self.actual['x'] = 99
        self.handoff(sid)
        self.tick()
        self.assertEqual(self.ack('pickup1')['error'], 'physical_scene_wrong_handoff_location')
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')
        self.actual.update(x=1., stopped_samples=0)
        self.handoff(sid, 'pickup2')
        self.tick()
        self.assertEqual(self.ack('pickup2')['error'], 'physical_stop_not_confirmed')

    def test_stop_is_rechecked_during_animation(self):
        sid = self.specimen(); self.handoff(sid); self.tick()
        self.actual['stopped_samples'] = 0
        self.tick(.8); self.tick(.8)
        self.assertEqual(self.ack('pickup1')['error'], 'physical_stop_not_confirmed')
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')

    def test_slow_real_time_factor_keeps_original_claim_but_requires_new_physical_samples(self):
        sid = self.specimen(); self.handoff(sid); self.tick()
        original_wall = time.time()
        # 0.8 simulated seconds can take five real seconds.  Admission proof is
        # now old, but fresh physical samples continuously confirm this transfer.
        with patch('mhs.smart_scene.time.time', return_value=original_wall+5):
            self.tick(.8)
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')
        with patch('mhs.smart_scene.time.time', return_value=original_wall+10):
            self.tick(.8)
        self.assertEqual(self.ack('pickup1')['status'], 'completed')

    def test_slow_transfer_rejects_a_stale_physical_sample(self):
        sid = self.specimen(); self.handoff(sid); self.tick()
        with patch('mhs.smart_scene.time.time', return_value=time.time()+5):
            self.sim += .8
            self.world.tick(self.sim)  # Deliberately do NOT refresh actual wall_time.
        self.assertEqual(self.ack('pickup1')['error'], 'physical_scene_pose_stale')
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')

    def test_crash_after_world_transfer_before_ack_recovers_without_second_transfer(self):
        sid = self.specimen(); self.handoff(sid); self.tick(); self.tick(.8)
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')
        self.assertFalse(self.normal_path('pickup1').exists())
        replacement = SmartWardWorld(self.root, self.actuator, lambda: copy.deepcopy(self.actual), self.config)
        replacement.tick(self.sim+.1)
        self.assertEqual(self.ack('pickup1')['result']['location'], 'robot')
        self.assertEqual(len(replacement.state['transfers']), 1)

    def test_epoch_reset_clears_world_and_never_replays_old_commands(self):
        self.specimen()
        atomic_json(self.directory/'control.json', {'run_id': 'run2', 'epoch': 'epoch2'})
        self.tick()
        self.assertEqual(self.world.state['run_id'], 'run2')
        self.assertEqual(self.world.state['specimens'], {})
        self.assertEqual(self.world.state['transfers'], {})

    def stop_control(self):
        atomic_json(self.directory/'control.json', {'run_id': 'run1', 'epoch': 'epoch1', 'enabled': False, 'stop_requested': True})

    def test_stop_cancels_uncommitted_handoff_and_preserves_specimen(self):
        sid = self.specimen(); self.handoff(sid); self.tick()
        self.stop_control(); self.tick()
        self.assertEqual(self.ack('pickup1')['error'], 'run_stopped_before_commit')
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')
        self.assertEqual(self.world.state['transfers'], {})
        self.assertEqual(self.actuator.doors['hospital-cabinet-b'], 0)

    def test_stop_does_not_rollback_committed_handoff_or_lose_ack(self):
        sid = self.specimen(); self.handoff(sid); self.tick(); self.tick(.8)
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')
        self.stop_control(); self.tick(.8)
        self.assertEqual(self.ack('pickup1')['status'], 'completed')
        self.assertEqual(self.ack('pickup1')['result']['location'], 'robot')
        self.assertEqual(len(self.world.state['transfers']), 1)

    def test_committed_handoff_restart_while_stopped_still_recovers_success_ack(self):
        sid = self.specimen(); self.handoff(sid); self.tick(); self.tick(.8)
        self.stop_control()
        replacement = SmartWardWorld(self.root, self.actuator, lambda: copy.deepcopy(self.actual), self.config)
        replacement.tick(self.sim+.1)
        self.assertEqual(self.ack('pickup1')['status'], 'completed')
        self.assertEqual(self.ack('pickup1')['result']['location'], 'robot')
        self.assertEqual(len(replacement.state['transfers']), 1)

    def test_stopped_queue_allows_demo_insertion_but_blocks_light_actuation(self):
        self.stop_control()
        self.specimen()
        self.assertEqual(len(self.world.state['specimens']), 1)
        self.command('light_when_stopped', 'set_brightness', {'room_id': 'room301', 'brightness_percent': 10})
        self.tick()
        self.assertEqual(self.ack('light_when_stopped')['error'], 'run_stopped_before_commit')
        self.assertEqual(self.actuator.lights['room301'], 60)

    def test_receive_requires_real_lab_arrival_and_availability(self):
        sid = self.specimen(); self.handoff(sid); self.complete_animation()
        self.actual.update(x=5., y=6.)
        self.command('receiver_off', 'set_receiver', {'available': False}); self.tick()
        self.handoff(sid, 'receive1', 'receive-transfer1', 'receive'); self.tick()
        self.assertEqual(self.ack('receive1')['error'], 'handoff_receiver_unavailable_or_wrong_station')
        self.command('receiver_on', 'set_receiver', {'available': True}); self.tick()
        self.handoff(sid, 'receive2', 'receive-transfer1', 'receive'); self.complete_animation()
        self.assertEqual(self.ack('receive2')['result']['location'], 'hospital-cabinet-lab')
        self.assertIn(sid, self.world.state['devices']['hospital-cabinet-lab']['specimens'])

    def test_real_light_readback_stuck_fault_and_paused_sampling(self):
        self.command('dim', 'set_brightness', {'room_id': 'room301', 'brightness_percent': 10}); self.tick()
        self.assertEqual(self.actuator.lights['room301'], 10)
        self.command('stuck', 'set_lamp_fault', {'room_id': 'room301', 'stuck': True}); self.tick()
        self.command('bright', 'set_brightness', {'room_id': 'room301', 'brightness_percent': 60}); self.tick()
        self.assertEqual(self.ack('bright')['result']['actual_percent'], 10)
        self.world.last_publish = 0; self.tick()
        observation = json.loads((self.directory/'observation.json').read_text())
        self.assertEqual(observation['devices']['hospital-sensor-301']['relative_illumination'], 28)
        self.assertEqual(observation['devices']['hospital-sensor-301']['unit'], 'relative_index_0_100')
        before = (self.directory/'observation.json').read_bytes()
        self.world.last_publish = 0; self.world.tick(self.sim)
        self.assertEqual((self.directory/'observation.json').read_bytes(), before)


class FaultQualificationTests(unittest.TestCase):
    setUp = ProtocolTests.setUp
    command = ProtocolTests.command
    normal_path = ProtocolTests.normal_path
    tick = ProtocolTests.tick
    ack = ProtocolTests.ack
    specimen = ProtocolTests.specimen
    handoff = ProtocolTests.handoff
    complete_animation = ProtocolTests.complete_animation

    def qualify(self, seconds=240):
        now = time.time()
        self.timeline = Timeline()
        self.world.faults.timeline = self.timeline
        self.q = {'active': True, 'qualification_id': 'qualification001', 'session_key': 'qualification:test',
                  'started_wall': now, 'deadline_wall': now+seconds, 'simulation_pause_requested': True}
        self.control = {'run_id': 'run1', 'epoch': 'epoch1', 'fault_qualification': self.q}
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)

    def fault(self, op, kind, args, session=None):
        value = {'run_id': 'run1', 'epoch': 'epoch1', 'qualification_id': self.q['qualification_id'],
                 'session_key': session or self.q['session_key'], 'operation_id': op,
                 'kind': kind, 'arguments': args, 'wall_time': time.time()}
        atomic_json(self.directory/'fault-commands'/(op+'.json'), value)
        self.world.poll_controls(self.sim)
        return json.loads((self.directory/'fault-acks'/(op+'.json')).read_text())

    def test_controls_require_explicit_current_qualification(self):
        self.qualify()
        ack = self.fault('wrong-session', 'set_cabinet_availability',
                         {'cabinet_id': 'hospital-cabinet-b', 'available': False}, session='demo:wrong')
        self.assertEqual(ack['status'], 'failed')
        self.assertTrue(self.world.state['devices']['hospital-cabinet-b']['available'])
        self.control['fault_qualification']['active'] = False
        atomic_json(self.directory/'control.json', self.control)
        ack = self.fault('closed', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': False})
        self.assertEqual(ack['status'], 'failed')

    def test_actual_unavailable_cabinet_blocks_handoff_and_restores_original(self):
        sid = self.specimen()
        self.qualify()
        self.assertEqual(self.fault('offline-b', 'set_cabinet_availability',
                                   {'cabinet_id': 'hospital-cabinet-b', 'available': False})['status'], 'completed')
        self.handoff(sid); self.tick()
        self.assertEqual(self.ack('pickup1')['error'], 'handoff_receiver_unavailable_or_wrong_station')
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'hospital-cabinet-b')
        self.q['active'] = False
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertTrue(self.world.state['devices']['hospital-cabinet-b']['available'])

    def test_ack_is_actual_original_bytes_released_without_second_transfer(self):
        sid = self.specimen(); self.qualify()
        self.handoff(sid); self.tick()
        self.assertIsNotNone(self.world.pending)
        self.assertEqual(self.fault('hold-pickup', 'hold_ack', {'target_operation_id': 'pickup1'})['status'], 'completed')
        self.tick(.8); self.tick(.8)
        held = self.normal_path('pickup1', 'withheld-acks')
        original = held.read_bytes()
        self.assertFalse(self.normal_path('pickup1').exists())
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')
        self.tick()
        self.assertEqual(held.read_bytes(), original)
        self.assertEqual(len(self.world.state['transfers']), 1)
        self.assertEqual(self.fault('release-pickup', 'release_ack', {'target_operation_id': 'pickup1'})['status'], 'completed')
        self.assertEqual(self.normal_path('pickup1').read_bytes(), original)
        self.assertFalse(held.exists())
        self.assertEqual(len(self.world.state['transfers']), 1)

    def test_already_visible_ack_is_never_hidden(self):
        sid = self.specimen(); self.qualify(); self.handoff(sid); self.complete_animation()
        original = self.normal_path('pickup1').read_bytes()
        ack = self.fault('too-late', 'hold_ack', {'target_operation_id': 'pickup1'})
        self.assertEqual(ack['error'], 'target_ack_already_visible')
        self.assertEqual(self.normal_path('pickup1').read_bytes(), original)

    def test_pause_is_actual_timeline_and_wall_polled_resume_does_not_refresh_observation(self):
        self.qualify()
        observation = (self.directory/'observation.json').read_bytes()
        self.assertEqual(self.fault('pause', 'pause_simulation', {'seconds': 8})['status'], 'completed')
        self.assertFalse(self.timeline.is_playing())
        deadline = self.world.faults.state['paused']['resume_wall']
        with patch('mhs.smart_scene.time.time', return_value=deadline+.01):
            self.world.poll_controls(self.sim)
        self.assertTrue(self.timeline.is_playing())
        self.assertEqual(self.timeline.plays, 1)
        self.assertEqual((self.directory/'observation.json').read_bytes(), observation)

    def test_pause_refuses_moving_stale_or_unverified_actual_physical_samples(self):
        self.qualify()
        original = copy.deepcopy(self.actual)
        cases = [({'speed_mps': .10}, 'motion_detected'),
                 ({'angular_speed_rad_s': -.04}, 'motion_detected'),
                 ({'wall_time': time.time()-2}, 'sample_stale'),
                 ({'velocity_wall_time': time.time()-2}, 'sample_stale'),
                 ({'velocity_sim_time': self.sim-.2}, 'sample_stale'),
                 ({'velocity_available': False}, 'sample_stale'),
                 ({'stopped_samples': 2}, 'stop_not_confirmed')]
        for index, (fields, expected) in enumerate(cases):
            self.actual = {**copy.deepcopy(original), **fields}
            ack = self.fault('unsafe-pause-'+str(index), 'pause_simulation', {'seconds': 8})
            self.assertEqual(ack['status'], 'failed')
            self.assertIn(expected, ack['error'])
            self.assertTrue(self.timeline.is_playing())
            self.assertIsNone(self.world.faults.state['paused'])
        self.assertEqual(self.timeline.pauses, 0)

    def test_pause_requires_backend_persistent_motion_lock(self):
        self.qualify()
        self.q.pop('simulation_pause_requested')
        atomic_json(self.directory/'control.json', self.control)
        ack = self.fault('unlocked-pause', 'pause_simulation', {'seconds': 8})
        self.assertEqual(ack['error'], 'qualification_pause_backend_motion_lock_not_confirmed')
        self.assertEqual(self.timeline.pauses, 0)

    def test_resume_preserves_motion_lock_and_unconfirmed_identity_requires_stop(self):
        self.qualify()
        self.fault('pause', 'pause_simulation', {'seconds': 8})
        self.assertFalse(self.timeline.is_playing())
        self.q['qualification_id'] = 'differentqualification002'
        self.q['simulation_pause_requested'] = False
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertFalse(self.timeline.is_playing())
        self.assertEqual(self.world.faults.state['qualification_id'], 'qualification001')
        self.assertIsNotNone(self.world.faults.state['paused'])
        self.control['stop_requested'] = True
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertTrue(self.timeline.is_playing())
        self.assertIsNone(self.world.faults.state['paused'])

    def test_explicit_resume_never_clears_backend_motion_latch(self):
        self.qualify()
        self.fault('pause', 'pause_simulation', {'seconds': 8})
        ack = self.fault('resume', 'resume_simulation', {})
        self.assertEqual(ack['status'], 'completed')
        self.assertTrue(self.timeline.is_playing())
        self.assertTrue(self.world.control['fault_qualification']['simulation_pause_requested'])
        self.assertFalse(ack['result']['resumes_navigation'])

    def test_begin_end_before_scene_poll_records_honest_no_fault_control_receipt(self):
        now = time.time()
        q = {'active': False, 'qualification_id': 'qualification-never-observed',
             'session_key': 'qualification:short', 'started_wall': now, 'deadline_wall': now+240}
        atomic_json(self.directory/'control.json', {'run_id': 'run1', 'epoch': 'epoch1',
                    'enabled': False, 'stop_requested': True, 'fault_qualification': q})
        self.world.poll_controls(self.sim)
        receipt = json.loads((self.directory/'fault-state.json').read_text())
        self.assertFalse(receipt['active'])
        self.assertTrue(receipt['no_fault_actions_observed'])
        self.assertEqual(receipt['qualification_id'], q['qualification_id'])
        self.assertEqual(receipt['holds'], {})
        self.assertIsNone(receipt['paused'])
        self.assertEqual(list(self.normal_path('scope-probe').parent.glob('*.json')), [])
        original = (self.directory/'fault-state.json').read_bytes()
        self.world.poll_controls(self.sim)
        self.assertEqual((self.directory/'fault-state.json').read_bytes(), original)

    def test_foreign_inactive_control_cannot_discard_unresolved_paused_qualification(self):
        self.qualify(); self.fault('pause', 'pause_simulation', {'seconds': 8})
        old = copy.deepcopy(self.world.faults.state)
        self.q.update(active=False, qualification_id='foreign-inactive-qualification', simulation_pause_requested=False)
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertEqual(self.world.faults.state['qualification_id'], old['qualification_id'])
        self.assertIsNotNone(self.world.faults.state['paused'])
        self.assertFalse(self.timeline.is_playing())

    def test_stop_cleans_controls_and_preserves_committed_original_ack(self):
        sid = self.specimen(); self.qualify(); self.handoff(sid); self.tick()
        self.fault('hold', 'hold_ack', {'target_operation_id': 'pickup1'})
        self.tick(.8); self.tick(.8)
        original = self.normal_path('pickup1', 'withheld-acks').read_bytes()
        self.fault('offline', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-c', 'available': False})
        self.fault('pause', 'pause_simulation', {'seconds': 8})
        self.control.update(enabled=False, stop_requested=True)
        self.q['active'] = False; atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertTrue(self.timeline.is_playing())
        self.assertTrue(self.world.state['devices']['hospital-cabinet-c']['available'])
        self.assertEqual(self.normal_path('pickup1').read_bytes(), original)
        self.assertEqual(self.world.state['specimens'][sid]['location'], 'robot')

    def test_restart_restores_saved_availability_and_releases_original_ack(self):
        sid = self.specimen(); self.qualify(); self.handoff(sid); self.tick()
        self.fault('hold', 'hold_ack', {'target_operation_id': 'pickup1'})
        self.tick(.8); self.tick(.8)
        original = self.normal_path('pickup1', 'withheld-acks').read_bytes()
        self.fault('offline', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-c', 'available': False})
        replacement = SmartWardWorld(self.root, self.actuator, lambda: self.actual, self.config, self.timeline)
        replacement.poll_controls(self.sim)
        self.assertTrue(replacement.state['devices']['hospital-cabinet-c']['available'])
        self.assertEqual(self.normal_path('pickup1').read_bytes(), original)
        self.assertFalse(replacement.faults.state['active'])
        self.assertIsNone(replacement.faults.qualification())

    def test_expired_or_modified_lease_cannot_extend_or_reopen(self):
        self.qualify()
        self.fault('offline', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': False})
        self.q['deadline_wall'] += 1
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertFalse(self.world.faults.state['active'])
        self.assertTrue(self.world.state['devices']['hospital-cabinet-b']['available'])
        self.q['deadline_wall'] -= 1
        atomic_json(self.directory/'control.json', self.control)
        self.world.poll_controls(self.sim)
        self.assertIsNone(self.world.faults.qualification())

    def test_fault_payload_conflict_retains_original_receipt_and_failure_budget_is_finite(self):
        self.qualify()
        self.fault('same-op', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': False})
        original = (self.directory/'fault-acks/same-op.json').read_bytes()
        self.fault('same-op', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': True})
        self.assertEqual((self.directory/'fault-acks/same-op.json').read_bytes(), original)
        self.assertFalse(self.world.state['devices']['hospital-cabinet-b']['available'])
        for i in range(19):
            self.assertEqual(self.fault('bad-'+str(i), 'invalid', {})['status'], 'failed')
        ack = self.fault('over-limit', 'set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': True})
        self.assertEqual(ack['error'], 'qualification_fault_operation_limit')
        self.assertFalse(self.world.state['devices']['hospital-cabinet-b']['available'])


class PhysicalSamplingTests(unittest.TestCase):
    def setUp(self):
        self.scene = SmartWardScene.__new__(SmartWardScene)
        self.scene.physical = self.scene.previous_physical = self.scene.last_velocity = None
        self.scene.stopped_samples = 0

    def capture(self, sim, wall, x=0, yaw=0):
        return self.scene._update_physical([x, 0, 0], yaw, sim, wall)

    def test_velocity_is_absent_before_first_actual_computation(self):
        self.capture(0, 100)
        self.assertNotIn('speed_mps', self.scene.physical)
        self.assertFalse(self.scene.physical['velocity_available'])
        self.assertEqual(self.scene.physical['stopped_samples'], 0)

    def test_tiny_ticks_keep_original_finite_velocity_and_capture_stamps(self):
        self.capture(0, 100); self.capture(.1, 100.5, .02)
        computed = copy.deepcopy(self.scene.physical)
        for sim, wall in [(.11, 100.55), (.13, 100.6), (.19, 100.9)]:
            self.capture(sim, wall, .02)
            self.assertEqual(self.scene.physical['speed_mps'], computed['speed_mps'])
            self.assertEqual(self.scene.physical['velocity_wall_time'], 100.5)
            self.assertEqual(self.scene.physical['velocity_sim_time'], .1)
            self.assertEqual(self.scene.physical['velocity_seq'], 1)
            self.assertTrue(self.scene.physical['velocity_available'])

    def test_pause_does_not_freshen_and_resume_requires_new_stillness_samples(self):
        self.capture(0, 100)
        for i in range(1, 4): self.capture(i*.1, 100+i*.3)
        before = copy.deepcopy(self.scene.physical)
        self.assertEqual(before['stopped_samples'], 3)
        for wall in [101, 103, 110]: self.assertFalse(self.capture(.3, wall))
        self.assertEqual(self.scene.physical, before)
        self.capture(.31, 110.1)
        self.assertFalse(self.scene.physical['velocity_available'])
        self.assertEqual(self.scene.physical['stopped_samples'], 0)
        self.assertEqual(self.scene.physical['velocity_wall_time'], 100.9)
        self.capture(.4, 110.5)
        self.assertEqual(self.scene.physical['stopped_samples'], 1)

    def test_motion_keeps_original_strict_stationary_gate(self):
        self.capture(0, 100)
        for i in range(1, 4): self.capture(i*.1, 100+i*.2)
        self.capture(.4, 100.8, .004)
        self.assertGreater(self.scene.physical['speed_mps'], .03)
        self.assertEqual(self.scene.physical['stopped_samples'], 0)

    def test_stationary_count_only_advances_on_actual_velocity_samples(self):
        self.capture(0, 100); self.capture(.1, 100.2)
        count, seq = self.scene.physical['stopped_samples'], self.scene.physical['seq']
        self.capture(.12, 100.25)
        self.assertEqual(self.scene.physical['stopped_samples'], count)
        self.assertEqual(self.scene.physical['velocity_seq'], 1)
        self.assertGreater(self.scene.physical['seq'], seq)


class GeometryTests(unittest.TestCase):
    def test_cabinet_device_ids_map_to_distinct_valid_USD_identifiers(self):
        mapped = [usd_device_identifier(ident) for ident in CABINETS]
        self.assertEqual(len(mapped), len(set(mapped)))
        self.assertTrue(all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value) for value in mapped))
        self.assertEqual(usd_device_identifier('hospital-cabinet-b'), 'hospital_cabinet_b')
        with self.assertRaisesRegex(ValueError, 'unknown_cabinet'):
            usd_device_identifier('../cabinet')

    def test_spatial_index_is_equivalent_to_full_collision_scan_fixed_seed(self):
        generator = random.Random(301303)
        room = {'id': 'indexed', 'bounds': [-3.2, -2.7, 3.3, 3.8], 'handoff_anchor': [3.3, 1]}
        for trial in range(20):
            obstacles = []
            for _ in range(80):
                x, y, z = generator.uniform(-5, 5), generator.uniform(-5, 5), generator.uniform(-1, 2)
                obstacles.append([x, y, z, x+generator.uniform(0, 1.2), y+generator.uniform(0, 1.2), z+generator.uniform(0, 2)])
            # Merged huge faces and a padding-boundary contact are never dropped.
            obstacles.extend([[-100, -100, -1, 100, 100, .02], [-3.4, -.5, .1, -3.2, .5, 1.4]])
            index = ObstacleIndex(obstacles, room['bounds'])
            for _ in range(20):
                x, y = generator.uniform(-3, 3), generator.uniform(-2.5, 3.5)
                candidate = [x-.2, y-.2, .04, x+.2, y+.2, 1.35]
                # Candidate center sampling is constrained to a whole in-room
                # envelope, as choose_cabinet_position does.
                if candidate[0] < -3.2 or candidate[1] < -2.7 or candidate[3] > 3.3 or candidate[4] > 3.8:
                    continue
                self.assertEqual(any(boxes_intersect(candidate, value, .06) for value in obstacles),
                                 any(boxes_intersect(candidate, value, .06) for value in index.query(candidate)))
            outcomes = []
            for use_index in (False, True):
                try:
                    outcomes.append(choose_cabinet_position(room, [.4, .9, 1.35], obstacles, [], use_spatial_index=use_index))
                except RuntimeError as error:
                    outcomes.append(str(error))
            self.assertEqual(outcomes[0], outcomes[1])

    def test_nav_volume_exclusion_requires_schema_and_no_real_geometry_or_collision(self):
        nav = {'type': 'NavMeshVolume', 'applied_schemas': ['NavMeshAreaAPI'],
               'is_gprim': False, 'collision_enabled': False, 'purpose': 'default', 'visibility': 'inherited'}
        self.assertEqual(geometry_inventory_decision(nav)[0], 'exclude')
        self.assertEqual(geometry_inventory_decision({**nav, 'collision_enabled': True})[0], 'obstacle')
        self.assertEqual(geometry_inventory_decision({**nav, 'is_gprim': True})[0], 'obstacle')
        # A large Mesh/path named NavMeshVolume remains geometry; never skip it.
        mesh = {**nav, 'type': 'Mesh', 'path': '/World/hospital/Navmesh/NavMeshVolume', 'is_gprim': True}
        self.assertEqual(geometry_inventory_decision(mesh)[0], 'obstacle')

    def test_guide_or_hidden_colliders_still_block_placement(self):
        hidden = {'type': 'Mesh', 'is_gprim': True, 'purpose': 'guide', 'visibility': 'invisible', 'collision_enabled': True}
        self.assertEqual(geometry_inventory_decision(hidden)[0], 'obstacle')
        self.assertEqual(geometry_inventory_decision({**hidden, 'collision_enabled': False})[0], 'exclude')
        self.assertEqual(geometry_inventory_decision({'type': 'UnknownBoundable'})[0], 'obstacle')

    def test_merged_mesh_uses_actual_faces_without_filling_empty_room(self):
        # Two real wall triangles have a whole-Mesh bound over the empty center.
        points = [(0, 0, .1), (0, 4, .1), (0, 4, 1.4),
                  (4, 0, .1), (4, 4, .1), (4, 4, 1.4)]
        boxes = mesh_face_bounds(points, [3, 3], [0, 1, 2, 3, 4, 5])
        room = {'id': 'merged', 'bounds': [0, 0, 4, 4], 'handoff_anchor': [4, 2]}
        placement = choose_cabinet_position(room, [.4, .9, 1.35], boxes, [])
        self.assertGreater(placement['candidate_count'], 0)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(mesh_face_bounds([(0, 0, 0), (4, 0, 0), (4, 4, 0)], [3], [0, 1, 2]), [])
        with self.assertRaisesRegex(ValueError, 'topology_invalid'):
            mesh_face_bounds(points, [3], [0, 1])
        with self.assertRaisesRegex(ValueError, 'index_invalid'):
            mesh_face_bounds(points, [3], [0, 1, 99])

    def test_deterministic_placement_respects_furniture_and_station_clearance(self):
        room = {'id': 'test', 'bounds': [0, 0, 4, 4], 'handoff_anchor': [4, 2]}
        obstacles = [[1, 1, .1, 2.5, 3, 1.5]]
        points = [{'x': 4.5, 'y': 2}]
        first = choose_cabinet_position(room, [.4, .9, 1.35], obstacles, points)
        self.assertEqual(first, choose_cabinet_position(room, [.4, .9, 1.35], obstacles, points))
        with self.assertRaisesRegex(RuntimeError, 'no_collision_free_placement'):
            choose_cabinet_position(room, [.4, .9, 1.35], [[0, 0, 0, 4, 4, 2]], points)

    def test_map_projection_never_changes_official_map_and_rejects_goal_intrusion(self):
        with isolated_directory() as temporary:
            root = Path(temporary)
            Image.new('L', (100, 100), 255).save(root/'source.png')
            (root/'source.yaml').write_text('image: source.png\nresolution: 0.1\norigin: [0, 0, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
            before = hashlib.sha256((root/'source.png').read_bytes()).hexdigest()
            cabinets = [{'id': 'cabinet', 'collision_bounds_xy': [1, 2, 2, 3]}]
            proof = write_overlay_map(root/'source.yaml', root/'overlay', cabinets, [{'x': 8, 'y': 8}])
            self.assertEqual(before, hashlib.sha256((root/'source.png').read_bytes()).hexdigest())
            image = Image.open(proof['map_image'])
            self.assertEqual(image.getpixel((15, 75)), 0)
            self.assertEqual(image.getpixel((80, 20)), 255)
            self.assertEqual(bounds_to_pixels([1, 2, 2, 3], load_map_metadata(root/'source.yaml'), image.size), [10, 70, 19, 79])
            with self.assertRaisesRegex(ValueError, 'intrudes_navigation_clearance'):
                write_overlay_map(root/'source.yaml', root/'bad', cabinets, [{'x': 2.1, 'y': 2.5}])


if __name__ == '__main__':
    unittest.main()
