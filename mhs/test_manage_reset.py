"""Unit checks for lifecycle ordering; no SSH, processes, ROS or simulation."""
import ast
import copy
import json
import math
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def lifecycle_namespace():
    # This lifecycle module is Linux-only and imports a deployed legacy manager.
    # Execute its actual reset functions with explicit local test dependencies;
    # importing the full manager must never touch service processes in unit tests.
    source = ast.parse((ROOT / 'mhs/manage.py').read_text(encoding='utf-8'))
    names = {'stop_run_and_owner', 'reset_preflight', 'restore_reset_services', 'reset'}
    selected = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(__name__='mhs.manage_reset_unit', __package__='mhs', ROOT=ROOT, MODE='smart-ward-v1',
                     SMART_MODE='smart-ward-v1', HOME=ROOT/'agent/test-home', CLI=['node', 'test-agnes.mjs'],
                     ENV={'HOSPITAL_MODE': 'smart-ward-v1'}, json=json, math=math, time=time)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT/'mhs/manage.py'), 'exec'), namespace)
    return namespace


class ResetPreflightTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        (self.base/'scene').mkdir()
        self.namespace = lifecycle_namespace()
        self.namespace.update(DATA_DIR=self.base, LOG=self.base)
        self.state = dict(run_id='old-run', stopped_confirmed=True, pose_fresh=True,
                          physical_pose={'position': [1, 2, 0]}, crowd={'fresh': True}, navigation=None,
                          smart_ward={'epoch': 'epoch-old', 'scene_fresh': True, 'operations': {},
                                      'scene_sample': {'run_id': 'old-run', 'epoch': 'epoch-old', 'seq': 8, 'wall_time': 102., 'sim_time': 1.}})
        self.api = Mock(side_effect=lambda *args: copy.deepcopy(self.state))
        self.namespace['api'] = self.api

    def qualify_closed(self):
        q = dict(run_id='old-run', epoch='epoch-old', qualification_id='qualification-old',
                 session_key='qualification:old', active=False, ended_wall=100., ended_scene_seq=7)
        self.state['smart_ward']['fault_qualification'] = q
        restored = {key: q[key] for key in ('run_id', 'epoch', 'qualification_id', 'session_key')}
        restored.update(active=False, paused=None, holds={})
        self.receipt = self.base/'scene/fault-state.json'
        self.receipt.write_text(json.dumps(restored), encoding='utf-8')
        return q, restored

    def test_read_only_preflight_rejects_unknown_handoff_and_navigation(self):
        for kind in ('handoff', 'navigation'):
            with self.subTest(kind=kind):
                self.state['navigation'] = {'status': 'unknown'} if kind == 'navigation' else None
                self.state['smart_ward']['operations'] = {'pending': {'status': 'unknown', 'command': {'kind': 'handoff'}}} if kind == 'handoff' else {}
                before = copy.deepcopy(self.state)
                with self.assertRaisesRegex(RuntimeError, 'reconcil'):
                    self.namespace['reset_preflight']('old-run')
                self.assertEqual(self.state, before)
        self.assertTrue(all(call.args == () for call in self.api.call_args_list))

    def test_fault_reset_requires_exact_restoration_new_sample_and_fresh_stop(self):
        q, restored = self.qualify_closed()
        mutations = [('active', True), ('paused', {'was_playing': True}), ('holds', {'old-ack': {}}), ('epoch', 'wrong-epoch')]
        for key, value in mutations:
            with self.subTest(key=key):
                self.receipt.write_text(json.dumps(dict(restored, **{key: value})), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'restoration_not_confirmed'):
                    self.namespace['reset_preflight']('old-run')
        self.receipt.write_text(json.dumps(restored), encoding='utf-8')
        q['active'] = True
        with self.assertRaisesRegex(RuntimeError, 'must_end_before_reset'):
            self.namespace['reset_preflight']('old-run')
        q['active'] = False
        for key, value in [('seq', 7), ('wall_time', 100.), ('run_id', 'another-run'), ('epoch', 'another-epoch')]:
            original = self.state['smart_ward']['scene_sample'][key]
            self.state['smart_ward']['scene_sample'][key] = value
            with self.subTest(sample=key), self.assertRaisesRegex(RuntimeError, 'restoration_not_confirmed'):
                self.namespace['reset_preflight']('old-run')
            self.state['smart_ward']['scene_sample'][key] = original
        self.state['pose_fresh'] = False
        with self.assertRaisesRegex(RuntimeError, 'restoration_not_confirmed'):
            self.namespace['reset_preflight']('old-run')
        self.state['pose_fresh'] = True
        self.assertEqual(self.namespace['reset_preflight']('old-run'), self.state)
        self.assertTrue(all(call.args == () for call in self.api.call_args_list))

    def test_reset_preflight_consumes_real_engine_public_sample_after_fault_end(self):
        from .test_smart_ward import SmartWardTests
        fixture = SmartWardTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        q = fixture.fault_begin()
        fixture.engine.end_fault_qualification(q['qualification_id'], q['session_key'], q['run_id'], q['epoch'], 'contract-end')
        fixture.place('station_b')
        fixture.observe()
        public = fixture.engine.state()
        qualification = public['smart_ward']['fault_qualification']
        restored = {key: qualification[key] for key in ('run_id', 'epoch', 'qualification_id', 'session_key')}
        restored.update(active=False, paused=None, holds={})
        (self.base/'scene/fault-state.json').write_text(json.dumps(restored), encoding='utf-8')
        self.assertNotIn('scene', public['smart_ward'])
        self.assertGreater(public['smart_ward']['scene_sample']['seq'], qualification['ended_scene_seq'])
        self.assertGreater(public['smart_ward']['scene_sample']['wall_time'], qualification['ended_wall'])
        self.api.side_effect = lambda *args: copy.deepcopy(public)
        result = self.namespace['reset_preflight'](public['run_id'])
        self.assertEqual(result['smart_ward']['scene_sample'], public['smart_ward']['scene_sample'])
        self.assertTrue(all(call.args == () for call in self.api.call_args_list))

    def install_lifecycle_mocks(self):
        names = ['phase', 'stop_run_and_owner', 'stop_process', 'process', 'spawn', 'stop', 'start']
        for name in names:
            self.namespace[name] = Mock()
        self.namespace['stop_run_and_owner'].return_value = dict(run_id='old-run')
        self.namespace['process'].return_value = 123
        revoke = Mock()
        scope = types.ModuleType('mhs.session_scope')
        scope.revoke_previous = revoke
        return revoke, scope

    def test_preflight_refusal_keeps_services_and_session_binding(self):
        revoke, scope = self.install_lifecycle_mocks()
        self.namespace['reset_preflight'] = Mock(side_effect=RuntimeError('handoff_unknown'))
        with patch.dict('sys.modules', {'mhs.session_scope': scope}), self.assertRaisesRegex(RuntimeError, 'handoff_unknown'):
            self.namespace['reset']()
        self.namespace['stop_process'].assert_not_called()
        self.namespace['stop_run_and_owner'].assert_not_called()
        self.namespace['spawn'].assert_not_called()
        revoke.assert_not_called()
        self.assertTrue(all(call.args == () for call in self.api.call_args_list))

    def test_initial_preflight_allows_active_navigation_only_until_stop_phase(self):
        self.state.update(stopped_confirmed=False,navigation={'status':'running'})
        self.assertEqual(self.namespace['reset_preflight']('old-run',require_stopped=False),self.state)
        with self.assertRaisesRegex(RuntimeError,'stop_not_confirmed'):
            self.namespace['reset_preflight']('old-run')
        self.state['navigation']['status']='unknown'
        with self.assertRaisesRegex(RuntimeError,'reconciliation'):
            self.namespace['reset_preflight']('old-run',require_stopped=False)

    def test_changed_run_refuses_owner_stop_before_any_mutation(self):
        self.state['run_id']='different-run'
        with self.assertRaisesRegex(RuntimeError,'session_run_mismatch'):
            self.namespace['stop_run_and_owner']('scene_reset',expected_run_id='old-run')
        self.assertTrue(all(call.args==() for call in self.api.call_args_list))

    def test_backend_race_refusal_restores_only_old_services_without_revoke(self):
        revoke, scope = self.install_lifecycle_mocks()
        self.namespace['reset_preflight'] = Mock(return_value=self.state)
        def response(path=None, arguments=None):
            if path == '/api/admin/reset':
                raise RuntimeError('pending_handoff_must_be_reconciled_before_reset')
            return copy.deepcopy(self.state)
        self.api.side_effect = response
        with patch.dict('sys.modules', {'mhs.session_scope': scope}), self.assertRaisesRegex(RuntimeError, 'pending_handoff'):
            self.namespace['reset']()
        self.assertEqual([call.args[0] for call in self.namespace['stop_process'].call_args_list], ['bridge', 'web'])
        self.assertEqual([call.args[0] for call in self.namespace['spawn'].call_args_list], ['web', 'bridge'])
        revoke.assert_not_called()
        evidence = list(self.base.glob('reset-recovery-*.json'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0].read_text())['errors'], [])

    def test_unknown_reset_outcome_never_restores_another_run(self):
        self.namespace['spawn'] = Mock()
        self.state['run_id'] = 'different-run'
        result = self.namespace['restore_reset_services']('old-run', {'web': True, 'bridge': True})
        self.namespace['spawn'].assert_not_called()
        self.assertIn('reset_outcome_unknown_or_run_changed', result['errors'][0])

    def test_success_revokes_only_after_backend_reset_confirmation(self):
        revoke, scope = self.install_lifecycle_mocks()
        self.namespace['reset_preflight'] = Mock(return_value=self.state)
        sequence = []
        def response(path=None, arguments=None):
            if path == '/api/admin/reset':
                sequence.append('backend_reset')
                return {'run_id': 'new-run'}
            return copy.deepcopy(self.state)
        self.api.side_effect = response
        revoke.side_effect = lambda: sequence.append('revoke')
        with patch.dict('sys.modules', {'mhs.session_scope': scope}):
            self.namespace['reset']()
        self.assertEqual(sequence, ['backend_reset', 'revoke'])
        self.namespace['stop'].assert_called_once_with(keep_workbench=True)
        self.namespace['start'].assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
