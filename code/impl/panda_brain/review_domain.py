"""复盘与经验域层（P16-T7，05 号定版）。

纯逻辑，无 HTTP 无 HTML。四块能力：

1. 时间线回放 —— 从工作区审计链确定性重建。审计流不携带时间戳，
   时间列一律如实标注「未记录」，绝不事后编造时刻；
   不用事后信息反推指责当时决策（doc 05 §3）。
2. 事件摘要与计划/实际对比 —— 每个里程碑区分
   系统记录 / 已发生但时间未记录 / 缺失（未补写），缺失不补写。
3. 问题候选生成 —— 规则驱动、只产「待核验」候选，每条带证据；
   置信度与核验状态互相独立（doc 05 §4.4）；智脑不定责（§5.1/§9）。
4. 整改任务与经验沉淀 —— 状态机 + 超期高亮 + 重开留痕；
   经验三态（候选/已确认/已失效），不覆盖当前事实与命令。

持久化：ReviewLedger 独立 JSON（同物资底账模式）。
已知边界：问题/整改/经验暂不在哈希审计链内（与 T2 二维码签发同级别
限制），WORKLOG 已记录；后续版本迁入 append_event。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

#: 审计流时间戳缺位的统一诚实标注。
NO_TIME_NOTE = "时间未记录（审计流无时钟源，待接入）"


# ---------------------------------------------------------------------------
# 1. 时间线回放


@dataclass(frozen=True)
class TimelineEntry:
    """一条回放记录：发生时间/来源/操作者/关联对象/状态（doc 05 §3）。"""

    sequence: int
    kind: str
    label: str
    actor: str
    object_ref: str
    time: str = ""  # 空＝审计流未携带时间戳


_TIMELINE_LABELS: dict[str, str] = {
    "PersonCheckedIn": "扫码入岗",
    "PersonCheckedOut": "人员离岗",
    "PersonStateMarked": "人员八态标记",
    "ReviewOwnerAssigned": "复盘负责人指派",
    "PersonLibraryImported": "长期人员库导入",
    "CommandDraftCaptured": "指令草稿捕获（未执行）",
    "CommandDraftConfirmed": "指令确认转自提方案",
    "CommandDraftDiscarded": "指令草稿作废",
    "FireEventStarted": "事件启动",
    "FireEventUpgraded": "事件升级",
    "FireEventClosed": "事件关闭",
    "EventQRIssued": "事件二维码签发",
    "EventQRRevoked": "事件二维码撤销",
    "RosterEntriesAdded": "名册登记（镜像）",
    "RosterPositionAssigned": "名册岗位指派（镜像）",
    "ReviewIssuesGenerated": "规则扫描产出候选问题",
    "ReviewHypothesesGenerated": "规则扫描产出根因假设候选",
    "ReviewIssueStatusChanged": "问题状态流转（镜像）",
    "ReviewRectDrafted": "整改草案生成（镜像）",
    "ReviewRectPublished": "整改审核发布（镜像）",
    "ReviewRectFeedbackRecorded": "责任人反馈登记（镜像）",
    "ReviewRectVerificationRecorded": "指定验收结果登记（镜像）",
    "ReviewRectRecorded": "整改任务建立（镜像）",
    "ReviewRectStatusChanged": "整改状态流转（镜像）",
    "ReviewExperienceRecorded": "经验候选登记（镜像）",
    "ReviewExperienceStatusChanged": "经验状态流转（镜像）",
    "ReviewExperienceProposed": "经验候选生成（镜像）",
    "ReviewExperienceRevised": "经验条件修订（镜像）",
    "ReviewHypothesisRecorded": "根因假设登记（镜像）",
    "ReviewHypothesisVerifiedItem": "根因假设逐条核实（镜像）",
    "ReviewHypothesisStatusChanged": "根因假设状态流转（镜像）",
    "ReviewReportVersioned": "报告新版本生成（镜像）",
    "ReviewReportEdited": "报告人工编辑（镜像）",
    "ReviewReportStatusChanged": "报告状态流转（镜像）",
    "IntelItemRecorded": "信息上报",
    "ClaimCreated": "事实记录",
    "CommanderFactReceived": "指挥员接收事实",
    "EvidenceRecorded": "证据入库",
    "AnnotationRecorded": "标绘登记",
    "AnnotationStatusChanged": "标注核验流转",
    "OptionsPrepared": "方案备妥",
    "GeneralDecisionRecorded": "指挥员决断",
    "ExecutionOrderDrafted": "命令草拟",
    "OrderSubmittedForConfirmation": "命令待确认",
    "OrderConfirmed": "命令下达确认",
    "FormalExecutionOrderPublished": "正式任务发布",
    "SquadArrivalReported": "中队到场报告",
    "SquadRosterLoaded": "扫码载入名册",
    "SquadCalloutSelected": "中队召集登记",
    "TemporaryPersonArrived": "临时支援到场",
    "CommandFlowOpened": "任务执行流程建立",
    "CommandFlowChanged": "任务执行反馈更新",
    "OrderActivated": "命令执行中",
    "OrderAnomalyPaused": "异常暂停",
    "OrderPaused": "暂停",
    "OrderResumed": "恢复执行",
    "OrderCancelled": "命令撤回",
    "OrderExpired": "命令过期",
    "OrderSuperseded": "命令被替代",
    "InformationGapOpened": "信息缺口打开",
    "InformationGapResolved": "信息缺口关闭",
    "GoalFrameRecorded": "目标框登记",
    "GoalFrameConfirmed": "目标框确认",
    "StaffPlanRecorded": "参谋计划",
    "StaffTaskRecorded": "参谋任务",
    "StaffTaskCompleted": "参谋任务完成",
    "MemoryCandidateProposed": "经验候选提出",
    "MemoryCandidateRejected": "经验候选否决",
    "ReviewRecorded": "复盘意见",
}

_OBJECT_KEYS = (
    "binding_id", "annotation_id", "order_id", "decision_id", "proposal_id",
    "gap_id", "goal_id", "task_id", "plan_id", "review_id", "candidate_id",
    "evidence_id", "question_id", "report_id", "option_id", "draft_id",
    "issue_id", "rect_id", "exp_id", "hyp_id", "qr_id",
)


def _describe_payload(payload: dict) -> tuple[str, str]:
    """从事件载荷提取 (操作者, 关联对象)；取不到就空串，不猜。"""
    actor = ""
    for key in ("operator", "confirmed_by", "by", "decided_by", "actor"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            actor = value
            break
    refs = []
    for key in _OBJECT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            refs.append(f"{key}={value}")
    for key in ("decision", "gap"):
        inner = payload.get(key)
        if isinstance(inner, dict) and inner.get("id"):
            refs.append(f"{key}.id={inner['id']}")
            if not actor:
                for akey in ("decided_by", "raised_by", "opened_by", "created_by"):
                    if isinstance(inner.get(akey), str) and inner[akey]:
                        actor = inner[akey]
                        break
    person = payload.get("person")
    if isinstance(person, dict):
        name = person.get("name") or person.get("phone") or ""
        if name:
            refs.append(f"person={name}")
            if not actor:
                actor = name
    return actor, "；".join(refs[:3])


def build_timeline(ws, limit: int | None = None) -> tuple[TimelineEntry, ...]:
    """按审计链顺序重建时间线；未知类型保留英文种类名，不丢弃。"""
    entries = []
    for event in getattr(ws, "events", ()) or []:
        payload = event.payload or {}
        actor, obj = _describe_payload(payload)
        entries.append(
            TimelineEntry(
                sequence=event.sequence,
                kind=event.kind,
                label=_TIMELINE_LABELS.get(event.kind, event.kind),
                actor=actor,
                object_ref=obj,
                time=getattr(event, "recorded_at", ""),
            )
        )
    if limit is not None and len(entries) > limit:
        entries = entries[-limit:]
    return tuple(entries)


# ---------------------------------------------------------------------------
# 2. 事件摘要与计划/实际对比

MILESTONE_PLAN_ORDER = ("队员到场", "首次侦查", "火线确认", "首批命令")


def event_digest(event, ws) -> tuple[tuple[str, str, str], ...]:
    """返回 (里程碑, 值, 数据状态) 三元组序列。

    数据状态 ∈ {系统记录, 已发生·时间未记录, 缺失（未补写）}。
    """
    rows: list[tuple[str, str, str]] = []
    discovered = getattr(event, "discovered_at", "") if event else ""
    created = getattr(event, "created_at", "") if event else ""
    closed = bool(event is not None and getattr(event, "status", "") == "closed")
    updated = getattr(event, "updated_at", "") if event else ""

    rows.append(("发现", discovered or "—", "系统记录" if discovered else "缺失（未补写）"))
    rows.append(("建事件/上报", created or "—", "系统记录" if created else "缺失（未补写）"))

    kinds_seq = {}
    for audit in (getattr(ws, 'events', ()) or []):
        kinds_seq.setdefault(audit.kind, audit.sequence)
    bound = bool(getattr(ws, 'event_id', ''))
    checkin_seq = (next((ev.sequence for ev in ws.events if ev.kind == 'SquadArrivalReported'
                        and (ev.payload or {}).get('arrived_phones')), None)
                   if bound else kinds_seq.get("PersonCheckedIn"))
    rows.append(
        ("首批到场报告" if bound else "前指成立/首批到场",
         f"审计序列 #{checkin_seq}" if checkin_seq else "—",
         "已发生·时间未记录" if checkin_seq else "缺失（未补写）")
    )
    verified_mark_seq = next(
        (ev.sequence for ev in (getattr(ws, "events", ()) or [])
         if ev.kind == "AnnotationStatusChanged"
         and (ev.payload or {}).get("to") in {"verified", "published"}),
        None,
    )
    rows.append(
        ("首态势（标注核验通过）",
         f"审计序列 #{verified_mark_seq}" if verified_mark_seq else "—",
         "已发生·时间未记录" if verified_mark_seq else "缺失（未补写）")
    )
    order_seq = next((e.sequence for e in ws.events
                      if e.kind in {'OrderConfirmed', 'FormalExecutionOrderPublished'}), None)
    rows.append(
        ("首批命令",
         f"审计序列 #{order_seq}" if order_seq else "—",
         "已发生·时间未记录" if order_seq else "缺失（未补写）")
    )
    anomalies = sum(1 for ev in (getattr(ws, "events", ()) or [])
                    if ev.kind in {"OrderAnomalyPaused", "OrderPaused"})
    rows.append(("异常暂停次数", str(anomalies), "系统记录"))
    rows.append(("结束", updated if closed else "进行中",
                 "系统记录" if closed else "—"))
    if discovered and closed and updated:
        try:
            start = datetime.fromisoformat(discovered)
            end = datetime.fromisoformat(updated)
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError('Invalid event interval')
            minutes = int((end - start).total_seconds() // 60)
            rows.append(("总时长（发现→关闭）", f"{minutes} 分钟", "系统记录"))
        except (ValueError, TypeError):
            rows.append(("总时长（发现→关闭）", "—", "缺失（时间格式无法解析，未补写）"))
    else:
        rows.append(("总时长（发现→关闭）", "—", "缺失（起止时间不全，未补写）"))
    return tuple(rows)


def plan_actual_rows(digest: tuple[tuple[str, str, str], ...]) -> tuple[dict, ...]:
    """计划/实际对比：差异只表偏差，不等同责任（doc 05 §2）。"""
    digest_map = {name: value for name, value, _ in digest}
    mapping = (
        ("队员到场", "首批到场报告" if '首批到场报告' in digest_map else "前指成立/首批到场"),
        ("首次侦查", "首次侦查（未记录）" if '首批到场报告' in digest_map else "首态势（标注核验通过）"),
        ("首批命令", "首批命令"),
    )
    rows = []
    for plan_name, actual_name in mapping:
        actual = digest_map.get(actual_name, "—")
        known = not str(actual).startswith("—") and "缺失" not in str(actual)
        rows.append({
            "item": plan_name,
            "plan": "未设置（计划基准待接入）",
            "actual": actual,
            "deviation": "—（计划基准未设置，无法计算偏差）",
            "actual_known": known,
        })
    return tuple(rows)


# ---------------------------------------------------------------------------
# 3. 问题卡片


SEVERITY_ORDER = {"关键": 0, "重要": 1, "一般": 2, "观察项": 3}
IMPACT_ORDER = {"全局": 0, "多组": 1, "局部": 2}
REVIEW_STATUS_WORDS = {
    "pending": "待核验",
    "confirmed": "已确认",
    "revised": "已修订",
    "rejected": "不成立",
    "rectified": "已转整改",
    "closed": "已关闭",
}
#: 合法流转（doc 05 §4.5）；重开由显式 reopen 动作处理。
ISSUE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "revised", "rejected"},
    "confirmed": {"rectified"},
    "revised": {"confirmed"},
    "rejected": {"closed"},
    "rectified": {"closed"},
    "closed": set(),
}


class ReviewDomainError(ValueError):
    """业务错误；消息可直接展示。"""


@dataclass
class ReviewIssue:
    """问题卡片（doc 05 §4.2 A标识/B事实/C判断/D状态四组字段）。"""

    # A 标识
    issue_id: str
    title: str
    category: str            # 指挥/作战/信息/资源后勤/通信/安全协同
    stage: str               # 所属阶段（自由文本，取自触发规则）
    event_id: str
    # B 事实
    trigger_basis: str       # 触发依据（哪条规则/哪组数据）
    description: str
    evidence_ids: tuple[str, ...] = ()
    affected: str = ""       # 影响对象
    # C 判断（需人工确认）
    impact_scope: str = "局部"   # 全局/多组/局部
    severity: str = "一般"       # 关键/重要/一般/观察项
    root_cause_hint: str = ""    # 根因假设引用（不内联长文本）
    to_verify: str = ""          # 待核验项
    confidence: str = "低"       # 高/中/低 —— 与核验状态独立
    analysis_version: str = "rule-v1"
    # D 状态
    review_status: str = "pending"
    related_issues: tuple[str, ...] = ()
    updated_by: str = ""
    updated_at: str = ""
    note: str = ""           # 否决/关闭原因等
    confirmed_assessment_ref: str = ""  # P12 负责人最近结论所依据的不可变核查版本
    decision_history: tuple[str, ...] = field(default_factory=tuple)

    def status_word(self) -> str:
        return REVIEW_STATUS_WORDS.get(self.review_status, self.review_status)


def sort_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    """默认排序：严重程度→影响范围→首次发生(以编号稳定代序)→编号。"""
    return sorted(
        issues,
        key=lambda i: (
            SEVERITY_ORDER.get(i.severity, 9),
            IMPACT_ORDER.get(i.impact_scope, 9),
            i.issue_id,
        ),
    )


def issue_can_transition(current: str, target: str) -> bool:
    return target in ISSUE_TRANSITIONS.get(current, set())


#: 复盘规则版本；规则变更时递增，卡片可追溯到产出口径。
RULE_VERSION = "review-rules-v1"

# 各规则的固定元数据（阶段/类型/严重度/置信度——全部保守取值）
_RULE_META = {
    "R1-anomaly-order": ("命令", "指挥", "重要", "高"),
    "R2-open-gap": ("态势", "信息", "一般", "高"),
    "R3-unresolved-slot": ("调配", "指挥", "观察项", "高"),
    "R4-draft-annotations": ("态势", "信息", "观察项", "中"),
    "R5-snapshot-missing": ("调配", "资源后勤", "重要", "中"),
}


def generate_issue_candidates(ws, event) -> list[ReviewIssue]:
    """规则驱动的候选问题生成。只描述真实偏差，产不出就返回空表。

    每条候选：待核验状态 + 证据（审计序列/对象 id）+ 保守置信度。
    """
    events = list(getattr(ws, "events", ()) or [])
    seq_of = {}
    for ev in events:
        seq_of.setdefault(ev.kind, []).append(ev.sequence)
    event_id = getattr(event, "event_id", "?") if event is not None else "?"
    candidates: list[ReviewIssue] = []

    def make(rule_key: str, title: str, description: str,
             evidence: tuple[str, ...], affected: str, to_verify: str) -> ReviewIssue:
        stage, category, severity, confidence = _RULE_META[rule_key]
        return ReviewIssue(
            issue_id=f"ISSUE-{rule_key}-{event_id}",
            title=title,
            category=category,
            stage=stage,
            event_id=event_id,
            trigger_basis=f"规则 {rule_key}（{RULE_VERSION}）",
            description=description,
            evidence_ids=evidence,
            affected=affected,
            impact_scope="局部",
            severity=severity,
            to_verify=to_verify,
            confidence=confidence,
            analysis_version=RULE_VERSION,
        )

    anomaly_seqs = seq_of.get("OrderAnomalyPaused", [])
    if anomaly_seqs:
        candidates.append(make(
            "R1-anomaly-order",
            f"存在异常暂停的命令（{len(anomaly_seqs)} 次）",
            "执行过程中出现异常暂停事件，需回放当时上下文判断原因。",
            tuple(f"audit#{s}" for s in anomaly_seqs),
            "相关执行中队/任务",
            "回放异常报告与当时的态势版本，确认是否为误报。",
        ))
    open_gaps = [
        g for g in (getattr(ws, "information_gaps", {}) or {}).values()
        if getattr(getattr(g, "status", None), "value", str(getattr(g, "status", ""))) == "open"
    ]
    if open_gaps:
        candidates.append(make(
            "R2-open-gap",
            f"收尾时仍有 {len(open_gaps)} 条信息缺口未关闭",
            "事件收尾节点存在未关闭的信息缺口，可能影响结论完整性。",
            tuple(f"gap:{g.id}" for g in open_gaps[:5]),
            "态势研判完整性",
            "逐条核对缺口是否已被其他渠道补齐。",
        ))
    roster_unresolved_n = _count_unresolved_roster(ws)
    if roster_unresolved_n:
        candidates.append(make(
            "R3-unresolved-slot",
            f"{roster_unresolved_n} 名入岗人员岗位未解析",
            "花名册绑定中存在岗位为空的记录，反映提前配名册环节不完整。",
            ("workspace:roster_bindings",),
            "编成岗位于首页组织架构的呈现",
            "核对是否为本职库覆盖不足或临时增援人员。",
        ))
    draft_marks = sum(
        1
        for a in (getattr(ws, "annotations", {}) or {}).values()
        if getattr(a, "status", None) is not None and a.status.value == "draft"
    )
    if draft_marks:
        candidates.append(make(
            "R4-draft-annotations",
            f"{draft_marks} 条标绘停留草稿未提交核验",
            "图上存在从未进入核验链的草稿标绘，正式态势可能不完整。",
            ("workspace:annotations[draft]",),
            "正式态势完整性",
            "确认草稿是作业残留还是遗漏提交。",
        ))
    return candidates


def _count_unresolved_roster(ws) -> int:
    bindings = getattr(ws, "roster_bindings", {}) or {}
    return sum(
        1 for b in bindings.values()
        if not getattr(b, "pos_code", "")
    )


# ---------------------------------------------------------------------------
# 4. 整改任务与经验


RECT_STATUS_WORDS = {
    "draft": "待审核草案",
    "assigned_pending": "待分派",
    "in_progress": "整改中",
    "awaiting_verify": "待验证",
    "done": "已完成",
    "closed": "已关闭",
}
RECT_TRANSITIONS: dict[str, set[str]] = {
    "assigned_pending": {"in_progress"},
    "in_progress": {"awaiting_verify"},
    "awaiting_verify": {"done", "in_progress"},   # 验证未通过 → 重新整改
    "done": {"closed"},
    "closed": set(),                               # 重开走 reopen 专用通道
}
EXP_STATUS_WORDS = {"candidate": "经验候选", "confirmed": "已确认", "expired": "已失效"}


@dataclass
class RectTask:
    """整改任务（doc 05 §8.1）。"""

    rect_id: str
    related_ids: tuple[str, ...]      # 关联问题/共性 id
    measures: str                     # 整改措施
    owner_unit: str
    owner_person: str
    priority: str = "中"              # 高/中/低
    deadline: str = ""                # YYYY-MM-DD
    verify_method: str = ""           # 验证方式
    event_id: str = ""
    status: str = "assigned_pending"
    updated_by: str = ""
    updated_at: str = ""
    history: tuple[str, ...] = field(default_factory=tuple)  # 流转留痕
    recurrence_of: str = ""           # 复现链回原任务 id
    source_issue_id: str = ""
    source_hyp_id: str = ""
    source_assessment_refs: tuple[str, ...] = ()
    acceptance_standard: str = ""
    verifier_name: str = ""
    responsible_phone: str = ""
    verifier_phone: str = ""
    published_by: str = ""
    published_at: str = ""
    feedback_records: tuple[dict, ...] = field(default_factory=tuple)
    verification_records: tuple[dict, ...] = field(default_factory=tuple)

    def status_word(self) -> str:
        return RECT_STATUS_WORDS.get(self.status, self.status)

    def is_overdue(self, today: date | None = None) -> bool:
        if not self.deadline or self.status in {"done", "closed"}:
            return False
        try:
            deadline = date.fromisoformat(self.deadline)
        except ValueError:
            return False
        return (today or date.today()) > deadline


def rect_can_transition(current: str, target: str) -> bool:
    return target in RECT_TRANSITIONS.get(current, set())


@dataclass
class Experience:
    """经验条目三态（doc 05 §10）：候选/已确认/已失效。"""

    exp_id: str
    content: str
    applicability: str        # 适用条件
    source_event_id: str
    status: str = "candidate"
    confirmed_by: str = ""
    expiry_condition: str = ""
    updated_by: str = ""
    updated_at: str = ""
    evidence_ids: tuple[str, ...] = ()
    limitations: str = ""
    source_rect_id: str = ""
    source_issue_id: str = ""
    source_hyp_id: str = ""
    condition_tags: tuple[str, ...] = ()
    counter_refs: tuple[str, ...] = ()
    history: tuple[str, ...] = field(default_factory=tuple)
    version: int = 1

    def status_word(self) -> str:
        return EXP_STATUS_WORDS.get(self.status, self.status)


# ---------------------------------------------------------------------------
# 根因假设（doc 05 §5：三类结论边界 / 相关·非因果 / 责任认定永不入 AI 卡片）

HYP_STATUS_WORDS = {"pending": "待验证", "verified": "已验证", "rejected": "不成立"}
HYP_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"verified", "rejected"},
    "verified": {"rejected"},   # 后续反证可推翻，留痕
    "rejected": {"pending"},    # 新证据重开，留痕
}
_CORRELATION_LEVELS = ("强", "中", "弱")
_CONFIDENCE_LEVELS = ("高", "中", "低")


def hyp_can_transition(current: str, target: str) -> bool:
    return target in HYP_TRANSITIONS.get(current, set())


def _validate_hypothesis(hyp: "RootCauseHypothesis") -> None:
    if not hyp.statement.strip():
        raise ReviewDomainError("原因假设内容不能为空")
    if hyp.correlation not in _CORRELATION_LEVELS:
        raise ReviewDomainError(
            f"相关性强度只能是 {'/'.join(_CORRELATION_LEVELS)}"
        )
    if hyp.confidence not in _CONFIDENCE_LEVELS:
        raise ReviewDomainError(f"置信度只能是 {'/'.join(_CONFIDENCE_LEVELS)}")
    for item in hyp.to_verify:
        if not str(item).strip():
            raise ReviewDomainError("待验证事实条目不能为空")


@dataclass
class RootCauseHypothesis:
    """根因假设卡（doc 05 §5.2 八字段）。

    因果纪律（§5.3）落为结构约束：
    - 所有输出只表述「相关」，相关性强度字段强制存在；
    - 「待验证事实」全部补足前，状态机拒绝进入「已验证」；
    - 域模型没有责任认定字段——定责只在负责人手里，不入系统卡片。
    """

    hyp_id: str                          # 假设编号（可被问题卡 root_cause_hint 引用）
    statement: str                       # 原因假设（一句话，只表述相关）
    correlation: str = "弱"              # 相关性强度 强/中/弱——属相关而非因果
    evidence_ids: tuple[str, ...] = ()   # 支持证据（审计序号/对象 id，逐条可跳转）
    counter_examples: str = ""           # 反例：不支持该假设的样本/事件
    to_verify: tuple[str, ...] = ()      # 待验证事实：补足才能从相关升为因果
    resolved_items: tuple[str, ...] = () # 已逐条核实的待验证事实原文
    verification_log: tuple[str, ...] = ()
    confidence: str = "低"               # 高/中/低——与结论状态互相独立
    status: str = "pending"
    related_issues: tuple[str, ...] = ()
    origin: str = "manual"               # manual / 规则键
    event_id: str = ""
    updated_by: str = ""
    updated_at: str = ""
    note: str = ""                       # 判定不成立依据等
    history: tuple[str, ...] = field(default_factory=tuple)
    confirmed_assessment_ref: str = ""

    def status_word(self) -> str:
        return HYP_STATUS_WORDS.get(self.status, self.status)

    def unresolved_verifications(self) -> tuple[str, ...]:
        done = set(self.resolved_items)
        return tuple(item for item in self.to_verify if item not in done)


def generate_hypothesis_candidates(ws, event) -> list[RootCauseHypothesis]:
    """规则驱动的根因假设候选：只从同期出现的真实信号推「相关性」。

    单一信号不产假设；两条独立信号同期出现才产出弱相关候选，
    并强制携带待验证事实清单。产不出就返回空表，不编造因果。
    """
    event_id = getattr(event, "event_id", "?") if event is not None else "?"
    events = list(getattr(ws, "events", ()) or [])
    seq_of: dict[str, list] = {}
    for ev in events:
        seq_of.setdefault(ev.kind, []).append(ev.sequence)
    anomaly_seqs = seq_of.get("OrderAnomalyPaused", [])
    open_gaps = [
        g for g in (getattr(ws, "information_gaps", {}) or {}).values()
        if getattr(getattr(g, "status", None), "value",
                   str(getattr(g, "status", ""))) == "open"
    ]
    draft_marks = sum(
        1 for a in (getattr(ws, "annotations", {}) or {}).values()
        if getattr(a, "status", None) is not None and a.status.value == "draft"
    )
    roster_unresolved_n = _count_unresolved_roster(ws)

    candidates: list[RootCauseHypothesis] = []

    def make(rule_key: str, statement: str, evidence: tuple[str, ...],
             to_verify: tuple[str, ...], related: tuple[str, ...]) -> RootCauseHypothesis:
        return RootCauseHypothesis(
            hyp_id=f"HYP-{rule_key}-{event_id}",
            statement=statement,
            correlation="弱",
            evidence_ids=evidence,
            to_verify=to_verify,
            related_issues=related,
            origin=rule_key,
            event_id=event_id,
        )

    if anomaly_seqs and open_gaps:
        candidates.append(make(
            "R6-corr-gap-order",
            "命令执行异常与态势信息缺口未闭合同期出现，可能互为关联因素。",
            tuple(f"audit#{s}" for s in anomaly_seqs[:3])
            + tuple(f"gap:{g.id}" for g in open_gaps[:3]),
            ("回放异常命令下达时刻的态势版本，核对当时信息缺口清单",
             "核对异常暂停报告结论是否提及情报缺失"),
            (f"ISSUE-R1-anomaly-order-{event_id}",
             f"ISSUE-R2-open-gap-{event_id}"),
        ))
    if draft_marks and roster_unresolved_n:
        candidates.append(make(
            "R7-corr-draft-slot",
            "标绘作业停留草稿与入岗人员岗位未解析同期出现，可能互为关联因素。",
            ("workspace:annotations[draft]", "workspace:roster_bindings"),
            ("核对草稿标绘作者与未解析岗位人员是否同组",
             "确认当日作业交接记录是否覆盖该岗位"),
            (f"ISSUE-R4-draft-annotations-{event_id}",
             f"ISSUE-R3-unresolved-slot-{event_id}"),
        ))
    return candidates


# ---------------------------------------------------------------------------
# 报告版本（doc 05 §12：AI 草稿→人工编辑→待审核→已发布→已归档；新证据→新版本）


REPORT_STATUS_WORDS = {
    "ai_draft": "系统草稿",
    "edited": "人工编辑",
    "pending_review": "待审核",
    "published": "已发布",
    "archived": "已归档",
}
REPORT_TRANSITIONS: dict[str, set[str]] = {
    "ai_draft": {"edited"},
    "edited": {"pending_review"},
    "pending_review": {"published", "edited"},   # 审核可退回修改
    "published": {"archived"},
    "archived": set(),                             # 新证据 → 生成新版本，不改旧版
}


def report_can_transition(current: str, target: str) -> bool:
    return target in REPORT_TRANSITIONS.get(current, set())


@dataclass
class ReviewReport:
    """复盘报告快照：一个版本一份内容，历史不静默覆盖（doc 05 §12）。"""

    report_id: str            # RPT-{event}-v{n}
    event_id: str
    version: int
    status: str = "ai_draft"
    content_md: str = ""     # 生成/编辑时刻的全文快照，发布后不可变
    created_at: str = ""
    edited_by: str = ""
    edited_at: str = ""
    history: tuple[str, ...] = field(default_factory=tuple)
    source_snapshot_json: str = ""  # 生成时事件来源，旧记录默认空且不可直接发布
    source_digest: str = ""
    source_workspace_version: int = 0
    source_audit_sequence: int = 0
    reviewed_by: str = ""
    reviewed_at: str = ""
    imported_from_report_id: str = ""
    imported_file_sha256: str = ""
    word_import_request_id: str = ""
    stage_report: bool = False

    def status_word(self) -> str:
        return REPORT_STATUS_WORDS.get(self.status, self.status)


# ---------------------------------------------------------------------------
# 5. 持久化（独立 JSON，同物资底账模式）


class ReviewLedger:
    """问题/整改/经验的本地底账。原子落盘 tmp+os.replace。"""

    def __init__(self) -> None:
        self.issues: dict[str, ReviewIssue] = {}
        self.rects: dict[str, RectTask] = {}
        self.experiences: dict[str, Experience] = {}
        self.hypotheses: dict[str, RootCauseHypothesis] = {}
        self.reports: dict[str, ReviewReport] = {}
        #: 复盘负责人（doc 05 §9）：由指挥员指派，全权负责核验/定责/发布。
        self.owner_phone: str = ""
        self.owner_name: str = ""

    # -- issues ---------------------------------------------------------

    def add_issue(self, issue: ReviewIssue) -> ReviewIssue:
        if issue.issue_id in self.issues:
            raise ReviewDomainError(f"问题编号已存在：{issue.issue_id}")
        self.issues[issue.issue_id] = issue
        return issue

    def transition_issue(self, issue_id: str, target: str, *,
                         operator: str, stamp: str, reason: str = "") -> ReviewIssue:
        issue = self._get(self.issues, issue_id, "问题")
        if target == issue.review_status:
            return issue  # 幂等重复提交直接跳过
        if not issue_can_transition(issue.review_status, target):
            raise ReviewDomainError(
                f"问题状态不允许从 {issue.status_word()} 变为 "
                f"{REVIEW_STATUS_WORDS.get(target, target)}"
            )
        if target in {"rejected", "closed"} and not reason.strip():
            raise ReviewDomainError("否决/关闭必须附原因（规范 05·§4.5）")
        updated = replace(
            issue, review_status=target,
            updated_by=operator, updated_at=stamp,
            note=(f"{issue.note}｜{stamp} {operator}: {reason}".strip("｜") if reason else issue.note),
        )
        self.issues[issue_id] = updated
        return updated

    def reopen_issue(self, issue_id: str, *, operator: str, stamp: str,
                     reason: str) -> ReviewIssue:
        """新证据进入 → 重开为待核验（doc 05 §4.5 状态流转末行）。"""
        issue = self._get(self.issues, issue_id, "问题")
        if not reason.strip():
            raise ReviewDomainError("重开必须说明新证据内容")
        updated = replace(
            issue, review_status="pending",
            updated_by=operator, updated_at=stamp,
            note=f"{issue.note}｜{stamp} {operator} 重开: {reason}".strip("｜"),
        )
        self.issues[issue_id] = updated
        return updated

    # -- rects ----------------------------------------------------------

    def add_rect(self, task: RectTask) -> RectTask:
        if task.rect_id in self.rects:
            raise ReviewDomainError(f"整改编号已存在：{task.rect_id}")
        unknown = [rid for rid in task.related_ids if rid not in self.issues]
        for rid in unknown:
            # 关联对象允许是共性规律 id（本切片尚未建模），只对确定不存在且
            # 非共性前缀的对象报错，避免把合法引用当脏数据拒绝。
            if rid.startswith("ISSUE-"):
                raise ReviewDomainError(f"关联问题不存在：{rid}")
        self.rects[task.rect_id] = task
        # 关联的问题自动进入「已转整改」语义的前置：仅标记，不改状态；
        # 状态仍由人工显式流转，保持审核关卡纪律。
        return task

    def transition_rect(self, rect_id: str, target: str, *,
                        operator: str, stamp: str, reason: str = "") -> RectTask:
        task = self._get(self.rects, rect_id, "整改任务")
        if target == task.status:
            return task
        if target == "in_progress" and task.status == "closed":
            raise ReviewDomainError("已关闭任务的改动须走重开流程并记录原因")
        if not rect_can_transition(task.status, target):
            raise ReviewDomainError(
                f"整改状态不允许从 {task.status_word()} 变为 "
                f"{RECT_STATUS_WORDS.get(target, target)}"
            )
        line = f"{stamp} {operator}: {task.status_word()}→{RECT_STATUS_WORDS[target]}"
        if reason.strip():
            line += f"（{reason.strip()}）"
        updated = replace(
            task, status=target, updated_by=operator, updated_at=stamp,
            history=task.history + (line,),
        )
        self.rects[rect_id] = updated
        return updated

    def mark_recurrence(self, rect_id: str, new_issue_id: str) -> RectTask:
        """整改后复现：新问题链回原任务，原任务提示复现待评估。"""
        task = self._get(self.rects, rect_id, "整改任务")
        line = f"复现标记 ← {new_issue_id}"
        updated = replace(
            task, recurrence_of=task.recurrence_of or task.rect_id,
            history=task.history + (line,),
        )
        self.rects[rect_id] = updated
        return updated

    # -- experiences ------------------------------------------------------

    def add_experience(self, exp: Experience) -> Experience:
        if exp.exp_id in self.experiences:
            raise ReviewDomainError(f"经验编号已存在：{exp.exp_id}")
        self.experiences[exp.exp_id] = exp
        return exp

    def set_experience_status(self, exp_id: str, status: str, *,
                              operator: str, stamp: str) -> Experience:
        exp = self._get(self.experiences, exp_id, "经验")
        if status not in EXP_STATUS_WORDS:
            raise ReviewDomainError(f"未知经验状态：{status}")
        if status == "confirmed" and exp.status != "candidate":
            raise ReviewDomainError("只有经验候选项可被确认为正式经验")
        updated = replace(exp, status=status, confirmed_by=(
            operator if status == "confirmed" else exp.confirmed_by
        ), updated_by=operator, updated_at=stamp)
        self.experiences[exp_id] = updated
        return updated

    # -- hypotheses -------------------------------------------------------

    def add_hypothesis(self, hyp: RootCauseHypothesis) -> RootCauseHypothesis:
        if hyp.hyp_id in self.hypotheses:
            raise ReviewDomainError(f"假设编号已存在：{hyp.hyp_id}")
        _validate_hypothesis(hyp)
        self.hypotheses[hyp.hyp_id] = hyp
        return hyp

    def transition_hypothesis(self, hyp_id: str, target: str, *,
                              operator: str, stamp: str,
                              reason: str = "") -> RootCauseHypothesis:
        """状态流转；「已验证」带因果硬门禁（doc 05 §5.3）。"""
        hyp = self._get(self.hypotheses, hyp_id, "根因假设")
        if target == hyp.status:
            return hyp
        if not hyp_can_transition(hyp.status, target):
            raise ReviewDomainError(
                f"假设状态不允许从 {hyp.status_word()} 变为 "
                f"{HYP_STATUS_WORDS.get(target, target)}"
            )
        reason = (reason or "").strip()
        if target == "verified":
            if not hyp.to_verify:
                raise ReviewDomainError(
                    "无待验证事实清单不得判定「已验证」——"
                    "「相关」升「因果」必须先列出并补足待验证事实（规范 05·§5.3）"
                )
            unresolved = hyp.unresolved_verifications()
            if unresolved:
                raise ReviewDomainError(
                    f"尚有 {len(unresolved)} 条待验证事实未核实"
                    f"（{'；'.join(unresolved)}）——相关不得直接写成因果"
                )
        if target == "rejected" and not reason:
            raise ReviewDomainError("判定不成立必须附依据（反例或核查结论）")
        line = (
            f"{stamp} {operator}: {hyp.status_word()}→"
            f"{HYP_STATUS_WORDS[target]}"
            + (f"（{reason}）" if reason else "")
        )
        updated = replace(
            hyp, status=target,
            note=reason if target == "rejected" else hyp.note,
            updated_by=operator, updated_at=stamp,
            history=hyp.history + (line,),
        )
        self.hypotheses[hyp_id] = updated
        return updated

    def resolve_verification(self, hyp_id: str, item: str, answer: str,
                             *, operator: str, stamp: str) -> RootCauseHypothesis:
        """逐条核实待验证事实；全部补足后才允许判「已验证」。"""
        hyp = self._get(self.hypotheses, hyp_id, "根因假设")
        item = (item or "").strip()
        answer = (answer or "").strip()
        if hyp.status != "pending":
            raise ReviewDomainError("仅待验证状态可补核实记录")
        if item not in hyp.to_verify:
            raise ReviewDomainError(f"待验证事实不存在：{item[:40] or '（空）'}")
        if item in hyp.resolved_items:
            raise ReviewDomainError("该条待验证事实已核实过")
        if not answer:
            raise ReviewDomainError("核实结论不能为空")
        line = f"{stamp} {operator}: 已核实【{item}】→{answer}"
        updated = replace(
            hyp,
            resolved_items=hyp.resolved_items + (item,),
            verification_log=hyp.verification_log + (line,),
            updated_by=operator, updated_at=stamp,
            history=hyp.history + (line,),
        )
        self.hypotheses[hyp_id] = updated
        return updated

    # -- reports ----------------------------------------------------------

    def next_report_version(self, event_id: str) -> int:
        """同事件报告的最新版本号 +1；无历史从 1 起。"""
        versions = [
            r.version for r in self.reports.values()
            if r.event_id == event_id
        ]
        return (max(versions) + 1) if versions else 1

    def add_report(self, report: ReviewReport) -> ReviewReport:
        if report.report_id in self.reports:
            raise ReviewDomainError(f"报告编号已存在：{report.report_id}")
        self.reports[report.report_id] = report
        return report

    def edit_report(self, report_id: str, content_md: str, *,
                    operator: str, stamp: str) -> ReviewReport:
        """人工编辑：仅 AI 草稿/人工编辑态可改；发布/归档后不可变。"""
        report = self._get(self.reports, report_id, "报告")
        if report.status in {"published", "archived"}:
            raise ReviewDomainError(
                f"{report_id} 已{report.status_word()}，内容不可变——"
                "新证据请生成新版本（规范 05·§12）"
            )
        if not content_md.strip():
            raise ReviewDomainError("编辑内容不能为空")
        line = f"{stamp} {operator}: 人工编辑保存"
        updated = replace(
            report, content_md=content_md, status="edited",
            edited_by=operator, edited_at=stamp,
            history=report.history + (line,),
        )
        self.reports[report_id] = updated
        return updated

    def transition_report(self, report_id: str, target: str, *,
                          operator: str, stamp: str) -> ReviewReport:
        report = self._get(self.reports, report_id, "报告")
        if target == report.status:
            return report
        if not report_can_transition(report.status, target):
            raise ReviewDomainError(
                f"报告状态不允许从 {report.status_word()} 变为 "
                f"{REPORT_STATUS_WORDS.get(target, target)}"
            )
        line = (
            f"{stamp} {operator}: {report.status_word()}→"
            f"{REPORT_STATUS_WORDS[target]}"
        )
        updated = replace(
            report, status=target,
            reviewed_by=operator if target == "published" else report.reviewed_by,
            reviewed_at=stamp if target == "published" else report.reviewed_at,
            history=report.history + (line,),
        )
        self.reports[report_id] = updated
        return updated

    # -- io ----------------------------------------------------------------

    @staticmethod
    def _get(mapping: dict, key: str, noun: str):
        obj = mapping.get(key)
        if obj is None:
            raise ReviewDomainError(f"{noun}不存在：{key}")
        return obj

    def save(self, path: str | Path) -> None:
        from .durable_storage import before_write
        before_write(path)
        data = {
            "issues": {k: asdict(v) for k, v in self.issues.items()},
            "rects": {k: asdict(v) for k, v in self.rects.items()},
            "experiences": {k: asdict(v) for k, v in self.experiences.items()},
            "hypotheses": {k: asdict(v) for k, v in self.hypotheses.items()},
            "reports": {k: asdict(v) for k, v in self.reports.items()},
            "owner": {"phone": self.owner_phone, "name": self.owner_name},
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)

    @classmethod
    def load(cls, path: str | Path) -> "ReviewLedger":
        ledger = cls()
        target = Path(path)
        if not target.exists():
            return ledger
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ledger  # 损坏文件按空底账起步，不静默伪造内容
        owner = data.get("owner") or {}
        ledger.owner_phone = owner.get("phone", "")
        ledger.owner_name = owner.get("name", "")
        for key, raw in (data.get("issues") or {}).items():
            raw["evidence_ids"] = tuple(raw.get("evidence_ids") or ())
            raw["related_issues"] = tuple(raw.get("related_issues") or ())
            raw["decision_history"] = tuple(raw.get("decision_history") or ())
            ledger.issues[key] = ReviewIssue(**raw)
        for key, raw in (data.get("rects") or {}).items():
            raw["related_ids"] = tuple(raw.get("related_ids") or ())
            raw["history"] = tuple(raw.get("history") or ())
            raw["source_assessment_refs"] = tuple(raw.get("source_assessment_refs") or ())
            raw["feedback_records"] = tuple({**row, 'evidence_refs': tuple(row.get('evidence_refs') or ())}
                                            for row in (raw.get("feedback_records") or ()))
            raw["verification_records"] = tuple({**row, 'evidence_refs': tuple(row.get('evidence_refs') or ())}
                                                for row in (raw.get("verification_records") or ()))
            ledger.rects[key] = RectTask(**raw)
        for key, raw in (data.get("experiences") or {}).items():
            raw["evidence_ids"] = tuple(raw.get("evidence_ids") or ())
            raw["condition_tags"] = tuple(raw.get("condition_tags") or ())
            raw["counter_refs"] = tuple(raw.get("counter_refs") or ())
            raw["history"] = tuple(raw.get("history") or ())
            ledger.experiences[key] = Experience(**raw)
        for key, raw in (data.get("hypotheses") or {}).items():
            raw["evidence_ids"] = tuple(raw.get("evidence_ids") or ())
            raw["to_verify"] = tuple(raw.get("to_verify") or ())
            raw["resolved_items"] = tuple(raw.get("resolved_items") or ())
            raw["verification_log"] = tuple(raw.get("verification_log") or ())
            raw["related_issues"] = tuple(raw.get("related_issues") or ())
            raw["history"] = tuple(raw.get("history") or ())
            ledger.hypotheses[key] = RootCauseHypothesis(**raw)
        for key, raw in (data.get("reports") or {}).items():
            raw["history"] = tuple(raw.get("history") or ())
            ledger.reports[key] = ReviewReport(**raw)
        return ledger


# ---------------------------------------------------------------------------
# 复盘问询引擎（doc 05 §11：只读不改；回答七字段；命令意图一律不执行）


@dataclass(frozen=True)
class ReviewAnswer:
    """doc 05 §11 要求的七字段回答。topic 为内部主题标记。"""

    topic: str
    conclusion: str                  # 结论
    scope: str                       # 数据范围
    references: tuple[str, ...]      # 引用事件
    evidence: tuple[str, ...]        # 证据链接（台账/审计条目标识）
    statistics: str                  # 统计口径
    excluded: str                    # 未纳入数据
    confidence: str                  # 置信度
    context_event_id: str = ''
    context_role: str = ''
    context_subject: str = ''
    facts: tuple[str, ...] = ()
    analysis: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    focus_ids: tuple[str, ...] = ()
    source_snapshot_json: str = ''
    source_digest: str = ''
    answered_at: str = ''


_REVIEW_TOPICS: tuple[tuple[str, str], ...] = (
    ("根因", "root"), ("因果", "root"),
    ("共性", "common"), ("规律", "common"), ("多次", "common"),
    ("对比", "common"), ("复现", "common"),
    ("问题", "issues"), ("隐患", "issues"),
    ("整改", "rect"), ("跟踪", "rect"),
    ("经验", "exp"), ("教训", "exp"),
    ("报告", "report"), ("版本", "report"),
    ("时间线", "timeline"), ("经过", "timeline"), ("过程", "timeline"),
)

#: 命令意图动词（确定性匹配）；与疑问词同现时视为分析性提问。
_COMMAND_VERBS = ("下达", "签发", "下发命令", "派出", "调派", "执行一下")
_QUESTION_MARKS = (
    "哪些", "多少", "几条", "什么", "为何", "为什么",
    "吗", "？", "?", "怎么", "如何",
)

REVIEW_ASK_NOTE = (
    "复盘问询为只读分析，不改变任何状态；支持主题：问题、根因假设、整改、经验、"
    "报告版本、共性对比、时间线，以及沿用沙盘的态势主题（队伍/标绘/水源/"
    "物资/决策/命令等），同数据同权限过滤。复盘页不支持下达命令。"
)

_MEMBER_VISIBLE_ISSUE_STATUS = frozenset({"confirmed", "revised", "rectified", "closed"})


def has_command_intent(question: str) -> bool:
    """命令意图判定：命中意图动词且不含任何疑问标记。"""
    verb_hit = any(v in question for v in _COMMAND_VERBS)
    asking = any(m in question for m in _QUESTION_MARKS)
    return verb_hit and not asking


def _review_route(question: str) -> tuple[str | None, tuple[str, ...]]:
    hits: list[str] = []
    for keyword, topic in _REVIEW_TOPICS:
        if keyword in question and topic not in hits:
            hits.append(topic)
    return (hits[0], tuple(hits)) if hits else (None, ())


def _visible_hypotheses(ledger: "ReviewLedger", role: str):
    """根因假设按角色可见性：成员只见「已验证」，其余如实计入未纳入。"""
    hyps = list(ledger.hypotheses.values())
    if role == "commander":
        return hyps, ""
    if role == "member":
        shown = [h for h in hyps if h.status == "verified"]
        hidden = len(hyps) - len(shown)
        note = (
            f"另有 {hidden} 条待验证/不成立假设未纳入你的可见范围"
            if hidden else ""
        )
        return shown, note
    return [], "当前角色无复盘数据查看权限"


def _visible_issues(ledger: "ReviewLedger", role: str):
    issues = list(ledger.issues.values())
    if role == "commander":
        return issues, ""
    if role == "member":
        hidden = [
            i for i in issues
            if i.review_status not in _MEMBER_VISIBLE_ISSUE_STATUS
        ]
        note = (
            f"另有 {len(hidden)} 条待核验/不成立记录未纳入你的可见范围"
            if hidden else ""
        )
        return [
            i for i in issues if i.review_status in _MEMBER_VISIBLE_ISSUE_STATUS
        ], note
    return [], "当前角色无复盘数据查看权限"


def answer_review_inquiry(
    question: str,
    *,
    ledger: "ReviewLedger",
    workspace,
    event_id: str = "",
    role: str = "commander",
    materials_ledger=None,
    mask_phone_fn=None,
) -> ReviewAnswer:
    """复盘问询：只读检索。查不到就如实说；证据不足明确说明。"""
    text = (question or "").strip()

    def _none(conclusion: str, *, excluded: str = "", topic: str = "none") -> ReviewAnswer:
        return ReviewAnswer(
            topic=topic,
            conclusion=conclusion,
            scope="复盘台账 + 当前事件工作区（实时读取）",
            references=(event_id,) if event_id else (),
            evidence=(),
            statistics="—",
            excluded=excluded,
            confidence="—",
        )

    if not text:
        return _none("问题不能为空。")

    # 命令意图一律不执行（doc 05 §11）
    if has_command_intent(text):
        return ReviewAnswer(
            topic="command_refused",
            conclusion="复盘页为分析视图，不支持下达命令。",
            scope="仅分析视图，无命令通道",
            references=(),
            evidence=(),
            statistics="—",
            excluded="命令类操作请回到沙盘按指挥链执行",
            confidence="—",
        )

    primary, all_hits = _review_route(text)
    if getattr(workspace, 'event_id', '') and primary == 'common':
        return _none('本期只复盘本事件；未开放跨事件共性、趋势或复现分析。', topic='common')

    if primary is None:
        # 沿用沙盘问询引擎：同数据同权限过滤（doc 05 §11）
        from panda_brain import sandtable  # 局部导入避免环依赖
        s_primary, _ = sandtable.route_question(text)
        if s_primary is None:
            return _none(
                "无法从当前数据回答该问题。" + REVIEW_ASK_NOTE,
            )
        base = sandtable.answer_inquiry(
            text,
            workspace,
            event=None,
            event_id=event_id,
            ledger=materials_ledger,
            role=role,
            mask_phone_fn=mask_phone_fn,
        )
        return ReviewAnswer(
            topic=base.topic,
            conclusion=base.conclusion,
            scope="沙盘工作区实时数据（与沙盘问询同源同权限）",
            references=(event_id,) if event_id else (),
            evidence=tuple(base.sources),
            statistics="按工作区审计流与注册表实时统计",
            excluded=base.unconfirmed or "无",
            confidence=base.confidence,
        )

    today = date.today().isoformat()

    if primary == "issues":
        visible, hidden_note = _visible_issues(ledger, role)
        if not visible:
            return _none(
                "尚无可查看的问题记录（证据不足）。" + hidden_note,
                excluded=hidden_note or "无",
                topic="issues",
            )
        by_status: dict[str, int] = {}
        for issue in visible:
            by_status[issue.review_status] = by_status.get(issue.review_status, 0) + 1
        dist = "、".join(
            f"{REVIEW_STATUS_WORDS.get(k, k)} {v} 条"
            for k, v in sorted(by_status.items())
        )
        open_items = [
            i for i in visible if i.review_status in {"pending", "confirmed", "revised"}
        ]
        sev_words = {"critical": "关键", "major": "重要", "minor": "一般", "observe": "观察项"}
        sev = {}
        for issue in open_items:
            sev[issue.severity] = sev.get(issue.severity, 0) + 1
        sev_txt = "、".join(
            f"{sev_words.get(k, k)} {v} 条" for k, v in sorted(sev.items())
        ) or "无开放问题"
        rejected_n = sum(1 for i in visible if i.review_status == "rejected")
        excluded_bits = [b for b in (
            hidden_note,
            f"另有 {rejected_n} 条已判定不成立，不计入开放问题" if rejected_n else "",
        ) if b]
        refs = sorted({i.event_id for i in visible if i.event_id})
        return ReviewAnswer(
            topic="issues",
            conclusion=(
                f"可查看问题共 {len(visible)} 条（{dist}）；"
                f"开放问题严重度分布：{sev_txt}。"
            ),
            scope=f"复盘台账问题分册（事件范围：{'、'.join(refs) if refs else '当前'}）",
            references=tuple(refs),
            evidence=tuple(f"issue:{i.issue_id}" for i in visible[:8]),
            statistics=(
                "按复盘台账实时统计；待核验条目为规则候选"
                "（review-rules-v1），未经人工确认不作为结论"
            ),
            excluded="；".join(excluded_bits) or "无",
            confidence="高" if all(i.review_status != "pending" for i in open_items) else "中",
        )

    if primary == "root":
        visible_h, hidden_note = _visible_hypotheses(ledger, role)
        if not visible_h:
            return _none(
                "尚无可查看的根因假设（证据不足）。" + hidden_note,
                excluded=hidden_note or "无",
                topic="root",
            )
        by_status: dict[str, int] = {}
        for h in visible_h:
            by_status[h.status] = by_status.get(h.status, 0) + 1
        dist = "、".join(
            f"{HYP_STATUS_WORDS.get(k, k)} {v} 条"
            for k, v in sorted(by_status.items())
        )
        head = visible_h[0].statement[:40]
        refs = sorted({h.event_id for h in visible_h if h.event_id})
        return ReviewAnswer(
            topic="root",
            conclusion=(
                f"根因假设共 {len(visible_h)} 条（{dist}）；"
                f"最新一条：{head}…。均为相关性表述，非因果认定。"
            ),
            scope=(
                "复盘台账根因假设分册"
                f"（事件范围：{'、'.join(refs) if refs else '当前'}）"
            ),
            references=tuple(refs),
            evidence=tuple(f"hyp:{h.hyp_id}" for h in visible_h[:8]),
            statistics=(
                "相关性强度（强/中/弱）属相关而非因果；"
                "待验证事实逐条补足后状态机才允许标「已验证」"
            ),
            excluded=hidden_note or "无",
            confidence=(
                "高" if any(h.status == "verified" for h in visible_h) else "中"
            ),
        )

    if primary == "rect":
        rects = list(ledger.rects.values())
        if not rects:
            return _none("尚无整改任务记录（证据不足）。", topic="rect")
        by_status = {}
        overdue = []
        for rect in rects:
            by_status[rect.status] = by_status.get(rect.status, 0) + 1
            if rect.deadline and rect.deadline < today and rect.status not in {"done", "closed"}:
                overdue.append(rect.rect_id)
        dist = "、".join(
            f"{RECT_STATUS_WORDS.get(k, k)} {v} 条"
            for k, v in sorted(by_status.items())
        )
        return ReviewAnswer(
            topic="rect",
            conclusion=(
                f"整改任务共 {len(rects)} 条（{dist}）。"
                + (f"超期未闭环：{'、'.join(overdue)}。" if overdue else "无超期未闭环任务。")
            ),
            scope="复盘台账整改分册（全部事件）",
            references=tuple(sorted({r.event_id for r in rects if r.event_id})),
            evidence=tuple(f"rect:{r.rect_id}" for r in rects[:8]),
            statistics="超期＝截止日早于今日且状态非已完成/已关闭；验证未通过会回炉重做",
            excluded="无" if role == "commander" else "整改分册对成员全量可见",
            confidence="高",
        )

    if primary == "exp":
        exps = list(ledger.experiences.values())
        if role == "member":
            shown = [e for e in exps if e.status == "confirmed"]
            hidden_n = len(exps) - len(shown)
            hidden_note = f"另有 {hidden_n} 条候选/失效经验未纳入你的可见范围" if hidden_n else ""
        else:
            shown, hidden_note = exps, ""
        if not shown:
            return _none("尚无可查看的经验记录（证据不足）。" + hidden_note, topic="exp")
        by_status = {}
        for exp in shown:
            by_status[exp.status] = by_status.get(exp.status, 0) + 1
        dist = "、".join(
            f"{EXP_STATUS_WORDS.get(k, k)} {v} 条"
            for k, v in sorted(by_status.items())
        )
        head = shown[0].content[:40]
        return ReviewAnswer(
            topic="exp",
            conclusion=f"经验沉淀共 {len(shown)} 条（{dist}）；最近一条：{head}…",
            scope="复盘台账经验分册",
            references=tuple(sorted({e.source_event_id for e in shown if e.source_event_id})),
            evidence=tuple(f"exp:{e.exp_id}" for e in shown[:8]),
            statistics="只有候选经人工确认才计入正式经验；不覆盖当前事实与命令",
            excluded=hidden_note or "无",
            confidence="高",
        )

    if primary == "report":
        reports = sorted(
            ledger.reports.values(), key=lambda r: (r.event_id, -r.version)
        )
        if role == "member":
            shown = [r for r in reports if r.status == "published"]
            hidden_note = (
                f"另有 {len(reports) - len(shown)} 个非发布版本未纳入你的可见范围"
                if len(shown) < len(reports) else ""
            )
        else:
            shown, hidden_note = reports, ""
        if not shown:
            return _none("尚无可查看的报告版本（证据不足）。" + hidden_note, topic="report")
        latest = shown[0]
        listing = "；".join(
            f"{r.report_id}（{REPORT_STATUS_WORDS.get(r.status, r.status)}）"
            for r in shown[:5]
        )
        return ReviewAnswer(
            topic="report",
            conclusion=f"报告版本：{listing}。最新版本为 {latest.report_id}。",
            scope="复盘台账报告分册（每版本为生成时点的全文快照）",
            references=tuple(sorted({r.event_id for r in shown if r.event_id})),
            evidence=tuple(f"report:{r.report_id}" for r in shown[:8]),
            statistics="新证据 → 新版本；发布/归档后内容不可变",
            excluded=hidden_note or "无",
            confidence="高",
        )

    if primary == "timeline":
        entries = build_timeline(workspace)
        if not entries:
            return _none("审计链暂无记录，无法重建时间线（证据不足）。", topic="timeline")
        latest_labels = "；".join(e.label for e in entries[-3:])
        return ReviewAnswer(
            topic="timeline",
            conclusion=(
                f"时间线共 {len(entries)} 条（审计链序号倒序）；最近三条：{latest_labels}。"
            ),
            scope="当前事件工作区审计链（确定性重建）",
            references=(event_id,) if event_id else (),
            evidence=tuple(f"audit:#{e.sequence}" for e in entries[-8:]),
            statistics=(
                "审计流有序号但无时钟源，时间维度不参与统计；"
                "不用事后信息反推指责当时决策"
            ),
            excluded="各条目具体时刻均未记录（待接入时钟源）",
            confidence="高",
        )

    # primary == "common"
    visible, hidden_note = _visible_issues(ledger, role)
    events_with_issues = sorted({i.event_id for i in visible if i.event_id})
    if len(events_with_issues) < 2:
        return _none(
            "样本不足：当前仅 "
            f"{len(events_with_issues) or 0} 个事件有问题记录，无法归纳共性规律。",
            excluded="共性归纳要求至少两个事件的问题数据",
            topic="common",
        )
    title_events: dict[str, set[str]] = {}
    for issue in visible:
        title_events.setdefault(issue.title.strip(), set()).add(issue.event_id)
    recurring = [
        (title, evts) for title, evts in title_events.items() if len(evts) >= 2
    ]
    if not recurring:
        return _none(
            f"已对比 {len(events_with_issues)} 个事件，未发现跨事件复现的同名问题。",
            topic="common",
        )
    recurring.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    lines = ";".join(f"{title}（{len(evts)} 个事件）" for title, evts in recurring[:5])
    ev_ids = [
        f"issue:{i.issue_id}"
        for title, _ in recurring[:3]
        for i in visible if i.title.strip() == title
    ][:8]
    return ReviewAnswer(
        topic="common",
        conclusion=f"跨事件复现问题 {len(recurring)} 类：{lines}。",
        scope=f"复盘台账问题分册（对比事件：{'、'.join(events_with_issues)}）",
        references=tuple(events_with_issues),
        evidence=tuple(ev_ids),
        statistics="同名问题出现在 ≥2 个事件即计为复现；仅统计你可查看的记录",
        excluded=hidden_note or "无",
        confidence="中",
    )
