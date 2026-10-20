"""Grounded recommendations are separate from publication and actual feedback."""
import json
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from urllib.parse import urlencode
from unittest.mock import patch

from panda_brain import deployment_recommendations as dr, sop_sand, assistant_view
from panda_brain.panel import make_server, PanelActionError
from panda_brain.task_release import releases
from panda_brain.persistence import save_workspace


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        self.f = TaskReleaseTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.state = self.f.state; self.eid = self.f.event_id; self.token = self.f.tokens[self.f.DEPUTY]
    def values(self, **changes):
        return dict(decision_id=self.f.decision_id, draft_version=self.f.draft.version,
            expected_version=0, expected_basis=dr.basis(self.state, self.state.workspace, self.f.draft),
            action='accept', recipient='', scope='', request_id='recommend-one') | changes
    def write(self, **changes):
        return self.state.handle_deployment_recommendation(self.eid, member_token=self.token, **self.values(**changes))
    def http(self, fields, token=None, header=None):
        server=make_server(self.state, port=0)
        thread=threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            conn=HTTPConnection('127.0.0.1', server.server_port, timeout=15)
            conn.request('POST','/tasks/recommendation',urlencode(fields, doseq=True),
                {'Cookie':'member_session='+(token or self.token),'Content-Type':'application/x-www-form-urlencoded','X-Sand-Event':header or self.eid})
            res=conn.getresponse();body=res.read().decode();status=res.status;conn.close()
            return status,body
        finally:server.shutdown();server.server_close()
    def fields(self, **changes):
        v=self.values();v['basis']=v.pop('expected_basis')
        return dict(event_id=self.eid, csrf=self.state.member_session(self.token).custody_csrf, ack='1', **v)|changes

    def test_readonly_grounded_cards_keep_unknown_location_and_native_editor(self):
        before=self.f.files();html=dr.render(self.state,self.token)
        for text in ('模拟接收补给','按清单核对实际良好数量','未提供，空间任务需补','采纳建议','调整内容','提交专业复核','data-recommend-editor','action="/sand/task-draft"'):
            self.assertIn(text,html)
        self.assertNotIn('data-recommend-map=',html)
        self.assertEqual(before,self.f.files())
        data=sop_sand.operation(self.state,self.token,dict(event_id=[self.eid],step=['3'],tool=['overview']))
        self.assertEqual('drafts',data['default_tool']);self.assertIn(self.f.decision_id,data['recommendation_bases'])
    def test_accept_is_named_idempotent_and_never_publish_receive_or_complete(self):
        args=self.values();before=self.f.files()
        one=self.state.handle_deployment_recommendation(self.eid,member_token=self.token,**args)
        after=self.f.files();two=self.state.handle_deployment_recommendation(self.eid,member_token=self.token,**args)
        self.assertEqual(one,two);self.assertEqual(after,self.f.files())
        self.assertEqual(self.f.DEPUTY,one.actor_id);self.assertEqual(1,len(dr.history(self.f.space())))
        self.assertEqual([],list(releases(self.f.space())))
        self.assertFalse(assistant_view.current(self.state,self.eid)['steps'][3]['complete'])
        self.assertEqual(before[self.state.registry_path],after[self.state.registry_path])
        self.assertEqual(before[self.state.materials_path],after[self.state.materials_path])
        self.assertIn('已采纳本版建议',dr.render(self.state,self.token))
    def test_review_request_is_named_and_visible_to_recipient_without_approval(self):
        self.write(action='review',recipient=self.f.OTHER,scope=self.f.scope)
        space=self.f.space();notice=dr.review_notice(self.state,space,self.f.OTHER)
        self.assertIn('复核请求',notice);self.assertIn(self.f.scope,notice)
        self.assertEqual('',dr.review_notice(self.state,space,self.f.SOURCE))
        self.assertFalse(releases(space))
        from panda_brain.task_release_page import body
        self.assertIn('data-deployment-review-notice',body(self.state,self.f.tokens[self.f.OTHER],only_event=self.eid))
        from panda_brain.panel import render_page
        status,page=render_page(self.state,'/tasks/release',member_token=self.f.tokens[self.f.OTHER])
        self.assertEqual(200,status);self.assertIn('data-deployment-review-notice',page)
        self.assertNotIn('确认正式发布本版',page)
    def test_map_preview_uses_only_unchanged_source_geometry(self):
        from panda_brain.models import Option
        from panda_brain.task_drafts import PlanTaskBlueprint
        shapes=({'geom':'line','pts':((30.12,103.56),(30.122,103.565)),'label':'匿名推演'},)
        self.state.workspace.add_option(Option('mapped-sample','匿名空间方案','匿名来源',(),(),sim_shapes=shapes,
            task_blueprint=PlanTaskBlueprint(main=self.f.draft.tasks[0].spec)))
        self.state.handle_plan_decision('mapped-sample','adopt')
        self.assertIn('data-recommend-map="mapped-sample"',dr.render(self.state,self.token))
        self.state.workspace.options['mapped-sample'].sim_shapes=({'geom':'line','pts':((31.0,104.0),(31.1,104.1))},)
        self.assertNotIn('data-recommend-map="mapped-sample"',dr.render(self.state,self.token))
    def test_source_change_rejects_old_basis_without_writes_and_clears_acceptance(self):
        old=self.values();self.write()
        self.state.registry.events[self.eid]=replace(self.state.registry.events[self.eid],location='已更新现场位置')
        before=self.f.files()
        with self.assertRaisesRegex(ValueError,'资料、职责或草案已变化'):
            self.state.handle_deployment_recommendation(self.eid,member_token=self.token,**(old|dict(expected_version=1,request_id='stale')))
        self.assertEqual(before,self.f.files());self.assertNotIn('已采纳本版建议',dr.render(self.state,self.token))
    def test_draft_revision_invalidates_old_confirmation_and_review_request(self):
        self.write(action='review',recipient=self.f.OTHER,scope=self.f.scope)
        draft=self.f.draft;task=draft.tasks[0]
        self.state.workspace.revise_task_drafts(draft.decision_id,event_id=self.eid,expected_version=1,
            common=draft.common,specs={task.id:replace(task.spec,objective='明确修订目标')})
        save_workspace(self.state.workspace,self.f.root/'snapshots'/(self.eid+'.db'))
        before=self.f.files()
        with self.assertRaisesRegex(ValueError,'草案版本已变化'):self.write()
        self.assertEqual(before,self.f.files());self.assertIn('原请求仅作历史',dr.review_notice(self.state,self.f.space(),self.f.OTHER))
    def test_unassigned_recipient_and_wrong_scope_reject_without_writes(self):
        before=self.f.files()
        for recipient,scope in ((self.f.SOURCE,self.f.scope),(self.f.OTHER,'错误专业')):
            with self.assertRaises(PermissionError):self.write(action='review',recipient=recipient,scope=scope)
        self.assertEqual(before,self.f.files())
    def test_named_personnel_change_invalidates_old_confirmation(self):
        args=self.values();self.write()
        self.state.registry.library.pop(self.f.TARGET)
        before=self.f.files()
        with self.assertRaisesRegex(ValueError,'资料、职责或草案已变化'):
            self.state.handle_deployment_recommendation(self.eid,member_token=self.token,
                **(args|dict(expected_version=1,request_id='missing-verifier')))
        self.assertEqual(before,self.f.files());self.assertNotIn('已采纳本版建议',dr.render(self.state,self.token))
    def test_only_current_named_deputy_and_active_fire_may_write(self):
        before=self.f.files()
        for phone in (self.f.DUTY,self.f.SOURCE,self.f.OTHER):
            with self.assertRaises(PermissionError):
                self.state.handle_deployment_recommendation(self.eid,member_token=self.f.tokens[phone],**self.values())
        self.assertEqual(before,self.f.files())
        self.state.registry.events[self.eid]=replace(self.state.registry.events[self.eid],status='closed')
        with self.assertRaises((ValueError,PermissionError,PanelActionError)):self.write()
    def test_http_csrf_duplicate_ack_cross_event_and_role_fail_closed(self):
        fields=self.fields();before=self.f.files()
        for bad,status in ((fields|dict(csrf='wrong'),403),(fields|dict(ack='0'),409),(fields|dict(action=['accept','review']),409),(fields|dict(extra='x'),409)):
            self.assertEqual(status,self.http(bad)[0])
        self.assertEqual(409,self.http(fields,header='foreign')[0])
        self.assertEqual(403,self.http(fields,token=self.f.tokens[self.f.SOURCE])[0])
        self.assertEqual(before,self.f.files())
        self.assertEqual(303,self.http(fields)[0]);after=self.f.files()
        self.assertEqual(303,self.http(fields)[0]);self.assertEqual(after,self.f.files())
    def test_persist_failure_leaves_live_and_disk_unchanged(self):
        before=self.f.files();events=tuple(self.state.workspace.events)
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('anonymous save failure')):
            with self.assertRaises(OSError):self.write()
        self.assertEqual(events,tuple(self.state.workspace.events));self.assertEqual(before,self.f.files())
    def test_missing_draft_has_honest_empty_state_and_no_fake_plan(self):
        self.state.workspace.task_drafts.clear();before=self.f.files()
        html=dr.render(self.state,self.token)
        self.assertIn('暂不能推荐执行任务',html);self.assertNotIn('/tasks/recommendation',html)
        self.assertEqual(before,self.f.files())
    def test_professional_duty_revocation_marks_request_historical(self):
        self.write(action='review',recipient=self.f.OTHER,scope=self.f.scope)
        self.state.handle_professional_duty(self.eid,deputy_token=self.token,subject_id=self.f.OTHER,
            professional_scope=self.f.scope,active=False,expected_version=1,evidence='匿名撤销依据',reason='职责改变',request_id='revoke-request-duty')
        self.assertIn('原请求仅作历史',dr.review_notice(self.state,self.f.space(),self.f.DEPUTY))
        before=self.f.files()
        with self.assertRaises(PermissionError):self.write(action='review',recipient=self.f.OTHER,scope=self.f.scope,expected_version=1,request_id='revoked')
        self.assertEqual(before,self.f.files())

