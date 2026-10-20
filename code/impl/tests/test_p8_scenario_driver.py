"""P8-T3 acceptance tests: observation-driven scenario (doc 11 §7 P8-T3)."""

from __future__ import annotations

import json
import unittest

from panda_brain.advisor_core import StaffCouncil
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.models import ClaimStatus
from panda_brain.scenario_driver import (
    ScenarioDriver,
    ThresholdConfigError,
    load_threshold_rules,
)
from panda_brain.workspace import ScenarioWorkspace

DEFAULT_THRESHOLDS = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "config"
    / "forest_fire_thresholds.yaml"
)


def _make_driver(workspace=None, *, with_chief: bool = False):
    workspace = workspace or ScenarioWorkspace("driver-test")
    store = ObservationStore()
    bridge = IntakeBridge(store, authorized_sources=("sensor-feed",))
    rules = load_threshold_rules(DEFAULT_THRESHOLDS)
    chief = ChiefAdvisor(StaffCouncil(FOREST_FIRE_PLAYBOOK)) if with_chief else None
    return ScenarioDriver(workspace, store, bridge, rules, chief), workspace, store


WATER_OK = {
    "site_id": "sx-hanjiang-001",
    "metric": "water_level_m",
    "unit": "m",
    "value": 3.4,
    "observed_at": "2026-08-22T01:00:00+00:00",
}
WATER_LOW = dict(WATER_OK, value=1.2, observed_at="2026-08-22T02:00:00+00:00")
WIND_HIGH = {
    "site_id": "sx-weather-01",
    "metric": "wind_speed_m_s",
    "unit": "m/s",
    "value": 12.0,
    "observed_at": "2026-08-22T01:30:00+00:00",
}
UNRELATED = {
    "site_id": "sx-weather-01",
    "metric": "temperature_c",
    "unit": "C",
    "value": 31.0,
    "observed_at": "2026-08-22T01:45:00+00:00",
}


class P8ScenarioDriverTests(unittest.TestCase):
    def test_empty_stream_is_bootstrap_required_and_changes_nothing(self) -> None:
        driver, workspace, _ = _make_driver()
        version_before = workspace.version

        result = driver.step()

        self.assertEqual("bootstrap_required", result["status"])
        self.assertEqual(version_before, workspace.version)

    def test_threshold_crossing_updates_domain_claim_to_observed(self) -> None:
        driver, workspace, store = _make_driver()
        driver.load_sequence([WATER_OK], source="sensor-feed")

        result = driver.step()

        self.assertIn("water:W1=confirmed", result["claims_updated"])
        claim = workspace.claims["water:W1"]
        self.assertEqual(ClaimStatus.OBSERVED, claim.status)
        self.assertEqual("confirmed", claim.value)

    def test_below_threshold_flips_back_and_unrelated_metric_never_touches_domain(self) -> None:
        driver, workspace, _ = _make_driver()
        driver.load_sequence([WATER_OK, WATER_LOW, UNRELATED], source="sensor-feed")

        driver.step()

        # Latest water reading (3.4→ then 1.2) is below threshold → unknown.
        self.assertEqual("unknown", workspace.claims["water:W1"].value)
        # Temperature observation bridged as a record but created no domain fact.
        self.assertNotIn("route:R2", workspace.claims)
        self.assertTrue(any(r.metric == "temperature_c" for r in _.observations()))

    def test_observations_never_produce_verified_claims(self) -> None:
        driver, workspace, _ = _make_driver()
        driver.load_sequence([WATER_OK, WIND_HIGH], source="sensor-feed")
        driver.step()

        for subject, claim in workspace.claims.items():
            self.assertNotEqual(
                ClaimStatus.VERIFIED, claim.status, f"{subject} must not auto-verify"
            )

    def test_same_sequence_twice_is_field_identical(self) -> None:
        def run_once() -> str:
            driver, workspace, _ = _make_driver()
            driver.load_sequence([WIND_HIGH, WATER_OK, UNRELATED, WATER_LOW], source="sensor-feed")
            driver.step(orchestrate_question="What is the situation?")
            return json.dumps(
                {
                    "events": [
                        [e.sequence, e.kind, e.hash] for e in workspace.events
                    ],
                    "claims": sorted(
                        (s, c.value, c.status.value) for s, c in workspace.claims.items()
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        self.assertEqual(run_once(), run_once())

    def test_bad_threshold_config_rejected(self) -> None:
        import tempfile
        from pathlib import Path

        import yaml

        bad = {"schema_version": 1, "thresholds": [{"subject": "water:W1", "op": "=="}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump(bad), encoding="utf-8")
            with self.assertRaises(ThresholdConfigError):
                load_threshold_rules(path)

    def test_script_mode_baseline_untouched(self) -> None:
        """The fixed synthetic script remains the regression baseline."""
        scenario = __import__("panda_brain.simulator", fromlist=["ForestFireScenario"]).ForestFireScenario()
        scenario.apply_e0()
        self.assertIn("fire:trend", scenario.workspace.claims)
        self.assertEqual(
            {"status": "bootstrap_required"},
            ObservationStore().status(),
        )


if __name__ == "__main__":
    unittest.main()
