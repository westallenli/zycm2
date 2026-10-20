"""P07-02: simulated device handovers and corrections remain review evidence."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.radio_devices import (
    attribution_for_call, holder_at, holder_reports, record_simulated_holder,
)
from panda_brain.radio_reports import record_simulated_call
from panda_brain.workspace import ScenarioWorkspace


class SimulatedRadioDeviceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('simulated-device', event_id='event-a')
        self.base = dict(
            event_id='event-a', device_id='radio-07',
            holder_id='leader-a', group_id='squad-a', call_sign='北线一队',
            effective_at='2026-09-29T09:00:00+08:00',
            source_ref='simulated-issue-log-1', reported_by='sim-operator',
            request_id='holder-1',
            change_kind='initial', expected_version=0,
            corrects_version=0, reason='模拟首配登记',
        )

    def record(self, recorded_at='2026-09-29T20:00:00+08:00', **changes):
        values = dict(self.base)
        values.update(changes)
        with patch('panda_brain.radio_devices._recorded_at', return_value=recorded_at):
            return record_simulated_holder(self.workspace, **values)

    def call(self, source_call_id, occurred_at, **changes):
        values = dict(
            event_id='event-a', source_call_id=source_call_id,
            source_ref='simulated-call-' + source_call_id,
            occurred_at=occurred_at, channel_id='single-channel',
            device_id='radio-07', call_sign='北线一队',
            reported_group_id='squad-a', reported_holder_id='leader-a',
            reported_speaker_id='leader-a', transcript='模拟通话', task_ids=(),
        )
        values.update(changes)
        return record_simulated_call(self.workspace, **values)

    def test_handover_uses_call_time_without_promoting_reported_identity(self):
        first = self.record()
        second = self.record(
            recorded_at='2026-09-29T20:01:00+08:00',
            holder_id='leader-b', source_ref='simulated-handover-log',
            request_id='holder-2', change_kind='handover', expected_version=1,
            effective_at='2026-09-29T10:00:00+08:00', reason='模拟设备交接')
        early = self.call('early', '2026-09-29T09:30:00+08:00')
        late = self.call('late', '2026-09-29T10:30:00+08:00',
                         reported_holder_id='leader-b',
                         reported_speaker_id='leader-b')
        self.assertEqual(first, attribution_for_call(self.workspace, early).report)
        self.assertEqual(second, attribution_for_call(self.workspace, late).report)
        self.assertEqual((), attribution_for_call(self.workspace, late).issues)
        self.assertEqual('pending_review', late.identity_status)
        self.assertEqual('', late.resolved_holder_id)
        self.assertEqual('', late.location)
        self.assertEqual({}, self.workspace.reports)
        self.assertEqual({}, self.workspace.orders)

    def test_late_correction_preserves_prior_as_known_view_and_later_handover(self):
        original = self.record()
        handover = self.record(
            recorded_at='2026-09-29T20:01:00+08:00',
            holder_id='leader-b', source_ref='simulated-handover-log',
            request_id='holder-2', change_kind='handover', expected_version=1,
            effective_at='2026-09-29T10:00:00+08:00', reason='模拟交接')
        corrected = self.record(
            recorded_at='2026-09-29T20:02:00+08:00',
            holder_id='leader-c', source_ref='simulated-correction-log',
            reported_by='sim-reviewer',
            request_id='holder-3', change_kind='correction', expected_version=2,
            corrects_version=1, reason='原携带人登记有误')
        self.assertEqual('sim-reviewer', corrected.reported_by)
        self.assertEqual(original, holder_at(
            self.workspace, 'event-a', 'radio-07',
            '2026-09-29T09:30:00+08:00', known_at=handover.recorded_at))
        self.assertEqual(corrected, holder_at(
            self.workspace, 'event-a', 'radio-07',
            '2026-09-29T09:30:00+08:00', known_at=corrected.recorded_at))
        self.assertEqual(handover, holder_at(
            self.workspace, 'event-a', 'radio-07',
            '2026-09-29T10:30:00+08:00'))
        self.assertEqual((original, handover, corrected),
                         holder_reports(self.workspace, 'event-a'))

    def test_retry_is_idempotent_and_conflicts_or_stale_versions_do_not_write(self):
        first = self.record()
        count = len(self.workspace.events)
        self.assertEqual(first, self.record())
        self.assertEqual(count, len(self.workspace.events))
        for changes in (
            {'holder_id': 'another'},
            {'request_id': 'holder-2', 'change_kind': 'handover',
             'expected_version': 0, 'effective_at': '2026-09-29T10:00:00+08:00'},
            {'request_id': 'holder-2', 'change_kind': 'correction',
             'expected_version': 1, 'corrects_version': 99},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(**changes)
            self.assertEqual(count, len(self.workspace.events))

    def test_conflicting_or_missing_call_hints_remain_visible_for_review(self):
        self.record()
        mismatch = self.call('mismatch', '2026-09-29T09:30:00+08:00',
                             call_sign='另一呼号', reported_holder_id='leader-x')
        view = attribution_for_call(self.workspace, mismatch)
        self.assertEqual('leader-a', view.report.holder_id)
        self.assertTrue(any('呼号' in issue for issue in view.issues))
        self.assertTrue(any('携带人' in issue for issue in view.issues))
        missing = self.call('missing', '2026-09-29T09:40:00+08:00', device_id='')
        self.assertIsNone(attribution_for_call(self.workspace, missing).report)
        self.assertTrue(attribution_for_call(self.workspace, missing).issues)
        no_speaker = self.call('no-speaker', '2026-09-29T09:50:00+08:00',
                               reported_speaker_id='')
        self.assertTrue(any('发话人' in issue for issue in
                            attribution_for_call(self.workspace, no_speaker).issues))
        self.assertEqual('pending_review', mismatch.identity_status)

    def test_cross_event_bad_source_and_audit_damage_fail_closed(self):
        count = len(self.workspace.events)
        for changes in ({'event_id': 'event-b'}, {'source_ref': ''},
                        {'reported_by': ''},
                        {'effective_at': '2026-09-29T09:00:00'},
                        {'holder_id': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(**changes)
            self.assertEqual(count, len(self.workspace.events))
        self.record()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'radio-devices.db'
            save_workspace(self.workspace, path)
            restored = load_workspace(path)
            self.assertEqual(holder_reports(self.workspace, 'event-a'),
                             holder_reports(restored, 'event-a'))
            restored._events[-1].payload['report']['holder_id'] = 'tampered'
            with self.assertRaises(ValueError):
                holder_reports(restored, 'event-a')


if __name__ == '__main__':
    unittest.main()
