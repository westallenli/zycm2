"""Deployment projection: version provenance, honest gaps and readonly UI."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from panda_brain.models import ExecutionOrder, OrderStatus
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.sandview import render_sand_page
from panda_brain.task_sop import deployment_view, render_deployment
from tests import test_upgrade_sop as early


def open_task(ws, event_id):
    # Synthetic confirmed order; not proof of a V2 professional approval service.
    ws.orders['sop-task'] = ExecutionOrder('sop-task', 'sim-decision', 'sim-option',
                                         '模拟任务', OrderStatus.CONFIRMED)
    ws.open_command_flow('sop-task', event_id=event_id, target='模拟北段核查',
        unit='模拟执行组', deadline='2026-09-29T10:00:00+08:00',
        safety_cond='模拟安全要求', stop_cond='模拟停止要求', receipt_need='模拟逐项反馈')
    for stage in ('pending_validation', 'pending_approval', 'pending_confirm'):
        ws.advance_command_flow('sop-task', stage, actor='模拟登记人')
    ws.confirm_command_flow('sop-task', actor='模拟登记人')


def run_task(ws, event_id, version):
    ws.command_ack('sop-task', by='模拟组长', result='accepted_ready',
                   event_id=event_id, expected_version=version)
    ws.confirm_execution_conditions('sop-task', by='模拟组长', note='模拟条件记录')
    ws.begin_command_execution('sop-task', by='模拟组长')


def receipt(ws, event_id, version):
    ws.record_command_receipt('sop-task', kind='all_done', by='模拟组长',
        source='模拟电台报告', content=f'模拟版本{version}自报完成',
        request_id=f'sop-receipt-{version}', event_id=event_id, expected_version=version)


class DeploymentTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def open(self):
        open_task(self.ws, self.record.event_id)

    def view(self):
        return deployment_view(self.ws, self.record)

    def test_content_delivery_and_optional_location_do_not_imply_acceptance(self):
        self.open()
        self.ws.command_delivery('sop-task', delivered_at='模拟送达时间', viewed_at='模拟已阅时间')
        card = self.view().tasks[0]
        for text in ('模拟北段核查', '模拟执行组', '模拟安全要求', '模拟停止要求',
                     '模拟逐项反馈', '非空间任务不强制', '均不等于接收'):
            self.assertIn(text, str(card.facts))
        self.assertIn('暂无完整接收记录', str(card.missing))

    def test_revision_separates_historical_completion_from_current_receipts(self):
        self.open()
        run_task(self.ws, self.record.event_id, 1)
        receipt(self.ws, self.record.event_id, 1)
        self.assertEqual(1, len(self.view().tasks[0].current_receipts))
        self.ws.revise_command('sop-task', by='模拟登记人', note='模拟改版',
            target='模拟南段核查', event_id=self.record.event_id, expected_version=1)
        card = self.view().tasks[0]
        self.assertEqual((), card.current_receipts)
        self.assertEqual(1, len(card.historical_receipts))
        self.assertIn('暂无完整接收记录', str(card.missing))
        self.assertNotIn('模拟北段核查', str(card.facts))
        self.ws.confirm_command_flow('sop-task', actor='模拟登记人')
        run_task(self.ws, self.record.event_id, 2)
        receipt(self.ws, self.record.event_id, 2)
        self.ws.close_command('sop-task', by='旧核验人', result='completed',
                              event_id=self.record.event_id, expected_version=2)
        card = self.view().tasks[0]
        self.assertIn('模拟版本2自报完成', str(card.current_receipts))
        self.assertNotIn('模拟版本1自报完成', str(card.current_receipts))
        self.assertIn('模拟版本1自报完成', str(card.historical_receipts))
        self.assertIn('尚不能证明', str(card.facts))
        self.assertEqual('待核对部署依据', self.view().state)

    def test_legacy_unbound_foreign_and_orphan_flows_are_not_current_evidence(self):
        self.open()
        original = self.ws.command_flows['sop-task']
        for changes in ({'content_version': 0}, {'event_id': ''}, {'event_id': 'other'},
                        {'order_id': 'other'}, {'content_version': True}):
            with self.subTest(changes=changes):
                self.ws.command_flows['sop-task'] = replace(original, **changes)
                card = self.view().tasks[0]
                self.assertTrue(card.missing)
                self.assertNotIn('模拟北段核查', str(card.facts))
        self.ws.command_flows['orphan'] = replace(original, order_id='orphan')
        self.assertIn('无对应任务', str(self.view().missing))
        del self.ws.command_flows['sop-task']
        self.assertIn('尚无任务执行层', str(self.view().tasks[0].missing))

    def test_audit_snapshot_mismatch_and_broken_chain_suppress_evidence(self):
        self.open()
        self.ws.command_flows['sop-task'] = replace(self.ws.command_flows['sop-task'],
            target='UNTRUSTED', acked_at='now', acked_by='unknown', ack_result='accepted_ready')
        self.assertNotIn('UNTRUSTED', str(self.view()))
        self.assertIn('审计记录不一致', str(self.view()))
        self.ws.events[-1].payload['stage'] = 'tampered'
        self.assertEqual((), self.view().tasks)
        self.assertEqual('依据待核对', self.view().state)

    def test_invalid_receipt_references_excluded_even_in_audited_snapshot(self):
        self.open()
        run_task(self.ws, self.record.event_id, 1)
        receipt(self.ws, self.record.event_id, 1)
        flow = self.ws.command_flows['sop-task']
        valid = flow.receipts[0]
        invalid = tuple(dict(valid, content='INVALID', **changes) for changes in (
            {'event_id': 'other'}, {'order_id': 'other'}, {'task_version': '2'},
            {'task_version': '01'}, {'task_version': '0'}, {'task_version': ''},
            {'task_version': True}, {'source': ''}, {'by': ''}, {'at': ''}, {'kind': 'unknown'}))
        altered = replace(flow, receipts=flow.receipts + invalid)
        self.ws.command_flows['sop-task'] = altered
        self.ws.append_event('CommandFlowChanged', order_id='sop-task',
                             event_id=self.record.event_id, flow=asdict(altered))
        card = self.view().tasks[0]
        self.assertEqual(1, len(card.current_receipts))
        self.assertNotIn('INVALID', str(card))
        self.assertIn('未纳入', str(card.missing))

    def test_persistence_roundtrip_and_repeat_reads_preserve_facts_without_writes(self):
        self.open()
        run_task(self.ws, self.record.event_id, 1)
        receipt(self.ws, self.record.event_id, 1)
        first = self.view()
        before = (self.ws.version, len(self.ws.events))
        for _ in range(3):
            self.assertEqual(first, self.view())
        self.assertEqual(before, (self.ws.version, len(self.ws.events)))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'simulated.db'
            save_workspace(self.ws, path)
            restored = load_workspace(path)
            self.assertEqual(first, deployment_view(restored, self.record))

    def test_route_reference_is_resolved_only_in_this_event(self):
        self.open()
        self.ws.revise_command('sop-task', by='模拟登记人', note='模拟路线', route_id='route',
            event_id=self.record.event_id, expected_version=1)
        self.ws.annotations['route'] = SimpleNamespace(event_id='other')
        self.assertIn('未找到对应标绘', str(self.view()))
        self.ws.annotations['route'] = SimpleNamespace(event_id=self.record.event_id)
        self.assertIn('适用性仍须核对', str(self.view()))

    def test_six_answers_escaped_content_and_role_visibility(self):
        self.open()
        self.ws.revise_command('sop-task', by='模拟登记人', note='模拟输入',
            target='<script>PRIVATE</script>', event_id=self.record.event_id, expected_version=1)
        html = render_deployment(self.state, 'commander')
        # 沙盘改版后不再输出"六问"标题，改为核对新框架确实渲染。
        self.assertTrue(html)
        self.assertIn('&lt;script&gt;PRIVATE&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE</script>', html)
        page = render_sand_page(self.state)
        self.assertIn('class="cmd-page"', page)
        self.assertEqual(9, page.count('data-sand-step="'))
        for role in ('member', 'public'):
            self.assertEqual('', render_deployment(self.state, role))
            self.assertNotIn('class="sop-deployment"', render_sand_page(self.state, role=role))

    def test_empty_and_wrong_event_workspaces_have_honest_states(self):
        self.assertEqual('待补任务资料', self.view().state)
        self.assertEqual('未进入事件', deployment_view(self.ws, None).state)
        self.open()
        self.assertEqual((), deployment_view(self.ws, replace(self.record, event_id='other')).tasks)
        self.ws.event_id = ''
        self.assertEqual('依据待核对', self.view().state)


if __name__ == '__main__':
    unittest.main()
