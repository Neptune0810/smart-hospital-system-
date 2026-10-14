"""Business/protocol tests with a fake navigation backend, never simulation proof."""
import copy
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from .smart_ward import SmartWardV1, NAVIGATION_QUALIFICATION_VERSION
from .scene_io import scene_path


ROOT = Path(__file__).resolve().parents[1]


class Backend:
    def ready(self): return True
    def zero(self): pass
    def cancel(self): pass
    def validate_route(self, route): pass
    def start_route(self, task_id, route): pass


class SmartWardTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.root = Path(self.temporary.name)
        for folder in ['configs', 'maps', 'runs']:
            (self.root / folder).mkdir()
        for item in ['configs/hospital-v2.json', 'maps/carter_hospital_navigation.png', 'maps/carter_hospital_navigation.yaml']:
            shutil.copy2(ROOT / item, self.root / item)
        self.stations = json.loads((ROOT / 'configs/stations.json').read_text(encoding='utf-8'))
        self.engine = SmartWardV1(self.root, self.stations, require_sim_truth=True)
        self.engine.backend = Backend()
        self.engine.guard.lease(True)
        self.seq = 0
        self.devices = {
            'hospital-cabinet-b': dict(kind='cabinet', station_id='station_b', available=True, specimens=[], capacity=2),
            'hospital-cabinet-c': dict(kind='cabinet', station_id='station_c', available=True, specimens=[], capacity=2),
            'hospital-cabinet-lab': dict(kind='cabinet', station_id='lab', available=True, specimens=[], capacity=20),
            'hospital-light-301': dict(kind='light', room_id='room301', available=True, brightness_percent=70),
            'hospital-light-303': dict(kind='light', room_id='room303', available=True, brightness_percent=70),
            'hospital-sensor-301': dict(kind='sensor', room_id='room301', available=True, relative_illumination=76),
            'hospital-sensor-303': dict(kind='sensor', room_id='room303', available=True, relative_illumination=76)}
        self.specimens = {}
        self.transfers = {}
        self.place('station_b')
        self.observe()

    def tearDown(self):
        self.engine.db.close()
        if self.root.resolve().parent != (ROOT / '.cache').resolve():
            raise RuntimeError('temporary_test_directory_outside_workspace_cache')
        self.temporary.cleanup()

    def place(self, station):
        point = self.stations[station]
        (self.root / 'runs/physical-pose.json').write_text(json.dumps(dict(position=[point['x'], point['y'], 0], wall_time=time.time())))
        (self.root / 'runs/crowd-v2.json').write_text(json.dumps(dict(people=[], wall_time=time.time())))
        start = self.engine.s['sim_time'] + 2
        self.engine.telemetry(point['x'], point['y'], 0, 0, start)
        self.engine.telemetry(point['x'], point['y'], 0, 0, start + 2)

    def observe(self, age=0):
        self.seq += 1
        observation = dict(run_id=self.engine.s['run_id'], epoch=self.engine.s['smart_ward']['epoch'], seq=self.seq,
                           wall_time=time.time() - age, sim_time=self.engine.s['sim_time'],
                           devices=self.devices, specimens=self.specimens, transfers=self.transfers)
        (self.engine.scene_dir / 'observation.json').write_text(json.dumps(observation))
        self.engine.tick()

    def start(self):
        return self.engine.start_watch('session-current', self.engine.s['run_id'], 'start-watch')

    def consume_notifications(self):
        """Synthetic gateway delivery + trusted consumed Core inbox, tests only.

        No model, scene action, navigation or physical ACK is manufactured.
        Successful-finalize fixtures must now supply this missing input state.
        """
        database = self.root / 'agent/agh-home-mhs/data/sessions.db'
        database.parent.mkdir(parents=True, exist_ok=True)
        session = self.engine.s['controller_session']
        with closing(sqlite3.connect(database)) as core, core:
            core.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER,session_key TEXT,lane BLOB,type TEXT,origin TEXT,trust TEXT,data TEXT)')
            seq = core.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events').fetchone()[0]
            core.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                (seq, session, b'main', 'inbox', 'system', 'trusted', json.dumps({'items': []})))
        ward = self.engine.s['smart_ward']
        current = {key for key, item in ward['incidents'].items() if item.get('episode') == ward['episode']}
        with self.engine._atomic():
            for notification_id, raw in self.engine.db.execute('SELECT id,body FROM ward_notifications WHERE run_id=?', (self.engine.s['run_id'],)).fetchall():
                body = json.loads(raw)
                if body.get('session_id') != session or not current.intersection(body.get('event_ids', [])):
                    continue
                worker = body.get('worker_id') or 'fixture-gateway'
                body.update(status='delivered', worker_id=worker, lease_until=0,
                    details=dict(worker_id=worker, expected_run_id=self.engine.s['run_id'],
                                 stage='sdk_accepted', queue_seq=seq, synthetic_test_fixture=True))
                self.engine.db.execute('UPDATE ward_notifications SET body=? WHERE id=?', (json.dumps(body), notification_id))

    def test_nav_result_commits_callback_state_and_event_once(self):
        engine = self.engine
        engine.nav = dict(nav_task_id='nav-one', goal_station='station_b', status='pending')
        engine.s.setdefault('navigation_tasks', {})['nav-one'] = dict(engine.nav)
        commits = []
        engine.db.set_trace_callback(lambda sql: commits.append(sql) if sql == 'COMMIT' else None)
        with patch.object(engine, '_write_snapshot', wraps=engine._write_snapshot) as snapshot:
            engine.nav_result('nav-one', 'running')
        self.assertEqual(len(commits), 1)
        self.assertEqual(snapshot.call_count, 1)
        durable = json.loads(engine.db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
        self.assertEqual(durable['navigation_tasks']['nav-one']['status'], 'running')
        events = [json.loads(row[0]) for row in engine.db.execute(
            'SELECT body FROM events WHERE run_id=?', (engine.s['run_id'],))]
        self.assertEqual(sum(e['kind'] == 'navigation_running' for e in events), 1)

    def test_nav_result_database_failure_rolls_back_status_and_event(self):
        engine = self.engine
        engine.nav = dict(nav_task_id='nav-one', goal_station='station_b', status='pending')
        engine.s.setdefault('navigation_tasks', {})['nav-one'] = dict(engine.nav)
        engine.save()
        engine.db.execute("CREATE TRIGGER reject_nav_snapshot BEFORE INSERT ON snapshots BEGIN SELECT RAISE(ABORT, 'test durable failure'); END")
        engine.db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            engine.nav_result('nav-one', 'running')
        self.assertEqual(engine.nav['status'], 'pending')
        self.assertEqual(engine.s['navigation_tasks']['nav-one']['status'], 'pending')
        self.assertFalse(any(json.loads(row[0])['kind'] == 'navigation_running' for row in engine.db.execute(
            'SELECT body FROM events WHERE run_id=?', (engine.s['run_id'],))))

    def sample(self, sid='S-sample', cabinet='hospital-cabinet-b'):
        self.specimens[sid] = dict(specimen_id=sid, location=cabinet, priority='normal', sealed=True,
                                  created_sim=self.engine.s['sim_time'], deadline_sim=self.engine.s['sim_time'] + 900)
        self.devices[cabinet]['specimens'].append(sid)
        self.observe()
        incident = next(i for i in self.engine.state()['smart_ward']['incidents'] if i.get('specimen_id') == sid)
        return self.engine.create_specimen_delivery(incident['incident_id'], 'create-' + sid)['orders'][0]

    def low_light(self):
        self.devices['hospital-sensor-301']['relative_illumination'] = 28
        for _ in range(3):
            self.observe()
        return next(i for i in self.engine.state()['smart_ward']['incidents'] if i['kind'] == 'low_light')

    def handoff_ack(self, operation, wrong_identity=False, observation=True):
        args = operation['command']['arguments']
        location = 'robot' if args['direction'] == 'pickup' else args['cabinet_id']
        result = {k: args[k] for k in ['transfer_id', 'order_id', 'specimen_id', 'direction', 'cabinet_id']}
        result['location'] = location
        ack = dict(run_id=self.engine.s['run_id'], epoch=self.engine.s['smart_ward']['epoch'],
                   operation_id=operation['operation_id'], status='completed', result=result,
                   wall_time=time.time(), sim_time=self.engine.s['sim_time'])
        if wrong_identity:
            ack['result']['specimen_id'] = 'S-wrong'
        folder = scene_path(self.engine.scene_dir, 'acks', self.engine.s['run_id'], self.engine.s['smart_ward']['epoch'], 'fixture-probe').parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (operation['operation_id'] + '.json')).write_text(json.dumps(ack))
        if observation:
            self.specimens[args['specimen_id']]['location'] = location
            self.transfers[args['transfer_id']] = dict(specimen_id=args['specimen_id'], direction=args['direction'],
                                                     location=location, operation_id=operation['operation_id'])
            self.observe()
        else:
            self.engine.tick()

    def lamp_ack(self, operation):
        ack = dict(run_id=self.engine.s['run_id'], epoch=self.engine.s['smart_ward']['epoch'],
                   operation_id=operation['operation_id'], status='completed', result={'brightness_percent': 70},
                   wall_time=time.time(), sim_time=self.engine.s['sim_time'])
        folder = scene_path(self.engine.scene_dir, 'acks', self.engine.s['run_id'], self.engine.s['smart_ward']['epoch'], 'fixture-probe').parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (operation['operation_id'] + '.json')).write_text(json.dumps(ack))
        self.engine.tick()

    def test_independent_store_and_explicit_watch_start(self):
        self.specimens['S-before-watch'] = dict(specimen_id='S-before-watch', location='hospital-cabinet-b', sealed=True,
                                              priority='normal', deadline_sim=900)
        self.observe()
        self.assertEqual(self.engine.verify_services()['total'], 0)
        self.start()
        self.assertEqual(self.engine.verify_services()['total'], 1)
        self.assertFalse((self.root / 'runs/mhs-v2').exists())
        self.assertEqual(self.engine.state()['protocol'], 'smart-ward-v1')
        self.assertEqual(len(self.engine.state()['smart_ward']['devices']), 8)

    def test_light_three_fresh_samples_and_no_duplicate_incident(self):
        self.start()
        self.devices['hospital-sensor-301']['relative_illumination'] = 28
        for _ in range(2):
            self.observe()
        self.assertEqual(self.engine.verify_services()['total'], 0)
        self.observe()
        for _ in range(5):
            self.observe()
        self.assertEqual(self.engine.verify_services()['total'], 1)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM ward_notifications').fetchone()[0], 1)

    def test_stale_sensor_is_not_low_light_evidence(self):
        self.start()
        self.devices['hospital-sensor-301']['relative_illumination'] = 1
        for _ in range(4):
            self.observe(age=20)
        self.assertEqual(self.engine.verify_services()['total'], 0)
        self.engine.s['smart_ward']['scene']['wall_time'] = 0
        self.assertFalse(self.engine.state()['smart_ward']['devices']['hospital-sensor-301']['available'])

    def test_duplicate_notifications_cannot_create_second_specimen_order(self):
        self.start()
        order = self.sample()
        incident = self.engine.state()['smart_ward']['incidents'][0]
        repeated = self.engine.create_specimen_delivery(incident['incident_id'], 'another-model-call')
        self.assertEqual(repeated['orders'][0]['order_id'], order['order_id'])
        self.assertEqual(len(self.engine.s['orders']), 1)
        with self.assertRaisesRegex(ValueError, 'operation_id_conflict'):
            self.engine.create_specimen_delivery('different-incident', 'another-model-call')

    def test_incident_and_outbox_roll_back_together(self):
        self.start()
        self.devices['hospital-sensor-301']['relative_illumination'] = 28
        for _ in range(2):
            self.observe()
        with patch.object(self.engine, '_notify', side_effect=RuntimeError('injected outbox failure')):
            with self.assertRaisesRegex(RuntimeError, 'outbox failure'):
                self.observe()
        self.assertEqual(self.engine.verify_services()['total'], 0)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM ward_notifications').fetchone()[0], 0)
        self.observe()
        self.assertEqual(self.engine.verify_services()['total'], 1)

    def test_notification_owner_lease_and_delivered_to_retry(self):
        self.start()
        self.low_light()
        row = self.engine.claim_notification('worker-one')
        self.assertIsNone(self.engine.claim_notification('worker-two'))
        details = dict(worker_id='worker-one', expected_run_id=self.engine.s['run_id'])
        with self.assertRaisesRegex(ValueError, 'worker'):
            self.engine.ack_notification(row['notification_id'], 'delivered', dict(details, worker_id='worker-two'))
        self.engine.ack_notification(row['notification_id'], 'delivered', details)
        self.engine.ack_notification(row['notification_id'], 'retry', details)
        self.assertEqual(self.engine.claim_notification('worker-one')['notification_id'], row['notification_id'])
        with self.assertRaisesRegex(ValueError, 'run_mismatch'):
            self.engine.ack_notification(row['notification_id'], 'delivered', dict(details, expected_run_id='old-run'))

    def test_handoff_requires_ack_and_new_scene_then_commits_once(self):
        self.start()
        order = self.sample()
        operation = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='pick'))
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        with self.assertRaisesRegex(ValueError, 'pending_handoff'):
            self.engine.mutate('navigate_route', dict(route_id='unused', operation_id='move'))
        self.handoff_ack(operation, observation=False)
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.handoff_ack(operation)
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        repeated = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='pick-new-model-call'))
        self.assertEqual(repeated['operation_id'], 'pick')
        self.assertEqual(repeated['status'], 'completed')
        self.place('lab')
        self.observe()
        receive = self.engine.device_action('hospital-cabinet-lab', 'handoff_order', dict(order_id=order['order_id'], operation_id='receive'))
        self.handoff_ack(receive)
        report = self.engine.verify_services()
        self.assertTrue(report['all_resolved'])
        self.assertEqual(report['specimen_delivered'], 1)
        self.assertEqual(len(report['receipts']), 1)
        self.engine.tick()
        self.assertEqual(len(self.engine.verify_services()['receipts']), 1)

    def test_ack_mismatch_never_changes_cargo_and_prevents_reset(self):
        self.start()
        order = self.sample()
        operation = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='pick'))
        self.handoff_ack(operation, wrong_identity=True)
        self.assertEqual(self.engine.ward_operation('pick')['status'], 'unknown')
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        with self.assertRaisesRegex(ValueError, 'pending_handoff'):
            self.engine.reset()

    def test_wrong_cabinet_wrong_location_and_old_business_entry_are_refused(self):
        self.start()
        order = self.sample()
        with self.assertRaisesRegex(ValueError, 'station_mismatch'):
            self.engine.device_action('hospital-cabinet-c', 'handoff_order', dict(order_id=order['order_id'], operation_id='wrong-cab'))
        self.place('station_a')
        with self.assertRaisesRegex(ValueError, 'stationary_arrival'):
            self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='wrong-place'))
        with self.assertRaisesRegex(ValueError, 'MHS_cabinet_ack'):
            self.engine.do('pickup_package', dict(order_id=order['order_id'], operation_id='bypass'))

    def test_light_command_ack_does_not_mean_environment_recovered(self):
        self.start()
        self.low_light()
        operation = self.engine.device_action('hospital-light-301', 'set_brightness', dict(brightness_percent=70, operation_id='light-one'))
        self.lamp_ack(operation)
        self.assertFalse(self.engine.verify_services()['all_resolved'])
        self.devices['hospital-sensor-301']['relative_illumination'] = 76
        for _ in range(2):
            self.observe()
        self.assertFalse(self.engine.verify_services()['all_resolved'])
        self.observe()
        self.assertTrue(self.engine.verify_services()['all_resolved'])
        self.assertEqual(self.engine.state()['smart_ward']['watch']['phase'], 'watching')

    def test_two_light_attempts_then_open_maintenance_ticket(self):
        self.start()
        self.low_light()
        for attempt in range(2):
            operation = self.engine.device_action('hospital-light-301', 'set_brightness', dict(brightness_percent=70, operation_id='light-' + str(attempt)))
            self.lamp_ack(operation)
            self.engine.s['sim_time'] += 31
            self.observe()
        report = self.engine.verify_services()
        self.assertEqual(report['resolved'], 0)
        self.assertEqual(len(report['unresolved']), 1)
        self.assertEqual(len(report['maintenance_tickets']), 1)
        self.assertEqual(report['maintenance_tickets'][0]['status'], 'open')
        self.assertFalse(report['all_resolved'])

    def test_model_budget_counts_actual_unique_requests_not_steps_or_idle(self):
        self.start()
        run = self.engine.s['run_id']
        self.engine.record_model_request('idle-request', run, 'session-current')
        self.assertIsNone(self.engine.s['mhs_budget'])
        self.low_light()
        self.engine.decision('before-step', run, 'session-current')
        self.assertEqual(self.engine.s['mhs_budget']['decisions'], 0)
        self.engine.record_model_request('provider-request', run, 'session-current')
        self.engine.record_model_request('provider-request', run, 'session-current')
        self.assertEqual(self.engine.s['mhs_budget']['decisions'], 1)
        self.engine.s['mhs_budget']['decisions'] = 60
        self.engine.tick()
        self.assertEqual(self.engine.s['mhs_budget']['stopped_reason'], 'decision_limit')
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])

    def test_next_light_event_reopens_episode_without_new_session(self):
        self.start()
        self.low_light()
        self.devices['hospital-sensor-301']['relative_illumination'] = 76
        for _ in range(3):
            self.observe()
        self.assertTrue(self.engine.s['mhs_finished'])
        self.engine.verify_services()
        self.consume_notifications()
        self.engine.finalize_episode(self.engine.s['run_id'], 'session-current')
        self.assertEqual(self.engine.s['smart_ward']['episode'], 1)
        self.low_light()
        self.assertFalse(self.engine.s['mhs_finished'])
        self.assertEqual(self.engine.s['smart_ward']['episode'], 2)
        self.assertEqual(self.engine.s['controller_session'], 'session-current')

    def test_stop_watch_prevents_new_incidents_and_reactivation(self):
        self.start()
        self.engine.stop_run('stop', self.engine.s['run_id'])
        self.low_light_without_lookup()
        self.assertEqual(self.engine.verify_services()['total'], 0)
        self.assertIsNone(self.engine.claim_notification('worker'))
        with self.assertRaisesRegex(ValueError, 'recovery_or_new_run'):
            self.engine.start_watch('session-current', self.engine.s['run_id'], 'restart')

    def low_light_without_lookup(self):
        self.devices['hospital-sensor-301']['relative_illumination'] = 1
        for _ in range(3):
            self.observe()

    def test_pending_transfer_survives_restart_and_ack_commits_once(self):
        self.start()
        order = self.sample()
        operation = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='persistent-pick'))
        epoch = self.engine.s['smart_ward']['epoch']
        self.engine.db.close()
        self.engine = SmartWardV1(self.root, self.stations, require_sim_truth=True)
        self.engine.backend = Backend()
        self.engine.guard.lease(True)
        self.place('station_b')
        self.assertEqual(self.engine.s['smart_ward']['epoch'], epoch)
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        self.assertEqual(self.engine.s['smart_ward']['watch']['phase'], 'paused')
        self.assertIsNone(self.engine.claim_notification('worker-after-restart'))
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.handoff_ack(operation)
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        self.engine.tick()
        events = [e for e in self.engine.state()['events'] if e['kind'] == 'package_picked_up']
        self.assertEqual(len(events), 1)

    def test_service_plan_status_is_derived_and_partial_plan_rejected(self):
        self.start()
        incident = self.low_light()
        version = self.engine.s['planning_version']
        with self.assertRaisesRegex(ValueError, 'missing_active_incidents'):
            self.engine.publish_service_plan(version, [dict(action='standby')], '待命', 'bad-plan')
        self.engine.publish_service_plan(version, [dict(incident_id=incident['incident_id'], action='set_brightness', device_id='hospital-light-301', status='completed')], '先恢复301照明', 'plan')
        self.assertEqual(self.engine.state()['smart_ward']['service_plan']['steps'][0]['status'], 'planned')

    def test_service_transport_plan_reuses_capacity_and_dependency_validation(self):
        self.start()
        orders = [self.sample('S-' + str(index)) for index in range(3)]
        steps = [dict(incident_id=o['incident_id'], action='pickup') for o in orders]
        steps += [dict(incident_id=o['incident_id'], action='deliver') for o in orders]
        with self.assertRaisesRegex(ValueError, 'exceeds_actual_capacity'):
            self.engine.publish_service_plan(self.engine.s['planning_version'], steps, '先取三份再送检', 'over-capacity-plan')
        self.assertIsNone(self.engine.s.get('delivery_plan'))
        steps = []
        for order in orders:
            steps.extend([dict(incident_id=order['incident_id'], action='pickup'), dict(incident_id=order['incident_id'], action='deliver')])
        self.engine.publish_service_plan(self.engine.s['planning_version'], steps, '按容量分批送检', 'valid-plan')
        self.assertEqual(self.engine.s['delivery_plan']['steps'][-1]['station_id'], 'lab')
        self.assertEqual(self.engine.s['delivery_plan']['planning_version'], self.engine.s['planning_version'])

    def test_model_failure_stop_saves_reason_and_scene_stop_request(self):
        self.start()
        self.low_light()
        self.engine.stop_run('failed-model', self.engine.s['run_id'], reason='notification_model_error')
        report = self.engine.verify_services()
        self.assertEqual(report['unresolved'][0]['reason'], 'notification_model_error')
        control = json.loads((self.engine.scene_dir / 'control.json').read_text())
        self.assertFalse(control['enabled'])
        self.assertTrue(control['stop_requested'])
        self.assertIn('verification_key', report)

    def test_explicit_failed_transfer_can_retry_same_transfer_with_new_operation(self):
        self.start()
        order = self.sample()
        first = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='first-pick'))
        ack = dict(run_id=self.engine.s['run_id'], epoch=self.engine.s['smart_ward']['epoch'], operation_id='first-pick',
                   status='failed', error='temporary_interlock', result={}, wall_time=time.time(), sim_time=self.engine.s['sim_time'])
        folder = scene_path(self.engine.scene_dir, 'acks', self.engine.s['run_id'], self.engine.s['smart_ward']['epoch'], 'fixture-probe').parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'first-pick.json').write_text(json.dumps(ack))
        self.engine.tick()
        second = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='second-pick'))
        self.assertEqual(first['transfer_id'], second['transfer_id'])
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        self.handoff_ack(second)
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])

    def test_final_verification_request_is_included_and_key_stays_stable(self):
        self.start()
        self.low_light()
        self.devices['hospital-sensor-301']['relative_illumination'] = 76
        for _ in range(3):
            self.observe()
        self.assertNotIn('end_wall', self.engine.s['mhs_budget'])
        self.engine.verify()  # Read-only HTTP report must not freeze the model ledger.
        self.assertNotIn('end_wall', self.engine.s['mhs_budget'])
        self.engine.record_model_request('final-verification-model', self.engine.s['run_id'], 'session-current')
        report = self.engine.verify_services()
        self.assertEqual(report['model_calls'], 1)
        self.assertIsNone(report['end_wall'])
        self.assertTrue(report['ready_to_finalize'])
        version = report['verification_event_version']
        self.engine.record_model_request('final-chinese-response-model', self.engine.s['run_id'], 'session-current')
        notification = self.engine.claim_notification('worker')
        self.engine.ack_notification(notification['notification_id'], 'delivered', dict(worker_id='worker', expected_run_id=self.engine.s['run_id']))
        self.consume_notifications()
        final = self.engine.finalize_episode(self.engine.s['run_id'], 'session-current')
        self.assertEqual(final['model_calls'], 2)
        self.assertIsNotNone(final['end_wall'])
        self.engine.ack_notification(notification['notification_id'], 'delivered', dict(worker_id='worker', expected_run_id=self.engine.s['run_id']))
        self.assertEqual(self.engine.state()['smart_ward']['verification_event_version'], version)

    def test_real_scene_protocol_core_integrates_with_business_transfer_commit(self):
        from .smart_scene import SmartWardWorld
        self.start()
        (self.root / 'configs/stations.json').write_text(json.dumps(self.stations), encoding='utf-8')
        world = SmartWardWorld(self.root, physical_reader=lambda: dict(x=self.engine.pose['x'], y=self.engine.pose['y'],
                                available=True, stopped_samples=3, wall_time=time.time()),
                               config={'sampling_hz': 1, 'lights': {'normal_percent': 60}})
        world.tick(self.engine.s['sim_time'])
        self.engine._seen_scene_seq = None
        self.engine.tick()

        def step(delta=.1):
            self.engine.s['sim_time'] += delta
            world.last_publish = 0  # Advance discrete test samples without a wall-clock sleep.
            world.tick(self.engine.s['sim_time'])
            self.engine.tick()

        self.engine.ward_command('insert_specimen', dict(cabinet_id='hospital-cabinet-b', priority='normal'), 'world-insert')
        step()
        incident = self.engine.state()['smart_ward']['incidents'][0]
        order = self.engine.create_specimen_delivery(incident['incident_id'], 'world-create')['orders'][0]
        self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='world-pick'))
        step(); step(.8)
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        step(.8)
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        self.place('lab')
        step()
        self.engine.device_action('hospital-cabinet-lab', 'handoff_order', dict(order_id=order['order_id'], operation_id='world-receive'))
        step(); step(.8); step(.8)
        self.assertEqual(world.state['specimens'][order['specimen_id']]['location'], 'hospital-cabinet-lab')
        self.assertTrue(self.engine.verify_services()['all_resolved'])

    def test_backend_restart_pauses_until_explicit_healthy_resume(self):
        self.start()
        self.low_light()
        self.engine.db.close()
        self.engine = SmartWardV1(self.root, self.stations, require_sim_truth=True)
        self.engine.backend = Backend()
        self.engine.guard.lease(True)
        self.place('station_b')
        self.observe()
        self.assertFalse(self.engine.state()['smart_ward']['watch']['enabled'])
        self.assertEqual(self.engine.state()['smart_ward']['watch']['phase'], 'paused')
        self.assertIsNone(self.engine.claim_notification('new-worker'))
        start_wall = self.engine.s['mhs_budget']['start_wall']
        start_sim = self.engine.s['mhs_budget']['start_sim']
        self.engine.s['sim_time'] += 300
        self.engine.tick()
        self.assertIsNone(self.engine.s['mhs_budget']['stopped_reason'])
        self.engine.start_watch('session-current', self.engine.s['run_id'], 'explicit-resume')
        self.assertEqual(self.engine.s['mhs_budget']['start_wall'], start_wall)
        self.assertEqual(self.engine.s['mhs_budget']['start_sim'], start_sim)
        self.assertEqual(self.engine.s['smart_ward']['pauses'][0]['sim_seconds'], 304)
        self.assertTrue(self.engine.state()['smart_ward']['watch']['enabled'])
        self.assertIsNotNone(self.engine.claim_notification('new-worker'))

    def test_restart_pause_does_not_extend_expired_episode_budget(self):
        self.start()
        self.low_light()
        self.engine.db.close()
        self.engine = SmartWardV1(self.root, self.stations, require_sim_truth=True)
        self.engine.backend = Backend()
        self.engine.guard.lease(True)
        self.place('station_b')
        self.observe()
        self.engine.s['sim_time'] += 3000
        self.engine.start_watch('session-current', self.engine.s['run_id'], 'resume-after-expiry')
        self.engine.tick()
        self.assertEqual(self.engine.s['mhs_budget']['stopped_reason'], 'sim_time_limit')
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        self.assertEqual(self.engine.state()['smart_ward']['incidents'][0]['status'], 'unresolved')

    def test_user_stop_records_unresolved_and_late_pickup_ack_does_not_reopen(self):
        self.start()
        order = self.sample()
        pending = self.engine.device_action('hospital-cabinet-b', 'handoff_order',
                                           dict(order_id=order['order_id'], operation_id='pick-before-stop'))
        self.engine.stop_run('user-stop', self.engine.s['run_id'])
        self.handoff_ack(pending)
        actual = self.engine.s['orders'][order['order_id']]
        self.assertTrue(actual['loaded'])  # Scene committed before the stop ACK.
        self.assertEqual(actual['status'], 'failed')
        incident = self.engine.s['smart_ward']['incidents'][order['incident_id']]
        self.assertEqual(incident['status'], 'unresolved')
        self.assertEqual(incident['reason'], 'user_stop')
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])

    def test_finalize_rejects_old_verification_when_new_event_joins_episode(self):
        self.start()
        self.low_light()
        self.devices['hospital-sensor-301']['relative_illumination'] = 76
        for _ in range(3):
            self.observe()
        self.engine.verify_services()
        self.low_light()
        with self.assertRaisesRegex(ValueError, 'verification_required'):
            self.engine.finalize_episode(self.engine.s['run_id'], 'session-current')
        self.assertNotIn('end_wall', self.engine.s['mhs_budget'])

    def test_expired_specimen_stays_in_cabinet_as_explicit_unresolved(self):
        self.specimens['S-expired'] = dict(specimen_id='S-expired', location='hospital-cabinet-b', sealed=True,
                                          priority='normal', deadline_sim=1)
        self.devices['hospital-cabinet-b']['specimens'].append('S-expired')
        self.observe()
        self.start()
        report = self.engine.verify_services()
        self.assertEqual(report['total'], 1)
        self.assertEqual(len(report['unresolved']), 1)
        self.assertIn('期限', report['unresolved'][0]['reason'])
        self.assertFalse(self.engine.s['orders'])
        self.assertEqual(self.engine.s['smart_ward']['specimens']['S-expired']['location'], 'hospital-cabinet-b')
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM ward_notifications').fetchone()[0], 1)

    def qualification(self, op='qualification-begin'):
        return self.engine.begin_navigation_qualification('qualification-session', self.engine.s['run_id'], op)

    def test_navigation_qualification_is_admin_only_empty_run_bounded_motion(self):
        q = self.qualification()['qualification']
        self.assertEqual(q['version'], 'smart-nav-20261013.3')
        self.assertEqual(q['deadline_wall'] - q['segment_started_wall'], 2400)
        self.assertEqual(q['total_deadline_wall'] - q['started_wall'], 7200)
        self.assertEqual(q['max_attempts'], 12)
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        self.assertIsNone(self.engine.claim_notification('worker'))
        self.assertEqual(q['allowed_stations'], ['station_b', 'lab', 'station_c', 'depot'])
        with self.assertRaisesRegex(ValueError, 'requires_reset'):
            self.engine.start_watch('qualification-session', self.engine.s['run_id'], 'start-in-qualification')
        route = dict(route_id='qualified-route', destination='lab', points=[[self.engine.pose['x'], self.engine.pose['y']], [0, 0]])
        with patch.object(self.engine.routes, 'selected', return_value=route):
            result = self.engine.do('navigate_route', dict(route_id='qualified-route', operation_id='qual-navigation'))
        self.assertEqual(result['goal_station'], 'lab')
        self.assertEqual(self.engine.s['smart_ward']['navigation_qualification']['attempts'], 1)
        with patch.object(self.engine.routes, 'selected', return_value=dict(route, destination='station_a')):
            with self.assertRaisesRegex(ValueError, 'destination_not_allowed'):
                self.engine.do('navigate_route', dict(route_id='wrong-route', operation_id='qual-wrong-destination'))

    def test_qualification_segments_keep_global_budget_and_do_not_restart_failure(self):
        q = self.qualification()['qualification']
        original_started, original_deadline = q['started_wall'], q['total_deadline_wall']
        for index in [2, 3]:
            self.engine.end_navigation_qualification(self.engine.s['run_id'], 'end-' + str(index))
            q = self.qualification('begin-' + str(index))['qualification']
            self.assertEqual(q['segment'], index)
            self.assertEqual(q['started_wall'] + 7200, q['total_deadline_wall'])
            self.assertEqual(q['started_wall'], original_started)
            self.assertEqual(q['total_deadline_wall'], original_deadline)
            self.engine.end_navigation_qualification(self.engine.s['run_id'], 'end-' + str(index))
            self.assertTrue(self.engine.s['smart_ward']['navigation_qualification']['active'])
        self.engine.end_navigation_qualification(self.engine.s['run_id'], 'end-final')
        with self.assertRaisesRegex(ValueError, 'not_continuable'):
            self.qualification('forbidden-fourth-segment')
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])

    def test_old_navigation_qualification_cannot_begin_extend_or_replay_as_new_version(self):
        self.qualification()
        self.engine.end_navigation_qualification(self.engine.s['run_id'], 'old-end')
        for version in (None, 'smart-nav-20261013.1', 'smart-nav-20261013.2'):
            q = self.engine.s['smart_ward']['navigation_qualification']
            if version is None:
                q.pop('version', None)
            else:
                q['version'] = version
            before = copy.deepcopy(q)
            operations = self.engine.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
            for operation in ('new-segment', 'qualification-begin'):
                with self.subTest(version=version, operation=operation), self.assertRaisesRegex(ValueError, 'version_requires_new_run'):
                    self.qualification(operation)
            self.assertEqual(self.engine.s['smart_ward']['navigation_qualification'], before)
            self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], operations)
            with self.assertRaisesRegex(ValueError, 'version_requires_new_run'):
                self.engine.do('navigate_route', dict(route_id='unused', operation_id='old-version-route'))

    def test_new_segment_is_clipped_to_original_global_deadline(self):
        first = self.qualification()['qualification']
        self.engine.end_navigation_qualification(self.engine.s['run_id'], 'first-end')
        later = first['total_deadline_wall'] - 5
        with patch('mhs.smart_ward.time.time', return_value=later), \
             patch.object(self.engine, 'stopped', return_value=True), patch.object(self.engine, 'fresh', return_value=True), \
             patch.object(self.engine, '_scene_fresh', return_value=True), patch.object(self.engine.guard, 'healthy', return_value=True):
            second = self.qualification('late-second-segment')['qualification']
        self.assertEqual(second['version'], NAVIGATION_QUALIFICATION_VERSION)
        self.assertEqual(second['started_wall'], first['started_wall'])
        self.assertEqual(second['total_deadline_wall'], first['total_deadline_wall'])
        self.assertEqual(second['deadline_wall'], first['total_deadline_wall'])
        self.assertEqual(second['deadline_wall'] - second['segment_started_wall'], 5)

    def test_public_scene_sample_projects_actual_consumed_scene_without_mutation(self):
        actual = copy.deepcopy(self.engine.s['smart_ward']['scene'])
        public = self.engine.state()
        expected = {key: actual[key] for key in ('run_id', 'epoch', 'seq', 'wall_time', 'sim_time')}
        self.assertEqual(public['smart_ward']['scene_sample'], expected)
        self.assertNotIn('scene', public['smart_ward'])
        public['smart_ward']['scene_sample']['seq'] = -1
        self.assertEqual(self.engine.s['smart_ward']['scene'], actual)

    def test_navigation_refreshes_new_disk_sample_before_freshness_gate(self):
        self.qualification()
        cached = self.engine.s['smart_ward']['scene']
        cached['wall_time'] = 0
        sample = copy.deepcopy(cached)
        sample.update(seq=cached['seq'] + 1, wall_time=time.time())
        (self.engine.scene_dir / 'observation.json').write_text(json.dumps(sample))
        route = dict(route_id='fresh-disk-route', destination='lab',
                     points=[[self.engine.pose['x'], self.engine.pose['y']], [0, 0]])
        with patch.object(self.engine.routes, 'selected', return_value=route):
            result = self.engine.do('navigate_route', dict(route_id=route['route_id'], operation_id='fresh-disk-navigation'))
        self.assertEqual(result['goal_station'], 'lab')
        self.assertEqual(self.engine.s['smart_ward']['scene']['wall_time'], sample['wall_time'])
        self.assertEqual(self.engine.s['smart_ward']['scene']['seq'], sample['seq'])

    def test_navigation_refresh_blocks_new_disk_specimen_in_empty_qualification(self):
        self.qualification()
        sample = copy.deepcopy(self.engine.s['smart_ward']['scene'])
        sample.update(seq=sample['seq'] + 1, wall_time=time.time())
        sample['specimens']['S-arrived-before-motion'] = dict(specimen_id='S-arrived-before-motion',
            location='hospital-cabinet-b', priority='normal', sealed=True, deadline_sim=900)
        (self.engine.scene_dir / 'observation.json').write_text(json.dumps(sample))
        with patch.object(self.engine.backend, 'start_route') as start:
            with self.assertRaisesRegex(ValueError, 'navigation_qualification_scope_changed'):
                self.engine.do('navigate_route', dict(route_id='unused', operation_id='new-disk-specimen-navigation'))
        start.assert_not_called()
        self.assertEqual(self.engine.s['smart_ward']['navigation_qualification']['attempts'], 0)

    def test_navigation_refresh_rejects_stale_and_wrong_epoch_disk_samples(self):
        self.qualification()
        cached = self.engine.s['smart_ward']['scene']
        cached['wall_time'] = 0
        for label in ['stale', 'wrong-epoch']:
            with self.subTest(sample=label):
                sample = copy.deepcopy(cached)
                sample.update(seq=cached['seq'] + 1, wall_time=time.time())
                if label == 'stale':
                    sample['wall_time'] -= 20
                else:
                    sample['epoch'] = 'other-epoch'
                (self.engine.scene_dir / 'observation.json').write_text(json.dumps(sample))
                with patch.object(self.engine.backend, 'start_route') as start:
                    with self.assertRaisesRegex(ValueError, 'navigation_qualification_feedback_or_lease_stale'):
                        self.engine.do('navigate_route', dict(route_id='unused', operation_id=label + '-disk-navigation'))
                start.assert_not_called()
                self.assertEqual(self.engine.s['smart_ward']['scene']['wall_time'], 0)
                self.assertEqual(self.engine.s['smart_ward']['navigation_qualification']['attempts'], 0)

    def test_qualification_timeout_stops_and_cannot_extend_budget(self):
        self.qualification()
        q = self.engine.s['smart_ward']['navigation_qualification']
        q['deadline_wall'] = time.time() - 1
        self.engine.tick()
        self.assertFalse(q['active'])
        self.assertEqual(q['reason'], 'qualification_wall_time_limit')
        self.assertFalse(self.engine.guard.armed)
        with self.assertRaisesRegex(ValueError, 'not_continuable'):
            self.qualification('forbidden-retry')

    def test_navigation_without_specimen_or_explicit_qualification_is_refused(self):
        self.start()
        with self.assertRaisesRegex(ValueError, 'no_active_specimen_transport'):
            self.engine.do('navigate_route', dict(route_id='unused', operation_id='not-qualified'))

    def navigation_task(self, with_identity=True):
        task = dict(nav_task_id='persisted-navigation', goal_station='lab', status='pending')
        self.engine.s['navigation_tasks'][task['nav_task_id']] = copy.deepcopy(task)
        self.engine.nav = copy.deepcopy(task)
        self.engine.save()
        if with_identity:
            self.engine.nav_goal_identity(task['nav_task_id'], '1' * 32, '/carter1/navigate_through_poses')
        return task['nav_task_id']

    def restart_with_stopped_feedback(self):
        self.engine.db.close()
        self.engine = SmartWardV1(self.root, self.stations, require_sim_truth=True)
        self.engine.backend = Backend()
        self.engine.guard.lease(True)
        self.place('station_b')
        self.observe()

    def test_goal_identity_is_committed_before_send_and_zero_uuid_is_refused(self):
        tid = self.navigation_task(with_identity=False)
        with self.engine._atomic():
            self.assertFalse(self.engine.nav_goal_identity(tid, '1' * 32, '/carter1/navigate_through_poses')['persisted'])
        with self.assertRaisesRegex(ValueError, 'invalid_nav_goal_identity'):
            self.engine.nav_goal_identity(tid, '0' * 32, '/carter1/navigate_through_poses')
        self.assertTrue(self.engine.nav_goal_identity(tid, '1' * 32, '/carter1/navigate_through_poses')['persisted'])
        with closing(sqlite3.connect(self.engine.data_dir / 'state.sqlite')) as db:
            persisted = json.loads(db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
        self.assertEqual(persisted['navigation_tasks'][tid]['nav_goal_uuid'], '1' * 32)
        with self.assertRaisesRegex(ValueError, 'identity_conflict'):
            self.engine.nav_goal_identity(tid, '2' * 32, '/carter1/navigate_through_poses')

    def test_restart_navigation_needs_exact_terminal_sample_and_real_stillness(self):
        self.start()
        tid = self.navigation_task()
        self.restart_with_stopped_feedback()
        self.assertTrue(self.engine.stopped())
        self.assertTrue(self.engine.s['smart_ward']['navigation_recovery_required'])
        self.assertEqual(self.engine.s['navigation_tasks'][tid]['status'], 'unknown')
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            self.engine.reset()
        self.assertFalse(self.engine.nav_goal_terminal(tid, '2' * 32, '/carter1/navigate_through_poses', 5)['confirmed'])
        self.assertFalse(self.engine.nav_goal_terminal(tid, '1' * 32, '/carter1/navigate_to_pose', 5)['confirmed'])
        self.assertFalse(self.engine.nav_goal_terminal(tid, '1' * 32, '/carter1/navigate_through_poses', 2)['confirmed'])
        self.assertTrue(self.engine.nav_goal_terminal(tid, '1' * 32, '/carter1/navigate_through_poses', 5)['confirmed'])
        self.engine.pose['speed'] = 0.1
        self.engine.tick()
        self.assertTrue(self.engine.s['smart_ward']['navigation_recovery_required'])
        self.place('station_b')
        self.engine.tick()
        self.assertFalse(self.engine.s['smart_ward']['navigation_recovery_required'])
        self.assertEqual(self.engine.s['navigation_tasks'][tid]['nav_recovery_confirmation']['status'], 5)
        self.engine.start_watch('session-current', self.engine.s['run_id'], 'resume-reconciled-navigation')

    def test_missing_uuid_stays_locked_despite_fresh_stationary_robot(self):
        tid = self.navigation_task(with_identity=False)
        self.restart_with_stopped_feedback()
        before = self.engine.db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND json_extract(body,'$.kind')='motion_guard_stop'", (self.engine.s['run_id'],)).fetchone()[0]
        for _ in range(3):
            self.engine.tick()
        after = self.engine.db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND json_extract(body,'$.kind')='motion_guard_stop'", (self.engine.s['run_id'],)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertTrue(self.engine.stopped())
        self.assertTrue(self.engine.s['smart_ward']['navigation_recovery_required'])
        self.assertFalse(self.engine.nav_goal_terminal(tid, '1' * 32, '/carter1/navigate_through_poses', 5)['confirmed'])
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            self.engine.reset()
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            self.engine.do('navigate_route', dict(route_id='unused', operation_id='unsafe-new-motion'))

    def test_uncertain_goal_result_locks_motion_until_exact_terminal(self):
        self.start()
        tid = self.navigation_task()
        self.assertTrue(self.engine.nav_goal_uncertain(tid, '1' * 32, '/carter1/navigate_through_poses', 'transport_lost')['recorded'])
        self.assertEqual(self.engine.nav['status'], 'unknown')
        self.assertTrue(self.engine.s['smart_ward']['navigation_recovery_required'])
        self.assertFalse(self.engine.nav_goal_terminal('late-old-task', '1' * 32, '/carter1/navigate_through_poses', 5)['confirmed'])
        self.assertTrue(self.engine.nav_goal_terminal(tid, '1' * 32, '/carter1/navigate_through_poses', 5)['confirmed'])
        self.engine.tick()
        self.assertFalse(self.engine.s['smart_ward']['navigation_recovery_required'])

    def fault_begin(self, duration=240):
        return self.engine.begin_fault_qualification('qualification:boundary', self.engine.s['run_id'],
            self.engine.s['smart_ward']['epoch'], 'fault-begin', duration)['qualification']

    def fault_command(self, fq, kind, arguments, operation_id):
        return self.engine.fault_qualification_command(fq['qualification_id'], fq['session_key'],
            fq['run_id'], fq['epoch'], kind, arguments, operation_id)

    def fault_ack(self, operation, **changes):
        ack = {k: operation['command'][k] for k in ['run_id', 'epoch', 'qualification_id', 'session_key', 'operation_id']}
        ack.update(status='completed', result={'actual': True}, wall_time=time.time(), sim_time=self.engine.s['sim_time'])
        ack.update(changes)
        folder = self.engine.scene_dir / 'fault-acks'
        folder.mkdir(exist_ok=True)
        (folder / (operation['operation_id'] + '.json')).write_text(json.dumps(ack), encoding='utf-8')
        self.engine.tick()

    def test_fault_qualification_requires_new_unclaimed_scoped_run(self):
        run, epoch = self.engine.s['run_id'], self.engine.s['smart_ward']['epoch']
        with self.assertRaisesRegex(ValueError, 'session_required'):
            self.engine.begin_fault_qualification('ordinary-session', run, epoch, 'wrong-session')
        with self.assertRaisesRegex(ValueError, 'epoch_mismatch'):
            self.engine.begin_fault_qualification('qualification:test', run, 'old-epoch', 'wrong-epoch')
        with self.assertRaisesRegex(ValueError, '1_to_1800'):
            self.engine.begin_fault_qualification('qualification:test', run, epoch, 'unbounded', 1801)
        self.engine.claim_session('existing-user', run)
        with self.assertRaisesRegex(ValueError, 'unclaimed_empty_run'):
            self.fault_begin()

    def test_fault_qualification_never_bypasses_normal_navigation_or_submits_model(self):
        fq = self.fault_begin()
        self.assertEqual(fq['deadline_wall'] - fq['started_wall'], 240)
        self.engine.start_watch(fq['session_key'], fq['run_id'], 'qualification-watch')
        with self.assertRaisesRegex(ValueError, 'no_active_specimen_transport'):
            self.engine.do('navigate_route', dict(route_id='unused', operation_id='qualification-no-order'))
        self.low_light()
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM ward_notifications').fetchone()[0], 0)
        self.assertIsNone(self.engine.claim_notification('workbench-worker'))
        with self.assertRaisesRegex(ValueError, 'no_model_calls'):
            self.engine.record_model_request('forbidden-model-request', fq['run_id'], fq['session_key'])
        self.assertEqual(self.engine.s['mhs_budget']['decisions'], 0)
        control = json.loads((self.engine.scene_dir / 'control.json').read_text())
        self.assertEqual(control['fault_qualification']['qualification_id'], fq['qualification_id'])
        self.assertTrue(control['fault_qualification']['active'])

    def test_fault_commands_are_bounded_idempotent_and_accept_only_exact_ack(self):
        fq = self.fault_begin()
        for kind, args in [('set_lamp_fault', {}), ('pause_simulation', {'seconds': 16}),
                           ('set_cabinet_availability', {'cabinet_id': 'foreign-cabinet', 'available': False})]:
            with self.assertRaises(ValueError):
                self.fault_command(fq, kind, args, 'bad-' + kind)
        op = self.fault_command(fq, 'set_cabinet_availability',
                                {'cabinet_id': 'hospital-cabinet-b', 'available': False}, 'fault-cabinet')
        self.assertTrue((self.engine.scene_dir / 'fault-commands' / 'fault-cabinet.json').exists())
        self.fault_ack(op, epoch='wrong-epoch')
        self.assertEqual(self.engine.fault_qualification_operation('fault-cabinet')['status'], 'unknown')
        self.fault_ack(op)
        repeated = self.fault_command(fq, 'set_cabinet_availability',
                                     {'cabinet_id': 'hospital-cabinet-b', 'available': False}, 'fault-cabinet')
        self.assertEqual(repeated['status'], 'completed')
        self.assertEqual(self.engine.s['smart_ward']['fault_qualification']['commands_used'], 1)
        self.assertEqual(len(repeated['ack_sha256']), 64)
        with self.assertRaisesRegex(ValueError, 'operation_id_conflict'):
            self.fault_command(fq, 'set_cabinet_availability',
                               {'cabinet_id': 'hospital-cabinet-b', 'available': True}, 'fault-cabinet')
        with self.assertRaisesRegex(ValueError, 'session_mismatch'):
            self.engine.fault_qualification_command(fq['qualification_id'], 'qualification:other', fq['run_id'],
                fq['epoch'], 'resume_simulation', {}, 'foreign-session-command')

    def test_fault_ack_hold_requires_real_pending_handoff_and_is_bounded(self):
        fq = self.fault_begin()
        self.engine.start_watch(fq['session_key'], fq['run_id'], 'qualification-watch')
        with self.assertRaisesRegex(ValueError, 'real_handoff'):
            self.fault_command(fq, 'hold_ack', {'target_operation_id': 'fake-target'}, 'fake-hold')
        order = self.sample('S-qualified')
        handoff = self.engine.device_action('hospital-cabinet-b', 'handoff_order',
            dict(order_id=order['order_id'], operation_id='real-qualified-handoff'))
        with self.assertRaisesRegex(ValueError, 'max_90'):
            self.fault_command(fq, 'hold_ack', {'target_operation_id': handoff['operation_id'], 'hold_seconds': 91}, 'unbounded-hold')
        hold = self.fault_command(fq, 'hold_ack', {'target_operation_id': handoff['operation_id']}, 'hold-real-ack')
        self.assertEqual(hold['command']['arguments']['hold_seconds'], 90)
        self.assertFalse(scene_path(self.engine.scene_dir, 'acks', self.engine.s['run_id'], self.engine.s['smart_ward']['epoch'], handoff['operation_id']).exists())
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.fault_ack(hold)
        release = self.fault_command(fq, 'release_ack', {'target_operation_id': handoff['operation_id']}, 'release-real-ack')
        self.fault_ack(release)
        self.assertIsNone(self.engine.s['smart_ward']['fault_qualification']['held_ack_operation'])
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])  # No fabricated business ACK.

    def test_fault_pause_refuses_active_or_unknown_navigation(self):
        fq = self.fault_begin()
        for status in ['pending', 'running', 'canceling', 'unknown']:
            with self.subTest(status=status):
                self.engine.s['navigation_tasks']['persisted-task'] = dict(status=status)
                with self.assertRaisesRegex(ValueError, 'pause_requires_fresh_stopped_robot_without_navigation'):
                    self.fault_command(fq, 'pause_simulation', {'seconds': 5}, 'pause-with-' + status)
                self.assertFalse(self.engine.s['smart_ward']['fault_qualification']['simulation_pause_requested'])
                self.assertEqual(self.engine.s['smart_ward']['fault_qualification']['commands_used'], 0)
                self.assertFalse((self.engine.scene_dir / 'fault-commands' / ('pause-with-' + status + '.json')).exists())

    def test_fault_pause_latches_motion_before_ack_and_after_resume(self):
        fq = self.fault_begin()
        pause = self.fault_command(fq, 'pause_simulation', {'seconds': 5}, 'pause-idle-robot')
        self.assertTrue(self.engine.s['smart_ward']['fault_qualification']['simulation_pause_requested'])
        self.assertFalse(self.engine.guard.armed)
        control = json.loads((self.engine.scene_dir / 'control.json').read_text())
        self.assertTrue(control['fault_qualification']['simulation_pause_requested'])
        for label in ['pause-pending', 'resume-completed']:
            if label == 'resume-completed':
                self.fault_ack(pause)
                resume = self.fault_command(fq, 'resume_simulation', {}, 'resume-idle-robot')
                self.fault_ack(resume)
                self.engine.guard.lease(True)
            with self.subTest(phase=label), patch.object(self.engine.backend, 'start_route') as start:
                with self.assertRaisesRegex(ValueError, 'simulation_pause_requires_new_run_before_motion'):
                    self.engine.do('navigate_route', dict(route_id='unused', operation_id='motion-' + label))
                start.assert_not_called()
        # Even the separate navigation qualification branch cannot bypass the latch.
        self.engine.s['smart_ward']['navigation_qualification'] = dict(active=True)
        with self.assertRaisesRegex(ValueError, 'simulation_pause_requires_new_run_before_motion'):
            self.engine.do('navigate_route', dict(route_id='unused', operation_id='motion-qualification-after-pause'))

    def test_fault_pause_operation_replay_keeps_motion_latched(self):
        fq = self.fault_begin()
        pause = self.fault_command(fq, 'pause_simulation', {'seconds': 5}, 'replay-pause')
        self.fault_ack(pause)
        resume = self.fault_command(fq, 'resume_simulation', {}, 'replay-resume')
        self.fault_ack(resume)
        used = self.engine.s['smart_ward']['fault_qualification']['commands_used']
        for kind, arguments, operation in [('pause_simulation', {'seconds': 5}, pause),
                                           ('resume_simulation', {}, resume)]:
            repeated = self.fault_command(fq, kind, arguments, operation['operation_id'])
            self.assertEqual(repeated['status'], 'completed')
            self.assertTrue(self.engine.s['smart_ward']['fault_qualification']['simulation_pause_requested'])
        self.assertEqual(self.engine.s['smart_ward']['fault_qualification']['commands_used'], used)
        with self.assertRaisesRegex(ValueError, 'simulation_pause_requires_new_run_before_motion'):
            self.engine.do('navigate_route', dict(route_id='unused', operation_id='motion-after-operation-replay'))

    def test_reset_refuses_active_fault_qualification_without_changing_identity(self):
        fq = self.fault_begin()
        control = (self.engine.scene_dir / 'control.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'fault_qualification_must_end_before_reset'):
            self.engine.reset()
        self.assertEqual(self.engine.s['run_id'], fq['run_id'])
        self.assertEqual(self.engine.s['smart_ward']['epoch'], fq['epoch'])
        self.assertEqual((self.engine.scene_dir / 'control.json').read_bytes(), control)

    def test_reset_refuses_ended_fault_until_exact_restoration_and_new_scene_sample(self):
        fq = self.fault_begin()
        self.engine.end_fault_qualification(fq['qualification_id'], fq['session_key'], fq['run_id'],
                                           fq['epoch'], 'fault-end-before-scene-restores')
        qualification = self.engine.s['smart_ward']['fault_qualification']
        restored = {key: qualification[key] for key in ['run_id', 'epoch', 'qualification_id', 'session_key']}
        restored.update(active=False, paused=None, holds={})
        receipt = self.engine.scene_dir / 'fault-state.json'
        control = (self.engine.scene_dir / 'control.json').read_bytes()
        attempts = [None, [], dict(restored, active=True), dict(restored, epoch='other-epoch'),
                    dict(restored, paused={'was_playing': True}), dict(restored, holds={'real-op': {}}), restored]
        for index, value in enumerate(attempts):
            if value is not None:
                receipt.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(attempt=index), self.assertRaisesRegex(ValueError, 'restoration_not_confirmed_before_reset'):
                self.engine.reset()
            self.assertEqual(self.engine.s['run_id'], fq['run_id'])
            self.assertEqual(self.engine.s['smart_ward']['epoch'], fq['epoch'])
            self.assertEqual((self.engine.scene_dir / 'control.json').read_bytes(), control)

        # A newer wall timestamp without a newer scene sequence is insufficient.
        observation = json.loads((self.engine.scene_dir / 'observation.json').read_text())
        observation['wall_time'] = time.time()
        (self.engine.scene_dir / 'observation.json').write_text(json.dumps(observation))
        with self.assertRaisesRegex(ValueError, 'restoration_not_confirmed_before_reset'):
            self.engine.reset()
        self.assertEqual(self.engine.s['run_id'], fq['run_id'])

    def test_reset_accepts_restored_fault_with_fresh_post_end_sample_and_real_stop(self):
        fq = self.fault_begin()
        pause = self.fault_command(fq, 'pause_simulation', {'seconds': 5}, 'pause-before-reset')
        self.fault_ack(pause)
        self.engine.end_fault_qualification(fq['qualification_id'], fq['session_key'], fq['run_id'],
                                           fq['epoch'], 'end-restored-before-reset')
        qualification = copy.deepcopy(self.engine.s['smart_ward']['fault_qualification'])
        restored = {key: qualification[key] for key in ['run_id', 'epoch', 'qualification_id', 'session_key']}
        restored.update(active=False, paused=None, holds={}, originals={})
        (self.engine.scene_dir / 'fault-state.json').write_text(json.dumps(restored), encoding='utf-8')
        self.place('station_b')
        self.observe()
        self.assertGreater(self.engine.s['smart_ward']['scene']['seq'], qualification['ended_scene_seq'])
        self.assertGreater(self.engine.s['smart_ward']['scene']['wall_time'], qualification['ended_wall'])
        result = self.engine.reset()
        self.assertNotEqual(result['run_id'], fq['run_id'])
        self.assertNotEqual(result['smart_ward']['epoch'], fq['epoch'])
        self.assertNotIn('fault_qualification', self.engine.s['smart_ward'])
        self.assertFalse(self.engine.guard.armed)

    def test_fault_qualification_command_count_and_expiry_are_finite(self):
        fq = self.fault_begin()
        for index in range(20):
            self.fault_command(fq, 'set_cabinet_availability',
                {'cabinet_id': 'hospital-cabinet-b', 'available': bool(index % 2)}, 'bounded-command-' + str(index))
        with self.assertRaisesRegex(ValueError, 'command_limit'):
            self.fault_command(fq, 'resume_simulation', {}, 'over-budget')
        self.engine.start_watch(fq['session_key'], fq['run_id'], 'qualification-watch')
        self.low_light()
        self.engine.s['smart_ward']['fault_qualification']['deadline_wall'] = time.time() - 1
        self.engine.tick()
        actual = self.engine.s['smart_ward']['fault_qualification']
        self.assertFalse(actual['active'])
        self.assertEqual(actual['reason'], 'qualification_deadline')
        self.assertEqual(self.engine.s['operator_stopped']['reason'], 'user_stop')
        self.assertEqual(self.engine.state()['smart_ward']['incidents'][0]['status'], 'unresolved')
        self.assertFalse(self.engine.guard.armed)
        control = json.loads((self.engine.scene_dir / 'control.json').read_text())
        self.assertFalse(control['fault_qualification']['active'])
        self.assertTrue(control['stop_requested'])

    def test_fault_qualification_end_preserves_actual_cargo_and_is_idempotent(self):
        fq = self.fault_begin(duration=1800)
        self.assertEqual(fq['deadline_wall'] - fq['started_wall'], 1800)
        self.engine.start_watch(fq['session_key'], fq['run_id'], 'qualification-watch')
        order = self.sample('S-end-qualified')
        handoff = self.engine.device_action('hospital-cabinet-b', 'handoff_order',
            dict(order_id=order['order_id'], operation_id='qualified-pickup'))
        self.handoff_ack(handoff)
        args = (fq['qualification_id'], fq['session_key'], fq['run_id'], fq['epoch'], 'qualification-end')
        result = self.engine.end_fault_qualification(*args)
        self.assertFalse(result['qualification']['active'])
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        self.assertEqual(self.engine.s['orders'][order['order_id']]['status'], 'failed')
        self.assertEqual(self.engine.state()['smart_ward']['incidents'][0]['reason'], 'user_stop')
        repeated = self.engine.end_fault_qualification(*args)
        self.assertEqual(result, repeated)
        with self.assertRaisesRegex(ValueError, 'operation_id_conflict'):
            self.engine.end_fault_qualification(*args, reason='changed-reason')
        self.assertTrue((self.engine.data_dir / fq['run_id'] / 'fault-qualification-report.json').exists())
        self.assertFalse(self.engine.guard.armed)

    def test_receiver_timeout_preserves_cargo_and_marks_unresolved(self):
        self.start()
        order = self.sample()
        pickup = self.engine.device_action('hospital-cabinet-b', 'handoff_order', dict(order_id=order['order_id'], operation_id='timeout-pick'))
        self.handoff_ack(pickup)
        self.devices['hospital-cabinet-lab']['available'] = False
        self.observe()
        self.engine.s['sim_time'] += 151
        self.observe()
        self.assertEqual(self.engine.s['orders'][order['order_id']]['status'], 'failed')
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])
        self.assertEqual(self.engine.s['mhs_outcome'], 'incomplete')
        self.assertEqual(len(self.engine.verify_services()['unresolved']), 1)
        with self.assertRaisesRegex(ValueError, 'recovery_or_new_run'):
            self.engine.device_action('hospital-light-301', 'set_brightness', dict(brightness_percent=60, operation_id='after-failure'))

    def test_declared_final_station_is_required_for_complete_task(self):
        self.start()
        # Version161: final-station motion belongs to a transport episode.
        order = self.sample()
        incident = self.engine.s['smart_ward']['incidents'][order['incident_id']]
        self.engine.publish_service_plan(self.engine.s['planning_version'], [
            dict(action='pickup', incident_id=incident['incident_id']),
            dict(action='deliver', incident_id=incident['incident_id']),
            dict(action='standby', station_id='station_a')], '标本交接后在A站待命', 'final-station-plan')
        pickup = self.engine.device_action('hospital-cabinet-b', 'handoff_order',
            dict(order_id=order['order_id'], operation_id='final-station-pickup'))
        self.handoff_ack(pickup)
        self.place('lab')
        self.observe()
        receive = self.engine.device_action('hospital-cabinet-lab', 'handoff_order',
            dict(order_id=order['order_id'], operation_id='final-station-receive'))
        self.handoff_ack(receive)
        self.assertFalse(self.engine.finished())
        self.assertFalse(self.engine.s['mhs_finished'])
        self.place('station_a')
        self.observe()
        self.assertTrue(self.engine.finished())


if __name__ == '__main__':
    unittest.main()
