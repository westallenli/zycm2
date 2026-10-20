"""P04-02: event-bound situation facts, provenance and review boundaries."""
import unittest
from dataclasses import replace

from panda_brain import situation_monitor as sm
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain.models import (Assessment, Claim, ClaimStatus, Evidence, IntelItem,
                               InformationGap, InformationGapStatus, SourceTrustLevel)
from panda_brain.sandview import render_sand_page
from panda_brain.sop import situation_view, render_situation
from tests import test_upgrade_sop as early
from tests import test_upgrade_checkin_flow as checkin


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class SituationSopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def add_facts(self):
        self.ws.record_evidence(Evidence.create('ev-1', '模拟气象观测：山口风向变化',
            '演示观测单', SourceTrustLevel.AUTHORIZED))
        self.ws.record_claim(Claim('c-1', '山口风向', '有变化', ClaimStatus.OBSERVED,
            '演示人员报告', 0.5, ('ev-1',)))
        self.ws.record_assessment(Assessment('a-1', '规则参谋', '需核对山口条件',
            ('ev-1',), risks=('局部风未核准',), unknowns=('地形影响待核对',)))

    def change(self, event_id=None):
        return self.ws.record_situation_change(dict(
            event_id=event_id or self.record.event_id,
            kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.REVIEW.value,
            fingerprint='a' * 64, reason='模拟风向资料待复核'))

    def test_evidence_and_analysis_are_linked_without_professional_approval(self):
        self.add_facts()
        view = situation_view(self.ws, self.record)
        rendered = render_situation(self.state, 'commander')
        self.assertIn('ev-1：模拟气象观测', str(view.sections))
        self.assertIn('来源已授权（不等于内容已核实）', rendered)
        self.assertIn('局部风未核准', rendered)
        self.assertIn('地形影响待核对', rendered)
        self.assertEqual('待专业确认', view.state)

    def test_missing_evidence_stays_visible_and_does_not_resolve_gaps(self):
        self.ws.record_assessment(Assessment('a-1', '规则参谋', '缺依据分析', ('lost',)))
        self.ws.record_information_gap(InformationGap('g-1', '地形', '尚无实测', '参谋'))
        view = situation_view(self.ws, self.record)
        self.assertIn('lost（本事件缺失）', str(view.sections))
        self.assertTrue(any('lost' in row for row in view.missing))
        self.assertEqual(InformationGapStatus.OPEN, self.ws.information_gaps['g-1'].status)

    def test_candidate_and_published_map_are_not_sop_completion(self):
        for i, status in enumerate((AnnotationStatus.PENDING_REVIEW, AnnotationStatus.PUBLISHED)):
            self.ws.record_annotation(MapAnnotation(id=f'm-{i}', kind=AnnotationKind.FIRE_POINT,
                title=f'模拟火点{i}', points=(), geo_points=((32.1, 110.2),),
                status=status, event_id=self.record.event_id, is_demo=True, source_ref=('观测原件-1',)))
        view = situation_view(self.ws, self.record)
        self.assertIn('模拟标绘', str(view.sections))
        self.assertIn('观测原件-1', str(view.sections))
        self.assertEqual('待专业确认', view.state)

    def test_cross_event_and_unbound_maps_and_reviews_are_excluded(self):
        for i, event_id in enumerate(('other', '')):
            # Deliberately model old or wrongly attached records, bypass write guards.
            self.ws.annotations[str(i)] = MapAnnotation(id=str(i), kind=AnnotationKind.FIRE_POINT,
                title='PRIVATE-MAP', points=(), geo_points=((32.1, 110.2),), event_id=event_id)
        self.change('other')
        self.ws.situation_reviews['r-x'] = sm.SituationReviewBatch('r-x', 'other',
            'PRIVATE-REVIEW', '2099', '2099', 0, 'b' * 64, summary='PRIVATE-SUMMARY')
        view = situation_view(self.ws, self.record)
        self.assertNotIn('PRIVATE', str(view))
        self.assertNotIn('模拟风向资料待复核', str(view.sections))
        self.assertTrue(any('归属' in text for text in view.missing))

    def test_acknowledged_change_is_still_pending_without_new_writes(self):
        self.add_facts()
        change = self.change()
        self.ws.situation_changes[change.change_id] = replace(change, status='acknowledged')
        version = self.ws.version
        for _ in range(3):
            view = situation_view(self.ws, self.record)
            self.assertEqual('有变化待复核', view.state)
            self.assertIn('已阅', str(view.sections))
        self.assertEqual(version, self.ws.version)
        self.assertEqual('acknowledged', self.ws.situation_changes[change.change_id].status)

    def test_absent_event_mismatch_and_corrupt_chain_never_expose_data(self):
        self.add_facts()
        self.assertEqual('未进入事件', situation_view(self.ws, None).state)
        self.assertEqual((), situation_view(self.ws, replace(self.record, event_id='other')).sections)
        self.ws.events[-1].payload['tampered'] = True
        self.assertEqual('依据待核对', situation_view(self.ws, self.record).state)
        self.assertEqual((), situation_view(self.ws, self.record).sections)

    def test_six_answers_escape_sources_and_preserve_all_parallel_entries(self):
        self.ws.record_intel(IntelItem('i-1', '地形', '<script>PRIVATE-INTEL</script>', '<source>'))
        html = render_sand_page(self.state)
        self.assertIn('&lt;script&gt;PRIVATE-INTEL&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE-INTEL</script>', html)
        section = render_situation(self.state, 'commander')
        for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
            self.assertIn(label, section)
        self.assertEqual(9, html.count('data-sand-step="'))
        self.assertNotIn('<form', section)
        for role in ('member', 'public'):
            self.assertEqual('', render_situation(self.state, role))
            self.assertNotIn('class="sop-situation"', render_sand_page(self.state, role=role))

    def test_real_review_batch_is_not_professional_and_new_data_is_not_covered(self):
        fixture = checkin.CheckinFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, event_id = fixture._demo_event()
        state = fixture.state
        state.handle_event_action(event_id, 'start', duty_token=fixture.tokens[fixture.DUTY])
        state.handle_enter_event(event_id)
        state.workspace.record_intel(IntelItem('i-1', '气象', '模拟观测待专业核对', '演示'))
        state.handle_sand_review_current()
        record = state.registry.events[event_id]
        before = situation_view(state.workspace, record)
        self.assertEqual('待专业确认', before.state)
        self.assertIn('规则汇总，不代表专业意见已确认', str(before.sections))
        state.workspace.record_intel(IntelItem('i-2', '地形', '新模拟资料', '演示'))
        after = situation_view(state.workspace, record)
        self.assertEqual('有变化待复核', after.state)
        self.assertTrue(any('版本晚于' in row for row in after.missing))
        self.assertIn('新模拟资料', str(after.sections))


if __name__ == '__main__':
    unittest.main()
