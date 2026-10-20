"""P07-06: sandview shows only bound-event, role-scoped read-only prompts."""
import unittest
from types import SimpleNamespace

from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.fire_events import EventRegistry
from panda_brain.models import PersonRecord
from panda_brain.offline_tasks import record_control, record_offline
from panda_brain.radio_reports import record_simulated_call
from panda_brain.radio_task_candidates import record_task_candidate
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace


class SandRadioFollowupTests(unittest.TestCase):
    def setUp(self):
        registry = EventRegistry()
        registry.import_person_library([
            PersonRecord(name='模拟副总指挥', phone='13977771002', unit='前指'),
            PersonRecord(name='模拟队长', phone='13977771003', unit='模拟队伍', is_leader=True),
        ])
        event = registry.create_event(
            name='模拟北段事件', report_no='P07-SAND', source='人工登记',
            discovered_at='2026-09-29T09:00:00+08:00', location='模拟地点')
        workspace = ScenarioWorkspace('p07-sand', event_id=event.event_id)
        self.state = SimpleNamespace(
            workspace=workspace, registry=registry,
            active_event_id=event.event_id, imagery=None, ledger=None,
            sand_qa_log=[], intent_notice='', voice_drafts={}, advisor_log=[])
        self.auth = EventAuthorizationService(
            workspace, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda person: person in {'duty', '13977771002'})
        self.auth.record_initial_appointment(
            'duty', event_id=event.event_id, role=Role.DEPUTY,
            subject_id='13977771002', evidence='模拟任命依据', request_id='appoint-sand')
        self.task_id = record_offline(
            workspace, self.auth, '13977771002', event_id=event.event_id,
            content='观察北段', group_id='squad-a',
            issued_by='模拟指挥员', recipient_id='13977771003',
            verifier_id='verifier', occurred_at='2026-09-29T09:00:00+08:00',
            due_at='2026-09-29T10:00:00+08:00',
            evidence='模拟线下指令', request_id='offline-sand')['task_id']
        record_simulated_call(
            workspace, event_id=event.event_id,
            source_call_id='call-private', source_ref='simulated-audio-private',
            occurred_at='2026-09-29T09:30:00+08:00',
            channel_id='single-channel', transcript='北段进展待核。',
            task_ids=(self.task_id,))
        record_task_candidate(
            workspace, event_id=event.event_id,
            source_call_id='call-private', task_id=self.task_id,
            segment_id='segment-1', task_version=1,
            proposed_action='progress', quoted_text='北段进展待核',
            annotated_by='sim-reviewer', basis='模拟标注',
            request_id='candidate-sand')

    def test_commander_sees_summary_and_evidence_without_write_actions(self):
        before = (self.state.workspace.version, len(self.state.workspace.events))
        html = render_sand_page(self.state)
        self.assertIn('待核对追问 2 项 · 查看依据', html)
        self.assertIn('<div class="sand-shell-meta"><b>模拟北段事件</b><a class="sand-followup-pill"', html)
        self.assertIn('id="drawer-followups"', html)
        self.assertIn('13977771003', html)
        self.assertIn('13977771002', html)
        self.assertIn('模拟队长（13977771003）', html)
        self.assertIn('模拟副总指挥（13977771002）', html)
        self.assertIn('simulated-audio-private', html)
        self.assertIn('任务当前版记录的接收人（现任身份待核）', html)
        self.assertIn('查看不等于处理完成', html)
        self.assertNotIn('action="/sand/followup-send"', html)
        self.assertEqual(before, (self.state.workspace.version,
                                  len(self.state.workspace.events)))

    def test_member_only_sees_own_prompt_and_public_sees_none(self):
        member = render_sand_page(self.state, role='member', viewer_id='13977771003')
        self.assertIn('待核对追问 1 项 · 查看依据', member)
        self.assertIn('13977771003', member)
        self.assertNotIn('simulated-audio-private', member)
        other = render_sand_page(self.state, role='member', viewer_id='other')
        self.assertIn('待核对追问 0 项 · 查看依据', other)
        self.assertNotIn('simulated-audio-private', other)
        public = render_sand_page(self.state, role='public')
        self.assertNotIn('drawer-followups', public)
        self.assertNotIn('simulated-audio-private', public)
        self.assertNotIn('待核对追问 2 项', public)

    def test_cross_event_binding_and_withdrawal_recalculate_without_mutation(self):
        other = self.state.registry.create_event(
            name='模拟其他事件', report_no='P07-OTHER', source='人工登记',
            discovered_at='2026-09-29T09:01:00+08:00', location='其他地点')
        self.state.active_event_id = other.event_id
        html = render_sand_page(self.state)
        self.assertNotIn('simulated-audio-private', html)
        self.assertIn('请先进入当前事件任务空间', html)
        self.state.active_event_id = self.state.workspace.event_id
        record_control(
            self.state.workspace, self.auth, '13977771002',
            event_id=self.state.workspace.event_id, task_id=self.task_id,
            expected_version=1, action='withdraw',
            occurred_at='2026-09-29T10:30:00+08:00',
            reason='模拟撤回', request_id='withdraw-sand')
        html = render_sand_page(self.state)
        self.assertIn('待核对追问 0 项 · 查看依据', html)
        self.assertNotIn('simulated-audio-private', html)


if __name__ == '__main__':
    unittest.main()
