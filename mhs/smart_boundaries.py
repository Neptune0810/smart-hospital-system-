"""Bounded real API / Isaac boundary qualification, without an Agent.

Run only on a new, empty smart-ward run with the project's workbench stopped.
This driver never resets a run, starts/stops processes, navigates, edits scene
files, fabricates samples or sends an SDK prompt. It does create three real
scene specimens and one or two business orders, then stops that qualification
run. Its evidence is explicitly separate from real-Agent acceptance.
"""
import argparse
import copy
import hashlib
import json
import math
import os
import signal
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .smart_navigation import physical_stationary


ROOT = Path(__file__).resolve().parents[1]
DEVICES = {'hospital-robot-01', 'hospital-cabinet-b', 'hospital-cabinet-c',
           'hospital-cabinet-lab', 'hospital-light-301', 'hospital-light-303',
           'hospital-sensor-301', 'hospital-sensor-303'}
CASES = ('source_capacity_and_operation_replay', 'fresh_sampling_incident_deduplication',
         'notification_retry_identity', 'duplicate_order_for_one_incident',
         'wrong_cabinet', 'unknown_order', 'wrong_physical_location',
         'legacy_handoff_entry_denied', 'operation_payload_conflict',
         'invalid_brightness_and_unknown_device')
UNIMPLEMENTED = {
    'robot_full': 'Requires actual pickups at B/C and a third real pickup attempt; this stationary driver never navigates.',
    'receiver_full': 'Requires six actual deliveries and a seventh attempt; no API may invent receiver occupancy.',
    'cabinet_offline': 'No approved environment command currently makes a source cabinet offline; do not fake its observation.',
    'handoff_ack_loss': 'Requires real arrival, a committed physical transfer, and a controlled ACK-withhold hook; this driver does not alter ACK files.',
    'stale_simulation_pause': 'Requires an explicit simulation pause/resume hook with new sampling afterwards; this driver does not stop the simulator.',
    'receiver_permanent_unavailable': 'Requires actual onboard cargo and 150 simulation seconds; covered only when a separate real transport test supplies evidence.',
    'backend_restart_during_motion': 'Requires a separate real navigation restart qualification and exact terminal goal reconciliation.',
    'wrong_specimen_payload_identity': 'handoff_order accepts order_id and derives specimen identity internally; mismatched scene ACK identity needs a controlled real fault hook, not a forged API payload.',
}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def append_json(path, value):
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def workbench_absent(root, connector=socket.create_connection, proc=Path('/proc')):
    """Check only this project's recorded process plus its fixed loopback port."""
    if not proc.is_dir():
        raise RuntimeError('boundary_driver_requires_server_procfs')
    marker = root / 'logs/mhs/workbench.pid'
    pid = None
    if marker.exists():
        pid = int(marker.read_text(encoding='utf-8').strip())
        directory = proc / str(pid)
        try:
            if not directory.joinpath('stat').read_text().split(') ', 1)[1].startswith('Z '):
                argv = directory.joinpath('cmdline').read_bytes().replace(b'\0', b' ').decode()
                if directory.joinpath('cwd').resolve() != root.resolve() or str(root / 'mhs/dist/workbench.mjs') not in argv:
                    raise RuntimeError('project_workbench_process_ownership_mismatch')
                return {'absent': False, 'project_pid': pid, 'port_closed': None}
        except FileNotFoundError:
            pass
    try:
        connection = connector(('127.0.0.1', 4194), timeout=.3)
    except ConnectionRefusedError:
        return {'absent': True, 'project_pid': pid, 'port_closed': True}
    except OSError as error:
        raise RuntimeError('workbench_port_state_unknown:' + type(error).__name__) from error
    connection.close()
    return {'absent': False, 'project_pid': pid, 'port_closed': False}


def ownership_projection(state):
    """Exclude normal timestamp/sample changes from refusal invariants."""
    ward = state.get('smart_ward', {})
    return {'orders': {key: {field: order.get(field) for field in ('specimen_id', 'package_id', 'status', 'loaded', 'receipt')}
                       for key, order in state.get('orders', {}).items()},
            'ownership': {key: value.get('location') for key, value in ward.get('specimens', {}).items()},
            'transfers': copy.deepcopy(ward.get('transfers', {})),
            'navigation_task_ids': sorted(state.get('navigation_tasks', {}))}


def selected_session_model_audit(root, session):
    path = root / 'agent/agh-home-mhs/data/sessions.db'
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        rows = list(connection.execute('SELECT seq,type,data FROM events WHERE session_key=? ORDER BY seq', (session,)))
    finally:
        connection.close()
    intents, requests = [], []
    for sequence, kind, raw in rows:
        if kind == 'request/sent':
            requests.append(sequence)
        elif kind == 'effect/intent' and json.loads(raw).get('kind') == 'inference':
            intents.append(sequence)
    return {'session_key': session, 'events': len(rows), 'request_sent_count': len(requests),
            'inference_intent_count': len(intents), 'request_sequences': requests, 'inference_sequences': intents}


class Boundaries:
    def __init__(self, arguments):
        self.args = arguments
        self.root = ROOT.resolve()
        if Path(os.environ.get('HOSPITAL_ROOT', self.root)).resolve() != self.root:
            raise ValueError('boundary_root_must_be_this_project')
        evidence_root = self.root / 'runs/smart-ward-v1/acceptance'
        default = evidence_root / ('boundaries-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        self.directory = Path(arguments.output).resolve() if arguments.output else default
        if not self.directory.is_relative_to(evidence_root) or self.directory == evidence_root:
            raise ValueError('boundary_evidence_must_be_new_child_of_smart_acceptance')
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.deadline = self.started + arguments.wall_seconds
        self.work_deadline = self.deadline - 30
        self.run_id = self.epoch = self.session = None
        self.worker = 'boundary-worker-' + uuid.uuid4().hex
        self.prefix = 'boundary-' + uuid.uuid4().hex[:16]
        self.interrupted = self.start_attempted = self.watch_started = False
        self.last_state = None
        self.claimed_notifications = {}
        self.specimens = self.orders = {}
        self.results = {name: {'status': 'NA', 'reason': 'not_executed'} for name in CASES}
        self.results.update({name: {'status': 'NA', 'reason': reason} for name, reason in UNIMPLEMENTED.items()})
        self.report = {'scope': 'real API and Isaac stationary boundary qualification; NOT Agent or transport acceptance',
                       'started_wall': self.started_wall, 'limits': {'wall_seconds': arguments.wall_seconds,
                       'scene_operation_wall_seconds': 20, 'maximum_successful_insertions': 3,
                       'maximum_orders': 2, 'navigation_allowed': False, 'model_calls_allowed': 0},
                       'cases': self.results}

    def record(self, kind, **values):
        append_json(self.directory / 'driver-events.jsonl', {'kind': kind, 'wall_time': time.time(), **values})

    def remaining(self, cleanup=False):
        value = (self.deadline if cleanup else self.work_deadline) - time.monotonic()
        if value <= 0 or self.interrupted and not cleanup:
            raise TimeoutError('boundary_interrupted' if self.interrupted else 'boundary_wall_budget_exhausted')
        return value

    def raw_api(self, path, arguments=None, *, cleanup=False):
        timeout = min(3, self.remaining(cleanup))
        request = urllib.request.Request('http://127.0.0.1:4188' + path,
            data=None if arguments is None else json.dumps(arguments, allow_nan=False).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, value = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, value = error.code, json.loads(error.read().decode())
        self.record('api', path=path, arguments=arguments, http_status=status, response=value)
        return status, value

    def api(self, path='/api/state', arguments=None, *, cleanup=False, expected_error=None, guarded=True):
        if arguments is not None and guarded and not cleanup:
            self.guard()
        status, value = self.raw_api(path, arguments, cleanup=cleanup)
        if expected_error:
            if status != 400 or value.get('ok') is not False or value.get('error') != expected_error:
                raise AssertionError('expected_refusal:' + expected_error + ':received:' + json.dumps(value))
            return value
        if status != 200 or value.get('ok') is False:
            raise RuntimeError('api_failed:' + str(status) + ':' + json.dumps(value))
        return value.get('result', value)

    def audit_zero_model(self, state=None):
        audit = selected_session_model_audit(self.root, self.session)
        value = state or self.last_state or {}
        model_ids = value.get('smart_ward', {}).get('model_request_ids', [])
        decisions = (value.get('mhs_budget') or {}).get('decisions', 0)
        audit.update(business_model_ids=list(model_ids), business_decisions=decisions)
        if audit['request_sent_count'] or audit['inference_intent_count'] or model_ids or decisions:
            raise RuntimeError('unexpected_model_request_in_boundary_qualification')
        return audit

    def guard(self):
        self.remaining()
        absent = workbench_absent(self.root)
        if not absent['absent']:
            raise RuntimeError('project_workbench_must_remain_absent')
        if self.session:
            self.audit_zero_model()
        if self.watch_started:
            # This production lease prevents another notification worker from
            # consuming the qualification's outbox; never submit an SDK prompt.
            self.claim_notification()

    def claim_notification(self):
        item = self.api('/api/admin/smart-ward/notifications/claim',
                        {'worker_id': self.worker, 'expected_run_id': self.run_id}, guarded=False)
        if item:
            if item.get('worker_id') != self.worker or item.get('session_id') != self.session or item.get('run_id') != self.run_id:
                raise RuntimeError('notification_lease_binding_mismatch')
            self.claimed_notifications[item['notification_id']] = copy.deepcopy(item)
        return item

    def state(self, *, cleanup=False):
        if not cleanup:
            self.guard()
        state = self.api(cleanup=cleanup)
        if self.run_id and state.get('run_id') != self.run_id:
            raise RuntimeError('qualification_run_changed')
        if state.get('navigation_tasks'):
            raise RuntimeError('unexpected_navigation_in_stationary_boundary_qualification')
        if self.watch_started and not cleanup and state.get('controller_session') != self.session:
            raise RuntimeError('qualification_controller_changed')
        if self.watch_started and not cleanup:
            observation = self.observation()
            if not physical_stationary(state, observation, self.run_id, self.epoch):
                write_json(self.directory / 'failed-physical-observation.json', {
                    'wall_time': time.time(), 'state': state, 'observation': observation,
                    'run_id': self.run_id, 'epoch': self.epoch,
                })
                raise RuntimeError('stationary_qualification_requires_continuous_fresh_physical_stop')
        self.last_state = state
        return state

    def observation(self):
        return json.loads((self.root / 'runs/smart-ward-v1/scene/observation.json').read_text(encoding='utf-8'))

    def preflight(self):
        absence = workbench_absent(self.root)
        state = self.state()
        ward = state.get('smart_ward', {})
        self.run_id, self.epoch = state.get('run_id'), ward.get('epoch')
        observation = self.observation()
        checks = {'mode': state.get('protocol') == 'smart-ward-v1', 'workbench_absent': absence['absent'],
                  'watch_off': not ward.get('watch', {}).get('enabled'), 'unclaimed': not state.get('controller_session'),
                  'no_orders': not state.get('orders'), 'no_specimens': not ward.get('specimens'),
                  'no_incidents': not ward.get('incidents'), 'no_prior_qualification': not ward.get('navigation_qualification'),
                  'not_stopped': not state.get('operator_stopped'), 'no_recovery': not any(ward.get(k) for k in
                      ('recovery_required', 'resume_required', 'navigation_recovery_required')),
                  'all_eight_fresh': set(ward.get('devices', {})) == DEVICES and all(
                      value.get('fresh') is True and value.get('available') is True for value in ward.get('devices', {}).values()),
                  'normal_lights': all(ward.get('devices', {}).get('hospital-light-' + suffix, {}).get('brightness_percent', 0) >= 50 for suffix in ('301', '303')),
                  'fresh_scene': ward.get('scene_fresh') is True, 'navigation_ready': state.get('navigation_ready') is True,
                  'physical_still': physical_stationary(state, observation, self.run_id, self.epoch)}
        write_json(self.directory / 'preflight.json', {'checks': checks, 'workbench': absence, 'state': state, 'observation': observation})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:' + ','.join(name for name, passed in checks.items() if not passed))
        self.session = 'qualification:boundaries:' + self.run_id + ':' + uuid.uuid4().hex[:12]
        self.audit_zero_model(state)
        self.start_attempted = True
        started = self.api('/api/admin/smart-ward/start', {'session_key': self.session,
            'expected_run_id': self.run_id, 'operation_id': self.prefix + '-start'})
        watch = started.get('watch', {})
        if watch.get('enabled') is not True or watch.get('session_id') != self.session:
            raise RuntimeError('boundary_watch_binding_not_confirmed')
        self.watch_started = True
        self.claim_notification()
        self.record('qualification_started', run_id=self.run_id, session=self.session, worker=self.worker)

    def scene_command(self, cabinet, label):
        command = {'kind': 'insert_specimen', 'arguments': {'cabinet_id': cabinet, 'priority': 'normal'},
                   'operation_id': self.prefix + '-' + label, 'expected_run_id': self.run_id}
        initial = self.api('/api/admin/smart-ward/command', command)
        deadline = min(time.monotonic() + 20, self.work_deadline)
        while time.monotonic() < deadline:
            self.guard()
            result = self.api('/api/ward/operation?id=' + urllib.parse.quote(command['operation_id']))
            if result.get('status') in {'completed', 'failed'}:
                return command, initial, result
            time.sleep(.5)
        raise TimeoutError('scene_command_result_unknown:' + command['operation_id'])

    def tool(self, name, arguments, **kwargs):
        return self.api('/api/tool', {'name': name, 'arguments': arguments}, **kwargs)

    def device(self, device, tool, arguments, **kwargs):
        return self.api('/api/ward/device', {'device_id': device, 'tool': tool, 'arguments': arguments}, **kwargs)

    def reject_unchanged(self, callback):
        before = ownership_projection(self.state())
        response = callback()
        after = ownership_projection(self.state())
        if before != after:
            raise AssertionError('refused_operation_changed_ownership_orders_transfers_or_navigation')
        return {'response': response, 'unchanged': after}

    def wait_sampling(self, case, *, minimum_sequence=None, after_ack=None, require_incidents=False):
        """Wait only for missing readback; never replay an environmental command."""
        deadline = min(time.monotonic() + 12, self.work_deadline)
        expected = set(self.specimens.values())
        barrier = minimum_sequence
        state = observation = None

        def fresh(scene):
            if not scene:
                return False
            if (scene.get('run_id'), scene.get('epoch')) != (self.run_id, self.epoch):
                raise RuntimeError('sampling_scene_identity_changed')
            sequence, stamp = scene.get('seq'), scene.get('wall_time')
            return (isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0
                    and isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                    and math.isfinite(stamp) and 0 <= time.time() - stamp <= 3)

        try:
            while time.monotonic() < deadline:
                self.remaining()
                state = self.state()
                observation = self.observation()
                ward = state['smart_ward']
                scene = ward.get('scene_sample') or {}
                for specimens in (ward.get('specimens', {}), observation.get('specimens', {})):
                    if set(specimens) - expected:
                        raise AssertionError('sampling_created_extra_specimen')
                if state.get('orders'):
                    raise AssertionError('sampling_created_unexpected_order')
                incidents = ward.get('incidents', [])
                if require_incidents:
                    incident_specimens = [item.get('specimen_id') for item in incidents]
                    if set(incident_specimens) - expected or len(incident_specimens) != len(set(incident_specimens)):
                        raise AssertionError('fresh_repeated_sampling_duplicated_or_unexpected_incident')
                observed_fresh, backend_fresh = fresh(observation), fresh(scene)
                if after_ack is not None:
                    observed_fresh = observed_fresh and observation.get('wall_time', 0) > after_ack['wall_time'] and observation.get('sim_time', -1) >= after_ack['sim_time']
                    backend_fresh = backend_fresh and scene.get('wall_time', 0) > after_ack['wall_time'] and scene.get('sim_time', -1) >= after_ack['sim_time']
                # Freeze the first post-ACK observation's sequence. Requiring the
                # backend to chase the latest file on every poll could starve.
                if barrier is None and observed_fresh:
                    barrier = observation['seq']
                complete = all(set(specimens) == expected for specimens in
                    (ward.get('specimens', {}), observation.get('specimens', {})))
                if (observed_fresh and backend_fresh and ward.get('scene_fresh') is True
                        and barrier is not None and min(observation['seq'], scene['seq']) >= barrier
                        and complete and (not require_incidents or len(incidents) == len(expected))):
                    proof = {'required_sequence': barrier, 'backend_sequence': scene['seq'],
                             'observation_sequence': observation['seq'], 'backend_wall_time': scene['wall_time'],
                             'observation_wall_time': observation['wall_time'], 'after_ack': after_ack}
                    write_json(self.directory / (case + '-sampling.json'), {'proof': proof, 'state': state, 'observation': observation})
                    self.record('sampling_synchronized', case=case, **proof)
                    return state, observation, proof
                self.record('sampling_wait', case=case, required_sequence=barrier,
                            backend_sequence=scene.get('seq'), observation_sequence=observation.get('seq'),
                            missing_specimens=sorted(expected - set(ward.get('specimens', {}))), incident_count=len(incidents))
                time.sleep(min(.5, self.remaining(), max(0, deadline - time.monotonic())))
            raise TimeoutError('scene_backend_sampling_not_synchronized:' + case)
        except BaseException as failure:
            path = self.directory / ('failed-sampling-' + uuid.uuid4().hex + '.json')
            with path.open('x', encoding='utf-8') as output:
                json.dump({'case': case, 'error': type(failure).__name__ + ':' + str(failure),
                           'wall_time': time.time(), 'state': state, 'observation': observation,
                           'required_sequence': barrier, 'after_ack': after_ack}, output, ensure_ascii=False, indent=2, allow_nan=False)
            self.record('sampling_failed', case=case, artifact=path.name, error=str(failure))
            raise

    def source_capacity_and_operation_replay(self):
        first = None
        acknowledgements = []
        for cabinet, label in [('hospital-cabinet-b', 'b1'), ('hospital-cabinet-b', 'b2'), ('hospital-cabinet-c', 'c1')]:
            command, initial, result = self.scene_command(cabinet, label)
            if result.get('status') != 'completed' or not result.get('result', {}).get('specimen_id'):
                raise AssertionError('real_insertion_failed:' + json.dumps(result))
            sid = result['result']['specimen_id']
            self.specimens[label] = sid
            acknowledgements.append((command['operation_id'], result['status'], result.get('ack'), sid))
            if first is None:
                first = command
                replay = self.api('/api/admin/smart-ward/command', command)
                if replay.get('operation_id') != initial.get('operation_id') or replay.get('result', {}).get('specimen_id') != sid:
                    raise AssertionError('same_operation_did_not_replay_same_specimen')
        command, _, result = self.scene_command('hospital-cabinet-b', 'b3-full')
        if result.get('status') != 'failed' or result.get('error') != 'source_cabinet_full':
            raise AssertionError('full_cabinet_did_not_reject_real_insertion')
        acknowledgements.append((command['operation_id'], result['status'], result.get('ack'), None))
        for operation_id, status, ack, sid in acknowledgements:
            if (not isinstance(ack, dict) or (ack.get('run_id'), ack.get('epoch'), ack.get('operation_id'), ack.get('status'))
                    != (self.run_id, self.epoch, operation_id, status)
                    or sid is not None and ack.get('result', {}).get('specimen_id') != sid
                    or any(isinstance(ack.get(key), bool) or not isinstance(ack.get(key), (int, float))
                           or not math.isfinite(ack[key]) for key in ('wall_time', 'sim_time'))):
                raise AssertionError('insertion_or_capacity_ack_identity_not_confirmed')
        if len(set(self.specimens.values())) != 3:
            raise AssertionError('three_insertions_did_not_acknowledge_three_unique_specimens')
        after_ack = {key: max(ack[key] for _, _, ack, _ in acknowledgements) for key in ('wall_time', 'sim_time')}
        _, _, proof = self.wait_sampling('source_capacity', after_ack=after_ack)
        return {'specimens': copy.deepcopy(self.specimens), 'replay_operation': first, 'full_result': result,
                'insertion_acks': [ack for _, _, ack, _ in acknowledgements], 'sampling': proof}

    def fresh_sampling_incident_deduplication(self):
        self.state()
        initial = self.observation()
        state, latest, proof = self.wait_sampling('incident_deduplication',
            minimum_sequence=initial['seq'] + 3, require_incidents=True)
        return {'first_sequence': initial['seq'], 'last_sequence': latest['seq'],
                'first_wall_time': initial['wall_time'], 'last_wall_time': latest['wall_time'],
                'incidents': state['smart_ward']['incidents'], 'order_count': 0, 'sampling': proof}

    def notification_retry_identity(self):
        self.guard()
        if not self.claimed_notifications:
            raise AssertionError('no_real_outbox_notification_observed')
        original = next(iter(self.claimed_notifications.values()))
        self.api('/api/admin/smart-ward/notifications/ack', {'notification_id': original['notification_id'],
            'status': 'retry', 'details': {'worker_id': self.worker, 'expected_run_id': self.run_id, 'reason': 'boundary_retry_test'}}, guarded=False)
        replay = self.claim_notification()
        if not replay or replay['notification_id'] != original['notification_id'] or replay['attempts'] != original['attempts'] + 1:
            raise AssertionError('retry_did_not_preserve_notification_identity')
        state = self.state()
        if state['orders'] or len(state['smart_ward']['incidents']) != 3:
            raise AssertionError('notification_retry_changed_business_entities')
        return {'original': original, 'retried': replay, 'no_model_request': self.audit_zero_model(state)}

    def duplicate_order_for_one_incident(self):
        state = self.state()
        incident = next(item for item in state['smart_ward']['incidents'] if item.get('specimen_id') == self.specimens['b1'])
        arguments = {'incident_id': incident['incident_id'], 'operation_id': self.prefix + '-order-b1'}
        created = self.tool('create_specimen_delivery', arguments)
        repeated = self.tool('create_specimen_delivery', arguments)
        redelivered = self.tool('create_specimen_delivery', {**arguments, 'operation_id': self.prefix + '-order-b1-repeat-notification'})
        ids = [item['orders'][0]['order_id'] for item in (created, repeated, redelivered)]
        state = self.state()
        if len(set(ids)) != 1 or len(state['orders']) != 1:
            raise AssertionError('duplicate_notification_or_operation_created_duplicate_order')
        self.orders['b1'] = ids[0]
        return {'created': created, 'same_operation': repeated, 'new_operation_same_incident': redelivered, 'order_count': 1}

    def wrong_cabinet(self):
        return self.reject_unchanged(lambda: self.device('hospital-cabinet-c', 'handoff_order',
            {'order_id': self.orders['b1'], 'operation_id': self.prefix + '-wrong-cabinet'}, expected_error='cabinet_order_station_mismatch'))

    def unknown_order(self):
        return self.reject_unchanged(lambda: self.device('hospital-cabinet-b', 'handoff_order',
            {'order_id': 'O-nonexistent-' + self.prefix, 'operation_id': self.prefix + '-unknown-order'}, expected_error='unknown_specimen_order'))

    def wrong_physical_location(self):
        state = self.state()
        station = 'station_b' if 'station_b' not in state.get('at_stations', []) else 'station_c'
        label, cabinet = ('b1', 'hospital-cabinet-b') if station == 'station_b' else ('c1', 'hospital-cabinet-c')
        point = state.get('physical_pose', {}).get('position', [])
        target = state['stations'][station]
        if len(point) < 2 or math.hypot(point[0] - target['x'], point[1] - target['y']) <= .5:
            raise AssertionError('cannot_independently_prove_wrong_physical_location')
        if label not in self.orders:
            incident = next(item for item in state['smart_ward']['incidents'] if item.get('specimen_id') == self.specimens[label])
            created = self.tool('create_specimen_delivery', {'incident_id': incident['incident_id'], 'operation_id': self.prefix + '-order-' + label})
            self.orders[label] = created['orders'][0]['order_id']
        result = self.reject_unchanged(lambda: self.device(cabinet, 'handoff_order',
            {'order_id': self.orders[label], 'operation_id': self.prefix + '-wrong-location'},
            expected_error='handoff_requires_fresh_scene_and_real_stationary_arrival'))
        result.update(actual_physical_pose=point, required_station=station, required_pose=target)
        return result

    def legacy_handoff_entry_denied(self):
        results = {}
        for name in ('pickup_package', 'confirm_delivery', 'create_delivery_orders', 'publish_delivery_plan'):
            results[name] = self.reject_unchanged(lambda name=name: self.tool(name,
                {'operation_id': self.prefix + '-legacy-' + name}, expected_error='smart_ward_requires_incident_and_device_handoff'))
        return results

    def operation_payload_conflict(self):
        return self.reject_unchanged(lambda: self.api('/api/admin/smart-ward/command',
            {'kind': 'insert_specimen', 'arguments': {'cabinet_id': 'hospital-cabinet-b', 'priority': 'urgent'},
             'operation_id': self.prefix + '-b1', 'expected_run_id': self.run_id}, expected_error='operation_id_conflict'))

    def invalid_brightness_and_unknown_device(self):
        invalid = self.reject_unchanged(lambda: self.device('hospital-light-301', 'set_brightness',
            {'brightness_percent': -1, 'operation_id': self.prefix + '-invalid-brightness'},
            expected_error='brightness_percent_must_be_0_to_100'))
        unknown = self.reject_unchanged(lambda: self.device('hospital-device-nonexistent', 'handoff_order',
            {'order_id': self.orders['b1'], 'operation_id': self.prefix + '-unknown-device'},
            expected_error='smart_ward_device_tool_not_allowed'))
        return {'invalid_brightness': invalid, 'unknown_device': unknown}

    def stop(self):
        if not self.start_attempted:
            return {'requested': False, 'reason': 'qualification_watch_never_started'}
        state = self.api(cleanup=True)
        if state.get('run_id') != self.run_id:
            raise RuntimeError('cleanup_refuses_to_stop_changed_run')
        result = self.api('/api/admin/stop-run', {'operation_id': self.prefix + '-stop',
            'expected_run_id': self.run_id, 'reason': 'boundary_qualification_finished'}, cleanup=True)
        end = min(time.monotonic() + 25, self.deadline)
        seen = set()
        while time.monotonic() < end:
            state = self.state(cleanup=True)
            observation = self.observation()
            if state.get('operator_stopped') and not state['smart_ward']['watch']['enabled'] and physical_stationary(state, observation, self.run_id, self.epoch):
                seen.add(observation.get('seq'))
                if len(seen) >= 3:
                    write_json(self.directory / 'final-state.json', state)
                    return {'requested': True, 'request_result': result, 'physical_stop_confirmed': True,
                            'new_scene_sequences': sorted(seen), 'watch_disabled': True}
            time.sleep(.5)
        raise TimeoutError('boundary_cleanup_physical_stop_not_confirmed')

    def run(self):
        error = cleanup_error = None
        active = 'preflight'
        try:
            self.preflight()
            for name in CASES:
                active = name
                started = time.time()
                details = getattr(self, name)()
                self.results[name] = {'status': 'passed', 'started_wall': started, 'ended_wall': time.time(), 'details': details}
                self.report['cases'] = self.results
                self.record('case_completed', name=name, result=self.results[name])
        except BaseException as failure:
            error = type(failure).__name__ + ':' + str(failure)
            if active in self.results:
                self.results[active] = {'status': 'failed', 'error': error, 'wall_time': time.time()}
            self.record('qualification_error', stage=active, error=error)
        finally:
            try:
                self.report['stop'] = self.stop()
            except BaseException as failure:
                cleanup_error = type(failure).__name__ + ':' + str(failure)
                self.record('cleanup_error', error=cleanup_error)
            if self.session:
                try:
                    self.report['model_audit'] = self.audit_zero_model(self.last_state)
                except BaseException as failure:
                    cleanup_error = (cleanup_error + ';' if cleanup_error else '') + 'model_audit:' + type(failure).__name__ + ':' + str(failure)
            self.report.update(run_id=self.run_id, epoch=self.epoch, session_key=self.session,
                ended_wall=time.time(), elapsed_wall_seconds=time.monotonic() - self.started,
                error=error, cleanup_error=cleanup_error,
                counts={status: sum(value['status'] == status for value in self.results.values()) for status in ('passed', 'failed', 'NA')},
                outcome='failed_or_incomplete' if error or cleanup_error else 'partial_NA',
                qualification_cases_passed=not error and not cleanup_error and all(self.results[name]['status'] == 'passed' for name in CASES),
                full_plan_acceptance_passed=False,
                artifacts={'driver_events': 'driver-events.jsonl', 'preflight': 'preflight.json', 'final_state': 'final-state.json'},
                source_sha256={str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (Path(__file__), self.root / 'mhs/smart_ward.py', self.root / 'mhs/smart_scene.py')})
            write_json(self.directory / 'report.json', self.report)
        print(json.dumps({'evidence': str(self.directory), 'outcome': self.report['outcome'],
                          'counts': self.report['counts'], 'error': error, 'cleanup_error': cleanup_error}, ensure_ascii=False))
        return 1 if error or cleanup_error else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wall-seconds', type=int, default=240)
    parser.add_argument('--output')
    arguments = parser.parse_args()
    if not 90 <= arguments.wall_seconds <= 240:
        parser.error('--wall-seconds must be 90..240; no unbounded qualification')
    driver = Boundaries(arguments)
    for selected in (signal.SIGTERM, signal.SIGINT):
        signal.signal(selected, lambda *_: setattr(driver, 'interrupted', True))
    raise SystemExit(driver.run())


if __name__ == '__main__':
    main()
