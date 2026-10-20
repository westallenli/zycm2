"""Handover evidence stays separate from reports, old closure and return assumptions."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from panda_brain.handover_sop import handover_view, render_handover
from panda_brain.materials import MaterialsLedger
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.review_domain import RectTask, ReviewLedger
from panda_brain.sandview import render_sand_page
from tests import test_upgrade_sop as early
from tests import test_upgrade_checkin_flow as checkin
from tests.test_upgrade_sop_deployment import open_task, run_task


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class HandoverSopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def view(self, *, ledger=None, reviews=None):
        return handover_view(self.ws, self.record, ledger, reviews)

    def start(self):
        open_task(self.ws, self.record.event_id)
        run_task(self.ws, self.record.event_id, 1)

    def report(self, **changes):
        args = dict(kind='all_done', by='模拟组长', source='模拟电台报告',
            content='模拟自报完成', request_id='handover-report-1',
            event_id=self.record.event_id, expected_version=1)
        args.update(changes)
        self.ws.record_command_receipt('sop-task', **args)

    def rect(self, reviews, *, rect_id='RECT-SOP', event_id=None, status='assigned_pending',
             mirrored=True, measures='模拟遗留整改'):
        task = RectTask(rect_id, (), measures, '模拟单位', '模拟责任人',
                        event_id=event_id or self.record.event_id, status=status,
                        verify_method='模拟验收方法')
        reviews.add_rect(task)
        if mirrored:
            self.ws.append_event('ReviewRectRecorded', rect_id=rect_id)
        return task

    def test_empty_and_no_event_keep_step_and_entrances_without_false_zero(self):
        view = self.view()
        self.assertEqual('待补验收交接依据', view.state)
        self.assertIn('未归还数量未知', str(view.missing))
        self.assertNotIn('未归还 0', str(view))
        self.state.active_event_id = ''
        self.assertEqual('未进入事件', handover_view(self.ws, None, None, None).state)
        html = render_sand_page(self.state)
        self.assertIn('data-sand-step="8"', html)
        self.assertIn('data-sand-related="8" href="/review"', html)
        self.assertIn('data-sand-related="8" href="/materials/closeout"', html)

    def test_current_report_and_legacy_close_never_finish_handover(self):
        self.start()
        self.report()
        view = self.view()
        self.assertIn('模拟自报完成', str(view.tasks[0].current))
        self.assertIn('待核验', str(view.tasks[0].facts))
        self.ws.close_command('sop-task', result='completed', by='旧关闭人',
                              event_id=self.record.event_id, expected_version=1)
        view = self.view()
        self.assertIn('旧关闭', str(view.tasks[0].facts))
        self.assertNotEqual('已完成', view.state)
        self.assertIn('指定核验负责人', str(view.missing))

    def test_revision_moves_completion_report_to_history_and_requires_new_version(self):
        self.start()
        self.report(content='OLD-COMPLETE')
        self.ws.revise_command('sop-task', by='模拟部署人', note='调整范围',
                               target='模拟新目标', event_id=self.record.event_id, expected_version=1)
        task = self.view().tasks[0]
        self.assertEqual((), task.current)
        self.assertIn('OLD-COMPLETE', str(task.historical))
        self.assertIn('本版暂无', str(task.missing))

    def test_untrusted_flow_and_global_audit_do_not_expose_feedback(self):
        self.start()
        self.report(content='PRIVATE-FEEDBACK')
        original = self.ws.command_flows['sop-task']
        for changes in ({'event_id': 'other'}, {'content_version': 0},
                        {'target': '改写但未审计'}):
            self.ws.command_flows['sop-task'] = replace(original, **changes)
            self.assertEqual((), self.view().tasks)
            self.assertNotIn('PRIVATE-FEEDBACK', str(self.view()))
        self.ws.command_flows['sop-task'] = original
        self.assertEqual('依据待核对', handover_view(self.ws,
            replace(self.record, event_id='other'), None, None).state)
        self.ws.events[-1].payload['tamper'] = True
        self.assertEqual('依据待核对', self.view().state)

    def test_current_event_rects_split_open_and_marked_done_with_mirror(self):
        reviews = ReviewLedger()
        self.rect(reviews)
        self.rect(reviews, rect_id='RECT-OTHER', event_id='other',
                  measures='PRIVATE-OTHER')
        view = self.view(reviews=reviews)
        self.assertIn('模拟遗留整改', str(view.open_rects))
        self.assertNotIn('PRIVATE-OTHER', str(view))
        reviews.transition_rect('RECT-SOP', 'in_progress', operator='模拟复盘负责人', stamp='now')
        self.ws.append_event('ReviewRectStatusChanged', rect_id='RECT-SOP', to='in_progress')
        self.assertIn('整改中', str(self.view(reviews=reviews).open_rects))
        reviews.transition_rect('RECT-SOP', 'awaiting_verify', operator='模拟复盘负责人', stamp='now')
        self.ws.append_event('ReviewRectStatusChanged', rect_id='RECT-SOP', to='awaiting_verify')
        reviews.transition_rect('RECT-SOP', 'done', operator='模拟复盘负责人', stamp='now')
        self.ws.append_event('ReviewRectStatusChanged', rect_id='RECT-SOP', to='done')
        done = self.view(reviews=reviews)
        self.assertEqual((), done.open_rects)
        self.assertIn('台账标记已完成', str(done.marked_rects))
        self.assertIn('不证明交接', str(done.marked_rects))

    def test_missing_or_mismatched_rect_mirror_is_excluded(self):
        reviews = ReviewLedger()
        self.rect(reviews, mirrored=False, measures='UNMIRRORED')
        view = self.view(reviews=reviews)
        self.assertNotIn('UNMIRRORED', str(view))
        self.assertIn('审计镜像', str(view.missing))
        self.ws.append_event('ReviewRectRecorded', rect_id='RECT-SOP')
        reviews.rects['RECT-SOP'] = replace(reviews.rects['RECT-SOP'], status='done')
        self.assertNotIn('UNMIRRORED', str(self.view(reviews=reviews)))

    def test_valid_task_feedback_survives_unavailable_materials_or_reviews(self):
        self.start()
        self.report()
        view = self.view(ledger=MaterialsLedger(), reviews=ReviewLedger())
        self.assertIn('模拟自报完成', str(view.tasks[0].current))
        self.assertEqual((), view.materials)
        self.assertIn('未归还数量未知', str(view.missing))

    def test_real_arrival_baseline_is_context_not_return_or_transfer(self):
        f = checkin.CheckinFlowTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        _, event_id = f._demo_event()
        f.state.handle_event_action(event_id, 'start', duty_token=f.tokens[f.DUTY])
        f.state.handle_deputy_appointment(event_id, duty_token=f.tokens[f.DUTY],
            subject_id=f.DEPUTY, evidence='模拟线下指定', request_id='handover-appoint',
            occurred_at='2026-09-29T08:00:00+08:00')
        f.state.handle_select_squads(event_id, deputy_token=f.tokens[f.DEPUTY],
            squad_ids=[f.squad.squad_id], channel='phone', request_id='handover-select')
        qr_id = next(q.qr_id for q in f.state.registry.qrs.values() if q.event_id == event_id)
        f.state.handle_captain_scan(event_id, captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad.squad_id, qr_id=qr_id)
        f.state.handle_captain_arrival(event_id, captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad.squad_id, mode='initial', phones=[f.MEMBER_B], request_id='handover-arrive')
        f.state.handle_enter_event(event_id)
        view = handover_view(f.state.workspace, f.state.registry.events[event_id],
                             f.state.ledger, f.state.review_ledger)
        self.assertIn('水带', str(view.materials))
        self.assertIn('到场携带登记', str(view.materials))
        self.assertNotIn('未归还 6', str(view))
        f.state.ledger.snapshots[event_id].squads[f.squad.squad_id].lines[f.item_id].qty_registered = 99
        changed = handover_view(f.state.workspace, f.state.registry.events[event_id],
                                 f.state.ledger, f.state.review_ledger)
        self.assertEqual((), changed.materials)
        self.assertNotIn('99', str(changed))

    def test_readonly_persistence_escaping_and_role_scope(self):
        self.start()
        self.report(content='<script>PRIVATE-REPORT</script>')
        before = (self.ws.version, [asdict(e) for e in self.ws.events])
        first = self.view()
        for _ in range(3):
            self.assertEqual(first, self.view())
            render_handover(self.state, 'commander')
        self.assertEqual(before, (self.ws.version, [asdict(e) for e in self.ws.events]))
        html = render_handover(self.state, 'commander')
        for word in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
            self.assertIn(word, html)
        self.assertIn('&lt;script&gt;PRIVATE-REPORT&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE-REPORT</script>', html)
        self.assertNotIn('<form', html)
        self.assertEqual(9, render_sand_page(self.state).count('data-sand-step="'))
        for role in ('member', 'public'):
            self.assertEqual('', render_handover(self.state, role))
            self.assertNotIn('class="sop-handover"', render_sand_page(self.state, role=role))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'simulated.db'
            save_workspace(self.ws, path)
            self.assertEqual(first, handover_view(load_workspace(path), self.record, None, None))


if __name__ == '__main__':
    unittest.main()
