"""Bounded real-Agent demonstrations; optional UI-only observation for normal.

Never creates an order, selects a route, navigates, hands off, resets a run, or
prompts the model to finish.  Those actions belong to the background Agnes
session.  Existing failures and evidence directories are never overwritten.
With --manual-ui this driver starts neither watch nor environmental events:
the operator clicks the actual workbench controls, separately recorded by CUA.
"""
import argparse
import collections
import hashlib
import json
import math
import os
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(os.environ.get('HOSPITAL_ROOT', Path(__file__).resolve().parents[1]))
CASES = ('normal', 'urgent', 'recovery')
REQUIRED_DEVICES = {'hospital-robot-01', 'hospital-cabinet-b', 'hospital-cabinet-c',
                    'hospital-cabinet-lab', 'hospital-light-301', 'hospital-light-303',
                    'hospital-sensor-301', 'hospital-sensor-303'}
RECOVERY_ENVIRONMENT = {
    'version': 'smart-recovery-environment-20261013.35',
    'pause_distance_metres': 3, 'minimum_pause_sim_seconds': 10,
    'restore_sim_upper_seconds': 60, 'restore_wall_upper_seconds': 300,
    'false_sample_max_age_seconds': 3,
    'prior_environment': {'restore_sim_upper_seconds': 60, 'restore_wall_upper_seconds': 45},
    'supersedes_local_candidate': {'version': 'smart-recovery-environment-20261013.33',
        'contract_error': 'The local undeployed candidate read internal scene instead of public scene_sample. It was not a real-Agent demonstration failure; its frozen evidence is retained.'},
    'revision_reason': 'Before the first real-Agent demonstration, allow bounded time for slow Agent native-watch registration. The earlier environment has not been demonstrated to fail.',
    'fallback_rule': 'An upper limit without accepted native watch and fresh post-pause false evidence restores only for cleanup and cannot pass recovery.',
    'unchanged_business_limits': {'real_requests': 60, 'sim_seconds': 2400, 'wall_seconds': 3600,
                                  'receiver_unavailable_sim_seconds': 150},
}


def finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def recovery_restore_decision(elapsed_sim, elapsed_wall, accepted_watch, fresh_false):
    """Choose bounded environment cleanup; a fallback never proves recovery."""
    if (not finite_nonnegative(elapsed_sim) or not finite_nonnegative(elapsed_wall)
            or not isinstance(accepted_watch, bool) or not isinstance(fresh_false, bool)):
        return {'restore': False, 'fallback': False, 'reason': 'receiver_wait_timing_unknown'}
    if accepted_watch and fresh_false and elapsed_sim >= RECOVERY_ENVIRONMENT['minimum_pause_sim_seconds']:
        return {'restore': True, 'fallback': False, 'reason': 'accepted_native_watch_after_minimum_pause'}
    limits = []
    if elapsed_sim >= RECOVERY_ENVIRONMENT['restore_sim_upper_seconds']:
        limits.append('simulation_seconds')
    if elapsed_wall >= RECOVERY_ENVIRONMENT['restore_wall_upper_seconds']:
        limits.append('wall_seconds')
    if limits:
        reason = 'receiver_wait_registration_timeout' if not accepted_watch else 'receiver_false_evidence_unknown'
        return {'restore': True, 'fallback': True, 'reason': reason, 'reached_limits': limits}
    return {'restore': False, 'fallback': False, 'reason': 'waiting_for_native_watch_and_fresh_false'}


def receiver_false_evidence(state, pause, now):
    """Require the actual cabinet sample, newer than this pause, with fresh identity."""
    unknown = {'passed': False, 'reason': 'receiver_false_evidence_unknown_or_stale'}
    if not isinstance(state, dict) or not isinstance(pause, dict):
        return unknown
    ward = state.get('smart_ward', {})
    if not isinstance(ward, dict) or not isinstance(ward.get('devices'), dict):
        return unknown
    scene = ward.get('scene_sample') or {}
    device = ward['devices'].get('hospital-cabinet-lab') or {}
    if not all(isinstance(value, dict) for value in (scene, device)):
        return unknown
    if (ward.get('scene_fresh') is not True or device.get('fresh') is not True
            or device.get('available') is not False):
        return unknown
    if (not pause.get('run_id') or not pause.get('epoch')
            or state.get('run_id') != pause['run_id'] or ward.get('epoch') != pause['epoch']
            or scene.get('run_id') != pause['run_id'] or scene.get('epoch') != pause['epoch']):
        return unknown
    baseline, sequence, scene_sequence = pause.get('sample_seq'), device.get('seq'), scene.get('seq')
    if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (baseline, sequence, scene_sequence))
            or sequence <= baseline or scene_sequence != sequence):
        return unknown
    stamp, scene_stamp = device.get('wall_time'), scene.get('wall_time')
    pause_wall, sample_sim, pause_sim = pause.get('wall_time'), device.get('sim_time'), pause.get('sim_time')
    scene_sim = scene.get('sim_time')
    if (not all(finite_nonnegative(value) for value in (now, stamp, scene_stamp, pause_wall, sample_sim, scene_sim, pause_sim))
            or not 0 <= now-stamp <= RECOVERY_ENVIRONMENT['false_sample_max_age_seconds']
            or scene_stamp != stamp or stamp <= pause_wall or sample_sim < pause_sim or scene_sim != sample_sim):
        return unknown
    return {'passed': True, 'reason': 'fresh_post_pause_unavailable_sample',
            'sample': {'run_id': pause['run_id'], 'epoch': pause['epoch'],
                       'device_id': 'hospital-cabinet-lab', 'seq': sequence, 'wall_time': stamp,
                       'sim_time': sample_sim, 'available': False, 'fresh': True,
                       'scene_seq': scene['seq'], 'scene_wall_time': scene_stamp,
                       'pause_operation_id': pause.get('operation_id')}}


def recovery_evidence_pass(details, specimen_delivered):
    return bool(details.get('receiver_false_observed') and details.get('receiver_false_evidence', {}).get('passed')
                and details.get('receiver_restored') and details.get('agent_receiver_watch_observed')
                and not details.get('receiver_environment_fallback') and specimen_delivered)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def append_json(path, value):
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n')


def request(base, path, arguments=None, timeout=5):
    headers = {'Content-Type': 'application/json'}
    if base.endswith(':4194'):
        headers['Origin'] = base
    query = urllib.request.Request(base+path, headers=headers,
        data=None if arguments is None else json.dumps(arguments).encode())
    try:
        with urllib.request.urlopen(query, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace')
        raise RuntimeError('HTTP_'+str(error.code)+':'+detail[:500]) from error
    if value.get('ok') is False:
        raise RuntimeError(value.get('error', 'API request failed'))
    return value.get('result', value)


def ledger_rows(root, session, after_seq=0):
    """Read only the selected project's selected session; never account files."""
    path = root/'agent/agh-home-mhs/data/sessions.db'
    connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
    try:
        return [{'seq': seq, 'ts': stamp, 'type': kind, 'data': json.loads(data)}
                for seq, stamp, kind, data in connection.execute(
                    'SELECT seq,ts,type,data FROM events WHERE session_key=? AND seq>? ORDER BY seq',
                    (session, after_seq))]
    finally:
        connection.close()


def call_arguments(row):
    data = row.get('data', {})
    return data.get('input', data.get('arguments', data.get('args', {}))) or {}


def receiver_watch_records(rows, after_seq=0):
    """Accepted native waits for the actual declared lab recovery condition."""
    accepted = {row.get('data', {}).get('toolUseId'): row for row in rows
                if row.get('type') == 'tool/result' and row.get('data', {}).get('toolUseId')
                and row.get('data', {}).get('isError') is not True}
    matches = []
    for row in rows:
        data = row.get('data', {})
        if row.get('seq', 0) <= after_seq or row.get('type') != 'tool/call' or data.get('name') != 'watch_device':
            continue
        args = call_arguments(row)
        until = args.get('until', {})
        result = accepted.get(data.get('toolUseId'))
        if args.get('device') == 'hospital-robot-01' and isinstance(until, dict) and until.get('state') == 'delivery_wait_lab' and until.get('eq') is True and set(until) == {'state', 'eq'} and result and result.get('seq', 0) > row.get('seq', 0):
            matches.append({'call': row, 'result': result})
    return matches


def summarize_ledger(rows):
    calls = [row for row in rows if row['type'] == 'tool/call']
    by_id = {row['data'].get('toolUseId'): row for row in calls}
    verifications, errors, ledgers, tokens, seen = [], [], [], {}, set()
    for row in rows:
        data = row['data']
        if row['type'] == 'cost/ledger':
            identity = data.get('effectId') or ('seq', row['seq'])
            if identity not in seen:
                seen.add(identity); ledgers.append(row)
                for key, value in data.get('tokens', {}).items():
                    if isinstance(value, (int, float)):
                        tokens[key] = tokens.get(key, 0)+value
        if row['type'] != 'tool/result':
            continue
        call = by_id.get(data.get('toolUseId'))
        if not call:
            continue
        name = call['data'].get('name', '')
        if data.get('isError'):
            errors.append({'seq': row['seq'], 'tool': name, 'content': data.get('content')})
        if name.endswith('verify_services') and not data.get('isError'):
            for part in data.get('content', []):
                if part.get('type') != 'text':
                    continue
                try:
                    body = json.loads(part['text'])
                    result = body.get('result', body)
                    if isinstance(result, dict) and 'incidents' in result:
                        verifications.append({'seq': row['seq'], 'report': result})
                except (ValueError, TypeError):
                    pass
    device_calls = [{'seq': row['seq'], 'arguments': call_arguments(row)} for row in calls if row['data'].get('name') == 'call_device']
    return {'range': {'first_seq': rows[0]['seq'] if rows else None, 'last_seq': rows[-1]['seq'] if rows else None},
            'request_count': sum(row['type'] == 'request/sent' for row in rows),
            'tool_calls': dict(collections.Counter(row['data'].get('name') for row in calls)),
            'device_calls': device_calls, 'verifications': verifications, 'tool_errors': errors,
            'token_usage': tokens, 'ledger_entry_count': len(ledgers), 'actual_monetary_cost': None,
            'cost_note': 'Provider ledger token estimates, deduplicated by effectId; actual billing is unavailable.'}


def completion_evidence(state, rows):
    """Require finalization before this driver's stop can manufacture end_wall.

    Post-turn costs/effects are permitted, but a later start, failed terminal turn,
    early verification or a stale verification cannot prove Agent completion.
    """
    ward = state.get('smart_ward', {})
    episode, version = ward.get('episode'), ward.get('verification_event_version')
    budget, verified = state.get('mhs_budget') or {}, ward.get('last_verification') or {}
    end = next((row for row in reversed(rows) if row.get('type') == 'turn/end'), None)
    later_start = bool(end and any(row.get('type') == 'turn/start' and row.get('seq', 0) > end['seq'] for row in rows))
    audit = summarize_ledger(rows)
    reports = [item for item in audit['verifications'] if item['report'].get('run_id') == state.get('run_id')
               and item['report'].get('episode') == episode and item['report'].get('verification_event_version') == version
               and item['report'].get('ready_to_finalize') is True and not item['report'].get('pending')
               and end and item['seq'] < end['seq']]
    ending = next((step for step in reversed((ward.get('service_plan') or {}).get('steps', []))
                   if step.get('action') == 'standby'), {})
    station = ending.get('station_id')
    checks = {'terminal_turn_completed': bool(end and end.get('data', {}).get('reason') == 'completed' and not later_start),
              'current_verification': bool(reports and verified.get('episode') == episode and verified.get('event_version') == version),
              'episode_finalized_before_driver_stop': bool(budget.get('end_wall')),
              'episode_finished': state.get('mhs_finished') is True,
              'published_final_station_arrived': bool(station and station in state.get('at_stations', [])),
              'fresh_and_stopped': bool(state.get('pose_fresh') and state.get('stopped_confirmed') and ward.get('scene_fresh')),
              'not_prematurely_stopped': not state.get('operator_stopped') and not budget.get('stopped_reason')}
    return {'passed': all(checks.values()), 'checks': checks, 'run_id': state.get('run_id'),
            'episode': episode, 'verification_event_version': version, 'budget_end_wall': budget.get('end_wall'),
            'final_station': station, 'terminal_turn': end,
            'matching_verification_seq': reports[-1]['seq'] if reports else None}


def manual_ui_evidence(state, facade, session, boundary_wall):
    """Link normal UI facade records to same-run scene ACKs and detected needs.

    Facade records prove the endpoint operations, not browser interaction by
    themselves. Actual CUA actions/screenshots must accompany a browser claim.
    """
    run_id = state.get('run_id'); ward = state.get('smart_ward', {})
    operations = [op for op in facade.get('operations', [])
                  if op.get('expected_run_id') == run_id and op.get('wall_time', 0) >= boundary_wall]
    starts = [op for op in operations if op.get('action') == 'smart_start' and op.get('status') == 'completed'
              and (op.get('new_session') or op.get('session_id')) == session]
    incidents = ward.get('incidents', [])
    if isinstance(incidents, dict): incidents = list(incidents.values())
    expected = {'specimen': ('insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}),
                'lighting': ('set_brightness', {'room_id': 'room301', 'brightness_percent': 10})}
    matches = {}; unexpected = []
    for op in operations:
        if op.get('action') != 'smart_event': continue
        role = next((name for name, (kind, args) in expected.items()
                     if op.get('kind') == kind and op.get('arguments') == args), None)
        if not role:
            unexpected.append(op); continue
        scene = ward.get('operations', {}).get(op.get('operation_id'), {})
        command, ack = scene.get('command', {}), scene.get('ack', {})
        if (op.get('status') != 'completed' or op.get('session_id') != session or not starts
            or op.get('wall_time', 0) < starts[0]['wall_time'] or scene.get('status') != 'completed'
            or scene.get('origin') != 'operator' or command.get('kind') != op.get('kind')
            or command.get('arguments') != op.get('arguments')
            or command.get('operation_id') != op.get('operation_id') or ack.get('status') != 'completed'
            or any(value.get('run_id') != run_id or value.get('epoch') != ward.get('epoch')
                   or value.get('operation_id') != op.get('operation_id') for value in (command, ack))):
            continue
        result = ack.get('result', {})
        if role == 'specimen':
            sid = result.get('specimen_id')
            demand = next((i for i in incidents if i.get('kind') == 'specimen_ready'
                           and i.get('specimen_id') == sid and i.get('cabinet_id') == 'hospital-cabinet-b'), None)
            valid = bool(sid and sid in ward.get('specimens', {}) and demand)
        else:
            demand = next((i for i in incidents if i.get('kind') == 'low_light' and i.get('room_id') == 'room301'), None)
            valid = bool(result.get('room_id') == 'room301' and result.get('requested_percent') == 10
                         and result.get('actual_percent') == 10 and demand)
        if valid: matches[role] = {'facade_operation': op, 'scene_operation': scene, 'incident': demand}
    checks = {'ui_watch_start_observed': bool(starts), 'b_normal_button_ack_and_demand': 'specimen' in matches,
              '301_dark_button_ack_and_demand': 'lighting' in matches,
              'no_extra_ui_environment_operations': not unexpected}
    return {'passed': all(checks.values()), 'checks': checks, 'start_operations': starts, 'matches': matches,
            'unexpected_operations': unexpected, 'browser_action_evidence': 'Separate CUA actions/screenshots required.'}


class Demonstration:
    def __init__(self, arguments):
        self.args = arguments
        self.manual_ui = getattr(arguments, 'manual_ui', False)
        if self.manual_ui and arguments.case != 'normal':
            raise ValueError('manual_ui_supports_normal_only')
        self.root = ROOT.resolve()
        default = self.root/'runs/smart-ward-v1/acceptance'/(arguments.case+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        self.directory = Path(arguments.output).resolve() if arguments.output else default
        if not self.directory.is_relative_to(self.root):
            raise ValueError('evidence_directory_must_be_inside_project')
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started_wall = time.time(); self.started = time.monotonic()
        self.deadline = self.started+arguments.wall_seconds
        self.observation_end = self.deadline-75
        self.run_id = self.session = None
        self.recorder = None; self.interrupted = False
        self.injections = []; self.details = {}; self.first_position = None
        self.last_state = None; self.seq_start = 0
        self.last_snapshot = 0
        self.ui_boundary_wall = None

    def api(self, path='/api/state', arguments=None, facade=False):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('acceptance_wall_budget_exhausted')
        return request('http://127.0.0.1:'+('4194' if facade else '4188'), path, arguments, timeout=min(5, remaining))

    def record(self, kind, **values):
        append_json(self.directory/'driver-events.jsonl', {'kind': kind, 'wall_time': time.time(), **values})

    def state(self):
        value = self.api()
        if self.run_id and value['run_id'] != self.run_id:
            raise RuntimeError('run_changed_during_acceptance')
        if self.manual_ui and self.session and value.get('controller_session') != self.session:
            raise RuntimeError('manual_ui_controller_changed')
        self.last_state = value
        if time.monotonic()-self.last_snapshot >= 2:
            append_json(self.directory/'states.jsonl', {'wall_time': time.time(), 'state': value})
            self.last_snapshot = time.monotonic()
        return value

    def preflight(self):
        state = self.state(); ward = state.get('smart_ward', {})
        checks = {'smart_mode': state.get('protocol') == 'smart-ward-v1',
                  'navigation_ready': state.get('navigation_ready') is True,
                  'pose_fresh': state.get('pose_fresh') is True,
                  'physical_pose_present': bool(state.get('physical_pose')),
                  'guard_healthy': state.get('motion_guard', {}).get('healthy') is True,
                  'stopped': state.get('stopped_confirmed') is True,
                  'watch_not_started': ward.get('watch', {}).get('enabled') is False,
                  'no_orders': not state.get('orders'), 'no_incidents': not ward.get('incidents'),
                  'no_specimens': not ward.get('specimens'), 'not_operator_stopped': not state.get('operator_stopped'),
                  'scene_fresh': ward.get('scene_fresh') is True,
                  'devices_present': REQUIRED_DEVICES <= set(ward.get('devices', {}))}
        facade = self.api('/api/demo/status', facade=True)
        checks['workbench_smart_profile'] = facade.get('profile') == 'smart-ward-v1'
        write_json(self.directory/'preflight.json', {'checks': checks, 'state': state, 'workbench': facade})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:'+','.join(key for key, value in checks.items() if not value))
        self.run_id = state['run_id']
        self.first_position = [state['pose']['x'], state['pose']['y']]
        if self.manual_ui:
            if ward.get('operations'):
                raise RuntimeError('manual_ui_boundary_after_environment_event')
            self.ui_boundary_wall = time.time()
            if state.get('controller_session'):
                self.freeze_ui_boundary(state)
            return
        command = {'action': 'smart_start', 'operation_id': 'accept-start-'+uuid.uuid4().hex,
                   'expected_run_id': self.run_id}
        if state.get('controller_session'):
            command['session_id'] = state['controller_session']
        start = self.api('/api/demo/command', command, facade=True)
        self.session = start.get('new_session')
        if not self.session:
            raise RuntimeError('smart_start_did_not_return_session')
        write_json(self.directory/'start.json', start)
        # Start initializes the session but does not prompt it.  Freeze the seq
        # boundary before the first environmental injection.
        rows = ledger_rows(self.root, self.session)
        self.seq_start = rows[-1]['seq'] if rows else 0
        self.record('watch_started', run_id=self.run_id, session_id=self.session, after_seq=self.seq_start)

    def freeze_ui_boundary(self, state):
        ward = state.get('smart_ward', {})
        if state.get('orders') or ward.get('incidents') or ward.get('specimens') or ward.get('operations'):
            raise RuntimeError('manual_ui_boundary_after_environment_event')
        session = state.get('controller_session')
        if not session:
            raise RuntimeError('manual_ui_controller_missing')
        rows = ledger_rows(self.root, session)
        if any(row['type'] in {'request/sent', 'tool/call', 'user/message'}
               or row['type'] == 'inbox' and row.get('data', {}).get('items') for row in rows):
            raise RuntimeError('manual_ui_boundary_after_model_input')
        self.session = session; self.seq_start = rows[-1]['seq'] if rows else 0
        if self.ui_boundary_wall is None: self.ui_boundary_wall = time.time()
        self.record('manual_ui_boundary_frozen', run_id=self.run_id, session_id=session,
                    after_seq=self.seq_start, boundary_wall=self.ui_boundary_wall)

    def wait_for_ui_start(self):
        print('WAITING_UI_START '+json.dumps({'run_id': self.run_id, 'session_id': self.session,
              'after_seq': self.seq_start, 'instructions': 'Click 开始值守; wait for WAITING_UI_EVENTS before environment buttons.'}, ensure_ascii=False), flush=True)
        while time.monotonic() < self.observation_end and not self.interrupted:
            state = self.state(); ward = state.get('smart_ward', {})
            if state.get('operator_stopped') or (state.get('mhs_budget') or {}).get('stopped_reason'):
                raise RuntimeError('manual_ui_stopped_before_watch_start')
            if self.session and state.get('controller_session') != self.session:
                raise RuntimeError('manual_ui_controller_changed')
            if not self.session and state.get('controller_session'):
                self.freeze_ui_boundary(state)
            if ward.get('watch', {}).get('enabled'):
                if not self.session or ward['watch'].get('session_id') != self.session:
                    raise RuntimeError('manual_ui_watch_controller_mismatch')
                # The boundary is already frozen; reject clicks made before the
                # driver is ready rather than hide their model requests.
                if state.get('orders') or ward.get('incidents') or ward.get('specimens') or ward.get('operations'):
                    raise RuntimeError('manual_ui_environment_before_ready')
                facade = self.api('/api/demo/status', facade=True)
                proof = manual_ui_evidence(state, facade, self.session, self.ui_boundary_wall)
                if proof['checks']['ui_watch_start_observed']:
                    self.details['manual_ui_start'] = proof['start_operations']
                    write_json(self.directory/'start.json', {'source': 'observed_workbench_ui', 'operations': proof['start_operations']})
                    self.record('watch_started', run_id=self.run_id, session_id=self.session, after_seq=self.seq_start, source='observed_ui')
                    print('WAITING_UI_EVENTS '+json.dumps({'run_id': self.run_id, 'session_id': self.session,
                          'after_seq': self.seq_start, 'instructions': 'Click B护士站 普通 放入封装标本, then 301 照明变暗.'}, ensure_ascii=False), flush=True)
                    return
            time.sleep(.25)
        raise TimeoutError('manual_ui_start_not_observed_within_budget')

    def inject(self, kind, arguments, label):
        if self.manual_ui:
            raise RuntimeError('manual_ui_driver_must_not_inject')
        command = {'kind': kind, 'arguments': arguments, 'operation_id': 'accept-'+label+'-'+uuid.uuid4().hex,
                   'expected_run_id': self.run_id}
        accepted = self.api('/api/admin/smart-ward/command', command)
        item = {'command': command, 'accepted': accepted, 'wall_time': time.time()}
        self.injections.append(item); self.record('environment_injection', **item)
        # A single accepted operation is observed; no new-ID mutation retry.
        end = min(time.monotonic()+20, self.observation_end)
        while time.monotonic() < end and not self.interrupted:
            operation = self.api('/api/ward/operation?id='+urllib.parse.quote(command['operation_id']))
            if operation.get('status') in {'completed', 'failed'}:
                item['result'] = operation
                self.record('environment_injection_result', operation=operation)
                if operation['status'] == 'failed':
                    raise RuntimeError('environment_injection_failed:'+str(operation.get('error')))
                return operation
            time.sleep(.25)
        raise TimeoutError('environment_injection_result_unknown:'+command['operation_id'])

    def initial_injections(self):
        if self.manual_ui: return
        if self.args.case == 'normal':
            self.inject('insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}, 'b-normal')
            self.inject('set_brightness', {'room_id': 'room301', 'brightness_percent': 10}, '301-dark')
        elif self.args.case == 'urgent':
            self.inject('insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}, 'b-normal')
        else:
            # Dim BEFORE making the actuator stuck; otherwise a normal stuck
            # light would remain bright and no real low-light event would exist.
            self.inject('set_brightness', {'room_id': 'room303', 'brightness_percent': 10}, '303-dark')
            self.inject('set_lamp_fault', {'room_id': 'room303', 'stuck': True}, '303-stuck')
            self.inject('insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}, 'b-normal')

    def watch_calls(self):
        rows = ledger_rows(self.root, self.session, self.seq_start)
        matches = receiver_watch_records(rows, self.details.get('receiver_pause_ledger_seq', self.seq_start))
        if matches:
            self.details['agent_receiver_native_watch_records'] = matches
        return bool(matches)

    def conditional_injections(self, state):
        if self.manual_ui: return
        orders = list(state.get('orders', {}).values())
        nav = state.get('navigation') or {}
        pose = state.get('pose') or {}
        if self.args.case == 'urgent' and not self.details.get('urgent_injected'):
            moved = math.dist([pose.get('x', 0), pose.get('y', 0)], self.first_position) > .30
            departed = nav.get('status') == 'running' and moved
            late = bool(orders) and all(order['status'] == 'delivered' for order in orders)
            if departed or late:
                self.details.update(urgent_injected=True, urgent_injection_after_departure=departed,
                                    urgent_injection_late=late, urgent_injection_sim=state['sim_time'])
                self.inject('insert_specimen', {'cabinet_id': 'hospital-cabinet-c', 'priority': 'urgent'}, 'c-urgent')
        if self.args.case != 'recovery':
            return
        loaded = any(order.get('loaded') for order in orders)
        stations = state.get('stations', {})
        lab = stations.get('lab') if isinstance(stations, dict) else None
        if not lab:
            lab = json.loads((self.root/'configs/stations.json').read_text())['lab']
        distance = math.hypot(pose.get('x', 1000)-lab['x'], pose.get('y', 1000)-lab['y'])
        if loaded and distance <= RECOVERY_ENVIRONMENT['pause_distance_metres'] and not self.details.get('receiver_paused'):
            before_pause = ledger_rows(self.root, self.session, self.seq_start)
            self.details['receiver_pause_ledger_seq'] = before_pause[-1]['seq'] if before_pause else self.seq_start
            operation = self.inject('set_receiver', {'available': False}, 'lab-pause')
            pause_wall = time.time()
            self.details.update(receiver_paused=True, pause_sim=state['sim_time'], pause_wall=pause_wall,
                                receiver_pause_boundary={'run_id': state.get('run_id'),
                                    'epoch': state.get('smart_ward', {}).get('epoch'),
                                    'sample_seq': (state.get('smart_ward', {}).get('scene_sample') or {}).get('seq'),
                                    'sim_time': state['sim_time'], 'wall_time': pause_wall,
                                    'operation_id': operation.get('operation_id')})
        if self.details.get('receiver_paused') and not self.details.get('receiver_restored'):
            now = time.time()
            evidence = receiver_false_evidence(state, self.details.get('receiver_pause_boundary'), now)
            self.details['receiver_false_latest_evidence'] = evidence
            if evidence['passed'] and not self.details.get('receiver_false_observed'):
                self.details.update(receiver_false_observed=True, receiver_false_evidence=evidence)
                self.record('receiver_fresh_false_observed', evidence=evidence)
            waiting = self.watch_calls()
            self.details['agent_receiver_watch_observed'] = self.details.get('agent_receiver_watch_observed', False) or waiting
            elapsed_sim = state['sim_time']-self.details['pause_sim']
            elapsed_wall = now-self.details['pause_wall']
            decision = recovery_restore_decision(elapsed_sim, elapsed_wall, bool(waiting),
                                                 bool(self.details.get('receiver_false_observed')))
            self.details['receiver_restore_decision'] = decision
            if decision['restore']:
                if decision['fallback']:
                    self.details['receiver_environment_fallback'] = decision
                    self.details['receiver_wait_registration_timeout'] = decision['reason'] == 'receiver_wait_registration_timeout'
                    self.record('receiver_environment_cleanup_fallback', decision=decision)
                self.inject('set_receiver', {'available': True}, 'lab-restore')
                self.details.update(receiver_restored=True, restore_sim=state['sim_time'], restore_wall=time.time())

    def business_result(self, state):
        ward = state.get('smart_ward', {}); incidents = ward.get('incidents', [])
        if isinstance(incidents, dict): incidents = list(incidents.values())
        orders = list(state.get('orders', {}).values())
        expected = 2 if self.args.case == 'urgent' else 1
        specimen_ok = len(orders) == expected and all(order.get('status') == 'delivered' and order.get('receipt') and not order.get('loaded') for order in orders)
        lights = [incident for incident in incidents if incident.get('kind') == 'low_light']
        light_ok = True if self.args.case == 'urgent' else any(incident.get('room_id') == ('room301' if self.args.case == 'normal' else 'room303') and incident.get('status') == ('resolved' if self.args.case == 'normal' else 'unresolved') for incident in lights)
        ticket_ok = self.args.case != 'recovery' or bool(ward.get('maintenance_tickets'))
        injection_ok = (True if self.args.case == 'normal' else self.details.get('urgent_injected', False)
                        if self.args.case == 'urgent' else self.details.get('receiver_restored', False))
        terminal = bool(incidents) and all(incident.get('status') in {'resolved', 'unresolved'} for incident in incidents)
        passed = bool(specimen_ok and light_ok and ticket_ok and injection_ok and terminal and state.get('stopped_confirmed'))
        return {'behavior_pass': passed, 'specimen_delivery_pass': specimen_ok, 'light_behavior_pass': light_ok,
                'maintenance_ticket_present': ticket_ok, 'all_incidents_terminal': terminal,
                'expected_orders': expected, 'actual_orders': len(orders), 'incidents': incidents,
                'receipts': [order['receipt'] for order in orders if order.get('receipt')],
                'delivery_order': [order['package_id'] for order in sorted(orders, key=lambda order: (order.get('receipt') or {}).get('sim_time', float('inf'))) if order.get('receipt')],
                'services_all_resolved': bool(incidents) and all(incident.get('status') == 'resolved' for incident in incidents)}

    def observe(self):
        consecutive_errors = 0
        while time.monotonic() < self.observation_end and not self.interrupted:
            try:
                state = self.state(); consecutive_errors = 0
            except (OSError, RuntimeError) as error:
                consecutive_errors += 1; self.record('state_read_error', error=str(error))
                if consecutive_errors >= 3: raise
                time.sleep(.5); continue
            self.conditional_injections(state)
            if self.manual_ui:
                previous = self.details.get('manual_ui_evidence', {}).get('passed')
                proof = manual_ui_evidence(state, self.api('/api/demo/status', facade=True), self.session, self.ui_boundary_wall)
                self.details['manual_ui_evidence'] = proof
                if proof['passed'] and not previous:
                    write_json(self.directory/'manual-ui-evidence.json', proof)
                    self.record('manual_ui_buttons_ack_and_demands_observed', proof=proof)
            if state.get('operator_stopped') or (state.get('mhs_budget') or {}).get('stopped_reason'):
                self.details['premature_stop'] = state.get('operator_stopped') or state['mhs_budget']['stopped_reason']; break
            if self.business_result(state)['behavior_pass']:
                if self.manual_ui and not self.details.get('manual_ui_evidence', {}).get('passed'):
                    time.sleep(.5); continue
                rows = ledger_rows(self.root, self.session, self.seq_start)
                proof = completion_evidence(state, rows)
                if proof['passed']:
                    self.details['pre_stop_completion'] = proof
                    self.details['agent_final_turn_observed'] = True
                    write_json(self.directory/'agent-completion-before-stop.json', {'proof': proof, 'state': state})
                    break
            time.sleep(.5)
        self.details['interrupted'] = self.interrupted
        self.details['wall_budget_reached'] = time.monotonic() >= self.observation_end

    def stop(self):
        if not self.run_id or not self.args.stop_after:
            return
        try:
            result = self.api('/api/demo/command', {'action': 'stop', 'operation_id': 'accept-stop-'+uuid.uuid4().hex,
                              'expected_run_id': self.run_id, 'session_id': self.session}, facade=True)
            self.record('stop_requested', result=result)
        except Exception as error:
            self.record('stop_request_error', error=str(error))
        end = min(time.monotonic()+25, self.deadline-25)
        while time.monotonic() < end:
            try:
                state = self.state()
                if state.get('stopped_confirmed') and not state.get('smart_ward', {}).get('watch', {}).get('enabled'):
                    self.details['final_stop_confirmed'] = True; return
            except Exception as error:
                self.record('stop_observation_error', error=str(error))
            time.sleep(.5)
        self.details['final_stop_confirmed'] = False

    def run(self):
        error = None; began = False
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'interrupted', True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'interrupted', True))
        sources = ['mhs/smart_acceptance.py', 'mhs/smart_scene.py', 'mhs/smart_ward.py', 'mhs/workbench.js',
                   'mhs/bridge.py', 'configs/smart-ward-v1.json', 'configs/stations.json']
        write_json(self.directory/'protocol.json', {'case': self.args.case, 'wall_limit_seconds': self.args.wall_seconds,
                   'started_wall': self.started_wall, 'environment_only_driver': True, 'stop_after': self.args.stop_after,
                   'input_mode': 'manual_ui_observation' if self.manual_ui else 'scripted_environment',
                   'driver_starts_watch': not self.manual_ui, 'driver_injects_environment': not self.manual_ui,
                    'recovery_environment': RECOVERY_ENVIRONMENT if self.args.case == 'recovery' else None,
                   'source_sha256': {name: hashlib.sha256((self.root/name).read_bytes()).hexdigest() if (self.root/name).exists() else None for name in sources}})
        try:
            self.preflight(); began = not self.manual_ui
            from .record import Recorder
            self.recorder = Recorder(self.directory/'video').start()
            if self.manual_ui:
                self.wait_for_ui_start(); began = True
            else: self.initial_injections()
            self.observe()
        except Exception as failure:
            error = str(failure); self.record('driver_failed', error=error)
        finally:
            self.stop()
            if self.recorder:
                try: self.recorder.stop()
                except Exception as failure: self.record('recorder_stop_error', error=str(failure))
        state = self.last_state or {}
        try:
            state = self.state()
            write_json(self.directory/'api-report.json', self.api('/api/report'))
        except Exception as failure:
            self.record('final_snapshot_error', error=str(failure))
        if self.manual_ui:
            try:
                proof = manual_ui_evidence(state, self.api('/api/demo/status', facade=True), self.session, self.ui_boundary_wall)
            except Exception as failure:
                proof = {'passed': False, 'error': str(failure)}
            self.details['manual_ui_evidence'] = proof
            write_json(self.directory/'manual-ui-evidence.json', proof)
        write_json(self.directory/'final-state.json', state)
        rows = []
        if self.session:
            try: rows = ledger_rows(self.root, self.session, self.seq_start)
            except Exception as failure: self.record('ledger_read_error', error=str(failure))
        for row in rows: append_json(self.directory/'session.jsonl', row)
        audit = summarize_ledger(rows); write_json(self.directory/'session-audit.json', audit)
        behavior = self.business_result(state)
        device_calls = [row['arguments'] for row in audit['device_calls']]
        actual_navigation = any(call.get('device') == 'hospital-robot-01' and call.get('tool') == 'navigate_route' for call in device_calls)
        handoff_devices = {call.get('device') for call in device_calls if call.get('tool') == 'handoff_order'}
        expected_handoff_devices = {'hospital-cabinet-b', 'hospital-cabinet-lab'} | ({'hospital-cabinet-c'} if self.args.case == 'urgent' else set())
        actual_handoff = expected_handoff_devices <= handoff_devices
        actual_light = self.args.case == 'urgent' or any(call.get('tool') == 'set_brightness' and call.get('device') == ('hospital-light-301' if self.args.case == 'normal' else 'hospital-light-303') for call in device_calls)
        verification = any(item['report'].get('run_id') == self.run_id and item['report'].get('total') == len(behavior['incidents'])
                           and item['report'].get('specimen_delivered') == len(behavior['receipts']) and not item['report'].get('pending') for item in audit['verifications'])
        agent_pass = bool(audit['request_count'] and actual_navigation and actual_handoff and actual_light and verification
                          and self.details.get('agent_final_turn_observed') and self.details.get('pre_stop_completion', {}).get('passed'))
        recovery = None if self.args.case != 'recovery' else recovery_evidence_pass(self.details, behavior['specimen_delivery_pass'])
        trigger_pass = self.args.case != 'urgent' or bool(self.details.get('urgent_injection_after_departure') and not self.details.get('urgent_injection_late'))
        video_frames = self.directory/'video/frames.jsonl'
        frames = [json.loads(line) for line in video_frames.read_text(encoding='utf-8').splitlines()] if video_frames.exists() else []
        frame_count = len(frames)
        unique_frames = {(frame.get('capture_wall_time'), frame.get('sha256')) for frame in frames}
        frame_audit = {'recorded_frames': frame_count, 'unique_captures': len(unique_frames),
                       'first_capture_wall': frames[0].get('capture_wall_time') if frames else None,
                       'last_capture_wall': frames[-1].get('capture_wall_time') if frames else None,
                       'sim_time_advanced': len({frame.get('sim_time') for frame in frames}) >= 2}
        write_json(self.directory/'frame-audit.json', frame_audit)
        passed = bool(began and not error and not self.interrupted and not self.details.get('premature_stop') and not self.details.get('wall_budget_reached')
                      and behavior['behavior_pass'] and agent_pass and trigger_pass and len(unique_frames) >= 3 and frame_audit['sim_time_advanced']
                      and (recovery is not False) and (not self.args.stop_after or self.details.get('final_stop_confirmed'))
                      and (not self.manual_ui or self.details.get('manual_ui_evidence', {}).get('passed')))
        report = {'case': self.args.case, 'status': 'passed' if passed else 'failed_or_incomplete' if began else 'NA',
                  'error': error, 'run_id': self.run_id, 'session_id': self.session,
                  'session_url': 'http://127.0.0.1:4189/?session='+urllib.parse.quote(self.session or ''),
                  'workbench_url': 'http://127.0.0.1:4193/?session='+urllib.parse.quote(self.session or ''),
                  'behavior': behavior, 'agent_pass': agent_pass, 'recovery_pass': recovery, 'scenario_trigger_pass': trigger_pass,
                  'input_mode': 'manual_ui_observation' if self.manual_ui else 'scripted_environment',
                   'recovery_environment': RECOVERY_ENVIRONMENT if self.args.case == 'recovery' else None,
                  'manual_ui_gate': self.details.get('manual_ui_evidence', {}).get('passed', False) if self.manual_ui else None,
                  'details': self.details, 'injections': self.injections, 'audit': audit,
                  'actual_frame_count': frame_count, 'frame_audit': frame_audit, 'video': 'video/simulation.mp4' if frame_count else None,
                  'elapsed_wall_seconds': time.monotonic()-self.started,
                  'limitations': [('UI facade operations and scene ACKs were observed; separate CUA actions/screenshots establish actual browser clicks.' if self.manual_ui else 'This is an environment-driven real-Agent demonstration, not a browser click acceptance.'),
                                  'Rendering is recorded from actual JPEGs; playback duration is not wall-clock experiment duration.',
                                  'Sensors and package loading/unloading are explicitly simulated.']}
        write_json(self.directory/'report.json', report)
        print(json.dumps({'status': report['status'], 'report': str(self.directory/'report.json')}, ensure_ascii=False))
        return 0 if passed else 1 if began else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--output', help='New evidence directory inside this project; existing directories are rejected.')
    parser.add_argument('--wall-seconds', type=int, default=3600, help='Total bounded run including teardown; 120..3600.')
    parser.add_argument('--stop-after', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--manual-ui', action='store_true', help='Normal only: observe actual browser start/event buttons; never start watch or inject environment from this driver.')
    arguments = parser.parse_args()
    if not 120 <= arguments.wall_seconds <= 3600:
        parser.error('--wall-seconds must be within 120..3600')
    if arguments.manual_ui and arguments.case != 'normal':
        parser.error('--manual-ui currently supports --case normal only')
    return Demonstration(arguments).run()


if __name__ == '__main__':
    raise SystemExit(main())
