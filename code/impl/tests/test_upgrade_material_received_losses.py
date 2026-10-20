"""Existing arrival items may consume actually received transfers, with frozen cuts."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from panda_brain.material_inventory_projection import project
from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.materials import MaterialsLedger
from panda_brain.panel import make_server, render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_manual_material_transfers as fixture
from .test_upgrade_material_inventory_projection import rehash


class Interrupted(BaseException):
    pass


class ReceivedLossTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'new_state',
                 'setUp', 'event_space', 'approve', 'send', 'receive',
                 'make_loan_received', 'approve_return'):
        locals()[name] = getattr(fixture.NamedTransferTests, name)

    def report(self, **changes):
        args = dict(captain_token=self.tokens[self.TARGET], squad_id=self.target_squad,
            item_id=self.item_id, loss_id='received-loss', kind='consumed', amount='3',
            expected_version='0', evidence='匿名实收后耗损报告', occurred_at='',
            reason='', request_id='received-loss-1')
        args.update(changes)
        return self.state.handle_manual_material_loss(self.event_id, **args)

    def full(self):
        self.approve()
        self.send()
        self.receive(quantity='4')

    def view(self, space=None, ledger=None):
        return project(space or self.event_space(), ledger or self.state.ledger,
                       event_id=self.event_id, item_id=self.item_id)

    def files(self):
        return {str(p): p.read_bytes() for p in (self.state.registry_path, self.state.materials_path,
                    Path(self.state.registry.events[self.event_id].snapshot_path))}

    def changed(self, record, mutate):
        ledger, space = copy.deepcopy(self.state.ledger), self.event_space()
        content = json.loads(record.content_json)
        mutate(content)
        revised = replace(record, content_json=json.dumps(content))
        ledger.flow_records[record.id] = revised
        for event in space.events:
            if event.payload.get('flow_record_id') == record.id:
                event.payload['record'] = asdict(revised)
        rehash(space)
        return ledger, space

    def test_partial_receipt_permits_loss_above_arrival_and_conserves(self):
        self.approve()
        self.send()
        self.receive()
        record = self.report()
        basis = json.loads(record.content_json)['inventory_basis']
        self.assertEqual(3, len(basis['transfer_refs']))
        self.assertEqual([], basis['loss_refs'])
        self.assertEqual((12, 7, 2, 3), (self.view().arrival_base, self.view().team_available,
                                       self.view().in_transit, self.view().losses))
        line = self.state.ledger.snapshots[self.event_id].squads[self.target_squad].lines[self.item_id]
        self.assertEqual((2, 2, 3), (line.qty_registered, line.qty_usable, line.consumed))
        self.assertEqual(2, self.state.ledger.carry_list(self.target_squad)[0].quantity)

    def test_approval_and_in_transit_do_not_expand_loss_capacity(self):
        self.approve()
        before = self.files()
        with self.assertRaises(ValueError):
            self.report()
        self.assertEqual(before, self.files())
        self.send()
        before = self.files()
        with self.assertRaises(ValueError):
            self.report()
        self.assertEqual(before, self.files())
        self.receive()
        self.report(amount='4')
        self.assertEqual((6, 4), (self.view().team_available, self.view().losses))

    def test_consumption_and_damage_share_received_capacity_once(self):
        self.full()
        self.report(amount='5')
        self.report(loss_id='received-damage', kind='damaged', amount='1', request_id='damage-1')
        before = self.files()
        with self.assertRaises(ValueError):
            self.report(loss_id='excess', amount='0.01', request_id='excess-1')
        self.assertEqual(before, self.files())
        self.assertEqual((6, 6), (self.view().team_available, self.view().losses))

    def test_correction_replaces_quantity_and_retry_keeps_original_cut(self):
        self.approve()
        self.send()
        self.receive()
        first = self.report()
        self.receive(quantity='2', expected_version='3', request_id='remaining-receipt')
        second = self.report(amount='5', expected_version='1', reason='匿名核对更正', request_id='received-loss-2')
        self.assertEqual((7, 5), (self.view().team_available, self.view().losses))
        self.assertEqual(4, len(json.loads(second.content_json)['inventory_basis']['transfer_refs']))
        self.assertEqual([first.id], json.loads(second.content_json)['inventory_basis']['loss_refs'])
        before = self.files()
        self.assertEqual(first, self.report())
        self.assertEqual(before, self.files())
        self.assertEqual(3, len(json.loads(first.content_json)['inventory_basis']['transfer_refs']))

    def test_new_transfer_out_deducts_before_later_loss(self):
        self.full()
        self.approve(transfer_id='out-2', request_id='approve-out', from_squad=self.target_squad,
                     to_squad=self.source_squad, quantity='2')
        self.send(transfer_id='out-2', request_id='send-out', member_token=self.tokens[self.TARGET])
        with self.assertRaises(ValueError):
            self.report(amount='5')
        self.report(amount='4')
        self.assertEqual((6, 2, 4), (self.view().team_available, self.view().in_transit, self.view().losses))

    def test_return_damage_never_funds_consumption(self):
        self.make_loan_received()
        self.approve_return()
        self.send(transfer_id='return-1', request_id='return-send', member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='return-1', request_id='return-receive', member_token=self.tokens[self.SOURCE],
                     quantity='3', damaged_quantity='1')
        with self.assertRaises(ValueError):
            self.report(captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad, amount='9')
        self.report(captain_token=self.tokens[self.SOURCE], squad_id=self.source_squad, amount='8')
        self.assertEqual((3, 8, 1), (self.view().team_available, self.view().losses, self.view().return_damage))

    def test_remaining_received_quantity_can_transfer_then_both_teams_withdraw(self):
        self.full()
        self.report(amount='5')
        self.approve(transfer_id='remaining-out', request_id='remaining-approve',
                     from_squad=self.target_squad, to_squad=self.source_squad, quantity='1')
        self.send(transfer_id='remaining-out', request_id='remaining-send', member_token=self.tokens[self.TARGET])
        self.receive(transfer_id='remaining-out', request_id='remaining-receive',
                     member_token=self.tokens[self.SOURCE], quantity='1')
        for squad, captain in ((self.target_squad, self.TARGET), (self.source_squad, self.SOURCE)):
            self.state.handle_team_material_withdrawal(self.event_id,
                captain_token=self.tokens[captain], squad_id=squad, exceptions=(),
                evidence='匿名余量撤离报告', request_id='withdraw-'+squad)
        view = self.view()
        self.assertEqual((12, 0, 7, 5), (view.arrival_base, view.team_available, view.carried_out, view.losses))
        self.assertEqual(2, self.state.ledger.carry_list(self.target_squad)[0].quantity)

    def test_unaudited_loss_basis_blocks_new_transfer_without_writing(self):
        self.full()
        record = self.report(amount='5')
        space = self.event_space()
        space._events = [e for e in space.events if e.payload.get('flow_record_id') != record.id]
        rehash(space)
        save_workspace(space, Path(self.state.registry.events[self.event_id].snapshot_path))
        before = self.files()
        with self.assertRaises(ValueError):
            self.approve(transfer_id='unbacked-out', request_id='unbacked-approve',
                         from_squad=self.target_squad, to_squad=self.source_squad, quantity='1')
        self.assertEqual(before, self.files())

    def test_missing_mirror_blocks_new_loss_without_writing(self):
        self.full()
        space = self.event_space()
        space._events = [e for e in space.events if e.kind != 'MaterialTransferStageReported']
        rehash(space)
        save_workspace(space, Path(self.state.registry.events[self.event_id].snapshot_path))
        before = self.files()
        with self.assertRaises(ValueError):
            self.report()
        self.assertEqual(before, self.files())

    def test_omitted_prior_loss_source_rejected_even_with_rehashed_mirror(self):
        self.full()
        self.report(loss_id='earlier-loss', amount='1', request_id='earlier-request')
        record = self.report(amount='2')
        ledger, space = self.changed(record, lambda c: c['inventory_basis'].update(loss_refs=[]))
        self.assertFalse(space.verify_audit_chain())
        with self.assertRaises(ValueError):
            self.view(space, ledger)
        result = assess_gap_inputs(space, ledger, event_id=self.event_id, item_id=self.item_id, order_id='missing')
        self.assertIsNone(result.book_available)
        self.assertIsNone(result.inventory)

    def test_future_receipt_cannot_back_old_loss_and_deleted_source_fails_reload(self):
        self.approve()
        self.send()
        self.receive()
        record = self.report()
        future = self.receive(quantity='2', expected_version='3', request_id='future-receipt')
        ledger, space = self.changed(record, lambda c: c['inventory_basis']['transfer_refs'].append(future.id))
        content = json.loads(ledger.flow_records[record.id].content_json)
        content['inventory_basis']['transfer_refs'].sort()
        revised = replace(ledger.flow_records[record.id], content_json=json.dumps(content))
        ledger.flow_records[record.id] = revised
        for event in space.events:
            if event.payload.get('flow_record_id') == record.id:
                event.payload['record'] = asdict(revised)
        rehash(space)
        with self.assertRaises(ValueError):
            self.view(space, ledger)
        raw = self.state.ledger.to_dict()
        prior = json.loads(record.content_json)['inventory_basis']['transfer_refs'][-1]
        raw['transfer_records'].pop(prior)
        path = self.root/'broken.json'
        path.write_text(json.dumps(raw), encoding='utf-8')
        with self.assertRaises(ValueError):
            MaterialsLedger.load(path)

    def test_restart_preserves_basis_and_legacy_reports_remain_readable(self):
        self.full()
        first = self.report(amount='5')
        view = self.view()
        self.state = self.new_state()
        self.assertEqual(view, self.view())
        self.tokens[self.TARGET], _ = self.state.member_login(self.TARGET, '123456')
        self.assertEqual(first, self.report(amount='5'))
        ledger, space = self.changed(first, lambda c: c.pop('inventory_basis'))
        with self.assertRaises(ValueError):
            self.view(space, ledger)  # No legacy report can exceed original arrival quantity.
        record = self.report(amount='1', expected_version='1', reason='匿名更正', request_id='legacy-compatible')
        ledger, space = self.changed(record, lambda c: c.pop('inventory_basis'))
        self.assertEqual(11, self.view(space, ledger).team_available)

    def test_save_failure_rolls_back_quantity_basis_and_mirror(self):
        self.full()
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=OSError('anonymous failure')):
            with self.assertRaises(OSError):
                self.report(amount='5')
        self.assertEqual(before, self.files())
        self.assertEqual(12, self.view().team_available)
        self.report(amount='5')
        self.assertEqual(7, self.view().team_available)

    def test_interruption_restores_entire_batch_and_requires_fresh_session(self):
        self.full()
        before = self.files()
        def stop(space, target):
            save_workspace(space, target)
            raise Interrupted()
        with patch('panda_brain.persistence.save_workspace', side_effect=stop):
            with self.assertRaises(Interrupted):
                self.report(amount='5')
        self.state = self.new_state()
        self.assertEqual(before, self.files())
        self.assertFalse(self.state.member_sessions)
        self.tokens[self.TARGET], _ = self.state.member_login(self.TARGET, '123456')
        self.report(amount='5')
        self.assertEqual(7, self.view().team_available)

    def test_existing_http_gate_accepts_received_quantity_and_displays_frozen_sources(self):
        self.full()
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(type('NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())
        form = dict(event_id=self.event_id, squad_id=self.target_squad, item_id=self.item_id,
            loss_id='http-received-loss', kind='consumed', amount='5', expected_version='0',
            evidence='匿名HTTP实收后耗损', occurred_at='', reason='', request_id='http-received-1')
        def post(token):
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/captain-arrival/material-loss',
                data=urllib.parse.urlencode(form).encode(), headers={'Cookie':f'member_session={token}'})
            try:
                with opener.open(request) as result:
                    return result.status
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                return code
        self.assertEqual(403, post(self.tokens[self.OTHER]))
        self.assertEqual(303, post(self.tokens[self.TARGET]))
        self.assertEqual(303, post(self.tokens[self.TARGET]))
        self.assertEqual(7, self.view().team_available)
        code, html = render_page(self.state, '/captain-arrival', member_token=self.tokens[self.TARGET])
        self.assertEqual(200, code)
        self.assertIn('报告时数量来源', html)
        self.assertIn('消耗 5', html)
        form['loss_id'], form['request_id'], form['amount'] = 'http-excess', 'http-excess', '2'
        self.assertEqual(409, post(self.tokens[self.TARGET]))


if __name__ == '__main__':
    unittest.main()
