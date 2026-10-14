"""Deterministic fixed-device streaming regressions; no SDK/server/Isaac launch.

Execute the production telemetry method in isolation to avoid bridge import's
runtime registration/capture side effects. The fake Source uses the pinned SDK's
on/rate/send contract; it does not alter the official checker or SDK.
"""
import ast
import copy
import math
import unittest
from pathlib import Path
from types import SimpleNamespace


class EndTelemetry(Exception):
    pass


class Clock:
    def __init__(self):
        self.now = 0.
        self.base = 1000.

    def advance(self, delay):
        self.now = round(self.now+delay, 10)

    def time(self):
        return self.base+self.now

    def monotonic(self):
        return self.now


class Reading:
    def __init__(self, clock, settings=None, refused=0):
        self.clock = clock
        self.settings = settings or (lambda now: (True, 1.))
        self.last = -math.inf
        self.refused = refused
        self.messages = []
        self.attempts = []

    def wants(self):
        on, hz = self.settings(self.clock.now)
        return on and self.clock.now-self.last >= .9/hz

    def send(self, data, *, t):
        self.attempts.append((self.clock.now, t))
        if not self.wants():
            return False
        if self.refused:
            self.refused -= 1
            return False
        self.last = self.clock.now
        self.messages.append(dict(at=self.clock.now, t=t, data=copy.deepcopy(data),
                                  hz=self.settings(self.clock.now)[1]))
        return True


class Device:
    def __init__(self, clock):
        self.clock = clock
        self.problem = None
        self.states = []

    def update(self, **values):
        self.states.append(dict(at=self.clock.now, **values))


def production_telemetry(namespace):
    tree = ast.parse((Path(__file__).with_name('bridge.py')).read_text(encoding='utf-8-sig'))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'WardDeviceBridge')
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'telemetry')
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<WardDeviceBridge.telemetry>', 'exec'), namespace)
    return namespace['telemetry']


def exercise(*, kind='sensor', phase=0., period=1., duration=12., latency=0.,
             state_filter=None, settings=None, refused=0):
    clock = Clock()
    ident = 'hospital-'+kind+'-301' if kind != 'cabinet' else 'hospital-cabinet-b'
    bridge = SimpleNamespace(ident=ident, kind=kind, room_id=None if kind == 'cabinet' else 'room301',
                             last_sample=None, latest=None, dev=Device(clock),
                             reading=Reading(clock, settings, refused))
    observed = []

    def request(path):
        if path != '/api/state':
            raise AssertionError('unexpected API path: '+path)
        index = math.floor((clock.now-phase)/period+1e-9)
        wall = clock.base+phase+index*period
        device = dict(fresh=True, available=True, seq=index+100, wall_time=wall,
                      relative_illumination=70., brightness_percent=60., stuck=False,
                      specimens=['specimen-1'], capacity=2, door_open_fraction=0.)
        if state_filter:
            device = state_filter(clock, device)
        state = dict(run_id='run-1', smart_ward=dict(devices={ident: device}, incidents=[]))
        observed.append(copy.deepcopy(state))
        return state

    async def to_thread(function, *arguments):
        result = function(*arguments)
        clock.advance(latency)
        return result

    async def sleep(delay):
        if delay != .1:
            raise AssertionError('fixed-device poll must be 0.1 seconds')
        clock.advance(delay)
        if clock.now >= duration:
            raise EndTelemetry()

    namespace = dict(asyncio=SimpleNamespace(to_thread=to_thread, sleep=sleep),
                     time=clock, math=math, request=request)
    async def sample():
        return await to_thread(request, '/api/state')
    namespace['state_sampler'] = SimpleNamespace(get=sample)
    method = production_telemetry(namespace)
    # All fake awaits complete synchronously. Drive the coroutine directly;
    # constructing a Windows asyncio loop would open a sandboxed socketpair.
    coroutine = method(bridge)
    try:
        coroutine.send(None)
    except EndTelemetry:
        pass
    else:
        coroutine.close()
        raise AssertionError('unexpected real asynchronous suspension in deterministic test')
    return bridge, clock, observed


class WardStreamingTests(unittest.TestCase):
    def assert_truthful_stream(self, bridge, clock, observed):
        messages = bridge.reading.messages
        self.assertTrue(messages)
        valid_wall = {device['wall_time'] for state in observed
                      for device in state['smart_ward']['devices'].values()}
        self.assertTrue(all(message['t'] in valid_wall for message in messages))
        self.assertEqual(len(messages), len({message['t'] for message in messages}))
        self.assertTrue(all(0 <= clock.base+message['at']-message['t'] <= .3500001 for message in messages))
        self.assertTrue(all(second['at']-first['at'] >= 1.-1e-9
                            for first, second in zip(messages, messages[1:])))

    def test_sample_phase_and_api_latency_keep_genuine_fresh_one_hz(self):
        for kind in ['cabinet', 'light', 'sensor']:
            for phase in [index*.05 for index in range(20)]:
                for period in [1., 1.03]:
                    with self.subTest(kind=kind, phase=phase, period=period):
                        bridge, clock, observed = exercise(kind=kind, phase=phase, period=period, latency=.03)
                        self.assert_truthful_stream(bridge, clock, observed)
                        self.assertGreaterEqual(len(bridge.reading.messages), 8)

    def test_checker_congestion_age_bound_after_drain_window(self):
        # Official check_mos.congestion: 2 s unread, then allow 1 s for
        # socket-buffer drainage; every later arrival must have age <= 0.5 s.
        paused, released = 2.7, 4.7
        bridge, clock, observed = exercise(phase=.35)
        frames = [(released if paused <= message['at'] <= released else message['at'], message['t'])
                  for message in bridge.reading.messages]
        late = [(at, wall) for at, wall in frames if released+1 < at <= released+2]
        self.assertTrue(late)
        self.assertTrue(all(clock.base+at-wall <= .5 for at, wall in late))
        # A 1 s bridge poll at this phase would resend captures 0.65 s old.
        self.assertGreater(1.-.35, .5)
        self.assert_truthful_stream(bridge, clock, observed)

    def test_configure_off_and_low_rate_do_not_consume_unsent_sample(self):
        def settings(now):
            return (not 2 <= now < 5, .5 if 5 <= now < 10 else 1.)
        bridge, clock, observed = exercise(settings=settings, duration=14.)
        messages = bridge.reading.messages
        self.assertFalse(any(2 <= message['at'] < 5 for message in messages))
        self.assertTrue(any(5 <= message['at'] < 7 for message in messages))
        low = [message for message in messages if message['hz'] == .5]
        self.assertGreaterEqual(len(low), 2)
        self.assertTrue(all(second['at']-first['at'] >= 1.8-1e-9 for first, second in zip(low, low[1:])))
        self.assert_truthful_stream(bridge, clock, observed)

    def test_simulation_pause_produces_no_duplicate_or_restamped_capture(self):
        def paused(clock, device):
            if 2 <= clock.now < 7:
                device.update(seq=102, wall_time=clock.base+2, fresh=clock.now-2 <= 3)
            return device
        bridge, clock, observed = exercise(state_filter=paused)
        self.assertFalse(any(2.35 < message['at'] < 7 for message in bridge.reading.messages))
        self.assertTrue(any(5.1 < state['at'] < 7 and not state['fresh'] and not state['available']
                            for state in bridge.dev.states))
        self.assertTrue(any(message['at'] >= 7 for message in bridge.reading.messages))
        self.assert_truthful_stream(bridge, clock, observed)

    def test_missing_invalid_future_and_old_timestamps_are_never_replaced(self):
        for wall in [None, 0., float('nan'), float('inf'), True, 'invalid', 2000., 999.4]:
            with self.subTest(wall=wall):
                bridge, _, _ = exercise(duration=.2, state_filter=lambda clock, device: dict(device, wall_time=wall))
                self.assertEqual(bridge.reading.messages, [])
        for seq in [None, 0, True, '1', 1.2]:
            with self.subTest(seq=seq):
                bridge, _, _ = exercise(duration=.2, state_filter=lambda clock, device: dict(device, seq=seq))
                self.assertEqual(bridge.reading.messages, [])

    def test_refused_send_keeps_current_sample_for_next_poll(self):
        bridge, clock, observed = exercise(refused=1, duration=.3)
        self.assertEqual(len(bridge.reading.attempts), 2)
        self.assertEqual(bridge.reading.messages[0]['at'], .1)
        self.assertEqual(bridge.reading.messages[0]['t'], clock.base)
        self.assert_truthful_stream(bridge, clock, observed)

    def test_api_error_marks_unknown_and_recovers_only_with_new_sample(self):
        def unavailable(clock, device):
            if 2 <= clock.now < 4:
                raise OSError('backend unavailable')
            return device
        bridge, clock, observed = exercise(state_filter=unavailable)
        self.assertTrue(any(2 <= state['at'] < 4 and state.get('fresh') is False
                            and state.get('available') is False for state in bridge.dev.states))
        self.assertFalse(any(2 <= message['at'] < 4 for message in bridge.reading.messages))
        self.assertTrue(any(message['at'] >= 4 for message in bridge.reading.messages))
        self.assert_truthful_stream(bridge, clock, observed)


if __name__ == '__main__':
    unittest.main()
