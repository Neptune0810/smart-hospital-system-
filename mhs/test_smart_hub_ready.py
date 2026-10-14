"""Pure/mock startup/deadline checks; no live port, process or device calls."""
import argparse
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import smart_navigation as navigation
from . import smart_disconnect as disconnect
from . import smart_hub_disconnect as hub
from . import smart_hub_ready as ready
from . import test_smart_disconnect as disconnect_tests
from . import test_smart_hub_disconnect as hub_tests
from .test_smart_navigation import complete_immediate

ROOT = Path(__file__).resolve().parents[1]


async def immediate_wait(awaitable, timeout):
    return await awaitable


class HubReadyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = hub_tests.HubDisconnectTests()
        self.fixture.fixture = disconnect_tests.DisconnectTests()

    def driver(self):
        with patch.object(navigation, 'ROOT', self.root):
            driver = ready.HubReadyQualification(argparse.Namespace(output=None, prior_report='unused', wall_seconds=600))
        driver.run_id = 'run'; driver.started_wall = 100.
        driver.deadline, driver.work_end, driver.segment_end = 600., 450., 2400.
        return driver

    def clock_patches(self, clock):
        async def advance(seconds): clock[0] += seconds
        return (patch.object(ready.time, 'monotonic', side_effect=lambda: clock[0]),
                patch.object(ready.asyncio, 'sleep', new=AsyncMock(side_effect=advance)),
                patch.object(ready.asyncio, 'wait_for', new=AsyncMock(side_effect=immediate_wait)))

    def prior(self):
        prior = self.fixture.prior()
        prior['cases'] = [{'case': name, 'status': 'failed' if name == 'hub_exit_guard' else 'NA',
            'pass': False if name == 'hub_exit_guard' else None} for name in navigation.ALL_CASES]
        prior['failure'] = {'case': 'hub_exit_guard', 'error': "ConnectionRefusedError(111, 'Connection refused')"}
        prior['qualification_scope_version'] = hub.QUALIFICATION_SCOPE_VERSION
        prior['source_sha256'].update(ready.SOURCE47_FILES)
        prior['prior_report']['sha256'] = hub.PRIOR_REPORT_SHA256
        return prior

    def test_delayed_listener_performs_only_one_actual_native_hello(self):
        driver = self.driver(); clock = [0.]
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', side_effect=[False, False, True]) as probes, \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock()) as original:
            complete_immediate(driver.connect_hub())
        original.assert_awaited_once_with(driver)
        self.assertEqual(probes.call_count, 3)
        proof = driver.report['hub_readiness_checks'][0]
        self.assertTrue(proof['connected']); self.assertEqual(proof['absolute_deadline_monotonic'], 20.)
        self.assertEqual(proof['native_hello_attempts'], 1); self.assertEqual(clock[0], .5)

    def test_never_ready_exhausts_original20_seconds_without_handshake_or_deadline_reset(self):
        driver = self.driver(); clock = [0.]
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', return_value=False), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock()) as original:
            with self.assertRaisesRegex(TimeoutError, 'original_connect_deadline_exhausted'):
                complete_immediate(driver.connect_hub())
        original.assert_not_called()
        self.assertEqual(clock[0], 20.)
        self.assertFalse(driver.report['hub_readiness_checks'][0]['connected'])
        self.assertEqual(driver.deadline, 600.)

    def test_global_work_deadline_clips_readiness_and_keeps_cleanup150(self):
        driver = self.driver(); clock = [449.6]
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', return_value=False), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock()) as original:
            with self.assertRaisesRegex(TimeoutError, 'qualification_wall_budget_exhausted'):
                complete_immediate(driver.connect_hub())
        original.assert_not_called()
        self.assertEqual(driver.report['hub_readiness_checks'][0]['absolute_deadline_monotonic'], 450.)
        self.assertEqual(driver.deadline-driver.work_end, 150.)

    def test_pre_websocket_refusal_race_retries_connection_only_under_same_deadline(self):
        driver = self.driver(); clock = [0.]; calls = []
        async def connect(owner):
            calls.append(clock[0])
            owner.hub = types.SimpleNamespace()
            if len(calls) == 1: raise ConnectionRefusedError('pre-websocket')
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', return_value=True), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock(side_effect=connect)), \
             patch.object(driver, 'service_exit', new=AsyncMock()) as motion:
            complete_immediate(driver.connect_hub())
        motion.assert_not_called()
        self.assertEqual(calls, [0., .25])
        self.assertEqual(driver.report['hub_readiness_checks'][0]['absolute_deadline_monotonic'], 20.)
        self.assertEqual(driver.report['hub_readiness_checks'][0]['native_hello_attempts'], 2)

    def test_partial_websocket_or_reader_refusal_is_never_retried(self):
        for field in ('ws', 'reader'):
            with self.subTest(field=field):
                driver = self.driver(); clock = [0.]
                async def partial(owner):
                    owner.hub = types.SimpleNamespace(**{field: object()})
                    raise ConnectionRefusedError('partial')
                a, b, c = self.clock_patches(clock)
                with a, b, c, patch.object(ready, 'port_listening', return_value=True), \
                     patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock(side_effect=partial)) as original:
                    with self.assertRaises(ConnectionRefusedError): complete_immediate(driver.connect_hub())
                self.assertEqual(original.await_count, 1); self.assertEqual(clock[0], 0.)

    def test_protocol_error_is_not_treated_as_listener_startup(self):
        driver = self.driver(); clock = [0.]
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', return_value=True), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock(side_effect=RuntimeError('hello rejected'))) as original:
            with self.assertRaisesRegex(RuntimeError, 'hello rejected'): complete_immediate(driver.connect_hub())
        self.assertEqual(original.await_count, 1)

    def test_late_native_hello_does_not_pass(self):
        driver = self.driver(); clock = [0.]
        async def late(owner): clock[0] = 20.1
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', return_value=True), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock(side_effect=late)):
            with self.assertRaisesRegex(TimeoutError, 'native_hello_after_original_connect_deadline'):
                complete_immediate(driver.connect_hub())
        self.assertFalse(driver.report['hub_readiness_checks'][0]['connected'])

    def test_fixed_prior47_closure_hash_stop_and_failure_contract(self):
        path = self.root/ready.PRIOR_REPORT_RELATIVE; path.parent.mkdir(parents=True)
        raw = json.dumps(self.prior()).encode(); path.write_bytes(raw)
        with patch.object(ready, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
            proof = ready.validate_prior_report(self.root, path, 121.)
            self.assertEqual(proof['counts'], {'passed': 0, 'failed': 1, 'NA': 8})
            with self.assertRaisesRegex(ValueError, 'closed_before_new_qualification'):
                ready.validate_prior_report(self.root, path, 119.)
            path.write_bytes(raw+b' ')
            with self.assertRaisesRegex(ValueError, 'sha256_mismatch'):
                ready.validate_prior_report(self.root, path, 121.)

    def test_original_motion_exit_cleanup_and_stop_contract_stay_inherited(self):
        driver = self.driver()
        self.assertIs(ready.HubReadyQualification.execute, hub.HubDisconnectQualification.execute)
        self.assertIs(ready.HubReadyQualification.service_exit, navigation.Qualification.service_exit)
        self.assertIs(ready.HubReadyQualification.moving, navigation.Qualification.moving)
        self.assertIs(ready.HubReadyQualification.cleanup, navigation.Qualification.cleanup)
        self.assertIs(ready.HubReadyQualification.api, disconnect.DisconnectQualification.api)
        self.assertEqual((driver.args.wall_seconds, driver.args.leg_seconds), (600, 1200))
        self.assertEqual(driver.report['limits']['moving_observation_wall_seconds'], 90)
        self.assertEqual(driver.report['requested_cases'], ['hub_exit_guard'])

    def test_execution_does_not_repeat_actual_motion_during_delayed_reconnect(self):
        driver = self.driver(); driver.claimed = True; clock = [0.]
        driver.preflight = AsyncMock(); driver.begin_segment = AsyncMock(); driver.end_segment = AsyncMock()
        source = self.root/'source'; source.write_bytes(b'frozen')
        driver.report['source_sha256'] = {'source': disconnect.digest(source)}
        async def exit_once(service, station):
            await driver.connect_hub()  # Only the restore-side connection waits.
            return {'service': service, 'station': station, 'actual_motion': True}
        driver.service_exit = AsyncMock(side_effect=exit_once)
        async def cleanup():
            driver.report.update({key: value for key, value in self.fixture.proof().items() if key not in {'cases', 'status'}})
            driver.submitted_stop = self.fixture.fixture.submitted()
        driver.cleanup = AsyncMock(side_effect=cleanup)
        a, b, c = self.clock_patches(clock)
        with a, b, c, patch.object(ready, 'port_listening', side_effect=[True, False, False, True]), \
             patch.object(navigation.Qualification, 'connect_hub', new=AsyncMock()) as native:
            self.assertEqual(complete_immediate(driver.execute()), 2)
        driver.service_exit.assert_awaited_once_with('daemon', 'station_b')
        self.assertEqual(native.await_count, 2)  # Initial + restored, not new motion.
        self.assertEqual(len(driver.report['hub_readiness_checks']), 2)
        self.assertTrue(driver.report['requested_subset_passed']); self.assertEqual(driver.report['status'], 'partial_NA')


if __name__ == '__main__':
    unittest.main()
