"""P14-T2: panel-triggered re-staffing closes the propose→matrix loop."""

from __future__ import annotations

import unittest

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.panel import PanelState, render_page
from panda_brain.simulator import ForestFireScenario


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


class P14RestaffTests(unittest.TestCase):
    def test_restaff_produces_newer_brief_including_commander_option(self) -> None:
        state, _ = _briefed_state()
        ws = state.workspace
        briefs_before = sorted(b.source_version for b in ws.decision_briefs.values())

        proposed = state.handle_propose_option(
            title="夜间低强度渗透侦察",
            summary="利用夜间低温窗口沿 R2 侧翼推进侦察。",
        )
        result = state.handle_restaff(session_token="test-token")

        self.assertIn("brief_id", result)
        latest = max(ws.decision_briefs.values(), key=lambda b: b.source_version)
        self.assertGreater(
            latest.source_version, briefs_before[-1] if briefs_before else 0
        )
        matrix_ids = {row.option_id for row in latest.option_matrix}
        self.assertIn(proposed["option_id"], matrix_ids)

    def test_restaff_without_session_raises(self) -> None:
        state, _ = _briefed_state()
        try:
            state.handle_restaff(session_token=None)
        except PermissionError as exc:
            self.assertIn("会话", str(exc))
        else:
            self.fail("expected PermissionError")

    def test_restaff_custom_question_lands_in_topic_and_brief(self) -> None:
        state, _ = _briefed_state()
        result = state.handle_restaff(
            "评估夜间侦察窗口。", session_token="test-token"
        )
        ws = state.workspace
        topic = ws.topics[result["topic_id"]]
        self.assertEqual("评估夜间侦察窗口。", topic.question)

    def test_brief_view_shows_restaff_button_only_in_command_mode(self) -> None:
        state, _ = _briefed_state()
        _, locked = render_page(state, "/brief", session_token=None)
        self.assertNotIn("/restaff", locked)

        _, armed = render_page(state, "/brief", session_token="test-token")
        self.assertIn("action='/restaff'", armed)


if __name__ == "__main__":
    unittest.main()
