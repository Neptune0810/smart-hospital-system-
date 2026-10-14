"""Pure synthetic business/Core input tests; no model, SSH, ROS or real ACK."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path
import unittest
from contextlib import closing
from unittest.mock import patch

from . import smart_ward as ward
from . import test_smart_ward as fixtures


class FinalizeNotifications193Tests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(ward.time, 'time', return_value=1000.)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.fixture = fixtures.SmartWardTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.engine = self.fixture.engine
        self.fixture.start(); self.fixture.low_light()
        self.fixture.devices['hospital-sensor-301']['relative_illumination'] = 76
        for _ in range(3): self.fixture.observe()
        self.assertTrue(self.engine.verify_services()['ready_to_finalize'])

    def bodies(self):
        return [json.loads(row[0]) for row in self.engine.db.execute(
            'SELECT body FROM ward_notifications WHERE run_id=?', (self.engine.s['run_id'],))]

    def put(self, body):
        self.engine.db.execute('INSERT OR REPLACE INTO ward_notifications VALUES (?,?,?)',
            (body['notification_id'], body['run_id'], json.dumps(body)))
        self.engine.db.commit()

    def core(self, items, *, seq=None, origin='system', trust='trusted', session=None, lane=b'main'):
        database = self.fixture.root/'agent/agh-home-mhs/data/sessions.db'
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as core, core:
            core.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER,session_key TEXT,lane BLOB,type TEXT,origin TEXT,trust TEXT,data TEXT)')
            seq = seq if seq is not None else core.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events').fetchone()[0]
            core.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)', (seq,
                session or self.engine.s['controller_session'], lane, 'inbox', origin, trust,
                json.dumps({'items': items})))
        return database

    def seal(self):
        return self.engine.finalize_episode(self.engine.s['run_id'], self.engine.s['controller_session'])

    def assert_open(self):
        self.assertFalse(self.engine.s['mhs_budget'].get('end_wall'))
        self.assertFalse(self.engine.s['smart_ward'].get('episode_reports'))

    def test_pending_and_claimed_before_SDK_ACK_both_hold_budget_open(self):
        for status in ('pending', 'leased'):
            body = self.bodies()[0]; body['status'] = status; self.put(body)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'notifications_pending'):
                self.seal()
            self.assert_open()

    def test_delivered_but_still_queued_defers_seal_then_consumed_seals_once(self):
        self.fixture.consume_notifications(); body = self.bodies()[0]
        self.core([{'commandId': body['notification_id'], 'itemId': 'queued', 'kind': 'follow_up', 'target': 'next-turn'}])
        with self.assertRaisesRegex(ValueError, 'notifications_pending'): self.seal()
        self.assert_open()
        self.core([])
        first = self.seal(); path = self.engine.data_dir/first['report_ref']['path']; raw = path.read_bytes()
        second = self.seal()
        self.assertEqual(first, second); self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), first['report_ref']['sha256'])

    def test_already_sealed_receipt_is_immutable_even_if_later_input_exists(self):
        self.fixture.consume_notifications(); first = self.seal()
        self.core([{'commandId': self.bodies()[0]['notification_id'], 'itemId': 'later'}])
        self.assertEqual(self.seal(), first)

    def test_no_current_notification_skips_Core_database_dependency(self):
        self.engine.db.execute('DELETE FROM ward_notifications'); self.engine.db.commit()
        with patch.object(ward.sqlite3, 'connect', side_effect=AssertionError('Core read forbidden')):
            self.engine._episode_notification_barrier()

    def test_missing_Core_database_for_delivered_input_is_unknown_not_consumed(self):
        self.fixture.consume_notifications()
        (self.fixture.root/'agent/agh-home-mhs/data/sessions.db').unlink()
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.assert_open()

    def test_latest_untrusted_inbox_or_old_inbox_cannot_prove_consumption(self):
        self.fixture.consume_notifications()
        self.core([], origin='principal')
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.core([], trust='untrusted')
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.assert_open()

    def test_other_session_or_lane_empty_inbox_does_not_clear_own_queued_input(self):
        self.fixture.consume_notifications(); body = self.bodies()[0]
        self.core([{'commandId': body['notification_id'], 'itemId': 'q'}])
        self.core([], session='other-session'); self.core([], lane=b'other')
        with self.assertRaisesRegex(ValueError, 'notifications_pending'): self.seal()

    def test_inbox_older_than_SDK_acceptance_is_unknown(self):
        self.fixture.consume_notifications(); body = self.bodies()[0]
        body['details']['queue_seq'] = 500; self.put(body)
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.assert_open()

    def test_historical_run_session_episode_notifications_do_not_borrow_budget(self):
        self.fixture.consume_notifications(); source = self.bodies()[0]
        ward_state = self.engine.s['smart_ward']
        ward_state['incidents']['old-incident'] = {'incident_id': 'old-incident', 'episode': 0, 'status': 'resolved', 'kind': 'low_light'}
        for dimension in ('run', 'session', 'episode'):
            body = copy.deepcopy(source); body.update(notification_id='old-'+dimension, status='pending')
            if dimension == 'run': body['run_id'] = 'old-run'
            elif dimension == 'session': body['session_id'] = 'other-session'
            else: body['event_ids'] = ['old-incident']
            self.put(body)
        self.core([{'commandId': 'old-episode', 'itemId': 'old-q'}])
        self.engine.verify_services()
        self.assertTrue(self.seal()['ok'])

    def test_many_historical_notifications_do_not_hit_current_episode_cap(self):
        self.fixture.consume_notifications(); source = self.bodies()[0]
        self.engine.s['smart_ward']['incidents']['old'] = {'incident_id': 'old', 'episode': 0, 'status': 'resolved', 'kind': 'low_light'}
        for index in range(650):
            body = copy.deepcopy(source); body.update(notification_id='history-'+str(index), event_ids=['old'], status='pending')
            self.put(body)
        self.engine.verify_services(); self.assertTrue(self.seal()['ok'])

    def test_too_many_current_notifications_fail_closed_and_JSON1_is_supported(self):
        self.assertEqual(self.engine.db.execute("SELECT value FROM json_each('[7]')").fetchone()[0], 7)
        source = self.bodies()[0]
        with self.engine._atomic():
            for index in range(601):
                body = copy.deepcopy(source); body['notification_id'] = 'current-'+str(index)
                self.engine.db.execute('INSERT INTO ward_notifications VALUES (?,?,?)',
                    (body['notification_id'], body['run_id'], json.dumps(body)))
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.assert_open()

    def test_missing_delivery_provenance_or_malformed_latest_state_is_unknown(self):
        self.fixture.consume_notifications(); body = self.bodies()[0]
        body['details'] = {}; self.put(body)
        with self.assertRaisesRegex(ValueError, 'notifications_state_unknown'): self.seal()
        self.assert_open()

    def test_real177_trusted_inbox782_blocks_but_consumed812_does_not(self):
        project = fixtures.ROOT
        source = project/'.cache/recovery177-usage108/session.jsonl'
        if not source.exists(): self.skipTest('closed177 raw contract fixture not supplied; not real acceptance')
        raw = source.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), 'f7ffe728aa9a23ecc16c70581faae34df555cba5740dbdb8f59c5590261cd360')
        rows = {row['seq']: row for row in map(json.loads, raw.splitlines())}
        queued, consumed = rows[782], rows[812]
        self.assertEqual(queued['origin'], 'system'); self.assertEqual(queued['trust'], 'trusted')
        self.assertEqual(queued['lane'], 'sqlite_blob_hex:6d61696e')
        notification_id = queued['data']['items'][0]['commandId']
        self.fixture.consume_notifications(); body = self.bodies()[0]
        self.engine.db.execute('DELETE FROM ward_notifications'); self.engine.db.commit()
        body['notification_id'] = notification_id; body['details']['queue_seq'] = 782; self.put(body)
        self.core(queued['data']['items'], seq=782, origin=queued['origin'], trust=queued['trust'])
        with self.assertRaisesRegex(ValueError, 'notifications_pending'): self.seal()
        self.assert_open()
        self.core(consumed['data']['items'], seq=812, origin=consumed['origin'], trust=consumed['trust'])
        self.assertTrue(self.seal()['ok'])
        # This reuses authentic closed input envelopes in a synthetic business
        # fixture. The original failed177 report/outcome is never changed.


if __name__ == '__main__': unittest.main(verbosity=2)
