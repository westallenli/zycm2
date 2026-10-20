"""P13-T2: confirm_goal_frame enters the panel — gated, idempotent, audited."""

from __future__ import annotations

import unittest

from panda_brain.auth import GeneralEntryAuth
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import GENERAL_ENTRY_ID
from panda_brain.panel import PanelActionError, PanelState, render_page
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


def _state_with_open_goal() -> tuple[PanelState, ChiefAdvisor, str]:
    scenario = ForestFireScenario()
    chief = ChiefAdvisor(scenario.council)
    ws = scenario.workspace
    frame = chief.open_goal_frame(ws, "Control the fireline safely.")
    state = PanelState(ws, db_path=None)
    state.sessions.add("test-token")
    return state, chief, frame.id


class P13GoalConfirmTests(unittest.TestCase):
    def test_confirm_goal_routes_through_general_entry(self) -> None:
        state, _, goal_id = _state_with_open_goal()
        result = state.handle_confirm_goal(goal_id)
        self.assertEqual(GENERAL_ENTRY_ID, result["confirmed_by"])

        confirmed_event = [
            e for e in state.workspace.events if e.kind == "GoalFrameConfirmed"
        ]
        self.assertEqual(1, len(confirmed_event))
        # The stored frame now carries the general-entry confirmation.
        self.assertIsNotNone(state.workspace.goal_frames[goal_id].confirmed_by)
        self.assertEqual(
            GENERAL_ENTRY_ID, state.workspace.goal_frames[goal_id].confirmed_by
        )

    def test_confirm_goal_rejects_unknown_and_repeat(self) -> None:
        state, _, goal_id = _state_with_open_goal()
        with self.assertRaises(PanelActionError):
            state.handle_confirm_goal("goal-nonexistent")
        state.handle_confirm_goal(goal_id)
        with self.assertRaises(PanelActionError):
            state.handle_confirm_goal(goal_id)

    def test_confirm_goal_locked_workspace_rejects(self) -> None:
        auth = GeneralEntryAuth(secret="p13-goal-secret")
        ws = ScenarioWorkspace("locked-goal", general_auth=auth)
        state = PanelState(ws, db_path=None)
        with self.assertRaises(PermissionError):
            state.handle_confirm_goal("goal-whatever")

    def test_brief_view_reflects_goal_state_conditionally(self) -> None:
        state, chief, goal_id = _state_with_open_goal()
        ws = state.workspace
        topic = chief.orchestrate(ws, "Which bounded option next?")
        chief.compose_decision_brief(ws, topic, ws.goal_frames[goal_id])

        _, armed = render_page(state, "/brief", session_token="test-token")
        self.assertIn("待指挥员确认", armed)
        self.assertIn("确认目标框", armed)

        _, locked = render_page(state, "/brief", session_token=None)
        self.assertNotIn("<form", locked)

        state.handle_confirm_goal(goal_id)
        _, done = render_page(state, "/brief", session_token="test-token")
        self.assertIn("已确认", done)
        self.assertNotIn("确认目标框", done)


if __name__ == "__main__":
    unittest.main()
