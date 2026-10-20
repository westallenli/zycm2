"""P16-T16 批次2验收：沙盘 §5 双向联动、§7 图层开关、§11 测算存为业务对象。

定版口径（03 号）：
- §5 点击地图对象展开态势详情；点击右栏条目定位图上对象（原生 JS，无 fetch）；
- §7 图层显隐开关（火情态势/资源部署/任务标绘三组）；
- §11 自由测算临时不落库；选择区域类型可一键存为「草稿」业务对象，
  走既有核验链（提交核验→确认→发布），绝不直接入正式态势。
"""

from __future__ import annotations

import json
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
from panda_brain.map_annotations import AnnotationKind, AnnotationStatus
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-interact-secret"


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
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class SandInteractE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = PanelState(
            ScenarioWorkspace("p16-interact", general_auth=GeneralEntryAuth(secret=_SECRET)),
            auth=GeneralEntryAuth(secret=_SECRET),
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        form = urllib.parse.urlencode({"username": "admin", "secret": _SECRET}).encode()
        req = urllib.request.Request(f"{self.base}/login", data=form, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        raw = ""
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
        self.cmd = raw.split(";")[0] if raw else ""
        # 登录后指挥员入口已解锁，再预置一条带地理坐标的人工标绘（P18 真实地图载荷）
        self.state.handle_sand_annotate(
            kind="fire_point", title="北坡火点", points="100,100",
            geo_text="32.50,110.50")

    def _get_sand(self):
        st, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        return body

    # -- §7 图层开关 + §5 联动渲染 ----------------------------------------

    def test_map_renders_layers_and_click_hooks(self):
        """P18 重排后：图层开关与载荷 JSON 由真实地图壳承接。"""
        body = self._get_sand()
        for layer in ("published", "candidate", "draft"):
            self.assertIn(f'data-cmd-layer="{layer}"', body)
        self.assertIn('id="cmd-map-data"', body)
        raw = body.split('id="cmd-map-data">')[1].split("</script>")[0]
        data = json.loads(raw.replace("<\\/", "</"))
        self.assertIn("ann-001", [m["id"] for m in data["marks"]])
        mark = next(m for m in data["marks"] if m["id"] == "ann-001")
        self.assertEqual("火点·北坡火点", mark["label"])

    def test_right_column_links_and_detail_card(self):
        body = self._get_sand()
        self.assertIn('id="cmd-bottom-info"', body)   # 底部对象条承接点击详情
        self.assertIn("点击地图对象查看详情", body)

    # -- §11 测算：纯计算不落库 / 一键存草稿走核验链 -----------------------

    def _count_marks(self):
        return len(self.state.workspace.annotations)

    def test_measure_only_leaves_no_trace(self):
        st, body = _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "0,0 400,0 400,300"}, cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("面积约", body)
        self.assertNotIn("已存为业务对象草稿", body)
        self.assertEqual(1, self._count_marks())

    def test_measure_save_creates_draft_via_review_chain(self):
        st, body = _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "0,0 400,0 400,300", "area_type": "危险区", "save": "1"},
            cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("已存为业务对象草稿", body)
        marks = list(self.state.workspace.annotations.values())
        self.assertEqual(2, len(marks))
        mark = marks[-1]
        self.assertEqual(AnnotationKind.AREA, mark.kind)
        self.assertEqual(AnnotationStatus.DRAFT, mark.status)   # 草稿，未发布
        self.assertIn("示意值", mark.note)
        self.assertIn("自由区域测算存档", mark.note)
        self.assertEqual((), self.state.workspace.verify_audit_chain())

    def test_measure_save_isolation_maps_kind(self):
        _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "10,10 50,10 50,50", "area_type": "隔离带", "save": "1"},
            cookie=self.cmd)
        marks = list(self.state.workspace.annotations.values())
        self.assertEqual(AnnotationKind.ISOLATION, marks[-1].kind)

    def test_member_cannot_measure_or_save(self):
        # 无会话/伪造会话 → 重定向登录页（fail-closed）
        for cookie in ("", "member_session=fake"):
            st, _ = _http(
                "POST", f"{self.base}/sand/measure",
                {"points": "0,0 400,0 400,300"}, cookie=cookie)
            self.assertIn(st, (302, 303))
        # 真实成员会话 → 沙盘作业动作 403（ask_only 白名单外）
        self.state.registry.import_person_library([
            PersonRecord(name="王五", phone="13912340009",
                         unit="林场哨所", home_position="供水组")])
        form = urllib.parse.urlencode(
            {"phone": "13912340009", "password": DEMO_INITIAL_PASSWORD}).encode()
        req = urllib.request.Request(
            f"{self.base}/login/member", data=form, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        raw = ""
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
        mcookie = raw.split(";")[0] if raw else ""
        self.assertTrue(mcookie, "成员登录应发会话 Cookie")
        st, body = _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "0,0 400,0 400,300"}, cookie=mcookie)
        self.assertEqual(403, st)
        self.assertEqual(1, self._count_marks())


if __name__ == "__main__":
    unittest.main()
