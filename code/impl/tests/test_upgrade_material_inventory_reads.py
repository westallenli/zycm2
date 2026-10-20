"""Current read surfaces share audited quantities and fail closed together."""
import copy
from dataclasses import replace
import json
import unittest

from panda_brain import sandtable
from panda_brain.grounded_inquiry import validate_sources
from panda_brain.handover_sop import _materials
from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.material_inventory_reads import read_event, citation
from panda_brain.material_withdrawals import WithdrawalException
from panda_brain.models import ExecutionOrder
from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_received_new_items as new_items
from .test_upgrade_material_inventory_projection import rehash


class InventoryReadTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space',
                 'approve','send','receive','make_loan_received','approve_return',
                 'setUp','full','report','files'):
        locals()[name] = getattr(new_items.ReceivedNewItemTests, name)

    def activate(self):
        self.commander = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.orders['read-order'] = ExecutionOrder('read-order', 'decision', 'option', '匿名待核任务')

    def view(self):
        return read_event(self.state.workspace, self.state.ledger, self.event_id)[self.item_id]

    def rows(self, demand=9):
        return self.state.ledger.compute_gap(self.event_id, {self.item_id:demand},
                                             workspace=self.state.workspace)

    def answer(self):
        return sandtable.answer_inquiry('物资', self.state.workspace,
                                       ledger=self.state.ledger, event_id=self.event_id)

    def page(self):
        return render_page(self.state, '/materials', session_token=self.commander,
                           query={'gap_order':['read-order'],'gap_item':[self.item_id]})[1]

    def test_approval_transit_partial_receipt_have_same_read_balance(self):
        self.activate()
        for stage, available, transit in (('arrival',10,0),('approval',10,0),('sent',6,4),('partial',8,2)):
            if stage=='approval': self.approve()
            if stage=='sent': self.send()
            if stage=='partial': self.receive()
            before = self.files()
            view = self.view()
            row = next(r for r in self.rows() if r['item_id']==self.item_id)
            assessment = assess_gap_inputs(self.state.workspace,self.state.ledger,
                event_id=self.event_id,item_id=self.item_id,order_id='read-order')
            with self.subTest(stage=stage):
                self.assertEqual((available,transit),(view.book_available,view.in_transit))
                self.assertEqual(available,row['available'])
                self.assertEqual(available,assessment.book_available)
                self.assertIn(f'当前账面可用 {available:g}',self.answer().conclusion)
                self.assertIn(f'当前账面可用 {available:g}',str(_materials(self.state.workspace,self.event_id,self.state.ledger)))
                self.assertEqual(before,self.files())

    def test_new_item_losses_and_correction_are_visible_in_all_readers(self):
        self.full(); self.report(amount='3'); self.activate()
        for amount, version, expected in (('3','1',7),('2','2',8)):
            if version=='2': self.report(amount=amount,expected_version='1',reason='模拟更正',request_id='read-correction')
            before = self.files()
            view = self.view()
            row = next(r for r in self.rows() if r['item_id']==self.item_id)
            self.assertEqual((10,expected,float(amount)),(view.arrival_base,row['available'],row['consumed']))
            self.assertIn(f'消耗 {amount}',self.answer().conclusion)
            self.assertIn(f'账面可用 {expected}',self.page())
            self.assertIn(f'当前账面可用 {expected}',str(_materials(self.state.workspace,self.event_id,self.state.ledger)))
            self.assertNotIn(self.item_id,self.state.ledger.snapshots[self.event_id].squads[self.target_squad].lines)
            self.assertEqual(before,self.files())

    def test_new_item_damage_and_consumption_are_separate(self):
        self.full(); self.report(amount='1')
        self.report(loss_id='damage',kind='damaged',amount='2',request_id='damage-1')
        self.activate()
        row = next(r for r in self.rows() if r['item_id']==self.item_id)
        self.assertEqual((7,1,2),(row['available'],row['consumed'],row['damaged']))
        self.assertIn('消耗 1，损坏 2',self.answer().conclusion)

    def test_departures_show_zero_available_without_restoring_losses(self):
        self.full(); self.report(amount='4')
        for phone, squad in ((self.SOURCE,self.source_squad),(self.TARGET,self.target_squad)):
            self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[phone],
                squad_id=squad,exceptions=(),evidence='匿名撤离',request_id='depart-'+squad)
        self.activate()
        row = next(r for r in self.rows(1) if r['item_id']==self.item_id)
        self.assertEqual((0,1,'已影响执行'),(row['available'],row['gap'],row['severity']))
        self.assertEqual((4,6),(self.view().losses,self.view().carried_out))
        self.assertIn('当前账面可用 0',self.answer().conclusion)

    def test_retained_custody_makes_total_unknown_not_zero(self):
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,exceptions=(WithdrawalException(self.item_id,2,'retained','模拟位置','模拟保管'),),
            evidence='匿名留置',request_id='retain-1')
        self.activate()
        row = next(r for r in self.rows(0) if r['item_id']==self.item_id)
        self.assertIsNone(row['available']); self.assertIsNone(row['gap'])
        self.assertEqual('待核对',row['severity'])
        self.assertIn('当前账面可用 待核对',self.answer().conclusion)
        self.assertIn('留置 2',str(_materials(self.state.workspace,self.event_id,self.state.ledger)))

    def assert_broken_readers(self):
        save_workspace(self.state.workspace, self.state.registry.events[self.event_id].snapshot_path)
        before = self.files()
        with self.assertRaises(ValueError): self.rows()
        assessment = assess_gap_inputs(self.state.workspace,self.state.ledger,
            event_id=self.event_id,item_id=self.item_id,order_id='read-order')
        self.assertIsNone(assessment.book_available)
        self.assertIn('当前物资流水来源待核对',self.answer().conclusion)
        self.assertFalse(any(s.startswith('inventory:') for s in self.answer().sources))
        self.assertEqual((),_materials(self.state.workspace,self.event_id,self.state.ledger)[0])
        self.assertIn('当前数量暂不展示',self.page())
        self.assertEqual(before,self.files())

    def test_deleted_receipt_mirror_fails_all_readers_even_after_rehash(self):
        self.full(); self.activate()
        last = next(r for r in self.state.ledger.transfer_records.values() if r.reference.version==3)
        self.state.workspace._events = [e for e in self.state.workspace.events if e.payload.get('transfer_record_id')!=last.id]
        rehash(self.state.workspace)
        self.assert_broken_readers()

    def test_deleted_receipt_record_keeps_orphan_mirror_and_fails_all_readers(self):
        self.full(); self.activate()
        last = next(r for r in self.state.ledger.transfer_records.values() if r.reference.version==3)
        self.state.ledger.transfer_records.pop(last.id)
        self.assert_broken_readers()

    def test_arrival_mirror_is_required_before_current_quantities(self):
        self.activate()
        from panda_brain.captain_arrivals import ARRIVAL_REPORTED
        self.state.workspace._events = [e for e in self.state.workspace.events if e.kind!=ARRIVAL_REPORTED]
        rehash(self.state.workspace)
        self.assert_broken_readers()

    def test_stale_inventory_citation_is_rejected_after_quantity_change(self):
        self.full(); self.report(); self.activate()
        answer = self.answer()
        validate_sources(answer,self.state.workspace,self.state.ledger,self.event_id,'commander')
        self.report(amount='2',expected_version='1',reason='模拟更正',request_id='citation-correction')
        with self.assertRaises(ValueError):
            validate_sources(answer,self.state.workspace,self.state.ledger,self.event_id,'commander')
        self.assertNotEqual(answer.sources,self.answer().sources)

    def test_other_events_and_catalog_unknowns_never_supply_current_balance(self):
        self.full(); self.activate()
        baseline = citation(read_event(self.state.workspace,self.state.ledger,self.event_id),self.state.ledger,self.event_id)
        first = next(iter(self.state.ledger.transfer_records.values()))
        other = replace(first,reference=replace(first.reference,event_id='other'))
        object.__setattr__(other, 'content_json', 'bad foreign JSON')  # Simulate foreign corruption after decoding.
        self.state.ledger.transfer_records[other.id] = other
        self.assertEqual(baseline,citation(read_event(self.state.workspace,self.state.ledger,self.event_id),self.state.ledger,self.event_id))
        with self.assertRaises(ValueError): read_event(self.state.workspace,self.state.ledger,'other')
        with self.assertRaises(ValueError): self.state.ledger.compute_gap(self.event_id,{'missing':1},workspace=self.state.workspace)

    def test_invalid_drill_demand_and_valid_zero_are_distinct(self):
        self.activate()
        for value in (-1,float('nan'),float('inf'),True):
            with self.subTest(value=value),self.assertRaises(ValueError): self.rows(value)
        row = next(r for r in self.rows(0) if r['item_id']==self.item_id)
        self.assertEqual(0,row['gap'])

    def test_reading_every_surface_preserves_all_business_files_and_models(self):
        self.full(); self.report(amount='2'); self.activate()
        before, ledger, events = self.files(),copy.deepcopy(self.state.ledger.to_dict()),self.state.workspace.events
        self.rows(); self.page(); self.answer(); _materials(self.state.workspace,self.event_id,self.state.ledger)
        self.assertEqual(before,self.files()); self.assertEqual(ledger,self.state.ledger.to_dict())
        self.assertEqual(events,self.state.workspace.events)

    def test_missing_event_database_keeps_page_readable_and_quantities_pending(self):
        self.activate()
        before = self.files()
        record = self.state.registry.events[self.event_id]
        self.state.registry.events[self.event_id] = replace(record, snapshot_path=str(self.root/'missing.db'))
        self.state.workspace.event_id = ''  # Legacy unbound workspace cannot replace the missing event DB.
        html = self.page()
        self.assertIn('当前数量暂不展示', html)
        self.assertIn('请先进入事件任务空间', html)
        self.assertNotIn('账面可用 10', html)
        from pathlib import Path
        self.assertEqual(before, {p:Path(p).read_bytes() for p in before})


if __name__=='__main__': unittest.main()
