"""Unified login HTTP regression: real credentials, session isolation and legacy compatibility."""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace


class UnifiedLoginTests(unittest.TestCase):
    DUTY = "13955550001"
    MEMBER = "13955550002"
    SECRET = "unified-test-secret"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        p = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        p.start(); self.addCleanup(p.stop)
        auth = GeneralEntryAuth(secret=self.SECRET)
        self.state = PanelState(ScenarioWorkspace("unified-test", general_auth=auth), auth=auth,
                                registry_path=root / "registry.json", materials_path=root / "materials.json",
                                duty_subjects=(self.DUTY,))
        self.state.registry.import_person_library([
            PersonRecord(name="测试值班", phone=self.DUTY, unit="模拟"),
            PersonRecord(name="测试成员", phone=self.MEMBER, unit="模拟"),
        ])
        self.server = make_server(self.state, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, form=None, cookie=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, None if form is None else urlencode(form),
                         {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            conn.close()

    def login(self, account, password, cookie=""):
        return self.request("POST", "/login/unified", dict(account=account, password=password), cookie)

    def cookie(self, headers, name):
        return next((v.split(";")[0] for k, v in headers if k.lower() == "set-cookie"
                     and v.startswith(name + "=") and not v.startswith(name + "=;")), "")

    def test_one_form_and_no_role_picker(self):
        status, _, body = self.request("GET", "/login")
        html = body.decode()
        self.assertEqual(200, status)
        self.assertEqual(1, html.count("<form"))
        self.assertIn('action="/login/unified"', html)
        self.assertIn('autocomplete="username"', html)
        self.assertIn('href="/temporary"', html)
        self.assertNotIn("指挥员登录", html)
        self.assertNotIn("人员登录", html)

    def test_admin_authenticates_and_lands_at_events(self):
        status, headers, _ = self.login(" ADMIN ", self.SECRET)
        self.assertEqual(303, status)
        self.assertIn(("Location", "/events"), headers)
        cookie = self.cookie(headers, "panel_session")
        self.assertTrue(cookie)
        self.assertTrue(self.state.command_mode(cookie.split("=", 1)[1]))
        self.assertFalse(self.cookie(headers, "member_session"))

    def test_member_login_has_no_commander_privilege_or_arrival(self):
        status, headers, _ = self.login(self.MEMBER, "123456")
        self.assertEqual(303, status)
        cookie = self.cookie(headers, "member_session")
        session = self.state.member_session(cookie.split("=", 1)[1])
        self.assertFalse(session.onboarded)
        self.assertFalse(self.cookie(headers, "panel_session"))
        self.assertEqual({}, self.state.registry.events)
        self.assertEqual(403, self.request("GET", "/models", cookie=cookie)[0])

    def test_duty_member_keeps_its_original_landing(self):
        status, headers, _ = self.login(self.DUTY, "123456")
        self.assertEqual(303, status)
        self.assertIn(("Location", "/events"), headers)

    def test_wrong_or_unknown_credentials_never_fall_back_to_admin(self):
        for account, password in [("admin", "123456"), (self.MEMBER, self.SECRET),
                                  ("13955559999", self.SECRET), ("", self.SECRET),
                                  (self.MEMBER, ""), ("<script>alert(1)</script>", self.SECRET)]:
            with self.subTest(account=account):
                status, headers, body = self.login(account, password)
                self.assertEqual(401, status)
                self.assertFalse(self.cookie(headers, "panel_session"))
                self.assertFalse(self.cookie(headers, "member_session"))
                self.assertNotIn("<script>alert(1)</script>", body.decode())
                self.assertNotIn(self.SECRET, body.decode())

    def test_admin_to_member_revokes_old_commander_session(self):
        _, old, _ = self.login("admin", self.SECRET)
        cookie = self.cookie(old, "panel_session")
        status, headers, _ = self.login(self.MEMBER, "123456", cookie)
        self.assertEqual(303, status)
        self.assertFalse(self.state.command_mode(cookie.split("=", 1)[1]))
        self.assertTrue(any(k.lower() == "set-cookie" and v.startswith("panel_session=;")
                            for k, v in headers))

    def test_member_to_admin_revokes_old_member_session(self):
        _, old, _ = self.login(self.MEMBER, "123456")
        cookie = self.cookie(old, "member_session")
        status, headers, _ = self.login("admin", self.SECRET, cookie)
        self.assertEqual(303, status)
        self.assertIsNone(self.state.member_session(cookie.split("=", 1)[1]))
        self.assertTrue(any(k.lower() == "set-cookie" and v.startswith("member_session=;")
                            for k, v in headers))

    def test_old_auth_routes_remain_compatible(self):
        self.assertEqual(303, self.request("POST", "/login", dict(secret=self.SECRET))[0])
        self.assertEqual(303, self.request("POST", "/login/member",
                                          dict(phone=self.MEMBER, password="123456"))[0])

    def test_login_assets_are_whitelisted_and_delivered(self):
        for name, mime in [("login.css", "text/css"), ("login.js", "text/javascript"),
                           ("login-landscape.png", "image/png")]:
            with self.subTest(name=name):
                status, headers, body = self.request("GET", "/static/" + name)
                self.assertEqual(200, status)
                self.assertTrue(body)
                self.assertTrue(dict(headers)["Content-Type"].startswith(mime))
        self.assertEqual(404, self.request("GET", "/static/../config/settings.yaml")[0])
