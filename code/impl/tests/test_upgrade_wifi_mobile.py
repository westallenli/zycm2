"""Current phone captain flows and explicitly limited same-Wi-Fi listening."""
from copy import deepcopy
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import http.client,json,os,re,shutil,tempfile,threading,unittest
from panda_brain import captain_mobile as mobile,event_qr,runtime_access,wifi_demo
from panda_brain.panel import make_server
from panda_brain.resource_demo import open_state

class Forms(HTMLParser):
    def __init__(self,text):super().__init__();self.rows=[];self.current=None;self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':self.current=dict(action=a.get('action'),fields={});self.rows.append(self.current)
        elif tag=='input' and self.current is not None and a.get('name') and a.get('type') not in {'checkbox','radio'}:
            self.current['fields'][a['name']]=a.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None
    def form(self,path,**match):
        return next(r['fields'] for r in self.rows if r['action']==path and all(r['fields'].get(k)==v for k,v in match.items())).copy()

class Http:
    def serve(self):
        self.server=make_server(self.state,port=0);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,method,path,form=None,cookie=''):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=25)
        try:
            connection.request(method,path,urlencode(form,doseq=True) if form is not None else None,{'Content-Type':'application/x-www-form-urlencoded','Cookie':cookie})
            response=connection.getresponse();headers=dict(response.getheaders())
            cookies=[v for k,v in response.getheaders() if k.lower()=='set-cookie']
            if cookies:headers['Set-Cookie']='\n'.join(cookies)
            return response.status,headers,response.read().decode('utf-8')
        finally:connection.close()
    def login(self,account='13900000031',**changes):
        status,headers,text=self.request('POST','/mobile/login',dict(account=account,password='123456',event_id=self.state.active_event_id,qr_id='qr-001')|changes)
        match=re.search(r'(?:^|\n)(member_session=[^;]+)',headers.get('Set-Cookie',''))
        return status,headers,text,(match[1] if match else '')

class PhoneTests(Http,unittest.TestCase):
    def setUp(self):
        base=os.environ.get('PZ_WIFI_TEST_ROOT')
        if not base:raise RuntimeError('Set PZ_WIFI_TEST_ROOT to the isolated latest delivery')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        data=Path(self.tmp.name)/'data';shutil.copytree(Path(base)/'data',data)
        path=data/'registry.json';v=json.loads(path.read_text(encoding='utf-8'))
        for e in v['events'].values():e['snapshot_path']=str(data/'snapshots'/Path(e['snapshot_path']).name)
        path.write_text(json.dumps(v,ensure_ascii=False),encoding='utf-8');self.state=open_state(data)
        self.state.wifi_origin='http://192.168.1.37:18705';self.serve()
    def files(self):return {str(p):p.read_bytes() for p in self.state.registry_path.parent.rglob('*') if p.is_file()}
    def test_scan_entry_preserves_event_and_never_checks_in_on_get(self):
        before=self.files();version=self.state.workspace.version
        status,_,text=self.request('GET','/mobile?event_id=fire-001&qr_id=qr-001')
        self.assertEqual(200,status);self.assertIn('队长登录',text)
        self.assertIn('name="qr_id" value="qr-001"',text);self.assertEqual(before,self.files());self.assertEqual(version,self.state.workspace.version)
    def test_login_bad_credentials_and_non_captain_are_rejected(self):
        for account,changes,expected in [('abc',{},401),('13900000031',dict(password='wrong'),401),('13900000120',{},403)]:
            with self.subTest(account=account):
                status,headers,_,_=self.login(account,**changes)
                self.assertEqual(expected,status);self.assertNotIn('Set-Cookie',headers)
    def test_invalid_foreign_revoked_and_duplicate_qr_context_fails_without_arrival(self):
        before=self.files()
        for url in ('/mobile?event_id=other&qr_id=qr-001','/mobile?event_id=fire-001&qr_id=qr-999','/mobile?qr_id=qr-001','/mobile?event_id=fire-001&event_id=other','/mobile?'+urlencode([(str(i),'x') for i in range(8)])):
            self.assertEqual(409,self.request('GET',url)[0],url)
        qr=self.state.registry.qrs['qr-001'];self.state.registry.qrs['qr-001']=replace(qr,revoked_at='2026-10-18T00:00:00+00:00')
        self.assertEqual(409,self.request('GET','/mobile?event_id=fire-001&qr_id=qr-001')[0]);self.assertEqual(before,self.files())
    def test_mobile_read_tabs_only_own_roster_and_keep_desktop_gates(self):
        status,headers,_,cookie=self.login();self.assertEqual(303,status);self.assertEqual('/mobile?tab=arrival',headers['Location'])
        before=self.files()
        for tab in mobile.TABS:
            status,_,text=self.request('GET','/mobile?tab='+tab,cookie=cookie)
            self.assertEqual(200,status,text[:500]);self.assertIn('mc-tabs',text);self.assertNotIn('日常维护',text)
            self.assertNotIn('示范甲市森林消防支队一中队',text)
        self.assertEqual(404,self.request('GET','/materials/daily',cookie=cookie)[0]);self.assertEqual(before,self.files())
    def test_callout_reply_from_phone_remains_a_report_not_arrival(self):
        _,_,_,cookie=self.login();text=self.request('GET','/mobile?tab=callouts',cookie=cookie)[2]
        self.assertIn('type="datetime-local"',text)
        fields=Forms(text).form('/callouts/reply')|dict(can_depart='yes',expected_people='10',eta='2026-10-18T20:30:00+08:00',reason='')
        status,headers,body=self.request('POST','/callouts/reply',fields,cookie)
        self.assertEqual(303,status,body[:500]);self.assertEqual('/mobile?tab=callouts',headers['Location'])
        sid=self.state.resource_demo['waiting_squad'];self.assertNotIn(sid,self.state.ledger.snapshots['fire-001'].squads)
    def test_scan_initial_supplement_readiness_and_duplicate_submit_share_state(self):
        _,_,_,cookie=self.login();before=self.state.workspace.version
        text=self.request('GET','/mobile?tab=arrival',cookie=cookie)[2]
        fields=Forms(text).form('/captain-arrival/scan');self.assertEqual('qr-001',fields['qr_id'])
        status,headers,_=self.request('POST','/captain-arrival/scan',fields,cookie)
        self.assertEqual(303,status);self.assertEqual('/mobile?tab=arrival',headers['Location']);self.assertGreater(self.state.workspace.version,before)
        sid=self.state.resource_demo['waiting_squad'];self.assertNotIn(sid,self.state.ledger.snapshots['fire-001'].squads)
        text=self.request('GET','/mobile?tab=arrival',cookie=cookie)[2]
        first=Forms(text).form('/captain-arrival/report',mode='initial');first['phone']='13900000040'
        self.assertEqual(303,self.request('POST','/captain-arrival/report',first,cookie)[0])
        arrival=self.state.ledger.snapshots['fire-001'].squads[sid];self.assertEqual(9,len(arrival.arrived_phones));lines=deepcopy(arrival.lines)
        self.assertEqual(303,self.request('POST','/captain-arrival/report',first,cookie)[0])
        self.assertEqual(9,len(self.state.ledger.snapshots['fire-001'].squads[sid].arrived_phones))
        text=self.request('GET','/mobile?tab=arrival',cookie=cookie)[2]
        supplement=Forms(text).form('/captain-arrival/report',mode='supplement');supplement['phone']='13900000040'
        self.assertEqual(303,self.request('POST','/captain-arrival/report',supplement,cookie)[0])
        arrival=self.state.ledger.snapshots['fire-001'].squads[sid];self.assertEqual(10,len(arrival.arrived_phones));self.assertEqual(lines,arrival.lines)
        text=self.request('GET','/mobile?tab=arrival',cookie=cookie)[2]
        ready=Forms(text).form('/captain-arrival/readiness')|dict(phone='13900000031',status='standby',order_id='',basis='模拟队长报告本人待命')
        self.assertEqual(303,self.request('POST','/captain-arrival/readiness',ready,cookie)[0])
        self.assertEqual('standby',self.state.event_readiness('fire-001')['13900000031'].status)
        self.assertFalse(self.state.workspace.verify_audit_chain())
    def test_logout_revoke_and_cross_team_form_never_grant_access(self):
        _,_,_,cookie=self.login();text=self.request('GET','/mobile?tab=arrival',cookie=cookie)[2]
        fields=Forms(text).form('/captain-arrival/scan');fields['squad_id']=self.state.resource_demo['called_squads'][0]
        before=self.files();self.assertIn(self.request('POST','/captain-arrival/scan',fields,cookie)[0],(403,409));self.assertEqual(before,self.files())
        token=cookie.split('=',1)[1];self.state.member_logout(token)
        text=self.request('GET','/mobile',cookie=cookie)[2];self.assertIn('队长登录',text);self.assertNotIn('队长扫码与到场确认',text)

class PolicyTests(unittest.TestCase):
    def test_private_explicit_listener_only_and_no_egress_expansion(self):
        for address,prefix,port in [('0.0.0.0',24,18705),('8.8.8.8',24,18705),('127.0.0.1',24,18705),('192.168.1.37',0,18705),('192.168.1.37',24,80)]:
            with self.assertRaises(ValueError):wifi_demo.network(address,prefix,port)
        with patch.object(runtime_access,'_runtime_enabled',True):
            with runtime_access.wifi_listener('192.168.1.37',18705):
                self.assertTrue(runtime_access.permit_socket_event('socket.bind',(None,('192.168.1.37',18705))))
                for event,args in [('socket.bind',(None,('0.0.0.0',18705))),('socket.bind',(None,('192.168.1.37',80))),('socket.connect',(None,('8.8.8.8',443)))]:
                    self.assertFalse(runtime_access.permit_socket_event(event,args))
            self.assertFalse(runtime_access.permit_socket_event('socket.bind',(None,('192.168.1.37',18705))))
    def test_phone_qr_decodes_to_link_and_has_no_credentials(self):
        import cv2,numpy as np
        url='http://192.168.1.37:18705/mobile?event_id=fire-001&qr_id=qr-001'
        svg=event_qr.qr_svg('qr-001',entry_url=url);size=int(re.search(r'viewBox="0 0 (\d+)',svg)[1])
        image=np.full((size,size),255,dtype=np.uint8)
        for x,y in re.findall(r'M(\d+),(\d+)h1v1h-1z',svg):image[int(y),int(x)]=0
        image=cv2.resize(image,None,fx=8,fy=8,interpolation=cv2.INTER_NEAREST)
        self.assertEqual(url,cv2.QRCodeDetector().detectAndDecode(image)[0])
        for bad in ('https://example.com/mobile','http://8.8.8.8/mobile','http://user:password@192.168.1.37:18705/mobile','http://192.168.1.37:18705/mobile?next=https://example.com'):
            with self.assertRaises(ValueError):event_qr.link_svg(bad)

class PhoneTaskTests(Http,unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        self.f=TaskReleaseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.record();self.f.safety();self.f.publish();self.state=self.f.state;self.serve()
    def test_current_task_accept_start_progress_complete_from_phone_forms(self):
        status,_,_,cookie=self.login(self.f.SOURCE,event_id=self.f.event_id,qr_id='');self.assertEqual(303,status)
        text=self.request('GET','/mobile?tab=tasks',cookie=cookie)[2]
        receipt=Forms(text).form('/tasks/formal')|dict(action='received',basis='模拟手机本人明确接收')
        self.assertEqual(303,self.request('POST','/tasks/formal',receipt,cookie)[0])
        for action in ('conditions','begin'):
            text=self.request('GET','/mobile?tab=tasks',cookie=cookie)[2]
            fields=Forms(text).form('/tasks/relations',action=action)|dict(basis='模拟手机核对条件与开始执行')
            self.assertEqual(303,self.request('POST','/tasks/relations',fields,cookie)[0])
        for action in ('progress','completed'):
            text=self.request('GET','/mobile?tab=feedback',cookie=cookie)[2]
            fields=Forms(text).form('/tasks/feedback',action=action)|dict(basis='模拟手机报告进展与完成',overall_basis='',damage_basis='')
            status,headers,body=self.request('POST','/tasks/feedback',fields,cookie)
            self.assertEqual(303,status,body[:500]);self.assertEqual('/mobile?tab=feedback',headers['Location'])
        self.assertTrue(any(f.stage=='done_pending_verify' for f in self.state.workspace.command_flows.values()))
        self.assertFalse(self.state.workspace.verify_audit_chain())
