"""Closeout facts, separate authority, absolute correction and durable recovery."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from panda_brain import material_closeout_accounts as service
from panda_brain import audited_records as ar
from panda_brain import material_catalog as catalog
from panda_brain.material_flows import MaterialFlowService
from panda_brain.record_contracts import ObjectReference
from panda_brain.panel import render_page
from . import test_upgrade_material_catalog as fixture


class CloseoutAccountsTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','grant','change'):
        locals()[name] = getattr(fixture.CatalogTests,name)

    def setUp(self):
        fixture.CatalogTests.setUp(self)
        self.state.handle_manual_material_loss(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,item_id=self.item_id,loss_id='closeout-consumed',kind='consumed',amount='3',
            expected_version='0',evidence='匿名实际消耗报告',occurred_at='2026-10-11T01:00:00+00:00',reason='已用',request_id='closeout-consumed')
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,evidence='匿名实际撤离',request_id='closeout-depart')
        self.state.registry.events[self.event_id] = replace(self.state.registry.events[self.event_id],status='closed')
        self.state._save_registry()

    def facts(self):
        return service.facts(self.event_space(),self.state.ledger,self.state.supply_event_spaces(),self.item_id,self.source_squad)

    def verify(self, **changes):
        request = changes.get('request_id','account-verify')
        old = next((r for _,r,v in ar.records(self.event_space(),service.KIND) if r.request_id==request),None)
        frozen = json.loads(old.content_json)['facts'] if old else self.facts()
        v=dict(member_token=self.tokens[self.LOGISTICS],item_id=self.item_id,squad_id=self.source_squad,
            lost=1.,carry_quantity=6.,basis='匿名逐项核实',reason='损耗与实际保管已核',expected_version=0,
            expected_facts_digest=ar.digest(frozen),request_id='account-verify')
        return self.state.handle_material_closeout_account(self.event_id,**(v|changes))

    def adjust(self, r, **changes):
        v=dict(operation='closeout',oid=r.reference.object_id,version=0,request='account-adjust',
            details=dict(event_id=self.event_id,closeout_ref=r.id))
        return self.change(**(v|changes))

    def test_confirm_then_adjust_restart_and_next_arrival_uses_residual(self):
        f=self.facts(); self.assertEqual((3.,7.),(f['consumed'],f['carry_ceiling']))
        old=self.state.ledger.to_dict()['snapshots']; r=self.verify()
        self.assertEqual(10,self.state.ledger.carry_list(self.source_squad)[0].quantity)
        self.adjust(r); self.assertEqual(6,self.state.ledger.carry_list(self.source_squad)[0].quantity)
        self.assertEqual(old,self.state.ledger.to_dict()['snapshots'])
        self.state=self.new_state(); catalog.inspect(self.state.ledger,self.state.supply_event_spaces())
        ledger=self.state.ledger
        MaterialFlowService(ledger,'anonymous-next').record_arrival(ObjectReference('anonymous-next','team_arrival',self.source_squad,1),
            arrived_phones=(self.SOURCE,),expected_version=0,actor_id=self.SOURCE,request_id='next-arrival',source='匿名下一次')
        self.assertEqual(6,ledger.snapshots['anonymous-next'].squads[self.source_squad].lines[self.item_id].qty_registered)

    def test_real_open_loan_and_borrowed_custody_stay_separate_from_carry(self):
        from .test_upgrade_material_closed_closeout import ClosedCustodyTests
        case=ClosedCustodyTests();case.setUp();self.addCleanup(case.doCleanups)
        f=service.facts(case.event_space(),case.state.ledger,case.state.supply_event_spaces(),case.item_id,case.target_squad)
        self.assertEqual(4.,f['unreturned'][0]['outstanding'])
        self.assertTrue(f['actual_custody'])
        from panda_brain.material_closeout_accounts_page import custody_text
        text=custody_text(f);self.assertIn('尚未归还 4',text);self.assertIn('保管人',text)
        ledger=case.state.materials_path.read_bytes()
        case.state.handle_material_closeout_account(case.event_id,member_token=case.tokens[case.LOGISTICS],
            item_id=case.item_id,squad_id=case.target_squad,lost=0.,carry_quantity=f['carry_ceiling'],
            basis='匿名借入未还与实际保管分列核实',reason='不冒充归还',expected_version=0,
            expected_facts_digest=ar.digest(f),request_id='real-loan-closeout')
        self.assertEqual(ledger,case.state.materials_path.read_bytes())
        self.assertEqual(4.,service.facts(case.event_space(),case.state.ledger,case.state.supply_event_spaces(),case.item_id,case.target_squad)['unreturned'][0]['outstanding'])

    def test_replay_is_zero_write_and_correction_is_absolute(self):
        r=self.verify(); a=self.adjust(r); before=self.files()
        self.assertEqual(r,self.verify()); self.assertEqual(a,self.adjust(r)); self.assertEqual(before,self.files())
        r2=self.verify(lost=2.,carry_quantity=5.,expected_version=1,request_id='account-correction',reason='追加遗失核实')
        self.adjust(r2,version=1,request='adjust-correction')
        self.assertEqual(5,self.state.ledger.carry_list(self.source_squad)[0].quantity)
        self.assertEqual(2,len(service.inspect(self.event_space(),self.state.ledger)))

    def test_superseded_verify_and_same_verified_ref_new_request_refused(self):
        r=self.verify(); self.adjust(r)
        with self.assertRaises(ValueError): self.adjust(r,version=1,request='double-adjust')
        r2=self.verify(expected_version=1,request_id='verify-new',reason='核实新版')
        with self.assertRaisesRegex(ValueError,'最新'): self.adjust(r,version=1,request='old-ref')
        self.adjust(r2,version=1,request='new-ref')

    def test_unknown_nonfinite_overcount_stale_sources_zero_write(self):
        before=self.files()
        for changes in (dict(lost=float('nan')),dict(carry_quantity=8.),dict(carry_quantity=7.,lost=1.),dict(expected_facts_digest='old'),dict(expected_version=1)):
            with self.assertRaises(ValueError): self.verify(**changes)
            self.assertEqual(before,self.files())

    def test_permissions_are_separate_and_revoke_immediate(self):
        before=self.files()
        with self.assertRaises(PermissionError): self.verify(member_token=self.tokens[self.OTHER])
        self.assertEqual(before,self.files()); r=self.verify()
        with self.assertRaises(PermissionError): self.adjust(r,token=self.tokens[self.LOGISTICS])
        # Revocation is only permitted while the granting event is writable.
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='active')
        self.state._save_registry(); self.grant(active=False,expected_version=1,request_id='account-revoke')
        with self.assertRaises(PermissionError): self.adjust(r)

    def test_no_departure_and_open_event_cannot_verify(self):
        before=self.files()
        with self.assertRaises(ValueError): self.verify(squad_id=self.target_squad)
        self.assertEqual(before,self.files())
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='active')
        self.state._save_registry()
        with self.assertRaisesRegex(ValueError,'关闭'): self.verify()

    def test_current_long_term_edits_are_not_overwritten_by_old_adjustment(self):
        r=self.verify(); self.adjust(r)
        self.change(oid='edit-carry',request='later-carry',details=dict(data_type='carry',row=dict(squad='一中队',item_name='模拟水带',quantity=4)))
        before=self.files(); self.adjust(r); self.assertEqual(before,self.files())
        self.assertEqual(4,self.state.ledger.carry_list(self.source_squad)[0].quantity)

    def test_catalog_edit_after_verification_requires_new_verification(self):
        r=self.verify()
        self.change(oid='edit-carry',request='changed-before-adjust',details=dict(data_type='carry',row=dict(squad='一中队',item_name='模拟水带',quantity=4)))
        before=self.files()
        with self.assertRaisesRegex(ValueError,'追加核实'): self.adjust(r)
        self.assertEqual(before,self.files())

    def test_page_current_facts_versions_and_no_legacy_combat_authority(self):
        self.verify()
        status,page=render_page(self.state,route='/materials/accounts',member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200,status); self.assertIn('未归还',page); self.assertIn('追加具名核实版',page)
        status,page=render_page(self.state,route='/materials/accounts',member_token=self.tokens[self.OTHER])
        self.assertEqual(200,status); self.assertIn('按当前核实版调整',page)

    def test_postwrite_failure_and_interrupt_restart_before_images(self):
        import panda_brain.persistence as persistence
        r=self.verify(); before=self.files(); save=persistence.save_workspace
        def fail(*args,**kw): save(*args,**kw); raise OSError('anonymous account postwrite')
        with patch.object(persistence,'save_workspace',side_effect=fail):
            with self.assertRaises(OSError): self.adjust(r)
        self.assertEqual(before,self.files())
        class Interrupted(BaseException): pass
        def interrupt(*args,**kw): save(*args,**kw); raise Interrupted()
        with patch.object(persistence,'save_workspace',side_effect=interrupt):
            with self.assertRaises(Interrupted): self.adjust(r)
        self.state=self.new_state(); self.tokens[self.OTHER],_=self.state.member_login(self.OTHER,'123456')
        self.assertEqual(before,self.files()); self.adjust(r)

    def test_unknown_stock_refused_and_verified_stock_adjusted_without_deduction(self):
        with self.assertRaisesRegex(ValueError,'核实该物资点'):
            service.facts(self.event_space(),self.state.ledger,self.state.supply_event_spaces(),self.item_id,self.source_squad,self.point_id)
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='active')
        self.state._save_registry()
        self.state.handle_material_supply_execution(self.event_id,member_token=self.tokens[self.LOGISTICS],action='verify_stock',
            point_id=self.point_id,item_id=self.item_id,quantity=8,basis='匿名实物盘点',expected_stock_version=0,
            expected_source_digest=self.digest,request_id='account-stock')
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        self.state._save_registry()
        f=service.facts(self.event_space(),self.state.ledger,self.state.supply_event_spaces(),self.item_id,self.source_squad,self.point_id)
        r=self.verify(point_id=self.point_id,expected_facts_digest=ar.digest(f)); stock=dict(self.state.ledger.supply_records)
        self.adjust(r); self.assertEqual(8,self.state.ledger.points[self.point_id].supplies[self.item_id])
        self.assertEqual(stock,self.state.ledger.supply_records)

    def test_report_source_deletion_fails_closed_and_keeps_current_files(self):
        r=self.verify(); self.state.ledger.withdrawal_records.clear(); self.state._save_ledger(); before=self.files()
        with self.assertRaises(ValueError): self.adjust(r)
        self.assertEqual(before,self.files())

    def test_real_http_csrf_duplicates_scope_and_explicit_adjustment(self):
        import http.client
        import threading
        from urllib.parse import urlencode
        from panda_brain.panel import make_server
        server=make_server(self.state,port=0); threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        leader=self.tokens[self.LOGISTICS]
        form=dict(csrf=self.state.member_session(leader).custody_csrf,action='verify',event_id=self.event_id,item_id=self.item_id,
            squad_id=self.source_squad,point_id='',lost='1',carry_quantity='6',basis='匿名核实',reason='实物依据',
            expected_facts_digest=ar.digest(self.facts()),expected_version='0',request_id='http-account')
        def post(v,token):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            c.request('POST','/materials/accounts',urlencode(v,doseq=True),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+token})
            result=c.getresponse(); status=result.status; result.read(); c.close(); return status
        before=self.files()
        self.assertEqual(403,post(form|dict(csrf='bad'),leader)); self.assertEqual(409,post(form|dict(expected_version=['0','0']),leader))
        self.assertEqual(409,post(form|dict(scope='command'),leader)); self.assertEqual(before,self.files())
        self.assertEqual(303,post(form,leader)); r=service.inspect(self.event_space(),self.state.ledger)[0][1]
        maint=self.tokens[self.OTHER]
        change=dict(csrf=self.state.member_session(maint).custody_csrf,action='adjust',event_id=self.event_id,closeout_ref=r.id,
            object_id=r.reference.object_id,expected_version='0',request_id='http-adjust',basis='匿名底账依据')
        self.assertEqual(303,post(change,maint)); self.assertEqual(6,self.state.ledger.carry_list(self.source_squad)[0].quantity)
