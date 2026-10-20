"""P08-02: named manual loss reports remain versioned and event bound."""
import json
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
from panda_brain.manual_material_reports import REPORTED
from panda_brain.material_flows import MaterialFlowService
from panda_brain.materials import MaterialsLedger
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError, PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.record_contracts import ObjectReference
from panda_brain.workspace import ScenarioWorkspace


class ManualMaterialReportTests(unittest.TestCase):
    DUTY = '13977772001'
    DEPUTY = '13977772002'
    CAPTAIN = '13977772003'
    MEMBER = '13977772004'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        snap = patch.object(wb, 'SNAPSHOT_DIR', root / 'snapshots')
        snap.start()
        self.addCleanup(snap.stop)
        self.auth = GeneralEntryAuth(secret='p08-manual-report-only')
        self.state = PanelState(
            ScenarioWorkspace('p08-manual', general_auth=self.auth), auth=self.auth,
            registry_path=root / 'registry.json', materials_path=root / 'materials.json',
            reviews_path=root / 'reviews.json', alarm_inbox_path=root / 'alarms.json',
            duty_subjects=(self.DUTY,))
        people = (
            PersonRecord(name='模拟值班员', phone=self.DUTY, unit='值班室'),
            PersonRecord(name='模拟副总指挥', phone=self.DEPUTY, unit='前指'),
            PersonRecord(name='模拟队长', phone=self.CAPTAIN, unit='一中队', is_leader=True),
            PersonRecord(name='模拟队员', phone=self.MEMBER, unit='一中队'),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            self.tokens[person.phone], error = self.state.member_login(person.phone, '123456')
            self.assertEqual('', error)
        event = self.state.registry.create_event(
            name='模拟物资事件', report_no='P08-MANUAL',
            discovered_at='2026-09-29T08:00:00+08:00',
            source='人工模拟登记', location='模拟北段')
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name='一中队', member_phones=(self.CAPTAIN, self.MEMBER),
            leader_phone=self.CAPTAIN, library=self.state.registry.library)
        self.squad_id = squad.squad_id
        item = self.state.ledger.upsert_item(name='模拟水带', unit='盘')
        self.item_id = item.item_id
        self.state.ledger.set_carry_line(self.squad_id, self.item_id, quantity=10)
        self.state._save_ledger()
        self.state.handle_event_action(self.event_id, 'start', duty_token=self.tokens[self.DUTY])
        qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                     if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='模拟任命依据', request_id='appoint-p08')
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad_id], channel='phone', request_id='select-p08')
        self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, qr_id=qr_id)

    def arrive(self):
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, mode='initial', phones=[], request_id='arrival-p08')

    def report(self, **changes):
        values = dict(captain_token=self.tokens[self.CAPTAIN], squad_id=self.squad_id,
                      item_id=self.item_id, loss_id='loss-p08', kind='consumed',
                      amount='3', expected_version='0', evidence='模拟现场手工报告-1',
                      occurred_at='2026-09-29T09:20:00+08:00', reason='',
                      request_id='loss-request-p08')
        values.update(changes)
        return self.state.handle_manual_material_loss(self.event_id, **values)

    def snapshot_line(self):
        return self.state.ledger.snapshots[self.event_id].squads[self.squad_id].lines[self.item_id]

    def event_space(self):
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        return load_workspace(target, general_auth=self.auth)

    def test_named_report_correction_and_read_only_display(self):
        self.arrive()
        first = self.report()
        self.assertEqual(self.CAPTAIN, first.actor_id)
        self.assertEqual('reported', first.data_kind)
        self.assertEqual('人工报告：模拟现场手工报告-1', first.source)
        self.assertEqual(3, self.snapshot_line().consumed)
        self.assertEqual(3, self.state.ledger.compute_gap(self.event_id, {self.item_id: 10})[0]['gap'])
        self.assertEqual(1, sum(e.kind == REPORTED for e in self.event_space().events))
        status, page = render_page(self.state, '/captain-arrival',
                                   member_token=self.tokens[self.CAPTAIN])
        self.assertEqual(200, status)
        self.assertIn('模拟水带 · 消耗 3 · 版本 1', page)
        self.assertIn('更正本条报告', page)
        corrected = self.report(amount='1', expected_version='1',
                                evidence='模拟现场复核更正-2', reason='前次计数重复',
                                request_id='loss-correct-p08')
        self.assertEqual(2, corrected.reference.version)
        self.assertEqual(1, self.snapshot_line().consumed)
        self.assertEqual(3, json.loads(first.content_json)['amount'])
        self.assertEqual(2, sum(e.kind == REPORTED for e in self.event_space().events))
        restored = MaterialsLedger.load(self.state.materials_path)
        self.assertEqual(1, restored.snapshots[self.event_id].squads[self.squad_id].lines[self.item_id].consumed)
        self.assertEqual((), self.event_space().verify_audit_chain())

    def test_before_arrival_wrong_actor_and_cross_event_rejected_without_write(self):
        before = self.state.ledger.to_dict()
        with self.assertRaises(ValueError):
            self.report()
        self.assertEqual(before, self.state.ledger.to_dict())
        self.arrive()
        before = self.state.ledger.to_dict()
        for token in (self.tokens[self.MEMBER], self.tokens[self.DEPUTY], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.report(captain_token=token)
        with self.assertRaises(ValueError):
            self.report(item_id='other')
        with self.assertRaises(PanelActionError):
            self.state.handle_manual_material_loss('other-event',
                captain_token=self.tokens[self.CAPTAIN], squad_id=self.squad_id,
                item_id=self.item_id, loss_id='loss-p08', kind='consumed',
                amount='3', expected_version='0', evidence='模拟来源',
                occurred_at='', reason='', request_id='other-request')
        self.assertEqual(before, self.state.ledger.to_dict())

    def test_retry_stale_correction_and_invalid_amount_do_not_double_count(self):
        self.arrive()
        first = self.report()
        self.assertEqual(first, self.report())
        self.assertEqual(1, sum(e.kind == REPORTED for e in self.event_space().events))
        before = self.state.ledger.to_dict()
        for changes in ({'amount': 'nan', 'request_id': 'bad-nan'},
                        {'amount': '11', 'request_id': 'bad-over'},
                        {'occurred_at': '2026-09-29T10:00:00', 'request_id': 'bad-time'},
                        {'amount': '2', 'request_id': 'loss-request-p08'},
                        {'expected_version': '1', 'reason': '', 'request_id': 'bad-correct'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(**changes)
        self.assertEqual(before, self.state.ledger.to_dict())
        self.assertEqual(3, self.snapshot_line().consumed)

    def test_internal_loss_cannot_be_corrected_through_captain_page(self):
        self.arrive()
        MaterialFlowService(self.state.ledger, self.event_id).record_loss(
            ObjectReference(self.event_id, 'material_loss', 'internal-p08', 1),
            squad_id=self.squad_id, item_id=self.item_id, kind='damaged', amount=1,
            expected_version=0, actor_id=self.DUTY, source='内部统计',
            request_id='internal-request-p08')
        self.state._save_ledger()
        status, page = render_page(self.state, '/captain-arrival',
                                   member_token=self.tokens[self.CAPTAIN])
        self.assertEqual(200, status)
        self.assertNotIn('内部统计', page)
        with self.assertRaisesRegex(ValueError, '不属于人工报告入口'):
            self.report(loss_id='internal-p08', kind='damaged',
                        expected_version='1', reason='尝试跨入口更正',
                        request_id='wrong-correction-p08')
        self.assertEqual(1, self.snapshot_line().damaged)

    def test_workspace_save_failure_can_be_repaired_by_same_request(self):
        self.arrive()
        with patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated save failure')):
            with self.assertRaises(OSError):
                self.report()
        # P14: the whole action now rolls back, including the material ledger.
        self.assertEqual(0, MaterialsLedger.load(self.state.materials_path)
                         .snapshots[self.event_id].squads[self.squad_id].lines[self.item_id].consumed)
        self.assertEqual(0, sum(e.kind == REPORTED for e in self.event_space().events))
        self.state = PanelState(
            ScenarioWorkspace('p08-restart', general_auth=self.auth), auth=self.auth,
            registry_path=self.root / 'registry.json',
            materials_path=self.root / 'materials.json',
            reviews_path=self.root / 'reviews.json',
            alarm_inbox_path=self.root / 'alarms.json',
            duty_subjects=(self.DUTY,))
        self.tokens[self.CAPTAIN], error = self.state.member_login(self.CAPTAIN, '123456')
        self.assertEqual('', error)
        self.report()
        self.assertEqual(1, sum(e.kind == REPORTED for e in self.event_space().events))
        self.assertEqual(3, self.snapshot_line().consumed)

    def test_ledger_save_failure_does_not_expose_a_half_report(self):
        self.arrive()
        before = self.state.ledger.to_dict()
        with patch.object(MaterialsLedger, 'save', side_effect=OSError('simulated ledger failure')):
            with self.assertRaises(OSError):
                self.report()
        self.assertEqual(before, self.state.ledger.to_dict())
        self.assertEqual(0, sum(e.kind == REPORTED for e in self.event_space().events))
        self.report()
        self.assertEqual(1, sum(e.kind == REPORTED for e in self.event_space().events))

    def test_http_member_gate_and_legacy_check_hidden_after_arrival(self):
        self.arrive()
        commander_token = self.state.login('p08-manual-report-only')
        self.state.handle_enter_event(self.event_id)
        status, page = render_page(self.state, '/materials',
                                   member_token=self.tokens[self.CAPTAIN])
        self.assertEqual(200, status)
        self.assertIn('无需另行核对', page)
        self.assertNotIn('action="/materials/check"', page)
        status, commander_page = render_page(self.state, '/materials',
                                             session_token=commander_token)
        self.assertEqual(200, status)
        self.assertNotIn('action="/materials/check"', commander_page)
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(type('NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())
        form = dict(event_id=self.event_id, squad_id=self.squad_id,
                    item_id=self.item_id, loss_id='loss-http-p08', kind='damaged',
                    amount='2', expected_version='0', evidence='模拟手工损坏报告',
                    occurred_at='', reason='', request_id='http-loss-p08')
        def post(token):
            request = urllib.request.Request(base + '/captain-arrival/material-loss',
                data=urllib.parse.urlencode(form).encode(),
                headers={'Cookie': f'member_session={token}'})
            try:
                return opener.open(request).status
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code
                finally:
                    exc.close()
        self.assertEqual(403, post(self.tokens[self.MEMBER]))
        with patch('panda_brain.persistence.save_workspace', side_effect=OSError('simulated save failure')):
            self.assertEqual(503, post(self.tokens[self.CAPTAIN]))
        self.assertEqual(0, sum(e.kind == REPORTED for e in self.event_space().events))
        self.assertEqual(303, post(self.tokens[self.CAPTAIN]))
        self.assertEqual(2, self.snapshot_line().damaged)
        self.assertEqual(1, sum(e.kind == REPORTED for e in self.event_space().events))


if __name__ == '__main__':
    unittest.main()
