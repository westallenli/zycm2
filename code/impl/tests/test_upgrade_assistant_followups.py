"""Automatic information assistance stays scoped and separate from command authority."""
from dataclasses import replace
import json
import unittest
from panda_brain import sop_assistance as sa, assistant_view as ui, workflow
from panda_brain.assistant_followups import Runtime, Journal
from tests.test_upgrade_business_modules import BusinessModulesTests as Fixture


class AssistantFollowupsTests(unittest.TestCase):
    setUp=Fixture.setUp
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,foreign_event,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.foreign_event,Fixture.page

    def enable(self):
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],initial_report='')
        self.state._save_registry()
        path=self.state.registry_path.with_name('assistant-policy.json')
        path.write_text(json.dumps(dict(version=1,automatic_followups=True,channel='in-app')),encoding='utf-8')
        runtime=Runtime(self.state,clock=lambda:10)
        self.state.assistant_runtime=runtime;runtime.tick();return runtime

    def duty_question(self,runtime,key='fire-initial-report'):
        return next(q for q in runtime.questions(self.event_id,recipient=self.DUTY) if q['key']==key)

    def reply_fields(self,q,phone=None,**overrides):
        phone=phone or self.DUTY;member=self.state.member_session(self.tokens[phone])
        values=dict(event_id=self.event_id,csrf=member.custody_csrf,question_id=q['id'],expected_version=str(q['version']),
                    outcome='provided',content='具名补充的初报内容，仅为资料来源。',source='模拟值班记录',occurred_at='')
        values.update(overrides);return {k:[v] for k,v in values.items()}

    def stable_bytes(self):
        return {key:value for key,value in self.bytes().items() if 'assistant-' not in str(key)}

    def test_nine_reports_are_readonly_and_normal_items_have_no_confirmation(self):
        before=self.bytes();result=sa.report(self.state,self.event_id)
        self.assertEqual(list(range(9)),[s['step'] for s in result['steps']])
        self.assertTrue(all(s['source']['event_id']==self.event_id and s['source']['snapshot_sha256'] for s in result['steps']))
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,html=self.page('/workflow/guide',phone)
            self.assertEqual(200,status);self.assertIn('data-assistant-report="0"',html)
            self.assertIn('data-assistant-report="0"',html);self.assertIn('正在协助',html)
            self.assertIn('需要您决定',html);self.assertIn('打开办理入口',html)
            self.assertNotIn('确认核对通过',html)
        self.assertEqual(before,self.bytes())

    def test_background_questions_are_targeted_deduplicated_without_business_writes(self):
        runtime=self.enable();before=self.stable_bytes();entries=len(runtime.journal.entries)
        runtime.last_tick=None;runtime.tick()
        self.assertEqual(entries,len(runtime.journal.entries));self.assertEqual(before,self.stable_bytes())
        q=self.duty_question(runtime);self.assertEqual('pending',q['status']);self.assertEqual('系统内补问',q['channel'])
        for q in runtime.questions(self.event_id):
            self.assertNotEqual('电话',q['channel']);self.assertEqual(self.event_id,q['event_id'])
        self.assertFalse(any(e.kind=='AssistantCommandPublished' for e in self.state.workspace.events))

    def test_personnel_questions_only_route_to_current_event_leader(self):
        runtime=self.enable()
        queries=[q for q in runtime.questions(self.event_id) if q['key'].startswith('readiness:')]
        self.assertTrue(queries)
        self.assertTrue(all(q['recipient']==self.OTHER for q in queries))
        self.assertFalse(any(q['recipient']==self.CAPTAIN for q in queries))
        self.assertFalse(any(q['recipient']==self.MEMBER_A for q in queries))

    def test_only_recipient_can_reply_and_event_csrf_version_are_required(self):
        runtime=self.enable();q=self.duty_question(runtime);before=self.bytes()
        for phone,overrides in ((self.CAPTAIN,{}),(self.DUTY,dict(csrf='bad')),
                               (self.DUTY,dict(event_id='fire-foreign')),(self.DUTY,dict(expected_version='99'))):
            with self.subTest(phone=phone,overrides=overrides):
                with self.assertRaises((PermissionError,ValueError)):
                    runtime.reply(self.tokens[phone],self.reply_fields(q,phone,**overrides))
        fields=self.reply_fields(q);fields['content'].append('duplicate')
        with self.assertRaises(ValueError):runtime.reply(self.tokens[self.DUTY],fields)
        self.assertEqual(before,self.bytes())

    def test_reply_is_sourced_supplement_not_ack_arrival_or_completion(self):
        runtime=self.enable();q=self.duty_question(runtime);before=self.stable_bytes();versions=self.state.workspace.version
        runtime.reply(self.tokens[self.DUTY],self.reply_fields(q))
        self.assertEqual('answered',runtime.journal.questions[q['id']]['status'])
        report=ui.current(self.state,self.event_id)
        finding=next(f for f in report['steps'][0]['findings'] if f['key']==q['key'])
        self.assertEqual('已收到具名补充',finding['status']);self.assertIn('原字段尚未更新',finding['text'])
        self.assertEqual('',self.state.registry.events[self.event_id].initial_report)
        self.assertEqual(before,self.stable_bytes());self.assertEqual(versions,self.state.workspace.version)
        with self.assertRaises(ValueError):runtime.reply(self.tokens[self.DUTY],self.reply_fields(q))

    def test_unavailable_answer_becomes_commander_exception(self):
        runtime=self.enable();q=self.duty_question(runtime)
        runtime.reply(self.tokens[self.DUTY],self.reply_fields(q,outcome='unavailable',content='授权资料目前无法取得'))
        self.assertEqual('escalated',runtime.journal.questions[q['id']]['status'])
        html=self.page('/workflow/guide',self.DEPUTY)[1]
        self.assertIn('相关人员报告无法取得资料',html);self.assertIn('授权资料目前无法取得',html)

    def test_new_source_resolves_information_request_and_restart_retains_history(self):
        runtime=self.enable();q=self.duty_question(runtime)
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],initial_report='新增有效初报')
        self.state._save_registry();runtime.last_tick=None;runtime.tick()
        self.assertEqual('resolved',runtime.journal.questions[q['id']]['status'])
        reopened=Runtime(self.state);self.assertEqual(runtime.journal.entries,reopened.journal.entries)
        self.assertEqual('resolved',reopened.journal.questions[q['id']]['status'])

    def test_revoked_recipient_cannot_reply_and_old_question_is_cancelled(self):
        runtime=self.enable();q=self.duty_question(runtime);self.state.configure_duty_subjects(())
        with self.assertRaises(PermissionError):runtime.reply(self.tokens[self.DUTY],self.reply_fields(q))
        runtime.last_tick=None;runtime.tick();self.assertEqual('cancelled',runtime.journal.questions[q['id']]['status'])
        self.assertFalse(runtime.questions(self.event_id,recipient=self.DUTY))

    def test_corrupt_journal_is_preserved_and_cannot_send_or_accept_replies(self):
        runtime=self.enable();path=runtime.journal.path
        payload=json.loads(path.read_text(encoding='utf-8'));payload['entries'][0]['data']['recipient']=self.CAPTAIN
        path.write_text(json.dumps(payload),encoding='utf-8');before=path.read_bytes()
        damaged=Runtime(self.state);self.assertTrue(damaged.journal.error);damaged.tick()
        self.assertEqual(before,path.read_bytes());self.assertFalse(damaged.questions(self.event_id))
        with self.assertRaises(ValueError):damaged.reply(self.tokens[self.DUTY],self.reply_fields(self.duty_question(runtime)))

    def test_wrong_event_or_damaged_audit_does_not_produce_current_report(self):
        before=self.bytes()
        with self.assertRaises(ValueError):sa.report(self.state,'not-current')
        self.state.workspace.events[0].payload['tampered']='invalid'
        with self.assertRaises(ValueError):sa.report(self.state,self.event_id)
        self.assertEqual(before,self.bytes())

    def test_inbox_hides_other_recipients_and_http_reply_preserves_business(self):
        runtime=self.enable();q=self.duty_question(runtime)
        server=self.server();runtime=self.state.assistant_runtime
        before=self.stable_bytes();status,html=self.page('/workflow/inbox',self.CAPTAIN)
        self.assertEqual(200,status);self.assertNotIn(q['id'],html)
        status,headers,_=self.request(server,'/workflow/assistance-reply',phone=self.DUTY,form={k:v[0] for k,v in self.reply_fields(q).items()})
        self.assertEqual(303,status);self.assertEqual('/workflow/inbox',headers['Location'])
        self.assertEqual(before,self.stable_bytes())

    def test_source_change_rejects_stale_reply_before_next_background_tick(self):
        runtime=self.enable();q=self.duty_question(runtime)
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],initial_report='新来源已补齐')
        self.state._save_registry();before=self.bytes()
        with self.assertRaises(ValueError):runtime.reply(self.tokens[self.DUTY],self.reply_fields(q))
        self.assertEqual(before,self.bytes())

    def test_unavailable_personnel_projection_does_not_resolve_pending_question(self):
        from unittest.mock import patch
        runtime=self.enable()
        q=next(q for q in runtime.questions(self.event_id) if q['key'].startswith('readiness:'))
        view=sa.sop.force_view(self.state.workspace,self.state.registry.events[self.event_id],self.state.ledger)
        with patch.object(sa.sop,'force_view',return_value=replace(view,state='依据待核对',missing=('人员来源暂不可读取',))):
            runtime.last_tick=None;runtime.tick()
        self.assertEqual('pending',runtime.journal.questions[q['id']]['status'])


class FormalTaskAssistanceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        self.f=TaskReleaseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.record();self.f.safety();release=self.f.publish()
        self.state=self.f.state;self.eid=self.f.event_id
        self.oid=json.loads(release.content_json)['tasks'][0]['order_id']
        self.state.registry_path.with_name('assistant-policy.json').write_text(json.dumps(dict(version=1,automatic_followups=True,channel='in-app')),encoding='utf-8')
        self.runtime=Runtime(self.state);self.runtime.tick()

    def test_formal_task_query_names_actual_recipient_and_keeps_version_separate(self):
        q=next(q for q in self.runtime.questions(self.eid) if q['key']=='task-feedback:'+self.oid)
        self.assertEqual(self.f.SOURCE,q['recipient']);self.assertEqual('1',q['scope'])
        self.assertIn('当前内容版本 1',q['question'])
        self.assertFalse(self.f.space().command_flows[self.oid].acked_at)
        self.assertFalse(self.f.space().command_flows[self.oid].closed_at)

    def test_task_query_reply_never_substitutes_formal_receipt(self):
        q=next(q for q in self.runtime.questions(self.eid) if q['key']=='task-feedback:'+self.oid)
        member=self.state.member_session(self.f.tokens[self.f.SOURCE]);before=self.f.files()
        values=dict(event_id=self.eid,csrf=member.custody_csrf,question_id=q['id'],expected_version='1',
            outcome='provided',content='模拟报告已完成',source='模拟来源',occurred_at='')
        self.runtime.reply(self.f.tokens[self.f.SOURCE],{k:[v] for k,v in values.items()})
        self.assertEqual(before,self.f.files());self.assertFalse(self.f.space().command_flows[self.oid].acked_at)

    def test_corrupt_current_flow_does_not_resolve_old_information_request(self):
        q=next(q for q in self.runtime.questions(self.eid) if q['key']=='task-feedback:'+self.oid)
        self.state.workspace.command_flows[self.oid].content_version=99
        result=sa.report(self.state,self.eid);self.runtime.synchronize(result)
        self.assertEqual('pending',self.runtime.journal.questions[q['id']]['status'])

    def test_automatic_preflight_uses_actual_current_professional_records(self):
        before=self.f.files();result=sa.report(self.state,self.eid)
        rule=next(f for f in result['steps'][3]['findings'] if f['key'].startswith('release-conditions:'))
        self.assertEqual('记录匹配',rule['status']);self.assertEqual(before,self.f.files())
        self.state.registry.library.pop(self.f.OTHER)
        result=sa.report(self.state,self.eid)
        rule=next(f for f in result['steps'][3]['findings'] if f['key'].startswith('release-conditions:'))
        self.assertNotEqual('记录匹配',rule['status']);self.assertEqual(before,self.f.files())
