"""Persistent named logistics grants, immediate revocation and write isolation."""
import http.client,json,threading,unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlencode
from . import test_upgrade_material_supply_contacts as fixture
from panda_brain import material_maintenance_authority as authority
from panda_brain.panel import render_page,make_server
from panda_brain.persistence import save_workspace


class MaintenanceAuthorityTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files'):
        locals()[name]=getattr(fixture.SupplyContactTests,name)
    def setUp(self):fixture.SupplyContactTests.setUp(self)
    def grant(self,**changes):
        return self.state.handle_material_maintenance_authority(self.event_id,**(dict(member_token=self.tokens[self.LOGISTICS],subject_id=self.OTHER,active=True,
            basis='匿名长期物资维护指定依据',reason='匿名由物资供应组长授予',expected_version=0,request_id='maintenance-grant-1')|changes))
    def space(self):return self.state._appointment_space(self.event_id)[0]
    def test_explicit_grant_revoke_regrant_restart_and_cross_event_scope(self):
        before=self.files()
        with self.assertRaises(PermissionError):self.state.require_material_maintenance(self.tokens[self.OTHER])
        self.assertEqual(before,self.files());first=self.grant();self.assertEqual(first,self.state.require_material_maintenance(self.tokens[self.OTHER]))
        self.state=self.new_state();self.tokens[self.OTHER],_=self.state.member_login(self.OTHER,'123456');self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456')
        self.assertEqual(first,self.state.require_material_maintenance(self.tokens[self.OTHER]))
        self.grant(active=False,expected_version=1,request_id='maintenance-revoke');before=self.files()
        with self.assertRaises(PermissionError):self.state.handle_material_carry(squad_id=self.source_squad,item_id=self.item_id,quantity='2',member_token=self.tokens[self.OTHER])
        self.assertEqual(before,self.files());self.grant(expected_version=2,request_id='maintenance-regrant')
        # Long-term scope does not require an active source event. Historical authority remains audited.
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed');self.state._save_registry()
        self.assertEqual(3,json.loads(self.state.require_material_maintenance(self.tokens[self.OTHER]).content_json)['grant_version'])
    def test_bad_actor_scope_subject_version_and_unchanged_status_never_write(self):
        before=self.files()
        for actor in (self.DEPUTY,self.DUTY,self.SOURCE,self.OTHER):
            with self.assertRaises(PermissionError):self.grant(member_token=self.tokens[actor])
        for values in (dict(subject_id='unknown'),dict(expected_version=True),dict(expected_version=1),dict(active='true'),dict(basis=''),dict(active=False)):
            with self.assertRaises(ValueError):self.grant(**values)
        self.assertEqual(before,self.files());self.grant();before=self.files()
        with self.assertRaises(ValueError):self.grant(expected_version=1,request_id='same-status')
        self.assertEqual(before,self.files())
    def test_other_event_leader_revoke_uses_global_subject_version(self):
        from panda_brain.models import PersonRecord
        first=self.grant();old_event=self.event_id
        event=self.state.registry.create_event(name='匿名第二维护事件',report_no='K08-SECOND',discovered_at='2026-10-10T08:00:00+08:00',source='匿名模拟',location='模拟区')
        self.event_id=event.event_id;self.state.handle_event_action(self.event_id,'start',duty_token=self.tokens[self.DUTY])
        self.state.handle_deputy_appointment(self.event_id,duty_token=self.tokens[self.DUTY],subject_id=self.DEPUTY,evidence='匿名第二事件任命',request_id='maint-deputy-second')
        self.LOGISTICS='13977773108';self.state.registry.import_person_library((PersonRecord(name='匿名第二后勤',phone=self.LOGISTICS,unit='模拟物资组'),))
        self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456');qr=next(q.qr_id for q in self.state.registry.qrs.values() if q.event_id==self.event_id)
        self.state.handle_temporary_scan(self.event_id,member_token=self.tokens[self.LOGISTICS],qr_id=qr)
        self.state.handle_temporary_assign(self.event_id,deputy_token=self.tokens[self.DEPUTY],phones=[self.LOGISTICS],pos_code='log-material',request_id='maint-log-second')
        self.state.handle_temporary_arrival(self.event_id,member_token=self.tokens[self.LOGISTICS]);self.appoint(self.LOGISTICS)
        with self.assertRaises(ValueError):self.grant(active=False,request_id='stale-other-event')
        second=self.grant(active=False,expected_version=1,request_id='revoke-other-event')
        self.assertEqual(1,second.reference.version);self.assertEqual(2,json.loads(second.content_json)['grant_version'])
        self.assertEqual(old_event,first.reference.event_id)
        with self.assertRaises(PermissionError):self.state.require_material_maintenance(self.tokens[self.OTHER])
    def test_replay_after_revoke_zero_save_conflict_and_source_missing_stops_writes(self):
        first=self.grant();self.grant(active=False,expected_version=1,request_id='revoke');before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay wrote')):
            self.assertEqual(first,self.grant())
        with self.assertRaises(ValueError):self.grant(reason='different')
        self.assertEqual(before,self.files());self.state.ledger.maintenance_records.pop(first.id);self.state.ledger.save(self.state.materials_path)
        with self.assertRaises(ValueError):self.state.require_material_maintenance(self.tokens[self.OTHER])
        with self.assertRaises(ValueError):self.grant(expected_version=2,request_id='broken-history')
    def test_missing_event_mirror_and_generic_forgery_fail_closed(self):
        first=self.grant();space=self.space();space.object_revisions.pop(first.id);save_workspace(space,self.root/'snapshots'/f'{self.event_id}.db')
        before=self.files()
        with self.assertRaises(ValueError):self.state.require_material_maintenance(self.tokens[self.OTHER])
        self.assertEqual(409,render_page(self.state,route='/materials/maintenance-authority',member_token=self.tokens[self.LOGISTICS])[0])
        with self.assertRaises(PermissionError):space.record_object_revision(first,expected_version=0)
        self.assertEqual(before,self.files())
    def test_named_long_term_write_grant_not_personnel_or_field_rights(self):
        self.state.login('p08-transfer-demo');before=self.files()
        for call in (lambda:self.state.handle_material_item(name='匿名物资'),lambda:self.state.handle_import_upload(data_type='carry',filename='a.csv',content='中队,物资,数量\n一中队,水带,2')):
            with self.assertRaises(PermissionError):call()
        self.assertEqual(before,self.files());self.grant()
        result=self.state.handle_material_catalog(member_token=self.tokens[self.OTHER],operation='edit',object_id='edit-item',expected_version=0,request_id='named-item',basis='匿名修改依据',details=dict(data_type='item',row=dict(name='匿名新物资',unit='盘')));self.assertTrue(any(i.name=='匿名新物资' for i in self.state.ledger.items.values()))
        before=self.files()
        for call in (lambda:self.state.handle_material_squad_leader(self.source_squad,self.OTHER,member_token=self.tokens[self.OTHER]),
                     lambda:self.state.handle_material_squad(name='匿名队',member_phones=self.OTHER,member_token=self.tokens[self.OTHER]),
                     lambda:self.state.handle_material_consume(event_id=self.event_id,squad_id=self.source_squad,item_id=self.item_id,amount='1'),
                     lambda:self.state.handle_material_supply_execution(self.event_id,member_token=self.tokens[self.OTHER],action='verify_stock',point_id=self.point_id,item_id=self.item_id,quantity=1,basis='匿名',expected_stock_version=0,expected_source_digest=self.digest,request_id='bad-field')):
            with self.assertRaises(PermissionError):call()
        self.assertEqual(before,self.files())
    def test_material_import_authority_rechecked_after_each_stage(self):
        import base64
        self.grant()
        values=dict(member_token=self.tokens[self.OTHER],object_id='import-revocation',basis='匿名导入依据')
        self.state.handle_material_catalog(**values,operation='upload',expected_version=0,request_id='up',details=dict(data_type='supply_point',filename='a.csv',original_base64=base64.b64encode('物资点,位置\n匿名库,匿名区'.encode()).decode()))
        self.state.handle_material_catalog(**values,operation='draft',expected_version=1,request_id='draft',details={})
        self.grant(active=False,expected_version=1,request_id='import-revoke');before=self.files()
        with self.assertRaises(PermissionError):self.state.handle_material_catalog(**values,operation='publish',expected_version=2,request_id='pub',details={})
        self.assertEqual(before,self.files())
    def test_postwrite_failure_and_interrupt_restore_grant_before_retry(self):
        import panda_brain.persistence as persistence
        before=self.files();save=persistence.save_workspace
        def fail(*a,**kw):save(*a,**kw);raise OSError('anonymous grant postwrite')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError):self.grant()
        self.assertEqual(before,self.files())
        class Interrupted(BaseException):pass
        def interrupt(*a,**kw):save(*a,**kw);raise Interrupted()
        with patch.object(persistence,'save_workspace',side_effect=interrupt):
            with self.assertRaises(Interrupted):self.grant()
        self.state=self.new_state();self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456');self.assertEqual(before,self.files());self.grant()
        self.assertEqual(1,len(authority.inspect(self.state.ledger,self.state.supply_event_spaces())))
    def test_readonly_http_csrf_duplicates_unknown_and_actual_named_grant(self):
        before=self.files();code,page=render_page(self.state,route='/materials/maintenance-authority',member_token=self.tokens[self.LOGISTICS]);self.assertEqual(200,code)
        self.assertIn('跨事件有效',page);self.assertEqual(before,self.files());self.assertEqual(404,render_page(self.state,route='/materials/maintenance-authority')[0])
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(event_id=self.event_id,subject_id=self.OTHER,active='true',basis='匿名HTTP授权依据',reason='匿名组长指定',expected_version='0',request_id='http-maintenance',csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf)
        def send(values,token=None,route='/materials/maintenance-authority'):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);conn.request('POST',route,urlencode(values,doseq=True),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.tokens[self.LOGISTICS])});resp=conn.getresponse();resp.read();conn.close();return resp.status
        self.assertEqual(403,send(fields|dict(csrf='bad')));self.assertEqual(409,send(fields|dict(expected_version=['0','0'])));self.assertEqual(409,send(fields|dict(scope='personnel')))
        wrong=self.tokens[self.DEPUTY];self.assertEqual(403,send(fields|dict(csrf=self.state.member_session(wrong).custody_csrf),wrong))
        self.assertEqual(303,send(fields));self.assertEqual(1,len(authority.inspect(self.state.ledger,self.state.supply_event_spaces())))
        member=self.tokens[self.OTHER];self.assertEqual(403,send(dict(name='匿名HTTP物资'),member,route='/materials/item'))
        self.assertEqual(403,send(dict(name='匿名HTTP物资',csrf=self.state.member_session(member).custody_csrf),member,route='/materials/item'))
