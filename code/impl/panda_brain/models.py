"""Stable domain objects for the offline command-staff simulation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

from .task_drafts import PlanTaskBlueprint


class ClaimStatus(StrEnum):
    UNKNOWN = "unknown"
    HYPOTHESIS = "hypothesis"
    OBSERVED = "observed"
    VERIFIED = "verified"
    CHALLENGED = "challenged"
    SUPERSEDED = "superseded"


class InformationGapStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class SourceTrustLevel(StrEnum):
    """Trust level attached to content when it enters the workspace (doc 11 §3.4)."""

    AUTHORIZED = "authorized"
    PENDING_VERIFICATION = "pending_verification"
    UNTRUSTED = "untrusted"


class AdvisorRole(StrEnum):
    """Generic advisor-staff roles shared across domains."""

    CHIEF_ADVISOR = "chief_advisor"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    OPTION = "option"
    RED_TEAM = "red_team"
    FEASIBILITY = "feasibility"
    VERIFICATION = "verification"
    MEMORY = "memory"
    EXECUTION = "execution"


class DecisionActor(StrEnum):
    COMMANDER = "commander"
    STAFF = "staff"


#: The single authenticated general entry assumed by the first-version safety
#: premise (single user, local run, one general entry; doc 11 §3.5).
GENERAL_ENTRY_ID = "local-general-entry"


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoleProfile:
    role: AdvisorRole
    title: str
    responsibility: str
    can_propose: bool
    can_decide: bool
    can_issue_order: bool
    can_publish: bool
    can_draft_orders: bool = False


#: Only the chief advisor drafts orders and briefs; only the option role
#: submits AdvisorProposals (doc 09 role powers, enforced structurally).
_CAN_PROPOSE = frozenset({AdvisorRole.CHIEF_ADVISOR, AdvisorRole.OPTION})

ROLE_PROFILES: dict[AdvisorRole, RoleProfile] = {
    role: RoleProfile(
        role=role,
        title=role.value.replace("_", " "),
        responsibility=role.value,
        can_propose=role in _CAN_PROPOSE,
        can_decide=False,
        can_issue_order=False,
        can_publish=False,
        can_draft_orders=role == AdvisorRole.CHIEF_ADVISOR,
    )
    for role in AdvisorRole
}


class DecisionType(StrEnum):
    ADOPT = "adopt"
    REJECT = "reject"
    MODIFY = "modify"
    DEFER = "defer"
    PROPOSE = "propose"
    REQUEST_RESEARCH = "request_research"
    PAUSE = "pause"
    CANCEL = "cancel"


class FeedbackType(StrEnum):
    ADD_FACT = "add_fact"
    CLARIFY = "clarify"
    ADOPT = "adopt"
    REJECT = "reject"
    MODIFY = "modify"
    DEFER = "defer"
    PROPOSE = "propose"
    REQUEST_RESEARCH = "request_research"
    PAUSE = "pause"


class InputClass(StrEnum):
    """GeneralInput classification gate (doc 11 §4.3).

    Casual talk never mutates shared state; clarifications answer questions;
    only formal corrections invalidate downstream drafts.
    """

    CASUAL_DISCUSSION = "casual_discussion"
    CLARIFICATION = "clarification"
    FORMAL_CORRECTION = "formal_correction"


class OrderStatus(StrEnum):
    """Full execution-order lifecycle (doc 11 §3.5).

    draft -> pending_confirm -> confirmed -> active(paused) ->
    completed/failed/cancelled/expired/superseded
    """

    DRAFT = "draft"
    PENDING_CONFIRM = "pending_confirm"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


#: Terminal orders can never be revived (doc 11 §3.5 rule 4).
TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.COMPLETED,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.SUPERSEDED,
    }
)


#: Decision types that may produce an execution order.
ORDER_DECISION_TYPES = frozenset({DecisionType.ADOPT, DecisionType.MODIFY, DecisionType.PROPOSE})


class Reversibility(StrEnum):
    """Action reversibility tier surfaced for trade-off decisions (doc 11 §7 P3)."""

    REVERSIBLE = "reversible"
    PARTIALLY_REVERSIBLE = "partially_reversible"
    IRREVERSIBLE = "irreversible"


class FeasibilityStatus(StrEnum):
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    PARTIAL = "partial"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_OUTCOME = "unknown_outcome"


#: Execution risk tiers; L3+ actions require a valid approval before activation
#: and before every executor call (doc 11 §3.5, §7 P4).
class RiskLevel(IntEnum):
    L0 = 0  # read-only observation
    L1 = 1  # low-impact local write
    L2 = 2  # bounded action inside the simulated world
    L3 = 3  # external side effect (publish, send, delete)
    L4 = 4  # large or irreversible external impact

    @classmethod
    def parse(cls, value: str) -> "RiskLevel":
        return cls[value.upper()]


class MemoryCandidateStatus(StrEnum):
    """Controlled-memory lifecycle: nothing influences advice until confirmed."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class StaffWorkType(StrEnum):
    """The seven staff work types defined in doc 10 §4."""

    FACT_GAP = "fact_gap"
    INTERPRETATION = "interpretation"
    OPTION_DESIGN = "option_design"
    RISK_CHALLENGE = "risk_challenge"
    EXECUTION_PREPARATION = "execution_preparation"
    VERIFICATION = "verification"
    LEARNING = "learning"


class StaffTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FeasibilityVerdict:
    """Structured per-option feasibility ruling by a feasibility post (doc 11 §7 P3)."""

    id: str
    option_id: str
    status: FeasibilityStatus
    blocking_conditions: tuple[str, ...] = ()
    notes: str = ""
    assessed_by: str = "FeasibilityAgent"
    source_version: int = 0


@dataclass(frozen=True)
class GeneralQuestionItem:
    """Structured escalation item detected while composing a decision brief."""

    id: str
    kind: str  # value_tradeoff | insufficient_evidence | key_disagreement | gap
    text: str
    subject: str | None = None


@dataclass(frozen=True)
class OptionRow:
    """One comparison-matrix row of the decision brief: what the general trades off."""

    option_id: str
    preconditions_met: tuple[str, ...]
    preconditions_unmet: tuple[str, ...]
    cost_estimate: str
    time_estimate: str
    reversibility: str
    no_go_triggered: bool
    feasibility_status: str
    blocking_conditions: tuple[str, ...]
    key_risks: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    """Immutable evidence snapshot with a content hash (doc 11 §6.2)."""

    id: str
    content: str
    content_hash: str
    source: str
    trust_level: SourceTrustLevel = SourceTrustLevel.PENDING_VERIFICATION

    @classmethod
    def create(
        cls,
        id: str,
        content: str,
        source: str,
        trust_level: SourceTrustLevel = SourceTrustLevel.PENDING_VERIFICATION,
    ) -> "Evidence":
        return cls(
            id=id,
            content=content,
            content_hash=content_digest(content),
            source=source,
            trust_level=trust_level,
        )


@dataclass(frozen=True)
class Claim:
    id: str
    subject: str
    value: Any
    status: ClaimStatus
    source: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntelItem:
    id: str
    subject: str
    summary: str
    source: str
    claim_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    source_version: int = 0
    trust_level: SourceTrustLevel = SourceTrustLevel.PENDING_VERIFICATION


@dataclass
class InformationGap:
    id: str
    subject: str
    reason: str
    discovered_by: str
    assessment_id: str | None = None
    status: InformationGapStatus = InformationGapStatus.OPEN
    resolved_by_claim_id: str | None = None
    source_version: int = 0


@dataclass(frozen=True)
class Assessment:
    id: str
    agent: str
    summary: str
    evidence_ids: tuple[str, ...]
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    advisor_role: AdvisorRole | None = None
    domain_role: str | None = None
    topic_id: str | None = None
    staff_task_id: str | None = None


@dataclass(frozen=True)
class Disagreement:
    id: str
    topic: str
    positions: dict[str, str]
    evidence_ids: tuple[str, ...]


@dataclass
class Option:
    id: str
    title: str
    summary: str
    evidence_ids: tuple[str, ...]
    claim_subjects: tuple[str, ...]
    risks: tuple[str, ...] = ()
    status: str = "active"
    proposer_role: AdvisorRole | None = None
    domain_role: str | None = None
    preconditions: tuple[str, ...] = ()
    cost_estimate: str = "unspecified"
    time_estimate: str = "unspecified"
    reversibility: Reversibility = Reversibility.REVERSIBLE
    no_go_conditions: tuple[str, ...] = ()
    proposed_by_commander: bool = False
    # 地图推演层（仅演示/推演用途，前台以虚线+「推演」标注呈现，绝不等同正式态势）。
    sim_shapes: tuple[dict, ...] = ()
    # Explicit task division; absent legacy data remains unknown.
    task_blueprint: PlanTaskBlueprint | None = None


@dataclass(frozen=True)
class AdvisoryTopic:
    id: str
    objective: str
    question: str
    constraints: tuple[str, ...] = ()
    source_version: int = 0


@dataclass
class AdvisorProposal:
    id: str
    topic_id: str
    advisor_role: AdvisorRole
    summary: str
    option_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    source_version: int = 0
    staff_task_id: str | None = None
    status: str = "active"


@dataclass
class StaffTask:
    """Minimal staff-task contract (doc 10 §6, doc 11 §7 P0 scope)."""

    id: str
    plan_id: str
    owner_agent: str
    owner_role: AdvisorRole
    work_type: StaffWorkType
    question_to_answer: str
    expected_deliverable: str
    allowed_inputs: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    quality_checks: tuple[str, ...] = ()
    status: StaffTaskStatus = StaffTaskStatus.PENDING
    output_assessment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StaffPlan:
    """A chief-advisor staffing plan with its minimal task breakdown."""

    id: str
    topic_id: str | None
    created_by: str
    question: str
    rationale_enabled: str
    tasks: tuple[StaffTask, ...] = ()
    source_version: int = 0
    #: Why the posts NOT in this plan were left out (doc 11 §5.1).
    rationale_disabled: str = ""


@dataclass(frozen=True)
class CommanderFeedback:
    id: str
    feedback_type: FeedbackType
    message: str
    source_version: int
    actor_role: DecisionActor
    decided_by: str
    input_class: InputClass
    topic_id: str | None = None
    proposal_id: str | None = None
    option_id: str | None = None
    claim_updates: tuple[Claim, ...] = ()


@dataclass(frozen=True)
class GeneralDecision:
    id: str
    decision_type: DecisionType
    option_id: str | None
    reason: str
    scope: str
    source_version: int
    actor_role: DecisionActor
    decided_by: str
    proposal_id: str | None = None
    accepted_risks: tuple[str, ...] = ()
    supersedes_decision_id: str | None = None


@dataclass
class ExecutionOrder:
    id: str
    decision_id: str
    option_id: str
    scope: str
    status: OrderStatus = OrderStatus.DRAFT
    drafted_by: str | None = None
    confirmed_by: str | None = None
    valid_until_version: int | None = None


@dataclass(frozen=True)
class ActionContract:
    """Structured action protocol; the executor accepts nothing less (doc 11 §3.5)."""

    id: str
    order_id: str
    action_type: str
    risk_level: RiskLevel
    capability_requirements: tuple[str, ...] = ()
    parameters: tuple[tuple[str, str], ...] = ()
    side_effect_boundary: str = "simulated-world-only"
    idempotency_key: str = ""
    drafted_by: str = ""
    source_version: int = 0


@dataclass(frozen=True)
class PolicyCheckResult:
    id: str
    order_id: str
    contract_id: str
    passed: bool
    failed_rules: tuple[str, ...] = ()
    checked_rules: tuple[str, ...] = ()
    source_version: int = 0


@dataclass(frozen=True)
class Approval:
    """Commander approval bound to action, object, risk tier, and validity window."""

    id: str
    action_contract_id: str
    order_id: str
    action_type: str
    risk_level: RiskLevel
    approver: str
    actor_role: DecisionActor
    valid_until_version: int | None = None
    source_version: int = 0


@dataclass(frozen=True)
class GeneralQuestion:
    """A question the system must hand back to the general instead of deciding."""

    id: str
    kind: str  # execution_anomaly | value_tradeoff | ...
    subject: str
    question: str
    context: tuple[str, ...] = ()
    status: str = "open"
    answer: str | None = None
    source_version: int = 0
    #: Suggested reply deadline on the logical version clock (doc 11 §5.5);
    #: overdue never auto-decides — it only enables a single reminder.
    suggested_reply_by_version: int | None = None


@dataclass(frozen=True)
class Review:
    """After-action review composed deterministically from the event timeline."""

    id: str
    topic_id: str | None
    scope: str
    findings: tuple[str, ...]
    entries_replayed: int
    created_by: str
    source_version: int = 0


@dataclass(frozen=True)
class MemoryCandidate:
    """Experience candidate with an explicit applicability boundary.

    It never writes back into current facts and never overrides commands;
    it may influence advice only after the general confirms it.
    """

    id: str
    content: str
    source_review_id: str
    applicability_boundary: tuple[str, ...]
    status: MemoryCandidateStatus = MemoryCandidateStatus.PROPOSED
    confirmed_by: str | None = None
    source_version: int = 0


@dataclass(frozen=True)
class OrchestrationFeedback:
    """Staffing-quality signal: repeated research, missing red team, premature
    escalation, or overwork."""

    id: str
    kind: str
    detail: str
    source_version: int = 0


@dataclass(frozen=True)
class ExecutionReport:
    id: str
    order_id: str
    status: AttemptStatus
    observations: tuple[str, ...]
    action_contract_id: str | None = None
    idempotency_key: str = ""


@dataclass(frozen=True)
class VerificationResult:
    id: str
    passed: bool
    unmet_conditions: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    id: str
    sequence: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""
    recorded_at: str = ""  # Server knowledge clock; empty on legacy records.


@dataclass(frozen=True)
class GoalFrame:
    """Commander-confirmed goal frame (doc 11 §3.2 目标态势)."""

    id: str
    statement: str
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    priority: str = "normal"
    confirmed_by: str | None = None
    source_version: int = 0


@dataclass(frozen=True)
class QuestionNode:
    id: str
    parent_id: str | None
    question: str
    kind: str  # root | gap | risk | option_selection | verification
    subject: str | None = None
    status: str = "open"


@dataclass(frozen=True)
class QuestionTree:
    id: str
    goal_frame_id: str
    nodes: tuple[QuestionNode, ...] = ()
    source_version: int = 0


@dataclass(frozen=True)
class DecisionBrief:
    """Fixed-structure decision material presented to the general (doc 10 §7)."""

    id: str
    topic_id: str
    goal_frame_id: str | None
    question: str
    situation_summary: tuple[str, ...]
    option_ids: tuple[str, ...]
    key_risks: tuple[str, ...]
    disagreement_ids: tuple[str, ...]
    open_gap_subjects: tuple[str, ...]
    recommendation: str
    general_questions: tuple[str, ...]
    source_version: int = 0
    option_matrix: tuple[OptionRow, ...] = ()
    question_items: tuple[GeneralQuestionItem, ...] = ()
    relevant_experiences: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonRecord:
    """常备人员库条目：一人一档，以手机号为唯一键（PRD-001 同构字段）。"""

    name: str
    phone: str
    unit: str = ""
    home_position: str = ""
    specialty: str = ""
    is_leader: bool = False


@dataclass(frozen=True)
class RosterBinding:
    """火情花名册条目：某人绑定到本场火的某个岗位槽位。"""

    id: str
    pos_code: str
    person: PersonRecord
    status: str  # active | checked_out
    bound_at_version: int = 0
    checked_out_at_version: int | None = None
