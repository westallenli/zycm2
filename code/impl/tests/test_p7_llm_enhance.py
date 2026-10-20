"""P7-T3 acceptance tests: LLM text enhancement with structural enforcement."""

from __future__ import annotations

import unittest

from panda_brain.chief_advisor import ChiefAdvisor
from panda_brain.llm_enhance import (
    ENHANCEMENT_SYSTEM_PROMPT,
    EnhancementRejectedError,
    LLMTextEnhancer,
    build_enhancement_prompt,
    parse_enhanced_reply,
)
from panda_brain.providers import MockProvider, NullProvider, ProviderUnavailableError
from panda_brain.simulator import ForestFireScenario

RESEARCH_SUMMARY = (
    "已汇总当前情报，并标出尚未核验的路线与水源条件。"
)


def _scripted_reply(summary: str, risks: list[str], evidence: list[str]) -> str:
    risk_lines = "\n".join(f"- {risk}" for risk in risks) or "- (none)"
    return f"SUMMARY: {summary}\nRISKS:\n{risk_lines}\nEVIDENCE: {', '.join(evidence)}"


def _run_orchestration(scenario: ForestFireScenario):
    chief = ChiefAdvisor(scenario.council)
    goal = chief.open_goal_frame(scenario.workspace, "Control safely.")
    topic = chief.orchestrate(scenario.workspace, "Next?")
    return chief, topic, goal


class P7LLMEnhancementTests(unittest.TestCase):
    def test_system_prompt_carries_prohibitions(self) -> None:
        self.assertIn("Do NOT invent observations", ENHANCEMENT_SYSTEM_PROMPT)
        self.assertIn("Do NOT issue orders, alerts", ENHANCEMENT_SYSTEM_PROMPT)

    def test_parse_rejects_invented_evidence_reference(self) -> None:
        from panda_brain.models import Assessment

        original = Assessment(
            id="a1",
            agent="ResearchAgent",
            summary="s",
            evidence_ids=("e1", "e2"),
        )
        forged = _scripted_reply("rephrased", ["r1"], ["e1", "e2", "evidence-made-up"])
        with self.assertRaises(EnhancementRejectedError) as ctx:
            parse_enhanced_reply(forged, original)
        self.assertIn("Evidence set altered", str(ctx.exception))

    def test_mock_enhancement_rewrites_text_and_leaves_audit_trail(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e0()

        provider = MockProvider()
        original_complete = provider.complete

        def dynamic_complete(*, system: str, prompt: str) -> str:
            evidence_line = [ln for ln in prompt.splitlines() if ln.startswith("EVIDENCE: ")][0]
            risk_block = prompt.split("\nRISKS:\n", 1)[1].split("\nEVIDENCE: ", 1)[0]
            kept = [
                ln.strip()[2:]
                for ln in risk_block.splitlines()
                if ln.strip().startswith("-") and ln.strip()[2:] not in ("", "(none)")
            ]
            if prompt.startswith("SUMMARY:\n已汇总当前情报"):
                new_summary = "Current intelligence organized; route and water checks remain open."
                new_risks = kept
            elif prompt.startswith("SUMMARY:\n已对可能把"):
                new_summary = "Assumptions that could turn incomplete information into unsafe commitment were challenged."
                new_risks = [f"CHECK: {risk}" for risk in kept]
            else:
                return original_complete(system=system, prompt=prompt)
            return (
                "SUMMARY: " + new_summary
                + "\nRISKS:\n" + ("\n".join("- " + r for r in new_risks) or "- (none)")
                + "\nEVIDENCE: " + evidence_line[len("EVIDENCE: "):].strip()
            )

        provider.complete = dynamic_complete
        enhancer = LLMTextEnhancer(provider)
        scenario.council.core.enhancer = enhancer

        _run_orchestration(scenario)

        # apply_e0 convenes its own pre-enhancer round; assert on the orchestrated one.
        research = [a for a in scenario.workspace.assessments if a.agent == "ResearchAgent"][-1]
        self.assertEqual(
            "Current intelligence organized; route and water checks remain open.",
            research.summary,
        )
        self.assertEqual((), research.risks)  # no risks existed; none may be invented
        redteam = [a for a in scenario.workspace.assessments if a.agent == "RedTeamAgent"][-1]
        self.assertTrue(redteam.risks)
        self.assertTrue(all(risk.startswith("CHECK: ") for risk in redteam.risks))
        self.assertGreaterEqual(enhancer.enhanced_count, 2)
        enhanced_events = [e for e in scenario.workspace.events if e.kind == "AssessmentEnhanced"]
        self.assertTrue(enhanced_events)
        self.assertEqual("mock", enhanced_events[0].payload["provider"])
        # Structure untouched: evidence ids identical to the rule layer's output.
        self.assertEqual(
            tuple(claim.id for claim in scenario.workspace.claims.values()),
            research.evidence_ids,
        )

    def test_provider_failure_falls_back_to_rule_version_with_audit(self) -> None:
        scenario = ForestFireScenario()
        scenario.apply_e0()
        enhancer = LLMTextEnhancer(NullProvider())
        scenario.council.core.enhancer = enhancer

        _run_orchestration(scenario)

        research = [a for a in scenario.workspace.assessments if a.agent == "ResearchAgent"][-1]
        self.assertEqual(RESEARCH_SUMMARY, research.summary)
        self.assertEqual(enhancer.rejected_count, len(scenario.council.playbook.specialists))
        rejections = [e for e in scenario.workspace.events if e.kind == "LLMEnhancementRejected"]
        self.assertTrue(rejections)
        self.assertIn("No LLM provider configured", rejections[0].payload["reason"])

    def test_no_enhancer_output_byte_identical_to_baseline(self) -> None:
        plain = ForestFireScenario()
        plain.apply_e0()
        _run_orchestration(plain)

        baseline = ForestFireScenario()
        baseline.apply_e0()
        _run_orchestration(baseline)

        self.assertEqual(
            [a.summary for a in plain.workspace.assessments],
            [a.summary for a in baseline.workspace.assessments],
        )
        self.assertEqual(
            [a.risks for a in plain.workspace.assessments],
            [a.risks for a in baseline.workspace.assessments],
        )
        self.assertEqual(
            [e.kind for e in plain.workspace.events], [e.kind for e in baseline.workspace.events]
        )

    def test_prompt_builder_lists_evidence_for_repetition(self) -> None:
        from panda_brain.models import Assessment

        assessment = Assessment(id="a2", agent="X", summary="s", evidence_ids=("e9",))
        prompt = build_enhancement_prompt(assessment)
        self.assertIn("EVIDENCE: e9", prompt)


if __name__ == "__main__":
    unittest.main()
