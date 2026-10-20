"""Campaign invariant checkers (doc 11 §7 P9-T4).

Each checker scans a workspace and returns precise violations instead of a
boolean, so a broken run names its own break. The suite is self-tested by
mutation: the acceptance tests deliberately corrupt a copy of a good
workspace and require the matching checker to catch exactly that corruption.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ClaimStatus, RiskLevel, SourceTrustLevel
from .workspace import ScenarioWorkspace

_PROPOSED_KIND = "MemoryCandidateProposed"
_CONFIRMED_KIND = "MemoryCandidateConfirmed"
_MUTABLE_EVENT_KINDS = {
    "ClaimRecorded",
    "ClaimUpdated",
    "GeneralDecisionRecorded",
    "ExecutionOrderDrafted",
    "OrderConfirmed",
    "OrderActivated",
}


@dataclass(frozen=True)
class Violation:
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{self.kind}] {self.detail}"


def check_audit_chain(workspace: ScenarioWorkspace) -> tuple[Violation, ...]:
    broken = workspace.verify_audit_chain()
    if broken:
        details = ", ".join(str(sequence) for sequence in broken)
        return (Violation("audit_chain", f"hash chain breaks at sequence {details}"),)
    return ()


def check_sensor_never_verifies(
    workspace: ScenarioWorkspace, *, sensor_sources: tuple[str, ...] = ("sensor-feed",)
) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for subject in sorted(workspace.claims):
        claim = workspace.claims[subject]
        if claim.status != ClaimStatus.VERIFIED:
            continue
        for evidence_id in claim.evidence_ids:
            evidence = workspace.evidence.get(evidence_id)
            if evidence is None:
                continue
            if evidence.source in sensor_sources or evidence.trust_level is not SourceTrustLevel.AUTHORIZED:
                violations.append(
                    Violation(
                        "sensor_never_verifies",
                        f"{subject} is VERIFIED on {evidence.source}/{evidence.trust_level.value}"
                        f" (evidence {evidence_id})",
                    )
                )
    return tuple(violations)


def check_l3_approvals_before_activation(workspace: ScenarioWorkspace) -> tuple[Violation, ...]:
    approval_seq_by_order: dict[str, int] = {}
    activation_seq_by_order: dict[str, int] = {}
    for event in workspace.events:
        if event.kind == "ApprovalRecorded":
            approval = event.payload.get("approval", {})
            order_id = approval.get("order_id")
            if order_id and order_id not in approval_seq_by_order:
                approval_seq_by_order[order_id] = event.sequence
        elif event.kind == "OrderActivated":
            order_id = event.payload.get("order_id")
            if order_id and order_id not in activation_seq_by_order:
                activation_seq_by_order[order_id] = event.sequence

    violations: list[Violation] = []
    for contract in workspace.action_contracts.values():
        if contract.risk_level < RiskLevel.L3:
            continue
        approved_at = approval_seq_by_order.get(contract.order_id)
        activated_at = activation_seq_by_order.get(contract.order_id)
        if approved_at is None:
            violations.append(
                Violation("l3_approval", f"L3+ contract {contract.id} (order {contract.order_id}) has no approval")
            )
        elif activated_at is not None and approved_at > activated_at:
            violations.append(
                Violation(
                    "l3_approval",
                    f"order {contract.order_id} activated at seq {activated_at} before"
                    f" approval at seq {approved_at}",
                )
            )
    return tuple(violations)


def check_memory_purity(workspace: ScenarioWorkspace) -> tuple[Violation, ...]:
    """Between proposing a candidate and confirming THAT candidate, no mutable
    domain/command event may occur — confirming experience must not touch facts
    or commands (doc 11 §6.6 red line)."""
    events = list(workspace.events)
    proposed = [e.sequence for e in events if e.kind == _PROPOSED_KIND]
    confirmed = [e.sequence for e in events if e.kind == _CONFIRMED_KIND]
    violations: list[Violation] = []
    for opened, closed in zip(proposed, confirmed):
        for event in events:
            if opened < event.sequence < closed and event.kind in _MUTABLE_EVENT_KINDS:
                violations.append(
                    Violation(
                        "memory_purity",
                        f"mutable event {event.kind} at seq {event.sequence} inside a"
                        f" memory-confirmation window ({opened} -> {closed})",
                    )
                )
    return tuple(violations)


def check_campaign_invariants(
    workspace: ScenarioWorkspace,
    *,
    sensor_sources: tuple[str, ...] = ("sensor-feed",),
) -> tuple[Violation, ...]:
    return (
        check_audit_chain(workspace)
        + check_sensor_never_verifies(workspace, sensor_sources=sensor_sources)
        + check_l3_approvals_before_activation(workspace)
        + check_memory_purity(workspace)
    )
