"""P4 acceptance tests: controlled execution and independent verification (doc 11 §7 P4)."""

from __future__ import annotations

import unittest

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import (
    AdvisorRole,
    AttemptStatus,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    RiskLevel,
)
from panda_brain.simulator import ForestFireScenario


class P4ControlledExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.workspace = self.scenario.workspace

    def _reach_e3(self) -> None:
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()

    def _issue_l2_order(self):
        """Full chain (draft -> contract -> confirm -> policy -> active) for the L2 approach action."""
        self._reach_e3()
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption for execution testing.",
            "simulated approach preparation",
        )
        return self.scenario.issue_order(decision.id)

    def _build_confirmed_l3_order(self):
        """Draft an L3 order chain up to CONFIRMED, without activating."""
        self._reach_e3()
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption for the approval gate test.",
            "simulated approach preparation",
        )
        order = self.workspace.draft_order(
            decision.id, drafted_by="DispatchChiefAgent", drafter_role=AdvisorRole.CHIEF_ADVISOR
        )
        self.workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type="external:sector-warning-publish",
            risk_level=RiskLevel.L3,
            capability_requirements=("external-publish",),
            parameters=(("channel", "sector-broadcast"),),
            side_effect_boundary="external-publish",
            idempotency_key=f"{order.id}:warn",
        )
        self.workspace.submit_order_for_confirmation(order.id)
        self.workspace.confirm_order(order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)
        return order

    def test_activation_requires_action_contract(self) -> None:
        self.scenario.apply_e0()  # the review places the option on the workspace
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "verify-conditions",
            "Synthetic adoption without a contract.",
            "verification scope",
        )
        order = self.workspace.draft_order(
            decision.id, drafted_by="DispatchChiefAgent", drafter_role=AdvisorRole.CHIEF_ADVISOR
        )
        self.workspace.submit_order_for_confirmation(order.id)
        self.workspace.confirm_order(order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)

        with self.assertRaises(ValueError):
            self.workspace.activate_order(order.id)

        self.workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=RiskLevel.L2,
            parameters=(("option_id", order.option_id),),
            idempotency_key=f"{order.id}:{order.option_id}",
        )
        self.workspace.activate_order(order.id)
        self.assertEqual("active", order.status.value)
        kinds = [event.kind for event in self.workspace.events]
        self.assertIn("ActionContractDrafted", kinds)
        self.assertIn("PolicyCheckFinished", kinds)

    def test_policy_check_gates_activation(self) -> None:
        self._reach_e3()  # route:R1 is now challenged
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption for the policy gate test.",
            "simulated approach preparation",
        )
        order = self.workspace.draft_order(
            decision.id, drafted_by="DispatchChiefAgent", drafter_role=AdvisorRole.CHIEF_ADVISOR
        )
        self.workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type="simulated:legacy-move",
            risk_level=RiskLevel.L2,
            parameters=(("claim_subject", "route:R1"),),
            idempotency_key=f"{order.id}:legacy",
        )
        self.workspace.submit_order_for_confirmation(order.id)
        self.workspace.confirm_order(order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)

        check = self.workspace.run_policy_checks(order.id)
        self.assertFalse(check.passed)
        self.assertIn("no-challenged-basis", check.failed_rules)

        with self.assertRaises(ValueError) as caught:
            self.workspace.activate_order(order.id)
        self.assertIn("no-challenged-basis", str(caught.exception))
        self.assertEqual("confirmed", order.status.value)

    def test_l3_without_approval_cannot_reach_executor(self) -> None:
        order = self._build_confirmed_l3_order()

        # Gate 1: activation refuses an unapproved L3+ action.
        with self.assertRaises(PermissionError):
            self.workspace.activate_order(order.id)
        self.assertEqual("confirmed", order.status.value)

        # The commander approves with a short validity window (headroom of two
        # covers the approval's own audit event plus the policy-check event).
        self.workspace.record_approval(
            self.workspace._latest_contract(order.id).id,
            actor_role=DecisionActor.COMMANDER,
            approver=GENERAL_ENTRY_ID,
            valid_until_version=self.workspace.version + 2,
        )
        self.workspace.activate_order(order.id)
        self.assertEqual("active", order.status.value)

        # The validity window passes while the order stays active.
        self.workspace.append_event("Tick", detail="approval window closes")
        self.workspace.append_event("Tick", detail="and a little more")

        # Gate 2: the executor boundary re-checks the approval itself.
        with self.assertRaises(PermissionError) as caught:
            self.scenario.executor.execute(
                order.id,
                AttemptStatus.SUCCEEDED,
                ("attempted publish after the approval expired",),
                idempotency_key=f"{order.id}:post-expiry-attempt",
            )
        self.assertIn("not approved", str(caught.exception))

    def test_approval_binding_identity_and_expiry(self) -> None:
        order = self._build_confirmed_l3_order()
        contract_id = self.workspace._latest_contract(order.id).id

        with self.assertRaises(PermissionError):
            self.workspace.record_approval(
                contract_id,
                actor_role=DecisionActor.STAFF,
                approver="staff-impersonation-attempt",
            )
        with self.assertRaises(PermissionError):
            self.workspace.record_approval(
                contract_id,
                actor_role=DecisionActor.COMMANDER,
                approver="unauthenticated-entry",
            )

        approval = self.workspace.record_approval(
            contract_id,
            actor_role=DecisionActor.COMMANDER,
            approver=GENERAL_ENTRY_ID,
            valid_until_version=self.workspace.version + 2,
        )
        self.assertEqual(GENERAL_ENTRY_ID, approval.approver)
        self.assertEqual("external:sector-warning-publish", approval.action_type)

        self.workspace.activate_order(order.id)
        self.assertEqual("active", order.status.value)

        # Expired approvals cannot be reused: resume re-checks them (rule 5).
        self.workspace.pause_order(order.id, "hold before publish", actor_role=DecisionActor.COMMANDER)
        self.workspace.append_event("Tick", detail="validity passes")
        with self.assertRaises(PermissionError):
            self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)

        self.workspace.record_approval(
            self.workspace._latest_contract(order.id).id,
            actor_role=DecisionActor.COMMANDER,
            approver=GENERAL_ENTRY_ID,
        )
        self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        self.assertEqual("active", order.status.value)

    def test_seven_outcomes_covered(self) -> None:
        outcomes = (
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMEOUT,
            AttemptStatus.PARTIAL,
            AttemptStatus.PERMISSION_DENIED,
            AttemptStatus.UNKNOWN_OUTCOME,
        )
        seen = []
        for outcome in outcomes:
            scenario = ForestFireScenario()
            scenario.apply_e1_route_blocked()
            scenario.apply_e2_wind_and_fire_increase()
            scenario.apply_e3_conditions_verified()
            decision = scenario.commander_decision(
                DecisionType.ADOPT,
                "prepare-approach-r2",
                "Synthetic adoption.",
                "simulated approach preparation",
            )
            order = scenario.issue_order(decision.id)
            report = scenario.simulate_e4(order.id, outcome)
            seen.append(report.status)
            self.assertEqual(outcome, report.status)
            self.assertIsNotNone(report.action_contract_id)
        for outcome in outcomes:
            self.assertIn(outcome, seen)
        # The seventh ending (pause/resume) is covered by the anomaly tests below.

    def test_idempotent_replay_no_duplicate_side_effects(self) -> None:
        order = self._issue_l2_order()
        before = len([e for e in self.workspace.events if e.kind == "ExecutionSimulated"])

        first = self.scenario.executor.execute(
            order.id,
            AttemptStatus.SUCCEEDED,
            ("first run",),
            idempotency_key=f"{order.id}:replay-probe",
        )
        second = self.scenario.executor.execute(
            order.id,
            AttemptStatus.SUCCEEDED,
            ("second run must not duplicate",),
            idempotency_key=f"{order.id}:replay-probe",
        )

        self.assertIs(first, second)
        after = len([e for e in self.workspace.events if e.kind == "ExecutionSimulated"])
        self.assertEqual(before + 1, after)
        replays = [e for e in self.workspace.events if e.kind == "IdempotentReplay"]
        self.assertEqual(1, len(replays))

    def test_anomaly_pauses_and_raises_general_question(self) -> None:
        order = self._issue_l2_order()
        options_before = {option_id: option.status for option_id, option in self.workspace.options.items()}

        report = self.scenario.simulate_e4(order.id, AttemptStatus.TIMEOUT)
        result = self.chief.handle_execution_report(self.workspace, order.id, report)

        self.assertTrue(result["paused"])
        self.assertEqual("paused", order.status.value)
        question = self.workspace.general_questions[result["question_id"]]
        self.assertEqual("execution_anomaly", question.kind)
        self.assertEqual(order.id, question.subject)
        self.assertTrue(question.context)
        self.assertEqual(
            options_before,
            {option_id: option.status for option_id, option in self.workspace.options.items()},
        )

    def test_resume_after_anomaly_rechecks_command_and_approval(self) -> None:
        # L2 path: anomaly pause then resume works while everything is valid.
        order = self._issue_l2_order()
        report = self.scenario.simulate_e4(order.id, AttemptStatus.FAILED)
        result = self.chief.handle_execution_report(self.workspace, order.id, report)
        self.assertTrue(result["paused"])
        self.workspace.answer_general_question(
            result["question_id"],
            "Resume after re-checks.",
            actor_role=DecisionActor.COMMANDER,
            answered_by=GENERAL_ENTRY_ID,
        )
        self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        self.assertEqual("active", order.status.value)

        # L3 path: an expired approval blocks resume (rule 5 fully closed).
        self.workspace.pause_order(order.id, "swap to L3 warning", actor_role=DecisionActor.COMMANDER)
        self.workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type="external:sector-warning-publish",
            risk_level=RiskLevel.L3,
            capability_requirements=("external-publish",),
            parameters=(("channel", "sector-broadcast"),),
            side_effect_boundary="external-publish",
            idempotency_key=f"{order.id}:warn",
        )
        self.workspace.record_approval(
            self.workspace._latest_contract(order.id).id,
            actor_role=DecisionActor.COMMANDER,
            approver=GENERAL_ENTRY_ID,
            valid_until_version=self.workspace.version,
        )
        self.workspace.append_event("Tick", detail="approval validity passes")
        with self.assertRaises(PermissionError):
            self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        self.workspace.record_approval(
            self.workspace._latest_contract(order.id).id,
            actor_role=DecisionActor.COMMANDER,
            approver=GENERAL_ENTRY_ID,
        )
        self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        self.assertEqual("active", order.status.value)

    def test_full_p4_chain_deterministic(self) -> None:
        def run_once():
            scenario = ForestFireScenario()
            chief = ChiefAdvisor(scenario.council)
            scenario.apply_e1_route_blocked()
            scenario.apply_e2_wind_and_fire_increase()
            scenario.apply_e3_conditions_verified()
            decision = scenario.commander_decision(
                DecisionType.ADOPT,
                "prepare-approach-r2",
                "Deterministic adoption.",
                "simulated approach preparation",
            )
            order = scenario.issue_order(decision.id)
            report = scenario.simulate_e4(order.id, AttemptStatus.PARTIAL)
            anomaly = chief.handle_execution_report(scenario.workspace, order.id, report)
            verification = scenario.verify_goal()
            return (
                report.status,
                report.action_contract_id,
                anomaly["paused"],
                verification.passed,
                tuple(sorted(scenario.workspace.replay()["event_kinds"].items())),
            )

        self.assertEqual(run_once(), run_once())


if __name__ == "__main__":
    unittest.main()
