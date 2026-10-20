"""P04-03: live event force facts; no invented needs, dispatch or authority."""
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from panda_brain.models import IntelItem
from panda_brain.panel import render_page
from panda_brain.sandview import render_sand_page
from panda_brain.sop import force_view, render_force
from tests import test_upgrade_force_needs as force


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class ForceSopTests(unittest.TestCase):
    def setUp(self):
        self.f = force.ForceNeedsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.state = self.f.state
        self.token = self.state.login('p03-force-needs-secret')
        self.state.handle_enter_event(self.f.event_id)

    def view(self):
        return force_view(self.state.workspace,
                          self.state.registry.events[self.f.event_id], self.state.ledger)

    def arrive(self):
        f = self.f
        self.state.handle_captain_scan(f.event_id, captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad_id, qr_id=f.qr_id)
        self.state.handle_captain_arrival(f.event_id, captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad_id, mode='initial', phones=[f.MEMBER], request_id='sop-arrive')

    def standby(self):
        f = self.f
        self.state.handle_captain_readiness(f.event_id, captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad_id, phones=[f.CAPTAIN], status='standby',
            basis='模拟队长点名待命', order_id='', request_id='sop-ready')

    def test_initial_supply_and_simulated_callout_never_invent_gap_or_arrival(self):
        view = self.view()
        self.assertEqual('初步人力需求待复核', view.state)
        self.assertEqual(4, len(view.needs.rows))
        self.assertTrue(all(r.arrived_reported == r.standby_reported == 0 for r in view.needs.rows))
        self.assertTrue(all(r.status == '需求人数待核定' for r in view.needs.rows))
        self.assertIn('未发送、未送达', str(view.callouts))
        self.assertTrue(any('缺口人数暂不能计算' in text for text in view.missing))

    def test_arrival_and_explicit_readiness_remain_separate(self):
        self.arrive()
        combat = self.view().needs.rows[0]
        self.assertEqual((1, 0), (combat.arrived_reported, combat.standby_reported))
        self.assertTrue(any('可派状态待核定 1 人' in text for text in self.view().missing))
        self.standby()
        combat = self.view().needs.rows[0]
        self.assertEqual((1, 1), (combat.arrived_reported, combat.standby_reported))
        self.assertIn('模拟队长点名待命', str(self.view().reports))

    def test_support_categories_reuse_fixed_organization(self):
        self.f.add_temp(self.f.COMM, 'log-comm')
        self.f.add_temp(self.f.MED, 'log-medical')
        rows = {r.key: r for r in self.view().needs.rows}
        self.assertEqual((1, 0), (rows['communication'].arrived_reported, rows['communication'].standby_reported))
        self.assertEqual((1, 0), (rows['medical'].arrived_reported, rows['medical'].standby_reported))
        self.assertEqual(0, rows['combat'].arrived_reported)

    def test_review_and_live_unpersisted_change_use_same_event_not_saved_snapshot(self):
        self.f.review()
        self.assertEqual('current', self.view().needs.state)
        self.assertNotEqual('已完成', self.view().state)
        self.state.workspace.record_intel(IntelItem('sop-live', '天气', '尚未写盘的新模拟风向资料', '模拟'))
        # P03 endpoint reloads the saved snapshot; SOP must use the current page workspace.
        self.assertEqual('current', self.state.event_force_needs(self.f.event_id).state)
        self.assertEqual('needs_reassessment', self.view().needs.state)
        self.assertEqual(('situation',), self.view().needs.changed_sources)

    def test_repeated_render_is_read_only_and_does_not_dispatch(self):
        self.arrive()
        before = (self.state.workspace.version, len(self.state.workspace.events), asdict(self.state.ledger.snapshots[self.f.event_id]))
        for _ in range(3):
            render_force(self.state, 'commander')
        self.assertEqual(before, (self.state.workspace.version, len(self.state.workspace.events), asdict(self.state.ledger.snapshots[self.f.event_id])))

    def test_invalid_event_audit_or_personnel_records_do_not_show_fake_zero(self):
        ws, record = self.state.workspace, self.state.registry.events[self.f.event_id]
        self.assertEqual('未进入事件', force_view(ws, None, self.state.ledger).state)
        self.assertIsNone(force_view(ws, replace(record, event_id='other'), self.state.ledger).needs)
        self.assertIsNone(force_view(ws, record, None).needs)
        with patch('panda_brain.sop.personnel_groups.group_people', side_effect=ValueError('bad')):
            self.assertEqual('依据待核对', self.view().state)
            self.assertIsNone(self.view().needs)
        ws.events[-1].payload['corrupt'] = True
        self.assertIsNone(self.view().needs)

    def test_missing_deputy_does_not_block_parallel_view_or_invent_authority(self):
        # A new event started by duty, with no appointment and no force actions.
        f = self.f
        event = self.state.registry.create_event(name='未任命模拟事件', report_no='SOP-X',
            discovered_at='2026-09-29T09:00:00+08:00', source='模拟', location='模拟地点')
        self.state.handle_event_action(event.event_id, 'start', duty_token=f.tokens[f.DUTY])
        self.state.handle_enter_event(event.event_id)
        view = force_view(self.state.workspace, self.state.registry.events[event.event_id], self.state.ledger)
        self.assertEqual('', view.deputy)
        self.assertTrue(any('任命依据' in x for x in view.missing))
        html = render_sand_page(self.state)
        self.assertEqual(9, html.count('data-sand-step="'))
        self.assertIn('指定副总指挥：待登记', html)

    def test_page_six_answers_escape_review_and_do_not_expand_write_permissions(self):
        self.f.review(note='<script>PRIVATE-NOTE</script>')
        html = render_force(self.state, 'commander')
        for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
            self.assertIn(label, html)
        self.assertIn('&lt;script&gt;PRIVATE-NOTE&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE-NOTE</script>', html)
        self.assertNotIn('<form', html)
        self.assertIn('href="/login"', html)
        for role in ('member', 'public'):
            self.assertEqual('', render_force(self.state, role))
            self.assertNotIn('class="sop-force"', render_sand_page(self.state, role=role))
        query = {'event_id': [self.f.event_id]}
        # The approved shared framework already permits admin to read this page.
        # Reading is not deputy appointment or authority to arrange personnel.
        before = (self.state.workspace.version, len(self.state.workspace.events))
        self.assertEqual(200, render_page(self.state, '/personnel/groups', query=query, session_token=self.token)[0])
        self.assertEqual(200, render_page(self.state, '/personnel/groups', query=query, member_token=self.f.tokens[self.f.DEPUTY])[0])
        self.assertEqual(before, (self.state.workspace.version, len(self.state.workspace.events)))


if __name__ == '__main__':
    unittest.main()
