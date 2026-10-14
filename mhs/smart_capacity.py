"""Bounded, real same-cabinet robot-full qualification; never an Agent demo.

On a new empty run, navigate to C through the production route/MHS interfaces,
pick up two actual specimens, insert a third into the now-empty C cabinet and
prove that its pickup (including one same-operation retry) is refused. No
teleport, fabricated occupancy/ACK, reset, process restart or model prompt.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import math
import re
import signal
import time
import uuid
from pathlib import Path

from .smart_boundaries import DEVICES, ownership_projection, workbench_absent, write_json
from .smart_faults import Faults, ACK_NAVIGATION_LEG_WALL_SECONDS
from .smart_navigation import arrived, physical_stationary, recording_summary


VERSION = 'smart-capacity-20261013.1'
STATION = 'station_c'
CABINET = 'hospital-cabinet-c'
MAX_WALL_SECONDS = 1800
CLEANUP_SECONDS = 60
CASES = ('verified_route_real_arrival_c', 'two_real_scene_ack_pickups',
         'third_actual_specimen_and_order', 'robot_full',
         'same_operation_refusal_replay', 'stopped_full_cargo_no_autoresume')
SOURCES = ('mhs/smart_capacity.py', 'mhs/smart_faults.py', 'mhs/smart_boundaries.py',
           'mhs/smart_navigation.py', 'mhs/smart_ward.py', 'mhs/engine_v2.py',
           'mhs/smart_scene.py', 'mhs/bridge.py', 'mhs/hub_client.py', 'mhs/record.py',
           'configs/stations.json', 'configs/smart-ward-v1.json', 'configs/nav2.yaml')


def exact_capacity_error(value):
    """Do not mistake wrong-location, timeout or scene-only errors for this gate."""
    return bool(re.search(r'(?<![A-Za-z0-9_])capacity_exceeded(?![A-Za-z0-9_])',
                          json.dumps(value, ensure_ascii=False, sort_keys=True)))


def pickup_proved(state, observation, ack, operation_id, order_id, specimen_id,
                  run_id, epoch, before_sequence):
    ward = state.get('smart_ward', {})
    sample = ward.get('scene_sample') or {}
    result = ack.get('result', {})
    transfer = result.get('transfer', result)
    tid = transfer.get('transfer_id')
    prepared = ward.get('operations', {}).get(operation_id, {})
    committed = ward.get('transfers', {}).get(tid, {})
    observed = observation.get('transfers', {}).get(tid, {})
    order = state.get('orders', {}).get(order_id, {})
    stamps = (ack.get('wall_time'), sample.get('wall_time'), observation.get('wall_time'))
    return bool(ack.get('run_id') == run_id and ack.get('epoch') == epoch
        and ack.get('operation_id') == operation_id and ack.get('status') == 'completed'
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in stamps)
        and sample.get('run_id') == run_id and sample.get('epoch') == epoch
        and isinstance(sample.get('seq'), int) and sample['seq'] > before_sequence
        and sample['wall_time'] >= ack['wall_time'] and observation['wall_time'] >= ack['wall_time']
        and transfer.get('order_id') == order_id and transfer.get('specimen_id') == specimen_id
        and transfer.get('cabinet_id') == CABINET and transfer.get('direction') == 'pickup'
        and transfer.get('location') == 'robot' and tid
        and prepared.get('status') == 'completed' and prepared.get('transfer_id') == tid
        and committed.get('status') == 'committed' and committed.get('operation_id') == operation_id
        and committed.get('specimen_id') == specimen_id and committed.get('location') == 'robot'
        and observed.get('operation_id') == operation_id and observed.get('order_id') == order_id
        and observed.get('specimen_id') == specimen_id and observed.get('location') == 'robot'
        and order.get('loaded') is True and order.get('status') == 'onboard'
        and order.get('specimen_id') == specimen_id
        and ward.get('specimens', {}).get(specimen_id, {}).get('location') == 'robot'
        and observation.get('specimens', {}).get(specimen_id, {}).get('location') == 'robot'
        and STATION in state.get('at_stations', [])
        and physical_stationary(state, observation, run_id, epoch))


def ownership_proved(state, observation, specimens, orders):
    """Both scene and business must agree; no business-only cargo shortcut."""
    expected = {specimens['c1']: 'robot', specimens['c2']: 'robot', specimens['c3']: CABINET}
    ward = state.get('smart_ward', {})
    for source in (ward.get('specimens', {}), observation.get('specimens', {})):
        if set(source) != set(expected) or any(source[sid].get('location') != loc for sid, loc in expected.items()):
            return False
    if set(state.get('orders', {})) != set(orders.values()) or state.get('capacity') != 2 or state.get('cargo_count') != 2:
        return False
    for label, oid in orders.items():
        order = state['orders'][oid]
        if order.get('specimen_id') != specimens[label] or order.get('loaded') is not (label != 'c3'):
            return False
    return all(sorted(source.get('devices', {}).get(CABINET, {}).get('specimens', [])) == [specimens['c3']]
               for source in (ward, observation))


def scene_files(root):
    result = {}
    scene = root / 'runs/smart-ward-v1/scene'
    for folder in ('commands', 'acks'):
        for path in sorted((scene / folder).glob('*.json')):
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('scene_evidence_file_not_regular:' + str(path))
            result[folder + '/' + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def refusal_projection(state, observation, inventory):
    return {'business': ownership_projection(state),
            'navigation_tasks': copy.deepcopy(state.get('navigation_tasks', {})),
            'scene_ownership': {sid: item.get('location') for sid, item in observation.get('specimens', {}).items()},
            'scene_transfers': copy.deepcopy(observation.get('transfers', {})),
            'cabinet_specimens': sorted(observation.get('devices', {}).get(CABINET, {}).get('specimens', [])),
            'operations': copy.deepcopy(state.get('smart_ward', {}).get('operations', {})),
            'scene_command_and_ack_sha256': inventory}


class Capacity(Faults):
    def __init__(self, arguments):
        if isinstance(arguments.wall_seconds, bool) or not isinstance(arguments.wall_seconds, int) or not 180 <= arguments.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError('capacity_wall_seconds_must_be_180_to_1800')
        arguments.case = 'capacity'  # Not the existing one-specimen ACK test.
        if not arguments.output:
            arguments.output = str(Path(__file__).resolve().parents[1] / 'runs/smart-ward-v1/acceptance' /
                ('capacity-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]))
        super().__init__(arguments)
        self.work_deadline = self.deadline - CLEANUP_SECONDS
        self.specimens, self.orders, self.incidents = {}, {}, {}
        self.insert_commands = self.order_requests = self.handoff_calls = self.navigation_calls = 0
        self.handoff_operations = {}
        self.expected_route_id = None
        self.cases = CASES
        self.results = {name: {'status': 'NA', 'reason': 'not_executed'} for name in CASES}
        self.results.update({name: {'status': 'NA', 'reason': reason} for name, reason in {
            'receiver_full': 'No six actual deliveries and seventh attempt are performed.',
            'cross_cabinet_full_pickup': 'Full-load motion to another pickup station is forbidden by production ready_actions; this test stays at C.',
            'real_agent_acceptance': 'Formal qualification explicitly forbids model calls; this is not an Agent demonstration.',
        }.items()})
        self.source_before = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in SOURCES}
        self.report = {'scope': 'real Isaac / MHS same-cabinet robot capacity qualification; NOT Agnes or full delivery acceptance',
            'qualification_version': VERSION, 'case': 'robot_full_same_cabinet', 'started_wall': self.started_wall,
            'limits': {'wall_seconds': arguments.wall_seconds, 'cleanup_reserve_seconds': CLEANUP_SECONDS,
                'navigation_leg_wall_seconds': ACK_NAVIGATION_LEG_WALL_SECONDS, 'maximum_navigation_tasks': 1,
                'maximum_specimens': 3, 'maximum_orders': 3, 'maximum_insert_commands': 3,
                'maximum_handoff_calls': 4, 'fault_commands': 20, 'model_calls_allowed': 0},
            'cases': self.results, 'full_plan_acceptance_passed': False, 'source_sha256_before': self.source_before}

    def state(self, *, cleanup=False):
        # Faults.state enforces max1 specimen/order; do not inherit that test scope.
        if not cleanup:
            self.guard()
        state = self.api(cleanup=cleanup)
        ward = state.get('smart_ward', {})
        if self.run_id and (state.get('run_id') != self.run_id or ward.get('epoch') != self.epoch):
            raise RuntimeError('capacity_qualification_run_or_epoch_changed')
        if self.qualification and not cleanup:
            q = ward.get('fault_qualification', {})
            if not q.get('active') or q.get('qualification_id') != self.qualification['qualification_id'] or q.get('session_key') != self.session or state.get('controller_session') != self.session:
                raise RuntimeError('formal_capacity_qualification_identity_changed')
        tasks = state.get('navigation_tasks', {})
        if len(tasks) > 1 or tasks and not self.nav_expected or any(t.get('goal_station') != STATION for t in tasks.values()):
            raise RuntimeError('capacity_navigation_scope_exceeded')
        if len(state.get('orders', {})) > 3 or len(ward.get('specimens', {})) > 3 or state.get('cargo_count', 0) > 2:
            raise RuntimeError('capacity_business_scope_exceeded')
        if len(ward.get('transfers', {})) > 2:
            raise RuntimeError('unexpected_third_transfer_in_capacity_qualification')
        self.last_state = state
        return state

    async def begin(self):
        state = await self.astate()
        ward = state.get('smart_ward', {})
        self.run_id, self.epoch = state.get('run_id'), ward.get('epoch')
        observation = self.observation()
        checks = {'smart_mode': state.get('protocol') == 'smart-ward-v1', 'workbench_absent': workbench_absent(self.root)['absent'],
            'unclaimed_empty': not state.get('controller_session') and not state.get('orders') and not ward.get('specimens') and not ward.get('incidents'),
            'watch_off': not ward.get('watch', {}).get('enabled'), 'not_stopped': not state.get('operator_stopped'),
            'no_prior_qualification': not ward.get('fault_qualification') and not ward.get('navigation_qualification'),
            'no_prior_navigation_or_budget': not state.get('navigation_tasks') and not state.get('mhs_budget'),
            'actual_c_navigation_needed': STATION not in state.get('at_stations', []),
            'no_recovery': not any(ward.get(k) for k in ('recovery_required', 'resume_required', 'navigation_recovery_required')),
            'eight_fresh_available': set(ward.get('devices', {})) == DEVICES and all(v.get('fresh') is True and v.get('available') is True for v in ward.get('devices', {}).values()),
            'normal_lights': all(ward.get('devices', {}).get('hospital-light-' + s, {}).get('brightness_percent', 0) >= 50 for s in ('301', '303')),
            'fresh_scene_nav': ward.get('scene_fresh') is True and state.get('navigation_ready') is True,
            'physical_stop': physical_stationary(state, observation, self.run_id, self.epoch)}
        write_json(self.directory / 'preflight.json', {'checks': checks, 'state': state, 'observation': observation})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:' + ','.join(k for k, v in checks.items() if not v))
        self.session = 'qualification:capacity:' + self.run_id + ':' + uuid.uuid4().hex[:12]
        self.audit_zero_model(state)
        self.start_attempted = True
        started = await self.aapi('/api/admin/smart-ward/fault-qualification/begin',
            {'session_key': self.session, 'expected_run_id': self.run_id, 'expected_epoch': self.epoch,
             'operation_id': self.prefix + '-formal-begin', 'duration_seconds': self.args.wall_seconds})
        self.qualification = started.get('qualification')
        if not self.qualification or not self.qualification.get('active') or self.qualification.get('session_key') != self.session:
            raise RuntimeError('formal_capacity_qualification_not_confirmed')
        await self.aapi('/api/admin/smart-ward/start', {'session_key': self.session, 'expected_run_id': self.run_id, 'operation_id': self.prefix + '-watch'})
        self.watch_started = True
        self.report['decision_gate_refusal'] = await self.aapi('/api/mhs/decision',
            {'decision_id': self.prefix + '-gate-probe', 'expected_run_id': self.run_id, 'session_key': self.session},
            expected_error='fault_qualification_has_no_model_calls')
        await self.connect()
        from .record import Recorder
        self.recorder = Recorder(self.directory).start()
        write_json(self.directory / 'begin.json', started)

    def scene_command(self, cabinet, label):
        if cabinet != CABINET or self.insert_commands >= 3:
            raise RuntimeError('capacity_insert_command_scope_exceeded')
        self.insert_commands += 1  # An unknown response still consumes this allowance.
        return super().scene_command(cabinet, label)

    async def rpc(self, method, arguments):
        if method == 'hub/call':
            tool, args = arguments.get('tool'), arguments.get('arguments', {})
            if tool == 'navigate_route':
                if self.navigation_calls >= 1 or arguments.get('device') != 'hospital-robot-01' or args.get('route_id') != self.expected_route_id:
                    raise RuntimeError('capacity_navigation_call_scope_exceeded')
                self.navigation_calls += 1
            elif tool == 'handoff_order':
                oid, op = args.get('order_id'), args.get('operation_id')
                label = next((k for k, v in self.orders.items() if v == oid), None)
                maximum = 2 if label == 'c3' else 1
                if (arguments.get('device') != CABINET or label is None or op != self.prefix + '-pickup-' + label
                        or self.handoff_calls >= 4 or self.handoff_operations.get(op, 0) >= maximum):
                    raise RuntimeError('capacity_handoff_call_scope_exceeded')
                self.handoff_calls += 1
                self.handoff_operations[op] = self.handoff_operations.get(op, 0) + 1
            else:
                raise RuntimeError('capacity_device_tool_not_allowed')
        return await super().rpc(method, arguments)

    async def insert_and_order(self, label):
        if self.order_requests >= 3 or label in self.orders:
            raise RuntimeError('capacity_order_request_scope_exceeded')
        command, initial, insertion = await asyncio.to_thread(self.scene_command, CABINET, label)
        ack = insertion.get('ack', {})
        sid = insertion.get('result', {}).get('specimen_id')
        if (insertion.get('status') != 'completed' or not sid or sid in self.specimens.values()
                or (ack.get('run_id'), ack.get('epoch'), ack.get('operation_id'), ack.get('status'))
                != (self.run_id, self.epoch, command['operation_id'], 'completed')
                or ack.get('result', {}).get('specimen_id') != sid):
            raise RuntimeError('actual_insert_ack_identity_not_proved')
        def detected(state):
            sample = state['smart_ward'].get('scene_sample') or {}
            return bool(state['smart_ward']['scene_fresh'] and sample.get('wall_time', 0) >= ack.get('wall_time', math.inf)
                and state['smart_ward']['specimens'].get(sid, {}).get('location') == CABINET
                and any(i.get('specimen_id') == sid and i.get('status') == 'new' for i in state['smart_ward']['incidents']))
        state = await self.wait_state(detected, 20)
        observation = self.observation()
        if observation.get('run_id') != self.run_id or observation.get('epoch') != self.epoch or observation.get('specimens', {}).get(sid, {}).get('location') != CABINET:
            raise RuntimeError('actual_insert_scene_ownership_not_proved')
        incident = next(i for i in state['smart_ward']['incidents'] if i.get('specimen_id') == sid)
        self.specimens[label], self.incidents[label] = sid, incident['incident_id']
        self.order_requests += 1
        created = await asyncio.to_thread(self.tool, 'create_specimen_delivery',
            {'incident_id': incident['incident_id'], 'operation_id': self.prefix + '-order-' + label})
        rows = created.get('orders', [])
        if len(rows) != 1 or rows[0].get('specimen_id') != sid or rows[0].get('pickup_station') != STATION or rows[0].get('dropoff_station') != 'lab':
            raise RuntimeError('detected_specimen_order_identity_not_proved')
        self.orders[label] = rows[0]['order_id']
        state = await self.astate()
        connection = __import__('sqlite3').connect((self.root / 'runs/smart-ward-v1/state.sqlite').resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            outbox = connection.execute('SELECT COUNT(*) FROM ward_notifications WHERE run_id=?', (self.run_id,)).fetchone()[0]
        finally:
            connection.close()
        suppressed = await self.aapi('/api/admin/smart-ward/notifications/claim', {'worker_id': self.worker, 'expected_run_id': self.run_id})
        if outbox != 0 or suppressed is not None:
            raise AssertionError('formal_capacity_outbox_not_suppressed')
        self.remaining()
        return {'command': command, 'initial': initial, 'actual_insert': insertion, 'incident': incident,
                'created': created, 'scene': observation, 'state': state, 'outbox_count': outbox}

    async def publish_plan(self, third=False):
        state = await self.astate()
        def step(action, label, station=None):
            row = {'action': action, 'incident_id': self.incidents[label]}
            if station:
                row['station_id'] = station
            return row
        if third:
            # Keep the production capacity validator: unload before C3 pickup.
            steps = [step('navigate', 'c1', 'lab'), step('deliver', 'c1'), step('deliver', 'c2'),
                     step('navigate', 'c3', STATION), step('pickup', 'c3'),
                     step('navigate', 'c3', 'lab'), step('deliver', 'c3')]
        else:
            steps = [step('navigate', 'c1', STATION), step('pickup', 'c1'), step('pickup', 'c2'),
                     step('navigate', 'c1', 'lab'), step('deliver', 'c1'), step('deliver', 'c2')]
        steps.append({'action': 'standby', 'station_id': 'lab'})
        return await asyncio.to_thread(self.tool, 'publish_service_plan', {'planning_version': state['planning_version'],
            'steps': steps, 'reason': '容量资格检查：实际到C取满两箱；第三箱须先卸货再取。原地负例探针后停止，保留真实载货，不执行送检。',
            'operation_id': self.prefix + ('-plan-third' if third else '-plan-initial')})

    async def arrival_c(self):
        plan = await self.publish_plan()
        options = await self.aapi('/api/mhs/routes', {'station_id': STATION})
        routes = options.get('routes', [])
        if not routes or any(r.get('validation') != 'nav2_verified' for r in routes):
            raise RuntimeError('fresh_verified_route_to_c_required')
        route = min(routes, key=lambda r: (r.get('distance_m', r.get('length_m', math.inf)), r['route_id']))
        self.expected_route_id = route['route_id']
        await self.device_state('hospital-robot-01', lambda d: d.get('available') and d.get('state', {}).get('values', {}).get('stop_confirmed') and not d.get('busy', {}).get('chassis'))
        self.nav_expected = True
        started = time.monotonic()
        receipt = await self.rpc('hub/call', {'device': 'hospital-robot-01', 'tool': 'navigate_route',
            'arguments': {'route_id': route['route_id'], 'operation_id': self.prefix + '-route-c'}})
        if not receipt.get('accepted') or not receipt.get('job'):
            raise RuntimeError('production_capacity_navigation_not_accepted')
        end = min(started + ACK_NAVIGATION_LEG_WALL_SECONDS, self.work_deadline)
        while time.monotonic() < end:
            state = await self.astate()
            if time.monotonic() >= end:
                break
            observation = self.observation()
            tasks = state.get('navigation_tasks', {})
            if len(tasks) == 1:
                tid, task = next(iter(tasks.items()))
                if task.get('route_id') != route['route_id'] or task.get('goal_station') != STATION:
                    raise RuntimeError('capacity_navigation_task_identity_mismatch')
                if task.get('status') in {'failed', 'canceled', 'unknown'}:
                    raise RuntimeError('capacity_navigation_' + task['status'])
                native = self.hub.jobs.get(receipt['job'])
                if arrived(state, observation, STATION, tid, self.run_id, self.epoch) and native:
                    if native.get('status') != 'done':
                        raise RuntimeError('native_capacity_navigation_not_done')
                    self.navigation = {'route': route, 'task_id': tid, 'receipt': receipt, 'result': native}
                    return {'plan': plan, 'options': options, 'navigation': self.navigation, 'state': state, 'observation': observation}
            await asyncio.sleep(.25)
        raise TimeoutError('real_c_arrival_not_proved_within_existing_fault_leg')

    async def pickup(self, label):
        before = await self.astate()
        before_sequence = before['smart_ward']['scene_sample']['seq']
        op, oid, sid = self.prefix + '-pickup-' + label, self.orders[label], self.specimens[label]
        await self.device_state(CABINET, lambda d: d.get('state', {}).get('values', {}).get('available') and not d.get('busy', {}).get('actuator'))
        receipt = await self.rpc('hub/call', {'device': CABINET, 'tool': 'handoff_order', 'arguments': {'order_id': oid, 'operation_id': op}})
        result = await self.finish(receipt, 85)
        if result.get('status') != 'done':
            raise RuntimeError('actual_pickup_native_not_done:' + json.dumps(result, ensure_ascii=False))
        ack_path = self.root / 'runs/smart-ward-v1/scene/acks' / (op + '.json')
        raw = ack_path.read_bytes()
        ack = json.loads(raw)
        end = min(time.monotonic() + 20, self.work_deadline)
        while time.monotonic() < end:
            state = await self.astate()
            observation = self.observation()
            if time.monotonic() >= end:
                break
            if pickup_proved(state, observation, ack, op, oid, sid, self.run_id, self.epoch, before_sequence):
                (self.directory / (label + '-original-pickup-ack.json')).write_bytes(raw)
                return {'receipt': receipt, 'native_result': result, 'original_ack': ack,
                        'ack_sha256': hashlib.sha256(raw).hexdigest(), 'state': state, 'observation': observation}
            await asyncio.sleep(.2)
        raise RuntimeError('original_pickup_ack_and_fresh_scene_not_reconciled_no_retry:' + op)

    async def refusal(self):
        state = await self.astate()
        observation = self.observation()
        if not ownership_proved(state, observation, self.specimens, self.orders) or STATION not in state.get('at_stations', []) or not physical_stationary(state, observation, self.run_id, self.epoch):
            raise RuntimeError('third_pickup_requires_real_c_stationary_full_cargo_and_c3_in_cabinet')
        before = refusal_projection(state, observation, scene_files(self.root))
        receipt = await self.rpc('hub/call', {'device': CABINET, 'tool': 'handoff_order',
            'arguments': {'order_id': self.orders['c3'], 'operation_id': self.prefix + '-pickup-c3'}})
        result = await self.finish(receipt, 15) if receipt.get('accepted') and receipt.get('job') else receipt
        if result.get('status') == 'done' or not exact_capacity_error(result):
            raise RuntimeError('third_pickup_capacity_refusal_not_proved:' + json.dumps(result, ensure_ascii=False))
        state = await self.astate()
        observation = self.observation()
        after = refusal_projection(state, observation, scene_files(self.root))
        if before != after or not ownership_proved(state, observation, self.specimens, self.orders) or not physical_stationary(state, observation, self.run_id, self.epoch):
            raise AssertionError('refused_full_pickup_changed_ownership_navigation_command_or_ack')
        self.remaining()
        return {'receipt': receipt, 'native_result': result, 'expected_reason': 'capacity_exceeded',
                'unchanged_projection': after, 'state': state, 'observation': observation}

    async def qualify(self):
        self.active_case = CASES[0]
        initial = [await self.insert_and_order(label) for label in ('c1', 'c2')]
        arrival = await self.arrival_c()
        self.pass_case(self.active_case, {'initial': initial, 'real_arrival': arrival})
        self.active_case = CASES[1]
        pickups = [await self.pickup(label) for label in ('c1', 'c2')]
        state = await self.astate()
        observation = self.observation()
        if state.get('cargo_count') != 2 or any(state['smart_ward']['specimens'][sid]['location'] != 'robot' or observation['specimens'][sid]['location'] != 'robot' for sid in self.specimens.values()) or state['smart_ward']['devices'][CABINET]['specimens'] or observation['devices'][CABINET]['specimens']:
            raise AssertionError('two_real_pickups_did_not_empty_c_and_fill_robot')
        self.pass_case(self.active_case, {'pickups': pickups, 'state': state, 'observation': observation})
        self.active_case = CASES[2]
        third = await self.insert_and_order('c3')
        third['capacity_respecting_plan'] = await self.publish_plan(third=True)
        self.pass_case(self.active_case, third)
        self.active_case = CASES[3]
        refusal = await self.refusal()
        self.pass_case(self.active_case, refusal)
        self.active_case = CASES[4]
        replay = await self.refusal()
        self.pass_case(self.active_case, {'same_operation_id': self.prefix + '-pickup-c3', 'refusal': replay})

    async def cleanup(self):
        # Faults.cleanup indexes ACK cases and assumes cargo1; this is independent.
        result = {'qualification_started': self.start_attempted}
        if not self.start_attempted:
            return result
        state = await self.aapi(cleanup=True)
        if state.get('run_id') != self.run_id or state.get('smart_ward', {}).get('epoch') != self.epoch:
            raise RuntimeError('capacity_cleanup_refuses_changed_run_or_epoch')
        if not self.qualification:
            q = state['smart_ward'].get('fault_qualification', {})
            if q.get('session_key') == self.session:
                self.qualification = q
        if not self.qualification:
            raise RuntimeError('capacity_begin_outcome_unknown_no_cleanup_mutation')
        result['end'] = await self.aapi('/api/admin/smart-ward/fault-qualification/end',
            {**self.identity(), 'operation_id': self.prefix + '-formal-end', 'reason': 'capacity_driver_finished_retained_cargo'}, cleanup=True)
        end = min(time.monotonic() + 25, self.deadline)
        seen = set()
        while time.monotonic() < end:
            state = await self.astate(cleanup=True)
            observation = self.observation()
            path = self.root / 'runs/smart-ward-v1/scene/fault-state.json'
            fault_state = json.loads(path.read_text()) if path.exists() else {}
            identity = ('qualification_id', 'session_key', 'run_id', 'epoch')
            if (state.get('operator_stopped') and not state['smart_ward']['watch']['enabled']
                    and not state.get('motion_guard', {}).get('armed')
                    and physical_stationary(state, observation, self.run_id, self.epoch)
                    and all(fault_state.get(k) == self.qualification.get(k) for k in identity)
                    and fault_state.get('active') is False and not fault_state.get('holds') and not fault_state.get('paused')
                    and observation.get('wall_time', 0) > result['end']['qualification']['ended_wall']):
                seen.add(observation['seq'])
                if len(seen) >= 3:
                    result.update(physical_stop_confirmed=True, new_scene_sequences=sorted(seen), state=state, scene_fault_state=fault_state)
                    write_json(self.directory / 'final-state.json', state)
                    if self.results[CASES[4]]['status'] == 'passed':
                        if not ownership_proved(state, observation, self.specimens, self.orders) or len(state['navigation_tasks']) != 1:
                            raise AssertionError('stop_did_not_preserve_two_cargo_and_third_in_c')
                        self.report['final_cargo_retained'] = True
                        self.pass_case(CASES[5], {'stopped_state': state, 'observation': observation, 'new_scene_sequences': sorted(seen),
                            'uncompleted_reason': 'Capacity qualification stopped deliberately: two actual specimens remain on robot; third remains in C; none is delivered.'})
                    return result
            await asyncio.sleep(.3)
        raise TimeoutError('capacity_formal_end_and_actual_stop_not_confirmed')

    async def run_async(self):
        error = cleanup_error = None
        self.active_case = 'preflight'
        try:
            await self.begin()
            await self.qualify()
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
            try:
                self.report['model_audit'] = self.audit_zero_model(self.last_state) if self.session else None
                source_after = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in SOURCES}
                self.report.update(source_sha256=source_after, source_unchanged=source_after == self.source_before)
                if not self.report['source_unchanged']:
                    raise RuntimeError('capacity_source_changed_during_qualification')
            except Exception as failure:
                cleanup_error = (cleanup_error + ';' if cleanup_error else '') + 'final_audit:' + repr(failure)
            counters = {'insert_commands_submitted': self.insert_commands, 'order_requests_submitted': self.order_requests,
                        'handoff_calls_submitted': self.handoff_calls, 'navigation_calls_submitted': self.navigation_calls,
                        'fault_commands_submitted': self.fault_commands}
            exact_scope = counters == {'insert_commands_submitted': 3, 'order_requests_submitted': 3,
                'handoff_calls_submitted': 4, 'navigation_calls_submitted': 1, 'fault_commands_submitted': 0}
            passed = bool(not error and not cleanup_error and exact_scope and self.report.get('final_cargo_retained')
                and self.report.get('cleanup', {}).get('physical_stop_confirmed')
                and all(self.results[name]['status'] == 'passed' for name in CASES))
            self.report.update(run_id=self.run_id, epoch=self.epoch, session_key=self.session, qualification=self.qualification,
                ended_wall=time.time(), elapsed_wall_seconds=time.monotonic() - self.started, error=error, cleanup_error=cleanup_error,
                scope_counters=counters, exact_scope_counters=exact_scope,
                counts={status: sum(v['status'] == status for v in self.results.values()) for status in ('passed', 'failed', 'NA')},
                requested_case_passed=passed, qualification_cases_passed=passed,
                outcome='partial_NA' if passed else 'failed_or_incomplete')
            write_json(self.directory / 'report.json', self.report)
        print(json.dumps({'evidence': str(self.directory), 'outcome': self.report['outcome'], 'counts': self.report['counts'],
                          'error': error, 'cleanup_error': cleanup_error}, ensure_ascii=False))
        return 2 if self.report['requested_case_passed'] else 1


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wall-seconds', type=int, default=MAX_WALL_SECONDS)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if not 180 <= args.wall_seconds <= MAX_WALL_SECONDS:
        parser.error('--wall-seconds must be 180..1800; cleanup reserves 60 seconds')
    return args


def main():
    driver = Capacity(arguments())
    for selected in (signal.SIGTERM, signal.SIGINT):
        signal.signal(selected, lambda *_: setattr(driver, 'interrupted', True))
    raise SystemExit(asyncio.run(driver.run_async()))


if __name__ == '__main__':
    main()
