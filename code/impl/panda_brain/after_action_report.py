"""After-action report generator (doc 11 §7 P9-T3).

Renders a deterministic Markdown after-action report from a lived-in
workspace. Offline and pure: the same workspace always produces the exact
same bytes — no clocks, no randomness, no I/O beyond the caller's write.
"""

from __future__ import annotations

from .workspace import ScenarioWorkspace

_DECISION_KINDS = ("GeneralDecisionRecorded",)
_ORDER_KINDS = (
    "ExecutionOrderDrafted",
    "OrderConfirmed",
    "ApprovalRecorded",
    "OrderActivated",
    "OrderAnomalyPaused",
    "OrderResumed",
    "OrderSuperseded",
    "ExecutionSimulated",
    "VerificationFinished",
)


def _decision_line(event) -> str:
    d = event.payload.get("decision", {})
    return (
        f"- 决断 `{d.get('id')}`：{d.get('decision_type')} 选项 `{d.get('option_id') or '—'}`"
        f"（{d.get('reason', '')}）"
    )


def _order_line(event) -> str:
    kind = event.kind
    if kind == "ExecutionSimulated":
        r = event.payload.get("report", {})
        return (
            f"- 执行 `{r.get('order_id')}` → **{r.get('status')}**"
            f"（{(r.get('observations') or ('',))[0]}）"
        )
    if kind == "VerificationFinished":
        v = event.payload.get("verification", {})
        verdict = "通过" if v.get("passed") else "未通过"
        unmet = "；".join(v.get("unmet_conditions") or ())
        return f"- 独立核验：**{verdict}**" + (f"（未满足：{unmet}）" if unmet else "")
    order_id = event.payload.get("order_id")
    extra = ""
    if kind == "ExecutionOrderDrafted":
        order = event.payload.get("order", {})
        order_id = order.get("id") or event.payload.get("order_id")
    if kind == "ApprovalRecorded":
        a = event.payload.get("approval", {})
        order_id = a.get("order_id") or order_id
        extra = f" 契约`{a.get('action_contract_id')}` 风险级 L{int(a.get('risk_level', 0))}"
    if kind == "OrderAnomalyPaused":
        extra = f"（{event.payload.get('reason', '')}）"
    if kind == "OrderCancelled":
        extra = f"（{event.payload.get('reason', '')}）"
    label = {
        "ExecutionOrderDrafted": "起草",
        "OrderConfirmed": "确认",
        "ApprovalRecorded": "审批",
        "OrderActivated": "激活",
        "OrderAnomalyPaused": "异常暂停",
        "OrderResumed": "恢复",
        "OrderSuperseded": "被接替",
        "VerificationFinished": "",
    }.get(kind, kind)
    return f"- {label} `{order_id}`{extra}"


def render_after_action_report(workspace: ScenarioWorkspace, *, title: str) -> str:
    lines: list[str] = []
    broken = workspace.verify_audit_chain()

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- 演练空间：`{workspace.scenario_id}`，收束版本 v{workspace.version}")
    lines.append(
        "- 审计链：" + ("完整" if not broken else f"**断链于序号 {broken}**")
    )

    # -- timeline ---------------------------------------------------------
    lines.append("")
    lines.append("## 一、决断与执行时间线")
    lines.append("")
    decision_open = False
    for event in workspace.events:
        if event.kind in _DECISION_KINDS:
            lines.append(_decision_line(event))
            decision_open = True
        elif event.kind in _ORDER_KINDS:
            if not decision_open:
                lines.append("_（无决断上下文）_")
                decision_open = True
            lines.append("  " + _order_line(event).lstrip("- "))
    if not decision_open:
        lines.append("（本战役无决断记录）")

    # -- reviews ------------------------------------------------------------
    lines.append("")
    lines.append("## 二、各日复盘发现")
    lines.append("")
    reviews = sorted(workspace.reviews.values(), key=lambda r: r.source_version)
    if reviews:
        for review in reviews:
            lines.append(f"### 复盘 `{review.id}`（范围：{review.scope}，回放 {review.entries_replayed} 条时间线条目）")
            for finding in review.findings:
                lines.append(f"- {finding}")
    else:
        lines.append("（无复盘记录）")

    # -- memory ---------------------------------------------------------------
    lines.append("")
    lines.append("## 三、经验沉淀（受控记忆）")
    lines.append("")
    candidates = sorted(workspace.memory_candidates.values(), key=lambda c: c.id)
    if candidates:
        confirmed = sum(1 for c in candidates if c.status.value == "confirmed")
        lines.append(
            f"共 {len(candidates)} 条候选，其中确认 {confirmed} 条、待定 "
            f"{sum(1 for c in candidates if c.status.value == 'proposed')} 条。"
        )
        for candidate in candidates:
            boundary = "、".join(candidate.applicability_boundary)
            lines.append(f"- `[{candidate.status.value}]` {candidate.content}（适用边界：{boundary}）")
    else:
        lines.append("（无经验候选）")

    # -- quality flags --------------------------------------------------------
    lines.append("")
    lines.append("## 四、编排质量旗标")
    lines.append("")
    feedbacks = list(workspace.orchestration_feedbacks)
    if feedbacks:
        for feedback in feedbacks:
            lines.append(f"- [{feedback.kind}] {feedback.detail}")
    else:
        lines.append("（无质量旗标）")

    # -- final situation -------------------------------------------------------
    lines.append("")
    lines.append("## 五、收束态势")
    lines.append("")
    for subject in sorted(workspace.claims):
        claim = workspace.claims[subject]
        lines.append(f"- {subject} = `{claim.value}`（{claim.status.value}）")
    latest_verification = max(
        workspace.verifications.values(), key=lambda v: v.id, default=None
    )
    if latest_verification is not None:
        verdict = "通过" if latest_verification.passed else "未通过"
        lines.append("")
        lines.append(f"最终独立核验：**{verdict}**（核验记录 `{latest_verification.id}`）")

    return "\n".join(lines) + "\n"
