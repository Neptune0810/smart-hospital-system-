"""Observe a real Agnes receiver-timeout boundary, never a delivered-order PASS.

Only three environmental commands are permitted: insert one ordinary B specimen,
disable Lab after verified real arrival/stillness, and restore Lab for cleanup.
The production workbench alone prompts Agnes; this observer never creates an
order, plans a route, hands off, verifies/finalizes on its behalf, or retries a
run. A missed delivery window, missing evidence, or insufficient original
3600-wall-second budget is retained explicitly.
"""
import argparse
import datetime
import hashlib
import json
import math
import re
import signal
import time
import urllib.parse
import uuid
from pathlib import Path

from .smart_acceptance import (Demonstration, REQUIRED_DEVICES, append_json,
    call_arguments, ledger_rows, receiver_false_evidence, receiver_watch_records,
    summarize_ledger, write_json)
from .smart_navigation import arrived, physical_stationary, recording_summary

VERSION = 'smart-receiver-timeout-20261013.1'
REASON = 'receiver_unavailable_150_sim_seconds'
OFFICIAL_CONTRACT = {'commit': '57d0c34b33527294042b7ab8a60d7c7c934e932e',
    'packages/mhs/server/brain.ts': 'ac52b842aeab2dd52ed11daa966941d96b4019b8bf3d2e0d37e39b7a1cf09d85',
    'packages/mhs/server/north.ts': 'c915301247b3d02e5405211773019068adb3e7833cf9541599ffca8f04c4279b',
    'packages/mhs/plugin/tools.ts': '6e789518aa0dfd7fc43bdbb2b1f06b424ce2bf9cbf2f6651965e5935c5f66ee8',
    'scope': 'Pinned reply structured data, Brain.watchDevice, North.fire/describeUntil eq and merged Brain.wake text; hidden watch event IDs are not exposed to this observer.'}
LIMITS = {'real_model_requests': 60, 'simulation_seconds': 2400,
          'wall_seconds': 3600, 'receiver_unavailable_sim_seconds': 150,
          'cleanup_reserve_wall_seconds': 90, 'maximum_environment_commands': 3}
SOURCES = ('mhs/smart_receiver_timeout.py', 'mhs/test_smart_receiver_timeout.py',
    'mhs/smart_acceptance.py', 'mhs/smart_navigation.py', 'mhs/smart_ward.py',
    'mhs/engine_v2.py', 'mhs/engine.py', 'mhs/smart_scene.py', 'mhs/bridge.py',
    'mhs/workbench.js', 'mhs/record.py', 'mhs/policy/index.js',
    'mhs/policy/completion.js', 'mhs/policy/model-requests.js',
    'mhs/policy/instructions-smart.md', 'mhs/policy/dist/index.mjs',
    'configs/stations.json', 'configs/smart-ward-v1.json')


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def wall(row):
    try:
        return datetime.datetime.fromisoformat(row['ts'].replace('Z', '+00:00')).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def text(row):
    parts = row.get('data', {}).get('content', [])
    return '\n'.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text') if isinstance(parts, list) else ''


def successful_tools(rows, suffix):
    calls = {row.get('data', {}).get('toolUseId'): row for row in rows
             if row.get('type') == 'tool/call' and row.get('data', {}).get('toolUseId')
             and row.get('data', {}).get('name', '').endswith(suffix)}
    matches = []
    for result in rows:
        data = result.get('data', {})
        call = calls.get(data.get('toolUseId'))
        if result.get('type') != 'tool/result' or data.get('isError') is True or not call or result.get('seq', 0) <= call['seq']:
            continue
        body = None
        for part in data.get('content', []):
            if part.get('type') == 'text':
                try:
                    candidate = json.loads(part['text'])
                    if isinstance(candidate, dict) and candidate.get('ok') is not False:
                        body = candidate.get('result', candidate)
                        break
                except (KeyError, ValueError, TypeError):
                    pass
        matches.append({'call': call, 'result': result, 'body': body})
    return matches


def timing_estimate(report, current_rtf, available_wall, current_start=None):
    """Measured prior real B/Lab legs + current real clock slope; no invented ETA."""
    unknown = {'go': False, 'reason': 'timing_evidence_missing_or_invalid'}
    if not number(current_rtf) or current_rtf <= 0 or not number(available_wall):
        return unknown
    legs = []
    for name, station in (('arrival_b', 'station_b'), ('arrival_lab', 'lab')):
        candidates = [row for row in report.get('cases', []) if row.get('case') == name and row.get('status') == 'passed']
        if len(candidates) != 1:
            return unknown
        details = candidates[0].get('details', {})
        nav, state = details.get('navigation', {}), details.get('state', {})
        task = state.get('navigation_tasks', {}).get(nav.get('task_id'), {})
        start, end = nav.get('started_wall'), details.get('confirmed_wall')
        start_sim, end_sim = nav.get('before', {}).get('sim_time'), state.get('sim_time')
        if (nav.get('station') != station or task.get('status') != 'succeeded' or task.get('goal_station') != station
                or details.get('hub_result', {}).get('status') != 'done'
                or not all(number(v) for v in (start, end, start_sim, end_sim)) or end <= start or end_sim <= start_sim):
            return unknown
        if name == 'arrival_b' and current_start is not None:
            before = nav.get('before', {}).get('physical_pose', {}).get('position', [])
            if len(before) < 2 or len(current_start) < 2 or math.dist(before[:2], current_start[:2]) > .5:
                return {'go': False, 'reason': 'historical_b_start_differs_from_current_start'}
        legs.append({'case': name, 'historical_wall_seconds': end-start, 'historical_sim_seconds': end_sim-start_sim,
                     'forecast_wall_seconds': max(end-start, (end_sim-start_sim)/current_rtf)})
    wait = 150/current_rtf
    # This explicit reserve is not a measured model latency or a completion guarantee.
    model_reserve, cleanup = 300, LIMITS['cleanup_reserve_wall_seconds']
    estimate = sum(row['forecast_wall_seconds'] for row in legs)+wait+model_reserve+cleanup
    return {'go': estimate <= available_wall, 'reason': 'bounded_forecast_fits' if estimate <= available_wall else 'original_wall_budget_known_insufficient',
            'current_measured_rtf': current_rtf, 'legs': legs, 'receiver_wait_forecast_wall_seconds': wait,
            'unmeasured_model_and_handoff_reserve_wall_seconds': model_reserve,
            'cleanup_reserve_wall_seconds': cleanup, 'forecast_total_wall_seconds': estimate,
            'available_wall_seconds': available_wall,
            'limitation': 'Feasibility forecast only. Actual model latency, route choice and clock slope can vary; no budget is extended.'}


def lab_arrival_window(state, observation, specimen_id, run_id, epoch):
    ward = state.get('smart_ward', {})
    orders = list(state.get('orders', {}).values())
    if len(orders) != 1 or orders[0].get('specimen_id') != specimen_id:
        return None
    order = orders[0]
    if order.get('receipt') or order.get('status') == 'delivered':
        raise RuntimeError('real_delivery_window_missed_already_delivered')
    receiving = [op for op in ward.get('operations', {}).values()
                 if op.get('status') in {'pending', 'unknown'} and op.get('command', {}).get('kind') == 'handoff'
                 and op.get('command', {}).get('arguments', {}).get('cabinet_id') == 'hospital-cabinet-lab']
    if receiving:
        raise RuntimeError('real_delivery_window_missed_receiver_handoff_already_prepared')
    tasks = [(tid, task) for tid, task in state.get('navigation_tasks', {}).items() if task.get('goal_station') == 'lab']
    for tid, task in reversed(tasks):
        if (order.get('loaded') is True and ward.get('specimens', {}).get(specimen_id, {}).get('location') == 'robot'
                and observation.get('specimens', {}).get(specimen_id, {}).get('location') == 'robot'
                and state.get('receivers', {}).get('lab') is True
                and state.get('receiver_unavailable_since', {}).get('lab') is None
                and arrived(state, observation, 'lab', tid, run_id, epoch)):
            return {'order_id': order['order_id'], 'task_id': tid, 'task': task}
    return None


def accepted_watch_data(record):
    """Official Reply.structured is primary; only the exact pinned text is fallback."""
    args = call_arguments(record['call'])
    if args.get('device') != 'hospital-robot-01' or args.get('until') != {'state': 'delivery_wait_lab', 'eq': True}:
        return None
    reply = record['result'].get('data', {})
    data = reply.get('structured')
    observed = 'tool_result_structured'
    if data is None:
        pattern = (r'Watching hospital-robot-01 as (w[1-9][0-9]*)\. If nothing else needs doing now, end your turn: '
            r'this conversation is woken with a message when it happens or after the timeout\. Do not wait, sleep or poll with other tools\.')
        match = re.fullmatch(pattern, text(record['result']))
        if not match:
            return None
        supplied = args.get('timeout', 600)
        if not number(supplied): return None
        data = {'watch': match[1], 'device': args['device'], 'until': args['until'], 'timeout': min(supplied, 3600)}
        if 'note' in args: data['note'] = args['note']
        observed = 'exact_pinned_watch_return_text_and_original_arguments'
    if not isinstance(data, dict): return None
    duration, note = data.get('timeout'), data.get('note')
    supplied = args.get('timeout', 600)
    if (not re.fullmatch(r'w[1-9][0-9]*', str(data.get('watch', '')))
            or data.get('device') != args['device'] or data.get('until') != args['until']
            or not number(duration) or not 1 <= duration <= 3600 or not number(supplied)
            or duration != min(supplied, 3600) or note != args.get('note') or note is not None and not isinstance(note, str)):
        return None
    return {**data, 'reference_source': observed}


def native_watch_linkage(rows, watches, timeout_wall, first_false_wall):
    """Unique live accepted watch + exact official matched text, not a hidden ID."""
    result = {'passed': False, 'status': 'NA', 'reason': 'no_unique_live_accepted_watch_and_exact_native_message',
        'proof_level': 'pinned_official_text_and_unique_accepted_watch',
        'native_event_id_directly_observed': False,
        'native_event_id_status': 'NA: North.fire private callback is not globally broadcast',
        'accepted_watch_records': watches, 'official_contract': OFFICIAL_CONTRACT}
    parsed = []
    for record in watches:
        data = accepted_watch_data(record)
        stamp = wall(record['call'])
        if data and number(stamp): parsed.append({'record': record, 'data': data, 'wall': stamp})
    candidates = [row for row in rows if row.get('type') == 'user/message' and number(wall(row))
                  and number(timeout_wall) and wall(row) >= timeout_wall and text(row).startswith('[AgnesHub] ')]
    result['actual_native_message_candidates'] = candidates
    consumed = set()
    for message in [r for r in rows if r.get('type') == 'user/message' and text(r).startswith('[AgnesHub] ') and number(wall(r))]:
        lines = text(message)[len('[AgnesHub] '):].split('\n')
        for line in lines:
            compatible = []
            for watch in parsed:
                data, record, start = watch['data'], watch['record'], watch['wall']
                note = ' ('+data['note']+')' if data.get('note') else ''
                matched = 'hospital-robot-01: delivery_wait_lab = true'+note+'.'
                expired = 'hospital-robot-01: waited '+format(data['timeout'], 'g')+' s and delivery_wait_lab = true did not happen'+note+'.'
                canceled = any(call_arguments(item['call']).get('watch') == data['watch']
                    and record['result']['seq'] < item['call']['seq'] < message['seq'] for item in successful_tools(rows, 'unwatch_device'))
                if data['watch'] in consumed or canceled or record['result']['seq'] >= message['seq']:
                    continue
                if line == expired:
                    consumed.add(data['watch']); continue
                if line == matched and 0 <= wall(message)-start < data['timeout']:
                    compatible.append(watch)
            if len(compatible) > 1:
                if wall(message) >= timeout_wall:
                    result.update(reason='multiple_indistinguishable_live_accepted_watches', ambiguous=compatible, message=message)
                    return result
                # One of these private watches fired; its identity was not
                # exposed. None can safely be reused as proof of a later event.
                consumed.update(w['data']['watch'] for w in compatible)
            if len(compatible) == 1:
                watch = compatible[0]
                consumed.add(watch['data']['watch'])
                if number(first_false_wall) and first_false_wall <= watch['wall'] <= timeout_wall <= wall(message):
                    result.update(passed=True, status='passed', reason='unique_live_watch_exact_official_matched_message',
                        watch=watch, message=message, message_seq=message['seq'], exact_matched_line=line)
                    return result
    return result


def incomplete_evidence(state, observation, rows, identity, boundary, first_since, native):
    """A closed undelivered task is expected; no cleanup state may prove it."""
    ward, budget = state.get('smart_ward', {}), state.get('mhs_budget') or {}
    sid, oid, iid = identity.get('specimen_id'), identity.get('order_id'), identity.get('incident_id')
    order = state.get('orders', {}).get(oid, {})
    incident = next((i for i in ward.get('incidents', []) if i.get('incident_id') == iid), {})
    since, sim = state.get('receiver_unavailable_since', {}).get('lab'), state.get('sim_time')
    timeout_ok = number(first_since) and since == first_since and number(sim) and sim-first_since >= 150
    failed_events = [e for e in state.get('events', []) if e.get('kind') == 'order_failed' and e.get('order_id') == oid and e.get('reason') == REASON]
    retained = boundary.get('failure_event')
    if isinstance(retained, dict) and retained not in failed_events and retained.get('kind') == 'order_failed' and retained.get('order_id') == oid and retained.get('reason') == REASON:
        failed_events.append(retained)
    failure_after_150 = any(number(e.get('sim_time')) and e['sim_time']-first_since >= 150 for e in failed_events) if number(first_since) else False
    # At this point the receiver must still be unavailable: restore is cleanup only.
    false_proof = receiver_false_evidence(state, boundary, time.time())
    watches = [w for w in receiver_watch_records(rows, boundary.get('ledger_seq', 0))
               if number(wall(w['result'])) and wall(w['result']) <= boundary.get('timeout_wall', -1)]
    deferred = [item for item in successful_tools(rows, 'defer_order')
                if call_arguments(item['call']).get('order_id') == oid
                and item['call']['seq'] > boundary.get('ledger_seq', 0)
                and number(wall(item['result'])) and wall(item['result']) <= boundary.get('timeout_wall', -1)
                and isinstance(item['body'], dict) and item['body'].get('order', {}).get('status') == 'deferred']
    ends = [r for r in rows if r.get('type') == 'turn/end']
    end = ends[-1] if ends else {}
    no_later_start = bool(end and not any(r.get('type') == 'turn/start' and r['seq'] > end['seq'] for r in rows))
    version, episode = ward.get('verification_event_version'), ward.get('episode')
    verified = ward.get('last_verification') or {}
    reports = [item for item in summarize_ledger(rows)['verifications']
        if item['report'].get('run_id') == state.get('run_id') and item['report'].get('episode') == episode
        and item['report'].get('verification_event_version') == version and item['report'].get('ready_to_finalize') is True
        and not item['report'].get('pending') and item['report'].get('specimen_delivered') == 0
        and any(i.get('incident_id') == iid and i.get('status') == 'unresolved' for i in item['report'].get('unresolved', []))
        and end and item['seq'] < end['seq']]
    after_wake = native.get('message_seq', math.inf)
    reads = [item for item in successful_tools(rows, 'get_state') if item['call']['seq'] > after_wake
        and isinstance(item['body'], dict) and item['body'].get('run_id') == state.get('run_id')
        and item['body'].get('mhs_outcome') == 'incomplete'
        and item['body'].get('orders', {}).get(oid, {}).get('reason') == REASON
        and reports and item['result']['seq'] < reports[-1]['seq']]
    reads += [item for item in successful_tools(rows, 'read_device') if item['call']['seq'] > after_wake
        and call_arguments(item['call']).get('device') == 'hospital-robot-01'
        and reports and item['result']['seq'] < reports[-1]['seq']]
    final = [row for row in rows if row.get('type') == 'assistant/message' and end and reports
        and reports[-1]['seq'] < row['seq'] < end['seq']]
    final_text = '\n'.join(text(row) for row in final)
    creates = [item for item in successful_tools(rows, 'create_specimen_delivery')
        if call_arguments(item['call']).get('incident_id') == iid and isinstance(item['body'], dict)
        and any(o.get('order_id') == oid and o.get('specimen_id') == sid for o in item['body'].get('orders', []))]
    plans = successful_tools(rows, 'publish_service_plan')
    queries = successful_tools(rows, 'get_route_options')
    device_calls = successful_tools(rows, 'call_device')
    navigated = {call_arguments(item['call']).get('arguments', {}).get('route_id') for item in device_calls
        if call_arguments(item['call']).get('device') == 'hospital-robot-01' and call_arguments(item['call']).get('tool') == 'navigate_route'}
    tasks = state.get('navigation_tasks', {})
    actual_routes = {task.get('goal_station'): task.get('route_id') for task in tasks.values() if task.get('status') == 'succeeded'}
    handoffs = [item for item in device_calls if call_arguments(item['call']).get('device') == 'hospital-cabinet-b'
        and call_arguments(item['call']).get('tool') == 'handoff_order' and call_arguments(item['call']).get('arguments', {}).get('order_id') == oid]
    transfers = [(tid, t) for tid, t in ward.get('transfers', {}).items() if t.get('specimen_id') == sid
        and t.get('direction') == 'pickup' and t.get('status') == 'committed' and t.get('location') == 'robot']
    pickup_records = []
    for tid, transfer in transfers:
        op = ward.get('operations', {}).get(transfer.get('operation_id'), {})
        args = op.get('command', {}).get('arguments', {})
        ack = op.get('ack') or {}
        actual = ack.get('result', {}).get('transfer', ack.get('result', {}))
        sampled = observation.get('transfers', {}).get(tid, {})
        if (args.get('order_id') == oid and args.get('cabinet_id') == 'hospital-cabinet-b'
                and (ack.get('run_id'), ack.get('epoch'), ack.get('operation_id'), ack.get('status'))
                    == (identity.get('run_id'), identity.get('epoch'), transfer.get('operation_id'), 'completed')
                and actual.get('transfer_id') == tid and actual.get('specimen_id') == sid and actual.get('order_id') == oid
                and actual.get('cabinet_id') == 'hospital-cabinet-b' and actual.get('location') == 'robot'
                and sampled.get('specimen_id') == sid and sampled.get('location') == 'robot'
                and sampled.get('operation_id') == transfer.get('operation_id')):
            pickup_records.append({'transfer': transfer, 'operation': op, 'original_ack': ack, 'new_scene_transfer': sampled})
    intents = [row for row in rows if row.get('type') == 'effect/intent' and row.get('data', {}).get('kind') == 'inference'
               and number(wall(row)) and number(budget.get('start_wall')) and wall(row) >= budget['start_wall']]
    ids = [row['data'].get('effectId') for row in intents]
    metered = bool(ids and all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
        and set(ids) == set(ward.get('model_request_ids', [])) and budget.get('decisions') == len(ids)
        and 0 < len(ids) <= 60 and number(budget.get('end_wall'))
        and budget['end_wall'] >= max(wall(row) for row in intents))
    checks = {'identity': state.get('run_id') == identity.get('run_id') and ward.get('epoch') == identity.get('epoch'),
        'single_actual_specimen_order_incident': len(state.get('orders', {})) == len(ward.get('specimens', {})) == len(ward.get('incidents', [])) == 1,
        'production_since_and_150_sim_seconds': timeout_ok and failure_after_150,
        'failed_exact_reason_retained_cargo': order.get('status') == 'failed' and order.get('reason') == REASON and order.get('loaded') is True and state.get('cargo_count') == 1,
        'actual_owner_robot': ward.get('specimens', {}).get(sid, {}).get('location') == observation.get('specimens', {}).get(sid, {}).get('location') == 'robot',
        'no_receiving_receipt': not any(o.get('receipt') for o in state.get('orders', {}).values()) and not observation.get('devices', {}).get('hospital-cabinet-lab', {}).get('specimens'),
        'incident_unresolved': incident.get('status') == 'unresolved',
        'incomplete_recovery_required': state.get('mhs_outcome') == 'incomplete' and ward.get('recovery_required') is True,
        'no_pending_unknown_handoff': not any(o.get('status') in {'pending', 'unknown'} and o.get('command', {}).get('kind') == 'handoff' for o in ward.get('operations', {}).values()),
        'fresh_actual_stop': physical_stationary(state, observation, identity.get('run_id'), identity.get('epoch')) and ward.get('scene_fresh') is True,
        'fresh_receiver_still_false': false_proof['passed'] and state.get('receivers', {}).get('lab') is False,
        'actual_agent_defer': bool(deferred), 'accepted_native_wait_before_timeout': bool(watches),
        'actual_agent_created_planned_queried_routes': bool(creates and plans and queries),
        'actual_agent_b_then_lab_navigation': all(actual_routes.get(station) in navigated for station in ('station_b', 'lab'))
            and all(actual_routes.get(station) for station in ('station_b', 'lab')),
        'actual_agent_pickup_ack_and_new_scene': bool(handoffs and len(transfers) == 1 and len(pickup_records) == 1),
        'native_wait_wake_linked': native.get('passed') is True,
        'agent_reread_after_native_wake': bool(reads),
        'current_event_verification': bool(reports and verified.get('episode') == episode and verified.get('event_version') == version),
        'explicit_undelivered_and_retained_report': bool(re.search(r'未交付|未送达|未能.{0,12}(?:送达|交付|送检)|无法交付|undelivered|not delivered', final_text, re.I)
            and re.search(r'保留|仍.{0,10}(?:车|机器人)|载货|retained|on.{0,8}robot', final_text, re.I)),
        'completed_terminal_turn': bool(end.get('data', {}).get('reason') == 'completed' and no_later_start),
        'final_response_metered_and_finalized': metered and number(wall(end)) and wall(end) >= budget.get('end_wall', math.inf),
        'not_operator_or_budget_stop': not state.get('operator_stopped') and not budget.get('stopped_reason')}
    return {'passed': all(checks.values()), 'checks': checks, 'business_outcome': 'incomplete',
        'delivered_acceptance_passed': False, 'production_receiver_since_sim': first_since,
        'elapsed_receiver_unavailable_sim': sim-first_since if number(sim) and number(first_since) else None,
        'failure_events': failed_events, 'defer_records': deferred, 'accepted_watch_records': watches,
        'native_wake': native, 'post_wake_reads': reads, 'verification_records': reports,
        'terminal_turn': end, 'final_messages': final, 'metered_inference_ids': ids,
        'actual_pickup_records': pickup_records}


class ReceiverTimeout(Demonstration):
    def __init__(self, arguments):
        arguments.case, arguments.manual_ui, arguments.stop_after = 'receiver_timeout', False, True
        if type(arguments.wall_seconds) is not int or not 120 <= arguments.wall_seconds <= 3600:
            raise ValueError('original_wall_budget_must_be_120_to_3600')
        super().__init__(arguments)
        self.observation_end = self.deadline-LIMITS['cleanup_reserve_wall_seconds']
        self.epoch = self.specimen_id = self.order_id = self.incident_id = None
        self.began = False
        self.boundary = None
        self.first_since = None
        self.environment_commands = 0
        self.pause_attempted = self.restore_attempted = False
        self.proof = None

    def observation(self):
        return json.loads((self.root/'runs/smart-ward-v1/scene/observation.json').read_text(encoding='utf-8'))

    def state(self):
        state = super().state()
        ward = state.get('smart_ward', {})
        if self.epoch and ward.get('epoch') != self.epoch:
            raise RuntimeError('epoch_changed_during_receiver_timeout')
        if self.session and state.get('controller_session') != self.session:
            raise RuntimeError('controller_changed_during_receiver_timeout')
        if len(state.get('orders', {})) > 1 or len(ward.get('specimens', {})) > 1 or len(ward.get('incidents', [])) > 1:
            raise RuntimeError('receiver_timeout_single_specimen_scope_exceeded')
        return state

    def preflight(self):
        state = self.state(); ward = state.get('smart_ward', {})
        self.run_id, self.epoch = state['run_id'], ward.get('epoch')
        observation = self.observation()
        checks = {'fresh_unclaimed': not state.get('controller_session') and not state.get('mhs_budget'),
            'smart_ready_empty': state.get('protocol') == 'smart-ward-v1' and state.get('navigation_ready') is True
                and not state.get('orders') and not ward.get('incidents') and not ward.get('specimens')
                and not ward.get('watch', {}).get('enabled') and not state.get('operator_stopped')
                and ward.get('scene_fresh') is True and state.get('motion_guard', {}).get('healthy') is True,
            'no_prior_motion_qualification_operations': not state.get('navigation_tasks') and not ward.get('navigation_qualification') and not ward.get('fault_qualification') and not ward.get('operations'),
            'no_recovery': not any(ward.get(k) for k in ('recovery_required', 'resume_required', 'navigation_recovery_required')),
            'all_eight_fresh_available': set(ward.get('devices', {})) == REQUIRED_DEVICES and all(d.get('fresh') is True and d.get('available') is True for d in ward.get('devices', {}).values()),
            'normal_local_lights': all(ward.get('devices', {}).get('hospital-light-'+room, {}).get('brightness_percent', 0) >= 50 for room in ('301', '303')),
            'actual_stationary': physical_stationary(state, observation, self.run_id, self.epoch),
            'receiver_initially_available': state.get('receivers', {}).get('lab') is True and state.get('receiver_unavailable_since', {}).get('lab') is None}
        facade = self.api('/api/demo/status', facade=True)
        checks['workbench_smart_profile'] = facade.get('profile') == 'smart-ward-v1'
        write_json(self.directory/'strict-preflight.json', {'checks': checks, 'state': state, 'observation': observation})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:'+','.join(k for k, v in checks.items() if not v))
        path = Path(self.args.navigation_report).resolve()
        base = self.root/'runs/smart-ward-v1/acceptance'
        if not path.is_relative_to(base) or path.is_symlink() or path.name != 'report.json':
            raise ValueError('timing_report_requires_explicit_project_acceptance_report')
        for parent in path.parents:
            if parent == self.root: break
            if parent.is_symlink(): raise ValueError('timing_report_symlink_refused')
        prior = json.loads(path.read_text(encoding='utf-8'))
        if not number(prior.get('ended_wall')) or prior.get('ended_wall', math.inf) >= self.started_wall:
            raise ValueError('historical_navigation_report_must_be_closed_before_this_run')
        required_provenance = ('configs/stations.json', 'configs/smart-ward-v1.json', 'runs/smart-ward-v1/scene/map/manifest.json')
        for name in required_provenance:
            recorded = prior.get('source_sha256', {}).get(name)
            if not recorded or hashlib.sha256((self.root/name).read_bytes()).hexdigest() != recorded:
                raise ValueError('historical_navigation_layout_or_map_changed:'+name)
        first_wall, first_sim = time.monotonic(), state['sim_time']
        seqs = set()
        while time.monotonic()-first_wall < 10:
            if time.monotonic() >= self.observation_end or self.interrupted:
                raise TimeoutError('preflight_rtf_budget_exhausted')
            state = self.state(); current = self.observation()
            if not physical_stationary(state, current, self.run_id, self.epoch):
                raise RuntimeError('preflight_rtf_requires_continuous_actual_stop')
            seqs.add(current.get('seq')); time.sleep(.25)
        elapsed = time.monotonic()-first_wall
        rtf = (state['sim_time']-first_sim)/elapsed
        estimate = timing_estimate(prior, rtf, self.deadline-time.monotonic(), state.get('physical_pose', {}).get('position'))
        estimate.update(historical_report=str(path.relative_to(self.root)), historical_report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            current_rtf_measurement={'elapsed_wall_seconds': elapsed, 'start_sim': first_sim, 'end_sim': state['sim_time'], 'actual_scene_sequences': sorted(s for s in seqs if isinstance(s, int))})
        if len(seqs) < 3: estimate.update(go=False, reason='rtf_measurement_requires_three_new_actual_samples')
        self.details['budget_feasibility'] = estimate
        write_json(self.directory/'budget-feasibility.json', estimate)
        if not estimate['go']:
            raise RuntimeError('NoGo:'+estimate['reason'])
        # The facade creates the only production controller. Mark the attempt
        # before posting: an unknown HTTP response still requires real cleanup.
        state = self.state()
        if state.get('controller_session') or state.get('orders') or state['smart_ward'].get('incidents') or state['smart_ward'].get('watch', {}).get('enabled'):
            raise RuntimeError('fresh_preflight_changed_before_start')
        self.session = 'agnes:local:local-dev:cli:workspace:ward-'+self.run_id
        self.began = True
        start = self.api('/api/demo/command', {'action': 'smart_start', 'operation_id': 'receiver-timeout-start-'+uuid.uuid4().hex,
            'expected_run_id': self.run_id}, facade=True)
        write_json(self.directory/'start.json', start)
        if start.get('new_session') != self.session:
            raise RuntimeError('actual_workbench_session_identity_mismatch')
        rows = ledger_rows(self.root, self.session)
        if any(r.get('type') in {'request/sent', 'tool/call', 'user/message'} for r in rows):
            raise RuntimeError('new_controller_already_has_model_or_task_input')
        self.seq_start = rows[-1]['seq'] if rows else 0

    def command(self, kind, arguments, label, cleanup=False):
        permitted = ((kind == 'insert_specimen' and arguments == {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'} and self.environment_commands == 0)
            or (kind == 'set_receiver' and arguments == {'available': False} and not self.pause_attempted and self.environment_commands == 1)
            or (kind == 'set_receiver' and arguments == {'available': True} and cleanup and self.pause_attempted and not self.restore_attempted))
        if not permitted or self.environment_commands >= 3:
            raise RuntimeError('environment_command_scope_exceeded')
        state = self.state()
        if state.get('operator_stopped'):
            raise RuntimeError('environment_restore_cannot_bypass_operator_stop')
        if not cleanup and (self.interrupted or time.monotonic() >= self.observation_end):
            raise TimeoutError('environment_command_work_budget_exhausted')
        self.environment_commands += 1
        if kind == 'set_receiver':
            if arguments['available']: self.restore_attempted = True
            else: self.pause_attempted = True
        command = {'kind': kind, 'arguments': arguments, 'expected_run_id': self.run_id,
                   'operation_id': 'receiver-timeout-'+label+'-'+uuid.uuid4().hex}
        item = {'command': command, 'requested_wall': time.time()}
        self.injections.append(item)
        self.record('environment_command_submitted', **item)
        item['accepted'] = self.api('/api/admin/smart-ward/command', command)
        end = min(time.monotonic()+20, self.deadline if cleanup else self.observation_end)
        while time.monotonic() < end:
            operation = self.api('/api/ward/operation?id='+urllib.parse.quote(command['operation_id']))
            if time.monotonic() >= end:
                raise TimeoutError('environment_ack_response_after_finite_deadline:'+command['operation_id'])
            if operation.get('status') == 'failed':
                item['result'] = operation
                raise RuntimeError('actual_environment_command_failed:'+json.dumps(operation, ensure_ascii=False))
            if operation.get('status') == 'completed':
                ack = operation.get('ack') or {}
                if (ack.get('run_id'), ack.get('epoch'), ack.get('operation_id'), ack.get('status')) != (self.run_id, self.epoch, command['operation_id'], 'completed'):
                    raise RuntimeError('actual_environment_ack_identity_missing')
                if kind == 'set_receiver' and ack.get('result', {}).get('available') is not arguments['available']:
                    raise RuntimeError('actual_receiver_ack_value_mismatch')
                item['result'] = operation
                self.record('environment_command_actual_ack', operation=operation)
                return operation
            time.sleep(.1)
        raise TimeoutError('environment_command_unknown_no_new_id_retry:'+command['operation_id'])

    def observe_timeout(self):
        clock_samples = []
        while time.monotonic() < self.observation_end and not self.interrupted:
            state, observation = self.state(), self.observation()
            clock_samples.append((time.monotonic(), state['sim_time']))
            clock_samples = [sample for sample in clock_samples if sample[0] >= clock_samples[-1][0]-20]
            rows = ledger_rows(self.root, self.session, self.seq_start)
            if state.get('operator_stopped') or (state.get('mhs_budget') or {}).get('stopped_reason'):
                raise RuntimeError('production_operator_or_budget_stop_before_boundary_completion')
            if self.boundary is None:
                window = lab_arrival_window(state, observation, self.specimen_id, self.run_id, self.epoch)
                if window:
                    duration = clock_samples[-1][0]-clock_samples[0][0]
                    rtf = ((clock_samples[-1][1]-clock_samples[0][1])/duration if duration >= 5
                           else self.details['budget_feasibility']['current_measured_rtf'])
                    remaining = self.deadline-time.monotonic()
                    needed = 150/rtf+300+LIMITS['cleanup_reserve_wall_seconds'] if number(rtf) and rtf > 0 else math.inf
                    forecast = {'current_measured_rtf': rtf, 'clock_samples': clock_samples,
                        'available_wall_seconds': remaining, 'needed_wall_seconds': needed if math.isfinite(needed) else None,
                        'go': math.isfinite(needed) and needed <= remaining,
                        'unmeasured_model_and_handoff_reserve_wall_seconds': 300,
                        'reason': 'Recheck the unchanged deadline before disabling an actually arrived receiver.'}
                    self.details['arrival_budget_feasibility'] = forecast
                    write_json(self.directory/'arrival-budget-feasibility.json', forecast)
                    if not forecast['go']:
                        raise RuntimeError('NoGo:remaining_original_budget_insufficient_for_150_sim_wait')
                    self.order_id = window['order_id']
                    self.incident_id = state['orders'][self.order_id]['incident_id']
                    write_json(self.directory/'real-lab-arrival-before-pause.json', {'window': window, 'state': state, 'observation': observation, 'ledger_last_seq': rows[-1]['seq'] if rows else self.seq_start})
                    boundary = {'run_id': self.run_id, 'epoch': self.epoch, 'sample_seq': (state['smart_ward'].get('scene_sample') or {}).get('seq'),
                        'sim_time': state['sim_time'], 'ledger_seq': rows[-1]['seq'] if rows else self.seq_start}
                    pause = self.command('set_receiver', {'available': False}, 'lab-disable')
                    boundary.update(wall_time=pause['ack']['wall_time'], operation_id=pause['operation_id'])
                    self.boundary = boundary
                    self.details['actual_receiver_disable'] = pause
                    write_json(self.directory/'receiver-pause-boundary.json', boundary)
            else:
                since = state.get('receiver_unavailable_since', {}).get('lab')
                false_proof = receiver_false_evidence(state, self.boundary, time.time())
                if self.first_since is None and number(since) and false_proof['passed']:
                    if since < self.boundary['sim_time']:
                        raise RuntimeError('receiver_since_predates_this_actual_environment_command')
                    self.first_since = since
                    self.boundary['first_false_sample_wall'] = false_proof['sample']['wall_time']
                    write_json(self.directory/'receiver-first-production-since.json', {'production_receiver_since_sim': since, 'first_observed_wall': time.time(), 'state': state, 'fresh_false_evidence': false_proof})
                if self.first_since is not None:
                    if since != self.first_since:
                        raise RuntimeError('receiver_continuous_unavailability_timer_changed')
                    order = state['orders'].get(self.order_id, {})
                    if state['sim_time']-since < 150 and order.get('status') == 'failed':
                        raise RuntimeError('order_closed_before_150_sim_seconds')
                    if state['sim_time']-since >= 150 and order.get('reason') == REASON:
                        if 'timeout_wall' not in self.boundary:
                            events = [e for e in state.get('events', []) if e.get('kind') == 'order_failed' and e.get('order_id') == self.order_id and e.get('reason') == REASON]
                            if not events: raise RuntimeError('actual_timeout_event_missing')
                            self.boundary['timeout_wall'] = events[0]['wall_time']
                            self.boundary['failure_event'] = events[0]
                            write_json(self.directory/'production-timeout-observed.json', {'boundary': self.boundary, 'state': state, 'observation': observation})
                        watches = receiver_watch_records(rows, self.seq_start)
                        native = native_watch_linkage(rows, watches, self.boundary['timeout_wall'], self.boundary['first_false_sample_wall'])
                        self.proof = incomplete_evidence(state, observation, rows, self.identity(), self.boundary, self.first_since, native)
                        if self.proof['passed']:
                            write_json(self.directory/'expected-incomplete-before-stop.json', {'proof': self.proof, 'state': state, 'observation': observation})
                            return
                        # Preserve unknown schema once the actual verified terminal turn is closed.
                        if self.proof['checks']['completed_terminal_turn'] and self.proof['checks']['current_event_verification'] and native.get('status') == 'NA':
                            raise RuntimeError('NA:native_watch_identity_linkage_unverified')
            time.sleep(.2)
        raise TimeoutError('original_receiver_timeout_observation_budget_exhausted')

    def identity(self):
        return {'run_id': self.run_id, 'epoch': self.epoch, 'specimen_id': self.specimen_id,
                'order_id': self.order_id, 'incident_id': self.incident_id}

    def cleanup(self):
        if not self.began:
            return {'mutations': 'none; qualification did not start'}
        # Freeze original evidence before environmental restore or operator stop.
        state = self.state(); observation = self.observation()
        rows = ledger_rows(self.root, self.session, self.seq_start)
        write_json(self.directory/'pre-cleanup-state.json', {'state': state, 'observation': observation, 'proof': self.proof, 'frozen_wall': time.time()})
        for row in rows: append_json(self.directory/'pre-cleanup-session.jsonl', row)
        result = {}
        try:
            write_json(self.directory/'api-report-before-stop.json', self.api('/api/report'))
        except Exception as error:
            result['pre_stop_api_report_error'] = repr(error)
        try:
            if self.pause_attempted:
                restore = self.command('set_receiver', {'available': True}, 'lab-restore', cleanup=True)
                result['restore_ack'] = restore
                end = min(time.monotonic()+20, self.deadline-45)
                seqs = set()
                while time.monotonic() < end:
                    current = self.state(); sample = current['smart_ward'].get('scene_sample') or {}
                    device = current['smart_ward']['devices'].get('hospital-cabinet-lab', {})
                    if (current['smart_ward'].get('scene_fresh') and device.get('fresh') is True and device.get('available') is True
                            and current.get('receivers', {}).get('lab') is True and sample.get('wall_time', 0) > restore['ack']['wall_time']):
                        seqs.add(sample['seq'])
                        if len(seqs) >= 3:
                            result['actual_restore_fresh_sequences'] = sorted(seqs); break
                    time.sleep(.2)
                if len(seqs) < 3: raise RuntimeError('receiver_restore_three_fresh_samples_not_proved')
        except Exception as error:
            result['restore_error'] = repr(error)
        finally:
            try:
                result['stop'] = self.api('/api/demo/command', {'action': 'stop', 'operation_id': 'receiver-timeout-stop-'+uuid.uuid4().hex,
                    'expected_run_id': self.run_id, 'session_id': self.session}, facade=True)
                end = min(time.monotonic()+20, self.deadline-25)
                seqs = set()
                while time.monotonic() < end:
                    state, observation = self.state(), self.observation()
                    if (state.get('operator_stopped') and not state['smart_ward']['watch']['enabled'] and not state.get('motion_guard', {}).get('armed')
                            and physical_stationary(state, observation, self.run_id, self.epoch)):
                        seqs.add(observation['seq'])
                        if len(seqs) >= 3:
                            retained = bool(self.order_id and state.get('cargo_count') == 1
                                and state.get('orders', {}).get(self.order_id, {}).get('loaded') is True
                                and state['orders'][self.order_id].get('status') == 'failed'
                                and state['orders'][self.order_id].get('reason') == REASON
                                and not state['orders'][self.order_id].get('receipt')
                                and state['smart_ward'].get('specimens', {}).get(self.specimen_id, {}).get('location') == 'robot'
                                and observation.get('specimens', {}).get(self.specimen_id, {}).get('location') == 'robot')
                            result.update(physical_stop_confirmed=True, final_cargo_retained=retained,
                                new_scene_sequences=sorted(seqs), state=state, observation=observation)
                            write_json(self.directory/'final-state.json', {'state': state, 'observation': observation})
                            break
                    time.sleep(.2)
                if len(seqs) < 3: raise RuntimeError('cleanup_actual_stop_not_proved')
            except Exception as error:
                result['stop_error'] = repr(error)
        return result

    def run(self):
        error = None; cleanup = {}; source_before = {}; recording = {}
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'interrupted', True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'interrupted', True))
        try:
            source_before = {n: hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in SOURCES}
            write_json(self.directory/'protocol.json', {'qualification_version': VERSION, 'limits': LIMITS,
                'driver_wall_seconds': self.args.wall_seconds, 'source_sha256': source_before,
                'scope': 'one real-Agent expected-incomplete receiver boundary; NOT delivered acceptance',
                'environment': ['one ordinary B specimen', 'Lab false only after real arrival/stillness', 'Lab restore only after evidence freeze'],
                'native_watch_proof': 'Pinned exact matched message plus unique live accepted watch; hidden watch event ID remains independently NA.',
                'official_contract': OFFICIAL_CONTRACT})
            self.preflight()
            from .record import Recorder
            self.recorder = Recorder(self.directory/'video').start()
            insertion = self.command('insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}, 'b-normal')
            self.specimen_id = insertion.get('result', {}).get('specimen_id')
            if not self.specimen_id or insertion['ack'].get('result', {}).get('specimen_id') != self.specimen_id:
                raise RuntimeError('actual_insert_specimen_ack_identity_missing')
            self.observe_timeout()
        except Exception as failure:
            error = type(failure).__name__+':'+str(failure)
            self.record('qualification_failed_or_NA', error=error)
        finally:
            try: cleanup = self.cleanup()
            except Exception as failure: cleanup = {'error': repr(failure)}
            if self.recorder:
                try:
                    self.recorder.stop(); recording = recording_summary(self.directory/'video')
                except Exception as failure: cleanup['recorder_error'] = repr(failure)
        try:
            rows = ledger_rows(self.root, self.session, self.seq_start) if self.session else []
        except Exception as failure:
            rows = []; cleanup['ledger_error'] = repr(failure)
        for row in rows: append_json(self.directory/'session.jsonl', row)
        audit = summarize_ledger(rows)
        try:
            after = {n: hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in source_before}
        except Exception as failure:
            after = {}; cleanup['source_audit_error'] = repr(failure)
        clean = bool(cleanup.get('physical_stop_confirmed') and cleanup.get('final_cargo_retained')
                     and not any(k.endswith('error') for k in cleanup)
                     and (not self.pause_attempted or cleanup.get('actual_restore_fresh_sequences')))
        passed = bool(not error and self.proof and self.proof['passed'] and clean and after == source_before
                      and self.environment_commands == 3 and recording.get('unique_actual_captures', 0) >= 2)
        classification = 'expected_incomplete_pass' if passed else 'NA' if not self.began or error and 'NA:' in error else 'failed_or_incomplete'
        report = {'qualification_version': VERSION, 'status': classification, 'expected_incomplete_pass': passed,
            'business_outcome': 'incomplete' if self.proof and self.proof['checks']['incomplete_recovery_required'] else None,
            'delivered_acceptance_passed': False, 'full_plan_acceptance_passed': False,
            'scope': 'real Agnes expected-incomplete boundary, independently reported from three delivery demonstrations',
            'error': error, **self.identity(), 'session_id': self.session, 'proof': self.proof,
            'limits': LIMITS, 'driver_wall_seconds': self.args.wall_seconds, 'environment_commands_submitted': self.environment_commands,
            'details': self.details, 'injections': self.injections, 'cleanup': cleanup, 'audit': audit,
            'recording': recording, 'source_sha256': source_before, 'source_unchanged': after == source_before,
            'started_wall': self.started_wall, 'ended_wall': time.time(), 'elapsed_wall_seconds': time.monotonic()-self.started,
            'limitations': ['No browser-click claim. The existing workbench receives only environmental device events; this observer sends no model prompt.',
                'Actual missed arrival/pause races are retained; the disable time is never backdated.',
                'Sensor state and loading are simulated; video uses actual timestamped Isaac JPEGs.',
                'NA and correct undelivered handling are never promoted to a delivered-task success.']}
        write_json(self.directory/'report.json', report)
        print(json.dumps({'status': classification, 'business_outcome': report['business_outcome'], 'report': str(self.directory/'report.json'), 'error': error}, ensure_ascii=False))
        return 0 if passed else 2 if classification == 'NA' else 1


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Unique new project evidence directory; never overwritten.')
    parser.add_argument('--wall-seconds', type=int, default=3600)
    parser.add_argument('--navigation-report', required=True, help='Explicit already-closed real navigation report with passed B and Lab arrival cases.')
    args = parser.parse_args(argv)
    if not 120 <= args.wall_seconds <= 3600:
        parser.error('--wall-seconds must be 120..3600; original business limits are unchanged')
    return args


def main():
    return ReceiverTimeout(arguments()).run()


if __name__ == '__main__':
    raise SystemExit(main())
