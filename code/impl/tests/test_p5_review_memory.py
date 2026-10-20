"""P5 acceptance tests: after-action review and controlled memory (doc 11 §7 P5)."""

from __future__ import annotations

import unittest

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import (
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    MemoryCandidate,
    MemoryCandidateStatus,
)
from panda_brain.simulator import ForestFireScenario


class P5ReviewMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.workspace = self.scenario.workspace

    def _run_e3_and_brief(self):
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()
        goal = self.chief.open_goal_frame(self.workspace, "Control the fireline safely.")
        topic = self.chief.orchestrate(self.workspace, "Which bounded option next?")
        return goal, self.chief.compose_decision_brief(self.workspace, topic, goal)

    def test_full_timeline_replay_covers_decision_execution_verification(self) -> None:
        self._run_e3_and_brief()
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption for replay.",
            "simulated approach preparation",
        )
        order = self.scenario.issue_order(decision.id)
        self.scenario.simulate_e4(order.id)
        self.scenario.verify_goal()

        timeline = self.workspace.timeline()
        kinds = [entry["kind"] for entry in timeline]
        for expected in (
            "GeneralDecisionRecorded",
            "ExecutionOrderDrafted",
            "ActionContractDrafted",
            "PolicyCheckFinished",
            "OrderConfirmed",
            "OrderActivated",
            "ExecutionSimulated",
            "VerificationFinished",
        ):
            self.assertIn(expected, kinds)
        # Audit order holds: decision precedes activation precedes execution.
        self.assertLess(
            kinds.index("GeneralDecisionRecorded"), kinds.index("OrderActivated")
        )
        self.assertLess(
            kinds.index("OrderActivated"), kinds.index("ExecutionSimulated")
        )

    def test_review_composes_findings_and_proposes_candidates(self) -> None:
        self._run_e3_and_brief()
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption.",
            "scope",
        )
        order = self.scenario.issue_order(decision.id)
        report = self.scenario.simulate_e4(order.id)
        self.scenario.verify_goal()
        self.chief.handle_execution_report(self.workspace, order.id, report)

        review = self.chief.conduct_review(self.workspace)

        self.assertIn(review.id, self.workspace.reviews)
        self.assertGreater(review.entries_replayed, 0)
        self.assertTrue(review.findings)
        candidates = [
            candidate
            for candidate in self.workspace.memory_candidates.values()
            if candidate.source_review_id == review.id
        ]
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(MemoryCandidateStatus.PROPOSED, candidate.status)

    def test_memory_candidate_requires_general_confirmation(self) -> None:
        self._run_e3_and_brief()
        review = self.chief.conduct_review(self.workspace)
        candidate_id = next(iter(self.workspace.memory_candidates))

        with self.assertRaises(PermissionError):
            self.workspace.confirm_memory_candidate(
                candidate_id,
                actor_role=DecisionActor.STAFF,
                confirmed_by="staff-attempt",
            )

        # Unconfirmed experience is invisible to retrieval.
        self.assertEqual((), self.chief.retrieve_relevant_experiences(self.workspace))

        confirmed = self.chief.confirm_memory_candidate(self.workspace, candidate_id)
        self.assertEqual(MemoryCandidateStatus.CONFIRMED, confirmed.status)
        self.assertEqual(GENERAL_ENTRY_ID, confirmed.confirmed_by)
        retrieved = self.chief.retrieve_relevant_experiences(self.workspace)
        self.assertTrue(any(candidate.id == candidate_id for candidate in retrieved))

    def test_confirmed_experience_influences_next_brief(self) -> None:
        _, brief_before = self._run_e3_and_brief()
        self.assertEqual((), brief_before.relevant_experiences)

        review = self.chief.conduct_review(self.workspace)
        first_candidate = next(iter(self.workspace.memory_candidates.values()))
        self.chief.confirm_memory_candidate(self.workspace, first_candidate.id)

        topic = self.chief.orchestrate(self.workspace, "Which bounded option next?")
        brief_after = self.chief.compose_decision_brief(self.workspace, topic)

        self.assertTrue(brief_after.relevant_experiences)
        self.assertIn(first_candidate.content, brief_after.relevant_experiences)

    def test_experience_never_becomes_fact(self) -> None:
        self._run_e3_and_brief()
        review = self.chief.conduct_review(self.workspace)
        claims_before = dict(self.workspace.claims)
        claim_events_before = sum(
            1 for event in self.workspace.events if event.kind.startswith("Claim")
        )

        candidate_id = next(iter(self.workspace.memory_candidates))
        self.chief.confirm_memory_candidate(self.workspace, candidate_id)
        self.chief.retrieve_relevant_experiences(self.workspace)
        topic = self.chief.orchestrate(self.workspace, "Next step?")
        self.chief.compose_decision_brief(self.workspace, topic)

        self.assertEqual(claims_before, self.workspace.claims)
        claim_events_after = sum(
            1 for event in self.workspace.events if event.kind.startswith("Claim")
        )
        self.assertEqual(claim_events_before, claim_events_after)

    def test_experience_cannot_override_new_command(self) -> None:
        self._run_e3_and_brief()
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Synthetic adoption.",
            "scope",
        )
        order = self.scenario.issue_order(decision.id)
        events_before = {event.kind: None for event in self.workspace.events}
        baseline_kinds = [event.kind for event in self.workspace.events]

        review = self.chief.conduct_review(self.workspace)
        for candidate_id in tuple(self.workspace.memory_candidates):
            self.chief.confirm_memory_candidate(self.workspace, candidate_id)
        self.chief.retrieve_relevant_experiences(self.workspace)

        self.assertEqual("active", order.status.value)
        new_kinds = [event.kind for event in self.workspace.events]
        forbidden = {"OrderCancelled", "OrderSuperseded", "OrderExpired"}
        newly_introduced = [
            kind for kind in new_kinds if kind in forbidden and kind not in baseline_kinds
        ]
        self.assertEqual([], newly_introduced)
        del events_before

    def test_orchestration_quality_flags(self) -> None:
        # Solo path: no red-team pass was ever convened.
        self.chief.solo_review(self.workspace, "Surface question?")
        flags = self.chief.review_orchestration_quality(self.workspace)
        kinds = {feedback.kind for feedback in flags}
        self.assertIn("missing_red_team", kinds)

        # Premature escalation: an open non-anomaly question with zero executions.
        self.workspace.raise_general_question(
            GeneralQuestion(
                id="gq-premature",
                kind="value_tradeoff",
                subject="none",
                question="Which weighting do you prefer?",
            )
        )
        flags = self.chief.review_orchestration_quality(self.workspace)
        kinds = {feedback.kind for feedback in flags}
        self.assertIn("premature_escalation", kinds)

        # Overwork: an impossible budget makes even zero tasks exceed it.
        flags = self.chief.review_orchestration_quality(self.workspace, max_tasks_per_goal=-1)
        kinds = {feedback.kind for feedback in flags}
        self.assertIn("overwork", kinds)

        # Repeated research: three reviews re-listing the same unknowns.
        scenario = ForestFireScenario()
        chief = ChiefAdvisor(scenario.council)
        scenario.apply_e0()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        flags = chief.review_orchestration_quality(scenario.workspace)
        kinds = {feedback.kind for feedback in flags}
        self.assertIn("repeated_research", kinds)

    def test_p5_flow_deterministic(self) -> None:
        def run_once():
            scenario = ForestFireScenario()
            chief = ChiefAdvisor(scenario.council)
            scenario.apply_e1_route_blocked()
            scenario.apply_e2_wind_and_fire_increase()
            scenario.apply_e3_conditions_verified()
            goal = chief.open_goal_frame(scenario.workspace, "Control safely.")
            topic = chief.orchestrate(scenario.workspace, "Next?")
            chief.compose_decision_brief(scenario.workspace, topic, goal)
            review = chief.conduct_review(scenario.workspace)
            first_candidate = next(iter(scenario.workspace.memory_candidates.values()))
            chief.confirm_memory_candidate(scenario.workspace, first_candidate.id)
            flags = chief.review_orchestration_quality(scenario.workspace)
            return (
                review.findings,
                tuple(sorted(candidate.content for candidate in scenario.workspace.memory_candidates.values())),
                tuple(feedback.kind for feedback in flags),
                tuple(sorted(scenario.workspace.replay()["event_kinds"].items())),
            )

        self.assertEqual(run_once(), run_once())


    def _propose_confirmed(
        self,
        review_id: str,
        candidate_id: str,
        content: str,
        boundary: tuple[str, ...],
    ) -> None:
        candidate = self.workspace.propose_memory_candidate(
            MemoryCandidate(
                id=candidate_id,
                content=content,
                source_review_id=review_id,
                applicability_boundary=boundary,
                source_version=self.workspace.version,
            )
        )
        self.assertEqual(MemoryCandidateStatus.PROPOSED, candidate.status)
        self.chief.confirm_memory_candidate(self.workspace, candidate_id)

    def test_retrieval_ranks_by_situation_lexical_overlap(self) -> None:
        """Same boundary tier: ordering must follow lexical overlap and stay
        deterministic across repeated calls."""
        self._run_e3_and_brief()
        review = self.chief.conduct_review(self.workspace)
        self._propose_confirmed(
            review.id,
            "mem-test-weak",
            "quarantine ledger harbor zebra marmalade paperwork",
            ("forest_fire",),
        )
        self._propose_confirmed(
            review.id,
            "mem-test-strong",
            "Approach on route R2 depends on water W1 availability and wind"
            " near the fireline; re-check both before any movement.",
            ("forest_fire",),
        )
        first = self.chief.retrieve_relevant_experiences(self.workspace)
        ids = [candidate.id for candidate in first]
        self.assertIn("mem-test-strong", ids)
        self.assertIn("mem-test-weak", ids)
        self.assertLess(ids.index("mem-test-strong"), ids.index("mem-test-weak"))
        second = self.chief.retrieve_relevant_experiences(self.workspace)
        self.assertEqual(first, second)

    def test_boundary_disjoint_experience_never_surfaces(self) -> None:
        """Heavy lexical overlap cannot beat a disjoint applicability
        boundary: inclusion stays gated on the boundary, not the text."""
        self._run_e3_and_brief()
        review = self.chief.conduct_review(self.workspace)
        self._propose_confirmed(
            review.id,
            "mem-test-disjoint",
            "Approach route R2 water W1 wind fireline; maximum textual overlap.",
            ("marine_logistics",),
        )
        retrieved = self.chief.retrieve_relevant_experiences(self.workspace)
        self.assertNotIn(
            "mem-test-disjoint", [candidate.id for candidate in retrieved]
        )


if __name__ == "__main__":
    unittest.main()
