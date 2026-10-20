"""Named transfer, borrowing, and withdrawal reports with audit mirrors."""

import json
from contextlib import nullcontext
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.handover_sop import handover_view, render_handover
from panda_brain.manual_material_transfers import RECORDED
from panda_brain.material_transfers import (transfer_balance, transfer_in_transit,
                                            loan_status)
from panda_brain.materials import MaterialsLedger
from panda_brain.material_withdrawals import (REPORTED as WITHDRAWAL_REPORTED,
                                              BorrowedCustody, WithdrawalException)
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class NamedTransferTests(unittest.TestCase):
    DUTY = '13977773001'
    DEPUTY = '13977773002'
    SOURCE = '13977773003'
    TARGET = '13977773004'
    OTHER = '13977773005'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        snap = patch.object(wb, 'SNAPSHOT_DIR', self.root / 'snapshots')
        snap.start()
        self.addCleanup(snap.stop)
        self.auth = GeneralEntryAuth(secret='p08-transfer-demo')
        self.state = self.new_state()
        people = (
            PersonRecord(name='模拟值班员', phone=self.DUTY, unit='值班室'),
            PersonRecord(name='模拟副总指挥', phone=self.DEPUTY, unit='前指'),
            PersonRecord(name='转出队长', phone=self.SOURCE, unit='一中队', is_leader=True),
            PersonRecord(name='接收队长', phone=self.TARGET, unit='二中队', is_leader=True),
            PersonRecord(name='其他成员', phone=self.OTHER, unit='一中队'),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            self.tokens[person.phone], error = self.state.member_login(person.phone, '123456')
            self.assertEqual('', error)
        event = self.state.registry.create_event(
            name='模拟调拨事件', report_no='P08-TRANSFER',
            discovered_at='2026-09-30T08:00:00+08:00',
            source='人工模拟登记', location='模拟北段',
        )
        self.event_id = event.event_id
        self.source_squad = self.state.ledger.add_squad(
            name='一中队', member_phones=(self.SOURCE, self.OTHER),
            leader_phone=self.SOURCE, library=self.state.registry.library,
        ).squad_id
        self.target_squad = self.state.ledger.add_squad(
            name='二中队', member_phones=(self.TARGET,),
            leader_phone=self.TARGET, library=self.state.registry.library,
        ).squad_id
        self.item_id = self.state.ledger.upsert_item(name='模拟水带', unit='盘').item_id
        self.state.ledger.set_carry_line(self.source_squad, self.item_id, quantity=10)
        self.state.ledger.set_carry_line(self.target_squad, self.item_id, quantity=2)
        self.state._save_ledger()
        self.state.handle_event_action(self.event_id, 'start', duty_token=self.tokens[self.DUTY])
        qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                     if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='模拟任命依据', request_id='appoint-p08-transfer',
        )
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.source_squad, self.target_squad],
            channel='phone', request_id='select-p08-transfer',
        )
        for squad_id, captain in ((self.source_squad, self.SOURCE),
                                  (self.target_squad, self.TARGET)):
            self.state.handle_captain_scan(
                self.event_id, captain_token=self.tokens[captain],
                squad_id=squad_id, qr_id=qr_id,
            )
            self.state.handle_captain_arrival(
                self.event_id, captain_token=self.tokens[captain],
                squad_id=squad_id, mode='initial', phones=[],
                request_id=f'arrival-{squad_id}',
            )

    def new_state(self):
        return PanelState(
            ScenarioWorkspace('p08-transfer', general_auth=self.auth), auth=self.auth,
            registry_path=self.root / 'registry.json',
            materials_path=self.root / 'materials.json',
            reviews_path=self.root / 'reviews.json',
            alarm_inbox_path=self.root / 'alarms.json',
            duty_subjects=(self.DUTY,),
        )

    def approve(self, **changes):
        args = dict(member_token=self.tokens[self.DEPUTY], stage='approved',
                    transfer_id='transfer-1', request_id='approve-transfer-1',
                    evidence='线下调拨确认', item_id=self.item_id,
                    from_squad=self.source_squad, to_squad=self.target_squad,
                    quantity='4')
        args.update(changes)
        return self.state.handle_manual_material_transfer(self.event_id, **args)

    def send(self, **changes):
        args = dict(member_token=self.tokens[self.SOURCE], stage='sent',
                    transfer_id='transfer-1', request_id='send-transfer-1',
                    evidence='实际交出单', expected_version='1')
        args.update(changes)
        return self.state.handle_manual_material_transfer(self.event_id, **args)

    def receive(self, **changes):
        args = dict(member_token=self.tokens[self.TARGET], stage='received',
                    transfer_id='transfer-1', request_id='receive-transfer-1',
                    evidence='现场实收单', expected_version='2', quantity='2')
        args.update(changes)
        return self.state.handle_manual_material_transfer(self.event_id, **args)

    def event_space(self):
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        return load_workspace(target, general_auth=self.auth)

    def test_named_stages_have_matching_ledger_and_event_mirrors(self):
        approved = self.approve()
        self.assertEqual(self.DEPUTY, approved.actor_id)
        self.assertEqual(0, transfer_in_transit(self.state.ledger, event_id=self.event_id,
                                                item_id=self.item_id))
        sent = self.send()
        self.assertEqual(self.SOURCE, sent.actor_id)
        self.assertEqual(4, transfer_in_transit(self.state.ledger, event_id=self.event_id,
                                                item_id=self.item_id))
        self.assertEqual(6, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.source_squad, item_id=self.item_id).available)
        received = self.receive()
        self.assertEqual(self.TARGET, received.actor_id)
        self.assertEqual(4, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.target_squad, item_id=self.item_id).available)
        self.assertEqual(3, sum(event.kind == RECORDED for event in self.event_space().events))
        self.assertEqual((), self.event_space().verify_audit_chain())
        self.assertEqual(self.state.ledger.to_dict(), MaterialsLedger.load(self.state.materials_path).to_dict())

    def test_wrong_actor_and_unselected_team_rejected_without_write(self):
        before = self.state.ledger.to_dict()
        with self.assertRaises(PermissionError):
            self.approve(member_token=self.tokens[self.OTHER])
        self.assertEqual(before, self.state.ledger.to_dict())
        self.approve()
        before = self.state.ledger.to_dict()
        for token in (self.tokens[self.TARGET], self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.send(member_token=token)
        self.assertEqual(before, self.state.ledger.to_dict())

    def test_same_request_repairs_missing_workspace_mirror(self):
        self.approve()
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(2, len(MaterialsLedger.load(self.state.materials_path).transfer_records))
        self.assertEqual(1, sum(event.kind == RECORDED for event in self.event_space().events))
        self.state = self.new_state()
        self.tokens[self.SOURCE], error = self.state.member_login(self.SOURCE, '123456')
        self.assertEqual('', error)
        self.send()
        self.assertEqual(2, sum(event.kind == RECORDED for event in self.event_space().events))

    def test_ledger_save_failure_keeps_both_stores_unchanged(self):
        before = self.state.ledger.to_dict()
        with patch.object(MaterialsLedger, 'save', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.approve()
        self.assertEqual(before, self.state.ledger.to_dict())
        self.assertEqual(0, sum(event.kind == RECORDED for event in self.event_space().events))

    def test_named_page_and_material_balances_follow_transfer_stages(self):
        commander = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        for phone in (self.DEPUTY, self.SOURCE, self.TARGET):
            status, page = render_page(self.state, '/material-transfers',
                                       member_token=self.tokens[phone])
            self.assertEqual(200, status)
            if phone == self.DEPUTY:
                self.assertIn('登记调拨确认', page)
            else:
                self.assertNotIn('登记调拨确认', page)
        status, paused = render_page(self.state, '/material-transfers', member_token=self.tokens[self.OTHER])
        self.assertEqual(200, status)
        self.assertIn('独立工作台已暂停', paused)
        self.assertNotIn('登记调拨确认', paused)
        self.approve()
        status, source_page = render_page(self.state, '/material-transfers',
                                          member_token=self.tokens[self.SOURCE])
        self.assertEqual(200, status)
        self.assertIn('报告实际交出', source_page)
        self.send()
        status, target_page = render_page(self.state, '/material-transfers',
                                          member_token=self.tokens[self.TARGET])
        self.assertEqual(200, status)
        self.assertIn('报告本次实收', target_page)
        self.receive()
        status, materials = render_page(self.state, '/materials', session_token=commander)
        self.assertEqual(200, status)
        self.assertIn('当前可用含已实收、扣已交出和耗损；在途不计', materials)
        self.assertIn('>6</td>', materials)
        self.assertIn('>4</td>', materials)

    def test_http_transfer_report_rejects_wrong_actor_and_accepts_named_stages(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(type(
            'NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())

        def post(phone, **form):
            request = urllib.request.Request(
                base + '/material-transfers/report',
                data=urllib.parse.urlencode(dict(event_id=self.event_id, **form)).encode(),
                headers={'Cookie': f'member_session={self.tokens[phone]}'})
            try:
                return opener.open(request).status
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code
                finally:
                    exc.close()

        approval = dict(stage='approved', transfer_id='http-transfer',
                        request_id='http-approve', evidence='线下批准单',
                        item_id=self.item_id, from_squad=self.source_squad,
                        to_squad=self.target_squad, quantity='4')
        self.assertEqual(403, post(self.OTHER, **approval))
        self.assertEqual(303, post(self.DEPUTY, **approval))
        sent = dict(stage='sent', transfer_id='http-transfer',
                    request_id='http-sent', evidence='实际交出单', expected_version='1')
        self.assertEqual(403, post(self.TARGET, **sent))
        self.assertEqual(303, post(self.SOURCE, **sent))
        received = dict(stage='received', transfer_id='http-transfer',
                        request_id='http-received', evidence='实际实收单',
                        expected_version='2', quantity='4')
        self.assertEqual(303, post(self.TARGET, **received))
        self.assertEqual(0, transfer_in_transit(self.state.ledger, event_id=self.event_id,
                                                 item_id=self.item_id))
        self.assertEqual(3, sum(event.kind == RECORDED for event in self.event_space().events))

    def make_loan_received(self):
        self.approve(transfer_id='loan-1', request_id='approve-loan-1',
                     purpose='loan', evidence='借用批准依据')
        self.send(transfer_id='loan-1', request_id='send-loan-1')
        self.receive(transfer_id='loan-1', request_id='receive-loan-1', quantity='4')

    def approve_return(self, **changes):
        args = dict(member_token=self.tokens[self.DEPUTY], stage='approved',
                    transfer_id='return-1', request_id='approve-return-1',
                    evidence='归还批准依据', item_id=self.item_id,
                    from_squad=self.target_squad, to_squad=self.source_squad,
                    quantity='3', purpose='return', return_for='loan-1')
        args.update(changes)
        return self.state.handle_manual_material_transfer(self.event_id, **args)

    def test_named_loan_and_return_have_event_mirrors_and_damage_balance(self):
        self.make_loan_received()
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').borrowed)
        self.approve_return()
        self.send(transfer_id='return-1', request_id='send-return-1',
                  member_token=self.tokens[self.TARGET])
        result = self.receive(transfer_id='return-1', request_id='receive-return-1',
                              member_token=self.tokens[self.SOURCE], quantity='3',
                              damaged_quantity='1')
        self.assertEqual(self.SOURCE, result.actor_id)
        self.assertEqual(1, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)
        self.assertEqual((8, 3), (
            transfer_balance(self.state.ledger, event_id=self.event_id,
                             squad_id=self.source_squad, item_id=self.item_id).available,
            transfer_balance(self.state.ledger, event_id=self.event_id,
                             squad_id=self.target_squad, item_id=self.item_id).available))
        self.assertEqual(6, sum(event.kind == RECORDED for event in self.event_space().events))
        self.assertEqual((), self.event_space().verify_audit_chain())
        self.assertEqual(self.state.ledger.to_dict(), MaterialsLedger.load(self.state.materials_path).to_dict())

    def test_return_wrong_session_and_damage_on_loan_receipt_rejected(self):
        self.approve(transfer_id='loan-1', request_id='approve-loan-1', purpose='loan')
        self.send(transfer_id='loan-1', request_id='send-loan-1')
        before = self.state.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.receive(transfer_id='loan-1', request_id='receive-loan-1',
                         quantity='4', damaged_quantity='1')
        self.assertEqual(before, self.state.ledger.to_dict())
        self.receive(transfer_id='loan-1', request_id='receive-loan-1', quantity='4')
        with self.assertRaises(PermissionError):
            self.approve_return(member_token=self.tokens[self.SOURCE])
        with self.assertRaises(PermissionError):
            self.approve_return(member_token=self.tokens[self.SOURCE],
                                return_for='unknown-loan')
        self.approve_return()
        before = self.state.ledger.to_dict()
        with self.assertRaises(PermissionError):
            self.send(transfer_id='return-1', request_id='send-return-1',
                      member_token=self.tokens[self.SOURCE])
        self.assertEqual(before, self.state.ledger.to_dict())

    def test_return_waits_for_missing_loan_mirror_and_same_request_repairs(self):
        self.approve(transfer_id='loan-1', request_id='approve-loan-1', purpose='loan')
        self.send(transfer_id='loan-1', request_id='send-loan-1')
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.receive(transfer_id='loan-1', request_id='receive-loan-1',
                             quantity='4')
        before = self.state.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.approve_return()
        self.assertEqual(before, self.state.ledger.to_dict())
        self.receive(transfer_id='loan-1', request_id='receive-loan-1', quantity='4')
        self.approve_return()
        self.assertEqual(4, sum(event.kind == RECORDED for event in self.event_space().events))

    def test_return_approval_same_request_repairs_its_missing_mirror(self):
        self.make_loan_received()
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.approve_return()
        self.assertEqual(4, len(MaterialsLedger.load(self.state.materials_path).transfer_records))
        self.assertEqual(3, sum(event.kind == RECORDED for event in self.event_space().events))
        self.approve_return()
        self.assertEqual(4, sum(event.kind == RECORDED for event in self.event_space().events))
        self.assertEqual(3, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').return_approved)

    def test_page_exposes_return_only_to_deputy_and_damage_to_receiving_captain(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        deputy_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.DEPUTY])[1]
        self.assertIn('value="loan"', deputy_page)
        self.assertNotIn('登记归还确认', deputy_page)
        self.make_loan_received()
        deputy_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.DEPUTY])[1]
        self.assertIn('登记归还确认', deputy_page)
        self.assertIn('name="return_for" value="loan-1"', deputy_page)
        self.assertIn(f'name="from_squad" value="{self.target_squad}"', deputy_page)
        self.assertIn(f'name="to_squad" value="{self.source_squad}"', deputy_page)
        source_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.SOURCE])[1]
        self.assertNotIn('登记归还确认', source_page)
        self.approve_return(quantity='4')
        self.send(transfer_id='return-1', request_id='send-return-1',
                  member_token=self.tokens[self.TARGET])
        source_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.SOURCE])[1]
        self.assertIn('name="damaged_quantity"', source_page)
        target_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.TARGET])[1]
        self.assertNotIn('name="damaged_quantity"', target_page)
        self.receive(transfer_id='return-1', request_id='receive-return-1',
                     member_token=self.tokens[self.SOURCE], quantity='4',
                     damaged_quantity='1')
        deputy_page = render_page(self.state, '/material-transfers',
                                  member_token=self.tokens[self.DEPUTY])[1]
        self.assertIn('累计归还损坏 1', deputy_page)
        self.assertNotIn('登记归还确认', deputy_page)

    def test_http_loan_return_reports_damage_and_rejects_invalid_receipt(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(type(
            'NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())

        def post(phone, **form):
            request = urllib.request.Request(
                base + '/material-transfers/report',
                data=urllib.parse.urlencode(dict(event_id=self.event_id, **form)).encode(),
                headers={'Cookie': f'member_session={self.tokens[phone]}'})
            try:
                return opener.open(request).status
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code
                finally:
                    exc.close()

        loan = dict(stage='approved', transfer_id='http-loan', request_id='http-loan-a',
                    evidence='借用批准', item_id=self.item_id,
                    from_squad=self.source_squad, to_squad=self.target_squad,
                    quantity='4', purpose='loan')
        self.assertEqual(403, post(self.OTHER, **loan))
        self.assertEqual(303, post(self.DEPUTY, **loan))
        self.assertEqual(303, post(self.SOURCE, stage='sent', transfer_id='http-loan',
                                   request_id='http-loan-s', evidence='借出交出',
                                   expected_version='1'))
        self.assertEqual(303, post(self.TARGET, stage='received', transfer_id='http-loan',
                                   request_id='http-loan-r', evidence='借出实收',
                                   expected_version='2', quantity='4'))
        returned = dict(stage='approved', transfer_id='http-return',
                        request_id='http-return-a', evidence='归还批准', item_id=self.item_id,
                        from_squad=self.target_squad, to_squad=self.source_squad,
                        quantity='3', purpose='return', return_for='http-loan')
        self.assertEqual(409, post(self.DEPUTY, **dict(returned, return_for='wrong-loan')))
        self.assertEqual(303, post(self.DEPUTY, **returned))
        self.assertEqual(303, post(self.TARGET, stage='sent', transfer_id='http-return',
                                   request_id='http-return-s', evidence='归还交出',
                                   expected_version='1'))
        receipt = dict(stage='received', transfer_id='http-return',
                       request_id='http-return-r', evidence='归还实收',
                       expected_version='2', quantity='3', damaged_quantity='1')
        self.assertEqual(409, post(self.SOURCE, **dict(receipt, damaged_quantity='4')))
        self.assertEqual(303, post(self.SOURCE, **receipt))
        self.assertEqual(1, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='http-loan').outstanding)
        self.assertEqual(6, sum(event.kind == RECORDED for event in self.event_space().events))

    def test_handover_shows_named_loss_and_open_loan_without_closing_it(self):
        self.make_loan_received()
        self.approve_return()
        self.send(transfer_id='return-1', request_id='send-return-1',
                  member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='return-1', request_id='receive-return-1',
                     member_token=self.tokens[self.SOURCE], quantity='3',
                     damaged_quantity='1')
        self.state.handle_manual_material_loss(
            self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, item_id=self.item_id, loss_id='loss-closeout',
            kind='consumed', amount='3', expected_version='0',
            evidence='收尾耗损初报', occurred_at='', reason='',
            request_id='loss-closeout-1')
        self.state.handle_manual_material_loss(
            self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, item_id=self.item_id, loss_id='loss-closeout',
            kind='consumed', amount='2', expected_version='1',
            evidence='收尾耗损更正', occurred_at='', reason='重复计数',
            request_id='loss-closeout-2')
        before = self.state.ledger.to_dict()
        space = self.event_space()
        view = handover_view(space, self.state.registry.events[self.event_id],
                             self.state.ledger, self.state.review_ledger)
        rows = '\n'.join(view.materials)
        self.assertIn('本队消耗报告 loss-closeout', rows)
        self.assertIn('模拟水带 2 盘', rows)
        self.assertIn('当前版本 2，历史更正保留', rows)
        self.assertIn('借用 loan-1', rows)
        self.assertIn('已实收归还 3，归还损坏 1，原流水未归还 1', rows)
        self.assertIn('归还 return-1', rows)
        self.assertIn('本次流水归还损坏 1', rows)
        self.assertIn('未登记为借还流水的未归还数量未知', str(view.missing))
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.assertIn('未归还 1', render_handover(self.state, 'commander'))
        self.assertEqual('', render_handover(self.state, 'member'))
        self.assertEqual(before, self.state.ledger.to_dict())

    def test_handover_hides_all_transfer_amounts_until_missing_mirror_is_repaired(self):
        self.make_loan_received()
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.approve_return()
        view = handover_view(self.event_space(), self.state.registry.events[self.event_id],
                             self.state.ledger, self.state.review_ledger)
        rows = '\n'.join(view.materials)
        self.assertNotIn('借用 loan-1', rows)
        self.assertNotIn('归还 return-1', rows)
        self.assertEqual((), view.materials)
        self.assertIn('物资版本/审计关联不一致，未展示登记量', str(view.missing))
        self.approve_return()
        repaired = handover_view(self.event_space(), self.state.registry.events[self.event_id],
                                  self.state.ledger, self.state.review_ledger)
        self.assertIn('借用 loan-1', '\n'.join(repaired.materials))

    def test_handover_hides_loss_amount_until_missing_mirror_is_repaired(self):
        args = dict(captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad,
                    item_id=self.item_id, loss_id='loss-closeout', kind='consumed',
                    amount='2', expected_version='0', evidence='现场耗损报告',
                    occurred_at='', reason='', request_id='loss-closeout-1')
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.state.handle_manual_material_loss(self.event_id, **args)
        view = handover_view(self.event_space(), self.state.registry.events[self.event_id],
                             self.state.ledger, self.state.review_ledger)
        self.assertNotIn('本队消耗报告', '\n'.join(view.materials))
        self.assertEqual((), view.materials)
        self.assertIn('物资版本/审计关联不一致，未展示登记量', str(view.missing))
        self.state.handle_manual_material_loss(self.event_id, **args)
        repaired = handover_view(self.event_space(), self.state.registry.events[self.event_id],
                                  self.state.ledger, self.state.review_ledger)
        self.assertIn('本队消耗报告 loss-closeout', '\n'.join(repaired.materials))

    def test_named_withdrawal_excludes_team_stock_and_preserves_reported_exceptions(self):
        exceptions = (
            WithdrawalException(self.item_id, 1, 'retained', '现场器材点', '现场保管员'),
            WithdrawalException(self.item_id, 2, 'handed_over', '北段集结点', '接收单位甲'),
        )
        before_carry = self.state.ledger.carry_lines.copy()
        result = self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, exceptions=exceptions,
            evidence='线下撤离清单', request_id='withdraw-source')
        self.assertEqual(self.SOURCE, result.actor_id)
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.source_squad,
                                             item_id=self.item_id).available)
        self.assertEqual(2, self.state.ledger.compute_gap(
            self.event_id, {self.item_id: 12})[0]['usable'])
        self.assertEqual(before_carry, self.state.ledger.carry_lines)
        self.assertEqual(1, sum(event.kind == WITHDRAWAL_REPORTED
                                for event in self.event_space().events))
        self.assertEqual(self.state.ledger.to_dict(),
                         MaterialsLedger.load(self.state.materials_path).to_dict())
        view = handover_view(self.event_space(), self.state.registry.events[self.event_id],
                             self.state.ledger, self.state.review_ledger)
        self.assertIn('随队撤出 7，留置待核 1，移交待核 2', '\n'.join(view.materials))
        self.assertIn('报告位置 北段集结点', '\n'.join(view.materials))
        same = self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad, exceptions=exceptions,
            evidence='线下撤离清单', request_id='withdraw-source')
        self.assertEqual(result, same)
        with self.assertRaises(ValueError):
            self.approve(transfer_id='new-transfer', request_id='new-transfer-a')
        with self.assertRaises(ValueError):
            self.state.handle_manual_material_loss(
                self.event_id, captain_token=self.tokens[self.SOURCE],
                squad_id=self.source_squad, item_id=self.item_id,
                loss_id='after-withdraw', kind='consumed', amount='1',
                expected_version='0', evidence='撤离后错误耗损', occurred_at='',
                reason='', request_id='after-withdraw')

    def test_withdrawal_rejects_wrong_actor_and_open_borrowing_without_write(self):
        before = self.state.ledger.to_dict()
        with self.assertRaises(PermissionError):
            self.state.handle_team_material_withdrawal(
                self.event_id, captain_token=self.tokens[self.OTHER],
                squad_id=self.source_squad, evidence='错误报告', request_id='wrong-actor')
        self.assertEqual(before, self.state.ledger.to_dict())
        self.make_loan_received()
        before = self.state.ledger.to_dict()
        for squad, actor in ((self.source_squad, self.SOURCE),
                             (self.target_squad, self.TARGET)):
            with self.subTest(squad=squad), self.assertRaises(ValueError):
                self.state.handle_team_material_withdrawal(
                    self.event_id, captain_token=self.tokens[actor],
                    squad_id=squad, evidence='仍有借还未结',
                    request_id=f'withdraw-{squad}')
        self.assertEqual(before, self.state.ledger.to_dict())
        self.approve_return(quantity='4')
        self.send(transfer_id='return-1', request_id='send-return-1',
                  member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='return-1', request_id='receive-return-1',
                     member_token=self.tokens[self.SOURCE], quantity='4')
        self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad, evidence='借还完成后撤离',
            request_id='withdraw-target')
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.target_squad,
                                             item_id=self.item_id).available)

    def test_withdrawal_same_request_repairs_missing_event_mirror(self):
        args = dict(captain_token=self.tokens[self.SOURCE],
                    squad_id=self.source_squad, evidence='线下撤离清单',
                    request_id='withdraw-source')
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.state.handle_team_material_withdrawal(self.event_id, **args)
        self.assertEqual(1, len(MaterialsLedger.load(
            self.state.materials_path).withdrawal_records))
        self.assertEqual(0, sum(event.kind == WITHDRAWAL_REPORTED
                                for event in self.event_space().events))
        self.state.handle_team_material_withdrawal(self.event_id, **args)
        self.assertEqual(1, sum(event.kind == WITHDRAWAL_REPORTED
                                for event in self.event_space().events))
        self.assertEqual((), self.event_space().verify_audit_chain())

    def test_withdrawal_waits_for_missing_transfer_mirror_repair(self):
        self.approve()
        self.send()
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.receive(quantity='4')
        args = dict(captain_token=self.tokens[self.SOURCE],
                    squad_id=self.source_squad, evidence='完成调拨后撤离',
                    request_id='withdraw-after-transfer')
        before = len(self.state.ledger.withdrawal_records)
        with self.assertRaises(ValueError):
            self.state.handle_team_material_withdrawal(self.event_id, **args)
        self.assertEqual(before, len(self.state.ledger.withdrawal_records))
        self.receive(quantity='4')
        self.state.handle_team_material_withdrawal(self.event_id, **args)

    def test_withdrawal_waits_for_missing_loss_mirror_repair(self):
        loss = dict(captain_token=self.tokens[self.SOURCE],
                    squad_id=self.source_squad, item_id=self.item_id,
                    loss_id='withdraw-loss', kind='consumed', amount='2',
                    expected_version='0', evidence='现场耗损单', occurred_at='',
                    reason='', request_id='withdraw-loss-1')
        # Deliberately reconstruct a pre-P14 partial legacy save, so its repair
        # and fail-closed handover protections remain covered after journaling.
        with patch('panda_brain.durable_storage.action', lambda state: nullcontext()), \
                patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.state.handle_manual_material_loss(self.event_id, **loss)
        args = dict(captain_token=self.tokens[self.SOURCE],
                    squad_id=self.source_squad, evidence='耗损结清后撤离',
                    request_id='withdraw-after-loss')
        with self.assertRaises(ValueError):
            self.state.handle_team_material_withdrawal(self.event_id, **args)
        self.assertFalse(self.state.ledger.withdrawal_records)
        self.state.handle_manual_material_loss(self.event_id, **loss)
        self.state.handle_team_material_withdrawal(self.event_id, **args)

    def test_captain_withdrawal_page_and_http_preserve_reported_exceptions(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        status, page = render_page(self.state, '/captain-arrival',
                                   member_token=self.tokens[self.SOURCE])
        self.assertEqual(200, status)
        self.assertIn('报告本队物资撤离', page)
        self.assertIn('当前可用 10', page)
        self.assertIn('name="exception_location"', page)
        status, paused = render_page(self.state, '/captain-arrival', member_token=self.tokens[self.OTHER])
        self.assertEqual(200, status)
        self.assertIn('独立工作台已暂停', paused)
        self.assertNotIn('报告本队物资撤离', paused)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(type(
            'NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())

        def post(phone, **changes):
            fields = dict(event_id=self.event_id, squad_id=self.source_squad,
                          csrf=self.state.member_session(self.tokens[phone]).custody_csrf,
                          request_id='http-withdraw-source', evidence='线下撤离清单',
                          exception_item_id=[self.item_id, self.item_id],
                          exception_kind=['retained', 'handed_over'],
                          exception_quantity=['1', '2'],
                          exception_location=['现场器材点', '北段集结点'],
                          exception_custodian=['现场保管员', '接收单位甲'])
            fields.update(changes)
            request = urllib.request.Request(
                base + '/captain-arrival/material-withdrawal',
                data=urllib.parse.urlencode(fields, doseq=True).encode(),
                headers={'Cookie': f'member_session={self.tokens[phone]}'})
            try:
                return opener.open(request).status
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code
                finally:
                    exc.close()

        self.assertEqual(403, post(self.OTHER))
        self.assertEqual(409, post(self.SOURCE, exception_quantity=['11', '0']))
        self.assertFalse(self.state.ledger.withdrawal_records)
        self.assertEqual(303, post(self.SOURCE))
        record = next(iter(self.state.ledger.withdrawal_records.values()))
        content = json.loads(record.content_json)
        self.assertEqual(7, content['lines'][0]['carried_out'])
        self.assertEqual(2, len(content['exceptions']))
        self.assertEqual(1, sum(event.kind == WITHDRAWAL_REPORTED
                                for event in self.event_space().events))
        self.assertEqual(303, post(self.SOURCE))
        self.assertEqual(1, len(self.state.ledger.withdrawal_records))
        status, page = render_page(self.state, '/captain-arrival',
                                   member_token=self.tokens[self.SOURCE])
        self.assertEqual(200, status)
        self.assertIn('本队已有具名撤离报告', page)
        self.assertIn('位置 北段集结点；保管对象 接收单位甲', page)
        self.assertNotIn('报告本队物资撤离', page)
        self.assertNotIn('action="/captain-arrival/material-loss"', page)

    def test_captain_withdrawal_page_blocks_open_loan(self):
        self.make_loan_received()
        status, page = render_page(self.state, '/captain-arrival',
                                   member_token=self.tokens[self.SOURCE])
        self.assertEqual(200, status)
        self.assertIn('未归还借用', page)
        self.assertNotIn('报告本队物资撤离', page)

    def test_borrower_departure_preserves_open_loan_and_reports_custody(self):
        self.make_loan_received()
        custody = (BorrowedCustody('loan-1', 4, '二中队归建车', '二中队队长'),)
        record = self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad, borrowed_custody=custody,
            evidence='借用装备随队归建清单', request_id='borrower-withdraw')
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.target_squad,
                                             item_id=self.item_id).available)
        self.assertEqual(6, self.state.ledger.compute_gap(
            self.event_id, {self.item_id: 12})[0]['usable'])
        self.assertEqual(4, json.loads(record.content_json)['borrowed_custody'][0]['quantity'])
        self.assertEqual(self.state.ledger.to_dict(),
                         MaterialsLedger.load(self.state.materials_path).to_dict())
        rows = '\n'.join(handover_view(
            self.event_space(), self.state.registry.events[self.event_id],
            self.state.ledger, self.state.review_ledger).materials)
        self.assertIn('撤离未归还借用', rows)
        self.assertIn('报告位置 二中队归建车', rows)
        self.assertIn('撤离时未归还 4', rows)
        same = self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad, borrowed_custody=custody,
            evidence='借用装备随队归建清单', request_id='borrower-withdraw')
        self.assertEqual(record, same)
        with self.assertRaises(ValueError):
            self.approve_return()
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)

    def test_departed_borrower_shows_pending_custody_without_dead_transfer_forms(self):
        self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)
        self.make_loan_received()
        before_departure = render_page(self.state, '/material-transfers',
                                       member_token=self.tokens[self.DEPUTY])[1]
        self.assertIn('登记归还确认', before_departure)
        self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad,
            borrowed_custody=(BorrowedCustody('loan-1', 4, '归建车<b>', '保管人甲'),),
            evidence='借用装备随队归建清单', request_id='borrower-withdraw')
        ledger_before = self.state.ledger.to_dict()
        status, deputy_page = render_page(self.state, '/material-transfers',
                                          member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn('未归还 4', deputy_page)
        self.assertIn('借入队伍已报告撤离', deputy_page)
        self.assertIn('报告位置 归建车&lt;b&gt;', deputy_page)
        self.assertIn('保管对象 保管人甲', deputy_page)
        self.assertIn('本处历史数量不作为业务未结清判据', deputy_page)
        self.assertNotIn('登记归还确认', deputy_page)
        self.assertNotIn('登记调拨确认', deputy_page)
        self.assertEqual(ledger_before, self.state.ledger.to_dict())
        with self.assertRaises(ValueError):
            self.approve_return()
        self.assertEqual(ledger_before, self.state.ledger.to_dict())

    def test_borrower_departure_requires_exact_unreturned_custody(self):
        self.make_loan_received()
        base = dict(evidence='借用装备撤离清单', request_id='borrower-withdraw')
        cases = (
            (),
            (BorrowedCustody('loan-1', 3, '二中队车', '队长'),),
            (BorrowedCustody('loan-1', 4, '', '队长'),),
            (BorrowedCustody('unknown', 4, '二中队车', '队长'),),
        )
        for custody in cases:
            with self.subTest(custody=custody), self.assertRaises(ValueError):
                self.state.handle_team_material_withdrawal(
                    self.event_id, captain_token=self.tokens[self.TARGET],
                    squad_id=self.target_squad, borrowed_custody=custody, **base)
        with self.assertRaises(ValueError):
            self.state.handle_team_material_withdrawal(
                self.event_id, captain_token=self.tokens[self.SOURCE],
                squad_id=self.source_squad,
                borrowed_custody=(BorrowedCustody('loan-1', 4, '二中队车', '队长'),),
                **base)
        self.assertFalse(self.state.ledger.withdrawal_records)
        self.approve_return()
        self.send(transfer_id='return-1', request_id='send-return-1',
                  member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='return-1', request_id='receive-return-1',
                     member_token=self.tokens[self.SOURCE], quantity='3',
                     damaged_quantity='1')
        custody = (BorrowedCustody('loan-1', 1, '二中队车', '队长'),)
        with self.assertRaises(ValueError):
            self.state.handle_team_material_withdrawal(
                self.event_id, captain_token=self.tokens[self.TARGET],
                squad_id=self.target_squad, borrowed_custody=custody,
                exceptions=(WithdrawalException(self.item_id, 3, 'retained',
                                                '现场器材点', '值守人员'),), **base)
        result = self.state.handle_team_material_withdrawal(
            self.event_id, captain_token=self.tokens[self.TARGET],
            squad_id=self.target_squad, borrowed_custody=custody,
            exceptions=(WithdrawalException(self.item_id, 2, 'retained',
                                            '现场器材点', '值守人员'),), **base)
        self.assertEqual(1, json.loads(result.content_json)['lines'][0]['carried_out'])
        self.assertEqual(1, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)

    def test_borrower_withdrawal_page_and_http_require_each_loan_custody(self):
        self.make_loan_received()
        self.approve(transfer_id='loan-2', request_id='approve-loan-2',
                     purpose='loan', quantity='2.1234567')
        self.send(transfer_id='loan-2', request_id='send-loan-2')
        self.receive(transfer_id='loan-2', request_id='receive-loan-2',
                     quantity='2.1234567')
        source_page = render_page(self.state, '/captain-arrival',
                                  member_token=self.tokens[self.SOURCE])[1]
        target_page = render_page(self.state, '/captain-arrival',
                                  member_token=self.tokens[self.TARGET])[1]
        self.assertIn('本队借出装备仍未满足全数后勤实收', source_page)
        self.assertNotIn('报告本队物资撤离', source_page)
        self.assertIn('报告本队物资撤离', target_page)
        self.assertIn('name="loan_transfer_id" value="loan-1"', target_page)
        self.assertIn('name="loan_transfer_id" value="loan-2"', target_page)
        self.assertIn('name="loan_quantity" value="2.1234567"', target_page)
        self.assertIn('未归还借用 loan-1', target_page)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(type(
            'NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())

        def post(phone, **changes):
            fields = dict(event_id=self.event_id, squad_id=self.target_squad,
                          csrf=self.state.member_session(self.tokens[phone]).custody_csrf,
                          request_id='http-borrower-withdraw', evidence='借用装备归建清单',
                          exception_item_id=[self.item_id, self.item_id],
                          exception_kind=['retained', 'handed_over'],
                          exception_quantity=['', ''],
                          exception_location=['', ''], exception_custodian=['', ''],
                          loan_transfer_id=['loan-1', 'loan-2'],
                          loan_quantity=['4', '2.1234567'],
                          loan_location=['二中队归建车', '二中队器材车'],
                          loan_custodian=['二中队队长', '二中队器材员'])
            fields.update(changes)
            request = urllib.request.Request(
                f'http://127.0.0.1:{server.server_port}/captain-arrival/material-withdrawal',
                data=urllib.parse.urlencode(fields, doseq=True).encode(),
                headers={'Cookie': f'member_session={self.tokens[phone]}'})
            try:
                return opener.open(request).status
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code
                finally:
                    exc.close()

        self.assertEqual(403, post(self.SOURCE))
        self.assertEqual(409, post(self.TARGET, loan_quantity=['3', '2.1234567']))
        self.assertEqual(409, post(self.TARGET, loan_custodian=['二中队队长']))
        self.assertFalse(self.state.ledger.withdrawal_records)
        self.assertEqual(303, post(self.TARGET))
        self.assertEqual(303, post(self.TARGET))
        record = next(iter(self.state.ledger.withdrawal_records.values()))
        custody = json.loads(record.content_json)['borrowed_custody']
        self.assertEqual([('loan-1', 4), ('loan-2', 2.1234567)],
                         [(row['loan_transfer_id'], row['quantity']) for row in custody])
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)
        self.assertEqual(2.1234567, loan_status(self.state.ledger, event_id=self.event_id,
                                                loan_transfer_id='loan-2').outstanding)
        self.assertEqual(0, transfer_balance(self.state.ledger, event_id=self.event_id,
                                             squad_id=self.target_squad,
                                             item_id=self.item_id).available)
        reported_page = render_page(self.state, '/captain-arrival',
                                    member_token=self.tokens[self.TARGET])[1]
        self.assertIn('位置 二中队器材车；保管对象 二中队器材员', reported_page)

    def test_borrower_page_hides_impossible_custody_quantity(self):
        self.make_loan_received()
        self.approve(transfer_id='ordinary-back', request_id='approve-ordinary-back',
                     from_squad=self.target_squad, to_squad=self.source_squad)
        self.send(transfer_id='ordinary-back', request_id='send-ordinary-back',
                  member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='ordinary-back', request_id='receive-ordinary-back',
                     member_token=self.tokens[self.SOURCE], quantity='4')
        self.assertEqual(4, loan_status(self.state.ledger, event_id=self.event_id,
                                        loan_transfer_id='loan-1').outstanding)
        page = render_page(self.state, '/captain-arrival',
                           member_token=self.tokens[self.TARGET])[1]
        self.assertIn('未归还借用量超过当前账面可用量', page)
        self.assertNotIn('报告本队物资撤离', page)


if __name__ == '__main__':
    unittest.main()
