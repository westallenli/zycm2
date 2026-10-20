"""P03-01: an explicit start produces one usable event QR or no start."""

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
from panda_brain.panel import PanelActionError, PanelState, make_server, render_events_page
from panda_brain.workspace import ScenarioWorkspace


class EventStartTests(unittest.TestCase):
    DUTY_PHONE = "13999990001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        auth = GeneralEntryAuth(secret="p03-start-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-start", general_auth=auth),
            auth=auth,
            registry_path=root / "registry.json",
            materials_path=root / "materials.json",
            reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json",
            duty_subjects=(self.DUTY_PHONE,),
        )
        self.command_token = self.state.login("p03-start-secret")
        self.assertIsNotNone(self.command_token)
        self.state.registry.import_person_library([
            PersonRecord(name="模拟值班员", phone=self.DUTY_PHONE, unit="模拟值班室")
        ])
        self.duty_token, error = self.state.member_login(self.DUTY_PHONE, "123456")
        self.assertEqual("", error)
        self.event_id = self.state.handle_create_event(
            name="演练火情", report_no="P03-1",
            discovered_at="2026-09-27T10:00:00+08:00",
            source="页面录入", location="青岗县",
        )["event_id"]

    def test_manual_event_starts_with_one_qr_and_audit(self):
        self.assertEqual({}, self.state.registry.qrs)
        result = self.state.handle_event_action(
            self.event_id, "start", duty_token=self.duty_token
        )
        self.assertEqual("active", result["status"])
        self.assertEqual("qr-001", result["qr_id"])
        self.assertEqual(self.event_id, self.state.registry.qrs[result["qr_id"]].event_id)
        self.assertEqual(1, len(self.state.registry.qrs))
        self.assertEqual(self.DUTY_PHONE, self.state.registry.qrs[result["qr_id"]].issued_by)
        self.assertIn("qr-001", render_events_page(self.state))
        with self.assertRaises(PanelActionError):
            self.state.handle_event_action(
                self.event_id, "start", duty_token=self.duty_token
            )
        self.assertEqual(1, len(self.state.registry.qrs))

    def test_general_entry_and_forged_duty_identity_cannot_start(self):
        for token in ("", "forged"):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.state.handle_event_action(self.event_id, "start", duty_token=token)
        self.assertEqual("registered", self.state.registry.events[self.event_id].status)
        self.assertEqual({}, self.state.registry.qrs)

    def test_ungranted_member_session_cannot_start(self):
        self.state.configure_duty_subjects(())
        with self.assertRaises(PermissionError):
            self.state.handle_event_action(
                self.event_id, "start", duty_token=self.duty_token
            )
        self.assertEqual("registered", self.state.registry.events[self.event_id].status)

    def test_locked_or_logged_out_duty_cannot_start(self):
        self.state.registry.credentials[self.DUTY_PHONE].locked = True
        with self.assertRaises(PermissionError):
            self.state.handle_event_action(
                self.event_id, "start", duty_token=self.duty_token
            )
        self.state.registry.credentials[self.DUTY_PHONE].locked = False
        self.state.member_logout(self.duty_token)
        with self.assertRaises(PermissionError):
            self.state.handle_event_action(
                self.event_id, "start", duty_token=self.duty_token
            )

    def test_build_failure_does_not_mark_event_started_or_issue_qr(self):
        with patch.object(self.state, "_build_event_space", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.state.handle_event_action(
                    self.event_id, "start", duty_token=self.duty_token
                )
        self.assertEqual("registered", self.state.registry.events[self.event_id].status)
        self.assertEqual({}, self.state.registry.qrs)

    def test_http_start_requires_named_duty_session(self):
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

        commander = f"panel_session={self.command_token}"
        duty = f"member_session={self.duty_token}"
        action = {"event_id": self.event_id, "action": "start"}
        status, _, duty_page = request("GET", "/events", cookie=duty)
        self.assertEqual(200, status)
        self.assertIn("火情事件", duty_page)
        self.assertNotIn('action="/events/create"', duty_page)
        status, _, commander_page = request("GET", "/events", cookie=commander)
        self.assertEqual(200, status)
        self.assertIn("等待值班人员启动处置", commander_page)
        self.assertNotIn("启动处置并生成二维码", commander_page)
        status, _, body = request("POST", "/events/action", cookie=commander,
                                  fields={**action, "role": "duty"})
        self.assertEqual(403, status)
        self.assertIn("值班人员", body)
        status, _, _ = request("POST", "/events/action",
                                cookie="member_session=forged", fields=action)
        self.assertEqual(403, status)
        self.assertEqual("registered", self.state.registry.events[self.event_id].status)
        status, headers, _ = request("POST", "/events/action", cookie=duty,
                                      fields=action)
        self.assertEqual(303, status)
        self.assertIn("issued=qr-001", headers["Location"])
        status, _, duty_receipt = request("GET", headers["Location"], cookie=duty)
        self.assertEqual(200, status)
        self.assertIn("事件已启动，二维码已签发", duty_receipt)
        self.assertEqual(self.DUTY_PHONE,
                         self.state.registry.qrs["qr-001"].issued_by)
        snapshot = self.state.registry.events[self.event_id].snapshot_path
        self.assertTrue(snapshot)
        from panda_brain.persistence import load_workspace
        restored = load_workspace(snapshot, general_auth=self.state.auth)
        started = next(e for e in restored.events if e.kind == "FireEventStarted")
        self.assertEqual(self.DUTY_PHONE, started.payload["operator"])
        self.assertEqual((), restored.verify_audit_chain())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
