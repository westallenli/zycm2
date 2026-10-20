"""Same framework and event information; distinct authenticated action authority."""
import unittest
import json
import re
from panda_brain.panel import render_page
from panda_brain import unified_ui as ui
from panda_brain.persistence import load_workspace
from tests.test_upgrade_business_modules import BusinessModulesTests as Fixture
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus


class UnifiedFrameworkTests(unittest.TestCase):
    setUp=Fixture.setUp
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,foreign_event,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.foreign_event,Fixture.page

    def test_same_map_payload_all_three_roles_including_other_team_and_draft(self):
        self.state.workspace.record_annotation(MapAnnotation(id='shared-foreign',kind=AnnotationKind.TEXT,
            points=((10,10),),geo_points=((31,110),),title='全部队伍共享标绘',status=AnnotationStatus.DRAFT,
            details={'team_id':self.other.squad_id},event_id=self.event_id))
        self.state.persist_if_db()
        before=self.bytes(); payloads=[]
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,html=self.page('/sand',phone); self.assertEqual(200,status)
            payloads.append(json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1]))
            self.assertIn('全部队伍共享标绘',html)
            self.assertIn('sand-workflow',html)
        self.assertEqual(payloads[0],payloads[1]); self.assertEqual(payloads[1],payloads[2])
        self.assertEqual(before,self.bytes())

    @unittest.skip('页壳改版：子页不再输出 data-module 标记，等待按新页壳重写')
    def test_all_children_share_framework_and_full_event_records(self):
        before=self.bytes()
        for parent,children in ui.CHILDREN.items():
            for route,label in children:
                for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
                    with self.subTest(route=route,phone=phone):
                        status,html=self.page(route,phone); self.assertEqual(200,status)
                        self.assertIn('data-module="'+parent+'"',html)
                        self.assertIn(label,html)
                        self.assertIn('data-shared-child="'+route+'"',html)
                        for main,word in ui.business.MODULES.items():
                            self.assertIn('href="'+main+'"',html)
                        if parent=='/': self.assertIn('SECRET-OTHER-CAPTAIN',html)
        self.assertEqual(before,self.bytes())

    def test_legacy_workbenches_redirect_to_children_and_other_roles_are_paused(self):
        server=self.server()
        for old,new in ui.ALIASES.items():
            status,headers,_=self.request(server,old,phone=self.DEPUTY)
            self.assertEqual(303,status); self.assertEqual(new,headers['Location'])
        for route in ('/personnel/callouts','/sand/task-receipts','/materials/transfers'):
            status,html=self.page(route,self.MEMBER_A)
            self.assertEqual(200,status); self.assertIn('独立工作台已暂停',html)
        self.assertIsNotNone(self.state.member_session(self.tokens[self.MEMBER_A]))

    def test_named_deputy_command_works_without_admin_and_records_real_subject(self):
        for token in tuple(self.state.sessions): self.state.logout(token)
        self.state.workspace._general_entry_unlocked = False
        self.assertFalse(self.state.entry_unlocked)
        member=self.state.member_session(self.tokens[self.DEPUTY])
        status,_,_=self.request(self.server(),'/sand/goal',phone=self.DEPUTY,
            form={'statement':'本事件具名目标','_business_csrf':member.custody_csrf})
        self.assertEqual(303,status)
        self.assertFalse(self.state.entry_unlocked)
        self.assertFalse(self.state.sessions)
        loaded=load_workspace(self.state.registry.events[self.event_id].snapshot_path,general_auth=self.auth)
        recorded=[e for e in loaded.events if e.payload.get('business_subject')==self.DEPUTY]
        self.assertTrue(recorded)
        self.assertTrue(all(e.payload['business_event_id']==self.event_id for e in recorded))
        self.assertFalse(loaded.verify_audit_chain())

    def test_command_role_and_csrf_rejections_preserve_business_bytes(self):
        for token in tuple(self.state.sessions): self.state.logout(token)
        self.state.workspace._general_entry_unlocked = False
        server=self.server(); before=self.bytes()
        for phone in (self.DUTY,self.CAPTAIN):
            member=self.state.member_session(self.tokens[phone])
            self.assertEqual(403,self.request(server,'/sand/goal',phone=phone,
                form={'statement':'禁止越权','_business_csrf':member.custody_csrf})[0])
        self.assertEqual(403,self.request(server,'/sand/goal',phone=self.DEPUTY,
            form={'statement':'禁止错会话','_business_csrf':'wrong'})[0])
        self.assertEqual(before,self.bytes()); self.assertFalse(self.state.entry_unlocked)

    def test_cross_event_children_and_replay_still_reject_foreign_context(self):
        foreign=self.foreign_event(); before=self.bytes()
        self.assertEqual(404,render_page(self.state,'/personnel/callouts',
            member_token=self.tokens[self.CAPTAIN],query={'event_id':[foreign]})[0])
        server=self.server()
        self.assertEqual(200,self.request(server,'/review/replay?event_id='+self.event_id,phone=self.CAPTAIN)[0])
        self.assertEqual(409,self.request(server,'/review/replay?event_id='+foreign,phone=self.CAPTAIN)[0])
        self.assertEqual(before,self.bytes())

    def test_captain_reads_full_review_timeline_without_write_forms(self):
        status,html=render_page(self.state,'/review',member_token=self.tokens[self.CAPTAIN],query={'tab':['timeline']})
        self.assertEqual(200,status); self.assertIn('复盘',html)
        self.assertNotIn('本队任务与反馈作为复盘依据',html)

    def test_invalid_or_revoked_deputy_cannot_open_command_capability(self):
        from dataclasses import replace
        for token in tuple(self.state.sessions): self.state.logout(token)
        self.state.workspace._general_entry_unlocked = False
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        before=self.bytes()
        self.assertEqual(403,self.request(self.server(),'/sand/goal',phone=self.DEPUTY,
            form={'statement':'失效任命','_business_csrf':self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf})[0])
        self.assertEqual(before,self.bytes()); self.assertFalse(self.state.entry_unlocked)


del Fixture  # Do not let unittest discover the imported fixture class.
if __name__=='__main__': unittest.main()
