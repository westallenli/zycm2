"""Behavior checks for the approved five-step review, not template snapshots."""
from dataclasses import asdict, replace
from html.parser import HTMLParser
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from tests import test_upgrade_review_closure as closure_tests
from panda_brain import review_flow as flow, review_meeting as meeting, review_causality as causal, review_closure as closure
from panda_brain.reviewview import render_review_page, export_review_markdown
from panda_brain.review_domain import ReviewLedger
from panda_brain.panel import make_server


class Visible(HTMLParser):
    def __init__(self,text):super().__init__();self.text=[];self.feed(text)
    def handle_data(self,text):self.text.append(text)


class FakeProvider:
    def __init__(self,callback):self.callback=callback;self.prompts=[]
    def complete(self,**kwargs):self.prompts.append(kwargs);return self.callback()


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        closure_tests.CausalAssessmentTests.setUp(self)
        self.token=self.tokens[2]
        self.state.workspace.append_event('FieldObservation',event_id='fire-a',
            statement='模拟：饮水已收到，但餐食仍需补给',recorded_at='2026-10-17T01:00:00+00:00')
        self.state.persist_if_db()
    def value(self):
        return dict(overview='模拟饮水完成，餐食保障需继续核对',findings=[dict(
            title='模拟餐食仍有缺口',facts='记录报告仍需餐食补给',impact='餐食是否影响作业待核',
            cause='可能交接遗漏，尚未证实',alternatives='也可能新增加了需求',missing='核对交接原文',
            recommendation='逐项列清需求并指定承接人',acceptance='每项需求有承接及验收记录',
            evidence=[self.state.workspace.version],counter=[])])
    def analysis(self):
        return flow.analyze(self.state,self.token,provider=FakeProvider(lambda:json.dumps(self.value(),ensure_ascii=False)))
    def confirmed_issue(self):
        row=self.analysis()
        return flow.decide(self.state,self.token,dict(analysis_ref=row.id,finding='0',decision='confirmed',reason='核对原始报告，仅确认餐食缺口存在，原因继续核实'))
    def test_model_output_persists_with_frozen_inputs_and_never_confirms(self):
        before=[asdict(e) for e in self.state.workspace.events]
        row=self.analysis();data=json.loads(row.content_json)
        self.assertEqual(before,[asdict(e) for e in self.state.workspace.events[:len(before)]])
        self.assertEqual('inferred',row.data_kind);self.assertEqual('model',data['origin'])
        self.assertEqual(1,len(data['findings']));self.assertFalse(flow.stale(self.state,row))
        self.assertNotIn('flow-',','.join(self.state.review_ledger.issues))
        self.assertFalse(self.state.workspace.verify_audit_chain())
    def test_invalid_or_foreign_model_citations_cannot_write(self):
        for mutate in (lambda v:v.update(findings='bad'),lambda v:v['findings'][0].update(evidence=[999]),lambda v:v['findings'][0].update(evidence=[])):
            value=self.value();mutate(value);before=self.state.workspace.version
            with self.assertRaises(ValueError):flow.analyze(self.state,self.token,provider=FakeProvider(lambda:json.dumps(value)))
            self.assertEqual(before,self.state.workspace.version)
        with self.assertRaises(ValueError):flow.analyze(self.state,self.token,provider=FakeProvider(lambda:'not json'))
    def test_new_facts_and_logout_reject_inflight_model_result(self):
        value=self.value()
        def changed():
            self.state.workspace.append_event('FieldObservation',event_id='fire-a',statement='模拟新回报')
            return json.dumps(value)
        with self.assertRaisesRegex(ValueError,'变化'):flow.analyze(self.state,self.token,provider=FakeProvider(changed))
        self.assertIsNone(flow.latest(self.state))
    def test_no_model_or_wrong_owner_is_explicit_and_no_fallback(self):
        before=self.state.workspace.version
        with self.assertRaisesRegex(ValueError,'尚未配置'):flow.analyze(self.state,self.token)
        with self.assertRaises(PermissionError):flow.analyze(self.state,self.tokens[0])
        self.assertEqual(before,self.state.workspace.version)
    def test_demo_network_permission_is_only_credentialed_model_context(self):
        from panda_brain import runtime_access as access
        with patch.object(access,'_runtime_enabled',True):
            self.assertFalse(access.permit_model_socket_event('socket.getaddrinfo',('api.deepseek.com',443)))
            mark=access._permit.set(frozenset({'1.1.1.1'}))
            try:
                self.assertTrue(access.permit_model_socket_event('socket.getaddrinfo',('api.deepseek.com',443)))
                self.assertTrue(access.permit_model_socket_event('socket.connect',(None,('1.1.1.1',443))))
                self.assertFalse(access.permit_model_socket_event('socket.connect',(None,('8.8.8.8',443))))
                self.assertFalse(access.permit_model_socket_event('socket.getaddrinfo',('example.com',443)))
            finally:access._permit.reset(mark)
    def test_model_experience_remains_candidate_until_owner_confirms(self):
        issue_id=self.confirmed_issue()
        from panda_brain import model_connections as models
        self.state.sand_deputy_models[(self.token,'fire-a')]=models.Session(cloud=models.profile('deepseek','','deepseek-chat','test-only',True))
        response=json.dumps(dict(content='模拟：按品类逐项核对保障需求',applicability='模拟多品类保障',limitations='仅限模拟，不替代现场核实'))
        with patch('panda_brain.model_connections.SafeProvider.complete',return_value=response):
            flow.dispatch(self.state,self.token,'flow-exp-analyze',dict(source_kind='issue',source_id=issue_id))
        exp=next(iter(self.state.review_ledger.experiences.values()))
        self.assertEqual('candidate',exp.status);self.assertEqual(issue_id,exp.source_issue_id)
        self.assertIn('不替代现场',exp.limitations)
    def test_failed_decision_save_rolls_back_all_files_and_memory(self):
        row=self.analysis();before=self.state.workspace.version
        original=self.state.reviews_path.read_bytes()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('模拟写入失败')):
            with self.assertRaises(OSError):flow.decide(self.state,self.token,dict(analysis_ref=row.id,finding='0',decision='confirmed',reason='核实'))
        self.assertEqual(before,self.state.workspace.version)
        self.assertEqual(original,self.state.reviews_path.read_bytes())
    def test_problem_confirmation_does_not_confirm_cause_and_can_rectify(self):
        issue_id=self.confirmed_issue();saved=flow.link(self.state,issue_id)
        self.assertTrue(causal.current_confirmation(self.state,'issue',issue_id))
        self.assertEqual('pending',self.state.review_ledger.hypotheses[saved['hyp_id']].status)
        flow.dispatch(self.state,self.token,'flow-rect-draft',dict(issue_id=issue_id))
        task=next(t for t in self.state.review_ledger.rects.values() if t.source_issue_id==issue_id)
        self.assertEqual('',task.source_hyp_id);self.assertIn('逐项',task.measures)
        self.assertEqual('draft',task.status)
        with self.assertRaisesRegex(ValueError,'已有'):flow.dispatch(self.state,self.token,'flow-rect-draft',dict(issue_id=issue_id))
    def test_stale_analysis_cannot_be_accepted(self):
        row=self.analysis();self.state.workspace.append_event('FieldObservation',event_id='fire-a',statement='新增资料')
        with self.assertRaisesRegex(ValueError,'新资料'):flow.decide(self.state,self.token,dict(analysis_ref=row.id,finding='0',decision='confirmed',reason='核对'))
    def test_reason_confirmation_keeps_missing_until_human_verifies(self):
        issue_id=self.confirmed_issue();hyp_id=flow.link(self.state,issue_id)['hyp_id']
        fields=dict(kind='hypothesis',target_id=hyp_id,source=str(flow.sources(self.state)[-1]['sequence']),
            decision='confirmed',reason='模拟逐项核对交接原文与人员报告',missing='仍缺交接原文',layer='direct')
        with self.assertRaises(ValueError):flow.confirm_existing(self.state,self.token,fields)
        fields['missing']='';flow.confirm_existing(self.state,self.token,fields)
        self.assertTrue(causal.current_confirmation(self.state,'hypothesis',hyp_id))
    def test_full_rectification_failed_acceptance_and_experience_report_chain(self):
        issue_id=self.confirmed_issue();flow.dispatch(self.state,self.token,'flow-rect-draft',dict(issue_id=issue_id))
        task=next(t for t in self.state.review_ledger.rects.values() if t.source_issue_id==issue_id)
        flow.dispatch(self.state,self.token,'flow-rect-publish',dict(rect_id=task.rect_id,
            responsible_phone=self.phones[0],verifier_phone=self.phones[1],deadline='2026-10-31',
            measures='模拟逐项核对餐食需求，指定承接人',acceptance_standard='每项有负责人和原始回执'))
        for outcome in ('failed','passed'):
            closure.record_rect_feedback(self.state,self.token,task.rect_id,provided_by='匿名0',duty='模拟清单维护',
                content='模拟已核对',evidence=flow.sources(self.state)[-1]['reference'],completed=True)
            closure.record_rect_verification(self.state,self.token,task.rect_id,verifier='匿名1',outcome=outcome,
                finding='模拟逐项核验',evidence=flow.sources(self.state)[-1]['reference'])
            self.assertEqual('in_progress' if outcome=='failed' else 'closed',self.state.review_ledger.rects[task.rect_id].status)
        exp=closure.propose_experience(self.state,self.token,source_kind='rect',source_id=task.rect_id)
        flow.dispatch(self.state,self.token,'flow-exp-revise',dict(exp_id=exp.exp_id,content='模拟多品类需求应逐项落实',
            applicability='模拟多品类保障',limitations='仅模拟验证，现场另核',reason='核对验收记录',evidence=exp.evidence_ids[0]))
        closure.decide_experience(self.state,self.token,exp.exp_id,target='confirmed',reason='模拟核对来源及限制')
        self.state._pending_review_actor=('owner','匿名2','fire-a',self.token)
        rid=self.state.handle_report_generate();report=self.state.review_ledger.reports[rid]
        self.assertIn('六、待核实',report.content_md);self.assertIn('可能交接遗漏',report.content_md)
        self.assertIn('验收未通过',report.content_md);self.assertIn('验收通过',report.content_md)
        self.assertNotIn('revision-',report.content_md)
        self.assertEqual('confirmed',ReviewLedger.load(self.state.reviews_path).experiences[exp.exp_id].status)
    def test_five_steps_no_opaque_fields_and_member_data_is_hidden(self):
        self.confirmed_issue()
        for tab in ('timeline','issues','rect','exp','report','root','analysis','summary'):
            page=render_review_page(self.state,role='owner',tab=tab,review_token=self.token)
            self.assertEqual(5,sum(page.count(f'<small>{i}</small>') for i in range(1,6)))
            visible=''.join(Visible(page).text)
            for old in ('共性规律','趋势统计','置信度','相关·','revision-','HYP-','支持证据编号'):
                # Exact raw payload only appears within intentionally expanded
                # original-record details on the timeline, not the analysis UI.
                if tab not in ('timeline','summary'):self.assertNotIn(old,visible)
        member=render_review_page(self.state,role='member',tab='timeline')
        self.assertNotIn('餐食',member);self.assertNotIn('<form',member)
    def test_http_owner_csrf_version_and_model_failure(self):
        self.state.registry.events['fire-a']=replace(self.state.registry.events['fire-a'],snapshot_path=str(self.state.db_path))
        server=make_server(self.state,port=0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        def post(token,**changes):
            fields=dict(event_id='fire-a',csrf=meeting.csrf(self.state,token),expected_version=str(self.state.workspace.version))
            fields.update(changes)
            request=Request(base+'/review/flow-analyze',data=urlencode(fields).encode(),headers={'Cookie':'member_session='+token})
            return urlopen(request)
        for token,changes,status in ((self.tokens[0],{},403),(self.token,{'csrf':'bad'},403),
                (self.token,{'expected_version':'-1'},409),(self.token,{},409)):
            with self.assertRaises(HTTPError) as caught:post(token,**changes)
            self.assertEqual(status,caught.exception.code,(changes,caught.exception.read().decode()[:500]))
        with patch('panda_brain.model_connections.SafeProvider.complete',return_value=json.dumps(self.value())):
            from panda_brain import model_connections as models
            self.state.sand_deputy_models[(self.token,'fire-a')]=models.Session(cloud=models.profile('deepseek','','deepseek-chat','test-only',True))
            with post(self.token) as response:self.assertEqual(200,response.status)
        self.assertIsNotNone(flow.latest(self.state))
