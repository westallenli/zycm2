"""Draft replies exercise the real HTTP gates; no browser token is an authority."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from panda_brain.auth import GeneralEntryAuth
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.sand_drafts import draft_context


class DraftHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        auth = GeneralEntryAuth(secret='draft-test-only')
        self.state = PanelState(ScenarioWorkspace('draft-test',general_auth=auth), auth=auth,
            **{name:root/(name+'.json') for name in ('registry_path','materials_path','reviews_path','imagery_path','imports_path','alarm_inbox_path')},upload_dir=root/'uploads')
        self.server = make_server(self.state,port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.close)
        _, headers, _ = self.request('/login', {'secret':'draft-test-only'})
        self.cookie = headers['Set-Cookie'].split(';',1)[0]
        self.token = self.cookie.split('=',1)[1]

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, data=None, cookie='', json_reply=False):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        headers = {'Cookie':cookie}
        if json_reply: headers['X-Sand-Draft']='1'
        if data is not None: headers['Content-Type']='application/x-www-form-urlencoded'
        conn.request('POST' if data is not None else 'GET',path,urlencode(data) if data is not None else None,headers)
        res=conn.getresponse();body=res.read().decode();result=(res.status,dict(res.getheaders()),body);conn.close();return result

    def send(self,path,fields,owner=None,event=None,cookie=None):
        context=draft_context(self.state,'commander',self.token)
        fields=dict(fields,_draft_owner=context[0] if owner is None else owner,_draft_event=context[1] if event is None else event)
        code,_,body=self.request(path,fields,self.cookie if cookie is None else cookie,True)
        return code,json.loads(body)

    def test_page_namespace_has_no_bearer_token_and_public_has_no_namespace(self):
        _,_,html=self.request('/sand',cookie=self.cookie)
        owner,event=draft_context(self.state,'commander',self.token)
        self.assertIn('data-draft-owner="'+owner+'"',html)
        self.assertIn('data-draft-event="'+event+'"',html)
        self.assertNotIn(self.token,html)
        _,_,public=self.request('/sand?projector=1',cookie=self.cookie)
        self.assertIn('data-draft-owner=""',public)

    def test_success_reply_is_business_acknowledgement(self):
        code,data=self.send('/sand/goal',{'statement':'保护村庄'})
        self.assertEqual(code,200);self.assertTrue(data['ok']);self.assertEqual(data['redirect'],'/sand#cmd-advisor')
        self.assertTrue(any(frame.statement == '保护村庄' for frame in self.state.workspace.goal_frames.values()))

    def test_validation_failure_retains_explicit_failure(self):
        before=self.state.workspace.version
        code,data=self.send('/sand/goal',{'statement':''})
        self.assertEqual(code,409);self.assertFalse(data['ok']);self.assertTrue(data['message'])
        self.assertEqual(before,self.state.workspace.version)

    def test_query_error_is_not_html_200_success(self):
        code,data=self.send('/sand/ask',{'question':''})
        self.assertEqual(code,422);self.assertFalse(data['ok'])

    def test_wrong_event_rejected_before_business_write(self):
        before=self.state.workspace.version
        code,data=self.send('/sand/goal',{'statement':'不能写入'},event='previous-event')
        self.assertEqual(code,409);self.assertFalse(data['ok'])
        self.assertEqual(before,self.state.workspace.version)

    def test_previous_login_rejected_even_if_new_login_is_authorized(self):
        old_owner=draft_context(self.state,'commander',self.token)[0]
        _,headers,_=self.request('/login',{'secret':'draft-test-only'})
        new_cookie=headers['Set-Cookie'].split(';',1)[0]
        code,data=self.send('/sand/goal',{'statement':'旧会话'},owner=old_owner,cookie=new_cookie)
        self.assertEqual(code,409);self.assertFalse(data['ok'])

    def test_anonymous_json_request_is_unauthorized(self):
        code,data=self.send('/sand/goal',{'statement':'匿名'},cookie='')
        self.assertEqual(code,401);self.assertFalse(data['ok'])

    def test_normal_form_keeps_original_redirect_contract(self):
        code,headers,_=self.request('/sand/goal',{'statement':'传统提交'},self.cookie)
        self.assertEqual(code,303);self.assertEqual(headers['Location'],'/sand#cmd-advisor')

    def test_scope_changes_on_role_event_and_workspace(self):
        first=draft_context(self.state,'commander',self.token)
        self.assertNotEqual(first[0],draft_context(self.state,'member',self.token)[0])
        self.state.active_event_id='another-event'
        self.assertNotEqual(first[1],draft_context(self.state,'commander',self.token)[1])
        self.assertEqual(('', ''),draft_context(self.state,'public',self.token))

    def upload(self, payload=b'not an image', event=None, content_type=None):
        owner, current = draft_context(self.state, 'commander', self.token)
        fields = {'_draft_owner':owner, '_draft_event':current if event is None else event,
                  'source_system':'模拟无人机', 'collected_at':'2026-09-26T10:00:00+08:00', 'has_spatial':'0'}
        boundary = 'p02-test-boundary'
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k,v in fields.items()]
        parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="demo.png"\r\nContent-Type: image/png\r\n\r\n'.encode()+payload+b'\r\n', f'--{boundary}--\r\n'.encode()]
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        conn.request('POST','/sand/image-upload',b''.join(parts),{'Cookie':self.cookie,'X-Sand-Draft':'1','Content-Type':content_type or 'multipart/form-data; boundary='+boundary})
        res=conn.getresponse();result=res.status,json.loads(res.read());conn.close();return result

    def test_measurement_computes_without_creating_annotation(self):
        before=len(self.state.workspace.annotations)
        code,data=self.send('/sand/measure',{'points':'0,0 10,0 10,10 0,10'})
        self.assertEqual(code,200);self.assertEqual(data['kind'],'measurement')
        self.assertEqual(data['result']['area_m2'],1600)
        self.assertEqual(len(self.state.workspace.annotations),before)

    def test_measurement_validation_returns_json(self):
        code,data=self.send('/sand/measure',{'points':'invalid'})
        self.assertEqual(code,422);self.assertFalse(data['ok'])

    def test_measurement_save_creates_only_draft(self):
        from panda_brain.map_annotations import AnnotationStatus
        code,data=self.send('/sand/measure',{'points':'0,0 10,0 10,10','save':'1','area_type':'危险区'})
        self.assertEqual(code,200);self.assertEqual(data['redirect'],'/sand#drawer-annotate')
        marks=list(self.state.workspace.annotations.values());self.assertEqual(len(marks),1)
        self.assertEqual(marks[0].status,AnnotationStatus.DRAFT)

    def test_upload_rejects_invalid_file_in_json(self):
        code,data=self.upload()
        self.assertEqual(code,409);self.assertIn('JPG/PNG',data['message'])
        self.assertFalse(list(Path(self.tmp.name).rglob('*.png')))

    def test_upload_scope_is_checked_before_writing_bytes(self):
        code,data=self.upload(b'\x89PNG\r\n\x1a\nsynthetic',event='old-event')
        self.assertEqual(code,409);self.assertFalse(data['ok'])
        self.assertFalse(list(Path(self.tmp.name).rglob('*.png')))

    def test_upload_success_has_business_acknowledgement(self):
        code,data=self.upload(b'\x89PNG\r\n\x1a\nsynthetic')
        self.assertEqual(code,200);self.assertTrue(data['ok'])
        self.assertEqual(len(self.state.imagery.for_event('none')),1)
        self.assertEqual(len(list(Path(self.tmp.name).rglob('*.png'))),1)

    def test_upload_invalid_multipart_returns_json(self):
        code,data=self.upload(content_type='multipart/form-data')
        self.assertEqual(code,422);self.assertFalse(data['ok'])

    def test_existing_object_error_does_not_turn_into_html_navigation(self):
        code,data=self.send('/sand/annotate-edit',{'annotation_id':'missing','title':'保留这段修改'})
        self.assertEqual(code,409);self.assertFalse(data['ok'])
        code,data=self.send('/sand/image-analyze',{'image_id':'missing'})
        self.assertEqual(code,409);self.assertFalse(data['ok'])

    def test_existing_object_action_checks_current_event(self):
        before=self.state.workspace.version
        code,data=self.send('/sand/review-current',{},event='old-event')
        self.assertEqual(code,409);self.assertFalse(data['ok'])
        self.assertEqual(self.state.workspace.version,before)
