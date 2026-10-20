"""P8-T2 acceptance tests: deterministic situation scoring (doc 11 §6.6)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.simulator import ForestFireScenario
from panda_brain.situation_score import (
    ScoreConfigError,
    SituationScorer,
    default_scorer,
)

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "forest_fire_situation_score.yaml"


def _raw_config() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))


class P8SituationScoreTests(unittest.TestCase):
    def _scored_scenario(self):
        scenario = ForestFireScenario()
        scenario.apply_e0()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        return scenario

    def test_full_claims_score_deterministically(self) -> None:
        scenario = self._scored_scenario()
        scorer = default_scorer()

        first = scorer.score(scenario.workspace.claims)
        second = scorer.score(scenario.workspace.claims)

        self.assertEqual("available", first.level)
        self.assertEqual(first, second)
        # fire expanding 1.0*0.25 + wind high 1.0*0.20 + route blocked 1.0*0.20
        # + water unknown 1.0*0.20 + team ready 0.0*0.15 = 0.85
        self.assertAlmostEqual(0.85, first.total, places=6)
        self.assertEqual((), first.missing_components)

    def test_missing_component_makes_total_unavailable_without_defaults(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e0()  # route/water still unknown-value but present; remove one claim entirely
        del scenario.workspace.claims["weather:wind"]

        score = default_scorer().score(scenario.workspace.claims)

        self.assertEqual("unavailable", score.level)
        self.assertIsNone(score.total)
        self.assertIn("wind", score.missing_components)
        wind = next(c for c in score.components if c.name == "wind")
        self.assertEqual("unavailable", wind.state)
        self.assertIn("absent", wind.reason)

    def test_unmapped_value_is_explicitly_unavailable(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e0()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        from panda_brain.models import Claim, ClaimStatus, SourceTrustLevel

        weird = Claim(
            id="claim-weird",
            subject="fire:trend",
            value="spinning-up",
            status=ClaimStatus.OBSERVED,
            source="test",
            confidence=0.5,
        )
        object.__setattr__(scenario.workspace.claims, "__setitem__", None) if False else None
        claims = dict(scenario.workspace.claims)
        claims["fire:trend"] = weird

        score = default_scorer().score(claims)

        self.assertEqual("unavailable", score.level)
        fire = next(c for c in score.components if c.name == "fire_trend")
        self.assertEqual("unavailable", fire.state)
        self.assertIn("'spinning-up'", fire.reason)

    def test_yaml_weight_edit_changes_score_with_zero_code_changes(self) -> None:
        raw = _raw_config()
        raw["weights"]["personnel_readiness"] = 0.55
        raw["weights"]["fire_trend"] = 0.0 - 0.40 + 0.0  # keep sum: adjust below properly
        # Recompute a valid distribution: shift 0.4 from others to readiness.
        raw["weights"] = {
            "fire_trend": 0.05,
            "wind": 0.10,
            "route_access": 0.10,
            "water_access": 0.20,
            "personnel_readiness": 0.55,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edited.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            scorer = SituationScorer(path)
            scenario = self._scored_scenario()
            score = scorer.score(scenario.workspace.claims)
            # fire 1.0*0.05 + wind 1.0*0.10 + route 1.0*0.10 + water 1.0*0.20 + team 0*0.55
            self.assertAlmostEqual(0.45, score.total, places=6)

    def test_bad_configs_rejected(self) -> None:
        bad_sum = _raw_config()
        bad_sum["weights"]["fire_trend"] = 0.9  # sum now 1.65
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "badsum.yaml"
            path.write_text(yaml.safe_dump(bad_sum), encoding="utf-8")
            with self.assertRaises(ScoreConfigError):
                SituationScorer(path)

        unknown_key = _raw_config()
        unknown_key["secret_bias"] = 0.99
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "badkey.yaml"
            path.write_text(yaml.safe_dump(unknown_key), encoding="utf-8")
            with self.assertRaises(ScoreConfigError):
                SituationScorer(path)

    def test_brief_carries_score_annex_only_when_scorer_bound(self) -> None:
        plain_chief = ChiefAdvisor(ForestFireScenario().council)
        self.assertIsNone(plain_chief.situation_scorer)

        scenario = self._scored_scenario()
        chief = ChiefAdvisor(scenario.council, situation_scorer=default_scorer())
        goal = chief.open_goal_frame(scenario.workspace, "Control safely.")
        topic = chief.orchestrate(scenario.workspace, "Next?")
        brief = chief.compose_decision_brief(scenario.workspace, topic, goal)

        annexes = [line for line in brief.situation_summary if line.startswith("Situation score:")]
        self.assertEqual(1, len(annexes))
        self.assertTrue(annexes[0].startswith("Situation score: 0.85"))
        del plain_chief, goal


if __name__ == "__main__":
    unittest.main()
