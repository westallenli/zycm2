"""Approved full-squad field App contract, shared state, audit and boundaries."""
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode
from copy import deepcopy
import http.client,json,math,os,unittest
from . import test_upgrade_wifi_mobile as fixtures
from panda_brain import captain_field as field

class Client:
    def call(self,method,path,value=None,token='',binary=False):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        try:
            c.request(method,'/app/v2/'+path,json.dumps(value).encode() if value is not None else None,{'Content-Type':'application/json','Authorization':'Bearer '+token})
            r=c.getresponse();raw=r.read();return (r.status,dict(r.getheaders()),raw) if binary else (r.status,json.loads(raw))
        finally:c.close()
    def native_login(self,account='13900000031'):
        s,v=self.call('POST','login',dict(account=account,password='123456'));self.assertEqual(200,s,v);return v['token']
    def state_view(self,token):
        s,v=self.call('GET','state',token=token);self.assertEqual(200,s,v);return v

class FieldTests(Client,fixtures.Http,unittest.TestCase):
    setUp=fixtures.PhoneTests.setUp
    files=fixtures.PhoneTests.files
    def test_current_scene_and_three_role_views_are_read_only(self):
        token=self.native_login();before=self.files();v=self.state_view(token)
        self.assertEqual('fire-001',v['event_id']);self.assertEqual(1,len(v['teams']));self.assertFalse(v['teams'][0]['arrived'])
        self.assertGreaterEqual(len(v['scene']['marks']),22);self.assertNotIn('missing_phones',json.dumps(v));self.assertEqual(before,self.files())
        for c in v['commands']:self.assertNotIn('recipient_id',c)
    def test_scan_whole_team_full_and_duplicate_never_double_carry(self):
        token=self.native_login();sid=self.state.resource_demo['waiting_squad'];request=dict(team_id=sid,qr_id='qr-001',request_id='field-full-arrival-01')
        self.assertEqual(200,self.call('POST','arrival',request,token)[0]);arrival=self.state.ledger.snapshots['fire-001'].squads[sid]
        self.assertEqual(set(self.state.ledger.squads[sid].member_phones),set(arrival.arrived_phones));lines=deepcopy(arrival.lines);version=self.state.workspace.version
        for rid in ('field-full-arrival-01','field-full-arrival-02'):
            self.assertEqual(200,self.call('POST','arrival',request|dict(request_id=rid),token)[0])
        self.assertEqual(version,self.state.workspace.version);self.assertEqual(lines,self.state.ledger.snapshots['fire-001'].squads[sid].lines);self.assertTrue(self.state_view(token)['teams'][0]['full'])
    def test_scan_rejects_foreign_revoked_and_extra_personnel_fields(self):
        token=self.native_login();sid=self.state.resource_demo['waiting_squad'];v=dict(team_id=sid,qr_id='qr-001',request_id='field-bad-arrival-01');before=self.files()
        for change in (dict(qr_id='qr-999'),dict(team_id=self.state.resource_demo['called_squads'][0]),dict(phones=[]),dict(count='10'),dict(mode='supplement')):
            self.assertIn(self.call('POST','arrival',v|change,token)[0],(403,409));self.assertEqual(before,self.files())
        self.state.registry.qrs['qr-001']=replace(self.state.registry.qrs['qr-001'],revoked_at='2026-10-18T00:00:00+00:00');self.assertEqual(409,self.call('POST','arrival',v,token)[0])
    def test_shortage_names_only_audited_idempotent_and_no_inventory_change(self):
        token=self.native_login();sid=self.state.resource_demo['waiting_squad'];before=deepcopy(self.state.ledger.to_dict());v=dict(team_id=sid,material='饮用水',request_id='field-shortage-01')
        for _ in range(2):self.assertEqual(200,self.call('POST','shortage',v,token)[0])
        rows=field.reports(self.state.workspace,field.SHORTAGE);self.assertEqual(1,len(rows));self.assertEqual('饮用水',rows[0]['material']);self.assertEqual(before,self.state.ledger.to_dict())
        self.assertEqual(409,self.call('POST','shortage',v|dict(material='水带'),token)[0])
        from panda_brain.resource_demo import open_state
        loaded=open_state(self.state.registry_path.parent);self.assertEqual(1,len(field.reports(loaded.workspace,field.SHORTAGE)));self.assertFalse(loaded.workspace.verify_audit_chain())
        deputy,_=self.state.member_login('13900000120','123456');self.assertIn('饮用水',field.report_section(self.state,deputy,field.SHORTAGE))
    def test_shortage_rejects_extra_fields_and_foreign_team(self):
        token=self.native_login();sid=self.state.resource_demo['waiting_squad'];before=self.files();v=dict(team_id=sid,material='饮用水',request_id='field-shortage-02')
        for extra in ('quantity','destination','needed_at','reason','description'):
            self.assertEqual(409,self.call('POST','shortage',v|{extra:'x'},token)[0])
        self.assertEqual(403,self.call('POST','shortage',v|dict(team_id='foreign'),token)[0]);self.assertEqual(before,self.files())
    def test_assistant_existing_event_and_unknown_weather_no_business_writes(self):
        token=self.native_login();before=self.files()
        for q,word in [('当前火情是什么','北山林火'),('天气风速是多少','暂无'),('物资情况','账面'),('本队命令','本队'),('风险点在哪里','登记'),('未知专业问题','无法回答')]:
            s,v=self.call('POST','ask',dict(question=q),token);self.assertEqual(200,s,v)
            self.assertIn(word,v['answer']+v.get('notice','')+str(v.get('sources','')))
        self.assertEqual(before,self.files())
    def test_tile_is_authorized_current_source_and_no_arbitrary_path(self):
        from panda_brain.local_satellite import load_installed
        self.state.local_satellite=load_installed(Path(os.environ['PZ_WIFI_TEST_ROOT']));token=self.native_login();v=self.state_view(token);scene=v['scene'];self.assertTrue(scene['map_available'])
        lat,lon=scene['center'];z=scene['zoom'];x=int((lon+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z);url='tile?'+urlencode(dict(z=z,x=x,y=y))
        s,h,raw=self.call('GET',url,token=token,binary=True);self.assertEqual(200,s);self.assertTrue(h['Content-Type'].startswith('image/'));self.assertGreater(len(raw),100)
        self.assertEqual(403,self.call('GET',url,binary=True)[0]);self.assertEqual(409,self.call('GET','tile?z=99&x=1&y=1',token=token)[0]);self.assertEqual(409,self.call('GET',url+'&path=x',token=token)[0])
    def test_expired_login_and_foreign_event_never_mutate(self):
        token=self.native_login();before=self.files();self.assertIn(self.call('GET','state?event_id=foreign',token=token)[0],(403,409));self.assertEqual(before,self.files());self.assertEqual(200,self.call('POST','logout',{},token)[0]);self.assertEqual(403,self.call('GET','state',token=token)[0])

class TeamCommandTests(Client,fixtures.Http,unittest.TestCase):
    setUp=fixtures.PhoneTaskTests.setUp
    def test_team_commands_completion_report_and_desktop_readback(self):
        token=self.native_login(self.f.SOURCE);v=self.state_view(token);self.assertTrue(v['commands']);c=v['commands'][0];self.assertEqual(self.f.source_squad,c['team_id']);self.assertNotIn('recipient_id',c)
        original=deepcopy(self.state.workspace.command_flows);request=dict(command_id=c['id'],version_ref=c['version_ref'],reply='本队已按命令完成，提交上级核验',request_id='field-command-reply-01')
        for _ in range(2):self.assertEqual(200,self.call('POST','reply',request,token)[0])
        rows=field.reports(self.state.workspace,field.REPLY);self.assertEqual(1,len(rows));self.assertEqual(original,self.state.workspace.command_flows);self.assertFalse(self.state.workspace.verify_audit_chain())
        deputy,_=self.state.member_login(self.f.DEPUTY,'123456');self.assertIn('本队已按命令完成',field.report_section(self.state,deputy,field.REPLY))
    def test_other_team_and_wrong_command_version_are_rejected(self):
        token=self.native_login(self.f.SOURCE);v=self.state_view(token);c=v['commands'][0];request=dict(command_id=c['id'],version_ref='wrong',reply='本队已完成',request_id='field-command-reply-02');self.assertEqual(403,self.call('POST','reply',request,token)[0])
        other=self.native_login(self.f.TARGET);self.assertFalse(self.state_view(other)['commands']);self.assertEqual(403,self.call('POST','reply',request|dict(version_ref=c['version_ref']),other)[0]);self.assertFalse(field.reports(self.state.workspace,field.REPLY))
