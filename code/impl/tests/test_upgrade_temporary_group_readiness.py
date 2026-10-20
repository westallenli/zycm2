"""P03-10 explicit temporary group leaders and scoped readiness reports."""

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
from panda_brain.temporary_group_readiness import (
    LEADER_ASSIGNED, REPORTED, readiness_state,
)
from panda_brain.workspace import ScenarioWorkspace


class TemporaryGroupReadinessTests(unittest.TestCase):
    DUTY = "13999995001"
    DEPUTY = "13999995002"
    LEADER = "13999995003"
    MEMBER = "13999995004"
    NEXT = "13999995005"
    SUPPORT = "13999995006"
    OTHER = "13999995007"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-temp-readiness-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-temp-readiness", general_auth=self.auth),
            auth=self.auth, registry_path=root / "registry.json",
            materials_path=root / "materials.json", reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json", duty_subjects=(self.DUTY,),
        )
        people = (
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="副总指挥", phone=self.DEPUTY, unit="前指"),
            PersonRecord(name="临时甲", phone=self.LEADER, unit="属地"),
            PersonRecord(name="临时乙", phone=self.MEMBER, unit="属地"),
            PersonRecord(name="临时丙", phone=self.NEXT, unit="属地"),
            PersonRecord(name="通信保障", phone=self.SUPPORT, unit="保障单位"),
            PersonRecord(name="无关人员", phone=self.OTHER, unit="其他"),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="临时组状态测试", report_no="P03-10",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.state.handle_event_action(
            self.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence="线下指定记录-010",
            request_id="appoint-010", occurred_at="2026-09-28T09:00:00+08:00",
        )

    def add(self, phone, group="tech-info", *, arrived=True):
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[phone], qr_id=self.qr_id,
        )
        self.state.handle_temporary_assign(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phones=[phone], pos_code=group, request_id=f"assign-{phone}",
        )
        if arrived:
            self.state.handle_temporary_arrival(
                self.event_id, member_token=self.tokens[phone],
            )

    def appoint(self, phone=None, group="tech-info", **changes):
        values = dict(
            deputy_token=self.tokens[self.DEPUTY], pos_code=group,
            phone=phone or self.LEADER, evidence="线下组长指定记录-010",
            request_id="leader-010",
        )
        return self.state.handle_temporary_group_leader(
            self.event_id, **(values | changes)
        )

    def report(self, **changes):
        values = dict(
            leader_token=self.tokens[self.LEADER], pos_code="tech-info",
            phones=[self.MEMBER], status="standby", basis="组长点名确认待命",
            order_id="", request_id="ready-010",
        )
        return self.state.handle_temporary_group_readiness(
            self.event_id, **(values | changes)
        )

    def test_assignment_and_arrival_do_not_grant_leadership_or_availability(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.assertEqual({}, self.state.event_temporary_group_leaders(self.event_id))
        self.assertEqual({}, self.state.event_readiness(self.event_id))
        with self.assertRaises(PermissionError):
            self.report()
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("已申报到场 2 人 · 队长／组长报告待命可派 0 人", page)

    def test_deputy_designates_arrived_group_member_then_leader_updates_subset(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.add(self.NEXT)
        first = self.appoint()
        self.assertEqual(first, self.appoint())
        self.assertEqual(self.LEADER, self.state.event_temporary_group_leaders(
            self.event_id)["tech-info"])
        before = self.state.ledger.snapshots.copy()
        self.report()
        self.assertEqual(before, self.state.ledger.snapshots)
        self.assertEqual("standby", self.state.event_readiness(
            self.event_id)[self.MEMBER].status)
        self.assertNotIn(self.NEXT, self.state.event_readiness(self.event_id))
        status, page = render_page(
            self.state, "/personnel/groups", member_token=self.tokens[self.DEPUTY],
            query={"event_id": [self.event_id]},
        )
        self.assertEqual(200, status)
        self.assertIn("队长／组长报告待命可派 1 人", page)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual("standby", readiness_state(
            restored, self.event_id, ledger=self.state.ledger)[self.MEMBER].status)
        self.assertEqual(1, sum(e.kind == LEADER_ASSIGNED for e in restored.events))
        self.assertEqual(1, sum(e.kind == REPORTED for e in restored.events))

    def test_ordinary_member_deputy_and_other_group_cannot_report(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.add(self.SUPPORT, "log-comm")
        self.appoint()
        for token in (self.tokens[self.MEMBER], self.tokens[self.DEPUTY],
                      self.tokens[self.SUPPORT], self.tokens[self.OTHER], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.report(leader_token=token)
        for changes in ({"phones": [self.SUPPORT]}, {"phones": [self.OTHER]},
                        {"phones": []}, {"phones": [self.MEMBER, self.MEMBER]},
                        {"basis": ""}, {"status": "auto"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.report(**changes)
        self.assertEqual({}, self.state.event_readiness(self.event_id))

    def test_support_group_is_separate_and_leader_replacement_revokes_old_leader(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.add(self.NEXT)
        self.add(self.SUPPORT, "log-comm")
        self.appoint()
        self.appoint(phone=self.SUPPORT, group="log-comm",
                     request_id="leader-support")
        self.state.handle_temporary_group_readiness(
            self.event_id, leader_token=self.tokens[self.SUPPORT],
            pos_code="log-comm", phones=[self.SUPPORT], status="standby",
            basis="通信保障组核对待命", order_id="", request_id="ready-support",
        )
        self.assertEqual("standby", self.state.event_readiness(
            self.event_id)[self.SUPPORT].status)
        with self.assertRaises(ValueError):
            self.report(phones=[self.SUPPORT])
        self.appoint(phone=self.NEXT, request_id="leader-next",
                     evidence="线下变更组长记录-010")
        with self.assertRaises(PermissionError):
            self.report()
        self.report(leader_token=self.tokens[self.NEXT], request_id="ready-next")
        self.assertEqual("standby", self.state.event_readiness(
            self.event_id)[self.MEMBER].status)

    def test_transfer_removes_old_group_authority_and_readiness(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.appoint()
        self.report()
        self.state.handle_person_group_transfer(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phone=self.LEADER, to_pos_code="tech-assess", request_id="move-leader",
        )
        self.assertEqual({}, self.state.event_temporary_group_leaders(self.event_id))
        with self.assertRaises(PermissionError):
            self.report(request_id="ready-again")
        self.state.handle_person_group_transfer(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phone=self.MEMBER, to_pos_code="tech-assess", request_id="move-member",
        )
        self.assertNotIn(self.MEMBER, self.state.event_readiness(self.event_id))
        self.state.handle_person_group_transfer(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phone=self.LEADER, to_pos_code="tech-info", request_id="return-leader",
        )
        self.state.handle_person_group_transfer(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phone=self.MEMBER, to_pos_code="tech-info", request_id="return-member",
        )
        self.assertEqual({}, self.state.event_temporary_group_leaders(self.event_id))
        self.assertNotIn(self.MEMBER, self.state.event_readiness(self.event_id))

    def test_leader_must_be_arrived_and_requests_are_idempotent(self):
        self.add(self.LEADER, arrived=False)
        self.add(self.MEMBER)
        with self.assertRaises(ValueError):
            self.appoint()
        with self.assertRaises(ValueError):
            self.appoint(phone=self.OTHER)
        with self.assertRaises(PermissionError):
            self.appoint(deputy_token=self.tokens[self.OTHER])
        self.state.handle_temporary_arrival(
            self.event_id, member_token=self.tokens[self.LEADER]
        )
        first = self.appoint()
        self.assertEqual(first, self.appoint())
        with self.assertRaises(ValueError):
            self.appoint(phone=self.MEMBER)
        report = self.report()
        self.assertEqual(report, self.report())
        with self.assertRaises(ValueError):
            self.report(phones=[self.LEADER])

    def test_http_scope_and_event_isolation(self):
        self.add(self.LEADER)
        self.add(self.MEMBER)
        self.appoint()
        self.assertEqual(404, render_page(
            self.state, "/temporary/leader", member_token=self.tokens[self.MEMBER])[0])
        self.assertEqual(200, render_page(
            self.state, "/temporary/leader", member_token=self.tokens[self.LEADER])[0])
        other = self.state.registry.create_event(
            name="其他事件", report_no="P03-10-B",
            discovered_at="2026-09-28T10:00:00+08:00", source="测试建档",
            location="邻县", created_by="fixture",
        )
        self.state.handle_event_action(
            other.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        with self.assertRaises(PermissionError):
            self.state.handle_temporary_group_readiness(
                other.event_id, leader_token=self.tokens[self.LEADER],
                pos_code="tech-info", phones=[self.MEMBER], status="standby",
                basis="跨事件伪造", order_id="", request_id="wrong-event",
            )
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
                event_id=self.event_id, pos_code="tech-info", phone=self.MEMBER,
                status="standby", basis="组长点名确认待命", order_id="",
                request_id="http-ready-010",
            )).encode()
            req = urllib.request.Request(
                base + "/temporary/leader/readiness", data=data, method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(403, post(self.MEMBER))
        self.assertEqual(303, post(self.LEADER))


if __name__ == "__main__":
    unittest.main()
