"""Bounded real Hub / Nav2 qualification, explicitly separate from Agnes.

Only the project administrator should run this on a fresh smart-ward run.
It never resets, injects specimens, creates orders or prompts an Agent.  It
uses the production navigate_route tool and the existing physical stop gates.
Failed cases are preserved and all subsequent cases are NA; no automatic retry.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import signal
import sqlite3
import sys
import time
import traceback
import uuid
from pathlib import Path


ROOT = Path(os.environ.get('HOSPITAL_ROOT', Path(__file__).resolve().parents[1]))
ROBOT = 'hospital-robot-01'
QUALIFICATION_VERSION = 'smart-nav-20261013.3'
MAX_LEG_WALL_SECONDS = 1200
MAX_SEGMENT_WALL_SECONDS = 2400
MAX_GLOBAL_WALL_SECONDS = 7200
BUDGET_REVISION = {
    'reason': 'Source25 B arrival (about 753 wall seconds) and the in-progress Lab physical-distance/RTF lower bound (about 955 wall seconds) show that a 900-second leg can be shorter than observed physical execution; leg and enclosing segment/global limits are revised only for a new independent navigation qualification.',
    'prior_version': 'smart-nav-20261013.2',
    'prior_limits': {'leg_wall_seconds': 900, 'segment_wall_seconds': 1800,
                     'global_wall_seconds': 5400},
    'source25_b_elapsed_wall_seconds_approx': 753,
    'source25_lab_observation': {
        'nav_task_id': '4aa3474e2caa44008b720d8bbd1eb244',
        'nav_goal_uuid': '0e0e2e760ae14058a4b7bfd3c9b5017a',
        'started_wall': 1791851887.876, 'observed_wall': 1791852391.299,
        'remaining_distance_m': 50.56, 'measured_rtf_approx': .224,
        'unchanged_max_linear_speed_mps': .5,
        'estimated_total_lower_bound_wall_seconds':
            1791852391.299 - 1791851887.876 + 50.56 / .5 / .224,
        'evidence_scope': 'Actual source25 progress snapshot before this revision, not a completed Lab result; the original run retains its original deadlines and reports.',
    },
    'cleanup_reserve_wall_seconds': 150,
    'unchanged': 'maps, routes, speeds, physical arrival/stop thresholds, three segments, twelve navigation tasks, no orders or model calls; Agent limits remain 60 requests/2400 simulation seconds/3600 wall seconds and fault qualification budgets are separate',
}
ACTIVE = {'pending', 'running', 'canceling'}
TERMINAL = {'succeeded', 'failed', 'canceled'}
SEGMENTS = (
    ('arrival_b', 'idempotent_route', 'arrival_lab'),
    ('arrival_c', 'arrival_depot', 'stop_real_motion', 'change_goal_after_stop'),
    ('bridge_exit_guard', 'hub_exit_guard'),
)
ALL_CASES = [name for segment in SEGMENTS for name in segment]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def append_json(path, value):
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n')


def recent(stamp, maximum, now=None):
    return isinstance(stamp, (int, float)) and math.isfinite(stamp) and 0 <= (time.time() if now is None else now)-stamp <= maximum


def physical_stationary(state, observation, run_id, epoch):
    """Same production thresholds, plus independent fresh scene readback."""
    truth = state.get('physical_pose') or {}
    actual = observation.get('physical_robot') or {}
    pose = state.get('pose') or {}
    return bool(state.get('pose_fresh') and recent(state.get('pose_wall_time'), 5)
        and recent(truth.get('wall_time'), 5)
        and observation.get('run_id') == run_id and observation.get('epoch') == epoch
        and recent(observation.get('wall_time'), 3) and recent(actual.get('wall_time'), 3)
        and actual.get('available') and actual.get('stopped_samples', 0) >= 3
        and actual.get('speed_mps', math.inf) < .03
        and abs(actual.get('angular_speed_rad_s', math.inf)) < .03
        and pose.get('speed', math.inf) < .05 and abs(pose.get('angular_speed', math.inf)) < .1
        and state.get('stopped_confirmed') is True
        and (not state.get('navigation') or state['navigation'].get('status') in TERMINAL))


def arrived(state, observation, station, task_id, run_id, epoch):
    task = state.get('navigation_tasks', {}).get(task_id, {})
    target = state['stations'][station]
    truth = state.get('physical_pose') or {}
    point = truth.get('position') or []
    return bool(task.get('status') == 'succeeded' and task.get('goal_station') == station
        and station in state.get('at_stations', []) and len(point) >= 2
        and math.hypot(point[0]-target['x'], point[1]-target['y']) <= .5
        and physical_stationary(state, observation, run_id, epoch))


def recording_summary(directory):
    path = directory/'frames.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    unique = {(row.get('capture_wall_time'), row.get('sha256')) for row in rows}
    stamps = [row['capture_wall_time'] for row in rows if isinstance(row.get('capture_wall_time'), (int, float))]
    video = directory/'simulation.mp4'
    return {'frames': len(rows), 'unique_actual_captures': len(unique),
            'first_capture_wall_time': min(stamps) if stamps else None,
            'last_capture_wall_time': max(stamps) if stamps else None,
            'capture_wall_span': max(stamps)-min(stamps) if len(stamps) > 1 else 0,
            'video': str(video) if video.exists() else None,
            'video_bytes': video.stat().st_size if video.exists() else 0,
            'source': 'actual Isaac JPEG frames with original capture stamps; playback is not a wall-clock duration measurement'}


class Qualification:
    def __init__(self, args):
        if (type(args.wall_seconds) is not int or not 300 <= args.wall_seconds <= MAX_GLOBAL_WALL_SECONDS
                or type(args.leg_seconds) is not int or not 30 <= args.leg_seconds <= MAX_LEG_WALL_SECONDS):
            raise ValueError('navigation_qualification_budget_out_of_bounds')
        self.args = args
        self.root = ROOT.resolve()
        default = self.root/'runs/smart-ward-v1/acceptance'/('navigation-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        self.directory = Path(args.output).resolve() if args.output else default
        if not self.directory.is_relative_to(self.root):
            raise ValueError('evidence_directory_must_be_inside_project')
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started_wall = time.time()
        self.started = time.monotonic()
        self.deadline = self.started+args.wall_seconds
        self.work_end = self.deadline-150  # reserve bounded stop/recovery/recording
        self.segment_end = None
        self.run_id = self.epoch = self.session = None
        self.hub = self.recorder = self.sampler = None
        self.manage = None
        self.claimed = False
        self.begin_outcome_unknown = False
        self.interrupted = False
        self.done = False
        self.last_state = self.last_observation = None
        self.last_snapshot = 0
        self.seen_events = set()
        self.stopped_services = set()
        self.results = []
        self.segment_records = []
        self.report = {'scope': 'real navigation and physical-stop qualification; this is NOT Agent acceptance',
                       'qualification_version': QUALIFICATION_VERSION, 'budget_revision': dict(BUDGET_REVISION),
                       'python_executable': sys.executable,
                       'started_wall': self.started_wall, 'limits': {'wall_seconds': args.wall_seconds,
                       'leg_wall_seconds': args.leg_seconds, 'segment_wall_seconds': MAX_SEGMENT_WALL_SECONDS,
                       'maximum_global_wall_seconds': MAX_GLOBAL_WALL_SECONDS,
                       'maximum_segments': 3, 'maximum_navigation_tasks': 12},
                       'cases': self.results, 'segments': self.segment_records}

    def record(self, kind, **values):
        append_json(self.directory/'driver-events.jsonl', {'kind': kind, 'wall_time': time.time(), **values})

    def remaining(self, *, cleanup=False, segment=True):
        end = self.deadline if cleanup else self.work_end
        if not cleanup and segment and self.segment_end is not None:
            end = min(end, self.segment_end)
        remaining = end-time.monotonic()
        if remaining <= 0 or self.interrupted and not cleanup:
            raise TimeoutError('qualification_interrupted' if self.interrupted else 'qualification_wall_budget_exhausted')
        return remaining

    async def api(self, path='/api/state', arguments=None, *, cleanup=False):
        timeout = min(65 if path == '/api/mhs/routes' else 8, self.remaining(cleanup=cleanup))
        if path != '/api/state':
            self.record('api_request', path=path, arguments=arguments)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self.manage.api, path, arguments), timeout)
        except Exception as error:
            self.record('api_error', path=path, error=repr(error), operation_id=(arguments or {}).get('operation_id'),
                        mutation_outcome='unknown_no_retry' if arguments and path != '/api/mhs/routes' else None)
            raise
        if path != '/api/state':
            self.record('api_result', path=path, result=result)
        return result

    def scene_observation(self):
        return json.loads((self.root/'runs/smart-ward-v1/scene/observation.json').read_text(encoding='utf-8'))

    async def state(self, *, cleanup=False, enforce_empty=True):
        state = await self.api(cleanup=cleanup)
        if self.run_id and state.get('run_id') != self.run_id:
            raise RuntimeError('qualification_run_changed')
        observation = self.scene_observation()
        self.last_state, self.last_observation = state, observation
        if enforce_empty and self.claimed:
            ward = state.get('smart_ward', {})
            if state.get('orders') or ward.get('incidents') or ward.get('specimens') or ward.get('watch', {}).get('enabled'):
                raise RuntimeError('qualification_scope_changed_orders_incidents_specimens_or_watch')
            if state.get('controller_session') != self.session:
                raise RuntimeError('qualification_controller_changed')
        for event in state.get('events', []):
            event_id = event.get('event_id')
            if event_id not in self.seen_events:
                append_json(self.directory/'api-events.jsonl', event)
                self.seen_events.add(event_id)
        if time.monotonic()-self.last_snapshot >= 1:
            snapshot = {key: value for key, value in state.items() if key != 'trajectory'}
            snapshot['trajectory_sample_count'] = len(state.get('trajectory', []))
            append_json(self.directory/'api-states.jsonl', {'wall_time': time.time(), 'state': snapshot,
                                                          'scene_observation': observation})
            self.last_snapshot = time.monotonic()
        return state, observation

    async def sample(self):
        while not self.done:
            try:
                await self.state(cleanup=True, enforce_empty=False)
            except Exception as error:
                self.record('sampler_error', error=repr(error))
            await asyncio.sleep(.3)

    def ownership(self, name):
        pid = self.manage.process(name)  # validates argv, cwd, project home
        if not pid:
            raise RuntimeError('owned_project_process_missing:'+name)
        base = Path('/proc')/str(pid)
        return {'name': name, 'pid': pid, 'cwd': str((base/'cwd').resolve()),
                'argv': (base/'cmdline').read_bytes().replace(b'\0', b' ').decode(),
                'stat_starttime': (base/'stat').read_text().split(') ', 1)[1].split()[19]}

    async def preflight(self):
        # Check driver dependencies before claiming or changing a live run.
        from .hub_client import HubClient
        from . import manage
        self.manage = manage
        state, observation = await self.state(enforce_empty=False)
        ward = state.get('smart_ward', {})
        checks = {'smart_mode': state.get('protocol') == 'smart-ward-v1' and manage.MODE == 'smart-ward-v1',
                  'navigation_ready': state.get('navigation_ready') is True,
                  'pose_fresh': state.get('pose_fresh') is True,
                  'physical_fresh': recent((state.get('physical_pose') or {}).get('wall_time'), 5),
                  'guard_healthy': state.get('motion_guard', {}).get('healthy') is True,
                  'stopped': state.get('stopped_confirmed') is True,
                  'scene_fresh': ward.get('scene_fresh') is True,
                  'watch_off': ward.get('watch', {}).get('enabled') is False,
                  'empty_orders': not state.get('orders'), 'empty_incidents': not ward.get('incidents'),
                  'empty_specimens': not ward.get('specimens'),
                  'fresh_run_no_prior_qualification': not ward.get('navigation_qualification'),
                  'fresh_run_no_prior_navigation': not state.get('navigation_tasks'),
                  'controller_unclaimed': not state.get('controller_session'),
                  'not_operator_stopped': not state.get('operator_stopped')}
        checks['independent_physical_stop'] = physical_stationary(state, observation, state['run_id'], ward.get('epoch'))
        processes = {name: self.ownership(name) for name in ('sim', 'bridge', 'daemon')}
        container = manage.container()
        checks['owned_container_running'] = bool(container and container['State']['Running'])
        write_json(self.directory/'preflight.json', {'checks': checks, 'state': state,
                    'scene_observation': observation, 'processes': processes,
                    'container': {'id': container.get('Id'), 'name': container.get('Name')} if container else None})
        if not all(checks.values()):
            raise RuntimeError('preflight_failed:'+','.join(name for name, passed in checks.items() if not passed))
        self.run_id, self.epoch = state['run_id'], ward['epoch']
        self.session = 'qualification:navigation:'+self.run_id+':'+uuid.uuid4().hex[:12]
        self.report.update(run_id=self.run_id, epoch=self.epoch, session_key=self.session)
        sources = ['mhs/smart_navigation.py', 'mhs/smart_ward.py', 'mhs/bridge.py',
                   'mhs/navigation_v2.py', 'mhs/guard.py', 'configs/stations.json',
                   'configs/smart-ward-v1.json', 'runs/smart-ward-v1/scene/map/manifest.json',
                   'runs/smart-ward-v1/scene/overlay-manifest.json']
        self.report['source_sha256'] = {name: hashlib.sha256((self.root/name).read_bytes()).hexdigest()
                                       if (self.root/name).is_file() else None for name in sources}
        self.recorder = __import__('mhs.record', fromlist=['Recorder']).Recorder(self.directory).start()
        self.sampler = asyncio.create_task(self.sample())

    async def connect_hub(self):
        from .hub_client import HubClient
        self.hub = HubClient(record=self.directory/'hub-messages.jsonl')
        await asyncio.wait_for(self.hub.__aenter__(), min(20, self.remaining()))

    async def rpc(self, method, arguments):
        return await asyncio.wait_for(self.hub.rpc(method, arguments), min(20, self.remaining()))

    async def close_hub(self):
        if self.hub:
            hub, self.hub = self.hub, None
            try:
                await asyncio.wait_for(hub.__aexit__(None, None, None), 4)
            except Exception as error:
                self.record('hub_close_error', error=repr(error))

    async def begin_segment(self, index):
        self.segment_end = None
        state, observation = await self.state()
        if not physical_stationary(state, observation, self.run_id, self.epoch):
            raise RuntimeError('segment_requires_actual_stopped_robot')
        body = {'session_key': self.session, 'expected_run_id': self.run_id,
                'operation_id': 'nav-qual-begin-'+str(index)+'-'+uuid.uuid4().hex}
        try:
            result = await self.api('/api/admin/smart-ward/navigation-qualification/begin', body)
        except (TimeoutError, OSError):
            self.begin_outcome_unknown = True
            raise
        qualification = result.get('qualification', result)
        if not qualification.get('active'):
            raise RuntimeError('qualification_begin_not_active')
        if qualification.get('session_key') != self.session:
            raise RuntimeError('qualification_session_identity_mismatch')
        self.claimed = True
        if qualification.get('version') != QUALIFICATION_VERSION:
            raise RuntimeError('qualification_version_requires_new_run')
        deadline_wall = qualification.get('deadline_wall')
        started_wall = qualification.get('started_wall')
        segment_started_wall = qualification.get('segment_started_wall')
        total_deadline_wall = qualification.get('total_deadline_wall')
        if (not recent(segment_started_wall, 10) or qualification.get('segment') != index
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                       for value in (started_wall, segment_started_wall, deadline_wall, total_deadline_wall))
                or not started_wall <= segment_started_wall < deadline_wall <= total_deadline_wall
                or deadline_wall - segment_started_wall > MAX_SEGMENT_WALL_SECONDS + 1e-6
                or total_deadline_wall - started_wall > MAX_GLOBAL_WALL_SECONDS + 1e-6
                or qualification.get('max_attempts') != 12):
            raise RuntimeError('qualification_budget_metadata_missing')
        self.segment_end = time.monotonic()+min(MAX_SEGMENT_WALL_SECONDS, max(0, deadline_wall-time.time()))
        self.segment_records.append({'segment': index, 'cases': list(SEGMENTS[index-1]),
                                     'begin': result, 'started_wall': time.time()})
        self.record('qualification_segment_started', segment=index, result=result)
        self.save()

    async def end_segment(self, index, reason='qualification_end', *, cleanup=False):
        result = await self.api('/api/admin/smart-ward/navigation-qualification/end',
            {'expected_run_id': self.run_id, 'operation_id': 'nav-qual-end-'+str(index)+'-'+uuid.uuid4().hex,
             'reason': reason}, cleanup=cleanup)
        state, observation = await self.wait_stopped(timeout=30, cleanup=cleanup)
        if result.get('qualification', {}).get('active'):
            raise RuntimeError('qualification_end_still_active')
        if self.segment_records and self.segment_records[-1]['segment'] == index:
            self.segment_records[-1].update(end=result, stopped_state=state, ended_wall=time.time())
        self.record('qualification_segment_ended', segment=index, result=result,
                    actual_physical=observation.get('physical_robot'))
        self.segment_end = None
        self.save()

    async def wait_stopped(self, task_id=None, required_status=None, timeout=30, *, cleanup=False):
        end = min(time.monotonic()+timeout, (self.deadline if cleanup else self.work_end))
        while time.monotonic() < end:
            state, observation = await self.state(cleanup=cleanup, enforce_empty=not cleanup)
            task = state.get('navigation_tasks', {}).get(task_id, {}) if task_id else state.get('navigation') or {}
            if physical_stationary(state, observation, self.run_id, self.epoch) and (
                not task_id or task.get('nav_task_id') == task_id and task.get('status') in TERMINAL
                and (required_status is None or task.get('status') == required_status)):
                return state, observation
            await asyncio.sleep(.2)
        raise TimeoutError('physical_stop_and_navigation_terminal_not_confirmed')

    async def ready(self, timeout=40):
        end = min(time.monotonic()+timeout, time.monotonic()+self.remaining())
        while time.monotonic() < end:
            state, observation = await self.state()
            devices = (await self.rpc('hub/devices', {})).get('devices', [])
            device = next((value for value in devices if value.get('id') == ROBOT), {})
            self.record('hub_robot_readiness', device=device)
            if physical_stationary(state, observation, self.run_id, self.epoch) and state.get('navigation_ready') and state.get('motion_guard', {}).get('healthy') and device.get('available') and device.get('state', {}).get('values', {}).get('stop_confirmed') and not device.get('busy', {}).get('chassis'):
                return state, observation
            await asyncio.sleep(.5)
        raise TimeoutError('hub_resource_or_guard_or_actual_stop_not_ready')

    async def begin_route(self, station, operation_id=None, route=None):
        await self.ready()
        before, observation = await self.state()
        options = await self.api('/api/mhs/routes', {'station_id': station}) if route is None else None
        if route is None:
            rows = (options or {}).get('routes', [])
            if not rows:
                raise RuntimeError('no_new_verified_route:'+station)
            if any(row.get('validation') != 'nav2_verified' for row in rows):
                raise RuntimeError('unverified_route_returned')
            # A deterministic shortest verified route is a qualification control,
            # not an assertion about Agnes route reasoning.
            route = min(rows, key=lambda row: (row.get('distance_m', row.get('length_m', math.inf)), row['route_id']))
        operation_id = operation_id or 'nav-qual-'+uuid.uuid4().hex
        params = {'device': ROBOT, 'tool': 'navigate_route',
                  'arguments': {'route_id': route['route_id'], 'operation_id': operation_id}}
        call_started = time.monotonic()
        call_started_wall = time.time()
        receipt = await self.rpc('hub/call', params)
        if not receipt.get('accepted') or not receipt.get('job'):
            raise RuntimeError('hub_route_rejected:'+json.dumps(receipt, ensure_ascii=False))
        end = min(time.monotonic()+12, time.monotonic()+self.remaining())
        old_ids = set(before.get('navigation_tasks', {}))
        while time.monotonic() < end:
            state, actual = await self.state()
            ids = set(state.get('navigation_tasks', {}))-old_ids
            if len(ids) == 1:
                task_id = ids.pop()
                task = state['navigation_tasks'][task_id]
                if task.get('route_id') != route['route_id'] or task.get('goal_station') != station:
                    raise RuntimeError('hub_route_task_identity_mismatch')
                result = {'station': station, 'route': route, 'options': options, 'operation_id': operation_id,
                          'receipt': receipt, 'task_id': task_id, 'before': before,
                          'started_wall': call_started_wall, 'started_monotonic': call_started}
                self.record('real_navigation_task', **{key: value for key, value in result.items() if key != 'before'})
                return result
            if len(ids) > 1:
                raise RuntimeError('multiple_navigation_tasks_from_one_operation')
            if receipt['job'] in self.hub.jobs:
                raise RuntimeError('hub_job_ended_before_navigation_task:'+json.dumps(self.hub.jobs[receipt['job']], ensure_ascii=False))
            await asyncio.sleep(.2)
        raise TimeoutError('real_navigation_task_not_observed')

    async def complete_route(self, navigation):
        task_id, station = navigation['task_id'], navigation['station']
        end = min(navigation['started_monotonic']+self.args.leg_seconds, time.monotonic()+self.remaining())
        while time.monotonic() < end:
            state, observation = await self.state()
            # The state response can arrive after the earliest leg, segment or
            # global-work deadline. A late arrival is never a qualification pass.
            if time.monotonic() >= end:
                raise TimeoutError('real_arrival_leg_or_qualification_budget_exhausted:'+station)
            task = state.get('navigation_tasks', {}).get(task_id, {})
            if task.get('status') in {'failed', 'canceled', 'unknown'}:
                raise RuntimeError('real_navigation_'+task['status']+':'+json.dumps(task, ensure_ascii=False))
            native = self.hub.jobs.get(navigation['receipt']['job'])
            if arrived(state, observation, station, task_id, self.run_id, self.epoch) and native:
                if native.get('status') != 'done':
                    raise RuntimeError('hub_job_not_done_after_arrival:'+json.dumps(native, ensure_ascii=False))
                return {'navigation': navigation, 'hub_result': native, 'state': state,
                        'scene_physical': observation.get('physical_robot'), 'confirmed_wall': time.time()}
            if native and native.get('status') not in {'done', 'running', 'accepted'}:
                raise RuntimeError('hub_navigation_failed:'+json.dumps(native, ensure_ascii=False))
            await asyncio.sleep(.3)
        raise TimeoutError('real_arrival_leg_or_qualification_budget_exhausted:'+station)

    async def arrival(self, station):
        navigation = await self.begin_route(station)
        return await self.complete_route(navigation)

    async def duplicate(self, original):
        navigation = original['navigation']
        before, _ = await self.ready()
        ids = set(before.get('navigation_tasks', {}))
        receipt = await self.rpc('hub/call', {'device': ROBOT, 'tool': 'navigate_route',
            'arguments': {'route_id': navigation['route']['route_id'], 'operation_id': navigation['operation_id']}})
        if not receipt.get('accepted'):
            raise RuntimeError('duplicate_route_transport_rejected:'+json.dumps(receipt))
        result = await self.hub.finish(receipt['job'], min(20, self.remaining()))
        after, actual = await self.wait_stopped()
        attempts_before = before['smart_ward']['navigation_qualification']['attempts']
        attempts_after = after['smart_ward']['navigation_qualification']['attempts']
        if result.get('status') != 'done' or set(after.get('navigation_tasks', {})) != ids or (after.get('navigation') or {}).get('nav_task_id') != navigation['task_id'] or attempts_after != attempts_before:
            raise RuntimeError('same_operation_created_new_task_or_changed_result')
        if not arrived(after, actual, navigation['station'], navigation['task_id'], self.run_id, self.epoch):
            raise RuntimeError('duplicate_route_lost_actual_arrival')
        return {'operation_id': navigation['operation_id'], 'route_id': navigation['route']['route_id'],
                'original_task_id': navigation['task_id'], 'receipt': receipt, 'hub_result': result,
                'qualification_attempts_before': attempts_before, 'qualification_attempts_after': attempts_after,
                'navigation_task_ids_before': sorted(ids), 'navigation_task_ids_after': sorted(after['navigation_tasks']),
                'state': after, 'scene_physical': actual.get('physical_robot')}

    async def moving(self, navigation):
        end = min(navigation['started_monotonic']+self.args.leg_seconds,
                  time.monotonic()+self.remaining(), time.monotonic()+90)
        while time.monotonic() < end:
            state, observation = await self.state()
            task = state.get('navigation_tasks', {}).get(navigation['task_id'], {})
            actual = observation.get('physical_robot') or {}
            if task.get('status') in TERMINAL:
                raise RuntimeError('navigation_ended_before_motion_observed:'+task.get('status', 'missing'))
            if task.get('status') == 'running' and state.get('pose', {}).get('speed', 0) > .1 and actual.get('speed_mps', 0) > .1 and recent(actual.get('wall_time'), 3) and recent(observation.get('wall_time'), 3) and recent((state.get('physical_pose') or {}).get('wall_time'), 5):
                return {'state': state, 'scene_physical': actual, 'observed_wall': time.time()}
            await asyncio.sleep(.2)
        raise TimeoutError('actual_scene_and_odometry_motion_above_0_1_mps_not_observed')

    async def cancel_motion(self):
        navigation = await self.begin_route('station_b')
        before = await self.moving(navigation)
        requested_wall = time.time()
        reply = await self.rpc('hub/stop', {'device': ROBOT})
        state, actual = await self.wait_stopped(navigation['task_id'], 'canceled')
        result = await self.hub.finish(navigation['receipt']['job'], min(15, self.remaining()))
        return {'navigation': navigation, 'before': before, 'stop_reply': reply,
                'stop_requested_wall': requested_wall, 'stop_confirmed_wall': time.time(),
                'hub_result': result, 'after': state, 'scene_physical': actual.get('physical_robot')}

    async def stable_no_resume(self, task_id, seconds=3):
        baseline, _ = await self.wait_stopped(task_id, 'canceled')
        ids = set(baseline['navigation_tasks'])
        end = time.monotonic()+seconds
        samples = []
        while time.monotonic() < end:
            self.remaining()
            state, actual = await self.state()
            if not physical_stationary(state, actual, self.run_id, self.epoch) or state.get('motion_guard', {}).get('armed') or set(state['navigation_tasks']) != ids or state['navigation_tasks'][task_id]['status'] != 'canceled':
                raise RuntimeError('old_motion_automatically_resumed_or_new_task_created')
            samples.append({'wall_time': time.time(), 'navigation': state.get('navigation'),
                            'guard': state.get('motion_guard'), 'scene_physical': actual.get('physical_robot')})
            await asyncio.sleep(.3)
        return {'navigation_task_ids': sorted(ids), 'samples': samples}

    async def restore(self, name, *, cleanup=False):
        if self.remaining(cleanup=cleanup) < (10 if name == 'bridge' else 50):
            raise TimeoutError('insufficient_reserved_time_for_owned_service_restore:'+name)
        self.record('restore_owned_service', service=name)
        if name == 'bridge':
            await asyncio.wait_for(asyncio.to_thread(self.manage.spawn, 'bridge',
                [str(self.root/'agent/mhs-venv/bin/python'), '-m', 'mhs.bridge'], self.manage.ENV),
                min(10, self.remaining(cleanup=cleanup)))
        else:
            await asyncio.wait_for(asyncio.to_thread(self.manage.start_daemon),
                min(50, self.remaining(cleanup=cleanup)))
        ownership = self.ownership(name)
        self.stopped_services.discard(name)
        self.record('owned_service_restored', ownership=ownership)
        return ownership

    async def service_exit(self, name, station):
        navigation = await self.begin_route(station)
        before = await self.moving(navigation)
        # Do not launch a process mutation whose bounded native completion
        # would extend past this segment.  to_thread cancellation cannot undo it.
        if self.remaining() < 35:
            raise TimeoutError('insufficient_segment_budget_before_process_exit')
        ownership = self.ownership(name)
        self.stopped_services.add(name)
        exit_wall = time.time()
        # This ownership-checking routine is the ONLY process stop mechanism.
        # Its bounded graceful shutdown is continuously observed by sample().
        await asyncio.wait_for(asyncio.to_thread(self.manage.stop_process, name), 25)
        if self.manage.process(name):
            raise RuntimeError('owned_process_did_not_exit:'+name)
        state, observation = await self.wait_stopped(navigation['task_id'], 'canceled')
        guard = state.get('motion_guard', {})
        guard_events = [event for event in state.get('events', [])
                        if event.get('kind') == 'motion_guard_stop' and event.get('wall_time', 0) >= exit_wall]
        if guard.get('armed') or guard.get('healthy') or not guard_events:
            raise RuntimeError('project_guard_stop_on_exit_not_observed:'+json.dumps(guard))
        confirmed_wall = time.time()
        before_restore = await self.stable_no_resume(navigation['task_id'])
        if name == 'daemon':
            await self.close_hub()
        restored = await self.restore(name)
        if name == 'daemon':
            await self.connect_hub()
        await self.ready()
        after_restore = await self.stable_no_resume(navigation['task_id'])
        return {'navigation': navigation, 'before': before, 'stopped_process': ownership,
                'exit_requested_wall': exit_wall, 'physical_stop_confirmed_wall': confirmed_wall,
                'guard_stop': guard, 'guard_stop_events': guard_events,
                'independent_lease_trip_observed': any(event.get('reason') in {'motion_lease_expired', 'hub_disconnected'} for event in guard_events),
                'stopped_state': state,
                'scene_physical': observation.get('physical_robot'),
                'before_reconnect': before_restore, 'restored_process': restored,
                'after_reconnect': after_restore,
                'hub_result': self.hub.jobs.get(navigation['receipt']['job']) if name == 'bridge' else None,
                'hub_result_note': 'Hub exit intentionally loses the old client job channel; API/scene terminal evidence is authoritative for physical stop.' if name == 'daemon' else None}

    def save(self):
        write_json(self.directory/'report.json', self.report)

    def passed(self, name, details):
        self.results.append({'case': name, 'status': 'passed', 'pass': True, 'details': details})
        print(json.dumps({'case': name, 'status': 'passed'}, ensure_ascii=False), flush=True)
        self.save()

    async def execute(self):
        current = 'preflight'
        original = None
        try:
            await self.preflight()
            for index, cases in enumerate(SEGMENTS, 1):
                current = 'segment_'+str(index)+'_begin'
                await self.begin_segment(index)
                if self.hub is None:
                    await self.connect_hub()
                for name in cases:
                    current = name
                    if name == 'arrival_b':
                        original = await self.arrival('station_b'); details = original
                    elif name == 'idempotent_route':
                        details = await self.duplicate(original)
                    elif name == 'arrival_lab':
                        details = await self.arrival('lab')
                    elif name == 'arrival_c':
                        details = await self.arrival('station_c')
                    elif name == 'arrival_depot':
                        details = await self.arrival('depot')
                    elif name == 'stop_real_motion':
                        details = await self.cancel_motion()
                    elif name == 'change_goal_after_stop':
                        details = await self.arrival('lab')
                    elif name == 'bridge_exit_guard':
                        details = await self.service_exit('bridge', 'station_b')
                    else:
                        details = await self.service_exit('daemon', 'station_c')
                    self.passed(name, details)
                current = 'segment_'+str(index)+'_end'
                await self.end_segment(index)
            self.report['status'] = 'passed'
        except Exception as error:
            self.report.update(status='NA' if not self.claimed else 'failed_or_incomplete',
                               failure={'case': current, 'error': repr(error), 'traceback': traceback.format_exc(),
                                        'wall_time': time.time()})
            self.results.append({'case': current, 'status': 'failed' if self.claimed else 'NA',
                                 'pass': False, 'error': repr(error)})
            self.record('qualification_failure', **self.report['failure'])
            print(json.dumps({'case': current, 'status': self.report['status'], 'error': str(error)}, ensure_ascii=False), flush=True)
        finally:
            completed = {row['case'] for row in self.results}
            for name in ALL_CASES:
                if name not in completed:
                    self.results.append({'case': name, 'status': 'NA', 'pass': None,
                                         'reason': 'not executed after first failure or finite budget; no rerun'})
            await self.cleanup()
            self.report.update(ended_wall=time.time(), elapsed_wall_seconds=time.monotonic()-self.started)
            self.save()
            print('EVIDENCE '+str(self.directory), flush=True)
        return 0 if self.report['status'] == 'passed' else 2 if self.report['status'] == 'NA' else 1

    async def cleanup(self):
        errors = []
        if self.begin_outcome_unknown and not self.claimed:
            try:
                state = await self.api(cleanup=True)
                q = state.get('smart_ward', {}).get('navigation_qualification') or {}
                if state.get('run_id') == self.run_id and state.get('controller_session') in {None, self.session} and not state.get('orders'):
                    # A timed-out request thread may still acquire the backend
                    # lock.  Lock this same empty run before a late begin can
                    # enable navigation; never take over another controller.
                    self.claimed = True
                    self.report['status'] = 'failed_or_incomplete'
                    self.record('unknown_begin_reconciliation', state=state, qualification=q)
            except Exception as error:
                errors.append('unknown_begin_reconciliation:'+repr(error))
        if self.claimed:
            # Stop is an independent action even when end timed out or failed.
            # All requests have fixed recorded operation IDs; none is retried.
            if not self.begin_outcome_unknown:
                try:
                    await self.api('/api/admin/smart-ward/navigation-qualification/end',
                        {'expected_run_id': self.run_id, 'operation_id': 'nav-qual-final-'+uuid.uuid4().hex,
                         'reason': 'navigation_qualification_finished' if self.report.get('status') == 'passed' else 'qualification_failed:'+self.report.get('failure', {}).get('case', 'unknown')}, cleanup=True)
                except Exception as error:
                    errors.append('final_qualification_end:'+repr(error))
            try:
                await self.api('/api/admin/stop-run', {'expected_run_id': self.run_id,
                    'operation_id': 'nav-qual-stop-'+uuid.uuid4().hex,
                    'reason': 'navigation_qualification_finished'}, cleanup=True)
            except Exception as error:
                errors.append('final_stop_request:'+repr(error))
            try:
                state, actual = await self.wait_stopped(timeout=30, cleanup=True)
                self.report['final_stop'] = {'state': state, 'scene_physical': actual.get('physical_robot'),
                                            'watch_disabled': not state.get('smart_ward', {}).get('watch', {}).get('enabled')}
            except Exception as error:
                errors.append('final_stop:'+repr(error))
            for name in ('daemon', 'bridge'):
                if name in self.stopped_services:
                    try:
                        await self.restore(name, cleanup=True)
                    except Exception as error:
                        errors.append('restore_'+name+':'+repr(error))
            try:
                path = self.root/'agent/agh-home-mhs/data/sessions.db'
                if path.exists():
                    connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
                    try:
                        rows = [{'seq': seq, 'ts': stamp, 'type': kind, 'data': json.loads(data)} for seq, stamp, kind, data in connection.execute(
                            'SELECT seq,ts,type,data FROM events WHERE session_key=? ORDER BY seq', (self.session,))]
                    finally:
                        connection.close()
                    write_json(self.directory/'qualification-session-ledger.json', rows)
                    count = sum(row['type'] == 'request/sent' for row in rows)
                    self.report['agent_model_requests_in_qualification_session'] = count
                    if count:
                        errors.append('qualification_unexpected_Agent_model_request')
            except Exception as error:
                errors.append('selected_session_ledger:'+repr(error))
        self.done = True
        if self.sampler:
            self.sampler.cancel()
            await asyncio.gather(self.sampler, return_exceptions=True)
        await self.close_hub()
        if self.recorder:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.recorder.stop), min(25, max(1, self.deadline-time.monotonic())))
            except Exception as error:
                errors.append('recording_stop:'+repr(error))
                # Only Recorder's direct Popen handle is addressed; no process
                # search or shared ffmpeg process can be terminated here.
                try:
                    if self.recorder.proc.poll() is None:
                        self.recorder.proc.terminate()
                        await asyncio.wait_for(asyncio.to_thread(self.recorder.proc.wait, 3), 4)
                except Exception as terminate_error:
                    errors.append('recording_process_termination:'+repr(terminate_error))
            self.report['recording'] = recording_summary(self.directory)
            self.report['recording']['ffmpeg_exit_code'] = self.recorder.proc.returncode
            if self.report['recording']['unique_actual_captures'] < 3 or not self.report['recording']['video_bytes'] or self.recorder.proc.returncode != 0:
                errors.append('actual_frame_recording_missing_or_insufficient')
        self.report['cleanup_errors'] = errors
        if errors and self.report.get('status') == 'passed':
            self.report['status'] = 'failed_or_incomplete'


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='new evidence directory inside this project; existing paths are refused')
    parser.add_argument('--wall-seconds', type=int, default=MAX_GLOBAL_WALL_SECONDS, choices=range(300, MAX_GLOBAL_WALL_SECONDS + 1), metavar='300..7200')
    parser.add_argument('--leg-seconds', type=int, default=MAX_LEG_WALL_SECONDS, choices=range(30, MAX_LEG_WALL_SECONDS + 1), metavar='30..1200')
    return parser.parse_args(argv)


async def run(options):
    test = Qualification(options)
    loop = asyncio.get_running_loop()
    for name in ('SIGINT', 'SIGTERM'):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), setattr, test, 'interrupted', True)
            except (NotImplementedError, RuntimeError):
                pass
    return await test.execute()


def main(argv=None):
    # Parse --help before creating an event loop or touching live services.
    options = arguments(argv)
    return asyncio.run(run(options))


if __name__ == '__main__':
    raise SystemExit(main())
