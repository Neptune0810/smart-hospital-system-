"""Run/epoch scene isolation, original ACK and stopped reconciliation checks.

Only deterministic protocol fixtures. No Isaac, ROS, model or server process.
"""
import copy
import json
import time
import unittest

from .scene_io import SceneIOConflict, publish_command, scene_path
from .smart_scene import SmartWardWorld, atomic_json
from . import test_smart_scene as original


class SceneScopeTests(unittest.TestCase):
    setUp = original.ProtocolTests.setUp
    command = original.ProtocolTests.command
    normal_path = original.ProtocolTests.normal_path
    tick = original.ProtocolTests.tick
    ack = original.ProtocolTests.ack
    specimen = original.ProtocolTests.specimen
    handoff = original.ProtocolTests.handoff
    complete_animation = original.ProtocolTests.complete_animation
    stop_control = original.ProtocolTests.stop_control
    qualify = original.FaultQualificationTests.qualify
    fault = original.FaultQualificationTests.fault

    def test_global_old_command_and_ACK_do_not_alias_current_same_operation(self):
        old = {'run_id':'oldrun','epoch':'oldepoch','operation_id':'same-call',
            'kind':'insert_specimen','arguments':{'cabinet_id':'hospital-cabinet-c'},'wall_time':time.time()}
        old_ack = {key:old[key] for key in ('run_id','epoch','operation_id')}
        old_ack.update(status='completed',result={'specimen_id':'not-a-current-specimen'},wall_time=1,sim_time=1)
        legacy_command = self.directory/'commands/same-call.json'
        legacy_ack = self.directory/'acks/same-call.json'
        atomic_json(legacy_command,old); atomic_json(legacy_ack,old_ack)
        before = (legacy_command.read_bytes(),legacy_ack.read_bytes())
        self.tick()
        self.assertEqual(self.world.state['specimens'],{})
        self.command('same-call','insert_specimen',{'cabinet_id':'hospital-cabinet-b'})
        self.tick()
        current = self.ack('same-call')
        self.assertEqual((current['run_id'],current['epoch']),('run1','epoch1'))
        self.assertEqual(len(self.world.state['specimens']),1)
        self.assertEqual(before,(legacy_command.read_bytes(),legacy_ack.read_bytes()))

    def test_same_operation_id_in_two_runs_has_independent_originals(self):
        first_id = self.specimen('reused-call')
        first_path = self.normal_path('reused-call')
        first_bytes = first_path.read_bytes()
        atomic_json(self.directory/'control.json',{'run_id':'run2','epoch':'epoch2'})
        self.tick()
        command = {'run_id':'run2','epoch':'epoch2','operation_id':'reused-call','kind':'insert_specimen',
            'arguments':{'cabinet_id':'hospital-cabinet-c'},'wall_time':time.time()}
        publish_command(self.directory,command); self.tick()
        path = scene_path(self.directory,'acks','run2','epoch2','reused-call')
        second = json.loads(path.read_text())
        self.assertNotEqual(first_id,second['result']['specimen_id'])
        self.assertEqual(first_path.read_bytes(),first_bytes)
        self.assertEqual(set(self.world.state['specimens']),{second['result']['specimen_id']})
        self.assertEqual(second['result']['specimen']['location'],'hospital-cabinet-c')

    def test_another_epoch_queue_is_not_read_or_ACKed(self):
        command = self.command('other-epoch','insert_specimen',{'cabinet_id':'hospital-cabinet-b'},epoch='old-epoch')
        before = scene_path(self.directory,'commands','run1','old-epoch','other-epoch').read_bytes()
        self.tick()
        self.assertEqual(self.world.state['specimens'],{})
        self.assertFalse(scene_path(self.directory,'acks','run1','old-epoch','other-epoch').exists())
        self.assertFalse(self.normal_path('other-epoch').exists())
        self.assertEqual(scene_path(self.directory,'commands','run1','old-epoch','other-epoch').read_bytes(),before)

    def test_wrong_identity_inside_current_folder_is_refused_without_foreign_ACK(self):
        poisoned = {'run_id':'run1','epoch':'old-epoch','operation_id':'wrong-scope','kind':'insert_specimen',
            'arguments':{'cabinet_id':'hospital-cabinet-b'},'wall_time':time.time()}
        path = scene_path(self.directory,'commands','run1','epoch1','wrong-scope')
        atomic_json(path,poisoned); self.tick()
        self.assertEqual(self.world.state['specimens'],{})
        self.assertFalse(self.normal_path('wrong-scope').exists())
        self.assertFalse(scene_path(self.directory,'acks','run1','old-epoch','wrong-scope').exists())
        conflicts = list(scene_path(self.directory,'fault-history','run1','epoch1','probe').parent.glob('command-conflict-*.json'))
        self.assertEqual(len(conflicts),1)
        self.assertEqual(json.loads(conflicts[0].read_text())['error'],'scene_run_or_epoch_mismatch')

    def test_existing_successful_ACK_is_not_rewritten_on_scene_payload_tamper(self):
        sid = self.specimen('immutable')
        original_ack = self.normal_path('immutable').read_bytes()
        # Simulate a damaged producer bypassing publish_command. The scene is
        # still required to refuse the changed meaning, preserving its original.
        path = scene_path(self.directory,'commands','run1','epoch1','immutable')
        damaged = json.loads(path.read_text()); damaged['arguments']['cabinet_id']='hospital-cabinet-c'
        atomic_json(path,damaged); self.tick()
        self.assertEqual(self.normal_path('immutable').read_bytes(),original_ack)
        self.assertEqual(set(self.world.state['specimens']),{sid})
        self.assertEqual(self.world.state['specimens'][sid]['location'],'hospital-cabinet-b')
        conflicts = list(scene_path(self.directory,'fault-history','run1','epoch1','probe').parent.glob('command-conflict-*.json'))
        self.assertEqual(len(conflicts),1)
        self.assertEqual(json.loads(conflicts[0].read_text())['error'],'operation_id_payload_conflict')

    def test_republished_current_unknown_handoff_after_stop_fails_without_moving_box(self):
        sid = self.specimen()
        legacy = {'run_id':'run1','epoch':'epoch1','operation_id':'original-unknown','kind':'handoff',
            'arguments':{'transfer_id':'t-original','order_id':'o-original','specimen_id':sid,
                'cabinet_id':'hospital-cabinet-b','direction':'pickup','station_id':'station_b',
                'arrival_proof':{'pose':{'x':1.,'y':2.},'wall_time':time.time()}},'wall_time':time.time()}
        old_path = self.directory/'commands/original-unknown.json'
        atomic_json(old_path,legacy); old_bytes=old_path.read_bytes()
        self.stop_control()
        # An explicit current producer reconciles the exact original operation;
        # the scene never reads the legacy inbox as a fallback.
        publish_command(self.directory,legacy); self.tick()
        ack = self.ack('original-unknown'); original_ack=self.normal_path('original-unknown').read_bytes()
        self.assertEqual(ack['status'],'failed')
        self.assertEqual(ack['error'],'run_stopped_before_commit')
        self.assertEqual(self.world.state['specimens'][sid]['location'],'hospital-cabinet-b')
        self.assertEqual(self.world.state['transfers'],{})
        self.assertIsNone(self.world.pending)
        self.assertEqual(self.actuator.doors['hospital-cabinet-b'],0.)
        self.tick()
        self.assertEqual(self.normal_path('original-unknown').read_bytes(),original_ack)
        self.assertEqual(old_path.read_bytes(),old_bytes)

    def test_original_ACK_replay_preserves_wall_and_sim_bytes(self):
        self.specimen('same')
        path=self.normal_path('same'); original_ack=path.read_bytes()
        self.tick(10)
        self.assertEqual(path.read_bytes(),original_ack)
        replacement=SmartWardWorld(self.root,self.actuator,lambda:copy.deepcopy(self.actual),self.config)
        replacement.tick(self.sim+.1)
        self.assertEqual(path.read_bytes(),original_ack)

    def test_hold_and_release_use_only_current_namespace_original_bytes(self):
        sid=self.specimen(); self.qualify(); self.handoff(sid); self.tick()
        legacy = self.directory/'withheld-acks/pickup1.json'
        atomic_json(legacy,{'run_id':'foreign','epoch':'foreign','operation_id':'pickup1','status':'failed'})
        legacy_bytes=legacy.read_bytes()
        self.assertEqual(self.fault('hold','hold_ack',{'target_operation_id':'pickup1'})['status'],'completed')
        self.tick(.8); self.tick(.8)
        held=self.normal_path('pickup1','withheld-acks'); held_bytes=held.read_bytes()
        self.assertFalse(self.normal_path('pickup1').exists())
        self.assertEqual(self.fault('release','release_ack',{'target_operation_id':'pickup1'})['status'],'completed')
        self.assertEqual(self.normal_path('pickup1').read_bytes(),held_bytes)
        self.assertEqual(self.normal_path('pickup1','fault-history').read_bytes(),held_bytes)
        self.assertFalse(held.exists())
        self.assertEqual(legacy.read_bytes(),legacy_bytes)
        self.assertEqual(len(self.world.state['transfers']),1)

    def test_hold_cannot_target_legacy_global_command(self):
        self.qualify()
        legacy = {'run_id':'run1','epoch':'epoch1','operation_id':'legacy-handoff','kind':'handoff','arguments':{}}
        atomic_json(self.directory/'commands/legacy-handoff.json',legacy)
        ack=self.fault('wrong-inbox','hold_ack',{'target_operation_id':'legacy-handoff'})
        self.assertEqual(ack['error'],'qualification_handoff_command_not_found')
        self.assertEqual(self.world.faults.state['holds'],{})

    def test_scope_change_releases_old_original_only_to_its_own_epoch(self):
        sid=self.specimen(); self.qualify(); self.handoff(sid); self.tick()
        self.fault('hold','hold_ack',{'target_operation_id':'pickup1'})
        self.tick(.8); self.tick(.8)
        held=self.normal_path('pickup1','withheld-acks'); original_bytes=held.read_bytes()
        atomic_json(self.directory/'control.json',{'run_id':'run2','epoch':'epoch2'})
        self.tick()
        self.assertEqual(self.normal_path('pickup1').read_bytes(),original_bytes)
        self.assertFalse(scene_path(self.directory,'acks','run2','epoch2','pickup1').exists())
        command={'run_id':'run2','epoch':'epoch2','operation_id':'pickup1','kind':'insert_specimen',
            'arguments':{'cabinet_id':'hospital-cabinet-c'},'wall_time':time.time()}
        publish_command(self.directory,command); self.tick()
        current=json.loads(scene_path(self.directory,'acks','run2','epoch2','pickup1').read_text())
        self.assertEqual(current['result']['specimen']['location'],'hospital-cabinet-c')
        self.assertEqual(self.normal_path('pickup1').read_bytes(),original_bytes)
        self.assertEqual(len(self.world.state['specimens']),1)
        self.assertNotIn(sid,self.world.state['specimens'])

    def test_existing_ACK_without_processed_world_refuses_new_actuation(self):
        command=self.command('uncertain','insert_specimen',{'cabinet_id':'hospital-cabinet-b'})
        ack={key:command[key] for key in ('run_id','epoch','operation_id')}
        ack.update(status='completed',result={},wall_time=time.time(),sim_time=self.sim)
        atomic_json(self.normal_path('uncertain'),ack)
        before=self.normal_path('uncertain').read_bytes()
        self.tick()
        self.assertEqual(self.world.state['specimens'],{})
        self.assertEqual(self.normal_path('uncertain').read_bytes(),before)
        conflicts=list(scene_path(self.directory,'fault-history','run1','epoch1','probe').parent.glob('command-conflict-*.json'))
        self.assertEqual(json.loads(conflicts[0].read_text())['error'],'scene_ack_without_durable_processed_command')

    def test_conflicting_held_replay_refused_without_replacing_original(self):
        sid=self.specimen(); self.qualify(); command=self.handoff(sid); self.tick()
        self.fault('hold','hold_ack',{'target_operation_id':'pickup1'})
        self.tick(.8); self.tick(.8)
        held=self.normal_path('pickup1','withheld-acks'); original_bytes=held.read_bytes()
        conflict=json.loads(original_bytes); conflict['status']='failed'; conflict['error']='different-result'
        with self.assertRaisesRegex(SceneIOConflict,'withheld_ack_replay_payload_conflict'):
            self.world.faults.write_ack(command,conflict)
        self.world.faults.state['holds']['pickup1']['deadline_wall']=time.time()-1
        with self.assertRaisesRegex(SceneIOConflict,'withheld_ack_replay_payload_conflict'):
            self.world.faults.write_ack(command,conflict)
        self.assertEqual(held.read_bytes(),original_bytes)
        self.assertFalse(self.normal_path('pickup1').exists())
        self.assertEqual(self.world.state['specimens'][sid]['location'],'robot')
        self.assertEqual(len(self.world.state['transfers']),1)


if __name__=='__main__':
    unittest.main()
