"""Atomic JSON publication only; no backend, model, service or simulator runs."""
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from . import smart_ward


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.directory = self.project / ('atomic-json-test-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.target = self.directory / 'report.json'
        self.previous = {'generation': 'previous', 'text': '原报告'}
        self.target.write_text(json.dumps(self.previous, ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        # Before recursive cleanup, verify this test's resolved absolute target.
        if (self.directory.is_symlink()
                or not self.directory.resolve(strict=True).is_relative_to(self.project.resolve(strict=True))
                or self.directory.parent != self.project
                or not self.directory.name.startswith('atomic-json-test-')):
            raise RuntimeError('refusing_atomic_test_cleanup_outside_project')
        shutil.rmtree(self.directory)

    def pending(self):
        return list(self.directory.glob('.report.json.tmp-*'))

    def test_flushed_fsynced_and_closed_before_replace_and_not_a_scene_json(self):
        payload = {'generation': 'new', 'text': '标本与灯光', 'items': list(range(30))}
        fsync, replace = os.fsync, os.replace
        observed, descriptor = [], []

        def sync(fd):
            descriptor.append(fd)
            temporary = self.pending()
            self.assertEqual(len(temporary), 1)
            self.assertNotIn(temporary[0], list(self.directory.glob('*.json')))
            self.assertEqual(json.loads(temporary[0].read_text(encoding='utf-8')), payload)
            self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), self.previous)
            observed.append('fsync')
            return fsync(fd)

        def publish(source, target):
            self.assertEqual(observed, ['fsync'])
            with self.assertRaises(OSError):
                os.fstat(descriptor[0])  # Text stream must be closed, also on Windows.
            observed.append('replace')
            return replace(source, target)

        with mock.patch.object(smart_ward.os, 'fsync', side_effect=sync), \
             mock.patch.object(smart_ward.os, 'replace', side_effect=publish):
            smart_ward._atomic_json(self.target, payload)
        self.assertEqual(observed, ['fsync', 'replace'])
        self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), payload)
        self.assertEqual(self.pending(), [])

    def test_two_concurrent_barrier_writers_publish_only_complete_owned_documents(self):
        payloads = {'writer-a': {'writer': 'a', 'text': 'A' * 200000},
                    'writer-b': {'writer': 'b', 'text': '病区' * 100000}}
        prepared = threading.Event()
        installed = {name: threading.Event() for name in payloads}
        release = {name: threading.Event() for name in payloads}
        barrier, lock = threading.Barrier(2, timeout=5), threading.Lock()
        temporaries, failures = [], []
        replace = os.replace

        def publish(source, target):
            name = threading.current_thread().name
            with lock:
                temporaries.append(Path(source))
                if len(temporaries) == 2:
                    prepared.set()
            barrier.wait()
            if not release[name].wait(5):
                raise TimeoutError('test_publish_release_missing')
            replace(source, target)
            installed[name].set()

        def writer(name):
            try:
                smart_ward._atomic_json(self.target, payloads[name])
            except BaseException as error:
                with lock:
                    failures.append(error)

        workers = [threading.Thread(name=name, target=writer, args=(name,)) for name in payloads]
        with mock.patch.object(smart_ward.os, 'replace', side_effect=publish):
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(prepared.wait(5), failures)
                self.assertEqual(len(set(temporaries)), 2)
                self.assertTrue(all(not path.name.endswith('.json') for path in temporaries))
                self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), self.previous)
                release['writer-a'].set()
                self.assertTrue(installed['writer-a'].wait(5), failures)
                self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), payloads['writer-a'])
                release['writer-b'].set()
                self.assertTrue(installed['writer-b'].wait(5), failures)
            finally:
                for event in release.values():
                    event.set()
                for worker in workers:
                    worker.join(6)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), payloads['writer-b'])
        self.assertEqual(self.pending(), [])

    def test_replace_failure_retains_old_report_and_cleans_only_current_temporary(self):
        original = self.target.read_bytes()
        legacy = self.directory / 'report.json.tmp'
        legacy.write_bytes(b'preserve previous failure evidence')
        unrelated = self.directory / '.report.json.tmp-other-writer'
        unrelated.write_bytes(b'other writer owns this')
        failure = OSError('injected replace failure')
        with mock.patch.object(smart_ward.os, 'replace', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                smart_ward._atomic_json(self.target, {'generation': 'new'})
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(legacy.read_bytes(), b'preserve previous failure evidence')
        self.assertEqual(unrelated.read_bytes(), b'other writer owns this')
        self.assertEqual(self.pending(), [unrelated])

    def test_fsync_failure_cannot_publish_partial_or_delete_old_report(self):
        original = self.target.read_bytes()
        failure = OSError('injected fsync failure')
        with mock.patch.object(smart_ward.os, 'fsync', side_effect=failure), \
             mock.patch.object(smart_ward.os, 'replace') as replace:
            with self.assertRaises(OSError) as caught:
                smart_ward._atomic_json(self.target, {'generation': 'new'})
        self.assertIs(caught.exception, failure)
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.pending(), [])

    def test_partial_write_or_flush_failure_does_not_publish(self):
        original_open = Path.open
        original = self.target.read_bytes()
        for stage in ('write', 'flush'):
            failure = OSError('injected ' + stage + ' failure')

            class BrokenStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def fileno(self):
                    return self.stream.fileno()

                def write(self, value):
                    if stage == 'write':
                        self.stream.write(value[:4])
                        raise failure
                    return self.stream.write(value)

                def flush(self):
                    if stage == 'flush':
                        raise failure
                    self.stream.flush()

            def open_file(path, mode='r', *args, **kwargs):
                stream = original_open(path, mode, *args, **kwargs)
                return BrokenStream(stream) if mode == 'x' and path.name.startswith('.report.json.tmp-') else stream

            with self.subTest(stage=stage), mock.patch.object(Path, 'open', new=open_file), \
                 mock.patch.object(smart_ward.os, 'replace') as replace:
                with self.assertRaises(OSError) as caught:
                    smart_ward._atomic_json(self.target, {'generation': 'new'})
            self.assertIs(caught.exception, failure)
            replace.assert_not_called()
            self.assertEqual(self.target.read_bytes(), original)
            self.assertEqual(self.pending(), [])

    def test_exclusive_name_collision_never_unlinks_preexisting_file(self):
        temporary = self.directory / '.report.json.tmp-fixed'
        temporary.write_bytes(b'foreign existing file')
        original = self.target.read_bytes()
        with mock.patch.object(smart_ward.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')):
            with self.assertRaises(FileExistsError):
                smart_ward._atomic_json(self.target, {'generation': 'new'})
        self.assertEqual(temporary.read_bytes(), b'foreign existing file')
        self.assertEqual(self.target.read_bytes(), original)

    def test_replaced_foreign_temporary_inode_is_not_removed_during_failure_cleanup(self):
        foreign = self.directory / 'foreign-owned.tmp'
        foreign.write_bytes(b'foreign owner must retain both links')
        original = self.target.read_bytes()
        failure = OSError('injected replacement race')

        def interrupted_publish(source, target):
            Path(source).unlink()
            os.link(foreign, source)
            raise failure

        with mock.patch.object(smart_ward.os, 'replace', side_effect=interrupted_publish):
            with self.assertRaises(OSError) as caught:
                smart_ward._atomic_json(self.target, {'generation': 'new'})
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0].read_bytes(), foreign.read_bytes())
        self.assertEqual(self.target.read_bytes(), original)

    def test_cleanup_failure_does_not_hide_primary_replace_error(self):
        original_unlink = Path.unlink
        primary = OSError('primary replace error')
        original = self.target.read_bytes()

        def denied_unlink(path, *args, **kwargs):
            if path.name.startswith('.report.json.tmp-'):
                raise PermissionError('secondary cleanup error')
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(smart_ward.os, 'replace', side_effect=primary), \
             mock.patch.object(Path, 'unlink', new=denied_unlink):
            with self.assertRaises(OSError) as caught:
                smart_ward._atomic_json(self.target, {'generation': 'new'})
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(len(self.pending()), 1)  # Retained, never reported as success.

    def test_serialization_failure_creates_no_temporary_and_preserves_old_bytes(self):
        original = self.target.read_bytes()
        with self.assertRaises(TypeError):
            smart_ward._atomic_json(self.target, {'bad': object()})
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.pending(), [])

    def test_new_parent_and_missing_target_are_supported(self):
        target = self.directory / 'new-parent' / 'command.json'
        smart_ward._atomic_json(target, {'kind': 'handoff', 'operation_id': 'actual-operation'})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')),
                         {'kind': 'handoff', 'operation_id': 'actual-operation'})
        self.assertEqual(list(target.parent.glob('*.tmp*')), [])


if __name__ == '__main__':
    unittest.main()
