"""Append-only shared workspace with explicit decision and order boundaries."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone

from . import tactical_map as tm
from . import situation_monitor as sm
from typing import Any

from .auth import GeneralCredentials, GeneralEntryAuth
from .record_contracts import ObjectRevision, explicit_time
from .task_drafts import TaskCommonContent, TaskDraft, TaskDraftBundle, TaskDraftSpec, build_task_drafts
from .models import (
    ActionContract,
    Assessment,
    AttemptStatus,
    Approval,
    Claim,
    ClaimStatus,
    AdvisorRole,
    AdvisoryTopic,
    AdvisorProposal,
    CommanderFeedback,
    DecisionActor,
    DecisionBrief,
    DecisionType,
    Disagreement,
    Event,
    Evidence,
    ExecutionOrder,
    ExecutionReport,
    FeedbackType,
    FeasibilityStatus,
    FeasibilityVerdict,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    GoalFrame,
    GeneralDecision,
    InformationGap,
    InformationGapStatus,
    InputClass,
    IntelItem,
    MemoryCandidate,
    MemoryCandidateStatus,
    Option,
    OrchestrationFeedback,
    ORDER_DECISION_TYPES,
    OrderStatus,
    PersonRecord,
    PolicyCheckResult,
    Review,
    ROLE_PROFILES,
    RoleProfile,
    RosterBinding,
    RiskLevel,
    SourceTrustLevel,
    StaffPlan,
    StaffTask,
    StaffTaskStatus,
    TERMINAL_ORDER_STATUSES,
    VerificationResult,
)
from .map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
    TACTICAL_KINDS,
    can_transition as annotation_can_transition,
    status_word as annotation_status_word,
    validate_geo_points,
)
from .org_model import (
    _DOMAIN_PARENT,
    COMMAND_NODES,
    POSITION_SLOTS,
    SLOT_INDEX,
    force_posture,
    mask_phone,
    normalize_phone,
)
from .safety_doctrine import SAFETY_DOCTRINE_RULES

GENESIS_HASH = "0" * 64


def _rule_no_challenged_basis(workspace: "ScenarioWorkspace", contract: ActionContract) -> bool:
    """Actions may not rest on challenged or superseded claims."""
    for key, subject in contract.parameters:
        if key == "claim_subject":
            claim = workspace.claims.get(subject)
            if claim is not None and claim.status in {ClaimStatus.CHALLENGED, ClaimStatus.SUPERSEDED}:
                return False
    return True


def _rule_declared_boundary(workspace: "ScenarioWorkspace", contract: ActionContract) -> bool:
    """Every contract must declare its side-effect boundary explicitly."""
    return bool(contract.side_effect_boundary)


def _rule_verified_basis_for_l3(workspace: "ScenarioWorkspace", contract: ActionContract) -> bool:
    """L3+ actions rest only on verified, authorized-source preconditions.

    Doc 11 §3.4 item 2: unverified content must never back an L3+ action or its
    drafting basis. A precondition claim must be VERIFIED (which structurally
    requires AUTHORIZED evidence) and we re-check evidence trust here as
    defence in depth. An option without declared preconditions cannot carry a
    high-impact action at all.
    """
    if contract.risk_level < RiskLevel.L3:
        return True
    order = workspace.orders.get(contract.order_id)
    if order is None:
        return False
    option = workspace.options.get(order.option_id)
    if option is None or not option.preconditions:
        return False
    for subject in option.preconditions:
        claim = workspace.claims.get(subject)
        if claim is None or claim.status != ClaimStatus.VERIFIED:
            return False
        if not any(
            evidence_id in workspace.evidence
            and workspace.evidence[evidence_id].trust_level == SourceTrustLevel.AUTHORIZED
            for evidence_id in claim.evidence_ids
        ):
            return False
    return True


# 展示层中文措辞：错误文本前台直出，故一律中文；内部枚举值仅出现在括注里。
_ORDER_STATUS_CN = {
    "draft": "草稿", "pending_confirm": "待签发", "confirmed": "已签发",
    "active": "执行中", "paused": "已暂停", "completed": "已完成",
    "failed": "失败", "cancelled": "已取消", "expired": "已过期",
    "superseded": "已被新命令替代",
}
_TASK_STATUS_CN = {
    "pending": "待办", "in_progress": "在办", "completed": "已完成",
    "failed": "失败", "blocked": "受阻",
}
_ROLE_CN = {"commander": "指挥员", "chief_advisor": "首席参谋", "staff": "参谋",
            "analyst": "分析参谋", "logistics": "保障参谋", "operations": "作战参谋",
            "recorder": "记录员", "reviewer": "审查参谋"}


def _ord_cn(value: object) -> str:
    v = str(getattr(value, "value", value))
    return _ORDER_STATUS_CN.get(v, v)


def _task_cn(value: object) -> str:
    v = str(getattr(value, "value", value))
    return _TASK_STATUS_CN.get(v, v)


def _role_cn(value: object) -> str:
    v = str(getattr(value, "value", value))
    return _ROLE_CN.get(v, v)


POLICY_RULES: dict[str, tuple[str, Any]] = {
    "no-challenged-basis": (
        "行动不得建立在已被质疑或已被替代的主张上。",
        _rule_no_challenged_basis,
    ),
    "declared-boundary": (
        "每份行动契约必须声明其副作用边界。",
        _rule_declared_boundary,
    ),
    "verified-basis-for-l3": (
        "高风险（L3+）行动必须建立在已经指挥员核验、且持授权来源证据的前置主张上；"
        "无前置条件的方案不得承载高风险行动。",
        _rule_verified_basis_for_l3,
    ),
}

# Safety-doctrine gates (P15 T3): hard constraints from 《灭火组织指挥手册》/
# 《前指工作指南》 join the same activation gate as the kernel rules.
POLICY_RULES.update(SAFETY_DOCTRINE_RULES)


class WorkspaceConflictError(Exception):
    """Raised when a write carries a stale revision (doc 11 §6.2 optimistic locking)."""

    def __init__(self, message: str, expected_version: int, current_version: int) -> None:
        super().__init__(message)
        self.expected_version = expected_version
        self.current_version = current_version


def event_hash(sequence: int, kind: str, payload: dict[str, Any], prev_hash: str, recorded_at: str = '') -> str:
    """Deterministic SHA-256 audit hash over one event and its predecessor."""
    basis = json.dumps(
        {"sequence": sequence, "kind": kind, "payload": payload, "prev_hash": prev_hash,
         **({'recorded_at': recorded_at} if recorded_at else {})},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class ScenarioWorkspace:
    """In-memory workspace used only by the offline scenario.

    Events are append-only. Current dictionaries are convenience indexes rebuilt
    from events during replay and never call an executor.
    """

    def __init__(
        self,
        scenario_id: str,
        *,
        general_auth: "GeneralEntryAuth | None" = None,
        event_id: str = "",
    ) -> None:
        self.scenario_id = scenario_id
        # Explicit event binding: scenario names are not event identities.
        self.event_id = event_id
        self.object_revisions: dict[str, ObjectRevision] = {}
        # General-entry lock state (doc 11 §3.5 item 4). Fail-closed: a workspace
        # bound to a real auth starts locked. An unbound workspace provisions an
        # ephemeral harness entry and opens it automatically — that is the
        # documented first-version premise of a single user on a local machine.
        self._general_entry_fingerprint: str | None = None
        self._general_entry_unlocked = False
        if general_auth is None:
            general_auth = GeneralEntryAuth()
            self._general_entry_fingerprint = general_auth.fingerprint
            self._general_entry_unlocked = True
        else:
            self._general_entry_fingerprint = general_auth.fingerprint
        # Audit-chain discipline: the event list is write-protected; the only
        # way in is append_event, which maintains the hash chain. A bypassed
        # write would break the chain's credibility entirely.
        self._events: list[Event] = []
        self.advisor_roles: dict[AdvisorRole, RoleProfile] = dict(ROLE_PROFILES)
        self.claims: dict[str, Claim] = {}
        self.intel_items: dict[str, IntelItem] = {}
        self.information_gaps: dict[str, InformationGap] = {}
        self.assessments: list[Assessment] = []
        self.disagreements: list[Disagreement] = []
        self.topics: dict[str, AdvisoryTopic] = {}
        self.proposals: dict[str, AdvisorProposal] = {}
        self.feedback: dict[str, CommanderFeedback] = {}
        self.options: dict[str, Option] = {}
        self.decisions: dict[str, GeneralDecision] = {}
        self.orders: dict[str, ExecutionOrder] = {}
        self.task_drafts: dict[str, TaskDraftBundle] = {}
        self.reports: dict[str, ExecutionReport] = {}
        self.evidence: dict[str, Evidence] = {}
        self.staff_plans: dict[str, StaffPlan] = {}
        self.staff_tasks: dict[str, StaffTask] = {}
        self.goal_frames: dict[str, GoalFrame] = {}
        self.decision_briefs: dict[str, DecisionBrief] = {}
        self.feasibility_verdicts: dict[str, FeasibilityVerdict] = {}
        self.action_contracts: dict[str, ActionContract] = {}
        self.policy_checks: dict[str, PolicyCheckResult] = {}
        self.approvals: dict[str, Approval] = {}
        self.general_questions: dict[str, GeneralQuestion] = {}
        self.reviews: dict[str, Review] = {}
        self.memory_candidates: dict[str, MemoryCandidate] = {}
        self.orchestration_feedbacks: list[OrchestrationFeedback] = []
        self.verifications: dict[str, VerificationResult] = {}
        # Force organization (P15 T1): standing person library keyed by phone,
        # task roster bindings keyed by binding id. Administrative staff data —
        # deliberately NOT gated behind the general entry (checking in is not
        # an exercise of command authority).
        self.person_library: dict[str, PersonRecord] = {}
        self.roster_bindings: dict[str, RosterBinding] = {}
        # Sandtable annotations (P16-A): schematic marks on the shared map.
        # Drafting is staff work and stays ungated (same premise as checking
        # in); verification / publication / withdrawal ARE authorized acts
        # and sit behind the general entry.
        self.annotations: dict[str, MapAnnotation] = {}
        # P19：路线冲突记录与命令战术层记录（定稿 §5.6、§6.2）。
        # 两者都是独立实体：不静默改写标绘状态，也不替代既有命令链。
        self.conflict_records: dict[str, tm.ConflictRecord] = {}
        self.command_flows: dict[str, tm.CommandFlow] = {}
        # P20（docs/20）：态势变化台账与复核批次。只记录“看到了什么变化”
        # 与“指挥员要求的一次复核”，不承载发布、撤回或命令权力。
        self.situation_changes: dict[str, sm.SituationChange] = {}
        self.situation_reviews: dict[str, sm.SituationReviewBatch] = {}
        self.append_event(
            "AdvisorCouncilEstablished",
            scenario_id=scenario_id,
            roles=tuple(role.value for role in self.advisor_roles),
        )

    @property
    def version(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[Event, ...]:
        """Read-only view over the audit trail."""
        return tuple(self._events)

    # ------------------------------------------------------------------
    # General-entry authentication (doc 11 §3.5 item 4)

    def _require_general_entry(self) -> None:
        check = getattr(self, '_named_command_check', None)
        if not self._general_entry_unlocked and not (check and check()):
            raise PermissionError(
                "指挥入口尚未认证，不能行使指挥权限——请先登录。"
            )

    def unlock_general_entry(self, credentials: object) -> None:
        """Open the entry for this process using proof-of-possession credentials."""
        if not isinstance(credentials, GeneralCredentials):
            raise PermissionError("指挥入口凭据无效：须凭登录签发的有效凭据操作。")
        if credentials.entry_id != GENERAL_ENTRY_ID or (
            self._general_entry_fingerprint is None
            or credentials.secret_fingerprint != self._general_entry_fingerprint
        ):
            raise PermissionError("指挥入口凭据与当前绑定入口不一致。")
        self._general_entry_unlocked = True

    def record_gap_assignment(self, *, gap_text: str, owner_phone: str, due_at: str,
                              assigned_by: str, request_id: str) -> Event:
        """把物资/力量缺口派给具名负责人。一条派单一条审计记录，重复请求幂等。"""
        for event in self._events:
            if event.kind == 'GapAssignment' and event.payload.get('request_id') == request_id:
                return event
        return self.append_event('GapAssignment', gap_text=gap_text, owner_phone=owner_phone,
                                 due_at=due_at, assigned_by=assigned_by, request_id=request_id)

    def gap_assignments(self) -> list[dict[str, str]]:
        return [dict(event.payload) for event in self._events if event.kind == 'GapAssignment']

    def append_event(self, kind: str, **payload: Any) -> Event:
        check = getattr(self, '_named_command_check', None)
        if check and check() and kind != 'ObjectRevisionRecorded':
            payload = dict(payload, business_subject=self._named_command_subject,
                           business_event_id=self.event_id, business_authority='event_deputy')
        sequence = self.version + 1
        previous = self._events[-1] if self._events else None
        prev_hash = previous.hash if previous is not None else GENESIS_HASH
        recorded_at = datetime.now(timezone.utc).isoformat() if self.event_id else ''
        event = Event(
            id=f"evt-{sequence:03d}",
            sequence=sequence,
            kind=kind,
            payload=payload,
            prev_hash=prev_hash,
            hash=event_hash(sequence, kind, payload, prev_hash, recorded_at),
            recorded_at=recorded_at,
        )
        self._events.append(event)
        return event

    def record_object_revision(self, revision: ObjectRevision, *, expected_version: int) -> ObjectRevision:
        """Append an internal record snapshot; this never publishes or verifies it.

        Caller supplies server-recorded time and provenance. New role-specific
        application services are separate from this existing core auth gate.
        """
        self._require_general_entry()
        if revision.reference.object_kind in {'event_role_assignment', 'map_annotation',
                                             'task_review_assessment', 'loan_custody_receipt',
                                             'loan_custody_verification', 'loan_custody_settlement',
                                             'material_supply_contact', 'material_supply_draft',
                                             'professional_assignment', 'task_release_evidence',
                                             'formal_task_release', 'formal_task_receipt',
                                             'formal_task_relations', 'formal_task_control',
                                             'verified_point_stock', 'formal_supply_order',
                                             'formal_supply_receipt', 'formal_task_feedback', 'material_maintenance_grant', 'material_catalog_change', 'material_closeout_account', 'personnel_management', 'offline_map_package', 'terminal_report', 'resource_planning_context'}:
            raise PermissionError('This record kind requires its dedicated application service')
        return self._record_object_revision(revision, expected_version=expected_version)

    def _record_object_revision(self, revision: ObjectRevision, *, expected_version: int) -> ObjectRevision:
        """Internal append primitive; caller must enforce its operation-specific gate."""
        ref = revision.reference
        if not self.event_id or ref.event_id != self.event_id:
            raise ValueError('Record belongs to another event or workspace is unbound')
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('Expected object version must be a nonnegative integer')
        recorded = explicit_time(revision.times.recorded_at)
        for previous in self.object_revisions.values():
            if previous.request_id == revision.request_id:
                if previous == revision:
                    return previous
                raise ValueError('Request ID was already used with different content')
        history = [item for item in self.object_revisions.values()
                   if (item.reference.event_id, item.reference.object_kind, item.reference.object_id)
                   == (ref.event_id, ref.object_kind, ref.object_id)]
        current = max(history, key=lambda item: item.reference.version) if history else None
        version = current.reference.version if current else 0
        if expected_version != version or ref.version != version + 1:
            raise ValueError('Stale or skipped object version')
        if current and recorded < explicit_time(current.times.recorded_at):
            raise ValueError('A correction cannot backdate its system recording time')
        self.append_event('ObjectRevisionRecorded', revision_id=revision.id, revision=asdict(revision))
        self.object_revisions[revision.id] = revision
        return revision

    def object_revision_at(self, event_id: str, object_kind: str, object_id: str, known_at: str) -> ObjectRevision | None:
        """Latest recorded version by cutoff, without importing later knowledge."""
        if not self.event_id or event_id != self.event_id:
            raise ValueError('History query belongs to another event or workspace is unbound')
        cutoff = explicit_time(known_at)
        matches = [item for item in self.object_revisions.values()
                   if (item.reference.event_id, item.reference.object_kind, item.reference.object_id)
                   == (event_id, object_kind, object_id)
                   and item.times.recorded_at and explicit_time(item.times.recorded_at) <= cutoff]
        return max(matches, key=lambda item: item.reference.version) if matches else None

    def verify_audit_chain(self) -> tuple[int, ...]:
        """Return the sequences of events whose hash linkage is broken (empty = intact)."""
        from .read_scope import reuse
        # Read scopes hold the authority lock and never execute business writes.
        return reuse('audit-chain', (self, id(self._events), self.version), self._verify_audit_chain)

    def _verify_audit_chain(self) -> tuple[int, ...]:
        broken: list[int] = []
        prev_hash = GENESIS_HASH
        for event in self.events:
            expected = event_hash(event.sequence, event.kind, event.payload, prev_hash, event.recorded_at)
            if event.prev_hash != prev_hash or event.hash != expected:
                broken.append(event.sequence)
            prev_hash = event.hash
        return tuple(broken)

    def _check_revision(self, expected_version: int | None) -> None:
        """Optimistic concurrency control (doc 11 §6.2): stale submissions are rejected."""
        if expected_version is not None and expected_version != self.version:
            raise WorkspaceConflictError(
                f"提交版本过期：期望 v{expected_version}，当前 v{self.version}。请刷新页面后重新提交。",
                expected_version=expected_version,
                current_version=self.version,
            )

    def record_evidence(self, evidence: Evidence) -> Evidence:
        """Record an immutable evidence snapshot; same id must carry identical content."""
        existing = self.evidence.get(evidence.id)
        if existing is not None:
            if existing.content_hash != evidence.content_hash:
                raise ValueError(f"证据不可变更：{evidence.id} 已存在且内容不同。")
            self.append_event("EvidenceReferenced", evidence_id=evidence.id)
            return existing
        self.evidence[evidence.id] = evidence
        self.append_event("EvidenceRecorded", evidence=asdict(evidence))
        return evidence

    def _require_evidence(self, evidence_ids: tuple[str, ...], *, for_verified: bool) -> None:
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in self.evidence]
        if missing:
            raise ValueError(f"主张只能引用已登记证据；缺少：{missing}")
        if for_verified:
            authorized = [
                evidence_id
                for evidence_id in evidence_ids
                if self.evidence[evidence_id].trust_level == SourceTrustLevel.AUTHORIZED
            ]
            if not authorized:
                raise ValueError("「已核验」主张至少要有一条授权来源证据。")

    def record_claim(self, claim: Claim) -> Claim:
        self._require_evidence(claim.evidence_ids, for_verified=(claim.status == ClaimStatus.VERIFIED))
        self.claims[claim.subject] = claim
        self.append_event("ClaimCreated", claim=asdict(claim))
        self._mark_options_affected(claim.subject)
        if claim.status == ClaimStatus.VERIFIED:
            self._resolve_information_gaps(claim)
        return claim

    def record_intel(self, intel: IntelItem) -> IntelItem:
        if intel.id in self.intel_items:
            self.append_event("IntelReferenced", intel_id=intel.id)
            return self.intel_items[intel.id]
        self.intel_items[intel.id] = intel
        self.append_event("IntelItemRecorded", intel=asdict(intel))
        return intel

    def record_information_gap(self, gap: InformationGap) -> InformationGap:
        existing = next(
            (
                current
                for current in self.information_gaps.values()
                if current.subject == gap.subject and current.status == InformationGapStatus.OPEN
            ),
            None,
        )
        if existing is not None:
            self.append_event(
                "InformationGapReferenced",
                gap_id=existing.id,
                referenced_by=gap.discovered_by,
                assessment_id=gap.assessment_id,
            )
            return existing
        self.information_gaps[gap.id] = gap
        self.append_event("InformationGapOpened", gap=asdict(gap))
        return gap

    def record_staff_plan(self, plan: StaffPlan) -> StaffPlan:
        """Record a chief-advisor staffing plan with its minimal task breakdown."""
        if plan.id in self.staff_plans:
            raise ValueError(f"参谋作业计划已存在：{plan.id}")
        plan_task_ids = set()
        for task in plan.tasks:
            if task.plan_id != plan.id:
                raise ValueError(f"任务 {task.id} 不属于计划 {plan.id}。")
            if task.id in plan_task_ids or task.id in self.staff_tasks:
                raise ValueError(f"参谋任务编号重复：{task.id}")
            plan_task_ids.add(task.id)
        for task in plan.tasks:
            unknown = [dep for dep in task.dependencies if dep not in plan_task_ids]
            if unknown:
                raise ValueError(f"任务 {task.id} 引用了计划外依赖：{unknown}")
        self.staff_plans[plan.id] = plan
        self.append_event(
            "StaffPlanRecorded",
            plan_id=plan.id,
            topic_id=plan.topic_id,
            created_by=plan.created_by,
            rationale_enabled=plan.rationale_enabled,
            task_count=len(plan.tasks),
        )
        for task in plan.tasks:
            self.staff_tasks[task.id] = task
            self.append_event(
                "StaffTaskRecorded",
                task_id=task.id,
                plan_id=plan.id,
                owner_agent=task.owner_agent,
                owner_role=task.owner_role.value,
                work_type=task.work_type.value,
                dependencies=task.dependencies,
            )
        return plan

    def complete_staff_task(self, task_id: str, output_assessment_ids: tuple[str, ...] = ()) -> StaffTask:
        task = self.staff_tasks.get(task_id)
        if task is None:
            raise KeyError(f"未知参谋任务：{task_id}")
        if task.status in {StaffTaskStatus.COMPLETED, StaffTaskStatus.FAILED, StaffTaskStatus.CANCELLED}:
            raise ValueError(f"参谋任务 {task_id} 已结束（{_task_cn(task.status)}）。")
        unmet = [dep for dep in task.dependencies if self.staff_tasks[dep].status != StaffTaskStatus.COMPLETED]
        if unmet:
            raise ValueError(f"参谋任务 {task_id} 的依赖未完成，暂不能结项：{unmet}")
        task.status = StaffTaskStatus.COMPLETED
        task.output_assessment_ids = output_assessment_ids
        self.append_event("StaffTaskCompleted", task_id=task_id, output_assessment_ids=output_assessment_ids)
        return task

    def update_claim(
        self,
        subject: str,
        *,
        value: Any,
        status: ClaimStatus,
        source: str,
        confidence: float,
        evidence_ids: tuple[str, ...],
        expected_version: int | None = None,
    ) -> Claim:
        self._check_revision(expected_version)
        self._require_evidence(evidence_ids, for_verified=(status == ClaimStatus.VERIFIED))
        previous = self.claims.get(subject)
        claim = Claim(
            id=f"claim-{self.version + 1:03d}",
            subject=subject,
            value=value,
            status=status,
            source=source,
            confidence=confidence,
            evidence_ids=evidence_ids,
        )
        self.claims[subject] = claim
        self.append_event(
            "ClaimChallenged" if previous else "ClaimCreated",
            previous_claim_id=previous.id if previous else None,
            claim=asdict(claim),
        )
        self._mark_options_affected(subject)
        if status == ClaimStatus.VERIFIED:
            self._resolve_information_gaps(claim)
        return claim

    def record_assessment(self, assessment: Assessment, *, expected_version: int | None = None) -> Assessment:
        self._check_revision(expected_version)
        self.assessments.append(assessment)
        self.append_event("AssessmentSubmitted", assessment=asdict(assessment))
        return assessment

    def record_disagreement(self, disagreement: Disagreement) -> Disagreement:
        self.disagreements.append(disagreement)
        self.append_event("DisagreementRecorded", disagreement=asdict(disagreement))
        return disagreement

    def record_topic(self, topic: AdvisoryTopic) -> AdvisoryTopic:
        if topic.id in self.topics:
            raise ValueError(f"议事主题已存在：{topic.id}")
        self.topics[topic.id] = topic
        self.append_event("AdvisoryTopicOpened", topic=asdict(topic))
        return topic

    def record_proposal(self, proposal: AdvisorProposal, *, expected_version: int | None = None) -> AdvisorProposal:
        self._check_revision(expected_version)
        if proposal.topic_id not in self.topics:
            raise ValueError("参谋建议书必须挂在已有议题上。")
        if proposal.id in self.proposals:
            raise ValueError(f"参谋建议书已存在：{proposal.id}")
        missing_options = [option_id for option_id in proposal.option_ids if option_id not in self.options]
        if missing_options:
            raise ValueError(f"建议书引用了不存在的方案：{missing_options}")
        self.proposals[proposal.id] = proposal
        self.append_event("AdvisorProposalSubmitted", proposal=asdict(proposal))
        return proposal

    def record_feedback(self, feedback: CommanderFeedback) -> CommanderFeedback:
        self._require_general_entry()
        if feedback.actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员可以记录反馈指令。")
        if feedback.decided_by != GENERAL_ENTRY_ID:
            raise PermissionError(
                "指挥员反馈必须出自已认证的指挥入口。"
            )
        # Doc 11 §4.3 input-class gate: casual talk never mutates shared state.
        deciding_types = {FeedbackType.ADOPT, FeedbackType.REJECT, FeedbackType.MODIFY, FeedbackType.PAUSE}
        if feedback.input_class == InputClass.CASUAL_DISCUSSION and (
            feedback.claim_updates or feedback.feedback_type in deciding_types
        ):
            raise ValueError(
                "普通讨论不能承载事实更新或决断；请让参谋改述为澄清或正式更正。"
            )
        if feedback.feedback_type == FeedbackType.REJECT and feedback.input_class != InputClass.FORMAL_CORRECTION:
            raise ValueError("只有正式更正才能否决方案。")
        if feedback.topic_id is not None and feedback.topic_id not in self.topics:
            raise ValueError("反馈必须挂在已有议题上。")
        if feedback.proposal_id is not None and feedback.proposal_id not in self.proposals:
            raise ValueError("反馈必须挂在已有参谋建议书上。")
        if feedback.option_id is not None:
            if feedback.option_id not in self.options:
                raise ValueError("反馈必须引用已有方案。")
            if feedback.proposal_id is not None and feedback.option_id not in self.proposals[feedback.proposal_id].option_ids:
                raise ValueError("反馈所选方案不在引用的参谋建议书内。")
        self.feedback[feedback.id] = feedback
        self.append_event("CommanderFeedbackRecorded", feedback=asdict(feedback))
        for claim in feedback.claim_updates:
            self.record_claim(claim)
            self.append_event(
                "CommanderFactReceived",
                feedback_id=feedback.id,
                claim_id=claim.id,
            )
        if feedback.feedback_type == FeedbackType.REJECT and feedback.option_id is not None:
            option = self.options[feedback.option_id]
            option.status = "rejected"
            self.append_event(
                "OptionRejected",
                feedback_id=feedback.id,
                option_id=feedback.option_id,
            )
        return feedback

    def add_option(self, option: Option, *, expected_version: int | None = None) -> Option:
        self._check_revision(expected_version)
        if option.proposer_role is not None and not ROLE_PROFILES[option.proposer_role].can_propose:
            raise PermissionError(
                f"角色 {_role_cn(option.proposer_role)} 无权提出方案。"
            )
        self.options[option.id] = option
        self.append_event("OptionsPrepared", option=asdict(option))
        return option

    def add_commander_option(
        self,
        option: Option,
        *,
        actor_role: DecisionActor,
        decided_by: str,
    ) -> Option:
        """Register an option proposed by the general himself (doc 11 §7 P3 acceptance).

        Bypasses the staff proposing-role gate by design — the general is not a
        staff post — but keeps full identity attribution and audit.
        """
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or decided_by != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可直接提出方案。")
        if option.id in self.options:
            raise ValueError(f"方案已存在：{option.id}")
        option.proposer_role = None
        option.proposed_by_commander = True
        self.options[option.id] = option
        self.append_event("CommanderOptionProposed", option=asdict(option), decided_by=decided_by)
        return option

    def record_feasibility_verdict(self, verdict: FeasibilityVerdict) -> FeasibilityVerdict:
        if verdict.option_id not in self.options:
            raise ValueError(f"可行性裁定引用了不存在的方案：{verdict.option_id}")
        self.feasibility_verdicts[verdict.id] = verdict
        self.append_event("FeasibilityVerdictRecorded", verdict=asdict(verdict))
        return verdict

    def record_decision(
        self,
        decision_type: DecisionType,
        option_id: str | None,
        reason: str,
        scope: str,
        *,
        actor_role: DecisionActor,
        decided_by: str,
        proposal_id: str | None = None,
        accepted_risks: tuple[str, ...] = (),
        supersedes_decision_id: str | None = None,
    ) -> GeneralDecision:
        """Record a general decision. Identity is structurally required: there is
        no default actor, and only the authenticated general entry passes."""
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员可以记录决断。")
        if decided_by != GENERAL_ENTRY_ID:
            raise PermissionError(
                "决断必须出自已认证的指挥入口。"
            )
        if proposal_id is not None:
            proposal = self.proposals.get(proposal_id)
            if proposal is None:
                raise ValueError("决断必须引用已有的参谋建议书。")
            if option_id is not None and option_id not in proposal.option_ids:
                raise ValueError("决断所选方案不在引用的参谋建议书内。")
        # Doc 11 §7 P3: adopting a risky option requires explicit, tracked risk acceptance.
        accepted_risks = tuple(accepted_risks)
        if decision_type == DecisionType.ADOPT and option_id is not None:
            option = self.options.get(option_id)
            if option is not None and option.risks:
                if not accepted_risks:
                    raise ValueError(
                        f"方案 {option_id} 存在已知风险，采纳前必须逐项显式确认接受。"
                    )
                untracked = [risk for risk in accepted_risks if risk not in option.risks]
                if untracked:
                    raise ValueError(f"方案 {option_id} 未登记以下被接受的风险：{untracked}")
        if supersedes_decision_id is not None and supersedes_decision_id not in self.decisions:
            raise ValueError(f"被替代的决断不存在：{supersedes_decision_id}")
        decision = GeneralDecision(
            id=f"decision-{self.version + 1:03d}",
            decision_type=decision_type,
            option_id=option_id,
            reason=reason,
            scope=scope,
            source_version=self.version,
            actor_role=actor_role,
            decided_by=decided_by,
            proposal_id=proposal_id,
            accepted_risks=accepted_risks,
            supersedes_decision_id=supersedes_decision_id,
        )
        # Validate/build before either decision or draft is written. Adoption is
        # still separate from the legacy order chain and formal publication.
        draft = None
        if self.event_id and decision_type == DecisionType.ADOPT:
            if self.verify_audit_chain():
                raise ValueError('当前事件审计链待核对，不能生成方案任务草案。')
            option = self.options.get(option_id)
            if option is None:
                raise ValueError('已绑定事件的方案采纳必须引用已有方案。')
            draft = build_task_drafts(option, decision, self.event_id)
        self.decisions[decision.id] = decision
        self.append_event("GeneralDecisionRecorded", decision=asdict(decision))
        if draft is not None:
            self.task_drafts[draft.id] = draft
            self.append_event('TaskDraftBundleCreated', event_id=self.event_id,
                              decision_id=decision.id, bundle=asdict(draft))
        return decision

    def task_drafts_for_decision(self, decision_id: str, *, event_id: str) -> TaskDraftBundle | None:
        """Read the original draft without regenerating from a changed option."""
        if not self.event_id or event_id != self.event_id:
            raise ValueError('任务草案不属于当前事件。')
        if self.verify_audit_chain():
            raise ValueError('任务草案审计链待核对。')
        draft = self.task_drafts.get(f'task-draft-{decision_id}')
        if draft is None:
            return None  # Historical adoption is not silently backfilled.
        decision = self.decisions.get(decision_id)
        if (draft.event_id != event_id or draft.decision_id != decision_id
                or decision is None or decision.decision_type != DecisionType.ADOPT
                or decision.option_id != draft.option_id):
            raise ValueError('任务草案的事件或采纳依据不一致。')
        expected = json.dumps(asdict(draft), sort_keys=True)
        snapshots = [e.payload.get('bundle') for e in self.events
                     if e.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}
                     and e.payload.get('event_id') == event_id
                     and e.payload.get('decision_id') == decision_id]
        if not snapshots or json.dumps(snapshots[-1], sort_keys=True) != expected:
            raise ValueError('任务草案与来源审计记录不一致。')
        decision_json = json.dumps(asdict(decision), sort_keys=True)
        if not any(e.kind == 'GeneralDecisionRecorded'
                   and json.dumps(e.payload.get('decision'), sort_keys=True) == decision_json
                   for e in self.events):
            raise ValueError('采纳决断与审计记录不一致。')
        return draft

    def revise_task_drafts(self, decision_id: str, *, event_id: str,
                           expected_version: int, common: TaskCommonContent,
                           specs: dict[str, TaskDraftSpec], add_child: bool = False,
                           remove_child_id: str = '') -> TaskDraftBundle:
        self._require_general_entry()
        draft = self.task_drafts_for_decision(decision_id, event_id=event_id)
        if draft is None:
            raise ValueError('该采纳决断没有可编辑的本事件任务草案。')
        if draft.status != 'draft' or type(expected_version) is not int or expected_version != draft.version:
            raise ValueError('草案版本已变化或不可编辑，请刷新并核对后重试。')
        if not isinstance(common, TaskCommonContent) or not isinstance(specs, dict):
            raise ValueError('任务编制内容格式不正确。')
        if set(specs) != {task.id for task in draft.tasks} or any(
                not isinstance(spec, TaskDraftSpec) for spec in specs.values()):
            raise ValueError('任务列表已变化，请刷新草案后重试。')
        if add_child and remove_child_id:
            raise ValueError('一次只能新增或移除一个子任务。')
        if remove_child_id and not any(task.id == remove_child_id and task.parent_id for task in draft.tasks):
            raise ValueError('只能移除本草案的关联子任务。')
        tasks = tuple(TaskDraft(task.id, task.parent_id, specs[task.id])
                      for task in draft.tasks if task.id != remove_child_id)
        if add_child:
            tasks += (TaskDraft(f'{draft.id}-child-added-v{draft.version + 1}',
                                draft.tasks[0].id, TaskDraftSpec()),)
        if common == draft.common and tasks == draft.tasks:
            return draft
        from datetime import datetime, timezone
        updated = replace(draft, common=common, tasks=tasks, version=draft.version + 1,
                          updated_at=datetime.now(timezone.utc).isoformat())
        self.task_drafts[draft.id] = updated
        self.append_event('TaskDraftBundleRevised', event_id=event_id,
                          decision_id=decision_id, actor=GENERAL_ENTRY_ID,
                          bundle=asdict(updated))
        return updated

    def _load_order(self, order_id: str) -> ExecutionOrder:
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"未知执行命令：{order_id}")
        return order

    def _ensure_order_mutable(self, order: ExecutionOrder) -> None:
        if order.id.startswith('formal-'):
            raise PermissionError('新版正式任务须经具名任务服务变更。')
        if order.status in TERMINAL_ORDER_STATUSES:
            raise ValueError(f"命令 {order.id} 已终结（{_ord_cn(order.status)}），不可再变更状态。")

    def _refresh_expiry(self, order: ExecutionOrder) -> bool:
        """Expire the order in place if it is past its validity window; return whether it expired."""
        live = {
            OrderStatus.DRAFT,
            OrderStatus.PENDING_CONFIRM,
            OrderStatus.CONFIRMED,
            OrderStatus.ACTIVE,
            OrderStatus.PAUSED,
        }
        if (
            order.status in live
            and order.valid_until_version is not None
            and self.version > order.valid_until_version
        ):
            order.status = OrderStatus.EXPIRED
            self.append_event("OrderExpired", order_id=order.id, valid_until_version=order.valid_until_version)
            return True
        return False

    def draft_order(
        self,
        decision_id: str,
        *,
        drafted_by: str,
        drafter_role: AdvisorRole,
    ) -> ExecutionOrder:
        """Draft an order from a commander decision. A draft is not a command (doc 11 §3.5 rule 1)."""
        if not ROLE_PROFILES[drafter_role].can_draft_orders:
            raise PermissionError(f"角色 {_role_cn(drafter_role)} 无权起草执行命令。")
        decision = self.decisions[decision_id]
        if decision.actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员决断能产生执行命令。")
        if decision.decision_type not in ORDER_DECISION_TYPES:
            raise PermissionError("只有采纳、修改或指挥员自提的决断才能产生执行命令。")
        if not decision.option_id:
            raise ValueError("起草执行命令前，决断必须先指向一个方案。")
        option = self.options.get(decision.option_id)
        if option is None or option.status != "active":
            raise ValueError("执行命令只能针对在议方案起草。")
        self._reject_untrusted_basis(option)
        order = ExecutionOrder(
            id=f"order-{self.version + 1:03d}",
            decision_id=decision.id,
            option_id=option.id,
            scope=decision.scope,
            status=OrderStatus.DRAFT,
            drafted_by=drafted_by,
        )
        self.orders[order.id] = order
        self.append_event("ExecutionOrderDrafted", order=asdict(order))
        return order

    def submit_order_for_confirmation(self, order_id: str) -> ExecutionOrder:
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.DRAFT:
            raise ValueError(f"只有草稿命令可提交签发（当前：{_ord_cn(order.status)}）。")
        order.status = OrderStatus.PENDING_CONFIRM
        self.append_event("OrderSubmittedForConfirmation", order_id=order.id)
        return order

    def confirm_order(self, order_id: str, *, actor_role: DecisionActor, confirmed_by: str) -> ExecutionOrder:
        """Confirm a pending order. Drafting and confirming are separate acts (doc 11 §3.5 rule 2)."""
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.PENDING_CONFIRM:
            raise ValueError(f"只有待签发命令可签发（当前：{_ord_cn(order.status)}）。")
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or confirmed_by != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可签发执行命令。")
        order.status = OrderStatus.CONFIRMED
        order.confirmed_by = confirmed_by
        self.append_event("OrderConfirmed", order_id=order.id, confirmed_by=confirmed_by)
        # Rule 3: when a new command for the same objective is confirmed, the old
        # active commands are atomically superseded, audit recorded in the same transaction.
        superseded = [
            other
            for other in self.orders.values()
            if other.id != order.id and other.status == OrderStatus.ACTIVE
        ]
        for other in superseded:
            other.status = OrderStatus.SUPERSEDED
            self.append_event("OrderSuperseded", order_id=other.id, superseded_by=order.id)
        return order

    def activate_order(self, order_id: str) -> ExecutionOrder:
        """Activate a confirmed order through the policy/approval gate."""
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.CONFIRMED:
            raise ValueError(f"只有已签发命令可生效（当前：{_ord_cn(order.status)}）。")
        contract = self._latest_contract(order_id)
        if contract is None:
            raise ValueError(
                f"命令 {order_id} 没有行动契约；执行器拒绝无契约的作业。"
            )
        check = self.run_policy_checks(order_id)
        if not check.passed:
            raise ValueError(
                f"命令 {order_id} 政策核查未通过：{'、'.join(check.failed_rules)}。"
            )
        if contract.risk_level >= RiskLevel.L3 and self._valid_approval(contract) is None:
            raise PermissionError(
                f"高风险行动「{contract.action_type}」须先取得有效批准才能生效。"
            )
        order.status = OrderStatus.ACTIVE
        self.append_event("OrderActivated", order_id=order.id)
        return order

    def issue_order(self, decision_id: str, *, drafted_by: str = "DispatchChiefAgent") -> ExecutionOrder:
        """Full-chain convenience path used by the offline scenario:
        draft -> contract -> pending_confirm -> confirmed -> policy/approval -> active."""
        decision = self.decisions[decision_id]
        order = self.draft_order(
            decision_id,
            drafted_by=drafted_by,
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
        )
        self.draft_action_contract(
            order.id,
            drafted_by=drafted_by,
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=RiskLevel.L2,
            capability_requirements=("simulated-world",),
            parameters=(("option_id", order.option_id), ("decision_id", decision.id)),
            idempotency_key=f"{order.id}:{order.option_id}",
        )
        self.submit_order_for_confirmation(order.id)
        self.confirm_order(order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID)
        return self.activate_order(order.id)

    def draft_action_contract(
        self,
        order_id: str,
        *,
        drafted_by: str,
        drafter_role: AdvisorRole,
        action_type: str,
        risk_level: RiskLevel,
        capability_requirements: tuple[str, ...] = (),
        parameters: tuple[tuple[str, str], ...] = (),
        side_effect_boundary: str = "simulated-world-only",
        idempotency_key: str = "",
    ) -> ActionContract:
        """Draft the structured action protocol for an order (doc 11 §3.5)."""
        if not ROLE_PROFILES[drafter_role].can_draft_orders:
            raise PermissionError(f"角色 {_role_cn(drafter_role)} 无权起草行动契约。")
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status not in {
            OrderStatus.DRAFT,
            OrderStatus.PENDING_CONFIRM,
            OrderStatus.CONFIRMED,
            OrderStatus.PAUSED,
        }:
            raise ValueError(f"行动契约只能在生效前或暂停中起草（当前：{_ord_cn(order.status)}）。")
        if not action_type:
            raise ValueError("行动契约必须写明动作类型。")
        contract = ActionContract(
            id=f"contract-{self.version + 1:03d}",
            order_id=order.id,
            action_type=action_type,
            risk_level=risk_level,
            capability_requirements=capability_requirements,
            parameters=tuple(parameters),
            side_effect_boundary=side_effect_boundary,
            idempotency_key=idempotency_key,
            drafted_by=drafted_by,
            source_version=self.version,
        )
        self.action_contracts[contract.id] = contract
        self.append_event("ActionContractDrafted", contract=asdict(contract))
        return contract

    def _latest_contract(self, order_id: str) -> ActionContract | None:
        candidates = [
            contract
            for contract in self.action_contracts.values()
            if contract.order_id == order_id
        ]
        return max(candidates, key=lambda contract: contract.source_version, default=None)

    def run_policy_checks(self, order_id: str) -> PolicyCheckResult:
        """Capability/risk/policy gate between confirmed and active (doc 11 §3.5 chain)."""
        contract = self._latest_contract(order_id)
        if contract is None:
            raise ValueError(f"命令 {order_id} 尚无行动契约；请先起草再生效。")
        failed: list[str] = []
        for rule_id, (_description, evaluate) in POLICY_RULES.items():
            if not evaluate(self, contract):
                failed.append(rule_id)
        result = PolicyCheckResult(
            id=f"policy-{self.version + 1:03d}",
            order_id=order_id,
            contract_id=contract.id,
            passed=not failed,
            failed_rules=tuple(failed),
            checked_rules=tuple(POLICY_RULES),
            source_version=self.version,
        )
        self.policy_checks[result.id] = result
        self.append_event("PolicyCheckFinished", check=asdict(result))
        return result

    def record_approval(
        self,
        action_contract_id: str,
        *,
        actor_role: DecisionActor,
        approver: str,
        valid_until_version: int | None = None,
    ) -> Approval:
        """Record a commander approval bound to action, object, risk tier, and validity."""
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or approver != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可授予批准。")
        contract = self.action_contracts.get(action_contract_id)
        if contract is None:
            raise KeyError(f"未知行动契约：{action_contract_id}")
        approval = Approval(
            id=f"approval-{self.version + 1:03d}",
            action_contract_id=contract.id,
            order_id=contract.order_id,
            action_type=contract.action_type,
            risk_level=contract.risk_level,
            approver=approver,
            actor_role=actor_role,
            valid_until_version=valid_until_version,
            source_version=self.version,
        )
        self.approvals[approval.id] = approval
        self.append_event("ApprovalRecorded", approval=asdict(approval))
        return approval

    def _valid_approval(self, contract: ActionContract) -> Approval | None:
        for approval in self.approvals.values():
            if approval.action_contract_id != contract.id:
                continue
            if approval.valid_until_version is None or self.version <= approval.valid_until_version:
                return approval
        return None

    def _reject_untrusted_basis(self, option: Option) -> None:
        """Doc 11 §3.4: untrusted content must never be the basis of order drafting."""
        evidence_ids = set(option.evidence_ids)
        for subject in option.claim_subjects:
            claim = self.claims.get(subject)
            if claim is not None:
                evidence_ids.update(claim.evidence_ids)
        untrusted = [
            evidence_id
            for evidence_id in sorted(evidence_ids)
            if evidence_id in self.evidence and self.evidence[evidence_id].trust_level == SourceTrustLevel.UNTRUSTED
        ]
        if untrusted:
            raise PermissionError(
                f"方案 {option.id} 的命令起草依赖了不可信内容：{untrusted}。请先核验来源或取得授权证据。"
            )

    def pause_order(self, order_id: str, reason: str, *, actor_role: DecisionActor) -> None:
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员可暂停执行命令。")
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.ACTIVE:
            raise ValueError(f"只有执行中的命令可暂停（当前：{_ord_cn(order.status)}）。")
        order.status = OrderStatus.PAUSED
        self.append_event("OrderPaused", order_id=order_id, reason=reason)

    def resume_order(self, order_id: str, *, actor_role: DecisionActor) -> None:
        """Resume a paused order after re-checking command AND approval validity (doc 11 §3.5 rule 5)."""
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员可恢复执行命令。")
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.PAUSED:
            raise ValueError(f"只有已暂停命令可恢复（当前：{_ord_cn(order.status)}）。")
        if self._refresh_expiry(order):
            raise ValueError(f"命令 {order_id} 暂停期间已过期，无法恢复。")
        contract = self._latest_contract(order_id)
        if contract is not None and contract.risk_level >= RiskLevel.L3 and self._valid_approval(contract) is None:
            raise PermissionError(
                f"命令 {order_id} 无法恢复：其高风险批准已过期，不得复用。"
            )
        order.status = OrderStatus.ACTIVE
        self.append_event("OrderResumed", order_id=order_id)

    def pause_order_for_anomaly(self, order_id: str, reason: str, report_id: str | None = None) -> None:
        """Safety interlock: freeze an order on an execution anomaly.

        This is not a staff decision and decides no strategy — it only stops the
        world and leaves an audit trail; the general is asked what is next.
        """
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        if order.status != OrderStatus.ACTIVE:
            raise ValueError(f"只有执行中的命令可因异常暂停（当前：{_ord_cn(order.status)}）。")
        order.status = OrderStatus.PAUSED
        self.append_event("OrderAnomalyPaused", order_id=order_id, reason=reason, report_id=report_id)

    def cancel_order(self, order_id: str, reason: str, *, actor_role: DecisionActor) -> None:
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER:
            raise PermissionError("只有指挥员可取消执行命令。")
        order = self._load_order(order_id)
        self._ensure_order_mutable(order)
        order.status = OrderStatus.CANCELLED
        self.append_event("OrderCancelled", order_id=order_id, reason=reason)

    def record_execution(
        self,
        order_id: str,
        status: AttemptStatus,
        observations: tuple[str, ...],
        *,
        action_contract_id: str | None = None,
        idempotency_key: str = "",
    ) -> ExecutionReport:
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"未知执行命令：{order_id}")
        if self._refresh_expiry(order):
            raise PermissionError("模拟动作需要执行中的命令；该命令已过期。")
        if order.status != OrderStatus.ACTIVE:
            raise PermissionError("模拟动作需要执行中的命令。")
        report = ExecutionReport(
            id=f"report-{self.version + 1:03d}",
            order_id=order_id,
            status=status,
            observations=observations,
            action_contract_id=action_contract_id,
            idempotency_key=idempotency_key,
        )
        self.reports[report.id] = report
        self.append_event("ExecutionSimulated", report=asdict(report))
        return report

    def record_verification(self, passed: bool, unmet_conditions: tuple[str, ...], evidence_ids: tuple[str, ...]) -> VerificationResult:
        result = VerificationResult(
            id=f"verification-{self.version + 1:03d}",
            passed=passed,
            unmet_conditions=unmet_conditions,
            evidence_ids=evidence_ids,
        )
        self.verifications[result.id] = result
        self.append_event("VerificationFinished", verification=asdict(result))
        return result

    def replay(self) -> dict[str, Any]:
        """Return a read-only reconstruction summary without invoking any executor."""
        by_kind: dict[str, int] = defaultdict(int)
        for event in self.events:
            by_kind[event.kind] += 1
        return {
            "scenario_id": self.scenario_id,
            "event_count": len(self.events),
            "event_kinds": dict(by_kind),
            "claim_statuses": {subject: claim.status.value for subject, claim in self.claims.items()},
            "orders": {order_id: order.status.value for order_id, order in self.orders.items()},
            "topic_count": len(self.topics),
            "proposal_count": len(self.proposals),
            "feedback_count": len(self.feedback),
            "intel_count": len(self.intel_items),
            "evidence_count": len(self.evidence),
            "staff_plan_count": len(self.staff_plans),
            "staff_task_count": len(self.staff_tasks),
            "open_staff_tasks": tuple(
                task_id
                for task_id, task in self.staff_tasks.items()
                if task.status not in {StaffTaskStatus.COMPLETED, StaffTaskStatus.FAILED, StaffTaskStatus.CANCELLED}
            ),
            "open_information_gaps": tuple(
                gap.subject
                for gap in self.information_gaps.values()
                if gap.status == InformationGapStatus.OPEN
            ),
        }

    def commander_briefing(self) -> dict[str, Any]:
        """Return the read-only shared view a commander needs for a decision."""
        collection_event_kinds = {
            "IntelItemRecorded",
            "IntelReferenced",
            "AssessmentSubmitted",
            "InformationGapOpened",
            "InformationGapReferenced",
            "InformationGapResolved",
            "CommanderFactReceived",
        }
        return {
            "scenario_id": self.scenario_id,
            "collection_events": tuple(
                asdict(event) for event in self.events if event.kind in collection_event_kinds
            ),
            "intelligence": tuple(asdict(item) for item in self.intel_items.values()),
            "open_information_gaps": tuple(
                asdict(gap)
                for gap in self.information_gaps.values()
                if gap.status == InformationGapStatus.OPEN
            ),
            "resolved_information_gaps": tuple(
                asdict(gap)
                for gap in self.information_gaps.values()
                if gap.status == InformationGapStatus.RESOLVED
            ),
            "assessments": tuple(asdict(item) for item in self.assessments),
            "disagreements": tuple(asdict(item) for item in self.disagreements),
            "options": tuple(asdict(item) for item in self.options.values()),
            "proposals": tuple(asdict(item) for item in self.proposals.values()),
            "feedback": tuple(asdict(item) for item in self.feedback.values()),
        }

    def _resolve_information_gaps(self, claim: Claim) -> None:
        for gap in self.information_gaps.values():
            if gap.subject == claim.subject and gap.status == InformationGapStatus.OPEN:
                gap.status = InformationGapStatus.RESOLVED
                gap.resolved_by_claim_id = claim.id
                self.append_event(
                    "InformationGapResolved",
                    gap_id=gap.id,
                    claim_id=claim.id,
                )

    def _mark_options_affected(self, subject: str) -> None:
        current_claim = self.claims[subject]
        if current_claim.status not in {ClaimStatus.CHALLENGED, ClaimStatus.SUPERSEDED}:
            return
        for option in self.options.values():
            if subject in option.claim_subjects and option.status == "active":
                option.status = "affected"
                self.append_event("OptionAffected", option_id=option.id, subject=subject)

    # ------------------------------------------------------------------
    # Goal frames, decision briefs, and downstream invalidation (P1)

    def record_goal_frame(self, frame: GoalFrame) -> GoalFrame:
        if frame.id in self.goal_frames:
            raise ValueError(f"目标框架已存在：{frame.id}")
        self.goal_frames[frame.id] = frame
        self.append_event("GoalFrameRecorded", goal_frame=asdict(frame))
        return frame

    def confirm_goal_frame(self, goal_frame_id: str, *, actor_role: DecisionActor, confirmed_by: str) -> GoalFrame:
        frame = self.goal_frames.get(goal_frame_id)
        if frame is None:
            raise KeyError(f"目标框架不存在：{goal_frame_id}")
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or confirmed_by != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可确认目标框架。")
        confirmed = GoalFrame(
            id=frame.id,
            statement=frame.statement,
            constraints=frame.constraints,
            success_criteria=frame.success_criteria,
            priority=frame.priority,
            confirmed_by=confirmed_by,
            source_version=self.version,
        )
        self.goal_frames[goal_frame_id] = confirmed
        self.append_event("GoalFrameConfirmed", goal_frame_id=goal_frame_id, confirmed_by=confirmed_by)
        return confirmed

    def record_decision_brief(self, brief: DecisionBrief) -> DecisionBrief:
        if brief.id in self.decision_briefs:
            raise ValueError(f"决策简报已存在：{brief.id}")
        if brief.topic_id not in self.topics:
            raise ValueError("决策简报必须挂在已有议题上。")
        missing_options = [option_id for option_id in brief.option_ids if option_id not in self.options]
        if missing_options:
            raise ValueError(f"决策简报引用了不存在的方案：{missing_options}")
        self.decision_briefs[brief.id] = brief
        self.append_event("DecisionBriefComposed", decision_brief=asdict(brief))
        return brief

    def invalidate_downstream(self, reason: str) -> tuple[str, ...]:
        """Invalidate active proposals and options after a formal correction (doc 11 §7 P1).

        Returns the ids of invalidated objects; audit events are appended in the same transaction.
        """
        invalidated: list[str] = []
        for proposal in self.proposals.values():
            if proposal.status == "active":
                proposal.status = "superseded"
                invalidated.append(proposal.id)
                self.append_event("ProposalSuperseded", proposal_id=proposal.id, reason=reason)
        for option in self.options.values():
            if option.status == "active":
                option.status = "affected"
                invalidated.append(option.id)
                self.append_event("OptionAffected", option_id=option.id, subject=f"invalidation:{reason}")
        return tuple(invalidated)

    def raise_general_question(self, question: GeneralQuestion) -> GeneralQuestion:
        """Hand a decision back to the general instead of deciding autonomously."""
        if question.id in self.general_questions:
            raise ValueError(f"请示已存在：{question.id}")
        self.general_questions[question.id] = question
        self.append_event("GeneralQuestionRaised", question=asdict(question))
        return question

    def answer_general_question(
        self,
        question_id: str,
        answer: str,
        *,
        actor_role: DecisionActor,
        answered_by: str,
    ) -> GeneralQuestion:
        """Answering is a general act routed through the authenticated entry."""
        question = self.general_questions.get(question_id)
        if question is None:
            raise KeyError(f"请示不存在：{question_id}")
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or answered_by != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可答复请示。")
        if question.status != "open":
            raise ValueError(f"请示 {question_id} 当前状态为「{'已答复' if question.status != 'open' else '待答复'}」。")
        answered = GeneralQuestion(
            id=question.id,
            kind=question.kind,
            subject=question.subject,
            question=question.question,
            context=question.context,
            status="answered",
            answer=answer,
            source_version=self.version,
        )
        self.general_questions[question_id] = answered
        self.append_event("GeneralQuestionAnswered", question_id=question_id, answer=answer)
        return answered

    # ------------------------------------------------------------------
    # After-action review and controlled memory (doc 11 §7 P5)

    _TIMELINE_KINDS = (
        "GeneralDecisionRecorded",
        "ExecutionOrderDrafted",
        "ActionContractDrafted",
        "OrderSubmittedForConfirmation",
        "OrderConfirmed",
        "PolicyCheckFinished",
        "ApprovalRecorded",
        "OrderActivated",
        "ExecutionSimulated",
        "OrderAnomalyPaused",
        "GeneralQuestionRaised",
        "VerificationFinished",
    )

    def timeline(self) -> tuple[dict[str, Any], ...]:
        """Full decision/execution/verification replay, in audit order."""
        return tuple(
            {"sequence": event.sequence, "kind": event.kind, "payload": event.payload}
            for event in self.events
            if event.kind in self._TIMELINE_KINDS
        )

    def record_review(self, review: Review) -> Review:
        if review.id in self.reviews:
            raise ValueError(f"复盘已存在：{review.id}")
        self.reviews[review.id] = review
        self.append_event("ReviewRecorded", review=asdict(review))
        return review

    def propose_memory_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        if candidate.id in self.memory_candidates:
            raise ValueError(f"经验候选已存在：{candidate.id}")
        if candidate.source_review_id not in self.reviews:
            raise ValueError("经验候选必须挂在已有复盘上。")
        self.memory_candidates[candidate.id] = candidate
        self.append_event("MemoryCandidateProposed", candidate=asdict(candidate))
        return candidate

    def confirm_memory_candidate(
        self,
        candidate_id: str,
        *,
        actor_role: DecisionActor,
        confirmed_by: str,
        reject: bool = False,
    ) -> MemoryCandidate:
        """Only the authenticated general entry can promote experience."""
        self._require_general_entry()
        if actor_role != DecisionActor.COMMANDER or confirmed_by != GENERAL_ENTRY_ID:
            raise PermissionError("只有已认证的指挥入口可确认经验入库。")
        candidate = self.memory_candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"经验候选不存在：{candidate_id}")
        if candidate.status != MemoryCandidateStatus.PROPOSED:
            raise ValueError(f"经验候选 {candidate_id} 已不在待确认状态。")
        confirmed = MemoryCandidate(
            id=candidate.id,
            content=candidate.content,
            source_review_id=candidate.source_review_id,
            applicability_boundary=candidate.applicability_boundary,
            status=MemoryCandidateStatus.REJECTED if reject else MemoryCandidateStatus.CONFIRMED,
            confirmed_by=confirmed_by,
            source_version=self.version,
        )
        self.memory_candidates[candidate_id] = confirmed
        self.append_event(
            "MemoryCandidateRejected" if reject else "MemoryCandidateConfirmed",
            candidate_id=candidate_id,
            confirmed_by=confirmed_by,
        )
        return confirmed

    def record_orchestration_feedback(self, feedback: OrchestrationFeedback) -> OrchestrationFeedback:
        self.orchestration_feedbacks.append(feedback)
        self.append_event("OrchestrationFeedbackRecorded", feedback=asdict(feedback))
        return feedback

    # ------------------------------------------------------------------
    # Force organization (P15 T1): standing library + QR-scan task roster

    def import_person_library(self, records) -> int:
        """Upsert standing-library entries keyed by normalized phone. Returns batch size."""
        normalized: dict[str, PersonRecord] = {}
        for record in records:
            phone = normalize_phone(record.phone)
            if not record.name.strip():
                raise ValueError(f"人员姓名不能为空（phone={phone}）")
            normalized[phone] = PersonRecord(
                name=record.name.strip(),
                phone=phone,
                unit=record.unit.strip(),
                home_position=record.home_position.strip(),
                specialty=record.specialty.strip(),
                is_leader=bool(record.is_leader),
            )
        if not normalized:
            raise ValueError("人员库导入批次为空。")
        self.person_library.update(normalized)
        self.append_event(
            "PersonLibraryImported",
            count=len(normalized),
            phones=tuple(sorted(normalized)),
        )
        return len(normalized)

    def scan_check_in(self, pos_code: str, *, phone: str, operator: str = "qrcode") -> RosterBinding:
        """Bind a registered person to a position slot (QR scan check-in)."""
        slot = SLOT_INDEX.get(pos_code)
        if slot is None:
            raise KeyError(f"未知岗位槽位：{pos_code}")
        phone_n = normalize_phone(phone)
        person = self.person_library.get(phone_n)
        if person is None:
            raise KeyError(f"该手机号未登记常备人员库，请先补录：{mask_phone(phone_n)}")
        active_bindings = [b for b in self.roster_bindings.values() if b.status == "active"]
        if any(b.person.phone == phone_n for b in active_bindings):
            held = next(b.pos_code for b in active_bindings if b.person.phone == phone_n)
            raise ValueError(f"{person.name} 已在岗（{held}），一人一岗不可重复报到。")
        occupied = [b for b in active_bindings if b.pos_code == pos_code]
        if len(occupied) >= slot.max_headcount:
            raise ValueError(f"{slot.name} 岗位已满（{slot.max_headcount} 人），请联系联络员调整。")
        if person.is_leader and any(b.person.is_leader for b in occupied):
            raise ValueError(f"{slot.name} 已有队长在岗，不可重复指定。")
        next_seq = len(self.roster_bindings) + 1
        binding_id = f"rbs-{next_seq:03d}"
        while binding_id in self.roster_bindings:
            next_seq += 1
            binding_id = f"rbs-{next_seq:03d}"
        binding = RosterBinding(
            id=binding_id,
            pos_code=pos_code,
            person=person,
            status="active",
            bound_at_version=self.version,
        )
        self.roster_bindings[binding_id] = binding
        from .fire_events import utc_now_iso  # 延迟导入避免环依赖

        self.append_event(
            "PersonCheckedIn",
            binding_id=binding_id,
            pos_code=pos_code,
            operator=operator,
            person=asdict(person),
            at=utc_now_iso(),  # P16-T15：签到时间入链（增量键，旧消费方不受影响）
        )
        return binding

    def check_out(self, pos_code: str, *, phone: str) -> RosterBinding:
        """Release a person from a position slot; the binding stays auditable."""
        phone_n = normalize_phone(phone)
        binding = next(
            (
                b
                for b in self.roster_bindings.values()
                if b.pos_code == pos_code and b.person.phone == phone_n and b.status == "active"
            ),
            None,
        )
        if binding is None:
            raise KeyError(f"{pos_code} 无此在岗人员：{mask_phone(phone_n)}")
        departed = RosterBinding(
            id=binding.id,
            pos_code=binding.pos_code,
            person=binding.person,
            status="checked_out",
            bound_at_version=binding.bound_at_version,
            checked_out_at_version=self.version,
        )
        self.roster_bindings[binding.id] = departed
        self.append_event(
            "PersonCheckedOut",
            binding_id=binding.id,
            pos_code=pos_code,
            phone=phone_n,
        )
        return departed

    def org_chart(self) -> dict[str, Any]:
        """Renderable org-tree snapshot: command nodes, slots, live members."""
        members_by_slot: dict[str, list[dict[str, Any]]] = {}
        filled_slots = 0
        total_members = 0
        for binding in sorted(self.roster_bindings.values(), key=lambda item: item.id):
            if binding.status != "active":
                continue
            members_by_slot.setdefault(binding.pos_code, []).append(
                {
                    "name": binding.person.name,
                    "phone": binding.person.phone,
                    "unit": binding.person.unit,
                    "is_leader": binding.person.is_leader,
                    "binding_id": binding.id,
                }
            )
        slots_by_domain: dict[str, list[dict[str, Any]]] = {}
        for slot in POSITION_SLOTS:
            members = members_by_slot.get(slot.pos_code, ())
            if members:
                filled_slots += 1
                total_members += len(members)
            slots_by_domain.setdefault(slot.domain, []).append(
                {
                    "pos_code": slot.pos_code,
                    "name": slot.name,
                    "max_headcount": slot.max_headcount,
                    "filled": len(members),
                    "members": tuple(members),
                }
            )
        nodes: list[dict[str, Any]] = []
        for node in COMMAND_NODES:
            node_slots: list[dict[str, Any]] = []
            for domain in sorted(_DOMAIN_PARENT):
                if _DOMAIN_PARENT[domain] == node.node_code:
                    node_slots.extend(slots_by_domain.get(domain, ()))
            nodes.append(
                {
                    "node_code": node.node_code,
                    "name": node.name,
                    "parent": node.parent,
                    "slots": tuple(node_slots),
                }
            )
        return {
            "nodes": tuple(nodes),
            "filled_slots": filled_slots,
            "total_slots": len(POSITION_SLOTS),
            "total_members": total_members,
        }

    def force_posture_report(self) -> dict[str, object]:
        """Advisory force-posture checks over currently active roster members."""
        active = tuple(b for b in self.roster_bindings.values() if b.status == "active")
        return force_posture(active)

    # ------------------------------------------------------------------
    # Sandtable annotations (P16-A)

    def _capture_annotation_history(self, annotation: MapAnnotation, operation: str) -> None:
        from .map_history import capture_annotation
        capture_annotation(self, annotation, operation)

    def record_annotation(self, annotation: MapAnnotation) -> MapAnnotation:
        """Draft a new mark on the sandtable (staff work, no general gate).

        AI-suggested marks are born ``pending_review`` — an algorithm can
        never publish into the shared picture by itself.
        """
        if annotation.id in self.annotations:
            raise ValueError(f"标绘编号已存在：{annotation.id}")
        if annotation.source.value == "ai-suggestion" and annotation.status not in {
            AnnotationStatus.DRAFT,
            AnnotationStatus.PENDING_REVIEW,
        }:
            raise ValueError("自动建议标绘只能以草稿/待核验状态入库")
        # P19 逐类闸门：直接以较高状态入库也必须先过字段与发布校验。
        if annotation.kind in TACTICAL_KINDS and annotation.status is not AnnotationStatus.DRAFT:
            if annotation.status is AnnotationStatus.PENDING_REVIEW:
                errors = tm.verify_gate(annotation)
                if errors:
                    raise ValueError("；".join(errors))
            elif annotation.status in {
                AnnotationStatus.VERIFIED,
                AnnotationStatus.PUBLISHED,
            }:
                errors = tm.publish_gate(
                    annotation, siblings=self.annotations.values(),
                    conflicts=self.tactical_conflicts(),
                )
                if errors:
                    raise ValueError("；".join(errors))
        self._capture_annotation_history(annotation, 'record')
        self.annotations[annotation.id] = annotation
        self.append_event(
            "AnnotationRecorded",
            annotation_id=annotation.id,
            annotation_kind=annotation.kind.value,
            status=annotation.status.value,
            source=annotation.source.value,
            certainty=annotation.certainty,
            title=annotation.title,
        )
        return annotation

    def set_annotation_status(
        self, annotation_id: str, target: AnnotationStatus
    ) -> MapAnnotation:
        """Move a mark along draft -> pending_review -> verified -> published.

        Verification, publication and withdrawal are authorized acts: they
        require the general entry. Import is not verification — GeoJSON
        marks always land as drafts (map_annotations discipline).
        """
        annotation = self.annotations.get(annotation_id)
        if annotation is None:
            raise KeyError(f"标绘不存在：{annotation_id}")
        if target is annotation.status:
            return annotation
        if not annotation_can_transition(annotation.status, target):
            raise ValueError(
                f"标绘状态不允许从 {annotation.status.value} 变为 {target.value}"
            )
        if target in {
            AnnotationStatus.VERIFIED,
            AnnotationStatus.PUBLISHED,
            AnnotationStatus.WITHDRAWN,
        }:
            self._require_general_entry()
        now = self._audit_now()
        if target is AnnotationStatus.PENDING_REVIEW:
            errors = tm.verify_gate(annotation)
            if errors:
                raise ValueError("；".join(errors))
        if target is AnnotationStatus.VERIFIED:
            errors = tm.verify_gate(annotation)
            if errors:
                raise ValueError("；".join(errors))
        if target is AnnotationStatus.PUBLISHED:
            errors = tm.publish_gate(
                annotation, siblings=self.annotations.values(),
                conflicts=self.tactical_conflicts(), now=now,
            )
            if errors:
                raise ValueError("；".join(errors))
        updated = annotation.with_status(target)
        if target is AnnotationStatus.VERIFIED:
            updated = replace(
                updated, verified_at=updated.verified_at or now,
                verification_record={"by": updated.created_by, "at": now,
                                     "conclusion": "已核验", "basis": "指挥端核验动作"},
                freshness_status=tm.FreshnessStatus.OK.value,
                updated_at=now,
            )
        elif target is AnnotationStatus.PUBLISHED:
            updated = replace(
                updated, published_at=now, updated_at=now,
                publication_record={"by": updated.created_by, "at": now,
                                    "conclusion": "已发布进入共享态势"},
            )
        elif target is AnnotationStatus.WITHDRAWN:
            updated = replace(updated, updated_at=now, withdrawn_at=now)
        self._capture_annotation_history(updated, 'status')
        self.annotations[annotation_id] = updated
        self.append_event(
            "AnnotationStatusChanged",
            annotation_id=annotation_id,
            annotation_version=updated.version,
            annotation_kind=updated.kind.value,
            from_status=annotation.status.value,
            to_status=target.value,
            changed_by=updated.created_by,
        )
        return updated

    def set_annotation_geo(
        self, annotation_id: str, geo_points: tuple[tuple[float, float], ...]
    ) -> MapAnnotation:
        """录入/更新标绘地理坐标（doc 03 §6 点线面上真图）。

        坐标与顶点一一对应，不做自动投影；留审计事件。
        """
        annotation = self.annotations.get(annotation_id)
        if annotation is None:
            raise KeyError(f"标绘不存在：{annotation_id}")
        validate_geo_points(annotation.points, geo_points)
        if self.event_id and annotation.status is AnnotationStatus.PUBLISHED:
            raise ValueError('Published map geometry requires a revision draft')
        updated = annotation.with_geo_points(geo_points)
        if updated == annotation:
            return annotation
        self._capture_annotation_history(updated, 'geometry')
        self.annotations[annotation_id] = updated
        self.append_event(
            "AnnotationGeoSet",
            annotation_id=annotation_id,
            vertex_count=len(updated.points),
            geo_count=len(geo_points),
        )
        return updated

    def publish_verified_annotations(self) -> tuple[str, ...]:
        """Publish every VERIFIED mark into the shared picture (one act)."""
        published: list[str] = []
        for annotation_id in sorted(self.annotations):
            annotation = self.annotations[annotation_id]
            if annotation.status is AnnotationStatus.VERIFIED:
                self.set_annotation_status(annotation_id, AnnotationStatus.PUBLISHED)
                published.append(annotation_id)
        return tuple(published)

    # ------------------------------------------------------------------
    # P19 战术态势：时效、冲突记录、修订草稿与命令战术层（docs/19 定稿）

    def _audit_now(self) -> str:
        from .fire_events import utc_now_iso       # 延迟导入避免环依赖

        return utc_now_iso()

    def tactical_conflicts(self, *, now: str = "") -> tuple[tm.ConflictRecord, ...]:
        """只读扫描当前标绘的路线冲突；不落库、不改对象状态。"""
        return tm.detect_conflicts(
            self.annotations.values(), now=now or self._audit_now()
        )

    @staticmethod
    def _conflict_key(rec: tm.ConflictRecord) -> tuple:
        return (
            rec.route_id, rec.route_version,
            rec.trigger_annotation_id, rec.trigger_version, rec.conflict_type,
        )

    def refresh_conflict_records(self, *, now: str = "") -> tuple[tm.ConflictRecord, ...]:
        """把扫描结果写成独立冲突记录（同键不重复；已关闭的不重开）。"""
        stamp = now or self._audit_now()
        known = {self._conflict_key(r): r for r in self.conflict_records.values()}
        added: list[tm.ConflictRecord] = []
        for rec in tm.detect_conflicts(self.annotations.values(), now=stamp):
            if self._conflict_key(rec) in known:
                continue
            cid = f"cfl-{len(self.conflict_records) + 1:03d}"
            stored = replace(rec, conflict_id=cid, detected_at=stamp)
            self.conflict_records[cid] = stored
            added.append(stored)
            self.append_event(
                "RouteConflictDetected",
                conflict_id=cid,
                route_id=stored.route_id,
                route_version=stored.route_version,
                trigger_annotation_id=stored.trigger_annotation_id,
                conflict_type=stored.conflict_type,
                level=stored.level,
                detected_at=stamp,
            )
        return tuple(added)

    _CONFLICT_CLOSURES = {
        "路线已修订", "风险对象已重新核验", "阻断已解除", "集结点已恢复",
        "司令决断已下达", "任务已取消",
    }

    def resolve_conflict(
        self, conflict_id: str, *, by: str, reason: str, resolution: str, at: str = ""
    ) -> tm.ConflictRecord:
        """冲突只能由明确业务结果关闭：改图层、点已查看、刷新页面无效。"""
        rec = self.conflict_records.get(conflict_id)
        if rec is None:
            raise KeyError(f"冲突记录不存在：{conflict_id}")
        self._require_general_entry()
        if rec.status == tm.ConflictStatus.RESOLVED.value:
            return rec
        if resolution not in self._CONFLICT_CLOSURES:
            raise ValueError(
                "冲突关闭必须给出明确业务结果："
                + "、".join(sorted(self._CONFLICT_CLOSURES))
            )
        if not reason.strip():
            raise ValueError("冲突关闭必须写明依据")
        stamp = at or self._audit_now()
        closed = replace(
            rec, status=tm.ConflictStatus.RESOLVED.value, handled_by=by,
            handled_at=stamp, resolution_reason=f"{resolution}：{reason}",
        )
        self.conflict_records[conflict_id] = closed
        self.append_event(
            "RouteConflictResolved",
            conflict_id=conflict_id,
            route_id=closed.route_id,
            resolution=resolution,
            handled_by=by,
            handled_at=stamp,
        )
        return closed

    def open_conflicts_for(self, route_id: str) -> tuple[tm.ConflictRecord, ...]:
        return tuple(
            r for r in sorted(self.conflict_records.values(), key=lambda x: x.conflict_id)
            if r.route_id == route_id and r.status == tm.ConflictStatus.OPEN.value
        )

    def update_annotation(
        self,
        annotation_id: str,
        *,
        title: str = "",
        note: str | None = None,
        details: dict[str, str] | None = None,
        responsible_unit: str | None = None,
        responsible_person: str | None = None,
        basis: tuple[str, ...] | None = None,
        source_ref: tuple[str, ...] | None = None,
        route_status: str | None = None,
        positioning_status: str | None = None,
        is_demo: bool | None = None,
        demo_notice: str | None = None,
        base_version: int | None = None,
        as_revision: bool = False,
    ) -> MapAnnotation:
        """标绘编辑：已发布对象必须形成修订草稿，原版本保留历史。

        `base_version` 不一致时拒收旧页面提交（定稿 §6.2 并发规则）。
        """
        annotation = self.annotations.get(annotation_id)
        if annotation is None:
            raise KeyError(f"标绘不存在：{annotation_id}")
        if base_version is not None and base_version != annotation.version:
            raise WorkspaceConflictError(
                f"对象版本已变化：提交基于 v{base_version}，当前 v{annotation.version}。"
                "请重新加载后修订。",
                expected_version=base_version,
                current_version=annotation.version,
            )
        changes: dict[str, object] = {}
        if title.strip():
            changes["title"] = title.strip()
        for key, value in (
            ("note", note), ("responsible_unit", responsible_unit),
            ("responsible_person", responsible_person),
            ("route_status", route_status), ("positioning_status", positioning_status),
            ("demo_notice", demo_notice),
        ):
            if value is not None:
                changes[key] = value
        if basis is not None:
            changes["basis"] = tuple(basis)
        if source_ref is not None:
            changes["source_ref"] = tuple(source_ref)
        if is_demo is not None:
            changes["is_demo"] = bool(is_demo)
        merged = dict(annotation.details)
        for key, value in (details or {}).items():
            text = str(value).strip()
            if not tm.detail_value_ok(text):
                continue          # 空值与“见备注”都不能写入专属字段
            merged[str(key)] = text
        if merged != annotation.details:
            changes["details"] = merged
        if not changes:
            return annotation
        if annotation.status is AnnotationStatus.PUBLISHED and not as_revision:
            raise ValueError("已发布对象不能直接修改：请建修订草稿（as_revision）后再走核验链")
        updated = replace(annotation, **changes)
        updated = replace(updated, version=annotation.version + 1,
                          updated_at=self._audit_now())
        if annotation.tactical:
            fresh = tm.freshness_of(updated, now=self._audit_now())
            updated = replace(updated, freshness_status=fresh.value)
        if as_revision and annotation.status is AnnotationStatus.PUBLISHED:
            updated = replace(
                updated,
                id=self._next_annotation_id(),
                status=AnnotationStatus.DRAFT,
                supersedes_id=annotation.id,
                verified_at="",
                published_at="",
                verification_record={},
                publication_record={},
            )
            self._capture_annotation_history(updated, 'revision_draft')
            self.annotations[updated.id] = updated
            self.append_event(
                "AnnotationRevisionDrafted",
                annotation_id=updated.id,
                supersedes=annotation.id,
                version=updated.version,
                annotation_kind=updated.kind.value,
            )
            return updated
        self._capture_annotation_history(updated, 'update')
        self.annotations[annotation_id] = updated
        self.append_event(
            "AnnotationUpdated",
            annotation_id=annotation_id,
            version=updated.version,
            fields=tuple(sorted(changes)),
        )
        return updated

    def _next_annotation_id(self) -> str:
        seq = 0
        for key in self.annotations:
            try:
                seq = max(seq, int(key.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        return f"ann-{seq + 1:03d}"

    def freshness_map(self, *, now: str = "") -> dict[str, str]:
        """逐对象时效（只产生需要复核/已失效提示，绝不自动删除）。"""
        stamp = now or self._audit_now()
        out: dict[str, str] = {}
        for annotation_id, annotation in self.annotations.items():
            if not annotation.tactical:
                continue
            out[annotation_id] = tm.freshness_of(annotation, now=stamp).value
        return out

    # ------------------------------------------------------------------
    # P20 态势自动发现与手动复核（docs/20 V1.0）
    #
    # 本段只做两件事：把新数据带来的变化记成可追溯台账，以及把指挥员
    # 要求的“复核当前态势”固定成一个带数据截止点的批次。
    # 不发布、不撕回、不改命令、不移动任何已发布对象。
    # ------------------------------------------------------------------

    def _next_change_id(self) -> str:
        seq = 0
        for key in self.situation_changes:
            try:
                seq = max(seq, int(key.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        return f"sit-{seq + 1:03d}"

    def _change_by_fingerprint(self, fingerprint: str):
        if not fingerprint:
            return None
        for change in self.situation_changes.values():
            if change.fingerprint == fingerprint:
                return change
        return None

    def record_situation_change(self, spec: dict, *, candidates: tuple[str, ...] = ()):
        """写入一条态势变化：同指纹不重复，仅等级上升才重新冒泡。"""
        fields = dict(spec)
        if candidates:
            fields["candidate_annotation_ids"] = tuple(dict.fromkeys(
                tuple(fields.get("candidate_annotation_ids") or ()) + tuple(candidates)))
        stamp = self._audit_now()
        existing = self._change_by_fingerprint(str(fields.get("fingerprint") or ""))
        if existing is not None:
            if sm.severity_rank(fields.get("severity")) <= sm.severity_rank(existing.severity):
                return None
            escalated = replace(
                existing,
                severity=str(fields.get("severity")),
                status=sm.ChangeStatus.NEW.value,
                reason=str(fields.get("reason") or existing.reason),
                detected_at=stamp,
                evidence_summary=str(fields.get("evidence_summary")
                                     or existing.evidence_summary),
                candidate_annotation_ids=fields["candidate_annotation_ids"],
                affected_option_ids=tuple(dict.fromkeys(
                    existing.affected_option_ids
                    + tuple(fields.get("affected_option_ids") or ()))),
            )
            self.situation_changes[existing.change_id] = escalated
            self.append_event(
                "SituationChangeEscalated",
                change_id=existing.change_id,
                change_kind=escalated.kind,
                event_id=escalated.event_id,
                from_severity=existing.severity,
                to_severity=escalated.severity,
                severity=escalated.severity,
                reason=escalated.reason,
                affected_annotation_ids=escalated.affected_annotation_ids,
                affected_option_ids=escalated.affected_option_ids,
                candidate_annotation_ids=escalated.candidate_annotation_ids,
                source_refs=escalated.source_refs,
                rule_version=escalated.rule_version,
                detected_at=stamp,
            )
            return escalated
        change = sm.SituationChange(
            change_id=self._next_change_id(),
            status=sm.ChangeStatus.NEW.value,
            received_at=stamp,
            **fields,
        )
        self.situation_changes[change.change_id] = change
        self.append_event(
            "SituationChangeRecorded",
            change_id=change.change_id,
            change_kind=change.kind,
            severity=change.severity,
            reason=change.reason,
            event_id=change.event_id,
            affected_annotation_ids=change.affected_annotation_ids,
            affected_option_ids=change.affected_option_ids,
            candidate_annotation_ids=change.candidate_annotation_ids,
            source_refs=change.source_refs,
            rule_version=change.rule_version,
            detected_at=change.detected_at or stamp,
        )
        return change

    def situation_scan(self, *, now: str = "", event_id: str = "") -> tuple:
        """自动发现：时效、冲突、方案影响三类确定性规则扫描。

        只写变化台账（以及幂等的冲突记录），不修改任何标绘对象与命令。
        """
        stamp = now or self._audit_now()
        self.refresh_conflict_records(now=stamp)
        marks = list(self.annotations.values())
        marks_by_id = {m.id: m for m in marks}
        specs = list(sm.scan_freshness(marks, event_id=event_id, now=stamp))
        specs += list(sm.scan_conflicts(
            self.conflict_records.values(), event_id=event_id, marks_by_id=marks_by_id,
        ))
        merged = specs + [c.as_spec() for c in sm.open_changes(self)]
        specs += list(sm.scan_options(
            merged, self.options.values(), event_id=event_id, now=stamp,
            marks_by_id=marks_by_id,
        ))
        added = []
        for spec in specs:
            recorded = self.record_situation_change(spec)
            if recorded is not None:
                added.append(recorded)
        return tuple(added)

    def supersede_situation_changes(
        self, annotation_id: str, *, by: str, note: str = ""
    ) -> tuple:
        """对象被重新核验/发布/撕回后，相关待复核提示自动让位（留审计）。

        只改变化台账状态，不动标绘对象本身。
        """
        stamp = self._audit_now()
        note = str(note or "").strip() or f"对象 {annotation_id} 已完成核验链流转"
        out: list = []
        for change in self.situation_changes.values():
            if not change.open_for_review:
                continue
            if annotation_id not in change.affected_annotation_ids:
                continue
            updated = replace(
                change,
                status=sm.ChangeStatus.SUPERSEDED.value,
                resolved_at=stamp,
                resolution=f"对象已重新核验/发布：{note}",
                handled_by=by,
            )
            self.situation_changes[change.change_id] = updated
            out.append(updated)
            self.append_event(
                "SituationChangeSuperseded",
                change_id=change.change_id,
                change_kind=updated.kind,
                annotation_id=annotation_id,
                handled_by=by,
                handled_at=stamp,
                note=note,
            )
        return tuple(out)

    def handle_situation_change(
        self, change_id: str, *, action: str, by: str, reason: str = ""
    ):
        """变化处理（已阅／按现有资料继续）：只改变化台账自身，不动态势。"""
        change = self.situation_changes.get(change_id)
        if change is None:
            raise KeyError(f"变化记录不存在：{change_id}")
        target = sm.ACTION_TARGET.get(str(action))
        if target is None:
            raise ValueError(
                "未知变化处理动作：" + str(action)
                + "（只允许：" + "、".join(sorted(sm.ACTION_TARGET)) + "）"
            )
        if str(action) in sm.REASON_REQUIRED and not str(reason).strip():
            raise ValueError("按现有资料继续必须写明原因，不能默默忽略一条变化")
        if change.status == target:
            return change
        if not sm.can_transition(change.status, target):
            raise ValueError(
                f"变化 {change_id} 当前为「{change.status_word}」，不允许该处理动作"
            )
        stamp = self._audit_now()
        note = str(reason).strip()
        updated = replace(
            change, status=target, resolved_at=stamp, handled_by=by,
            resolution=(f"{str(action)}：{note}" if note else str(action)),
        )
        self.situation_changes[change_id] = updated
        self.append_event(
            "SituationChangeHandled",
            change_id=change_id,
            change_kind=updated.kind,
            action=str(action),
            from_status=change.status,
            to_status=updated.status,
            reason=note,
            handled_by=by,
            handled_at=stamp,
        )
        return updated

    def review_current_situation(
        self, *, requested_by: str, now: str = "", event_id: str = ""
    ) -> tuple:
        """指挥员发起的「复核当前态势」：同一数据截止点幂等。"""
        self._require_general_entry()
        stamp = now or self._audit_now()
        changes = sm.open_changes(self)
        prior = sm.latest_review(self)
        if prior is not None and not changes and prior.workspace_version_after == self.version:
            # 没有新数据写入就重复点击：返回同一批次，不制造第二条结论。
            return prior, False
        payload = sm.build_review(
            self, changes, requested_by=requested_by, now=stamp,
            cutoff_at=stamp, prior=prior,
        )
        fingerprint = payload["fingerprint"]
        for batch in self.situation_reviews.values():
            if batch.fingerprint == fingerprint:
                return batch, False
        batch_id = f"srev-{len(self.situation_reviews) + 1:03d}"
        batch = sm.SituationReviewBatch(
            review_batch_id=batch_id,
            event_id=event_id,
            requested_by=requested_by,
            requested_at=stamp,
            cutoff_at=stamp,
            workspace_version_before=self.version,
            prior_review_batch_id=prior.review_batch_id if prior else "",
            **payload,
        )
        self.situation_reviews[batch_id] = batch
        for change in changes:
            self.situation_changes[change.change_id] = replace(
                change,
                status=sm.ChangeStatus.REVIEWED.value,
                review_batch_id=batch_id,
                resolved_at=stamp,
                resolution="已纳入态势复核批次",
            )
        self.append_event(
            "SituationReviewCompleted",
            review_batch_id=batch_id,
            event_id=event_id,
            outcome=batch.outcome,
            cutoff_at=stamp,
            source_change_ids=batch.source_change_ids,
            new_findings=batch.new_findings,
            affected_option_ids=batch.affected_option_ids,
            candidate_annotation_ids=batch.candidate_annotation_ids,
            requires_commander_action=batch.requires_commander_action,
            workspace_version_before=batch.workspace_version_before,
            requested_by=requested_by,
            rule_version=batch.rule_version,
        )
        stored = replace(batch, workspace_version_after=self.version)
        self.situation_reviews[batch_id] = stored
        return stored, True

    def situation_feed(self, *, now: str = "", limit: int = 6) -> dict:
        """只读态势馈送（页面轻量刷新用）；本方法不写任何数据。"""
        stamp = now or self._audit_now()
        open_rows = sm.open_changes(self)
        latest = sm.latest_review(self)
        raw_stamps = dict.fromkeys(
            c.observed_at or c.detected_at or c.received_at
            for c in self.situation_changes.values())
        # 字符串排序会被 ISO/空格混合格式骗到；按解析后的时间取最新。
        stamps = sorted(
            (s for s in raw_stamps if s),
            key=lambda s: (sm.parse_time(s) or sm.parse_time("1970-01-01 00:00"), s),
        )
        return {
            "feed_version": f"v{self.version}-{len(open_rows)}",
            "workspace_version": self.version,
            "checked_at": stamp,
            "latest_data_at": stamps[-1] if stamps else "",
            "latest_review_at": latest.requested_at if latest else "",
            "latest_review_id": latest.review_batch_id if latest else "",
            "pending_count": len(open_rows),
            "needs_commander_count": sum(1 for c in open_rows if c.requires_commander),
            "changes": [c.as_row() for c in open_rows[:limit]],
            "rule_version": sm.RULE_VERSION,
        }

    def register_observation(
        self,
        *,
        kind: str,
        title: str,
        geo_points,
        observed_at: str,
        source_ref: str,
        details: dict[str, str] | None = None,
        positioning_status: str = "",
        note: str = "",
        is_demo: bool = False,
        created_by: str = "local-general-entry",
        event_id: str = "",
        now: str = "",
    ) -> dict:
        """统一数据进入入口：一条已校验的现场观测进入事件空间。

        fail-closed：无坐标、无来源、无观测时间一律拒收，不补造。
        产出只能是草稿/待核验候选与变化台账，绝不直接发布。
        """
        if not tm.is_tactical(str(kind)):
            raise ValueError(
                "现场观测必须使用战术对象类型；旧式通用标绘不进入自动发现"
            )
        geo = tuple(tuple(float(v) for v in p) for p in tuple(geo_points or ()))
        if not geo:
            raise ValueError(
                "现场观测必须带地理坐标（纬度, 经度）："
                "无坐标不进入态势，也不产生任何空间结论"
            )
        tm.validate_geometry(str(kind), geo)
        if not str(source_ref or "").strip():
            raise ValueError("现场观测必须写明来源编号，不写来源就不进入态势")
        if not str(observed_at or "").strip():
            raise ValueError("现场观测必须写明观测时间，不假定“就是现在”")
        positioning = str(positioning_status or "").strip() or tm.PositioningStatus.UNREGISTERED.value
        if positioning not in {p.value for p in tm.PositioningStatus}:
            raise ValueError(f"未知定位状态：{positioning}")
        allowed = tm.allowed_detail_keys(str(kind))
        clean: dict[str, str] = {}
        wrong: list[str] = []
        for key, value in (details or {}).items():
            text = str(value).strip()
            if not text:
                continue
            if str(key) not in allowed:
                wrong.append(str(key))
                continue
            clean[str(key)] = text[:500]
        if wrong:
            raise ValueError(
                f"{tm.kind_word(str(kind))}不适用字段："
                + "、".join(tm.detail_word(w) for w in sorted(set(wrong)))
            )
        stamp = now or self._audit_now()
        seq = 0
        for key in self.annotations:
            try:
                seq = max(seq, int(key.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        mark = MapAnnotation(
            id=f"ann-{seq + 1:03d}",
            kind=AnnotationKind(str(kind)),
            points=(),
            title=(str(title).strip() or tm.kind_word(str(kind)))[:40],
            status=AnnotationStatus.DRAFT,
            note=str(note or "")[:500],
            source=AnnotationSource.AI_SUGGESTION,
            created_by=created_by,
            geo_points=geo,
            details=clean,
            source_ref=(str(source_ref).strip(),),
            positioning_status=positioning,
            is_demo=bool(is_demo),
            demo_notice=("演示数据、非现场实时数据" if is_demo else ""),
            event_id=event_id,
            created_at=stamp,
            updated_at=stamp,
        )
        gate_errors = tm.verify_gate(mark)
        if not gate_errors:
            mark = replace(mark, status=AnnotationStatus.PENDING_REVIEW)
        self.record_annotation(mark)
        notice = "" if positioning == tm.PositioningStatus.LOCATED.value else (
            f"定位状态：{tm.positioning_word(positioning)}——"
            "仅提示方向与量级，不能作为精确坐标依据"
        )
        specs = list(sm.scan_observation(
            self.annotations.values(),
            kind=str(kind),
            geo=geo,
            event_id=event_id,
            observed_at=str(observed_at),
            source_ref=str(source_ref).strip(),
            positioning_status=positioning,
            now=stamp,
        ))
        specs.append({
            "event_id": event_id,
            "kind": sm.ChangeKind.CANDIDATE_CREATED.value,
            "severity": (sm.ChangeSeverity.NOTICE.value if gate_errors
                         else sm.ChangeSeverity.REVIEW.value),
            "reason": (
                f"新观测形成{tm.kind_word(str(kind))}「{mark.title}」："
                + ("专属字段不足，保留为草稿待补充" if gate_errors
                   else "已进入待核验，需人工核对后才能上图")
            ),
            "fingerprint": sm.compute_fingerprint(
                sm.RULE_VERSION, event_id, "candidate", mark.id, mark.version,
            ),
            "detected_at": stamp,
            "observed_at": str(observed_at),
            "source_refs": (str(source_ref).strip(),),
            "source_digest": sm.source_digest((mark.id, mark.version, sorted(clean))),
            "affected_annotation_ids": (mark.id,),
            "affected_option_ids": (),
            "candidate_annotation_ids": (mark.id,),
            "evidence_summary": "来源：" + str(source_ref).strip(),
            "positioning_notice": notice,
        })
        if gate_errors or positioning != tm.PositioningStatus.LOCATED.value:
            missing = list(gate_errors)
            if positioning != tm.PositioningStatus.LOCATED.value:
                missing.append(
                    f"定位状态为{tm.positioning_word(positioning)}，需补充定位依据"
                )
            specs.append({
                "event_id": event_id,
                "kind": sm.ChangeKind.INFORMATION_GAP.value,
                "severity": sm.ChangeSeverity.REVIEW.value,
                "reason": "现场观测信息不足：" + "；".join(missing)[:260],
                "fingerprint": sm.compute_fingerprint(
                    sm.RULE_VERSION, event_id, "gap", mark.id, tuple(sorted(missing)),
                ),
                "detected_at": stamp,
                "observed_at": str(observed_at),
                "source_refs": (str(source_ref).strip(),),
                "source_digest": sm.source_digest((mark.id, tuple(sorted(missing)))),
                "affected_annotation_ids": (mark.id,),
                "affected_option_ids": (),
                "candidate_annotation_ids": (mark.id,),
                "evidence_summary": "；".join(missing)[:240],
                "positioning_notice": "",
            })
        change_ids: list[str] = []
        for spec in specs:
            recorded = self.record_situation_change(spec, candidates=(mark.id,))
            if recorded is not None:
                change_ids.append(recorded.change_id)
        self.refresh_conflict_records(now=stamp)
        self.append_event(
            "SituationObservationRecorded",
            annotation_id=mark.id,
            annotation_kind=str(kind),
            annotation_status=mark.status.value,
            event_id=event_id,
            observed_at=str(observed_at),
            source_ref=str(source_ref).strip(),
            positioning_status=positioning,
            vertex_count=len(geo),
            change_ids=tuple(change_ids),
            gate_errors=tuple(gate_errors),
            recorded_at=stamp,
        )
        return {
            "annotation_id": mark.id,
            "status": mark.status.value,
            "status_word": annotation_status_word(mark.status),
            "change_ids": tuple(change_ids),
            "missing_fields": tuple(gate_errors),
            "positioning_notice": notice,
        }

    # —— 命令战术层（挂在既有 ExecutionOrder 上，不替代审批链） ----------

    def open_command_flow(
        self, order_id: str, *, event_id: str = "", task_level: str = "normal",
        target: str = "", unit: str = "", route_id: str = "", start_time: str = "",
        deadline: str = "", safety_cond: str = "", stop_cond: str = "",
        receipt_need: str = "", conflict_level: str = "",
    ) -> tm.CommandFlow:
        """为已下达的命令建立战术执行记录（幂等：已存在则返回）。"""
        if order_id not in self.orders:
            raise ValueError(f"命令不存在：{order_id}（必须先生成命令再建立执行记录）")
        if self.event_id and event_id != self.event_id:
            raise ValueError('Command belongs to another event or event reference is missing')
        existing = self.command_flows.get(order_id)
        if existing is not None:
            if event_id != existing.event_id:
                raise ValueError('Existing command belongs to another event')
            return existing
        if task_level not in {t.value for t in tm.TaskLevel}:
            raise ValueError(f"未知任务等级：{task_level}")
        flow = tm.CommandFlow(
            order_id=order_id, event_id=event_id, task_level=task_level,
            target=target, unit=unit, route_id=route_id, start_time=start_time,
            deadline=deadline, safety_cond=safety_cond, stop_cond=stop_cond,
            receipt_need=receipt_need, conflict_level=conflict_level,
            content_version=1 if self.event_id else 0,
        )
        self.command_flows[order_id] = flow
        self.append_event(
            "CommandFlowOpened", order_id=order_id, task_level=task_level,
            stage=flow.stage, route_id=route_id, event_id=event_id,
            content_version=flow.content_version,
            **({'flow': asdict(flow)} if flow.content_version else {}),
        )
        return flow

    def _flow_act(self, order_id: str, method: str, *, event_id: str | None = None, **kw) -> tm.CommandFlow:
        if order_id.startswith('formal-'):
            raise PermissionError('新版正式任务须经具名任务服务回执或核验。')
        flow = self.command_flows.get(order_id)
        if flow is None:
            raise ValueError(f"命令 {order_id} 尚无战术执行记录")
        if self.event_id and flow.event_id != self.event_id:
            raise ValueError('Command flow is outside this workspace event')
        if event_id is not None and event_id != flow.event_id:
            raise ValueError('Command reference belongs to another event')
        if flow.content_version and method in {'record_ack', 'add_receipt', 'revise', 'close'}:
            if not self.event_id or event_id != self.event_id:
                raise ValueError('Explicit bound event reference required')
        updated: tm.CommandFlow = getattr(flow, method)(**kw)
        if updated == flow:
            return flow                                  # 幂等重提：不写重复审计
        self.command_flows[order_id] = updated
        self.append_event(
            "CommandFlowChanged",
            order_id=order_id,
            act=method,
            stage=updated.stage,
            control_state=updated.control_state,
            version=updated.version,
            event_id=updated.event_id, content_version=updated.content_version,
            receipt_count=len(updated.receipts),
            **({'flow': asdict(updated)} if updated.content_version else {}),
        )
        return updated

    def confirm_command_flow(
        self, order_id: str, *, actor: str, at: str = "", note: str = ""
    ) -> tm.CommandFlow:
        """司令确认下达：必须先过命令链审批，且不得在阻断/决断未处置时下达。"""
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError(f"命令不存在：{order_id}")
        if order.status not in {OrderStatus.CONFIRMED, OrderStatus.ACTIVE, OrderStatus.PAUSED}:
            raise ValueError(
                f"命令尚未确认（当前 {order.status.value}），不能进入待接收"
            )
        flow = self.command_flows.get(order_id)
        if flow is not None and flow.conflict_level in {
                tm.ConflictLevel.BLOCKED.value, tm.ConflictLevel.DECISION.value}:
            raise ValueError("该路线存在三级阻断或四级待决断，必须先处置再下达")
        return self._flow_act(
            order_id, "moved_to", stage=tm.CommandStage.CONFIRMED_UNACK.value,
            actor=actor, at=at or self._audit_now(), note=note,
        )

    def advance_command_flow(
        self, order_id: str, stage: str, *, actor: str, at: str = "", note: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "moved_to", stage=stage, actor=actor,
                              at=at or self._audit_now(), note=note)

    def command_delivery(
        self, order_id: str, *, delivered_at: str = "", viewed_at: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "record_delivery",
                              at=self._audit_now(),
                              delivered=delivered_at, viewed=viewed_at)

    def command_ack(
        self, order_id: str, *, by: str, result: str, at: str = "",
        event_id: str | None = None, expected_version: int | None = None,
    ) -> tm.CommandFlow:
        """执行单位接收（与司令确认是两个独立动作）。"""
        return self._flow_act(order_id, "record_ack", at=at or self._audit_now(),
                              by=by, result=result, event_id=event_id, expected_version=expected_version)

    def confirm_execution_conditions(
        self, order_id: str, *, by: str, note: str, at: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "confirm_conditions",
                              at=at or self._audit_now(), by=by, note=note)

    def begin_command_execution(
        self, order_id: str, *, by: str, at: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "begin", at=at or self._audit_now(), by=by)

    def record_command_receipt(
        self, order_id: str, *, kind: str, by: str, at: str = "", where: str = "",
        content: str = "", personnel: str = "", risk: str = "", need: str = "",
        source: str = "", request_id: str = "",
        event_id: str | None = None, expected_version: int | None = None,
    ) -> tm.CommandFlow:
        """回执不自动改写正式火线、队伍位置或危险区（定稿 §9.4）。"""
        return self._flow_act(
            order_id, "add_receipt", kind=kind, by=by, at=at or self._audit_now(),
            where=where, content=content, personnel=personnel, risk=risk,
            need=need, source=source, request_id=request_id,
            event_id=event_id, expected_version=expected_version,
        )

    def pause_command(
        self, order_id: str, *, reason: str, reassess_at: str, by: str, at: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "pause", reason=reason, reassess_at=reassess_at,
                              at=at or self._audit_now(), by=by)

    def resume_command(self, order_id: str, *, by: str, at: str = "", note: str = "") -> tm.CommandFlow:
        return self._flow_act(order_id, "resume", at=at or self._audit_now(), by=by, note=note)

    def request_command_withdraw(
        self, order_id: str, *, reason: str, by: str, at: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "request_withdraw", reason=reason,
                              at=at or self._audit_now(), by=by)

    def command_withdraw_ack(
        self, order_id: str, *, kind: str, by: str, at: str = "", source: str = ""
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "withdraw_ack", kind=kind, by=by,
                              at=at or self._audit_now(), source=source)

    def resend_command(
        self, order_id: str, *, by: str, at: str = "", changed: bool = False
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "resend", at=at or self._audit_now(),
                              by=by, changed=changed)

    def revise_command(
        self, order_id: str, *, by: str, note: str, at: str = "",
        event_id: str | None = None, expected_version: int | None = None, **changes
    ) -> tm.CommandFlow:
        return self._flow_act(order_id, "revise", at=at or self._audit_now(),
                              by=by, note=note, event_id=event_id,
                              expected_version=expected_version, **changes)

    def close_command(
        self, order_id: str, *, result: str, by: str, at: str = "", facts: str = "",
        handling: str = "", outcome: str = "", note: str = "",
        event_id: str | None = None, expected_version: int | None = None,
    ) -> tm.CommandFlow:
        flow = self._flow_act(
            order_id, "close", result=result, at=at or self._audit_now(), by=by,
            facts=facts, handling=handling, outcome=outcome, note=note,
            event_id=event_id, expected_version=expected_version,
        )
        return flow

    def command_overdue_notices(self, *, now: str = "") -> tuple[str, ...]:
        """接收/回执超时只产生提醒与升级事项，不自动判定失联。"""
        stamp = now or self._audit_now()
        out: list[str] = []
        for order_id in sorted(self.command_flows):
            out.extend(self.command_flows[order_id].timeouts(stamp))
        return tuple(out)

    def command_flow(self, order_id: str) -> tm.CommandFlow | None:
        return self.command_flows.get(order_id)

