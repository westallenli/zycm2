"""P20 态势自动发现与手动复核的规则内核（docs/20 V1.0）。

本模块是**纯规则域**：只读地扫描当前事件工作区，产出可追溯的「态势变化」与
「态势复核批次」。它不渲染页面、不处理 HTTP、更不写正式态势。

钉死的边界（定稿 §3）：

- 自动发现只能新建变化记录、待核验候选、信息缺口和冲突提示；
  绝不修改已发布对象的几何、状态、责任单位或发布记录；
- 自动发现不碰命令链：不暂停、不撤销、不下达、不完成；
- 同一来源重复进入必须幂等（指纹去重），不重复刷屏；
- 坐标不明、影像未配准时只产限制提示与信息缺口，不给精确空间结论；
- 复核批次固定数据截止点；同一截止点重复点击返回同一次结果。

接口/数据库使用英文受控值，前台一律经本模块词表转中文。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from . import tactical_map as tm

#: 规则版本：变化指纹的一部分。规则语义变化时递增，旧记录不会被误当同类合并。
RULE_VERSION = "p20-r1"

#: 复核周期之外还要多久才升级提醒（分钟）；避免同一问题反复冒泡。
REPEAT_SUPPRESS_MINUTES = 90

#: 空间粗筛阈值（米）。只用于「值不值得提醒复核」，不作为精确判定依据。
NEAR_LIMIT_M = 400.0
OVERLAP_LIMIT_M = 150.0


class SituationError(ValueError):
    """态势域错误；消息可直接显示给指挥员。"""


# ---------------------------------------------------------------------------
# 受控枚举与中文词表


class ChangeKind(str, Enum):
    """初版固定六类（定稿 §6.3），不允许多种自由文本类型。"""

    FRESHNESS_EXPIRED = "source_freshness_expired"
    CONFLICT_DETECTED = "source_conflict_detected"
    OBSERVATION_CONFLICT = "source_observation_conflict"
    CANDIDATE_CREATED = "source_candidate_created"
    INFORMATION_GAP = "source_information_gap"
    OPTION_AFFECTED = "source_option_affected"


class ChangeStatus(str, Enum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"
    DISMISSED = "dismissed"


class ChangeSeverity(str, Enum):
    NOTICE = "notice"
    REVIEW = "review"
    DECISION = "decision"


class ReviewOutcome(str, Enum):
    COMPLETED = "completed"
    NO_MATERIAL_CHANGE = "no_material_change"
    BLOCKED = "blocked"


_WORDS: dict[str, dict[str, str]] = {
    "change_kind": {
        ChangeKind.FRESHNESS_EXPIRED.value: "正式对象需要复核",
        ChangeKind.CONFLICT_DETECTED.value: "路线冲突",
        ChangeKind.OBSERVATION_CONFLICT.value: "新观测与正式态势不一致",
        ChangeKind.CANDIDATE_CREATED.value: "新增待核验候选",
        ChangeKind.INFORMATION_GAP.value: "信息缺口",
        ChangeKind.OPTION_AFFECTED.value: "方案受态势影响",
    },
    "change_status": {
        ChangeStatus.NEW.value: "待复核",
        ChangeStatus.ACKNOWLEDGED.value: "已阅",
        ChangeStatus.REVIEWED.value: "已复核",
        ChangeStatus.SUPERSEDED.value: "已被替代",
        ChangeStatus.DISMISSED.value: "已按现有资料继续",
    },
    "change_severity": {
        ChangeSeverity.NOTICE.value: "提示",
        ChangeSeverity.REVIEW.value: "需复核",
        ChangeSeverity.DECISION.value: "需指挥员处理",
    },
    "review_outcome": {
        ReviewOutcome.COMPLETED.value: "已完成复核",
        ReviewOutcome.NO_MATERIAL_CHANGE.value: "未发现实质变化",
        ReviewOutcome.BLOCKED.value: "复核受阻",
    },
}


def _word(table: str, value: object) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "")
    return _WORDS[table].get(text, text or "—")


def kind_word(v: object) -> str:
    return _word("change_kind", v)


def status_word(v: object) -> str:
    return _word("change_status", v)


def severity_word(v: object) -> str:
    return _word("change_severity", v)


#: 升级判定：同一指纹的变化只在等级严格提高时才重新冒泡。
_SEVERITY_RANK = {
    ChangeSeverity.NOTICE.value: 0,
    ChangeSeverity.REVIEW.value: 1,
    ChangeSeverity.DECISION.value: 2,
}


def severity_rank(v: object) -> int:
    return _SEVERITY_RANK.get(str(getattr(v, "value", v) or ""), 0)


def outcome_word(v: object) -> str:
    return _word("review_outcome", v)


#: 变化状态允许的流转（幂等 + 不允许回退成 new）
_ALLOWED_NEXT: dict[str, set[str]] = {
    ChangeStatus.NEW.value: {
        ChangeStatus.ACKNOWLEDGED.value,
        ChangeStatus.REVIEWED.value,
        ChangeStatus.DISMISSED.value,
        ChangeStatus.SUPERSEDED.value,
    },
    ChangeStatus.ACKNOWLEDGED.value: {
        ChangeStatus.REVIEWED.value,
        ChangeStatus.DISMISSED.value,
        ChangeStatus.SUPERSEDED.value,
    },
    ChangeStatus.REVIEWED.value: {ChangeStatus.SUPERSEDED.value},
    ChangeStatus.DISMISSED.value: {ChangeStatus.SUPERSEDED.value},
    ChangeStatus.SUPERSEDED.value: set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in _ALLOWED_NEXT.get(str(current), set())


#: 变化处理动作 → 目标状态（前台只暴露这四个业务动作）
ACTION_TARGET = {
    "ack": ChangeStatus.ACKNOWLEDGED.value,
    "continue": ChangeStatus.DISMISSED.value,
}

#: 「已按现有资料继续」必须写原因；「已阅」不需要。
REASON_REQUIRED = frozenset({"continue"})


# ---------------------------------------------------------------------------
# 指纹与时间工具


def compute_fingerprint(*parts: object) -> str:
    """稳定指纹：对部件做规范化 JSON 后取 SHA-256（顺序无关的集合自行排序）。"""
    normalized = [str(p if p is not None else "") for p in parts]
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def source_digest(parts: object) -> str:
    """来源摘要：内容变了才允许产生新变化（同一份数据重复进入不算新情况）。"""
    if isinstance(parts, (dict, list, tuple)):
        blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    else:
        blob = str(parts or "")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def parse_time(text: object):
    return tm._parse_time(text)


def minutes_between(earlier: object, later: object) -> float | None:
    a, b = parse_time(earlier), parse_time(later)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def _clip(text: object, limit: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


# ---------------------------------------------------------------------------
# 领域对象


@dataclass
class SituationChange:
    """一条态势变化记录：谁、什么时候、因为什么数据、提醒谁复核。

    只描述「看到了什么变化」，不承载任何发布/命令权力。
    """

    change_id: str
    event_id: str
    kind: str
    status: str
    severity: str
    fingerprint: str
    reason: str
    detected_at: str = ""
    observed_at: str = ""
    received_at: str = ""
    source_refs: tuple[str, ...] = ()
    source_digest: str = ""
    affected_annotation_ids: tuple[str, ...] = ()
    affected_option_ids: tuple[str, ...] = ()
    candidate_annotation_ids: tuple[str, ...] = ()
    related_change_id: str = ""
    evidence_summary: str = ""
    positioning_notice: str = ""
    rule_version: str = RULE_VERSION
    review_batch_id: str = ""
    resolved_at: str = ""
    resolution: str = ""
    handled_by: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {k.value for k in ChangeKind}:
            raise SituationError(f"未知变化类型：{self.kind}")
        if self.status not in {s.value for s in ChangeStatus}:
            raise SituationError(f"未知变化状态：{self.status}")
        if self.severity not in {s.value for s in ChangeSeverity}:
            raise SituationError(f"未知变化等级：{self.severity}")
        if not str(self.reason or "").strip():
            raise SituationError("变化记录必须写明原因，不能只给类型")
        if len(self.fingerprint) != 64:
            raise SituationError("变化指纹缺失或不合法：重复进入无法去重")

    @property
    def kind_word(self) -> str:
        return kind_word(self.kind)

    @property
    def status_word(self) -> str:
        return status_word(self.status)

    @property
    def severity_word(self) -> str:
        return severity_word(self.severity)

    @property
    def open_for_review(self) -> bool:
        """未处理与已阅仍属待复核；已复核/已替代/已忽略不再出现在复核清单。"""
        return self.status in {
            ChangeStatus.NEW.value, ChangeStatus.ACKNOWLEDGED.value,
        }

    @property
    def requires_commander(self) -> bool:
        return self.severity == ChangeSeverity.DECISION.value

    def located(self) -> str:
        """可定位的对象编号（前端据此在地图上高亮，不移动视角之外的东西）。"""
        return self.affected_annotation_ids[0] if self.affected_annotation_ids else ""

    def summary_line(self) -> str:
        head = f"【{self.kind_word}】{self.severity_word}"
        where = ""
        if self.affected_annotation_ids:
            where = "｜" + "、".join(self.affected_annotation_ids[:2])
        return _clip(f"{head}｜{_clip(self.reason, 90)}{where}")

    def as_row(self) -> dict:
        """前台行：只出中文与业务编号，不出内部枚举值。"""
        return {
            "id": self.change_id,
            "kind": self.kind,
            "kind_word": self.kind_word,
            "severity": self.severity,
            "severity_word": self.severity_word,
            "status_word": self.status_word,
            "reason": _clip(self.reason, 160),
            "detected": _clip(self.detected_at, 19),
            "observed": _clip(self.observed_at, 19),
            "locate": self.located(),
            "candidates": list(self.candidate_annotation_ids)[:3],
            "options": list(self.affected_option_ids)[:3],
            "notice": _clip(self.positioning_notice, 90),
            "evidence": _clip(self.evidence_summary, 120),
            "needs_commander": self.requires_commander,
        }


    def as_spec(self) -> dict:
        """回灌扫描器时用的规格（仅用于方案影响判定，不代写新结论）。"""
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "severity": self.severity,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "detected_at": self.detected_at,
            "observed_at": self.observed_at,
            "source_refs": self.source_refs,
            "source_digest": self.source_digest,
            "affected_annotation_ids": self.affected_annotation_ids,
            "affected_option_ids": self.affected_option_ids,
            "candidate_annotation_ids": self.candidate_annotation_ids,
            "evidence_summary": self.evidence_summary,
            "positioning_notice": self.positioning_notice,
        }


@dataclass
class SituationReviewBatch:
    """指挥员「复核当前态势」的一次可追溯批次（定稿 §5.2）。"""

    review_batch_id: str
    event_id: str
    requested_by: str
    requested_at: str
    cutoff_at: str
    workspace_version_before: int
    fingerprint: str
    outcome: str = ReviewOutcome.COMPLETED.value
    summary: str = ""
    source_change_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    prior_review_batch_id: str = ""
    new_findings: tuple[str, ...] = ()
    invalidated_findings: tuple[str, ...] = ()
    information_gap_ids: tuple[str, ...] = ()
    candidate_annotation_ids: tuple[str, ...] = ()
    affected_option_ids: tuple[str, ...] = ()
    recommendation: str = ""
    requires_commander_action: bool = False
    rule_version: str = RULE_VERSION
    #: 本次复核写完后工作区版本：重复点击且无新数据时直接返回同一批次。
    workspace_version_after: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in {o.value for o in ReviewOutcome}:
            raise SituationError(f"未知复核结论：{self.outcome}")
        if len(self.fingerprint) != 64:
            raise SituationError("复核批次指纹缺失：同一数据截止点无法幂等")

    @property
    def outcome_word(self) -> str:
        return outcome_word(self.outcome)

    def as_row(self) -> dict:
        return {
            "id": self.review_batch_id,
            "at": _clip(self.requested_at, 19),
            "cutoff": _clip(self.cutoff_at, 19),
            "outcome_word": self.outcome_word,
            "summary": _clip(self.summary, 240),
            "findings": [_clip(x, 120) for x in self.new_findings[:4]],
            "invalidated": [_clip(x, 120) for x in self.invalidated_findings[:4]],
            "candidates": list(self.candidate_annotation_ids[:3]),
            "options": list(self.affected_option_ids[:3]),
            "recommendation": _clip(self.recommendation, 200),
            "requires_commander_action": bool(self.requires_commander_action),
            "change_count": len(self.source_change_ids),
        }


# ---------------------------------------------------------------------------
# 空间粗筛（只用于判断「值不值得提醒」，不作为精确判定）


def _point_distance_m(a, b) -> float:
    return tm.ground_distance_m(tuple(a), tuple(b))


def _ring_contains(ring, pt) -> bool:
    """射线法判断点是否落在面内（经纬度平面近似，小范围可接受）。"""
    x, y = float(pt[0]), float(pt[1])
    inside = False
    n = len(ring)
    for i in range(n):
        ax, ay = float(ring[i][0]), float(ring[i][1])
        bx, by = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        if (ay > y) != (by > y):
            t = (y - ay) / (by - ay) if by != ay else 0.0
            if x < ax + t * (bx - ax):
                inside = not inside
    return inside


def distance_to_mark_m(geo, mark_geo, *, geometry: str) -> float:
    """观测点到对象几何的粗筛距离（米）。面：是否在环内或到最近顶点距离。"""
    if not geo or not mark_geo:
        return float("inf")
    if geometry == "point":
        return _point_distance_m(geo[0], mark_geo[0])
    if geometry == "polygon" and len(mark_geo) >= 3:
        if _ring_contains(mark_geo, geo[0]):
            return 0.0
    return min(
        _point_distance_m(pt, mp) for pt in geo for mp in mark_geo
    )


def _positioning_notice(mark) -> str:
    """坐标质量限制说明；无限制返回空串。"""
    value = getattr(mark, "positioning_status", "") or ""
    if value in {"rough", "unregistered"}:
        return (
            f"定位状态：{tm.positioning_word(value)}——"
            "仅提示方向与量级，不能作为精确坐标依据"
        )
    if getattr(mark, "is_demo", False):
        return "演示近似位置，非现场实测"
    return ""


def mark_geo(mark) -> tuple:
    """权威几何：只认 geo_points；无坐标一律不产空间结论。"""
    geo = tuple(getattr(mark, "geo_points", ()) or ())
    return geo


# ---------------------------------------------------------------------------
# 扫描规则（纯函数；输入是工作区当前状态，输出是变化规格字典）


def _spec(*, kind: str, severity: str, reason: str, fingerprint_parts: tuple,
          event_id: str = "", detected_at: str = "", observed_at: str = "",
          source_refs=(), digest_parts=None, affected=(), options=(),
          candidates=(), evidence: str = "", notice: str = "") -> dict:
    return {
        "event_id": event_id or "",
        "kind": str(kind),
        "severity": str(severity),
        "reason": _clip(reason),
        "fingerprint": compute_fingerprint(RULE_VERSION, event_id, *fingerprint_parts),
        "detected_at": detected_at or "",
        "observed_at": observed_at or "",
        "source_refs": tuple(str(s) for s in source_refs if str(s).strip()),
        "source_digest": source_digest(digest_parts) if digest_parts is not None else "",
        "affected_annotation_ids": tuple(str(a) for a in affected if a),
        "affected_option_ids": tuple(str(o) for o in options if o),
        "candidate_annotation_ids": tuple(str(c) for c in candidates if c),
        "evidence_summary": _clip(evidence),
        "positioning_notice": _clip(notice),
    }


def scan_freshness(marks, *, event_id: str = "", now: str = "") -> list[dict]:
    """正式/已核验对象超出复核周期 → 只提醒复核，不改对象本身。"""
    out: list[dict] = []
    for mark in sorted(marks, key=lambda m: m.id):
        if not mark.tactical:
            continue
        if mark.status.value not in {"published", "verified"}:
            continue
        fresh = tm.freshness_of(mark, now=now)
        if fresh is tm.FreshnessStatus.OK:
            continue
        severity = (ChangeSeverity.REVIEW.value
                    if fresh is tm.FreshnessStatus.EXPIRED
                    else ChangeSeverity.NOTICE.value)
        stamp = (mark.details.get("checked_at") or mark.details.get("last_report")
                 or mark.details.get("reported_at") or mark.updated_at or "")
        out.append(_spec(
            kind=ChangeKind.FRESHNESS_EXPIRED.value,
            severity=severity,
            reason=(
                f"{tm.kind_word(mark.kind.value)}「{mark.title}」"
                f"{'已超过复核周期' if fresh is tm.FreshnessStatus.EXPIRED else '接近复核周期'}"
                f"，最后依据时间 {stamp or '未登记'}，需要重新核查"
            ),
            fingerprint_parts=("freshness", mark.id, int(mark.version)),
            event_id=event_id, detected_at=now, observed_at=stamp,
            source_refs=tuple(mark.source_ref),
            digest_parts=(mark.id, mark.version, fresh.value, stamp),
            affected=(mark.id,),
            evidence=f"时效状态：{tm.fresh_word(fresh)}；对象版本 v{mark.version}",
            notice=_positioning_notice(mark),
        ))
    return out


_CONFLICT_SEVERITY = {
    "notice": ChangeSeverity.NOTICE.value,
    "review": ChangeSeverity.REVIEW.value,
    "blocked": ChangeSeverity.DECISION.value,
    "decision": ChangeSeverity.DECISION.value,
}


def scan_conflicts(records, *, event_id: str = "", marks_by_id=None) -> list[dict]:
    """冲突台账里的未关闭记录 → 变化提醒（不自动改路线、不暂停命令）。"""
    marks_by_id = marks_by_id or {}
    out: list[dict] = []
    for rec in sorted(records, key=lambda r: r.conflict_id):
        if rec.status != tm.ConflictStatus.OPEN.value:
            continue
        route = marks_by_id.get(rec.route_id)
        trigger = marks_by_id.get(rec.trigger_annotation_id)
        notice = "；".join(
            n for n in (_positioning_notice(route) if route else "",
                        _positioning_notice(trigger) if trigger else "") if n
        )
        out.append(_spec(
            kind=ChangeKind.CONFLICT_DETECTED.value,
            severity=_CONFLICT_SEVERITY.get(rec.level, ChangeSeverity.REVIEW.value),
            reason=(
                f"{tm.kind_word(route.kind.value) if route is not None else '路线'}"
                f"「{route.title if route is not None else rec.route_id}」"
                f"{rec.level_word}：{rec.summary or rec.relation}"
            ),
            fingerprint_parts=("conflict", rec.conflict_id, rec.level),
            event_id=event_id, detected_at=rec.detected_at,
            source_refs=(rec.conflict_id,) + tuple(rec.evidence_refs),
            digest_parts=(rec.conflict_id, rec.level, rec.summary),
            affected=(rec.route_id, rec.trigger_annotation_id),
            evidence=f"冲突记录 {rec.conflict_id}（{rec.level_word}）",
            notice=notice,
        ))
    return out


def _option_text(option) -> str:
    """方案的可匹配文本：只取已登记的编号与名称，不做语义猜测。"""
    parts = [
        str(getattr(option, "summary", "") or ""),
        " ".join(str(s) for s in (getattr(option, "claim_subjects", ()) or ())),
        " ".join(str(s) for s in (getattr(option, "preconditions", ()) or ())),
        " ".join(str(s) for s in (getattr(option, "no_go_conditions", ()) or ())),
        " ".join(str(s) for s in (getattr(option, "risks", ()) or ())),
    ]
    return " ｜ ".join(parts)


def _spatial_hits(option, points) -> bool:
    """方案上图几何与变化对象在粗筛半径内相邻→ True（只用于提醒，不作精确判定）。"""
    shapes = getattr(option, "sim_shapes", ()) or ()
    for shape in shapes:
        pts = shape.get("pts") if isinstance(shape, dict) else None
        for raw in (pts or ()):
            try:
                lat, lon = float(raw[0]), float(raw[1])
            except (TypeError, ValueError, IndexError):
                continue
            for pt in points:
                try:
                    if tm.ground_distance_m((lat, lon), tuple(pt)) <= NEAR_LIMIT_M:
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def scan_options(
    changes, options, *, event_id: str = "", now: str = "", marks_by_id=None
) -> list[dict]:
    """变化命中方案引用的对象 → 标记方案受态势影响。

    两个确定性匹配通道：① 方案文本里出现对象编号或对象名称；
    ② 方案上图几何与变化对象在粗筛半径内相邻。
    只标记，不否决、不采纳、不替指挥员决断。
    """
    marks_by_id = marks_by_id or {}
    hits: dict[str, set[str]] = {}
    for change in changes:
        if change.get("severity") == ChangeSeverity.NOTICE.value:
            continue
        touched = tuple(dict.fromkeys(
            tuple(change.get("affected_annotation_ids") or ())
            + tuple(change.get("candidate_annotation_ids") or ())))
        if not touched:
            continue
        tokens: dict[str, set[str]] = {}
        points: list[tuple] = []
        for aid in touched:
            tokens[aid] = {aid}
            mark = marks_by_id.get(aid)
            if mark is None:
                continue
            title = str(getattr(mark, "title", "") or "").strip()
            if len(title) >= 4:
                tokens[aid].add(title)
            geo = mark_geo(mark)
            points.extend(geo or ())
        for option in options:
            if str(getattr(option, "status", "")) not in {"active", "affected"}:
                continue
            text = _option_text(option)
            matched = {aid for aid, group in tokens.items()
                       if any(token in text for token in group)}
            if not matched and points and _spatial_hits(option, points):
                matched = set(touched)
            if matched:
                hits.setdefault(option.id, set()).update(matched)
    out: list[dict] = []
    for option in sorted(options, key=lambda o: o.id):
        affected = sorted(hits.get(option.id, set()))
        if not affected:
            continue
        out.append(_spec(
            kind=ChangeKind.OPTION_AFFECTED.value,
            severity=ChangeSeverity.REVIEW.value,
            reason=(
                f"方案「{getattr(option, 'title', option.id)}」引用的对象"
                f"被 {len(affected)} 项新变化命中，建议重新比较"
            ),
            fingerprint_parts=("option", option.id, tuple(sorted(affected))),
            event_id=event_id, detected_at=now,
            digest_parts=(option.id, tuple(affected)),
            affected=tuple(affected), options=(option.id,),
            evidence="变化命中的对象：" + "、".join(affected[:4]),
        ))
    return out


def scan_observation(
    marks,
    *,
    kind: str,
    geo,
    event_id: str = "",
    observed_at: str = "",
    source_ref: str = "",
    positioning_status: str = "",
    now: str = "",
    near_limit_m: float = NEAR_LIMIT_M,
    overlap_limit_m: float = OVERLAP_LIMIT_M,
) -> list[dict]:
    """新观测与既有正式对象的差异扫描。

    fail-closed：没有权威地理坐标（geo_points）就不产任何空间结论。
    """
    geo = tuple(tuple(p) for p in (geo or ()))
    if len(geo) < 1:
        return []
    notice = ""
    if positioning_status in {"rough", "unregistered"}:
        notice = (
            f"定位状态：{tm.positioning_word(positioning_status)}——"
            "仅能提示方向与量级，不能作为精确坐标依据"
        )
    out: list[dict] = []
    for mark in sorted(marks, key=lambda m: m.id):
        if mark.status.value not in {"published", "verified"}:
            continue
        target = mark_geo(mark)
        if not target:
            continue
        dist = distance_to_mark_m(geo, target, geometry=mark.geometry)
        same_kind = mark.kind.value == str(kind)
        limit = overlap_limit_m if same_kind else near_limit_m
        if dist > limit:
            continue
        severity = (ChangeSeverity.REVIEW.value if same_kind
                    else ChangeSeverity.NOTICE.value)
        out.append(_spec(
            kind=ChangeKind.OBSERVATION_CONFLICT.value,
            severity=severity,
            reason=(
                f"新观测与{tm.kind_word(mark.kind.value)}「{mark.title}」"
                f"位置差异约 {dist:.0f} 米（粗筛），需要人工复核是否修订"
            ),
            fingerprint_parts=(
                "observation", mark.id, int(mark.version), source_ref,
                source_digest([(round(float(p[0]), 5), round(float(p[1]), 5)) for p in geo]),
            ),
            event_id=event_id, detected_at=now, observed_at=observed_at,
            source_refs=(source_ref,) if source_ref else (),
            digest_parts=(
                mark.id, mark.version,
                [(round(float(p[0]), 5), round(float(p[1]), 5)) for p in geo],
            ),
            affected=(mark.id,),
            evidence=(
                f"观测时间 {observed_at or '未登记'}｜来源 {source_ref or '未登记'}｜"
                f"粗筛距离约 {dist:.0f} 米"
            ),
            notice=notice or _positioning_notice(mark),
        ))
    return out


# ---------------------------------------------------------------------------
# 复核批次装配


def build_review(
    ws,
    changes,
    *,
    requested_by: str,
    now: str,
    cutoff_at: str,
    prior=None,
) -> dict:
    """把待复核变化汇总成一次可追溯复核结论（不发布、不改命令）。

    返回字段直接对齐 ``SituationReviewBatch`` 的关键入参。
    """
    changes = list(changes)
    marks = {m.id: m for m in ws.annotations.values()}
    fingerprint = compute_fingerprint(
        RULE_VERSION, getattr(ws, "scenario_id", ""), cutoff_at, ws.version,
        tuple(sorted(c.change_id for c in changes)),
    )
    if not changes:
        return {
            "fingerprint": fingerprint,
            "outcome": ReviewOutcome.NO_MATERIAL_CHANGE.value,
            "summary": "本次纳入的资料未发现需复核的已记录变化；未覆盖的现场信息仍需人工核对。",
            "recommendation": "本轮无新增待复核项；现有部署与命令是否继续由指挥员依据现场核验决定。",
            "requires_commander_action": False,
            "source_change_ids": (),
            "source_refs": (),
            "new_findings": (),
            "invalidated_findings": (),
            "candidate_annotation_ids": (),
            "affected_option_ids": (),
            "information_gap_ids": (),
        }

    new_findings: list[str] = []
    refs: list[str] = []
    candidates: list[str] = []
    options: list[str] = []
    for change in changes:
        new_findings.append(change.summary_line())
        refs.extend(change.source_refs)
        candidates.extend(change.candidate_annotation_ids)
        options.extend(change.affected_option_ids)

    # 上一轮提示里、本轮不再出现的变化＝已失效（只作说明，不动正式对象）。
    invalidated: list[str] = []
    if prior is not None:
        still = {c.affected_annotation_ids for c in changes}
        flat = {a for group in still for a in group}
        for cid in prior.source_change_ids:
            old = getattr(ws, "situation_changes", {}).get(cid)
            if old is None:
                continue
            vanished = [a for a in old.affected_annotation_ids if a not in flat]
            if vanished:
                where = "、".join(vanished[:2])
                invalidated.append(
                    f"上一轮提示的对象 {where} 本轮未再出现，旧提示可视为已过期"
                )

    needs = any(c.requires_commander for c in changes)
    review_count = sum(1 for c in changes if c.kind == ChangeKind.FRESHNESS_EXPIRED.value)
    conflict_count = sum(1 for c in changes if c.kind == ChangeKind.CONFLICT_DETECTED.value)
    cand_count = sum(1 for c in changes if c.kind == ChangeKind.CANDIDATE_CREATED.value)
    summary = (
        f"本轮纳入 {len(changes)} 项变化：需复核时效 {review_count} 项、"
        f"路线冲突 {conflict_count} 项、新增候选 {cand_count} 项；"
        "正式态势与命令保持原样，变化只进入待复核。"
    )
    if needs:
        recommendation = (
            "存在需要指挥员处理的变化：建议先复核受影响对象，"
            "再决定是否发布修订或调整命令。"
        )
    elif candidates:
        recommendation = "建议核验新增待核验候选；核验通过后由指挥员决定是否发布。"
    else:
        recommendation = "本轮变化未触发程序性处理要求；现有作业是否继续仍须核对现场条件。"

    return {
        "fingerprint": fingerprint,
        "outcome": ReviewOutcome.COMPLETED.value,
        "summary": summary,
        "recommendation": recommendation,
        "requires_commander_action": needs,
        "source_change_ids": tuple(c.change_id for c in changes),
        "source_refs": tuple(dict.fromkeys(r for r in refs if r)),
        "new_findings": tuple(new_findings),
        "invalidated_findings": tuple(invalidated),
        "candidate_annotation_ids": tuple(dict.fromkeys(candidates)),
        "affected_option_ids": tuple(dict.fromkeys(options)),
        "information_gap_ids": (),
    }


def latest_review(ws):
    """最近一次复核批次（无则 None）。"""
    batches = sorted(
        getattr(ws, "situation_reviews", {}).values(),
        key=lambda b: (b.requested_at, b.review_batch_id),
    )
    return batches[-1] if batches else None


def open_changes(ws, *, limit: int = 0):
    rows = [c for c in sorted(ws.situation_changes.values(), key=lambda c: c.change_id)
            if c.open_for_review]
    return rows[:limit] if limit else rows


__all__ = [
    "RULE_VERSION",
    "ChangeKind",
    "ChangeStatus",
    "ChangeSeverity",
    "ReviewOutcome",    "SituationChange",
    "SituationReviewBatch",
    "SituationError",
    "ACTION_TARGET",
    "REASON_REQUIRED",
    "can_transition",
    "compute_fingerprint",
    "source_digest",
    "kind_word",
    "status_word",
    "severity_word",
    "outcome_word",
    "scan_freshness",
    "scan_conflicts",
    "scan_options",
    "scan_observation",
    "build_review",
    "latest_review",
    "open_changes",
    "severity_rank",
]
