"""P16-T3 验收：首页路由接线（P17-P2 起为自绘组织架构；门禁与静态资源）。"""

from __future__ import annotations
from tests.personnel_support import personnel_submit
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture

import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-home-secret"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _http(method: str, url: str, form: dict | None = None, cookie: str = ""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _cookie_of(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith(f"{name}="):
            value = part.strip().split("=", 1)[1]
            if value:
                return f"{name}={value}"
    return ""


class HomePageRouteE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p16-home-route", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
        )
        state.registry.import_person_library(
            [
                PersonRecord(
                    name="张三",
                    phone="13912340001",
                    unit="县防火办",
                    home_position="扑救一组（一分区）",
                ),
            ]
        )
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        # 指挥员登录 + 建事件 + 启动 + 名册 + 进场（完整指挥侧上下文）
        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.assertEqual(303, status)
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00", "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST",
                f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action, "csrf": next(m.custody_csrf for m in self.state.member_sessions.values() if m.phone in {g.subject_id for g in self.state._duty_grants})},
                cookie=self.cmd,
            )
        personnel_submit(self.state, _http,
            "POST",
            f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"},
            cookie=self.cmd,
        )

    def _member_onboard(self) -> str:
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        return member

    # -- 门禁 ------------------------------------------------------------------

    def test_anonymous_redirects_to_login(self):
        status, headers, _ = _http("GET", f"{self.base}/")
        self.assertEqual(303, status)
        self.assertIn("/login", headers.get("Location", ""))

    def test_member_without_checkin_stays_on_login(self):
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        self.assertTrue(member)
        status, headers, _ = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(303, status)
        self.assertIn("/login", headers.get("Location", ""))

    # -- 指挥员首页 --------------------------------------------------------------

    def test_commander_home_is_self_drawn_with_attendance(self):
        status, _, body = _http("GET", f"{self.base}/", cookie=self.cmd)
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        # P17-P2：零图片依赖，组织架构由系统自绘
        self.assertNotIn("/static/org_chart.png", text)
        self.assertNotIn("<img", text)
        self.assertIn("<svg", text)                      # 连线层为内联 SVG
        self.assertIn("联合指挥部/工作组", text)          # 结构节点齐全
        self.assertIn("扑救一组（一分区）", text)
        self.assertIn("到场总览", text)          # 已进场且名册已配置
        self.assertIn("实到 <b>0</b> / 应到 <b>1</b>", text)

    def test_commander_home_before_entering_any_event_is_honest_empty(self):
        # 新开一个未进场的面板上下文
        with tempfile.TemporaryDirectory() as tmp:
            patcher = unittest.mock.patch.object(
                wb, "SNAPSHOT_DIR", Path(tmp) / "snapshots"
            )
            patcher.start()
            try:
                auth = GeneralEntryAuth(secret="x")
                fresh_state = PanelState(
                    ScenarioWorkspace("fresh", general_auth=auth), auth=auth
                )
                server = make_server(fresh_state, port=0)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    base = f"http://127.0.0.1:{server.server_address[1]}"
                    _, headers, _ = _http("POST", f"{base}/login", {"secret": "x"})
                    cmd = _cookie_of(headers, "panel_session")
                    status, _, body = _http("GET", f"{base}/", cookie=cmd)
                    self.assertEqual(200, status)
                    text = body.decode("utf-8")
                    # 未进事件也自绘完整结构（纯编制视图），不依赖图片与事件数据
                    self.assertNotIn("org_chart", text)
                    self.assertIn("联合指挥部/工作组", text)
                    self.assertIn("尚未进入任何事件任务空间", text)
                finally:
                    server.shutdown()
            finally:
                patcher.stop()

    # -- 成员首页 ----------------------------------------------------------------

    def test_onboarded_member_sees_self_card_with_full_phone(self):
        member = self._member_onboard()
        status, _, body = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertIn("我的岗位信息", text)
        self.assertIn("13912340001", text)               # 本人完整号码
        self.assertIn("已到场", text)
        self.assertIn("扑救一组（一分区）", text)

    # -- 静态资源 ------------------------------------------------------------------

    def test_static_org_image_decoupled_returns_404(self):
        # P17-P2：原图下线，旧客户端请求不再回退到图片（运行时零图片依赖）。
        status, _, _ = _http("GET", f"{self.base}/static/org_chart.png")
        self.assertEqual(404, status)

    def test_static_whitelist_blocks_other_paths(self):
        status, _, _ = _http("GET", f"{self.base}/static/panel.py")
        self.assertEqual(404, status)
        status, _, _ = _http("GET", f"{self.base}/static/../panda_brain/panel.py")
        self.assertIn(status, (404, 400))

    # -- 总览迁移 ------------------------------------------------------------------

    def test_overview_route_serves_legacy_dark_view(self):
        status, _, body = _http("GET", f"{self.base}/overview", cookie=self.cmd)
        self.assertEqual(200, status)
        self.assertIn("共享态势", body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
