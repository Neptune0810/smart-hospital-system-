"""Real, bounded smart-ward fault qualification; never an Agent demonstration.

The project workbench must remain absent. Formal fault qualification suppresses
outbox delivery and rejects model requests. The driver only uses production
business / MHS operations and the finite admin fault queue. It never resets a
run, edits observations/ACKs, kills services or bypasses navigation gates.
"""
import argparse
import asyncio
import hashlib
import json
import math
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .smart_boundaries import Boundaries, DEVICES, ownership_projection, workbench_absent, write_json
from .smart_navigation import arrived, physical_stationary, recording_summary


ACK_QUALIFICATION_VERSION = 'smart-fault-ack-20261013.2'
ACK_NAVIGATION_LEG_WALL_SECONDS = 900
ACK_BUDGET_REVISION = {
    'reason': 'The retained real B arrival took approximately 772 wall seconds, so the prior 600-second independent ACK navigation leg is known to be insufficient.',
    'prior_leg_wall_seconds': 600,
    'observed_b_arrival_wall_seconds_approx': 772,
    'original_navigation_evidence_id': '072950-944c9c29',
    'evidence_source': 'root-audited retained original navigation evidence; not a new measurement by this driver',
    'reference_navigation_qualification_version': 'smart-nav-20261013.2',
    'unchanged': '1800-wall-second total maximum, 60-second cleanup reserve, handoff unknown after 60 wall seconds, 90-second original ACK hold, 20 fault commands, one specimen/order/navigation task, no models, routes/maps/speeds/arrival/stop thresholds; Agent budgets remain separate',
}


STATIONARY_CASES = ('source_cabinet_offline_refusal', 'source_cabinet_restore',
                    'actual_timeline_pause_stale_unknown', 'resume_three_new_samples')
ACK_CASES = ('verified_route_real_arrival_b', 'original_ack_withheld',
             'unknown_handoff_locks_motion', 'same_operation_reconciliation',
             'original_ack_release_commits_once', 'stopped_cargo_no_autoresume')


def native_error_contains(value, expected):
    return expected in json.dumps(value, ensure_ascii=False, sort_keys=True)


def stale_pause_evidence(state, observation, incident_id, baseline):
    """Old time/sequence and explicit unknown are necessary, not just no motion."""
    ward = state.get('smart_ward', {})
    incident = next((item for item in ward.get('incidents', []) if item.get('incident_id') == incident_id), {})
    devices = ward.get('devices', {})
    return bool(observation.get('seq') == baseline.get('seq')
        and observation.get('sim_time') == baseline.get('sim_time')
        and observation.get('wall_time') == baseline.get('wall_time')
        and time.time() - observation.get('wall_time', time.time()) > 3
        and ward.get('scene_fresh') is False
        and all(devices.get(ident, {}).get('fresh') is False and devices.get(ident, {}).get('available') is False
                for ident in DEVICES - {'hospital-robot-01'})
        and incident and incident.get('status') not in {'resolved', 'unresolved'})


class Faults(Boundaries):
    def __init__(self, arguments):
        if not arguments.output:
            root = Path(__file__).resolve().parents[1]
            arguments.output = str(root / 'runs/smart-ward-v1/acceptance' /
                ('faults-' + arguments.case + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]))
        super().__init__(arguments)
        self.work_deadline = self.deadline - (60 if arguments.case == 'ack' else 30)
        self.qualification = None
        self.hub = self.recorder = None
        self.nav_expected = False
        self.navigation = None
        self.fault_commands = 0
        self.handoff_operation = None
        self.cases = STATIONARY_CASES if arguments.case == 'stationary' else ACK_CASES
        self.results = {name: {'status': 'NA', 'reason': 'not_executed'} for name in self.cases}
        self.results.update({name: {'status': 'NA', 'reason': reason} for name, reason in {
            'robot_full': 'No third real pickup is attempted by this qualification.',
            'receiver_full': 'No six real receiver deliveries and seventh attempt are performed.',
            'backend_restart_during_motion': 'This qualification never restarts a process.',
            'wrong_specimen_ack_identity': 'Original ACK bytes are retained; no mismatched ACK is fabricated.',
        }.items()})
        self.report = {'scope': 'real Isaac / MHS fault qualification; NOT Agnes or full delivery acceptance',
            'case': arguments.case, 'started_wall': self.started_wall, 'cases': self.results,
            'limits': {'wall_seconds': arguments.wall_seconds, 'cleanup_reserve_seconds': 60 if arguments.case == 'ack' else 30,
                       'fault_commands': 20,
                       'navigation_leg_wall_seconds': ACK_NAVIGATION_LEG_WALL_SECONDS if arguments.case == 'ack' else 0,
                       'maximum_navigation_tasks': 1 if arguments.case == 'ack' else 0,
                       'maximum_specimens': 1, 'maximum_orders': 1, 'model_calls_allowed': 0},
            'full_plan_acceptance_passed': False}
        if arguments.case == 'ack':
            self.report.update(qualification_version=ACK_QUALIFICATION_VERSION,
                               budget_revision=dict(ACK_BUDGET_REVISION))

    def raw_api(self, path, arguments=None, *, cleanup=False):
        timeout = min(65 if path == '/api/mhs/routes' else 8, self.remaining(cleanup))
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

    def api(self, path='/api/state', arguments=None, *, cleanup=False, **kwargs):
        if arguments is not None and cleanup and not workbench_absent(self.root)['absent']:
            raise RuntimeError('cleanup_mutation_requires_project_workbench_absent')
        return super().api(path, arguments, cleanup=cleanup, **kwargs)

    def guard(self):
        self.remaining()
        if not workbench_absent(self.root)['absent']:
            raise RuntimeError('project_workbench_must_remain_absent')
        if self.session:
            self.audit_zero_model()
        if self.qualification and time.time() >= self.qualification['deadline_wall']:
            raise TimeoutError('formal_fault_qualification_expired')

    def state(self, *, cleanup=False):
        if not cleanup:
            self.guard()
        state = self.api(cleanup=cleanup)
        if self.run_id and state.get('run_id') != self.run_id:
            raise RuntimeError('fault_qualification_run_changed')
        ward = state.get('smart_ward', {})
        if self.qualification and not cleanup:
            q = ward.get('fault_qualification', {})
            if not q.get('active') or q.get('qualification_id') != self.qualification['qualification_id'] or q.get('session_key') != self.session:
                raise RuntimeError('formal_fault_qualification_identity_or_active_state_changed')
            if state.get('controller_session') != self.session:
                raise RuntimeError('fault_controller_changed')
        tasks = state.get('navigation_tasks', {})
        if len(tasks) > 1 or tasks and not self.nav_expected:
            raise RuntimeError('unexpected_navigation_scope_in_fault_qualification')
        if len(state.get('orders', {})) > 1 or len(ward.get('specimens', {})) > 1:
            raise RuntimeError('fault_qualification_business_scope_exceeded')
        self.last_state = state
        return state

    async def astate(self, *, cleanup=False):
        return await asyncio.to_thread(self.state, cleanup=cleanup)

    async def aapi(self, path='/api/state', arguments=None, **kwargs):
        return await asyncio.to_thread(self.api, path, arguments, **kwargs)

    async def connect(self):
        from .hub_client import HubClient
        self.hub = HubClient(record=self.directory / 'hub-messages.jsonl')
        await asyncio.wait_for(self.hub.__aenter__(), min(20, self.remaining()))

    async def rpc(self, method, arguments):
        # hub/call is a mutation even when the device will refuse it.
        if method in {'hub/call', 'hub/stop'}:
            await asyncio.to_thread(self.guard)
        return await asyncio.wait_for(self.hub.rpc(method, arguments), min(20, self.remaining()))

    async def finish(self, receipt, timeout=20):
        if not receipt.get('accepted') or not receipt.get('job'):
            raise RuntimeError('native_call_not_accepted:' + json.dumps(receipt))
        return await asyncio.wait_for(self.hub.finish(receipt['job'], min(timeout, self.remaining())), min(timeout + 1, self.remaining()))

    async def begin(self):
        absent = workbench_absent(self.root)
        state = await self.astate()
        ward = state.get('smart_ward', {})
        self.run_id, self.epoch = state.get('run_id'), ward.get('epoch')
        observation = self.observation()
        checks = {'smart_mode': state.get('protocol') == 'smart-ward-v1', 'workbench_absent': absent['absent'],
            'unclaimed_empty': not state.get('controller_session') and not state.get('orders') and not ward.get('specimens') and not ward.get('incidents'),
            'watch_off': not ward.get('watch', {}).get('enabled'), 'not_stopped': not state.get('operator_stopped'),
            'no_prior_qualification': not ward.get('fault_qualification') and not ward.get('navigation_qualification'),
            'no_prior_navigation': not state.get('navigation_tasks'), 'no_budget': not state.get('mhs_budget'),
            'actual_b_navigation_needed': self.args.case != 'ack' or 'station_b' not in state.get('at_stations', []),
            'no_recovery': not any(ward.get(key) for key in ('recovery_required', 'resume_required', 'navigation_recovery_required')),
            'eight_fresh_available': set(ward.get('devices', {})) == DEVICES and all(
                value.get('available') is True and value.get('fresh') is True for value in ward.get('devices', {}).values()),
            'normal_lights': all(ward.get('devices', {}).get('hospital-light-' + suffix, {}).get('brightness_percent', 0) >= 50 for suffix in ('301', '303')),
            'nav_ready': state.get('navigation_ready') is True, 'fresh_scene': ward.get('scene_fresh') is True,
            'physical_stop': physical_stationary(state, observation, self.run_id, self.epoch)}
        write_json(self.directory / 'preflight.json', {'checks': checks, 'workbench': absent, 'state': state, 'observation': observation})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:' + ','.join(key for key, value in checks.items() if not value))
        self.session = 'qualification:faults:' + self.run_id + ':' + uuid.uuid4().hex[:12]
        self.audit_zero_model(state)
        self.start_attempted = True
        started = await self.aapi('/api/admin/smart-ward/fault-qualification/begin',
            {'session_key': self.session, 'expected_run_id': self.run_id, 'expected_epoch': self.epoch,
             'operation_id': self.prefix + '-formal-begin', 'duration_seconds': self.args.wall_seconds})
        self.qualification = started.get('qualification')
        if not self.qualification or not self.qualification.get('active') or self.qualification.get('session_key') != self.session:
            raise RuntimeError('formal_fault_qualification_not_confirmed')
        await self.aapi('/api/admin/smart-ward/start', {'session_key': self.session, 'expected_run_id': self.run_id, 'operation_id': self.prefix + '-watch'})
        self.watch_started = True
        self.report['decision_gate_refusal'] = await self.aapi('/api/mhs/decision',
            {'decision_id': self.prefix + '-qualification-gate-probe', 'expected_run_id': self.run_id, 'session_key': self.session},
            expected_error='fault_qualification_has_no_model_calls')
        await self.connect()
        if self.args.case == 'ack':
            self.recorder = __import__('mhs.record', fromlist=['Recorder']).Recorder(self.directory).start()
        write_json(self.directory / 'begin.json', started)

    def identity(self):
        return {'qualification_id': self.qualification['qualification_id'], 'session_key': self.session,
                'expected_run_id': self.run_id, 'expected_epoch': self.epoch}

    async def fault(self, kind, arguments, label):
        if self.fault_commands >= 20:
            raise RuntimeError('driver_fault_command_limit')
        self.fault_commands += 1  # an unknown response still consumes the driver allowance
        command = {**self.identity(), 'kind': kind, 'arguments': arguments, 'operation_id': self.prefix + '-fault-' + label}
        accepted = await self.aapi('/api/admin/smart-ward/fault-qualification/command', command)
        end = min(time.monotonic() + 12, self.work_deadline)
        while time.monotonic() < end:
            await asyncio.to_thread(self.guard)
            result = await self.aapi('/api/admin/smart-ward/fault-qualification/operation?id=' + urllib.parse.quote(command['operation_id']))
            if result.get('status') == 'failed':
                raise RuntimeError('actual_fault_command_failed:' + json.dumps(result, ensure_ascii=False))
            if result.get('status') == 'completed':
                self.record('actual_fault_ack', accepted=accepted, result=result)
                return result
            await asyncio.sleep(.1)
        raise TimeoutError('fault_command_ack_unknown_no_retry:' + command['operation_id'])

    async def wait_state(self, predicate, timeout=20, *, cleanup=False):
        end = min(time.monotonic() + timeout, self.deadline if cleanup else self.work_deadline)
        while time.monotonic() < end:
            state = await self.astate(cleanup=cleanup)
            if predicate(state):
                return state
            await asyncio.sleep(.2)
        raise TimeoutError('required_real_fault_state_not_observed')

    async def create_specimen_order(self):
        _, _, insertion = await asyncio.to_thread(self.scene_command, 'hospital-cabinet-b', 'real-b-specimen')
        if insertion.get('status') != 'completed':
            raise RuntimeError('real_specimen_insertion_failed')
        self.specimen_id = insertion['result']['specimen_id']
        state = await self.wait_state(lambda value: any(item.get('specimen_id') == self.specimen_id for item in value['smart_ward']['incidents']))
        incident = next(item for item in state['smart_ward']['incidents'] if item.get('specimen_id') == self.specimen_id)
        self.incident_id = incident['incident_id']
        order = await asyncio.to_thread(self.tool, 'create_specimen_delivery',
            {'incident_id': self.incident_id, 'operation_id': self.prefix + '-order'})
        self.order_id = order['orders'][0]['order_id']
        database = self.root / 'runs/smart-ward-v1/state.sqlite'
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            outbox_count = connection.execute('SELECT COUNT(*) FROM ward_notifications WHERE run_id=?', (self.run_id,)).fetchone()[0]
        finally:
            connection.close()
        suppressed = await self.aapi('/api/admin/smart-ward/notifications/claim',
            {'worker_id': self.worker, 'expected_run_id': self.run_id})
        if outbox_count != 0 or suppressed is not None:
            raise AssertionError('formal_qualification_did_not_suppress_real_incident_outbox')
        return {'scene_insertion': insertion, 'incident': incident, 'order': order,
                'formal_notification_suppression': {'actual_run_outbox_count': outbox_count, 'claim_result': suppressed}}

    async def device_state(self, device, predicate, timeout=15):
        end = min(time.monotonic() + timeout, self.work_deadline)
        while time.monotonic() < end:
            await self.astate()
            rows = (await self.rpc('hub/devices', {})).get('devices', [])
            item = next((row for row in rows if row.get('id') == device), {})
            if predicate(item):
                return item
            await asyncio.sleep(.2)
        raise TimeoutError('native_device_feedback_not_observed:' + device)

    async def native_refusal(self, device, tool, arguments, expected):
        before = ownership_projection(await self.astate())
        try:
            receipt = await self.rpc('hub/call', {'device': device, 'tool': tool, 'arguments': arguments})
            result = await self.finish(receipt, 12) if receipt.get('accepted') and receipt.get('job') else receipt
        except RuntimeError as error:
            receipt, result = None, {'rpc_error': str(error)}
        if not native_error_contains(result, expected) or result.get('status') == 'done':
            raise AssertionError('native_refusal_reason_not_proved:' + expected + ':' + json.dumps(result, ensure_ascii=False))
        after = ownership_projection(await self.astate())
        if before != after:
            raise AssertionError('refused_native_call_changed_ownership_or_navigation')
        return {'receipt': receipt, 'native_result': result, 'expected_reason': expected, 'unchanged_ownership': after}

    def pass_case(self, name, details):
        self.results[name] = {'status': 'passed', 'wall_time': time.time(), 'details': details}
        self.record('case_completed', name=name, details=details)

    async def stationary(self):
        self.active_case = STATIONARY_CASES[0]
        created = await self.create_specimen_order()
        offline = await self.fault('set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': False}, 'offline')
        state = await self.wait_state(lambda value: value['smart_ward']['scene_fresh'] and value['smart_ward']['devices']['hospital-cabinet-b'].get('available') is False)
        native = await self.device_state('hospital-cabinet-b', lambda item: item.get('state', {}).get('values', {}).get('fresh') is True and item.get('state', {}).get('values', {}).get('available') is False)
        refusal = await self.native_refusal('hospital-cabinet-b', 'handoff_order',
            {'order_id': self.order_id, 'operation_id': self.prefix + '-offline-handoff'}, 'device_unavailable_or_stale')
        self.pass_case(self.active_case, {'created': created, 'actual_offline_ack': offline, 'native_device': native,
            'refusal': refusal, 'rejection_layer': 'production MHS guard using fresh actual unavailable cabinet feedback; not a successful scene handoff'})
        self.active_case = STATIONARY_CASES[1]
        restored = await self.fault('set_cabinet_availability', {'cabinet_id': 'hospital-cabinet-b', 'available': True}, 'restore')
        state = await self.wait_state(lambda value: value['smart_ward']['scene_fresh'] and value['smart_ward']['devices']['hospital-cabinet-b'].get('available') is True)
        native = await self.device_state('hospital-cabinet-b', lambda item: item.get('state', {}).get('values', {}).get('fresh') is True and item.get('state', {}).get('values', {}).get('available') is True)
        self.pass_case(self.active_case, {'actual_restore_ack': restored, 'native_device': native, 'holder': state['smart_ward']['specimens'][self.specimen_id]})
        self.active_case = STATIONARY_CASES[2]
        await self.aapi('/api/admin/smart-ward/command', {'kind': 'set_brightness', 'arguments': {'room_id': 'room301', 'brightness_percent': 10}, 'operation_id': self.prefix + '-dim', 'expected_run_id': self.run_id})
        state = await self.wait_state(lambda value: any(item.get('kind') == 'low_light' and item.get('room_id') == 'room301' for item in value['smart_ward']['incidents']))
        lighting = next(item for item in state['smart_ward']['incidents'] if item.get('kind') == 'low_light')
        pause = await self.fault('pause_simulation', {'seconds': 8}, 'pause')
        if pause.get('result', {}).get('actual_playing') is not False:
            raise AssertionError('actual_timeline_pause_not_acknowledged')
        baseline = self.observation()
        started_wall = pause['result']['paused']['started_wall']
        stale = None
        while time.time() - started_wall < 5.3:
            state = await self.astate()
            observation = self.observation()
            if time.time() - started_wall > 3.4 and stale_pause_evidence(state, observation, lighting['incident_id'], baseline):
                stale = {'state': state, 'observation': observation, 'observed_wall': time.time()}
            await asyncio.sleep(.15)
        if stale is None or time.time() - started_wall >= 7.5:
            raise AssertionError('five_second_actual_pause_stale_unknown_window_not_proved')
        self.pass_case(self.active_case, {'actual_pause_ack': pause, 'baseline': baseline, 'stale_unknown_evidence': stale,
            'actual_pause_wall_seconds_before_resume': time.time() - started_wall})
        self.active_case = STATIONARY_CASES[3]
        resume = await self.fault('resume_simulation', {}, 'resume')
        if resume.get('result', {}).get('actual_playing') is not True or resume.get('result', {}).get('resumes_navigation') is not False:
            raise AssertionError('actual_resume_or_no_navigation_resume_not_confirmed')
        sequences = set()
        end = min(time.monotonic() + 15, self.work_deadline)
        while time.monotonic() < end:
            state = await self.astate()
            observation = self.observation()
            if state['smart_ward']['scene_fresh'] and observation.get('seq', 0) > baseline['seq']:
                sequences.add(observation['seq'])
                if len(sequences) >= 3:
                    break
            await asyncio.sleep(.2)
        if len(sequences) < 3:
            raise AssertionError('resume_did_not_produce_three_new_actual_samples')
        # Restore the actual local lamp and require genuine recovery afterwards.
        await self.aapi('/api/admin/smart-ward/command', {'kind': 'set_brightness', 'arguments': {'room_id': 'room301', 'brightness_percent': 60}, 'operation_id': self.prefix + '-restore-light', 'expected_run_id': self.run_id})
        recovered = await self.wait_state(lambda value: any(item.get('incident_id') == lighting['incident_id'] and item.get('status') == 'resolved' for item in value['smart_ward']['incidents']))
        if recovered.get('navigation_tasks'):
            raise AssertionError('timeline_resume_started_navigation')
        self.pass_case(self.active_case, {'actual_resume_ack': resume, 'new_sequences': sorted(sequences), 'fresh_state_after_actual_light_restore': recovered})

    async def publish_plan(self):
        state = await self.astate()
        steps = [{'action': 'navigate', 'incident_id': self.incident_id, 'station_id': 'station_b'},
                 {'action': 'pickup', 'incident_id': self.incident_id},
                 {'action': 'navigate', 'incident_id': self.incident_id, 'station_id': 'lab'},
                 {'action': 'deliver', 'incident_id': self.incident_id},
                 {'action': 'standby', 'station_id': 'lab'}]
        return await asyncio.to_thread(self.tool, 'publish_service_plan', {'planning_version': state['planning_version'],
            'steps': steps, 'reason': '故障验收：按正常路线到B取件，核验原交接ACK后停止并保留载货，不执行配送。', 'operation_id': self.prefix + '-plan'})

    async def arrival_b(self):
        plan = await self.publish_plan()
        options = await self.aapi('/api/mhs/routes', {'station_id': 'station_b'})
        routes = options.get('routes', [])
        if not routes or any(row.get('validation') != 'nav2_verified' for row in routes):
            raise RuntimeError('fresh_verified_route_to_b_required')
        route = min(routes, key=lambda row: (row.get('distance_m', row.get('length_m', math.inf)), row['route_id']))
        await self.device_state('hospital-robot-01', lambda item: item.get('available') and item.get('state', {}).get('values', {}).get('stop_confirmed') and not item.get('busy', {}).get('chassis'))
        self.nav_expected = True
        started = time.monotonic()
        receipt = await self.rpc('hub/call', {'device': 'hospital-robot-01', 'tool': 'navigate_route',
            'arguments': {'route_id': route['route_id'], 'operation_id': self.prefix + '-route-b'}})
        if not receipt.get('accepted') or not receipt.get('job'):
            raise RuntimeError('production_mhs_navigation_not_accepted')
        end = min(started + ACK_NAVIGATION_LEG_WALL_SECONDS, self.work_deadline)
        while time.monotonic() < end:
            state = await self.astate()
            observation = self.observation()
            tasks = state.get('navigation_tasks', {})
            if len(tasks) == 1:
                task_id, task = next(iter(tasks.items()))
                if task.get('route_id') != route['route_id'] or task.get('goal_station') != 'station_b':
                    raise RuntimeError('real_navigation_task_identity_mismatch')
                if task.get('status') in {'failed', 'canceled', 'unknown'}:
                    raise RuntimeError('real_navigation_' + task['status'])
                native = self.hub.jobs.get(receipt['job'])
                if arrived(state, observation, 'station_b', task_id, self.run_id, self.epoch) and native:
                    if native.get('status') != 'done':
                        raise RuntimeError('native_navigation_did_not_complete_successfully')
                    self.navigation = {'route': route, 'task_id': task_id, 'receipt': receipt, 'result': native}
                    return {'plan': plan, 'options': options, 'navigation': self.navigation, 'state': state, 'observation': observation}
            await asyncio.sleep(.25)
        raise TimeoutError('real_b_arrival_not_proved_within_' + str(ACK_NAVIGATION_LEG_WALL_SECONDS) + '_wall_seconds')

    async def ack(self):
        self.active_case = ACK_CASES[0]
        created = await self.create_specimen_order()
        arrival = await self.arrival_b()
        self.pass_case(self.active_case, {'created': created, 'real_arrival': arrival})
        self.active_case = ACK_CASES[1]
        self.handoff_operation = self.prefix + '-pickup-original'
        await self.device_state('hospital-cabinet-b', lambda item: item.get('state', {}).get('values', {}).get('available') and not item.get('busy', {}).get('actuator'))
        receipt = await self.rpc('hub/call', {'device': 'hospital-cabinet-b', 'tool': 'handoff_order',
            'arguments': {'order_id': self.order_id, 'operation_id': self.handoff_operation}})
        if not receipt.get('accepted') or not receipt.get('job'):
            raise RuntimeError('original_real_handoff_not_accepted')
        end = min(time.monotonic() + 8, self.work_deadline)
        prepared = None
        while time.monotonic() < end:
            state = await self.astate()
            prepared = state['smart_ward']['operations'].get(self.handoff_operation)
            if prepared:
                break
            await asyncio.sleep(.02)
        if not prepared or prepared.get('status') != 'pending':
            raise RuntimeError('missed_pending_handoff_window_no_retry')
        # If the original ACK wins this race the fault API refuses. Preserve the
        # failed attempt; never remove it or generate a second handoff operation.
        hold = await self.fault('hold_ack', {'target_operation_id': self.handoff_operation, 'hold_seconds': 90}, 'hold-original')
        withheld_path = self.root / 'runs/smart-ward-v1/scene/withheld-acks' / (self.handoff_operation + '.json')
        end = min(time.monotonic() + 30, self.work_deadline)
        raw_ack = None
        while time.monotonic() < end:
            state = await self.astate()
            if withheld_path.is_file():
                raw_ack = withheld_path.read_bytes()
                break
            await asyncio.sleep(.1)
        if raw_ack is None:
            raise RuntimeError('actual_original_ack_not_withheld')
        original_ack = json.loads(raw_ack)
        scene = self.observation()
        if original_ack.get('status') != 'completed' or original_ack.get('operation_id') != self.handoff_operation or scene.get('specimens', {}).get(self.specimen_id, {}).get('location') != 'robot':
            raise AssertionError('withheld_ack_did_not_describe_real_committed_pickup')
        if state['orders'][self.order_id].get('loaded') or state['smart_ward']['operations'][self.handoff_operation].get('status') == 'completed':
            raise AssertionError('business_committed_before_original_ack_release')
        (self.directory / 'original-withheld-ack.json').write_bytes(raw_ack)
        original_sha = hashlib.sha256(raw_ack).hexdigest()
        self.pass_case(self.active_case, {'hold_ack': hold, 'original_ack': original_ack, 'original_ack_sha256': original_sha, 'scene': scene})
        self.active_case = ACK_CASES[2]
        deadline = min(prepared['created_wall'] + 67, time.time() + self.remaining())
        unknown = None
        while time.time() < deadline:
            state = await self.astate()
            item = state['smart_ward']['operations'][self.handoff_operation]
            if item.get('status') == 'unknown' and time.time() - prepared['created_wall'] > 60:
                unknown = state
                break
            await asyncio.sleep(.25)
        if unknown is None:
            raise RuntimeError('unknown_handoff_after_60_wall_seconds_not_observed')
        before = ownership_projection(unknown)
        refused = await self.aapi('/api/mhs/navigate', {'route_id': self.navigation['route']['route_id'],
            'operation_id': self.prefix + '-blocked-route-api'}, expected_error='pending_handoff_must_be_reconciled_before_motion')
        native_refused = await self.native_refusal('hospital-robot-01', 'navigate_route',
            {'route_id': self.navigation['route']['route_id'], 'operation_id': self.prefix + '-blocked-route-mhs'},
            'pending_handoff_must_be_reconciled_before_motion')
        state = await self.astate()
        if ownership_projection(state) != before or not physical_stationary(state, self.observation(), self.run_id, self.epoch):
            raise AssertionError('unknown_transfer_motion_lock_did_not_hold_actual_robot')
        self.pass_case(self.active_case, {'unknown_state': unknown, 'production_api_refusal': refused, 'mhs_refusal': native_refused, 'still_state': state})
        self.active_case = ACK_CASES[3]
        replay = await asyncio.to_thread(self.device, 'hospital-cabinet-b', 'handoff_order',
            {'order_id': self.order_id, 'operation_id': self.handoff_operation})
        state = await self.astate()
        handoffs = [item for item in state['smart_ward']['operations'].values() if item['command']['kind'] == 'handoff']
        if replay.get('operation_id') != self.handoff_operation or replay.get('status') != 'unknown' or len(handoffs) != 1 or state['orders'][self.order_id].get('loaded'):
            raise AssertionError('same_operation_unknown_reconciliation_created_extra_transfer')
        self.pass_case(self.active_case, {'same_operation_production_device_api': replay, 'handoff_count': len(handoffs), 'state': state})
        self.active_case = ACK_CASES[4]
        released = await self.fault('release_ack', {'target_operation_id': self.handoff_operation}, 'release-original')
        state = await self.wait_state(lambda value: value['smart_ward']['operations'][self.handoff_operation].get('status') == 'completed' and value['orders'][self.order_id].get('loaded') is True)
        visible_path = self.root / 'runs/smart-ward-v1/scene/acks' / (self.handoff_operation + '.json')
        visible = visible_path.read_bytes()
        (self.directory / 'released-original-ack.json').write_bytes(visible)
        if hashlib.sha256(visible).hexdigest() != original_sha or state['smart_ward']['specimens'][self.specimen_id].get('location') != 'robot':
            raise AssertionError('release_did_not_preserve_original_ack_bytes_and_one_robot_holder')
        native = await self.finish(receipt, 12)
        if native.get('status') != 'done':
            raise AssertionError('original_mhs_handoff_did_not_complete_after_release')
        await self.device_state('hospital-cabinet-b', lambda item: not item.get('busy', {}).get('actuator'))
        replay_receipt = await self.rpc('hub/call', {'device': 'hospital-cabinet-b', 'tool': 'handoff_order',
            'arguments': {'order_id': self.order_id, 'operation_id': self.handoff_operation}})
        replay_native = await self.finish(replay_receipt, 12)
        final = await self.astate()
        if replay_native.get('status') != 'done' or len(final['smart_ward']['transfers']) != 1 or final.get('cargo_count') != 1 or len(final['orders']) != 1:
            raise AssertionError('native_same_operation_replay_did_not_commit_exactly_one_ownership')
        database = self.root / 'runs/smart-ward-v1/state.sqlite'
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            events = [json.loads(row[0]) for row in connection.execute('SELECT body FROM events WHERE run_id=?', (self.run_id,))]
        finally:
            connection.close()
        pickups = [item for item in events if item.get('kind') == 'package_picked_up' and item.get('order_id') == self.order_id]
        if len(pickups) != 1:
            raise AssertionError('pickup_business_commit_count_was_not_one')
        self.pass_case(self.active_case, {'release_ack': released, 'original_sha256': original_sha,
            'visible_sha256': hashlib.sha256(visible).hexdigest(), 'original_native_result': native,
            'same_operation_native_replay': replay_native, 'pickup_commit_events': pickups, 'state': final})

    async def cleanup(self):
        result = {'qualification_started': self.start_attempted}
        if self.start_attempted:
            state = await self.aapi(cleanup=True)
            if state.get('run_id') != self.run_id:
                raise RuntimeError('cleanup_refuses_to_mutate_changed_run')
            if not self.qualification:
                q = state.get('smart_ward', {}).get('fault_qualification', {})
                if q.get('session_key') == self.session:
                    self.qualification = q
            if self.qualification:
                result['end'] = await self.aapi('/api/admin/smart-ward/fault-qualification/end',
                    {**self.identity(), 'operation_id': self.prefix + '-formal-end', 'reason': 'fault_driver_finished'}, cleanup=True)
            else:
                raise RuntimeError('formal_begin_outcome_unknown_without_matching_identity_no_cleanup_mutation')
            seen = set()
            end = min(time.monotonic() + 25, self.deadline)
            while time.monotonic() < end:
                state = await self.astate(cleanup=True)
                observation = self.observation()
                fault_state_path = self.root / 'runs/smart-ward-v1/scene/fault-state.json'
                fault_state = json.loads(fault_state_path.read_text()) if fault_state_path.exists() else {}
                if state.get('operator_stopped') and not state['smart_ward']['watch']['enabled'] and physical_stationary(state, observation, self.run_id, self.epoch) and fault_state.get('active') is False and not fault_state.get('holds') and not fault_state.get('paused'):
                    seen.add(observation['seq'])
                    if len(seen) >= 3:
                        result.update(physical_stop_confirmed=True, new_scene_sequences=sorted(seen), state=state, scene_fault_state=fault_state)
                        write_json(self.directory / 'final-state.json', state)
                        break
                await asyncio.sleep(.3)
            if not result.get('physical_stop_confirmed'):
                raise TimeoutError('formal_end_restoration_and_physical_stop_not_confirmed')
            if self.args.case == 'ack' and self.results[ACK_CASES[4]]['status'] == 'passed':
                if state.get('cargo_count') != 1 or state['smart_ward']['specimens'][self.specimen_id].get('location') != 'robot' or len(state['navigation_tasks']) != 1:
                    raise AssertionError('stop_did_not_retain_one_cargo_and_original_navigation_only')
                self.pass_case(ACK_CASES[5], {'stopped_state': state, 'restored_fault_controls': fault_state, 'new_scene_sequences': sorted(seen)})
        return result

    async def run_async(self):
        error = cleanup_error = None
        self.active_case = 'preflight'
        try:
            await self.begin()
            await (self.stationary() if self.args.case == 'stationary' else self.ack())
        except BaseException as failure:
            error = type(failure).__name__ + ':' + str(failure)
            if self.active_case in self.results:
                self.results[self.active_case] = {'status': 'failed', 'error': error, 'wall_time': time.time()}
            self.record('qualification_error', stage=self.active_case, error=error)
        finally:
            try:
                self.report['cleanup'] = await self.cleanup()
            except BaseException as failure:
                cleanup_error = type(failure).__name__ + ':' + str(failure)
                self.record('cleanup_error', error=cleanup_error)
            if self.hub:
                try:
                    await asyncio.wait_for(self.hub.__aexit__(None, None, None), 4)
                except Exception as failure:
                    cleanup_error = (cleanup_error + ';' if cleanup_error else '') + 'hub_close:' + repr(failure)
            if self.recorder:
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.recorder.stop), min(25, self.remaining(cleanup=True)))
                    self.report['recording'] = recording_summary(self.directory)
                except Exception as failure:
                    cleanup_error = (cleanup_error + ';' if cleanup_error else '') + 'recording:' + repr(failure)
            if self.session:
                try:
                    self.report['model_audit'] = self.audit_zero_model(self.last_state)
                except Exception as failure:
                    cleanup_error = (cleanup_error + ';' if cleanup_error else '') + 'model_audit:' + repr(failure)
            self.report.update(run_id=self.run_id, epoch=self.epoch, session_key=self.session, qualification=self.qualification,
                ended_wall=time.time(), elapsed_wall_seconds=time.monotonic() - self.started,
                fault_commands_submitted=self.fault_commands, error=error, cleanup_error=cleanup_error,
                counts={status: sum(value['status'] == status for value in self.results.values()) for status in ('passed', 'failed', 'NA')},
                outcome='failed_or_incomplete' if error or cleanup_error else 'partial_NA',
                requested_case_passed=not error and not cleanup_error and all(self.results[name]['status'] == 'passed' for name in self.cases),
                source_sha256={name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in
                    ('mhs/smart_faults.py', 'mhs/smart_boundaries.py', 'mhs/smart_navigation.py',
                     'mhs/smart_ward.py', 'mhs/smart_scene.py', 'mhs/bridge.py', 'mhs/hub_client.py', 'mhs/record.py',
                     'configs/stations.json', 'configs/smart-ward-v1.json')})
            write_json(self.directory / 'report.json', self.report)
        print(json.dumps({'evidence': str(self.directory), 'outcome': self.report['outcome'], 'counts': self.report['counts'],
                          'error': error, 'cleanup_error': cleanup_error}, ensure_ascii=False))
        return 1 if error or cleanup_error else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('stationary', 'ack'), default='stationary')
    parser.add_argument('--wall-seconds', type=int)
    parser.add_argument('--output')
    arguments = parser.parse_args()
    maximum = 240 if arguments.case == 'stationary' else 1800
    arguments.wall_seconds = arguments.wall_seconds or maximum
    if not (90 if arguments.case == 'stationary' else 180) <= arguments.wall_seconds <= maximum:
        parser.error('--wall-seconds must be 90..240 for stationary or 180..1800 for ack')
    driver = Faults(arguments)
    for selected in (signal.SIGTERM, signal.SIGINT):
        signal.signal(selected, lambda *_: setattr(driver, 'interrupted', True))
    raise SystemExit(asyncio.run(driver.run_async()))


if __name__ == '__main__':
    main()
