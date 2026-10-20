"""Current-fire panels reuse named forms; navigating never executes a task."""
import json,unittest
from dataclasses import replace
from http.client import HTTPConnection
from urllib.parse import urlencode
from panda_brain import sop_sand,workflow
from panda_brain.panel import render_page,make_server

class SandWorkTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_sop_progress import SopProgressTests
        self.f=SopProgressTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.state=self.f.state;self.eid=self.f.eid;self.fix=self.f.f
        self.token=self.fix.tokens[self.fix.DEPUTY]
    def query(self,step=3,tool='overview',**changes):return dict(event_id=[self.eid],step=[str(step)],tool=[tool])|changes
    def test_four_guides_reach_original_modules_without_business_writes(self):
        before=self.fix.files();member=self.state.member_session(self.token)
        for step in range(3,7):
            target=workflow.select(self.state,self.token,{k:[v] for k,v in dict(event_id=self.eid,csrf=member.custody_csrf,mode='guide',step=str(step),node='0',target='/workflow/guide').items()})
            self.assertEqual('/workflow/guide',target)
            self.assertNotIn('sand-command-work',workflow.destination(step,0,self.eid))
        self.assertEqual(before,self.fix.files())
    def test_four_readonly_overviews_have_same_action_counts_and_original_sources(self):
        before=self.fix.files()
        for step in range(3,7):
            data=sop_sand.operation(self.state,self.token,self.query(step))
            self.assertEqual(self.eid,data['event_id']);self.assertEqual(step,data['step'])
            self.assertIn('data-assistant-report',data['html'])
            self.assertIn('项待办',data['labels'][step]);self.assertTrue(data['menu'])
        self.assertEqual(before,self.fix.files())
    def test_native_forms_keep_versions_csrf_requests_and_only_current_fire(self):
        before=self.fix.files()
        for tool,route in (('drafts','/sand/task-draft'),('classify','/dispatch/task-review'),('professionals','/dispatch/professionals'),('release','/tasks/release')):
            data=sop_sand.operation(self.state,self.token,self.query(tool=tool));self.assertIn('action="'+route+'"',data['html'])
        data=sop_sand.operation(self.state,self.token,self.query(tool='release'))
        self.assertIn('name="draft_version"',data['html']);self.assertIn('name="request_id"',data['html']);self.assertIn('name="csrf"',data['html'])
        self.assertEqual(before,self.fix.files())
    def test_unknown_duplicate_cross_fire_and_wrong_stage_tools_are_rejected(self):
        before=self.fix.files()
        for q in (self.query(event_id=['foreign']),self.query(step=['3','4']),self.query(tool='../../settings'),self.query(4,'classify'),self.query()|dict(extra=['1'])):
            with self.assertRaises((ValueError,PermissionError)):sop_sand.operation(self.state,self.token,q)
        self.assertEqual(before,self.fix.files())
    def test_captain_and_duty_gain_no_draft_or_publication_authority(self):
        self.f.publish();before=self.fix.files()
        for phone in (self.fix.DUTY,self.fix.SOURCE):
            token=self.fix.tokens[phone]
            data=sop_sand.operation(self.state,token,self.query(tool='drafts'))
            self.assertNotIn('action="/sand/task-draft"',data['html'])
            html=sop_sand.operation(self.state,token,self.query(tool='release'))['html']
            self.assertNotIn('确认正式发布本版',html)
        self.assertEqual(before,self.fix.files())
    def test_current_named_receipt_forms_and_feedback_are_reused(self):
        self.f.publish();before=self.fix.files();token=self.fix.tokens[self.fix.SOURCE]
        html=sop_sand.operation(self.state,token,self.query(tool='receipts'))['html']
        self.assertIn('action="/tasks/formal"',html);self.assertIn(self.f.publication.id,html)
        self.assertIn('name="expected_version"',html)
        for tool in ('control','feedback'):
            data=sop_sand.operation(self.state,token,self.query(4,tool))
            self.assertEqual(tool,data['tool']);self.assertIn(self.fix.draft.tasks[0].spec.title,data['html'])
        self.assertEqual(before,self.fix.files())
    def test_existing_forms_filter_other_events_before_loading_their_workspace(self):
        from panda_brain import formal_task_receipts,formal_task_feedback_page,task_relations_page
        other=self.state.registry.create_event(name='SECRET-OTHER-FIRE',report_no='DEMO-OTHER',discovered_at='2026-10-14T08:00:00+08:00',source='匿名测试',location='其他区域')
        self.state.registry.events[other.event_id]=replace(other,status='closed',snapshot_path='missing-other-snapshot.db')
        for module in (formal_task_receipts,formal_task_feedback_page,task_relations_page):
            html=module.body(self.state,self.token,only_event=self.eid)
            self.assertNotIn('SECRET-OTHER-FIRE',html)
    def test_revoked_deputy_and_closed_event_cannot_read_old_panel(self):
        self.state.registry.events[self.eid]=replace(self.state.registry.events[self.eid],status='closed')
        with self.assertRaises(PermissionError):sop_sand.operation(self.state,self.token,self.query())
    def test_shell_retains_map_context_without_duplicate_work_panel(self):
        before=self.fix.files();status,html=render_page(self.state,'/sand',member_token=self.token)
        self.assertEqual(200,status);self.assertEqual(1,html.count('data-sand-context'))
        self.assertNotIn('data-sand-workspace',html);self.assertNotIn('id="sand-command-work"',html)
        self.assertNotIn('/static/sop-sand.js',html);self.assertNotIn('任务与执行',html)
        self.assertIn('/static/map-brief.js',html);self.assertIn('/static/sand-command.js',html)
        status,public=render_page(self.state,'/sand',member_token=self.token,query={'projector':['1']})
        self.assertNotIn('data-sand-context',public);self.assertEqual(before,self.fix.files())
    def test_post_event_header_rejects_stale_tab_before_original_handler(self):
        import threading
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        before=self.fix.files();conn=HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        try:
            conn.request('POST','/sand/task-draft',body=urlencode(dict(action='save')),headers={'Cookie':'member_session='+self.token,'Content-Type':'application/x-www-form-urlencoded','X-Sand-Event':'foreign'})
            res=conn.getresponse();self.assertEqual(409,res.status);self.assertIn('当前火情已变化',res.read().decode())
        finally:conn.close()
        self.assertEqual(before,self.fix.files())
    def test_work_panel_assets_are_served_locally_and_readonly(self):
        import threading
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        before=self.fix.files()
        for file,kind in (('sop-sand.js','javascript'),('sop-sand.css','text/css')):
            conn=HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            try:
                conn.request('GET','/static/'+file);res=conn.getresponse();self.assertEqual(200,res.status);self.assertIn(kind,res.getheader('Content-Type'));self.assertTrue(res.read())
            finally:conn.close()
        self.assertEqual(before,self.fix.files())
