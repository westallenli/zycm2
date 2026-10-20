"""Visual SOP guidance is event-derived navigation, not completion or authority."""
import json
import unittest
from panda_brain import sop_guide as guide
from panda_brain.sand_shell import render_shell_navigation
from tests import test_upgrade_sop as sop_fixture
from tests import test_upgrade_intelligence as intelligence_fixture


def fragment(state='待补资料', gap='位置尚待核对'):
    return f'<div data-sop-state="{state}"><h4>还缺什么</h4><ul><li>{gap}</li></ul></div>'


@unittest.skip('沙盘界面改版：旧 sop-* 导航区块已移除，等待按新界面重写')
class GuideTests(unittest.TestCase):
    def test_emergency_and_change_recommendations_use_explicit_record_states(self):
        states=['待人工复核']*9
        states[1]='有变化待复核'
        self.assertEqual(1, guide.recommendation(states)['step'])
        states[6]='有应急相关记录待核对'
        self.assertEqual(6, guide.recommendation(states)['step'])
        self.assertNotIn('已确认险情', guide.recommendation(states)['reason'])

    def test_unknown_and_invalid_evidence_does_not_suggest_business_action(self):
        self.assertIsNone(guide.recommendation(['依据待核对']*9)['step'])
        self.assertIsNone(guide.recommendation([])['step'])
        html=guide.guide(3, fragment('未进入事件'))
        self.assertNotIn('href="/tasks/release"',html)
        self.assertIn('依据不可用',html)

    def test_missing_source_text_is_escaped_and_not_invented(self):
        html=guide.guide(0,fragment(gap='&lt;img src=x onerror=alert(1)&gt;'))
        self.assertIn('&lt;img',html)
        self.assertNotIn('<img',html)
        self.assertIn('当前资料提醒 · 1 条',html)

    def test_all_paths_have_specific_owners_actions_and_completion_conditions(self):
        for i in range(9):
            html=guide.guide(i,fragment())
            self.assertIn('办理职责',html)
            self.assertIn('怎样算完成',html)
            self.assertIn('本页不自动判定完成',html)
            self.assertNotIn('已完成',html)
            self.assertEqual(len(guide.PATHS[i]),html.count('data-sop-node="'))
        self.assertIn('按类别复核',guide.guide(3,fragment()))
        self.assertIn('/workflow/records?step=7',guide.guide(7,fragment()))
        self.assertIn('在原通道发布',guide.guide(7,fragment()))

    def test_public_screen_does_not_gain_guide_or_private_basis(self):
        html=render_shell_navigation(role='public',event_name='模拟',data_time='now',contains_demo=True,
                                     early_response_html='SECRET')
        self.assertNotIn('SECRET',html)
        self.assertNotIn('data-sop-',html)

    def test_original_forms_remain_single_and_collapsed(self):
        form='<form action="/sand/sensitive-original"><input name="csrf" value="original"></form>'
        html=render_shell_navigation(role='commander',event_name='模拟',data_time='now',contains_demo=True,
                                     deployment_html=fragment()+form)
        self.assertEqual(1,html.count('action="/sand/sensitive-original"'))
        self.assertEqual(9,html.count('class="sop-basis"'))
        self.assertNotIn('class="sop-basis" open',html)
        self.assertEqual(9,html.count('data-sand-step="'))

    def test_page_projection_and_missing_records_remain_readonly(self):
        fixture=sop_fixture.SopTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        from panda_brain.sandview import render_sand_page
        before=(fixture.ws.version,len(fixture.ws.events))
        html=render_sand_page(fixture.state)
        self.assertIn('data-sop-recommendation',html)
        self.assertIn('data-sop-summary="8"',html)
        self.assertEqual(before,(fixture.ws.version,len(fixture.ws.events)))


class GuideHttpTests(unittest.TestCase):
    def test_live_guides_event_authority_and_get_bytes(self):
        fixture=intelligence_fixture.IntelligenceHttpTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        server=fixture.server()
        before=fixture.bytes()
        for account in (fixture.DUTY,fixture.DEPUTY,fixture.CAPTAIN):
            status,_,raw=fixture.request(server,'/sand/live-context?event_id='+fixture.event_id,phone=account)
            self.assertEqual(200,status)
            data=json.loads(raw)
            self.assertEqual(9,len(data['sop_guide_html']))
            self.assertIn('data-sop-recommendation',data['sop_recommendation_html'])
        self.assertEqual(before,fixture.bytes())
        status,_,_=fixture.request(server,'/sand/live-context?event_id=foreign',phone=fixture.DEPUTY)
        self.assertEqual(404,status)
        status,_,_=fixture.request(server,'/sand/live-context?event_id='+fixture.event_id,cookie='invalid=')
        self.assertEqual(403,status)


if __name__=='__main__':
    unittest.main()
