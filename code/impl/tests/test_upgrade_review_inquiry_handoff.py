"""P13 remaining scope: grounded follow-up, viewer boundaries and handoff data."""
import hashlib
import json
import threading
import unittest
from dataclasses import asdict, replace
from http.client import HTTPConnection
from urllib.parse import urlencode

from panda_brain import review_inquiry, review_handoff, review_meeting
from panda_brain.review_domain import RootCauseHypothesis, ReviewIssue, RectTask
from panda_brain.panel import make_server
from panda_brain.reviewview import render_review_page
from tests import test_upgrade_review_reports as fixture


class InquiryTests(unittest.TestCase):
    meeting = fixture.ReportSnapshotTests.meeting
    assessment = fixture.ReportSnapshotTests.assessment

    def setUp(self):
        fixture.ReportSnapshotTests.setUp(self)

    def ask(self, question, viewer='owner', role='commander', **kwargs):
        return self.state.handle_review_ask(question, role, viewer_id=viewer, **kwargs)

    def test_facts_analysis_and_unverified_with_frozen_source_and_no_business_write(self):
        self.assessment('issue', 'issue-a', missing_info='原始时钟未核')
        def persisted():
            return (self.state.workspace.version, self.state.db_path.read_bytes(),
                    self.state.reviews_path.read_bytes() if self.state.reviews_path.exists() else None)
        before = persisted()
        first = self.ask('issue-a 有哪些未核实证据？')
        self.assertTrue(first.facts)
        self.assertIn('原始时钟未核', '；'.join(first.unverified))
        self.assertIn('其他解释', '；'.join(first.analysis))
        source = review_inquiry.source_page(self.state, 'commander', 'owner', first.source_digest, 'issue:issue-a')
        self.assertIn('匿名待核问题', source)
        self.state.review_ledger.issues['issue-a'] = replace(self.state.review_ledger.issues['issue-a'], title='后来修订问题')
        second = self.ask('那依据呢？', follow_up=True)
        self.assertEqual(first.topic, second.topic)
        self.assertIn('后来修订问题', second.conclusion)
        self.assertNotIn('后来修订问题', review_inquiry.source_page(self.state, 'commander', 'owner', first.source_digest, 'issue:issue-a'))
        self.assertEqual(before, persisted())

    def test_verified_causal_layer_and_stale_confirmation_are_not_flattened(self):
        self.state.review_ledger.add_hypothesis(RootCauseHypothesis('hyp-a', '匿名延误线索',
            event_id='fire-a', to_verify=('时序',), resolved_items=('时序',)))
        basis = self.assessment('hypothesis', 'hyp-a')
        self.state.handle_review_hyp_status('hyp-a', 'verified', reason='匿名核实', assessment_ref=basis.id)
        self.assertIn('负责人已核实直接原因', self.ask('根因 hyp-a').conclusion)
        self.assessment('hypothesis', 'hyp-a', expected_object_version='1', request_id='new-basis', missing_info='新证据待核')
        answer = self.ask('根因 hyp-a')
        self.assertNotIn('负责人已核实直接原因', answer.conclusion)
        self.assertIn('旧确认已过期', answer.excluded)
        self.assertNotIn('匿名延误线索', self.ask('根因 hyp-a', role='member', viewer='member').conclusion)

    def test_viewer_event_and_unknown_ids_do_not_borrow_context_or_sources(self):
        owner = self.ask('issue-a 有什么问题？')
        self.ask('本次整改进展如何', viewer='other')
        with self.assertRaises(ValueError):
            review_inquiry.source_page(self.state, 'commander', 'other', owner.source_digest, 'issue:issue-a')
        self.assertNotIn('匿名待核问题', self.ask('那依据呢', viewer='other', follow_up=True).conclusion)
        unknown = self.ask('issue-foreign 有什么问题？')
        self.assertEqual((), unknown.facts)
        self.assertEqual({}, json.loads(unknown.source_snapshot_json)['records'])
        self.assertIn('未找到指定编号', unknown.excluded)
        self.state.active_event_id = 'fire-b'
        with self.assertRaises(ValueError):
            self.ask('有哪些问题')

    def test_command_refusal_and_tampered_revision_fail_closed(self):
        before = self.state.workspace.version
        answer = self.ask('派出队伍执行一下')
        self.assertEqual('command_refused', answer.topic)
        self.assertEqual(before, self.state.workspace.version)
        basis = self.assessment('issue', 'issue-a')
        self.state.workspace.object_revisions[basis.id] = replace(basis, content_json='{"forged":true}')
        with self.assertRaises(ValueError):
            self.ask('有哪些问题')

    def test_task_question_keeps_reported_basis_and_does_not_invent_delay(self):
        from panda_brain.offline_tasks import record_offline, amend_offline
        result = record_offline(self.state.workspace, review_meeting.service(self.state), self.tokens[1],
            event_id='fire-a', content='匿名任务', group_id='', issued_by='匿名线下指挥员',
            recipient_id='13912340001', verifier_id='', occurred_at='2026-01-01T01:00:00+00:00',
            evidence='匿名指令依据', known_progress='尚未核实', request_id='task-one')
        answer = self.ask(result['task_id']+' 任务延误原因和调整依据')
        by_id = self.ask(result['task_id']+' 当前版本与依据是什么？')
        self.assertEqual('tasks', by_id.topic)
        self.assertIn('匿名任务', by_id.conclusion)
        self.assertIn('匿名指令依据', '；'.join(by_id.analysis))
        missing = self.ask('offline-task-missing 当前版本与依据是什么？')
        self.assertEqual((), missing.facts)
        self.assertEqual({}, json.loads(missing.source_snapshot_json)['records'])
        self.assertIn('匿名指令依据', '；'.join(answer.analysis))
        self.assertIn('无明确截止时刻', answer.excluded)
        self.assertIn('不能单独证明延误原因', '；'.join(answer.analysis))
        other = record_offline(self.state.workspace, review_meeting.service(self.state), self.tokens[1],
            event_id='fire-a', content='另一个不能混用的任务', group_id='', issued_by='匿名线下指挥员',
            recipient_id='13912340001', verifier_id='', occurred_at='2026-01-01T01:00:00+00:00',
            evidence='不同指令依据', known_progress='尚未核实', request_id='task-two')
        self.assertNotEqual(result['task_id'], other['task_id'])
        self.assertNotIn('另一个不能混用的任务', self.ask(result['task_id']+' 任务的依据').conclusion)
        amend_offline(self.state.workspace, review_meeting.service(self.state), self.tokens[1],
            event_id='fire-a', task_id=result['task_id'], expected_version=1, content='匿名任务第二版',
            group_id='', recipient_id='13912340001', verifier_id='', occurred_at='2026-01-01T02:00:00+00:00',
            reason='匿名明确变更依据', request_id='task-amend')
        report_id = self.state.handle_report_generate()
        data = review_handoff.package(self.state.review_ledger.reports[report_id], 'fire-a')
        self.assertTrue(any(n['kind']=='offline_task_version' and n['version']==1 for n in data['nodes']))
        second = next(n for n in data['nodes'] if n['kind']=='offline_task_version' and n['version']==2)
        self.assertEqual('匿名任务第二版', second['data']['records'][0]['content'])
        self.assertEqual(1, second['data']['records'][0]['version'])
        self.assertEqual(2, second['data']['records'][0]['next_version'])
        self.assertTrue(any(r['field']=='task_id' and r['resolution']=='resolved' for r in data['relations']))
        member = self.ask('有哪些任务', role='member', viewer='member:13912340002')
        self.assertNotIn('匿名任务', member.conclusion)
        self.assertNotIn('审计 #', self.ask('时间线', role='member', viewer='member:13912340002').conclusion)

    def test_handoff_preserves_snapshot_versions_and_explicit_or_missing_relations(self):
        self.state.review_ledger.add_rect(RectTask('rect-a', ('issue-a', 'missing-reference'),
            '匿名措施', '模拟', '匿名0', event_id='fire-a'))
        rid = self.state.handle_report_generate()
        report = self.state.review_ledger.reports[rid]
        original = review_handoff.package(report, 'fire-a')
        self.meeting()
        after = review_handoff.package(report, 'fire-a')
        self.assertEqual(original['source_snapshot'], after['source_snapshot'])
        canonical = json.dumps(after['source_snapshot'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(report.source_digest, hashlib.sha256(canonical.encode()).hexdigest())
        self.assertTrue(any(r['raw_reference']=='issue-a' and r['resolution']=='resolved' for r in after['relations']))
        self.assertTrue(any(r['raw_reference']=='missing-reference' and r['resolution']=='missing' for r in after['relations']))
        self.assertNotIn(self.tokens[1], json.dumps(after))
        self.assertTrue(any(n['version'] is None for n in after['nodes']))
        with self.assertRaises(ValueError):
            review_handoff.package(report, 'fire-b')


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        fixture.ReportSnapshotTests.setUp(self)
        self.server=make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, fields=None, cookie=None):
        conn=HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if fields is not None else 'GET', path,
                body=urlencode(fields) if fields is not None else None,
                headers={'Cookie': cookie or 'member_session='+self.tokens[1],
                         'X-Review-Ask':'1', 'Content-Type':'application/x-www-form-urlencoded'})
            response=conn.getresponse()
            return response.status,response.read()
        finally:conn.close()

    def test_ask_form_scope_sources_and_viewer_private_history(self):
        fields=dict(event_id='fire-a', csrf=review_meeting.csrf(self.state,self.tokens[1]), question='有哪些问题')
        self.assertEqual(409,self.request('/review/ask',{**fields,'csrf':'wrong'})[0])
        self.assertEqual(409,self.request('/review/ask',{**fields,'event_id':'fire-b'})[0])
        before=self.state.workspace.version
        self.assertEqual(200,self.request('/review/ask',fields)[0])
        answer=self.state.review_qa_log[-1][1]
        source='/review/inquiry-source?'+urlencode(dict(event_id='fire-a',digest=answer.source_digest,ref='issue:issue-a'))
        self.assertEqual(200,self.request(source)[0])
        commander=self.state.login('anonymous-report-secret')
        self.assertEqual(409,self.request(source,cookie='panel_session='+commander)[0])
        self.assertEqual(404,self.request(source,cookie='invalid=none')[0])
        self.assertEqual(before,self.state.workspace.version)
        page=render_review_page(self.state,role='owner',tab='summary',inquiry_viewer='member:13912340002',review_token=self.tokens[1])
        self.assertIn('补充现场情况',page)
        self.assertNotIn('/review/ask',page)
        other=render_review_page(self.state,role='commander',inquiry_viewer='commander')
        self.assertNotIn(answer.source_digest,other)

    def test_json_handoff_download_is_authenticated_event_scoped_and_read_only(self):
        rid=self.state.handle_report_generate()
        url='/export/review?'+urlencode(dict(event_id='fire-a',report_id=rid,format='json'))
        before=self.state.workspace.version
        status,body=self.request(url)
        self.assertEqual(200,status)
        self.assertEqual('panda-review-handoff-v1',json.loads(body)['schema'])
        self.assertEqual(404,self.request(url,cookie='invalid=none')[0])
        self.assertEqual(409,self.request(url+'&event_id=fire-b')[0])
        self.assertEqual(before,self.state.workspace.version)


if __name__ == '__main__':
    unittest.main()
