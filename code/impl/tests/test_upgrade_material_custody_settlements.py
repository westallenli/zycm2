"""Anonymous post-departure verification and settlement; no inventory mutation."""
import copy
from dataclasses import asdict, replace
import json
import unittest
from unittest.mock import patch

from panda_brain import material_custody_settlements as decisions
from panda_brain.material_transfers import loan_status, transfer_balance
from panda_brain.persistence import save_workspace
from panda_brain.panel import PanelActionError
from . import test_upgrade_material_custody_receipts as fixture


class CustodySettlementTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files'):
        locals()[name] = getattr(fixture.CustodyReceiptTests, name)

    def setUp(self):
        fixture.CustodyReceiptTests.setUp(self)

    def status(self, **changes):
        return self.state.material_custody_settlement(self.event_id,
            **(dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1') | changes))

    def verify(self, batch, **changes):
        values = dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1',
            receipt_id=batch.id, result='passed', conclusion='匿名逐件核对相符',
            evidence='模拟核验清单', request_id='verify-1', expected_version=0,
            expected_receipt_version=1)
        return self.state.handle_material_custody_verification(self.event_id, **(values | changes))

    def settle(self, **changes):
        values = dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1',
            evidence='模拟整笔结清确认', request_id='settle-1', expected_version=0,
            expected_receipt_version=1, expected_verification_version=1)
        return self.state.handle_material_custody_settlement(self.event_id, **(values | changes))

    def full(self, damaged=0):
        batch = self.receipt(quantity=4, damaged_quantity=damaged)
        verification = self.verify(batch)
        return batch, verification

    def test_partial_full_and_latest_verification_require_separate_settlement(self):
        first = self.receipt(damaged_quantity=0)
        passed = self.verify(first)
        before = self.files()
        with self.assertRaises(ValueError):
            self.settle()
        self.assertEqual(before, self.files())
        second = self.receipt(quantity=2, damaged_quantity=0, expected_version=1, request_id='receipt-2')
        self.assertEqual((second.id,), self.status().unverified_receipt_ids)
        with self.assertRaises(ValueError):
            self.settle(expected_receipt_version=2)
        for index, result in enumerate(('unknown', 'failed', 'passed'), 1):
            self.verify(second, result=result, request_id='verify-' + str(index + 1),
                        expected_version=index, expected_receipt_version=2)
            current = self.status()
            self.assertEqual((), current.unverified_receipt_ids)
            self.assertFalse(current.settled)
            if result != 'passed':
                self.assertEqual((second.id,), current.not_passed_receipt_ids)
                with self.assertRaises(ValueError):
                    self.settle(expected_receipt_version=2, expected_verification_version=index + 1)
        self.verify(first, result='failed', request_id='verify-5', expected_version=4,
                    expected_receipt_version=2)
        self.assertEqual((first.id,), self.status().not_passed_receipt_ids)
        with self.assertRaises(ValueError):
            self.settle(expected_receipt_version=2, expected_verification_version=5)
        latest = self.verify(first, request_id='verify-6', expected_version=5, expected_receipt_version=2)
        settlement = self.settle(expected_receipt_version=2, expected_verification_version=6)
        current = self.status()
        self.assertTrue(current.settled)
        self.assertEqual(6, len(current.verifications))
        self.assertEqual(latest.id, current.effective_verifications[0].id)
        self.assertEqual(passed.id, current.verifications[0].id)
        frozen = json.loads(settlement.content_json)
        self.assertEqual([first.id, second.id], frozen['receipt_ids'])
        self.assertEqual([r.id for r in current.effective_verifications], frozen['verification_ids'])

    def test_damage_requires_handling_but_preserves_ledger_departure_and_restart(self):
        ledger = self.state.ledger.to_dict()
        original = self.files()[self.state.materials_path]
        departure = [r for r in self.event_space().object_revisions.values()
                     if r.reference.object_kind == 'team_material_withdrawal']
        self.full(damaged=1)
        before = self.files()
        for values in ({}, dict(handling_evidence='已核对模拟处理记录'),
                       dict(handling_conclusion='模拟处置已核实')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.settle(**values)
            self.assertEqual(before, self.files())
        self.settle(handling_evidence='已核对模拟处理记录', handling_conclusion='模拟隔离保管，不计可用')
        current = self.status()
        self.assertTrue(current.settled)
        self.assertEqual(1, current.receipt_summary.damaged_quantity)
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(original, self.state.materials_path.read_bytes())
        self.assertEqual(departure, [r for r in self.event_space().object_revisions.values()
                                    if r.reference.object_kind == 'team_material_withdrawal'])
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                       loan_transfer_id='loan-1').outstanding)
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
            squad_id=self.target_squad, item_id=self.item_id).available)
        self.state = self.new_state()
        self.assertFalse(self.state.entry_unlocked)
        self.assertFalse(self.state.member_sessions)
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        before = self.files()
        self.assertEqual(asdict(current), asdict(self.status()))
        self.assertEqual(before, self.files())

    def test_same_receiver_may_verify_no_damage_needs_no_handling(self):
        batch, verification = self.full()
        settlement = self.settle()
        self.assertEqual([self.LOGISTICS] * 3, [r.actor_id for r in (batch, verification, settlement)])
        self.assertTrue(self.status().settled)
        self.assertEqual('', verification.times.occurred_at)
        self.assertEqual('reported', verification.data_kind)
        self.assertEqual('', json.loads(settlement.content_json)['handling_evidence'])

    def test_current_named_authority_replacement_keeps_historical_actor(self):
        batch, old = self.full()
        self.state.registry.maintenance_phones.add(self.OTHER)
        for phone in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            before = self.files()
            with self.subTest(phone=phone), self.assertRaises(PermissionError):
                self.verify(batch, member_token=self.tokens[phone])
            with self.assertRaises(PermissionError):
                self.settle(member_token=self.tokens[phone])
            with self.assertRaises(PermissionError):
                self.status(member_token=self.tokens[phone])
            self.assertEqual(before, self.files())
        self.appoint(self.REPLACEMENT)
        with self.assertRaises(PermissionError):
            self.verify(batch)  # Even an original replay requires current authority.
        with self.assertRaises(PermissionError):
            self.settle()
        current = self.verify(batch, member_token=self.tokens[self.REPLACEMENT],
                              expected_version=1, request_id='verify-new')
        settlement = self.settle(member_token=self.tokens[self.REPLACEMENT], expected_verification_version=2)
        summary = self.status(member_token=self.tokens[self.REPLACEMENT])
        self.assertEqual([self.LOGISTICS, self.REPLACEMENT], [r.actor_id for r in summary.verifications])
        self.assertEqual(current.id, summary.effective_verifications[0].id)
        self.assertEqual(self.REPLACEMENT, settlement.actor_id)
        self.assertEqual(old.id, summary.verifications[0].id)

    def test_replay_after_later_action_and_settlement_is_zero_write_conflicts_fail(self):
        batch, original = self.full()
        self.verify(batch, expected_version=1, request_id='verify-2')
        settlement = self.settle(expected_verification_version=2)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('must not save')):
            self.assertEqual(original, self.verify(batch))
            self.assertEqual(settlement, self.settle(expected_verification_version=2))
            self.assertEqual(batch, self.receipt(quantity=4, damaged_quantity=0))
        for values in (dict(conclusion='other'), dict(expected_version=1), dict(result='failed')):
            with self.assertRaises(ValueError):
                self.verify(batch, **values)
        with self.assertRaises(ValueError):
            self.settle(evidence='other', expected_verification_version=2)
        self.assertEqual(before, self.files())

    def test_terminal_state_rejects_new_verification_settlement_and_receipt(self):
        batch, _ = self.full()
        self.settle()
        before = self.files()
        with self.assertRaises(ValueError):
            self.verify(batch, expected_version=1, request_id='new-verification')
        with self.assertRaises(ValueError):
            self.settle(expected_version=1, request_id='new-settlement')
        with self.assertRaises(ValueError):
            self.receipt(quantity=1e-10, damaged_quantity=0, expected_version=1, request_id='new-receipt')
        self.assertEqual(before, self.files())

    def test_versions_sources_clocks_and_cross_references_reject_zero_write(self):
        batch, _ = self.full()
        before = self.files()
        values = [dict(expected_version=v) for v in (True, -1, '1', 0)]
        values += [dict(expected_receipt_version=v) for v in (True, -1, 0, 2)]
        values += [dict(receipt_id='wrong-batch'), dict(loan_transfer_id='wrong-loan'),
                   dict(result='verified'), dict(conclusion=''), dict(evidence=' '),
                   dict(request_id='receipt-1'), dict(occurred_at='2026-10-09T12:00:00')]
        for change in values:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(batch, **(dict(expected_version=1, request_id='verify-new') | change))
            self.assertEqual(before, self.files())
        for change in (dict(expected_receipt_version=0), dict(expected_verification_version=0),
                       dict(expected_version=True), dict(evidence=''), dict(request_id='verify-1'),
                       dict(loan_transfer_id='wrong-loan'), dict(handling_conclusion=' text ')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.settle(**change)
            self.assertEqual(before, self.files())
        with self.assertRaises(ValueError):
            decisions.summary(self.event_space(), self.state.ledger,
                              event_id='wrong-event', loan_transfer_id='loan-1')

    def test_missing_extra_mirrors_and_frozen_reference_tampering_fail_closed(self):
        _, verification = self.full()
        settlement = self.settle()
        space = self.event_space()
        for record in (verification, settlement):
            broken = copy.deepcopy(space)
            broken.object_revisions.pop(record.id)
            with self.assertRaises(ValueError):
                decisions.summary(broken, self.state.ledger, event_id=self.event_id, loan_transfer_id='loan-1')
            broken = copy.deepcopy(space)
            broken.append_event('ObjectRevisionRecorded', revision_id=record.id, revision=asdict(record))
            with self.assertRaises(ValueError):
                decisions.summary(broken, self.state.ledger, event_id=self.event_id, loan_transfer_id='loan-1')
        # Rebuild a valid hash chain with a mismatched frozen settlement reference.
        for change in (dict(verification_ids=[]), dict(receipt_ids=['wrong']),
                       dict(withdrawal_id='wrong'), dict(damaged_quantity=1)):
            broken = copy.deepcopy(space)
            broken._events.pop()
            altered = replace(settlement, content_json=json.dumps(json.loads(settlement.content_json) | change))
            broken.object_revisions[altered.id] = altered
            broken.append_event('ObjectRevisionRecorded', revision_id=altered.id, revision=asdict(altered))
            self.assertEqual((), broken.verify_audit_chain())
            with self.assertRaises(ValueError):
                decisions.summary(broken, self.state.ledger, event_id=self.event_id, loan_transfer_id='loan-1')

    def test_generic_commander_cannot_bypass_either_dedicated_kind(self):
        _, verification = self.full()
        settlement = self.settle()
        space = self.event_space()
        space.unlock_general_entry(self.auth.authenticate('p08-transfer-demo'))
        for record in (verification, settlement):
            with self.assertRaises(PermissionError):
                space.record_object_revision(record, expected_version=0)

    def test_closed_event_allows_existing_material_replay_and_upgraded_can_continue(self):
        batch, _ = self.full()
        self.state.registry.transition(self.event_id, 'upgrade')
        self.state.registry.save(self.state.registry_path)
        settlement = self.settle()
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        self.verify(batch)
        self.assertEqual(settlement, self.settle())
        with self.assertRaises(PanelActionError):
            self.state._appointment_space(self.event_id, for_write=True)
        self.assertEqual(before, self.files())

    def test_active_workspace_save_failure_and_replay_keep_memory_and_snapshot_consistent(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        batch, verification = self.full()
        before = self.files()
        memory = copy.deepcopy(self.state.workspace.object_revisions)
        with patch.object(self.state, 'persist_if_db', side_effect=OSError('anonymous active save failure')):
            with self.assertRaises(OSError):
                self.settle()
        self.assertEqual(before, self.files())
        self.assertEqual(memory, self.state.workspace.object_revisions)
        settlement = self.settle()
        self.assertIn(settlement.id, self.state.workspace.object_revisions)
        before = self.files()
        with patch.object(self.state, 'persist_if_db', side_effect=AssertionError('replay must not persist')):
            self.assertEqual(verification, self.verify(batch))
            self.assertEqual(settlement, self.settle())
        self.assertEqual(before, self.files())

    def test_postwrite_failure_rolls_back_both_actions_and_retry_succeeds(self):
        batch = self.receipt(quantity=4, damaged_quantity=0)
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous decision post-write failure')
        for action in (lambda: self.verify(batch), self.settle):
            before = self.files()
            with patch('panda_brain.persistence.save_workspace', side_effect=fail):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual(before, self.files())
            action()
        self.assertTrue(self.status().settled)

    def test_interrupted_settlement_restart_recovers_before_retry(self):
        self.full()
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise fixture.Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop):
            with self.assertRaises(fixture.Interrupted):
                self.settle()
        self.assertTrue(list((self.root / '.state-transactions').glob('txn-*')))
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.member_sessions)
        self.assertFalse(self.state.entry_unlocked)
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertFalse(self.status().settled)
        self.settle()
        self.assertTrue(self.status().settled)


if __name__ == '__main__':
    unittest.main()
