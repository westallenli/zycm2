import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from panda_brain import tactical_map as tm
from panda_brain.persistence import decode_entity, encode_entity, load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace


class TaskVersionTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('simulation', event_id='fire-a')
        self.ws.command_flows['task-1'] = tm.CommandFlow(
            order_id='task-1', event_id='fire-a', content_version=1,
            target='北段封堵', unit='team-a', stage='running',
            acked_at='2026-09-24T08:00:00+08:00',
            history=({'by': 'leader', 'do': '开始执行'},))

    @property
    def flow(self):
        return self.ws.command_flows['task-1']

    def receipt(self, **changes):
        args = dict(kind='all_done', by='leader', content='本版完成报告', source='radio-1',
                    request_id='receipt-1', event_id='fire-a', expected_version=1,
                    at='2026-09-24T09:00:00+08:00')
        args.update(changes)
        return self.ws.record_command_receipt('task-1', **args)

    def revise(self, **changes):
        args = dict(by='deputy', note='调整目标', target='南段封堵',
                    event_id='fire-a', expected_version=1)
        args.update(changes)
        return self.ws.revise_command('task-1', **args)

    def test_old_completion_cannot_close_new_content(self):
        self.receipt()
        revised = self.revise()
        self.assertEqual(2, revised.content_version)
        self.assertEqual('pending_confirm', revised.stage)
        self.assertEqual('', revised.acked_at)
        self.assertEqual('北段封堵', revised.content_history[0]['target'])
        # Even after the revised task starts, old evidence cannot close it.
        self.ws.command_flows['task-1'] = replace(revised, stage='running')
        before = self.ws.events
        with self.assertRaises(tm.TacticalError):
            self.ws.close_command('task-1', by='verifier', result='completed',
                                  event_id='fire-a', expected_version=2)
        self.assertEqual(before, self.ws.events)
        self.receipt(expected_version=2, request_id='receipt-2')
        self.assertEqual('done_pending_verify', self.flow.stage)
        self.ws.close_command('task-1', by='verifier', result='completed',
                              event_id='fire-a', expected_version=2)
        self.assertEqual('closed', self.flow.stage)

    def test_stale_missing_boolean_and_future_versions_rejected_without_write(self):
        self.revise()
        before = self.ws.events
        for version in (None, 1, True, 3):
            with self.assertRaises(tm.TacticalError):
                self.receipt(expected_version=version)
        self.assertEqual(before, self.ws.events)

    def test_cross_event_and_missing_event_rejected(self):
        before = self.ws.events
        for event in ('fire-b', None, ''):
            with self.assertRaises(ValueError):
                self.receipt(event_id=event)
        self.assertEqual(before, self.ws.events)

    def test_retry_and_conflict(self):
        first = self.receipt()
        before = self.ws.events
        self.assertEqual(first, self.receipt(at='2026-09-24T09:01:00+08:00'))
        self.assertEqual(before, self.ws.events)
        with self.assertRaises(tm.TacticalError):
            self.receipt(content='different report')
        with self.assertRaises(tm.TacticalError):
            self.receipt(request_id='')
        self.assertEqual(before, self.ws.events)

    def test_receipt_request_id_cannot_be_reused_for_new_content_version(self):
        self.receipt()
        revised = self.revise()
        self.ws.command_flows['task-1'] = replace(revised, stage='running')
        with self.assertRaises(tm.TacticalError):
            self.receipt(expected_version=2)

    def test_progress_and_resend_do_not_change_content_version(self):
        self.receipt(kind='running')
        self.assertEqual(1, self.flow.content_version)
        self.assertEqual('running', self.flow.stage)
        self.ws.resend_command('task-1', by='deputy')
        self.assertEqual(1, self.flow.content_version)
        self.assertEqual(2, self.flow.version)  # old transport/change counter retained
        self.assertTrue(self.flow.acked_at)

    def test_stale_revision_and_identity_override_rejected(self):
        self.revise()
        before = self.ws.events
        with self.assertRaises(tm.TacticalError):
            self.revise(target='西段')
        for changes in ({'content_version': 1}, {'receipts': ()}, {'stage': 'running'}):
            with self.assertRaises(tm.TacticalError):
                self.revise(expected_version=2, **changes)
        self.assertEqual(before, self.ws.events)

    def test_no_content_change_does_not_reset_ack(self):
        before = self.ws.events
        flow = self.revise(target='北段封堵')
        self.assertEqual(1, flow.content_version)
        self.assertTrue(flow.acked_at)
        self.assertEqual(before, self.ws.events)

    def test_receipt_cannot_skip_reconfirmation_or_reopen_closed_task(self):
        self.revise()
        with self.assertRaises(tm.TacticalError):
            self.receipt(expected_version=2)
        self.ws.command_flows['task-1'] = replace(self.flow, stage='closed')
        with self.assertRaises(tm.TacticalError):
            self.receipt(expected_version=2)

    def test_ack_must_reference_current_content(self):
        self.revise()
        self.ws.command_flows['task-1'] = replace(self.flow, stage='confirmed_unack')
        with self.assertRaises(tm.TacticalError):
            self.ws.command_ack('task-1', by='leader', result='accepted_ready',
                                event_id='fire-a', expected_version=1)
        self.ws.command_ack('task-1', by='leader', result='accepted_ready',
                            event_id='fire-a', expected_version=2)
        self.assertEqual('acked_not_started', self.flow.stage)

    def test_roundtrip_preserves_versions_receipts_history_and_retry(self):
        self.receipt()
        self.revise()
        self.ws.command_flows['task-1'] = replace(self.flow, stage='running')
        self.receipt(expected_version=2, request_id='receipt-2')
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/'state.db'
            save_workspace(self.ws, db)
            restored = load_workspace(db)
        self.assertEqual(self.flow, restored.command_flows['task-1'])
        self.ws = restored
        before = self.ws.events
        self.receipt(expected_version=2, request_id='receipt-2')
        self.assertEqual(before, self.ws.events)
        self.assertEqual((), self.ws.verify_audit_chain())
        with self.assertRaises(tm.TacticalError):
            self.receipt()

    def test_legacy_decode_does_not_invent_content_versions(self):
        payload = json.loads(encode_entity(self.flow))
        del payload['data']['content_version']
        del payload['data']['content_history']
        restored = decode_entity(json.dumps(payload))
        self.assertEqual(0, restored.content_version)
        self.assertEqual((), restored.content_history)

    def test_new_event_flow_requires_event_and_binds_first_content(self):
        self.ws.orders['new'] = object()  # open only checks existence; not persisted
        with self.assertRaises(ValueError):
            self.ws.open_command_flow('new', event_id='fire-b')
        flow = self.ws.open_command_flow('new', event_id='fire-a')
        self.assertEqual(1, flow.content_version)
        with self.assertRaises(ValueError):
            self.ws.open_command_flow('new', event_id='fire-b')

    def test_receipt_content_is_in_audit_hash(self):
        self.receipt()
        payload = self.ws._events[-1].payload
        self.assertEqual('1', payload['flow']['receipts'][0]['task_version'])
        payload['flow']['receipts'][0]['content'] = 'tampered'
        self.assertTrue(self.ws.verify_audit_chain())

    def test_generic_stage_advance_cannot_bypass_receipt_or_close_gates(self):
        before = self.ws.events
        with self.assertRaises(tm.TacticalError):
            self.ws.advance_command_flow('task-1', 'done_pending_verify', actor='operator')
        self.assertEqual(before, self.ws.events)
        self.receipt()
        before = self.ws.events
        with self.assertRaises(tm.TacticalError):
            self.ws.advance_command_flow('task-1', 'closed', actor='operator')
        self.assertEqual(before, self.ws.events)


if __name__ == '__main__':
    unittest.main()
