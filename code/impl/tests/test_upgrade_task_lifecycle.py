"""P06-01: adoption creates drafts, never publication or task completion."""
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import DecisionActor, DecisionType, GENERAL_ENTRY_ID, Option
from panda_brain.panel import PanelState
from panda_brain.persistence import decode_entity, encode_entity, load_workspace, save_workspace
from panda_brain.record_contracts import explicit_time
from panda_brain.task_drafts import PlanTaskBlueprint, TaskCommonContent, TaskDraftSpec
from panda_brain.workspace import ScenarioWorkspace


class PlanTaskDraftTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('simulation', event_id='event-a')
        self.option = Option('plan-a', '模拟北线方案', '目标摘要，不是完成标准',
                             ('evidence-unknown',), ('队伍描述不是编制',),
                             time_estimate='约两小时',
                             sim_shapes=({'kind': 'line', 'geo': [[32.1, 110.2]]},))
        self.ws.add_option(self.option)

    def decide(self, kind=DecisionType.ADOPT, **changes):
        args = dict(decision_type=kind, option_id=self.option.id, reason='模拟选择',
                    scope='当前事件', actor_role=DecisionActor.COMMANDER,
                    decided_by=GENERAL_ENTRY_ID)
        args.update(changes)
        return self.ws.record_decision(**args)

    def draft(self, decision):
        return self.ws.task_drafts_for_decision(decision.id, event_id='event-a')

    def test_simple_adoption_creates_one_unpublished_main_with_honest_gaps(self):
        draft = self.draft(self.decide())
        self.assertEqual('draft', draft.status)
        self.assertEqual('event-a', draft.event_id)
        self.assertEqual(1, len(draft.tasks))
        main = draft.tasks[0]
        self.assertEqual('', main.parent_id)
        self.assertEqual(self.option.summary, main.spec.objective)
        self.assertEqual('', main.spec.completion_standard)
        self.assertEqual('', main.spec.responsible_group_id)
        self.assertEqual('', main.spec.verifier_id)
        self.assertEqual(TaskCommonContent(), draft.resolved_content(main.id))
        self.assertIsNotNone(explicit_time(draft.recorded_at))
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.command_flows)
        self.assertEqual({}, self.ws.action_contracts)
        self.assertEqual({}, self.ws.approvals)
        self.assertEqual({}, self.ws.reports)
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_explicit_independent_children_and_common_references(self):
        common = TaskCommonContent(safety_measures='专业待复核措施',
                                   command_relationship='指定现场指挥',
                                   support_measures='保障待落实')
        main = TaskDraftSpec(title='总任务', objective='整体目标',
                             completion_standard='整体目标核验标准',
                             responsible_group_id='group-main', verifier_id='person-v',
                             common_fields=('safety_measures', 'support_measures'))
        child = TaskDraftSpec(title='通信保障', objective='建立通信',
                              completion_standard='逐点通联报告',
                              responsible_group_id='group-radio', verifier_id='person-r',
                              content=TaskCommonContent(feedback_requirement='独立反馈'),
                              common_fields=('safety_measures', 'command_relationship'))
        self.option.task_blueprint = PlanTaskBlueprint(main, (child,), common)
        draft = self.draft(self.decide())
        self.assertEqual((main, child), tuple(t.spec for t in draft.tasks))
        self.assertEqual(draft.tasks[0].id, draft.tasks[1].parent_id)
        resolved = draft.resolved_content(draft.tasks[1].id)
        self.assertEqual(common.safety_measures, resolved.safety_measures)
        self.assertEqual('独立反馈', resolved.feedback_requirement)
        self.assertEqual('', resolved.support_measures)  # not implicitly inherited
        self.assertEqual('', resolved.location)  # non-spatial task is valid
        with self.assertRaises(ValueError):
            draft.resolved_content('other-task')

    def test_all_explicit_business_fields_are_preserved(self):
        content = TaskCommonContent(location='待核查北段范围', time_requirement='接令后待确认',
            feedback_requirement='按阶段报告', field_constraints='现场条件待核对',
            safety_measures='安全内容', firefighting_method='方法待专业判断',
            command_relationship='指挥关系', coordination='协助通报', support_measures='保障内容')
        self.option.task_blueprint = PlanTaskBlueprint(main=TaskDraftSpec(content=content))
        draft = self.draft(self.decide())
        self.assertEqual(content, draft.resolved_content(draft.tasks[0].id))

    def test_source_snapshot_preserves_estimates_evidence_and_simulated_geometry(self):
        self.option.risks = ('风险待专业复核',)
        draft = self.draft(self.decide(accepted_risks=self.option.risks))
        source = json.loads(draft.source_option_json)
        self.assertEqual(['风险待专业复核'], source['risks'])
        self.assertEqual(['evidence-unknown'], source['evidence_ids'])
        self.assertEqual('约两小时', source['time_estimate'])
        self.assertEqual(self.option.sim_shapes[0], source['sim_shapes'][0])
        self.option.summary = '后来更改的方案'
        self.option.sim_shapes[0]['geo'][0][0] = 0
        self.assertEqual(source, json.loads(draft.source_option_json))
        self.assertEqual(draft, self.draft(self.ws.decisions[draft.decision_id]))
        with self.assertRaises(FrozenInstanceError):
            draft.tasks[0].spec.objective = '改写'

    def test_repeated_reads_never_duplicate_or_regenerate_drafts(self):
        decision = self.decide()
        draft = self.draft(decision)
        events = self.ws.events
        self.option.task_blueprint = PlanTaskBlueprint(children=(TaskDraftSpec(),))
        for _ in range(3):
            self.assertEqual(draft, self.draft(decision))
        self.assertEqual(events, self.ws.events)
        self.assertEqual(1, len(self.ws.task_drafts))

    def test_reject_and_defer_do_not_create_tasks(self):
        for kind in (DecisionType.REJECT, DecisionType.DEFER):
            self.assertIsNone(self.draft(self.decide(kind)))
        self.assertEqual({}, self.ws.task_drafts)

    def test_unbound_legacy_adoption_does_not_invent_event_or_backfill(self):
        self.ws.event_id = ''
        decision = self.decide()
        self.assertEqual({}, self.ws.task_drafts)
        self.ws.event_id = 'event-a'
        self.assertIsNone(self.draft(decision))

    def test_missing_plan_and_invalid_blueprint_fail_without_partial_decision(self):
        events = self.ws.events
        with self.assertRaises(ValueError):
            self.decide(option_id='missing')
        self.option.task_blueprint = {'main': 'untyped'}
        with self.assertRaises(ValueError):
            self.decide()
        self.assertEqual(events, self.ws.events)
        self.assertEqual({}, self.ws.decisions)
        self.assertEqual({}, self.ws.task_drafts)

    def test_unauthorized_and_risk_not_accepted_fail_without_drafts(self):
        events = self.ws.events
        with self.assertRaises(PermissionError):
            self.decide(decided_by='someone-else')
        self.option.risks = ('风险',)
        with self.assertRaises(ValueError):
            self.decide()
        self.assertEqual(events, self.ws.events)
        self.assertFalse(self.ws.task_drafts)

    def test_cross_event_and_missing_event_read_rejected(self):
        decision = self.decide()
        for event in ('event-b', '', None):
            with self.assertRaises(ValueError):
                self.ws.task_drafts_for_decision(decision.id, event_id=event)

    def test_invalid_common_reference_and_multiple_principals_rejected(self):
        for args in (dict(common_fields=('verifier_id',)),
                     dict(common_fields=('location', 'location')),
                     dict(common_fields=('location',), content=TaskCommonContent(location='本地')),
                     dict(responsible_group_id=['group-a', 'group-b'])):
            with self.assertRaises(ValueError):
                TaskDraftSpec(**args)

    def test_snapshot_and_decision_tampering_is_not_trusted(self):
        decision = self.decide()
        draft = self.draft(decision)
        self.ws.task_drafts[draft.id] = replace(draft, status='published')
        with self.assertRaises(ValueError):
            self.draft(decision)
        self.ws.task_drafts[draft.id] = draft
        self.ws.decisions[decision.id] = replace(decision, reason='changed')
        with self.assertRaises(ValueError):
            self.draft(decision)

    def test_audit_tampering_rejects_reads_and_new_adoption_without_write(self):
        decision = self.decide()
        self.ws._events[-1].payload['bundle']['tasks'][0]['spec']['objective'] = '篡改'
        events = self.ws.events
        with self.assertRaises(ValueError):
            self.draft(decision)
        with self.assertRaises(ValueError):
            self.decide()
        self.assertEqual(events, self.ws.events)

    def test_sqlite_roundtrip_preserves_typed_children_source_and_audit(self):
        self.option.task_blueprint = PlanTaskBlueprint(children=(TaskDraftSpec(title='独立协作'),))
        decision = self.decide()
        draft = self.draft(decision)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'drafts.db'
            save_workspace(self.ws, db)
            self.ws = load_workspace(db)
        self.assertEqual(draft, self.draft(decision))
        self.assertIsInstance(self.ws.options['plan-a'].task_blueprint, PlanTaskBlueprint)
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_legacy_option_codec_does_not_invent_task_division(self):
        payload = json.loads(encode_entity(self.option))
        del payload['data']['task_blueprint']
        self.assertIsNone(decode_entity(json.dumps(payload)).task_blueprint)

    def test_authenticated_panel_adoption_generates_and_persists_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = GeneralEntryAuth(secret='test-p06-secret')
            ws = ScenarioWorkspace('simulation', event_id='event-a', general_auth=auth)
            ws.add_option(self.option)
            state = PanelState(ws, auth=auth, db_path=root/'state.db',
                               registry_path=root/'registry.json', materials_path=root/'materials.json',
                               reviews_path=root/'reviews.json', imagery_path=root/'imagery.json',
                               alarm_inbox_path=root/'alarms.json')
            from panda_brain.fire_events import FireEventRecord
            state.registry.events['event-a'] = FireEventRecord('event-a', '匿名活动草案事件', 'P06-fixture',
                '2026-09-29T09:00:00+08:00', '匿名隔离夹具', '模拟区域', status='active')
            state.active_event_id = 'event-a'
            with self.assertRaises(PermissionError):
                state.handle_plan_decision('plan-a', 'adopt')
            self.assertFalse(ws.task_drafts)
            state.login('test-p06-secret')
            decision_id = state.handle_plan_decision('plan-a', 'adopt')
            restored = load_workspace(root/'state.db')
            self.assertIsNotNone(restored.task_drafts_for_decision(decision_id, event_id='event-a'))
            self.assertEqual({}, restored.orders)


if __name__ == '__main__':
    unittest.main()
