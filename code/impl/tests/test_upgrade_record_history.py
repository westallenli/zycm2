"""P01-02: event references, independent clocks and persistent history."""
import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path
from panda_brain.record_contracts import ObjectReference, RecordTimes, ObjectRevision
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.auth import GeneralEntryAuth

class RecordHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('same-scenario', event_id='fire-a')

    def revision(self, version=1, **changes):
        values = dict(reference=ObjectReference('fire-a', 'report', 'report-1', version),
            times=RecordTimes(occurred_at='2026-09-24T08:00:00+08:00', recorded_at=f'2026-09-24T10:0{version}:00+08:00'),
            source='manual-report', data_kind='reported', actor_id='operator-a',
            request_id=f'request-{version}', content_json='{"quantity": 3}')
        values.update(changes)
        return ObjectRevision(**values)

    def test_late_record_not_visible_before_recording(self):
        first = self.ws.record_object_revision(self.revision(), expected_version=0)
        self.assertIsNone(self.ws.object_revision_at('fire-a', 'report', 'report-1', '2026-09-24T09:00:00+08:00'))
        self.assertEqual(first, self.ws.object_revision_at('fire-a', 'report', 'report-1', '2026-09-24T10:01:00+08:00'))
        self.assertEqual('', first.times.collected_at)
        self.assertEqual('', first.times.received_at)

    def test_correction_preserves_old_version_and_cutoff(self):
        first = self.ws.record_object_revision(self.revision(), expected_version=0)
        second = self.ws.record_object_revision(self.revision(2, content_json='{"quantity": 2}'), expected_version=1)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first, self.ws.object_revision_at('fire-a', 'report', 'report-1', '2026-09-24T10:01:30+08:00'))
        self.assertEqual(second, self.ws.object_revision_at('fire-a', 'report', 'report-1', '2026-09-24T02:02:00+00:00'))
        self.assertEqual(2, len(self.ws.object_revisions))
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_retry_is_idempotent_but_conflicting_retry_is_rejected(self):
        revision = self.revision()
        self.ws.record_object_revision(revision, expected_version=0)
        count = self.ws.version
        self.assertEqual(revision, self.ws.record_object_revision(revision, expected_version=0))
        with self.assertRaises(ValueError):
            self.ws.record_object_revision(replace(revision, content_json='{}'), expected_version=0)
        self.assertEqual(count, self.ws.version)

    def test_stale_or_skipped_version_rejected_without_write(self):
        self.ws.record_object_revision(self.revision(), expected_version=0)
        count = self.ws.version
        for revision, expected in ((self.revision(2), 0), (self.revision(3), 1)):
            with self.assertRaises(ValueError):
                self.ws.record_object_revision(revision, expected_version=expected)
        self.assertEqual(count, self.ws.version)

    def test_cross_event_and_unbound_workspace_rejected(self):
        before = self.ws.version
        wrong = self.revision(reference=ObjectReference('fire-b', 'report', 'report-1', 1))
        with self.assertRaises(ValueError):
            self.ws.record_object_revision(wrong, expected_version=0)
        with self.assertRaises(ValueError):
            ScenarioWorkspace('same-scenario').record_object_revision(self.revision(), expected_version=0)
        with self.assertRaises(ValueError):
            self.ws.object_revision_at('fire-b', 'report', 'report-1', '2026-09-24T12:00:00+08:00')
        self.assertEqual(before, self.ws.version)

    def test_unknown_times_roundtrip_and_authority_not_restored(self):
        revision = self.revision(times=RecordTimes(recorded_at='2026-09-24T10:00:00+08:00'))
        self.ws.record_object_revision(revision, expected_version=0)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'state.db'
            save_workspace(self.ws, db)
            restored = load_workspace(db, general_auth=GeneralEntryAuth())
        self.assertEqual('fire-a', restored.event_id)
        self.assertEqual(revision, restored.object_revisions[revision.id])
        self.assertEqual('', restored.object_revisions[revision.id].times.occurred_at)
        self.assertEqual((), restored.verify_audit_chain())
        with self.assertRaises(PermissionError):
            restored.record_object_revision(self.revision(2), expected_version=1)

    def test_legacy_snapshot_no_invented_event_or_time(self):
        legacy = ScenarioWorkspace('legacy')
        original = legacy.events
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'legacy.db'
            save_workspace(legacy, db)
            import sqlite3
            with sqlite3.connect(db) as connection:
                connection.execute("DELETE FROM meta WHERE key='event_id'")
            connection.close()
            restored = load_workspace(db)
        self.assertEqual('', restored.event_id)
        self.assertEqual({}, restored.object_revisions)
        self.assertEqual([(e.id, e.sequence, e.kind, e.hash, e.prev_hash) for e in original],
                         [(e.id, e.sequence, e.kind, e.hash, e.prev_hash) for e in restored.events])
        self.assertEqual(json.dumps([e.payload for e in original], sort_keys=True),
                         json.dumps([e.payload for e in restored.events], sort_keys=True))
        self.assertEqual((), restored.verify_audit_chain())

    def test_invalid_or_naive_times_rejected(self):
        for value in ('yesterday', '2026-09-24', '2026-09-24T10:00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RecordTimes(received_at=value)

    def test_identity_and_version_must_be_explicit(self):
        for args in (('', 'report', 'r', 1), ('a', '', 'r', 1), ('a', 'report', '', 1), ('a', 'report', 'r', 0), ('a', 'report', 'r', True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ObjectReference(*args)

    def test_recording_time_required_and_correction_not_backdated(self):
        with self.assertRaises(ValueError):
            self.ws.record_object_revision(self.revision(times=RecordTimes()), expected_version=0)
        self.ws.record_object_revision(self.revision(), expected_version=0)
        before = self.ws.version
        with self.assertRaises(ValueError):
            self.ws.record_object_revision(self.revision(2, times=RecordTimes(recorded_at='2026-09-24T09:00:00+08:00')), expected_version=1)
        self.assertEqual(before, self.ws.version)

    def test_simulation_source_preserved_without_verification(self):
        self.ws.record_object_revision(self.revision(data_kind='simulated'), expected_version=0)
        payload = self.ws.events[-1].payload
        self.assertEqual('simulated', payload['revision']['data_kind'])
        self.assertEqual('manual-report', payload['revision']['source'])
        self.assertEqual({}, self.ws.verifications)

    def test_retry_and_new_version_after_restart(self):
        first = self.revision()
        self.ws.record_object_revision(first, expected_version=0)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'restart.db'
            save_workspace(self.ws, db)
            restored = load_workspace(db)
            before = restored.version
            self.assertEqual(first, restored.record_object_revision(first, expected_version=0))
            self.assertEqual(before, restored.version)
            restored.record_object_revision(self.revision(2), expected_version=1)
            save_workspace(restored, db)
            again = load_workspace(db)
        self.assertEqual(2, len(again.object_revisions))
        self.assertEqual((), again.verify_audit_chain())

    def test_invalid_content_and_data_kind_rejected(self):
        for change in ({'content_json':'[]'}, {'content_json':'{"a": NaN}'}, {'data_kind':'verified'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.revision(**change)

    def test_revision_time_is_in_audit_payload(self):
        self.ws.record_object_revision(self.revision(), expected_version=0)
        event = self.ws.events[-1]
        self.assertEqual('2026-09-24T08:00:00+08:00', event.payload['revision']['times']['occurred_at'])
        event.payload['revision']['times']['recorded_at'] = '2026-09-24T07:00:00+08:00'
        self.assertIn(event.sequence, self.ws.verify_audit_chain())
