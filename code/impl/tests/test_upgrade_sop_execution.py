"""Execution SOP keeps report provenance, unresolved issues and simulation apart."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from panda_brain.execution_sop import execution_view, render_execution
from panda_brain.models import AttemptStatus, OrderStatus
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.sandview import render_sand_page
from tests import test_upgrade_sop as early
from tests.test_upgrade_sop_deployment import open_task, run_task


class ExecutionSopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def start(self):
        open_task(self.ws, self.record.event_id)
        run_task(self.ws, self.record.event_id, 1)

    def view(self):
        return execution_view(self.ws, self.record)

    def report(self, **changes):
        args = dict(kind='running', by='模拟组长', at='2026-09-29T11:00:00+08:00',
            source='模拟对讲原文引用', content='模拟核查进展', request_id='execution-1',
            where='模拟北坡', personnel='模拟人员情况', risk='', need='',
            event_id=self.record.event_id, expected_version=1)
        args.update(changes)
        return self.ws.record_command_receipt('sop-task', **args)

    def revise(self):
        return self.ws.revise_command('sop-task', by='模拟部署人', note='模拟目标变更',
            target='模拟南坡', event_id=self.record.event_id, expected_version=1)

    def test_progress_source_location_risk_and_support_are_reported_not_verified(self):
        self.start()
        self.report(kind='cannot_execute', risk='模拟道路阻塞', need='模拟请求通信支援')
        card = self.view().tasks[0]
        self.assertEqual('有执行事项待核对', self.view().state)
        for word in ('模拟组长', '11:00:00', '来源原文 模拟对讲', '报告位置（待核查） 模拟北坡',
                     '模拟人员情况', '模拟道路阻塞', '模拟请求通信支援'):
            self.assertIn(word, str(card.current))
        self.assertIn('是否解除须核对', str(card.attention))
        self.assertIn('原始对讲音频/转写证据', str(self.view().missing))
        # A later generic progress report cannot silently clear a prior issue.
        self.report(request_id='execution-2')
        self.assertIn('模拟道路阻塞', str(self.view().tasks[0].attention))

    def test_revision_moves_old_issues_to_history_and_requires_new_feedback(self):
        self.start()
        self.report(kind='exec_abnormal', risk='旧版异常')
        self.revise()
        card = self.view().tasks[0]
        self.assertEqual((), card.current)
        self.assertEqual((), card.attention)
        self.assertIn('旧版异常', str(card.historical))
        self.assertIn('内容版本 1 → 2', str(card.changes))
        self.assertIn('模拟目标变更', str(card.changes))
        self.assertIn('暂无来源完整', str(card.missing))
        self.assertIn('开始执行记录：未记录', str(card.facts))
        self.ws.confirm_command_flow('sop-task', actor='模拟部署人')
        run_task(self.ws, self.record.event_id, 2)
        self.report(expected_version=2, request_id='version-2', content='新版反馈')
        self.assertIn('新版反馈', str(self.view().tasks[0].current))
        self.assertNotIn('旧版异常', str(self.view().tasks[0].current))

    def test_pause_withdraw_and_retained_old_reasons_do_not_leak_into_new_version(self):
        self.start()
        self.ws.pause_command('sop-task', reason='模拟暂停原因', reassess_at='模拟约定重评时刻', by='模拟组长')
        self.assertIn('模拟约定重评时刻', str(self.view().tasks[0].attention))
        self.ws.request_command_withdraw('sop-task', reason='模拟撤回原因', by='模拟组长')
        self.assertIn('模拟撤回原因', str(self.view().tasks[0].attention))
        self.assertNotIn('模拟暂停原因', str(self.view().tasks[0].attention))
        self.ws.command_withdraw_ack('sop-task', kind='stopped', by='模拟组长', source='模拟电台')
        # Legacy withdrawal receipts carry no version. Do not invent it.
        self.assertEqual((), self.view().tasks[0].current)
        self.assertIn('旧撤回回执', str(self.view().tasks[0].missing))
        self.revise()
        self.assertEqual((), self.view().tasks[0].attention)
        self.assertNotIn('模拟撤回原因', str(self.view()))

    def test_reported_completion_and_legacy_close_do_not_complete_sop(self):
        self.start()
        self.report(kind='all_done', content='模拟全部完成')
        self.assertEqual('待核对执行依据', self.view().state)
        self.ws.close_command('sop-task', by='旧核验人', result='completed',
                              event_id=self.record.event_id, expected_version=1)
        self.assertIn('不证明新版指定核验授权', str(self.view().tasks[0].facts))
        self.assertNotEqual('已完成', self.view().state)

    def test_untrusted_flow_and_broken_chain_exclude_execution_details(self):
        self.start()
        original = self.report(content='PRIVATE-REPORT')
        for change in ({'event_id': 'other'}, {'order_id': 'other'}, {'content_version': 0},
                       {'content_version': True}, {'target': 'unaudited'}):
            self.ws.command_flows['sop-task'] = replace(original, **change)
            self.assertNotIn('PRIVATE-REPORT', str(self.view()))
            self.assertTrue(self.view().tasks[0].missing)
        self.ws.command_flows['sop-task'] = original
        self.ws.events[-1].payload['act'] = 'tampered'
        self.assertEqual('依据待核对', self.view().state)
        self.assertEqual((), self.view().tasks)

    def test_invalid_receipt_and_change_references_cannot_become_current_issues(self):
        self.start()
        flow = self.report()
        invalid = tuple(dict(flow.receipts[0], content='INVALID', risk='INVALID', **changes)
            for changes in ({'event_id': 'other'}, {'order_id': 'other'}, {'task_version': '2'},
                            {'task_version': '01'}, {'task_version': '9' * 5000}, {'source': ''},
                            {'at': ''}, {'by': ''}, {'kind': 'unknown'}))
        bad_change = dict(event_id='other', order_id='sop-task', task_version='1',
                          revised_by='PRIVATE', revised_at='now', reason='INVALID')
        altered = replace(flow, receipts=flow.receipts + invalid, content_history=(bad_change,))
        self.ws.command_flows['sop-task'] = altered
        self.ws.append_event('CommandFlowChanged', order_id='sop-task',
                             event_id=self.record.event_id, flow=asdict(altered))
        self.assertEqual(1, len(self.view().tasks[0].current))
        self.assertEqual((), self.view().tasks[0].attention)
        self.assertNotIn('INVALID', str(self.view()))
        self.assertNotIn('PRIVATE', str(self.view()))
        self.assertIn('有修订历史缺少', str(self.view().tasks[0].missing))

    def test_simulated_reports_stay_separate_and_require_audit_and_existing_order(self):
        self.start()
        self.ws.orders['sop-task'].status = OrderStatus.ACTIVE
        report = self.ws.record_execution('sop-task', AttemptStatus.SUCCEEDED, ('模拟观测',))
        self.assertIn('模拟成功', str(self.view().simulated))
        self.assertIn('不计作现场进展', str(self.view().simulated))
        self.assertEqual((), self.view().tasks[0].current)
        self.ws.reports[report.id] = replace(report, observations=('UNTRUSTED',))
        self.assertEqual((), self.view().simulated)
        self.assertNotIn('UNTRUSTED', str(self.view()))
        self.ws.reports[report.id] = report
        del self.ws.orders['sop-task']
        self.assertEqual((), self.view().simulated)
        self.assertIn('无对应任务', str(self.view().missing))

    def test_repeat_load_and_persistence_are_readonly_without_timeout_inference(self):
        self.start()
        self.report()
        original = self.view()
        before = (self.ws.version, [asdict(event) for event in self.ws.events])
        with patch.object(self.ws, 'command_overdue_notices', side_effect=AssertionError('unreviewed timeouts')):
            for _ in range(3):
                self.assertEqual(original, self.view())
        self.assertEqual(before, (self.ws.version, [asdict(event) for event in self.ws.events]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.db'
            save_workspace(self.ws, path)
            self.assertEqual(original, execution_view(load_workspace(path), self.record))

    def test_empty_unbound_and_wrong_event_are_not_proof_of_no_action(self):
        self.assertEqual('待补执行资料', self.view().state)
        self.assertIn('不能推定现场没有', str(self.view().missing))
        self.assertEqual('未进入事件', execution_view(self.ws, None).state)
        self.start()
        self.assertEqual((), execution_view(self.ws, replace(self.record, event_id='other')).tasks)
        self.ws.event_id = ''
        self.assertEqual('依据待核对', self.view().state)

    def test_six_answers_escaping_parallel_actions_and_role_boundaries(self):
        self.start()
        self.report(risk='<script>PRIVATE</script>')
        html = render_execution(self.state, 'commander')
        # 沙盘改版后不再输出"六问"标题，改为核对新框架确实渲染。
        self.assertTrue(html)
        self.assertIn('&lt;script&gt;PRIVATE&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE</script>', html)
        page = render_sand_page(self.state)
        self.assertIn('class="cmd-page"', page)
        self.assertEqual(9, page.count('data-sand-step="'))
        for role in ('member', 'public'):
            self.assertEqual('', render_execution(self.state, role))
            self.assertNotIn('class="sop-execution"', render_sand_page(self.state, role=role))


if __name__ == '__main__':
    unittest.main()
