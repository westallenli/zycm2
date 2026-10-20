"""Read-only existing event quantities, provenance, conservation and rear isolation."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from panda_brain.material_inventory_projection import project
from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.material_withdrawals import WithdrawalException
from panda_brain.models import ExecutionOrder
from panda_brain.panel import render_page
from panda_brain.workspace import event_hash, GENESIS_HASH
from . import test_upgrade_manual_material_transfers as transfer_fixture
from . import test_upgrade_lender_material_withdrawals as custody_fixture


def rehash(space):
    previous, events = GENESIS_HASH, []
    for sequence, entry in enumerate(space.events, 1):
        digest = event_hash(sequence, entry.kind, entry.payload, previous, entry.recorded_at)
        events.append(replace(entry, sequence=sequence, prev_hash=previous, hash=digest))
        previous = digest
    space._events = events


class InventoryProjectionTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'new_state', 'event_space',
                 'approve', 'send', 'receive', 'make_loan_received', 'approve_return', 'setUp'):
        locals()[name] = getattr(transfer_fixture.NamedTransferTests, name)

    def projection(self, *, space=None, ledger=None, **changes):
        return project(space or self.event_space(), ledger or self.state.ledger,
                       **(dict(event_id=self.event_id, item_id=self.item_id) | changes))

    def files(self):
        return {p: p.read_bytes() for p in (self.state.registry_path, self.state.materials_path,
                Path(self.state.registry.events[self.event_id].snapshot_path))}

    def test_arrival_approval_sent_partial_receipt_conserve_without_mutation(self):
        self.assertEqual(12, self.projection().book_available)
        self.approve()
        self.assertEqual((12, 0), (self.projection().team_available, self.projection().in_transit))
        self.send()
        self.assertEqual((8, 4), (self.projection().team_available, self.projection().in_transit))
        self.receive()
        before = self.files()
        ledger = self.state.ledger.to_dict()
        view = self.projection()
        self.assertEqual((12, 10, 2), (view.arrival_base, view.book_available, view.in_transit))
        self.assertEqual(3, len(view.transfer_refs))
        self.assertEqual(before, self.files())
        self.assertEqual(ledger, self.state.ledger.to_dict())
        result = assess_gap_inputs(self.event_space(), self.state.ledger,
            event_id=self.event_id, item_id=self.item_id, order_id='unknown-task')
        self.assertEqual(view, result.inventory)
        self.assertEqual(10, result.book_available)
        self.assertIsNone(result.current_gap)
        self.assertIsNone(result.projected_gap)
        self.assertIn('经专业核定的物资用量依据', result.missing)

    def test_return_damage_is_separate_and_not_usable_or_in_transit(self):
        self.make_loan_received()
        self.approve_return()
        self.send(transfer_id='return-1', request_id='send-return-1', member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='return-1', request_id='receive-return-1',
            member_token=self.tokens[self.SOURCE], quantity='3', damaged_quantity='1')
        view = self.projection()
        self.assertEqual((12, 11, 0, 1), (view.arrival_base, view.team_available, view.in_transit, view.return_damage))
        self.assertEqual(11, view.book_available)
        self.assertEqual(6, len(view.transfer_refs))

    def test_reported_losses_and_correction_use_latest_total_not_all_versions(self):
        self.approve()
        self.state.handle_manual_material_loss(self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, item_id=self.item_id, loss_id='loss-1', kind='consumed',
            amount='3', expected_version='0', evidence='匿名消耗报告', occurred_at='', reason='', request_id='loss-1')
        self.assertEqual((9, 3), (self.projection().team_available, self.projection().losses))
        self.state.handle_manual_material_loss(self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, item_id=self.item_id, loss_id='loss-1', kind='consumed',
            amount='1', expected_version='1', evidence='匿名更正报告', occurred_at='', reason='匿名数量更正', request_id='loss-2')
        view = self.projection()
        self.assertEqual((11, 1, 2), (view.team_available, view.losses, len(view.loss_refs)))

    def test_departure_exceptions_keep_location_and_owner_without_inventory_assignment(self):
        self.state.handle_team_material_withdrawal(self.event_id,
            captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad,
            exceptions=(WithdrawalException(self.item_id, 2, 'retained', '模拟留置点', '<保管人>'),
                        WithdrawalException(self.item_id, 1, 'handed_over', '模拟移交点', '保管对象待核')),
            evidence='匿名撤离例外清单', request_id='withdraw-1')
        view = self.projection()
        self.assertEqual((2, 7, 2, 1), (view.team_available, view.carried_out, view.retained, view.handed_over))
        self.assertIsNone(view.book_available)
        self.assertEqual(('模拟留置点', '<保管人>'), (view.exceptions[0].location, view.exceptions[0].custodian))
        self.assertEqual(1, len(view.withdrawal_refs))
        result = assess_gap_inputs(self.event_space(), self.state.ledger,
            event_id=self.event_id, item_id=self.item_id, order_id='unknown')
        self.assertIsNone(result.book_available)
        self.assertIn('留置/移交位置及可调度归属待核对，未并入队伍账面可用', result.missing)

    def test_missing_or_orphaned_mirrors_and_deleted_last_receipt_fail_closed(self):
        self.approve()
        self.send()
        last = self.receive()
        original_space, original_ledger = self.event_space(), copy.deepcopy(self.state.ledger)
        for mode in ('missing-mirror', 'missing-last', 'changed-mirror', 'bad-key'):
            space, ledger = copy.deepcopy(original_space), copy.deepcopy(original_ledger)
            if mode == 'missing-mirror':
                space._events = [e for e in space.events if e.payload.get('transfer_record_id') != last.id]
                rehash(space)
            elif mode == 'missing-last':
                ledger.transfer_records.pop(last.id)
            elif mode == 'changed-mirror':
                # Hash mismatch is an explicit failure, not a valid new business event.
                next(e for e in space.events if e.payload.get('transfer_record_id') == last.id).payload['stage'] = 'sent'
            else:
                ledger.transfer_records['wrong-key'] = ledger.transfer_records.pop(last.id)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.projection(space=space, ledger=ledger)
            result = assess_gap_inputs(space, ledger, event_id=self.event_id, item_id=self.item_id, order_id='unknown')
            self.assertIsNone(result.book_available)
            self.assertIsNone(result.inventory)

    def test_reordered_but_rehashed_transfer_mirrors_and_overdraw_fail(self):
        self.approve()
        self.send()
        self.receive()
        space = self.event_space()
        mirrors = [i for i, e in enumerate(space.events) if e.kind == 'MaterialTransferStageReported']
        space._events[mirrors[0]], space._events[mirrors[1]] = space._events[mirrors[1]], space._events[mirrors[0]]
        rehash(space)
        self.assertFalse(space.verify_audit_chain())
        with self.assertRaises(ValueError):
            self.projection(space=space)
        ledger = copy.deepcopy(self.state.ledger)
        ledger.snapshots[self.event_id].squads[self.source_squad].lines[self.item_id].consumed = 10
        with self.assertRaises(ValueError):
            self.projection(ledger=ledger)

    def test_other_event_is_excluded_and_wrong_event_request_fails(self):
        self.approve()
        baseline = self.projection()
        foreign = replace(next(iter(self.state.ledger.transfer_records.values())),
            reference=replace(next(iter(self.state.ledger.transfer_records.values())).reference, event_id='other'),
            content_json='{"invalid_foreign_source": true}')
        self.state.ledger.transfer_records[foreign.id] = foreign
        self.assertEqual(baseline, self.projection())
        with self.assertRaises(ValueError):
            self.projection(event_id='other')

    def test_new_item_received_by_team_without_arrival_line_is_counted_once(self):
        new = self.state.ledger.upsert_item(name='模拟水泵', unit='台').item_id
        # Extend only this fixture's authenticated arrival baseline and its matching mirror.
        ledger = copy.deepcopy(self.state.ledger)
        from panda_brain.materials import SnapshotLine
        ledger.snapshots[self.event_id].squads[self.source_squad].lines[new] = SnapshotLine(new, qty_registered=2, qty_usable=2)
        arrival = next(r for r in ledger.flow_records.values() if r.reference.object_id == self.source_squad)
        data = json.loads(arrival.content_json)
        data['carry_baseline'][new] = 2
        ledger.flow_records[arrival.id] = replace(arrival, content_json=json.dumps(data))
        self.state.ledger = ledger
        self.state._save_ledger()
        self.approve(item_id=new, quantity='2')
        self.send()
        self.receive(quantity='2')
        view = self.projection(item_id=new)
        self.assertEqual((2, 2, 0), (view.arrival_base, view.book_available, view.in_transit))

    def test_existing_material_page_remains_read_only_and_shows_partition_sources(self):
        self.approve()
        self.send()
        self.receive()
        token = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.orders['projection-order'] = ExecutionOrder('projection-order', 'decision', 'option', '匿名待核任务')
        self.state.persist_if_db()
        before = self.files()
        code, html = render_page(self.state, '/materials', session_token=token,
            query={'gap_order':['projection-order'], 'gap_item':[self.item_id]})
        self.assertEqual(200, code)
        for text in ('事件物资流水数量核对', '队伍账面可用：10', '调拨在途：2', '当前不足 待核实', '无版本化耗损报告'):
            self.assertIn(text, html)
        for ref in self.projection().transfer_refs:
            self.assertIn(ref, html)
        self.assertEqual(before, self.files())

    def test_material_page_invalid_transfer_sources_render_pending_without_projection(self):
        self.approve()
        self.send()
        last = self.receive()
        token = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.state.workspace.orders['projection-order'] = ExecutionOrder('projection-order', 'decision', 'option', '匿名待核任务')
        self.state.persist_if_db()
        self.state.ledger.transfer_records['wrong-key'] = self.state.ledger.transfer_records.pop(last.id)
        before = self.files()
        code, html = render_page(self.state, '/materials', session_token=token,
            query={'gap_order':['projection-order'], 'gap_item':[self.item_id]})
        self.assertEqual(200, code)
        self.assertIn('调拨、撤离与数量守恒来源待核对', html)
        self.assertNotIn('事件物资流水数量核对', html)
        self.assertEqual(before, self.files())


class RearInventoryTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'full', 'verify', 'settle', 'status', 'report', 'setUp'):
        locals()[name] = getattr(custody_fixture.LenderWithdrawalTests, name)
    projection = InventoryProjectionTests.projection

    def test_partial_rear_receipt_damage_is_within_withdrawn_not_added_on_site(self):
        self.receipt()
        before = self.files()
        view = self.projection()
        self.assertEqual((6, 6, 2, 1, 2), (view.team_available, view.carried_out,
                         view.rear_received, view.rear_damaged, view.rear_unreceived))
        self.assertEqual(6, view.book_available)
        self.assertEqual(before, self.files())

    def test_settlement_then_lender_departure_and_restart_do_not_create_rear_stock(self):
        self.full(damaged=1)
        self.settle(handling_evidence='匿名损坏依据', handling_conclusion='隔离保管')
        self.report()
        before = self.files()
        view = self.projection()
        self.assertEqual((0, 12, 4, 1, 0), (view.book_available, view.carried_out,
                         view.rear_received, view.rear_damaged, view.rear_unreceived))
        self.assertEqual(3, len(view.custody_refs))
        self.assertEqual(before, self.files())
        self.state = self.new_state()
        self.assertEqual(view, self.projection())

    def test_broken_settlement_reference_hides_all_inventory_quantities(self):
        self.full()
        settlement = self.settle()
        self.report()
        space = self.event_space()
        space.object_revisions.pop(settlement.id)
        with self.assertRaises(ValueError):
            self.projection(space=space)
        result = assess_gap_inputs(space, self.state.ledger, event_id=self.event_id,
                                  item_id=self.item_id, order_id='unknown')
        self.assertIsNone(result.inventory)
        self.assertIsNone(result.book_available)


if __name__ == '__main__':
    unittest.main()
