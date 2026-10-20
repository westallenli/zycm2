"""P16-T6 验收：指挥调度沙盘页（doc 03——阶段机/标绘核验链/测算/问询/权限）。"""

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
from panda_brain.official_source import OfficialPage
from panda_brain.settings import SystemSettings
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-sand-secret"


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
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8", "replace")
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


class SandPageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-sand", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            imagery_path=tmp / "imagery.json",
            upload_dir=tmp / "uploads",
        )
        self.state.registry.import_person_library(
            [
                PersonRecord(
                    name="张三", phone="13912340001", unit="县防火办",
                    home_position="扑救一组（一分区）",
                ),
                PersonRecord(name="李四", phone="13912340002", unit="林场哨所",
                             home_position="扑救二组（二分区）"),
            ]
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http("POST", f"{self.base}/events/action",
                  {"event_id": "fire-001", "action": action}, cookie=self.cmd)

    def _member_session(self) -> str:
        personnel_submit(self.state, _http, "POST", f"{self.base}/events/fire-001/roster/add",
              {"phones": "13912340002"}, cookie=self.cmd)
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340002", "password": DEMO_INITIAL_PASSWORD},
        )
        cookie = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=cookie)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=cookie)
        return cookie

    def _onboard_zhang(self) -> None:
        personnel_submit(self.state, _http, "POST", f"{self.base}/events/fire-001/roster/add",
              {"phones": "13912340001"}, cookie=self.cmd)
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)

    def _annotate(self, title="东线火点", points="900,300", kind="fire_point") -> None:
        st, _, _ = _http(
            "POST", f"{self.base}/sand/annotate",
            {"kind": kind, "title": title, "points": points,
             "certainty": "", "note": ""},
            cookie=self.cmd,
        )
        self.assertEqual(303, st)

    # -- 门禁与基础呈现 -------------------------------------------------

    def test_anonymous_redirects_to_login(self):
        st, headers, _ = _http("GET", f"{self.base}/sand")
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))

    def test_commander_sees_full_page(self):
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        for needle in ("现场阶段", "火情响应", "态势问询", "自由区域测算", "核验链",
                       "本页不以示意图冒充真实地图"):
            self.assertIn(needle, body)
        # 未入岗：初期侦查未满足，如实显示
        self.assertIn("已入岗绑定 0 条", body)

    def test_stage_advances_with_real_signals(self):
        self._onboard_zhang()
        self._annotate(title="推定火线", points="500,200 700,260", kind="fireline")
        # 提交核验 → 已核验（态势形成条件满足）
        _http("POST", f"{self.base}/sand/annotate-status",
              {"annotation_id": "ann-001", "target": "pending_review"}, cookie=self.cmd)
        _http("POST", f"{self.base}/sand/annotate-status",
              {"annotation_id": "ann-001", "target": "verified"}, cookie=self.cmd)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("已入岗绑定 1 条", body)
        self.assertIn("已核验/已发布标绘 1 条", body)
        self.assertIn("态势形成", body)

    # -- 标绘核验链 -----------------------------------------------------

    def test_annotate_and_lifecycle(self):
        self._annotate()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("东线火点", body)
        self.assertIn("ann-001", body)
        # 非法流转：草稿直接发布 → 拒绝
        st, _, body = _http("POST", f"{self.base}/sand/annotate-status",
                            {"annotation_id": "ann-001", "target": "published"},
                            cookie=self.cmd)
        self.assertEqual(409, st)
        # 合法链：draft → pending_review → verified → published
        for target in ("pending_review", "verified", "published"):
            st, _, _ = _http("POST", f"{self.base}/sand/annotate-status",
                             {"annotation_id": "ann-001", "target": target},
                             cookie=self.cmd)
            self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("已发布", body)
        # 发布后撤回保留历史
        st, _, _ = _http("POST", f"{self.base}/sand/annotate-status",
                         {"annotation_id": "ann-001", "target": "withdrawn"},
                         cookie=self.cmd)
        self.assertEqual(303, st)

    def test_ai_candidate_demo_flow(self):
        st, _, _ = _http("POST", f"{self.base}/sand/ai-candidate", {}, cookie=self.cmd)
        self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("【演示】疑似火点", body)
        self.assertIn("模拟后台送达一条候选（演示数据）", body)
        self.assertIn("确认为正式态势", body)
        # 确认候选 → 已核验
        st, _, _ = _http("POST", f"{self.base}/sand/candidate",
                         {"annotation_id": "ann-001", "action": "verify"}, cookie=self.cmd)
        self.assertEqual(303, st)
        marks = self.state.workspace.annotations
        self.assertEqual("verified", marks["ann-001"].status.value)
        self.assertEqual("ai-suggestion", marks["ann-001"].source.value)

    # -- 自由测算 -------------------------------------------------------

    def test_measure_temp_only(self):
        st, _, body = _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "100,100 300,100 300,250 100,250"}, cookie=self.cmd,
        )
        self.assertEqual(200, st)
        self.assertIn("面积", body)
        self.assertIn("示意值", body)
        # 正方形 200x150 单位 → 200*4m * 150*4m = 800m * 600m = 480000 m²
        self.assertIn("480,000.0 平方米", body)
        # 临时测算不落库
        self.assertEqual({}, self.state.workspace.annotations)
        # 输入不合法：明示失败不伪造
        st, _, body = _http(
            "POST", f"{self.base}/sand/measure",
            {"points": "100,100 200,100"}, cookie=self.cmd,
        )
        self.assertEqual(200, st)
        self.assertIn("测算失败", body)

    # -- 问询 -----------------------------------------------------------

    def test_ask_personnel_and_unknown(self):
        self._onboard_zhang()
        st, _, _ = _http("POST", f"{self.base}/sand/ask",
                         {"question": "现在队伍多少人"}, cookie=self.cmd)
        self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("当前在岗 1 人", body)
        self.assertIn("张三", body)
        # 回答六字段齐全（doc 03 §13.1）
        self.assertIn("数据时间", body)
        self.assertIn("置信度", body)
        # 无关问题：明说无法回答并列出支持主题
        st, _, _ = _http("POST", f"{self.base}/sand/ask",
                         {"question": "今天晚饭吃什么"}, cookie=self.cmd)
        self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("无法从当前数据回答该问题", body)

    def test_ask_empty_question_is_rejected(self):
        st, _, body = _http("POST", f"{self.base}/sand/ask",
                            {"question": "   "}, cookie=self.cmd)
        self.assertEqual(200, st)  # 内联错误卡，不中断页面
        self.assertIn("问询失败", body)

    def test_official_link_is_explicit_read_only_action(self):
        url = "https://www.mem.gov.cn/gk/notice.html"
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn('action="/sand/source-check"', body)
        with unittest.mock.patch("panda_brain.settings.load_settings",
                                 return_value=SystemSettings()):
            st, _, body = _http("POST", f"{self.base}/sand/source-check",
                                {"source_url": url}, cookie=self.cmd)
            self.assertEqual(200, st)
            self.assertIn("联网资料核验未启用", body)
            page = OfficialPage(url=url, title="官方公告",
                                excerpt="<危险>需核对", stated_date="2026-10-05",
                                fetched_at="2026-10-06T00:00:00+00:00")
            with unittest.mock.patch("panda_brain.official_source.read_official_page",
                                     return_value=page) as reader:
                st, _, body = _http("POST", f"{self.base}/sand/source-check",
                                    {"source_url": url}, cookie=self.cmd)
        self.assertEqual(200, st)
        reader.assert_called_once_with(url, network_enabled=False)
        self.assertIn("官方网页摘录（待人工核对）", body)
        self.assertIn("&lt;危险&gt;需核对", body)
        self.assertNotIn("<危险>", body)
        self.assertEqual([], self.state.inquiry_journal.for_viewer(
            self.state.active_event_id, "local-general-entry"))

    # -- 权限矩阵（doc 03 §12） -----------------------------------------

    def test_member_readonly_but_can_ask(self):
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn("标绘作业（指挥员）", body)
        self.assertNotIn("自由区域测算（临时，指挥员）", body)
        # 维护动作全部拒绝
        st, _, _ = _http("POST", f"{self.base}/sand/annotate",
                         {"kind": "fire_point", "title": "x", "points": "1,1"},
                         cookie=member)
        self.assertEqual(403, st)
        st, _, _ = _http("POST", f"{self.base}/sand/measure",
                         {"points": "1,1 2,2 3,3"}, cookie=member)
        self.assertEqual(403, st)
        st, _, _ = _http("POST", f"{self.base}/sand/ai-candidate", {}, cookie=member)
        self.assertEqual(403, st)
        # 问询允许
        st, _, _ = _http("POST", f"{self.base}/sand/ask",
                         {"question": "图上有哪些火点"}, cookie=member)
        self.assertEqual(303, st)

    def test_projector_public_view_hides_unpublished(self):
        self._annotate(title="已发布火点")
        self._annotate(title="草稿水源", kind="water")
        for target in ("pending_review", "verified", "published"):
            _http("POST", f"{self.base}/sand/annotate-status",
                  {"annotation_id": "ann-001", "target": target}, cookie=self.cmd)
        st, _, body = _http("GET", f"{self.base}/sand?projector=1", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("公共屏 · 仅已发布内容", body)
        self.assertIn("已发布火点", body)
        self.assertNotIn("草稿水源", body)
        # 公共屏无作业工具、无候选标注区
        self.assertNotIn("标绘作业", body)
        self.assertNotIn("模拟后台送达一条候选", body)

    # -- 真实底图（doc 03 §6） ------------------------------------------

    def test_real_map_without_coords_is_honest(self):
        # P18：无坐标时仍加载真实地图壳，但必须显式横幅说明，不以示意图損塞
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("当前无事件坐标，也没有已录入地理坐标的标绘", body)
        self.assertNotIn("示意底图 · 非实测坐标", body)

    def test_real_map_with_coords_and_validation(self):
        # 非法坐标 → 409 不入库
        received=receive_confirmed_fixture(self.state, {"name": "错标火情", "report_no": "HC-ERR",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "某县", "lat": "999", "lon": "200"})
        self.assertEqual("INVALID_ARGUMENT",received["code"])
        # 合法坐标建新事件并进入
        received=receive_confirmed_fixture(self.state, {"name": "带坐标火情", "report_no": "HC-GEO",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "青岗县北", "lat": "30.1234", "lon": "103.5678"})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http("POST", f"{self.base}/events/action",
                  {"event_id": "fire-002", "action": action}, cookie=self.cmd)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("leaflet", body.lower())
        self.assertIn("30.1234", body)
        self.assertIn("瓦片加载失败", body)  # 失败横幅脚本随页携带
        # 坐标落盘持久化
        self.assertEqual(self.state.registry.events["fire-002"].lat, 30.1234)

    # -- 无人机影像导入与配准（doc 03 §9） ------------------------------

    def _multipart(self, fields: dict, file_field: tuple[str, str, bytes] | None):
        boundary = "----p16boundary"
        lines = []
        for name, value in fields.items():
            lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}")
        body = ("\r\n".join(lines)).encode()
        if file_field is not None:
            fname, ctype, payload = file_field
            part = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n"
            ).encode() + payload + b"\r\n"
            body += b"\r\n" + part if body else part
        body += f"--{boundary}--\r\n".encode()
        return body, f"multipart/form-data; boundary={boundary}"

    def _upload_png(self, *, has_spatial="0", spatial_range="", **overrides) -> str:
        png = b"\x89PNG\r\n\x1a\n" + b"fakeimagedata" * 10
        fields = {
            "source_system": overrides.get("source_system", "护林通无人机"),
            "collected_by": overrides.get("collected_by", "县无人机队"),
            "collected_at": overrides.get("collected_at", "2026-08-24T09:30:00+08:00"),
            "has_spatial": has_spatial,
            "spatial_range": spatial_range,
            "geo_precision": "±5m",
            "note": "",
        }
        raw, ctype = self._multipart(fields, ("spot.png", "image/png", png))
        req = urllib.request.Request(
            f"{self.base}/sand/image-upload", data=raw, method="POST"
        )
        req.add_header("Cookie", self.cmd)
        req.add_header("Content-Type", ctype)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        if code != 303:
            raise AssertionError(f"upload failed: {code}")
        return next(iter(self.state.imagery.images))

    def test_image_upload_registration_analysis_flow(self):
        img_id = self._upload_png(has_spatial="0")
        image = self.state.imagery.get(img_id)
        self.assertEqual("awaiting_registration", image.status)
        self.assertTrue(image.file_path.startswith("uploads/fire-001/"))
        # 原图落盘且可被指挥员读取（file_path 相对于静态目录）
        from panda_brain.panel import UPLOAD_DIR
        stored = Path(self.state.upload_dir).parent / image.file_path
        self.assertTrue(stored.exists())
        st, headers, _ = _http("GET", f"{self.base}/uploads/{image.file_path}",
                               cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("image/png", headers.get("Content-Type", ""))

        # 未配准硬拦截分析（状态机与配准守卫双重防线）
        st, _, body = _http("POST", f"{self.base}/sand/image-analyze",
                            {"image_id": img_id}, cookie=self.cmd)
        self.assertEqual(409, st)
        self.assertTrue(("未配准" in body) or ("不允许" in body), body)

        # 人工配准 → 叠加 → 演示分析 → 候选生成
        for target in ("located", "overlaid"):
            st, _, _ = _http("POST", f"{self.base}/sand/image-status",
                             {"image_id": img_id, "target": target,
                              "spatial_range": "四至:103.9,30.1~103.95,30.15"},
                             cookie=self.cmd)
            self.assertEqual(303, st)
        ann_id = None
        st, _, _ = _http("POST", f"{self.base}/sand/image-analyze",
                         {"image_id": img_id}, cookie=self.cmd)
        self.assertEqual(303, st)
        image = self.state.imagery.get(img_id)
        self.assertEqual("candidate_generated", image.status)
        ann_id = image.related_annotation_id
        self.assertTrue(ann_id and ann_id in self.state.workspace.annotations)
        self.assertEqual("pending_review",
                         self.state.workspace.annotations[ann_id].status.value)

        # 指挥员确认 → 标注转已核验、图像转已确认
        st, _, _ = _http("POST", f"{self.base}/sand/image-decision",
                         {"image_id": img_id, "decision": "confirm"},
                         cookie=self.cmd)
        self.assertEqual(303, st)
        self.assertEqual("confirmed", self.state.imagery.get(img_id).status)
        self.assertEqual("verified",
                         self.state.workspace.annotations[ann_id].status.value)

    def test_image_reject_keeps_history(self):
        img_id = self._upload_png(has_spatial="1",
                                  spatial_range="覆盖青岗河谷东段")
        image = self.state.imagery.get(img_id)
        self.assertEqual("located", image.status)  # 有空间信息自动定位
        _http("POST", f"{self.base}/sand/image-status",
              {"image_id": img_id, "target": "overlaid"}, cookie=self.cmd)
        _http("POST", f"{self.base}/sand/image-analyze",
              {"image_id": img_id}, cookie=self.cmd)
        ann_id = self.state.imagery.get(img_id).related_annotation_id
        st, _, _ = _http("POST", f"{self.base}/sand/image-decision",
                         {"image_id": img_id, "decision": "reject"},
                         cookie=self.cmd)
        self.assertEqual(303, st)
        self.assertEqual("rejected", self.state.imagery.get(img_id).status)
        self.assertEqual("withdrawn",
                         self.state.workspace.annotations[ann_id].status.value)
        # 原图与记录保留，不物理删除
        stored = Path(self.state.upload_dir).parent / self.state.imagery.get(img_id).file_path
        self.assertTrue(stored.exists())

    def test_upload_rejects_bad_content_and_member(self):
        member = self._member_session()
        # 成员上传 → 403
        png = b"\x89PNG\r\n\x1a\nxx"
        raw, ctype = self._multipart({"source_system": "s",
                                      "collected_at": "t", "has_spatial": "0"},
                                     ("x.png", "image/png", png))
        req = urllib.request.Request(f"{self.base}/sand/image-upload",
                                     data=raw, method="POST")
        req.add_header("Cookie", member)
        req.add_header("Content-Type", ctype)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(403, code)
        # 伪图像（魔数不符）→ 409
        raw, ctype = self._multipart({"source_system": "s",
                                      "collected_at": "t", "has_spatial": "0"},
                                     ("fake.png", "image/png", b"not-an-image"))
        req = urllib.request.Request(f"{self.base}/sand/image-upload",
                                     data=raw, method="POST")
        req.add_header("Cookie", self.cmd)
        req.add_header("Content-Type", ctype)
        try:
            with opener.open(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(409, code)
        # 成员看沙盘：无原图链接、无导入表单；指挥员可见
        self._upload_png()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=member)
        self.assertNotIn("看原图", body)
        self.assertNotIn("导入图像", body)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("看原图", body)
        self.assertIn("导入图像", body)
        # 成员直接访问原图 URL → 404
        img = next(iter(self.state.imagery.images.values()))
        st, _, _ = _http("GET", f"{self.base}/uploads/{img.file_path}",
                         cookie=member)
        self.assertEqual(404, st)

    # -- P16-T11：地理坐标标注叠加（doc 03 §6 点线面上真图） -------------

    def test_annotate_with_geo_renders_on_real_map(self):
        st, _, _ = _http(
            "POST", f"{self.base}/sand/annotate",
            {"kind": "fire_point", "title": "东线火点", "points": "900,300",
             "certainty": "", "note": "", "geo": "33.12,108.45"},
            cookie=self.cmd,
        )
        self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("33.12", body)
        self.assertIn("ann-001", body)
        # P18：带坐标标绘进入真实地图载荷（旧叠加卡文案已由新壳承接）
        import json as _json
        raw = body.split('id="cmd-map-data">')[1].split("</script>")[0]
        data = _json.loads(raw.replace("<\\/", "</"))
        ids = [m["id"] for m in data["marks"]]
        self.assertIn("ann-001", ids)
        mark = next(m for m in data["marks"] if m["id"] == "ann-001")
        self.assertEqual([[33.12, 108.45]], mark["pts"])

    def test_annotate_geo_attach_flow_with_validation(self):
        self._annotate()  # 无坐标草稿火点
        # 顶点数不一致 → 拒绝（不做自动投影）
        st, _, _ = _http("POST", f"{self.base}/sand/annotate-geo",
                         {"annotation_id": "ann-001",
                          "geo": "33.12,108.45;33.13,108.46"}, cookie=self.cmd)
        self.assertEqual(409, st)
        # 越界纬度 → 拒绝
        st, _, _ = _http("POST", f"{self.base}/sand/annotate-geo",
                         {"annotation_id": "ann-001",
                          "geo": "95.0,108.45"}, cookie=self.cmd)
        self.assertEqual(409, st)
        # 合法补录 → 审计事件 + 上图
        st, _, _ = _http("POST", f"{self.base}/sand/annotate-geo",
                         {"annotation_id": "ann-001",
                          "geo": "33.12，108.45"}, cookie=self.cmd)
        self.assertEqual(303, st)
        mark = self.state.workspace.annotations["ann-001"]
        self.assertEqual(((33.12, 108.45),), mark.geo_points)
        kinds = [e.kind for e in self.state.workspace._events]
        self.assertIn("AnnotationGeoSet", kinds)
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("33.12", body)

    def test_geo_annotation_role_visibility_chain(self):
        self._annotate()
        _http("POST", f"{self.base}/sand/annotate-geo",
              {"annotation_id": "ann-001", "geo": "33.12,108.45"},
              cookie=self.cmd)
        # 公共屏（projector 模式）：草稿不可见
        st, _, body = _http("GET", f"{self.base}/sand?projector=1", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertNotIn("33.12", body)
        # 成员：待核验即可见但未发布（虚线）
        _http("POST", f"{self.base}/sand/annotate-status",
              {"annotation_id": "ann-001", "target": "pending_review"},
              cookie=self.cmd)
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=member)
        self.assertIn("33.12", body)
        self.assertIn('"published": false', body)
        # 发布后公共屏可见
        for target in ("verified", "published"):
            _http("POST", f"{self.base}/sand/annotate-status",
                  {"annotation_id": "ann-001", "target": target},
                  cookie=self.cmd)
        st, _, body = _http("GET", f"{self.base}/sand?projector=1", cookie=self.cmd)
        self.assertIn("33.12", body)
        self.assertIn('"published": true', body)

    def test_geo_tools_card_forms_and_member_absence(self):
        self._annotate()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn("/sand/annotate-geo", body)
        self.assertIn("未录入", body)
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/sand", cookie=member)
        self.assertNotIn("/sand/annotate-geo", body)
        # 成员直连补录接口 → 403（指挥员作业）
        st, _, _ = _http("POST", f"{self.base}/sand/annotate-geo",
                         {"annotation_id": "ann-001", "geo": "33.12,108.45"},
                         cookie=member)
        self.assertEqual(403, st)


if __name__ == "__main__":
    unittest.main()
