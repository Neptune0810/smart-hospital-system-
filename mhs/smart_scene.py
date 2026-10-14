"""Durable smart-ward scene protocol and an independent Isaac USD overlay.

The protocol core imports without Isaac.  Scene readings are explicitly
simulation state/model readings, never clinical lux or visual recognition.
"""
import copy
import hashlib
import json
import math
import re
import time
import uuid
from functools import wraps
from pathlib import Path

if __package__ in {None, ''}:
    from scene_io import (SceneIOConflict, iter_commands, publish_ack_bytes,
                          read_ack as read_scene_ack, scene_path,
                          write_ack as publish_scene_ack)
else:
    from .scene_io import (SceneIOConflict, iter_commands, publish_ack_bytes,
                           read_ack as read_scene_ack, scene_path,
                           write_ack as publish_scene_ack)

CABINETS = {
    'hospital-cabinet-b': ('station_b', 'nurse_b', 2),
    'hospital-cabinet-c': ('station_c', 'nurse_c', 2),
    'hospital-cabinet-lab': ('lab', 'lab_reception', 6),
}
ROOMS = {'room301': 'ward301', 'room303': 'ward303'}


def usd_device_identifier(device_id):
    """Explicit device-ID -> valid, collision-free USD prim identifier."""
    mapping = {ident: ident.replace('-', '_') for ident in CABINETS}
    if len(set(mapping.values())) != len(mapping) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value) for value in mapping.values()):
        raise RuntimeError('smart_device_USD_identifier_mapping_invalid')
    if device_id not in mapping:
        raise ValueError('unknown_cabinet_device_id')
    return mapping[device_id]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def operation_signature(command):
    return hashlib.sha256(json.dumps({k: command[k] for k in ('run_id', 'epoch', 'operation_id', 'kind', 'arguments')}, sort_keys=True, allow_nan=False).encode()).hexdigest()


def relative_illumination(brightness):
    return round(min(100., max(0., 20.+.8*float(brightness))), 2)


def overlay_edit(method):
    """Author every visual/physics change in the project layer, never the asset."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.Usd.EditContext(self.stage, self.layer):
            return method(self, *args, **kwargs)
    return wrapped


def geometry_inventory_decision(record):
    """Classify from USD evidence, never from a large bound or a path name.

    An authored navigation volume is metadata only when it is not renderable
    geometry and has no enabled collision API.  A hidden/guide real collider
    remains an obstacle.  Unknown Boundables remain conservative obstacles.
    """
    collision = bool(record.get('collision_enabled'))
    schema_tokens = [str(record.get('type', '')), *record.get('applied_schemas', [])]
    navigation_volume = any('navmeshvolume' in value.lower() for value in schema_tokens)
    if navigation_volume and not record.get('is_gprim') and not collision:
        return 'exclude', 'navigation_volume_schema_non_renderable_without_collision'
    if not collision and record.get('purpose') == 'guide':
        return 'exclude', 'guide_purpose_without_enabled_collision'
    if not collision and record.get('visibility') == 'invisible':
        return 'exclude', 'invisible_without_enabled_collision'
    return 'obstacle', 'enabled_collision_or_visible_or_unknown_geometry'


def boxes_intersect(first, second, padding=0.):
    return all(first[k] <= second[k+3]+padding and first[k+3] >= second[k]-padding for k in range(3))


def mesh_face_bounds(points, counts, indices, region_bounds=()):
    """Conservative face bounds from actual transformed Mesh vertices.

    Whole n-gon bounds avoid assuming triangulation of concave polygons.  This
    separates walls/furniture in merged triangle meshes without dropping their
    real geometry.  Invalid topology must fall back to the whole Mesh bound.
    """
    if sum(counts) != len(indices) or any(count < 3 for count in counts):
        raise ValueError('mesh_topology_invalid')
    offset = 0
    bounds = []
    for count in counts:
        selected = indices[offset:offset+count]
        offset += count
        if any(index < 0 or index >= len(points) for index in selected):
            raise ValueError('mesh_index_invalid')
        vertices = [points[index] for index in selected]
        if any(len(vertex) != 3 or not all(math.isfinite(float(v)) for v in vertex) for vertex in vertices):
            raise ValueError('mesh_point_invalid')
        box = [min(vertex[k] for vertex in vertices) for k in range(3)]+[max(vertex[k] for vertex in vertices) for k in range(3)]
        if box[5] <= .08 or box[2] >= 1.5:
            continue
        if not region_bounds or any(boxes_intersect(box, region, .06) for region in region_bounds):
            bounds.append(box)
    return bounds


class ObstacleIndex:
    """Conservative room-local XY buckets; final 3-D collision check unchanged."""
    def __init__(self, obstacles, room_bounds, cell=.5, padding=.06):
        self.obstacles, self.cell, self.buckets = obstacles, cell, {}
        x0, y0, x1, y1 = room_bounds
        for index, box in enumerate(obstacles):
            # Clip only the bucket extent, never the actual collision geometry.
            left, bottom = max(x0, box[0]-padding), max(y0, box[1]-padding)
            right, top = min(x1, box[3]+padding), min(y1, box[4]+padding)
            if left > right or bottom > top:
                continue
            for i in range(math.floor(left/cell), math.floor(right/cell)+1):
                for j in range(math.floor(bottom/cell), math.floor(top/cell)+1):
                    self.buckets.setdefault((i, j), []).append(index)

    def query(self, bounds):
        indices = set()
        for i in range(math.floor(bounds[0]/self.cell), math.floor(bounds[3]/self.cell)+1):
            for j in range(math.floor(bounds[1]/self.cell), math.floor(bounds[4]/self.cell)+1):
                indices.update(self.buckets.get((i, j), ()))
        return (self.obstacles[index] for index in sorted(indices))


def choose_cabinet_position(room, dimensions, obstacles, protected_points, grid=.25, use_spatial_index=True):
    """Deterministic full-room grid; reject original furniture and goal intrusion.

    Dimensions are world-axis x/y/z.  Obstacles are aligned 3-D world bounds.
    All candidate choices and the chosen envelope are suitable for provenance.
    """
    width, depth, height = dimensions
    x0, y0, x1, y1 = room['bounds']
    anchor = room['handoff_anchor'][:2]
    candidates = []
    obstacle_index = ObstacleIndex(obstacles, room['bounds']) if use_spatial_index else None
    for i in range(math.floor((x1-x0)/grid)+1):
        for j in range(math.floor((y1-y0)/grid)+1):
            x, y = x0+i*grid, y0+j*grid
            bounds = [x-width/2, y-depth/2, .04, x+width/2, y+depth/2, height]
            if bounds[0] < x0+.08 or bounds[1] < y0+.08 or bounds[3] > x1-.08 or bounds[4] > y1-.08:
                continue
            # Keep the original opening clear; never place the cabinet across it.
            if math.dist([x, y], anchor) < .85:
                continue
            nearby = obstacle_index.query(bounds) if obstacle_index else obstacles
            if any(boxes_intersect(bounds, obstacle, .06) for obstacle in nearby):
                continue
            xy = [bounds[0], bounds[1], bounds[3], bounds[4]]
            if any(math.hypot(p['x']-min(max(p['x'], xy[0]), xy[2]), p['y']-min(max(p['y'], xy[1]), xy[3])) < .70 for p in protected_points):
                continue
            candidates.append((math.dist([x, y], anchor), x, y, xy))
    if not candidates:
        raise RuntimeError('smart_cabinet_no_collision_free_placement:'+room['id'])
    distance, x, y, bounds = min(candidates)
    return {'position': [x, y, height/2], 'collision_bounds_xy': bounds,
            'selection': 'nearest handoff anchor, then x, then y; 0.25m deterministic grid',
            'candidate_count': len(candidates), 'distance_to_handoff_anchor': distance}


class SceneQualificationFaults:
    """Explicit admin qualification only; never an Agent/device feature.

    Controls are wall-polled so a genuinely paused timeline can be resumed.
    An ACK hold stores the first genuinely produced ACK bytes; release moves
    those exact bytes into the normal inbox without refreshing their timestamp.
    """
    KINDS = {'set_cabinet_availability', 'hold_ack', 'release_ack',
             'pause_simulation', 'resume_simulation'}

    def __init__(self, world, timeline=None):
        self.world, self.timeline = world, timeline
        self.directory = world.directory
        for name in ('fault-commands', 'fault-acks', 'fault-history'):
            (self.directory/name).mkdir(parents=True, exist_ok=True)
        path = self.directory/'fault-state.json'
        self.state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        self.invalid_files = set()
        prior_poll = ((self.state or {}).get('paused') or {}).get('resume_requested_poll_sequence', 0)
        self.poll_sequence = prior_poll if type(prior_poll) is int and prior_poll >= 0 else 0
        self.resume_after_restart = bool(((self.state or {}).get('paused') or {}).get('phase') == 'resume_requested')

    def event(self, kind, **arguments):
        with (self.directory/'fault-events.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(dict(kind=kind, wall_time=time.time(), **arguments), ensure_ascii=False, allow_nan=False)+'\n')

    def save(self):
        if self.state is not None:
            atomic_json(self.directory/'fault-state.json', self.state)

    def qualification(self):
        control = self.world.control or {}
        q = control.get('fault_qualification') or {}
        now = time.time()
        start, end = q.get('started_wall'), q.get('deadline_wall')
        valid = (q.get('active') is True and isinstance(q.get('qualification_id'), str)
                 and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}', q['qualification_id']))
                 and isinstance(q.get('session_key'), str) and q['session_key'].startswith('qualification:')
                 and isinstance(start, (int, float)) and not isinstance(start, bool) and math.isfinite(start)
                 and isinstance(end, (int, float)) and not isinstance(end, bool) and math.isfinite(end)
                 and 0 < end-start <= 1800 and start <= now < end)
        if valid and self.state and self.state.get('qualification_id') == q['qualification_id']:
            # A new control-file write cannot renew a closed qualification or
            # extend the original finite wall-clock lease.
            valid = (self.state.get('active') is True
                     and self.state.get('started_wall') == start
                     and self.state.get('deadline_wall') == end)
        return q if valid else None

    def _identity(self, q):
        return {key: (self.world.control or {})[key] for key in ('run_id', 'epoch')} | {
            'qualification_id': q['qualification_id'], 'session_key': q['session_key']}

    def _record_unobserved_closed_qualification(self):
        """Acknowledge an inactive valid control with no executed fault actions.

        begin/end can both happen before the next scene poll. This is an honest
        no-actions-observed control receipt, never a fabricated business ACK.
        Unresolved original ACKs or a paused timeline must never be discarded.
        """
        control = self.world.control or {}
        q = control.get('fault_qualification') or {}
        if q.get('active') is not False or not isinstance(q.get('qualification_id'), str):
            return
        if (not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', q['qualification_id']) or
                not isinstance(q.get('session_key'), str) or not q['session_key'].startswith('qualification:')):
            return
        start, end = q.get('started_wall'), q.get('deadline_wall')
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end))
                or not 0 < end-start <= 1800 or start > time.time()):
            return
        if not self.world.state or any(control.get(k) != self.world.state.get(k) for k in ('run_id', 'epoch')):
            return
        identity = self._identity(q)
        if self.state:
            if self.state.get('active') or self.state.get('paused') or self.state.get('holds'):
                return
            if all(self.state.get(k) == v for k, v in identity.items()):
                return  # Preserve the original restoration evidence/timestamp.
            atomic_json(self.directory/'fault-history'/(self.state['qualification_id']+'.json'), self.state)
        self.state = dict(identity, active=False, started_wall=start, deadline_wall=end,
                          originals={}, holds={}, processed={}, paused=None, ended_wall=time.time(),
                          end_reason='inactive_qualification_no_fault_actions_observed',
                          no_fault_actions_observed=True)
        self.save()
        self.event('inactive_qualification_no_fault_actions_observed', **identity)

    def _ensure_state(self, q):
        identity = self._identity(q)
        if self.state and all(self.state.get(key) == value for key, value in identity.items()):
            return True
        if self.state:
            self.close('qualification_identity_changed')
            if self.state.get('paused'):
                return False  # Keep the unresolved pause/latch evidence.
            # Keep every prior control record; original ACK files also remain.
            prior = self.state.get('qualification_id', uuid.uuid4().hex)
            atomic_json(self.directory/'fault-history'/(prior+'.json'), self.state)
        self.state = dict(identity, active=True, started_wall=q['started_wall'],
                          deadline_wall=q['deadline_wall'], originals={}, holds={}, processed={}, paused=None)
        self.save()
        return True

    def _release(self, target, reason):
        command = {key: self.state[key] for key in ('run_id', 'epoch')}
        command['operation_id'] = target
        held = scene_path(self.directory, 'withheld-acks', command['run_id'], command['epoch'], target)
        if held.exists():
            original = held.read_bytes()
            # Publication validates the original identity and exact bytes. It
            # never refreshes time or replaces another scope's ACK. Retain a
            # scoped archival original before removing the held inbox copy.
            publish_ack_bytes(self.directory, command, original)
            publish_ack_bytes(self.directory, command, original, channel='fault-history')
            held.unlink()
            self.event('original_ack_released', operation_id=target, reason=reason,
                       sha256=hashlib.sha256(original).hexdigest(), bytes=len(original))
        else:
            self.event('ack_hold_removed_before_ack_produced', operation_id=target, reason=reason)
        self.state['holds'].pop(target, None)
        self.save()

    def _resume(self, reason):
        paused = self.state.get('paused') if self.state else None
        if paused:
            control = self.world.control or {}
            q = control.get('fault_qualification') or {}
            same_identity = all(control.get(key) == self.state.get(key) for key in ('run_id', 'epoch'))
            motion_locked = (same_identity and q.get('qualification_id') == self.state['qualification_id']
                             and q.get('simulation_pause_requested') is True)
            if control.get('stop_requested') is not True and not motion_locked:
                # Playing a timeline is an actuator action. Without the
                # backend's permanent motion latch (or its stop), an old Nav2
                # command must not become live again after this fault test.
                if not paused.get('resume_blocked_motion_lock_unconfirmed'):
                    paused['resume_blocked_motion_lock_unconfirmed'] = True
                    self.save()
                    self.event('timeline_resume_blocked_motion_lock_unconfirmed', reason=reason,
                               run_id=self.state.get('run_id'), epoch=self.state.get('epoch'))
                return False
            actual_playing = None
            if paused['was_playing'] and self.timeline:
                if paused.get('phase') != 'resume_requested' or self.resume_after_restart:
                    # Timeline controls may only take effect at the next Kit
                    # update. Persist recovery before issuing the actuator call.
                    try:
                        already_playing = self.timeline.is_playing()
                    except Exception as error:
                        already_playing = None
                        self.event('timeline_resume_pre_request_readback_failed', error=repr(error))
                    paused.update(phase='resume_requested', resume_requested_wall=time.time(), resume_reason=reason,
                                  resume_requested_poll_sequence=self.poll_sequence,
                                  resume_requires_later_poll=already_playing is not False)
                    self.resume_after_restart = False
                    self.save()
                    try:
                        self.timeline.play()
                    except Exception as error:
                        paused['resume_call_error'] = repr(error)
                        self.save()
                        self.event('timeline_resume_call_failed', reason=reason, error=repr(error), resumes_navigation=False)
                        return False
                try:
                    actual_playing = self.timeline.is_playing()
                except Exception as error:
                    self.event('timeline_resume_readback_failed', error=repr(error), resumes_navigation=False)
                    return False
                if not actual_playing:
                    return False  # Pending, not a transient exception or ACK.
                if (paused.get('resume_requires_later_poll') and
                        self.poll_sequence <= paused.get('resume_requested_poll_sequence', self.poll_sequence)):
                    return False  # A queued pause/play pair needs a later actual readback.
            self.event('timeline_resume_requested', reason=reason,
                       actual_playing=actual_playing,
                       original_pause=paused, resumes_navigation=False)
            self.state['paused'] = None
            self.save()
        return True

    def close(self, reason):
        if not self.state or (not self.state.get('active') and not self.state.get('paused')
                              and not self.state.get('holds') and not self.state.get('restoration_pending')):
            return
        # Recovery of committed world state/ACKs is allowed after operator stop.
        for target in list(self.state.get('holds', {})):
            self._release(target, reason)
        restored = self._resume(reason)
        current = self.world.state or {}
        if all(self.state.get(key) == current.get(key) for key in ('run_id', 'epoch')):
            for cabinet, original in self.state.get('originals', {}).items():
                current['devices'][cabinet]['available'] = original
            self.world._save()
        self.state.update(active=False, end_reason=reason)
        if not restored:
            self.state['restoration_pending'] = True
            self.save()
            return
        self.state.pop('restoration_pending', None)
        self.state['ended_wall'] = time.time()
        self.event('fault_controls_restored', reason=reason, originals=self.state.get('originals', {}),
                   run_id=self.state.get('run_id'), epoch=self.state.get('epoch'),
                   qualification_id=self.state.get('qualification_id'))
        self.save()

    def _finish_timeline_pending(self, operation_id, sim_time, result=None, error=None):
        item = self.state['processed'][operation_id]
        if not item.get('pending_timeline'):
            return
        command = item['command']
        path = self.directory/'fault-acks'/(operation_id+'.json')
        if path.exists():
            ack = json.loads(path.read_text(encoding='utf-8'))
        else:
            ack = {key: command.get(key) for key in ('run_id', 'epoch', 'qualification_id', 'session_key', 'operation_id')}
            ack.update(status='failed' if error else 'completed', result=result or {},
                       wall_time=time.time(), sim_time=sim_time)
            if error:
                ack['error'] = error
            atomic_json(path, ack)
            self.event('qualification_fault_command_result', ack=ack)
        item.update(ack=copy.deepcopy(ack), pending_timeline=False)
        self.save()

    def _poll_timeline(self, sim_time, qualification):
        if not self.state or not self.timeline:
            return
        paused = self.state.get('paused')
        try:
            playing = self.timeline.is_playing()
        except Exception as error:
            self.event('timeline_control_readback_failed', error=repr(error))
            return
        now = time.time()
        if (paused and paused.get('phase') == 'pause_requested' and not playing
                and qualification and now < paused['resume_wall'] and not paused.get('pause_call_error')):
            paused.update(phase='paused', pause_confirmed_wall=now)
            self.save()
        if paused and paused.get('phase') == 'resume_requested':
            self._resume('pending_resume_actual_readback')
            paused = self.state.get('paused')
        for operation_id, item in list(self.state.get('processed', {}).items()):
            kind = item.get('pending_timeline')
            if not kind:
                continue
            if kind == 'pause_simulation':
                if (paused and paused.get('phase') == 'paused' and qualification
                        and not playing and now < paused['resume_wall']):
                    self._finish_timeline_pending(operation_id, sim_time,
                        {'actual_playing': False, 'paused': copy.deepcopy(paused), 'sim_time': sim_time})
                elif (not paused or paused.get('phase') == 'resume_requested'
                      or now >= item['deadline_wall'] or not qualification):
                    self._finish_timeline_pending(operation_id, sim_time,
                        error='actual_timeline_pause_not_confirmed_before_wall_limit')
            elif kind == 'resume_simulation':
                if now >= item['deadline_wall']:
                    self._finish_timeline_pending(operation_id, sim_time,
                        error='actual_timeline_resume_not_confirmed_before_wall_limit')
                elif not paused and playing:
                    self._finish_timeline_pending(operation_id, sim_time,
                        {'actual_playing': True, 'resumes_navigation': False, 'sim_time': sim_time})

    def _pause_guard(self, q, sim_time):
        if q.get('simulation_pause_requested') is not True:
            raise ValueError('qualification_pause_backend_motion_lock_not_confirmed')
        actual = self.world.physical_reader() if self.world.physical_reader else None
        if not actual or actual.get('available') is not True or actual.get('stopped_samples', 0) < 3:
            raise ValueError('qualification_pause_physical_stop_not_confirmed')
        fields = ('x', 'y', 'wall_time', 'sim_time', 'speed_mps', 'angular_speed_rad_s',
                  'velocity_wall_time', 'velocity_sim_time')
        if any(isinstance(actual.get(key), bool) or not isinstance(actual.get(key), (int, float))
               or not math.isfinite(actual[key]) for key in fields):
            raise ValueError('qualification_pause_physical_sample_invalid')
        if actual['speed_mps'] < 0:
            raise ValueError('qualification_pause_physical_sample_invalid')
        now = time.time()
        if (not 0 <= now-actual['wall_time'] <= 1 or
                not 0 <= now-actual['velocity_wall_time'] <= 1 or
                not 0 <= sim_time-actual['velocity_sim_time'] <= .11 or
                actual.get('velocity_available') is not True):
            raise ValueError('qualification_pause_physical_sample_stale')
        if actual['speed_mps'] >= .03 or abs(actual['angular_speed_rad_s']) >= .03:
            raise ValueError('qualification_pause_physical_motion_detected')
        return copy.deepcopy(actual)

    def write_ack(self, command, ack):
        target = command['operation_id']
        semantic = lambda value: {key: val for key, val in value.items() if key not in {'wall_time', 'sim_time'}}
        try:
            visible = read_scene_ack(self.directory, command)
        except FileNotFoundError:
            visible = None
        if visible is not None:
            if semantic(visible) != semantic(ack):
                raise SceneIOConflict('scene_ack_replay_payload_conflict')
            return visible
        same_scope = self.state and all(self.state.get(key) == command.get(key) for key in ('run_id', 'epoch'))
        hold = (self.state or {}).get('holds', {}).get(target) if same_scope else None
        held_path = scene_path(self.directory, 'withheld-acks', command['run_id'], command['epoch'], target)
        if held_path.exists():
            original = read_scene_ack(self.directory, command, channel='withheld-acks')
            if semantic(original) != semantic(ack):
                raise SceneIOConflict('withheld_ack_replay_payload_conflict')
            if not same_scope:
                raise SceneIOConflict('scoped_withheld_ack_recovery_identity_unconfirmed')
        if command.get('kind') == 'handoff' and hold and self.qualification() and time.time() < hold['deadline_wall']:
            if not held_path.exists():
                publish_scene_ack(self.directory, command, ack, channel='withheld-acks')
                self.event('actual_ack_withheld', operation_id=target,
                           sha256=hashlib.sha256(held_path.read_bytes()).hexdigest(),
                           actual_ack_wall_time=ack['wall_time'], actual_ack_sim_time=ack['sim_time'])
            # Replays cannot create a newer ACK while the original is withheld.
            original = read_scene_ack(self.directory, command, channel='withheld-acks')
            if semantic(original) != semantic(ack):
                raise SceneIOConflict('withheld_ack_replay_payload_conflict')
            return original
        if same_scope and held_path.exists():
            self._release(target, 'hold_ended_before_ack_replay')
            return read_scene_ack(self.directory, command)
        return publish_scene_ack(self.directory, command, ack)

    def _execute(self, command, q, sim_time):
        kind, args = command['kind'], command['arguments']
        if kind not in self.KINDS:
            raise ValueError('qualification_fault_kind_not_allowed')
        if kind == 'set_cabinet_availability':
            cabinet, available = args['cabinet_id'], args['available']
            if cabinet not in CABINETS or not isinstance(available, bool):
                raise ValueError('qualification_cabinet_availability_invalid')
            device = self.world.state['devices'][cabinet]
            self.state['originals'].setdefault(cabinet, device['available'])
            # Persist the original before changing the actual scene state.
            self.save()
            device['available'] = available
            self.world._save()
            return {'cabinet_id': cabinet, 'available': device['available'], 'source': 'simulation_state'}
        if kind in {'hold_ack', 'release_ack'}:
            target = args['target_operation_id']
            if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', target) or target in {'.', '..'}:
                raise ValueError('qualification_ack_target_id_invalid')
            if kind == 'release_ack':
                if target not in self.state['holds']:
                    raise ValueError('qualification_ack_target_not_held')
                self._release(target, 'explicit_release_same_operation')
                return {'target_operation_id': target, 'released': True}
            source = scene_path(self.directory, 'commands', command['run_id'], command['epoch'], target)
            if not source.is_file():
                raise ValueError('qualification_handoff_command_not_found')
            prepared = json.loads(source.read_text(encoding='utf-8'))
            if prepared.get('kind') != 'handoff' or any(prepared.get(key) != command[key] for key in ('run_id', 'epoch')):
                raise ValueError('qualification_ack_target_not_current_handoff')
            if scene_path(self.directory, 'acks', command['run_id'], command['epoch'], target).exists():
                raise ValueError('target_ack_already_visible')
            if self.state['holds'] and target not in self.state['holds']:
                raise ValueError('qualification_only_one_ack_hold_at_a_time')
            seconds = args.get('hold_seconds', 90)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 90:
                raise ValueError('qualification_hold_seconds_invalid')
            # A second new operation cannot extend the original hold lease.
            self.state['holds'].setdefault(target, {'deadline_wall': min(q['deadline_wall'], time.time()+seconds),
                                                    'armed_wall': time.time()})
            return {'target_operation_id': target, 'hold': copy.deepcopy(self.state['holds'][target])}
        if not self.timeline:
            raise ValueError('actual_timeline_control_unavailable')
        if kind == 'pause_simulation':
            seconds = args.get('seconds', 5)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 5 <= seconds <= 15:
                raise ValueError('qualification_pause_seconds_invalid')
            if self.state.get('paused') or not self.timeline.is_playing():
                raise ValueError('qualification_timeline_already_paused')
            physical_stop = self._pause_guard(q, sim_time)
            self.state['paused'] = {'was_playing': True, 'started_wall': time.time(),
                                    'resume_wall': min(q['deadline_wall'], time.time()+seconds),
                                    'sim_time': sim_time, 'physical_stop': physical_stop,
                                    'backend_motion_lock_confirmed': True, 'phase': 'pause_requested'}
            # Persist the safe auto-resume deadline before requesting the pause.
            self.save()
            try:
                self.timeline.pause()
            except Exception as error:
                self.state['paused'].update(phase='pause_failed', pause_call_error=repr(error))
                self.save()
                raise RuntimeError('actual_timeline_pause_call_failed:'+repr(error)) from error
            return None  # Completion requires actual readback, even for a synchronous fake.
        if not self._resume('explicit_qualification_resume'):
            if self.state.get('paused', {}).get('resume_blocked_motion_lock_unconfirmed'):
                raise RuntimeError('qualification_resume_backend_motion_lock_not_confirmed')
            return None
        if not self.timeline.is_playing():
            raise RuntimeError('actual_timeline_resume_not_confirmed')
        return {'actual_playing': True, 'resumes_navigation': False, 'sim_time': sim_time}

    def tick(self, sim_time):
        self.poll_sequence += 1
        q = self.qualification()
        self._poll_timeline(sim_time, q)
        if not q:
            self.close('qualification_inactive_expired_or_invalid')
            self._record_unobserved_closed_qualification()
        else:
            if not self._ensure_state(q):
                q = None
            else:
                now = time.time()
                for target, hold in list(self.state['holds'].items()):
                    if now >= hold['deadline_wall']:
                        self._release(target, 'hold_deadline_reached')
                if self.state.get('paused') and now >= self.state['paused']['resume_wall']:
                    self._resume('pause_wall_limit_reached')
        self._poll_timeline(sim_time, self.qualification())
        for path in sorted((self.directory/'fault-commands').glob('*.json')):
            try:
                command = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(command, dict):
                    raise ValueError('qualification_fault_command_not_object')
            except (ValueError, OSError) as error:
                fingerprint = (path.name, str(error))
                if fingerprint not in self.invalid_files:
                    self.invalid_files.add(fingerprint)
                    self.event('invalid_fault_command_file', filename=path.name, error=str(error))
                continue
            op = command.get('operation_id', '')
            if not isinstance(op, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', op) or op in {'.', '..'} or path.stem != op:
                continue
            # Original fault receipts are permanent evidence across epochs.
            if (self.directory/'fault-acks'/path.name).exists():
                prior = (self.state or {}).get('processed', {}).get(op)
                signature = hashlib.sha256(json.dumps(command, sort_keys=True, allow_nan=False).encode()).hexdigest()
                if prior and prior['signature'] != signature:
                    # Record conflict separately; do not replace the original
                    # successful receipt with a second meaning for this ID.
                    marker = self.directory/'fault-history'/(self.state['qualification_id']+'-'+op+'-conflict.json')
                    if not marker.exists():
                        atomic_json(marker, {'error': 'qualification_fault_operation_payload_conflict',
                                             'original_signature': prior['signature'], 'conflicting_command': command})
                        self.event('qualification_fault_operation_payload_conflict', operation_id=op)
                continue
            ack = {key: command.get(key) for key in ('run_id', 'epoch', 'qualification_id', 'session_key', 'operation_id')}
            ack.update(status='failed', result={}, wall_time=time.time(), sim_time=sim_time)
            try:
                if not q or any(command.get(key) != value for key, value in self._identity(q).items()):
                    raise ValueError('fault_qualification_identity_or_lease_invalid')
                signature = hashlib.sha256(json.dumps(command, sort_keys=True, allow_nan=False).encode()).hexdigest()
                prior = self.state['processed'].get(op)
                if prior:
                    if prior['signature'] != signature:
                        if prior.get('pending_timeline'):
                            marker = self.directory/'fault-history'/(self.state['qualification_id']+'-'+op+'-conflict.json')
                            if not marker.exists():
                                atomic_json(marker, {'error': 'qualification_fault_operation_payload_conflict',
                                    'original_signature': prior['signature'], 'conflicting_command': command})
                            continue  # Never replace the original pending operation or its ACK.
                        raise ValueError('qualification_fault_operation_payload_conflict')
                    if prior.get('pending_timeline'):
                        continue
                    ack = prior['ack']
                else:
                    if len(self.state['processed']) >= 20:
                        raise ValueError('qualification_fault_operation_limit')
                    try:
                        result = self._execute(command, q, sim_time)
                        if result is None:
                            paused = self.state.get('paused') or {}
                            self.state['processed'][op] = {'signature': signature, 'command': copy.deepcopy(command),
                                'pending_timeline': command['kind'],
                                'deadline_wall': paused.get('resume_wall', q['deadline_wall'])}
                            self.save()
                            self._poll_timeline(sim_time, self.qualification())
                            continue
                        ack.update(status='completed', result=result)
                    except (ValueError, TypeError, KeyError, RuntimeError) as error:
                        ack['error'] = str(error)
                    # Count and freeze failed authorized attempts too.
                    self.state['processed'][op] = {'signature': signature, 'ack': copy.deepcopy(ack)}
                    self.save()
            except (ValueError, TypeError, KeyError, RuntimeError) as error:
                ack['error'] = str(error)
            atomic_json(self.directory/'fault-acks'/path.name, ack)
            self.event('qualification_fault_command_result', ack=ack)


class SmartWardWorld:
    """Disk queue -> validated scene change -> world commit -> durable ACK."""
    def __init__(self, root, actuator=None, physical_reader=None, config=None, timeline=None):
        self.root = Path(root)
        self.directory = self.root/'runs/smart-ward-v1/scene'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config or json.loads((self.root/'configs/smart-ward-v1.json').read_text(encoding='utf-8'))
        self.actuator = actuator
        self.physical_reader = physical_reader
        self.stations = json.loads((self.root/'configs/stations.json').read_text(encoding='utf-8'))
        self.state = None
        self.control = None
        self.pending = None
        self.last_publish = 0
        self.last_sim = None
        self.faults = SceneQualificationFaults(self, timeline)

    def _initial(self, control):
        normal = float(self.config.get('lights', {}).get('normal_percent', 60))
        devices = {ident: {'kind': 'cabinet', 'station_id': station, 'room_id': room,
                            'capacity': capacity, 'available': True, 'door_angle': 0.,
                            'door_open_fraction': 0., 'specimens': [], 'source': 'simulation_state'}
                   for ident, (station, room, capacity) in CABINETS.items()}
        for room in ROOMS:
            suffix = room[-3:]
            devices['hospital-light-'+suffix] = {'kind': 'light', 'room_id': room, 'available': True,
                                                 'brightness_percent': normal, 'stuck': False, 'source': 'USD_light_attribute_readback'}
            devices['hospital-sensor-'+suffix] = {'kind': 'sensor', 'room_id': room, 'available': True,
                                                  'relative_illumination': relative_illumination(normal),
                                                  'unit': 'relative_index_0_100', 'source': 'simulation_model',
                                                  'model': 'clamp(20+0.8*actual_brightness_percent,0,100)'}
        return {'run_id': control['run_id'], 'epoch': control['epoch'], 'seq': 0,
                'devices': devices, 'specimens': {}, 'transfers': {}, 'processed': {}}

    def _save(self):
        atomic_json(self.directory/'world.json', self.state)
        if self.actuator and hasattr(self.actuator, 'save_overlay'):
            self.actuator.save_overlay()

    def _control(self):
        try:
            control = json.loads((self.directory/'control.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return False
        if not control.get('run_id') or not control.get('epoch'):
            raise ValueError('invalid_smart_scene_control')
        self.control = control
        identity = (control['run_id'], control['epoch'])
        if self.state is not None and identity == (self.state['run_id'], self.state['epoch']):
            return True
        saved = self.directory/'world.json'
        prior = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else None
        # On process restart load the actual durable world before restoring
        # qualification controls; otherwise saved unavailable cabinets could
        # survive cleanup while their original value was discarded.
        if self.state is None:
            self.state = prior
        self.faults.close('scene_run_or_epoch_changed_or_process_restarted')
        self.pending = None
        self.last_sim = None
        self.state = prior if prior and identity == (prior['run_id'], prior['epoch']) else self._initial(control)
        for ident, device in self.state['devices'].items():
            if device['kind'] == 'cabinet':
                device.update(door_angle=0., door_open_fraction=0.)
                if self.actuator:
                    self.actuator.set_door(ident, 0.)
            elif device['kind'] == 'light' and self.actuator:
                self.actuator.set_light(device['room_id'], device['brightness_percent'])
        self._save()
        return True

    def _stop_requested(self):
        # Missing flags keep the original protocol compatible.  Environmental
        # demo injections remain allowed while scheduling is disabled.
        return bool(self.control.get('stop_requested', False) or not self.control.get('enabled', True))

    def _ack(self, command, status, result, sim_time, error=None):
        ack = {k: command[k] for k in ('run_id', 'epoch', 'operation_id')}
        ack.update(status=status, result=result, wall_time=time.time(), sim_time=sim_time)
        if error:
            ack['error'] = error
        return self.faults.write_ack(command, ack)

    def _finish(self, command, result, sim_time, error=None):
        status = 'failed' if error else 'completed'
        self.state['processed'][command['operation_id']] = {'signature': operation_signature(command),
                'status': status, 'result': result, 'error': error}
        # A crash after this durable commit but before ACK can only replay the result.
        self._save()
        return self._ack(command, status, result, sim_time, error)

    def _command_conflict(self, command, reason, prior=None):
        """Record a refused current-scope command without replacing its ACK."""
        signature = hashlib.sha256(json.dumps(command, sort_keys=True, allow_nan=False).encode()).hexdigest()
        marker = scene_path(self.directory, 'fault-history', self.state['run_id'], self.state['epoch'],
                            'command-conflict-'+signature)
        if not marker.exists():
            atomic_json(marker, {'error': reason, 'original_signature': (prior or {}).get('signature'),
                                 'conflicting_command': command})
            self.faults.event('scene_command_refused_without_ack_replacement', operation_id=command.get('operation_id'),
                              run_id=self.state['run_id'], epoch=self.state['epoch'], error=reason)

    def _light(self, room):
        if room not in ROOMS:
            raise ValueError('unknown_light_room')
        return self.state['devices']['hospital-light-'+room[-3:]]

    def _check_transfer(self, args, claim_validated=False):
        ident, direction = args['cabinet_id'], args['direction']
        if ident not in CABINETS or direction not in {'pickup', 'receive'}:
            raise ValueError('invalid_handoff_cabinet_or_direction')
        cabinet = self.state['devices'][ident]
        if not cabinet['available'] or args['station_id'] != cabinet['station_id']:
            raise ValueError('handoff_receiver_unavailable_or_wrong_station')
        specimen = self.state['specimens'].get(args['specimen_id'])
        if not specimen or not specimen['sealed']:
            raise ValueError('specimen_missing_or_not_sealed')
        robot_count = sum(s['location'] == 'robot' for s in self.state['specimens'].values())
        if direction == 'pickup':
            if ident == 'hospital-cabinet-lab' or specimen['location'] != ident:
                raise ValueError('specimen_not_in_pickup_cabinet')
            if robot_count >= 2:
                raise ValueError('robot_capacity_exceeded')
        else:
            if ident != 'hospital-cabinet-lab' or specimen['location'] != 'robot':
                raise ValueError('specimen_not_on_robot_or_wrong_receiver')
            if len(cabinet['specimens']) >= cabinet['capacity']:
                raise ValueError('receiver_cabinet_full')
        proof = args.get('arrival_proof', {})
        if not claim_validated and not 0 <= time.time()-float(proof.get('wall_time', 0)) <= 3:
            raise ValueError('handoff_arrival_proof_stale')
        actual = self.physical_reader() if self.physical_reader else None
        if not actual or not actual.get('available') or actual.get('stopped_samples', 0) < 3:
            raise ValueError('physical_stop_not_confirmed')
        if not all(isinstance(actual.get(k), (int, float)) and math.isfinite(actual[k]) for k in ('x', 'y', 'wall_time')):
            raise ValueError('physical_scene_pose_invalid')
        if not 0 <= time.time()-actual['wall_time'] <= 1:
            raise ValueError('physical_scene_pose_stale')
        target = self.stations[args['station_id']]
        if math.hypot(actual['x']-target['x'], actual['y']-target['y']) > .5:
            raise ValueError('physical_scene_wrong_handoff_location')
        claimed = proof.get('pose', {})
        if not all(isinstance(claimed.get(k), (int, float)) and math.isfinite(claimed[k]) for k in ('x', 'y')) or math.hypot(actual['x']-claimed['x'], actual['y']-claimed['y']) > .30:
            raise ValueError('handoff_pose_disagrees_with_scene')
        return specimen, cabinet, actual

    def _execute(self, command, sim_time):
        args, kind = command['arguments'], command['kind']
        if kind == 'set_brightness' and self._stop_requested():
            raise ValueError('run_stopped_before_commit')
        if kind == 'insert_specimen':
            ident = args['cabinet_id']
            if ident not in {'hospital-cabinet-b', 'hospital-cabinet-c'}:
                raise ValueError('specimens_must_be_inserted_at_source_cabinet')
            cabinet = self.state['devices'][ident]
            if len(cabinet['specimens']) >= cabinet['capacity']:
                raise ValueError('source_cabinet_full')
            priority = args.get('priority', 'normal')
            if priority not in {'normal', 'urgent'}:
                raise ValueError('invalid_specimen_priority')
            sid = 'S-'+hashlib.sha256((str(command['run_id'])+':'+str(command['epoch'])+':'+command['operation_id']).encode()).hexdigest()[:16]
            deadlines = self.config.get('specimen_deadlines', {})
            duration = deadlines.get(priority, 300 if priority == 'urgent' else 900)
            specimen = {'specimen_id': sid, 'location': ident, 'priority': priority, 'sealed': True,
                        'created_sim': sim_time, 'deadline_sim': sim_time+float(duration)}
            self.state['specimens'][sid] = specimen
            cabinet['specimens'].append(sid)
            return {'specimen_id': sid, 'specimen': copy.deepcopy(specimen)}
        if kind == 'set_brightness':
            light = self._light(args['room_id'])
            value = args['brightness_percent']
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('invalid_brightness_percent')
            if not light['stuck']:
                if self.actuator:
                    self.actuator.set_light(args['room_id'], value)
                light['brightness_percent'] = float(value)
            if self.actuator:
                light['brightness_percent'] = self.actuator.read_light(args['room_id'])
            return {'room_id': args['room_id'], 'requested_percent': value, 'actual_percent': light['brightness_percent'], 'stuck': light['stuck']}
        if kind == 'set_lamp_fault':
            if not isinstance(args['stuck'], bool):
                raise ValueError('invalid_lamp_fault')
            light = self._light(args['room_id']); light['stuck'] = args['stuck']
            return {'room_id': args['room_id'], 'stuck': light['stuck']}
        if kind == 'set_receiver':
            if not isinstance(args['available'], bool):
                raise ValueError('invalid_receiver_availability')
            self.state['devices']['hospital-cabinet-lab']['available'] = args['available']
            return {'available': args['available']}
        if kind == 'handoff':
            transfer_id = args['transfer_id']
            previous = self.state['transfers'].get(transfer_id)
            if previous:
                expected = {k: args[k] for k in ('specimen_id', 'direction', 'cabinet_id', 'order_id')}
                if any(previous.get(k) != value for k, value in expected.items()):
                    raise ValueError('transfer_id_payload_conflict')
                return {**copy.deepcopy(previous), 'transfer': copy.deepcopy(previous), 'replayed': True}
            if self._stop_requested():
                raise ValueError('run_stopped_before_commit')
            self._check_transfer(args)
            self.pending = {'command': command, 'start_sim': sim_time}
            return None
        raise ValueError('unknown_scene_command')

    def poll_controls(self, sim_time):
        if not self._control():
            return False
        self.faults.tick(sim_time)
        return True

    def tick(self, sim_time):
        if not self.poll_controls(sim_time):
            return
        # A paused scene cannot produce new sensor evidence or execute a
        # transfer.  Wall-clock publication alone must never refresh old data.
        if self.last_sim is not None and sim_time <= self.last_sim:
            return
        self.last_sim = sim_time
        if self.pending:
            item = self.pending; command = item['command']; args = command['arguments']
            if self._stop_requested() and not item.get('transferred'):
                cabinet = self.state['devices'][args['cabinet_id']]
                cabinet['door_open_fraction'] = 0.
                if self.actuator:
                    self.actuator.set_door(args['cabinet_id'], 0.)
                self._finish(command, {}, sim_time, 'run_stopped_before_commit')
                self.pending = None
                return
            if not item.get('transferred'):
                try:
                    # Original proof was checked at admission.  Animation can
                    # exceed three wall seconds at low real-time factor: each
                    # subsequent check uses a NEW physical sample, and requires
                    # it to remain stopped at the original validated claim.
                    self._check_transfer(args, claim_validated=True)
                except (ValueError, KeyError) as error:
                    cabinet = self.state['devices'][args['cabinet_id']]
                    cabinet['door_open_fraction'] = 0.
                    if self.actuator:
                        self.actuator.set_door(args['cabinet_id'], 0.)
                    self._finish(command, {}, sim_time, str(error))
                    self.pending = None
                    return
            elapsed = max(0., sim_time-item['start_sim'])
            fraction = min(1., elapsed/.75) if elapsed <= .75 else max(0., 1.-(elapsed-.75)/.75)
            cabinet = self.state['devices'][args['cabinet_id']]
            cabinet['door_open_fraction'] = fraction
            if self.actuator:
                self.actuator.set_door(args['cabinet_id'], fraction)
            if elapsed >= .75 and not item.get('transferred'):
                try:
                    specimen, cabinet, actual = self._check_transfer(args, claim_validated=True)
                    if args['direction'] == 'pickup':
                        cabinet['specimens'].remove(args['specimen_id']); specimen['location'] = 'robot'
                    else:
                        cabinet['specimens'].append(args['specimen_id']); specimen['location'] = args['cabinet_id']
                    transfer = {k: args[k] for k in ('transfer_id', 'order_id', 'specimen_id', 'cabinet_id', 'direction')}
                    transfer.update(location=specimen['location'], operation_id=command['operation_id'], physical_arrival=actual, sim_time=sim_time)
                    self.state['transfers'][args['transfer_id']] = transfer
                    item['result'] = {**copy.deepcopy(transfer), 'transfer': copy.deepcopy(transfer)}
                    item['transferred'] = True
                    self._save()
                except (ValueError, KeyError) as error:
                    item.update(error=str(error), result={}, transferred=True)
            if elapsed >= 1.5:
                self._finish(command, item['result'], sim_time, item.get('error'))
                cabinet['door_open_fraction'] = 0.
                if self.actuator:
                    self.actuator.set_door(args['cabinet_id'], 0.)
                self.pending = None
                self._save()
        else:
            for path in iter_commands(self.directory, self.state['run_id'], self.state['epoch']):
                command = json.loads(path.read_text(encoding='utf-8'))
                ident = command.get('operation_id', '')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', ident) or ident in {'.', '..'} or path.stem != ident:
                    continue  # Never let untrusted identifiers choose a path.
                if (command.get('run_id'), command.get('epoch')) != (self.state['run_id'], self.state['epoch']):
                    self._command_conflict(command, 'scene_run_or_epoch_mismatch')
                    continue
                signature = operation_signature(command)
                prior = self.state['processed'].get(ident)
                if prior:
                    if signature != prior['signature']:
                        self._command_conflict(command, 'operation_id_payload_conflict', prior)
                    else:
                        try:
                            read_scene_ack(self.directory, command)
                        except FileNotFoundError:
                            self._ack(command, prior['status'], prior['result'], sim_time, prior.get('error'))
                    continue
                if any(scene_path(self.directory, channel, command['run_id'], command['epoch'], ident).exists()
                       for channel in ('acks', 'withheld-acks')):
                    self._command_conflict(command, 'scene_ack_without_durable_processed_command')
                    continue
                try:
                    result = self._execute(command, sim_time)
                    if result is not None:
                        self._finish(command, result, sim_time)
                except (ValueError, KeyError, TypeError) as error:
                    self._finish(command, {}, sim_time, str(error))
                if self.pending:
                    break
        if self.actuator:
            self.actuator.sync_packages(self.state)
        now = time.time()
        if now-self.last_publish >= 1/float(self.config.get('sampling_hz', 1)):
            self.last_publish = now
            self.state['seq'] += 1
            for room in ROOMS:
                light = self._light(room)
                if self.actuator:
                    light['brightness_percent'] = self.actuator.read_light(room)
                sensor = self.state['devices']['hospital-sensor-'+room[-3:]]
                sensor['relative_illumination'] = relative_illumination(light['brightness_percent'])
            for device in self.state['devices'].values():
                device.update(wall_time=now, sim_time=sim_time, seq=self.state['seq'])
            if self.actuator and hasattr(self.actuator, 'read_door'):
                for ident in CABINETS:
                    self.state['devices'][ident]['door_open_fraction'] = self.actuator.read_door(ident)
            observation = {k: copy.deepcopy(self.state[k]) for k in ('run_id', 'epoch', 'seq', 'devices', 'specimens', 'transfers')}
            observation.update(wall_time=now, sim_time=sim_time)
            if self.physical_reader:
                observation['physical_robot'] = self.physical_reader()
            self._save()
            atomic_json(self.directory/'observation.json', observation)


class SmartWardScene:
    """Actual scene objects, collision envelopes, USD readback and physical stop."""
    def __init__(self, root, stage, robot_body):
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics
        self.root, self.stage, self.body = Path(root), stage, robot_body
        self.Gf, self.Usd, self.UsdGeom, self.UsdLux, self.UsdPhysics = Gf, Usd, UsdGeom, UsdLux, UsdPhysics
        overlay_directory = self.root/'runs/smart-ward-v1/scene/overlays'
        overlay_directory.mkdir(parents=True, exist_ok=True)
        self.overlay_path = overlay_directory/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex+'.usda')
        self.layer = Sdf.Layer.CreateNew(str(self.overlay_path))
        if self.layer is None:
            raise RuntimeError('smart_scene_overlay_creation_failed')
        stage.GetSessionLayer().subLayerPaths.insert(0, self.layer.identifier)
        self.config = json.loads((self.root/'configs/smart-ward-v1.json').read_text(encoding='utf-8'))
        hospital = json.loads((self.root/'configs/hospital-v2.json').read_text(encoding='utf-8'))
        self.rooms = {room['id']: room for room in hospital['rooms']}
        self.stations = json.loads((self.root/'configs/stations.json').read_text(encoding='utf-8'))
        self.base = '/World/HospitalSmartDevices'
        self.doors, self.lamps, self.package_positions = {}, {}, {}
        self.physical = None; self.previous_physical = None; self.stopped_samples = 0; self.last_velocity = None
        inventory_started = time.monotonic()
        obstacles, inventory = self._inventory_original_geometry()
        inventory_seconds = time.monotonic()-inventory_started
        original_obstacles = copy.deepcopy(obstacles)
        atomic_json(self.root/'runs/smart-ward-v1/scene/placement-audit.json',
                    {'source': 'USD_instance_proxy_geometry_and_collision_inventory', 'original_obstacles': original_obstacles,
                     'inventory': inventory,
                     'counts': {'boundables': len(inventory), 'mesh': sum(row['type'] == 'Mesh' for row in inventory),
                                'instance_proxies': sum(row['instance_proxy'] for row in inventory),
                                'excluded': sum(row['decision'] == 'exclude' for row in inventory),
                                'mesh_faces_read': sum(row.get('face_count', 0) for row in inventory),
                                'placement_obstacles': len(obstacles)},
                     'inventory_wall_seconds': inventory_seconds,
                     'room_ids': [v[1] for v in CABINETS.values()], 'phase': 'before_placement'})
        if not any(row['type'] == 'Mesh' for row in inventory):
            raise RuntimeError('smart_geometry_inventory_no_original_meshes')
        placements = []
        for ident, (station, room_id, capacity) in CABINETS.items():
            # x-depth cabinets face +X at B/C and -X at lab; door slides along Y.
            custom = self.config.get('cabinets', {}).get(ident, {})
            dimensions = custom.get('dimensions_m', [.40, .90 if capacity == 2 else 1.20, 1.35])
            if len(dimensions) != 3 or any(not math.isfinite(float(v)) or float(v) <= 0 for v in dimensions):
                raise ValueError('invalid_cabinet_dimensions:'+ident)
            placement = choose_cabinet_position(self.rooms[room_id], dimensions, [o['bounds'] for o in obstacles], list(self.stations.values()))
            placement.update(id=ident, room_id=room_id, station_id=station, capacity=capacity, dimensions_m=dimensions)
            placements.append(placement)
            self._build_cabinet(placement)
            # Subsequent cabinets must not overlap an already selected cabinet.
            b = placement['collision_bounds_xy']; obstacles.append({'path': self.base+'/'+usd_device_identifier(ident), 'bounds': [b[0], b[1], 0, b[2], b[3], dimensions[2]]})
        for room_id, layout_id in ROOMS.items():
            bounds = self.rooms[layout_id]['bounds']
            position = [(bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2, 2.6]
            self._build_light(room_id, position)
            self.set_light(room_id, self.config.get('lights', {}).get('normal_percent', 60))
        try:
            from .smart_map import write_overlay_map
        except ImportError:
            from smart_map import write_overlay_map
        self.map_proof = write_overlay_map(self.root/'maps/carter_hospital_navigation.yaml', self.root/'runs/smart-ward-v1/scene/map', placements, list(self.stations.values()))
        self.save_overlay()
        atomic_json(self.root/'runs/smart-ward-v1/scene/layout-proof.json', {'placements': placements, 'original_obstacles': original_obstacles,
                    'map': self.map_proof, 'source': 'original_USD_world_bounds_and_configured_rooms', 'collision_enabled': True,
                    'overlay_path': str(self.overlay_path), 'overlay_sha256': hashlib.sha256(self.overlay_path.read_bytes()).hexdigest(),
                    'overlay_manifest': str(self.root/'runs/smart-ward-v1/scene/overlay-manifest.json'),
                    'official_root_layer_saved': False,
                    'changes': 'Project USD sublayer: three static cabinets, envelope-contained sliding doors and two local lights; official assets unchanged'})
        import omni.timeline
        self.timeline = omni.timeline.get_timeline_interface()
        self.world = SmartWardWorld(self.root, actuator=self, physical_reader=lambda: copy.deepcopy(self.physical),
                                    config=self.config, timeline=self.timeline)

    def _inventory_original_geometry(self):
        """Expand furniture instances and retain evidence for every exclusion.

        Visible Meshes are placement obstacles even when not physical colliders.
        Mesh face bounds are only used for unapproximated triangle collisions or
        non-collision render geometry.  Convex/bounding/simplified colliders keep
        their full bound so the placement cannot silently weaken real physics.
        """
        Usd, UsdGeom, UsdPhysics = self.Usd, self.UsdGeom, self.UsdPhysics
        cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                    [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy, UsdGeom.Tokens.guide],
                    useExtentsHint=False)
        transforms = UsdGeom.XformCache(Usd.TimeCode.Default())
        rooms = [self.rooms[value[1]]['bounds'] for value in CABINETS.values()]
        regions = [[b[0], b[1], .04, b[2], b[3], 1.5] for b in rooms]
        records, obstacles = [], []
        # Stage.Traverse() omits instance proxies.  PrimRange with this predicate
        # visits the referenced furniture's actual Mesh children and transforms.
        for prim in Usd.PrimRange(self.stage.GetPseudoRoot(), Usd.TraverseInstanceProxies()):
            if not str(prim.GetPath()).startswith('/World/hospital/') or not prim.IsA(UsdGeom.Boundable):
                continue
            imageable = UsdGeom.Imageable(prim)
            record = {'path': str(prim.GetPath()), 'type': prim.GetTypeName(),
                      'applied_schemas': list(prim.GetAppliedSchemas()),
                      'instance_proxy': prim.IsInstanceProxy(), 'is_gprim': prim.IsA(UsdGeom.Gprim),
                      'purpose': str(imageable.ComputePurpose()), 'visibility': str(imageable.ComputeVisibility()),
                      'collision_attributes': {}, 'collision_ancestry': [], 'collision_enabled': False}
            for attribute in prim.GetAttributes():
                if 'collision' in attribute.GetName().lower():
                    value = attribute.Get()
                    record['collision_attributes'][attribute.GetName()] = value if isinstance(value, (bool, int, float, str)) or value is None else str(value)
            ancestor = prim
            while ancestor and not ancestor.IsPseudoRoot():
                if ancestor.HasAPI(UsdPhysics.CollisionAPI):
                    value = UsdPhysics.CollisionAPI(ancestor).GetCollisionEnabledAttr().Get()
                    record['collision_ancestry'].append({'path': str(ancestor.GetPath()), 'enabled': value})
                    if value is not False:
                        record['collision_enabled'] = True
                ancestor = ancestor.GetParent()
            decision, reason = geometry_inventory_decision(record)
            record.update(decision=decision, reason=reason)
            box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
            if box.IsEmpty():
                record.update(decision='exclude', reason='empty_USD_world_bound')
                records.append(record)
                continue
            lower, upper = box.GetMin(), box.GetMax()
            bounds = [float(v) for v in (*lower, *upper)]
            record['bounds'] = bounds
            records.append(record)
            if decision == 'exclude':
                continue
            if upper[2] <= .08 or lower[2] >= 1.5 or not any(boxes_intersect(bounds, region, .06) for region in regions):
                record['placement_scope'] = 'outside_cabinet_room_or_height'
                continue
            shape_bounds = [bounds]
            record['geometry_representation'] = 'conservative_full_USD_bound'
            if prim.IsA(UsdGeom.Mesh):
                mesh = UsdGeom.Mesh(prim)
                approximation = UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() if prim.HasAPI(UsdPhysics.MeshCollisionAPI) else 'none'
                subdivision = mesh.GetSubdivisionSchemeAttr().Get()
                record.update(collision_approximation=str(approximation), subdivision_scheme=str(subdivision))
                # Subdivision surfaces and approximated physics may extend past
                # individual face bounds; keep their whole bound conservatively.
                if (not record['collision_enabled'] or str(approximation) == 'none') and str(subdivision) == 'none':
                    try:
                        transform = transforms.GetLocalToWorldTransform(prim)
                        points = [tuple(float(v) for v in transform.Transform(self.Gf.Vec3d(*point))) for point in mesh.GetPointsAttr().Get()]
                        counts = [int(v) for v in mesh.GetFaceVertexCountsAttr().Get()]
                        indices = [int(v) for v in mesh.GetFaceVertexIndicesAttr().Get()]
                        shape_bounds = mesh_face_bounds(points, counts, indices, regions)
                        record.update(geometry_representation='actual_world_mesh_face_bounds',
                                      face_count=len(counts), placement_face_count=len(shape_bounds))
                    except (ValueError, TypeError, RuntimeError) as error:
                        record['face_bounds_fallback_reason'] = str(error)
            record['placement_obstacle_count'] = len(shape_bounds)
            for index, part in enumerate(shape_bounds):
                obstacles.append({'path': record['path'], 'part': index, 'bounds': part,
                                  'representation': record['geometry_representation'],
                                  'collision_enabled': record['collision_enabled']})
        return obstacles, records

    @overlay_edit
    def _cube(self, path, position, scale, color, collision=False):
        cube = self.UsdGeom.Cube.Define(self.stage, path); cube.CreateSizeAttr(1.)
        cube.CreateDisplayColorAttr([self.Gf.Vec3f(*color)])
        transform = self.UsdGeom.Xformable(cube.GetPrim())
        translate = transform.AddTranslateOp(); translate.Set(self.Gf.Vec3d(*position))
        transform.AddScaleOp().Set(self.Gf.Vec3d(*scale))
        if collision:
            self.UsdPhysics.CollisionAPI.Apply(cube.GetPrim()).CreateCollisionEnabledAttr(True)
        return cube, translate

    @overlay_edit
    def _build_cabinet(self, placement):
        x, y, z = placement['position']; depth, width, height = placement['dimensions_m']; ident = placement['id']
        base = self.base+'/'+usd_device_identifier(ident)
        front = -1 if ident.endswith('lab') else 1
        # Actual colliding cabinet frame; the map reserves its full envelope.
        self._cube(base+'/Back', [x-front*(depth/2-.02), y, z], [.04, width, height], [.12, .30, .34], True)
        for side in (-1, 1):
            self._cube(base+'/Side_'+str(side).replace('-', 'm'), [x, y+side*(width/2-.02), z],
                       [depth, .04, height], [.12, .30, .34], True)
        self._cube(base+'/Top', [x, y, height-.02], [depth, width, .04], [.12, .30, .34], True)
        self._cube(base+'/Bottom', [x, y, .06], [depth, width, .04], [.12, .30, .34], True)
        door_origin = [x+front*(depth/2-.02), y-width*.25, z]
        door, operation = self._cube(base+'/SlidingDoor', door_origin, [.02, width*.46, height*.92], [.63, .81, .80], True)
        self.UsdPhysics.RigidBodyAPI.Apply(door.GetPrim()).CreateKinematicEnabledAttr(True)
        self.doors[ident] = (operation, door_origin, width*.50)
        columns = 1 if placement['capacity'] == 2 else 2
        rows = placement['capacity']//columns
        slots = []
        for index in range(placement['capacity']):
            column, row = index % columns, index//columns
            slots.append([x+front*(depth/2-.09), y+(column-(columns-1)/2)*width*.43, .20+row*(height-.32)/rows])
            shelf = list(slots[-1]); shelf[2] -= .08
            self._cube(base+'/Slot_'+str(index), shelf, [depth*.8, width*.85/columns, .025], [.08, .13, .15], True)
        self.package_positions[ident] = slots

    @overlay_edit
    def _build_light(self, room, position):
        light = self.UsdLux.RectLight.Define(self.stage, self.base+'/Light_'+room)
        light.CreateWidthAttr(.8); light.CreateHeightAttr(.8)
        light.CreateColorAttr(self.Gf.Vec3f(1., .94, .84))
        self.UsdGeom.Xformable(light.GetPrim()).AddTranslateOp().Set(self.Gf.Vec3d(*position))
        self.lamps[room] = light

    def save_overlay(self):
        if self.layer.dirty:
            if not self.layer.Save():
                raise RuntimeError('smart_scene_overlay_save_failed')
            atomic_json(self.root/'runs/smart-ward-v1/scene/overlay-manifest.json',
                        {'path': str(self.overlay_path), 'sha256': hashlib.sha256(self.overlay_path.read_bytes()).hexdigest(),
                         'wall_time': time.time(), 'official_root_layer_saved': False,
                         'attachment': 'session_layer_sublayer'})

    @overlay_edit
    def set_door(self, ident, fraction):
        operation, origin, span = self.doors[ident]
        # Both endpoints and the moving collider stay inside the map envelope;
        # no swinging obstacle enters the aisle.
        position = list(origin); position[1] += span*float(fraction)
        operation.Set(self.Gf.Vec3d(*position))
        if abs(self.read_door(ident)-fraction) > 1e-5:
            raise RuntimeError('sliding_door_USD_readback_mismatch:'+ident)

    def read_door(self, ident):
        operation, origin, span = self.doors[ident]
        return min(1., max(0., (float(operation.Get()[1])-origin[1])/span))

    @overlay_edit
    def set_light(self, room, percent):
        self.lamps[room].CreateIntensityAttr(float(percent)*50.)

    def read_light(self, room):
        return min(100., max(0., float(self.lamps[room].GetIntensityAttr().Get())/50.))

    @overlay_edit
    def sync_packages(self, state):
        for ident, positions in self.package_positions.items():
            ids = state['devices'][ident]['specimens']
            for index, position in enumerate(positions):
                path = self.base+'/'+usd_device_identifier(ident)+'/Package_'+str(index)
                prim = self.stage.GetPrimAtPath(path)
                if index >= len(ids):
                    if prim:
                        self.UsdGeom.Imageable(prim).MakeInvisible()
                    continue
                if not prim:
                    self._cube(path, position, [.05, .16, .17], [.93, .53, .17])
                    prim = self.stage.GetPrimAtPath(path)
                self.UsdGeom.Imageable(prim).MakeVisible()
                from pxr import Sdf
                prim.CreateAttribute('hospital:specimenId', Sdf.ValueTypeNames.String).Set(ids[index])
        robot_ids = [sid for sid, specimen in state['specimens'].items() if specimen['location'] == 'robot']
        transform = self.UsdGeom.XformCache().GetLocalToWorldTransform(self.body)
        for index in range(2):
            path = self.base+'/RobotPackage_'+str(index)
            prim = self.stage.GetPrimAtPath(path)
            if index >= len(robot_ids):
                if prim:
                    self.UsdGeom.Imageable(prim).MakeInvisible()
                continue
            position = transform.Transform(self.Gf.Vec3d(-.10, -.13+index*.26, .75))
            if not prim:
                self._cube(path, list(position), [.18, .16, .17], [.93, .53, .17])
                prim = self.stage.GetPrimAtPath(path)
            self.UsdGeom.Xformable(prim).GetOrderedXformOps()[0].Set(position)
            self.UsdGeom.Imageable(prim).MakeVisible()
            from pxr import Sdf
            prim.CreateAttribute('hospital:specimenId', Sdf.ValueTypeNames.String).Set(robot_ids[index])

    def _update_physical(self, position, yaw, sim_time, now):
        """Capture only advancing simulation samples; preserve real velocity stamps."""
        if self.physical and sim_time <= self.physical['sim_time']:
            return False
        current = {'x': float(position[0]), 'y': float(position[1]), 'yaw': float(yaw),
                   'wall_time': now, 'sim_time': sim_time, 'available': True,
                   'seq': (self.physical or {}).get('seq', 0)+1}
        prior = self.previous_physical
        velocity = self.last_velocity
        if velocity and (now-velocity['velocity_wall_time'] > 1.0 or
                         sim_time-velocity['velocity_sim_time'] > .11):
            self.stopped_samples = 0
        if prior and sim_time-prior['sim_time'] >= .10-1e-9:
            dt = sim_time-prior['sim_time']
            speed = math.hypot(current['x']-prior['x'], current['y']-prior['y'])/dt
            delta = math.atan2(math.sin(yaw-prior['yaw']), math.cos(yaw-prior['yaw']))
            angular = abs(delta)/dt
            if math.isfinite(speed) and math.isfinite(angular):
                velocity = {'speed_mps': speed, 'angular_speed_rad_s': angular,
                            'velocity_wall_time': now, 'velocity_sim_time': sim_time,
                            'velocity_seq': (velocity or {}).get('velocity_seq', 0)+1}
                self.last_velocity = velocity
                self.stopped_samples = self.stopped_samples+1 if speed < .03 and angular < .03 else 0
            else:
                self.last_velocity = velocity = None
                self.stopped_samples = 0
            self.previous_physical = current.copy()
        elif not prior:
            self.previous_physical = current.copy()
        fresh_velocity = bool(velocity and 0 <= now-velocity['velocity_wall_time'] <= 1.0
                              and 0 <= sim_time-velocity['velocity_sim_time'] <= .11)
        if velocity:
            current.update(velocity)
        current['velocity_available'] = fresh_velocity
        current['stopped_samples'] = self.stopped_samples if fresh_velocity else 0
        self.physical = current
        return True

    def tick(self, sim_time):
        # A paused timeline must not freshen pose or stationary evidence.
        if self.physical and sim_time <= self.physical['sim_time']:
            self.world.tick(sim_time)
            return
        now = time.time()
        transform = self.UsdGeom.XformCache().GetLocalToWorldTransform(self.body)
        position = transform.ExtractTranslation()
        direction = transform.TransformDir(self.Gf.Vec3d(1, 0, 0))
        yaw = math.atan2(direction[1], direction[0])
        self._update_physical(position, yaw, sim_time, now)
        self.world.tick(sim_time)

    def poll_controls(self, sim_time):
        return self.world.poll_controls(sim_time)
