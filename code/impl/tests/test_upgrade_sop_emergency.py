"""Emergency access, current-version signals and reported support boundaries."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from panda_brain.emergency_sop import emergency_view, render_emergency
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.sandview import render_sand_page
from tests import test_upgrade_sop as early
from tests import test_upgrade_force_needs as force
from tests.test_upgrade_sop_deployment import open_task, run_task


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class EmergencySopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def view(self):
        return emergency_view(self.ws, self.record, self.state.ledger)

    def start(self):
        open_task(self.ws, self.record.event_id)
        run_task(self.ws, self.record.event_id, 1)

    def report(self, **changes):
        args = dict(kind='personnel_safety', by='模拟组长', source='模拟电台',
            content='模拟安全事项待核对', request_id='emergency-1',
            event_id=self.record.event_id, expected_version=1)
        args.update(changes)
        return self.ws.record_command_receipt('sop-task', **args)

    def support_fixture(self):
        f = force.ForceNeedsTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_empty_or_missing_event_keeps_emergency_entry_accessible(self):
        self.assertEqual('待补应急救援依据', self.view().state)
        self.assertIn('不能据此判断现场无险情', str(self.view().missing))
        self.state.active_event_id = ''
        self.assertEqual('未进入事件', emergency_view(self.ws, None, None).state)
        html = render_sand_page(self.state)
        self.assertIn('data-sand-step="6"', html)
        self.assertIn('data-sand-related="6" href="/materials"', html)
        self.assertIn('随时可进入，不等待其他工作项完成', html)

    def test_urgency_comes_from_audited_current_level_not_name_keywords(self):
        open_task(self.ws, self.record.event_id)
        self.ws.orders['sop-task'].scope = '紧急救援（名称不是分类）'
        self.assertEqual((), self.view().signals)
        self.ws.revise_command('sop-task', by='模拟部署人', note='模拟等级调整',
            task_level='urgent', event_id=self.record.event_id, expected_version=1)
        self.assertIn('任务等级记录为紧急', str(self.view().signals))
        self.assertIn('当前内容版本 2', str(self.view().signals))
        self.assertIn('不证明救援事件已成立', str(self.view().signals))

    def test_current_safety_communication_and_support_signals_survive_missing_ledger(self):
        self.start()
        for i, kind in enumerate(('personnel_safety', 'personnel_abnormal', 'comm_abnormal',
                                  'comm_lost_pending', 'cannot_stop', 'exec_abnormal', 'cannot_execute')):
            self.report(kind=kind, request_id=f'emergency-{i}', content=f'模拟事项{i}')
        self.report(kind='running', request_id='support', content='模拟申请协助', need='模拟通信支援')
        view = self.view()
        self.assertIsNone(view.force.needs)
        self.assertEqual('有应急相关记录待核对', view.state)
        self.assertIn('模拟事项0', str(view.signals))
        self.assertIn('模拟通信支援', str(view.signals))
        self.assertIn('模拟电台', str(view.signals))
        self.assertIn('是否解除须核对', str(view.signals))
        html = render_emergency(self.state, 'commander')
        self.assertIn('未知人数不显示为零', html)
        self.assertNotIn('医疗保障：申报到场 0 人', html)

    def test_revision_keeps_old_safety_feedback_only_in_history(self):
        self.start()
        self.report(content='OLD-SAFETY')
        self.ws.revise_command('sop-task', by='模拟部署人', note='模拟改版',
            target='模拟新目标', event_id=self.record.event_id, expected_version=1)
        view = self.view()
        self.assertEqual((), view.signals)
        self.assertEqual((), view.execution.tasks[0].current)
        self.assertIn('OLD-SAFETY', str(view.execution.tasks[0].historical))
        self.assertIn('暂无可核对的本版', str(view.missing))

    def test_untrusted_flow_or_event_never_contributes_signals(self):
        self.start()
        original = self.report(content='PRIVATE-SIGNAL')
        for changes in ({'event_id': 'other'}, {'order_id': 'other'}, {'content_version': 0},
                        {'content_version': True}, {'task_level': 'urgent'}):
            self.ws.command_flows['sop-task'] = replace(original, **changes)
            self.assertEqual((), self.view().signals)
            self.assertNotIn('PRIVATE-SIGNAL', str(self.view()))
        self.assertEqual('依据待核对', emergency_view(self.ws, replace(self.record, event_id='other'), None).state)
        self.ws.command_flows['sop-task'] = original
        self.ws.events[-1].payload['tampered'] = True
        self.assertEqual('依据待核对', self.view().state)
        self.assertEqual((), self.view().execution.tasks)
        self.assertEqual((), self.view().signals)

    def test_closed_or_completed_report_does_not_resolve_support_or_complete_sop(self):
        self.start()
        self.report(kind='all_done', content='模拟完成报告', need='模拟仍需核对交接')
        self.ws.close_command('sop-task', result='completed', by='旧核验人',
            event_id=self.record.event_id, expected_version=1)
        self.assertEqual('有应急相关记录待核对', self.view().state)
        self.assertIn('是否解除须核对', str(self.view().signals))
        self.assertIn('不证明新版指定核验授权', str(self.view().execution.tasks[0].facts))

    def test_medical_communication_arrival_is_not_readiness_or_rescue_capability(self):
        f = self.support_fixture()
        f.add_temp(f.COMM, 'log-comm')
        f.add_temp(f.MED, 'log-medical')
        f.state.login('p03-force-needs-secret')
        f.state.handle_enter_event(f.event_id)
        record = f.state.registry.events[f.event_id]
        view = emergency_view(f.state.workspace, record, f.state.ledger)
        rows = {row.key: row for row in view.force.needs.rows}
        self.assertEqual((1, 0), (rows['communication'].arrived_reported, rows['communication'].standby_reported))
        self.assertEqual((1, 0), (rows['medical'].arrived_reported, rows['medical'].standby_reported))
        html = render_emergency(f.state, 'commander')
        self.assertIn('指定副总指挥：副总指挥', html)
        self.assertIn('岗位分类不等于专业资质', html)
        self.assertIn('需求人数待核定，缺口人数暂不能计算', html)

    def test_invalid_support_projection_does_not_hide_valid_task_feedback(self):
        f = self.support_fixture()
        f.state.login('p03-force-needs-secret')
        f.state.handle_enter_event(f.event_id)
        ws = f.state.workspace
        open_task(ws, f.event_id)
        run_task(ws, f.event_id, 1)
        ws.record_command_receipt('sop-task', kind='personnel_safety', by='模拟组长', source='模拟电台',
            content='VALID-SAFETY', request_id='safety-1', event_id=f.event_id, expected_version=1)
        with patch('panda_brain.sop.personnel_groups.group_people', side_effect=ValueError('bad')):
            view = emergency_view(ws, f.state.registry.events[f.event_id], f.state.ledger)
        self.assertIsNone(view.force.needs)
        self.assertIn('VALID-SAFETY', str(view.signals))

    def test_repeated_reads_and_persistence_do_not_dispatch_or_infer_timeouts(self):
        self.start()
        self.report()
        before = (self.ws.version, [asdict(event) for event in self.ws.events])
        first = self.view()
        with patch.object(self.ws, 'command_overdue_notices', side_effect=AssertionError('unreviewed timeouts')):
            for _ in range(3):
                self.assertEqual(first, self.view())
        self.assertEqual(before, (self.ws.version, [asdict(event) for event in self.ws.events]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.db'
            save_workspace(self.ws, path)
            self.assertEqual(first, emergency_view(load_workspace(path), self.record, None))

    def test_six_answers_escape_report_and_do_not_expand_role_permissions(self):
        self.start()
        self.report(content='<script>PRIVATE</script>')
        html = render_emergency(self.state, 'commander')
        for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
            self.assertIn(label, html)
        self.assertIn('&lt;script&gt;PRIVATE&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE</script>', html)
        self.assertNotIn('<form', html)
        self.assertIn('通用指挥登录不获得调配权限', html)
        self.assertEqual(9, render_sand_page(self.state).count('data-sand-step="'))
        for role in ('member', 'public'):
            self.assertEqual('', render_emergency(self.state, role))
            self.assertNotIn('class="sop-emergency"', render_sand_page(self.state, role=role))


if __name__ == '__main__':
    unittest.main()
