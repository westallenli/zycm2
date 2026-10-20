"""Approved four-step duty flow, atomic handoff, real subject and preserved history."""
from pathlib import Path
import json
import unittest
from unittest.mock import patch
from dataclasses import replace
from tests import test_upgrade_confirmed_fire as fixture
from panda_brain import duty_intake
from panda_brain.persistence import load_workspace
from panda_brain.event_authorization import event_grants


class DutyIntakeTests(unittest.TestCase):
    DUTY,MEMBER=fixture.ConfirmedFireTests.DUTY,fixture.ConfirmedFireTests.MEMBER
    setUp=fixture.ConfirmedFireTests.setUp
    start_http,request,bytes=fixture.ConfirmedFireTests.start_http,fixture.ConfirmedFireTests.request,fixture.ConfirmedFireTests.bytes

    def prepare(self):
        self.token=self.state.member_login(self.DUTY,'123456')[0]
        self.cookie='member_session='+self.token
        self.csrf=self.state.member_session(self.token).custody_csrf
        self.subject=duty_intake.catalogue()[0]['people'][1]['subject_id']
        self.start_http()

    def intake(self):
        self.prepare()
        self.assertEqual(303,self.request('POST','/events/alarm/simulate',
            dict(csrf=self.csrf,request_id='demo-001'),self.cookie)[0])
        self.eid=next(iter(self.state.registry.events))

    def start(self):
        self.intake()
        self.assertEqual(303,self.request('POST','/events/action',
            dict(csrf=self.csrf,action='start',event_id=self.eid),self.cookie)[0])

    def select(self,**overrides):
        return self.request('POST','/events/deputy-select',
            dict(csrf=self.csrf,request_id='select-001',event_id=self.eid,subject_id=self.subject,**overrides),self.cookie)

    def test_real_four_step_flow_without_admin_and_no_duty_event_entry(self):
        self.intake()
        self.assertEqual('registered',self.state.registry.events[self.eid].status)
        self.assertFalse(self.state.registry.qrs)
        self.assertEqual(303,self.request('POST','/events/action',dict(csrf=self.csrf,action='start',event_id=self.eid),self.cookie)[0])
        self.assertEqual('active',self.state.registry.events[self.eid].status)
        self.assertFalse(self.state.registry.qrs)
        html=self.request('GET','/events',cookie=self.cookie)[2]
        self.assertIn('全省应急人员名单',html); self.assertIn('确认副总指挥并生成二维码',html)
        self.assertIn('启动完成，待值班人员选定副总指挥',html)
        self.assertNotIn('等待值班人员启动处置后生成',html)
        self.assertNotIn('name="evidence"',html); self.assertNotIn('进入事件',html)
        status,headers,_=self.select(); self.assertEqual(303,status)
        self.assertIn('/events?completed='+self.eid,dict(headers)['Location'])
        html=self.request('GET','/events?completed='+self.eid,cookie=self.cookie)[2]
        self.assertIn('值班工作已完成',html); self.assertIn('event-qr-symbol',html)
        self.assertNotIn('进入事件',html); self.assertFalse(self.state.sessions)
        self.assertEqual('',self.state.member_session(self.token).selected_event_id)
        self.assertEqual('',self.state.active_event_id)
        deputy,error=self.state.member_login(self.subject,'123456')
        self.assertFalse(error); self.assertEqual([self.eid],self.state.deputy_events(deputy))
        for path in ('/events','/','/sand','/materials','/review'):
            self.assertIn('href="'+path+'"',html)

    def test_simulation_and_selection_retries_are_byte_idempotent(self):
        self.start(); before=self.bytes()
        self.assertEqual(303,self.request('POST','/events/alarm/simulate',dict(csrf=self.csrf,request_id='demo-001'),self.cookie)[0])
        self.assertEqual(before,self.bytes()); self.assertEqual(1,len(self.state.registry.events))
        self.assertEqual(303,self.select()[0]); before=self.bytes()
        self.assertEqual(303,self.select()[0]); self.assertEqual(before,self.bytes())
        self.assertEqual(1,len(self.state.registry.qrs))

    def test_gets_are_readonly_and_catalogue_is_separate_from_business_registry(self):
        self.start(); before=self.bytes()
        for path in ('/events','/events/'+self.eid+'/details'):
            self.assertEqual(200,self.request('GET',path,cookie=self.cookie)[0])
        self.assertEqual(before,self.bytes()); self.assertNotIn(self.subject,self.state.registry.library)

    def test_invalid_choice_and_closed_or_unstarted_event_never_issue_a_code(self):
        self.intake(); before=self.bytes(); self.assertEqual(409,self.select()[0]); self.assertEqual(before,self.bytes())
        self.state.handle_event_action(self.eid,'start',duty_token=self.token,defer_initial_qr=True)
        f=dict(csrf=self.csrf,request_id='bad-choice',event_id=self.eid,subject_id=self.MEMBER)
        before=self.bytes(); self.assertEqual(409,self.request('POST','/events/deputy-select',f,self.cookie)[0]); self.assertEqual(before,self.bytes())
        self.state.registry.events[self.eid]=replace(self.state.registry.events[self.eid],status='closed')
        before=self.bytes(); self.assertEqual(409,self.select()[0]); self.assertEqual(before,self.bytes()); self.assertFalse(self.state.registry.qrs)

    def test_non_duty_and_wrong_or_missing_csrf_cannot_operate(self):
        self.start(); ordinary=self.state.member_login(self.MEMBER,'123456')[0]
        before=self.bytes()
        for path,fields in (('/events/alarm/simulate',dict(request_id='forbidden')),
                            ('/events/deputy-select',dict(request_id='forbidden',subject_id=self.subject,event_id=self.eid))):
            for cookie in ('','member_session='+ordinary):
                self.assertEqual(403,self.request('POST',path,dict(fields,csrf=self.csrf),cookie)[0])
            self.assertEqual(403,self.request('POST',path,dict(fields,csrf='bad'),self.cookie)[0])
            self.assertEqual(403,self.request('POST',path,fields,self.cookie)[0])
        self.assertEqual(before,self.bytes())

    def test_partial_registry_save_failure_rolls_back_appointment_code_and_new_person(self):
        self.start(); before=self.bytes()
        with patch.object(self.state,'_save_registry',side_effect=OSError('simulated save fault')):
            self.assertEqual(503,self.select()[0])
        self.assertEqual(before,self.bytes()); self.assertFalse(self.state.registry.qrs)
        self.assertIsNone(self.state.deputy_appointment(self.eid)); self.assertNotIn(self.subject,self.state.registry.library)
        self.assertEqual(303,self.select()[0])

    def test_handoff_audit_records_real_duty_and_exact_external_catalogue_source(self):
        self.start(); self.assertEqual(303,self.select()[0])
        ws=load_workspace(self.state.registry.events[self.eid].snapshot_path,general_auth=self.auth)
        self.assertFalse(ws.verify_audit_chain()); self.assertEqual(self.subject,event_grants(ws)[0].subject_id)
        selected=next(e for e in ws.events if e.kind=='ProvincialDeputySelected')
        self.assertEqual(self.DUTY,selected.payload['operator'])
        self.assertEqual(duty_intake.catalogue()[1],selected.payload['catalogue_digest'])
        self.assertEqual(self.subject,selected.payload['selected_person']['subject_id'])
        kinds=[e.kind for e in ws.events]
        self.assertLess(kinds.index('FireEventStarted'),kinds.index('ObjectRevisionRecorded'))
        self.assertLess(kinds.index('ObjectRevisionRecorded'),kinds.index('EventQRIssued'))

    def test_existing_events_qrs_and_assignments_are_preserved(self):
        self.prepare()
        from tests.test_upgrade_confirmed_fire import message
        old=self.state.handle_confirmed_fire_event(message('legacy'))['event_id']
        qr=self.state.handle_event_action(old,'start',duty_token=self.token)['qr_id']
        self.state.handle_deputy_appointment(old,duty_token=self.token,subject_id=self.MEMBER,
            evidence='原线下任命',request_id='legacy-selection')
        before=Path(self.state.registry.events[old].snapshot_path).read_bytes(); oldqr=self.state.registry.qrs[qr]
        self.state.handle_duty_demo(duty_token=self.token,request_id='new-demo')
        self.eid=next(e for e in self.state.registry.events if e!=old)
        self.state.handle_event_action(self.eid,'start',duty_token=self.token,defer_initial_qr=True)
        self.assertEqual(303,self.select()[0])
        self.assertEqual(before,Path(self.state.registry.events[old].snapshot_path).read_bytes())
        self.assertEqual(oldqr,self.state.registry.qrs[qr])

    def test_duplicate_nonce_fields_rejected_without_changes(self):
        self.start(); before=self.bytes()
        f=[('csrf',self.csrf),('csrf',self.csrf),('request_id','duplicate-fields'),('subject_id',self.subject),('event_id',self.eid)]
        self.assertEqual(403,self.request('POST','/events/deputy-select',f,self.cookie)[0]); self.assertEqual(before,self.bytes())

    def test_old_closed_event_without_snapshot_does_not_block_duty_workflow(self):
        self.prepare()
        from tests.test_upgrade_confirmed_fire import message
        eid=self.state.handle_confirmed_fire_event(message('old-closed'))['event_id']
        self.state.registry.events[eid]=replace(self.state.registry.events[eid],status='closed')
        self.state._save_registry(); before=self.bytes()
        status,_,html=self.request('GET','/events',cookie=self.cookie)
        self.assertEqual(200,status); self.assertIn('模拟报警接入',html)
        self.assertIn('历史任务记录待核对',html); self.assertIn('事件已关闭',html)
        self.assertEqual(before,self.bytes()); self.assertNotIn('进入事件',html)
        self.assertEqual(303,self.request('POST','/events/alarm/simulate',dict(csrf=self.csrf,request_id='after-old-closed'),self.cookie)[0])
        self.assertEqual(2,len(self.state.registry.events))
        fresh=next(e for e in self.state.registry.events if e!=eid)
        self.assertEqual(303,self.request('POST','/events/action',dict(csrf=self.csrf,event_id=fresh,action='start'),self.cookie)[0])
        self.assertEqual(303,self.request('POST','/events/deputy-select',dict(csrf=self.csrf,event_id=fresh,subject_id=self.subject,request_id='after-old-closed-selection'),self.cookie)[0])

    def test_existing_appointment_cannot_be_replaced_by_new_selection(self):
        self.start(); self.assertEqual(303,self.select()[0]); before=self.bytes()
        f=dict(csrf=self.csrf,event_id=self.eid,subject_id=duty_intake.catalogue()[0]['people'][2]['subject_id'],request_id='replacement')
        self.assertEqual(409,self.request('POST','/events/deputy-select',f,self.cookie)[0]); self.assertEqual(before,self.bytes())


if __name__=='__main__': unittest.main()
