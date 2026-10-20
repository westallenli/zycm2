"""P17-P0 验收：五页导航冻结 + 首屏落点 + 前台禁用词回归钉。

口径来源：DEVELOPMENT_PLAN_PAGES_V1 §5/§6-P0。
- 主导航固定五页：火情事件｜人员架构｜沙盘指挥｜人员与物资｜复盘；
  旧 /map /overview /timeline /drill 路由保留兼容但不再进入主导航；
- 成员无火情事件权限：导航不出现「火情事件」，登录后首屏为「/」，
  未刷码者由首页门禁 fail-closed 送回登录页；
- 五页页面源码不出现 AI/参谋自述词（LLM 卡片、智脑候选、参谋部正在等）。
"""

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

_SECRET = "p17-fivepage-secret"

#: 前台禁用词（DEVELOPMENT_PLAN_PAGES_V1 §6-P0 任务 6）：任何一词出现在
#: 五页业务页面源码中即为回归。后台模块注释/审计载荷不受此约束。
_BANNED_FRONT_WORDS: tuple[str, ...] = (
    "LLM",
    "llm",
    "智脑建议",
    "智脑候选",
    "智脑识别",
    "智脑分析",
    "智脑硬约束",
    "智脑不定责",
    "智脑正在",
    "参谋部正在",
    "参谋推荐",
    "AI 分析中",
    "AI 刚产出",
    "AI 候选",
    "AI 草稿",
    "AI 卡片",
)


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
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8")


def _cookie_of(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith(f"{name}="):
            value = part.strip().split("=", 1)[1]
            if value:
                return f"{name}={value}"
    return ""


class FivePageNavE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p17-fivepage", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
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
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.state = state
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        # 指挥员登录 + 建事件 + 启动 + 签发二维码 + 进场（完整指挥侧上下文）
        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.assertEqual(303, status)
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-17",
             "discovered_at": "2026-08-25T09:00:00+08:00", "location": "青岗县",
             "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST",
                f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action},
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
        self.assertTrue(member)
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        return member

    # -- 导航冻结 --------------------------------------------------------------

    def test_commander_nav_is_five_pages_in_fixed_order(self):
        st, _, body = _http("GET", f"{self.base}/events", cookie=self.cmd)
        self.assertEqual(200, st)
        for label in ("火情事件", "人员架构", "沙盘指挥", "人员与物资", "复盘"):
            self.assertIn(f">{label}</a>", body)
        first = body.index(">火情事件</a>")
        second = body.index(">人员架构</a>")
        third = body.index(">沙盘指挥</a>")
        fourth = body.index(">人员与物资</a>")
        fifth = body.index(">复盘</a>")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertLess(third, fourth)
        self.assertLess(fourth, fifth)

    def test_legacy_routes_removed_from_light_nav(self):
        for route in ("/events", "/sand", "/materials", "/review"):
            st, _, body = _http("GET", f"{self.base}{route}", cookie=self.cmd)
            self.assertEqual(200, st)
            self.assertNotIn('href="/map"', body)
            self.assertNotIn('href="/overview"', body)
            self.assertNotIn('href="/timeline"', body)
            self.assertNotIn('href="/drill"', body)
            self.assertNotIn("态势一张图", body)
            self.assertNotIn("指挥台总览", body)
            self.assertNotIn("演练控制台", body)

    def test_legacy_compat_routes_still_resolve(self):
        # 兼容路由保留：直接输入 URL 仍可达（此处以 /overview 为例）。
        st, _, _ = _http("GET", f"{self.base}/overview", cookie=self.cmd)
        self.assertEqual(200, st)

    def test_member_nav_excludes_fire_registration(self):
        member = self._member_onboard()
        st, _, body = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn(">火情事件</a>", body)
        self.assertNotIn('href="/events"', body)
        for label in ("人员架构", "沙盘指挥", "人员与物资", "复盘"):
            self.assertIn(f">{label}</a>", body)

    # -- 首屏落点 ----------------------------------------------------------------

    def test_member_login_lands_on_org_page(self):
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        location = ""
        for key, value in headers.items():
            if key.lower() == "location":
                location = value
        self.assertEqual("/", location)
        # 未刷码 → 首页门禁 fail-closed 送回登录页。
        st, headers2, _ = _http("GET", f"{self.base}/", cookie=member)
        self.assertIn(st, (302, 303))
        self.assertIn("/login", headers2.get("Location", ""))

    def test_home_page_titled_org_structure(self):
        st, _, body = _http("GET", f"{self.base}/", cookie=self.cmd)
        self.assertEqual(200, st)
        # 浅色五页模板无 h1 标题行，页面名在 <title> 与导航高亮中体现。
        self.assertIn("<title>人员架构 · 破局智援参谋前指平台</title>", body)
        self.assertIn('<a href="/" class="on">人员架构</a>', body)

    # -- 前台禁用词回归钉 ----------------------------------------------------------

    def test_five_commander_pages_free_of_banned_words(self):
        for route in ("/events", "/", "/sand", "/materials", "/review"):
            st, _, body = _http("GET", f"{self.base}{route}", cookie=self.cmd)
            self.assertEqual(200, st, route)
            hits = [w for w in _BANNED_FRONT_WORDS if w in body]
            self.assertEqual([], hits, f"{route} 出现禁用自述词: {hits}")

    def test_member_pages_free_of_banned_words(self):
        member = self._member_onboard()
        for route in ("/", "/sand", "/materials", "/review"):
            st, _, body = _http("GET", f"{self.base}{route}", cookie=member)
            self.assertEqual(200, st, route)
            hits = [w for w in _BANNED_FRONT_WORDS if w in body]
            self.assertEqual([], hits, f"{route} 出现禁用自述词: {hits}")


if __name__ == "__main__":
    unittest.main()
