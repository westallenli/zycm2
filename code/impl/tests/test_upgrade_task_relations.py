"""Anonymous actual publication versions, relation conflicts and start gates."""
from dataclasses import replace,asdict
import http.client,json,threading,unittest
from unittest.mock import patch
from urllib.parse import urlencode
from . import test_upgrade_task_release as fixture
from panda_brain.task_drafts import TaskDraftSpec
from panda_brain.task_relations import reference,published,history,current,control_history,gate
from panda_brain.panel import render_page,make_server
from panda_brain.persistence import save_workspace


class TaskRelationsTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','space','files','assess'):
        locals()[name]=getattr(fixture.TaskReleaseTests,name)

    def setUp(self):
        fixture.TaskReleaseTests.setUp(self)
        draft=self.draft
        draft=self.state.workspace.revise_task_drafts(self.decision_id,event_id=self.event_id,expected_version=1,
            common=draft.common,specs={t.id:t.spec for t in draft.tasks},add_child=True)
        specs={t.id:t.spec for t in draft.tasks}
        specs[draft.tasks[1].id]=replace(draft.tasks[0].spec,title='模拟协作子任务',objective='模拟协作目标',
            responsible_group_id=self.target_squad,verifier_id=self.DEPUTY)
        self.draft=self.state.workspace.revise_task_drafts(self.decision_id,event_id=self.event_id,expected_version=2,
            common=draft.common,specs=specs)
        save_workspace(self.state.workspace,self.root/'snapshots'/f'{self.event_id}.db')
        for i,task in enumerate(self.draft.tasks):
            self.state.handle_task_review_assessment(self.event_id,deputy_token=self.tokens[self.DEPUTY],
                decision_id=self.decision_id,task_id=task.id,expected_draft_version=3,expected_review_version=1 if i==0 else 0,
                category='routine',reason='匿名协同分类',basis_ref='匿名规则',request_id=f'relation-assess-{i}')
            for purpose,payload in (('rules',dict(category='routine',required_scopes=[],safety_conditions=['模拟通道已核对'])),
                                    ('safety',{'模拟通道已核对':True})):
                self.state.handle_task_release_action(self.event_id,member_token=self.tokens[self.OTHER],action='evidence',
                    decision_id=self.decision_id,task_id=task.id,draft_version=3,purpose=purpose,scope=self.scope,payload=payload,
                    evidence='匿名协同条件依据',reason='核对本版',expected_version=0,request_id=f'relation-{purpose}-{i}')
        self.publication=fixture.TaskReleaseTests.publish(self)
        tasks=list(published(self.space()).values())
        self.first=reference(tasks[0]);self.second=reference(tasks[1])

    def relate(self,**changes):
        return self.state.handle_task_relations(self.event_id,**(dict(member_token=self.tokens[self.DEPUTY],
            action='relations',task=self.first,dependencies=[],mutex=[],collaboration=[],basis='匿名关联变更依据',
            expected_version=0,request_id='relations-main')|changes))

    def receive(self,ref,actor):
        self.state.handle_formal_task_receipt(self.event_id,member_token=self.tokens[actor],order_id=ref['order_id'],
            publication_ref=ref['publication_ref'],expected_version=0,action='received',basis='匿名本版接收',request_id='ack-'+ref['order_id'])

    def control(self,ref=None,actor=None,action='conditions',**changes):
        return self.state.handle_task_relations(self.event_id,**(dict(member_token=self.tokens[actor or self.SOURCE],
            action=action,task=ref or self.first,basis='匿名执行条件已实际核对',expected_version=0,request_id='control-main-'+action)|changes))

    def test_current_refs_dependencies_block_start_and_collaboration_is_actual_child(self):
        self.relate(dependencies=[self.second],collaboration=[self.second])
        self.receive(self.first,self.SOURCE)
        before=self.files()
        with self.assertRaisesRegex(ValueError,'依赖任务'):self.control()
        self.assertEqual(before,self.files())
        self.assertEqual(self.second,current(self.space())[self.first['order_id']][1]['collaboration'][0])
        self.assertFalse(self.space().command_flows[self.first['order_id']].started_at)

    def test_cycle_self_duplicate_and_wrong_collaboration_reject_without_save(self):
        self.relate(dependencies=[self.second]);before=self.files()
        for values in (dict(task=self.second,dependencies=[self.first]),dict(task=self.second,collaboration=[self.first]),
                       dict(task=self.second,mutex=[self.second]),dict(task=self.second,dependencies=[self.first,self.first])):
            with self.assertRaises(ValueError):self.relate(request_id='bad-graph',**values)
            self.assertEqual(before,self.files())

    def test_mutual_exclusion_applies_from_both_sides_and_does_not_start_automatically(self):
        self.relate(mutex=[self.second])
        self.receive(self.first,self.SOURCE);self.receive(self.second,self.TARGET)
        self.control(); self.control(action='begin',expected_version=1)
        before=self.files()
        with self.assertRaisesRegex(ValueError,'互斥任务'):self.control(self.second,self.TARGET,request_id='second-conditions')
        self.assertEqual(before,self.files())
        self.assertEqual('running',self.space().command_flows[self.first['order_id']].stage)
        self.assertEqual('acked_not_started',self.space().command_flows[self.second['order_id']].stage)

    def test_change_invalidates_conditions_and_records_reminder_without_external_send(self):
        self.receive(self.first,self.SOURCE);self.control()
        self.relate(collaboration=[self.second])
        self.assertFalse(self.space().command_flows[self.first['order_id']].conditions_confirmed_at)
        with self.assertRaises(ValueError):self.control(action='begin',expected_version=1)
        self.control(expected_version=1,request_id='new-conditions')
        self.control(action='begin',expected_version=2)
        reminder=[e for e in self.space().events if e.kind=='TaskRelationReminderRecorded'][-1]
        self.assertEqual('local-only-not-sent',reminder.payload['notification'])
        with self.assertRaises(ValueError):self.relate(expected_version=1,request_id='change-running')

    def test_conditions_and_start_need_current_recipient_and_named_ack(self):
        before=self.files()
        with self.assertRaises(ValueError):self.control()
        with self.assertRaises(PermissionError):self.control(actor=self.DEPUTY)
        self.assertEqual(before,self.files())
        self.receive(self.first,self.SOURCE)
        with self.assertRaises(ValueError):self.control(action='begin')
        self.control();self.control(action='begin',expected_version=1)
        self.assertEqual(2,len(control_history(self.space())))

    def test_wrong_event_version_bad_types_and_unauthorized_cannot_change_relations(self):
        before=self.files()
        for ref in (self.second|dict(draft_version=99),self.second|dict(draft_version=True),self.second|dict(publication_ref='foreign-event')):
            with self.assertRaises(ValueError):self.relate(dependencies=[ref])
        with self.assertRaises(ValueError):self.relate(dependencies=None)
        with self.assertRaises(PermissionError):self.relate(member_token=self.tokens[self.SOURCE])
        self.assertEqual(before,self.files())

    def test_request_replay_zero_save_conflict_and_postwrite_failure_rollback(self):
        import panda_brain.persistence as persistence
        before=self.files();save=persistence.save_workspace
        def fail(*a,**kw):save(*a,**kw);raise OSError('anonymous relation post-write failure')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError):self.relate(collaboration=[self.second])
        self.assertEqual(before,self.files())
        first=self.relate(collaboration=[self.second]);before=self.files()
        with patch.object(persistence,'save_workspace',side_effect=AssertionError('replay saved')):
            self.assertEqual(first,self.relate(collaboration=[self.second]))
        with self.assertRaises(ValueError):self.relate(basis='不同依据')
        self.assertEqual(before,self.files())

    def test_stale_draft_invalidates_source_and_missing_receipt_source_blocks_start(self):
        self.receive(self.first,self.SOURCE)
        old=next(r for r in self.state.workspace.object_revisions.values() if r.reference.object_kind=='formal_task_receipt')
        self.state.workspace.object_revisions.pop(old.id)
        with self.assertRaises(ValueError):self.control()
        # Restore solely in the anonymous test fixture, then revise the draft.
        self.state.workspace.object_revisions[old.id]=old
        self.state.workspace.revise_task_drafts(self.decision_id,event_id=self.event_id,expected_version=3,common=self.draft.common,
            specs={t.id:replace(t.spec,objective='已变更目标') if t.id==self.task_id else t.spec for t in self.draft.tasks})
        save_workspace(self.state.workspace,self.root/'snapshots'/f'{self.event_id}.db')
        with self.assertRaises(ValueError):self.control()

    def test_missing_relation_mirror_get_fails_closed_with_readable_page(self):
        row=self.relate();before=self.files()
        self.state.workspace.object_revisions.pop(row.id)
        # Persist a damaged anonymous source; do not rebuild its audit mirror.
        save_workspace(self.state.workspace,self.root/'snapshots'/f'{self.event_id}.db')
        code,page=render_page(self.state,route='/tasks/relations',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(409,code);self.assertNotIn('保存本版关联',page)
        self.assertIn('不一致',page)

    def test_get_is_readonly_http_csrf_duplicates_unknown_and_actual_relations(self):
        before=self.files()
        code,page=render_page(self.state,route='/tasks/relations',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code);self.assertIn('模拟协作子任务',page);self.assertEqual(before,self.files())
        self.assertEqual(404,render_page(self.state,route='/tasks/relations')[0])
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(event_id=self.event_id,action='relations',task=json.dumps(self.first),expected_version='0',
            request_id='http-relations',basis='匿名实际HTTP关联',csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf,
            collaboration=json.dumps(self.second))
        def send(values,token=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            conn.request('POST','/tasks/relations',urlencode(values,doseq=True),
                {'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.tokens[self.DEPUTY])})
            resp=conn.getresponse();resp.read();conn.close();return resp.status
        self.assertEqual(403,send(fields|dict(csrf='bad')))
        self.assertEqual(409,send(fields|dict(expected_version=['0','0'])))
        self.assertEqual(409,send(fields|dict(actor='admin')))
        self.assertEqual(403,send(fields,self.tokens[self.SOURCE]))
        self.assertEqual(303,send(fields));self.assertEqual(1,len(history(self.space())))


if __name__=='__main__':unittest.main()
