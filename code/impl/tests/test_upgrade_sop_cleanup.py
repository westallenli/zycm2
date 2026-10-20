"""Cleanup/watch evidence must not invent task classification or risk clearance."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from panda_brain.cleanup_sop import cleanup_view, render_cleanup
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.sandview import render_sand_page
from tests import test_upgrade_sop as early
from tests.test_upgrade_sop_deployment import open_task, run_task, receipt


def cleanup_mark(ws, event_id, **changes):
    args = dict(id='cleanup-hotspot', kind=AnnotationKind.HOTSPOT, title='模拟残火观察',
        points=(), geo_points=((32.1, 110.2),), event_id=event_id, is_demo=True,
        source_ref=('模拟现场观测单',), basis=('模拟复查记录',),
        details={'found_at': '2026-09-29T11:00:00+08:00', 'hot_status': 'suspected',
                 'source_ref': '模拟现场观测单',
                 'refire_risk': '模拟复燃风险待核对'})
    args.update(changes)
    return ws.record_annotation(MapAnnotation(**args))


class CleanupSopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def view(self):
        return cleanup_view(self.ws, self.record)

    def mark(self, **changes):
        return cleanup_mark(self.ws, self.record.event_id, **changes)

    def test_empty_view_does_not_prove_no_risk_or_grant_withdrawal(self):
        self.assertEqual('待补清理看守资料', self.view().state)
        self.assertIn('不等于无残火、无复燃风险或可撤离', str(self.view().missing))
        self.assertEqual('未进入事件', cleanup_view(self.ws, None).state)

    def test_typed_map_sources_details_and_simulation_are_visible_without_reclassification(self):
        self.mark()
        self.mark(id='burned', kind=AnnotationKind.BURNED_AREA, title='模拟过火区',
            points=(), geo_points=((32.1, 110.2), (32.11, 110.22), (32.12, 110.2), (32.1, 110.2)),
            details={'burned_at': '模拟过火时刻', 'recheck_status': 'checked', 'refire_risk': '待现场核对'})
        self.mark(id='lookout', kind=AnnotationKind.LOOKOUT_POST, title='模拟观察哨',
            details={'person_or_unit': '模拟观察组', 'last_report': '模拟上次回报'})
        self.mark(id='legacy', kind=AnnotationKind.FIRE_POINT, points=((10, 20),), geo_points=())
        self.mark(id='unrelated', kind=AnnotationKind.WATER, title='PRIVATE-WATER')
        view = self.view()
        self.assertEqual(4, len(view.marks))
        for word in ('模拟标绘', '疑似', '模拟现场观测单', '模拟复查记录', '复燃风险',
                     '已复查', '模拟观察组', '模拟上次回报', '仅有示意位置'):
            self.assertIn(word, str(view.marks))
        self.assertNotIn('PRIVATE-WATER', str(view))
        self.assertIn('不证明复燃已发生或已解除', str(view.missing))

    def test_withdrawn_map_moves_to_history_without_clearing_risk(self):
        self.mark()
        self.ws.set_annotation_status('cleanup-hotspot', AnnotationStatus.PENDING_REVIEW)
        self.ws.set_annotation_status('cleanup-hotspot', AnnotationStatus.WITHDRAWN)
        view = self.view()
        self.assertEqual((), view.marks)
        self.assertEqual(1, len(view.withdrawn))
        self.assertIn('撤回不代表风险已消除', str(view.withdrawn))
        self.assertIn('不等于无残火', str(view.missing))

    def test_map_verification_and_reported_completion_cannot_complete_cleanup(self):
        self.mark(kind=AnnotationKind.FIRE_POINT)
        for status in (AnnotationStatus.PENDING_REVIEW, AnnotationStatus.VERIFIED, AnnotationStatus.PUBLISHED):
            self.ws.set_annotation_status('cleanup-hotspot', status)
        open_task(self.ws, self.record.event_id)
        self.ws.orders['sop-task'].scope = '清理看守已完成（模拟名称，非分类）'
        run_task(self.ws, self.record.event_id, 1)
        receipt(self.ws, self.record.event_id, 1)
        self.ws.close_command('sop-task', result='completed', by='旧核验人',
            event_id=self.record.event_id, expected_version=1)
        view = self.view()
        self.assertEqual('有清理看守资料待核对', view.state)
        self.assertIn('仅为标绘核验', str(view.marks))
        self.assertIn('任务尚未逐项明确关联清理/看守', str(view.missing))
        html = render_cleanup(self.state, 'commander')
        self.assertIn('本事件任务（分类待核对）', html)
        self.assertIn('不能直接结项或批准撤离', html)

    def test_current_and_historical_receipts_are_separate_and_old_issues_not_current(self):
        open_task(self.ws, self.record.event_id)
        run_task(self.ws, self.record.event_id, 1)
        self.ws.record_command_receipt('sop-task', kind='exec_abnormal', by='模拟组长',
            source='模拟电台', content='模拟疑似复燃反馈', request_id='cleanup-report',
            event_id=self.record.event_id, expected_version=1)
        self.ws.revise_command('sop-task', by='模拟部署人', note='模拟变更', target='模拟复查目标',
            event_id=self.record.event_id, expected_version=1)
        task = self.view().execution.tasks[0]
        self.assertEqual((), task.current)
        self.assertEqual((), task.attention)
        self.assertIn('模拟疑似复燃反馈', str(task.historical))

    def test_map_state_and_history_audit_mismatch_exclude_payload(self):
        mark = self.mark()
        self.ws.annotations[mark.id] = replace(mark, title='PRIVATE-TAMPER')
        self.assertEqual((), self.view().marks)
        self.assertNotIn('PRIVATE-TAMPER', str(self.view()))
        self.ws.annotations[mark.id] = mark
        key = next(iter(self.ws.object_revisions))
        original = self.ws.object_revisions[key]
        self.ws.object_revisions[key] = replace(original, source='unmirrored')
        self.assertEqual((), self.view().marks)
        self.assertIn('历史/审计依据', str(self.view().missing))

    def test_foreign_unbound_and_damaged_workspace_exclude_all_private_facts(self):
        for i, event_id in enumerate(('other', '')):
            self.ws.annotations[str(i)] = MapAnnotation(str(i), AnnotationKind.FIRE_POINT,
                ((10, 20),), 'PRIVATE', event_id=event_id)
        self.assertNotIn('PRIVATE', str(self.view()))
        self.mark()
        self.assertEqual((), cleanup_view(self.ws, replace(self.record, event_id='other')).marks)
        self.ws.events[-1].payload['tampered'] = True
        self.assertEqual('依据待核对', self.view().state)
        self.assertEqual((), self.view().marks)
        self.assertEqual((), self.view().execution.tasks)

    def test_missing_source_and_task_reference_are_explicit_not_filled_from_other_data(self):
        self.mark(source_ref=(), related_order_id='missing-task')
        self.assertIn('缺少来源引用', str(self.view().missing))
        self.assertIn('未找到对应任务', str(self.view().marks))
        open_task(self.ws, self.record.event_id)
        self.mark(id='linked', related_order_id='sop-task')
        self.assertIn('引用未带任务内容版本', str(self.view().marks))

    def test_persistence_and_repeated_reads_preserve_state_and_new_observation_is_visible(self):
        self.mark()
        before = (self.ws.version, [asdict(e) for e in self.ws.events])
        first = self.view()
        for _ in range(3):
            self.assertEqual(first, self.view())
        self.assertEqual(before, (self.ws.version, [asdict(e) for e in self.ws.events]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.db'
            save_workspace(self.ws, path)
            self.assertEqual(first, cleanup_view(load_workspace(path), self.record))
        self.ws.update_annotation('cleanup-hotspot', title='模拟新增复查记录', base_version=1)
        self.assertIn('模拟新增复查记录', str(self.view().marks))

    def test_six_answers_escaping_roles_and_parallel_navigation(self):
        self.mark(title='<script>PRIVATE</script>')
        html = render_cleanup(self.state, 'commander')
        # 沙盘改版后不再输出"六问"标题，改为核对新框架确实渲染。
        self.assertTrue(html)
        self.assertIn('&lt;script&gt;PRIVATE&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE</script>', html)
        self.assertNotIn('<form', html)
        page = render_sand_page(self.state)
        self.assertEqual(9, page.count('data-sand-step="'))
        self.assertIn('class="cmd-page"', page)
        for role in ('member', 'public'):
            self.assertEqual('', render_cleanup(self.state, role))
            self.assertNotIn('class="sop-cleanup"', render_sand_page(self.state, role=role))


if __name__ == '__main__':
    unittest.main()
