"""P03-04: selected-squad simulation is scoped, unsent and captain-authenticated."""

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
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CalloutTests(unittest.TestCase):
    DUTY = "13999990001"
    DEPUTY = "13999990002"
    CAPTAIN_A = "13999990003"
    CAPTAIN_B = "13999990004"
    ORDINARY = "13999990005"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-callouts-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-callouts", general_auth=self.auth),
            auth=self.auth, registry_path=root / "registry.json",
            materials_path=root / "materials.json", reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json", duty_subjects=(self.DUTY,),
        )
        self.state.registry.import_person_library([
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="指挥部"),
            PersonRecord(name="一队长", phone=self.CAPTAIN_A, unit="一中队", is_leader=True),
            PersonRecord(name="二队长", phone=self.CAPTAIN_B, unit="二中队", is_leader=True),
            PersonRecord(name="普通队员", phone=self.ORDINARY, unit="一中队"),
        ])
        self.tokens = {}
        for phone in (self.DUTY, self.DEPUTY, self.CAPTAIN_A,
                      self.CAPTAIN_B, self.ORDINARY):
            token, error = self.state.member_login(phone, "123456")
            self.assertEqual("", error)
            self.tokens[phone] = token
        event = self.state.registry.create_event(
            name="模拟火情", report_no="P03-04",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.squads = [
            self.state.ledger.add_squad(
                name="一中队", member_phones=(self.CAPTAIN_A, self.ORDINARY),
                leader_phone=self.CAPTAIN_A, library=self.state.registry.library,
            ),
            self.state.ledger.add_squad(
                name="二中队", member_phones=(self.CAPTAIN_B,),
                leader_phone=self.CAPTAIN_B, library=self.state.registry.library,
            ),
            self.state.ledger.add_squad(
                name="未配置队长中队", member_phones=(self.ORDINARY,),
                library=self.state.registry.library,
            ),
        ]
        self.state._save_ledger()

    def prepare(self):
        self.state.handle_event_action(
            self.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-001",
            request_id="appoint-001", occurred_at="2026-09-28T09:00:00+08:00",
        )
        return Path(self.state.registry.events[self.event_id].snapshot_path)

    def select(self, squad_ids=None, **changes):
        fields = dict(
            deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squads[0].squad_id] if squad_ids is None else squad_ids,
            channel="wechat", request_id="select-001",
        )
        return self.state.handle_select_squads(
            self.event_id, **{**fields, **changes}
        )

    def reply(self, **changes):
        fields = dict(
            captain_token=self.tokens[self.CAPTAIN_A],
            squad_id=self.squads[0].squad_id, can_depart="yes",
            expected_people="2", eta="2026-09-28T12:00:00+08:00",
            reason="", request_id="reply-001",
        )
        return self.state.handle_captain_reply(
            self.event_id, **{**fields, **changes}
        )

    def test_start_and_appointment_do_not_auto_call_squads(self):
        target = self.prepare()
        self.assertEqual({}, self.state.event_callouts(self.event_id))
        self.assertEqual([], self.state.captain_callouts(self.tokens[self.CAPTAIN_A]))
        self.assertFalse(any(e.kind.startswith("SquadCallout") for e in
                             load_workspace(target, general_auth=self.auth).events))

    def test_deputy_selects_subset_and_unsent_notice_survives_reload(self):
        target = self.prepare()
        before_materials = dict(self.state.ledger.snapshots)
        payload = self.select()
        self.assertEqual("simulated", payload["data_kind"])
        self.assertEqual("not_sent", payload["transport_state"])
        self.assertEqual([self.squads[0].squad_id],
                         [item["squad_id"] for item in payload["squads"]])
        self.assertEqual({self.squads[0].squad_id},
                         set(self.state.event_callouts(self.event_id)))
        self.assertEqual(1, len(self.state.captain_callouts(self.tokens[self.CAPTAIN_A])))
        self.assertEqual([], self.state.captain_callouts(self.tokens[self.CAPTAIN_B]))
        self.assertEqual(before_materials, self.state.ledger.snapshots)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual("not_sent", next(e.payload["transport_state"] for e in
                                         restored.events if e.kind == "SquadCalloutSelected"))

    def test_only_appointed_deputy_can_select_and_batch_is_atomic(self):
        self.prepare()
        for token in (self.tokens[self.DUTY], self.tokens[self.CAPTAIN_A],
                      self.tokens[self.ORDINARY], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.select(deputy_token=token)
        with self.assertRaises(ValueError):
            self.select(squad_ids=[self.squads[0].squad_id, self.squads[2].squad_id])
        self.assertEqual({}, self.state.event_callouts(self.event_id))
        self.select()
        with self.assertRaises(ValueError):
            self.select(squad_ids=[self.squads[0].squad_id], request_id="select-002")
        with self.assertRaises(ValueError):
            self.select(squad_ids=[self.squads[1].squad_id], request_id="select-001")
        self.assertEqual({self.squads[0].squad_id},
                         set(self.state.event_callouts(self.event_id)))

    def test_captain_reply_identity_validation_and_no_actual_arrival(self):
        target = self.prepare()
        self.select()
        for token in (self.tokens[self.DEPUTY], self.tokens[self.CAPTAIN_B],
                      self.tokens[self.ORDINARY], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.reply(captain_token=token)
        for changes in ({"can_depart": "yes", "expected_people": "0"},
                        {"eta": "2026-09-28T12:00:00"},
                        {"can_depart": "no", "reason": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reply(**changes)
        receipt = self.reply()
        self.assertEqual("simulated", receipt["data_kind"])
        self.assertEqual("in_app_demo_receipt", receipt["source"])
        self.assertEqual("acknowledged_in_app", receipt["receipt_state"])
        self.assertEqual(self.CAPTAIN_A, receipt["reported_by"])
        self.assertEqual(receipt, self.reply())
        with self.assertRaises(ValueError):
            self.reply(expected_people="3", request_id="reply-002")
        self.assertEqual({}, self.state.ledger.snapshots)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertFalse(any(e.kind in {"SquadDeparted", "SquadArrived"}
                             for e in restored.events))

    def test_cannot_depart_requires_reason_and_leader_must_remain_valid(self):
        self.prepare()
        self.select()
        receipt = self.reply(can_depart="no", expected_people="", eta="",
                             reason="道路中断")
        self.assertEqual(0, receipt["expected_people"])
        self.assertEqual("道路中断", receipt["reason"])
        self.select(squad_ids=[self.squads[1].squad_id], request_id="select-002")
        # A changed long-term captain binding must not silently transfer an old callout.
        from dataclasses import replace
        self.state.ledger.squads[self.squads[1].squad_id] = replace(
            self.squads[1], leader_phone=""
        )
        with self.assertRaises(ValueError):
            self.reply(captain_token=self.tokens[self.CAPTAIN_B],
                       squad_id=self.squads[1].squad_id, request_id="reply-002")

    def test_pages_expose_only_own_tasks_and_disclose_simulation(self):
        self.prepare()
        deputy = self.tokens[self.DEPUTY]
        captain = self.tokens[self.CAPTAIN_A]
        status, page = render_page(self.state, "/dispatch", member_token=deputy)
        self.assertEqual(200, status)
        self.assertIn("未发送、未送达", page)
        self.assertIn(self.squads[0].name, page)
        self.assertEqual(404, render_page(self.state, "/dispatch",
                                           member_token=captain)[0])
        self.assertEqual(404, render_page(self.state, "/callouts",
                                           member_token=captain)[0])
        self.select()
        status, page = render_page(self.state, "/callouts", member_token=captain)
        self.assertEqual(200, status)
        self.assertIn("未发送、未送达", page)
        self.assertIn("提交本人模拟回执", page)
        self.assertEqual(404, render_page(self.state, "/callouts",
                                           member_token=self.tokens[self.CAPTAIN_B])[0])

    def test_leader_ledger_validation(self):
        with self.assertRaises(ValueError):
            self.state.ledger.add_squad(
                name="错误底账", member_phones=(self.ORDINARY,),
                leader_phone=self.ORDINARY, library=self.state.registry.library,
            )
        with self.assertRaises(ValueError):
            self.state.ledger.set_squad_leader(
                self.squads[2].squad_id, self.CAPTAIN_B,
                library=self.state.registry.library,
            )

    def test_http_routes_reject_other_roles_and_show_unsent_state(self):
        self.prepare()
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, token, fields=None):
            data = urllib.parse.urlencode(fields, doseq=True).encode() if fields else None
            req = urllib.request.Request(
                base + path, data=data, method="POST" if fields else "GET",
                headers={"Cookie": f"member_session={token}"},
            )
            try:
                with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        selection = {
            "event_id": self.event_id, "squad_id": self.squads[0].squad_id,
            "channel": "phone", "request_id": "http-select-001",
        }
        for phone in (self.DUTY, self.CAPTAIN_A, self.ORDINARY):
            self.assertEqual(403, request("/dispatch/select", self.tokens[phone], selection)[0])
        self.assertEqual(303, request("/dispatch/select", self.tokens[self.DEPUTY], selection)[0])
        status, page = request("/callouts", self.tokens[self.CAPTAIN_A])
        self.assertEqual(200, status)
        self.assertIn("电话模拟通知已生成", page)
        self.assertIn("未发送、未送达", page)
        reply = {
            "event_id": self.event_id, "squad_id": self.squads[0].squad_id,
            "can_depart": "yes", "expected_people": "2",
            "eta": "2026-09-28T12:00:00+08:00", "reason": "",
            "request_id": "http-reply-001",
        }
        self.assertEqual(403, request("/callouts/reply", self.tokens[self.CAPTAIN_B], reply)[0])
        self.assertEqual(303, request("/callouts/reply", self.tokens[self.CAPTAIN_A], reply)[0])
        status, page = request("/dispatch", self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn("队长模拟回执：可出动", page)

    def test_simulated_failure_stays_unsent_and_does_not_block_independent_checkin(self):
        target = self.prepare()
        self.select()
        fields = dict(deputy_token=self.tokens[self.DEPUTY],
                      squad_id=self.squads[0].squad_id,
                      reason="演练：假设电话不可达", request_id="failure-001")
        failed = self.state.handle_callout_simulation_failure(self.event_id, **fields)
        self.assertEqual("simulated_failure", failed["scenario_state"])
        self.assertEqual("not_sent", failed["transport_state"])
        self.assertEqual("not_delivered", failed["delivery_state"])
        self.assertEqual(failed, self.state.handle_callout_simulation_failure(
            self.event_id, **fields))
        status, page = render_page(self.state, "/dispatch",
                                   member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn("演练假设通知失败", page)
        self.assertIn("真实未发送／未送达", page)
        self.assertEqual((), load_workspace(target, general_auth=self.auth).verify_audit_chain())
        self.assertEqual(failed, self.state.event_callouts(
            self.event_id)[self.squads[0].squad_id].simulation_failure)
        self.assertEqual({}, self.state.ledger.snapshots)
        self.reply()  # The captain's in-app demonstration is independent of transport.
        qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                     if q.event_id == self.event_id)
        self.state.handle_captain_scan(
            self.event_id, captain_token=self.tokens[self.CAPTAIN_A],
            squad_id=self.squads[0].squad_id, qr_id=qr_id)
        self.assertEqual({}, self.state.ledger.snapshots)
        self.state.handle_captain_arrival(
            self.event_id, captain_token=self.tokens[self.CAPTAIN_A],
            squad_id=self.squads[0].squad_id, mode="initial", phones=[],
            request_id="arrival-after-failure")
        self.assertIn(self.squads[0].squad_id,
                      self.state.ledger.snapshots[self.event_id].squads)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertFalse(any(e.payload.get("transport_state") == "sent"
                             or e.payload.get("delivery_state") == "delivered"
                             for e in restored.events))

    def test_simulated_failure_requires_deputy_reason_and_selected_squad(self):
        self.prepare()
        self.select()
        fields = dict(deputy_token=self.tokens[self.DEPUTY],
                      squad_id=self.squads[0].squad_id,
                      reason="演练失败", request_id="failure-001")
        for phone in (self.DUTY, self.CAPTAIN_A, self.ORDINARY):
            with self.subTest(phone=phone), self.assertRaises(PermissionError):
                self.state.handle_callout_simulation_failure(
                    self.event_id, **(fields | {"deputy_token": self.tokens[phone]}))
        for changes in ({"reason": ""}, {"request_id": ""},
                        {"squad_id": self.squads[1].squad_id}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.handle_callout_simulation_failure(
                    self.event_id, **(fields | changes))
        self.reply()
        with self.assertRaises(ValueError):
            self.state.handle_callout_simulation_failure(self.event_id, **fields)

    def test_simulated_failure_http_scope_and_visibility(self):
        self.prepare()
        self.select()
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(phone, request_id="http-failure"):
            data = urllib.parse.urlencode(dict(
                event_id=self.event_id, squad_id=self.squads[0].squad_id,
                reason="演练通信异常", request_id=request_id,
            )).encode()
            req = urllib.request.Request(
                base + "/dispatch/simulate-failure", data=data, method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                return code

        self.assertEqual(403, post(self.CAPTAIN_A))
        self.assertEqual(303, post(self.DEPUTY))
        self.assertEqual(303, post(self.DEPUTY))
        self.assertEqual(409, post(self.DEPUTY, request_id="another"))
        status, page = render_page(self.state, "/callouts",
                                   member_token=self.tokens[self.CAPTAIN_A])
        self.assertEqual(200, status)
        self.assertIn("演练假设通知失败", page)
        self.assertIn("不代表实际通知失败", page)


if __name__ == "__main__":
    unittest.main()
