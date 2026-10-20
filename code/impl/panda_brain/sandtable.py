"""指挥调度沙盘域层（P16-T6，03 号定版）。

三块纯逻辑，无 HTTP 无 HTML：

1. 阶段状态机推导 —— 八阶段由真实信号驱动，不伪造推进：
   每个阶段的进入条件映射到工作区/注册表里的真实记录，
   推导不出的一律如实标注「未满足」，绝不默认推进。
2. 自由区域测算 —— 任意轨迹闭合多边形的面积/周长。
   沙盘是示意面不是投影坐标系，结果一律带「示意值」标注；
   临时测算不落库，保存为业务对象走标绘核验链。
3. 态势问询引擎 —— 关键词路由到真实数据，回答必须携带
   结论 / 数据时间 / 来源 / 地图位置 / 置信度 / 未确认内容（doc 03 §13.1）；
   没有匹配数据就明说无法回答，绝不编造。

权限口径（doc 03 §12）：公共屏只见已发布；到场人员可看
已核验与智脑候选；指挥员全量。问询回答按角色过滤数据源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .map_annotations import AnnotationKind, AnnotationStatus, MapAnnotation
from .materials import MaterialsLedger

#: 与 map_annotations.indicative_length_m 保持同一比例尺（示意值口径）。
SCALE_M_PER_UNIT = 4.0

STAGES: tuple[str, ...] = (
    "火情响应",
    "初期侦查",
    "态势形成",
    "资源调配",
    "方案决策",
    "命令执行",
    "异常处置",
    "收尾复盘",
)

#: 顺序阶段（不含覆盖态「异常处置」）——derive_stage 的 conditions 与之一一对应。
#: 异常处置按定版口径是覆盖态：出现即显著提示，不占顺序进度位。
SEQUENTIAL_STAGES: tuple[str, ...] = STAGES[:6] + (STAGES[7],)

#: 异常处置是覆盖态：出现即显著提示，不代表流程回退。
ABNORMAL_ORDER_STATUSES = frozenset({"paused", "failed"})
#: 命令真正进入执行的最低状态。
EXECUTING_ORDER_STATUSES = frozenset({"confirmed", "active"})

_QUESTION_TOPICS: tuple[tuple[str, str], ...] = (
    ("队伍", "personnel"),
    ("人员", "personnel"),
    ("在岗", "personnel"),
    ("多少人", "personnel"),
    ("火线", "annotations"),
    ("火点", "annotations"),
    ("烟雾", "annotations"),
    ("危险区", "annotations"),
    ("隔离带", "annotations"),
    ("水源", "water"),
    ("物资", "materials"),
    ("补给", "materials"),
    ("面积", "areas"),
    ("多大", "areas"),
    ("周长", "areas"),
    ("决策", "decisions"),
    ("方案", "decisions"),
    ("定案", "decisions"),
    ("命令", "orders"),
    ("任务", "orders"),
    ("缺口", "gaps"),
    ("未核实", "gaps"),
    ("待决策", "pending"),
)


# ---------------------------------------------------------------------------
# 1. 阶段状态机推导


@dataclass(frozen=True)
class StageCondition:
    """一个阶段的进入条件与其真实证据说明。"""

    stage: str
    met: bool
    evidence: str


@dataclass(frozen=True)
class StageView:
    """阶段推导结果：当前阶段 + 覆盖态 + 全部条件证据。"""

    current: str
    index: int          # 0-based，对应 SEQUENTIAL_STAGES / conditions
    overlay: str | None  # 异常处置触发时为该阶段名
    conditions: tuple[StageCondition, ...]


def _latest_ws_time(ws) -> str:
    """工作区最近一次 append_event 的时间戳；没有事件则空串。"""
    for ev in reversed(getattr(ws, "_events", ())):
        stamp = getattr(ev, "at", None) or getattr(ev, "ts", None)
        if stamp:
            return str(stamp)
    return ""


def derive_stage(ws, event=None, ledger: MaterialsLedger | None = None) -> StageView:
    """按真实记录推导八阶段。任何一步都引用可核对的数量证据。"""
    bindings = getattr(ws, "roster_bindings", {}) or {}
    active_bindings = sum(1 for b in bindings.values() if b.status == "active")
    annotations = getattr(ws, "annotations", {}) or {}
    settled_marks = sum(
        1
        for a in annotations.values()
        if a.status in {AnnotationStatus.VERIFIED, AnnotationStatus.PUBLISHED}
    )
    event_id = getattr(event, "event_id", "") if event is not None else ""
    snapshot_ready = bool(ledger and ledger.snapshots.get(event_id))
    decisions = getattr(ws, "decisions", {}) or {}
    orders = getattr(ws, "orders", {}) or {}
    executing = sum(1 for o in orders.values() if o.status in EXECUTING_ORDER_STATUSES)
    abnormal = sum(1 for o in orders.values() if o.status in ABNORMAL_ORDER_STATUSES)
    reviews = getattr(ws, "reviews", {}) or {}
    closed = bool(event is not None and getattr(event, "status", "") == "closed")

    conditions = (
        StageCondition("火情响应", True, "事件已登记，任务空间已建立"),
        StageCondition(
            "初期侦查",
            active_bindings > 0,
            f"已入岗绑定 {active_bindings} 条（前指成立的标志）",
        ),
        StageCondition(
            "态势形成",
            settled_marks > 0,
            f"已核验/已发布标绘 {settled_marks} 条（首轮态势证据）",
        ),
        StageCondition(
            "资源调配",
            snapshot_ready,
            "本事件到场物资快照已生成" if snapshot_ready else "本事件尚无到场物资快照",
        ),
        StageCondition(
            "方案决策",
            len(decisions) > 0,
            f"指挥员决策 {len(decisions)} 条",
        ),
        StageCondition(
            "命令执行",
            executing > 0,
            f"进入执行状态的命令 {executing} 条",
        ),
        StageCondition(
            "收尾复盘",
            closed or len(reviews) > 0,
            (
                f"事件状态 {'已关闭' if closed else '进行中'}，"
                f"复盘记录 {len(reviews)} 条"
            ),
        ),
    )
    met_indexes = [i for i, c in enumerate(conditions) if c.met]
    # conditions 与 SEQUENTIAL_STAGES 一一对应；「异常处置」是覆盖态（overlay），
    # 不占顺序位——修复 off-by-one：旧写法直接 STAGES[current_index]，
    # 收尾复盘满足时会错显为「异常处置」。
    current_index = max(met_indexes) if met_indexes else 0
    overlay = "异常处置" if abnormal > 0 else None
    return StageView(
        current=SEQUENTIAL_STAGES[current_index],
        index=current_index,
        overlay=overlay,
        conditions=conditions,
    )


# ---------------------------------------------------------------------------
# 2. 自由区域测算


class MeasureError(ValueError):
    """测算输入不合法；消息可直接展示。"""


def parse_points(text: str) -> tuple[tuple[float, float], ...]:
    """解析「x,y x,y …」或每行一对的坐标串。"""
    tokens = text.replace("\n", " ").split()
    points: list[tuple[float, float]] = []
    for token in tokens:
        parts = token.split(",")
        if len(parts) != 2:
            raise MeasureError(f"坐标「{token}」不是 x,y 形式")
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            raise MeasureError(f"坐标「{token}」含非数字") from None
        points.append((x, y))
    if not points:
        raise MeasureError("请至少输入一个坐标")
    return tuple(points)


def measure_polygon(points: tuple[tuple[float, float], ...]) -> dict:
    """鞋带公式面积 + 周长；全部为示意值（沙盘非投影坐标系）。"""
    if len(points) < 3:
        raise MeasureError("自由区域至少需要 3 个顶点才能闭合")
    area2 = 0.0
    perimeter = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
        perimeter += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    area_units = abs(area2) / 2.0
    scale2 = SCALE_M_PER_UNIT * SCALE_M_PER_UNIT
    return {
        "vertices": n,
        "area_m2": round(area_units * scale2, 1),
        "perimeter_m": round(perimeter * SCALE_M_PER_UNIT, 1),
        "indicative": True,
    }


def indicative_area_m2(annotation: MapAnnotation) -> float:
    """单个面状标绘的示意面积（鞋带公式，同比例尺）。"""
    pts = annotation.points
    if annotation.geometry != "polygon" or len(pts) < 3:
        return 0.0
    area2 = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    return round(abs(area2) / 2.0 * SCALE_M_PER_UNIT**2, 1)


# ---------------------------------------------------------------------------
# 3. 态势问询引擎


@dataclass(frozen=True)
class InquiryAnswer:
    """doc 03 §13.1 要求的六字段回答。topic 为内部主题标记。"""

    topic: str
    conclusion: str
    data_time: str
    sources: tuple[str, ...]
    location: str
    confidence: str
    unconfirmed: str
    matched_topics: tuple[str, ...] = field(default_factory=tuple)


def visible_statuses(role: str) -> frozenset[AnnotationStatus]:
    """按角色过滤可见的标绘核验状态（doc 03 §12 权限矩阵）。"""
    if role == "commander":
        return frozenset(AnnotationStatus)
    if role == "member":
        return frozenset(
            {
                AnnotationStatus.PENDING_REVIEW,
                AnnotationStatus.VERIFIED,
                AnnotationStatus.PUBLISHED,
            }
        )
    return frozenset({AnnotationStatus.PUBLISHED})  # 公共屏


def _status_word(status: AnnotationStatus) -> str:
    from .map_annotations import status_word as _sw

    return _sw(status)


def route_question(question: str) -> tuple[str | None, tuple[str, ...]]:
    """返回命中的主主题与全部命中主题。无命中返回 (None, ())。"""
    hits: list[str] = []
    for keyword, topic in _QUESTION_TOPICS:
        if keyword in question and topic not in hits:
            hits.append(topic)
    # Missing *information* is a distinct intent; material/personnel quantities
    # and questions about past decisions retain their own topics.
    information = r'(?:资料|信息|依据|数据)'
    missing = (any(word in question for word in ('缺项','信息缺口','资料缺口'))
        or re.search(r'缺(?:少)?(?:什么|哪些|哪几项|哪方面|的)?'+information, question)
        or re.search(information+r'.{0,8}(?:还缺|缺少|不足|待补)',question)
        or re.search(r'(?:补充|补齐)(?:什么|哪些|哪几项)?'+information,question)
        or ('决策' in question and re.search(r'还缺(?:什么|哪些)',question)))
    resolved_history = re.search(r'(?:解决了|补齐了|关闭了|消除了).*(?:缺口|缺项)',question)
    if missing and not resolved_history:
        hits = ['gaps']+[topic for topic in hits if topic != 'gaps']
    if not hits:
        return None, ()
    return hits[0], tuple(hits)


def answer_inquiry(
    question: str,
    ws,
    event=None,
    ledger: MaterialsLedger | None = None,
    role: str = "commander",
    mask_phone_fn=None,
    *,
    event_id: str | None = None,
) -> InquiryAnswer:
    """关键词路由的真实数据问答。查不到就如实说无法回答。"""
    primary, all_hits = route_question(question)
    data_time = _latest_ws_time(ws) or (
        getattr(event, "updated_at", "") if event is not None else ""
    )
    sources: list[str] = []
    unconfirmed = ""
    confidence = "高"

    def _none_answer(reason: str) -> InquiryAnswer:
        return InquiryAnswer(
            topic=primary or "unknown",
            conclusion=reason,
            data_time=data_time,
            sources=(),
            location="—",
            confidence="—",
            unconfirmed="",
            matched_topics=all_hits,
        )

    if primary is None:
        supported = "、".join(
            ["队伍在岗", "火线火点", "水源", "物资补给", "区域面积",
             "指挥员决策", "命令执行", "信息缺口", "待决策事项"]
        )
        return _none_answer(
            f"无法从当前数据回答该问题。当前支持问询的主题：{supported}。"
        )
    annotations = getattr(ws, "annotations", {}) or {}
    allowed = visible_statuses(role)

    if primary == "personnel":
        bindings = getattr(ws, "roster_bindings", {}) or {}
        active = [b for b in bindings.values() if b.status == "active"]
        if not active:
            return _none_answer("尚无已入岗人员（花名册无活动绑定记录）。")
        lines = []
        for binding in sorted(active, key=lambda b: getattr(b, "pos_code", "")):
            person = getattr(binding, "person", None)
            phone = getattr(person, "phone", "") if person else ""
            name = (getattr(person, "name", "") or phone or "？") if person else "？"
            slot = getattr(binding, "pos_code", "") or "未定岗"
            shown_phone = phone
            if role != "commander" and mask_phone_fn is not None:
                shown_phone = mask_phone_fn(phone)
            lines.append(f"{name}（{shown_phone} · {slot}）")
        sources = [f"binding:{getattr(b, 'id', '')}" for b in active[:8]]
        return InquiryAnswer(
            topic="personnel",
            conclusion=f"当前在岗 {len(active)} 人：" + "；".join(lines),
            data_time=data_time,
            sources=tuple(sources),
            location="首页组织架构原图 · 各编制槽位",
            confidence="高",
            unconfirmed=(
                f"另有 {len(bindings) - len(active)} 条绑定不在活动状态"
                if len(bindings) > len(active)
                else ""
            ),
            matched_topics=all_hits,
        )

    if primary in {"annotations", "water"}:
        wanted_kinds = (
            {AnnotationKind.WATER}
            if primary == "water"
            else {
                AnnotationKind.FIRE_POINT,
                AnnotationKind.FIRELINE,
                AnnotationKind.ISOLATION,
                AnnotationKind.AREA,
            }
        )
        marks = [
            a
            for a in annotations.values()
            if a.kind in wanted_kinds and a.status in allowed
        ]
        draft_pending = [
            a
            for a in annotations.values()
            if a.kind in wanted_kinds
            and a.status not in allowed
            and a.status != AnnotationStatus.WITHDRAWN
        ]
        if not marks:
            reason = (
                f"当前图上没有{('水源' if primary == 'water' else '火线/火点类')}标绘。"
                + (
                    f"另有 {len(draft_pending)} 条你无权查看的未发布记录。"
                    if draft_pending and role == "public"
                    else ""
                )
            )
            return _none_answer(reason)
        parts = []
        for mark in marks:
            head = f"{mark.title}（{_status_word(mark.status)}"
            if mark.certainty:
                head += f"/{'疑似' if mark.certainty == 'suspected' else '已确认'}"
            pos = "、".join(f"({x:.0f},{y:.0f})" for x, y in mark.points[:3])
            parts.append(f"{head}·示意坐标 {pos}）")
        unconfirmed = ""
        if role == "commander":
            pending_n = sum(
                1
                for a in annotations.values()
                if a.kind in wanted_kinds
                and a.status == AnnotationStatus.DRAFT
            )
            unconfirmed = (
                f"草稿 {pending_n} 条尚未提交核验" if pending_n else ""
            )
        elif any(a.status == AnnotationStatus.PENDING_REVIEW for a in marks):
            unconfirmed = "存在待核验的候选标注，未进入正式态势"
        return InquiryAnswer(
            topic=primary,
            conclusion=f"图上相关标绘 {len(marks)} 条：" + "；".join(parts),
            data_time=data_time,
            sources=tuple(f"mark:{a.id}" for a in marks[:8]),
            location="沙盘地图（示意图层）",
            confidence="高" if all(a.status == AnnotationStatus.PUBLISHED for a in marks) else "中",
            unconfirmed=unconfirmed,
            matched_topics=all_hits,
        )

    if primary == "materials":
        # Material answers are scoped to an explicit event, never ledger order.
        # Review callers have an ID without a registry object; both must agree
        # when provided. Missing data must not inherit another source's time.
        data_time = ""
        record_event_id = getattr(event, "event_id", "") if event is not None else ""
        if event_id is not None and record_event_id and event_id != record_event_id:
            return _none_answer("查询事件与当前事件不一致，请重新选择事件。")
        event_id = record_event_id if event_id is None else event_id
        if not event_id:
            return _none_answer("尚未选择当前事件，请先进入事件再查询物资。")
        snapshot = getattr(ledger, "snapshots", {}).get(event_id)
        if snapshot is None:
            return _none_answer("本事件尚无到场物资快照，无法确认到场物资情况。")
        if getattr(snapshot, "event_id", "") != event_id:
            return _none_answer("本事件物资快照归属不一致，请核查记录。")
        target = snapshot
        data_time = getattr(snapshot, "updated_at", "") or ""
        squads = getattr(target, "squads", {}) or {}
        total_lines = sum(len(getattr(s, "lines", {})) for s in squads.values())
        missing = []
        for squad in squads.values():
            for line in getattr(squad, "lines", {}).values():
                qty_usable = getattr(line, "qty_usable", None)
                missing_n = getattr(line, "missing", 0) or 0
                if missing_n or qty_usable is None:
                    item_name = getattr(line, "item_name", "") or getattr(line, "item_id", "")
                    squad_label = getattr(squad, "name", "") or getattr(squad, "squad_id", "未知队伍")
                    missing.append(f"{squad_label}/{item_name}")
        conclusion = (
            f"到场中队 {len(squads)} 支，随队清单行 {total_lines} 行。"
            if squads
            else "本事件尚无到场物资快照。"
        )
        if missing:
            conclusion += f"待关注：{'、'.join(missing[:5])}"
        from .material_inventory_reads import read_event, citation, describe
        try:
            views = read_event(ws, ledger, event_id)
        except ValueError:
            return InquiryAnswer(
                topic='materials', conclusion=conclusion + ' 当前物资流水来源待核对，不能确认可用量。',
                data_time='', sources=(f'snapshot:{event_id}',),
                location='/materials 页 · 数量来源待核', confidence='待核',
                unconfirmed='到场清单行数仅是历史登记；缺少完整来源时不回退到原携行数量。',
                matched_topics=all_hits)
        conclusion += ' 当前事件物资报告：' + ('；'.join(
            describe(view, ledger.items[item]) for item, view in views.items()) or '清单为空。')
        return InquiryAnswer(
            topic="materials",
            conclusion=conclusion,
            data_time=data_time,
            sources=(f"snapshot:{event_id}", citation(views, ledger, event_id)),
            location="/materials 页 · 到场情况段",
            confidence="报告来源可追溯，实物待核",
            unconfirmed="数量来自具名报告，非独立核验；留置/移交归属未核不计总账可用，正式当前及预计缺口仍待任务和专业用量依据。",
            matched_topics=all_hits,
        )

    if primary == "areas":
        areas = [
            a
            for a in annotations.values()
            if a.geometry == "polygon" and a.status in allowed
        ]
        if not areas:
            return _none_answer("图上暂无面状标绘（可用自由测算后保存为业务对象）。")
        parts = [
            f"{a.title}（{_status_word(a.status)}·约 {indicative_area_m2(a):,.0f} m² 示意值）"
            for a in areas
        ]
        return InquiryAnswer(
            topic="areas",
            conclusion="；".join(parts),
            data_time=data_time,
            sources=tuple(f"mark:{a.id}" for a in areas[:8]),
            location="沙盘地图（示意图层）",
            confidence="低",
            unconfirmed="面积为沙盘示意换算值，非实测；正式数字需测绘或影像判读确认",
            matched_topics=all_hits,
        )

    if primary == "decisions":
        decisions = getattr(ws, "decisions", {}) or {}
        if not decisions:
            return _none_answer("尚无指挥员决策记录。")
        latest = list(decisions.values())[-1]
        return InquiryAnswer(
            topic="decisions",
            conclusion=(
                f"共 {len(decisions)} 条决策；最新一条：{latest.reason}"
                f"（{latest.decided_by}）"
            ),
            data_time=data_time,
            sources=(f"decision:{latest.id}",),
            location="/overview 指挥台总览 · 决策历史",
            confidence="高",
            unconfirmed="",
            matched_topics=all_hits,
        )

    if primary == "orders":
        orders = getattr(ws, "orders", {}) or {}
        if not orders:
            return _none_answer("尚无执行命令。")
        by_status: dict[str, int] = {}
        for order in orders.values():
            key = str(order.status)
            by_status[key] = by_status.get(key, 0) + 1
        summary = "、".join(f"{k} {v} 条" for k, v in sorted(by_status.items()))
        blocked = [
            order.id
            for order in orders.values()
            if str(order.status) in {"paused", "failed"}
        ]
        unconfirmed = f"异常命令：{'、'.join(blocked)}，需重新研判" if blocked else ""
        return InquiryAnswer(
            topic="orders",
            conclusion=f"命令 {len(orders)} 条（{summary}）。",
            data_time=data_time,
            sources=tuple(f"order:{o.id}" for o in list(orders.values())[:8]),
            location="/orders 命令页",
            confidence="高",
            unconfirmed=unconfirmed,
            matched_topics=all_hits,
        )

    if primary == "gaps":
        gaps = getattr(ws, "information_gaps", {}) or {}
        open_gaps = [
            g for g in gaps.values() if getattr(g.status, "value", str(g.status)) == "open"
        ]
        if not open_gaps:
            return _none_answer("当前无未关闭的信息缺口。")
        subjects = [
            getattr(g, "subject", "") or getattr(g, "question", "") or str(g.id)[:12]
            for g in open_gaps[:5]
        ]
        return InquiryAnswer(
            topic="gaps",
            conclusion=f"未关闭信息缺口 {len(open_gaps)} 条：\n"+'\n'.join(f'{i}. {s}' for i,s in enumerate(subjects,1)),
            data_time=data_time,
            sources=tuple(f"gap:{g.id}" for g in open_gaps[:5]),
            location="/brief 态势简报页",
            confidence="高",
            unconfirmed="以上缺口本身即待核实内容",
            matched_topics=all_hits,
        )

    # pending：待决策事项
    questions = getattr(ws, "general_questions", {}) or {}
    goals = getattr(ws, "goal_frames", {}) or {}
    proposals = getattr(ws, "proposals", {}) or {}
    open_q = sum(1 for q in questions.values() if getattr(q, "status", "") == "open")
    unconfirmed_goals = sum(
        1 for g in goals.values() if getattr(g, "confirmed_by", None) is None
    )
    active_p = sum(1 for p in proposals.values() if getattr(p, "status", "") == "active")
    if not (open_q or unconfirmed_goals or active_p):
        return _none_answer("当前没有待决策事项。")
    return InquiryAnswer(
        topic="pending",
        conclusion=(
            f"待答请示 {open_q} 项，待确认目标框 {unconfirmed_goals} 项，"
            f"参谋活跃建议 {active_p} 条。"
        ),
        data_time=data_time,
        sources=("workspace:pending_counts",),
        location="右侧待决策卡 · /questions /brief",
        confidence="高",
        unconfirmed="",
        matched_topics=all_hits,
    )


SUPPORTED_TOPICS_NOTE = (
    "支持问询：队伍在岗 / 火线火点 / 水源 / 物资补给 / 区域面积 / "
    "决策 / 命令 / 信息缺口 / 待决策事项"
)
