"""P9-T1 acceptance tests: campaign script data assets and strict loader."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from panda_brain.campaign_script import (
    CampaignConfigError,
    load_campaign_script,
)

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"
DEFAULT_SCRIPT = SCENARIO_DIR / "campaign_hanjiang_72h.yaml"


class P9CampaignScriptTests(unittest.TestCase):
    def test_default_campaign_loads_with_full_arc(self) -> None:
        script = load_campaign_script(DEFAULT_SCRIPT)
        self.assertEqual("hanjiang-72h", script.campaign_id)
        self.assertEqual(2, len(script.days))
        waves = script.waves
        self.assertGreaterEqual(len(waves), 5)
        hours = [wave.hours_elapsed for wave in waves]
        self.assertEqual(sorted(hours), hours)
        all_outcomes = {option_id for wave in waves for option_id in wave.outcomes}
        # The escalation arc must include a timeout with an approved retry path.
        retries = {wave.wave_id: wave.retries for wave in waves if wave.retries}
        self.assertTrue(retries, "campaign must exercise the anomaly-retry interlock")
        self.assertIn("prepare-approach-r2", all_outcomes)
        verified_reports = [
            report
            for wave in waves
            for report in wave.field_reports
            if report.status == "verified"
        ]
        self.assertTrue({"water:W1", "route:R2", "fire:trend"} <= {
            report.subject for report in verified_reports
        })

    def test_domain_must_match_playbook(self) -> None:
        with self.assertRaises(CampaignConfigError):
            load_campaign_script(DEFAULT_SCRIPT, expected_domain="urban_flood")

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(CampaignConfigError):
            load_campaign_script(SCENARIO_DIR / "no_such_campaign.yaml")

    def _write(self, payload: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "campaign.yaml"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return path

    @staticmethod
    def _minimal(**overrides) -> dict:
        base = {
            "schema_version": 1,
            "campaign_id": "t",
            "title": "t",
            "domain": "forest_fire",
            "authorized_sources": ["sensor-feed"],
            "days": [
                {
                    "day": 1,
                    "label": "d1",
                    "waves": [
                        {
                            "wave_id": "w1",
                            "hours_elapsed": 1,
                            "observations": [],
                            "field_reports": [],
                            "outcomes": {},
                        }
                    ],
                }
            ],
        }
        base.update(overrides)
        return base

    def test_structural_violations_rejected(self) -> None:
        cases = {
            "unknown top key": lambda p: p.update(extra=1),
            "empty days": lambda p: p.update(days=[]),
            "non-increasing day numbers": lambda p: p["days"].append(
                {"day": 1, "label": "dup", "waves": [{"wave_id": "w9", "hours_elapsed": 99}]}
            ),
            "duplicate wave id": lambda p: p["days"][0]["waves"].append(
                {"wave_id": "w1", "hours_elapsed": 5}
            ),
            "non-increasing hours": lambda p: p["days"][0]["waves"][0].update(hours_elapsed=0)
            or p["days"][0]["waves"].append({"wave_id": "w2", "hours_elapsed": 0}),
            "bad attempt status": lambda p: p["days"][0]["waves"][0].update(
                outcomes={"opt": {"outcome": "sort_of_worked"}}
            ),
            "untagged field subject": lambda p: p["days"][0]["waves"][0].update(
                field_reports=[{"subject": "water", "value": "x", "status": "verified",
                                "evidence_id": "e1", "summary": "s"}]
            ),
            "illegal report status": lambda p: p["days"][0]["waves"][0].update(
                field_reports=[{"subject": "water:W1", "value": "x", "status": "assumed",
                                "evidence_id": "e1", "summary": "s"}]
            ),
            "malformed observation": lambda p: p["days"][0]["waves"][0].update(
                observations=[{"site_id": "s", "metric": "m", "unit": "u", "value": "high", "observed_at": "t"}]
            ),
            "retry without answer": lambda p: p["days"][0]["waves"][0].update(
                outcomes={"opt": {"outcome": "timeout"}}
            )
            or None,
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                payload = self._minimal()
                if name == "retry without answer":
                    payload["days"][0]["waves"][0]["outcomes"] = {
                        "opt": {
                            "first": {"outcome": "timeout"},
                            "retry": {"outcome": "succeeded"},  # answer missing
                        }
                    }
                else:
                    mutate(payload)
                with self.assertRaises(CampaignConfigError):
                    load_campaign_script(self._write(payload))

    def test_retry_plan_parses_when_answer_present(self) -> None:
        payload = self._minimal()
        payload["days"][0]["waves"][0]["outcomes"] = {
            "prepare-approach-r2": {
                "first": {"outcome": "timeout", "observations": ["comms lost"]},
                "retry": {"answer": "resume", "outcome": "succeeded", "observations": ["done"]},
            }
        }
        script = load_campaign_script(self._write(payload))
        plan = script.waves[0].retries["prepare-approach-r2"]
        self.assertEqual("resume", plan.answer)
        self.assertEqual("succeeded", plan.world.outcome.value)


if __name__ == "__main__":
    unittest.main()
