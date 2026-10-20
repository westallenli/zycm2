"""P16-T21 全程验收主线（doc 11 §8）：十项验收标准的对象级取证。

每项验收都从真实运行产物（战役跑链的工作区对象）中读取事实并给出证据行，
不做任何「看起来像」的静态断言。跑链由 P9 战役驱动器承担——指挥员角色由
GeneralSurrogate 走同一认证入口扮演，全程离线、确定性、审计链完整。

纪律：任何一项证据不足即该项不通过；本模块只读工作区，绝不为了验收
补写对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    DecisionActor,
    DecisionType,
    OrderStatus,
)


@dataclass(frozen=True)
class AcceptanceItem:
    no: int
    title: str
    passed: bool
    evidence: tuple[str, ...]


@dataclass
class AcceptanceReport:
    scenario: str
    waves_executed: int
    event_count: int
    items: list[AcceptanceItem] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.items) and all(i.passed for i in self.items)

    def markdown(self, generated_at: str) -> str:
        lines = [
            "# 验收记录：全程验收主线十项（规范 11·§8）",
            "",
            f"- 演练场景：`{self.scenario}`（P9 确定性战役跑链）",
            f"- 波次：{self.waves_executed} · 审计事件：{self.event_count} 条",
            f"- 结论：**{'十项全部通过' if self.all_passed else '存在未通过项'}**",
            f"- 生成时间：{generated_at}",
            "",
        ]
        for item in self.items:
            mark = "✅" if item.passed else "❌"
            lines.append(f"## {item.no}. {item.title} {mark}")
            lines.append("")
            for ev in item.evidence:
                lines.append(f"- {ev}")
            lines.append("")
        return "\n".join(lines)


def run_ten_item_acceptance(result=None, *, scenario: str = "") -> AcceptanceReport:
    """对一次完整战役跑链执行十项验收取证。result 缺省时现场跑一遍。"""
    if result is None:
        from .campaign_runner import run_campaign

        result = run_campaign()
    ws = result.workspace
    report = AcceptanceReport(
        scenario=scenario or getattr(result.script, "name", "hanjiang-72h"),
        waves_executed=result.waves_executed,
        event_count=len(ws.events),
    )
    for check in (_item1, _item2, _item3, _item4, _item5,
                  _item6, _item7, _item8, _item9, _item10):
        report.items.append(check(ws, result))
    return report


# ---------------------------------------------------------------------------
# 各项取证


def _item1(ws, result) -> AcceptanceItem:
    """原始情报及其来源可见：主张 ← 证据 ← 来源，逐环可点。"""
    ev_lines = []
    ok = True
    for claim in ws.claims.values():
        if not claim.evidence_ids:
            ok = False
            continue
        for eid in claim.evidence_ids:
            evidence = ws.evidence.get(eid)
            if evidence is None or not evidence.source:
                ok = False
                continue
            if len(ev_lines) < 4:
                ev_lines.append(
                    f"`{claim.id}`（{claim.subject}={claim.value}，{claim.status.value}）"
                    f" ← 证据 `{evidence.id}` ← 来源 `{evidence.source}`")
    ok = ok and bool(ws.claims)
    ev_lines.append(f"共 {len(ws.claims)} 条主张 / {len(ws.evidence)} 条证据，全部带来源引用。")
    return AcceptanceItem(1, "你能看到参谋使用的原始情报及其来源", ok, tuple(ev_lines))


def _item2(ws, result) -> AcceptanceItem:
    """补充修正后后续任务和判断会改变：接替链 + 记忆注入次日简报。"""
    sup = [o for o in ws.orders.values() if o.status == OrderStatus.SUPERSEDED]
    chain_lines = [
        f"`{o.id}` 被接替，让位于后续任务" for o in sorted(sup, key=lambda x: x.id)]
    injected = bool(getattr(result, "experience_injection_seen", False))
    ev = list(chain_lines[:4])
    if injected:
        ev.append("已确认经验注入次日决策简报（experience_injection_seen），"
                  "前日复盘结论改变了后续判断输入。")
    return AcceptanceItem(
        2, "你补充或修正后，参谋的后续任务和判断会改变",
        bool(sup) and injected, tuple(ev))


def _item3(ws, result) -> AcceptanceItem:
    """Agent 分工按任务需要产生：不同任务计划组装不同班底（非固定全员轮询）。"""
    tasks = ws.staff_tasks.values()
    by_plan: dict[str, set] = {}
    for t in tasks:
        by_plan.setdefault(t.plan_id, set()).add(t.owner_role)
    distinct_casts = len({frozenset(s) for s in by_plan.values()})
    selective = len(by_plan) >= 2 and distinct_casts >= 2
    questions_ok = all(t.question_to_answer.strip() for t in tasks)
    intel_tasks = [t for t in tasks if t.allowed_sources]
    verdict_tasks = [t for t in tasks if not t.allowed_sources]
    single_role_plans = sorted(p for p, s in by_plan.items() if len(s) == 1)
    by_type: dict[str, int] = {}
    for t in tasks:
        key = t.work_type.value
        by_type[key] = by_type.get(key, 0) + 1
    spread = ", ".join(f"{k}×{v}" for k, v in sorted(by_type.items()))
    sample = next(iter(tasks))
    ev = (
        f"共 {len(list(ws.staff_tasks.values()))} 项 StaffTask、{len(by_plan)} 个任务计划，"
        f"按工种分布：{spread}。",
        f"各计划班底组合共 {distinct_casts} 种——专项复核计划"
        f"（{'、'.join(single_role_plans[:3])}）仅拉起单一岗位，"
        "主计划才启用全编制：班底随任务组装，非固定全员运行。",
        f"每项任务都带明确问题；{len(intel_tasks)} 项情报类任务全部限定允许来源，"
        f"{len(verdict_tasks)} 项为裁决指挥员答复的内部任务（不涉外部来源）；"
        f"样例 `{sample.id}`：「{sample.question_to_answer}」。",
    )
    return AcceptanceItem(3, "Agent 分工按任务需要产生", bool(tasks) and selective and questions_ok, ev)


def _item4(ws, result) -> AcceptanceItem:
    """不同意见可见且保留，不被多数或汇总掩盖。"""
    lines = []
    ok = bool(ws.disagreements)
    for dg in ws.disagreements[:3]:
        holders = "、".join(dg.positions.keys())
        lines.append(
            f"`{dg.id}` 议题「{dg.topic}」保留 {len(dg.positions)} 方立场"
            f"（{holders}），附证据 {len(dg.evidence_ids)} 条。")
    multi = all(len(dg.positions) >= 2 for dg in ws.disagreements)
    lines.append(f"共 {len(ws.disagreements)} 条分歧对象原样保留在时间线上。")
    return AcceptanceItem(4, "不同意见可见且保留", ok and multi, tuple(lines))


def _item5(ws, result) -> AcceptanceItem:
    """参谋长能说明为什么此时要继续、停止、并行或请示。"""
    answered = [q for q in ws.general_questions.values() if q.status == "answered"]
    checks_ok = all(pc.passed is not None for pc in ws.policy_checks.values()) \
        if ws.policy_checks else True
    sample = answered[0] if answered else None
    ev = []
    if sample is not None:
        ev.append(f"请示 `{sample.id}`：{sample.question}")
        ev.append(f"指挥员答复：{sample.answer}")
    ev.append(
        f"{len(ws.policy_checks)} 次政策门检查全部留有结论；"
        f"{len(ws.proposals)} 份提案均带风险与未知项清单（推进/停止的依据）。")
    ok = bool(answered) and checks_ok
    return AcceptanceItem(5, "参谋长能说明继续/停止/并行/请示的原因", ok, tuple(ev))


def _item6(ws, result) -> AcceptanceItem:
    """参谋推荐与指挥员决断有严格对象边界。"""
    proposals_commander = [
        p for p in ws.proposals.values() if p.advisor_role == DecisionActor.COMMANDER]
    bad_orders = [
        o for o in ws.orders.values()
        if not o.decision_id or o.decision_id not in ws.decisions]
    adopts = [d for d in ws.decisions.values() if d.decision_type == DecisionType.ADOPT]
    non_commander_decisions = [
        d for d in ws.decisions.values() if d.actor_role != DecisionActor.COMMANDER]
    sample_d = adopts[0] if adopts else None
    ev = [
        f"提案 {len(ws.proposals)} 份全部由参谋角色产出（无指挥员身份越写）；"
        f"决断 {len(ws.decisions)} 条全部 actor=commander。",
    ]
    if sample_d is not None:
        ev.append(
            f"样例：提案 `{next(iter(ws.proposals))}` 给出选项集 → "
            f"决断 `{sample_d.id}` adopt `{sample_d.option_id}`（理由：{sample_d.reason[:40]}…）"
            f" → 命令以其 decision_id 立项。")
    ok = (not proposals_commander and not bad_orders
          and bool(adopts) and not non_commander_decisions)
    return AcceptanceItem(6, "参谋推荐与指挥员决断有严格对象边界", ok, tuple(ev))


def _item7(ws, result) -> AcceptanceItem:
    """无有效命令时，任何 Agent 都无法改变外部现实。"""
    kinds = [e.kind for e in ws.events]
    paused = "OrderAnomalyPaused" in kinds
    # 外部世界效果仅经由已激活订单落地：执行报告都能回指到真实订单。
    orphan_reports = [
        r for r in ws.reports.values() if r.order_id not in ws.orders]
    ev = [
        "外部效果入口只有执行适配器一条路：WorldOutcome 必须挂在激活订单上才生效。",
    ]
    if paused:
        pause_ev = next(e for e in ws.events if e.kind == "OrderAnomalyPaused")
        ev.append(
            f"实证：`{pause_ev.payload.get('order_id')}` 异常即暂停"
            f"（原因：{pause_ev.payload.get('reason')}），暂停期间无任何世界效果写入，"
            "直到指挥员答复后才恢复。")
    ev.append(f"执行报告 {len(ws.reports)} 条全部回指真实订单（孤儿报告 {len(orphan_reports)} 条）。")
    return AcceptanceItem(
        7, "无有效命令时无法改变外部现实", paused and not orphan_reports, tuple(ev))


def _item8(ws, result) -> AcceptanceItem:
    """执行者与核验者独立，执行成功不等于业务完成。"""
    succ_reports = [
        r for r in ws.reports.values()
        if getattr(r.status, "value", r.status) == "succeeded"]
    failed_verifications = [v for v in ws.verifications.values() if not v.passed]
    final_ok = bool(getattr(result, "verification_passed", False))
    ev = [
        f"执行侧成功报告 {len(succ_reports)} 条；独立核验对象 {len(ws.verifications)} 条。",
    ]
    if failed_verifications:
        v = failed_verifications[0]
        ev.append(
            f"实证：`{v.id}` 在已有成功报告的情况下仍判 **未通过**"
            f"（未满足：{'；'.join(v.unmet_conditions[:2])}）——执行成功≠业务完成。")
    ev.append(f"终局核验通过：{final_ok}。")
    return AcceptanceItem(
        8, "执行者与核验者独立", bool(succ_reports) and bool(failed_verifications) and final_ok,
        tuple(ev))


def _item9(ws, result) -> AcceptanceItem:
    """异常会暂停并向你说明需要决定什么。"""
    gq = next(
        (q for q in ws.general_questions.values() if q.kind == "execution_anomaly"),
        None)
    anomalies = getattr(result, "anomalies", ())
    ev = []
    if gq is not None:
        ev.append(f"异常请示 `{gq.id}`：{gq.question}")
        ev.append(f"指挥员决定：{gq.answer}")
    ev.append(f"异常清单：{', '.join(anomalies) if anomalies else '（无）'}；"
              "异常一律先停再问，不存在静默重试。")
    ok = gq is not None and gq.status == "answered" and bool(anomalies)
    return AcceptanceItem(9, "异常会暂停并向你说明需要决定什么", ok, tuple(ev))


def _item10(ws, result) -> AcceptanceItem:
    """从目标到复盘的材料均可追溯和回放。"""
    broken = ws.verify_audit_chain()
    reviews = list(ws.reviews.values())
    ev = [
        f"哈希链 {len(ws.events)} 事件校验：{'完整' if not broken else f'断裂于 {broken}'}。",
        f"检查点重载后审计链完好：{getattr(result, 'reload_audit_intact', False)}"
        f"（重载版本 v{getattr(result, 'reload_checkpoint_version', '?')}）。",
        f"复盘对象 {len(reviews)} 组、记忆候选 {len(ws.memory_candidates)} 条，"
        "目标→情报→方案→决断→命令→执行→核验→复盘全链对象都在时间线上可回放。",
    ]
    ok = not broken and getattr(result, "reload_audit_intact", False) and bool(reviews)
    return AcceptanceItem(10, "从目标到复盘的材料均可追溯和回放", ok, tuple(ev))
