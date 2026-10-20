"""P03-03: the duty officer records, but does not make, offline deputy appointments."""

import json
import concurrent.futures
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
from panda_brain.authorization import Role
from panda_brain.event_authorization import event_grants
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelActionError, PanelState, make_server, render_duty_events_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DeputyAppointmentTests(unittest.TestCase):
    DUTY = "13999990001"
    DEPUTY = "13999990002"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-appointment-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-appointment", general_auth=self.auth),
            auth=self.auth,
            registry_path=root / "registry.json",
            materials_path=root / "materials.json",
            reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY,),
        )
        self.state.registry.import_person_library([
            PersonRecord(name="模拟值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="模拟副总指挥", phone=self.DEPUTY, unit="指挥部"),
        ])
        self.duty_token, error = self.state.member_login(self.DUTY, "123456")
        self.assertEqual("", error)
        self.deputy_token, error = self.state.member_login(self.DEPUTY, "123456")
        self.assertEqual("", error)
        event = self.state.registry.create_event(
            name="模拟火情", report_no="P03-03", discovered_at="2026-09-28T08:00:00+08:00",
            source="测试建档", location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.fields = {
            "event_id": self.event_id,
            "subject_id": self.DEPUTY,
            "evidence": "模拟线下任命记录-001",
            "request_id": "p03-03-request-001",
            "occurred_at": "2026-09-28T09:00:00+08:00",
        }

    def start(self):
        # No general-entry login: duty start must work on its own authority.
        self.assertIsNone(self.state._credentials)
        self.state.handle_event_action(self.event_id, "start", duty_token=self.duty_token)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        self.assertTrue(target.is_file())
        return target

    def appoint(self, **changes):
        fields = {**self.fields, **changes}
        return self.state.handle_deputy_appointment(
            fields.pop("event_id"), duty_token=self.duty_token, **fields
        )

    def test_duty_only_start_then_appointment_persists_reported_provenance(self):
        target = self.start()
        before = load_workspace(target, general_auth=self.auth)
        self.assertEqual(self.event_id, before.event_id)
        self.assertEqual((), event_grants(before))
        record = self.appoint()
        self.assertEqual(self.DUTY, record.actor_id)
        self.assertEqual("reported", record.data_kind)
        self.assertEqual("manual-appointment", record.source)
        self.assertEqual(self.fields["occurred_at"], record.times.occurred_at)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual(self.event_id, restored.event_id)
        self.assertEqual((Role.DEPUTY,), tuple(g.role for g in event_grants(restored)))
        self.assertEqual(self.DEPUTY, event_grants(restored)[0].subject_id)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertIn("模拟线下任命记录-001", render_duty_events_page(
            self.state, self.state.member_session(self.duty_token)))

    def test_before_start_and_invalid_report_do_not_grant(self):
        with self.assertRaises(PanelActionError):
            self.appoint()
        target = self.start()
        for changes in ({"evidence": ""}, {"subject_id": "missing"},
                        {"occurred_at": "2026-09-28T09:00:00"},
                        {"request_id": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.appoint(**changes)
        self.assertEqual((), event_grants(load_workspace(target, general_auth=self.auth)))

    def test_idempotent_retry_conflict_and_replacement_guard(self):
        target = self.start()
        first = self.appoint()
        before = len(load_workspace(target, general_auth=self.auth).events)
        self.assertEqual(first, self.appoint())
        self.assertEqual(before, len(load_workspace(target, general_auth=self.auth).events))
        with self.assertRaises(ValueError):
            self.appoint(evidence="另一份依据")
        with self.assertRaises(ValueError):
            self.appoint(request_id="new-request")
        self.assertEqual(before, len(load_workspace(target, general_auth=self.auth).events))

    def test_legacy_snapshot_requires_start_audit_to_bind_event(self):
        target = self.start()
        legacy = load_workspace(target, general_auth=self.auth)
        legacy.event_id = ""
        save_workspace(legacy, target)
        self.appoint()
        self.assertEqual(self.event_id, load_workspace(target, general_auth=self.auth).event_id)

    def test_wrong_event_snapshot_and_closed_event_are_rejected(self):
        target = self.start()
        other = self.state.registry.create_event(
            name="另一场模拟火情", report_no="P03-03-B",
            discovered_at="2026-09-28T08:30:00+08:00",
            source="测试建档", location="另一县", created_by="fixture",
        )
        self.state.registry.transition(other.event_id, "start", operator="fixture")
        self.state.registry.set_snapshot_path(other.event_id, str(target))
        with self.assertRaises(PanelActionError):
            self.appoint(event_id=other.event_id)
        self.assertEqual((), event_grants(load_workspace(target, general_auth=self.auth)))
        self.state.registry.transition(self.event_id, "close", operator="fixture")
        with self.assertRaises(PanelActionError):
            self.appoint()

    def test_concurrent_initial_reports_leave_one_appointment(self):
        target = self.start()
        barrier = threading.Barrier(2)

        def submit(index):
            barrier.wait(timeout=5)
            try:
                return self.appoint(request_id=f"concurrent-{index}",
                                    evidence=f"线下记录-{index}")
            except ValueError as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (1, 2)))
        self.assertEqual(1, sum(not isinstance(result, Exception) for result in results))
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual(1, len(event_grants(restored)))
        self.assertEqual((), restored.verify_audit_chain())

    def test_http_duty_form_and_non_duty_denials(self):
        self.start()
        command = self.state.login("p03-appointment-secret")
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(method, path, *, cookie="", fields=None):
            data = urllib.parse.urlencode(fields).encode() if fields is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            if cookie:
                req.add_header("Cookie", cookie)
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                with opener.open(req, timeout=5) as resp:
                    return resp.status, dict(resp.headers), resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, dict(exc.headers), exc.read().decode()

        duty = f"member_session={self.duty_token}"
        commander = f"panel_session={command}"
        ordinary = f"member_session={self.deputy_token}"
        status, _, page = request("GET", "/events", cookie=duty)
        self.assertEqual(200, status)
        self.assertIn('action="/events/deputy-select"', page)
        self.assertIn("全省应急人员名单", page)
        for cookie in (commander, ordinary, "member_session=forged"):
            status, _, _ = request("POST", "/events/appointment", cookie=cookie,
                                   fields={**self.fields, "role": "duty"})
            self.assertEqual(403, status)
        status, _, _ = request("POST", "/events/appointment", cookie=duty,
                               fields=self.fields)
        self.assertEqual(303, status)
        status, _, page = request("GET", "/events", cookie=duty)
        self.assertEqual(200, status)
        self.assertIn("已指定副总指挥", page)
        self.assertNotIn('action="/events/appointment"', page)
        record = self.state.deputy_appointment(self.event_id)
        self.assertEqual(self.DUTY, record.actor_id)
        self.assertEqual(self.DEPUTY, json.loads(record.content_json)["subject_id"])


if __name__ == "__main__":
    unittest.main()
