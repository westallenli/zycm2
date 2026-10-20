"""P2 comparison acceptance: solo chief vs minimal three posts (doc 11 §7 P2).

Same goal, same offline materials (E0 facts + E1 road block), two staffing
arrangements. The staffed arrangement must detect at least one risk,
counterexample, or evidence gap the solo chief missed; the cost of both runs
is recorded. Repeated runs without net gain force a re-estimate of the
staffing arrangement before P3.
"""

from __future__ import annotations

from dataclasses import dataclass

from .advisor_core import StaffCouncil, subset_playbook
from .chief_advisor import ChiefAdvisor
from .forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from .models import InformationGapStatus
from .simulator import ForestFireScenario

MINIMAL_THREE_POSTS = ("ResearchAgent", "AnalysisAgent", "RedTeamAgent")

_OBJECTIVE = "Control the changing fireline safely without committing unverified resources."
_QUESTION_BEFORE = "What should the staff clarify before the next decision?"
_QUESTION_AFTER = "How does the blocked route change the next decision?"


@dataclass(frozen=True)
class StaffingTrial:
    mode: str
    event_count: int
    assessment_count: int
    staff_task_count: int
    proposal_count: int
    risks_detected: tuple[str, ...]
    open_gap_subjects: tuple[str, ...]

    @property
    def findings(self) -> frozenset[str]:
        return frozenset(
            {f"risk:{risk}" for risk in self.risks_detected}
            | {f"gap:{subject}" for subject in self.open_gap_subjects}
        )


@dataclass(frozen=True)
class ComparisonReport:
    solo: StaffingTrial
    staffed: StaffingTrial
    unique_staffed_findings: tuple[str, ...]

    @property
    def has_net_gain(self) -> bool:
        return len(self.unique_staffed_findings) > 0


def _run_solo_trial() -> StaffingTrial:
    scenario = ForestFireScenario()
    chief = ChiefAdvisor(scenario.council)
    workspace = scenario.workspace

    chief.open_goal_frame(workspace, _OBJECTIVE)
    chief.solo_review(workspace, _QUESTION_BEFORE)
    scenario.apply_e1_facts()
    chief.solo_review(workspace, _QUESTION_AFTER)

    return _collect(workspace, "solo_chief")


def _run_staffed_trial() -> StaffingTrial:
    scenario = ForestFireScenario()
    council = StaffCouncil(subset_playbook(FOREST_FIRE_PLAYBOOK, MINIMAL_THREE_POSTS))
    chief = ChiefAdvisor(council)
    workspace = scenario.workspace

    goal = chief.open_goal_frame(workspace, _OBJECTIVE)
    topic = chief.orchestrate(workspace, _QUESTION_BEFORE)
    chief.build_question_tree(workspace, goal)
    scenario.apply_e1_facts()
    chief.orchestrate(workspace, _QUESTION_AFTER)

    return _collect(workspace, "minimal_three_posts")


def _collect(workspace, mode: str) -> StaffingTrial:
    return StaffingTrial(
        mode=mode,
        event_count=workspace.version,
        assessment_count=len(workspace.assessments),
        staff_task_count=len(workspace.staff_tasks),
        proposal_count=len(workspace.proposals),
        risks_detected=tuple(
            dict.fromkeys(
                risk
                for assessment in workspace.assessments
                for risk in assessment.risks
            )
        ),
        open_gap_subjects=tuple(
            gap.subject
            for gap in workspace.information_gaps.values()
            if gap.status == InformationGapStatus.OPEN
        ),
    )


def run_comparison() -> ComparisonReport:
    """Run both staffing arrangements over identical materials and diff the findings."""
    solo = _run_solo_trial()
    staffed = _run_staffed_trial()
    unique = tuple(sorted(staffed.findings - solo.findings))
    return ComparisonReport(solo=solo, staffed=staffed, unique_staffed_findings=unique)
