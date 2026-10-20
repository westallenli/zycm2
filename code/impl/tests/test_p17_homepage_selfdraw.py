"""P17-P2 验收：首页组织架构「数据驱动自绘」（02 号 V0.3）。

红线：
- 结构唯一来源 org_model.py；页面自绘节点与连线，运行时零图片依赖
  （不引用 org_chart.png / org_anchors.json，静态路由一并下线）；
- 布局为确定性纯函数：节点与连线无重叠遮挡；
- 登录人按签到绑定/名册指派落入对应组并高亮；无名册、未刷码如实呈现；
- 联系方式按观看者身份脱敏。
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
from html.parser import HTMLParser
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD, EventRegistry
from panda_brain.homepage import (
    BRANCH_ORDER,
    esc,
    mask_for_viewer,
    org_layout,
    render_homepage_view,
    viewer_assignments,
)
from panda_brain.models import PersonRecord
from panda_brain.org_model import POSITION_SLOTS, slot_domain_parent
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p17-selfdraw-secret"

_LIBRARY = [
    PersonRecord(
        name="张三", phone="13912340001", unit="县防火办",
        home_position="扑救一组（一分区）", is_leader=True,
    ),
    PersonRecord(name="李四", phone="13912340002", unit="林场的护站", home_position=""),
]


def _make_registry_with_roster() -> tuple[EventRegistry, str]:
    registry = EventRegistry()
    registry.import_person_library(_LIBRARY)
    record = registry.create_event(
        name="汉江上游山火",
        report_no="HC-P2",
        discovered_at="2026-08-25T08:00:00+08:00",
        source="页面录入",
        location="青岗县",
    )
    registry.add_roster_entry(record.event_id, "13912340001")  # → pjjz-1
    return registry, record.event_id


class OrgLayoutTest(unittest.TestCase):
    """布局模型：完整、确定、无重叠。"""

    def setUp(self):
        self.layout = org_layout()

    def test_counts_match_org_model_exactly(self):
        boxes = self.layout["boxes"]
        tops = [b for b in boxes if b["kind"] == "top"]
        heads = [b for b in boxes if b["kind"] == "head"]
        slots = [b for b in boxes if b["kind"] == "slot"]
        self.assertEqual(2, len(tops))                       # joint + general
        self.assertEqual(len(BRANCH_ORDER), len(heads))
        self.assertEqual(len(POSITION_SLOTS), len(slots))    # 32 个一线组逐一对位
        self.assertEqual(
            {b["code"] for b in slots}, {s.pos_code for s in POSITION_SLOTS}
        )

    def test_deterministic(self):
        self.assertEqual(self.layout, org_layout())

    def test_all_boxes_within_canvas(self):
        cw, ch = self.layout["canvas_w"], self.layout["canvas_h"]
        for b in self.layout["boxes"]:
            self.assertGreaterEqual(b["x"], 0)
            self.assertGreaterEqual(b["y"], 0)
            self.assertLessEqual(b["x"] + b["w"], cw)
            self.assertLessEqual(b["y"] + b["h"], ch)

    def test_no_overlap_between_boxes(self):
        boxes = sorted(
            self.layout["boxes"],
            key=lambda b: (b["x"], b["y"]),
        )
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                separated = (
                    a["x"] + a["w"] <= b["x"]
                    or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"]
                    or b["y"] + b["h"] <= a["y"]
                )
                self.assertTrue(
                    separated,
                    f"节点重叠：{a['label']} 与 {b['label']}",
                )

    def test_column_parent_integrity(self):
        heads = {
            b["code"]: b for b in self.layout["boxes"] if b["kind"] == "head"
        }
        for box in self.layout["boxes"]:
            if box["kind"] != "slot":
                continue
            parent = slot_domain_parent(box["code"])
            # 组卡必须落在其隶属线首的同一列（x 相同）
            same_col = [
                b for b in self.layout["boxes"]
                if b["kind"] == "slot"
                and b["x"] == box["x"]
                and slot_domain_parent(b["code"]) == parent
            ]
            self.assertTrue(same_col)
            head = heads[parent]
            self.assertEqual(head["x"], box["x"])

    def test_links_present_and_axis_aligned(self):
        # 每列两条（母线下垂线 + 列内脊线）+ 顶部三段；所有连线均为水平或垂直
        self.assertEqual(len(BRANCH_ORDER) * 2 + 3, len(self.layout["links"]))
        for x1, y1, x2, y2 in self.layout["links"]:
            self.assertTrue(x1 == x2 or y1 == y2, f"斜线不允许：{(x1, y1, x2, y2)}")


class ViewerLandingTest(unittest.TestCase):
    """登录人按签到绑定 / 名册指派落入对应组。"""

    def setUp(self):
        self.registry, self.event_id = _make_registry_with_roster()
        self.workspace = ScenarioWorkspace("p17-p2")
        self.workspace.import_person_library(_LIBRARY)

    def test_every_clickable_node_has_one_detail_panel(self):
        class Panels(HTMLParser):
            def __init__(self):
                super().__init__(); self.references = []; self.ids = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('data-panel'): self.references.append(attrs['data-panel'])
                if attrs.get('id'): self.ids.append(attrs['id'])
        for role in ('commander', 'member', 'public'):
            with self.subTest(role=role):
                html = render_homepage_view(
                    event_record=self.registry.events[self.event_id], registry=self.registry,
                    workspace=self.workspace, event_id=self.event_id,
                    viewer_role=role, viewer_phone='13912340001' if role == 'member' else '',
                )
                parsed = Panels(); parsed.feed(html)
                self.assertEqual(len(org_layout()['boxes']), len(parsed.references))
                for panel in parsed.references:
                    self.assertEqual(1, parsed.ids.count(panel), panel)

    def test_bound_member_lands_in_group_highlighted(self):
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")
        html = render_homepage_view(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            viewer_role="member",
            viewer_phone="13912340001",
            generated_at="2026-08-25T10:00:00+08:00",
        )
        self.assertIn('data-me="1"', html)
        self.assertIn(f'{esc("张三")}（我）', html)

    def test_roster_only_member_marked_not_arrived(self):
        # 名册已指派 pjjz-1 但未刷码：落组但如实标注「我 · 未到场」
        html = render_homepage_view(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            viewer_role="member",
            viewer_phone="13912340001",
        )
        self.assertIn('data-me="1"', html)
        self.assertIn(f'{esc("张三")}（我 · 未到场）', html)

    def test_assignments_binding_wins_over_roster(self):
        codes = viewer_assignments(
            self.registry, self.workspace, self.event_id,
            viewer_phone="13912340001",
        )
        self.assertEqual([{"pos_code": "pjjz-1", "bound": False}], codes)
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")
        codes = viewer_assignments(
            self.registry, self.workspace, self.event_id,
            viewer_phone="13912340001",
        )
        self.assertEqual([{"pos_code": "pjjz-1", "bound": True}], codes)


class HonestyTest(unittest.TestCase):
    """无名册 / 未排班 / 通信未接入均如实显示，绝不虚构数字。"""

    def setUp(self):
        self.registry, self.event_id = _make_registry_with_roster()
        self.workspace = ScenarioWorkspace("p17-p2-h")
        self.workspace.import_person_library(_LIBRARY)

    def test_no_roster_means_no_fabricated_badges(self):
        # 另建一个完全无名册的事件：到场角标绝不虚构，空态如实标注
        fresh = self.registry.create_event(
            name="无名册事件", report_no="R-P2X",
            discovered_at="2026-08-25T09:00:00+08:00",
            source="页面录入", location="青岗县",
        )
        html = render_homepage_view(
            event_record=self.registry.events[fresh.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=fresh.event_id,
        )
        self.assertNotIn('class="hp-n-badge', html)   # 无名册不出到场角标
        self.assertIn("名册未配置", html)
        self.assertIn("本事件名册未配置", html)

    def test_configured_roster_shows_badge_and_unassigned_note(self):
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")
        html = render_homepage_view(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
        )
        self.assertIn(">1/1</span>", html)     # pjjz-1 满员到齐
        self.assertIn("实到 <b>1</b> / 应到 <b>1</b>", html)
        self.assertIn("通信状态：待接入", html)

    def test_masking_rules_apply_in_node_panels(self):
        self.registry.add_roster_entry(self.event_id, "13912340002")   # 李四也在名册
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")
        commander_html = render_homepage_view(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            viewer_role="commander",
        )
        member_html = render_homepage_view(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            viewer_role="member",
            viewer_phone="13912340001",
        )
        # 本人完整号码在两种视角都可见；他人号码仅指挥员可见
        self.assertIn("13912340001", commander_html)
        self.assertIn("13912340001", member_html)
        self.assertIn("13912340002", commander_html)
        self.assertNotIn("13912340002", member_html)
        self.assertIn(mask_for_viewer(
            "13912340002", viewer_role="member", viewer_phone="13912340001"
        ), member_html)


class SelfDrawE2E(unittest.TestCase):
    """HTTP 层：指挥员与成员首页均为自绘视图；原图路由已下线。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p17-selfdraw", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
        )
        state.registry.import_person_library(_LIBRARY)
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-25T09:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST", f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action, "csrf": next(m.custody_csrf for m in self.state.member_sessions.values() if m.phone in {g.subject_id for g in self.state._duty_grants})},
                cookie=self.cmd,
            )
        personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"},
            cookie=self.cmd,
        )

    def test_commander_page_self_drawn_no_image_reference(self):
        status, _, body = _http("GET", f"{self.base}/", cookie=self.cmd)
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertNotIn("org_chart", text)
        self.assertNotIn("org_anchors", text)
        self.assertNotIn("<img", text)
        self.assertIn("<svg", text)
        self.assertIn("组织架构由系统按岗位编制数据自绘", text)

    def test_onboarded_member_sees_self_highlight_in_org_chart(self):
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        status, _, body = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertIn('data-me="1"', text)          # 登录人高亮入组
        self.assertIn(f'{esc("张三")}（我）', text)
        self.assertNotIn("org_chart", text)

    def test_legacy_image_route_is_gone(self):
        status, _, _ = _http("GET", f"{self.base}/static/org_chart.png")
        self.assertEqual(404, status)
        status, _, _ = _http("GET", f"{self.base}/static/org_anchors.json")
        self.assertEqual(404, status)


# -- HTTP 小工具 ---------------------------------------------------------------

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


if __name__ == "__main__":
    unittest.main()
