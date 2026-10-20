"""P07-04: simulated device coordinates remain review evidence only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.radio_devices import record_simulated_holder
from panda_brain.radio_positions import (
    latest_device_position, position_samples, record_simulated_position,
    review_device_position,
)
from panda_brain.workspace import ScenarioWorkspace


class SimulatedRadioPositionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('radio-position', event_id='event-a')
        self.base = dict(
            event_id='event-a', source_sample_id='sample-1',
            source_ref='simulated-gnss-record-1', device_id='radio-07',
            latitude=32.10, longitude=110.50, accuracy_m=12.0,
            captured_at='2026-09-29T09:30:00+08:00',
            received_at='2026-09-29T09:31:00+08:00',
            reported_group_id='squad-a', reported_holder_id='leader-a')

    def record(self, recorded_at='2026-09-29T20:00:00+08:00', **changes):
        values = dict(self.base)
        values.update(changes)
        with patch('panda_brain.radio_positions._recorded_at',
                   return_value=recorded_at):
            return record_simulated_position(self.workspace, **values)

    def holder(self, recorded_at='2026-09-29T19:00:00+08:00', **changes):
        values = dict(
            event_id='event-a', device_id='radio-07', holder_id='leader-a',
            group_id='squad-a', call_sign='北线一队',
            effective_at='2026-09-29T09:00:00+08:00',
            source_ref='simulated-issue-log', reported_by='sim-operator',
            request_id='holder-1', change_kind='initial', expected_version=0,
            reason='模拟首配登记')
        values.update(changes)
        with patch('panda_brain.radio_devices._recorded_at',
                   return_value=recorded_at):
            return record_simulated_holder(self.workspace, **values)

    def test_source_precision_and_coordinates_persist_without_map_or_task_write(self):
        sample = self.record()
        self.assertEqual('WGS84', sample.coordinate_system)
        self.assertEqual('simulated', sample.data_kind)
        self.assertEqual('reported_unverified', sample.trust_status)
        self.assertEqual((sample,), position_samples(self.workspace, 'event-a'))
        self.assertEqual({}, self.workspace.reports)
        self.assertEqual({}, self.workspace.orders)
        self.assertEqual({}, self.workspace.annotations)

    def test_repeated_source_is_idempotent_conflict_and_invalid_samples_rejected(self):
        sample = self.record()
        count = len(self.workspace.events)
        self.assertEqual(sample, self.record())
        for changes in (
            {'latitude': 32.11}, {'event_id': 'event-b'},
            {'source_ref': ''}, {'device_id': ''},
            {'latitude': 91}, {'longitude': float('nan')},
            {'accuracy_m': 0}, {'accuracy_m': float('inf')},
            {'captured_at': '2026-09-29T09:30:00'},
            {'received_at': '2026-09-29T09:29:00+08:00'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(**changes)
            self.assertEqual(count, len(self.workspace.events))

    def test_late_arrival_and_as_known_view_use_capture_and_record_times(self):
        first = self.record()
        later = self.record(
            recorded_at='2026-09-29T20:01:00+08:00',
            source_sample_id='sample-2', source_ref='simulated-gnss-record-2',
            captured_at='2026-09-29T10:00:00+08:00',
            received_at='2026-09-29T10:01:00+08:00', latitude=32.11)
        self.record(
            recorded_at='2026-09-29T20:02:00+08:00',
            source_sample_id='sample-3', source_ref='simulated-gnss-record-3',
            captured_at='2026-09-29T09:45:00+08:00',
            received_at='2026-09-29T09:46:00+08:00', latitude=32.12)
        self.assertEqual(later, latest_device_position(
            self.workspace, 'event-a', 'radio-07'))
        self.assertEqual(first, latest_device_position(
            self.workspace, 'event-a', 'radio-07',
            as_of='2026-09-29T09:40:00+08:00'))
        self.assertEqual(first, latest_device_position(
            self.workspace, 'event-a', 'radio-07',
            known_at='2026-09-29T20:00:00+08:00'))

    def test_freshness_requires_explicit_review_window_and_absence_stays_unknown(self):
        missing = review_device_position(
            self.workspace, 'event-a', 'radio-07',
            now='2026-09-29T10:00:00+08:00')
        self.assertIsNone(missing.sample)
        self.assertIsNone(missing.age_seconds)
        self.assertEqual('unknown', missing.freshness)
        self.record()
        unknown = review_device_position(
            self.workspace, 'event-a', 'radio-07',
            now='2026-09-29T10:00:00+08:00')
        self.assertEqual(1800.0, unknown.age_seconds)
        self.assertEqual('unknown', unknown.freshness)
        stale = review_device_position(
            self.workspace, 'event-a', 'radio-07',
            now='2026-09-29T10:00:00+08:00', review_window_seconds=900)
        self.assertEqual('over_review_window', stale.freshness)
        self.assertTrue(any('核查窗口' in issue for issue in stale.issues))
        with self.assertRaises(ValueError):
            review_device_position(self.workspace, 'event-a', 'radio-07',
                                   now='2026-09-29T10:00:00+08:00',
                                   review_window_seconds=0)

    def test_holder_handover_and_conflicting_source_are_only_review_clues(self):
        self.holder()
        self.holder(
            recorded_at='2026-09-29T19:01:00+08:00',
            holder_id='leader-b', group_id='squad-b',
            effective_at='2026-09-29T10:00:00+08:00',
            source_ref='simulated-handover-log', request_id='holder-2',
            change_kind='handover', expected_version=1, reason='模拟交接')
        self.record(source_sample_id='sample-2',
                    captured_at='2026-09-29T10:30:00+08:00',
                    received_at='2026-09-29T10:31:00+08:00')
        review = review_device_position(
            self.workspace, 'event-a', 'radio-07',
            now='2026-09-29T10:40:00+08:00')
        self.assertTrue(any('自报队伍' in issue for issue in review.issues))
        self.assertTrue(any('自报携带人' in issue for issue in review.issues))
        self.assertEqual('simulated_unverified', review.status)
        self.assertEqual({}, self.workspace.reports)

    def test_sqlite_restore_and_tamper_fail_closed(self):
        sample = self.record()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'radio-position.db'
            save_workspace(self.workspace, path)
            restored = load_workspace(path)
            self.assertEqual((sample,), position_samples(restored, 'event-a'))
            restored._events[-1].payload['position']['latitude'] = 0.0
            with self.assertRaises(ValueError):
                position_samples(restored, 'event-a')


if __name__ == '__main__':
    unittest.main()
