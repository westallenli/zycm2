"""P8-T4 acceptance tests: read-only commander console (doc 11 §7 P8-T4)."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.console import build_console_text, main
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


def _lived_in_workspace():
    scenario = ForestFireScenario()
    scenario.apply_e1_route_blocked()
    scenario.apply_e2_wind_and_fire_increase()
    scenario.apply_e3_conditions_verified()
    chief = ChiefAdvisor(scenario.council)
    goal = chief.open_goal_frame(scenario.workspace, "Control the fireline safely.")
    topic = chief.orchestrate(scenario.workspace, "Which bounded option next?")
    chief.compose_decision_brief(scenario.workspace, topic, goal)
    return scenario.workspace


class P8ConsoleTests(unittest.TestCase):
    def test_renders_all_sections_for_lived_in_workspace(self) -> None:
        workspace = _lived_in_workspace()
        version_before = workspace.version

        text = build_console_text(workspace)

        for marker in (
            "指挥台",
            "指挥员入口",
            "审计链：完整",
            "-- 共享态势 --",
            "-- 最新决策简报 --",
            "推荐：",
            "-- 在途命令 --",
            "-- 待你回答 --",
        ):
            self.assertIn(marker, text)
        self.assertEqual(version_before, workspace.version)

    def test_locked_entry_shows_authentication_notice(self) -> None:
        from panda_brain.auth import GeneralEntryAuth

        workspace = ScenarioWorkspace("locked-console", general_auth=GeneralEntryAuth(secret="x"))
        text = build_console_text(workspace)
        self.assertIn("锁定", text)

    def test_main_smoke_on_demo_scenario_is_read_only(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([])
        self.assertEqual(0, code)
        self.assertIn("指挥台", buffer.getvalue())

    def test_db_backed_console_matches_saved_version(self) -> None:
        workspace = _lived_in_workspace()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            save_workspace(workspace, db_path)

            from panda_brain.auth import GeneralEntryAuth

            auth = GeneralEntryAuth(secret="console-secret")
            restored = load_workspace(db_path, general_auth=auth)
            restored.unlock_general_entry(auth.authenticate("console-secret"))

            text = build_console_text(restored)
            self.assertIn(f"v{workspace.version}", text)
            self.assertIn("已认证", text)


if __name__ == "__main__":
    unittest.main()
