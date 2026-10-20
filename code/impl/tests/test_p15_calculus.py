"""P15 T2 — fire calculus: handbook four-estimations, materials, route doctrine filter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.fire_calculus import (
    MaterialBaselineError,
    RouteCandidate,
    crews_needed,
    estimate_from_roster_headcount,
    estimate_suppression,
    filter_routes,
    load_material_baseline,
    material_requirements,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "material_baseline.yaml"


class HandbookReferenceExampleTest(unittest.TestCase):
    """手册原例逐字复算：500 米火线，蔓延 100 米/小时，分队灭火速度 100 米/小时。

    原文：1 个 30 人分队永远扑不灭；增加 1 个分队 5 小时扑灭；增加 5 个分队
    1 小时就能扑灭。
    """

    def test_one_crew_can_never_win(self):
        estimate = estimate_suppression(500, 100, crews=1, crew_rate_m_h=100)
        self.assertIsNone(estimate.hours_required)
        self.assertEqual(estimate.verdict, "unwinnable")
        self.assertEqual(estimate.net_closing_rate_m_h, 0)
        self.assertIn("无法扑灭", estimate.explanation)

    def test_two_crews_five_hours(self):
        estimate = estimate_suppression(500, 100, crews=2, crew_rate_m_h=100)
        self.assertEqual(estimate.verdict, "suppressible")
        self.assertAlmostEqual(estimate.hours_required, 5.0)
        self.assertIn("5.0 小时", estimate.explanation)

    def test_six_crews_one_hour(self):
        estimate = estimate_suppression(500, 100, crews=6, crew_rate_m_h=100)
        self.assertAlmostEqual(estimate.hours_required, 1.0)

    def test_crews_needed_matches_handbook(self):
        self.assertEqual(
            crews_needed(500, 100, target_hours=5, crew_rate_m_h=100), 2
        )
        self.assertEqual(
            crews_needed(500, 100, target_hours=1, crew_rate_m_h=100), 6
        )

    def test_invalid_inputs_rejected(self):
        for kwargs in (
            {"fireline_length_m": 0, "spread_rate_m_h": 10, "crews": 1},
            {"fireline_length_m": 100, "spread_rate_m_h": -1, "crews": 1},
            {"fireline_length_m": 100, "spread_rate_m_h": 10, "crews": 0},
        ):
            with self.assertRaises(ValueError):
                estimate_suppression(**kwargs)
        with self.assertRaises(ValueError):
            crews_needed(500, 100, target_hours=0)

    def test_roster_headcount_folds_into_standard_crews(self):
        # 手册标准分队 30 人：59 人折算 2 个分队 → 5 小时口径
        estimate = estimate_from_roster_headcount(59, 500, 100)
        self.assertEqual(estimate.crews, 2)
        self.assertAlmostEqual(estimate.hours_required, 5.0)
        empty = estimate_from_roster_headcount(0, 500, 100)
        self.assertEqual(empty.verdict, "unwinnable")
        self.assertIn("尚无一线作战人员", empty.explanation)


class MaterialRequirementTest(unittest.TestCase):
    def setUp(self):
        self.baseline = load_material_baseline(CONFIG)

    def test_baseline_loads_with_profiles(self):
        self.assertEqual(self.baseline.crew_standard_size, 30)
        for code in ("direct-attack", "pump-relay", "fireline-cut", "burn-out", "mop-up"):
            self.assertIn(code, self.baseline.profiles)

    def test_pump_relay_requirements_scale_deterministically(self):
        result = material_requirements(
            self.baseline, "pump-relay", crews=6, hours=4.5
        )
        items = {entry["item"]: entry["qty"] for entry in result["equipment"]}
        self.assertEqual(items["高压水泵"], 6)
        self.assertEqual(items["水带"], 24)
        consumables = {entry["item"]: entry["qty"] for entry in result["consumables"]}
        self.assertEqual(consumables["汽油"], 18)  # ceil(4 × 4.5)
        self.assertIn("水泵接力", result["profile_name"])

    def test_unknown_tactic_rejected(self):
        with self.assertRaises(KeyError):
            material_requirements(self.baseline, "no-such-tactic", crews=1)

    def test_zero_hours_drops_consumables_but_keeps_equipment(self):
        result = material_requirements(self.baseline, "mop-up", crews=2, hours=0)
        self.assertTrue(result["equipment"])
        self.assertEqual(result["consumables"], ())

    def test_malformed_baseline_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text(
                "schema_version: '1'\nname: x\nbasis: y\ncrew_standard_size: 30\nprofiles: {}\n",
                encoding="utf-8",
            )
            with self.assertRaises(MaterialBaselineError):
                load_material_baseline(bad)


class RouteDoctrineFilterTest(unittest.TestCase):
    def test_clean_route_passes_with_endorsement(self):
        clean = RouteCandidate(route_id="r-a", name="沿 R1 道路侧翼依托突入", notes="借助道路依托")
        result = filter_routes([clean])
        self.assertEqual(len(result["passed"]), 1)
        self.assertEqual(result["rejected"], ())
        self.assertIn("可行 1", result["summary"])

    def test_doctrine_violations_cited_verbatim(self):
        bad = RouteCandidate(
            route_id="r-b",
            name="山脊下切路线",
            downhill_from_ridge=True,
            narrow_gully=True,
        )
        headwind = RouteCandidate(route_id="r-c", name="迎火头强攻", headwind_at_firehead=True)
        steep = RouteCandidate(route_id="r-d", name="陡坡近道", slope_gt_25=True)
        result = filter_routes([bad, headwind, steep])
        self.assertEqual(len(result["passed"]), 0)
        by_id = {entry["route_id"]: entry for entry in result["rejected"]}
        self.assertIn("不从山上向下处突破", by_id["r-b"]["violated_clauses"][0])
        self.assertIn("窄沟狭谷", by_id["r-b"]["violated_clauses"][1])
        self.assertIn("不逆风迎火头", by_id["r-c"]["violated_clauses"][0])
        self.assertIn("25°", by_id["r-d"]["violated_clauses"][0])

    def test_summary_counts(self):
        result = filter_routes(
            [
                RouteCandidate(route_id="ok", name="可行线"),
                RouteCandidate(route_id="no", name="违令线", no_cover=True),
            ]
        )
        self.assertEqual(result["summary"], "候选 2 条：可行 1，违令否决 1。")


if __name__ == "__main__":
    unittest.main()
