"""Scoped file/business regressions only; no real robot, model, SDK or Nav."""
import ast
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .scene_io import (SceneIOConflict, default_operation_id, iter_commands,
    publish_ack_bytes, publish_command, read_ack, scene_path, write_ack)
from . import test_smart_ward as fixtures
from .smart_scene import SmartWardWorld


PROJECT = next(parent for parent in Path(__file__).resolve().parents if (parent/'configs/stations.json').is_file())


def command(run='run-one', epoch='epoch-one', operation='explicit-same'):
    return dict(run_id=run, epoch=epoch, operation_id=operation, kind='set_brightness',
        arguments={'room_id':'room301','brightness_percent':60}, wall_time=123.)


def ack(value, **changes):
    result={key:value[key] for key in ('run_id','epoch','operation_id')}
    result.update(status='completed',result={'brightness_percent':60},wall_time=124.,sim_time=1.)
    result.update(changes);return result


class SceneIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=PROJECT/'.cache')
        self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name).resolve()
        self.assertTrue(self.folder.is_relative_to((PROJECT/'.cache').resolve()))

    def test_same_call_identity_is_stable_and_all_scope_dimensions_distinct(self):
        base=('run1','epoch1','hospital-cabinet-b','handoff_order','same-hub-call')
        expected=default_operation_id(*base)
        self.assertEqual(expected,default_operation_id(*base))
        for index,value in enumerate(('run2','epoch2','hospital-cabinet-c','set_brightness','different-call')):
            changed=list(base);changed[index]=value
            self.assertNotEqual(expected,default_operation_id(*changed))

    def test_explicit_id_is_separate_across_runs_and_epochs_and_flat_old_bytes_remain(self):
        legacy=self.folder/'commands';legacy.mkdir()
        old=legacy/'explicit-same.json';old.write_bytes(b'old global command original')
        old_ack_dir=self.folder/'acks';old_ack_dir.mkdir()
        old_ack=old_ack_dir/'explicit-same.json';old_ack.write_bytes(b'old global ack original')
        before=(old.read_bytes(),old_ack.read_bytes())
        values=[command(),command(epoch='epoch-two'),command(run='run-two')]
        paths=[publish_command(self.folder,value) for value in values]
        self.assertEqual(len(set(paths)),3)
        self.assertEqual(iter_commands(self.folder,'run-one','epoch-one'),[paths[0]])
        with self.assertRaises(FileNotFoundError):read_ack(self.folder,values[0])
        for value in values:write_ack(self.folder,value,ack(value))
        self.assertEqual((old.read_bytes(),old_ack.read_bytes()),before)

    def test_replay_preserves_bytes_and_same_scope_command_conflict_is_rejected(self):
        value=command();path=publish_command(self.folder,value);raw=path.read_bytes()
        self.assertEqual(publish_command(self.folder,copy.deepcopy(value)),path)
        changed=copy.deepcopy(value);changed['arguments']['brightness_percent']=40
        with self.assertRaises(SceneIOConflict):publish_command(self.folder,changed)
        self.assertEqual(path.read_bytes(),raw)

    def test_ack_wrong_scope_is_not_a_current_result(self):
        value=command();publish_command(self.folder,value)
        with self.assertRaisesRegex(SceneIOConflict,'scope_mismatch'):
            write_ack(self.folder,value,ack(value,epoch='old-epoch'))
        self.assertFalse(scene_path(self.folder,'acks',value['run_id'],value['epoch'],value['operation_id']).exists())

    def test_original_ack_bytes_and_capture_time_are_preserved_during_release(self):
        value=command();raw=json.dumps(ack(value),indent=1).encode()
        publish_ack_bytes(self.folder,value,raw,channel='withheld-acks')
        minimal={key:value[key] for key in ('run_id','epoch','operation_id')}
        publish_ack_bytes(self.folder,minimal,raw)
        path=scene_path(self.folder,'acks',*minimal.values())
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(read_ack(self.folder,minimal)['wall_time'],124.)
        with self.assertRaises(SceneIOConflict):write_ack(self.folder,value,ack(value,wall_time=999.))
        self.assertEqual(path.read_bytes(),raw)

    def test_invalid_scope_components_never_select_a_path(self):
        for value in ('..','.','../other','a/b','a\\b','',None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                scene_path(self.folder,'acks','run',value,'op')


class BusinessScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SmartWardTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.engine=self.fixture.engine;self.fixture.start()

    def prepared(self):
        order=self.fixture.sample()
        operation=self.engine.device_action('hospital-cabinet-b','handoff_order',
            {'order_id':order['order_id'],'operation_id':'explicit-reused-handoff'})
        return order,operation

    def test_old_flat_ack_is_ignored_and_only_current_ack_plus_fresh_owner_can_commit(self):
        order,operation=self.prepared();folder=self.engine.scene_dir/'acks';folder.mkdir(exist_ok=True)
        old=folder/(operation['operation_id']+'.json')
        original=ack(command(run='old-run',epoch='old-epoch',operation=operation['operation_id']))
        old.write_text(json.dumps(original));raw=old.read_bytes()
        self.engine.tick()
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.fixture.handoff_ack(operation,observation=False)
        self.assertEqual(self.engine.ward_operation(operation['operation_id'])['status'],'unknown')
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.fixture.handoff_ack(operation,observation=True)
        self.assertEqual(self.engine.ward_operation(operation['operation_id'])['status'],'completed')
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        self.assertEqual(old.read_bytes(),raw)

    def test_same_scope_existing_command_conflict_is_durable_unknown_and_motion_locked(self):
        order,operation=self.prepared();current=operation['command']
        path=scene_path(self.engine.scene_dir,'commands',current['run_id'],current['epoch'],current['operation_id'])
        conflict=copy.deepcopy(current);conflict['arguments']['specimen_id']='different-specimen'
        path.write_text(json.dumps(conflict));raw=path.read_bytes()
        self.engine._publish_commands()
        item=self.engine.ward_operation(operation['operation_id'])
        self.assertEqual(item['status'],'unknown');self.assertEqual(item['error'],'scene_command_scope_conflict')
        self.assertTrue(self.engine.s['smart_ward']['recovery_required'])
        self.assertFalse(self.engine.guard.state()['armed'])
        self.assertEqual(path.read_bytes(),raw)
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        stored=json.loads(self.engine.db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
        self.assertTrue(stored['smart_ward']['operations'][operation['operation_id']]['publication_conflict'])

    def test_source_only_original_operation_can_receive_real_stop_before_commit_failed_scene_ack(self):
        # This is an in-process world/business unit test with synthetic physical
        # samples. The ACK is produced by the actual staged scene code, never
        # fabricated as a business ownership shortcut.
        root=self.fixture.root
        (root/'configs/stations.json').write_text(json.dumps(self.fixture.stations))
        station=self.fixture.stations['station_b']
        physical={'x':station['x'],'y':station['y'],'available':True,'stopped_samples':3,
            'speed_mps':0.,'angular_speed_rad_s':0.,'velocity_available':True}
        def sample():
            return dict(physical,wall_time=time.time(),velocity_wall_time=time.time(),
                sim_time=self.engine.s['sim_time'],velocity_sim_time=self.engine.s['sim_time'])
        world=SmartWardWorld(root,physical_reader=sample,config={'sampling_hz':100,'lights':{'normal_percent':60}})
        inserted=self.engine.ward_command('insert_specimen',{'cabinet_id':'hospital-cabinet-b','priority':'normal'},'actual-world-insert')
        world.tick(self.engine.s['sim_time']);self.engine.tick()
        # The business fixture already consumed its synthetic seq1 before this
        # real protocol world was constructed. Obtain the world's next actual
        # fresh sample rather than resetting the consumer's sequence gate.
        time.sleep(.02);world.tick(self.engine.s['sim_time']+.02);self.engine.tick()
        sid=read_ack(self.engine.scene_dir,inserted['command'])['result']['specimen_id']
        incident=next(row for row in self.engine.s['smart_ward']['incidents'].values() if row.get('specimen_id')==sid)
        order=self.engine.create_specimen_delivery(incident['incident_id'],'actual-world-order')['orders'][0]
        operation=self.engine.device_action('hospital-cabinet-b','handoff_order',
            {'order_id':order['order_id'],'operation_id':'legacy-source-only'})
        current=self.engine.s['smart_ward']['operations'][operation['operation_id']]
        current.update(status='unknown',error='scene_ack_identity_invalid')  # Migrated old55 snapshot condition.
        path=scene_path(self.engine.scene_dir,'commands',current['command']['run_id'],current['command']['epoch'],operation['operation_id'])
        path.unlink()  # Reproduce source-only preparation: no current scene command was ever submitted.
        legacy=self.engine.scene_dir/'commands'/(operation['operation_id']+'.json')
        legacy.write_bytes(b'old run command original; must not be reused or overwritten')
        original_legacy=legacy.read_bytes()
        self.engine.save()
        with self.assertRaisesRegex(ValueError,'pending_handoff_must_be_reconciled'):
            self.engine.reset()
        self.engine.stop_run('stop-before-reconcile',self.engine.s['run_id'])
        replay=self.engine.device_action('hospital-cabinet-b','handoff_order',
            {'order_id':order['order_id'],'operation_id':operation['operation_id']})
        self.assertEqual(replay['operation_id'],operation['operation_id'])
        world.tick(self.engine.s['sim_time']+.04);self.engine.tick()
        original=read_ack(self.engine.scene_dir,operation['command'])
        self.assertEqual(original['status'],'failed');self.assertIn('stopped',original['error'])
        self.assertEqual(world.state['specimens'][sid]['location'],'hospital-cabinet-b')
        self.assertFalse(world.state['transfers']);self.assertEqual(self.engine.state()['cargo_count'],0)
        self.assertEqual(self.engine.ward_operation(operation['operation_id'])['status'],'failed')
        self.assertFalse(any(row['status'] in {'pending','unknown'} for row in self.engine.s['smart_ward']['operations'].values()))
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.assertEqual(legacy.read_bytes(),original_legacy)
        old_run=self.engine.s['run_id']
        self.engine.reset()
        self.assertNotEqual(self.engine.s['run_id'],old_run)


class BridgeScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_id_reads_fresh_current_api_scope_instead_of_telemetry_cache(self):
        source=Path(__file__).with_name('bridge.py')
        node=next(row for row in ast.parse(source.read_text()).body if isinstance(row,ast.AsyncFunctionDef) and row.name=='fresh_operation_id')
        response={'run_id':'current-run','smart_ward':{'epoch':'current-epoch','scene_fresh':True}}
        requested=Mock(return_value=response)
        namespace={'asyncio':asyncio,'request':requested,'default_operation_id':default_operation_id,'CallError':RuntimeError}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
        call=SimpleNamespace(id='same-call')
        value=await namespace['fresh_operation_id'](call,'hospital-cabinet-b','handoff_order')
        self.assertEqual(value,default_operation_id('current-run','current-epoch','hospital-cabinet-b','handoff_order','same-call'))
        requested.assert_called_once_with('/api/state')
        response['smart_ward']['scene_fresh']=False
        with self.assertRaises(RuntimeError):await namespace['fresh_operation_id'](call,'hospital-cabinet-b','handoff_order')
