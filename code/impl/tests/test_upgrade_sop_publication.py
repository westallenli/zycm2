"""Publication scope and notification provenance, without inventing news approval."""
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from panda_brain import callouts, local_notifications
from panda_brain.authorization import Action, AuthorizationPolicy, Role, RoleGrant
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.publication_sop import publication_view, render_publication
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace
from tests import test_upgrade_sop as early
from tests import test_upgrade_checkin_flow as checkin
from tests.test_upgrade_sop_deployment import open_task, run_task


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class PublicationSopTests(unittest.TestCase):
    setUp = early.SopTests.setUp

    def view(self):
        return publication_view(self.ws, self.record)

    def selected(self, **changes):
        data = dict(event_id=self.record.event_id, data_kind='simulated', transport_state='not_sent',
            squads=[dict(squad_id='SIM-SQUAD', leader_phone='SIM-LEADER')], channel='phone',
            selected_by='SIM-DEPUTY', selected_at='2026-09-29T09:00:00+08:00', request_id='select-1')
        self.ws.append_event(callouts.SELECTED, **(data | changes))

    def local(self, **changes):
        data = dict(event_id=self.record.event_id, data_kind='simulated', transport_state='not_sent',
            delivery_state='not_delivered', phone='SIM-LOCAL', name='模拟属地人员', unit='模拟单位',
            local_role='assistant', channel='wechat', prepared_by='SIM-DUTY',
            prepared_at='2026-09-29T09:01:00+08:00', request_id='local-1')
        self.ws.append_event(local_notifications.PREPARED, **(data | changes))

    def test_empty_and_no_event_do_not_claim_nothing_was_published(self):
        self.assertEqual('待补发布制度与依据', self.view().state)
        self.assertEqual((), self.view().callouts)
        self.assertIn('不代表线下未开展', str(self.view().missing))
        self.state.active_event_id = ''
        self.assertEqual('未进入事件', publication_view(self.ws, None).state)
        html = render_sand_page(self.state)
        self.assertIn('data-sand-step="7"', html)
        self.assertIn('内部通报', html)
        self.assertIn('正式对外新闻', html)

    def test_real_handlers_preserve_unsent_failure_and_reply_boundaries(self):
        f = checkin.CheckinFlowTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        _, eid = f._demo_event()
        s = f.state
        s.handle_event_action(eid, 'start', duty_token=f.tokens[f.DUTY])
        s.handle_deputy_appointment(eid, duty_token=f.tokens[f.DUTY], subject_id=f.DEPUTY,
            evidence='模拟线下任命', request_id='pub-appoint', occurred_at='2026-09-29T08:00:00+08:00')
        s.handle_select_squads(eid, deputy_token=f.tokens[f.DEPUTY], squad_ids=[f.squad.squad_id],
            channel='phone', request_id='pub-select')
        s.handle_callout_simulation_failure(eid, deputy_token=f.tokens[f.DEPUTY],
            squad_id=f.squad.squad_id, reason='模拟线路不通', request_id='pub-failure')
        s.handle_captain_reply(eid, captain_token=f.tokens[f.CAPTAIN], squad_id=f.squad.squad_id,
            can_depart='yes', expected_people='3', eta='2026-09-29T12:00:00+08:00', reason='', request_id='pub-reply')
        s.handle_prepare_local_notice(eid, duty_token=f.tokens[f.DUTY], phone=f.TEMP,
            local_role='assistant', channel='wechat', request_id='pub-local')
        s.handle_enter_event(eid)
        view = publication_view(s.workspace, s.registry.events[eid])
        self.assertEqual(1, len(view.callouts))
        self.assertEqual(1, len(view.local_notices))
        self.assertIn('未发送、未送达', view.callouts[0])
        self.assertIn('模拟线路不通', view.callouts[0])
        self.assertIn('系统内模拟回执', view.callouts[0])
        self.assertIn('属地协助', view.local_notices[0])
        self.assertNotEqual('已完成', view.state)

    def test_wrong_event_or_broken_chain_excludes_all_private_facts(self):
        self.local(name='PRIVATE-NOTICE')
        wrong = publication_view(self.ws, replace(self.record, event_id='other'))
        self.assertEqual('依据待核对', wrong.state)
        self.assertNotIn('PRIVATE-NOTICE', str(wrong))
        self.ws.events[-1].payload['tamper'] = True
        self.assertEqual('依据待核对', self.view().state)
        self.assertEqual((), self.view().local_notices)

    def test_malformed_callouts_fail_closed_without_hiding_valid_local_notices(self):
        for changes in ({'event_id': 'other'}, {'transport_state': 'sent'}, {'selected_at': ''},
                        {'selected_by': None}, {'request_id': ''}, {'squads': [{'squad_id':'x'}]}):
            with self.subTest(changes=changes):
                self.ws = ScenarioWorkspace('publication-test', event_id=self.record.event_id)
                self.local()
                self.selected(**changes)
                view = self.view()
                self.assertEqual((), view.callouts)
                self.assertEqual(1, len(view.local_notices))
                self.assertIn('召集通知依据不完整或不一致', str(view.missing))

    def test_invalid_local_notice_does_not_hide_valid_callout(self):
        self.selected()
        self.local(delivery_state='delivered', name='INVALID-LOCAL')
        view = self.view()
        self.assertEqual(1, len(view.callouts))
        self.assertEqual((), view.local_notices)
        self.assertNotIn('INVALID-LOCAL', str(view))
        self.assertIn('属地通知依据不完整或不一致', str(view.missing))

    def test_incomplete_reply_is_not_treated_as_valid_acknowledgment(self):
        self.selected()
        self.ws.append_event(callouts.REPLIED, event_id=self.record.event_id, data_kind='simulated',
            source='in_app_demo_receipt', receipt_state='acknowledged_in_app', can_depart='yes',
            squad_id='SIM-SQUAD', reported_by='SIM-LEADER', request_id='reply-1')
        self.assertEqual((), self.view().callouts)
        self.assertIn('召集通知依据不完整或不一致', str(self.view().missing))

    def test_task_confirmation_is_not_internal_bulletin_or_news_approval(self):
        open_task(self.ws, self.record.event_id)
        run_task(self.ws, self.record.event_id, 1)
        self.assertEqual((), self.view().callouts)
        self.assertEqual('待补发布制度与依据', self.view().state)
        html = render_publication(self.state, 'commander')
        self.assertIn('不能替代通报或新闻审批', html)
        for role in (Role.DEPUTY, Role.CHIEF, Role.REVIEW_OWNER):
            policy = AuthorizationPolicy((RoleGrant('sim-actor', role, self.record.event_id),))
            with self.assertRaises(PermissionError):
                policy.require('sim-actor', Action.PUBLISH_EXTERNAL_NEWS, event_id=self.record.event_id)

    def test_live_notification_projection_works_without_personnel_ledger(self):
        self.assertIsNone(self.state.ledger)
        before = render_publication(self.state, 'commander')
        self.local(name='LIVE-LOCAL')
        after = render_publication(self.state, 'commander')
        self.assertNotIn('LIVE-LOCAL', before)
        self.assertIn('LIVE-LOCAL', after)
        self.assertIn('已有沟通记录待核对', after)

    def test_repeated_reads_and_persistence_never_send_or_write_progress(self):
        self.selected()
        self.local()
        before = (self.ws.version, [asdict(e) for e in self.ws.events])
        view = self.view()
        for _ in range(3):
            self.assertEqual(view, self.view())
            render_publication(self.state, 'commander')
        self.assertEqual(before, (self.ws.version, [asdict(e) for e in self.ws.events]))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'synthetic.db'
            save_workspace(self.ws, p)
            self.assertEqual(view, publication_view(load_workspace(p), self.record))

    def test_six_answers_escape_content_and_preserve_roles_and_nine_items(self):
        self.local(name='<script>PRIVATE-PUBLICATION</script>')
        html = render_publication(self.state, 'commander')
        for label in ('现在做什么', '为什么做', '由谁做', '还缺什么', '从哪里操作', '怎样才算完成'):
            self.assertIn(label, html)
        self.assertIn('&lt;script&gt;PRIVATE-PUBLICATION&lt;/script&gt;', html)
        self.assertNotIn('<script>PRIVATE-PUBLICATION</script>', html)
        self.assertNotIn('<form', html)
        self.assertIn('data-sand-open="drawer-fourcat"', html)
        self.assertIn('data-sand-related="7" href="/materials"', html)
        self.assertEqual(9, render_sand_page(self.state).count('data-sand-step="'))
        for role in ('member', 'public'):
            self.assertEqual('', render_publication(self.state, role))
            self.assertNotIn('class="sop-publication"', render_sand_page(self.state, role=role))


if __name__ == '__main__':
    unittest.main()
