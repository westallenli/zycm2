"""Navigation preferences remain separate from event grants and business state."""
import json
import re
import unittest
from dataclasses import replace
from panda_brain.panel import render_page
from panda_brain import workflow
from tests.test_upgrade_business_modules import BusinessModulesTests as Fixture


class WorkflowModesTests(unittest.TestCase):
    setUp=Fixture.setUp
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,foreign_event,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.foreign_event,Fixture.page

    def fields(self,phone=None,**overrides):
        phone=phone or self.DEPUTY
        member=self.state.member_session(self.tokens[phone])
        data=dict(event_id=self.event_id,csrf=member.custody_csrf,mode='guide',step='2',node='1',target='/personnel/arrangements')
        data.update(overrides)
        return data

    def test_single_appointed_event_login_landing_shows_both_large_choices(self):
        before=self.bytes(); original=self.state.workspace
        html=self.page('/events',self.DEPUTY)[1]
        self.assertIn('data-workflow-choice',html)
        self.assertIn('九步走引导',html);self.assertIn('功能直达',html)
        self.assertIn(self.event_id,html)
        self.assertNotIn('另一模拟事件',html)
        self.assertIs(original,self.state.workspace);self.assertEqual(before,self.bytes())

    def test_multiple_appointments_require_event_selection_before_choices(self):
        foreign=self.foreign_event()
        self.state.handle_deputy_appointment(foreign,duty_token=self.tokens[self.DUTY],subject_id=self.DEPUTY,
            evidence='第二事件模拟任命',request_id='workflow-second-appointment',occurred_at='2026-10-13T12:00:00+08:00')
        html=self.page('/events',self.DEPUTY)[1]
        self.assertNotIn('data-workflow-choice',html)
        member=self.state.member_session(self.tokens[self.DEPUTY])
        status,headers,_=self.request(self.server(),'/events/select',phone=self.DEPUTY,
            form=dict(event_id=foreign,csrf=member.custody_csrf))
        self.assertEqual(303,status);self.assertEqual('/workflow',headers['Location'])
        self.assertIn(foreign,self.page('/workflow',self.DEPUTY)[1])

    def test_selection_and_cross_module_reading_do_not_write_business_or_arrival(self):
        before=self.bytes(); member=self.state.member_session(self.tokens[self.DEPUTY]);original=self.state.workspace
        status,headers,_=self.request(self.server(),'/workflow/mode',phone=self.DEPUTY,form=self.fields())
        self.assertEqual(303,status);self.assertEqual('/personnel/arrangements',headers['Location'])
        self.assertEqual(dict(mode='guide',step=2,node=1),member.workflow_preferences[self.event_id])
        for route in ('/workflow/guide','/','/sand','/materials','/review','/personnel/groups','/sand/task-feedback'):
            status,html=self.page(route,self.DEPUTY);self.assertEqual(200,status)
            self.assertIn('data-workflow-mode="guide"',html)
            self.assertIn('03 · 力量调用',html)
            self.assertIs(original,self.state.workspace)
        self.assertEqual(before,self.bytes());self.assertFalse(member.onboarded)

    def test_all_nine_steps_and_actions_return_to_real_existing_features(self):
        member=self.state.member_session(self.tokens[self.DEPUTY])
        before=self.bytes()
        for i in range(9):
            workflow.select(self.state,self.tokens[self.DEPUTY],{k:[v] for k,v in self.fields(step=str(i),node='0',target='/workflow/guide').items()})
            html=self.page('/workflow/guide',self.DEPUTY)[1]
            self.assertEqual(9,html.count('class="workflow-step'))
            self.assertIn(f'data-workflow-current="{i}"',html)
            self.assertIn('本页不自动判定完成',html)
        self.assertEqual('/personnel/arrangements',workflow.destination(2,1))
        self.assertEqual('/sand/task-release',workflow.destination(3,2))
        self.assertEqual('/sand#cmd-advisor',workflow.destination(1,0))
        self.assertEqual('/events/'+self.event_id+'/details',workflow.destination(0,0,self.event_id))
        self.assertEqual(before,self.bytes())

    def test_mode_and_step_are_isolated_between_people_and_events(self):
        workflow.select(self.state,self.tokens[self.DEPUTY],{k:[v] for k,v in self.fields().items()})
        for phone in (self.DUTY,self.CAPTAIN):
            self.assertEqual({},self.state.member_session(self.tokens[phone]).workflow_preferences)
            self.assertIn('data-workflow-mode="direct"',self.page('/sand',phone)[1])
        member=self.state.member_session(self.tokens[self.DEPUTY])
        self.assertEqual('direct',workflow.preference(member,'other')['mode'])
        token=self.tokens[self.DEPUTY];self.state.member_logout(token)
        new,error=self.state.member_login(self.DEPUTY,'123456');self.assertFalse(error)
        self.assertEqual({},self.state.member_session(new).workflow_preferences)

    def test_invalid_csrf_event_target_and_indices_do_not_change_preferences(self):
        server=self.server();before=self.bytes();member=self.state.member_session(self.tokens[self.DEPUTY])
        for overrides,expected in ((dict(csrf='wrong'),403),(dict(event_id='foreign'),403),
                (dict(target='https://example.com'),409),(dict(step='9'),409),
                (dict(node='99'),409),(dict(mode='admin'),409)):
            self.assertEqual(expected,self.request(server,'/workflow/mode',phone=self.DEPUTY,
                form=self.fields(**overrides))[0])
        self.assertEqual({},member.workflow_preferences);self.assertEqual(before,self.bytes())

    def test_guidance_does_not_add_command_authority_for_duty_or_captain(self):
        server=self.server()
        for phone in (self.DUTY,self.CAPTAIN):
            self.assertEqual(303,self.request(server,'/workflow/mode',phone=phone,form=self.fields(phone))[0])
            member=self.state.member_session(self.tokens[phone]);before=self.bytes()
            self.assertEqual(403,self.request(server,'/sand/goal',phone=phone,
                form=dict(statement='禁止越权',_business_csrf=member.custody_csrf))[0])
            self.assertEqual(before,self.bytes())

    def test_status_is_event_bound_and_closed_event_keeps_historical_read_scope(self):
        server=self.server();self.request(server,'/workflow/mode',phone=self.DEPUTY,form=self.fields())
        before=self.bytes()
        status,_,raw=self.request(server,'/workflow/status?event_id='+self.event_id,phone=self.DEPUTY)
        self.assertEqual(200,status);self.assertEqual(9,len(json.loads(raw)['states']))
        self.assertEqual(404,self.request(server,'/workflow/status?event_id=foreign',phone=self.DEPUTY)[0])
        self.assertEqual(before,self.bytes())
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        self.assertEqual(200,self.page('/workflow/guide',self.DEPUTY)[0])
        self.assertEqual(303,self.request(server,'/workflow/mode',phone=self.DEPUTY,form=self.fields())[0])
        self.assertEqual(before,self.bytes())

    def test_public_screen_has_no_navigation_preference_or_private_guide(self):
        self.request(self.server(),'/workflow/mode',phone=self.DEPUTY,form=self.fields())
        status,html=render_page(self.state,'/sand',member_token=self.tokens[self.CAPTAIN],query={'projector':['1']})
        self.assertEqual(200,status);self.assertNotIn('data-workflow-bar',html)

    def test_inplace_switch_accepts_blank_destination_and_returns_only_navigation(self):
        from http.client import HTTPConnection
        from urllib.parse import urlencode
        server=self.server();before=self.bytes()
        connection=HTTPConnection('127.0.0.1',server.server_port,timeout=8)
        try:
            connection.request('POST','/workflow/mode',body=urlencode(self.fields(mode='direct',target='')),
                headers={'Cookie':'member_session='+self.tokens[self.DEPUTY],
                         'Content-Type':'application/x-www-form-urlencoded','X-Workflow-Mode':'1'})
            response=connection.getresponse();self.assertEqual(200,response.status)
            payload=json.loads(response.read());self.assertTrue(payload['ok'])
            self.assertIn('data-workflow-mode="direct"',payload['bar'])
            self.assertNotIn('data-sand-shell',payload['bar'])
        finally:connection.close()
        with self.state._event_authority_lock:self.assertEqual(before,self.bytes())

    def test_newly_selected_provincial_deputy_gets_choices_on_first_real_login(self):
        from tests import test_upgrade_duty_intake as intake
        fixture=intake.DutyIntakeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.start();self.assertEqual(303,fixture.select()[0])
        status,headers,_=fixture.request('POST','/login/unified',
            dict(account=fixture.subject,password='123456'))
        self.assertEqual(303,status);self.assertIn(('Location','/events'),headers)
        cookie=next(value.split(';',1)[0] for name,value in headers if name=='Set-Cookie' and value.startswith('member_session='))
        before=fixture.bytes()
        status,_,html=fixture.request('GET','/events',cookie=cookie)
        self.assertEqual(200,status);self.assertIn('data-workflow-choice',html)
        self.assertIn('九步走引导',html);self.assertIn('功能直达',html)
        self.assertEqual(before,fixture.bytes());self.assertFalse(fixture.state.sessions)


del Fixture
if __name__=='__main__':unittest.main()
