"""Observer/queue unit checks only; not ROS timing or navigation evidence."""
import json
from pathlib import Path
import tempfile
import types
import unittest

from .runtime_diagnostics import (RuntimeDiagnostics, configure, record,
                                  trace_span, actual_freshness, snapshot, status)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(dir=root/'.cache')
        self.root = Path(self.temporary.name)
        self.diagnostics = []

    def tearDown(self):
        for diagnostics in self.diagnostics:
            diagnostics.close(wait_seconds=1)
        self.temporary.cleanup()

    def diagnostic(self, **kwargs):
        diagnostics = RuntimeDiagnostics(self.root/'runs/smart-ward-v1',
            lambda: 'run-a', start=False, **kwargs)
        self.diagnostics.append(diagnostics)
        return diagnostics

    def test_schema_run_identity_and_file_are_from_capture(self):
        run = ['old-run']
        diagnostics = RuntimeDiagnostics(self.root/'runs', lambda: run[0], start=False)
        self.diagnostics.append(diagnostics)
        self.assertTrue(diagnostics.record('guard_trip', reason='telemetry_stale'))
        run[0] = 'new-run'
        row = diagnostics.queue.get_nowait()
        diagnostics._write(row)
        self.assertEqual(row['run_id'], 'old-run')
        self.assertEqual(row['schema'], 'hospital-runtime-diagnostics-v1')
        self.assertIsInstance(row['monotonic_time'], float)
        self.assertIsInstance(row['thread_id'], int)
        file = next((self.root/'runs/old-run').glob('*.jsonl'))
        self.assertEqual(json.loads(file.read_text())['fields']['reason'], 'telemetry_stale')
        self.assertFalse((self.root/'runs/new-run').exists())

    def test_full_queue_is_nonblocking_and_loss_is_visible(self):
        diagnostics = self.diagnostic(queue_size=1)
        self.assertTrue(diagnostics.record('first'))
        self.assertFalse(diagnostics.record('guard_trip'))
        self.assertEqual(diagnostics.queue.qsize(), 1)
        self.assertEqual(diagnostics.status()['dropped'], 1)
        self.assertEqual(diagnostics.status()['dropped_exception_events'], 1)
        self.assertEqual(diagnostics.status()['last_drop']['event'], 'guard_trip')
        self.assertFalse(diagnostics.directory.exists())

    def test_writer_error_is_counted_without_propagating(self):
        diagnostics = self.diagnostic()
        blocker = self.root/'file'
        blocker.write_text('not a directory')
        diagnostics.directory = blocker
        diagnostics.record('event')
        diagnostics._write(diagnostics.queue.get_nowait())
        self.assertEqual(diagnostics.status()['write_errors'], 1)
        self.assertEqual(diagnostics.status()['written'], 0)

    def test_file_budget_drops_are_visible_and_no_file_created(self):
        diagnostics = self.diagnostic(max_written_bytes=1)
        diagnostics.record('event')
        diagnostics._write(diagnostics.queue.get_nowait())
        self.assertEqual(diagnostics.status()['file_limit_dropped'], 1)
        self.assertFalse(diagnostics.directory.exists())

    def test_fast_span_no_event_slow_and_error_preserve_exception(self):
        now = [1.]
        diagnostics = self.diagnostic(clock=lambda: now[0])
        engine = types.SimpleNamespace(_runtime_diagnostics=diagnostics)
        with trace_span(engine, 'fast'):
            now[0] += .1
        self.assertTrue(diagnostics.queue.empty())
        with trace_span(engine, 'slow'):
            now[0] += .21
        self.assertEqual(diagnostics.queue.get_nowait()['event'], 'slow_span')
        with self.assertRaisesRegex(ValueError, 'original'):
            with trace_span(engine, 'error'):
                raise ValueError('original')
        self.assertEqual(diagnostics.queue.get_nowait()['event'], 'span_error')

    def test_unconfigured_engine_is_noop_and_does_not_hide_errors(self):
        engine = types.SimpleNamespace()
        self.assertFalse(record(engine, 'event'))
        self.assertEqual(snapshot(engine), {})
        self.assertEqual(status(engine), {'enabled': False})
        with self.assertRaisesRegex(RuntimeError, 'original'):
            with trace_span(engine, 'span'):
                raise RuntimeError('original')

    def engine(self):
        class Guard:
            armed = True
            reason = None
            def trip(self, reason):
                self.armed = False
                self.reason = reason
                return 'original-stop-result'
        engine = types.SimpleNamespace(root=self.root,
            data_dir=self.root/'runs/smart-ward-v1', s={'run_id':'run-a'},
            last_pose_wall=123., guard=Guard(), calls=[])
        physical = dict(wall_time=120., physics_time=11.)
        crowd = dict(wall_time=121., sim_time=12., fresh=False, people=[{},{}])
        def physical_pose():
            engine.calls.append('physical')
            return physical
        def read_crowd():
            engine.calls.append('crowd')
            return crowd
        engine.physical_pose=physical_pose
        engine.routes=types.SimpleNamespace(crowd=read_crowd)
        def fresh():
            return engine.physical_pose() is not None and engine.routes.crowd()['fresh']
        engine.fresh=fresh
        return engine, physical, crowd

    def test_original_fresh_calls_once_and_trip_keeps_original_result(self):
        engine, physical, crowd=self.engine()
        diagnostics=configure(engine,snapshot_getter=lambda:{'pose_wall':engine.last_pose_wall})
        self.diagnostics.append(diagnostics)
        self.assertFalse(engine.fresh())
        checked=actual_freshness(engine)
        self.assertEqual(engine.calls,['physical','crowd'])
        self.assertEqual(checked['physical_pose']['producer_wall_time'],120.)
        self.assertFalse(checked['crowd']['returned_fresh'])
        self.assertEqual(checked['crowd']['people_count'],2)
        self.assertIs(engine.physical_pose(),physical)
        self.assertIs(engine.routes.crowd(),crowd)
        before=list(engine.calls)
        self.assertEqual(engine.guard.trip('telemetry_stale'),'original-stop-result')
        self.assertEqual(engine.calls,before)
        self.assertFalse(engine.guard.armed)

    def test_snapshot_failure_does_not_prevent_original_stop(self):
        engine,_,_=self.engine()
        diagnostics=configure(engine,snapshot_getter=lambda:(_ for _ in ()).throw(RuntimeError('diagnostic failure')))
        self.diagnostics.append(diagnostics)
        self.assertEqual(engine.guard.trip('stop'),'original-stop-result')
        self.assertFalse(engine.guard.armed)
        self.assertGreaterEqual(diagnostics.status()['snapshot_errors'],1)

    def test_original_fresh_exception_is_not_swallowed(self):
        engine,_,_=self.engine()
        def failing():raise ValueError('source-error')
        engine.fresh=failing
        diagnostics=configure(engine)
        self.diagnostics.append(diagnostics)
        with self.assertRaisesRegex(ValueError,'source-error'):engine.fresh()

    def test_summary_contains_loss_status_and_span_counts(self):
        diagnostics=self.diagnostic(queue_size=3)
        diagnostics.note_span('ros.update',.3)
        diagnostics.summary()
        row=diagnostics.queue.get_nowait()
        self.assertEqual(row['event'],'summary')
        self.assertEqual(row['fields']['spans']['ros.update']['calls'],1)
        self.assertIn('dropped',row['fields']['status'])

    def test_default_close_does_not_join_blocked_writer(self):
        diagnostics=self.diagnostic()
        blocking=types.SimpleNamespace(join=lambda **kwargs:self.fail('stop must not join writer'))
        diagnostics.thread=blocking
        diagnostics.close()
        diagnostics.thread=None

    def test_invalid_run_id_cannot_escape_output_directory(self):
        diagnostics=self.diagnostic()
        diagnostics.run_id_getter=lambda:'../../outside'
        self.assertFalse(diagnostics.record('event'))
        self.assertEqual(diagnostics.status()['invalid_run_ids'],1)
        self.assertTrue(diagnostics.queue.empty())


if __name__=='__main__':unittest.main()
