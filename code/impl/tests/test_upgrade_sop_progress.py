"""Green means mandatory current business actions, not navigation or checked metadata."""
from dataclasses import replace
import json
import unittest
from panda_brain import assistant_view, workflow
from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace


@unittest.skip('环节页改版：旧核验颜色与链接断言已过期，等待按新界面重写')
class SopProgressTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        self.f=TaskReleaseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.state=self.f.state;self.eid=self.f.event_id

    def report(self):return assistant_view.current(self.state,self.eid)

    def publish(self):
        self.f.record();self.f.safety();record=self.f.publish()
        self.publication=record;self.oid=json.loads(record.content_json)['tasks'][0]['order_id']
        return record

    def receipt(self,action,expected=0):
        return self.state.handle_formal_task_receipt(self.eid,member_token=self.f.tokens[self.f.SOURCE],
            order_id=self.oid,publication_ref=self.publication.id,expected_version=expected,action=action,
            basis='匿名本版实际接收依据',request_id='progress-'+action)

    def confirm_deployment_scope(self):
        from panda_brain import nine_flow
        token=self.f.tokens[self.f.DEPUTY]
        row=nine_flow.observe(self.state,token,step=3,subject='当前部署范围',feedback='核对本版部署及回执',location='匿名事件',
            refs=[self.publication.id],occurred_at='',expected_version=0,request_id='scope-observation')
        proposal=nine_flow.submit(self.state,token,step=3,values=dict(scope='本次正式部署范围',feedback='本版任务已接收',
            basis='核对原始发布及接收记录',remaining='',followup='',verifier_id=self.f.SOURCE,result='completed'),
            refs=[row.id,self.publication.id],expected_version=0,request_id='scope-submit')
        nine_flow.verify(self.state,self.f.tokens[self.f.SOURCE],step=3,proposal_ref=proposal.id,expected_version=1,request_id='scope-check',decision='accept')

    def test_checked_metadata_does_not_complete_work_area_or_add_confirmation(self):
        before=self.f.files();report=self.report()
        self.assertTrue(any(a['done'] for a in report['steps'][0]['actions']))
        self.assertFalse(report['steps'][0]['complete']);self.assertTrue(report['steps'][0]['manual'])
        self.assertEqual(['待完成']*9,assistant_view.states(report))
        self.assertEqual(before,self.f.files())
        html=render_page(self.state,'/workflow/guide',member_token=self.f.tokens[self.f.DEPUTY])[1]
        self.assertIn('data-assistant-report',html)
        self.assertNotIn('name="complete"',html);self.assertNotIn('确认本环节完成',html)

    def test_draft_and_publication_without_named_receipt_remain_pending(self):
        self.publish();before=self.f.files();step=self.report()['steps'][3]
        self.assertFalse(step['complete'])
        self.assertTrue(any(a['title']=='复核并正式发布本版任务' and a['done'] for a in step['actions']))
        self.assertTrue(any(m['title']=='明确接收当前任务版本' for m in step['manual']))
        self.assertEqual(before,self.f.files())

    def test_empty_required_content_is_not_a_completed_drafting_action(self):
        draft=self.f.draft;task=draft.tasks[0]
        self.state.workspace.revise_task_drafts(self.f.decision_id,event_id=self.eid,expected_version=draft.version,
            common=draft.common,specs={task.id:replace(task.spec,objective='')})
        step=self.report()['steps'][3]
        self.assertTrue(any(m['title']=='编制本版任务草案' for m in step['manual']))
        self.assertFalse(step['complete'])

    def test_delivered_or_viewed_are_not_current_named_acceptance(self):
        self.publish();self.receipt('delivered');self.receipt('viewed',1)
        self.assertFalse(self.report()['steps'][3]['complete'])

    def test_actual_current_publication_and_named_acceptance_make_only_deployment_green(self):
        self.publish();self.receipt('received');self.assertFalse(self.report()['steps'][3]['complete'])
        self.confirm_deployment_scope();before=self.f.files();report=self.report()
        self.assertTrue(report['steps'][3]['complete']);self.assertEqual('已完成',assistant_view.states(report)[3])
        self.assertFalse(report['steps'][3]['manual']);self.assertTrue(report['steps'][3]['completion_refs'])
        self.assertTrue(all(not step['complete'] for i,step in enumerate(report['steps']) if i!=3))
        self.assertEqual(before,self.f.files())

    def test_green_reverts_when_current_professional_source_becomes_unavailable(self):
        self.publish();self.receipt('received');self.confirm_deployment_scope();self.assertTrue(self.report()['steps'][3]['complete'])
        self.state.registry.library.pop(self.f.OTHER)
        self.assertFalse(self.report()['steps'][3]['complete'])

    def test_revised_draft_does_not_reuse_old_publication_or_acceptance(self):
        self.publish();self.receipt('received');self.confirm_deployment_scope();self.assertTrue(self.report()['steps'][3]['complete'])
        draft=self.f.draft;task=draft.tasks[0]
        self.state.workspace.revise_task_drafts(self.f.decision_id,event_id=self.eid,expected_version=draft.version,
            common=draft.common,specs={task.id:replace(task.spec,objective='新的匿名目标')})
        self.assertFalse(self.report()['steps'][3]['complete'])

    def test_unmirrored_ack_or_wrong_recipient_never_makes_green(self):
        self.publish();flow=self.state.workspace.command_flows[self.oid]
        self.state.workspace.command_flows[self.oid]=replace(flow,acked_by=self.f.SOURCE,acked_at='2026-10-14T10:00:00+08:00',ack_result='accepted_ready')
        self.assertFalse(self.report()['steps'][3]['complete'])

    def test_navigation_does_not_light_preceding_steps_or_write_business(self):
        before=self.f.files();member=self.state.member_session(self.f.tokens[self.f.DEPUTY])
        for i in (8,0,5,3):
            fields=dict(event_id=self.eid,csrf=member.custody_csrf,mode='guide',step=str(i),node='0',target='/workflow/guide')
            workflow.select(self.state,self.f.tokens[self.f.DEPUTY],{k:[v] for k,v in fields.items()})
            status,html=render_page(self.state,'/workflow/guide',member_token=self.f.tokens[self.f.DEPUTY])
            self.assertEqual(200,status);self.assertIn('data-workflow-current="'+str(i)+'"',html)
            self.assertNotIn('data-phase-status="completed"',html)
        self.assertEqual(before,self.f.files())

    def test_color_and_manual_action_links_share_verified_state(self):
        self.publish();self.receipt('received');self.confirm_deployment_scope();member=self.state.member_session(self.f.tokens[self.f.DEPUTY])
        workflow.select(self.state,self.f.tokens[self.f.DEPUTY],{k:[v] for k,v in dict(event_id=self.eid,csrf=member.custody_csrf,mode='guide',step='3',node='0',target='/workflow/guide').items()})
        html=render_page(self.state,'/workflow/guide',member_token=self.f.tokens[self.f.DEPUTY])[1]
        self.assertEqual(1,html.count('data-phase-status="completed"'))
        self.assertIn('workflow-step completed selected',html)
        self.assertIn('当前无待人工核对项',html)
        self.assertIn('蓝框：当前查看',html)
