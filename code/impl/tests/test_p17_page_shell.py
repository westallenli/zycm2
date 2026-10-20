"""P17 修复回归钉：五页必须走统一页壳（_wrap_business_page → _light_wrap）。

背景：/sand /materials /review 曾返回裸片段（无 DOCTYPE/样式/导航），浏览器里
内容堆平、看起来「没有界面」；单测只断言内容标记抓不到。本钉把「完整页面
结构」本身钉死：任何业务面 GET 响应必须是整页（DOCTYPE 开头、</html> 结尾、
含 <style> 与五页顶栏），禁止在路由里直接返回渲染函数的裸片段。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from tests.confirmed_fire_support import receive_confirmed_fixture
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

_SECRET = "p17-shell-secret"


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


class PageShellE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p17-shell", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
        )
        state.registry.import_person_library(
            [
                PersonRecord(
                    name="张三", phone="13912340001", unit="县防火办",
                    home_position="扑救一组（一分区）",
                ),
                PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
            ]
        )
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        received=receive_confirmed_fixture(self.state, {"name": "测试山火", "report_no": "SH-1",
             "discovered_at": "2026-08-25T08:00:00+08:00", "location": "西坡"})
        self.assertEqual("CREATED",received["code"])

    def _get(self, path: str, cookie: str = "") -> tuple[int, str]:
        status, _, html = _http("GET", f"{self.base}{path}", cookie=cookie or self.cmd)
        return status, html


class FullPageStructureTest(PageShellE2E):
    def test_five_pages_are_full_documents(self):
        """指挥员视角五页：DOCTYPE 开头、</html> 结尾、含 <style> 与顶栏。"""
        for path in ("/", "/events", "/sand", "/materials", "/review"):
            status, html = self._get(path)
            with self.subTest(path=path):
                self.assertEqual(status, 200)
                self.assertTrue(
                    html.lstrip().startswith("<!DOCTYPE html>"),
                    f"{path} 不是整页文档——裸片段会被浏览器渲染成无样式乱页",
                )
                self.assertTrue(html.rstrip().endswith("</html>"), f"{path} 未闭合")
                self.assertIn("<style>", html)
                self.assertIn("指挥协同工作台", html)
                for label in ("火情事件", "人员架构", "沙盘指挥", "人员与物资", "复盘"):
                    self.assertIn(label, html, f"{path} 顶栏缺导航项「{label}」")

    def test_review_projector_public_shell(self):
        """公共屏免登录：整页壳、无顶栏导航、只声明公共屏。"""
        status, html = self._get("/review?projector=1", cookie="")
        self.assertEqual(status, 200)
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertTrue(html.rstrip().endswith("</html>"))
        self.assertIn("<style>", html)
        self.assertIn("公共屏 · 仅已发布内容", html)
        self.assertNotIn('href="/events"', html)

    def test_sand_projector_shell(self):
        """沙盘公共屏模式（登录会话 + projector=1）：也不得出现主导航。"""
        status, html = self._get("/sand?projector=1")
        self.assertEqual(status, 200)
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("公共屏 · 仅已发布内容", html)
        self.assertNotIn('href="/events"', html)

    def test_member_shell_uses_member_nav(self):
        """成员视角：整页 + 成员导航（无火情事件入口）+ 退出登录。"""
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        self.assertTrue(member)
        for path in ("/sand", "/materials", "/review"):
            req = urllib.request.Request(self.base + path, headers={"Cookie": member})
            opener = urllib.request.build_opener()
            try:
                with opener.open(req, timeout=5) as resp:
                    html = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                self.fail(f"{path} 成员 GET 失败 {exc.code}")
            with self.subTest(path=path):
                self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
                self.assertTrue(html.rstrip().endswith("</html>"))
                self.assertNotIn('href="/events"', html)
                self.assertIn("退出登录", html)


class HomepageFitTest(PageShellE2E):
    def test_org_canvas_fits_window(self):
        """自绘组织图：按窗口自适应缩放脚本在位（不靠拖动看全图）。"""
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("fitOrg", html)
        self.assertIn("window.addEventListener('resize'", html)
        self.assertIn("正在自适应窗口缩放", html)
        # 旧的固定档位已废弃
        self.assertNotIn("0.55,0.75", html)

    def test_zoom_buttons_still_present(self):
        status, html = self._get("/")
        self.assertEqual(status, 200)
        for label in ("缩小", "重置缩放", "放大"):
            self.assertIn(f'aria-label="{label}"', html)


if __name__ == "__main__":
    unittest.main()
