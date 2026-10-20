"""D4: actual closed-event closeout without reopening any operational action."""
from copy import deepcopy
from dataclasses import asdict
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain.panel import PanelActionError, make_server, render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_custody_decision_page as custody
from . import test_upgrade_material_supply_receipts as supply


class Interrupted(BaseException):
    pass


class ClosedCustodyTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'server', 'status', 'verify', 'settle',
                 'full', 'verify_form', 'settle_form', 'post_decision', 'page'):
        locals()[name] = getattr(custody.CustodyDecisionPageTests, name)

    def setUp(self):
        custody.CustodyDecisionPageTests.setUp(self)
        self.state.handle_event_action(self.event_id, 'close')

    def test_closed_partial_damage_verification_and_explicit_settlement_preserve_sources(self):
        registry = self.state.registry_path.read_bytes()
        ledger = self.state.materials_path.read_bytes()
        originals = dict(self.event_space().object_revisions)
        first = self.receipt()
        self.verify(first)
        self.assertEqual(2, self.status().receipt_summary.unreceived_quantity)
        with self.assertRaises(ValueError):
            self.settle(handling_evidence='模拟处理单', handling_conclusion='模拟损坏隔离')
        second = self.receipt(quantity=2, damaged_quantity=0, expected_version=1, request_id='receipt-2')
        self.verify(second, expected_version=1, expected_receipt_version=2, request_id='verify-2')
        with self.assertRaises(ValueError):
            self.settle(expected_receipt_version=2, expected_verification_version=2)
        settled = self.settle(expected_receipt_version=2, expected_verification_version=2,
                             handling_evidence='模拟处理单', handling_conclusion='模拟损坏隔离')
        view = self.status()
        self.assertTrue(view.settled)
        self.assertEqual((4, 1, 0), (view.receipt_summary.received_quantity,
            view.receipt_summary.damaged_quantity, view.receipt_summary.unreceived_quantity))
        self.assertEqual(self.LOGISTICS, settled.actor_id)
        self.assertEqual('closed', self.state.registry.events[self.event_id].status)
        self.assertEqual(registry, self.state.registry_path.read_bytes())
        self.assertEqual(ledger, self.state.materials_path.read_bytes())
        self.assertTrue(all(self.event_space().object_revisions[key] == row for key, row in originals.items()))
        self.assertEqual(self.event_space().object_revisions, self.state.workspace.object_revisions)

    def test_closed_current_named_leader_only_and_invalid_sources_quantities_versions(self):
        before = self.files()
        for phone in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            with self.subTest(phone=phone), self.assertRaises(PermissionError):
                self.receipt(member_token=self.tokens[phone])
        for values in (dict(loan_transfer_id='unknown'), dict(quantity=5), dict(expected_version=1),
                       dict(damaged_quantity=3), dict(quantity=float('nan')), dict(location='')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.receipt(**values)
        self.assertEqual(before, self.files())
        with self.assertRaises(PanelActionError):
            self.appoint(self.REPLACEMENT)
        with self.assertRaises(PanelActionError):
            self.state._appointment_space(self.event_id, for_write=True)
        self.assertEqual(before, self.files())

    def test_closed_exact_replays_zero_save_and_conflicts_rejected_after_restart(self):
        batch, verification = self.full()
        settlement = self.settle()
        original = asdict(self.status())
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay must not save')):
            self.assertEqual(batch, self.receipt(quantity=4, damaged_quantity=0))
            self.assertEqual(verification, self.verify(batch))
            self.assertEqual(settlement, self.settle())
        with self.assertRaises(ValueError):
            self.receipt(quantity=4, damaged_quantity=0, location='模拟其他位置')
        self.assertEqual(original, asdict(self.status()))
        self.assertEqual(before, self.files())

    def test_closed_archived_http_uses_explicit_event_and_never_switches_context(self):
        self.state.active_event_id = None
        context = deepcopy(self.state.workspace)
        base = self.server()
        code, page = render_page(self.state, '/material-custody', member_token=self.tokens[self.LOGISTICS],
                                query={'event_id': [self.event_id]})
        self.assertEqual(200, code)
        self.assertIn('事件已关闭', page)
        fields = dict(event_id=self.event_id, csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            loan_transfer_id='loan-1', quantity='4', damaged_quantity='0', location='模拟收尾库位',
            custodian='模拟保管人', evidence='模拟实收单', request_id='http-receipt', expected_version='0', occurred_at='')
        code, data = self.post_decision(base, 'report', fields)
        self.assertEqual(200, code)
        batch = self.event_space().object_revisions[data['record_id']]
        self.assertEqual(200, self.post_decision(base, 'verify', self.verify_form(batch))[0])
        self.assertEqual(200, self.post_decision(base, 'settle', self.settle_form())[0])
        self.assertTrue(self.status().settled)
        self.assertIsNone(self.state.active_event_id)
        self.assertEqual(context.events, self.state.workspace.events)
        self.assertEqual('closed', self.state.registry.events[self.event_id].status)
        before = self.files()
        self.assertEqual(403, self.post_decision(base, 'report', fields | dict(event_id='wrong'))[0])
        self.assertEqual(403, self.post_decision(base, 'report', fields | dict(csrf='wrong'))[0])
        self.assertEqual(403, self.post_decision(base, 'report', fields, phone=self.DEPUTY)[0])
        self.assertEqual(409, self.post_decision(base, 'report', fields | dict(role='log-material'))[0])
        self.assertEqual(before, self.files())

    def test_active_event_still_requires_current_context_and_registered_is_refused(self):
        event = self.state.registry.create_event(name='模拟未启动', report_no='K10',
            discovered_at='2026-10-11T08:00:00+08:00', source='人工模拟', location='模拟位置')
        with self.assertRaises(ValueError):
            self.state.handle_material_custody_receipt(event.event_id,
                member_token=self.tokens[self.LOGISTICS], loan_transfer_id='loan-1', quantity=1,
                damaged_quantity=0, location='模拟库', custodian='模拟人', evidence='模拟单',
                request_id='wrong', expected_version=0)
        # The closed exception must not turn another live event into an implicit switch.
        from dataclasses import replace
        self.state.registry.events[self.event_id] = replace(self.state.registry.events[self.event_id], status='active')
        self.state.active_event_id = None
        self.assertEqual(404, render_page(self.state, '/material-custody',
            member_token=self.tokens[self.LOGISTICS], query={'event_id': [self.event_id]})[0])

    def test_closed_postwrite_failures_and_interruption_restore_then_retry(self):
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous closed save failure')
        actions = [lambda: self.receipt(quantity=4, damaged_quantity=0)]
        for step in range(3):
            action = actions[-1]
            before = self.files()
            memory = deepcopy(self.state.workspace.object_revisions)
            with patch('panda_brain.persistence.save_workspace', side_effect=fail), self.assertRaises(OSError):
                action()
            self.assertEqual(before, self.files())
            self.assertEqual(memory, self.state.workspace.object_revisions)
            row = action()
            if step == 0:
                batch = row
                actions.append(lambda: self.verify(batch))
            elif step == 1:
                actions.append(self.settle)
        self.assertTrue(self.status().settled)

    def test_closed_interruption_freezes_and_restart_restores_unsettled_state(self):
        self.full()
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop), self.assertRaises(Interrupted):
            self.settle()
        with self.assertRaises(RuntimeError):
            self.settle()
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(before, self.files())
        self.assertFalse(self.status().settled)
        self.settle()
        self.assertTrue(self.status().settled)


class ClosedSupplyTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'event_space', 'appoint', 'view', 'report', 'files', 'space',
                 'status', 'plan', 'attach', 'review', 'verify', 'publish', 'act', 'receive', 'inventory', 'loss'):
        locals()[name] = getattr(supply.SupplyReceiptTests, name)

    def setUp(self):
        supply.SupplyReceiptTests.setUp(self)
        self.state.handle_event_action(self.event_id, 'close')

    def test_closed_preexisting_dispatch_receipts_damage_debt_and_no_operational_reopen(self):
        registry = self.state.registry_path.read_bytes()
        flow = deepcopy(self.space().command_flows)
        stock = deepcopy(self.state.ledger.supply_records)
        self.receive(quantity=3, damaged_quantity=1)
        value = self.inventory()
        self.assertEqual((14, 3, 1, 1), (value.book_available, value.supply_received,
            value.supply_damaged, value.supply_in_transit))
        self.assertEqual(self.baseline, {sid: asdict(v) for sid, v in self.state.ledger.snapshots[self.event_id].squads.items()})
        self.assertEqual(stock, self.state.ledger.supply_records)
        self.assertEqual(flow, self.space().command_flows)
        self.assertEqual(registry, self.state.registry_path.read_bytes())
        before = self.files()
        for action in (self.verify, self.act, lambda: self.act('cancel'), self.publish, self.loss,
                       lambda: self.appoint(self.REPLACEMENT)):
            with self.assertRaises((ValueError, PanelActionError)):
                action()
            self.assertEqual(before, self.files())

    def test_closed_supply_unknown_unshipped_overquantity_and_wrong_actor_zero_write(self):
        before = self.files()
        for values in (dict(order_id='unknown'), dict(quantity=5), dict(expected_version=1),
                       dict(damaged_quantity=3), dict(squad_id='unknown')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.receive(**values)
        for person in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            with self.assertRaises(PermissionError):
                self.receive(member_token=self.tokens[person])
        self.assertEqual(before, self.files())

    def test_closed_supply_http_explicit_archived_event_csrf_and_source_replay(self):
        self.state.active_event_id = None
        context = deepcopy(self.state.workspace)
        code, page = render_page(self.state, '/material-supply/receipts', member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200, code)
        self.assertIn('事件已关闭', page)
        self.assertIn('/material-custody?event_id=' + self.event_id, page)
        self.assertIn('action="/material-supply/receipts"', page)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fields = dict(event_id=self.event_id, order_id=self.order_id, squad_id=self.target_squad,
            location='模拟关闭后实收位置', quantity='2', damaged_quantity='0', basis='模拟收尾单',
            expected_version='0', request_id='http-closed-supply',
            csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf)
        def send(values, phone=self.LOGISTICS):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('POST', '/material-supply/receipts', urlencode(values, doseq=True),
                {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': 'member_session=' + self.tokens[phone]})
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status
        before = self.files()
        self.assertEqual(403, send(fields | dict(csrf='wrong')))
        self.assertEqual(403, send(fields | dict(csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf), self.DEPUTY))
        self.assertEqual(409, send(fields | dict(order_id='wrong')))
        self.assertEqual(before, self.files())
        self.assertEqual(303, send(fields))
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            self.assertEqual(303, send(fields))
        self.assertEqual(before, self.files())
        self.assertEqual(context.events, self.state.workspace.events)
        self.assertIsNone(self.state.active_event_id)
        self.state = self.new_state()
        self.assertEqual(14, self.inventory().book_available)
        self.assertEqual('closed', self.state.registry.events[self.event_id].status)

    def test_closed_supply_cross_file_failure_and_interruption_recover_without_double_receipt(self):
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous closed supply save failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail), self.assertRaises(OSError):
            self.receive()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.ledger.supply_receipts)
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop), self.assertRaises(Interrupted):
            self.receive()
        with self.assertRaises(RuntimeError):
            self.receive()
        self.state = self.new_state()
        self.tokens[self.LOGISTICS], _ = self.state.member_login(self.LOGISTICS, '123456')
        self.assertEqual(before, self.files())
        self.receive()
        self.assertEqual(14, self.inventory().book_available)
        self.assertEqual(1, len(self.state.ledger.supply_receipts))

