"""P6-T2 acceptance tests: SQLite persistence (doc 11 §6.4)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulator import ForestFireScenario


def _build_lived_in_scenario() -> ForestFireScenario:
    """A scenario that has been through decision, order, execution, and review."""
    scenario = ForestFireScenario()
    scenario.apply_e1_route_blocked()
    scenario.apply_e2_wind_and_fire_increase()
    scenario.apply_e3_conditions_verified()

    from panda_brain.chief_advisor import ChiefAdvisor
    from panda_brain.models import DecisionType

    chief = ChiefAdvisor(scenario.council)
    goal = chief.open_goal_frame(scenario.workspace, "Control the fireline safely.")
    topic = chief.orchestrate(scenario.workspace, "Which bounded option next?")
    chief.compose_decision_brief(scenario.workspace, topic, goal)
    decision = scenario.commander_decision(
        DecisionType.ADOPT,
        "prepare-approach-r2",
        "Synthetic adoption for persistence test.",
        "persistence",
    )
    order = scenario.issue_order(decision.id)
    report = scenario.simulate_e4(order.id)
    scenario.verify_goal()
    chief.handle_execution_report(scenario.workspace, order.id, report)
    chief.conduct_review(scenario.workspace)
    del goal
    return scenario


def _event_signature(workspace) -> list[tuple[int, str, str, str, str]]:
    return [
        (
            event.sequence,
            event.kind,
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
            event.prev_hash,
            event.hash,
        )
        for event in workspace.events
    ]


class P6PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "state" / "workspace.db"
        self.addCleanup(self._tmp.cleanup)

    def test_roundtrip_state_identical(self) -> None:
        scenario = _build_lived_in_scenario()
        save_workspace(scenario.workspace, self.db_path)
        restored = load_workspace(self.db_path)

        self.assertEqual(scenario.workspace.scenario_id, restored.scenario_id)
        self.assertEqual(_event_signature(scenario.workspace), _event_signature(restored))
        for attr in (
            "claims",
            "options",
            "orders",
            "approvals",
            "action_contracts",
            "policy_checks",
            "decisions",
            "evidence",
            "staff_plans",
            "staff_tasks",
            "memory_candidates",
            "reviews",
            "general_questions",
            "decision_briefs",
        ):
            original = getattr(scenario.workspace, attr)
            loaded = getattr(restored, attr)
            self.assertEqual(sorted(original), sorted(loaded), attr)
            for key in original:
                self.assertEqual(original[key], loaded[key], f"{attr}[{key}]")
        self.assertEqual(
            [a.id for a in scenario.workspace.assessments],
            [a.id for a in restored.assessments],
        )

    def test_audit_chain_intact_after_roundtrip(self) -> None:
        scenario = _build_lived_in_scenario()
        save_workspace(scenario.workspace, self.db_path)
        restored = load_workspace(self.db_path)
        self.assertEqual((), restored.verify_audit_chain())

    def test_tampered_event_row_breaks_chain_on_load(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e0()
        save_workspace(scenario.workspace, self.db_path)

        connection = sqlite3.connect(self.db_path)
        with connection:
            connection.execute(
                "UPDATE events SET payload = ? WHERE sequence = 2",
                (json.dumps({"scenario_id": "synthetic-southeast-fireline", "tampered": True}),),
            )
        connection.close()

        restored = load_workspace(self.db_path)
        broken = restored.verify_audit_chain()
        self.assertIn(2, broken)

    def test_version_preserved_and_authority_never_persists(self) -> None:
        scenario = _build_lived_in_scenario()
        save_workspace(scenario.workspace, self.db_path)

        auth = GeneralEntryAuth(secret="reload-secret")
        restored = load_workspace(self.db_path, general_auth=auth)
        self.assertEqual(scenario.workspace.version, restored.version)
        self.assertFalse(restored._general_entry_unlocked)
        with self.assertRaises(PermissionError):
            auth.authenticate("wrong")
        restored.unlock_general_entry(auth.authenticate("reload-secret"))
        self.assertTrue(restored._general_entry_unlocked)

    def test_save_is_full_snapshot_replace(self) -> None:
        scenario = _build_lived_in_scenario()
        save_workspace(scenario.workspace, self.db_path)
        save_workspace(scenario.workspace, self.db_path)  # overwrite, not append
        restored = load_workspace(self.db_path)
        self.assertEqual(len(scenario.workspace.events), len(restored.events))

        connection = sqlite3.connect(self.db_path)
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        connection.close()
        self.assertEqual(len(scenario.workspace.events), count)


if __name__ == "__main__":
    unittest.main()
