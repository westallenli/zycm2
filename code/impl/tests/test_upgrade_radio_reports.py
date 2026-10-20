"""P07-01: simulated radio evidence stays separate from task execution facts."""
import tempfile
import unittest
from pathlib import Path

from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.offline_tasks import record_offline, task_state
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.radio_reports import calls_for_event, record_simulated_call
from panda_brain.workspace import ScenarioWorkspace


class RadioReportContractTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('radio-simulation', event_id='event-a')
        self.base = dict(
            event_id='event-a', source_call_id='sim-call-1',
            source_ref='simulated-radio-log-1',
            occurred_at='2026-09-29T09:30:00+08:00',
            collected_at='', received_at='',
            channel_id='single-channel', device_id='radio-07',
            call_sign='北线一队', reported_group_id='squad-a',
            reported_holder_id='leader-a', reported_speaker_id='leader-a',
            transcript='报告北段观察进展', audio_ref='', task_ids=(),
        )

    def capture(self, **changes):
        args = dict(self.base)
        args.update(changes)
        return record_simulated_call(self.workspace, **args)

    def create_offline_task(self):
        auth = EventAuthorizationService(
            self.workspace, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda person: person in {'deputy', 'duty'})
        auth.record_initial_appointment(
            'duty', event_id='event-a', role=Role.DEPUTY,
            subject_id='deputy', evidence='模拟任命依据', request_id='appoint-radio')
        return record_offline(
            self.workspace, auth, 'deputy', event_id='event-a',
            content='线下已下达的模拟北段观察任务', group_id='squad-a',
            issued_by='模拟现场指挥员', recipient_id='leader-a',
            verifier_id='', occurred_at='2026-09-29T09:00:00+08:00',
            evidence='模拟指令来源', request_id='offline-radio')['task_id']

    def test_multiple_calls_can_refer_to_one_existing_task_without_execution_side_effect(self):
        task_id = self.create_offline_task()
        before = task_state(self.workspace, 'event-a')[task_id]
        first = self.capture(task_ids=(task_id,))
        second = self.capture(source_call_id='sim-call-2',
                              source_ref='simulated-radio-log-2',
                              occurred_at='2026-09-29T09:40:00+08:00',
                              transcript='再次报告北段情况', task_ids=(task_id,))
        self.assertEqual((task_id,), first.task_ids)
        self.assertEqual((task_id,), second.task_ids)
        self.assertEqual('simulated', first.data_kind)
        self.assertEqual('pending_review', first.identity_status)
        self.assertEqual('reported_unverified', first.content_status)
        self.assertEqual('leader-a', first.reported_speaker_id)
        self.assertEqual('squad-a', first.reported_group_id)
        self.assertEqual('leader-a', first.reported_holder_id)
        self.assertEqual('', first.resolved_holder_id)
        self.assertEqual('', first.location)
        self.assertEqual(before, task_state(self.workspace, 'event-a')[task_id])
        self.assertEqual({}, self.workspace.reports)
        self.assertEqual({}, self.workspace.orders)
        self.assertEqual(2, len(calls_for_event(self.workspace, 'event-a')))

    def test_repeat_delivery_is_idempotent_but_conflicting_payload_is_rejected(self):
        first = self.capture()
        count = len(self.workspace.events)
        self.assertEqual(first, self.capture())
        self.assertEqual(count, len(self.workspace.events))
        with self.assertRaises(ValueError):
            self.capture(transcript='另一段文字')
        self.assertEqual(count, len(self.workspace.events))

    def test_unresolved_identity_and_missing_task_remain_pending(self):
        call = self.capture(call_sign='', device_id='', reported_group_id='',
                            reported_holder_id='', reported_speaker_id='',
                            transcript='', audio_ref='simulated-audio-asset-1')
        self.assertEqual('pending_review', call.identity_status)
        self.assertEqual((), call.task_ids)
        self.assertEqual('', call.transcript)
        self.assertEqual('simulated-audio-asset-1', call.audio_ref)
        self.assertEqual('', call.times.collected_at)
        self.assertEqual('', call.times.received_at)
        self.assertTrue(call.times.recorded_at)
        self.assertEqual((), self.workspace.verify_audit_chain())

    def test_explicit_collection_and_receipt_times_survive_without_inference(self):
        call = self.capture(
            collected_at='2026-09-29T09:31:00+08:00',
            received_at='2026-09-29T09:32:00+08:00')
        self.assertEqual('2026-09-29T09:30:00+08:00', call.times.occurred_at)
        self.assertEqual('2026-09-29T09:31:00+08:00', call.times.collected_at)
        self.assertEqual('2026-09-29T09:32:00+08:00', call.times.received_at)
        self.assertEqual(call, calls_for_event(self.workspace, 'event-a')[0])

    def test_rejects_unknown_task_other_event_and_missing_provenance_before_write(self):
        count = len(self.workspace.events)
        for changes in (
            {'task_ids': ('not-a-task',)},
            {'event_id': 'event-b'},
            {'source_ref': ''},
            {'occurred_at': '2026-09-29T09:30:00'},
            {'transcript': '', 'audio_ref': ''},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.capture(**changes)
            self.assertEqual(count, len(self.workspace.events))

    def test_audited_call_survives_restart_and_tampering_fails_closed(self):
        first = self.capture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'radio.db'
            save_workspace(self.workspace, path)
            restored = load_workspace(path)
            self.assertEqual((first,), calls_for_event(restored, 'event-a'))
            self.assertEqual(first, record_simulated_call(restored, **self.base))
            self.assertEqual((), restored.verify_audit_chain())
            restored._events[-1].payload['call']['source_ref'] = 'changed'
            with self.assertRaises(ValueError):
                calls_for_event(restored, 'event-a')


if __name__ == '__main__':
    unittest.main()
