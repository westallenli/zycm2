"""P07-03: simulated call segments are review candidates, not task receipts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.offline_tasks import amend_offline, record_control, record_offline, task_state
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.radio_devices import record_simulated_holder
from panda_brain.radio_reports import record_simulated_call
from panda_brain.radio_task_candidates import (
    assess_task_candidate, record_task_candidate, task_candidates,
)
from panda_brain.workspace import ScenarioWorkspace


class RadioTaskCandidateTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('radio-candidate', event_id='event-a')
        self.auth = EventAuthorizationService(
            self.workspace, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda person: person in {'duty', 'deputy'})
        self.auth.record_initial_appointment(
            'duty', event_id='event-a', role=Role.DEPUTY,
            subject_id='deputy', evidence='模拟任命依据', request_id='appoint-p07-03')
        self.task_id = record_offline(
            self.workspace, self.auth, 'deputy', event_id='event-a',
            content='北段观察', group_id='squad-a', issued_by='模拟指挥员',
            recipient_id='leader-a', verifier_id='',
            occurred_at='2026-09-29T09:00:00+08:00',
            evidence='模拟线下指令', request_id='offline-p07-03')['task_id']
        self.call = record_simulated_call(
            self.workspace, event_id='event-a', source_call_id='call-1',
            source_ref='simulated-audio-1',
            occurred_at='2026-09-29T09:30:00+08:00',
            channel_id='single-channel', device_id='radio-07',
            call_sign='北线一队', reported_group_id='squad-a',
            reported_holder_id='leader-a', reported_speaker_id='leader-a',
            transcript='收到北段观察任务。北段观察正在进行。',
            task_ids=(self.task_id,))
        self.base = dict(
            event_id='event-a', source_call_id='call-1', task_id=self.task_id,
            segment_id='segment-1', task_version=1,
            proposed_action='received', quoted_text='收到北段观察任务',
            annotated_by='sim-reviewer', basis='模拟转写逐段标注',
            request_id='candidate-1')

    def candidate(self, **changes):
        values = dict(self.base)
        values.update(changes)
        return record_task_candidate(self.workspace, **values)

    def test_multiple_segments_keep_source_and_leave_task_state_untouched(self):
        before = task_state(self.workspace, 'event-a')[self.task_id]
        first = self.candidate()
        second = self.candidate(
            segment_id='segment-2', proposed_action='progress',
            quoted_text='北段观察正在进行', request_id='candidate-2')
        self.assertEqual((first, second), task_candidates(self.workspace, 'event-a'))
        self.assertEqual('simulated-audio-1', first.source_ref)
        self.assertEqual('pending_review', assess_task_candidate(self.workspace, first).status)
        self.assertEqual(before, task_state(self.workspace, 'event-a')[self.task_id])
        self.assertEqual({}, self.workspace.reports)
        self.assertEqual({}, self.workspace.orders)

    def test_missing_or_wrong_provenance_cannot_become_candidate(self):
        count = len(self.workspace.events)
        for changes in (
            {'event_id': 'event-b'}, {'source_call_id': 'unknown'},
            {'task_id': 'unknown'}, {'quoted_text': '转写里不存在的内容'},
            {'quoted_text': ''}, {'task_version': 2},
            {'proposed_action': 'publish'}, {'annotated_by': ''},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.candidate(**changes)
            self.assertEqual(count, len(self.workspace.events))

    def test_retry_conflict_and_correction_preserve_original(self):
        first = self.candidate()
        count = len(self.workspace.events)
        self.assertEqual(first, self.candidate())
        self.assertEqual(count, len(self.workspace.events))
        with self.assertRaises(ValueError):
            self.candidate(quoted_text='北段观察正在进行')
        with self.assertRaises(ValueError):
            self.candidate(request_id='candidate-2', expected_version=0,
                           corrects_version=1, proposed_action='progress',
                           quoted_text='北段观察正在进行')
        corrected = self.candidate(
            request_id='candidate-2', expected_version=1,
            corrects_version=1, proposed_action='progress',
            quoted_text='北段观察正在进行', basis='模拟转写人工更正')
        self.assertEqual(2, corrected.version)
        self.assertEqual((first, corrected), task_candidates(self.workspace, 'event-a'))
        self.assertEqual('received', first.proposed_action)
        self.assertEqual('pending_review', assess_task_candidate(self.workspace, corrected).status)

    def test_device_hint_match_is_still_unverified(self):
        with patch('panda_brain.radio_devices._recorded_at',
                   return_value='2026-09-29T20:00:00+08:00'):
            record_simulated_holder(
                self.workspace, event_id='event-a', device_id='radio-07',
                holder_id='leader-a', group_id='squad-a', call_sign='北线一队',
                effective_at='2026-09-29T09:00:00+08:00',
                source_ref='simulated-issue-log', reported_by='sim-reviewer',
                request_id='holder-candidate-1', change_kind='initial',
                expected_version=0, reason='模拟设备分配')
        candidate = self.candidate()
        assessment = assess_task_candidate(self.workspace, candidate)
        self.assertTrue(any('尚无可信身份' in issue for issue in assessment.issues))
        self.assertEqual('pending_review', assessment.status)
        self.assertEqual('reported_offline',
                         task_state(self.workspace, 'event-a')[self.task_id].state)

    def test_task_version_change_requires_review_without_erasing_evidence(self):
        candidate = self.candidate()
        amend_offline(
            self.workspace, self.auth, 'deputy', event_id='event-a',
            task_id=self.task_id, expected_version=1, content='北段改为守望',
            group_id='squad-a', recipient_id='leader-a', verifier_id='',
            occurred_at='2026-09-29T09:40:00+08:00',
            reason='模拟线下变更', request_id='amend-p07-03')
        issues = assess_task_candidate(self.workspace, candidate).issues
        self.assertTrue(any('版本已变化' in issue for issue in issues))
        self.assertEqual(candidate, task_candidates(self.workspace, 'event-a')[0])

    def test_paused_task_keeps_candidate_pending(self):
        candidate = self.candidate()
        record_control(
            self.workspace, self.auth, 'deputy', event_id='event-a',
            task_id=self.task_id, expected_version=1, action='pause',
            occurred_at='2026-09-29T09:40:00+08:00',
            reason='模拟暂停', request_id='pause-p07-03')
        issues = assess_task_candidate(self.workspace, candidate).issues
        self.assertTrue(any('已暂停或终止' in issue for issue in issues))
        self.assertEqual('pending_review', assess_task_candidate(
            self.workspace, candidate).status)

    def test_sqlite_restore_and_tamper_fail_closed(self):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'radio-candidates.db'
            save_workspace(self.workspace, path)
            restored = load_workspace(path)
            self.assertEqual((candidate,), task_candidates(restored, 'event-a'))
            restored._events[-1].payload['candidate']['quoted_text'] = '篡改'
            with self.assertRaises(ValueError):
                task_candidates(restored, 'event-a')


if __name__ == '__main__':
    unittest.main()
