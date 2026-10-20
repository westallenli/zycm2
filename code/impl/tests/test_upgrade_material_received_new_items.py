"""Actually received items outside a team's initial carry list remain journal based."""
from dataclasses import asdict
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from panda_brain.materials import MaterialsLedger
from panda_brain.material_transfers import transfer_balance, loan_status
from panda_brain.panel import make_server, render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_received_losses as prior
from . import test_upgrade_manual_material_transfers as fixture
from .test_upgrade_material_inventory_projection import rehash


class ReceivedNewItemTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space',
                 'approve','send','receive','make_loan_received','approve_return'):
        locals()[name] = getattr(fixture.NamedTransferTests,name)
    for name in ('full','view','files','changed'):
        locals()[name] = getattr(prior.ReceivedLossTests,name)

    def setUp(self, empty=False):
        original = MaterialsLedger.set_carry_line
        def initial_carry(ledger, squad_id, item_id, **args):
            if ledger.squads[squad_id].leader_phone == self.TARGET:
                if empty:
                    return None
                other = ledger.upsert_item(name='模拟安全帽',unit='顶').item_id
                return original(ledger,squad_id,other,quantity=2)
            return original(ledger,squad_id,item_id,**args)
        # Use the real initial arrival service with this fixture's alternate list.
        with patch.object(MaterialsLedger,'set_carry_line',initial_carry):
            fixture.NamedTransferTests.setUp(self)
        self.initial_team = asdict(self.state.ledger.snapshots[self.event_id].squads[self.target_squad])
        self.initial_carry = self.state.ledger.to_dict()['carry_lines']

    def report(self, **changes):
        changes.setdefault('amount','1')
        return prior.ReceivedLossTests.report(self,**changes)

    def assert_initial_unchanged(self):
        self.assertEqual(self.initial_team,asdict(self.state.ledger.snapshots[self.event_id].squads[self.target_squad]))
        self.assertEqual(self.initial_carry,self.state.ledger.to_dict()['carry_lines'])
        self.assertNotIn(self.item_id,self.state.ledger.snapshots[self.event_id].squads[self.target_squad].lines)

    def test_catalog_master_carry_approval_and_transit_do_not_permit_loss(self):
        self.state.ledger.set_carry_line(self.target_squad,self.item_id,quantity=20)
        self.state._save_ledger()
        for stage in ('catalog-only','approved','sent'):
            if stage=='approved': self.approve()
            if stage=='sent': self.send()
            before = self.files()
            with self.subTest(stage=stage):
                for amount in ('0','1'):
                    with self.assertRaises(ValueError): self.report(amount=amount)
                self.assertEqual(before,self.files())
                html = render_page(self.state,'/captain-arrival',member_token=self.tokens[self.TARGET])[1]
                self.assertNotIn(f'<option value="{self.item_id}">',html)

    def test_partial_receipt_consumption_and_damage_preserve_initial_list(self):
        self.approve(); self.send(); self.receive()
        first = self.report()
        self.report(loss_id='new-damage',kind='damaged',amount='.5',request_id='new-damage-1')
        view = self.view()
        self.assertEqual((10,6.5,2,1.5),(view.arrival_base,view.team_available,view.in_transit,view.losses))
        self.assertEqual(3,len(json.loads(first.content_json)['inventory_basis']['transfer_refs']))
        self.assert_initial_unchanged()

    def test_empty_initial_list_exposes_only_actually_received_item(self):
        self.setUp(empty=True)
        html = render_page(self.state,'/captain-arrival',member_token=self.tokens[self.TARGET])[1]
        self.assertNotIn('记录人工报告',html)
        self.full()
        html = render_page(self.state,'/captain-arrival',member_token=self.tokens[self.TARGET])[1]
        self.assertIn(f'<option value="{self.item_id}">模拟水带</option>',html)
        self.assertIn('初次清单外的物资',html)
        self.report(amount='4')
        self.assertEqual((6,4),(self.view().team_available,self.view().losses))
        self.assert_initial_unchanged()

    def test_full_receipt_consumption_and_damage_cannot_exceed_received(self):
        self.full(); self.report(amount='3')
        self.report(loss_id='new-damage',kind='damaged',amount='1',request_id='new-damage-1')
        before = self.files()
        with self.assertRaises(ValueError): self.report(loss_id='excess',amount='.1',request_id='new-excess')
        self.assertEqual(before,self.files())
        self.assertEqual((6,4),(self.view().team_available,self.view().losses))
        self.assert_initial_unchanged()

    def test_correction_after_second_receipt_uses_difference_and_old_retry_is_zero_write(self):
        self.approve(); self.send(); self.receive()
        first = self.report(amount='2')
        self.receive(quantity='2',expected_version='3',request_id='second-receipt')
        second = self.report(amount='3',expected_version='1',reason='匿名更正',request_id='loss-second')
        self.assertEqual((7,3),(self.view().team_available,self.view().losses))
        self.assertEqual([first.id],json.loads(second.content_json)['inventory_basis']['loss_refs'])
        before = self.files()
        self.assertEqual(first,self.report(amount='2'))
        self.assertEqual(before,self.files())
        self.assert_initial_unchanged()

    def test_remaining_new_item_transfers_and_two_teams_withdraw_without_new_baseline(self):
        self.full(); self.report(amount='3')
        self.approve(transfer_id='remaining',request_id='remaining-a',from_squad=self.target_squad,
                     to_squad=self.source_squad,quantity='1')
        self.send(transfer_id='remaining',request_id='remaining-s',member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='remaining',request_id='remaining-r',member_token=self.tokens[self.SOURCE],quantity='1')
        for squad,captain in ((self.target_squad,self.TARGET),(self.source_squad,self.SOURCE)):
            self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[captain],
                squad_id=squad,exceptions=(),evidence='匿名撤离清单',request_id='new-withdraw-'+squad)
        self.assertEqual((10,0,7,3),(self.view().arrival_base,self.view().team_available,self.view().carried_out,self.view().losses))
        self.assert_initial_unchanged()
        before = self.files()
        with self.assertRaises(ValueError): self.report(loss_id='after',request_id='after-withdraw')
        self.assertEqual(before,self.files())

    def test_new_item_loan_consumption_does_not_clear_borrowing_or_allow_departure(self):
        self.make_loan_received(); self.report()
        self.assertEqual(4,loan_status(self.state.ledger,event_id=self.event_id,loan_transfer_id='loan-1').outstanding)
        self.assertEqual(3,transfer_balance(self.state.ledger,event_id=self.event_id,squad_id=self.target_squad,item_id=self.item_id).available)
        before = self.files()
        with self.assertRaises(ValueError):
            self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.TARGET],
                squad_id=self.target_squad,evidence='未结借用',request_id='blocked-new-withdraw')
        self.assertEqual(before,self.files())

    def test_return_damage_does_not_restore_capacity_without_arrival_row(self):
        self.full()
        self.approve(transfer_id='loan-back',request_id='loan-back-a',purpose='loan',
                     from_squad=self.target_squad,to_squad=self.source_squad,quantity='4')
        self.send(transfer_id='loan-back',request_id='loan-back-s',member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='loan-back',request_id='loan-back-r',member_token=self.tokens[self.SOURCE],quantity='4')
        self.approve(transfer_id='return-back',request_id='return-back-a',purpose='return',return_for='loan-back',quantity='4')
        self.send(transfer_id='return-back',request_id='return-back-s')
        self.receive(transfer_id='return-back',request_id='return-back-r',quantity='4',damaged_quantity='4')
        before = self.files()
        with self.assertRaises(ValueError): self.report()
        self.assertEqual(before,self.files())
        self.assertEqual((6,4),(self.view().team_available,self.view().return_damage))

    def test_missing_receipt_mirror_hides_choice_and_refuses_loss(self):
        self.full()
        space = self.event_space()
        space._events = [e for e in space.events if e.kind!='MaterialTransferStageReported']
        rehash(space)
        save_workspace(space,Path(self.state.registry.events[self.event_id].snapshot_path))
        before = self.files()
        html = render_page(self.state,'/captain-arrival',member_token=self.tokens[self.TARGET])[1]
        self.assertIn('物资数量来源待核对',html)
        self.assertNotIn('记录人工报告',html)
        with self.assertRaises(ValueError): self.report()
        self.assertEqual(before,self.files())

    def test_future_receipt_and_legacy_without_basis_do_not_back_new_item_loss(self):
        self.approve(); self.send(); self.receive()
        first = self.report()
        future = self.receive(quantity='2',expected_version='3',request_id='future')
        ledger,space = self.changed(first,lambda c:c['inventory_basis'].update(transfer_refs=sorted(c['inventory_basis']['transfer_refs']+[future.id])))
        with self.assertRaises(ValueError): self.view(space,ledger)
        ledger,space = self.changed(first,lambda c:c.pop('inventory_basis'))
        with self.assertRaises(ValueError): self.view(space,ledger)

    def test_deleted_receipt_source_hides_correction_and_refuses_new_write(self):
        self.full(); self.report()
        self.state.ledger.transfer_records = {key:r for key,r in self.state.ledger.transfer_records.items()
                                              if r.reference.version < 3}
        self.state.ledger.save(self.state.materials_path)
        before = self.files()
        html = render_page(self.state,'/captain-arrival',member_token=self.tokens[self.TARGET])[1]
        self.assertIn('物资数量来源待核对',html)
        self.assertNotIn('更正本条报告',html)
        with self.assertRaises(ValueError): self.report(loss_id='after-deletion',request_id='after-deletion')
        self.assertEqual(before,self.files())
        with self.assertRaises(ValueError): MaterialsLedger.load(self.state.materials_path)

    def test_restart_retains_journal_losses_and_frozen_sources(self):
        self.full(); first = self.report(amount='3')
        view = self.view()
        self.state = self.new_state()
        self.assertEqual(view,self.view())
        self.tokens[self.TARGET],_ = self.state.member_login(self.TARGET,'123456')
        before = self.files()
        self.assertEqual(first,self.report(amount='3'))
        self.assertEqual(before,self.files())
        self.assert_initial_unchanged()

    def test_postwrite_failure_restores_new_item_history_and_original_baseline(self):
        self.full(); before = self.files()
        def fail(space,target):
            save_workspace(space,target)
            raise OSError('anonymous postwrite failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=fail):
            with self.assertRaises(OSError): self.report(amount='3')
        self.assertEqual(before,self.files())
        self.assertEqual(10,self.view().team_available)
        self.report(amount='3')
        self.assertEqual(7,self.view().team_available)
        self.assert_initial_unchanged()

    def test_interruption_restart_recovers_without_adding_snapshot_row(self):
        self.full(); before = self.files()
        def stop(space,target):
            save_workspace(space,target)
            raise prior.Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=stop):
            with self.assertRaises(prior.Interrupted): self.report(amount='3')
        self.state = self.new_state()
        self.assertEqual(before,self.files())
        self.assertFalse(self.state.member_sessions)
        self.tokens[self.TARGET],_ = self.state.member_login(self.TARGET,'123456')
        self.report(amount='3')
        self.assertEqual(7,self.view().team_available)
        self.assert_initial_unchanged()

    def test_existing_http_route_accepts_new_item_and_keeps_authority_quantity_gates(self):
        self.full()
        server = make_server(self.state,port=0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(thread.join); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(type('NoRedirect',(urllib.request.HTTPRedirectHandler,),
            {'redirect_request':lambda self,*args,**kwargs:None})())
        form = dict(event_id=self.event_id,squad_id=self.target_squad,item_id=self.item_id,
            loss_id='new-http',kind='consumed',amount='3',expected_version='0',
            evidence='匿名新物资耗损',occurred_at='',reason='',request_id='new-http-1')
        def post(token):
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/captain-arrival/material-loss',
                data=urllib.parse.urlencode(form).encode(),headers={'Cookie':f'member_session={token}'})
            try:
                with opener.open(request) as response: return response.status
            except urllib.error.HTTPError as error:
                code=error.code; error.close(); return code
        self.assertEqual(403,post(self.tokens[self.OTHER]))
        self.assertEqual(303,post(self.tokens[self.TARGET])); self.assertEqual(303,post(self.tokens[self.TARGET]))
        self.assertEqual((7,3),(self.view().team_available,self.view().losses))
        form.update(loss_id='excess-http',amount='2',request_id='excess-http')
        before=self.files()
        self.assertEqual(409,post(self.tokens[self.TARGET]))
        self.assertEqual(before,self.files())
        self.assert_initial_unchanged()
