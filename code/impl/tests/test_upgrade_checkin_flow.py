"""P03-13: one event's cross-role check-in with a clearly simulated alarm."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError, PanelState
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class CheckinFlowTests(unittest.TestCase):
    DUTY = "13999995001"
    DEPUTY = "13999995002"
    CAPTAIN = "13999995003"
    MEMBER_A = "13999995004"
    MEMBER_B = "13999995005"
    TEMP = "13999995006"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-flow-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-flow", general_auth=self.auth), auth=self.auth,
            registry_path=root / "registry.json", materials_path=root / "materials.json",
            reviews_path=root / "reviews.json", alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        self.assertIsNotNone(self.state.login("p03-flow-secret"))
        people = (
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="指挥部"),
            PersonRecord(name="队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
            PersonRecord(name="队员甲", phone=self.MEMBER_A, unit="一中队"),
            PersonRecord(name="队员乙", phone=self.MEMBER_B, unit="一中队"),
            PersonRecord(name="属地协助", phone=self.TEMP, unit="属地"),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        self.squad = self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN, self.MEMBER_A, self.MEMBER_B),
            leader_phone=self.CAPTAIN, library=self.state.registry.library,
        )
        item = self.state.ledger.upsert_item(name="水带", unit="盘", default_carry_qty=6)
        self.item_id = item.item_id
        self.state.ledger.set_carry_line(self.squad.squad_id, item.item_id)
        self.state._save_ledger()

    def _demo_event(self):
        alarm_id = self.state.handle_alarm_demo_ingest()
        alarm = self.state.alarm_inbox.alarms[alarm_id]
        self.assertEqual("created", alarm.status)
        event_id = alarm.event_id
        self.assertEqual("registered", self.state.registry.events[event_id].status)
        self.assertEqual({}, self.state.registry.qrs)
        return alarm_id, event_id

    def test_demo_alarm_to_missing_person_supplement_and_temporary_assignment(self):
        alarm_id, event_id = self._demo_event()
        with self.assertRaises(PermissionError):
            self.state.handle_event_action(
                event_id, "start", duty_token=self.tokens[self.DEPUTY])
        started = self.state.handle_event_action(
            event_id, "start", duty_token=self.tokens[self.DUTY])
        qr_id = started["qr_id"]
        target = Path(self.state.registry.events[event_id].snapshot_path)
        saved = load_workspace(target, general_auth=self.auth)
        confirmations = [e.payload for e in saved.events
                         if e.kind == "ExternalFireEventReceived"]
        self.assertEqual(1, len(confirmations))
        self.assertEqual(alarm_id, confirmations[0]["alarm_id"])
        self.assertEqual("simulated", confirmations[0]["data_kind"])
        self.assertEqual(self.state.alarm_inbox.alarms[alarm_id].raw_digest,
                         confirmations[0]["raw_digest"])
        self.assertEqual("pre_start_inbox_baseline", confirmations[0]["source"])
        with self.assertRaises(PanelActionError):
            self.state.handle_event_action(
                event_id, "start", duty_token=self.tokens[self.DUTY])
        with self.assertRaises(PermissionError):
            self.state.handle_select_squads(
                event_id, deputy_token=self.tokens[self.CAPTAIN],
                squad_ids=[self.squad.squad_id], channel="phone",
                request_id="select-flow")
        self.state.handle_deputy_appointment(
            event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-013",
            request_id="appoint-flow", occurred_at="2026-09-28T09:00:00+08:00")
        self.state.handle_select_squads(
            event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad.squad_id], channel="phone",
            request_id="select-flow")
        self.assertEqual({}, self.state.ledger.snapshots)
        self.state.handle_callout_simulation_failure(
            event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_id=self.squad.squad_id, reason="演练：假设无法拨通",
            request_id="failure-flow")
        self.state.handle_captain_reply(
            event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id, can_depart="yes", expected_people="3",
            eta="2026-09-28T12:00:00+08:00", reason="", request_id="reply-flow")
        loaded = self.state.handle_captain_scan(
            event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id, qr_id=qr_id)
        self.assertEqual(loaded, self.state.handle_captain_scan(
            event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id, qr_id=qr_id))
        self.assertEqual({}, self.state.ledger.snapshots)
        self.state.handle_captain_arrival(
            event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id, mode="initial", phones=[self.MEMBER_B],
            request_id="arrival-flow")
        first = self.state.ledger.snapshots[event_id].squads[self.squad.squad_id]
        self.assertEqual({self.CAPTAIN, self.MEMBER_A}, set(first.arrived_phones))
        self.assertEqual(6, first.lines[self.item_id].qty_registered)
        self.state.handle_captain_arrival(
            event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id, mode="supplement", phones=[self.MEMBER_B],
            request_id="supplement-flow")
        supplemented = self.state.ledger.snapshots[event_id].squads[self.squad.squad_id]
        self.assertEqual(3, len(supplemented.arrived_phones))
        self.assertEqual(first.arrival_id, supplemented.arrival_id)
        self.assertEqual(6, supplemented.lines[self.item_id].qty_registered)
        with self.assertRaises(ValueError):
            self.state.handle_temporary_assign(
                event_id, deputy_token=self.tokens[self.DEPUTY],
                phones=[self.TEMP], pos_code="tech-info", request_id="assign-early")
        registered = self.state.handle_temporary_scan(
            event_id, member_token=self.tokens[self.TEMP], qr_id=qr_id)
        self.assertEqual("", registered.arrived_at)
        self.assertEqual("", registered.pos_code)
        self.state.handle_temporary_assign(
            event_id, deputy_token=self.tokens[self.DEPUTY],
            phones=[self.TEMP], pos_code="tech-info", request_id="assign-flow")
        self.assertEqual("", self.state.temporary_people(event_id)[self.TEMP].arrived_at)
        self.state.handle_temporary_arrival(event_id, member_token=self.tokens[self.TEMP])
        self.assertTrue(self.state.event_group_people(event_id)[self.TEMP].arrived)
        saved = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), saved.verify_audit_chain())
        self.assertEqual(1, sum(e.kind == "ExternalFireEventReceived" for e in saved.events))
        self.assertFalse(any(e.payload.get("transport_state") == "sent"
                             or e.payload.get("delivery_state") == "delivered"
                             for e in saved.events))

    def test_other_event_cannot_reuse_first_qr_or_appointment_and_manual_has_no_alarm(self):
        _, first_id = self._demo_event()
        first_qr = self.state.handle_event_action(
            first_id, "start", duty_token=self.tokens[self.DUTY])["qr_id"]
        second = self.state.registry.create_event(
            name="另一场人工登记", report_no="P03-13-MANUAL",
            discovered_at="2026-09-28T10:00:00+08:00", source="人工登记",
            location="邻县", created_by=self.DUTY)
        second_id = second.event_id
        second_qr = self.state.handle_event_action(
            second_id, "start", duty_token=self.tokens[self.DUTY])["qr_id"]
        self.assertNotEqual(first_qr, second_qr)
        self.state.handle_deputy_appointment(
            first_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-013",
            request_id="appoint-first", occurred_at="2026-09-28T09:00:00+08:00")
        with self.assertRaises(PermissionError):
            self.state.handle_select_squads(
                second_id, deputy_token=self.tokens[self.DEPUTY],
                squad_ids=[self.squad.squad_id], channel="phone",
                request_id="select-second")
        with self.assertRaises(ValueError):
            self.state.handle_temporary_scan(
                second_id, member_token=self.tokens[self.TEMP], qr_id=first_qr)
        self.state.handle_temporary_scan(
            first_id, member_token=self.tokens[self.TEMP], qr_id=first_qr)
        with self.assertRaises((PanelActionError, ValueError)):
            self.state.handle_temporary_scan(
                second_id, member_token=self.tokens[self.TEMP], qr_id=second_qr)
        self.assertEqual({}, self.state.temporary_people(second_id))
        manual = load_workspace(
            self.state.registry.events[second_id].snapshot_path, general_auth=self.auth)
        self.assertEqual((), manual.verify_audit_chain())
        self.assertFalse(any(e.kind == "ExternalFireEventReceived" for e in manual.events))

    def test_incomplete_pre_start_alarm_history_blocks_start_without_issuing_qr(self):
        alarm_id, event_id = self._demo_event()
        self.state.alarm_inbox.alarms[alarm_id].history = []
        with self.assertRaisesRegex(PanelActionError, "火情来源历史不完整"):
            self.state.handle_event_action(
                event_id, "start", duty_token=self.tokens[self.DUTY])
        self.assertEqual("registered", self.state.registry.events[event_id].status)
        self.assertEqual({}, self.state.registry.qrs)


if __name__ == "__main__":
    unittest.main()
