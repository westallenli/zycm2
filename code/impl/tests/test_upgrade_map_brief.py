"""Map decisions preserve evidence; reports cannot silently become verified completion."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from panda_brain import map_brief as mb, sand_command as sc
from panda_brain.durable_storage import action


class MapBriefTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_sand_command import CommandTests
        self.f=CommandTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.s=self.f.s;self.token=self.f.token;self.eid=self.f.eid
        self.s.registry.events[self.eid]=replace(self.s.registry.events[self.eid],lat=40.0,lon=116.0)
        self.s._save_registry()

    def summary(self):return sc.summary(self.s,self.token)['map_brief']

    def values(self,**changes):
        s=self.summary()['suggestions'][0]
        return dict(op='map-presentation',event_id=self.eid,csrf=self.s.member_session(self.token).custody_csrf,
            id=s['id'],basis=s['basis'],action='up',title=s['title'],points=json.dumps(s['points']),
            reason='匿名显示依据',expected_version=str(s['version']),request_id='map-first')|changes

    def write(self,v):
        with action(self.s):return mb.write(self.s,self.token,v)

    def test_readonly_gap_positions_and_original_geometry(self):
        before=self.f.f.files();data=self.summary()
        self.assertTrue(data['suggestions']);self.assertEqual('preview',data['suggestions'][0]['mode'])
        self.assertTrue(data['items']);self.assertTrue(any(not i['points'] for i in data['items']))
        self.assertEqual(before,self.f.f.files());self.assertFalse(self.s.workspace.orders)

    def test_edit_down_up_preserves_original_source_and_history(self):
        initial=self.summary()['suggestions'][0];v=self.values(action='edit',title='人工调整',points='[[40.1,116.1]]')
        self.write(v);self.write(v)
        for index,mode in enumerate(('down','up'),1):self.write(self.values(action=mode,request_id='map-'+str(index)))
        current=self.summary()['suggestions'][0]
        self.assertEqual('人工调整',current['title']);self.assertEqual([[40.1,116.1]],current['points'])
        self.assertEqual(initial['original'],current['original']);self.assertEqual(3,len(current['history']))
        self.assertFalse(self.s.workspace.orders);self.assertFalse(self.s.workspace.verify_audit_chain())

    def test_source_change_does_not_overwrite_edit_and_rejects_stale_submit(self):
        self.write(self.values(action='edit',title='保留人工修改',points='[[40.1,116.1]]'))
        old=self.values(request_id='stale')
        self.s.registry.events[self.eid]=replace(self.s.registry.events[self.eid],lat=41.0)
        updated=self.summary()['suggestions'][0]
        self.assertTrue(updated['changed']);self.assertEqual([[40.1,116.1]],updated['points'])
        with self.assertRaises(ValueError):self.write(old)

    def test_http_permissions_scope_csrf_and_validation(self):
        values=self.values();before=self.f.f.files()
        for bad in ({'csrf':'wrong'},{'event_id':'other'},{'basis':'old'},{'points':'[[999,0]]'}, {'extra':'bad'}):
            code,_=self.f.http(values|bad);self.assertIn(code,(403,409))
        token=self.f.f.tokens[self.f.f.DUTY]
        code,_=self.f.http(values|{'csrf':self.s.member_session(token).custody_csrf},token);self.assertEqual(403,code)
        self.assertEqual(before,self.f.f.files())
        code,body=self.f.http(values);self.assertEqual(200,code,body)

    def test_failed_save_rolls_back_record_and_file(self):
        before=self.f.f.files();v=self.values()
        with patch.object(self.s,'_persist_active_task_snapshot',side_effect=OSError('injected')):
            with self.assertRaises(OSError):self.write(v)
        self.assertEqual({},mb.history(self.s.workspace));self.assertEqual(before,self.f.f.files())

    def test_located_report_and_unlocated_report_not_invented(self):
        v=self.f.values();self.f.write(v)
        self.f.write(v|dict(action='report',expected_version='1',request_id='report-one',ack='',report='未取得定位',observed_at='2026-10-14T10:00:00Z'),self.f.f.tokens[self.f.f.DUTY])
        item=next(i for i in self.summary()['items'] if i['id'].startswith('recon:'))
        self.assertEqual([],item['points']);self.assertEqual('reply',item['category'])


class MapTaskStates(unittest.TestCase):
    def test_real_feedback_difficulty_completion_and_verified_are_distinct(self):
        from tests.test_upgrade_formal_task_feedback import FeedbackTests
        f=FeedbackTests();f.setUp();self.addCleanup(f.doCleanups)
        def item():
            # Use the current event workspace loaded by the existing business methods.
            f.state.workspace=f.space();f.state.active_event_id=f.event_id
            brief=sc.summary(f.state,f.tokens[f.DEPUTY])['map_brief']
            return next(i for i in brief['items'] if i['id']=='task:'+f.ref['order_id'])
        f.begin();f.feedback('difficulty');self.assertEqual('urgent',item()['category'])
        f.feedback('resolved');self.assertEqual('reply',item()['category'])
        f.receive(quantity=4);f.feedback('completed');self.assertEqual('verify',item()['category'])
        f.feedback('verify');final=item();self.assertEqual('done',final['category']);self.assertEqual(4,len(final['history']))
        self.assertEqual([],final['points'])
