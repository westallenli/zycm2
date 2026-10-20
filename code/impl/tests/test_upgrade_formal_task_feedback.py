"""Named current task feedback, physical evidence and closure gates."""
import http.client,json,threading,unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlencode
from . import test_upgrade_material_supply_receipts as supply_fixture
from . import test_upgrade_task_relations as relation_fixture
from panda_brain import formal_task_feedback as feedback
from panda_brain.formal_task_receipts import published
from panda_brain.task_relations import reference,gate
from panda_brain.panel import render_page,make_server
from panda_brain.persistence import save_workspace


class FeedbackTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','space','status','plan','attach','review','verify','publish','act','receive','inventory','loss'):
        locals()[name]=getattr(supply_fixture.SupplyReceiptTests,name)
    def setUp(self):
        supply_fixture.SupplyReceiptTests.setUp(self)
        self.ref=reference(next(iter(published(self.space()).values())))
    def begin(self):
        self.state.handle_formal_task_receipt(self.event_id,member_token=self.tokens[self.SOURCE],order_id=self.ref['order_id'],publication_ref=self.ref['publication_ref'],
            expected_version=0,action='received',basis='匿名具名接收',request_id='feedback-ack')
        for version,action in enumerate(('conditions','begin')):
            self.state.handle_task_relations(self.event_id,member_token=self.tokens[self.SOURCE],action=action,task=self.ref,basis='匿名条件核对及开始',expected_version=version,request_id='feedback-'+action)
    def feedback(self,action='progress',**changes):
        count=sum(r.reference.object_id==self.ref['order_id'] for r,v in feedback.history(self.space(),self.state.ledger))
        return self.state.handle_formal_task_feedback(self.event_id,**(dict(member_token=self.tokens[self.TARGET if action=='verify' else self.SOURCE],order_id=self.ref['order_id'],
            publication_ref=self.ref['publication_ref'],action=action,basis='匿名当前版实际反馈',expected_version=count,request_id='feedback-report-'+str(count),
            overall_basis='匿名整体目标已逐项核实' if action=='verify' else '',damage_basis='')|changes))
    def test_receipt_never_closes_then_progress_and_named_verification(self):
        self.receive(quantity=4);self.assertEqual('confirmed_unack',self.space().command_flows[self.ref['order_id']].stage)
        self.begin();self.feedback();self.feedback('partial');self.feedback('completed')
        self.assertEqual('done_pending_verify',self.space().command_flows[self.ref['order_id']].stage)
        self.feedback('verify');self.assertTrue(feedback.verified(self.space(),self.state.ledger,self.ref['order_id']))
        self.assertEqual(16,self.inventory().book_available)
        self.state=self.new_state();self.assertTrue(feedback.verified(self.space(),self.state.ledger,self.ref['order_id']))
    def test_missing_receipt_damage_and_overall_basis_block_without_writing(self):
        self.begin();self.receive(quantity=3,damaged_quantity=1);self.feedback('completed');before=self.files()
        with self.assertRaisesRegex(ValueError,'欠收'):self.feedback('verify')
        self.assertEqual(before,self.files());self.receive(quantity=1,expected_version=1,request_id='last-receipt');before=self.files()
        for values in ({},{'damage_basis':'匿名损坏核验','overall_basis':''}):
            with self.assertRaises(ValueError):self.feedback('verify',**values)
            self.assertEqual(before,self.files())
        self.feedback('verify',damage_basis='匿名损坏处置已核实不计可用');self.assertEqual(15,self.inventory().book_available)
    def test_current_actor_version_start_and_pending_state_required(self):
        before=self.files()
        with self.assertRaises(ValueError):self.feedback()
        self.assertEqual(before,self.files());self.begin();before=self.files()
        for values in (dict(member_token=self.tokens[self.DEPUTY]),dict(publication_ref='foreign'),dict(expected_version=True),dict(overall_basis='伪造核验')):
            with self.assertRaises((ValueError,PermissionError)):self.feedback(**values)
            self.assertEqual(before,self.files())
        with self.assertRaises(ValueError):self.feedback('verify')
        self.feedback('completed');before=self.files()
        with self.assertRaises(ValueError):self.feedback()
        with self.assertRaises(PermissionError):self.feedback('verify',member_token=self.tokens[self.SOURCE])
        self.assertEqual(before,self.files())
    def test_difficulty_must_be_explicitly_resolved_before_completed_close(self):
        self.begin();self.receive(quantity=4);self.feedback('difficulty');before=self.files()
        with self.assertRaises(ValueError):self.feedback('completed')
        with self.assertRaises(ValueError):self.feedback('verify')
        self.assertEqual(before,self.files())
    def test_resolved_history_and_completion_no_implicit_resolution(self):
        self.begin();before=self.files()
        with self.assertRaises(ValueError):self.feedback('resolved')
        self.assertEqual(before,self.files());self.feedback('difficulty');self.feedback('resolved');self.receive(quantity=4);self.feedback('completed');self.feedback('verify')
        self.assertTrue(feedback.verified(self.space(),self.state.ledger,self.ref['order_id']))
    def test_exact_replay_after_close_zero_save_and_conflict(self):
        self.begin();first=self.feedback();self.receive(quantity=4);self.feedback('completed');self.feedback('verify');before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay wrote')):
            self.assertEqual(first,self.feedback(expected_version=0,request_id='feedback-report-0'))
        with self.assertRaises(ValueError):self.feedback(expected_version=0,request_id='feedback-report-0',basis='不同')
        self.assertEqual(before,self.files())
    def test_changed_review_missing_feedback_mirror_and_generic_forgery_fail_closed(self):
        self.begin();row=self.feedback();self.state.workspace.object_revisions.pop(row.id)
        save_workspace(self.state.workspace,self.root/'snapshots'/f'{self.event_id}.db')
        before=self.files();self.assertEqual(409,render_page(self.state,route='/tasks/feedback',member_token=self.tokens[self.SOURCE])[0])
        with self.assertRaises(ValueError):self.feedback()
        with self.assertRaises(PermissionError):self.state.workspace.record_object_revision(row,expected_version=0)
        self.assertEqual(before,self.files())
    def test_changed_safety_does_not_accept_new_feedback(self):
        self.begin();self.review();before=self.files()
        with self.assertRaises(ValueError):self.feedback()
        self.assertEqual(before,self.files())
    def test_postwrite_failure_and_interrupt_recovery_no_partial_feedback(self):
        import panda_brain.persistence as persistence
        self.begin();before=self.files();save=persistence.save_workspace
        def fail(*a,**kw):save(*a,**kw);raise OSError('anonymous feedback postwrite failure')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError):self.feedback()
        self.assertEqual(before,self.files())
        class Interrupted(BaseException):pass
        def interrupt(*a,**kw):save(*a,**kw);raise Interrupted()
        with patch.object(persistence,'save_workspace',side_effect=interrupt):
            with self.assertRaises(Interrupted):self.feedback()
        with self.assertRaises(Exception):self.feedback()
        self.state=self.new_state();self.tokens[self.SOURCE],_=self.state.member_login(self.SOURCE,'123456')
        self.assertEqual(before,self.files());self.feedback();self.assertEqual(1,len(feedback.history(self.space(),self.state.ledger)))
    def test_readonly_and_strict_actual_http_reports(self):
        self.begin();before=self.files();code,page=render_page(self.state,route='/tasks/feedback',member_token=self.tokens[self.SOURCE]);self.assertEqual(200,code)
        self.assertIn('待收 4',page);self.assertEqual(before,self.files());self.assertEqual(404,render_page(self.state,route='/tasks/feedback')[0])
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(event_id=self.event_id,order_id=self.ref['order_id'],publication_ref=self.ref['publication_ref'],action='progress',basis='匿名HTTP当前执行报告',expected_version='0',request_id='http-feedback',csrf=self.state.member_session(self.tokens[self.SOURCE]).custody_csrf)
        def send(values,token=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);conn.request('POST','/tasks/feedback',urlencode(values,doseq=True),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.tokens[self.SOURCE])});resp=conn.getresponse();resp.read();conn.close();return resp.status
        self.assertEqual(403,send(fields|dict(csrf='bad')));self.assertEqual(409,send(fields|dict(expected_version=['0','0'])));self.assertEqual(409,send(fields|dict(actor='admin')))
        self.assertEqual(303,send(fields));self.assertEqual(1,len(feedback.history(self.space(),self.state.ledger)))


class ParentFeedbackTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','space','files','assess','relate','receive','control'):
        locals()[name]=getattr(relation_fixture.TaskRelationsTests,name)
    def setUp(self):relation_fixture.TaskRelationsTests.setUp(self)
    def feedback(self,ref,actor,action,**changes):
        count=sum(r.reference.object_id==ref['order_id'] for r,v in feedback.history(self.space(),self.state.ledger))
        return self.state.handle_formal_task_feedback(self.event_id,**(dict(member_token=self.tokens[actor],order_id=ref['order_id'],publication_ref=ref['publication_ref'],
            action=action,basis='匿名拆分任务逐项核验',expected_version=count,request_id='parent-'+ref['order_id']+'-'+str(count))|changes))
    def begin(self,ref,actor):
        self.receive(ref,actor);self.control(ref,actor,request_id=ref['order_id']+'-conditions');self.control(ref,actor,action='begin',expected_version=1,request_id=ref['order_id']+'-begin')
    def test_parent_needs_closed_child_and_overall_goal_not_child_sum(self):
        self.begin(self.first,self.SOURCE);self.begin(self.second,self.TARGET);self.feedback(self.first,self.SOURCE,'completed');before=self.files()
        verifier=published(self.space())[self.first['order_id']][2]['verifier_id']
        with self.assertRaisesRegex(ValueError,'子任务'):self.feedback(self.first,verifier,'verify',overall_basis='匿名总目标')
        self.assertEqual(before,self.files());self.feedback(self.second,self.TARGET,'completed')
        with self.assertRaises(ValueError):self.feedback(self.second,self.DEPUTY,'verify',overall_basis='子不能总')
        self.feedback(self.second,self.DEPUTY,'verify');self.feedback(self.first,verifier,'verify',overall_basis='匿名整体目标独立核实')
        self.assertTrue(feedback.verified(self.space(),self.state.ledger,self.first['order_id']))
    def test_verified_dependency_unblocks_and_removed_running_relationship_stays_locked(self):
        self.relate(dependencies=[self.second]);self.begin(self.second,self.TARGET);before=self.files()
        with self.assertRaises(ValueError):self.relate(expected_version=1,request_id='remove-running')
        self.assertEqual(before,self.files());self.feedback(self.second,self.TARGET,'completed');self.feedback(self.second,self.DEPUTY,'verify')
        self.assertEqual((),gate(self.space(),self.first['order_id'],self.state.ledger)[0]);self.begin(self.first,self.SOURCE)
