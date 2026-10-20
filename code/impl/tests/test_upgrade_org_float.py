"""Shared floating organisation layout preserves real arrangement and arrival facts."""
import re
import unittest
from panda_brain import homepage
from panda_brain.panel import render_page
from tests import test_upgrade_personnel_home as fixture, test_upgrade_deputy_modules as http


class OrgFloatTests(unittest.TestCase):
    def setUp(self):
        fixture.PersonnelHomeTests.setUp(self)
        self.state.handle_select_squads(self.event_id,deputy_token=self.tokens[self.DEPUTY],squad_ids=[self.squad.squad_id],channel='phone',request_id='float-callout')
        self.state.handle_captain_scan(self.event_id,captain_token=self.tokens[self.CAPTAIN],squad_id=self.squad.squad_id,qr_id=self.qr_id)
    DUTY,DEPUTY,CAPTAIN=fixture.PersonnelHomeTests.DUTY,fixture.PersonnelHomeTests.DEPUTY,fixture.PersonnelHomeTests.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=fixture.PersonnelHomeTests.MEMBER_A,fixture.PersonnelHomeTests.MEMBER_B,fixture.PersonnelHomeTests.TEMP
    _demo_event,bytes,register,home=fixture.PersonnelHomeTests._demo_event,fixture.PersonnelHomeTests.bytes,fixture.PersonnelHomeTests.register,fixture.PersonnelHomeTests.home
    server,request=http.DeputyModulesTests.server,http.DeputyModulesTests.request

    def slot(self,html,code):
        index=next(i for i,b in enumerate(homepage.org_layout()['boxes']) if b['code']==code)
        return re.search(r'id="hp-panel-'+str(index)+r'" hidden>(.*?)</div>',html,re.S)[1]

    def test_all_three_share_main_graph_and_two_permanent_floating_entries(self):
        self.register();before=self.bytes()
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,html=self.home(phone);self.assertEqual(200,status)
            self.assertIn('id="org-stage"',html)
            self.assertIn('临时岗位人员库',html);self.assertIn('调组安排',html)
            self.assertEqual(40,html.count('data-panel="hp-panel-'))
            self.assertLess(html.index('class="hp-canvas"'),html.index('id="org-pool"'))
            self.assertNotIn('class="hp-side"',html)
            self.assertNotIn('class="lz-card unified-children"',html)
            for route in ('/events','/','/sand','/materials','/review'):self.assertIn('href="'+route+'"',html)
        self.assertEqual(before,self.bytes())

    def test_real_pending_pool_and_original_arrangement_endpoints(self):
        self.register();html=self.home(self.DEPUTY)[1]
        self.assertIn('待安排 <b>1</b> 人',html);self.assertIn(self.TEMP,html)
        self.assertIn('action="/temporary/assign"',html)
        self.assertIn('data-org-action',html)
        self.assertEqual('',self.state.temporary_people(self.event_id)[self.TEMP].arrived_at)

    def test_initial_assignment_and_transfer_update_node_details_without_arrival(self):
        self.register()
        self.state.handle_temporary_assign(self.event_id,deputy_token=self.tokens[self.DEPUTY],phones=[self.TEMP],pos_code='tech-info',request_id='float-assign')
        html=self.home(self.DEPUTY)[1]
        self.assertIn('属地协助',self.slot(html,'tech-info'))
        self.assertIn('未到场',self.slot(html,'tech-info'))
        self.state.handle_person_group_transfer(self.event_id,deputy_token=self.tokens[self.DEPUTY],phone=self.TEMP,to_pos_code='log-comm',request_id='float-transfer')
        html=self.home(self.DEPUTY)[1]
        self.assertIn('属地协助',self.slot(html,'log-comm'))
        self.assertNotIn('属地协助',self.slot(html,'tech-info'))
        pool=html.split('id="org-pool"',1)[1].split('id="org-arrange"',1)[0]
        self.assertIn('通讯保障组',pool)
        self.assertNotIn('信息收集组',pool)
        self.assertEqual('',self.state.temporary_people(self.event_id)[self.TEMP].arrived_at)

    def test_arrival_facts_and_team_inventory_remain_after_group_transfer(self):
        self.register();self.state.handle_temporary_assign(self.event_id,deputy_token=self.tokens[self.DEPUTY],phones=[self.TEMP],pos_code='tech-info',request_id='float-arrival-assign')
        self.state.handle_temporary_arrival(self.event_id,member_token=self.tokens[self.TEMP])
        arrival=self.state.temporary_people(self.event_id)[self.TEMP].arrived_at
        self.state.handle_person_group_transfer(self.event_id,deputy_token=self.tokens[self.DEPUTY],phone=self.TEMP,to_pos_code='log-comm',request_id='float-arrival-transfer')
        self.assertEqual(arrival,self.state.temporary_people(self.event_id)[self.TEMP].arrived_at)
        self.assertIn('已到场',self.slot(self.home(self.DEPUTY)[1],'log-comm'))

    def test_other_roles_cannot_use_arrangement_actions(self):
        self.register();server=self.server();before=self.bytes()
        for phone in (self.DUTY,self.CAPTAIN):
            html=self.home(phone)[1];self.assertIn('fieldset disabled',html)
            self.assertEqual(403,self.request(server,'/temporary/assign',phone=phone,
                form={'event_id':self.event_id,'phone':self.TEMP,'pos_code':'tech-info','request_id':'wrong-'+phone})[0])
        self.assertEqual(before,self.bytes())

    def test_panel_names_and_person_values_are_escaped(self):
        from dataclasses import replace
        self.state.registry.library[self.TEMP]=replace(self.state.registry.library[self.TEMP],name='<script>floatbad</script>')
        self.register();html=self.home(self.DEPUTY)[1]
        self.assertIn('&lt;script&gt;floatbad&lt;/script&gt;',html);self.assertNotIn('<script>floatbad</script>',html)

    def test_new_styles_and_scripts_are_served_without_mutation(self):
        server=self.server();before=self.bytes()
        for path in ('/static/org-float.css','/static/org-float.js'):
            self.assertEqual(200,self.request(server,path,phone=self.DEPUTY)[0])
        self.assertEqual(before,self.bytes())


if __name__=='__main__':unittest.main()
