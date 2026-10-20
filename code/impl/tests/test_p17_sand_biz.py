"""P17-P3 验收：沙盘指挥页业务化收口（DEVELOPMENT_PLAN_PAGES_V1 §P3）。

- 右栏信息统一为 事实/判断/建议/指令 四类（doc 03 §4）；
- 图像分析与候选生成为后台任务结果，前台只显示待处理业务卡；
- 页面零 AI 自述；正式态势不被草稿/候选直接覆盖（既有钉保持）。
"""

from __future__ import annotations

import unittest

from panda_brain.fire_events import EventRegistry
from panda_brain.map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
)
from panda_brain.panel import PanelState
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


def _add_candidate(ws: ScenarioWorkspace, ann_id: str, title: str) -> None:
    ws.record_annotation(
        MapAnnotation(
            id=ann_id,
            kind=AnnotationKind.FIRE_POINT,
            points=((880.0, 320.0),),
            title=title,
            status=AnnotationStatus.PENDING_REVIEW,
            certainty="suspected",
            note="演示数据：模拟无人机影像识别结果，非真实分析",
            source=AnnotationSource.AI_SUGGESTION,
        )
    )


class FourCategoryLayoutTest(unittest.TestCase):
    """右栏四类分区：事实/判断/建议/指令 各出现一次，层级正确。"""

    def setUp(self):
        self.ws = ScenarioWorkspace("p17-p3")
        self.state = _FakeState(self.ws)

    def render(self, role="commander"):
        return render_sand_page(self.state, role=role)

    def test_four_category_headers_present_once_each(self):
        html = self.render()
        for header in (
            "<h3>事实 · 观测与证据</h3>",
            "<h3>判断 · 分析与推断</h3>",
            "<h3>建议 · 待指挥员决断</h3>",
            "<h3>指令 · 正式命令</h3>",
        ):
            self.assertEqual(1, html.count(header), header)
        self.assertIn("事实/判断/建议/指令（规范 03·§4）", html)

    def test_judgment_block_shows_stage_and_data_time(self):
        html = self.render()
        self.assertIn("当前阶段判定：<b>火情响应</b>", html)   # 初始阶段
        self.assertIn("数据截至", html)
        self.assertNotIn("异常处置", html.split("判断 ·")[1].split("建议 ·")[0])

    def test_advice_block_lists_active_options_and_decisions(self):
        # 直接向工作区登记一个 active 备选方案（与 propose-option 同构）
        from panda_brain.models import Option

        self.ws.options["opt-001"] = Option(
            id="opt-001",
            title="东线隔离带开设",
            summary="派出二队在阴坡开设隔离带",
            evidence_ids=(),
            claim_subjects=("扑救二组",),
            risks=("风向突变",),
            no_go_conditions=("风力大于 6 级",),
        )
        html = self.render()
        self.assertIn("东线隔离带开设", html)
        self.assertIn("风险 1 项", html)
        self.assertIn("有禁用条件", html)
        self.assertIn("待指挥员决断", html)
        self.assertIn("备选方案 <b>1</b> 个", html)

    def test_order_block_honest_when_empty(self):
        html = self.render()
        self.assertIn("无命令。</li>", html)


class CandidateAsBusinessCardTest(unittest.TestCase):
    """候选＝后台任务送达的待处理业务卡；演示入口语义为「模拟送达」。"""

    def setUp(self):
        self.ws = ScenarioWorkspace("p17-p3-c")
        self.state = _FakeState(self.ws)
        _add_candidate(self.ws, "ann-901", "【演示】疑似火点 001")

    def test_commander_sees_pending_card_with_decisions(self):
        html = render_sand_page(self.state, role="commander")
        self.assertIn("<h4>待核验候选标注（后台分析任务送达）</h4>", html)
        self.assertIn("【待核验·待确认】【演示】疑似火点 001", html)
        self.assertIn("确认为正式态势", html)
        self.assertIn("模拟后台送达一条候选（演示数据）", html)
        self.assertNotIn("生成一次演示候选标注", html)
        self.assertNotIn("生成演示候选", html)

    def test_member_sees_card_without_actions(self):
        html = render_sand_page(self.state, role="member")
        self.assertIn("【待核验·待确认】", html)
        self.assertNotIn("/sand/candidate", html)
        self.assertNotIn("模拟后台送达一条候选", html)

    def test_public_screen_sees_neither_candidates_nor_demo_entry(self):
        html = render_sand_page(self.state, role="public")
        self.assertNotIn("待核验候选标注", html)
        self.assertNotIn("ann-901", html)
        self.assertNotIn("模拟后台送达一条候选", html)

    def test_imagery_card_declares_background_semantics(self):
        import panda_brain.imagery as _img

        class _EmptyLedger:
            def for_event(self, _event_id):
                return []

        self.state.imagery = _EmptyLedger()
        html = render_sand_page(self.state, role="commander")
        self.assertIn("图像分析与候选生成为后台任务结果", html)
        self.assertIn("不显示分析过程", html)
        self.assertIn("当前未接入真实影像分析，相关按钮均为显式标注的演示数据入口", html)


class NoAiSelfNarrationTest(unittest.TestCase):
    """三种角色视图均无 AI/参谋自述词汇。"""

    def setUp(self):
        self.ws = ScenarioWorkspace("p17-p3-n")
        _add_candidate(self.ws, "ann-902", "【演示】疑似火点 002")
        self.state = _FakeState(self.ws)

    def test_banned_words_absent_all_roles(self):
        banned = ("智脑", "参谋部正在", "参谋推荐", "AI 候选", "AI 草稿", "LLM")
        for role in ("commander", "member", "public"):
            with self.subTest(role=role):
                html = render_sand_page(self.state, role=role)
                for word in banned:
                    self.assertNotIn(word, html)

    def test_voice_pending_note_kept_without_runtime_status(self):
        html = render_sand_page(self.state, role="commander")
        self.assertIn("语音识别通道待接入", html)
        self.assertNotIn("正在分析", html)
        self.assertNotIn("正在研判", html)


if __name__ == "__main__":
    unittest.main()
