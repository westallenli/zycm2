"""P12-01 anonymous single-event evidence review and meeting handoff."""

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from panda_brain.auth import GeneralEntryAuth
from panda_brain.authorization import Role
from panda_brain.fire_events import FireEventRecord
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.persistence import load_workspace
from panda_brain.review_domain import ReviewIssue, RootCauseHypothesis, ReviewLedger
from panda_brain import review_meeting, review_causality, review_closure
from panda_brain.reviewview import render_review_page
from panda_brain.workspace import ScenarioWorkspace


class CausalAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        auth = GeneralEntryAuth(secret='anonymous-test')
        self.state = PanelState(
            ScenarioWorkspace('anonymous', event_id='fire-a', general_auth=auth),
            auth=auth, registry_path=root/'registry.json',
            materials_path=root/'materials.json', reviews_path=root/'reviews.json',
            db_path=root/'state.db')
        self.state.active_event_id = 'fire-a'
        self.state.registry.events['fire-a'] = FireEventRecord(
            'fire-a', '匿名模拟', 'anon', '2026-01-01T00:00:00+00:00', '模拟', '未知', status='closed')
        self.phones = ('13912340001', '13912340002', '13912340003')
        self.state.registry.import_person_library([
            PersonRecord(name=f'匿名{i}', phone=phone, unit='模拟')
            for i, phone in enumerate(self.phones)])
        self.state.configure_duty_subjects((self.phones[0],))
        self.tokens = [self.state.member_login(phone, '123456')[0] for phone in self.phones]
        review_meeting.service(self.state).record_initial_appointment(
            self.tokens[0], event_id='fire-a', role=Role.DEPUTY,
            subject_id=self.phones[1], evidence='匿名指定', request_id='deputy')
        self.state.handle_review_owner_assign(
            self.phones[2], deputy_token=self.tokens[1],
            evidence='匿名会议', request_id='owner')
        self.state.review_ledger.add_issue(ReviewIssue(
            'issue-a', '匿名问题候选', '信息', '态势', 'fire-a',
            '模拟规则', '待核事实', evidence_ids=('audit#1',)))
        self.state.review_ledger.add_hypothesis(RootCauseHypothesis(
            'hyp-a', '匿名关联假设', event_id='fire-a', related_issues=('issue-a',)))
        self.state.review_ledger.add_issue(ReviewIssue(
            'issue-b', '他事件问题', '信息', '态势', 'fire-b', '模拟', '不得显示'))
        self.state._save_reviews()

    def fields(self, **changes):
        fields = dict(target_kind='issue', target_id='issue-a', support_refs='audit#1',
                      counter_refs='', counter_note='暂无反证，待进一步核对',
                      alternatives='也可能是模拟输入延迟', missing_info='原始采集时刻未核',
                      verify_targets='模拟队长及值班记录', meeting_ref='', meeting_finding='',
                      expected_object_version='0', request_id='assessment-one')
        fields.update(changes)
        return fields

    def confirmed_pair(self):
        self.state._pending_review_actor = ('owner', '匿名2', 'fire-a', self.tokens[2])
        self.state.review_ledger.hypotheses['hyp-a'] = replace(
            self.state.review_ledger.hypotheses['hyp-a'], to_verify=('核对时序',))
        self.state.review_ledger.resolve_verification(
            'hyp-a', '核对时序', '匿名记录支持', operator='匿名2', stamp='2026-01-01T00:00:00+00:00')
        meeting = review_meeting.record_note(self.state, self.tokens[2], dict(
            subject='匿名证据核对', provided_by='匿名0', feedback='仅模拟资料核对',
            evidence_refs='evt-001', occurred_at='', request_id='closure-meeting'))
        for kind, target in (('issue', 'issue-a'), ('hypothesis', 'hyp-a')):
            basis = review_causality.record_assessment(self.state, self.tokens[2], self.fields(
                target_kind=kind, target_id=target, request_id='closure-'+kind,
                meeting_ref=meeting.id, meeting_finding='模拟依据已核对', missing_info='',
                causal_layer='direct' if kind == 'hypothesis' else 'undetermined'))
            if kind == 'issue':
                self.state.handle_review_issue_status(target, 'confirmed', reason='人工核对匿名依据',
                                                      assessment_ref=basis.id)
            else:
                self.state.handle_review_hyp_status(target, 'verified', reason='人工核对匿名依据',
                                                    assessment_ref=basis.id, operator='匿名2')

    def published_rect(self):
        self.confirmed_pair()
        task = review_closure.create_rect_draft(self.state, self.tokens[2], 'issue-a', 'hyp-a')
        return review_closure.publish_rect(self.state, self.tokens[2], task.rect_id,
            measures='核对匿名输入记录并记录复核结果', owner_unit='模拟', owner_person='匿名0',
            responsible_phone=self.phones[0], deadline='2026-12-31',
            acceptance_standard='原始记录与复核记录逐项一致', verifier_name='匿名1',
            verifier_phone=self.phones[1])

    def test_owner_writes_immutable_versions_and_meeting_remains_pending(self):
        first = review_causality.record_assessment(self.state, self.tokens[2], self.fields())
        self.assertEqual('pending_verification', json.loads(first.content_json)['status'])
        self.assertEqual(first, review_causality.record_assessment(self.state, self.tokens[2], self.fields()))
        meeting = review_meeting.record_note(self.state, self.tokens[2], dict(
            subject='匿名问题核实', provided_by='模拟队长', feedback='仅报告另一解释',
            evidence_refs='evt-001', occurred_at='', request_id='meeting-assessment'))
        second = review_causality.record_assessment(self.state, self.tokens[2], self.fields(
            expected_object_version='1', request_id='assessment-two',
            meeting_ref=meeting.id, meeting_finding='会议报告仍需对照原始记录',
            counter_refs=meeting.id, counter_note='提供人与原始资料不一致，待核'))
        self.assertEqual(2, second.reference.version)
        self.assertEqual(first.content_json, load_workspace(self.state.db_path).object_revisions[first.id].content_json)
        self.assertEqual(second.id, review_causality.assessments(self.state)['issue:issue-a'].id)
        page = render_review_page(self.state, role='owner', tab='analysis')
        self.assertIn('核实记录（2）', page)
        self.assertIn(first.id, self.state.workspace.object_revisions)
        self.assertEqual('pending', self.state.review_ledger.issues['issue-a'].review_status)
        self.assertEqual('pending', self.state.review_ledger.hypotheses['hyp-a'].status)

    def test_cross_event_and_unresolved_references_rejected_without_write(self):
        before = self.state.workspace.version
        for changes in (dict(target_id='issue-b'), dict(support_refs='made-up'),
                        dict(counter_refs='workspace:invented'),
                        dict(meeting_ref='foreign', meeting_finding='核实'),
                        dict(meeting_finding='无会议依据'),
                        dict(alternatives='')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review_causality.record_assessment(self.state, self.tokens[2], self.fields(**changes))
        self.assertEqual(before, self.state.workspace.version)
        self.assertNotIn('他事件问题', render_review_page(self.state, role='owner', tab='analysis'))

    def test_named_owner_only_and_failed_save_do_not_publish(self):
        before = self.state.workspace.version
        for token in (None, self.tokens[0], self.tokens[1]):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                review_causality.record_assessment(self.state, token, self.fields())
        with patch('panda_brain.persistence.save_workspace', side_effect=OSError('模拟磁盘失败')):
            with self.assertRaises(OSError):
                review_causality.record_assessment(self.state, self.tokens[2], self.fields())
        self.assertEqual(before, self.state.workspace.version)

    def test_stale_version_and_reused_request_rejected(self):
        review_causality.record_assessment(self.state, self.tokens[2], self.fields())
        before = self.state.workspace.version
        with self.assertRaises(ValueError):
            review_causality.record_assessment(self.state, self.tokens[2], self.fields(
                request_id='different', alternatives='新解释'))
        with self.assertRaises(ValueError):
            review_causality.record_assessment(self.state, self.tokens[2], self.fields(
                alternatives='篡改'))
        self.assertEqual(before, self.state.workspace.version)

    def test_unbound_workspace_does_not_offer_assessment(self):
        self.state.workspace.event_id = ''
        page = render_review_page(self.state, role='owner', tab='analysis')
        self.assertIn('请先进入已绑定的本事件任务空间', page)
        self.assertNotIn('action="/review/assessment"', page)
        with self.assertRaises(ValueError):
            review_causality.record_assessment(self.state, self.tokens[2], self.fields())

    def test_owner_conclusion_requires_latest_supported_meeting_assessment(self):
        self.state._pending_review_actor = ('owner', '匿名2', 'fire-a', self.tokens[2])
        first = review_causality.record_assessment(self.state, self.tokens[2], self.fields())
        with self.assertRaises(ValueError):
            review_causality.decision_basis(self.state, 'issue', 'issue-a', first.id,
                                            '会议判断', positive=True)
        meeting = review_meeting.record_note(self.state, self.tokens[2], dict(
            subject='问题核实', provided_by='模拟队长', feedback='仅报告，不自动认定责任',
            evidence_refs='evt-001', occurred_at='', request_id='decision-meeting'))
        second = review_causality.record_assessment(self.state, self.tokens[2], self.fields(
            expected_object_version='1', request_id='assessment-two',
            missing_info='', meeting_ref=meeting.id, meeting_finding='核对原始记录后确认该问题'))
        with self.assertRaises(ValueError):
            review_causality.decision_basis(self.state, 'issue', 'issue-a', first.id,
                                            '旧版', positive=True)
        self.state.handle_review_issue_status('issue-a', 'confirmed', reason='已核对支持与其他解释',
                                              assessment_ref=second.id)
        self.assertTrue(review_causality.current_confirmation(self.state, 'issue', 'issue-a'))
        third = review_causality.record_assessment(self.state, self.tokens[2], self.fields(
            expected_object_version='2', request_id='assessment-three',
            missing_info='', meeting_ref=meeting.id, meeting_finding='出现新反证待复核',
            counter_refs=meeting.id))
        self.assertFalse(review_causality.current_confirmation(self.state, 'issue', 'issue-a'))
        self.state.handle_review_issue_status('issue-a', 'confirmed', reason='已重新核对反证',
                                              assessment_ref=third.id)
        self.assertTrue(review_causality.current_confirmation(self.state, 'issue', 'issue-a'))
        self.assertEqual(2, len(self.state.review_ledger.issues['issue-a'].decision_history))

    def test_root_cause_cannot_be_confirmed_with_unresolved_verification(self):
        self.state._pending_review_actor = ('owner', '匿名2', 'fire-a', self.tokens[2])
        self.state.review_ledger.hypotheses['hyp-a'] = replace(
            self.state.review_ledger.hypotheses['hyp-a'], to_verify=('核对时序',))
        meeting = review_meeting.record_note(self.state, self.tokens[2], dict(
            subject='根因核实', provided_by='模拟队长', feedback='待核对时序',
            evidence_refs='evt-001', occurred_at='', request_id='root-meeting'))
        basis = review_causality.record_assessment(self.state, self.tokens[2], self.fields(
            target_kind='hypothesis', target_id='hyp-a', request_id='root-assessment',
            missing_info='', meeting_ref=meeting.id, meeting_finding='会议补充核对',
            causal_layer='direct'))
        with self.assertRaises(ValueError):
            self.state.review_ledger.transition_hypothesis(
                'hyp-a', 'verified', operator='匿名负责人', stamp='2026-01-01T00:00:00+00:00')
        with self.assertRaises(Exception):
            self.state.handle_review_hyp_status('hyp-a', 'verified', reason='仅同期出现',
                                                assessment_ref=basis.id, operator='匿名负责人')
        self.state.review_ledger.resolve_verification(
            'hyp-a', '核对时序', '原始记录支持', operator='匿名负责人', stamp='2026-01-01T00:00:00+00:00')
        self.state.handle_review_hyp_status('hyp-a', 'verified', reason='时序、反证和其他解释已核对',
                                            assessment_ref=basis.id, operator='匿名负责人')
        self.assertTrue(review_causality.current_confirmation(self.state, 'hypothesis', 'hyp-a'))

    def test_http_form_requires_owner_and_event_bound_csrf(self):
        server = make_server(self.state, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        def post(token, changes=None):
            form = {**self.fields(), 'event_id': 'fire-a',
                    'csrf': review_meeting.csrf(self.state, token),
                    'expected_version': str(self.state.workspace.version),
                    **(changes or {})}
            request = Request(base+'/review/assessment', data=urlencode(form).encode(),
                              headers={'Cookie': 'member_session='+token})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, response.read().decode()
            except HTTPError as exc:
                try:
                    return exc.code, exc.read().decode()
                finally:
                    exc.close()
        self.assertEqual(403, post(self.tokens[1])[0])
        self.assertEqual(403, post(self.tokens[2], {'csrf': 'wrong'})[0])
        result = post(self.tokens[2])
        self.assertEqual(200, result[0], result[1])
        self.assertIn('核实结果及判断理由', render_review_page(self.state, role='owner', tab='analysis'))
        self.assertEqual(1, review_causality.assessments(self.state)['issue:issue-a'].reference.version)

    def test_rect_requires_named_feedback_and_independent_verification(self):
        task = self.published_rect()
        self.assertEqual('in_progress', task.status)
        self.assertEqual(('issue-a',), task.related_ids)
        with self.assertRaises(ValueError):
            review_closure.record_rect_verification(self.state, self.tokens[2], task.rect_id,
                verifier='匿名1', outcome='passed', finding='通过', evidence='audit#1')
        with self.assertRaises(PermissionError):
            review_closure.record_rect_feedback(self.state, self.tokens[0], task.rect_id,
                provided_by='匿名0', duty='模拟', content='已完成', evidence='audit#1', completed=True)
        review_closure.record_rect_feedback(self.state, self.tokens[2], task.rect_id,
            provided_by='匿名0', duty='模拟责任人', content='已逐项核对', evidence='audit#1', completed=True)
        self.assertEqual('awaiting_verify', self.state.review_ledger.rects[task.rect_id].status)
        with self.assertRaises(PermissionError):
            review_closure.record_rect_verification(self.state, self.tokens[0], task.rect_id,
                verifier='匿名1', outcome='passed', finding='通过', evidence='audit#1')
        review_closure.record_rect_verification(self.state, self.tokens[2], task.rect_id,
            verifier='匿名1', outcome='failed', finding='仍有一项未核', evidence='audit#1')
        self.assertEqual('in_progress', self.state.review_ledger.rects[task.rect_id].status)
        review_closure.record_rect_feedback(self.state, self.tokens[2], task.rect_id,
            provided_by='匿名0', duty='模拟责任人', content='已补核', evidence='audit#1', completed=True)
        review_closure.record_rect_verification(self.state, self.tokens[2], task.rect_id,
            verifier='匿名1', outcome='passed', finding='逐项符合标准', evidence='audit#1')
        final = self.state.review_ledger.rects[task.rect_id]
        self.assertEqual('closed', final.status)
        self.assertEqual(2, len(final.verification_records))
        self.assertEqual('closed', self.state.registry.events['fire-a'].status)
        reloaded = ReviewLedger.load(self.state.reviews_path).rects[task.rect_id]
        self.assertEqual(final.source_assessment_refs, reloaded.source_assessment_refs)
        self.assertEqual(final.feedback_records, reloaded.feedback_records)
        self.assertEqual(final.verification_records, reloaded.verification_records)
        self.assertIn('action="/review/exp-propose"', render_review_page(
            self.state, role='owner', tab='exp', review_token=self.tokens[2]))

    def test_experience_versions_conditions_and_counterexample(self):
        task = self.published_rect()
        review_closure.record_rect_feedback(self.state, self.tokens[2], task.rect_id,
            provided_by='匿名0', duty='模拟', content='完成', evidence='audit#1', completed=True)
        review_closure.record_rect_verification(self.state, self.tokens[2], task.rect_id,
            verifier='匿名1', outcome='passed', finding='符合标准', evidence='audit#1')
        exp = review_closure.propose_experience(self.state, self.tokens[2],
                                                source_kind='rect', source_id=task.rect_id)
        with self.assertRaises(ValueError):
            review_closure.decide_experience(self.state, self.tokens[2], exp.exp_id,
                                             target='confirmed', reason='尚无条件')
        exp = review_closure.revise_experience(self.state, self.tokens[2], exp.exp_id,
            content='匿名输入须二次复核', applicability='出现采集时间差时',
            limitations='资料来源无法追溯时不适用', condition_tags='采集时间差=是',
            evidence='audit#1', reason='补足人工核对条件')
        self.assertEqual(2, exp.version)
        review_closure.decide_experience(self.state, self.tokens[2], exp.exp_id,
                                         target='confirmed', reason='人工核对来源与限制')
        self.assertEqual('confirmed', self.state.review_ledger.experiences[exp.exp_id].status)
        with patch('panda_brain.observation_context.current_context', return_value=dict(
                rows=[dict(claim=dict(subject='采集时间差', value='是', status='verified'), evidence=[dict(evidence_id='anon')])],
                revision=10)):
            self.state.registry.events['fire-b'] = FireEventRecord(
                'fire-b', '另一个匿名模拟', 'anon', '2026-01-02T00:00:00+00:00', '模拟', '未知')
            self.state.active_event_id = 'fire-b'
            self.state.workspace.event_id = 'fire-b'
            recs = review_closure.recommendations(self.state)
            self.assertEqual(1, len(recs))
            self.assertTrue(recs[0]['matched'])
        with patch('panda_brain.observation_context.current_context', return_value=dict(
                rows=[dict(claim=dict(subject='采集时间差', value='否', status='verified'), evidence=[dict(evidence_id='anon')])],
                revision=11)):
            recs = review_closure.recommendations(self.state)
            self.assertFalse(recs[0]['matched'])
            self.assertIn('当前 否', recs[0]['missing_tags'][0])
        self.state.active_event_id = 'fire-a'
        self.state.workspace.event_id = 'fire-a'
        review_closure.decide_experience(self.state, self.tokens[2], exp.exp_id,
            target='expired', reason='模拟反例出现', evidence='audit#1')
        self.assertEqual('expired', self.state.review_ledger.experiences[exp.exp_id].status)
        self.assertTrue(self.state.review_ledger.experiences[exp.exp_id].counter_refs)
        self.assertEqual('expired', ReviewLedger.load(self.state.reviews_path).experiences[exp.exp_id].status)

    def test_bound_legacy_rect_and_experience_routes_are_blocked(self):
        server = make_server(self.state, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        for route in ('rect-new', 'rect-status', 'exp-new', 'exp-status'):
            with self.subTest(route=route):
                form = dict(event_id='fire-a', csrf=review_meeting.csrf(self.state, self.tokens[2]),
                            expected_version=str(self.state.workspace.version),
                            content='绕过候选', measures='绕过整改')
                request = Request(base+'/review/'+route, data=urlencode(form).encode(),
                                  headers={'Cookie': 'member_session='+self.tokens[2]})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=5)
                self.assertEqual(409, caught.exception.code)
                caught.exception.close()
        self.assertFalse(self.state.review_ledger.rects)
        self.assertFalse(self.state.review_ledger.experiences)


if __name__ == '__main__':
    unittest.main()
