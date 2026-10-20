"""P10 acceptance tests: panel skeleton, auth sessions, and read-only views."""

from __future__ import annotations

import ast
import unittest
import urllib.request
from pathlib import Path

from panda_brain.panel import (
    ROUTES,
    PanelState,
    load_state,
    make_server,
    render_page,
)

PANEL_PATH = Path(__file__).resolve().parents[1] / "panda_brain" / "panel.py"

_ALLOWED_IMPORTS = {
    "__future__",
    "argparse",
    "copy",  # standard-library deepcopy isolates the two saved material views
    "dataclasses",  # stdlib replace updates immutable per-session model profiles
    "html",
    "http",
    "json",   # P20：/sand/situation-feed 只读 JSON 馈送
    "os",
    "pathlib",
    "re",  # stdlib: legacy personnel view disables superseded write controls
    "sys",
    "threading",  # stdlib lock protects one initial appointment per event
    "time",
    "secrets",
    "sqlite3",  # stdlib persistence errors are shown as rejected review writes
    "urllib",
}


class P10PanelSkeletonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = load_state()  # demo = full campaign workspace
        assert cls.state.workspace is not None

    def test_zero_third_party_imports(self) -> None:
        tree = ast.parse(PANEL_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                continue  # relative in-package imports are fine
        self.assertEqual(set(), imported - _ALLOWED_IMPORTS)

    def test_five_routes_render_with_fixed_markers(self) -> None:
        markers = {
            "/overview": ["共享态势", "审计链", "态势评分"],
            "/brief": ["方案对比矩阵", "参谋推荐"],
            "/orders": ["在途命令"],
            "/questions": ["待你回答"],
            "/reviews": ["复盘记录", "受控记忆候选"],
        }
        for route, needles in markers.items():
            with self.subTest(route=route):
                status, page = render_page(self.state, route)
                self.assertEqual(200, status)
                for needle in needles:
                    self.assertIn(needle, page)

    def test_demo_state_is_fully_populated(self) -> None:
        ws = self.state.workspace
        self.assertTrue(ws.claims)
        self.assertTrue(ws.decision_briefs)
        _, page = render_page(self.state, "/brief")
        self.assertIn("prepare-approach-r2", page)
        self.assertIn("经验", page)  # confirmed experiences injected in brief view

    def test_rendering_is_read_only(self) -> None:
        version_before = self.state.workspace.version
        for route, _label in ROUTES:
            render_page(self.state, route)
        self.assertEqual(version_before, self.state.workspace.version)

    def test_unknown_route_is_404(self) -> None:
        status, _page = render_page(self.state, "/nope")
        self.assertEqual(404, status)


class P10AuthSessionTests(unittest.TestCase):
    """T2: general-entry authentication and in-memory sessions."""

    def _locked_state(self, tmp: str) -> PanelState:
        from panda_brain.auth import GeneralEntryAuth
        from panda_brain.persistence import load_workspace, save_workspace

        demo = load_state()
        db_path = Path(tmp) / "panel.db"
        save_workspace(demo.workspace, db_path)
        auth = GeneralEntryAuth(secret="panel-secret-1")
        workspace = load_workspace(db_path, general_auth=auth)
        return PanelState(workspace, db_path=db_path, auth=auth)

    def test_locked_state_shows_no_command_forms(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = self._locked_state(tmp)
            self.assertFalse(state.entry_unlocked)
            for route, _label in ROUTES:
                if route == "/":
                    # 首页门禁（doc 01 §9）：未登录不得进入首页，重定向登录页
                    status, _page = render_page(state, route)
                    self.assertEqual(303, status)
                    continue
                status, page = render_page(state, route)
                self.assertEqual(200, status)
                self.assertNotIn("<form", page)
                if route == "/overview":
                    self.assertIn("锁定", page)

    def test_login_rejects_wrong_secret_without_session(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = self._locked_state(tmp)
            self.assertIsNone(state.login("wrong"))
            self.assertEqual(set(), state.sessions)
            self.assertFalse(state.entry_unlocked)

    def test_login_unlocks_and_issues_session(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = self._locked_state(tmp)
            token = state.login("panel-secret-1")
            self.assertIsNotNone(token)
            assert token is not None
            self.assertTrue(state.session_valid(token))
            self.assertTrue(state.entry_unlocked)
            status, page = render_page(state, "/overview", session_token=token)
            self.assertIn("指挥模式", page)
            self.assertIn("登出", page)

            # Command affordances appear on action pages once unlocked.
            from panda_brain.models import GeneralQuestion

            state.workspace.raise_general_question(
                GeneralQuestion(
                    id="gq-panel-probe",
                    kind="insufficient_evidence",
                    subject="probe",
                    question="面板可见性探针问题？",
                    context=(),
                    source_version=state.workspace.version,
                )
            )
            _, qpage = render_page(state, "/questions", session_token=token)
            self.assertIn("答复并定向重排", qpage)

    def test_logout_and_restart_reset_sessions(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = self._locked_state(tmp)
            token = state.login("panel-secret-1")
            assert token is not None
            state.logout(token)
            self.assertFalse(state.session_valid(token))
            _, page = render_page(state, "/", session_token=token)
            self.assertNotIn("指挥模式", page)
            # A fresh panel process holds no sessions at all.
            fresh = PanelState(state.workspace, auth=state.auth)
            self.assertEqual(set(), fresh.sessions)
            self.assertFalse(fresh.session_valid(token))

    def test_http_login_roundtrip_sets_cookie_and_401_on_failure(self) -> None:
        import tempfile
        import threading
        import urllib.error
        import urllib.parse

        with tempfile.TemporaryDirectory() as tmp:
            state = self._locked_state(tmp)
            server = make_server(state, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{port}"
                data = urllib.parse.urlencode({"secret": "nope"}).encode()
                req = urllib.request.Request(f"{base}/login", data=data, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=5)
                    self.fail("expected 401")
                except urllib.error.HTTPError as err:
                    self.assertEqual(401, err.code)

                data = urllib.parse.urlencode({"secret": "panel-secret-1"}).encode()
                req = urllib.request.Request(f"{base}/login", data=data, method="POST")

                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                        return None

                opener = urllib.request.build_opener(NoRedirect)
                try:
                    opener.open(req, timeout=5)
                    self.fail("expected redirect")
                except urllib.error.HTTPError as err:
                    self.assertEqual(303, err.code)
                    set_cookie = err.headers.get("Set-Cookie", "")
                self.assertIn("panel_session=", set_cookie)
                self.assertIn("HttpOnly", set_cookie)
                token = set_cookie.split("panel_session=", 1)[1].split(";", 1)[0]
                req = urllib.request.Request(
                    f"{base}/overview", headers={"Cookie": f"panel_session={token}"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                self.assertIn("指挥模式", body)
            finally:
                server.shutdown()
                server.server_close()


class P10PanelServerTests(unittest.TestCase):
    def test_binds_loopback_only_and_serves(self) -> None:
        state = load_state()
        server = make_server(state, port=0)
        host, port = server.server_address[:2]
        try:
            self.assertEqual("127.0.0.1", host)
            import threading

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/overview", timeout=5
            ) as resp:
                body = resp.read().decode("utf-8")
                self.assertEqual(200, resp.status)
            self.assertIn("破局智援参谋 · 指挥台", body)
            self.assertIn("共享态势", body)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
