"""P18 第三批：时间轴推演（方案一）+ 目标圈附加图例（方案二并入）。

钉住：
- 推演是纯函数、确定性、只读——绝不写回正式态势；
- 前台必须带「规则推演·非实测」口径；结论词 来得及/勉强/来不及 由 火至-作业 富余判定；
- 地图载荷含 proj（steps/rings/targets）；方案卡出「推演结论」一行；
- 无火源时如实说「推演暂不可用」，不编数。
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from panda_brain import sand_sim
from panda_brain.fire_events import EventRegistry
from panda_brain.map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
)
from panda_brain.panel import PanelState
from panda_brain.auth import GeneralEntryAuth
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace


class PureMathTest(unittest.TestCase):
    def test_shift_distance_matches(self):
        p = (33.2, 107.6)
        q = sand_sim.shift_pt(p, 600.0)  # 5h × 120m/h
        self.assertAlmostEqual(sand_sim.haversine_m(p, q), 600.0, delta=3.0)

    def test_verdict_words_by_margin(self):
        fronts = [(33.20, 107.50)]
        # 远处短线：火至≈1113/120=9.3h，作业≈111/60=1.9h → 来得及
        far = [(33.21, 107.50), (33.211, 107.50)]
        v = sand_sim.shape_verdict(far, fronts)
        self.assertEqual("来得及", v["word"])
        # 近处长线：火至≈111/120=0.9h，作业≈890/60=14.8h → 来不及
        near = [(33.201, 107.50), (33.209, 107.50)]
        v2 = sand_sim.shape_verdict(near, fronts)
        self.assertEqual("来不及", v2["word"])

    def test_no_front_returns_none(self):
        self.assertIsNone(sand_sim.shape_verdict([(33.2, 107.5)], []))


def _mark(kind, title, pts, status=AnnotationStatus.PUBLISHED):
    return {
        "id": f"ann-{title}", "kind": kind, "label": f"{kind}·{title}",
        "geometry": "point" if len(pts) == 1 else "line",
        "published": status == AnnotationStatus.PUBLISHED,
        "candidate": False, "draft": status == AnnotationStatus.DRAFT,
        "withdrawn": False, "pts": [list(p) for p in pts], "note": "", "source": "人工录入",
    }


class ProjectionTest(unittest.TestCase):
    def test_steps_offset_fireline_downwind(self):
        marks = [_mark("fireline", "北火线", [(33.20, 107.50), (33.20, 107.52)])]
        steps = sand_sim.project_steps(marks)
        self.assertEqual(3, len(steps))
        self.assertEqual(240, steps[0]["r_m"])  # 2h×120
        d0 = sand_sim.haversine_m((33.20, 107.50), steps[0]["lines"][0][0])
        self.assertAlmostEqual(240.0, d0, delta=3.0)

    def test_rings_and_targets_inside_flag(self):
        marks = [
            _mark("fire_point", "火点", [(33.20, 107.50)]),
            _mark("water", "河湾水源", [(33.202, 107.502)]),   # ~300m 内
            _mark("headquarters", "前指", [(33.25, 107.55)]),  # 圈外
        ]
        rings, targets = sand_sim.rings_and_targets(marks)
        self.assertEqual(1, len(rings))
        by = {t["label"]: t["inside"] for t in targets}
        self.assertTrue(by["water·河湾水源"])
        self.assertFalse(by["headquarters·前指"])

    def test_enrich_sets_summary_and_verdicts(self):
        marks = [_mark("fireline", "北火线", [(33.20, 107.50)])]
        sims = [{
            "id": "opt-a", "letter": "A", "title": "北侧阻隔",
            "shapes": [{"geom": "line", "label": "阻隔线",
                        "pts": [[33.24, 107.50], [33.241, 107.50]]}],
        }]
        proj = sand_sim.enrich_sims(marks, sims)
        self.assertIn("规则推演", proj["caliber"])
        self.assertIn("阻隔线", sims[0]["summary"])
        self.assertIn(sims[0]["shapes"][0]["verdict"]["word"], sims[0]["summary"])

    def test_no_front_honest_unavailable(self):
        sims = [{"id": "opt-x", "letter": "A", "title": "t",
                 "shapes": [{"geom": "line", "label": "l", "pts": [[33.1, 107.4]]}]}]
        sand_sim.enrich_sims([], sims)
        self.assertIn("推演暂不可用", sims[0]["summary"])


# ---------------------------------------------------------------------------
# 页面接线


class _FakeState:
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


class PageWiringTest(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace("p18-sim")
        self.ws.record_annotation(MapAnnotation(
            id="ann-001", kind=AnnotationKind.FIRELINE, points=(),
            geo_points=((33.20, 107.50), (33.20, 107.52)),
            title="北火线", status=AnnotationStatus.PUBLISHED,
            source=AnnotationSource.MANUAL))
        from panda_brain.models import AdvisorRole, Option
        self.ws.add_option(Option(
            id="opt-a", title="北侧阻隔", summary="沿北岭开设隔离带",
            evidence_ids=(), claim_subjects=(), risks=(),
            proposer_role=AdvisorRole.OPTION,
            sim_shapes=({"geom": "line",
                         "pts": ((33.24, 107.49), (33.245, 107.52)),
                         "label": "北岭隔离带"},)))
        self.state = _FakeState(self.ws)

    def _payload(self, html):
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S)
        return raw.group(1)

    def test_page_has_projection_verdict_and_layers(self):
        html = render_sand_page(self.state, role="commander")
        self.assertIn("推演结论", html)
        self.assertIn("规则推演", html)
        self.assertIn("非实测", html)
        self.assertIn('id="cmd-layer-rings"', html)
        payload = self._payload(html)
        self.assertIn('"proj"', payload)
        self.assertIn('"steps"', payload)
        self.assertIn('"rings"', payload)
        self.assertIn("预测火线", html)            # 步长图例文案在位
        self.assertIn("起火后", html)
        self.assertNotIn("正在思考", html)      # 零 AI 自述红线不破

    def test_public_screen_payload_no_plans_projections(self):
        html = render_sand_page(self.state, role="public")
        self.assertNotIn('id="cmd-advisor"', html)
        payload = self._payload(html)
        self.assertNotIn("opt-a", payload)      # 方案标题/推演不进公共屏载荷
        self.assertNotIn('id="cmd-layer-rings"', html)


if __name__ == "__main__":
    unittest.main()
