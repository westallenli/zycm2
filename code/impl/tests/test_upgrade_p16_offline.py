"""Licensed map bytes and named original-report replay; isolated anonymous data."""
import base64,copy,hashlib,http.client,io,json,sqlite3,threading,unittest,zipfile
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from unittest.mock import patch
from panda_brain import offline_resources as maps,terminal_reports as reports
from panda_brain.panel import make_server,PanelActionError
from panda_brain.persistence import save_workspace
from panda_brain.workspace import event_hash
from panda_brain.authorization import Role
from . import test_upgrade_manual_material_transfers as transfer
from . import test_upgrade_formal_task_feedback as formal

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
def stamp():return datetime.now(timezone.utc).isoformat()
def package(*,manifest=None,files=None):
    m=dict(name='匿名栅格测试资源',source='自行生成的测试像素，非真实地图',license='仅匿名软件测试',license_ref='匿名测试声明，不代表真实地图授权',
        valid_until=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),cache_allowed=True,export_allowed=False,coordinate_system='EPSG:3857',
        bounds=[-180,-85,180,85],min_zoom=0,max_zoom=0,geography_basis='仅测试XYZ路径，未验证实地坐标',data_kind='simulated')
    if manifest:m.update(manifest)
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('manifest.json',json.dumps(m))
        for key,blob in (files or {'tiles/0/0/0.png':PNG}).items():z.writestr(key,blob)
    return base64.b64encode(stream.getvalue()).decode()

class P16Tests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space'):
        locals()[name]=getattr(transfer.NamedTransferTests,name)
    def setUp(self):transfer.NamedTransferTests.setUp(self)
    def files(self):return {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.transaction' not in str(p)}
    def map(self,**changes):
        args=dict(member_token=self.tokens[self.DEPUTY],operation='import',basis='匿名资源授权、坐标范围及缓存核对',archive_base64=package(),expected_version=0,request_id='map-import-001');args.update(changes)
        return self.state.handle_offline_map_package(self.event_id,**args)
    def draft(self,**changes):
        c=reports.context(self.state,self.event_id,self.tokens[self.SOURCE])
        value=dict(schema_version=1,event_id=self.event_id,actor_id=self.SOURCE,request_id='original-report-001',created_at=stamp(),context_digest=c['context_digest'],operation='material_loss',
            details=dict(squad_id=self.source_squad,item_id=self.item_id,loss_id='offline-loss-001',kind='consumed',amount=1,expected_version='0',evidence='断线时本队实际报告，待核实',occurred_at='2026-10-10T10:00:00+08:00',reason='匿名原报告'))
        value.update(changes);return value
    def submit(self,v,token=None):return self.state.handle_terminal_report(self.event_id,member_token=token or self.tokens[self.SOURCE],envelope=v)
    def test_maps_explicit_permission_raw_source_versions_and_restart(self):
        before=self.files();encoded=package()
        with self.assertRaises(PermissionError):self.map(member_token=self.tokens[self.SOURCE],archive_base64=encoded)
        self.assertEqual(before,self.files());r=self.map(archive_base64=encoded);selected=maps.current(self.event_space());self.assertEqual(r.id,selected[0].id)
        self.assertEqual(hashlib.sha256(base64.b64decode(encoded)).hexdigest(),selected[1]['sha256']);self.assertEqual(PNG,maps.tile(self.event_space(),r.id,'0/0/0.png')[0])
        before=self.files();self.map(archive_base64=encoded);self.assertEqual(before,self.files())
        self.map(operation='revoke',archive_base64='',expected_version=1,request_id='map-revoke-001');self.assertIsNone(maps.current(self.event_space()));self.assertIsNone(maps.tile(self.event_space(),r.id,'0/0/0.png'))
        self.state=self.new_state();self.assertEqual(2,len(maps.inspect(self.event_space())));self.assertIsNone(maps.current(self.event_space()))
    def test_old_revision_and_expired_license_never_fall_back(self):
        old=self.map();new=self.map(expected_version=1,request_id='map-update-001',archive_base64=package(manifest={'name':'新版匿名资源'}))
        self.assertIsNone(maps.tile(self.event_space(),old.id,'0/0/0.png'));self.assertIsNotNone(maps.tile(self.event_space(),new.id,'0/0/0.png'))
        self.assertIsNone(maps.current(self.event_space(),now=datetime.now(timezone.utc)+timedelta(days=2)))
        before=self.files()
        with self.assertRaises(ValueError):self.map(expected_version=2,request_id='expired-map-001',archive_base64=package(manifest={'valid_until':'2020-01-01T00:00:00+00:00'}))
        self.assertEqual(before,self.files())
    def test_zip_traversal_unknown_coordinate_duplicate_fields_and_empty_tiles(self):
        for raw in [package(files={'../evil.png':PNG}),package(manifest={'coordinate_system':'GCJ-02'}),package(files={'tiles/0/1/0.png':PNG}),package(files={'tiles/0/0/0.svg':b'<svg/>'}),base64.b64encode(b'broken').decode()]:
            with self.assertRaises(ValueError):maps.archive(raw)
        with self.assertRaises(ValueError):maps.loads('{"name":1,"name":2}')
        with self.assertRaises(ValueError):maps.loads('{"amount":NaN}')
    def test_mbtiles_tms_readonly_fixed_sql_and_no_views(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE tiles(zoom_level,tile_column,tile_row,tile_data)');db.execute('INSERT INTO tiles VALUES (1,0,0,?)',(PNG,));raw=db.serialize();db.close()
        encoded=package(manifest={'min_zoom':1,'max_zoom':1},files={'map.mbtiles':raw});self.assertEqual({'1/0/1'},set(maps.archive(encoded)[2]))
        db=sqlite3.connect(':memory:');db.execute('CREATE VIEW tiles AS SELECT 0 zoom_level,0 tile_column,0 tile_row,1 tile_data');raw=db.serialize();db.close()
        with self.assertRaisesRegex(ValueError,'普通'):maps.archive(package(files={'map.mbtiles':raw}))
    def test_map_save_failure_rolls_back_and_then_exact_retry(self):
        before=self.files();encoded=package()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('anonymous save failure')):
            with self.assertRaises(OSError):self.map(archive_base64=encoded)
        self.assertEqual(before,self.files());self.assertFalse(maps.inspect(self.event_space()));self.map(archive_base64=encoded)
    def test_map_and_report_generic_revision_bypass_refused(self):
        r=self.map();space=self.event_space();space.unlock_general_entry(self.auth.authenticate('p08-transfer-demo'))
        with self.assertRaises(PermissionError):space.record_object_revision(r,expected_version=0)
        v=self.draft();r=self.submit(v);space=self.event_space();space.unlock_general_entry(self.auth.authenticate('p08-transfer-demo'))
        with self.assertRaises(PermissionError):space.record_object_revision(r,expected_version=0)
    def test_resource_mirror_damage_never_serves_or_replaces_old_source(self):
        r=self.map();space=self.event_space();space.object_revisions[r.id]=replace(r,source='untrusted replacement')
        with self.assertRaises(ValueError):maps.current(space)
        with self.assertRaises(ValueError):maps.record(space,self.DEPUTY,operation='import',basis='不能覆盖坏来源',archive_base64=package(),expected_version=1,request_id='tamper-replace-001')
        self.state.workspace=space;self.state.active_event_id=self.event_id
        status=maps.view_status(self.state);self.assertIsNone(status['resource']);self.assertIn('停止供图',status['error'])
    def test_original_content_tamper_even_with_rebuilt_audit_disagrees_with_business(self):
        v=self.draft();r=self.submit(v);space=self.event_space();changed=json.loads(r.content_json);changed['envelope']['details']['amount']=2
        altered=replace(r,content_json=json.dumps(changed));space.object_revisions[r.id]=altered
        from dataclasses import asdict
        previous=space.events[0].prev_hash;events=[]
        for e in space.events:
            payload={'revision_id':r.id,'revision':asdict(altered)} if e.kind=='ObjectRevisionRecorded' and e.payload.get('revision_id')==r.id else e.payload
            h=event_hash(e.sequence,e.kind,payload,previous,e.recorded_at);events.append(replace(e,payload=payload,prev_hash=previous,hash=h));previous=h
        space._events=events;self.assertFalse(space.verify_audit_chain())
        with self.assertRaisesRegex(ValueError,'实际业务'):reports.history(space)
    def test_named_library_cannot_revoke_active_frozen_captain(self):
        before=self.files()
        with self.assertRaisesRegex(ValueError,'冻结队长'):
            self.state.handle_personnel_management(self.event_id,member_token=self.tokens[self.DEPUTY],operation='import_library',basis='匿名职责核对',
                details={'csv_text':'改为非队长,'+self.SOURCE+',匿名中队,,否'},expected_version=0,request_id='personnel-captain-revoke')
        self.assertEqual(before,self.files())
    def test_missing_existing_event_source_cannot_rebuild_history_on_enter(self):
        self.state.login('p08-transfer-demo')
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],snapshot_path='',status='closed')
        before=self.files()
        with self.assertRaisesRegex(PanelActionError,'不能新建替代历史'):self.state.handle_enter_event(self.event_id)
        self.assertEqual(before,self.files());self.assertEqual('closed',self.state.registry.events[self.event_id].status)
        self.assertIsNone(maps.active_for(self.state))
    def test_context_is_named_and_does_not_include_credential_or_csrf(self):
        before=self.files();value=reports.context(self.state,self.event_id,self.tokens[self.SOURCE]);self.assertEqual(before,self.files())
        self.assertEqual(self.SOURCE,value['actor_id']);self.assertEqual(1,len(value['templates']));self.assertNotIn('csrf',value)
        self.assertNotIn('123456',json.dumps(value));self.assertIsNone(value['map'])
        with self.assertRaises(PermissionError):reports.context(self.state,self.event_id,None)
    def test_original_loss_replay_is_zero_write_and_preserves_all_fields(self):
        v=self.draft();r=self.submit(v);self.assertEqual(v,json.loads(r.content_json)['envelope']);before=self.files();same=self.submit(v);self.assertEqual(r.id,same.id);self.assertEqual(before,self.files())
        rows=reports.history(self.event_space());self.assertEqual(1,len(rows));self.assertTrue(rows[0][2]['business_events'])
        self.state=self.new_state();token,error=self.state.member_login(self.SOURCE,'123456');self.assertEqual('',error)
        self.assertEqual(r.id,self.submit(v,token).id);self.assertEqual(1,len(reports.history(self.event_space())))
    def test_unknown_occurrence_clock_stays_empty_and_report_time_is_separate(self):
        v=self.draft();v['details']['occurred_at']='';self.submit(v)
        business=next(r for r in self.state.ledger.flow_records.values() if r.request_id==v['request_id']+'-business')
        self.assertEqual('',business.times.occurred_at);self.assertEqual('',business.times.collected_at)
        self.assertTrue(business.times.recorded_at);self.assertEqual('',reports.history(self.event_space())[0][2]['envelope']['details']['occurred_at'])
    def test_wrong_identity_closed_event_lock_and_verify_are_not_replayed(self):
        v=self.draft();before=self.files()
        with self.assertRaises(PermissionError):self.submit(v,self.tokens[self.TARGET])
        with self.assertRaises(ValueError):self.submit(v|{'operation':'verify'})
        self.assertEqual(before,self.files());self.state.registry.credentials[self.SOURCE]=replace(self.state.registry.credentials[self.SOURCE],locked=True)
        with self.assertRaises(PermissionError):self.submit(v)
        self.state.registry.credentials[self.SOURCE]=replace(self.state.registry.credentials[self.SOURCE],locked=False)
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        with self.assertRaises(PanelActionError):self.submit(v)
        self.assertFalse(reports.history(self.event_space()))
    def test_changed_sources_conflict_keep_original_and_explicit_new_request(self):
        v=self.draft();frozen=copy.deepcopy(v);self.map();before=self.files()
        with self.assertRaisesRegex(ValueError,'来源或版本'):self.submit(v)
        self.assertEqual(frozen,v);self.assertEqual(before,self.files());self.assertFalse(reports.history(self.event_space()))
        fresh=self.draft(request_id='original-report-002');self.submit(fresh);self.assertEqual(frozen,v)
    def test_nonfinite_bool_wrong_time_and_same_id_changed_content_refused(self):
        v=self.draft();before=self.files()
        for amount in [True,'nan','inf',-1,0]:
            with self.assertRaises(ValueError):self.submit(v|{'details':v['details']|{'amount':amount}})
        with self.assertRaises(ValueError):self.submit(v|{'created_at':'2099-01-01T00:00:00+00:00'})
        self.assertEqual(before,self.files());self.submit(v);before=self.files()
        with self.assertRaises(ValueError):self.submit(v|{'details':v['details']|{'amount':2}})
        self.assertEqual(before,self.files())
    def test_business_then_receipt_save_failure_rolls_back_both_files(self):
        v=self.draft();before=self.files();real=save_workspace;calls=[]
        def failing(*args,**kwargs):
            calls.append(1);real(*args,**kwargs)
            if len(calls)==2:raise OSError('anonymous receipt save failed after write')
        with patch('panda_brain.persistence.save_workspace',side_effect=failing):
            with self.assertRaises(OSError):self.submit(v)
        self.assertEqual(2,len(calls));self.assertEqual(before,self.files());self.assertFalse(reports.history(self.event_space()));self.submit(v)
    def test_interrupt_requires_restart_then_original_retry(self):
        class Interrupted(BaseException):pass
        v=self.draft();before=self.files();real=save_workspace;calls=[]
        def failing(*args,**kwargs):
            calls.append(1);real(*args,**kwargs)
            if len(calls)==2:raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=failing):
            with self.assertRaises(Interrupted):self.submit(v)
        self.assertTrue(self.state._storage_interrupted)
        with self.assertRaises(RuntimeError):self.submit(v)
        self.state=self.new_state();self.assertEqual(before,self.files());self.assertFalse(reports.history(self.event_space()))
        token,error=self.state.member_login(self.SOURCE,'123456');self.assertEqual('',error);self.submit(v,token)
    def test_existing_offline_instruction_feedback_only_no_automatic_close(self):
        task=self.state.handle_offline_task_record(self.event_id,deputy_token=self.tokens[self.DEPUTY],content='匿名线下已下达观察',group_id=self.source_squad,
            issued_by='匿名现场指挥',recipient_id=self.SOURCE,verifier_id=self.TARGET,occurred_at='2026-10-10T09:00:00+08:00',evidence='匿名原线下指令',known_progress='',parent_id='',request_id='offline-instruction-001')
        c=reports.context(self.state,self.event_id,self.tokens[self.SOURCE]);template=next(t for t in c['templates'] if t['operation']=='offline_feedback')
        v=self.draft(operation='offline_feedback',details=template['fixed']|dict(action='received',basis='断网原始接收反馈'))
        self.submit(v)
        from panda_brain.offline_tasks import task_state
        current=task_state(self.event_space(),self.event_id)[task['task_id']];self.assertEqual('received',current.state)
        self.assertFalse(self.event_space().orders)
    def test_http_live_csrf_revision_tiles_export_and_public_shell(self):
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        def call(path,value=None,token=None):
            c=http.client.HTTPConnection(*server.server_address,timeout=10);headers={'Content-Type':'application/json'}
            if token:headers['Cookie']='panda_member='+token
            c.request('POST' if value else 'GET',path,json.dumps(value) if value else None,headers);r=c.getresponse();out=(r.status,r.read(),dict(r.getheaders()));c.close();return out
        # Cookie name is the real panel contract, not a user-provided actor field.
        from panda_brain.panel import _MEMBER_COOKIE as MEMBER_COOKIE
        def authenticated(path,value=None,token=None):
            c=http.client.HTTPConnection(*server.server_address,timeout=10);headers={'Content-Type':'application/json','Cookie':MEMBER_COOKIE+'='+(token or self.tokens[self.SOURCE])}
            c.request('POST' if value else 'GET',path,json.dumps(value) if value else None,headers);r=c.getresponse();out=(r.status,r.read(),dict(r.getheaders()));c.close();return out
        self.assertEqual(200,call('/offline')[0]);self.assertEqual(403,call('/offline/events')[0]);r=self.map();ctx=json.loads(authenticated('/offline/context?event_id='+self.event_id)[1])
        self.assertEqual(self.SOURCE,ctx['actor_id']);self.assertEqual('no-store',authenticated('/offline/context?event_id='+self.event_id)[2]['Cache-Control'])
        url=ctx['map']['urls'][0];self.assertEqual(403,call(url)[0]);self.assertEqual(PNG,authenticated(url)[1]);self.assertEqual(403,authenticated('/offline/map-export?event_id='+self.event_id)[0])
        v=self.draft();before=self.files();self.assertEqual(403,authenticated('/offline/reports',{'csrf':'wrong','envelope':v})[0]);self.assertEqual(before,self.files())
        self.assertEqual(200,authenticated('/offline/reports',{'csrf':ctx['csrf'],'envelope':v})[0]);before=self.files();self.assertEqual(200,authenticated('/offline/reports',{'csrf':ctx['csrf'],'envelope':v})[0]);self.assertEqual(before,self.files())
    def test_http_named_map_import_export_and_no_automatic_directory_tiles(self):
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        token=self.tokens[self.DEPUTY];csrf=self.state.member_session(token).custody_csrf
        def call(path,value=None,cookie=True):
            c=http.client.HTTPConnection(*server.server_address,timeout=10);headers={'Content-Type':'application/json'}
            if cookie:headers['Cookie']='member_session='+token
            c.request('POST' if value else 'GET',path,json.dumps(value) if value else None,headers);r=c.getresponse();out=r.status,r.read();c.close();return out
        self.assertEqual(404,call('/offlinemap/0/0/0.png')[0])
        encoded=package(manifest={'export_allowed':True});value=dict(csrf=csrf,event_id=self.event_id,operation='import',basis='匿名缓存与导出许可报告',archive_base64=encoded,expected_version=0,request_id='http-map-import-001')
        self.assertEqual(200,call('/offline/map',value)[0]);self.assertEqual(base64.b64decode(encoded),call('/offline/map-export?event_id='+self.event_id)[1]);self.assertEqual(200,call('/offline/map',value)[0])
        self.assertEqual(403,call('/offline/map-export?event_id='+self.event_id,cookie=False)[0]);self.assertEqual(409,call('/offline/map',value|dict(request_id='http-map-import-002',unexpected='越界'))[0])

class P16FormalTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','space','status','plan','attach','review','verify','publish','act','receive','inventory','loss','begin'):
        locals()[name]=getattr(formal.FeedbackTests,name)
    def setUp(self):formal.FeedbackTests.setUp(self);self.begin()
    def test_formal_current_executor_report_does_not_verify_or_publish(self):
        c=reports.context(self.state,self.event_id,self.tokens[self.SOURCE]);t=next(t for t in c['templates'] if t['operation']=='formal_feedback')
        v=dict(schema_version=1,event_id=self.event_id,actor_id=self.SOURCE,request_id='formal-offline-001',created_at=stamp(),context_digest=c['context_digest'],operation='formal_feedback',details=t['fixed']|dict(action='progress',basis='断线时原实际进展'))
        r=self.state.handle_terminal_report(self.event_id,member_token=self.tokens[self.SOURCE],envelope=v);self.assertEqual(v,json.loads(r.content_json)['envelope'])
        self.assertEqual('running',self.space().command_flows[self.ref['order_id']].stage);before=self.files()
        self.assertEqual(r.id,self.state.handle_terminal_report(self.event_id,member_token=self.tokens[self.SOURCE],envelope=v).id);self.assertEqual(before,self.files())
        with self.assertRaises(ValueError):self.state.handle_terminal_report(self.event_id,member_token=self.tokens[self.SOURCE],envelope=v|{'request_id':'formal-offline-002','details':v['details']|{'action':'verify'}})
