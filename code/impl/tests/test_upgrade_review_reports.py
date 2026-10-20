"""P13-02: anonymous report provenance, stale evidence gates and P12 content."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.authorization import Role
from panda_brain.fire_events import FireEventRecord
from panda_brain.models import PersonRecord, ExecutionReport, AttemptStatus
from panda_brain.panel import PanelState, PanelActionError
from panda_brain.review_domain import (
    ReviewLedger, ReviewReport, ReviewIssue, RootCauseHypothesis, RectTask, Experience,
)
from panda_brain import review_meeting, review_causality, review_reports
from panda_brain.reviewview import export_review_markdown, render_review_page
from panda_brain.workspace import ScenarioWorkspace


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        auth = GeneralEntryAuth(secret='anonymous-report-secret')
        self.state = PanelState(
            ScenarioWorkspace('anonymous-report', event_id='fire-a', general_auth=auth),
            auth=auth, registry_path=root/'registry.json',
            materials_path=root/'materials.json', reviews_path=root/'reviews.json',
            db_path=root/'state.db')
        self.state.active_event_id = 'fire-a'
        self.state.registry.events['fire-a'] = FireEventRecord(
            'fire-a', '匿名模拟', 'anon', '2026-01-01T00:00:00+00:00', '模拟', '未知', status='closed')
        phones = ('13912340001', '13912340002')
        self.state.registry.import_person_library([
            PersonRecord(name=f'匿名{i}', phone=phone, unit='模拟')
            for i, phone in enumerate(phones)])
        self.state.configure_duty_subjects((phones[0],))
        self.tokens = [self.state.member_login(phone, '123456')[0] for phone in phones]
        review_meeting.service(self.state).record_initial_appointment(
            self.tokens[0], event_id='fire-a', role=Role.DEPUTY,
            subject_id=phones[1], evidence='匿名指定', request_id='deputy')
        self.state.handle_review_owner_assign(
            phones[1], deputy_token=self.tokens[1], evidence='匿名指定', request_id='owner')
        self.state._pending_review_actor = ('owner', '匿名1', 'fire-a', self.tokens[1])
        self.state.review_ledger.add_issue(ReviewIssue(
            'issue-a', '匿名待核问题', '信息', '态势', 'fire-a', '模拟', '匿名事实',
            evidence_ids=('audit#1',), to_verify='原始时钟待核'))

    def generate(self):
        return self.state.handle_report_generate()

    def submit(self, rid):
        report = self.state.review_ledger.reports[rid]
        self.state.handle_report_edit(rid, report.content_md + '\n人工会议补充：保留待核项。')
        self.state.handle_report_status(rid, 'pending_review')

    def meeting(self, request='meeting-one'):
        return review_meeting.record_note(self.state, self.tokens[1], dict(
            subject='匿名会议', provided_by='匿名0', feedback='原始时钟待核',
            evidence_refs='evt-001', occurred_at='', request_id=request))

    def assessment(self, kind, target, **changes):
        meeting = self.meeting()
        fields = dict(target_kind=kind, target_id=target, support_refs='audit#1',
                      counter_refs='', counter_note='暂无反证', alternatives='也可能输入迟到',
                      missing_info='', verify_targets='匿名原始记录', meeting_ref=meeting.id,
                      meeting_finding='匿名会议已核', causal_layer='direct',
                      expected_object_version='0', request_id='basis-'+kind)
        fields.update(changes)
        return review_causality.record_assessment(self.state, self.tokens[1], fields)

    def test_source_survives_report_lifecycle_and_unresolved_items_can_publish(self):
        self.state.review_ledger.add_rect(RectTask(
            'rect-a', ('issue-a',), '仍在整改', '模拟', '匿名0', event_id='fire-a', status='in_progress'))
        rid = self.generate()
        original = self.state.review_ledger.reports[rid]
        source = json.loads(original.source_snapshot_json)
        self.assertEqual('fire-a', source['event_id'])
        self.assertIn('issue-a', source['review']['issues'])
        self.assertNotIn(self.tokens[1], original.source_snapshot_json)
        self.submit(rid)
        self.assertEqual('', review_reports.freshness_error(self.state, self.state.review_ledger.reports[rid]))
        self.state.handle_report_status(rid, 'published')
        published = ReviewLedger.load(self.state.reviews_path).reports[rid]
        self.assertEqual(original.source_snapshot_json, published.source_snapshot_json)
        self.assertEqual('复盘负责人·匿名1', published.reviewed_by)
        self.assertTrue(published.reviewed_at)
        self.assertEqual('pending', self.state.review_ledger.issues['issue-a'].review_status)
        self.assertEqual('in_progress', self.state.review_ledger.rects['rect-a'].status)
        page = render_review_page(self.state, role='owner', tab='report', selected_report=rid)
        self.assertIn('生成时间', page)
        self.assertIn(published.source_digest, self.state.review_ledger.reports[rid].source_digest)

    def test_new_meeting_after_submission_requires_new_version_without_overwrite(self):
        rid = self.generate()
        self.submit(rid)
        old = self.state.review_ledger.reports[rid]
        self.meeting()
        before = self.state.workspace.version
        with self.assertRaisesRegex(PanelActionError, '新版本'):
            self.state.handle_report_status(rid, 'published')
        self.assertEqual(before, self.state.workspace.version)
        self.assertEqual(old, self.state.review_ledger.reports[rid])
        newer = self.generate()
        self.submit(newer)
        self.state.handle_report_status(newer, 'published')
        self.assertIn('原始时钟待核', self.state.review_ledger.reports[newer].content_md)
        self.assertNotEqual(old.source_digest, self.state.review_ledger.reports[newer].source_digest)
        self.assertEqual(old, ReviewLedger.load(self.state.reviews_path).reports[rid])

    def test_source_detects_business_changes_but_excludes_other_events(self):
        rid = self.generate()
        self.submit(rid)
        report = self.state.review_ledger.reports[rid]
        self.state.review_ledger.add_issue(ReviewIssue(
            'foreign', '他事件不可见', '信息', '态势', 'fire-b', '模拟', '外部'))
        self.assertEqual('', review_reports.freshness_error(self.state, report))
        self.assertNotIn('他事件不可见', report.source_snapshot_json)
        self.state.review_ledger.issues['issue-a'] = replace(
            self.state.review_ledger.issues['issue-a'], description='后补业务事实')
        with self.assertRaisesRegex(PanelActionError, '资料或证据已变化'):
            self.state.handle_report_status(rid, 'published')

    def test_old_missing_snapshot_and_tampered_snapshot_cannot_publish(self):
        old = ReviewReport('old', 'fire-a', 1, status='pending_review', content_md='旧版原文')
        self.state.review_ledger.add_report(old)
        with self.assertRaisesRegex(PanelActionError, '缺少来源快照'):
            self.state.handle_report_status('old', 'published')
        rid = self.generate()
        self.submit(rid)
        report = self.state.review_ledger.reports[rid]
        self.state.review_ledger.reports[rid] = replace(report, source_snapshot_json='{}')
        with self.assertRaisesRegex(PanelActionError, '校验失败'):
            self.state.handle_report_status(rid, 'published')

    def test_permission_phase_and_superseded_version_gates(self):
        self.state._pending_review_actor = None
        with self.assertRaises(PermissionError):
            self.generate()
        self.state._pending_review_actor = ('owner', '匿名1', 'fire-a', self.tokens[1])
        event = self.state.registry.events['fire-a']
        self.state.registry.events['fire-a'] = replace(event, status='active')
        with self.assertRaisesRegex(PanelActionError, '事件结束'):
            self.generate()
        self.state.registry.events['fire-a'] = event
        rid = self.generate()
        self.submit(rid)
        self.generate()
        with self.assertRaisesRegex(PanelActionError, '更新的报告版本'):
            self.state.handle_report_status(rid, 'published')
        self.state.review_ledger.reports['foreign'] = ReviewReport('foreign', 'fire-b', 1)
        with self.assertRaisesRegex(PanelActionError, '不属于当前事件'):
            self.state.handle_report_status('foreign', 'published')

    def test_p12_fields_and_current_causal_status_are_preserved(self):
        issue_basis = self.assessment('issue', 'issue-a')
        self.state.handle_review_issue_status('issue-a', 'confirmed', reason='匿名核实', assessment_ref=issue_basis.id)
        self.state.review_ledger.add_hypothesis(RootCauseHypothesis(
            'hyp-a', '模拟输入延迟', event_id='fire-a', related_issues=('issue-a',),
            to_verify=('时序',), resolved_items=('时序',)))
        cause_basis = self.assessment('hypothesis', 'hyp-a')
        self.state.handle_review_hyp_status('hyp-a', 'verified', reason='匿名核实原因', assessment_ref=cause_basis.id)
        self.state.review_ledger.add_rect(RectTask(
            'rect-a', ('issue-a',), '模拟措施', '模拟单位', '匿名0', event_id='fire-a',
            source_hyp_id='hyp-a', source_assessment_refs=(cause_basis.id,),
            acceptance_standard='匿名验收标准', verifier_name='匿名验收人',
            feedback_records=({'provided_by': '匿名责任人', 'content': '模拟完成报告', 'kind': 'completed'},),
            verification_records=({'verifier': '匿名验收人', 'finding': '模拟未通过说明', 'outcome': 'failed'},)))
        self.state.review_ledger.add_experience(Experience(
            'exp-a', '匿名经验', '模拟条件', 'fire-a', evidence_ids=('audit#1',),
            limitations='不可外推', condition_tags=('主题=模拟值',), counter_refs=('audit#1',)))
        self.state.workspace.reports['feedback-a'] = ExecutionReport(
            'feedback-a', 'order-a', AttemptStatus.SUCCEEDED, ('匿名任务反馈',))
        plan = review_meeting.record_note(self.state, self.tokens[1], dict(
            subject='匿名计划比较', provided_by='匿名0', feedback='原始计划待核',
            evidence_refs='evt-001', occurred_at='', request_id='plan', record_type='plan',
            planned_at='2026-01-01T01:00:00+00:00'))
        review_meeting.record_note(self.state, self.tokens[1], dict(
            subject='匿名计划比较', provided_by='匿名0', feedback='实际报告待核',
            evidence_refs='evt-001', occurred_at='2026-01-01T01:05:00+00:00',
            request_id='actual', record_type='actual', plan_reference=plan.id))
        text = export_review_markdown(self.state)
        for value in ('负责人已核实直接原因', '也可能输入迟到',
                      '匿名验收标准', '模拟完成报告', '模拟未通过说明', '未通过，退回整改',
                      '不可外推', '主题=模拟值', '会议补充', '匿名核实原因',
                      '匿名任务反馈', '时间差 +5 分钟'):
            self.assertIn(value, text)
        self.assertIn(cause_basis.id,self.state.workspace.object_revisions)
        self.assertIn(plan.id,self.state.workspace.object_revisions)
        self.assessment('hypothesis', 'hyp-a', expected_object_version='1', request_id='new-basis',
                        missing_info='新增缺口')
        text = export_review_markdown(self.state)
        self.assertIn('旧确认已过期，待重新核实', text)
        self.assertNotIn('负责人已核实直接原因', text)


if __name__ == '__main__':
    unittest.main()
