"""P6-T4 acceptance tests: reviewed-config playbook (doc 11 §7 P6-T4)."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from panda_brain.advisor_core import subset_playbook
from panda_brain.forest_fire_playbook import (
    DEFAULT_CONFIG_PATH,
    FOREST_FIRE_PLAYBOOK,
    build_playbook,
)
from panda_brain.p2_comparison import MINIMAL_THREE_POSTS
from panda_brain.playbook_config import PlaybookConfigError, load_playbook_config


def _load_raw() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class P6PlaybookConfigTests(unittest.TestCase):
    def test_default_config_loads_into_equivalent_playbook(self) -> None:
        cfg = load_playbook_config(DEFAULT_CONFIG_PATH)
        self.assertEqual("forest_fire", cfg.name)
        self.assertEqual(
            [spec.agent_id for spec in FOREST_FIRE_PLAYBOOK.specialists],
            [entry["agent_id"] for entry in cfg.staffing],
        )
        self.assertEqual(
            {spec.agent_id: spec.dependencies for spec in FOREST_FIRE_PLAYBOOK.specialists},
            {entry["agent_id"]: tuple(entry["dependencies"]) for entry in cfg.staffing},
        )

    def test_missing_dependency_rejected_explicitly(self) -> None:
        raw = _load_raw()
        raw["staffing"][1]["dependencies"] = ["NonexistentAgent"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(PlaybookConfigError) as ctx:
                load_playbook_config(path)
            self.assertIn("unknown specialists", str(ctx.exception))

    def test_unknown_top_level_key_rejected(self) -> None:
        raw = _load_raw()
        raw["secret_geography"] = {"rivers": "made-up"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(PlaybookConfigError) as ctx:
                load_playbook_config(path)
            self.assertIn("unknown top-level keys", str(ctx.exception))

    def test_invalid_role_rejected(self) -> None:
        raw = _load_raw()
        raw["staffing"][0]["role"] = "supreme_commander"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "badrole.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(PlaybookConfigError):
                load_playbook_config(path)

    def test_yaml_edit_changes_staffing_and_materials_with_zero_code_changes(self) -> None:
        raw = _load_raw()
        # Remove one specialist and rewire its dependents; change an option cost.
        raw["staffing"] = [e for e in raw["staffing"] if e["agent_id"] != "FireActionAgent"]
        for entry in raw["options"]:
            if entry["id"] == "verify-conditions":
                entry["cost_estimate"] = "negligible"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edited.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            edited = build_playbook(load_playbook_config(path))

        agent_ids = [spec.agent_id for spec in edited.specialists]
        self.assertNotIn("FireActionAgent", agent_ids)
        self.assertIn("LogisticsSupportAgent", agent_ids)  # its dependency chain intact

        from panda_brain.simulator import ForestFireScenario

        scenario_ws = ForestFireScenario().workspace  # E0-seeded claims
        options = edited.build_options(scenario_ws, ())
        verify = next(option for option in options if option.id == "verify-conditions")
        self.assertEqual("negligible", verify.cost_estimate)

    def test_subset_playbook_still_works_on_loaded_playbook(self) -> None:
        subset = subset_playbook(FOREST_FIRE_PLAYBOOK, MINIMAL_THREE_POSTS)
        self.assertEqual(MINIMAL_THREE_POSTS, tuple(spec.agent_id for spec in subset.specialists))


if __name__ == "__main__":
    unittest.main()
