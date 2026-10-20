"""D5: current batch replacements, invalid decisions, conflicts and recovery."""
from dataclasses import asdict
import json
import http.client
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace
from panda_brain import material_supply_receipts as supply_receipts
from . import test_upgrade_material_closed_closeout as closed
from . import test_upgrade_material_supply_receipts as supply
from . import test_upgrade_formal_task_feedback as task_feedback
from . import test_upgrade_material_custody_decision_page as active_custody


class Interrupted(BaseException):
    pass


class CustodyCorrectionTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT',
                 'new_state','approve','send','receive','event_space','make_loan_received',
                 'appoint','receipt','view','files','server','status','verify','settle','full',
                 'verify_form','settle_form','post_decision','page'):
        locals()[name] = getattr(closed.ClosedCustodyTests, name)

    def setUp(self):
        closed.ClosedCustodyTests.setUp(self)

    def correct(self, batch, **changes):
        return self.receipt(**(dict(corrects_id=batch.id, correction_reason='模拟错数更正',
            quantity=4, damaged_quantity=0, expected_version=1, request_id='correct-1') | changes))

    def invalidate(self, row, **changes):
        return self.state.handle_material_custody_invalidation(self.event_id,
            **(dict(member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1',
                corrects_id=row.id, correction_reason='模拟结清依据需要重核', evidence='模拟更正单',
                request_id='invalidate-1', expected_version=1, expected_receipt_version=1,
                expected_verification_version=1) | changes))

    def test_receipt_replacement_keeps_history_invalidates_affected_and_rechecks(self):
        old, verified = self.full()
        settled = self.settle()
        ledger = self.state.materials_path.read_bytes()
        registry = self.state.registry_path.read_bytes()
        corrected = self.correct(old, damaged_quantity=1)
        status = self.status()
        self.assertFalse(status.settled)
        self.assertEqual((corrected.id,), status.unverified_receipt_ids)
        self.assertEqual((old, corrected), status.receipt_summary.records)
        self.assertEqual((corrected,), status.receipt_summary.effective_records)
        self.assertEqual((4, 1, 0), (status.receipt_summary.received_quantity,
            status.receipt_summary.damaged_quantity, status.receipt_summary.unreceived_quantity))
        self.assertEqual((settled,), status.settlements)
        with self.assertRaises(ValueError):
            self.settle(expected_version=1, expected_receipt_version=2, request_id='settle-2')
        self.verify(corrected, expected_version=1, expected_receipt_version=2, request_id='verify-2')
        self.settle(expected_version=1, expected_receipt_version=2, expected_verification_version=2,
            request_id='settle-2', handling_evidence='模拟核实处理', handling_conclusion='模拟损坏隔离')
        self.assertTrue(self.status().settled)
        self.assertEqual(ledger, self.state.materials_path.read_bytes())
        self.assertEqual(registry, self.state.registry_path.read_bytes())
        self.assertEqual('closed', self.state.registry.events[self.event_id].status)
        before = asdict(self.status())
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(before, asdict(self.status()))

    def test_reduce_to_zero_and_restore_without_duplicate_add(self):
        old, _ = self.full()
        self.settle()
        corrected = self.correct(old, quantity=0)
        self.assertEqual((0, 4), (self.view().received_quantity, self.view().unreceived_quantity))
        latest = self.correct(corrected, expected_version=2, request_id='correct-2', quantity=3)
        self.assertEqual((3, 1), (self.view().received_quantity, self.view().unreceived_quantity))
        self.assertEqual((latest,), self.view().effective_records)
        before = self.files()
        for values in (dict(quantity=5), dict(quantity=-1), dict(damaged_quantity=5),
                       dict(expected_version=0), dict(correction_reason=''), dict(corrects_id='other')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.correct(latest, **(dict(expected_version=3, request_id='bad') | values))
        self.assertEqual(before, self.files())

    def test_unaffected_batch_verification_survives_but_old_batch_cannot_be_verified(self):
        first = self.receipt(damaged_quantity=0)
        v1 = self.verify(first)
        second = self.receipt(quantity=2, damaged_quantity=0, expected_version=1, request_id='receipt-2')
        self.verify(second, expected_version=1, expected_receipt_version=2, request_id='verify-2')
        self.settle(expected_receipt_version=2, expected_verification_version=2)
        corrected = self.correct(second, quantity=2, expected_version=2)
        self.assertEqual((v1,), self.status().effective_verifications)
        self.assertEqual((corrected.id,), self.status().unverified_receipt_ids)
        with self.assertRaises(ValueError):
            self.verify(second, expected_version=2, expected_receipt_version=3, request_id='bad')

    def test_verification_correction_invalidates_settlement_and_preserves_old_replay(self):
        batch, original = self.full()
        self.settle()
        revised = self.verify(batch, corrects_id=original.id, correction_reason='模拟核验更正',
            result='failed', expected_version=1, request_id='reverify-1')
        self.assertFalse(self.status().settled)
        self.assertEqual((batch.id,), self.status().not_passed_receipt_ids)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(original, self.verify(batch))
            self.assertEqual(revised, self.verify(batch, corrects_id=original.id, correction_reason='模拟核验更正',
                result='failed', expected_version=1, request_id='reverify-1'))
        self.assertEqual(before, self.files())

    def test_explicit_settlement_correction_requires_fresh_verification(self):
        batch, _ = self.full()
        settled = self.settle()
        invalid = self.invalidate(settled)
        self.assertFalse(self.status().settled)
        self.assertEqual((batch.id,), self.status().unverified_receipt_ids)
        with self.assertRaises(ValueError):
            self.settle(expected_version=2, request_id='settle-3')
        self.verify(batch, expected_version=1, request_id='verify-2')
        new = self.settle(expected_version=2, expected_verification_version=2, request_id='settle-3')
        self.assertEqual((settled, invalid, new), self.status().settlements)
        self.assertTrue(self.status().settled)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(invalid, self.invalidate(settled))
            self.assertEqual(settled, self.settle())
        self.assertEqual(before, self.files())

    def test_all_corrections_refuse_wrong_actor_and_changed_original_request(self):
        batch, verification = self.full()
        settled = self.settle()
        before = self.files()
        for actor in (self.DEPUTY,self.SOURCE,self.TARGET,self.OTHER):
            with self.assertRaises(PermissionError): self.correct(batch, member_token=self.tokens[actor])
            with self.assertRaises(PermissionError): self.invalidate(settled, member_token=self.tokens[actor])
            with self.assertRaises(PermissionError): self.verify(batch, member_token=self.tokens[actor], corrects_id=verification.id,
                correction_reason='模拟更正', expected_version=1, request_id='bad')
        self.assertEqual(before, self.files())
        self.correct(batch)
        before = self.files()
        with self.assertRaises(ValueError): self.correct(batch, location='不同位置')
        self.assertEqual(before, self.files())

    def test_http_correction_forms_replay_and_field_guards(self):
        batch, verification = self.full()
        self.settle()
        base = self.server()
        fields = dict(event_id=self.event_id, csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            loan_transfer_id='loan-1', corrects_id=batch.id, correction_reason='<模拟更正>', quantity='4', damaged_quantity='1',
            location='<模拟位置>', custodian='模拟人', evidence='<模拟依据>', request_id='http-correct', expected_version='1', occurred_at='')
        self.assertIn('/material-custody/correct', self.page())
        self.assertIn('/material-custody/invalidate', self.page())
        code, data = self.post_decision(base, 'correct', fields)
        self.assertEqual(200, code)
        self.assertIn('&lt;模拟位置&gt;', data['card_html'])
        self.assertNotIn('<模拟位置>', data['card_html'])
        self.assertIn('尚未核验', data['card_html'])
        self.assertIn(batch.id, data['card_html'])
        before = self.files()
        self.assertEqual(200, self.post_decision(base, 'correct', fields)[0])
        for change, code in ((dict(csrf='wrong'),403),(dict(corrects_id=verification.id),409),
                             (dict(expected_version='0'),409),(dict(correction_reason=''),409)):
            self.assertEqual(code, self.post_decision(base, 'correct', fields | change)[0])
        self.assertEqual(409, self.post_decision(base, 'correct', fields | dict(extra='bad'))[0])
        self.assertEqual(before, self.files())

    def test_postwrite_failures_and_restart_recovery_keep_old_settlement(self):
        batch, _ = self.full()
        settled = self.settle()
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous correction save failure')
        for operation in (lambda: self.correct(batch), lambda: self.invalidate(settled)):
            with patch('panda_brain.persistence.save_workspace', side_effect=fail), self.assertRaises(OSError): operation()
            self.assertEqual(before, self.files())
            self.assertTrue(self.status().settled)
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop), self.assertRaises(Interrupted):
            self.correct(batch)
        with self.assertRaises(RuntimeError): self.correct(batch)
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(before, self.files())
        self.assertTrue(self.status().settled)
        self.correct(batch)
        self.assertFalse(self.status().settled)

    def test_http_verification_correction_and_settlement_invalidation(self):
        batch, verification = self.full()
        self.settle()
        base = self.server()
        form = self.verify_form(batch, corrects_id=verification.id, correction_reason='模拟追加核验依据',
            expected_version='1', request_id='http-reverify')
        code, data = self.post_decision(base, 'reverify', form)
        self.assertEqual(200, code)
        self.assertFalse(self.status().settled)
        self.assertIn('全部核验旧版与更正来源', data['card_html'])
        self.assertIn(verification.id, data['card_html'])
        new = self.settle(expected_version=1, expected_verification_version=2, request_id='settle-2')
        invalidate = dict(event_id=self.event_id, csrf=form['csrf'], loan_transfer_id='loan-1',
            corrects_id=new.id, correction_reason='模拟结清依据更正', evidence='模拟追加依据',
            request_id='http-invalidate', expected_version='2', expected_receipt_version='1',
            expected_verification_version='2', occurred_at='')
        self.assertEqual(200, self.post_decision(base, 'invalidate', invalidate)[0])
        self.assertEqual((batch.id,), self.status().unverified_receipt_ids)
        before = self.files()
        self.assertEqual(200, self.post_decision(base, 'invalidate', invalidate)[0])
        self.assertEqual(403, self.post_decision(base, 'invalidate', invalidate | dict(csrf='wrong'))[0])
        self.assertEqual(409, self.post_decision(base, 'invalidate', invalidate | dict(confirm='yes'))[0])
        self.assertEqual(before, self.files())


class SupplyCorrectionTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT',
                 'new_state','event_space','appoint','view','report','files','space','status','plan',
                 'attach','review','verify','publish','act','receive','inventory','loss'):
        locals()[name] = getattr(supply.SupplyReceiptTests, name)

    def setUp(self):
        supply.SupplyReceiptTests.setUp(self)

    begin = task_feedback.FeedbackTests.begin
    feedback = task_feedback.FeedbackTests.feedback

    def correct(self, batch, **changes):
        return self.receive(**(dict(corrects_id=batch.id, correction_reason='模拟更正依据',
            expected_version=1, request_id='supply-correct-1', quantity=3, damaged_quantity=1) | changes))

    def test_effective_quantity_old_rows_and_no_duplicate_inventory(self):
        old = self.receive()
        corrected = self.correct(old)
        view = self.inventory()
        self.assertEqual((14,3,1,1), (view.book_available,view.supply_received,view.supply_damaged,view.supply_in_transit))
        self.assertEqual([corrected.id], [r.id for r,v in supply_receipts.history(self.space(),self.state.ledger)])
        self.assertEqual([old.id,corrected.id], [r.id for r,v in supply_receipts.history(self.space(),self.state.ledger,historical=True)])
        original = asdict(view)
        self.loss()
        self.assertEqual(13, self.inventory().book_available)
        self.state = self.new_state()
        self.assertEqual(13, self.inventory().book_available)

    def test_zero_correction_then_new_batch_and_stale_target_rejected(self):
        old = self.receive()
        corrected = self.correct(old, quantity=0, damaged_quantity=0)
        self.assertEqual((12,0,4),(self.inventory().book_available,self.inventory().supply_received,self.inventory().supply_in_transit))
        self.receive(quantity=4, expected_version=2, request_id='receipt-3')
        self.assertEqual(16,self.inventory().book_available)
        before = self.files()
        with self.assertRaises(ValueError): self.correct(old, expected_version=3, request_id='stale')
        with self.assertRaises(ValueError): self.correct(corrected, quantity=1, damaged_quantity=0,expected_version=3,request_id='overflow')
        self.assertEqual(before,self.files())

    def test_conflict_after_loss_refuses_even_safe_looking_increase(self):
        old = self.receive()
        self.loss()
        before = self.files()
        with self.assertRaises(ValueError): self.correct(old,quantity=4,damaged_quantity=0)
        self.assertEqual(before,self.files())
        self.assertEqual(13,self.inventory().book_available)

    def test_conflict_after_transfer_approval_or_receiver_departure(self):
        old = self.receive()
        self.state.handle_manual_material_transfer(self.event_id,member_token=self.tokens[self.DEPUTY],stage='approved',
            transfer_id='used-supply',request_id='used-supply-1',evidence='模拟调拨',item_id=self.item_id,
            from_squad=self.target_squad,to_squad=self.source_squad,quantity='1')
        before = self.files()
        with self.assertRaises(ValueError):self.correct(old)
        self.assertEqual(before,self.files())

    def test_conflict_after_receiver_departure(self):
        old = self.receive()
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad,evidence='模拟撤离',request_id='supply-used-withdrawal')
        before = self.files()
        with self.assertRaises(ValueError):self.correct(old)
        self.assertEqual(before,self.files())

    def test_conflict_after_explicit_task_verification_preserves_closed_task(self):
        from panda_brain.formal_task_receipts import published
        from panda_brain.task_relations import reference
        from panda_brain import formal_task_feedback as feedback
        self.ref=reference(next(iter(published(self.space()).values())))
        old=self.receive(quantity=4)
        self.begin();self.feedback('completed');self.feedback('verify')
        before=self.files()
        with self.assertRaises(ValueError):self.correct(old)
        self.assertEqual(before,self.files())
        self.assertTrue(feedback.verified(self.space(),self.state.ledger,self.ref['order_id']))

    def test_closed_event_replacement_and_original_replay_no_save(self):
        old = self.receive()
        self.state.handle_event_action(self.event_id,'close')
        corrected = self.correct(old)
        before = self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay saved')):
            self.assertEqual(old,self.receive())
            self.assertEqual(corrected,self.correct(old))
        self.assertEqual(before,self.files())
        self.assertEqual('closed',self.state.registry.events[self.event_id].status)

    def test_two_file_postwrite_failure_interruption_and_retry(self):
        old = self.receive()
        before = self.files()
        def fail(space,target):
            save_workspace(space,target)
            raise OSError('anonymous supply correction failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=fail),self.assertRaises(OSError):self.correct(old)
        self.assertEqual(before,self.files())
        def stop(space,target):
            save_workspace(space,target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=stop),self.assertRaises(Interrupted):self.correct(old)
        self.state=self.new_state()
        self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456')
        self.assertEqual(before,self.files())
        self.correct(old)
        self.assertEqual(14,self.inventory().book_available)

    def test_permissions_versions_quantities_and_source_deletion(self):
        old = self.receive()
        before=self.files()
        for values in (dict(quantity=-1),dict(quantity=5),dict(damaged_quantity=4),dict(correction_reason=''),dict(expected_version=0),dict(corrects_id='other')):
            with self.subTest(values=values),self.assertRaises(ValueError):self.correct(old,**values)
        with self.assertRaises(PermissionError):self.correct(old,member_token=self.tokens[self.DEPUTY])
        self.assertEqual(before,self.files())
        self.correct(old)
        self.state.ledger.supply_receipts.pop(old.id)
        with self.assertRaises(ValueError):self.inventory()

    def test_actual_http_supply_correction_preserves_history_and_replay(self):
        from panda_brain.panel import make_server
        old=self.receive()
        self.state.handle_event_action(self.event_id,'close')
        code,page=render_page(self.state,'/material-supply/receipts',member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200,code)
        self.assertIn('name="corrects_id"',page)
        server=make_server(self.state,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(event_id=self.event_id,csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            order_id=self.order_id,corrects_id=old.id,correction_reason='<模拟理由>',expected_version='1',
            request_id='http-supply-correction',squad_id=self.target_squad,location='<模拟位置>',
            quantity='3',damaged_quantity='1',basis='<模拟更正依据>')
        def post(values,person=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
            try:
                conn.request('POST','/material-supply/receipts',urlencode(values),headers={
                    'Cookie':'member_session='+self.tokens[person or self.LOGISTICS],
                    'Content-Type':'application/x-www-form-urlencoded'})
                response=conn.getresponse();response.read();return response.status
            finally:conn.close()
        self.assertEqual(303,post(fields))
        self.assertEqual((3,1),(self.inventory().supply_received,self.inventory().supply_in_transit))
        before=self.files()
        self.assertEqual(303,post(fields))
        for values,status in ((dict(csrf='wrong'),403),(dict(expected_version='0'),409),
                              (dict(correction_reason=''),409),(dict(extra='bad'),409)):
            self.assertEqual(status,post(fields|values))
        self.assertEqual(403,post(fields,person=self.DEPUTY))
        self.assertEqual(before,self.files())
        page=render_page(self.state,'/material-supply/receipts',member_token=self.tokens[self.LOGISTICS])[1]
        self.assertIn('&lt;模拟位置&gt;',page)
        self.assertNotIn('<模拟位置>',page)
        self.assertIn(old.id,page)


class UsedSettlementTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT',
                 'new_state','approve','send','receive','event_space','make_loan_received',
                 'appoint','receipt','view','files','status','verify','settle','full','correct','invalidate'):
        locals()[name]=getattr(CustodyCorrectionTests,name)

    def setUp(self):
        active_custody.CustodyDecisionPageTests.setUp(self)

    def test_lender_departure_freezes_settlement_and_all_corrections_refuse(self):
        batch,verified=self.full();settled=self.settle()
        self.state.handle_team_material_withdrawal(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,evidence='模拟借出队撤离',request_id='used-settlement-departure')
        self.state.handle_event_action(self.event_id,'close')
        before=self.files()
        for operation in (lambda:self.correct(batch),lambda:self.invalidate(settled),
            lambda:self.verify(batch,corrects_id=verified.id,correction_reason='模拟更正',expected_version=1,request_id='bad')):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(before,self.files())
        self.assertTrue(self.status().settled)
