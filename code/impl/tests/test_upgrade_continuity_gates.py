"""K12 named personnel, closed legacy routes and provenance reconstruction."""
from dataclasses import replace
import base64
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from panda_brain import material_catalog as catalog
from panda_brain import personnel_management as personnel
from panda_brain import audited_records as ar
from panda_brain.panel import render_page,make_server,PanelActionError
from . import test_upgrade_material_catalog as fixture


class ContinuityGatesTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','grant','change','upload','stage'):
        locals()[name]=getattr(fixture.CatalogTests,name)
    def setUp(self):
        fixture.CatalogTests.setUp(self)
        self.token=self.state.login('p08-transfer-demo'); self.state.handle_enter_event(self.event_id)

    def manage(self,operation='import_library',details=None,**changes):
        v=dict(member_token=self.tokens[self.DEPUTY],operation=operation,
            details=details or dict(csv_text='姓名,手机号,单位\n模拟新增,13977779991,模拟单位'),
            basis='匿名副指挥长明确安排',expected_version=0,request_id='personnel-'+operation)
        return self.state.handle_personnel_management(self.event_id,**(v|changes))

    def test_deputy_library_roster_and_maintenance_are_named_audited_and_restart(self):
        r=self.manage(); self.manage('add_roster',dict(phones=['13977779991']))
        self.manage('assign_roster',dict(phone='13977779991',pos_code='log-material'))
        self.manage('review_maintenance',dict(phone='13977779991',active=True))
        before=self.files(); self.assertEqual(r,self.manage()); self.assertEqual(before,self.files())
        self.assertEqual(4,len(personnel.inspect(self.event_space())))
        self.state=self.new_state(); self.assertIn('13977779991',self.state.registry.maintenance_phones)
        self.assertEqual('log-material',self.state.registry.rosters[self.event_id].entries['13977779991'].pos_code)

    def test_named_state_keeps_scope_and_effective_order_requirements(self):
        self.manage('add_roster',dict(phones=[self.OTHER]))
        self.manage('mark_state',dict(phone=self.OTHER,action='lost_contact',order_id='',note='人工报告'))
        self.assertEqual(self.DEPUTY,self.event_space().events[-2].payload['operator'])
        before=self.files()
        with self.assertRaises(ValueError): self.manage('mark_state',dict(phone=self.OTHER,action='executing',order_id='missing',note=''),expected_version=1,request_id='bad-state')
        self.assertEqual(before,self.files())

    def test_named_squad_captain_preserves_frozen_callout_and_three_file_recovery(self):
        details=dict(squad_id='',name='模拟新增中队',member_phones=[self.SOURCE],leader_phone=self.SOURCE,headcount_planned=1,contact='模拟联络')
        r=self.manage('squad',details); sid=next(iter(json.loads(r.content_json)['after']))
        self.assertEqual(self.SOURCE,self.state.ledger.squads[sid].leader_phone)
        before=self.files(); self.assertEqual(r,self.manage('squad',details)); self.assertEqual(before,self.files())
        selected=next(iter(__import__('panda_brain.callouts',fromlist=['callout_state']).callout_state(self.event_space(),self.event_id)))
        old=self.state.ledger.squads[selected]
        with self.assertRaises(ValueError):
            self.manage('squad',details|dict(squad_id=selected,member_phones=[self.TARGET],leader_phone=self.TARGET),expected_version=1,request_id='replace-frozen')
        self.assertEqual(before,self.files()); self.assertEqual(old,self.state.ledger.squads[selected])
        import panda_brain.persistence as persistence
        save=persistence.save_workspace
        def fail(*args,**kw): save(*args,**kw); raise OSError('anonymous three-file postwrite')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError): self.manage('squad',details|dict(name='模拟第二队'),expected_version=1,request_id='three-file')
        self.assertEqual(before,self.files())

    def test_old_general_personnel_and_all_other_roles_rejected(self):
        before=self.files()
        for call in (lambda:self.state.handle_checkin('log-material',self.OTHER),lambda:self.state.handle_roster_add(self.event_id,[self.OTHER]),
            lambda:self.state.handle_import_member_library('姓名,手机号\n模拟旁路,13977779992'),lambda:self.state.handle_maintenance_grant(self.OTHER),
            lambda:self.state.handle_person_state_mark(self.event_id,'lost_contact',phone=self.OTHER)):
            with self.assertRaises(PermissionError): call()
            self.assertEqual(before,self.files())
        for who in (self.LOGISTICS,self.OTHER,self.SOURCE,self.TARGET,self.DUTY):
            with self.assertRaises(PermissionError): self.manage(member_token=self.tokens[who])
        self.assertEqual(before,self.files())

    def test_closed_legacy_actions_and_named_personnel_zero_write_context_switch_allowed(self):
        self.state.handle_event_action(self.event_id,'close'); before=self.files()
        for call in (lambda:self.state.handle_plan_decision('missing','adopt'),lambda:self.state.handle_checkin('log-material',self.OTHER),
            lambda:self.state.handle_person_state_mark(self.event_id,'lost_contact',phone=self.OTHER),lambda:self.state.handle_sand_goal('目标'),lambda:self.manage()):
            with self.assertRaises((PanelActionError,PermissionError)): call()
            self.assertEqual(before,self.files())
        self.state.handle_enter_event(self.event_id); self.assertEqual('closed',self.state.registry.events[self.event_id].status)
        self.assertEqual(before,self.files())

    def test_personnel_postwrite_failure_and_interruption_recover_both_files(self):
        import panda_brain.persistence as persistence
        before=self.files(); save=persistence.save_workspace
        def fail(*args,**kw): save(*args,**kw); raise OSError('anonymous personnel postwrite')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError): self.manage()
        self.assertEqual(before,self.files())
        class Interrupted(BaseException): pass
        def interrupted(*args,**kw): save(*args,**kw); raise Interrupted()
        with patch.object(persistence,'save_workspace',side_effect=interrupted):
            with self.assertRaises(Interrupted): self.manage()
        self.state=self.new_state(); self.tokens[self.DEPUTY],_=self.state.member_login(self.DEPUTY,'123456')
        self.assertEqual(before,self.files()); self.manage()

    def test_historical_authority_requires_latest_at_write_not_just_any_old_grant(self):
        first=self.change(); self.grant(active=False,expected_version=1,request_id='hist-revoke')
        self.grant(active=True,expected_version=2,request_id='hist-regrant')
        second=self.change(version=1,request='hist-edit',details=dict(data_type='item',row=dict(name='模拟另一物资',unit='盘')))
        rows=__import__('panda_brain.material_maintenance_authority',fromlist=['inspect']).inspect(self.state.ledger,self.state.supply_event_spaces())
        latest=__import__('panda_brain.material_maintenance_authority',fromlist=['at_revision']).at_revision(rows,self.state.supply_event_spaces(),second)
        self.assertEqual(3,json.loads(latest.content_json)['grant_version'])
        first_grant=__import__('panda_brain.material_maintenance_authority',fromlist=['at_revision']).at_revision(rows,self.state.supply_event_spaces(),first)
        self.assertEqual(1,json.loads(first_grant.content_json)['grant_version'])
        self.assertEqual(2,len(catalog.inspect(self.state.ledger,self.state.supply_event_spaces())))

    def test_original_document_mapping_rows_and_subsequent_versions_are_reconstructed(self):
        r=self.upload('item','物资名称,单位,数量\n匿名来源物资,盘,2'.encode()); self.stage('draft',1); self.stage('publish',2)
        self.assertEqual(3,len(catalog.inspect(self.state.ledger,self.state.supply_event_spaces())))
        # Rebuild audit mirrors as a corruption fixture, never as a recovery technique.
        from panda_brain.workspace import event_hash,GENESIS_HASH
        space=self.state.workspace; final=next(r for r,v in catalog.inspect(self.state.ledger,self.state.supply_event_spaces()) if v['operation']=='publish')
        v=json.loads(final.content_json); v['details']['rows'][0]['quantity']='9'
        changed=replace(final,content_json=ar.dump(v)); space.object_revisions[final.id]=changed; self.state.ledger.catalog_records[final.id]=changed
        events=[]; previous=GENESIS_HASH
        for entry in space.events:
            payload=entry.payload
            if entry.kind=='ObjectRevisionRecorded' and payload.get('revision_id')==final.id:
                from dataclasses import asdict
                payload=dict(revision_id=final.id,revision=asdict(changed))
            value=replace(entry,payload=payload,prev_hash=previous)
            value=replace(value,hash=event_hash(value.sequence,value.kind,value.payload,value.prev_hash,value.recorded_at))
            events.append(value); previous=value.hash
        space._events=events
        with self.assertRaisesRegex(ValueError,'逐行来源'): catalog.inspect(self.state.ledger,self.state.supply_event_spaces(override=space))

    def test_actual_named_page_http_csrf_wrong_role_duplicate_and_success(self):
        status,page=render_page(self.state,'/personnel/manage',member_token=self.tokens[self.DEPUTY]); self.assertEqual(200,status); self.assertIn('维护长期人员库',page)
        server=make_server(self.state,port=0); threading.Thread(target=server.serve_forever,daemon=True).start(); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        token=self.tokens[self.DEPUTY]; form=dict(csrf=self.state.member_session(token).custody_csrf,event_id=self.event_id,operation='import_library',expected_version='0',request_id='http-personnel',basis='匿名本人安排',csv_text='姓名,手机号\n模拟HTTP人员,13977779990')
        def post(v,use=token):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5); c.request('POST','/personnel/manage',urlencode(v,doseq=True),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+use})
            r=c.getresponse(); status=r.status; r.read(); c.close(); return status
        before=self.files(); self.assertEqual(403,post(form|dict(csrf='bad'))); self.assertEqual(409,post(form|dict(expected_version=['0','0']))); self.assertEqual(before,self.files())
        self.assertEqual(303,post(form)); self.assertIn('13977779990',self.state.registry.library)
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        c.request('POST','/events/'+self.event_id+'/roster/add',urlencode(dict(phones=self.OTHER)),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'panda_session='+self.token})
        response=c.getresponse(); self.assertIn(response.status,{303,403}); response.read(); c.close()
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        c.request('POST','/login/member',urlencode(dict(phone=self.OTHER,password='123456')),{'Content-Type':'application/x-www-form-urlencoded'})
        response=c.getresponse();self.assertEqual(303,response.status);self.assertEqual('/materials/records',response.getheader('Location'));response.read();c.close()
