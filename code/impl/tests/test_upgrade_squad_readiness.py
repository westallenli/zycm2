"""P03-09: captain reports partial readiness independently of attendance."""

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
from panda_brain.models import ExecutionOrder, OrderStatus, PersonRecord
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.squad_readiness import REPORTED, readiness_state
from panda_brain.workspace import ScenarioWorkspace


class SquadReadinessTests(unittest.TestCase):
    DUTY = "13999994001"
    DEPUTY = "13999994002"
    CAPTAIN = "13999994003"
    MEMBER_A = "13999994004"
    MEMBER_B = "13999994005"
    OTHER = "13999994006"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-readiness-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-readiness", general_auth=self.auth), auth=self.auth,
            registry_path=root / "registry.json", materials_path=root / "materials.json",
            reviews_path=root / "reviews.json", alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        people = (
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="前指"),
            PersonRecord(name="队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
            PersonRecord(name="队员甲", phone=self.MEMBER_A, unit="一中队"),
            PersonRecord(name="队员乙", phone=self.MEMBER_B, unit="一中队"),
            PersonRecord(name="无关人员", phone=self.OTHER, unit="其他"),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="队内状态测试", report_no="P03-09",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN, self.MEMBER_A, self.MEMBER_B),
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
            subject_id=self.DEPUTY, evidence="线下指定记录-009",
            request_id="appoint-009", occurred_at="2026-09-28T09:00:00+08:00",
        )
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad_id], channel="phone", request_id="select-009",
        )

    def load(self, *, missing=()):
        self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, qr_id=self.qr_id,
        )
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad_id, mode="initial", phones=list(missing),
            request_id="arrival-009",
        )

    def report(self, **changes):
        args = dict(
            captain_token=self.tokens[self.CAPTAIN], squad_id=self.squad_id,
            phones=[self.MEMBER_A], status="standby", basis="队长点名确认可待命",
            order_id="", request_id="ready-009",
        )
        return self.state.handle_captain_readiness(
            self.event_id, **(args | changes)
        )

    def test_arrival_alone_is_not_deployable_and_subset_update_is_independent(self):
        self.load()
        self.assertEqual({}, self.state.event_readiness(self.event_id))
        before = self.state.ledger.snapshots[self.event_id].squads[self.squad_id]
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("已申报到场 3 人 · 队长／组长报告待命可派 0 人", page)
        self.report()
        self.assertEqual("standby", self.state.event_readiness(
            self.event_id)[self.MEMBER_A].status)
        self.assertNotIn(self.MEMBER_B, self.state.event_readiness(self.event_id))
        self.assertEqual(before, self.state.ledger.snapshots[self.event_id].squads[self.squad_id])
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertIn("已申报到场 3 人 · 队长／组长报告待命可派 1 人 · 到场但可派待核定 2 人", page)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual("standby", readiness_state(
            restored, self.event_id, ledger=self.state.ledger)[self.MEMBER_A].status)

    def test_only_selected_current_captain_can_report_arrived_members(self):
        self.load(missing=[self.MEMBER_B])
        for token in (self.tokens[self.DUTY], self.tokens[self.DEPUTY],
                      self.tokens[self.MEMBER_A], self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.report(captain_token=token)
        for changes in ({"phones": [self.MEMBER_B]}, {"phones": [self.OTHER]},
                        {"phones": []}, {"phones": [self.MEMBER_A, self.MEMBER_A]},
                        {"basis": ""}, {"status": "automatic"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(**changes)
        self.assertEqual({}, self.state.event_readiness(self.event_id))

    def test_partial_update_and_request_retry_do_not_rewrite_other_members(self):
        self.load()
        first = self.report(phones=[self.CAPTAIN, self.MEMBER_A])
        self.assertEqual(first, self.report(phones=[self.CAPTAIN, self.MEMBER_A]))
        with self.assertRaises(ValueError):
            self.report(phones=[self.MEMBER_B])
        self.report(phones=[self.MEMBER_A], status="resting", basis="交班轮休",
                    request_id="ready-010")
        states = self.state.event_readiness(self.event_id)
        self.assertEqual("standby", states[self.CAPTAIN].status)
        self.assertEqual("resting", states[self.MEMBER_A].status)
        self.assertNotIn(self.MEMBER_B, states)
        space, _, _ = self.state._appointment_space(self.event_id)
        self.assertEqual(2, sum(e.kind == REPORTED for e in space.events))

    def test_executing_requires_live_order_and_terminal_order_returns_unknown(self):
        self.load()
        with self.assertRaises(ValueError):
            self.report(status="executing")
        order = ExecutionOrder(id="ord-009", decision_id="dec-x", option_id="opt-x",
                               scope="演示", status=OrderStatus.ACTIVE)
        space, target, _ = self.state._appointment_space(self.event_id)
        space.orders[order.id] = order
        save_workspace(space, target)
        self.report(status="executing", order_id=order.id, basis="队长确认正在执行任务")
        self.assertEqual("executing", self.state.event_readiness(
            self.event_id)[self.MEMBER_A].status)
        space, target, _ = self.state._appointment_space(self.event_id)
        space.orders[order.id].status = OrderStatus.COMPLETED
        save_workspace(space, target)
        self.assertNotIn(self.MEMBER_A, self.state.event_readiness(self.event_id))
        with self.assertRaises(ValueError):
            self.report(status="executing", order_id=order.id,
                        request_id="ready-010")

    def test_evacuated_cannot_be_silently_restored_or_counted_available(self):
        self.load()
        self.report(status="evacuated", basis="队长报告撤离")
        self.assertEqual("evacuated", self.state.event_readiness(
            self.event_id)[self.MEMBER_A].status)
        with self.assertRaises(ValueError):
            self.report(request_id="ready-010")
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("已撤离", page)
        self.assertIn("队长／组长报告待命可派 0 人", page)

    def test_http_denies_member_and_accepts_captain_without_auto_arrival(self):
        self.load()
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
                event_id=self.event_id, squad_id=self.squad_id,
                phone=self.MEMBER_A, status="standby", basis="队长点名确认可待命",
                order_id="", request_id="http-ready-009",
            )).encode()
            req = urllib.request.Request(
                base + "/captain-arrival/readiness", data=data, method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(403, post(self.MEMBER_A))
        self.assertEqual(303, post(self.CAPTAIN))
        self.assertEqual(1, len(self.state.event_readiness(self.event_id)))

    def test_other_event_and_forged_unarrived_state_do_not_become_available(self):
        self.load(missing=[self.MEMBER_B])
        other = self.state.registry.create_event(
            name="邻县事件", report_no="P03-09-B",
            discovered_at="2026-09-28T10:00:00+08:00", source="测试建档",
            location="邻县", created_by="fixture",
        )
        self.state.handle_event_action(
            other.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        with self.assertRaises(PermissionError):
            self.state.handle_captain_readiness(
                other.event_id, captain_token=self.tokens[self.CAPTAIN],
                squad_id=self.squad_id, phones=[self.MEMBER_A], status="standby",
                basis="跨事件伪造", order_id="", request_id="wrong-event",
            )
        self.assertEqual({}, self.state.event_readiness(other.event_id))
        space, _, _ = self.state._appointment_space(self.event_id)
        space.append_event(
            REPORTED, event_id=self.event_id, squad_id=self.squad_id,
            phones=[self.MEMBER_B], status="standby", basis="伪造缺员可派",
            actor=self.CAPTAIN, reported_at="2026-09-28T11:00:00+08:00",
            order_id="", request_id="forged-unarrived", data_kind="reported",
        )
        with self.assertRaises(ValueError):
            readiness_state(space, self.event_id, ledger=self.state.ledger)


if __name__ == "__main__":
    unittest.main()
