"""Contract tests for the synthetic forest-fire command-staff scenario."""

from __future__ import annotations

import unittest

from panda_brain.forest_fire_roles import ForestFireCommandPostRole
from panda_brain.models import (
    AdvisorRole,
    AttemptStatus,
    Claim,
    ClaimStatus,
    DecisionActor,
    DecisionType,
    Evidence,
    FeedbackType,
    GENERAL_ENTRY_ID,
    InformationGapStatus,
    InputClass,
    Option,
    OrderStatus,
    RiskLevel,
    SourceTrustLevel,
)
from panda_brain.simulator import ForestFireScenario


class ForestFireScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.scenario.apply_e0()

    def _verified_conditions_and_decision(self) -> object:
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()
        return self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic commander adoption for test only.",
            "simulated approach preparation",
        )

    def test_e0_surfaces_information_gaps_and_disagreement(self) -> None:
        assessments = {assessment.agent: assessment for assessment in self.scenario.workspace.assessments}
        self.assertIn("route:R2", assessments["ResearchAgent"].unknowns)
        self.assertIn("water:W1", assessments["ResearchAgent"].unknowns)
        self.assertEqual(1, len(self.scenario.workspace.disagreements))
        self.assertIn("verify-conditions", self.scenario.workspace.options)
        self.assertGreaterEqual(len(self.scenario.workspace.intel_items), 7)
        self.assertIn("route:R2", self.scenario.workspace.replay()["open_information_gaps"])
        self.assertIn("water:W1", self.scenario.workspace.replay()["open_information_gaps"])
        briefing = self.scenario.workspace.commander_briefing()
        self.assertGreaterEqual(len(briefing["intelligence"]), 7)
        self.assertTrue(any(gap["subject"] == "route:R2" for gap in briefing["open_information_gaps"]))

    def test_forward_command_post_roles_are_registered_and_attributed(self) -> None:
        self.assertEqual(9, len(self.scenario.workspace.advisor_roles))
        self.assertFalse(self.scenario.workspace.advisor_roles[AdvisorRole.CHIEF_ADVISOR].can_issue_order)
        self.assertIsNone(self.scenario.role_bindings[ForestFireCommandPostRole.COMMANDER].advisor_role)
        self.assertEqual(
            AdvisorRole.ANALYSIS,
            self.scenario.role_bindings[ForestFireCommandPostRole.PROFESSIONAL_DEPUTY].advisor_role,
        )

        assessments = {assessment.agent: assessment for assessment in self.scenario.workspace.assessments}
        self.assertEqual(AdvisorRole.RESEARCH, assessments["ResearchAgent"].advisor_role)
        self.assertEqual(AdvisorRole.ANALYSIS, assessments["AnalysisAgent"].advisor_role)
        self.assertEqual(AdvisorRole.RED_TEAM, assessments["RedTeamAgent"].advisor_role)
        self.assertEqual(AdvisorRole.CHIEF_ADVISOR, assessments["DispatchChiefAgent"].advisor_role)
        self.assertEqual(AdvisorRole.FEASIBILITY, assessments["LogisticsSupportAgent"].advisor_role)
        self.assertEqual("information_reporting", assessments["InformationReportingAgent"].domain_role)
        self.assertEqual(AdvisorRole.OPTION, self.scenario.workspace.options["verify-conditions"].proposer_role)

    def test_non_commander_cannot_record_general_decision(self) -> None:
        with self.assertRaises(PermissionError):
            self.scenario.workspace.record_decision(
                DecisionType.ADOPT,
                "verify-conditions",
                "Staff proposals are not command decisions.",
                "simulated verification",
                actor_role=DecisionActor.STAFF,
                decided_by="staff-impersonation-attempt",
            )

    def test_e1_marks_old_route_option_as_affected(self) -> None:
        self.scenario.workspace.record_evidence(
            Evidence.create("claim-route-r1", "Legacy route observation reused as option evidence.", "synthetic-initial-observation")
        )
        legacy = self.scenario.workspace.add_option(
            Option(
                id="legacy-r1",
                title="Legacy route",
                summary="Synthetic stale option",
                evidence_ids=("claim-route-r1",),
                claim_subjects=("route:R1",),
            )
        )
        self.assertEqual("active", legacy.status)
        self.scenario.apply_e1_route_blocked()
        self.assertEqual("affected", self.scenario.workspace.options["legacy-r1"].status)

    def test_commander_fact_changes_next_research_output(self) -> None:
        self.scenario.workspace.record_evidence(
            Evidence.create(
                "commander-evidence-r2",
                "Commander confirms the alternative route is passable.",
                "commander-report",
                SourceTrustLevel.AUTHORIZED,
            )
        )
        self.scenario.commander_feedback(
            FeedbackType.ADD_FACT,
            "Commander confirms the alternative route is passable.",
            claim_updates=(
                Claim(
                    "commander-route-r2",
                    "route:R2",
                    "passable",
                    ClaimStatus.VERIFIED,
                    "commander-report",
                    1.0,
                    ("commander-evidence-r2",),
                ),
            ),
        )
        self.scenario.reconsider_after_feedback()

        research = [item for item in self.scenario.workspace.assessments if item.agent == "ResearchAgent"][-1]
        self.assertNotIn("route:R2", research.unknowns)
        self.assertEqual(ClaimStatus.VERIFIED, self.scenario.workspace.claims["route:R2"].status)
        route_gap = next(gap for gap in self.scenario.workspace.information_gaps.values() if gap.subject == "route:R2")
        self.assertEqual(InformationGapStatus.RESOLVED, route_gap.status)

    def test_commander_rejection_changes_next_proposal(self) -> None:
        proposal = next(reversed(tuple(self.scenario.workspace.proposals.values())))
        self.scenario.commander_feedback(
            FeedbackType.REJECT,
            "Commander rejects verification as the next option for this round.",
            proposal_id=proposal.id,
            option_id="verify-conditions",
        )
        self.scenario.reconsider_after_feedback()

        latest_proposal = next(reversed(tuple(self.scenario.workspace.proposals.values())))
        self.assertEqual("rejected", self.scenario.workspace.options["verify-conditions"].status)
        self.assertNotIn("verify-conditions", latest_proposal.option_ids)

    def test_commander_modification_changes_next_analysis_risk(self) -> None:
        proposal = next(reversed(tuple(self.scenario.workspace.proposals.values())))
        self.scenario.commander_feedback(
            FeedbackType.MODIFY,
            "Commander requires the revised option to preserve reserve capacity.",
            proposal_id=proposal.id,
        )
        self.scenario.reconsider_after_feedback()

        analysis = [item for item in self.scenario.workspace.assessments if item.agent == "AnalysisAgent"][-1]
        self.assertTrue(any("Commander requested a revised option" in risk for risk in analysis.risks))

    def test_e2_refreshes_risk_and_preserves_red_team_view(self) -> None:
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        red_team = [item for item in self.scenario.workspace.assessments if item.agent == "RedTeamAgent"][-1]
        self.assertTrue(any("风力与火势趋势" in risk for risk in red_team.risks))
        self.assertGreaterEqual(len(self.scenario.workspace.disagreements), 3)

    def test_an_order_requires_commander_decision(self) -> None:
        self.scenario.apply_e3_conditions_verified()
        with self.assertRaises(KeyError):
            self.scenario.issue_order("decision-missing")
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic commander adoption for test only.",
            "simulated approach preparation",
        )
        order = self.scenario.issue_order(decision.id)
        self.assertEqual(decision.id, order.decision_id)

    def test_rejected_recommendation_cannot_issue_order(self) -> None:
        decision = self.scenario.commander_decision(
            DecisionType.REJECT,
            "verify-conditions",
            "Synthetic commander rejection.",
            "no execution",
        )
        with self.assertRaises(PermissionError):
            self.scenario.issue_order(decision.id)

    def test_execution_success_does_not_complete_goal(self) -> None:
        decision = self._verified_conditions_and_decision()
        order = self.scenario.issue_order(decision.id)
        report = self.scenario.simulate_e4(order.id, AttemptStatus.SUCCEEDED)
        verification = self.scenario.verify_goal()
        self.assertEqual(AttemptStatus.SUCCEEDED, report.status)
        self.assertFalse(verification.passed)
        self.assertIn("fire trend is not independently verified as controlled", verification.unmet_conditions)

    def test_unknown_outcome_is_retained_for_reconciliation(self) -> None:
        self.scenario.apply_e3_conditions_verified()
        decision = self.scenario.commander_decision(
            DecisionType.MODIFY,
            "prepare-approach-r2",
            "Synthetic modification.",
            "simulated approach preparation",
        )
        order = self.scenario.issue_order(decision.id)
        report = self.scenario.simulate_e4(order.id, AttemptStatus.UNKNOWN_OUTCOME)
        self.assertEqual(AttemptStatus.UNKNOWN_OUTCOME, report.status)
        self.assertIn("requires reconciliation", report.observations[0])

    def test_replay_reconstructs_state_without_execution(self) -> None:
        self.scenario.apply_e1_route_blocked()
        replay = self.scenario.replay()
        self.assertGreater(replay["event_count"], 0)
        self.assertIn("ClaimChallenged", replay["event_kinds"])
        self.assertEqual("challenged", replay["claim_statuses"]["route:R1"])

    def test_order_lifecycle_follows_doc11_states(self) -> None:
        decision = self._verified_conditions_and_decision()
        workspace = self.scenario.workspace

        order = workspace.draft_order(
            decision.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
        )
        self.assertEqual(OrderStatus.DRAFT, order.status)
        # Rule 1: a draft is not a command.
        with self.assertRaises(PermissionError):
            workspace.record_execution(order.id, AttemptStatus.SUCCEEDED, ())

        workspace.submit_order_for_confirmation(order.id)
        self.assertEqual(OrderStatus.PENDING_CONFIRM, order.status)
        # Rule 2: drafting and confirming are separate acts; staff cannot confirm.
        with self.assertRaises(PermissionError):
            workspace.confirm_order(order.id, actor_role=DecisionActor.STAFF, confirmed_by="staff-entry")

        workspace.confirm_order(order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)
        self.assertEqual(OrderStatus.CONFIRMED, order.confirmed_by and order.status)
        self.assertEqual(GENERAL_ENTRY_ID, order.confirmed_by)

        workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=RiskLevel.L2,
            parameters=(("option_id", order.option_id),),
            idempotency_key=f"{order.id}:{order.option_id}",
        )
        workspace.activate_order(order.id)
        self.assertEqual(OrderStatus.ACTIVE, order.status)

        kinds = [event.kind for event in workspace.events]
        for expected in (
            "ExecutionOrderDrafted",
            "ActionContractDrafted",
            "PolicyCheckFinished",
            "OrderSubmittedForConfirmation",
            "OrderConfirmed",
            "OrderActivated",
        ):
            self.assertIn(expected, kinds)

    def test_confirming_a_new_order_supersedes_the_active_one(self) -> None:
        first_decision = self._verified_conditions_and_decision()
        first_order = self.scenario.issue_order(first_decision.id)
        self.assertEqual(OrderStatus.ACTIVE, first_order.status)

        second_decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic replacement adoption.",
            "simulated approach preparation",
        )
        workspace = self.scenario.workspace
        second_order = workspace.draft_order(
            second_decision.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
        )
        workspace.submit_order_for_confirmation(second_order.id)
        workspace.confirm_order(second_order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)

        # Rule 3: the old active command dies at confirmation time, not at activation.
        self.assertEqual(OrderStatus.SUPERSEDED, first_order.status)

        workspace.draft_action_contract(
            second_order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{second_order.option_id}",
            risk_level=RiskLevel.L2,
            parameters=(("option_id", second_order.option_id),),
            idempotency_key=f"{second_order.id}:{second_order.option_id}",
        )
        workspace.activate_order(second_order.id)
        self.assertEqual(OrderStatus.ACTIVE, second_order.status)
        superseded_events = [
            event for event in workspace.events if event.kind == "OrderSuperseded"
        ]
        self.assertTrue(any(event.payload.get("superseded_by") == second_order.id for event in superseded_events))
        with self.assertRaises(PermissionError):
            workspace.record_execution(first_order.id, AttemptStatus.SUCCEEDED, ())

    def test_terminal_orders_cannot_be_revived(self) -> None:
        decision = self._verified_conditions_and_decision()
        order = self.scenario.issue_order(decision.id)
        workspace = self.scenario.workspace

        workspace.cancel_order(order.id, "changed situation", actor_role=DecisionActor.COMMANDER)
        self.assertEqual(OrderStatus.CANCELLED, order.status)
        with self.assertRaises(ValueError):
            workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        with self.assertRaises(ValueError):
            workspace.cancel_order(order.id, "cancel again", actor_role=DecisionActor.COMMANDER)
        with self.assertRaises(ValueError):
            workspace.pause_order(order.id, "pause cancelled", actor_role=DecisionActor.COMMANDER)

    def test_only_commander_may_pause_resume_or_cancel(self) -> None:
        decision = self._verified_conditions_and_decision()
        order = self.scenario.issue_order(decision.id)
        workspace = self.scenario.workspace

        with self.assertRaises(PermissionError):
            workspace.pause_order(order.id, "staff pause attempt", actor_role=DecisionActor.STAFF)
        with self.assertRaises(PermissionError):
            workspace.resume_order(order.id, actor_role=DecisionActor.STAFF)
        with self.assertRaises(PermissionError):
            workspace.cancel_order(order.id, "staff cancel attempt", actor_role=DecisionActor.STAFF)
        self.assertEqual(OrderStatus.ACTIVE, order.status)

    def test_resume_rechecks_expiry_after_pause(self) -> None:
        decision = self._verified_conditions_and_decision()
        order = self.scenario.issue_order(decision.id)
        workspace = self.scenario.workspace

        order.valid_until_version = workspace.version + 1
        workspace.pause_order(order.id, "hold pending weather", actor_role=DecisionActor.COMMANDER)
        self.assertEqual(OrderStatus.PAUSED, order.status)

        workspace.append_event("Tick", detail="time passes")
        workspace.append_event("Tick", detail="more time passes")

        with self.assertRaises(ValueError):
            workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
        self.assertEqual(OrderStatus.EXPIRED, order.status)
        with self.assertRaises(PermissionError):
            workspace.record_execution(order.id, AttemptStatus.SUCCEEDED, ())

    def test_full_scenario_audit_chain_verifies(self) -> None:
        decision = self._verified_conditions_and_decision()
        order = self.scenario.issue_order(decision.id)
        self.scenario.simulate_e4(order.id, AttemptStatus.PARTIAL)
        self.scenario.verify_goal()
        self.assertEqual((), self.scenario.workspace.verify_audit_chain())
        self.assertGreater(self.scenario.workspace.replay()["event_count"], 40)

    def test_input_classes_gate_scenario_feedback(self) -> None:
        # A clarification cannot carry a rejection.
        with self.assertRaises(ValueError):
            self.scenario.commander_feedback(
                FeedbackType.REJECT,
                "Maybe drop verification?",
                input_class=InputClass.CLARIFICATION,
                option_id="verify-conditions",
            )
        self.assertEqual("active", self.scenario.workspace.options["verify-conditions"].status)

        # A formal correction does reject it.
        self.scenario.commander_feedback(
            FeedbackType.REJECT,
            "Commander rejects verification as the next option for this round.",
            option_id="verify-conditions",
        )
        self.assertEqual("rejected", self.scenario.workspace.options["verify-conditions"].status)

    def test_conclusions_trace_to_staff_tasks(self) -> None:
        workspace = self.scenario.workspace
        replay = workspace.replay()
        self.assertGreaterEqual(replay["staff_plan_count"], 1)
        self.assertGreaterEqual(replay["staff_task_count"], 8)

        plan = next(iter(workspace.staff_plans.values()))
        self.assertTrue(plan.rationale_enabled)
        self.assertEqual("ChiefAdvisor", plan.created_by)

        for assessment in workspace.assessments:
            self.assertIsNotNone(assessment.staff_task_id)
            self.assertIn(assessment.staff_task_id, workspace.staff_tasks)
            task = workspace.staff_tasks[assessment.staff_task_id]
            self.assertEqual(task.owner_agent, assessment.agent)
        proposal = next(iter(workspace.proposals.values()))
        self.assertIsNotNone(proposal.staff_task_id)
        self.assertEqual("option_design", workspace.staff_tasks[proposal.staff_task_id].work_type.value)


if __name__ == "__main__":
    unittest.main()
