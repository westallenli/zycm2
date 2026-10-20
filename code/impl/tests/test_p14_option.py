"""P14-T1: commander-proposed options enter the panel — gated, attributed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import GENERAL_ENTRY_ID
from panda_brain.panel import PanelActionError, PanelState, render_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


def _briefed_state() -> tuple[PanelState, ChiefAdvisor]:
    scenario = ForestFireScenario()
    chief = ChiefAdvisor(scenario.council)
    ws = scenario.workspace
    scenario.apply_e1_route_blocked()
    scenario.apply_e2_wind_and_fire_increase()
    scenario.apply_e3_conditions_verified()
    goal = chief.open_goal_frame(ws, "Control the fireline safely.")
    topic = chief.orchestrate(ws, "Which bounded option next?")
    chief.compose_decision_brief(ws, topic, goal)
    state = PanelState(ws, db_path=None)
    state.sessions.add("test-token")
    return state, chief


class P14CommanderOptionTests(unittest.TestCase):
    def test_propose_option_audits_commander_attribution(self) -> None:
        state, _ = _briefed_state()
        result = state.handle_propose_option(
            title="夜间低强度渗透侦察",
            summary="利用夜间低温窗口沿 R2 侧翼推进侦察，避开正午风力峰值。",
            claim_subjects="route:R2, weather:wind",
            risks="夜间能见度不足",
            no_go_conditions="风速再次超过 10 m/s",
            preconditions="weather:wind 为 low",
            reversibility="partially_reversible",
        )
        option_id = result["option_id"]
        self.assertRegex(option_id, r"^opt-cmd-v\d{3,}$")

        events = [
            e for e in state.workspace.events
            if e.kind == "CommanderOptionProposed"
        ]
        self.assertEqual(1, len(events))
        payload_option = events[0].payload["option"]
        self.assertTrue(payload_option["proposed_by_commander"])
        self.assertIsNone(payload_option["proposer_role"])

        stored = state.workspace.options[option_id]
        self.assertTrue(stored.proposed_by_commander)
        self.assertIsNone(stored.proposer_role)
        self.assertEqual(("route:R2", "weather:wind"), stored.claim_subjects)

        # Snapshot write-back survives reload.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "p14.db"
            save_workspace(state.workspace, db_path)
            loaded = load_workspace(db_path)
        self.assertIn(option_id, loaded.options)

    def test_propose_rejects_empty_fields_bad_reversibility(self) -> None:
        state, _ = _briefed_state()
        with self.assertRaises(PanelActionError):
            state.handle_propose_option(title="  ", summary="概述")
        with self.assertRaises(PanelActionError):
            state.handle_propose_option(title="标题", summary="")
        with self.assertRaises(PanelActionError):
            state.handle_propose_option(
                title="标题", summary="概述", reversibility="sometimes"
            )

    def test_propose_locked_workspace_rejects_generically(self) -> None:
        auth = GeneralEntryAuth(secret="p14-secret")
        ws = ScenarioWorkspace("locked-p14", general_auth=auth)
        state = PanelState(ws, db_path=None)
        with self.assertRaises(PermissionError) as ctx:
            state.handle_propose_option(title="t", summary="s")
        self.assertIn("认证", str(ctx.exception))

    def test_brief_view_shows_form_only_in_command_mode(self) -> None:
        state, _ = _briefed_state()
        _, locked = render_page(state, "/brief", session_token=None)
        self.assertNotIn("指挥员自提方案", locked)

        _, armed = render_page(state, "/brief", session_token="test-token")
        self.assertIn("指挥员自提方案", armed)
        self.assertIn("action='/propose-option'", armed)


if __name__ == "__main__":
    unittest.main()
