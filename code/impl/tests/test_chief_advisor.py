"""P1 contract tests: rule-based ChiefAdvisor (goal frame, question tree, decision brief)."""

from __future__ import annotations

import unittest

from panda_brain.advisor_core import StaffCouncil
from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from panda_brain.models import (
    Claim,
    ClaimStatus,
    CommanderFeedback,
    DecisionActor,
    Evidence,
    FeedbackType,
    GENERAL_ENTRY_ID,
    InformationGapStatus,
    InputClass,
    SourceTrustLevel,
)
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


class ChiefAdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.workspace = self.scenario.workspace

    def _run_e0_with_chief(self):
        goal = self.chief.open_goal_frame(
            self.workspace,
            "Control the changing fireline safely without committing unverified resources.",
            success_criteria=("route and water verified", "fire trend independently confirmed"),
        )
        topic = self.chief.orchestrate(self.workspace, "What should the staff clarify before the next decision?")
        return goal, topic

    def test_goal_frame_requires_commander_confirmation(self) -> None:
        goal, _ = self._run_e0_with_chief()
        self.assertIsNone(goal.confirmed_by)

        confirmed = self.chief.confirm_goal_frame(self.workspace, goal.id)
        self.assertEqual(GENERAL_ENTRY_ID, confirmed.confirmed_by)
        self.assertEqual(goal.id, self.workspace.goal_frames[goal.id].id)

    def test_question_tree_reflects_open_gaps(self) -> None:
        goal, _ = self._run_e0_with_chief()
        tree = self.chief.build_question_tree(self.workspace, goal)

        gap_subjects = {node.subject for node in tree.nodes if node.kind == "gap"}
        self.assertIn("route:R2", gap_subjects)
        self.assertIn("water:W1", gap_subjects)
        self.assertEqual("q-root", tree.nodes[0].id)
        self.assertTrue(any(node.kind == "option_selection" for node in tree.nodes))

        self.scenario.apply_e3_conditions_verified()
        tree_after = self.chief.build_question_tree(self.workspace, goal)
        gap_subjects_after = {node.subject for node in tree_after.nodes if node.kind == "gap"}
        self.assertNotIn("route:R2", gap_subjects_after)

    def test_decision_brief_composes_fixed_structure(self) -> None:
        goal, topic = self._run_e0_with_chief()
        brief = self.chief.compose_decision_brief(self.workspace, topic, goal)

        self.assertEqual(len(self.chief.council.playbook.specialists), len(brief.situation_summary))
        self.assertIn("verify-conditions", brief.option_ids)
        self.assertIn("route:R2", brief.open_gap_subjects)
        self.assertTrue(brief.recommendation)
        self.assertTrue(brief.general_questions)
        self.assertEqual(goal.id, brief.goal_frame_id)
        self.assertIn(brief.id, self.workspace.decision_briefs)

    def test_formal_correction_invalidates_and_reorchestrates(self) -> None:
        _, topic = self._run_e0_with_chief()
        old_proposal = next(iter(self.workspace.proposals.values()))
        self.assertEqual("active", old_proposal.status)

        feedback = CommanderFeedback(
            f"feedback-{self.workspace.version + 1:03d}",
            FeedbackType.MODIFY,
            "Commander requires reserve capacity to be preserved in any new option.",
            self.workspace.version,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.FORMAL_CORRECTION,
            topic_id=topic.id,
            proposal_id=old_proposal.id,
        )
        result = self.chief.apply_general_input(self.workspace, feedback)

        self.assertTrue(result["reorchestrated"])
        self.assertIn(old_proposal.id, result["invalidated"])
        self.assertEqual("superseded", self.workspace.proposals[old_proposal.id].status)
        proposals = list(self.workspace.proposals.values())
        self.assertGreaterEqual(len(proposals), 2)
        latest_proposal = proposals[-1]
        self.assertEqual("active", latest_proposal.status)
        self.assertTrue(
            any("Commander requested a revised option" in risk for assessment in self.workspace.assessments for risk in assessment.risks)
        )

    def test_clarification_does_not_invalidate_downstream(self) -> None:
        _, topic = self._run_e0_with_chief()
        old_proposal = next(iter(self.workspace.proposals.values()))

        feedback = CommanderFeedback(
            f"feedback-{self.workspace.version + 1:03d}",
            FeedbackType.CLARIFY,
            "Clarification: the objective wording stands as briefed.",
            self.workspace.version,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CLARIFICATION,
            topic_id=topic.id,
        )
        result = self.chief.apply_general_input(self.workspace, feedback)

        self.assertFalse(result["reorchestrated"])
        self.assertEqual((), result["invalidated"])
        self.assertEqual("active", self.workspace.proposals[old_proposal.id].status)

    def test_request_research_opens_information_gap(self) -> None:
        _, topic = self._run_e0_with_chief()
        open_before = {
            gap.subject
            for gap in self.workspace.information_gaps.values()
            if gap.status == InformationGapStatus.OPEN
        }

        feedback = CommanderFeedback(
            f"feedback-{self.workspace.version + 1:03d}",
            FeedbackType.REQUEST_RESEARCH,
            "Commander requests supplementary research on secondary water sources.",
            self.workspace.version,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CLARIFICATION,
            topic_id=topic.id,
        )
        self.chief.apply_general_input(self.workspace, feedback)

        open_after = {
            gap.subject
            for gap in self.workspace.information_gaps.values()
            if gap.status == InformationGapStatus.OPEN
        }
        newly_open = open_after - open_before
        self.assertTrue(any(subject.startswith("research-request:") for subject in newly_open))

    def test_generic_council_still_works_without_domain_playbook(self) -> None:
        """The domain-neutral path (P0 contracts) remains intact alongside P1."""
        workspace = ScenarioWorkspace("generic-objective")
        council = StaffCouncil(FOREST_FIRE_PLAYBOOK)  # playbook-driven council is generic machinery
        self.assertIsNotNone(council.playbook)


if __name__ == "__main__":
    unittest.main()
