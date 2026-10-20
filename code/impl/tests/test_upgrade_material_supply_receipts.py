"""Actual supply receipts conserve quantities through existing event flows."""
from dataclasses import asdict,replace
import http.client,json,threading,unittest
from unittest.mock import patch
from urllib.parse import urlencode
from panda_brain import material_supply_receipts as receipts
from panda_brain.material_inventory_reads import read_event
from panda_brain.material_transfers import transfer_balance
from panda_brain.materials import MaterialsLedger
from panda_brain.panel import render_page,make_server
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_supply_execution as fixture


class Interrupted(BaseException):pass


class SupplyReceiptTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','space','status','plan','attach','review','verify','publish','act'):
        locals()[name]=getattr(fixture.SupplyExecutionTests,name)

    def setUp(self):
        fixture.SupplyExecutionTests.setUp(self);self.verify();self.publication=self.publish();self.act(quantity=4)
        self.order_id=next(iter(self.status()[2]))
        self.baseline={sid:asdict(value) for sid,value in self.state.ledger.snapshots[self.event_id].squads.items()}

    def receive(self,**changes):
        return self.state.handle_material_supply_receipt(self.event_id,**(dict(member_token=self.tokens[self.LOGISTICS],order_id=self.order_id,
            squad_id=self.target_squad,location='模拟北段保管点',quantity=2,damaged_quantity=0,basis='模拟实收清点单',expected_version=0,request_id='supply-receive-1')|changes))

    def inventory(self,item_id=None):return read_event(self.space(),self.state.ledger,self.event_id)[item_id or self.item_id]

    def loss(self,**changes):
        return self.state.handle_manual_material_loss(self.event_id,**(dict(captain_token=self.tokens[self.TARGET],squad_id=self.target_squad,item_id=self.item_id,
            loss_id='supply-loss',kind='consumed',amount='1',expected_version='0',evidence='模拟使用记录',occurred_at='',reason='',request_id='supply-loss-1')|changes))

    def test_dispatch_in_transit_partial_receipt_damage_and_no_task_auto_progress(self):
        first=self.inventory();self.assertEqual((12,4,0),(first.book_available,first.supply_in_transit,first.supply_received))
        self.receive(quantity=3,damaged_quantity=1);view=self.inventory()
        self.assertEqual((14,3,1,1),(view.book_available,view.supply_received,view.supply_damaged,view.supply_in_transit))
        balance=transfer_balance(self.state.ledger,event_id=self.event_id,squad_id=self.target_squad,item_id=self.item_id)
        self.assertEqual((2,2,4),(balance.arrival_base,balance.received,balance.available))
        self.receive(quantity=1,expected_version=1,request_id='supply-receive-2');self.assertEqual(15,self.inventory().book_available)
        self.assertEqual(self.baseline,{sid:asdict(value) for sid,value in self.state.ledger.snapshots[self.event_id].squads.items()})
        flow=next(iter(self.space().command_flows.values()));self.assertEqual('confirmed_unack',flow.stage);self.assertFalse(flow.started_at or flow.closed_at)

    def test_batches_explicitly_assign_different_actual_teams_and_keep_storage_and_debt(self):
        a=self.receive(quantity=1);b=self.receive(quantity=2,squad_id=self.source_squad,location='模拟南段仓位',expected_version=1,request_id='supply-receive-2')
        self.assertEqual(self.LOGISTICS,a.actor_id);self.assertEqual('模拟南段仓位',json.loads(b.content_json)['location'])
        self.assertEqual(3,transfer_balance(self.state.ledger,event_id=self.event_id,squad_id=self.target_squad,item_id=self.item_id).available)
        self.assertEqual(12,transfer_balance(self.state.ledger,event_id=self.event_id,squad_id=self.source_squad,item_id=self.item_id).available)
        self.assertEqual(1,self.inventory().supply_in_transit)

    def test_zero_unknown_oversupply_damage_stale_wrong_team_and_actor_no_write(self):
        before=self.files()
        for changes in (dict(quantity=0),dict(quantity=None),dict(quantity=5),dict(damaged_quantity=3),dict(expected_version=True),dict(expected_version=1),dict(squad_id='unknown'),dict(location=''),dict(quantity=float('inf'))):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.receive(**changes)
        for person in (self.DEPUTY,self.SOURCE,self.TARGET,self.OTHER):
            with self.assertRaises(PermissionError):self.receive(member_token=self.tokens[person])
        self.assertEqual(before,self.files())

    def test_replay_after_later_batch_and_dispatch_freezes_original_refs_and_zero_save(self):
        first=self.receive();self.receive(expected_version=1,request_id='supply-receive-2');before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay saved')):self.assertEqual(first,self.receive())
        with self.assertRaises(ValueError):self.receive(location='another')
        self.assertEqual(before,self.files())
        self.state=self.new_state();self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456')
        self.assertEqual(16,self.inventory().book_available);self.assertEqual(2,len(receipts.history(self.space(),self.state.ledger)))

    def test_unshipped_and_withdrawn_receiver_cannot_add_field_receipt(self):
        self.plan(1,name='unshipped');self.publish();other=next(oid for oid in self.status()[2] if oid!=self.order_id)
        with self.assertRaises(ValueError):self.receive(order_id=other)
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.TARGET],squad_id=self.target_squad,evidence='模拟撤离',request_id='receiver-withdrawal')
        before=self.files()
        with self.assertRaises(ValueError):self.receive()
        self.assertEqual(before,self.files())

    def test_new_item_good_receipt_loss_transfer_and_withdrawal_keep_original_arrival(self):
        iid=self.state.ledger.upsert_item(name='模拟新增接头',unit='个').item_id
        pid=self.state.ledger.add_supply_point(name='模拟接头库',source='pending',supplies={iid:None}).point_id;self.state._save_ledger()
        digest=self.view(item_id=iid,source_id=pid).current_source_digest
        contact=self.report(item_id=iid,source_id=pid,expected_source_digest=digest,available_quantity=6,promised_delivery_at='2026-10-10T12:00:00+08:00',request_id='new-contact')
        self.plan(6,name='new-item-plan',contact=contact,item=iid);self.verify(point_id=pid,item_id=iid,expected_source_digest=digest,quantity=6,request_id='new-stock');self.publish()
        oid=next(oid for oid,value in self.status()[2].items() if value['item_id']==iid);self.act(order_id=oid,quantity=6,request_id='new-dispatch')
        self.receive(order_id=oid,quantity=3,damaged_quantity=1,request_id='new-receipt')
        self.assertEqual(2,self.inventory(iid).book_available);loss=self.loss(item_id=iid)
        self.assertIn('supply_refs',json.loads(loss.content_json)['inventory_basis'])
        self.state.handle_manual_material_transfer(self.event_id,member_token=self.tokens[self.DEPUTY],stage='approved',transfer_id='supply-transfer',request_id='transfer-approve',evidence='模拟调拨',
            item_id=iid,from_squad=self.target_squad,to_squad=self.source_squad,quantity='1')
        self.state.handle_manual_material_transfer(self.event_id,member_token=self.tokens[self.TARGET],stage='sent',transfer_id='supply-transfer',request_id='transfer-send',evidence='模拟实交',expected_version='1')
        self.state.handle_manual_material_transfer(self.event_id,member_token=self.tokens[self.SOURCE],stage='received',transfer_id='supply-transfer',request_id='transfer-receive',evidence='模拟实收',expected_version='2',quantity='1')
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.SOURCE],squad_id=self.source_squad,evidence='模拟携行撤离',request_id='source-withdrawal')
        view=self.inventory(iid);self.assertEqual((0,1,1,3,1),(view.book_available,view.consumed,view.supply_damaged,view.supply_in_transit,view.carried_out))
        self.assertNotIn(iid,self.state.ledger.snapshots[self.event_id].squads[self.target_squad].lines)
        self.assertEqual(self.baseline[self.source_squad]['lines'],asdict(self.state.ledger.snapshots[self.event_id].squads[self.source_squad])['lines'])
        self.state=self.new_state();self.assertEqual(asdict(view),asdict(self.inventory(iid)))

    def test_missing_receipt_or_dispatch_mirror_stops_inventory_loss_and_generic_forgery(self):
        receipt=self.receive();self.state.ledger.supply_receipts.pop(receipt.id)
        with self.assertRaises(ValueError):self.inventory()
        with self.assertRaises(ValueError):self.loss()
        code,page=render_page(self.state,route='/material-supply/receipts',member_token=self.tokens[self.LOGISTICS]);self.assertEqual(409,code);self.assertNotIn('<form',page)
        with self.assertRaises(PermissionError):self.state.workspace.record_object_revision(replace(receipt,reference=replace(receipt.reference,object_id='fake')),expected_version=0)

    def test_missing_dispatch_object_with_surviving_audit_refuses_receipt_projection(self):
        self.receive();dispatch=next(r for r,v in self.status()[0] if v.get('action')=='dispatch')
        self.state.workspace.object_revisions.pop(dispatch.id)
        save_workspace(self.state.workspace,self.root/'snapshots'/(self.event_id+'.db'))
        with self.assertRaises(ValueError):self.inventory()
        with self.assertRaises(ValueError):self.receive(expected_version=1,request_id='second')

    def test_later_dispatch_keeps_prior_receipt_source_cut_and_original_replay(self):
        self.plan(4,name='second-batch-plan');self.publish()
        oid=next(oid for oid in self.status()[2] if oid!=self.order_id)
        self.act(order_id=oid,quantity=2,request_id='second-order-dispatch-1')
        first=self.receive(order_id=oid,quantity=1,request_id='second-order-receipt-1')
        self.act(order_id=oid,quantity=2,expected_version=2,request_id='second-order-dispatch-2')
        self.receive(order_id=oid,quantity=3,expected_version=1,request_id='second-order-receipt-2')
        before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay saved')):
            self.assertEqual(first,self.receive(order_id=oid,quantity=1,request_id='second-order-receipt-1'))
        self.assertEqual(before,self.files());self.assertEqual(16,self.inventory().book_available)

    def test_postwrite_failure_and_interruption_recover_both_files_before_retry(self):
        before=self.files()
        def fail(space,target):save_workspace(space,target);raise OSError('anonymous receipt save failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=fail),self.assertRaises(OSError):self.receive()
        self.assertEqual(before,self.files());self.assertFalse(self.state.ledger.supply_receipts)
        def stop(space,target):save_workspace(space,target);raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=stop),self.assertRaises(Interrupted):self.receive()
        with self.assertRaises(RuntimeError):self.receive()
        self.state=self.new_state();self.assertEqual(before,self.files());self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456');self.receive()
        self.assertEqual(14,self.inventory().book_available)

    def test_named_http_receipt_rejects_duplicate_csrf_and_unknown_without_defaulting_damage(self):
        before=self.files();code,page=render_page(self.state,route='/material-supply/receipts',member_token=self.tokens[self.LOGISTICS]);self.assertEqual(200,code);self.assertIn('仍在途：4',page);self.assertEqual(before,self.files())
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(event_id=self.event_id,order_id=self.order_id,squad_id=self.target_squad,location='模拟北段',quantity='2',damaged_quantity='0',basis='模拟HTTP实收',expected_version='0',request_id='http-receipt',csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf)
        def send(values,token=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('POST','/material-supply/receipts',urlencode(values,doseq=True),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.tokens[self.LOGISTICS])});r=c.getresponse();r.read();c.close();return r.status
        self.assertEqual(403,send(fields|dict(csrf='wrong')))
        for values in (fields|dict(actor=self.DEPUTY),fields|dict(quantity=['1','2']),fields|dict(damaged_quantity=''),fields|dict(quantity='NaN')):self.assertEqual(409,send(values))
        wrong=fields|dict(csrf=self.state.member_session(self.tokens[self.TARGET]).custody_csrf);self.assertEqual(403,send(wrong,self.tokens[self.TARGET]));self.assertEqual(before,self.files());self.assertEqual(303,send(fields));self.assertEqual(14,self.inventory().book_available)
