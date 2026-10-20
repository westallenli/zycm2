"""P8-T1 acceptance tests: compliance fixes found by the doc-11 audit."""

from __future__ import annotations

import unittest

from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import (
    AdvisorRole,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    RiskLevel,
)
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


class P8ComplianceFixTests(unittest.TestCase):
    def _l3_contract_on(self, scenario, option_id: str):
        workspace = scenario.workspace
        decision = scenario.commander_decision(
            DecisionType.ADOPT,
            option_id,
            "L3 compliance probe.",
            "compliance",
        )
        order = workspace.draft_order(
            decision.id, drafted_by="DispatchChiefAgent", drafter_role=AdvisorRole.CHIEF_ADVISOR
        )
        workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type="external:sector-warning-publish",
            risk_level=RiskLevel.L3,
            side_effect_boundary="external-publish",
        )
        workspace.submit_order_for_confirmation(order.id)
        workspace.confirm_order(
            order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID
        )
        return order

    def test_l3_policy_requires_verified_authorized_preconditions(self) -> None:
        """§3.4 item 2: unverified content never backs an L3+ action."""
        scenario = ForestFireScenario()
        scenario.apply_e0()  # verify-conditions exists; route/water NOT verified yet
        order = self._l3_contract_on(scenario, "verify-conditions")

        with self.assertRaises(ValueError) as ctx:
            scenario.workspace.activate_order(order.id)
        self.assertIn("verified-basis-for-l3", str(ctx.exception))

    def test_l3_policy_passes_on_verified_precondition_basis(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        scenario.apply_e3_conditions_verified()  # route:R2 / water:W1 verified+authorized
        order = self._l3_contract_on(scenario, "prepare-approach-r2")

        result = scenario.workspace.run_policy_checks(order.id)
        self.assertIn("verified-basis-for-l3", result.checked_rules)
        self.assertNotIn("verified-basis-for-l3", result.failed_rules)

    def test_general_question_carries_suggested_reply_deadline(self) -> None:
        """§5.5: questions carry a suggested deadline on the version clock."""
        workspace = ScenarioWorkspace("deadline-test")
        deadline_at = workspace.version + 10
        question = GeneralQuestion(
            id="gq-deadline",
            kind="value_tradeoff",
            subject="s",
            question="q",
            suggested_reply_by_version=deadline_at,
        )
        workspace.raise_general_question(question)
        stored = workspace.general_questions["gq-deadline"]
        self.assertEqual(deadline_at, stored.suggested_reply_by_version)

        # Old snapshots without the field still load (default None).
        with tempfile_roundtrip(workspace) as db_path:
            restored = load_workspace(db_path)
            self.assertIn("gq-deadline", restored.general_questions)


def tempfile_roundtrip(workspace):
    import tempfile
    from pathlib import Path

    context = tempfile.TemporaryDirectory()

    class _Ctx:
        def __enter__(self_inner):
            context.__enter__()
            db_path = Path(context.name) / "state.db"
            save_workspace(workspace, db_path)
            return db_path

        def __exit__(self_inner, *args):
            return context.__exit__(*args)

    return _Ctx()


class P8StaffPlanRationaleTests(unittest.TestCase):
    def test_plan_states_why_posts_were_left_out(self) -> None:
        """§5.1: staffing plans explain enabled AND disabled posts."""
        scenario = ForestFireScenario()
        chief = __import__("panda_brain.chief_advisor", fromlist=["ChiefAdvisor"]).ChiefAdvisor(
            scenario.council
        )
        topic = chief.orchestrate(scenario.workspace, "Rationale check?")
        plan = next(iter(scenario.workspace.staff_plans.values()))
        self.assertTrue(plan.rationale_enabled)
        for expected in ("verification", "execution"):
            self.assertIn(expected, plan.rationale_disabled, plan.rationale_disabled)
        del topic


if __name__ == "__main__":
    unittest.main()
