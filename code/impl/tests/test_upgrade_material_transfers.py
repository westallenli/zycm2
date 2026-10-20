"""P08-06a event transfer journal: handover, transit and actual receipt."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.material_flows import MaterialFlowService
from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.material_transfers import (
    MaterialTransferService, transfer_balance, transfer_in_transit,
    loan_status, return_damaged,
)
from panda_brain.materials import MaterialsLedger
from panda_brain.material_withdrawals import WithdrawalException, project_withdrawal
from panda_brain.record_contracts import ObjectReference
from panda_brain.workspace import ScenarioWorkspace


class MaterialTransferTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('transfers', event_id='fire-a')
        auth = EventAuthorizationService(
            self.workspace, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda subject: subject == 'deputy',
        )
        auth.record_initial_appointment(
            'duty', event_id='fire-a', role=Role.DEPUTY,
            subject_id='deputy', evidence='线下任命记录', request_id='appointment-1',
        )
        self.ledger = MaterialsLedger()
        self.ledger.upsert_item(name='水带', unit='盘', item_id='hose')
        self.ledger.add_squad(name='转出队', squad_id='source',
                              member_phones=('captain-a',), leader_phone='captain-a')
        self.ledger.add_squad(name='接收队', squad_id='target',
                              member_phones=('captain-b',), leader_phone='captain-b')
        self.ledger.set_carry_line('source', 'hose', quantity=10)
        self.ledger.set_carry_line('target', 'hose', quantity=2)
        arrivals = MaterialFlowService(self.ledger, 'fire-a')
        for squad, captain in (('source', 'captain-a'), ('target', 'captain-b')):
            arrivals.record_arrival(
                ObjectReference('fire-a', 'team_arrival', squad, 1),
                arrived_phones=(captain,), expected_version=0, actor_id=captain,
                source='队长到场报告', request_id=f'arrival-{squad}',
            )
        self.service = MaterialTransferService(self.workspace, self.ledger, 'fire-a')

    def approve(self, transfer_id='transfer-1', quantity=4, **changes):
        args = dict(transfer_id=transfer_id, item_id='hose', from_squad='source',
                    to_squad='target', quantity=quantity, actor_id='deputy',
                    source='指定副总指挥线下确认', request_id=f'approve-{transfer_id}')
        args.update(changes)
        return self.service.approve(**args)

    def send(self, transfer_id='transfer-1', **changes):
        args = dict(transfer_id=transfer_id, actor_id='captain-a',
                    source='转出队实际交出报告', request_id=f'send-{transfer_id}')
        args.update(changes)
        return self.service.send(**args)

    def receive(self, quantity, **changes):
        args = dict(transfer_id='transfer-1', quantity=quantity,
                    actor_id='captain-b', source='接收队实收报告',
                    request_id='receive-1', expected_version=2)
        args.update(changes)
        return self.service.receive(**args)

    def balance(self, squad):
        return transfer_balance(self.ledger, event_id='fire-a',
                                squad_id=squad, item_id='hose').available

    def borrow_received(self, quantity=4):
        self.approve('loan-1', quantity=quantity, purpose='loan')
        self.send('loan-1')
        self.receive(quantity, transfer_id='loan-1', request_id='receive-loan-1')

    def approve_return(self, quantity=3, **changes):
        args = dict(transfer_id='return-1', item_id='hose',
                    from_squad='target', to_squad='source', quantity=quantity,
                    actor_id='deputy', source='指定副总指挥确认归还',
                    request_id='approve-return-1', purpose='return', return_for='loan-1')
        args.update(changes)
        return self.service.approve(**args)

    def test_approval_has_no_stock_effect_handover_transits_then_partial_receipts(self):
        before_snapshot = self.ledger.snapshots['fire-a'].squads['source'].lines['hose'].qty_registered
        self.approve()
        self.assertEqual(0, self.ledger.compute_gap('fire-a', {'hose': 12})[0]['gap'])
        self.assertEqual((10, 2, 0), (self.balance('source'), self.balance('target'),
                                    transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        self.send()
        self.assertEqual(4, self.ledger.compute_gap('fire-a', {'hose': 12})[0]['gap'])
        self.assertEqual((6, 2, 4), (self.balance('source'), self.balance('target'),
                                    transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        self.receive(1.5)
        self.assertEqual(2.5, self.ledger.compute_gap('fire-a', {'hose': 12})[0]['gap'])
        self.assertEqual((6, 3.5, 2.5), (self.balance('source'), self.balance('target'),
                                        transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        self.receive(2.5, request_id='receive-2', expected_version=3)
        self.assertEqual(0, self.ledger.compute_gap('fire-a', {'hose': 12})[0]['gap'])
        self.assertEqual((6, 6, 0), (self.balance('source'), self.balance('target'),
                                    transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        self.assertEqual(before_snapshot, self.ledger.snapshots['fire-a'].squads['source'].lines['hose'].qty_registered)

    def test_role_team_and_event_gates_fail_without_writes(self):
        before = self.ledger.to_dict()
        for changes in ({'actor_id': 'captain-a'}, {'from_squad': 'target', 'to_squad': 'target'},
                        {'quantity': 0}, {'quantity': float('nan')}, {'item_id': 'other'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, PermissionError)):
                self.approve(**changes)
        with self.assertRaises(ValueError):
            MaterialTransferService(self.workspace, self.ledger, 'fire-b')
        self.assertEqual(before, self.ledger.to_dict())

        unauthorised = MaterialTransferService(
            ScenarioWorkspace('no-appointment', event_id='fire-a'),
            self.ledger, 'fire-a',
        )
        with self.assertRaises(PermissionError):
            unauthorised.approve(
                transfer_id='transfer-1', item_id='hose', from_squad='source',
                to_squad='target', quantity=4, actor_id='deputy',
                source='无任命', request_id='unauthorised-1',
            )
        self.assertEqual(before, self.ledger.to_dict())

        self.approve()
        before = self.ledger.to_dict()
        with self.assertRaises(PermissionError):
            self.send(actor_id='captain-b')
        with self.assertRaises(ValueError):
            self.receive(1)
        self.assertEqual(before, self.ledger.to_dict())

    def test_handover_rechecks_audited_deputy_approval(self):
        approved = self.approve()
        self.ledger.transfer_records[approved.id] = replace(approved, actor_id='outsider')
        before = self.ledger.to_dict()
        with self.assertRaises(PermissionError):
            self.send()
        self.assertEqual(before, self.ledger.to_dict())

    def test_overdraw_duplicate_receipt_and_stale_version_are_rejected(self):
        self.approve(quantity=8)
        self.send()
        before = self.ledger.to_dict()
        self.approve('transfer-2', quantity=3)
        with self.assertRaises(ValueError):
            self.send('transfer-2')
        self.assertEqual(len(before['transfer_records']) + 1,
                         len(self.ledger.transfer_records))  # second approval only
        self.receive(5)
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.receive(4, request_id='receive-2', expected_version=3)
        with self.assertRaises(ValueError):
            self.receive(1, request_id='receive-2', expected_version=2)
        self.assertEqual(before, self.ledger.to_dict())

    def test_idempotent_requests_and_conflicts(self):
        first = self.approve()
        self.assertEqual(first, self.approve())
        sent = self.send()
        self.assertEqual(sent, self.send())
        received = self.receive(2)
        self.assertEqual(received, self.receive(2))
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.receive(3)
        self.assertEqual(before, self.ledger.to_dict())

    def test_loss_after_handover_cannot_spend_sent_quantity_again(self):
        self.approve(quantity=4)
        self.send()
        flow = MaterialFlowService(self.ledger, 'fire-a')
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            flow.record_loss(
                ObjectReference('fire-a', 'material_loss', 'loss-1', 1),
                squad_id='source', item_id='hose', kind='consumed', amount=7,
                expected_version=0, actor_id='captain-a', source='耗损报告', request_id='loss-1',
            )
        self.assertEqual(before, self.ledger.to_dict())
        flow.record_loss(
            ObjectReference('fire-a', 'material_loss', 'loss-1', 1),
            squad_id='source', item_id='hose', kind='consumed', amount=3,
            expected_version=0, actor_id='captain-a', source='耗损报告', request_id='loss-2',
        )
        self.assertEqual(3, self.balance('source'))

    def test_unbacked_loss_snapshot_cannot_release_extra_stock(self):
        self.approve(quantity=8)
        line = self.ledger.snapshots['fire-a'].squads['source'].lines['hose']
        line.consumed = 2
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(before, self.ledger.to_dict())

    def test_formal_gap_book_amount_waits_for_transfer_source_reconciliation(self):
        self.approve()
        self.send()
        assessment = assess_gap_inputs(
            self.workspace, self.ledger, event_id='fire-a', item_id='hose',
            order_id='missing-task',
        )
        self.assertIsNone(assessment.book_available)
        self.assertIsNone(assessment.inventory)
        self.assertIsNone(assessment.current_gap)
        self.assertIsNone(assessment.projected_gap)
        self.assertIn('调拨、撤离与数量守恒来源待核对', assessment.missing)

    def test_restart_preserves_stages_and_rejects_orphaned_history(self):
        self.approve()
        self.send()
        self.receive(2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'materials.json'
            self.ledger.save(path)
            restored = MaterialsLedger.load(path)
            self.assertEqual(self.ledger.to_dict(), restored.to_dict())
            self.assertEqual(2, transfer_in_transit(restored, event_id='fire-a', item_id='hose'))
            raw = self.ledger.to_dict()
            second = next(key for key, value in raw['transfer_records'].items()
                          if value['reference']['version'] == 2)
            del raw['transfer_records'][second]
            path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(ValueError):
                MaterialsLedger.load(path)

    def test_loan_return_keeps_relation_transit_damage_and_outstanding(self):
        self.borrow_received()
        self.assertEqual((4, 0, 0, 4), (
            loan_status(self.ledger, event_id='fire-a', loan_transfer_id='loan-1').borrowed,
            loan_status(self.ledger, event_id='fire-a', loan_transfer_id='loan-1').returned,
            return_damaged(self.ledger, event_id='fire-a', item_id='hose'),
            loan_status(self.ledger, event_id='fire-a', loan_transfer_id='loan-1').outstanding))
        self.approve_return()
        self.assertEqual(3, loan_status(self.ledger, event_id='fire-a',
                                        loan_transfer_id='loan-1').return_approved)
        self.assertEqual((6, 6), (self.balance('source'), self.balance('target')))
        self.service.send(transfer_id='return-1', actor_id='captain-b',
                          source='借用队交出', request_id='send-return-1')
        self.assertEqual((6, 3, 3), (self.balance('source'), self.balance('target'),
                                    transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        self.service.receive(transfer_id='return-1', quantity=3, damaged_quantity=1,
                             actor_id='captain-a', source='原队实收并报告损坏',
                             request_id='receive-return-1', expected_version=2)
        self.assertEqual((8, 3, 0), (self.balance('source'), self.balance('target'),
                                    transfer_in_transit(self.ledger, event_id='fire-a', item_id='hose')))
        status = loan_status(self.ledger, event_id='fire-a', loan_transfer_id='loan-1')
        self.assertEqual((4, 3, 1, 1), (status.borrowed, status.returned,
                                        status.damaged_on_return, status.outstanding))
        row = self.ledger.compute_gap('fire-a', {'hose': 12})[0]
        self.assertEqual((1, 1), (row['gap'], row['damaged']))
        self.assertEqual(10, self.ledger.snapshots['fire-a'].squads['source'].lines['hose'].qty_registered)

    def test_withdrawal_draft_defaults_to_current_balance_and_tracks_exceptions(self):
        self.borrow_received()
        self.approve_return()
        self.service.send(transfer_id='return-1', actor_id='captain-b',
                          source='借用队交出', request_id='send-return-1')
        self.service.receive(transfer_id='return-1', quantity=3, damaged_quantity=1,
                             actor_id='captain-a', source='原队实收并报告损坏',
                             request_id='receive-return-1', expected_version=2)
        before = self.ledger.to_dict()
        default = project_withdrawal(self.ledger, event_id='fire-a', squad_id='target')
        self.assertEqual((3, 3, 0, 0), (
            default.lines[0].on_site_before, default.lines[0].carried_out,
            default.lines[0].retained, default.lines[0].handed_over))
        self.assertEqual((('loan-1', 1),), default.open_loans)
        exceptions = (
            WithdrawalException('hose', 1, 'retained', '现场器材点', '现场保管员'),
            WithdrawalException('hose', 1, 'handed_over', '北段集结点', '接收单位甲'),
        )
        draft = project_withdrawal(self.ledger, event_id='fire-a',
                                   squad_id='target', exceptions=exceptions)
        self.assertEqual((3, 1, 1, 1), (
            draft.lines[0].on_site_before, draft.lines[0].carried_out,
            draft.lines[0].retained, draft.lines[0].handed_over))
        self.assertEqual((('loan-1', 1),), draft.open_loans)
        self.assertEqual((), draft.unresolved_transfers)
        self.assertEqual(before, self.ledger.to_dict())

    def test_withdrawal_draft_keeps_unreceived_transfer_outside_on_site(self):
        self.approve(quantity=4)
        approved = project_withdrawal(self.ledger, event_id='fire-a', squad_id='source')
        self.assertEqual(10, approved.lines[0].carried_out)
        self.assertEqual(('transfer-1',), approved.unresolved_transfers)
        self.send()
        self.receive(1.5)
        source = project_withdrawal(self.ledger, event_id='fire-a', squad_id='source')
        target = project_withdrawal(self.ledger, event_id='fire-a', squad_id='target')
        self.assertEqual((6, 3.5), (source.lines[0].carried_out,
                                    target.lines[0].carried_out))
        self.assertEqual(('transfer-1',), target.unresolved_transfers)
        self.assertEqual(2.5, transfer_in_transit(self.ledger, event_id='fire-a',
                                                  item_id='hose'))

    def test_withdrawal_draft_rejects_untracked_or_overdrawn_exceptions(self):
        before = self.ledger.to_dict()
        valid = WithdrawalException('hose', 1, 'retained', '现场器材点', '保管员')
        invalid = (
            (WithdrawalException('hose', 11, 'retained', '现场器材点', '保管员'),),
            (WithdrawalException('hose', float('nan'), 'retained', '现场器材点', '保管员'),),
            (WithdrawalException('unknown', 1, 'retained', '现场器材点', '保管员'),),
            (WithdrawalException('hose', 1, 'handed_over', '', '接收人'),),
            (WithdrawalException('hose', 1, 'handed_over', '现场', ''),),
            (valid, valid),
        )
        for exceptions in invalid:
            with self.subTest(exceptions=exceptions), self.assertRaises(ValueError):
                project_withdrawal(self.ledger, event_id='fire-a',
                                   squad_id='source', exceptions=exceptions)
        with self.assertRaises(ValueError):
            project_withdrawal(self.ledger, event_id='other', squad_id='source')
        with self.assertRaises(ValueError):
            project_withdrawal(self.ledger, event_id='fire-a', squad_id='unknown')
        self.assertEqual(before, self.ledger.to_dict())

    def test_return_requires_received_matching_loan_and_bounded_quantity(self):
        self.approve('loan-1', purpose='loan')
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.approve_return()
        self.assertEqual(before, self.ledger.to_dict())
        self.send('loan-1')
        self.receive(2, transfer_id='loan-1', request_id='receive-loan-1')
        for changes in ({'quantity': 3}, {'item_id': 'other'},
                        {'from_squad': 'source', 'to_squad': 'target'},
                        {'return_for': 'missing'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.approve_return(**changes)
        self.approve_return(quantity=2)
        self.assertEqual(self.approve_return(quantity=2), self.approve_return(quantity=2))
        with self.assertRaises(ValueError):
            self.approve_return(quantity=1, transfer_id='return-2',
                                request_id='approve-return-2')

    def test_damage_only_on_return_receipt_and_replay_is_idempotent(self):
        self.approve()
        self.send()
        with self.assertRaises(ValueError):
            self.receive(2, damaged_quantity=1)
        self.borrow_received()
        self.approve_return()
        self.service.send(transfer_id='return-1', actor_id='captain-b',
                          source='借用队交出', request_id='send-return-1')
        args = dict(transfer_id='return-1', quantity=3, damaged_quantity=1,
                    actor_id='captain-a', source='原队实收并报告损坏',
                    request_id='receive-return-1', expected_version=2)
        first = self.service.receive(**args)
        self.assertEqual(first, self.service.receive(**args))
        before = self.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.service.receive(**dict(args, damaged_quantity=2))
        self.assertEqual(before, self.ledger.to_dict())

    def test_loan_return_survives_reload_and_rejects_broken_link(self):
        self.borrow_received()
        self.approve_return(quantity=2)
        self.service.send(transfer_id='return-1', actor_id='captain-b',
                          source='借用队交出', request_id='send-return-1')
        self.service.receive(transfer_id='return-1', quantity=2, damaged_quantity=0.5,
                             actor_id='captain-a', source='原队实收',
                             request_id='receive-return-1', expected_version=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'materials.json'
            self.ledger.save(path)
            restored = MaterialsLedger.load(path)
            self.assertEqual(2, loan_status(restored, event_id='fire-a',
                                             loan_transfer_id='loan-1').outstanding)
            payload = json.loads(path.read_text(encoding='utf-8'))
            approval = next(raw for raw in payload['transfer_records'].values()
                            if raw['reference']['object_id'] == 'return-1'
                            and raw['reference']['version'] == 1)
            content = json.loads(approval['content_json'])
            content['return_for'] = 'missing-loan'
            approval['content_json'] = json.dumps(content)
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(ValueError):
                MaterialsLedger.load(path)


if __name__ == '__main__':
    unittest.main()
