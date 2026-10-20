"""LLM text-enhancement layer for staff assessments (doc 11 §7 P7-T3).

Plan A boundary: the rule layer produces ALL structure and facts — evidence
references, unknowns, roles, options. The LLM may only rephrase the wording of
the summary and risk lines. Enforcement is structural (ADR-11-05):

- The reply must repeat the exact evidence-id set it was given; any addition
  (an invented reference) or deletion is rejected and the rule version is kept;
- Every outcome — accepted or rejected — leaves an audit event naming the
  provider, so provenance is always traceable;
- With no provider bound, nothing changes at all (the no-key premise).

The system prompt states the prohibitions ("do not invent observations / do
not publish alerts"), but per ADR-11-05 prompts carry no enforcement duty.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from .models import Assessment
from .providers import ProviderUnavailableError
from .workspace import ScenarioWorkspace

ENHANCEMENT_SYSTEM_PROMPT = """You are a wording assistant inside an offline command-staff decision system.
STRICT PROHIBITIONS:
- Do NOT invent observations, facts, numbers, or evidence references.
- Do NOT issue orders, alerts, recommendations, or official statements.
You may ONLY rephrase the wording of the provided SUMMARY and RISKS lines,
keeping every fact, number, subject, and evidence reference exactly as given.
Reply in exactly this format:
SUMMARY: <one rephrased sentence>
RISKS:
- <rephrased risk or unchanged>
EVIDENCE: <repeat the provided evidence ids, comma-separated, unmodified>"""


class EnhancementRejectedError(ValueError):
    """LLM output violated the text-only boundary."""


def build_enhancement_prompt(assessment: Assessment) -> str:
    risk_lines = "\n".join(f"- {risk}" for risk in assessment.risks) or "- (none)"
    return (
        f"SUMMARY:\n{assessment.summary}\n"
        f"RISKS:\n{risk_lines}\n"
        f"EVIDENCE: {', '.join(assessment.evidence_ids)}\n"
        "Rephrase the summary and each risk line. Repeat EVIDENCE unchanged."
    )


def parse_enhanced_reply(raw: str, original: Assessment) -> tuple[str, tuple[str, ...]]:
    """Extract (summary, risks); reject any drift in the evidence set."""
    try:
        summary_section = raw.split("SUMMARY:", 1)[1].split("RISKS:", 1)[0].strip()
        body = raw.split("RISKS:", 1)[1]
        risks_section, evidence_section = body.split("EVIDENCE:", 1)
    except IndexError:
        raise EnhancementRejectedError("Reply does not follow the required format.") from None

    summary = " ".join(summary_section.split())
    if not summary:
        raise EnhancementRejectedError("Empty summary.")

    risks = tuple(
        line.strip().lstrip("-").strip()
        for line in risks_section.strip().splitlines()
        if line.strip().startswith("-") and line.strip().lstrip("-").strip() not in {"", "(none)"}
    )

    declared = {
        item.strip() for item in evidence_section.strip().split(",") if item.strip()
    }
    expected = set(original.evidence_ids)
    if declared != expected:
        raise EnhancementRejectedError(
            f"Evidence set altered: expected {sorted(expected)}, got {sorted(declared)}."
            " Invented references are forbidden."
        )
    if len(summary) > 4000 or any(len(risk) > 2000 for risk in risks):
        raise EnhancementRejectedError("Rephrased text exceeds length limits.")
    return summary, risks


class LLMTextEnhancer:
    """Callable enhancer suitable for ``AdvisorCouncil(..., enhancer=...)``."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.enhanced_count = 0
        self.rejected_count = 0

    def __call__(self, workspace: ScenarioWorkspace, assessment: Assessment) -> Assessment:
        try:
            raw = self.provider.complete(
                system=ENHANCEMENT_SYSTEM_PROMPT,
                prompt=build_enhancement_prompt(assessment),
            )
            summary, risks = parse_enhanced_reply(raw, assessment)
        except (ProviderUnavailableError, EnhancementRejectedError) as exc:
            self.rejected_count += 1
            workspace.append_event(
                "LLMEnhancementRejected",
                assessment_id=assessment.id,
                provider=getattr(self.provider, "name", "unknown"),
                reason=str(exc),
            )
            return assessment

        candidate = replace(assessment, summary=summary, risks=risks)
        if candidate == assessment:
            return assessment
        self.enhanced_count += 1
        workspace.append_event(
            "AssessmentEnhanced",
            assessment_id=assessment.id,
            provider=self.provider.name,
        )
        return candidate


#: Type alias for the council integration point.
Enhancer = Callable[[ScenarioWorkspace, Assessment], Assessment]
