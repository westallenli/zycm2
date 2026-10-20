"""Domain-neutral contract tests for the reusable advisor council."""

from __future__ import annotations

import unittest

from panda_brain.advisor_core import AdvisorAgentSpec, AdvisorCouncil
from panda_brain.models import (
    AdvisoryTopic,
    AdvisorProposal,
    AdvisorRole,
    Assessment,
    Claim,
    ClaimStatus,
    CommanderFeedback,
    DecisionActor,
    DecisionType,
    Evidence,
    FeedbackType,
    GENERAL_ENTRY_ID,
    InformationGapStatus,
    InformationGap,
    InputClass,
    IntelItem,
    Option,
    SourceTrustLevel,
    StaffPlan,
    StaffTask,
    StaffWorkType,
)
from panda_brain.workspace import ScenarioWorkspace, WorkspaceConflictError


def commander_feedback(feedback_id: str, feedback_type: FeedbackType, message: str, source_version: int, **kwargs) -> CommanderFeedback:
    return CommanderFeedback(
        feedback_id,
        feedback_type,
        message,
        source_version,
        actor_role=DecisionActor.COMMANDER,
        decided_by=GENERAL_ENTRY_ID,
        input_class=InputClass.FORMAL_CORRECTION,
        **kwargs,
    )


class AdvisorCoreTests(unittest.TestCase):
    def test_generic_council_convenes_without_domain_assumptions(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_evidence(
            Evidence.create("evidence-target", "Synthetic target observation.", "synthetic-source")
        )
        claim = Claim(
            "claim-target",
            "target:status",
            "uncertain",
            ClaimStatus.OBSERVED,
            "synthetic-source",
            0.8,
            ("evidence-target",),
        )
        workspace.record_claim(claim)

        council = AdvisorCouncil(
            (
                AdvisorAgentSpec(
                    "research",
                    AdvisorRole.RESEARCH,
                    lambda current: Assessment(
                        "assessment-research",
                        "research",
                        "Collected the current target observation.",
                        ("evidence-target",),
                        unknowns=("target:status",),
                        advisor_role=AdvisorRole.RESEARCH,
                    ),
                ),
                AdvisorAgentSpec(
                    "red-team",
                    AdvisorRole.RED_TEAM,
                    lambda current: Assessment(
                        "assessment-red-team",
                        "red-team",
                        "Identified a possible stale assumption.",
                        ("evidence-target",),
                        risks=("The target status may have changed.",),
                        advisor_role=AdvisorRole.RED_TEAM,
                    ),
                ),
            )
        )

        assessments = council.convene(workspace)

        self.assertEqual(2, len(assessments))
        self.assertEqual(AdvisorRole.RESEARCH, assessments[0].advisor_role)
        self.assertEqual(AdvisorRole.RED_TEAM, assessments[1].advisor_role)
        self.assertEqual(2, len(workspace.assessments))

    def test_generic_council_rejects_role_mismatch(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        council = AdvisorCouncil(
            role_by_agent={"analysis": AdvisorRole.ANALYSIS}
        )

        with self.assertRaises(ValueError):
            council.record_assessments(
                workspace,
                (
                    Assessment(
                        "assessment-analysis",
                        "analysis",
                        "Mismatched role.",
                        (),
                        advisor_role=AdvisorRole.RESEARCH,
                    ),
                ),
            )

    def test_topic_proposal_feedback_and_decision_form_a_chain(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        council = AdvisorCouncil()
        topic = council.start_conference(
            workspace,
            AdvisoryTopic(
                "topic-001",
                "Reach the objective safely.",
                "Which option should be considered next?",
                constraints=("commander decides",),
            ),
        )
        option = workspace.add_option(
            Option(
                "option-001",
                "Verify the current condition",
                "Collect enough evidence before acting.",
                (),
                (),
                proposer_role=AdvisorRole.OPTION,
            )
        )
        proposal = council.submit_proposal(
            workspace,
            AdvisorProposal(
                "proposal-001",
                topic.id,
                AdvisorRole.OPTION,
                "Present one bounded option for commander review.",
                (option.id,),
                (),
            ),
        )
        workspace.record_evidence(
            Evidence.create("evidence-commander-fact", "Commander-supplied verified fact.", "commander-report", SourceTrustLevel.AUTHORIZED)
        )
        feedback = workspace.record_feedback(
            commander_feedback(
                "feedback-001",
                FeedbackType.MODIFY,
                "Keep the option, but require a fresh observation first.",
                workspace.version,
                topic_id=topic.id,
                proposal_id=proposal.id,
            )
        )
        decision = workspace.record_decision(
            DecisionType.MODIFY,
            option.id,
            feedback.message,
            "bounded simulated review",
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            proposal_id=proposal.id,
        )

        self.assertEqual(proposal.id, decision.proposal_id)
        self.assertEqual(GENERAL_ENTRY_ID, decision.decided_by)
        self.assertEqual(topic.id, proposal.topic_id)
        self.assertEqual(FeedbackType.MODIFY, feedback.feedback_type)
        self.assertEqual(1, workspace.replay()["proposal_count"])

    def test_decision_identity_is_structurally_required(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")

        with self.assertRaises(TypeError):
            workspace.record_decision(DecisionType.ADOPT, None, "no actor supplied", "scope")  # type: ignore[call-arg]
        with self.assertRaises(PermissionError):
            workspace.record_decision(
                DecisionType.ADOPT,
                None,
                "staff impersonation attempt",
                "scope",
                actor_role=DecisionActor.STAFF,
                decided_by="staff-impersonation-attempt",
            )
        with self.assertRaises(PermissionError):
            workspace.record_decision(
                DecisionType.ADOPT,
                None,
                "wrong entry id",
                "scope",
                actor_role=DecisionActor.COMMANDER,
                decided_by="unauthenticated-entry",
            )

        decision = workspace.record_decision(
            DecisionType.ADOPT,
            None,
            "authenticated commander decision",
            "scope",
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
        )
        self.assertEqual(DecisionActor.COMMANDER, decision.actor_role)
        self.assertEqual(GENERAL_ENTRY_ID, decision.decided_by)

    def test_commander_fact_feedback_updates_shared_claims(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        topic = AdvisoryTopic("topic-002", "Reach the objective", "What changed?")
        workspace.record_topic(topic)
        workspace.record_evidence(
            Evidence.create("commander-evidence", "Commander-reported verified status.", "commander-report", SourceTrustLevel.AUTHORIZED)
        )
        claim = Claim(
            "claim-new",
            "target:status",
            "ready",
            ClaimStatus.VERIFIED,
            "commander-report",
            1.0,
            ("commander-evidence",),
        )
        workspace.record_feedback(
            commander_feedback(
                "feedback-002",
                FeedbackType.ADD_FACT,
                "The commander supplied a verified status.",
                workspace.version,
                topic_id=topic.id,
                claim_updates=(claim,),
            )
        )

        self.assertEqual("ready", workspace.claims["target:status"].value)
        self.assertIn("CommanderFactReceived", workspace.replay()["event_kinds"])

    def test_intelligence_and_gaps_are_shared_workspace_objects(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        council = AdvisorCouncil()
        intel = council.record_intelligence(
            workspace,
            (
                IntelItem(
                    "intel-001",
                    "target:status",
                    "The current target state is uncertain.",
                    "synthetic-source",
                ),
            ),
        )
        workspace.record_information_gap(
            InformationGap(
                "gap-001",
                "target:status",
                "No verified status is available.",
                "research",
            )
        )

        self.assertEqual(intel[0].id, workspace.intel_items["intel-001"].id)
        self.assertEqual(("target:status",), workspace.replay()["open_information_gaps"])
        workspace.record_evidence(
            Evidence.create("evidence-target-verified", "Verified target status report.", "commander-report", SourceTrustLevel.AUTHORIZED)
        )
        workspace.record_claim(
            Claim(
                "claim-target-verified",
                "target:status",
                "ready",
                ClaimStatus.VERIFIED,
                "commander-report",
                1.0,
                ("evidence-target-verified",),
            )
        )
        self.assertEqual(
            InformationGapStatus.RESOLVED,
            next(iter(workspace.information_gaps.values())).status,
        )

    def test_claims_may_only_reference_recorded_evidence(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        with self.assertRaises(ValueError):
            workspace.record_claim(
                Claim(
                    "claim-ghost",
                    "subject:x",
                    "value",
                    ClaimStatus.OBSERVED,
                    "synthetic-source",
                    0.5,
                    ("evidence-never-recorded",),
                )
            )
        self.assertNotIn("subject:x", workspace.claims)

    def test_verified_claim_requires_authorized_evidence(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_evidence(
            Evidence.create("evidence-pending", "Unverified field report.", "field-report")
        )
        with self.assertRaises(ValueError):
            workspace.update_claim(
                "subject:y",
                value="confirmed",
                status=ClaimStatus.VERIFIED,
                source="field-report",
                confidence=0.9,
                evidence_ids=("evidence-pending",),
            )
        self.assertNotIn("subject:y", workspace.claims)

        workspace.record_evidence(
            Evidence.create("evidence-check", "Authorized verification result.", "verification-team", SourceTrustLevel.AUTHORIZED)
        )
        workspace.update_claim(
            "subject:y",
            value="confirmed",
            status=ClaimStatus.VERIFIED,
            source="verification-team",
            confidence=0.95,
            evidence_ids=("evidence-pending", "evidence-check"),
        )
        self.assertEqual(ClaimStatus.VERIFIED, workspace.claims["subject:y"].status)

    def test_evidence_records_are_immutable(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        original = Evidence.create("evidence-fixed", "Original content.", "synthetic-source")
        workspace.record_evidence(original)

        identical = Evidence.create("evidence-fixed", "Original content.", "synthetic-source")
        workspace.record_evidence(identical)
        self.assertEqual(1, len(workspace.evidence))

        with self.assertRaises(ValueError):
            workspace.record_evidence(
                Evidence.create("evidence-fixed", "Tampered content.", "synthetic-source")
            )
        self.assertEqual("Original content.", workspace.evidence["evidence-fixed"].content)

    def test_audit_chain_detects_tampering(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.append_event("SomethingHappened", detail="first")
        workspace.append_event("SomethingHappened", detail="second")
        self.assertEqual((), workspace.verify_audit_chain())

        workspace.events[1].payload["detail"] = "tampered"
        broken = workspace.verify_audit_chain()
        self.assertEqual((2,), broken)

    def test_staff_plan_dependency_order_enforced(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        plan = StaffPlan(
            id="staffplan-001",
            topic_id=None,
            created_by="ChiefAdvisor",
            question="Test question?",
            rationale_enabled="Minimal staffing for a contract test.",
            tasks=(
                StaffTask(
                    "task-a", "staffplan-001", "ResearchAgent", AdvisorRole.RESEARCH,
                    StaffWorkType.FACT_GAP, "What is known?", "Assessment",
                ),
                StaffTask(
                    "task-b", "staffplan-001", "AnalysisAgent", AdvisorRole.ANALYSIS,
                    StaffWorkType.INTERPRETATION, "What does it mean?", "Assessment",
                    dependencies=("task-a",),
                ),
            ),
        )
        workspace.record_staff_plan(plan)

        with self.assertRaises(ValueError):
            workspace.complete_staff_task("task-b")
        workspace.complete_staff_task("task-a")
        workspace.complete_staff_task("task-b")
        with self.assertRaises(ValueError):
            workspace.complete_staff_task("task-a")  # terminal tasks cannot complete twice

    def test_stale_revision_is_rejected(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_evidence(Evidence.create("ev-1", "First observation.", "synthetic-source"))
        stale_version = workspace.version
        workspace.append_event("Tick", detail="another writer advanced the state")

        with self.assertRaises(WorkspaceConflictError) as caught:
            workspace.update_claim(
                "subject:r",
                value="fresh",
                status=ClaimStatus.OBSERVED,
                source="synthetic-source",
                confidence=0.8,
                evidence_ids=("ev-1",),
                expected_version=stale_version,
            )
        self.assertEqual(stale_version, caught.exception.expected_version)
        self.assertEqual(stale_version + 1, caught.exception.current_version)
        self.assertNotIn("subject:r", workspace.claims)

        # Current-version submissions pass.
        workspace.update_claim(
            "subject:r",
            value="fresh",
            status=ClaimStatus.OBSERVED,
            source="synthetic-source",
            confidence=0.8,
            evidence_ids=("ev-1",),
            expected_version=workspace.version,
        )
        self.assertIn("subject:r", workspace.claims)

    def test_input_classes_gate_mutation(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_evidence(
            Evidence.create("ev-casual", "Commander remark evidence.", "commander-report", SourceTrustLevel.AUTHORIZED)
        )
        claim = Claim("c-casual", "subject:c", "x", ClaimStatus.VERIFIED, "commander-report", 1.0, ("ev-casual",))

        casual_fact = CommanderFeedback(
            "fb-c-1", FeedbackType.ADD_FACT, "By the way...", workspace.version,
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CASUAL_DISCUSSION, claim_updates=(claim,),
        )
        with self.assertRaises(ValueError):
            workspace.record_feedback(casual_fact)

        casual_decision = CommanderFeedback(
            "fb-c-2", FeedbackType.ADOPT, "Sounds fine I guess.", workspace.version,
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CASUAL_DISCUSSION,
        )
        with self.assertRaises(ValueError):
            workspace.record_feedback(casual_decision)

        clarification_reject = CommanderFeedback(
            "fb-c-3", FeedbackType.REJECT, "Maybe drop that option?", workspace.version,
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CLARIFICATION,
        )
        with self.assertRaises(ValueError):
            workspace.record_feedback(clarification_reject)

        # A pure remark without mutations is recordable as casual talk.
        casual_note = CommanderFeedback(
            "fb-c-4", FeedbackType.CLARIFY, "Noted, keep going.", workspace.version,
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
            input_class=InputClass.CASUAL_DISCUSSION,
        )
        workspace.record_feedback(casual_note)
        self.assertEqual(1, len(workspace.feedback))

    def test_untrusted_evidence_blocks_order_drafting(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_evidence(Evidence.create("ev-ok", "Authorized verification.", "verification-team", SourceTrustLevel.AUTHORIZED))
        workspace.record_evidence(Evidence.create("ev-bad", "Anonymous rumor.", "unknown-stranger", SourceTrustLevel.UNTRUSTED))
        workspace.record_claim(Claim("c-ok", "road:status", "open", ClaimStatus.VERIFIED, "verification-team", 0.95, ("ev-ok",)))
        workspace.record_claim(Claim("c-bad", "crowd:mood", "calm", ClaimStatus.HYPOTHESIS, "unknown-stranger", 0.3, ("ev-bad",)))

        clean_option = workspace.add_option(
            Option("opt-clean", "Proceed verified", "...", ("ev-ok",), ("road:status",), proposer_role=AdvisorRole.OPTION)
        )
        dirty_option = workspace.add_option(
            Option("opt-dirty", "Move on rumor", "...", ("ev-ok", "ev-bad"), ("road:status", "crowd:mood"), proposer_role=AdvisorRole.OPTION)
        )
        clean_decision = workspace.record_decision(
            DecisionType.ADOPT, "opt-clean", "verified basis", "scope",
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
        )
        dirty_decision = workspace.record_decision(
            DecisionType.ADOPT, "opt-dirty", "rumor-based", "scope",
            actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
        )

        with self.assertRaises(PermissionError):
            workspace.draft_order(dirty_decision.id, drafted_by="ChiefAdvisor", drafter_role=AdvisorRole.CHIEF_ADVISOR)

        order = workspace.draft_order(clean_decision.id, drafted_by="ChiefAdvisor", drafter_role=AdvisorRole.CHIEF_ADVISOR)
        self.assertEqual("draft", order.status.value)

        with self.assertRaises(PermissionError):
            workspace.draft_order(clean_decision.id, drafted_by="ResearchAgent", drafter_role=AdvisorRole.RESEARCH)

    def test_option_submission_requires_proposing_role(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        with self.assertRaises(PermissionError):
            workspace.add_option(
                Option("opt-r", "Research proposal", "...", (), (), proposer_role=AdvisorRole.RESEARCH)
            )
        option = workspace.add_option(
            Option("opt-o", "Option proposal", "...", (), (), proposer_role=AdvisorRole.OPTION)
        )
        self.assertIn("opt-o", workspace.options)

    def test_commander_briefing_exposes_collection_and_gaps(self) -> None:
        workspace = ScenarioWorkspace("generic-objective")
        workspace.record_intel(
            IntelItem("intel-briefing", "target:status", "Current state is uncertain.", "source")
        )
        workspace.record_information_gap(
            InformationGap("gap-briefing", "target:status", "Need verification.", "research")
        )

        briefing = workspace.commander_briefing()

        self.assertEqual("intel-briefing", briefing["intelligence"][0]["id"])
        self.assertEqual("target:status", briefing["open_information_gaps"][0]["subject"])
        self.assertTrue(any(event["kind"] == "IntelItemRecorded" for event in briefing["collection_events"]))


if __name__ == "__main__":
    unittest.main()
