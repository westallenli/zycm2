"""P03-07 fixed organization projection and named deputy person transfers."""

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class PersonnelGroupTests(unittest.TestCase):
    DUTY = "13999992001"
    DEPUTY = "13999992002"
    CAPTAIN = "13999992003"
    FIXED = "13999992004"
    TEMP_A = "13999992005"
    TEMP_B = "13999992006"
    OTHER = "13999992007"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-groups-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-groups", general_auth=self.auth), auth=self.auth,
            registry_path=root / "registry.json", materials_path=root / "materials.json",
            reviews_path=root / "reviews.json", alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        persons = (
            PersonRecord(name="值班", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="指定副总指挥", phone=self.DEPUTY, unit="前指"),
            PersonRecord(name="队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
            PersonRecord(name="作战队员", phone=self.FIXED, unit="一中队"),
            PersonRecord(name="临时甲", phone=self.TEMP_A, unit="属地"),
            PersonRecord(name="临时乙", phone=self.TEMP_B, unit="属地"),
            PersonRecord(name="无关人员", phone=self.OTHER, unit="其他"),
        )
        self.state.registry.import_person_library(persons)
        self.tokens = {}
        for person in persons:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="组织投影测试", report_no="P03-07",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN, self.FIXED),
            position_slots=("pjjz-1",), leader_phone=self.CAPTAIN,
            library=self.state.registry.library,
        )
        self.squad_id = squad.squad_id
        self.state.handle_event_action(self.event_id, "start",
                                       duty_token=self.tokens[self.DUTY])
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-007",
            request_id="appoint-007", occurred_at="2026-09-28T09:00:00+08:00",
        )
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad_id], channel="phone", request_id="select-007",
        )

    def load_fixed(self):
        return self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, qr_id=self.qr_id,
        )

    def register_temp(self, phone, pos_code="tech-info"):
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[phone], qr_id=self.qr_id,
        )
        self.state.handle_temporary_assign(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phones=[phone], pos_code=pos_code, request_id=f"initial-{phone}",
        )

    def transfer(self, phone, target, request_id="transfer-007", **changes):
        args = dict(deputy_token=self.tokens[self.DEPUTY], phone=phone,
                    to_pos_code=target, request_id=request_id)
        return self.state.handle_person_group_transfer(
            self.event_id, **(args | changes)
        )

    def test_fixed_roster_group_is_frozen_and_arrival_is_separate(self):
        loaded = self.load_fixed()
        self.assertEqual({"pjjz-1"}, {p["pos_code"] for p in loaded.members})
        self.state.ledger.squads[self.squad_id] = replace(
            self.state.ledger.squads[self.squad_id], position_slots=("pjjz-2",)
        )
        people = self.state.event_group_people(self.event_id)
        self.assertEqual("pjjz-1", people[self.FIXED].pos_code)
        self.assertFalse(people[self.FIXED].arrived)
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, mode="initial", phones=[self.FIXED],
            request_id="arrival-007",
        )
        people = self.state.event_group_people(self.event_id)
        self.assertTrue(people[self.CAPTAIN].arrived)
        self.assertFalse(people[self.FIXED].arrived)
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("本组 2 人 · 已申报到场 1 人", page)

    def test_fixed_person_transfer_keeps_squad_arrival_and_materials(self):
        self.load_fixed()
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, mode="initial", phones=[],
            request_id="arrival-007",
        )
        before = self.state.ledger.snapshots[self.event_id].squads[self.squad_id]
        result = self.transfer(self.FIXED, "tech-info")
        self.assertEqual("pjjz-1", result["from_pos_code"])
        self.assertEqual("tech-info", self.state.event_group_people(
            self.event_id)[self.FIXED].pos_code)
        self.assertEqual(before, self.state.ledger.snapshots[self.event_id].squads[self.squad_id])
        self.assertEqual((self.CAPTAIN, self.FIXED), self.state.captain_loaded_roster(
            self.event_id, self.squad_id).phones)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        self.assertEqual((), load_workspace(target, general_auth=self.auth).verify_audit_chain())
        self.assertEqual(result, self.transfer(self.FIXED, "tech-info"))
        with self.assertRaises(ValueError):
            self.transfer(self.FIXED, "pjjz-2")
        second = self.transfer(self.FIXED, "pjjz-2", request_id="transfer-008")
        self.assertEqual("tech-info", second["from_pos_code"])

    def test_temp_transfer_updates_both_group_views_without_new_arrival(self):
        self.register_temp(self.TEMP_A)
        self.register_temp(self.TEMP_B)
        self.state.handle_temporary_arrival(
            self.event_id, member_token=self.tokens[self.TEMP_A]
        )
        self.transfer(self.TEMP_A, "tech-assess")
        group = self.state.event_group_people(self.event_id)
        self.assertEqual("tech-assess", group[self.TEMP_A].pos_code)
        self.assertEqual("tech-info", group[self.TEMP_B].pos_code)
        self.assertTrue(group[self.TEMP_A].arrived)
        self.assertFalse(group[self.TEMP_B].arrived)
        self.assertEqual({}, self.state.ledger.snapshots)
        status, page = render_page(self.state, "/temporary",
                                   member_token=self.tokens[self.TEMP_A])
        self.assertEqual(200, status)
        self.assertIn("独立工作台已暂停", page)
        status, other_page = render_page(self.state, "/temporary",
                                         member_token=self.tokens[self.TEMP_B])
        self.assertEqual(200, status)
        self.assertIn("独立工作台已暂停", other_page)

    def test_unknown_group_requires_named_deputy_and_no_guessing(self):
        self.state.ledger.squads[self.squad_id] = replace(
            self.state.ledger.squads[self.squad_id], position_slots=()
        )
        self.load_fixed()
        self.assertEqual("", self.state.event_group_people(self.event_id)[self.FIXED].pos_code)
        for token in (self.tokens[self.DUTY], self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.transfer(self.FIXED, "pjjz-1", deputy_token=token)
        with self.assertRaises(ValueError):
            self.transfer(self.OTHER, "pjjz-1")
        with self.assertRaises(ValueError):
            self.transfer(self.FIXED, "invalid")
        result = self.transfer(self.FIXED, "pjjz-1")
        self.assertEqual("", result["from_pos_code"])
        self.assertEqual("pjjz-1", self.state.event_group_people(
            self.event_id)[self.FIXED].pos_code)

    def test_pending_temporary_person_cannot_use_transfer_as_initial_assignment(self):
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[self.TEMP_A], qr_id=self.qr_id,
        )
        with self.assertRaises(ValueError):
            self.transfer(self.TEMP_A, "tech-info")
        self.assertEqual("", self.state.temporary_people(self.event_id)[self.TEMP_A].pos_code)
        self.register_temp(self.TEMP_B)
        self.assertEqual("tech-info", self.state.event_group_people(
            self.event_id)[self.TEMP_B].pos_code)

    def test_group_page_scopes_members_and_http_transfer(self):
        self.load_fixed()
        self.register_temp(self.TEMP_A)
        status,paused=render_page(self.state,"/personnel/groups",member_token=self.tokens[self.OTHER],query={"event_id":[self.event_id]})
        self.assertEqual(200,status); self.assertIn("独立工作台已暂停",paused)
        status, own = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.TEMP_A],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("独立工作台已暂停",own)
        self.assertNotIn("扑救一组（一分区）", own)
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def post(phone):
            fields = dict(event_id=self.event_id, phone=self.FIXED,
                          to_pos_code="tech-info", request_id="http-transfer-007")
            req = urllib.request.Request(
                base + "/personnel/transfer",
                data=urllib.parse.urlencode(fields).encode(), method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(403, post(self.OTHER))
        self.assertEqual(303, post(self.DEPUTY))
        self.assertEqual("tech-info", self.state.event_group_people(
            self.event_id)[self.FIXED].pos_code)


if __name__ == "__main__":
    unittest.main()
