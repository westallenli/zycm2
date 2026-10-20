"""P03-06 temporary personnel registration, presence and deputy assignment."""

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
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError, PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class TemporaryPersonnelTests(unittest.TestCase):
    DUTY = "13999991001"
    DEPUTY = "13999991002"
    OTHER = "13999991003"
    TEMP_A = "13999991004"
    TEMP_B = "13999991005"
    CAPTAIN = "13999991006"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-temp-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-temp", general_auth=self.auth), auth=self.auth,
            registry_path=root / "registry.json", materials_path=root / "materials.json",
            reviews_path=root / "reviews.json", alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        people = (
            PersonRecord(name="值班", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="指定副总指挥", phone=self.DEPUTY, unit="指挥部"),
            PersonRecord(name="其他人", phone=self.OTHER, unit="其他"),
            PersonRecord(name="临时甲", phone=self.TEMP_A, unit="属地"),
            PersonRecord(name="临时乙", phone=self.TEMP_B, unit="属地"),
            PersonRecord(name="队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="临时人员测试", report_no="P03-06",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN,),
            leader_phone=self.CAPTAIN, library=self.state.registry.library,
        )
        self.state.handle_event_action(self.event_id, "start",
                                       duty_token=self.tokens[self.DUTY])
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-006",
            request_id="appoint-006", occurred_at="2026-09-28T09:00:00+08:00",
        )

    def scan(self, phone=TEMP_A, **changes):
        args = dict(member_token=self.tokens[phone], qr_id=self.qr_id)
        return self.state.handle_temporary_scan(self.event_id, **(args | changes))

    def assign(self, phones=None, **changes):
        args = dict(deputy_token=self.tokens[self.DEPUTY],
                    phones=[self.TEMP_A] if phones is None else phones,
                    pos_code="tech-info", request_id="assign-006")
        return self.state.handle_temporary_assign(self.event_id, **(args | changes))

    def test_personal_scan_is_separate_and_idempotent(self):
        for token in (self.tokens[self.CAPTAIN], None):
            with self.subTest(token=token), self.assertRaises((ValueError, PermissionError)):
                self.scan(member_token=token)
        with self.assertRaises(ValueError):
            self.scan(qr_id="invalid")
        person = self.scan()
        self.assertEqual("", person.arrived_at)
        self.assertEqual("", person.pos_code)
        self.assertEqual(person, self.scan())
        self.assertEqual({}, self.state.ledger.snapshots)
        self.assertEqual({}, self.state.ledger.flow_records)
        self.assertFalse(self.state.member_session(self.tokens[self.TEMP_A]).onboarded)
        self.state.registry.add_roster_entry(
            self.event_id, self.TEMP_A, pos_code="tech-info", added_by="fixture"
        )
        self.assertEqual("temporary_personnel", self.state.member_scan(
            self.tokens[self.TEMP_A], self.qr_id).kind)
        with self.assertRaises(PanelActionError):
            self.state.member_confirm(self.tokens[self.TEMP_A], self.qr_id)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        saved = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), saved.verify_audit_chain())
        self.assertEqual(1, sum(e.kind == "TemporaryPersonRegistered" for e in saved.events))

    def test_arrival_is_separate_from_registration_and_assignment(self):
        self.scan()
        self.assign()
        before = self.state.temporary_people(self.event_id)[self.TEMP_A]
        self.assertEqual("", before.arrived_at)
        token = self.tokens[self.TEMP_A]
        reported = self.state.handle_temporary_arrival(self.event_id, member_token=token)
        self.assertTrue(reported.arrived_at)
        self.assertEqual(reported, self.state.handle_temporary_arrival(
            self.event_id, member_token=token))
        self.assertEqual({}, self.state.ledger.snapshots)
        self.assertEqual({}, self.state.ledger.flow_records)
        status, page = render_page(self.state, "/temporary", member_token=token)
        self.assertEqual(200, status)
        self.assertIn("独立工作台已暂停", page)
        self.assertTrue(self.state.temporary_people(self.event_id)[self.TEMP_A].arrived_at)
        self.state.member_logout(token)
        relogged, error = self.state.member_login(self.TEMP_A, "123456")
        self.assertEqual("", error)
        self.assertEqual(200, render_page(self.state, "/temporary",
                                          member_token=relogged)[0])

    def test_only_appointed_deputy_may_batch_assign_atomically(self):
        self.scan()
        self.scan(phone=self.TEMP_B)
        for token in (self.tokens[self.DUTY], self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.assign(deputy_token=token)
        with self.assertRaises(ValueError):
            self.assign(phones=[self.TEMP_A, self.OTHER])
        self.assertEqual("", self.state.temporary_people(self.event_id)[self.TEMP_A].pos_code)
        with self.assertRaises(ValueError):
            self.assign(pos_code="made-up")
        result = self.assign(phones=[self.TEMP_A, self.TEMP_B])
        self.assertEqual("tech-info", result["pos_code"])
        self.assertEqual(result, self.assign(phones=[self.TEMP_A, self.TEMP_B]))
        with self.assertRaises(ValueError):
            self.assign(phones=[self.TEMP_B])
        with self.assertRaises(ValueError):
            self.assign(request_id="assign-007")
        self.assertEqual("tech-info", self.state.temporary_people(
            self.event_id)[self.TEMP_B].pos_code)
        status,paused=render_page(self.state, "/personnel/temporary",member_token=self.tokens[self.OTHER])
        self.assertEqual(200,status); self.assertIn("独立工作台已暂停",paused)
        status, page = render_page(self.state, "/temporary/assign",
                                   member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn("已编组 2 人", page)

    def test_wrong_event_qr_and_cross_event_registration_are_refused(self):
        other = self.state.registry.create_event(
            name="另一事件", report_no="P03-06-B",
            discovered_at="2026-09-28T10:00:00+08:00", source="测试建档",
            location="另一县", created_by="fixture",
        )
        self.state.handle_event_action(other.event_id, "start",
                                       duty_token=self.tokens[self.DUTY])
        other_qr = next(q.qr_id for q in self.state.registry.qrs.values()
                        if q.event_id == other.event_id)
        with self.assertRaises(ValueError):
            self.scan(qr_id=other_qr)
        self.scan()
        with self.assertRaises(PanelActionError):
            self.state.handle_temporary_scan(
                other.event_id, member_token=self.tokens[self.TEMP_A], qr_id=other_qr,
            )
        self.state.registry.revoke_qr(self.qr_id, revoked_by=self.DUTY)
        with self.assertRaises(ValueError):
            self.scan(phone=self.TEMP_B)

    def test_http_routes_enforce_named_member_and_deputy(self):
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def request(path, phone=None, fields=None):
            data = urllib.parse.urlencode(fields, doseq=True).encode() if fields else None
            req = urllib.request.Request(
                base + path, data=data, method="POST" if fields else "GET",
                headers={"Cookie": f"member_session={self.tokens[phone]}"} if phone else {},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        scan = {"event_id": self.event_id, "qr_id": self.qr_id}
        self.assertEqual(403, request("/temporary/scan", fields=scan)[0])
        self.assertEqual(303, request("/temporary/scan", self.TEMP_A, scan)[0])
        assignment = {"event_id": self.event_id, "phone": self.TEMP_A,
                      "pos_code": "tech-info", "request_id": "http-assign"}
        self.assertEqual(403, request("/temporary/assign", self.OTHER, assignment)[0])
        self.assertEqual(303, request("/temporary/assign", self.DEPUTY, assignment)[0])
        self.assertEqual(303, request("/temporary", self.TEMP_A)[0])
        status, page = request("/personnel/temporary", self.TEMP_A)
        self.assertEqual(200, status)
        self.assertIn("独立工作台已暂停", page)
        self.assertEqual(303, request("/temporary/arrive", self.TEMP_A,
                                      {"event_id": self.event_id})[0])
        self.assertTrue(self.state.temporary_people(self.event_id)[self.TEMP_A].arrived_at)
        self.assertIn("独立工作台已暂停",request("/personnel/temporary",self.TEMP_A)[1])


if __name__ == "__main__":
    unittest.main()
