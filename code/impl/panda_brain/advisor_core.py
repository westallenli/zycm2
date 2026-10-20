"""Domain-neutral advisor council contracts and orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Iterable

from .models import (
    AdvisoryTopic,
    AdvisorProposal,
    AdvisorRole,
    Assessment,
    Disagreement,
    FeasibilityVerdict,
    InformationGap,
    InformationGapStatus,
    IntelItem,
    Option,
    StaffPlan,
    StaffTask,
    StaffWorkType,
)
from .workspace import ScenarioWorkspace

if TYPE_CHECKING:
    from .llm_enhance import Enhancer


AssessmentBuilder = Callable[[ScenarioWorkspace], Assessment]


@dataclass(frozen=True)
class AdvisorAgentSpec:
    agent_id: str
    role: AdvisorRole
    build_assessment: AssessmentBuilder


@dataclass(frozen=True)
class SpecialistSpec:
    """A specialist contribution inside a domain playbook."""

    agent_id: str
    role: AdvisorRole
    work_type: StaffWorkType
    question_to_answer: str
    expected_deliverable: str
    dependencies: tuple[str, ...] = ()  # agent_ids of prerequisite specialists
    forbidden_actions: tuple[str, ...] = ("issuing orders", "recording decisions")
    build: Callable[[ScenarioWorkspace, tuple[str, ...]], Assessment] = None  # (workspace, feedback_risks)


@dataclass(frozen=True)
class DomainPlaybook:
    """Domain content supplied to the generic council; no domain logic lives in the council itself."""

    name: str
    objective: str
    specialists: tuple[SpecialistSpec, ...]
    option_dependencies: tuple[str, ...] = ()  # agent_ids whose output the option design needs
    build_intel: Callable[[ScenarioWorkspace], Iterable[IntelItem]] = None
    build_options: Callable[[ScenarioWorkspace, tuple[str, ...]], list[Option]] = None
    build_disagreement: Callable[[ScenarioWorkspace], Disagreement | None] = None
    build_feasibility_verdicts: Callable[[ScenarioWorkspace], Iterable[FeasibilityVerdict]] | None = None


def subset_playbook(base: DomainPlaybook, keep_agents: tuple[str, ...]) -> DomainPlaybook:
    """Derive a reduced staffing arrangement from a base playbook (P2 comparison)."""
    specialists = tuple(spec for spec in base.specialists if spec.agent_id in keep_agents)
    missing = {
        dependency
        for spec in specialists
        for dependency in spec.dependencies
        if dependency not in keep_agents
    }
    if missing:
        raise ValueError(f"Kept specialists depend on excluded agents: {sorted(missing)}")
    option_dependencies = tuple(
        agent for agent in base.option_dependencies if agent in keep_agents
    )
    return replace(base, specialists=specialists, option_dependencies=option_dependencies)


class AdvisorCouncil:
    """Coordinates generic advisor roles without domain-specific assumptions."""

    def __init__(
        self,
        agents: Iterable[AdvisorAgentSpec] = (),
        role_by_agent: dict[str, AdvisorRole] | None = None,
        enhancer: "Enhancer | None" = None,
    ) -> None:
        self.agents = tuple(agents)
        self.role_by_agent = dict(role_by_agent or {})
        self.enhancer = enhancer
        agent_ids = [agent.agent_id for agent in self.agents] + list(self.role_by_agent)
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("Advisor agent ids must be unique.")

    def convene(self, workspace: ScenarioWorkspace) -> list[Assessment]:
        assessments = [agent.build_assessment(workspace) for agent in self.agents]
        self.record_assessments(workspace, assessments)
        return assessments

    def start_conference(self, workspace: ScenarioWorkspace, topic: AdvisoryTopic) -> AdvisoryTopic:
        workspace.record_topic(topic)
        workspace.append_event("SituationConferenceStarted", topic_id=topic.id)
        return topic

    def submit_proposal(
        self,
        workspace: ScenarioWorkspace,
        proposal: AdvisorProposal,
    ) -> AdvisorProposal:
        if proposal.advisor_role != AdvisorRole.OPTION:
            raise ValueError("Only the generic OptionAgent role may submit an AdvisorProposal.")
        return workspace.record_proposal(proposal)

    def record_intelligence(
        self,
        workspace: ScenarioWorkspace,
        intel_items: Iterable[IntelItem],
    ) -> list[IntelItem]:
        return [workspace.record_intel(intel) for intel in intel_items]

    def record_assessments(
        self,
        workspace: ScenarioWorkspace,
        assessments: Iterable[Assessment],
    ) -> list[Assessment]:
        expected_roles = {
            **self.role_by_agent,
            **{agent.agent_id: agent.role for agent in self.agents},
        }
        recorded: list[Assessment] = []
        for assessment in assessments:
            if self.enhancer is not None:
                assessment = self.enhancer(workspace, assessment)
            expected_role = expected_roles.get(assessment.agent)
            if expected_role is not None and assessment.advisor_role != expected_role:
                raise ValueError(
                    f"Assessment role mismatch for {assessment.agent}: "
                    f"expected {expected_role.value}, got "
                    f"{assessment.advisor_role.value if assessment.advisor_role else None}."
                )
            workspace.record_assessment(assessment)
            for index, unknown in enumerate(assessment.unknowns, start=1):
                workspace.record_information_gap(
                    InformationGap(
                        id=f"gap-{assessment.id}-{index:02d}",
                        subject=unknown,
                        reason=f"{assessment.agent} identified missing or unverified information.",
                        discovered_by=assessment.agent,
                        assessment_id=assessment.id,
                        source_version=workspace.version,
                    )
                )
            recorded.append(assessment)
        return recorded


class StaffCouncil:
    """Domain-neutral council driver: plan → intel → specialists → options → proposal.

    All domain knowledge is injected through a DomainPlaybook; this class only
    orchestrates staffing plans, task completion, and proposal assembly.
    """

    OPTION_AGENT_ID = "OptionAgent"

    def __init__(self, playbook: DomainPlaybook, *, enhancer: "Enhancer | None" = None) -> None:
        self.playbook = playbook
        self.core = AdvisorCouncil(
            role_by_agent={spec.agent_id: spec.role for spec in playbook.specialists},
            enhancer=enhancer,
        )

    def review(self, workspace: ScenarioWorkspace, topic: AdvisoryTopic | None = None) -> list[Option]:
        if topic is not None:
            self.core.start_conference(workspace, topic)

        plan = self._build_staff_plan(workspace, topic)
        workspace.record_staff_plan(plan)
        task_id_by_agent = {
            task.owner_agent: task.id
            for task in plan.tasks
        }

        self.core.record_intelligence(workspace, self.playbook.build_intel(workspace))
        feedback_risks = self._feedback_risks(workspace)

        # Specialists execute in declared (topological) order; each completes its task.
        for spec in self.playbook.specialists:
            assessment = replace(
                spec.build(workspace, feedback_risks),
                staff_task_id=task_id_by_agent[spec.agent_id],
            )
            self.core.record_assessments(workspace, (assessment,))
            workspace.complete_staff_task(task_id_by_agent[spec.agent_id], (assessment.id,))

        disagreement = self.playbook.build_disagreement(workspace)
        if disagreement is not None:
            workspace.record_disagreement(disagreement)

        if self.playbook.build_feasibility_verdicts is not None:
            for verdict in self.playbook.build_feasibility_verdicts(workspace):
                workspace.record_feasibility_verdict(verdict)

        options = list(self.playbook.build_options(workspace, feedback_risks))
        catalogue_ids = {option.id for option in options}
        external_active = [
            option
            for option in workspace.options.values()
            if option.id not in catalogue_ids and option.status == "active"
        ]
        available_options = [
            option
            for option in options
            if workspace.options.get(option.id, option).status != "rejected"
        ] + external_active
        for option in options:
            existing = workspace.options.get(option.id)
            if existing is None:
                workspace.add_option(option)
            elif existing.status == "affected":
                existing.status = "active"
                existing.evidence_ids = option.evidence_ids
                existing.claim_subjects = option.claim_subjects
                existing.risks = option.risks
                existing.proposer_role = option.proposer_role
                existing.domain_role = option.domain_role
                workspace.append_event("OptionRefreshed", option_id=existing.id)

        workspace.complete_staff_task(task_id_by_agent[self.OPTION_AGENT_ID])

        if topic is not None:
            self.core.submit_proposal(
                workspace,
                AdvisorProposal(
                    id=f"proposal-{workspace.version + 1:03d}",
                    topic_id=topic.id,
                    advisor_role=AdvisorRole.OPTION,
                    summary="Presented the current option set, evidence gaps, risks, and unknowns for commander review.",
                    option_ids=tuple(option.id for option in available_options),
                    evidence_ids=tuple(dict.fromkeys(
                        evidence_id
                        for option in available_options
                        for evidence_id in option.evidence_ids
                    )),
                    risks=tuple(dict.fromkeys(risk for option in available_options for risk in option.risks)),
                    unknowns=tuple(dict.fromkeys(
                        gap.subject
                        for gap in workspace.information_gaps.values()
                        if gap.status == InformationGapStatus.OPEN
                    )),
                    source_version=workspace.version,
                    staff_task_id=task_id_by_agent[self.OPTION_AGENT_ID],
                ),
            )
        return list(workspace.options.values())

    def _build_staff_plan(self, workspace: ScenarioWorkspace, topic: AdvisoryTopic | None) -> StaffPlan:
        suffix = f"{workspace.version + 1:03d}"
        prefix = f"stafftask-{suffix}"
        plan_id = f"staffplan-{suffix}"
        index_by_agent = {spec.agent_id: index for index, spec in enumerate(self.playbook.specialists, start=1)}
        total = len(self.playbook.specialists) + 1

        tasks: list[StaffTask] = []
        for spec in self.playbook.specialists:
            tasks.append(
                StaffTask(
                    id=f"{prefix}-{index_by_agent[spec.agent_id]:02d}",
                    plan_id=plan_id,
                    owner_agent=spec.agent_id,
                    owner_role=spec.role,
                    work_type=spec.work_type,
                    question_to_answer=spec.question_to_answer,
                    expected_deliverable=spec.expected_deliverable,
                    allowed_inputs=("claims", "intel_items"),
                    allowed_sources=("synthetic-observations",),
                    forbidden_actions=spec.forbidden_actions,
                    dependencies=tuple(
                        f"{prefix}-{index_by_agent[dep]:02d}" for dep in spec.dependencies
                    ),
                )
            )
        option_index = total
        tasks.append(
            StaffTask(
                id=f"{prefix}-{option_index:02d}",
                plan_id=plan_id,
                owner_agent=self.OPTION_AGENT_ID,
                owner_role=AdvisorRole.OPTION,
                work_type=StaffWorkType.OPTION_DESIGN,
                question_to_answer="Which bounded option set should be presented for commander review?",
                expected_deliverable="AdvisorProposal with option set",
                allowed_inputs=("claims", "intel_items", "assessments"),
                allowed_sources=("synthetic-observations",),
                forbidden_actions=("issuing orders", "recording decisions"),
                dependencies=tuple(
                    f"{prefix}-{index_by_agent[dep]:02d}"
                    for dep in self.playbook.option_dependencies
                ),
            )
        )
        staffed_roles = {spec.role for spec in self.playbook.specialists} | {AdvisorRole.OPTION}
        unstaffed = [role.value for role in AdvisorRole if role not in staffed_roles]
        return StaffPlan(
            id=plan_id,
            topic_id=topic.id if topic is not None else None,
            created_by="ChiefAdvisor",
            question=topic.question if topic is not None else "Refresh the shared situation assessment.",
            rationale_enabled=(
                "Minimal staffing for commander decision preparation:"
                + ", ".join(spec.agent_id for spec in self.playbook.specialists)
                + f", {self.OPTION_AGENT_ID}; no additional roles activated."
            ),
            rationale_disabled=(
                "Posts left unstaffed in this reviewed configuration: "
                + (", ".join(unstaffed) if unstaffed else "none")
                + "."
            ),
            tasks=tuple(tasks),
            source_version=workspace.version,
        )

    @staticmethod
    def _feedback_risks(workspace: ScenarioWorkspace) -> tuple[str, ...]:
        latest_feedback = next(reversed(tuple(workspace.feedback.values())), None)
        risks: list[str] = []
        if latest_feedback is not None:
            if latest_feedback.feedback_type.value == "reject" and latest_feedback.option_id:
                risks.append(
                    f"Commander rejected option {latest_feedback.option_id}; do not restate it without new evidence."
                )
            elif latest_feedback.feedback_type.value == "modify":
                risks.append("Commander requested a revised option before any new decision.")
        return tuple(risks)
