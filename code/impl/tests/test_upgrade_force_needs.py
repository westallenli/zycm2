"""P03-11 qualitative force needs and source-bound reassessment cues."""

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
from panda_brain.force_needs import REVIEWED, need_view
from panda_brain.models import (
    ExecutionOrder, IntelItem, OrderStatus, PersonRecord,
)
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.sop import force_review_cue
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace


class ForceNeedsTests(unittest.TestCase):
    DUTY = "13999996001"
    DEPUTY = "13999996002"
    CAPTAIN = "13999996003"
    MEMBER = "13999996004"
    COMM = "13999996005"
    MED = "13999996006"
    OTHER = "13999996007"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-force-needs-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-force-needs", general_auth=self.auth),
            auth=self.auth, registry_path=root / "registry.json",
            materials_path=root / "materials.json", reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json", duty_subjects=(self.DUTY,),
        )
        people = (
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="前指"),
            PersonRecord(name="队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
            PersonRecord(name="作战队员", phone=self.MEMBER, unit="一中队"),
            PersonRecord(name="通信保障", phone=self.COMM, unit="保障单位"),
            PersonRecord(name="医疗保障", phone=self.MED, unit="医疗单位"),
            PersonRecord(name="无关人员", phone=self.OTHER, unit="其他"),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="初步人力需求测试", report_no="P03-11",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN, self.MEMBER),
            position_slots=("pjjz-1",), leader_phone=self.CAPTAIN,
            library=self.state.registry.library,
        )
        self.squad_id = squad.squad_id
        self.state.handle_event_action(
            self.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-011",
            request_id="appoint-011", occurred_at="2026-09-28T09:00:00+08:00",
        )
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad_id], channel="phone", request_id="select-011",
        )

    def view(self):
        return self.state.event_force_needs(self.event_id)

    def review(self, **changes):
        values = dict(
            deputy_token=self.tokens[self.DEPUTY],
            note="先核对作战与通信医疗保障需求，待任务和现场资料完善",
            expected_fingerprint=self.view().fingerprint,
            request_id="review-011",
        )
        return self.state.handle_force_needs_review(
            self.event_id, **(values | changes)
        )

    def add_temp(self, phone, group):
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[phone], qr_id=self.qr_id,
        )
        self.state.handle_temporary_assign(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phones=[phone], pos_code=group, request_id=f"assign-{phone}",
        )
        self.state.handle_temporary_arrival(
            self.event_id, member_token=self.tokens[phone]
        )

    def test_initial_needs_are_qualitative_across_combat_and_support(self):
        view = self.view()
        self.assertEqual("initial_pending", view.state)
        self.assertEqual(4, len(view.rows))
        self.assertEqual({"作战力量", "通信保障", "医疗保障", "后勤及其他保障"},
                         {row.label for row in view.rows})
        self.assertTrue(all(row.status == "需求人数待核定" for row in view.rows))
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("首次启动后待核定初步人力需求", page)
        self.assertIn("缺少专业参数时不生成需求缺口数", page)
        self.assertNotIn("需增派 3 人", page)
        self.assertEqual(404, render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.OTHER],
            query={"event_id": [self.event_id]})[0])

    def test_review_is_deputy_only_idempotent_persistent_and_not_a_dispatch(self):
        before = self.state.ledger.snapshots.copy()
        first = self.review()
        self.assertEqual(first, self.review())
        self.assertEqual("current", self.view().state)
        self.assertEqual(before, self.state.ledger.snapshots)
        with self.assertRaises(ValueError):
            self.review(note="不同内容")
        for token in (self.tokens[self.DUTY], self.tokens[self.CAPTAIN],
                      self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.review(deputy_token=token, request_id="review-denied")
        space, target, _ = self.state._appointment_space(self.event_id)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual(1, sum(e.kind == REVIEWED for e in restored.events))
        self.assertEqual("current", need_view(
            restored, self.event_id, record=self.state.registry.events[self.event_id],
            people=self.state.event_group_people(self.event_id),
            readiness=self.state.event_readiness(self.event_id)).state)

    def test_arrival_and_readiness_changes_trigger_reassessment_without_counting_need(self):
        self.review()
        self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, qr_id=self.qr_id,
        )
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, mode="initial", phones=[self.MEMBER],
            request_id="arrival-011",
        )
        view = self.view()
        self.assertEqual("needs_reassessment", view.state)
        self.assertIn("personnel", view.changed_sources)
        combat = next(row for row in view.rows if row.key == "combat")
        self.assertEqual((1, 0), (combat.arrived_reported, combat.standby_reported))
        self.review(request_id="review-012")
        self.state.handle_captain_readiness(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, phones=[self.CAPTAIN], status="standby",
            basis="队长点名待命", order_id="", request_id="ready-011",
        )
        combat = next(row for row in self.view().rows if row.key == "combat")
        self.assertEqual((1, 1), (combat.arrived_reported, combat.standby_reported))
        self.assertEqual("需求人数待核定", combat.status)
        self.assertEqual("needs_reassessment", self.view().state)

    def test_communication_and_medical_supply_are_separate_facts(self):
        self.add_temp(self.COMM, "log-comm")
        self.add_temp(self.MED, "log-medical")
        view = self.view()
        comm = next(row for row in view.rows if row.key == "communication")
        med = next(row for row in view.rows if row.key == "medical")
        self.assertEqual((1, 0), (comm.arrived_reported, comm.standby_reported))
        self.assertEqual((1, 0), (med.arrived_reported, med.standby_reported))
        self.assertEqual("需求人数待核定", comm.status)
        self.assertEqual("需求人数待核定", med.status)

    def test_task_situation_and_event_changes_each_reopen_review(self):
        self.review()
        space, target, _ = self.state._appointment_space(self.event_id)
        space.orders["ord-011"] = ExecutionOrder(
            id="ord-011", decision_id="dec-x", option_id="opt-x",
            scope="演示任务", status=OrderStatus.ACTIVE,
        )
        save_workspace(space, target)
        self.assertEqual(("tasks",), self.view().changed_sources)
        self.review(request_id="review-012")
        space, target, _ = self.state._appointment_space(self.event_id)
        space.record_intel(IntelItem(
            id="intel-011", subject="风向", summary="现场报告风向变化待核实",
            source="演示人工报告",
        ))
        save_workspace(space, target)
        self.assertEqual(("situation",), self.view().changed_sources)
        self.review(request_id="review-013")
        old = self.state.registry.events[self.event_id]
        self.state.registry.events[self.event_id] = replace(
            old, initial_report="火情范围新报告待核实"
        )
        self.assertEqual(("event",), self.view().changed_sources)

    def test_commander_gets_live_qualitative_force_review_cue(self):
        self.state.login("p03-force-needs-secret")
        self.state.handle_enter_event(self.event_id)
        version = self.state.workspace.version
        self.assertEqual("initial_pending", force_review_cue(self.state)["state"])
        self.assertIn("初步人力需求尚未", render_sand_page(self.state, role="commander"))
        self.assertNotIn('id="cmd-force-review"',
                         render_sand_page(self.state, role="member"))
        self.assertNotIn('id="cmd-force-review"',
                         render_sand_page(self.state, role="public"))
        self.review()
        self.assertEqual("current", force_review_cue(self.state)["state"])
        self.assertIn('id="cmd-force-review" data-state="current" hidden',
                      render_sand_page(self.state, role="commander"))
        self.state.workspace.record_intel(IntelItem(
            id="intel-force-cue", subject="风向", summary="风向变化待核",
            source="现场报告"))
        cue = force_review_cue(self.state)
        self.assertEqual("needs_reassessment", cue["state"])
        self.assertIn("火情与态势资料", cue["message"])
        self.assertIn("到场或待命人数不等于需求人数", cue["message"])
        self.assertIn(cue["message"], render_sand_page(self.state, role="commander"))
        self.assertEqual(version + 2, self.state.workspace.version)
        self.state.workspace.events[-1].payload["source"] = "被篡改来源"
        self.assertEqual("unavailable", force_review_cue(self.state)["state"])
        self.assertNotIn("复核依据已变化（火情与态势资料）",
                         render_sand_page(self.state, role="commander"))

    def test_stale_form_and_unrelated_audit_do_not_falsify_reassessment(self):
        old_fingerprint = self.view().fingerprint
        self.add_temp(self.COMM, "log-comm")
        with self.assertRaises(ValueError):
            self.review(expected_fingerprint=old_fingerprint)
        self.review()
        space, target, _ = self.state._appointment_space(self.event_id)
        space.append_event("UnrelatedDiagnosticRecorded", note="不影响人力输入")
        save_workspace(space, target)
        self.assertEqual("current", self.view().state)
        space, _, _ = self.state._appointment_space(self.event_id)
        valid = self.view().latest_review
        space.append_event(REVIEWED, **(valid | dict(
            actor=self.OTHER, request_id="forged-review-011",
        )))
        with self.assertRaises(ValueError):
            need_view(space, self.event_id,
                      record=self.state.registry.events[self.event_id],
                      people=self.state.event_group_people(self.event_id),
                      readiness=self.state.event_readiness(self.event_id))

    def test_http_scope_and_other_event_isolation(self):
        self.review()
        other = self.state.registry.create_event(
            name="另一场火情", report_no="P03-11-B",
            discovered_at="2026-09-28T10:00:00+08:00", source="测试建档",
            location="邻县", created_by="fixture",
        )
        self.state.handle_event_action(
            other.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.assertEqual("initial_pending", self.state.event_force_needs(other.event_id).state)
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def post(phone):
            data = urllib.parse.urlencode(dict(
                event_id=self.event_id, note="再次复核当前资料",
                expected_fingerprint=self.view().fingerprint,
                request_id="http-review-011",
            )).encode()
            req = urllib.request.Request(
                base + "/personnel/force-needs/review", data=data, method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(403, post(self.OTHER))
        self.assertEqual(303, post(self.DEPUTY))
        self.assertEqual("initial_pending", self.state.event_force_needs(other.event_id).state)


if __name__ == "__main__":
    unittest.main()
