"""P7-T4 acceptance tests: targeted re-staffing after question answers."""

from __future__ import annotations

import unittest

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.models import (
    DecisionActor,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    StaffTaskStatus,
    StaffWorkType,
)
from panda_brain.simulator import ForestFireScenario
from panda_brain.workspace import ScenarioWorkspace


def _question(kind: str, qid: str) -> GeneralQuestion:
    return GeneralQuestion(
        id=qid,
        kind=kind,
        subject="test-subject",
        question=f"Commander, a {kind} needs your call.",
    )


class P7TargetedRestaffingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.workspace = self.scenario.workspace

    def _answer(self, qid: str) -> dict:
        return self.chief.handle_question_answer(
            self.workspace,
            qid,
            "Weigh speed over caution this window.",
            actor_role=DecisionActor.COMMANDER,
            answered_by=GENERAL_ENTRY_ID,
        )

    def test_value_tradeoff_reopens_option_design_only(self) -> None:
        workspace = self.workspace
        before_proposals = {pid: p.status for pid, p in workspace.proposals.items()}

        workspace.raise_general_question(_question("value_tradeoff", "gq-vt"))
        result = self._answer("gq-vt")

        self.assertTrue(result["plan_id"])
        task = workspace.staff_tasks[result["task_id"]]
        self.assertEqual(StaffWorkType.OPTION_DESIGN, task.work_type)
        self.assertEqual(StaffTaskStatus.COMPLETED, task.status)
        assessment = next(
            a for a in workspace.assessments if a.id == result["assessment_id"]
        )
        self.assertIn("gq-vt", assessment.summary)
        # No blanket invalidation: proposals keep their statuses.
        self.assertEqual(
            before_proposals,
            {pid: p.status for pid, p in workspace.proposals.items()},
        )
        applied = [e for e in workspace.events if e.kind == "QuestionAnswerApplied"]
        self.assertEqual(1, len(applied))
        self.assertTrue(applied[0].payload["rerouted"])

    def test_insufficient_evidence_opens_fact_gap_with_unknowns(self) -> None:
        workspace = self.workspace
        workspace.raise_general_question(_question("insufficient_evidence", "gq-ie"))
        result = self._answer("gq-ie")

        task = workspace.staff_tasks[result["task_id"]]
        self.assertEqual(StaffWorkType.FACT_GAP, task.work_type)
        assessment = next(
            a for a in workspace.assessments if a.id == result["assessment_id"]
        )
        self.assertIn(StaffWorkType.FACT_GAP.value, assessment.summary)
        self.assertTrue(assessment.unknowns)

    def test_key_disagreement_reruns_red_team_challenge(self) -> None:
        workspace = self.workspace
        workspace.raise_general_question(_question("key_disagreement", "gq-kd"))
        result = self._answer("gq-kd")

        task = workspace.staff_tasks[result["task_id"]]
        self.assertEqual(StaffWorkType.RISK_CHALLENGE, task.work_type)
        self.assertEqual("RedTeamAgent", task.owner_agent)
        assessment = next(
            a for a in workspace.assessments if a.id == result["assessment_id"]
        )
        self.assertTrue(assessment.risks)
        self.assertIn("Re-examine", assessment.risks[0])

    def test_unroutable_kind_records_answer_without_restaffing(self) -> None:
        workspace = self.workspace
        workspace.raise_general_question(_question("execution_anomaly", "gq-an"))
        version_before = workspace.version
        result = self._answer("gq-an")

        self.assertIsNone(result["plan_id"])
        applied = [e for e in workspace.events if e.kind == "QuestionAnswerApplied"]
        self.assertFalse(applied[0].payload["rerouted"])
        self.assertLess(workspace.version, version_before + 6)

    def test_answer_still_requires_authenticated_entry(self) -> None:
        from panda_brain.auth import GeneralEntryAuth

        raw = ScenarioWorkspace("locked-qa", general_auth=GeneralEntryAuth(secret="locked"))
        locked_chief = ChiefAdvisor(self.chief.council)
        raw.raise_general_question(_question("value_tradeoff", "gq-lock"))
        with self.assertRaises(PermissionError):
            locked_chief.handle_question_answer(
                raw,
                "gq-lock",
                "answer",
                actor_role=DecisionActor.COMMANDER,
                answered_by=GENERAL_ENTRY_ID,
            )


if __name__ == "__main__":
    unittest.main()
