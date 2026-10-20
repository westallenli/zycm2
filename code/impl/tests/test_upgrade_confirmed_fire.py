"""Approved 2026-10-12: upstream-confirmed creation, explicit named duty start."""
from copy import deepcopy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import EventRegistry
from panda_brain.alarm_inbox import AlarmInbox
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


def message(number='confirmed-001'):
    return dict(messageId='msg-'+number, messageType='FIRE_EVENT', sourceSystem='MULTIMODAL_DEMO',
                sourceRecordId=number, sourceVersion=1, operation='CREATE',
                eventTime='2026-10-12T10:00:00+08:00',
                data=dict(name='已确认模拟火情', location='匿名演示林区', status='confirmed',
                          initialReport='上游已核实（模拟）', lat=30.1, lon=110.2,
                          adminRegion='演示行政区', keyTargets=['演示目标']))


class ConfirmedFireTests(unittest.TestCase):
    DUTY = '13955551001'
    MEMBER = '13955551002'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = patch.object(wb, 'SNAPSHOT_DIR', self.root/'snapshots'); p.start(); self.addCleanup(p.stop)
        self.auth = GeneralEntryAuth(secret='confirmed-fire-test')
        self.state = PanelState(ScenarioWorkspace('confirmed-fire-test', general_auth=self.auth), auth=self.auth,
            registry_path=self.root/'registry.json', materials_path=self.root/'materials.json',
            reviews_path=self.root/'reviews.json', alarm_inbox_path=self.root/'alarms.json',
            duty_subjects=(self.DUTY,))
        self.state.registry.import_person_library([PersonRecord(name='模拟值班', phone=self.DUTY, unit='模拟'),
                                                   PersonRecord(name='模拟成员', phone=self.MEMBER, unit='模拟')])

    def start_http(self):
        server = make_server(self.state, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.server = server

    def request(self, method, path, form=None, cookie=''):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            c.request(method, path, None if form is None else urlencode(form),
                      {'Content-Type':'application/x-www-form-urlencoded', 'Cookie':cookie})
            r = c.getresponse(); return r.status, r.getheaders(), r.read().decode()
        finally:
            c.close()

    def login(self, account, password):
        status, headers, _ = self.request('POST', '/login/unified', dict(account=account,password=password))
        self.assertEqual(303,status)
        return next(v.split(';')[0] for k,v in headers if k.lower()=='set-cookie' and 'Max-Age=0' not in v)

    def bytes(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_receive_without_admin_login_creates_only_registered_event(self):
        result = self.state.handle_confirmed_fire_event(message())
        self.assertEqual('CREATED',result['code'])
        event = self.state.registry.events[result['event_id']]
        self.assertEqual('registered',event.status)
        self.assertEqual('confirmed-001',event.report_no)
        self.assertEqual((30.1,110.2),(event.lat,event.lon))
        self.assertEqual('演示行政区',event.admin_region)
        self.assertFalse(self.state.entry_unlocked)
        self.assertFalse(event.snapshot_path)
        self.assertFalse(self.state.active_event_id)
        self.assertEqual({},self.state.registry.qrs)
        self.assertEqual('created',self.state.alarm_inbox.alarms[result['alarm_id']].status)
        self.assertEqual(1,len(EventRegistry.load(self.state.registry_path).events))
        self.assertEqual(1,len(AlarmInbox.load(self.state.alarm_inbox_path).alarms))

    def test_duplicate_push_keeps_event_and_every_business_byte(self):
        original = self.state.handle_confirmed_fire_event(message()); before = self.bytes()
        again = self.state.handle_confirmed_fire_event(message())
        self.assertEqual('DUPLICATE',again['code']); self.assertEqual(original['event_id'],again['event_id'])
        self.assertEqual(before,self.bytes()); self.assertEqual(1,len(self.state.registry.events))

    def test_invalid_time_coordinates_and_types_leave_no_partial_records(self):
        for field,value in [('lat',91),('lon',float('nan')),('lat',None),('name',123),('discoveredAt','invalid')]:
            m = message(); m['data'][field]=value
            with self.subTest(field=field,value=value):
                result=self.state.handle_confirmed_fire_event(m)
                self.assertEqual('INVALID_ARGUMENT',result['code']); self.assertEqual({},self.state.registry.events)
                self.assertEqual({},self.state.alarm_inbox.alarms); self.assertEqual({},self.bytes())
        self.assertEqual('INVALID_ARGUMENT',self.state.handle_confirmed_fire_event(None)['code'])

    def test_legacy_pending_and_rejected_records_are_not_rewritten(self):
        for rejected in (False,True):
            m=message('legacy-'+str(rejected)); old=self.state.alarm_inbox.ingest_message(m)
            if rejected: self.state.alarm_inbox.reject(old['alarm_id'],reason='历史记录',operator='historical')
            before=deepcopy(self.state.alarm_inbox.__dict__)
            self.assertEqual('HISTORICAL_RECORD',self.state.handle_confirmed_fire_event(m)['code'])
            self.assertEqual(before,self.state.alarm_inbox.__dict__)
        self.assertEqual({},self.state.registry.events)

    def test_save_failure_rolls_back_both_files_and_memory_then_retry(self):
        self.state._save_registry(); self.state._save_alarm_inbox(); before=self.bytes()
        with patch.object(self.state,'_save_alarm_inbox',side_effect=OSError('simulated save failure')):
            with self.assertRaises(OSError): self.state.handle_confirmed_fire_event(message())
        self.assertEqual(before,self.bytes()); self.assertEqual({},self.state.registry.events)
        self.assertEqual({},self.state.alarm_inbox.alarms)
        self.assertEqual('CREATED',self.state.handle_confirmed_fire_event(message())['code'])

    def test_update_does_not_change_event_lifecycle_or_original_source(self):
        self.state.handle_confirmed_fire_event(message()); before=self.bytes()
        m=message(); m.update(operation='UPDATE',sourceVersion=2)
        self.assertEqual('INVALID_ARGUMENT',self.state.handle_confirmed_fire_event(m)['code'])
        self.assertEqual(before,self.bytes())

    def test_broken_source_index_cannot_create_duplicate_or_fill_history(self):
        self.state.registry.external_index['MULTIMODAL_DEMO|confirmed-001']='fire-missing'
        self.assertEqual('SOURCE_CONFLICT',self.state.handle_confirmed_fire_event(message())['code'])
        self.assertEqual({},self.state.registry.events)
        self.assertEqual({},self.state.alarm_inbox.alarms)

    def test_admin_ui_has_no_manual_create_or_repeat_review_and_legacy_posts_retired(self):
        self.start_http(); cookie=self.login('admin','confirmed-fire-test')
        status,_,body=self.request('GET','/events',cookie=cookie); self.assertEqual(200,status)
        for obsolete in ['登记新火情','外部报警待确认','确认建档','拒绝原因','重复关联','action="/events/create"']:
            self.assertNotIn(obsolete,body)
        self.assertIn('真实推送接口：待对接',body)
        for path in ['/events/create','/events/alarm/action']:
            self.assertEqual(410,self.request('POST',path,{},cookie)[0])
        self.assertEqual(404,self.request('GET','/events/alarm/alm-001/confirm',cookie=cookie)[0])
        self.assertEqual({},self.state.registry.events)

    def test_simulated_push_creates_event_without_start_and_detail_is_read_only(self):
        self.start_http(); admin=self.login('admin','confirmed-fire-test')
        status,headers,_=self.request('POST','/events/alarm/demo',{},admin); self.assertEqual(303,status)
        self.assertTrue(any(k=='Location' and '/events?alarm_event=fire-' in v for k,v in headers))
        event=next(iter(self.state.registry.events.values()))
        self.assertEqual('registered',event.status); self.assertEqual({},self.state.registry.qrs)
        before=self.bytes()
        status,_,body=self.request('GET',f'/events/{event.event_id}/details',cookie=admin)
        self.assertEqual(200,status); self.assertIn('DEMO_CONSOLE',body); self.assertIn('待启动处置',body)
        self.assertEqual(before,self.bytes())

    def test_only_named_duty_can_start_and_source_enters_valid_audit_chain(self):
        created=self.state.handle_confirmed_fire_event(message()); eid=created['event_id']
        self.start_http(); admin=self.login('admin','confirmed-fire-test')
        for cookie in [admin,self.login(self.MEMBER,'123456'),'']:
            self.assertEqual(403,self.request('POST','/events/action',dict(action='start',event_id=eid),cookie)[0])
        duty=self.login(self.DUTY,'123456')
        status,_,body=self.request('GET','/events',cookie=duty)
        self.assertEqual(200,status); self.assertIn('启动处置',body)
        self.assertEqual(200,self.request('GET',f'/events/{eid}/details',cookie=duty)[0])
        status,_,_=self.request('POST','/events/action',dict(action='start',event_id=eid,csrf=self.state.member_session(duty.split('=',1)[1]).custody_csrf),duty)
        self.assertEqual(303,status); self.assertEqual('active',self.state.registry.events[eid].status)
        self.assertEqual(0,len(self.state.registry.qrs))  # current duty flow signs after selection
        saved=load_workspace(self.state.registry.events[eid].snapshot_path,general_auth=self.auth)
        self.assertEqual((),saved.verify_audit_chain())
        sources=[e.payload for e in saved.events if e.kind=='ExternalFireEventReceived']
        self.assertEqual(1,len(sources)); self.assertEqual('upstream:MULTIMODAL_DEMO',sources[0]['operator'])
        self.assertEqual(self.state.alarm_inbox.alarms[created['alarm_id']].raw_digest,sources[0]['raw_digest'])
        self.assertEqual(409,self.request('POST','/events/action',dict(action='start',event_id=eid,csrf=self.state.member_session(duty.split('=',1)[1]).custody_csrf),duty)[0])

    def test_unprivileged_member_cannot_simulate_push_or_read_detail(self):
        eid=self.state.handle_confirmed_fire_event(message())['event_id']
        self.start_http(); member=self.login(self.MEMBER,'123456')
        self.assertEqual(403,self.request('POST','/events/alarm/demo',{},member)[0])
        self.assertEqual(404,self.request('GET',f'/events/{eid}/details',cookie=member)[0])
        self.assertEqual(1,len(self.state.registry.events))

    def test_detail_escapes_upstream_html(self):
        m=message();m['data']['name']='<script>alert(1)</script>'
        eid=self.state.handle_confirmed_fire_event(m)['event_id']
        self.start_http();admin=self.login('admin','confirmed-fire-test')
        body=self.request('GET',f'/events/{eid}/details',cookie=admin)[2]
        self.assertIn('&lt;script&gt;',body);self.assertNotIn('<script>alert(1)</script>',body)


if __name__=='__main__': unittest.main()
