"""Current team information, photo integrity, recovery and map concurrency."""
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import base64,hashlib,http.client,io,json,socket,time,unittest
from PIL import Image
from panda_brain import captain_reports as reports
from .test_upgrade_captain_field import Client
from . import test_upgrade_wifi_mobile as fixtures

def jpeg(*,gps=False,size=(80,60)):
    im=Image.new('RGB',size,(30,100,60));out=io.BytesIO();exif=Image.Exif()
    if gps:exif[270]='synthetic private metadata';exif[274]=6
    im.save(out,format='JPEG',quality=85,exif=exif);return out.getvalue()

class CaptainReportTests(Client,fixtures.Http,unittest.TestCase):
    setUp=fixtures.PhoneTests.setUp
    files=fixtures.PhoneTests.files
    def body(self,photos=None,**extra):
        return dict(team_id=self.state.resource_demo['waiting_squad'],content='模拟上报：巡护道路被倒木阻断，现场待核实',request_id='photo-report-001',photos=[base64.b64encode(p).decode() for p in (photos or [])])|extra
    def raw(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        try:
            c.request(method,path,body,headers or {});r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
        finally:c.close()
    def test_text_report_duplicate_and_shared_assistant_computer_source(self):
        token=self.native_login();value=self.body();ledger=deepcopy(self.state.ledger.to_dict())
        for _ in range(2):self.assertEqual(200,self.call('POST','report',value,token)[0])
        self.assertEqual(1,len(reports.rows(self.state.workspace)));self.assertEqual(ledger,self.state.ledger.to_dict())
        s,v=self.call('GET','reports',token=token);self.assertEqual(1,len(v['reports']));self.assertIn('待核实',v['reports'][0]['status'])
        s,v=self.call('POST','ask',dict(question='现场上报了什么道路情况'),token);self.assertIn('倒木',v['answer']);self.assertIn('待核实',v['answer']);self.assertTrue(any(x.startswith('field:') for x in v['references']))
        from panda_brain.intelligence import collect,content
        view,_=collect(self.state,'fire-001');self.assertIn('倒木',content(view));self.assertFalse(view['groups']['field_reports'][0]['photo_links'])
        self.assertEqual(409,self.call('POST','report',value|dict(content='另一条内容'),token)[0])
    def test_three_photos_normalize_metadata_reload_and_authenticated_read(self):
        token=self.native_login();value=self.body([jpeg(gps=True),jpeg(),jpeg(size=(50,50))]);self.assertEqual(200,self.call('POST','report',value,token)[0])
        self.assertEqual(200,self.call('POST','report',value,token)[0]);row=reports.rows(self.state.workspace)[0];self.assertEqual(3,len(row['photos']))
        url=reports.photo_url(row,0,native=True);s,h,data=self.raw('GET',url,headers={'Authorization':'Bearer '+token});self.assertEqual(200,s);self.assertEqual('image/jpeg',h['Content-Type']);self.assertEqual(row['photos'][0]['sha256'],hashlib.sha256(data).hexdigest())
        with Image.open(io.BytesIO(data)) as image:self.assertFalse(image.getexif());self.assertEqual((60,80),image.size)
        self.assertEqual(403,self.raw('GET',url)[0]);self.assertEqual(403,self.raw('GET',url.replace('fire-001','foreign'),headers={'Authorization':'Bearer '+token})[0])
        deputy,_=self.state.member_login('13900000120','123456');cookie='member_session='+deputy
        self.assertEqual(200,self.raw('GET',reports.photo_url(row,0),headers={'Cookie':cookie})[0])
        from panda_brain.intelligence import collect,content
        view,_=collect(self.state,'fire-001');self.assertIn('/captain-reports/photo?',content(view));self.assertIn('loading="lazy"',content(view))
        from panda_brain.resource_demo import open_state
        loaded=open_state(self.state.registry_path.parent);again=reports.rows(loaded.workspace)[0];self.assertEqual(row,again);self.assertFalse(loaded.workspace.verify_audit_chain())
    def test_invalid_photos_exact_fields_and_foreign_team_are_atomic(self):
        token=self.native_login();original=self.files();valid=self.body([jpeg()])
        cases=[valid|dict(photos=['x']),self.body([jpeg()]*4),valid|dict(photos=[base64.b64encode(b'\xff\xd8broken\xff\xd9').decode()]),valid|dict(photos=[base64.b64encode(b'x'*(1024*1024+1)).decode()]),valid|dict(photos=[base64.b64encode(jpeg(size=(2050,10))).decode()]),valid|dict(actor='someone'),valid|dict(content='')]
        for value in cases:
            with self.subTest(keys=list(value)):self.assertEqual(409,self.call('POST','report',value,token)[0]);self.assertEqual(original,self.files())
        self.assertEqual(403,self.call('POST','report',valid|dict(team_id='foreign'),token)[0]);self.assertEqual(original,self.files())
    def test_failed_snapshot_rolls_back_photo_and_record(self):
        token=self.native_login();original=self.files()
        with patch.object(self.state,'_persist_active_task_snapshot',side_effect=OSError('synthetic save failure')):
            self.assertEqual(503,self.call('POST','report',self.body([jpeg()]),token)[0])
        self.assertEqual(original,self.files());self.assertFalse(reports.rows(self.state.workspace));self.assertFalse(self.state.workspace.verify_audit_chain())
        self.assertEqual(200,self.call('POST','report',self.body([jpeg()]),token)[0])
    def test_photo_tamper_is_not_served_and_logout_revokes_read(self):
        token=self.native_login();self.assertEqual(200,self.call('POST','report',self.body([jpeg()]),token)[0]);row=reports.rows(self.state.workspace)[0];path=reports.photo_path(self.state,'fire-001',row['photos'][0]['sha256']);path.write_bytes(b'changed');url=reports.photo_url(row,0,native=True)
        self.assertEqual(409,self.raw('GET',url,headers={'Authorization':'Bearer '+token})[0]);self.call('POST','logout',{},token);self.assertEqual(403,self.raw('GET',url,headers={'Authorization':'Bearer '+token})[0])
    def test_missing_body_framing_and_duplicate_json_are_rejected(self):
        token=self.native_login();auth={'Authorization':'Bearer '+token,'Content-Type':'application/json'};original=self.files()
        self.assertEqual(409,self.raw('POST','/app/v2/report','{}',auth)[0])
        text=json.dumps(self.body())[:-1]+',"content":"duplicate"}'
        self.assertEqual(409,self.raw('POST','/app/v2/report',text,auth)[0]);self.assertEqual(original,self.files())
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10);c.putrequest('POST','/app/v2/report');c.putheader('Authorization','Bearer '+token);c.putheader('Content-Type','application/json');c.putheader('Content-Length',str(reports.MAX_BODY+1));c.endheaders();r=c.getresponse();self.assertEqual(409,r.status);r.read();c.close()
    def test_slow_photo_body_does_not_hold_map_state_lock(self):
        token=self.native_login();raw=json.dumps(self.body([jpeg()])).encode();c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15);c.putrequest('POST','/app/v2/report');c.putheader('Content-Type','application/json');c.putheader('Authorization','Bearer '+token);c.putheader('Content-Length',str(len(raw)));c.endheaders();c.send(raw[:20]);time.sleep(.15)
        try:
            start=time.monotonic();s,v=self.call('GET','state',token=token);elapsed=time.monotonic()-start;self.assertEqual(200,s);self.assertLess(elapsed,4,'slow body blocked state/map requests')
            c.send(raw[20:]);r=c.getresponse();self.assertEqual(200,r.status);r.read()
        finally:c.close()
