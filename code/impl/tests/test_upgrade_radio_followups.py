"""P07-05: follow-up prompts are merged read-only leads for named humans."""
import unittest
from unittest.mock import patch

from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.offline_tasks import (
    record_control, record_feedback, record_offline, task_state, verify_close,
)
from panda_brain.radio_devices import record_simulated_holder
from panda_brain.radio_followups import followup_board
from panda_brain.radio_positions import record_simulated_position
from panda_brain.radio_reports import record_simulated_call
from panda_brain.radio_task_candidates import record_task_candidate
from panda_brain.workspace import ScenarioWorkspace


NOW = '2026-09-29T23:00:00+08:00'


class RadioFollowupTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('radio-followup', event_id='event-a')
        self.auth = EventAuthorizationService(
            self.workspace, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda person: person in {'duty', 'deputy', 'verifier'})

    def appoint(self):
        self.auth.record_initial_appointment(
            'duty', event_id='event-a', role=Role.DEPUTY,
            subject_id='deputy', evidence='模拟指定依据', request_id='appoint-followup')

    def task(self, **changes):
        values = dict(
            event_id='event-a', content='观察北段', group_id='squad-a',
            issued_by='模拟指挥员', recipient_id='leader-a',
            verifier_id='verifier', occurred_at='2026-09-29T09:00:00+08:00',
            evidence='模拟线下指令', request_id='offline-followup', due_at='')
        values.update(changes)
        return record_offline(self.workspace, self.auth, 'deputy', **values)['task_id']

    def feedback(self, task_id, action, request_id):
        return record_feedback(
            self.workspace, lambda token: token, 'leader-a',
            event_id='event-a', task_id=task_id, expected_version=1,
            action=action, basis='模拟本人报告', request_id=request_id,
            is_current_leader=lambda group, actor:
            group == 'squad-a' and actor == 'leader-a')

    def test_missing_named_coordinator_fails_closed_and_bad_window_rejected(self):
        board = followup_board(self.workspace, 'event-a', now=NOW)
        self.assertEqual((), board.prompts)
        self.assertTrue(any('副总指挥' in issue for issue in board.issues))
        for bad in (0, -1, float('inf'), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                followup_board(self.workspace, 'event-a', now=NOW,
                               position_review_window_seconds=bad)
        with self.assertRaises(ValueError):
            followup_board(self.workspace, 'event-b', now=NOW)

    def test_explicit_deadline_and_difficulty_merge_for_current_recipient(self):
        self.appoint()
        task_id = self.task(due_at='2026-09-29T10:00:00+08:00')
        self.feedback(task_id, 'received', 'received-1')
        self.feedback(task_id, 'started', 'started-1')
        self.feedback(task_id, 'difficulty', 'difficulty-1')
        count = len(self.workspace.events)
        board = followup_board(self.workspace, 'event-a', now=NOW)
        self.assertEqual(board, followup_board(self.workspace, 'event-a', now=NOW))
        self.assertEqual(count, len(self.workspace.events))
        self.assertEqual(1, len(board.prompts))
        prompt = board.prompts[0]
        self.assertEqual('leader-a', prompt.owner_id)
        self.assertEqual(('task_recorded_recipient',), prompt.owner_sources)
        self.assertEqual('deputy', prompt.coordinator_id)
        self.assertEqual(('explicit_deadline_passed',
                          'reported_difficulty_needs_question'), prompt.reason_codes)
        self.assertTrue(any('difficulty-1' in ref for ref in prompt.evidence_refs))
        self.assertEqual('suggested_unhandled', prompt.status)
        self.assertEqual('blocked', task_state(self.workspace, 'event-a')[task_id].state)

    def test_completion_routes_to_verifier_and_closed_task_disappears(self):
        self.appoint()
        task_id = self.task()
        self.feedback(task_id, 'received', 'received-1')
        self.feedback(task_id, 'started', 'started-1')
        self.feedback(task_id, 'completed', 'completed-1')
        prompts = followup_board(self.workspace, 'event-a', now=NOW).prompts
        self.assertEqual(1, len(prompts))
        self.assertEqual('verifier', prompts[0].owner_id)
        self.assertEqual(('task_recorded_verifier',), prompts[0].owner_sources)
        self.assertEqual(('completion_needs_verification',), prompts[0].reason_codes)
        verify_close(
            self.workspace, lambda token: token, 'verifier',
            event_id='event-a', task_id=task_id, expected_version=1,
            evidence='模拟独立核验', overall_goal_evidence='模拟整体目标核验',
            request_id='close-followup', subject_exists=lambda _: True)
        self.assertEqual((), followup_board(self.workspace, 'event-a', now=NOW).prompts)

    def test_completed_task_without_named_verifier_reports_gap_not_false_owner(self):
        self.appoint()
        task_id = self.task(verifier_id='')
        self.feedback(task_id, 'received', 'received-1')
        self.feedback(task_id, 'started', 'started-1')
        self.feedback(task_id, 'completed', 'completed-1')
        board = followup_board(self.workspace, 'event-a', now=NOW)
        self.assertEqual((), board.prompts)
        self.assertTrue(any(task_id in issue and '核验负责人' in issue
                            for issue in board.issues))

    def test_corrected_radio_segments_merge_under_deputy_review(self):
        self.appoint()
        task_id = self.task()
        record_simulated_call(
            self.workspace, event_id='event-a', source_call_id='call-1',
            source_ref='simulated-audio-1', occurred_at='2026-09-29T09:30:00+08:00',
            channel_id='single-channel', transcript='收到任务。正在观察。',
            task_ids=(task_id,))
        first = dict(
            event_id='event-a', source_call_id='call-1', task_id=task_id,
            segment_id='segment-1', task_version=1, proposed_action='received',
            quoted_text='收到任务', annotated_by='sim-reviewer',
            basis='模拟逐段标注', request_id='candidate-1')
        record_task_candidate(self.workspace, **first)
        record_task_candidate(
            self.workspace, **{**first, 'proposed_action': 'progress',
                               'quoted_text': '正在观察', 'basis': '模拟标注更正',
                               'request_id': 'candidate-2',
                               'expected_version': 1, 'corrects_version': 1})
        record_task_candidate(
            self.workspace, **{**first, 'segment_id': 'segment-2',
                               'proposed_action': 'progress',
                               'quoted_text': '正在观察', 'request_id': 'candidate-3'})
        prompts = followup_board(self.workspace, 'event-a', now=NOW).prompts
        self.assertEqual(1, len(prompts))
        self.assertEqual('deputy', prompts[0].owner_id)
        self.assertEqual(('event_appointment',), prompts[0].owner_sources)
        self.assertEqual(('radio_candidate_needs_review',), prompts[0].reason_codes)
        self.assertEqual(2, len(prompts[0].evidence_refs))
        self.assertTrue(any('segment-1:v2' in ref for ref in prompts[0].evidence_refs))

    def test_position_window_and_holder_conflict_merge_by_device(self):
        self.appoint()
        with patch('panda_brain.radio_devices._recorded_at',
                   return_value='2026-09-29T20:00:00+08:00'):
            record_simulated_holder(
                self.workspace, event_id='event-a', device_id='radio-07',
                holder_id='leader-a', group_id='squad-a', call_sign='北线一队',
                effective_at='2026-09-29T09:00:00+08:00',
                source_ref='simulated-issue-log', reported_by='sim-operator',
                request_id='holder-followup', change_kind='initial',
                expected_version=0, reason='模拟领用')
        with patch('panda_brain.radio_positions._recorded_at',
                   return_value='2026-09-29T21:00:00+08:00'):
            record_simulated_position(
                self.workspace, event_id='event-a', source_sample_id='sample-1',
                source_ref='simulated-gnss-1', device_id='radio-07',
                latitude=32.1, longitude=110.5, accuracy_m=15,
                captured_at='2026-09-29T10:00:00+08:00',
                reported_group_id='squad-b', reported_holder_id='leader-b')
        prompt = followup_board(
            self.workspace, 'event-a', now=NOW,
            position_review_window_seconds=3600).prompts[0]
        self.assertEqual('device', prompt.subject_kind)
        self.assertEqual('deputy', prompt.owner_id)
        self.assertEqual(('position_holder_conflict',
                          'position_over_review_window'), prompt.reason_codes)
        without_window = followup_board(self.workspace, 'event-a', now=NOW).prompts[0]
        self.assertEqual(('position_holder_conflict',), without_window.reason_codes)
        self.assertEqual({}, self.workspace.reports)
        self.assertEqual({}, self.workspace.orders)

    def test_no_inferred_silence_alert_and_paused_task_not_urged(self):
        self.appoint()
        task_id = self.task()
        self.assertEqual((), followup_board(self.workspace, 'event-a', now=NOW).prompts)
        record_control(
            self.workspace, self.auth, 'deputy', event_id='event-a',
            task_id=task_id, expected_version=1, action='pause',
            occurred_at='2026-09-29T10:00:00+08:00',
            reason='模拟暂停', request_id='pause-followup')
        self.assertEqual((), followup_board(self.workspace, 'event-a', now=NOW).prompts)


if __name__ == '__main__':
    unittest.main()
