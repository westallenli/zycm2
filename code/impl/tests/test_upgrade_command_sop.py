"""Work queue reflects source evidence, request identity and human authority."""
import unittest
from panda_brain import sand_command as sc


class CommandSopTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_initial_map import InitialMapTests
        self.f=InitialMapTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.s=self.f.s;self.token=self.f.token

    def summary(self):return sc.summary(self.s,self.token)

    def test_queue_specific_missing_fields_and_explicit_priority_basis(self):
        before=self.f.f.f.f.files();data=self.summary();rows=data['command_sop']['rows']
        self.assertTrue(rows)
        self.assertEqual(sorted(r['priority'] for r in rows),[r['priority'] for r in rows])
        for row in rows:
            self.assertTrue(row['priority_reason']);self.assertNotEqual('缺资料',row['title'])
            self.assertTrue(row['owner']);self.assertIn('deadline',row)
        self.assertTrue(any(r['stage'] in {'核清火情','跟踪执行','核验结果','动态调整'} for r in rows))
        self.assertEqual(before,self.f.f.f.f.files());self.assertEqual([],data['demo_plans'])

    def test_sent_request_and_reply_replace_the_same_todo_without_closing_gap(self):
        g=next(r for r in self.summary()['command_sop']['rows'] if r['action']=='recon-new')
        command=self.f.f.f;v=command.values(need=g['need']);command.write(v)
        rows=self.summary()['command_sop']['rows'];todo=next(r for r in rows if r['id']=='recon:'+v['id'])
        self.assertEqual('recon-report',todo['action']);self.assertEqual(v['recipient'],todo['owner'])
        self.assertFalse(any(r['id']==g['id'] for r in rows))
        command.write(v|dict(action='report',expected_version='1',request_id='sop-reply',ack='',report='只补充文字，位置仍未知',observed_at='2026-10-16T10:00:00Z'),command.f.tokens[command.f.DUTY])
        after=self.summary()['command_sop']['rows'];todo2=next(r for r in after if r['id']==todo['id'])
        self.assertEqual('recon-review',todo2['action']);self.assertIn('待核对',todo2['state'])
        self.assertFalse(self.s.workspace.orders)

    def test_three_demo_plans_are_scoped_distinct_complete_and_readonly(self):
        self.f.authorize();before=self.f.f.f.f.files();decisions=dict(self.s.workspace.decisions);data=self.summary();plans=data['demo_plans']
        self.assertEqual(3,len(plans));self.assertTrue(all(p['demo'] and p['basis'] and p['preconditions'] and p['phases'] for p in plans))
        self.assertEqual(3,len({str(p['shapes']) for p in plans}))
        for p in plans:
            self.assertEqual(3,len(p['shapes']));self.assertEqual(p['shapes'][0]['points'][0],p['shapes'][0]['points'][-1])
        self.assertEqual(before,self.f.f.f.f.files());self.assertEqual(decisions,self.s.workspace.decisions)
        self.f.authorize('reported');self.assertEqual([],self.summary()['demo_plans'])

    def test_read_role_only_receives_authorized_recon(self):
        command=self.f.f.f;command.write(command.values())
        duty=command.f.tokens[command.f.DUTY];data=sc.summary(self.s,duty)
        self.assertFalse(data['deputy']);self.assertTrue(all(r['owner']==command.f.DUTY for r in data['command_sop']['rows'] if r['id'].startswith('recon:')))

    def test_completion_todo_uses_the_verifier_not_the_reporter(self):
        from tests.test_upgrade_formal_task_feedback import FeedbackTests
        from panda_brain.formal_task_receipts import published
        f=FeedbackTests();f.setUp();self.addCleanup(f.doCleanups)
        f.begin();f.receive(quantity=4);f.feedback('completed')
        f.state.workspace=f.space();f.state.active_event_id=f.event_id
        row=next(r for r in sc.summary(f.state,f.tokens[f.DEPUTY])['command_sop']['rows'] if r['id']=='task:'+f.ref['order_id'])
        task=published(f.state.workspace)[f.ref['order_id']][2]
        self.assertEqual(task['verifier_id'],row['owner']);self.assertEqual('核验结果',row['stage'])
