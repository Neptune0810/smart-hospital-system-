"""Route-state isolation tests; fake Nav2 data is never real navigation proof."""
import ast
import copy
import hashlib
import json
import math
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .engine_v2 import HospitalV2
from .route_evidence import EVIDENCE_KEY, route_summary, write_route_evidence


ROOT = Path(__file__).resolve().parents[1]


def has_dense_path(value):
    if isinstance(value, dict):
        return 'nav2_path' in value or any(has_dense_path(item) for item in value.values())
    if isinstance(value, list):
        return any(has_dense_path(item) for item in value)
    return False


def production_poses():
    """Use the actual ROS pose-building body without importing ROS on Windows."""
    tree = ast.parse((ROOT / 'mhs/navigation_v2.py').read_text(encoding='utf-8'))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef)
                and item.name == 'V2Navigator')
    method = next(item for item in node.body if isinstance(item, ast.FunctionDef)
                  and item.name == 'poses')

    def pose_stamped():
        return SimpleNamespace(header=SimpleNamespace(frame_id=None, stamp=None),
            pose=SimpleNamespace(position=SimpleNamespace(x=None, y=None),
                                 orientation=SimpleNamespace(z=None, w=None)))

    namespace = {'math': math, 'PoseStamped': pose_stamped}
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 'navigation_v2.py:V2Navigator.poses', 'exec'), namespace)
    return namespace['poses']


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.root = Path(self.temporary.name)
        self.data = self.root / 'runs/smart-ward-v1'
        self.route = {'route_id': 'R-test', 'destination': 'lab', 'validation': 'nav2_verified',
                      'points': [[0., 0.], [1., 1.]], '_cells': [(0, 0), (1, 1)],
                      'nav2_path': [[0.123456789012345, -2.34], [1., 1.]],
                      'length_m': 1.41, 'kind': 'patient_friendly'}

    def tearDown(self):
        if self.root.resolve().parent != (ROOT / '.cache').resolve():
            raise RuntimeError('temporary_test_directory_outside_workspace_cache')
        self.temporary.cleanup()

    def write(self, run_id='run-one'):
        return write_route_evidence(self.root, self.data, run_id, self.route)

    def test_raw_path_preserves_coordinates_and_hash_verifies(self):
        reference = self.write()
        raw = (self.root / reference['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), reference['sha256'])
        self.assertEqual(len(raw), reference['size_bytes'])
        document = json.loads(raw)
        self.assertEqual(document['run_id'], 'run-one')
        self.assertEqual(document['route']['nav2_path'], self.route['nav2_path'])
        self.assertEqual(document['route']['points'], self.route['points'])
        self.assertEqual(reference['nav2_path_points'], 2)
        self.assertEqual(Path(reference['path']).name, reference['sha256'] + '.json')

    def test_same_evidence_is_reused_without_overwrite_or_recursive_reference(self):
        reference = self.write()
        path = self.root / reference['path']
        original_stat = path.stat()
        self.route[EVIDENCE_KEY] = copy.deepcopy(reference)
        with patch.object(Path, 'write_bytes', side_effect=AssertionError('overwrite')):
            self.assertEqual(self.write(), reference)
        self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(len(list(path.parent.glob('*.json'))), 1)
        self.assertNotIn(EVIDENCE_KEY, json.loads(path.read_bytes())['route'])

    def test_tampered_or_partial_existing_evidence_is_rejected_and_not_overwritten(self):
        reference = self.write()
        path = self.root / reference['path']
        path.write_bytes(b'partial-existing-evidence')
        with self.assertRaisesRegex(ValueError, 'route_evidence_hash_mismatch'):
            self.write()
        self.assertEqual(path.read_bytes(), b'partial-existing-evidence')

    def test_different_path_and_run_keep_all_prior_evidence(self):
        first = self.write()
        self.route['nav2_path'][0][0] += .001
        second = self.write()
        other_run = self.write('run-two')
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertNotEqual(second['path'], other_run['path'])
        self.assertNotEqual(second['sha256'], other_run['sha256'])
        for reference in [first, second, other_run]:
            self.assertEqual(hashlib.sha256((self.root / reference['path']).read_bytes()).hexdigest(),
                             reference['sha256'])

    def test_summary_preserves_coarse_semantics_without_cache_alias(self):
        self.route[EVIDENCE_KEY] = self.write()
        summary = route_summary(self.route)
        self.assertFalse(has_dense_path(summary))
        self.assertNotIn('_cells', summary)
        for field in ['points', 'route_id', 'destination', 'validation', 'length_m', 'kind', EVIDENCE_KEY]:
            self.assertEqual(summary[field], self.route[field])
        summary['points'][0][0] = 99
        summary[EVIDENCE_KEY]['sha256'] = 'changed'
        self.assertEqual(self.route['points'][0][0], 0.)
        self.assertNotEqual(self.route[EVIDENCE_KEY]['sha256'], 'changed')


class RouteStateTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.root = Path(self.temporary.name)
        for folder in ['configs', 'maps', 'runs']:
            (self.root / folder).mkdir()
        for relative in ['configs/hospital-v2.json', 'maps/carter_hospital_navigation.png',
                         'maps/carter_hospital_navigation.yaml']:
            shutil.copy2(ROOT / relative, self.root / relative)
        stations = json.loads((ROOT / 'configs/stations.json').read_text(encoding='utf-8'))
        self.engine = HospitalV2(self.root, stations)
        self.engine.backend = SimpleNamespace(ready=lambda: True, zero=lambda: None,
            cancel=lambda: None, validate_route=self.validate, start_route=self.start)
        start = stations['pharmacy']
        (self.root / 'runs/physical-pose.json').write_text(json.dumps(
            {'position': [start['x'], start['y'], 0.], 'wall_time': time.time()}))
        (self.root / 'runs/crowd-v2.json').write_text(json.dumps({'people': [], 'wall_time': time.time()}))
        self.engine.telemetry(start['x'], start['y'], 0., 0., 10.)
        self.engine.telemetry(start['x'], start['y'], 0., 0., 12.)
        self.rows = []
        end = stations['station_a']
        for index, kind in enumerate(['shortest', 'patient_friendly', 'quiet_avoidance']):
            points = [[start['x'] + (end['x'] - start['x']) * j / 9,
                       start['y'] + (end['y'] - start['y']) * j / 9] for j in range(10)]
            route = {'route_id': 'R-unit-' + str(index), 'kind': kind,
                'map_version': self.engine.routes.version, 'destination': 'station_a',
                'points': points, 'length_m': 75.01, 'estimated_navigation_seconds': 151.1,
                'crowd_exposure_person_m': 3.2, 'quiet_zone_m': 2.1,
                'crowd_capture_wall': time.time(), 'created_wall': time.time(),
                'validation': 'occupancy_clearance_only', '_cells': [(j, j) for j in range(10)]}
            self.rows.append(route)
            self.engine.routes.cache[route['route_id']] = route
        self.dense = [[float(i) / 1000, float(i) / 999] for i in range(20000)]
        self.started = []

    def tearDown(self):
        self.engine.db.close()
        if self.root.resolve().parent != (ROOT / '.cache').resolve():
            raise RuntimeError('temporary_test_directory_outside_workspace_cache')
        self.temporary.cleanup()

    def validate(self, route):
        route['nav2_path'] = copy.deepcopy(self.dense)

    def start(self, task_id, route):
        self.started.append((task_id, route))

    def options(self):
        with patch.object(self.engine.routes, 'options', return_value=self.rows):
            return self.engine.compute_route_options(dict(self.engine.pose),
                dict(self.engine.stations['station_a'], id='station_a'), [])

    def test_three_dense_candidates_never_enter_state_event_or_snapshot(self):
        result = self.options()
        self.assertEqual(len(result['routes']), 3)
        state = self.engine.state()
        snapshot = json.loads(self.engine.db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
        for value in [result, self.engine.s, state, snapshot]:
            self.assertFalse(has_dense_path(value))
        for row, summary in zip(self.rows, result['routes']):
            self.assertEqual(row['nav2_path'], self.dense)
            self.assertEqual(row['points'], summary['points'])
            for key in ['route_id', 'kind', 'validation', 'length_m', 'estimated_navigation_seconds',
                        'crowd_exposure_person_m', 'quiet_zone_m', 'destination']:
                self.assertEqual(row[key], summary[key])
            artifact = json.loads((self.root / summary[EVIDENCE_KEY]['path']).read_bytes())
            self.assertEqual(artifact['route']['nav2_path'], self.dense)
        raw_size = sum(reference['size_bytes'] for reference in
                       [row[EVIDENCE_KEY] for row in self.rows])
        self.assertLess(len(json.dumps(state)), raw_size / 20)
        event = next(row for row in state['events'] if row['kind'] == 'route_options_generated')
        self.assertEqual(event['result'], result)

    def test_selected_cache_and_production_nav2_poses_are_unchanged(self):
        result = self.options()
        route = self.rows[1]
        navigator = SimpleNamespace(engine=self.engine,
            get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: 'fixed-stamp')))
        poses = production_poses()
        before = poses(navigator, copy.deepcopy(route))
        self.engine.guard.lease(True)
        task = self.engine.do('navigate_route', {'route_id': route['route_id'], 'operation_id': 'unit-nav'})
        selected = self.engine.routes.selected(route['route_id'], [])
        self.assertIs(selected, route)
        self.assertIs(self.started[0][1], selected)
        self.assertEqual(self.started[0][0], task['nav_task_id'])
        self.assertEqual(selected['nav2_path'], self.dense)
        self.assertEqual(poses(navigator, selected), before)
        self.assertFalse(has_dense_path(self.engine.state()))
        self.assertEqual(self.engine.s['selected_route'], result['routes'][1])
        self.engine.s['selected_route']['points'][0][0] += 1
        self.assertEqual(poses(navigator, selected), before)

    def test_evidence_failure_cannot_leave_verified_selectable_candidate(self):
        with patch('mhs.engine_v2.write_route_evidence', side_effect=OSError('disk-proof-failure')):
            result = self.options()
        self.assertEqual(result['routes'], [])
        self.assertEqual(len(result['rejected']), 3)
        self.assertTrue(all(row['error'] == 'disk-proof-failure' for row in result['rejected']))
        for row in self.rows:
            with self.assertRaisesRegex(ValueError, 'route_not_nav2_verified'):
                self.engine.routes.selected(row['route_id'], [])
        self.assertEqual(self.started, [])

    def test_smart_ward_only_final_submission_is_one_durable_transaction(self):
        from .smart_ward import SmartWardV1
        ward = SmartWardV1(self.root, self.engine.stations)
        try:
            ward.backend = self.engine.backend
            ward.pose = dict(self.engine.pose)
            ward.routes.cache.update({row['route_id']: row for row in self.rows})
            commits = []
            ward.db.set_trace_callback(lambda sql: commits.append(sql) if sql == 'COMMIT' else None)
            validations = []
            evidence_writes = []

            def validate(route):
                self.assertFalse(ward.lock._is_owned())
                self.assertEqual(ward._transaction_depth, 0)
                validations.append(route['route_id'])
                self.validate(route)

            def evidence(*arguments):
                self.assertFalse(ward.lock._is_owned())
                self.assertEqual(ward._transaction_depth, 0)
                evidence_writes.append(arguments[-1]['route_id'])
                return write_route_evidence(*arguments)

            ward.backend.validate_route = validate
            with patch.object(ward.routes, 'options', return_value=self.rows), \
                 patch('mhs.engine_v2.write_route_evidence', side_effect=evidence), \
                 patch.object(ward, '_write_snapshot', wraps=ward._write_snapshot) as snapshot:
                result = ward.compute_route_options(dict(ward.pose),
                    dict(ward.stations['station_a'], id='station_a'), [])
            self.assertEqual(len(result['routes']), 3)
            self.assertEqual(validations, evidence_writes)
            self.assertEqual(len(commits), 1)
            self.assertEqual(snapshot.call_count, 1)
            durable = json.loads(ward.db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
            self.assertEqual(durable['route_options'], result['routes'])
            self.assertFalse(has_dense_path(durable))
            events = [json.loads(row[0]) for row in ward.db.execute(
                'SELECT body FROM events WHERE run_id=?', (ward.s['run_id'],))]
            self.assertEqual(sum(row['kind'] == 'route_options_generated' for row in events), 1)
            self.assertFalse(has_dense_path(events))
        finally:
            ward.db.close()


if __name__ == '__main__':
    unittest.main()
