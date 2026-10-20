"""Settled lending departure preserves history, borrowed liabilities and recovery."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from panda_brain import material_withdrawals as departures
from panda_brain.material_transfers import loan_status, transfer_balance
from panda_brain.materials import MaterialsLedger
from panda_brain.models import PersonRecord
from panda_brain.persistence import save_workspace
from panda_brain.panel import PanelActionError
from . import test_upgrade_material_custody_settlements as fixture
from .test_upgrade_material_custody_receipts import Interrupted


class LenderWithdrawalTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'full', 'verify', 'settle', 'status'):
        locals()[name] = getattr(fixture.CustodySettlementTests, name)

    def setUp(self):
        fixture.CustodySettlementTests.setUp(self)

    def report(self, **changes):
        return self.state.handle_team_material_withdrawal(self.event_id, **(dict(
            captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad,
            evidence='匿名借出队撤离清单', request_id='depart-lender') | changes))

    def review(self, **changes):
        return self.state.material_withdrawal_review(self.event_id, **(dict(
            captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad) | changes))

    def third_team(self):
        phone = '13977773008'
        self.state.registry.import_person_library((PersonRecord(
            name='匿名三队长', phone=phone, unit='三中队', is_leader=True),))
        token, error = self.state.member_login(phone, '123456')
        self.assertEqual('', error)
        squad = self.state.ledger.add_squad(name='三中队', member_phones=(phone,),
            leader_phone=phone, library=self.state.registry.library).squad_id
        self.state.ledger.set_carry_line(squad, self.item_id, quantity=10)
        self.state._save_ledger()
        self.state.handle_select_squads(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[squad], channel='phone', request_id='select-third')
        qr = next(q.qr_id for q in self.state.registry.qrs.values() if q.event_id == self.event_id)
        self.state.handle_captain_scan(self.event_id, captain_token=token, squad_id=squad, qr_id=qr)
        self.state.handle_captain_arrival(self.event_id, captain_token=token, squad_id=squad,
            mode='initial', phones=[], request_id='arrival-third')
        return squad, token

    def loan_two(self, *, incoming=False):
        third, token = self.third_team()
        self.approve(transfer_id='loan-2', request_id='approve-loan-2', purpose='loan', quantity='2',
            from_squad=third if incoming else self.source_squad,
            to_squad=self.source_squad if incoming else third)
        self.send(transfer_id='loan-2', request_id='send-loan-2',
            member_token=token if incoming else self.tokens[self.SOURCE])
        self.receive(transfer_id='loan-2', request_id='receive-loan-2', quantity='2',
            member_token=self.tokens[self.SOURCE] if incoming else token)
        return third, token

    def test_full_receipt_and_passed_verification_still_require_explicit_settlement(self):
        before = self.files()
        self.assertFalse(self.review().can_report)
        self.assertEqual(('loan-1',), self.review().unsettled_lender_loans)
        self.assertEqual(before, self.files())
        for stage in ('none', 'received', 'verified'):
            if stage == 'received':
                batch = self.receipt(quantity=4, damaged_quantity=0)
            elif stage == 'verified':
                self.verify(batch)
            before = self.files()
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                self.report()
            self.assertEqual(before, self.files())
        settlement = self.settle()
        current = self.review()
        self.assertTrue(current.can_report)
        self.assertEqual((), current.borrowed_loans)
        self.assertEqual(settlement.id, current.settled_lender_loans[0].settlement_id)

    def test_departure_freezes_settlement_without_rewriting_history_or_adding_rear_stock(self):
        self.full(damaged=1)
        settlement = self.settle(handling_evidence='匿名损坏核对', handling_conclusion='隔离保管')
        ledger = self.state.ledger.to_dict()
        borrower = departures.withdrawal_record(self.state.ledger,
            event_id=self.event_id, squad_id=self.target_squad)
        record = self.report()
        content = json.loads(record.content_json)
        self.assertEqual([['loan-1', 4]], content['open_loans'])
        self.assertEqual([dict(loan_transfer_id='loan-1', quantity=4, settlement_id=settlement.id)],
                         content['settled_lender_loans'])
        self.assertNotIn('borrowed_custody', content)
        line = next(line for line in content['lines'] if line['item_id'] == self.item_id)
        self.assertEqual((6, 6), (line['on_site_before'], line['carried_out']))
        after = self.state.ledger.to_dict()
        self.assertEqual({k: v for k, v in ledger.items() if k != 'withdrawal_records'},
                         {k: v for k, v in after.items() if k != 'withdrawal_records'})
        self.assertEqual(borrower, departures.withdrawal_record(self.state.ledger,
            event_id=self.event_id, squad_id=self.target_squad))
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                       loan_transfer_id='loan-1').outstanding)
        for squad in (self.source_squad, self.target_squad):
            self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
                squad_id=squad, item_id=self.item_id).available)
        self.assertEqual(after, MaterialsLedger.load(self.state.materials_path).to_dict())
        self.state = self.new_state()
        self.tokens[self.SOURCE], _ = self.state.member_login(self.SOURCE, '123456')
        self.assertEqual(record, self.review().existing)
        self.assertFalse(self.review().can_report)
        self.assertEqual((), self.event_space().verify_audit_chain())

    def test_only_existing_current_captain_may_read_or_report(self):
        self.full()
        self.settle()
        self.state.registry.maintenance_phones.add(self.OTHER)
        before = self.files()
        for phone in (self.DEPUTY, self.TARGET, self.LOGISTICS, self.OTHER, self.REPLACEMENT):
            with self.subTest(phone=phone), self.assertRaises(PermissionError):
                self.review(captain_token=self.tokens[phone])
            with self.assertRaises(PermissionError):
                self.report(captain_token=self.tokens[phone])
        self.assertEqual(before, self.files())

    def test_every_lending_loan_must_be_settled(self):
        self.full()
        self.settle()
        third, token = self.loan_two()
        self.assertEqual(('loan-2',), self.review().unsettled_lender_loans)
        with self.assertRaises(ValueError):
            self.report()
        self.state.handle_team_material_withdrawal(self.event_id, captain_token=token,
            squad_id=third, borrowed_custody=(departures.BorrowedCustody('loan-2', 2, '匿名后方', '三队长'),),
            evidence='匿名三队撤离', request_id='depart-third')
        batch = self.receipt(loan_transfer_id='loan-2', quantity=2, damaged_quantity=0, request_id='receipt-two')
        self.verify(batch, loan_transfer_id='loan-2', request_id='verify-two')
        self.settle(loan_transfer_id='loan-2', request_id='settle-two')
        record = self.report()
        content = json.loads(record.content_json)
        self.assertEqual(2, len(content['settled_lender_loans']))
        self.assertEqual(4, content['lines'][0]['carried_out'])

    def test_mixed_borrowing_requires_its_own_custody_despite_settled_lending(self):
        self.full()
        self.settle()
        self.loan_two(incoming=True)
        self.assertEqual((('loan-2', 2),), self.review().borrowed_loans)
        before = self.files()
        for entries in ((), (departures.BorrowedCustody('loan-1', 4, '匿名后方', '队长'),),
                        (departures.BorrowedCustody('loan-2', 1, '匿名后方', '队长'),)):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.report(borrowed_custody=entries)
            self.assertEqual(before, self.files())
        record = self.report(borrowed_custody=(departures.BorrowedCustody('loan-2', 2, '匿名后方', '队长'),))
        content = json.loads(record.content_json)
        self.assertEqual(8, content['lines'][0]['carried_out'])
        self.assertEqual('loan-2', content['borrowed_custody'][0]['loan_transfer_id'])
        self.assertEqual('loan-1', content['settled_lender_loans'][0]['loan_transfer_id'])

    def test_related_unfinished_transfer_still_blocks_after_settlement(self):
        self.full()
        self.settle()
        third, token = self.third_team()
        self.approve(to_squad=third, quantity='1')
        before = self.files()
        self.assertFalse(self.review().can_report)
        self.assertEqual(('transfer-1',), self.review().draft.unresolved_transfers)
        with self.assertRaises(ValueError):
            self.report()
        self.assertEqual(before, self.files())

    def test_missing_source_mirror_and_frozen_reference_conflicts_fail_closed(self):
        self.full()
        settlement = self.settle()
        space = self.event_space()
        broken = copy.deepcopy(space)
        broken.object_revisions.pop(settlement.id)
        with self.assertRaises(ValueError):
            departures.review_withdrawal(broken, self.state.ledger,
                event_id=self.event_id, squad_id=self.source_squad)
        record = self.report()
        for change in (dict(quantity=3), dict(settlement_id='wrong-settlement'), dict(loan_transfer_id='wrong-loan')):
            ledger = copy.deepcopy(self.state.ledger)
            content = json.loads(record.content_json)
            content['settled_lender_loans'][0].update(change)
            ledger.withdrawal_records[record.id] = replace(record, content_json=json.dumps(content))
            with self.subTest(change=change), self.assertRaises(ValueError):
                departures.review_withdrawal(self.event_space(), ledger,
                    event_id=self.event_id, squad_id=self.source_squad)

    def test_exact_replay_writes_nothing_and_conflicting_replay_is_rejected(self):
        self.full()
        self.settle()
        record = self.report()
        before = self.files()
        with (patch('panda_brain.materials.MaterialsLedger.save', side_effect=AssertionError('must not save')),
              patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('must not save'))):
            self.assertEqual(record, self.report())
        with self.assertRaises(ValueError):
            self.report(evidence='other')
        self.assertEqual(before, self.files())

    def test_settlement_must_precede_departure_even_with_a_valid_rebuilt_audit_chain(self):
        self.full()
        settlement = self.settle()
        self.report()
        broken = self.event_space()
        entries = list(broken.events)
        moved = next(e for e in entries if e.kind == 'ObjectRevisionRecorded'
                     and e.payload.get('revision_id') == settlement.id)
        entries.remove(moved)
        entries.append(moved)
        broken._events.clear()
        for entry in entries:
            broken.append_event(entry.kind, **entry.payload)
        self.assertEqual((), broken.verify_audit_chain())
        with self.assertRaises(ValueError):
            departures.review_withdrawal(broken, self.state.ledger,
                event_id=self.event_id, squad_id=self.source_squad)

    def test_active_workspace_failure_restores_memory_and_replay_skips_persistence(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.full()
        self.settle()
        before = self.files()
        memory = copy.deepcopy(self.state.workspace.events)
        with patch.object(self.state, 'persist_if_db', side_effect=OSError('anonymous active save failure')):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(before, self.files())
        self.assertEqual(memory, self.state.workspace.events)
        record = self.report()
        before = self.files()
        with patch.object(self.state, 'persist_if_db', side_effect=AssertionError('must not persist')):
            self.assertEqual(record, self.report())
        self.assertEqual(before, self.files())

    def test_postwrite_failure_rolls_back_and_restart_can_report(self):
        self.full()
        self.settle()
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous lender save failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(before, self.files())
        self.assertIsNone(self.review().existing)
        self.report()

    def test_interruption_recovers_before_image_before_retry(self):
        self.full()
        self.settle()
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop):
            with self.assertRaises(Interrupted):
                self.report()
        self.assertTrue(list((self.root / '.state-transactions').glob('txn-*')))
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.member_sessions)
        self.tokens[self.SOURCE], _ = self.state.member_login(self.SOURCE, '123456')
        self.assertTrue(self.review().can_report)
        self.report()

    def test_closed_event_rejects_departure_and_replay(self):
        self.full()
        self.settle()
        self.report()
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        with self.assertRaises(PanelActionError):
            self.report()
        self.assertEqual(before, self.files())
