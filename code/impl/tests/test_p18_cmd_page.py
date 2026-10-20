"""P18 指挥工作台回归钉（docs/18_沙盘指挥页开发设计_V1.0 实现验收第一批）。

- 页面壳：全屏地图主区 + 右侧「参谋对话框／方案推演区」+ 底部对象条；
- 四类信息全景（事实/判断/建议/指令）保留并收进抽屉（兼容 doc 03 §4 口径钉）；
- 图上直接绘制产出地理坐标标绘；未录坐标不上真实地图；
- 方案区决断：采纳须显式接受全部风险；否决/暂缓必填原因且不可重复否决；
- 目标框登记/确认与信息缺口「按现有资料继续」均入审计哈希链。
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import EventRegistry
from panda_brain.map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
)
from panda_brain.models import (
    AdvisorRole,
    InformationGap,
    InformationGapStatus,
    Option,
)
from panda_brain.panel import PanelState, PanelActionError
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace


class _FakeState:
    """render_sand_page 只需 workspace 与若干惰性属性。"""

    def __init__(self, ws: ScenarioWorkspace):
        self.workspace = ws
        self.imagery = None
        self.ledger = None
        self.active_event_id = ""
        self.registry = EventRegistry()
        self.sand_qa_log = []
        self.intent_notice = ""
        self.voice_drafts = {}
        self.advisor_log = []


def _geo_mark(ws: ScenarioWorkspace, ann_id: str, title: str, status: AnnotationStatus,
              pts: tuple[tuple[float, float], ...]) -> None:
    ws.record_annotation(
        MapAnnotation(
            id=ann_id,
            kind=AnnotationKind.FIRE_POINT,
            points=(),
            geo_points=pts,
            title=title,
            status=status,
            source=AnnotationSource.MANUAL,
        )
    )


class CommandShellLayoutTest(unittest.TestCase):
    """页面壳与角色可见性。"""

    def setUp(self):
        self.ws = ScenarioWorkspace("p18-shell")
        _geo_mark(self.ws, "ann-pub-1", "已发布火点", AnnotationStatus.PUBLISHED,
                  ((32.10, 110.20),))
        _geo_mark(self.ws, "ann-drft-1", "草稿火线", AnnotationStatus.DRAFT,
                  ((32.11, 110.21), (32.12, 110.22)))
        self.state = _FakeState(self.ws)

    def render(self, role="commander"):
        return render_sand_page(self.state, role=role)

    def test_commander_shell_ids_present_once(self):
        html = self.render()
        for anchor in (
            'id="cmd-page"', 'id="cmd-map"', 'id="cmd-map-data"',
            'id="cmd-advisor"', 'id="cmd-plans"', 'id="cmd-bottom"',
            'id="cmd-rail"',
        ):
            self.assertEqual(1, html.count(anchor), anchor)

    def test_four_category_block_preserved_once_each(self):
        html = self.render()
        for header in (
            "<h3>事实 · 观测与证据</h3>",
            "<h3>判断 · 分析与推断</h3>",
            "<h3>建议 · 待指挥员决断</h3>",
            "<h3>指令 · 正式命令</h3>",
        ):
            self.assertEqual(1, html.count(header), header)
        self.assertIn("事实/判断/建议/指令（规范 03·§4）", html)

    def test_commander_has_compose_and_decision_forms(self):
        _add_option(self.ws, "opt-shell", "北侧阻隔", risks=())
        html = self.render()
        for marker in (
            'action="/sand/goal"', 'action="/sand/ask"',
            'action="/sand/intent"', 'action="/sand/restaff-sand"',
            'action="/sand/self-option"', 'action="/sand/plan-decision"',
        ):
            self.assertIn(marker, html)

    def test_member_is_read_only(self):
        html = self.render(role="member")
        self.assertNotIn('data-draw-kind="fire_point"', html)
        self.assertNotIn('action="/sand/goal"', html)
        self.assertNotIn('action="/sand/plan-decision"', html)
        self.assertIn('action="/sand/ask"', html)          # 问询保留
        self.assertIn("命令与决断仅指挥员可用", html)

    def test_public_sees_only_published_and_no_panels(self):
        html = self.render(role="public")
        self.assertNotIn('id="cmd-advisor"', html)
        self.assertNotIn('id="cmd-plans"', html)
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S,
        )
        self.assertIsNotNone(raw)
        self.assertIn("ann-pub-1", raw.group(1))
        self.assertNotIn("ann-drft-1", raw.group(1))

    def test_schematic_only_mark_listed_not_mapped(self):
        """仅示意坐标的旧标绘：出现在核验链表格，但不进真实地图载荷。"""
        self.ws.record_annotation(
            MapAnnotation(
                id="ann-schem-1",
                kind=AnnotationKind.FIRE_POINT,
                points=((860.0, 300.0),),
                title="示意坐标老标绘",
                status=AnnotationStatus.PUBLISHED,
                source=AnnotationSource.MANUAL,
            )
        )
        html = self.render()
        self.assertIn("示意坐标老标绘", html)
        self.assertIn("未录坐标，未上图", html)
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S,
        )
        self.assertNotIn("ann-schem-1", raw.group(1))


def _real_state(tmp: Path, secret: str = "p18-secret") -> PanelState:
    auth = GeneralEntryAuth(secret=secret)
    state = PanelState(
        ScenarioWorkspace("p18-domain", general_auth=auth),
        auth=auth,
        registry_path=tmp / "reg.json",
        materials_path=tmp / "mat.json",
    )
    state.login(secret)
    return state


class GeoDrawFlowTest(unittest.TestCase):
    """图上绘制 → 仅地理坐标入库 → 出现在真实地图载荷。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _real_state(self.tmp)

    def test_geo_only_annotation_is_accepted_and_mapped(self):
        ann_id = self.state.handle_sand_annotate(
            kind="fireline", title="北坡火线",
            points="", geo_text="32.10,110.20;32.12,110.24",
        )
        self.assertTrue(ann_id)
        html = render_sand_page(self.state, role="commander")
        self.assertIn("32.12, 110.24", html)
        self.assertIn(ann_id, html)

    def test_blank_coordinates_are_refused(self):
        with self.assertRaises(PanelActionError):
            self.state.handle_sand_annotate(
                kind="fire_point", title="无坐标", points="", geo_text="",
            )


def _add_option(ws: ScenarioWorkspace, oid: str, title: str, risks=("北坡风大",)) -> Option:
    opt = Option(
        id=oid, title=title, summary=f"{title}概述",
        evidence_ids=(), claim_subjects=(), risks=tuple(risks),
        proposer_role=AdvisorRole.OPTION,
    )
    ws.add_option(opt)
    return opt


class PlanDecisionFlowTest(unittest.TestCase):
    """方案区决断链：风险门、原因门、重复否决守卫、审计入链。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _real_state(self.tmp)
        self.ws = self.state.workspace
        _add_option(self.ws, "opt-a", "北侧阻隔")

    def test_adopt_requires_explicit_risk_acceptance(self):
        with self.assertRaises(PanelActionError):
            self.state.handle_plan_decision("opt-a", "adopt")
        decision_id = self.state.handle_plan_decision(
            "opt-a", "adopt", reason="", accept_risks=True)
        self.assertTrue(decision_id)
        self.assertEqual(1, len(self.ws.decisions))
        decision = next(iter(self.ws.decisions.values()))
        self.assertEqual(("北坡风大",), decision.accepted_risks)

    def test_reject_and_defer_require_reason(self):
        _add_option(self.ws, "opt-b", "东侧合围", risks=())
        with self.assertRaises(PanelActionError):
            self.state.handle_plan_decision("opt-b", "reject")
        self.state.handle_plan_decision("opt-b", "reject", reason="资源不足")
        rejected = [d for d in self.ws.decisions.values()
                    if d.option_id == "opt-b"]
        self.assertEqual(1, len(rejected))
        # 已否决方案不可再次决断
        with self.assertRaises(PanelActionError):
            self.state.handle_plan_decision("opt-b", "defer", reason="再看看")

    def test_unknown_action_and_option_refused(self):
        with self.assertRaises(PanelActionError):
            self.state.handle_plan_decision("opt-x", "adopt", accept_risks=True)
        with self.assertRaises(PanelActionError):
            self.state.handle_plan_decision("opt-a", "execute", accept_risks=True)


class GoalAndGapFlowTest(unittest.TestCase):
    """目标草案→确认；信息缺口按现有资料继续→留痕。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _real_state(self.tmp)
        self.ws = self.state.workspace

    def test_goal_register_then_confirm(self):
        goal_id = self.state.handle_sand_goal("保护东线村庄，控制火线不过河")
        frame = self.ws.goal_frames[goal_id]
        self.assertIsNone(frame.confirmed_by)
        self.state.handle_confirm_goal(goal_id)
        self.assertIsNotNone(self.ws.goal_frames[goal_id].confirmed_by)

    def test_gap_defer_writes_audit_event_once(self):
        gap = InformationGap(
            id="gap-x1", subject="东线火线边界超时未更新",
            reason="影响方案风险判断", discovered_by="AnalysisAgent",
        )
        self.ws.record_information_gap(gap)
        self.state.handle_sand_gap_defer("gap-x1")
        kinds = [e.kind for e in self.ws.events]
        self.assertEqual(1, kinds.count("InformationGapDeferred"))
        # 已处理过不再重复留痕（缺口仍开放，但同一动作幂等拒绝）
        self.assertRaises(
            PanelActionError, self.state.handle_sand_gap_defer, "gap-x1")
        self.assertEqual(
            InformationGapStatus.OPEN, self.ws.information_gaps["gap-x1"].status)


class StaffDrawAndSimTest(unittest.TestCase):
    """含糊代绘请求要求核查位置；方案地图推演层单独验证。

    2026-08-26 口径：手动标注＝图上画符号＋旁侧信息窗填名称，
    语音/文字可提出标绘请求，但不能凭事件中心编造几何；前台不出现坐标输入框；
    方案「上图」点击后在地图上叠加推演对象判断（2026-08-28 改：一次只看一套）。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _real_state(self.tmp)
        self.ws = self.state.workspace
        self.state.handle_create_event(
            name="测试山火", report_no="T-01",
            discovered_at="2026-08-26T08:00:00+08:00", source="测试",
            location="北坡", lat="33.2", lon="107.6",
        )
        self.state.active_event_id = sorted(self.state.registry.events)[-1]

    def test_staff_text_draw_needs_evidenced_geometry(self):
        """Only an event centre and a vague place name cannot locate a firebreak."""
        version = self.ws.version
        events = len(self.ws.events)
        result = self.state.handle_voice_input(
            "在北坡画一条隔离带", role="commander")
        self.assertEqual("clarification", result["kind"])
        self.assertIn("图上标绘", result["message"])
        self.assertEqual({}, self.ws.annotations)
        self.assertEqual(version, self.ws.version)
        self.assertEqual(events, len(self.ws.events))
        legacy = self.state.handle_voice_input("画一个区域", role="commander")
        self.assertEqual("clarification", legacy["kind"])
        self.assertFalse(self.ws.annotations)
        self.assertFalse(self.ws.orders)

    def test_member_cannot_staff_draw(self):
        result = self.state.handle_voice_input("画一条隔离带", role="member")
        self.assertNotEqual("staff_draw", result.get("kind"))

    def test_front_has_no_coordinate_inputs_but_float_form(self):
        html = render_sand_page(self.state, role="commander")
        seg = html.split('id="cmd-anno-float"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn('name="points"', seg)   # 标绘信息窗不出现坐标输入
        self.assertNotIn('placeholder="地理坐标', html)
        self.assertIn('id="cmd-anno-float"', html)
        self.assertIn('type="hidden" name="geo"', html)

    def test_sim_layer_payload_and_button(self):
        opt = _add_option(self.ws, "opt-sim", "北侧阻隔")
        opt.sim_shapes = (
            {"geom": "line", "pts": ((33.22, 107.60), (33.23, 107.62)),
             "label": "阻隔线"},
        )
        html = render_sand_page(self.state, role="commander")
        self.assertIn('data-sim="opt-sim"', html)
        self.assertIn("上图", html)
        self.assertNotIn("查看推演", html)
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S,
        )
        self.assertIn('"sims"', raw.group(1))
        self.assertIn("opt-sim", raw.group(1))


if __name__ == "__main__":
    unittest.main()
