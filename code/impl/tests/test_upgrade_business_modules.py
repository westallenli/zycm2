"""Five module visibility does not expand team data or action authority."""
import json
import unittest
from panda_brain.panel import render_page
from panda_brain.models import PersonRecord
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain import business_modules as business
from tests import test_upgrade_deputy_modules as deputy
from tests import test_upgrade_personnel_home as home


class BusinessModulesTests(unittest.TestCase):
    DUTY, DEPUTY, CAPTAIN = home.PersonnelHomeTests.DUTY, home.PersonnelHomeTests.DEPUTY, home.PersonnelHomeTests.CAPTAIN
    MEMBER_A, MEMBER_B, TEMP = home.PersonnelHomeTests.MEMBER_A, home.PersonnelHomeTests.MEMBER_B, home.PersonnelHomeTests.TEMP
    _demo_event = home.PersonnelHomeTests._demo_event
    bytes = home.PersonnelHomeTests.bytes
    server = deputy.DeputyModulesTests.server
    request = deputy.DeputyModulesTests.request
    foreign_event = deputy.DeputyModulesTests.foreign_event
    NAV = deputy.DeputyModulesTests.NAV
    OTHER = '13999995007'
    OTHER_MEMBER = '13999995008'

    def setUp(self):
        home.PersonnelHomeTests.setUp(self)
        self.state.registry.import_person_library([
            PersonRecord(name='SECRET-OTHER-CAPTAIN', phone=self.OTHER, unit='SECRET-OTHER-TEAM', is_leader=True),
            PersonRecord(name='SECRET-OTHER-MEMBER', phone=self.OTHER_MEMBER, unit='SECRET-OTHER-TEAM')])
        self.tokens[self.OTHER] = self.state.member_login(self.OTHER, '123456')[0]
        self.other = self.state.ledger.add_squad(name='SECRET-OTHER-TEAM', member_phones=(self.OTHER,self.OTHER_MEMBER),
            leader_phone=self.OTHER, library=self.state.registry.library)
        self.state.ledger.set_carry_line(self.other.squad_id,self.item_id)
        self.state._save_ledger()
        self.state.handle_select_squads(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad.squad_id,self.other.squad_id], channel='phone',request_id='modules-callouts')
        for phone,squad in ((self.CAPTAIN,self.squad),(self.OTHER,self.other)):
            self.state.handle_captain_scan(self.event_id,captain_token=self.tokens[phone],
                squad_id=squad.squad_id,qr_id=self.qr_id)
        self.state.handle_captain_arrival(self.event_id,captain_token=self.tokens[self.OTHER],
            squad_id=self.other.squad_id,mode='initial',phones=[],request_id='modules-other-arrival')

    def page(self,route,phone):
        return render_page(self.state,route,member_token=self.tokens[phone])

    def test_all_three_roles_have_five_readonly_modules_before_captain_arrival(self):
        before = self.bytes()
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            for route,_ in self.NAV:
                with self.subTest(phone=phone,route=route):
                    status,html = self.page(route,phone)
                    self.assertEqual(200,status)
                    for target,label in self.NAV:
                        self.assertIn('href="'+target+'"',html)
                        self.assertIn(label,html)
        self.assertEqual(before,self.bytes())
        self.assertEqual('',self.state.member_session(self.tokens[self.CAPTAIN]).arrived_at)
        self.assertFalse(any(self.CAPTAIN in squad.arrived_phones
            for squad in self.state.ledger.snapshots[self.event_id].squads.values()))

    def test_captain_home_shares_full_event_names(self):
        html = self.page('/',self.CAPTAIN)[1]
        self.assertIn('本事件全部召集队伍与人员',html)
        self.assertIn('队员甲',html)
        for secret in ('SECRET-OTHER-CAPTAIN','SECRET-OTHER-MEMBER',self.OTHER,self.OTHER_MEMBER):
            self.assertIn(secret,html)
        self.assertIn('data-panel=',html)
        self.assertIn('选队召集与回执',html)

    def test_captain_current_event_materials_show_own_team_inventory(self):
        self.state.handle_captain_arrival(self.event_id,captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id,mode='initial',phones=[],request_id='modules-own-arrival')
        html = self.page('/materials/event',self.CAPTAIN)[1]
        self.assertIn('水带',html)
        self.assertIn('一中队',html)
        self.assertNotIn('SECRET-OTHER-TEAM',html)
        self.assertNotIn('action="/materials/planning"',html)

    def test_captain_sand_shares_all_event_marks(self):
        for marker,title,team,status in (('shared','SHARED-FIRE','',AnnotationStatus.PUBLISHED),
                ('own','OWN-ROUTE',self.squad.squad_id,AnnotationStatus.PUBLISHED),
                ('other','SECRET-OTHER-ROUTE',self.other.squad_id,AnnotationStatus.PUBLISHED),
                ('draft','SECRET-DRAFT','',AnnotationStatus.DRAFT)):
            self.state.workspace.record_annotation(MapAnnotation(id=marker,kind=AnnotationKind.TEXT,
                points=((10,10),),geo_points=((31,110),),title=title,status=status,
                details={'team_id':team},event_id=self.event_id))
        self.state.persist_if_db()
        html = self.page('/sand',self.CAPTAIN)[1]
        self.assertIn('SHARED-FIRE',html)
        self.assertIn('OWN-ROUTE',html)
        self.assertIn('SECRET-OTHER-ROUTE',html)
        self.assertIn('SECRET-DRAFT',html)
        self.assertEqual(200,render_page(self.state,'/sand',member_token=self.tokens[self.CAPTAIN],
                                       query={'projector':['1']})[0])

    def test_http_captain_cannot_use_detail_apis_or_foreign_event_parameters(self):
        server = self.server()
        before = self.bytes()
        status,_,body = self.request(server,'/sand/situation-feed',phone=self.CAPTAIN)
        self.assertEqual(200,status)
        self.assertTrue(json.loads(body))
        self.assertNotIn('另一模拟事件',body)
        for route in ('/sand/source','/sand/task-draft','/review/replay','/review/inquiry-source'):
            self.assertIn(self.request(server,route,phone=self.CAPTAIN)[0],(200,404,409))
        foreign = self.foreign_event()
        member = self.state.member_session(self.tokens[self.CAPTAIN])
        before = self.bytes()
        self.assertEqual(403,self.request(server,'/events/select',phone=self.CAPTAIN,
            form={'event_id':foreign,'csrf':member.custody_csrf})[0])
        self.assertEqual(404,self.page('/events/'+foreign+'/details',self.CAPTAIN)[0])
        self.assertEqual(before,self.bytes())

    def test_existing_captain_reply_accepts_own_team_and_rejects_other_team(self):
        server = self.server()
        form = {'event_id':self.event_id,'squad_id':self.squad.squad_id,'can_depart':'yes',
                'expected_people':'3','eta':'2026-10-12T11:00:00+08:00','reason':'','request_id':'modules-own-reply'}
        self.assertEqual(303,self.request(server,'/callouts/reply',phone=self.CAPTAIN,form=form)[0])
        before = self.bytes()
        form.update(squad_id=self.other.squad_id,request_id='modules-other-reply')
        self.assertIn(self.request(server,'/callouts/reply',phone=self.CAPTAIN,form=form)[0],(403,409))
        self.assertEqual(before,self.bytes())

    def test_duty_selection_follows_own_session_and_keeps_write_gates(self):
        foreign = self.foreign_event()
        self.state.handle_event_action(foreign,'enter')
        original = self.state.workspace
        member = self.state.member_session(self.tokens[self.DUTY])
        server = self.server()
        self.assertEqual(303,self.request(server,'/events/select',phone=self.DUTY,
            form={'event_id':self.event_id,'csrf':member.custody_csrf})[0])
        before = self.bytes()
        for route,_ in self.NAV:
            html = self.page(route,self.DUTY)[1]
            if route!='/events':
                self.assertNotIn('另一模拟事件',html)
            self.assertIs(original,self.state.workspace)
        self.assertEqual(403,self.request(server,'/events/action',phone=self.DUTY,
            form={'event_id':self.event_id,'action':'close'})[0])
        self.assertEqual(before,self.bytes())

    def test_registered_duty_event_has_honest_empty_modules_and_can_still_start(self):
        pending = self.state.registry.create_event(name='待启动模拟火情',report_no='MODULES-PENDING',
            discovered_at='2026-10-12T11:00:00+08:00',source='模拟测试',location='模拟位置').event_id
        member = self.state.member_session(self.tokens[self.DUTY])
        server = self.server()
        self.assertEqual(303,self.request(server,'/events/select',phone=self.DUTY,
            form={'event_id':pending,'csrf':member.custody_csrf})[0])
        before = self.bytes()
        for route,_ in self.NAV[1:]:
            status,html = self.page('/materials/event' if route=='/materials' else route,self.DUTY)
            self.assertEqual(200,status)
            self.assertIn('请先选择已启动火情' if route=='/materials' else '本事件尚未启动',html)
        self.assertEqual(before,self.bytes())
        self.assertEqual(303,self.request(server,'/events/action',phone=self.DUTY,
            form={'event_id':pending,'action':'start','csrf':member.custody_csrf})[0])
        self.assertEqual('active',self.state.registry.events[pending].status)

    def test_captain_without_callout_sees_empty_modules_without_admin_data(self):
        newcomer = '13999995009'
        self.state.registry.import_person_library([PersonRecord(name='未召集队长',phone=newcomer,unit='未召集队',is_leader=True)])
        self.tokens[newcomer] = self.state.member_login(newcomer,'123456')[0]
        self.state.ledger.add_squad(name='未召集队',member_phones=(newcomer,),leader_phone=newcomer,
                                    library=self.state.registry.library)
        for route,_ in self.NAV:
            status,html = self.page(route,newcomer)
            self.assertEqual(200,status)
            self.assertNotIn('SECRET-OTHER',html)
            self.assertNotIn(self.event_id,html)

    def test_captain_login_goes_to_events_and_ordinary_members_keep_old_gate(self):
        status,headers,_ = self.request(self.server(),'/login/unified',phone=self.CAPTAIN,
                                       form={'account':self.CAPTAIN,'password':'123456'})
        self.assertEqual(303,status)
        self.assertEqual('/events',headers['Location'])
        self.assertEqual(404,self.page('/events',self.MEMBER_A)[0])
        self.assertEqual('captain',business.role(self.state,self.tokens[self.CAPTAIN]))


if __name__ == '__main__':
    unittest.main()
