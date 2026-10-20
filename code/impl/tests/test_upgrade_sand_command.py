"""Named reconnaissance, actual image bounds, and model preview boundaries."""
import json
import base64
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain import sand_command as sc
from panda_brain.durable_storage import action
from panda_brain.panel import make_server, render_page
from panda_brain.persistence import load_workspace


class CommandTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_task_release import TaskReleaseTests
        self.f=TaskReleaseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.s=self.f.state;self.eid=self.f.event_id;self.token=self.f.tokens[self.f.DEPUTY]

    def values(self, **changes):
        return dict(event_id=self.eid,id='RECON-one',expected_version='0',action='send',
            need='核实北侧通道是否可用',area='北侧通道',recipient=self.f.DUTY,deadline='下一次会商前回报',
            basis=sc.summary(self.s,self.token)['basis'],report='',observed_at='',geo='',kind='',
            ack='1',request_id='recon-confirm-one')|changes

    def write(self,v,token=None):
        with action(self.s):return sc.write_recon(self.s,token or self.token,v)

    def http(self, fields, token=None):
        server=make_server(self.s,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=HTTPConnection('127.0.0.1',server.server_port,timeout=20)
            conn.request('POST','/sand/command-assistant',urlencode(fields),
                {'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.token),'X-Sand-Event':self.eid})
            r=conn.getresponse();body=r.read();return r.status,json.loads(body)
        finally:server.shutdown();server.server_close();thread.join()

    def test_read_summary_and_render_never_dispatch(self):
        before=self.f.files();a=sc.summary(self.s,self.token)
        self.assertEqual([],a['requests']);self.assertIn(self.f.DUTY,a['recipients'])
        self.assertTrue(a['gaps']);self.assertEqual(before,self.f.files())
        status,page=render_page(self.s,'/sand',member_token=self.token)
        self.assertEqual(200,status);self.assertIn('sand-unified-assistant',page)
        self.assertIn('/static/sand-command.js',page);self.assertEqual(before,self.f.files())

    def test_confirmed_request_is_named_idempotent_and_reloads(self):
        v=self.values();before=self.f.files();self.write(v);self.write(v)
        rows=sc.recon_history(self.s.workspace);self.assertEqual(1,len(rows))
        self.assertEqual('sent',rows['RECON-one']['status'])
        saved=load_workspace(self.s.registry.events[self.eid].snapshot_path,general_auth=self.s.auth)
        self.assertEqual(rows,sc.recon_history(saved));self.assertFalse(saved.verify_audit_chain())
        for p in (self.s.registry_path,self.s.materials_path):self.assertEqual(before[p],p.read_bytes())

    def test_without_confirmation_or_wrong_recipient_cannot_send(self):
        for changes in ({'ack':''},{'recipient':'not-a-current-person'},{'area':''},{'deadline':''}):
            before=self.f.files()
            with self.assertRaises(ValueError):self.write(self.values(**changes))
            self.assertEqual(before,self.f.files())

    def test_only_current_deputy_can_dispatch_and_stale_basis_rejected(self):
        before=self.f.files()
        with self.assertRaises(PermissionError):self.write(self.values(),self.f.tokens[self.f.DUTY])
        with self.assertRaises(ValueError):self.write(self.values(basis='0'*64))
        self.assertEqual(before,self.f.files())

    def test_named_report_without_position_does_not_invent_map_or_close_tasks(self):
        v=self.values();self.write(v)
        report=v|dict(action='report',expected_version='1',request_id='report-one',ack='',
            report='尚未核实可通行，已拍摄原图待补充。',observed_at='2026-10-14T10:00:00+08:00')
        before_marks=dict(self.s.workspace.annotations)
        self.write(report,self.f.tokens[self.f.DUTY]);self.write(report,self.f.tokens[self.f.DUTY])
        r=sc.recon_history(self.s.workspace)['RECON-one'];self.assertEqual('reported',r['status'])
        self.assertEqual(before_marks,self.s.workspace.annotations);self.assertFalse(r['annotation_id'])
        self.assertEqual(0,len(self.s.workspace.orders))

    def test_other_person_cannot_report_and_old_version_refused(self):
        v=self.values();self.write(v);before=self.f.files()
        report=v|dict(action='report',expected_version='1',request_id='report-one',ack='',report='匿名回报',observed_at='2026-10-14T10:00:00Z')
        with self.assertRaises(PermissionError):self.write(report)
        with self.assertRaises(ValueError):self.write(report|{'expected_version':'0'},self.f.tokens[self.f.DUTY])
        self.assertEqual(before,self.f.files())

    def test_located_report_requires_actual_object_kind(self):
        v=self.values();self.write(v)
        r=v|dict(action='report',expected_version='1',request_id='report-geo',ack='',report='实际核实火点',observed_at='2026-10-14T10:00:00Z',geo='30.12,103.56')
        before=self.f.files()
        with self.assertRaises(ValueError):self.write(r,self.f.tokens[self.f.DUTY])
        self.assertEqual(before,self.f.files())
        self.write(r|{'kind':'hotspot'},self.f.tokens[self.f.DUTY])
        saved=sc.recon_history(self.s.workspace)['RECON-one'];mark=self.s.workspace.annotations[saved['annotation_id']]
        self.assertEqual(((30.12,103.56),),mark.geo_points)
        self.assertNotEqual('published',mark.status.value)

    def test_save_error_rolls_back_both_observation_and_request(self):
        before=self.f.files()
        with patch.object(self.s,'_persist_active_task_snapshot',side_effect=OSError('anonymous injected write failure')):
            with self.assertRaises(OSError):self.write(self.values())
        self.assertEqual({},sc.recon_history(self.s.workspace));self.assertEqual(before,self.f.files())

    def test_image_range_descriptions_and_gps_points_are_not_footprints(self):
        for value in ('北山','30,103','30,103,NaN,104','31,103,30,104','30,103,31,999'):
            self.assertIsNone(sc.image_bounds(value))
        self.assertEqual([[30.,103.],[31.,104.]],sc.image_bounds('30,103,31,104'))
        self.assertEqual([],sc.imagery_payload(self.s,'public'))

    def test_wide_arrow_control_geometry_is_saved_as_draft_without_publication(self):
        geo='30.120,103.560;30.122,103.561;30.124,103.565;30.121,103.559'
        annotation_id=self.s.handle_sand_annotate(kind='arrow',title='匿名宽尾箭头',points='',geo_text=geo,action='draft')
        mark=self.s.workspace.annotations[annotation_id]
        self.assertEqual(4,len(mark.geo_points));self.assertEqual('draft',mark.status.value)
        from panda_brain.sandview import _marks_payload
        payload=next(m for m in _marks_payload(self.s,'commander') if m['id']==annotation_id)
        self.assertEqual(4,len(payload['pts']));self.assertEqual('arrow',payload['kind'])

    def test_real_command_assets_are_served_with_script_mime_and_no_business_writes(self):
        before=self.f.files();server=make_server(self.s,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for name in ('sand-command.js','sand-command.css','wide-arrow.js'):
                conn=HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                conn.request('GET','/static/'+name);response=conn.getresponse()
                self.assertEqual(200,response.status);self.assertTrue(response.read())
                self.assertIn('text/',response.getheader('Content-Type'));conn.close()
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(before,self.f.files())

    def test_image_alignment_is_named_scoped_versioned_and_idempotent(self):
        self.s.upload_dir=self.f.root/'uploads'
        self.s.imagery_path=self.f.root/'imagery.json'
        image_id=self.s.handle_image_upload(source_system='匿名无人机',collected_at='2026-10-14T10:00:00Z',
            has_spatial=False,filename='anonymous.png',payload=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII='))
        image=sc.imagery_payload(self.s,'member')[0]
        v=dict(op='image-align',event_id=self.eid,csrf=self.s.member_session(self.token).custody_csrf,
            image_id=image_id,bounds='30.11,103.55,30.13,103.57',image_basis=image['basis'],request_id='align-one')
        status,_=self.http(v|{'bounds':'北山'});self.assertEqual(409,status)
        status,_=self.http(v|{'csrf':self.s.member_session(self.f.tokens[self.f.DUTY]).custody_csrf},self.f.tokens[self.f.DUTY]);self.assertEqual(403,status)
        status,r=self.http(v);self.assertEqual(200,status,r)
        status,r=self.http(v);self.assertEqual(200,status,r)
        self.assertEqual([[30.11,103.55],[30.13,103.57]],sc.imagery_payload(self.s,'member')[0]['bounds'])
        self.assertEqual('overlaid',self.s.imagery.get(image_id).status)
        status,_=self.http(v|{'request_id':'align-stale','bounds':'30.10,103.55,30.13,103.57'});self.assertEqual(409,status)

    def test_report_becomes_current_intelligence_without_claiming_gap_closed(self):
        v=self.values();self.write(v)
        r=v|dict(action='report',expected_version='1',request_id='report-data',ack='',report='仍需核实通道，无法判断可通行。',observed_at='2026-10-14T10:00:00Z')
        self.write(r,self.f.tokens[self.f.DUTY])
        from panda_brain.intelligence import collect
        result,_=collect(self.s,self.eid)
        self.assertIn('无法判断',result['groups']['recon'][0]['text'])
        self.assertTrue(result['gaps'])

    def test_http_rejects_csrf_duplicates_scope_and_unknown_fields(self):
        base=dict(op='recon',csrf=self.s.member_session(self.token).custody_csrf,**self.values())
        status,_=self.http(base|{'csrf':'wrong'});self.assertEqual(403,status)
        status,_=self.http(base|{'event_id':'other'});self.assertEqual(409,status) if status==409 else self.assertEqual(403,status)
        status,_=self.http(base|{'extra':'bad'});self.assertEqual(409,status)
        status,r=self.http(base);self.assertEqual(200,status,r)
        self.assertEqual(1,len(sc.recon_history(self.s.workspace)))

    def test_model_preview_never_calls_provider_and_binds_actor_version(self):
        from panda_brain.sandtable import InquiryAnswer
        local=InquiryAnswer('unknown','没有现场记录','',(),'', '未知','')
        class Provider:
            name='anonymous-fake'
            calls=0
            def complete(self,**kwargs):self.calls+=1;return json.dumps({'answer':'这是知识说明，不代表现场事实。','unknown':'现场适用性待核验'})
        provider=Provider()
        with patch.object(self.s,'_build_enhancer',return_value=SimpleNamespace(provider=provider)):
            preview=sc.model_preview(self.s,self.token,'解释术语',local)
            self.assertEqual(0,provider.calls)
            with self.assertRaises(ValueError):sc.model_confirm(self.s,self.f.tokens[self.f.DUTY],preview['token'])
            preview=sc.model_preview(self.s,self.token,'解释术语',local)
            result=sc.model_confirm(self.s,self.token,preview['token'])
            self.assertEqual(1,provider.calls);self.assertEqual('这是知识说明，不代表现场事实。',result['conclusion'])
            self.assertIn('待核验',result['unconfirmed'])
            with self.assertRaises(ValueError):sc.model_confirm(self.s,self.token,preview['token'])

    def test_model_error_preserves_local_history_and_does_not_execute(self):
        from panda_brain.sandtable import InquiryAnswer
        local=InquiryAnswer('unknown','缺少资料','',(),'', '未知','')
        class Broken:
            def complete(self,**kwargs):raise RuntimeError('anonymous')
        with patch.object(self.s,'_build_enhancer',return_value=SimpleNamespace(provider=Broken())):
            preview=sc.model_preview(self.s,self.token,'分析',local)
            with self.assertRaises(ValueError):sc.model_confirm(self.s,self.token,preview['token'])
        self.assertEqual({},self.s.workspace.orders)

    def admin_model(self):
        admin=self.s.login('p08-transfer-demo')
        session=self.s.model_session(admin)
        self.s.handle_model_settings(dict(csrf=session.csrf,action='save-local',base_url='http://127.0.0.1:11434/v1',
            model='anonymous-local',api_key='anonymous-key-not-real',enabled='on',timeout='1'),session_token=admin)
        self.s.handle_model_settings(dict(csrf=session.csrf,action='select-mode',mode='local'),session_token=admin)
        return admin

    def share(self,admin,**changes):
        session=self.s.model_session(admin)
        values=dict(csrf=session.csrf,action='share-sand',event_id=self.eid,expected_revision=str(session.revision),ack='1')|changes
        self.s.handle_model_settings(values,session_token=admin)

    def local_answer(self):
        from panda_brain.sandtable import InquiryAnswer
        return InquiryAnswer('unknown','没有本地现场记录','',(),'', '未知','')

    def test_admin_can_delegate_current_event_without_exposing_credentials_or_settings(self):
        admin=self.admin_model();self.share(admin)
        with self.assertRaises(PermissionError):self.s.model_session(self.token)
        provider,_,_=sc.configured_model(self.s,self.eid)
        self.assertEqual('local',provider.connection.vendor)
        _,page=render_page(self.s,'/sand',member_token=self.token)
        self.assertNotIn('anonymous-key-not-real',page)
        status,_=render_page(self.s,'/models',member_token=self.token)
        self.assertEqual(403,status)
        self.assertNotIn('anonymous-key-not-real',json.dumps(sc.summary(self.s,self.token)))

    def test_delegation_requires_admin_csrf_current_event_enabled_profile_and_confirmation(self):
        admin=self.admin_model()
        from panda_brain.panel import PanelActionError
        with self.assertRaises(PermissionError):self.share(self.token)
        with self.assertRaises(PermissionError):self.share(admin,csrf='wrong')
        for changes in ({'ack':'0'},{'event_id':'other-event'},{'expected_revision':'0'}):
            with self.assertRaises(PanelActionError):self.share(admin,**changes)
        self.assertFalse(self.s.sand_model_bindings)

    def test_admin_stop_or_configuration_change_invalidates_deputy_preview(self):
        admin=self.admin_model();self.share(admin)
        preview=sc.model_preview(self.s,self.token,'解释术语',self.local_answer())
        self.share(admin,action='stop-sand',ack='0')
        with self.assertRaises(ValueError):sc.model_confirm(self.s,self.token,preview['token'])
        self.share(admin)
        preview=sc.model_preview(self.s,self.token,'解释术语',self.local_answer())
        session=self.s.model_session(admin)
        self.s.handle_model_settings(dict(csrf=session.csrf,action='clear-local'),session_token=admin)
        with self.assertRaises(ValueError):sc.model_confirm(self.s,self.token,preview['token'])
        with self.assertRaises(ValueError):sc.configured_model(self.s,self.eid)

    def test_admin_logout_releases_delegation_and_provider_previews(self):
        admin=self.admin_model();self.share(admin)
        preview=sc.model_preview(self.s,self.token,'解释术语',self.local_answer())
        self.s.logout(admin)
        self.assertNotIn(preview['token'],self.s.deepseek_previews)
        with self.assertRaises(ValueError):sc.configured_model(self.s,self.eid)
        self.assertNotIn('anonymous-key-not-real',repr(self.s.sand_model_bindings))

    def test_another_event_does_not_inherit_admin_session_delegation(self):
        admin=self.admin_model();self.share(admin)
        with patch.object(self.s,'_build_enhancer',return_value=None):
            with self.assertRaises(ValueError):sc.configured_model(self.s,'foreign-event')
        self.assertNotIn('foreign-event',self.s.sand_model_bindings)

    def test_admin_local_transport_is_called_only_after_named_deputy_confirmation(self):
        captured=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                payload=json.dumps({'choices':[{'message':{'content':json.dumps({'answer':'匿名本地模型回答','unknown':'现场适用性待核验'},ensure_ascii=False)}}]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            def log_message(self,*_):pass
        server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            admin=self.admin_model();session=self.s.model_session(admin)
            self.s.handle_model_settings(dict(csrf=session.csrf,action='save-local',base_url=f'http://127.0.0.1:{server.server_port}/v1',model='anonymous-local',enabled='on',timeout='1'),session_token=admin)
            self.share(admin);preview=sc.model_preview(self.s,self.token,'解释术语',self.local_answer())
            self.assertEqual([],captured)
            answer=sc.model_confirm(self.s,self.token,preview['token'])
            self.assertEqual(1,len(captured));self.assertIn('匿名本地模型回答',answer['conclusion'])
            self.assertEqual('anonymous-local',captured[0]['model'])
            self.assertNotIn('anonymous-key-not-real',json.dumps(captured))
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
