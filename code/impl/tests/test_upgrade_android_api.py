"""Current native Android contract and original business effects over real HTTP."""
import http.client,json,os,time,unittest
from copy import deepcopy
from urllib.parse import urlencode
from . import test_upgrade_wifi_mobile as fixtures
from panda_brain import android_api as api

class Client:
    def call(self,method,path,value=None,token='',raw=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=25)
        try:
            body=raw if raw is not None else json.dumps(value).encode() if value is not None else None
            c.request(method,path,body,{'Content-Type':'application/json','Authorization':'Bearer '+token} if token else {'Content-Type':'application/json'})
            r=c.getresponse();data=r.read();return r.status,json.loads(data)
        finally:c.close()
    def native_login(self,account='13900000031',**changes):
        status,value=self.call('POST','/app/v1/login',dict(account=account,password='123456',event_id=self.state.active_event_id,qr_id='qr-001')|changes)
        return status,value,value.get('token','')
    def native_view(self,token,tab='arrival',**q):return self.call('GET','/app/v1/view?'+urlencode(dict(tab=tab)|q),token=token)
    def form(self,view,title):return next(b for b in view['blocks'] if b['kind']=='form' and title in b['title'])
    def send(self,token,form,**fields):return self.call('POST','/app/v1/action',dict(action_id=form['action_id'],fields=fields),token)

class NativeTests(Client,fixtures.Http,unittest.TestCase):
    setUp=fixtures.PhoneTests.setUp
    files=fixtures.PhoneTests.files
    def test_native_auth_and_strict_json(self):
        for account,password,expected in [('abc','123456',401),('13900000031','wrong',401),('13900000120','123456',403)]:
            s,v,t=self.native_login(account,password=password);self.assertEqual(expected,s);self.assertFalse(t)
        for raw in (b'[]',b'{"account":"x","account":"y","password":"p"}',b'{'):
            self.assertEqual(409,self.call('POST','/app/v1/login',raw=raw)[0])
        self.assertEqual(403,self.native_view('bad')[0])
    def test_read_only_native_fields_own_team_and_event(self):
        s,_,token=self.native_login();self.assertEqual(200,s);before=self.files()
        for tab in ('callouts','arrival','tasks','feedback'):
            s,v=self.native_view(token,tab);self.assertEqual(200,s);self.assertEqual('fire-001',v['event_id'])
            self.assertNotIn('<form',json.dumps(v));self.assertNotIn('custody_csrf',json.dumps(v))
            for f in [b for b in v['blocks'] if b['kind']=='form']:
                self.assertNotIn('fixed',f);self.assertNotIn('route',f)
        self.assertEqual(before,self.files());self.assertEqual(409,self.native_view(token,event_id='foreign')[0])
    def test_native_callout_datetime_and_no_arrival(self):
        _,_,token=self.native_login();_,v=self.native_view(token,'callouts');form=self.form(v,'回执')
        self.assertTrue(any(f['name']=='eta' and f['type']=='datetime-local' for f in form['fields']))
        self.assertEqual(200,self.send(token,form,can_depart='yes',expected_people='10',eta='2026-10-18T20:30:00+08:00',reason='')[0])
        self.assertNotIn(self.state.resource_demo['waiting_squad'],self.state.ledger.snapshots['fire-001'].squads)
    def test_native_arrival_duplicate_supplement_and_readiness(self):
        _,_,token=self.native_login();_,v=self.native_view(token);form=self.form(v,'载入')
        self.assertEqual(200,self.send(token,form,qr_id='qr-001')[0]);sid=self.state.resource_demo['waiting_squad']
        self.assertNotIn(sid,self.state.ledger.snapshots['fire-001'].squads)
        _,v=self.native_view(token);form=self.form(v,'一次计入')
        self.assertEqual(200,self.send(token,form,phone=['13900000040'])[0]);lines=deepcopy(self.state.ledger.snapshots['fire-001'].squads[sid].lines)
        self.assertEqual(200,self.send(token,form,phone=['13900000040'])[0]);self.assertEqual(9,len(self.state.ledger.snapshots['fire-001'].squads[sid].arrived_phones))
        _,v=self.native_view(token);self.assertEqual(200,self.send(token,self.form(v,'确认补到'),phone=['13900000040'])[0]);self.assertEqual(10,len(self.state.ledger.snapshots['fire-001'].squads[sid].arrived_phones));self.assertEqual(lines,self.state.ledger.snapshots['fire-001'].squads[sid].lines)
        _,v=self.native_view(token);f=self.form(v,'记录所选人员状态');self.assertEqual(200,self.send(token,f,phone=['13900000031'],status='standby',basis='模拟App本人明确待命',order_id='')[0]);self.assertFalse(self.state.workspace.verify_audit_chain())
    def test_fixed_field_cross_session_foreign_phone_and_expiry_rejected(self):
        _,_,token=self.native_login();_,v=self.native_view(token);f=self.form(v,'载入');before=self.files()
        self.assertEqual(409,self.send(token,f,qr_id='qr-001',event_id='foreign')[0])
        self.assertEqual(409,self.send(token,f,qr_id='qr-001',squad_id='foreign')[0])
        _,_,other=self.native_login();self.assertEqual(409,self.send(other,f,qr_id='qr-001')[0])
        self.state._android_sessions[token]['tickets'][f['action_id']]['at']=time.monotonic()-1801
        self.assertEqual(409,self.send(token,f,qr_id='qr-001')[0]);self.assertEqual(before,self.files())
        _,v=self.native_view(token);self.send(token,self.form(v,'载入'),qr_id='qr-001');_,v=self.native_view(token);f=self.form(v,'一次计入');before=self.files()
        self.assertEqual(409,self.send(token,f,phone=['13900000001'])[0]);self.assertEqual(before,self.files())
    def test_native_logout_and_unknown_route(self):
        _,_,token=self.native_login();self.assertEqual(404,self.call('GET','/app/v1/unknown',token=token)[0]);self.assertEqual(200,self.call('POST','/app/v1/logout',{},token)[0]);self.assertEqual(403,self.native_view(token)[0])
    def test_apk_download_is_fixed_and_installer_page_uses_native_entry(self):
        path=api.apk_path(self.state);path.parent.mkdir(exist_ok=True);path.write_bytes(b'APK-test-fixture')
        self.assertIn('/app/download',api.install_page(self.state));self.assertIn('App 内电脑服务地址',api.install_page(self.state))
        for url,expected in [('/app/download',200),('/app/download?path=registry.json',404),('/app/download/../data/registry.json',404)]:
            c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
            try:
                c.request('GET',url);r=c.getresponse();body=r.read();self.assertEqual(expected,r.status)
                if expected==200:self.assertEqual(b'APK-test-fixture',body);self.assertEqual('application/vnd.android.package-archive',r.getheader('Content-Type'))
            finally:c.close()

class NativeTaskTests(Client,fixtures.Http,unittest.TestCase):
    setUp=fixtures.PhoneTaskTests.setUp
    # Use the latest released-task fixture, not an historical demo root.
    def test_native_formal_task_receive_conditions_begin_feedback(self):
        s,_,token=self.native_login(self.f.SOURCE,event_id=self.f.event_id,qr_id='');self.assertEqual(200,s)
        _,v=self.native_view(token,'tasks');self.assertEqual(200,self.send(token,self.form(v,'接收报告'),action='received',basis='模拟App本人接收')[0])
        for title in ('确认当前执行条件','明确报告开始执行'):
            _,v=self.native_view(token,'tasks');self.assertEqual(200,self.send(token,self.form(v,title),basis='模拟App核对条件与执行')[0])
        for title in ('报告执行进展','报告全部完成待核验'):
            _,v=self.native_view(token,'feedback');f=self.form(v,title);self.assertEqual(200,self.send(token,f,basis='模拟App本人回报')[0])
        self.assertTrue(any(f.stage=='done_pending_verify' for f in self.state.workspace.command_flows.values()));self.assertFalse(self.state.workspace.verify_audit_chain())
