"""Pure AST-loaded launcher checks; never import or run the live manager."""
import ast
import json
import os
from pathlib import Path
import signal
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).with_name('manage.py')
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
SMART = 'smart-ward-v1'
ROOT = Path('/project/hospital-delivery-agent')


def launcher(selected_mode):
    # Evaluate only the actual candidate's configuration assignments. Runtime
    # imports, lifecycle entry points, mkdir, subprocess, and network stay absent.
    namespace = {'ROOT': ROOT, 'SMART_MODE': SMART, 'mode': lambda root: selected_mode,
        'data_dir': lambda root: root/'runs'/selected_mode, 'map_yaml': lambda root: root/'map.yaml',
        'Path': Path, 'os': os, 'json': json, 'signal': signal}
    names = {'HOME', 'MODE', 'DATA_DIR', 'MAP_YAML', 'RUNTIME', 'ENV', 'CLI'}
    selected = []
    for node in TREE.body:
        if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) and target.id in names for target in node.targets):
            selected.append(node)
        elif isinstance(node, ast.If) and any(isinstance(child, ast.Constant) and child.value == 'HOSPITAL_INFER_ADMISSION' for child in ast.walk(node)):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {'process', 'stop_process', 'start_daemon'}:
            selected.append(node)
    with patch.dict(os.environ, {'HOSPITAL_INFER_ADMISSION': 'inherited-value'}, clear=True):
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


def process_fixture(namespace, argv):
    marker = Mock(); marker.exists.return_value = True; marker.read_text.return_value = '123'
    namespace['LOG'] = Mock(); namespace['LOG'].__truediv__ = Mock(return_value=marker)
    # Explicit /proc-only fake. No actual process or environment is inspected.
    proc = Mock(); process = Mock(); proc.__truediv__ = Mock(return_value=process)
    entries = {'stat': Mock(), 'cmdline': Mock(), 'cwd': Mock()}
    entries['stat'].read_text.return_value = '123 (node) S ' + '0 '*30
    entries['cmdline'].read_bytes.return_value = argv.encode().replace(b' ', b'\0')+b'\0'
    entries['cwd'].resolve.return_value = ROOT
    process.joinpath.side_effect = entries.__getitem__
    namespace['Path'] = lambda value: proc if value == '/proc' else Path(value)
    return marker, process, proc


class IndependentRuntimeTests(unittest.TestCase):
    def test_failed127_bundle_is_never_owned_by_new_smart_manager(self):
        configured = launcher(SMART)
        old = ROOT/'agent/agh-runtime-smart-ward127/daemon.mjs'
        process_fixture(configured, f"node {old} --home {configured['HOME']}")
        with self.assertRaisesRegex(RuntimeError, 'process_ownership_mismatch:daemon'):
            configured['process']('daemon')

    def test_smart_bundle_cli_and_flag_are_exact(self):
        configured = launcher(SMART)
        expected = ROOT/'agent/agh-runtime-smart-ward145'
        self.assertEqual(configured['RUNTIME'], expected)
        self.assertEqual(configured['CLI'], ['node', str(expected/'agnes.mjs')])
        self.assertEqual(configured['ENV']['HOSPITAL_INFER_ADMISSION'], 'smart-ward-v1.89')

    def test_failed89_bundle_is_never_owned_by_new_smart_manager(self):
        configured = launcher(SMART)
        old = ROOT/'agent/agh-runtime-smart-ward89/daemon.mjs'
        process_fixture(configured, f"node {old} --home {configured['HOME']}")
        with self.assertRaisesRegex(RuntimeError, 'process_ownership_mismatch:daemon'):
            configured['process']('daemon')

    def test_old_mhs_bundle_and_no_admission_flag_remain(self):
        configured = launcher('mhs-v2')
        self.assertEqual(configured['RUNTIME'], ROOT/'agent/agh-runtime-mhs')
        self.assertEqual(configured['CLI'], ['node', str(ROOT/'agent/agh-runtime-mhs/agnes.mjs')])
        self.assertNotIn('HOSPITAL_INFER_ADMISSION', configured['ENV'])

    def test_smart_web_recognizes_only_its_new_runtime(self):
        configured = launcher(SMART)
        argv = f"node {configured['RUNTIME']/'agnes.mjs'} serve --home {configured['HOME']}"
        process_fixture(configured, argv)
        self.assertEqual(configured['process']('web'), 123)

    def test_smart_daemon_rejects_old_runtime_before_killing(self):
        configured = launcher(SMART)
        argv = f"node {ROOT/'agent/agh-runtime-mhs/daemon.mjs'} --home {configured['HOME']}"
        process_fixture(configured, argv)
        with patch.object(os, 'killpg', create=True) as kill:
            with self.assertRaisesRegex(RuntimeError, 'process_ownership_mismatch:daemon'):
                configured['stop_process']('daemon')
        kill.assert_not_called()

    def test_legacy_manager_rejects_smart_binary(self):
        configured = launcher('mhs-v2')
        argv = f"node {ROOT/'agent/agh-runtime-smart-ward145/daemon.mjs'} --home {configured['HOME']}"
        process_fixture(configured, argv)
        with self.assertRaisesRegex(RuntimeError, 'process_ownership_mismatch:daemon'):
            configured['process']('daemon')

    def test_legacy_manager_accepts_its_original_binary(self):
        configured = launcher('mhs-v2')
        argv = f"node {configured['RUNTIME']/'daemon.mjs'} --home {configured['HOME']}"
        process_fixture(configured, argv)
        self.assertEqual(configured['process']('daemon'), 123)

    def test_daemon_discovery_does_not_adopt_old_bundle(self):
        configured = launcher(SMART)
        old_argv = f"node {ROOT/'agent/agh-runtime-mhs/daemon.mjs'} --home {configured['HOME']}"
        _, q, proc = process_fixture(configured, old_argv)
        discovery = Mock(); discovery.exists.return_value = True
        discovery.read_text.return_value = json.dumps({'ready': True, 'owner': {'pid': 123},
            'web': {'url': 'ws://127.0.0.1:4195'}})
        home = Mock(); home.__truediv__ = Mock(return_value=discovery)
        home.__str__ = Mock(return_value=str(ROOT/'agent/agh-home-mhs'))
        configured['HOME'] = home
        def fake_path(*parts):
            q.exists.return_value = True
            if parts == ('/proc', '123'): return q
            if parts == ('/proc',): return proc
            self.fail('unexpected path probe: '+str(parts))
        configured['Path'] = fake_path
        configured['spawn'] = Mock()
        with self.assertRaisesRegex(RuntimeError, 'daemon_ownership_mismatch'):
            configured['start_daemon']()
        configured['spawn'].assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
