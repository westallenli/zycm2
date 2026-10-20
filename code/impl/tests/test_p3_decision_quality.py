"""P3 acceptance tests: option/decision quality (doc 11 §7 P3)."""

from __future__ import annotations

import unittest

from panda_brain.advisor_core import DomainPlaybook, StaffCouncil
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import (
    AdvisorRole,
    Claim,
    ClaimStatus,
    CommanderFeedback,
    DecisionActor,
    DecisionType,
    Evidence,
    FeedbackType,
    GENERAL_ENTRY_ID,
    InputClass,
    Option,
    Reversibility,
    SourceTrustLevel,
)
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


class P3DecisionQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.workspace = self.scenario.workspace

    def _run_to_e3_with_chief(self):
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()
        goal = self.chief.open_goal_frame(
            self.workspace,
            "Control the changing fireline safely without committing unverified resources.",
        )
        topic = self.chief.orchestrate(self.workspace, "Which bounded option should be prepared next?")
        return goal, self.chief.compose_decision_brief(self.workspace, topic, goal)

    def test_options_carry_structured_tradeoff_fields(self) -> None:
        _, brief = self._run_to_e3_with_chief()

        self.assertTrue(brief.option_matrix)
        for row in brief.option_matrix:
            self.assertNotEqual("unspecified", row.cost_estimate)
            self.assertNotEqual("unspecified", row.time_estimate)
            self.assertIn(row.reversibility, {r.value for r in Reversibility})
            self.assertIn(row.feasibility_status, {"feasible", "infeasible", "unknown"})
        prepare_row = next(row for row in brief.option_matrix if row.option_id == "prepare-approach-r2")
        self.assertEqual((), prepare_row.preconditions_unmet)
        self.assertIn("route:R2", prepare_row.preconditions_met)

    def test_no_go_triggered_option_flagged_and_not_recommended(self) -> None:
        self.scenario.apply_e1_route_blocked()  # route:R1 becomes challenged
        goal = self.chief.open_goal_frame(self.workspace, "Hold the line safely.")
        self.workspace.record_evidence(
            Evidence.create("evidence-legacy", "Legacy-route commitment plan.", "synthetic-plan", SourceTrustLevel.AUTHORIZED)
        )
        self.workspace.add_option(
            Option(
                id="commit-legacy-route",
                title="Commit via the original route",
                summary="Uses the now-challenged original route.",
                evidence_ids=("evidence-legacy",),
                claim_subjects=("route:R1",),
                proposer_role=AdvisorRole.OPTION,
                cost_estimate="low",
                time_estimate="short",
                reversibility=Reversibility.REVERSIBLE,
                no_go_conditions=("route:R1",),
            )
        )
        topic = self.chief.orchestrate(self.workspace, "How do we proceed?")
        brief = self.chief.compose_decision_brief(self.workspace, topic, goal)

        row = next(row for row in brief.option_matrix if row.option_id == "commit-legacy-route")
        self.assertTrue(row.no_go_triggered)
        self.assertEqual("infeasible", row.feasibility_status)
        self.assertFalse(brief.recommendation.startswith("推荐「commit-legacy-route」"))

    def test_risk_acceptance_gate_on_adopt(self) -> None:
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()
        decision_input = (
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption for the risk-acceptance gate test.",
            "simulated approach preparation",
        )

        with self.assertRaises(ValueError):
            self.workspace.record_decision(
                *decision_input,
                actor_role=DecisionActor.COMMANDER,
                decided_by=GENERAL_ENTRY_ID,
            )

        option = self.workspace.options["prepare-approach-r2"]
        self.assertTrue(option.risks)
        decision = self.workspace.record_decision(
            *decision_input,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            accepted_risks=(option.risks[0],),
        )
        self.assertEqual((option.risks[0],), decision.accepted_risks)
        recorded = [
            event
            for event in self.workspace.events
            if event.kind == "GeneralDecisionRecorded"
            and event.payload["decision"]["accepted_risks"] == (option.risks[0],)
        ]
        self.assertEqual(1, len(recorded))

        with self.assertRaises(ValueError):
            self.workspace.record_decision(
                *decision_input,
                actor_role=DecisionActor.COMMANDER,
                decided_by=GENERAL_ENTRY_ID,
                accepted_risks=("a risk this option never tracked",),
            )

    def test_insufficient_evidence_forces_conditional_recommendation(self) -> None:
        workspace = ScenarioWorkspace("thin-evidence")
        workspace.record_evidence(
            Evidence.create("ev-thin", "Unverified field report.", "field-report")
        )
        workspace.record_claim(
            Claim("c-thin", "bridge:status", "passable", ClaimStatus.HYPOTHESIS, "field-report", 0.4, ("ev-thin",))
        )
        workspace.add_option(
            Option(
                id="cross-bridge",
                title="Cross the bridge",
                summary="Only available course of action, resting on an unverified report.",
                evidence_ids=("ev-thin",),
                claim_subjects=("bridge:status",),
                proposer_role=AdvisorRole.OPTION,
                preconditions=("bridge:status",),
                cost_estimate="medium",
                time_estimate="short",
                reversibility=Reversibility.IRREVERSIBLE,
            )
        )
        thin_council = StaffCouncil(
            DomainPlaybook(
                name="thin",
                objective="Domain-neutral control objective.",
                specialists=(),
                build_intel=lambda current: (),
                build_options=lambda current, feedback_risks: [],
                build_disagreement=lambda current: None,
            )
        )
        thin_chief = ChiefAdvisor(thin_council)
        topic = thin_chief.orchestrate(workspace, "Should we cross?")
        brief = thin_chief.compose_decision_brief(workspace, topic)

        self.assertTrue(
            any(item.kind == "insufficient_evidence" for item in brief.question_items)
        )
        # No pseudo-certainty: an unverified premise yields at most a conditional
        # recommendation, never an unconditional one.
        self.assertTrue(brief.recommendation.startswith("有条件推荐「cross-bridge」"))
        self.assertIn("在此之前不得投入", brief.recommendation)
        self.assertFalse(brief.recommendation.startswith("推荐「cross-bridge」"))

    def test_value_tradeoff_triggers_general_question(self) -> None:
        _, brief = self._run_to_e3_with_chief()

        tradeoff_items = [item for item in brief.question_items if item.kind == "value_tradeoff"]
        self.assertEqual(1, len(tradeoff_items))
        self.assertIn("verify-conditions", tradeoff_items[0].text)
        self.assertIn("prepare-approach-r2", tradeoff_items[0].text)

    def test_key_disagreement_escalates_to_general(self) -> None:
        self.scenario.apply_e0()
        goal = self.chief.open_goal_frame(self.workspace, "Hold the objective safely.")
        topic = self.chief.orchestrate(self.workspace, "What should the staff clarify next?")
        brief = self.chief.compose_decision_brief(self.workspace, topic, goal)

        disagreement_items = [item for item in brief.question_items if item.kind == "key_disagreement"]
        self.assertGreaterEqual(len(disagreement_items), 1)
        self.assertIn("第二梯队戒备状态", disagreement_items[0].text)

    def test_commander_rejects_all_and_proposes_own_option(self) -> None:
        goal, brief = self._run_to_e3_with_chief()
        recommended_ids = [
            row.option_id
            for row in brief.option_matrix
            if not row.no_go_triggered and row.feasibility_status != "infeasible"
        ]
        self.assertTrue(recommended_ids)

        # The general rejects every staff recommendation, formally, one by one.
        for option_id in recommended_ids:
            self.scenario.commander_feedback(
                FeedbackType.REJECT,
                f"Commander rejects {option_id}.",
                option_id=option_id,
            )

        commander_option = Option(
            id="commander-hold-and-warn",
            title="Hold perimeter and issue early warning",
            summary="The general's own course: hold both teams on the perimeter and warn the sector.",
            evidence_ids=("evidence-e0-t1", "evidence-e0-t2"),
            claim_subjects=("team:T1", "team:T2", "fire:trend"),
            preconditions=(),
            cost_estimate="low",
            time_estimate="short",
            reversibility=Reversibility.REVERSIBLE,
        )
        feedback = CommanderFeedback(
            f"feedback-{self.workspace.version + 1:03d}",
            FeedbackType.PROPOSE,
            "Commander proposes holding the perimeter and issuing an early warning.",
            self.workspace.version,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.FORMAL_CORRECTION,
            topic_id=next(reversed(self.workspace.topics)),
        )
        result = self.chief.apply_general_input(self.workspace, feedback, proposed_option=commander_option)

        self.assertTrue(result["reorchestrated"])
        self.assertEqual("commander-hold-and-warn", result["commander_option_id"])
        registered = self.workspace.options["commander-hold-and-warn"]
        self.assertTrue(registered.proposed_by_commander)

        # Re-orchestrated material centres on the commander's own option.
        new_topic = next(reversed(tuple(self.workspace.topics.values())))
        new_brief = self.chief.compose_decision_brief(self.workspace, new_topic, goal)
        self.assertIn("commander-hold-and-warn", new_brief.option_ids)
        self.assertTrue(new_brief.recommendation.startswith("推荐「commander-hold-and-warn」"))
        for option_id in recommended_ids:
            if option_id == "commander-hold-and-warn":
                continue
            self.assertFalse(new_brief.recommendation.startswith(f"推荐「{option_id}」"))
        commander_row = next(
            row for row in new_brief.option_matrix if row.option_id == "commander-hold-and-warn"
        )
        self.assertEqual("feasible", commander_row.feasibility_status)
        proposal = next(reversed(tuple(self.workspace.proposals.values())))
        self.assertIn("commander-hold-and-warn", proposal.option_ids)

    def test_full_p3_flow_deterministic(self) -> None:
        def run_once():
            scenario = ForestFireScenario()
            chief = ChiefAdvisor(scenario.council)
            scenario.apply_e1_route_blocked()
            scenario.apply_e2_wind_and_fire_increase()
            scenario.apply_e3_conditions_verified()
            goal = chief.open_goal_frame(scenario.workspace, "Control the fireline safely.")
            topic = chief.orchestrate(scenario.workspace, "Which bounded option next?")
            brief = chief.compose_decision_brief(scenario.workspace, topic, goal)
            return (
                brief.recommendation,
                brief.option_matrix,
                brief.question_items,
                tuple(sorted(scenario.workspace.feasibility_verdicts)),
                scenario.workspace.replay()["event_kinds"],
            )

        self.assertEqual(run_once(), run_once())


if __name__ == "__main__":
    unittest.main()
