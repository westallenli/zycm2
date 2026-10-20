"""Current delivery acceptance for the three approved October 18 fixes."""
from dataclasses import asdict,replace
import json,os,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain import intelligence,nine_flow,review_flow_view,sandtable
from panda_brain.grounded_inquiry import validate_sources
from panda_brain.panel import PanelActionError
from panda_brain.resource_demo import DEPUTY,open_state


class RoutingTests(unittest.TestCase):
    def test_missing_information_intent_precedes_decisions(self):
        for q in ('当前决策缺什么资料？','当前还缺哪些资料？','决策缺项有哪些？',
                  '方案还需要补充什么信息？','信息缺口有哪些？','当前决策还缺什么？'):
            with self.subTest(q=q):self.assertEqual('gaps',sandtable.route_question(q)[0])
    def test_normal_decisions_resources_and_gap_resolution_are_not_hijacked(self):
        for q,topic in (('最近决策是什么？','decisions'),('比较三个方案','decisions'),
                        ('物资还缺多少？','materials'),('人员缺口有多少？','personnel'),
                        ('哪个决策解决了信息缺口？','decisions')):
            with self.subTest(q=q):self.assertEqual(topic,sandtable.route_question(q)[0])


class LatestDeliveryFixTests(unittest.TestCase):
    def setUp(self):
        # Explicit current-delivery copy only; never silently use a historical demo.
        root=os.environ.get('PZ_LATEST_TEST_ROOT')
        if not root:raise RuntimeError('PZ_LATEST_TEST_ROOT must name the isolated current delivery')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        data=Path(self.tmp.name)/'data';shutil.copytree(Path(root)/'data',data)
        reg=data/'registry.json';v=json.loads(reg.read_text(encoding='utf-8'))
        for event in v['events'].values():event['snapshot_path']=str(data/'snapshots'/Path(event['snapshot_path']).name)
        reg.write_text(json.dumps(v,ensure_ascii=False),encoding='utf-8')
        self.state=open_state(data);self.token,error=self.state.member_login(DEPUTY,'123456')
        self.assertFalse(error);self.eid=self.state.active_event_id
    def files(self):
        return {str(p):p.read_bytes() for p in self.state.registry_path.parent.rglob('*') if p.is_file()}
    def section(self):
        return review_flow_view.markdown(self.state).split('## 六、待核实与后续事项',1)[1].split('## 七、',1)[0]
    def ask(self,q):
        return self.state.handle_grounded_sand_ask(q,role='member',member_token=self.token)
    def test_report_lists_actual_supply_shortages_and_handover(self):
        before=self.files();section=self.section()
        for text in ('餐食','30','照明','4','跟进','来源','看守'):
            self.assertIn(text,section)
        self.assertNotIn('当前无已登记待办',section);self.assertEqual(before,self.files())
    def test_report_omits_verified_and_superseded_tasks(self):
        section=self.section()
        self.assertNotIn('待完成任务：',section)
        self.assertNotIn('第1版',section)
    def test_new_report_preserves_old_versions_and_frozen_pending(self):
        old={k:asdict(v) for k,v in self.state.review_ledger.reports.items()}
        self.state._pending_review_actor=('owner','模拟负责人',self.eid,self.token)
        rid=self.state.handle_report_generate(stage=True)
        report=self.state.review_ledger.reports[rid]
        self.assertIn('餐食',report.content_md.split('## 六、')[1]);self.assertTrue(report.source_snapshot_json)
        self.assertTrue(json.loads(report.source_snapshot_json)['event_pending'])
        for k,v in old.items():self.assertEqual(v,asdict(self.state.review_ledger.reports[k]))
        frozen=report.content_md
        self.state.workspace.append_event('FieldObservation',event_id=self.eid,statement='模拟新增资料，不改旧报告')
        self.assertEqual(frozen,report.content_md)
        from panda_brain.review_reports import freshness_error
        self.assertIn('变化',freshness_error(self.state,report))
    def test_gap_questions_share_panel_sources_and_numbered_answers(self):
        expected=intelligence.collect(self.state,self.eid)[0]['gaps'];before=self.state.workspace.version
        for q in ('当前决策缺什么资料？','当前还缺哪些资料？'):
            answer=self.ask(q)
            self.assertEqual('gaps',answer.topic);self.assertIn('1.',answer.conclusion)
            self.assertIn(str(len(expected)),answer.conclusion)
            self.assertTrue(answer.sources)
            self.assertNotIn('最新一条',answer.conclusion)
            self.assertIn('待核',answer.unconfirmed)
        self.assertEqual(before,self.state.workspace.version)
    def test_gap_citation_rejects_tampering_stale_event_and_unprivileged_reader(self):
        answer=self.ask('当前还缺哪些资料？')
        args=(self.state.workspace,self.state.ledger,self.eid,'member')
        opts=dict(actor_id=DEPUTY,deputy=True,registry=self.state.registry,state=self.state)
        self.assertTrue(validate_sources(answer,*args,**opts))
        with self.assertRaises(ValueError):validate_sources(replace(answer,sources=('decision-gaps:forged',)),*args,**opts)
        with self.assertRaises(ValueError):validate_sources(answer,*args,**(opts|dict(deputy=False)))
        self.state.workspace.append_event('FieldObservation',event_id=self.eid,statement='模拟新资料')
        with self.assertRaises(ValueError):validate_sources(answer,*args,**opts)
        with self.assertRaises(ValueError):validate_sources(answer,self.state.workspace,self.state.ledger,'another','member',**opts)
    def test_gap_inquiry_requires_valid_event_session(self):
        self.state.member_logout(self.token)
        with self.assertRaises(PanelActionError):self.ask('当前决策缺什么资料？')
    def test_model_chat_retains_checked_gap_source_with_fake_provider(self):
        from panda_brain import sand_command
        class Provider:
            name='local-test-only'
            def complete(self,**kwargs):return json.dumps(dict(answer='请逐项核对已有缺项。',unknown='现场资料待核实'))
        answer=self.ask('当前决策缺什么资料？');before=self.state.workspace.version
        with patch.object(sand_command,'configured_model',return_value=(Provider(),'test-binding',self.token)):
            preview=sand_command.model_preview(self.state,self.token,'当前决策缺什么资料？',answer)
            result=sand_command.model_confirm(self.state,self.token,preview['token'])
        self.assertEqual(answer.sources,result['sources']);self.assertEqual(before,self.state.workspace.version)
        self.assertIn('待核实',result['unconfirmed'])
    def test_unavailable_supply_sources_are_pending_instead_of_empty(self):
        with patch('panda_brain.resource_advisor.assess',side_effect=ValueError('模拟来源损坏')):
            self.assertIn('保障资料待核对',self.section())


class CurrentTaskPendingTests(unittest.TestCase):
    def test_newly_published_current_task_has_owner_deadline_and_source(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        fixture=TaskReleaseTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.record();fixture.safety();value=json.loads(fixture.publish().content_json)
        state=fixture.state
        from panda_brain.review_pending import business_pending
        before=fixture.files();lines='\n'.join(business_pending(state))
        self.assertIn(value['tasks'][0]['spec']['title'],lines)
        for text in ('待完成任务：','责任','时间要求','来源'):self.assertIn(text,lines)
        self.assertEqual(before,fixture.files())


class CurrentHandoverPendingTests(unittest.TestCase):
    def test_latest_resolved_checkpoint_replaces_previous_remaining_items(self):
        from tests.test_upgrade_nine_flow import NineFlowTests
        fixture=NineFlowTests();fixture.setUpClass();self.addCleanup(fixture.tearDownClass)
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        first=fixture.obs(8)
        proposal=fixture.submit(first,step=8,values=dict(result='handover',remaining='模拟未交接事项',followup='具名接收人明日复核'))
        fixture.accept(proposal,step=8)
        from panda_brain.review_pending import business_pending
        self.assertIn('模拟未交接事项','\n'.join(business_pending(fixture.state)))
        second=fixture.obs(8,ident='b')
        proposal=fixture.submit(second,step=8,ident='b',expected_version=2)
        fixture.accept(proposal,step=8,ident='b')
        self.assertNotIn('模拟未交接事项','\n'.join(business_pending(fixture.state)))
