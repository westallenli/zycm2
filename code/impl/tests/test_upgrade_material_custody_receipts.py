"""Anonymous named post-departure receipts, authority, quantities and recovery."""
import copy
from dataclasses import asdict
from pathlib import Path
import unittest
from unittest.mock import patch

from panda_brain.material_custody_receipts import KIND, summary
from panda_brain.material_transfers import loan_status, transfer_balance
from panda_brain.material_withdrawals import BorrowedCustody
from panda_brain.models import PersonRecord
from panda_brain.persistence import save_workspace
from . import test_upgrade_manual_material_transfers as fixture


class Interrupted(BaseException):
    pass


class CustodyReceiptTests(unittest.TestCase):
    DUTY = fixture.NamedTransferTests.DUTY
    DEPUTY = fixture.NamedTransferTests.DEPUTY
    SOURCE = fixture.NamedTransferTests.SOURCE
    TARGET = fixture.NamedTransferTests.TARGET
    OTHER = fixture.NamedTransferTests.OTHER
    LOGISTICS = '13977773006'
    REPLACEMENT = '13977773007'
    new_state = fixture.NamedTransferTests.new_state
    approve = fixture.NamedTransferTests.approve
    send = fixture.NamedTransferTests.send
    receive = fixture.NamedTransferTests.receive
    event_space = fixture.NamedTransferTests.event_space
    make_loan_received = fixture.NamedTransferTests.make_loan_received

    def setUp(self):
        fixture.NamedTransferTests.setUp(self)
        for phone in (self.LOGISTICS, self.REPLACEMENT):
            self.state.registry.import_person_library((PersonRecord(
                name='匿名后勤人员', phone=phone, unit='模拟物资供应组'),))
            self.tokens[phone], error = self.state.member_login(phone, '123456')
            self.assertEqual('', error)
            qr = next(q.qr_id for q in self.state.registry.qrs.values()
                      if q.event_id == self.event_id)
            self.state.handle_temporary_scan(self.event_id,
                member_token=self.tokens[phone], qr_id=qr)
            self.state.handle_temporary_assign(self.event_id,
                deputy_token=self.tokens[self.DEPUTY], phones=[phone],
                pos_code='log-material', request_id='assign-' + phone)
            self.state.handle_temporary_arrival(self.event_id,
                member_token=self.tokens[phone])
        self.appoint(self.LOGISTICS)
        self.make_loan_received()
        self.state.handle_team_material_withdrawal(self.event_id,
            captain_token=self.tokens[self.TARGET], squad_id=self.target_squad,
            borrowed_custody=(BorrowedCustody('loan-1', 4, '模拟归建车', '报告保管人'),),
            evidence='匿名撤离清单', request_id='depart-borrower')

    def appoint(self, phone):
        return self.state.handle_temporary_group_leader(self.event_id,
            deputy_token=self.tokens[self.DEPUTY], pos_code='log-material',
            phone=phone, evidence='线下物资供应组长指定', request_id='leader-' + phone)

    def receipt(self, **changes):
        values = dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1',
            quantity=2, damaged_quantity=1, location='模拟仓储点', custodian='报告保管人甲',
            evidence='匿名实收清单', request_id='receipt-1', expected_version=0)
        return self.state.handle_material_custody_receipt(self.event_id, **(values | changes))

    def view(self, **changes):
        return self.state.material_custody_receipts(self.event_id,
            **(dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1') | changes))

    def files(self):
        paths = (self.state.registry_path, self.state.materials_path,
                 Path(self.state.registry.events[self.event_id].snapshot_path))
        return {p: p.read_bytes() for p in paths}

    def test_partial_receipts_damage_and_read_only_summary_never_settle_or_add_stock(self):
        ledger = self.state.ledger.to_dict()
        before = self.files()
        self.assertEqual(4, self.view().unreceived_quantity)
        self.assertEqual(before, self.files())
        first = self.receipt()
        self.assertEqual('', first.times.occurred_at)
        self.assertEqual(self.LOGISTICS, first.actor_id)
        self.assertEqual((2, 1, 2, 1), (self.view().received_quantity,
            self.view().damaged_quantity, self.view().unreceived_quantity, self.view().version))
        self.receipt(quantity=2, damaged_quantity=0, expected_version=1,
                     request_id='receipt-2', occurred_at='2026-10-08T21:30:00+08:00')
        current = self.view()
        self.assertEqual((4, 1, 0, 2), (current.received_quantity, current.damaged_quantity,
                                       current.unreceived_quantity, current.version))
        self.assertIn('未结清', current.conclusion)
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                       loan_transfer_id='loan-1').outstanding)
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
            squad_id=self.target_squad, item_id=self.item_id).available)
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(asdict(current), asdict(self.view()))

    def test_only_current_named_material_leader_not_deputy_captain_or_review_maintenance(self):
        self.state.registry.maintenance_phones.add(self.OTHER)
        before = self.files()
        for phone in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            with self.subTest(phone=phone), self.assertRaises(PermissionError):
                self.receipt(member_token=self.tokens[phone], loan_transfer_id='unknown')
            with self.assertRaises(PermissionError):
                self.view(member_token=self.tokens[phone])
        self.assertEqual(before, self.files())
        self.receipt()
        self.appoint(self.REPLACEMENT)
        with self.assertRaises(PermissionError):
            self.receipt(request_id='old-leader', expected_version=1)
        self.receipt(member_token=self.tokens[self.REPLACEMENT], request_id='new-leader',
                     expected_version=1, quantity=1, damaged_quantity=0)
        current = self.view(member_token=self.tokens[self.REPLACEMENT])
        self.assertEqual([self.LOGISTICS, self.REPLACEMENT], [r.actor_id for r in current.records])

    def test_invalid_quantities_versions_clocks_and_cross_event_references_write_nothing(self):
        before = self.files()
        invalid = [dict(quantity=q) for q in (0, -1, True, float('inf'), float('nan'), 5)]
        invalid += [dict(damaged_quantity=d) for d in (-1, 3, True, float('nan'))]
        invalid += [dict(expected_version=v) for v in (True, -1, 1, '0')]
        invalid += [dict(occurred_at='2026-10-08T21:00:00'), dict(location=''),
                    dict(custodian=''), dict(evidence=''), dict(request_id=' '),
                    dict(loan_transfer_id='other-event-loan')]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.receipt(**values)
            self.assertEqual(before, self.files())
        with self.assertRaises(ValueError):
            summary(self.event_space(), self.state.ledger,
                    event_id='another-event', loan_transfer_id='loan-1')

    def test_replay_after_later_batch_is_idempotent_but_conflicts_and_overreceipt_fail(self):
        first = self.receipt()
        self.receipt(quantity=1, damaged_quantity=0, request_id='receipt-2', expected_version=1)
        before = self.files()
        self.assertEqual(first, self.receipt())
        self.assertEqual(before, self.files())
        for values in (dict(quantity=1), dict(location='other'), dict(expected_version=1),
                       dict(request_id='receipt-3', expected_version=2, quantity=2)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.receipt(**values)
        self.assertEqual(before, self.files())

    def test_missing_mirrors_and_generic_commander_write_are_rejected(self):
        first = self.receipt()
        space = self.event_space()
        space.unlock_general_entry(self.auth.authenticate('p08-transfer-demo'))
        with self.assertRaises(PermissionError):
            space.record_object_revision(first, expected_version=0)
        broken = copy.deepcopy(space)
        broken.object_revisions.pop(first.id)
        with self.assertRaises(ValueError):
            summary(broken, self.state.ledger, event_id=self.event_id, loan_transfer_id='loan-1')
        broken = copy.deepcopy(space)
        broken._events.pop()
        with self.assertRaises(ValueError):
            summary(broken, self.state.ledger, event_id=self.event_id, loan_transfer_id='loan-1')

    def test_postwrite_failure_rolls_back_and_same_request_can_retry(self):
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous receipt post-write failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            with self.assertRaises(OSError):
                self.receipt()
        self.assertEqual(before, self.files())
        self.assertEqual(0, self.view().version)
        self.receipt()
        self.assertEqual(1, self.view().version)

    def test_interrupted_write_requires_restart_recovers_and_clears_sessions(self):
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop):
            with self.assertRaises(Interrupted):
                self.receipt()
        self.assertTrue(list((self.root / '.state-transactions').glob('txn-*')))
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.member_sessions)
        self.assertFalse(self.state.entry_unlocked)
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(0, self.view().version)
        self.receipt()
        self.assertEqual(1, self.view().version)


if __name__ == '__main__':
    unittest.main()
