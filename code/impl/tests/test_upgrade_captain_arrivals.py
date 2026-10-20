"""P03-05: captain QR loads a frozen roster before reported team attendance."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError, PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CaptainArrivalTests(unittest.TestCase):
    DUTY = "13999990001"
    DEPUTY = "13999990002"
    CAPTAIN = "13999990003"
    MEMBER_A = "13999990004"
    MEMBER_B = "13999990005"
    STRANGER = "13999990006"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-arrival-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-arrival", general_auth=self.auth), auth=self.auth,
            registry_path=root / "registry.json", materials_path=root / "materials.json",
            reviews_path=root / "reviews.json", alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        people = [
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="指挥部"),
            PersonRecord(name="一队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
            PersonRecord(name="队员甲", phone=self.MEMBER_A, unit="一中队"),
            PersonRecord(name="队员乙", phone=self.MEMBER_B, unit="一中队"),
            PersonRecord(name="其他人", phone=self.STRANGER, unit="其他"),
        ]
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="模拟火情", report_no="P03-05",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.squad = self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN, self.MEMBER_A, self.MEMBER_B),
            leader_phone=self.CAPTAIN, library=self.state.registry.library,
        )
        item = self.state.ledger.upsert_item(name="水带", unit="盘", default_carry_qty=6)
        self.item_id = item.item_id
        self.state.ledger.set_carry_line(self.squad.squad_id, item.item_id)
        self.state._save_ledger()
        self.state.handle_event_action(self.event_id, "start", duty_token=self.tokens[self.DUTY])
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-001",
            request_id="appoint-001", occurred_at="2026-09-28T09:00:00+08:00",
        )
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad.squad_id], channel="phone", request_id="select-001",
        )

    def scan(self, **changes):
        values = dict(captain_token=self.tokens[self.CAPTAIN],
                      squad_id=self.squad.squad_id, qr_id=self.qr_id)
        return self.state.handle_captain_scan(self.event_id, **{**values, **changes})

    def report(self, *, mode="initial", phones=None, request_id="arrival-001", **changes):
        values = dict(captain_token=self.tokens[self.CAPTAIN],
                      squad_id=self.squad.squad_id, mode=mode,
                      phones=[] if phones is None else phones, request_id=request_id)
        return self.state.handle_captain_arrival(self.event_id, **{**values, **changes})

    def test_scan_freezes_roster_without_arrival_or_inventory(self):
        for token in (self.tokens[self.MEMBER_A], self.tokens[self.DEPUTY], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.scan(captain_token=token)
        with self.assertRaises(ValueError):
            self.scan(qr_id="unknown")
        loaded = self.scan()
        self.assertEqual((self.CAPTAIN, self.MEMBER_A, self.MEMBER_B), loaded.phones)
        self.state.ledger.squads[self.squad.squad_id] = replace(
            self.squad, member_phones=(self.CAPTAIN, self.MEMBER_A)
        )
        self.assertEqual({}, self.state.ledger.snapshots)
        self.assertEqual({}, self.state.ledger.flow_records)
        self.assertEqual(loaded, self.scan())
        self.assertEqual(1, sum(e.kind == "SquadRosterLoaded" for e in
                                load_workspace(Path(self.state.registry.events[self.event_id].snapshot_path),
                                               general_auth=self.auth).events))
        status, page = render_page(self.state, "/captain-arrival",
                                   member_token=self.tokens[self.CAPTAIN])
        self.assertEqual(200, status)
        self.assertIn("名册已载入，尚未确认任何人到场", page)
        self.assertEqual(404, render_page(self.state, "/captain-arrival",
                                           member_token=self.tokens[self.MEMBER_A])[0])

    def test_wrong_event_qr_revoked_qr_and_unselected_squad_fail(self):
        unselected = self.state.ledger.add_squad(
            name="未选中中队", member_phones=(self.CAPTAIN,),
            leader_phone=self.CAPTAIN, library=self.state.registry.library,
        )
        with self.assertRaises(PermissionError):
            self.scan(squad_id=unselected.squad_id)
        other = self.state.registry.create_event(
            name="另一场火情", report_no="P03-05-B",
            discovered_at="2026-09-28T08:30:00+08:00", source="测试建档",
            location="另一县", created_by="fixture",
        )
        self.state.handle_event_action(other.event_id, "start",
                                       duty_token=self.tokens[self.DUTY])
        other_qr = next(q.qr_id for q in self.state.registry.qrs.values()
                        if q.event_id == other.event_id)
        with self.assertRaises(ValueError):
            self.scan(qr_id=other_qr)
        self.state.registry.revoke_qr(self.qr_id, revoked_by=self.DUTY)
        with self.assertRaises(ValueError):
            self.scan()
        self.assertIsNone(self.state.captain_loaded_roster(self.event_id, self.squad.squad_id))

    def test_partial_arrival_then_supplement_does_not_double_count_carry(self):
        self.scan()
        initial = self.report(phones=[self.MEMBER_A, self.MEMBER_B])
        self.assertEqual(1, initial.reference.version)
        snapshot = self.state.ledger.snapshots[self.event_id].squads[self.squad.squad_id]
        self.assertEqual({self.CAPTAIN}, set(snapshot.arrived_phones))
        self.assertEqual(6, snapshot.lines[self.item_id].qty_registered)
        first_arrival_id = snapshot.arrival_id
        supplement = self.report(
            mode="supplement", phones=[self.MEMBER_A], request_id="arrival-002"
        )
        self.assertEqual(2, supplement.reference.version)
        final = self.report(
            mode="supplement", phones=[self.MEMBER_B], request_id="arrival-003"
        )
        self.assertEqual(3, final.reference.version)
        snapshot = self.state.ledger.snapshots[self.event_id].squads[self.squad.squad_id]
        self.assertEqual(set(self.squad.member_phones), set(snapshot.arrived_phones))
        self.assertEqual(first_arrival_id, snapshot.arrival_id)
        self.assertEqual(6, snapshot.lines[self.item_id].qty_registered)
        self.assertEqual(initial.times.occurred_at,
                         self.state.member_session(self.tokens[self.CAPTAIN]).arrived_at)
        self.assertEqual(3, len(self.state.ledger.flow_records))
        self.assertEqual(supplement, self.report(
            mode="supplement", phones=[self.MEMBER_A], request_id="arrival-002"
        ))
        with self.assertRaises(ValueError):
            self.report(mode="supplement", phones=[self.MEMBER_B], request_id="arrival-004")
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        self.assertEqual((), load_workspace(target, general_auth=self.auth).verify_audit_chain())
        self.assertEqual(3, sum(e.kind == "SquadArrivalReported" for e in
                                load_workspace(target, general_auth=self.auth).events))

    def test_full_arrival_and_invalid_reports(self):
        with self.assertRaises(ValueError):
            self.report()
        self.scan()
        for missing in ([self.CAPTAIN], [self.STRANGER], [self.MEMBER_A, self.MEMBER_A]):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.report(phones=missing)
        for token in (self.tokens[self.MEMBER_A], self.tokens[self.DEPUTY], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.report(captain_token=token)
        flow = self.report()
        self.assertEqual(set(self.squad.member_phones),
                         set(json.loads(flow.content_json)["arrived_phones"]))
        with self.assertRaises(ValueError):
            self.report(request_id="arrival-002")
        self.assertEqual(flow, self.report())
        self.state.member_logout(self.tokens[self.CAPTAIN])
        again, error = self.state.member_login(self.CAPTAIN, "123456")
        self.assertEqual("", error)
        self.assertEqual(self.event_id, self.state._effective_bound_event(
            self.state.member_session(again)))

    def test_old_individual_checkin_cannot_bypass_squad_flow(self):
        self.state.registry.add_roster_entry(
            self.event_id, self.MEMBER_A, pos_code="pjjz-1", added_by="fixture"
        )
        member = self.tokens[self.MEMBER_A]
        self.assertEqual("squad_captain_flow", self.state.member_scan(member, self.qr_id).kind)
        with self.assertRaises(PanelActionError):
            self.state.member_confirm(member, self.qr_id)
        with self.assertRaises(PanelActionError):
            self.state.member_confirm(self.tokens[self.CAPTAIN], self.qr_id)
        self.assertEqual({}, self.state.ledger.snapshots)

    def test_http_captain_flow_and_other_role_denials(self):
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, phone, fields=None):
            data = urllib.parse.urlencode(fields, doseq=True).encode() if fields else None
            req = urllib.request.Request(
                base + path, data=data, method="POST" if fields else "GET",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        fields = {"event_id": self.event_id, "squad_id": self.squad.squad_id,
                  "qr_id": self.qr_id}
        self.assertEqual(403, request("/captain-arrival/scan", self.MEMBER_A, fields)[0])
        self.assertEqual(303, request("/captain-arrival/scan", self.CAPTAIN, fields)[0])
        status, page = request("/captain-arrival", self.CAPTAIN)
        self.assertEqual(200, status)
        self.assertIn("仅队长扫码匹配", page)
        self.assertIn("未到人员", page)
        report = {"event_id": self.event_id, "squad_id": self.squad.squad_id,
                  "mode": "initial", "phone": [self.MEMBER_B],
                  "request_id": "http-arrival-001"}
        self.assertEqual(403, request("/captain-arrival/report", self.MEMBER_A, report)[0])
        self.assertEqual(303, request("/captain-arrival/report", self.CAPTAIN, report)[0])
        status, page = request("/captain-arrival", self.CAPTAIN)
        self.assertEqual(200, status)
        self.assertIn("已申报到场 2/3 人", page)


if __name__ == "__main__":
    unittest.main()
