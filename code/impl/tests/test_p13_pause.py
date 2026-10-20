"""P13-T1: pause_order enters the panel — gated, reasoned, audited."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import DecisionActor, DecisionType
from panda_brain.panel import PanelActionError, PanelState, esc, render_page
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


def _state_with_active_order() -> tuple[PanelState, str]:
    scenario = ForestFireScenario()
    chief = ChiefAdvisor(scenario.council)
    ws = scenario.workspace
    scenario.apply_e1_route_blocked()
    scenario.apply_e2_wind_and_fire_increase()
    scenario.apply_e3_conditions_verified()
    goal = chief.open_goal_frame(ws, "Control the fireline safely.")
    topic = chief.orchestrate(ws, "Which bounded option next?")
    chief.compose_decision_brief(ws, topic, goal)
    decision = scenario.commander_decision(
        DecisionType.ADOPT,
        "prepare-approach-r2",
        "Panel pause test adoption.",
        "scope",
    )
    order = scenario.issue_order(decision.id)
    state = PanelState(ws, db_path=None)
    state.sessions.add("test-token")
    return state, order.id


class P13PauseTests(unittest.TestCase):
    def test_pause_active_order_audits_reason_and_persists(self) -> None:
        state, order_id = _state_with_active_order()
        result = state.handle_pause(order_id, "天气窗口消失，暂停等待复核")
        self.assertEqual("paused", result["status"])

        paused_events = [
            e for e in state.workspace.events if e.kind == "OrderPaused"
        ]
        self.assertEqual(1, len(paused_events))
        self.assertEqual(order_id, paused_events[0].payload.get("order_id"))
        self.assertEqual(
            "天气窗口消失，暂停等待复核", paused_events[0].payload.get("reason")
        )
        self.assertEqual("paused", state.workspace.orders[order_id].status.value)

        # Snapshot write-back makes it visible after reload.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "p13.db"
            save_workspace(state.workspace, db_path)
            loaded = load_workspace(db_path)
        self.assertEqual("paused", loaded.orders[order_id].status.value)

    def test_pause_rejects_empty_reason_unknown_order_wrong_state(self) -> None:
        state, order_id = _state_with_active_order()
        with self.assertRaises(PanelActionError):
            state.handle_pause(order_id, "   ")
        with self.assertRaises(PanelActionError):
            state.handle_pause("order-nonexistent", "原因")
        state.handle_pause(order_id, "第一次暂停")
        with self.assertRaises(PanelActionError):
            state.handle_pause(order_id, "对已暂停命令再次暂停")

    def test_pause_locked_workspace_rejects_without_leaking_existence(self) -> None:
        auth = GeneralEntryAuth(secret="p13-secret")
        ws = ScenarioWorkspace("locked-drill", general_auth=auth)
        state = PanelState(ws, db_path=None)
        with self.assertRaises(PermissionError) as ctx:
            state.handle_pause("order-whatever", "原因")
        self.assertIn("认证", str(ctx.exception))

    def test_orders_view_shows_form_only_in_command_mode(self) -> None:
        state, order_id = _state_with_active_order()
        _, locked_body = render_page(state, "/orders", session_token=None)
        self.assertNotIn("<form", locked_body)

        _, armed_body = render_page(state, "/orders", session_token="test-token")
        self.assertIn(f"value='{esc(order_id)}'", armed_body)
        self.assertIn("暂停原因（必填）", armed_body)


if __name__ == "__main__":
    unittest.main()
