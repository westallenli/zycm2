"""P16-T15：复盘趋势与统计口径（doc 05 §7 定版）。纯标准库、只读计算。

定版纪律（05 号 §7 原文）：
- 指标公开口径 = 名称 / 计算方法 / 样本数量 / 缺失处理 / 时间边界；
- 不做黑盒综合评分，必须可下钻到事实；
- 无数据源的典型指标如实标注「暂无数据源」并说明原因，绝不编造数值。

数据源边界（诚实声明）：
- 异常任务占比：各事件任务空间链上的 OrderActivated / OrderAnomalyPaused
  计数——立即可算；
- 物资缺口频次：全局物资台账的到场快照行 missing>0 —— 立即可算；
- 队伍平均到场时长：依赖链上 FireEventStarted.at 与
  PersonCheckedIn.at 双时间戳（P16-T15 起新捕获的事件才具备），
  旧事件按缺失处理规则排除并留痕；
- 其余典型指标当前无事实源，见 _UNAVAILABLE_METRICS。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MetricResult:
    """单个指标的公开口径与结果。value=None 表示本样本集下不可算。"""

    key: str
    name: str
    method: str              # 计算方法（公开口径）
    unit: str
    sample_count: int        # 纳入计算的样本数
    excluded_count: int      # 因缺失被排除的样本数
    missing_policy: str      # 缺失处理
    time_bound: str          # 时间边界
    value: str | None
    available: bool          # False = 整体无数据源（结构性缺失）
    unavailable_reason: str = ""
    samples: tuple[dict[str, str], ...] = ()


#: 结构性无数据源的典型指标（doc 05 §7 列举；理由如实，待事实源接入后改算）。
_UNAVAILABLE_METRICS: tuple[dict[str, str], ...] = (
    {
        "key": "first_situation_duration",
        "name": "首态势形成时长",
        "reason": "链上无「态势形成」事实事件（该概念未落为可审计动作），无法计时。",
    },
    {
        "key": "command_ack_rate",
        "name": "命令接收率",
        "reason": "成员侧无命令回执事实（沙盘下发无接收确认环节），分母分子均不存在。",
    },
    {
        "key": "ontime_task_rate",
        "name": "任务按时完成率",
        "reason": "订单/任务模型无截止时间字段，无法判定「按时」，不猜测约定。",
    },
    {
        "key": "feedback_missing_rate",
        "name": "反馈缺失率",
        "reason": "「应反馈集合」无事实定义（哪些动作必须反馈未建模），无法计缺失。",
    },
    {
        "key": "comm_outage_duration",
        "name": "通信异常影响时长",
        "reason": "通信通道整体标注「待接入」，无任何通信状态事实可计时。",
    },
    {
        "key": "reignite_handling_duration",
        "name": "复燃处置时长",
        "reason": "整改后复现已有关联记录，但无「复燃处置起止」事实对，无法计时。",
    },
)

_TIME_BOUND_ALL = "全部已登记事件（创建至今），逐事件快照独立计入"


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat((ts or "").strip())
    except ValueError:
        return None


def _load_spaces(registry, load_event_space):
    """逐事件装载任务空间；loader 异常/返回 None 一律记为加载失败。"""
    spaces: list[tuple[str, object, str]] = []
    for event_id in getattr(registry, "events", {}):
        note = ""
        space = None
        try:
            space = load_event_space(event_id) if load_event_space else None
        except Exception as exc:  # noqa: BLE001 - 单事件损坏不拖垮整体
            space = None
            note = f"快照加载失败：{exc}"
        if space is None and not note:
            note = "任务空间快照不存在"
        spaces.append((event_id, space, note))
    return spaces


def _anomaly_ratio(spaces) -> MetricResult:
    activated = 0
    anomaly = 0
    excluded = 0
    samples: list[dict[str, str]] = []
    for event_id, space, note in spaces:
        if space is None:
            excluded += 1
            samples.append({"event_id": event_id, "detail": f"排除——{note}"})
            continue
        kinds = [getattr(e, "kind", "") for e in space.events]
        a = kinds.count("OrderActivated")
        p = kinds.count("OrderAnomalyPaused")
        activated += a
        anomaly += p
        detail = f"激活 {a} 单，异常暂停 {p} 单" if a or p else "链上无订单记录"
        samples.append({"event_id": event_id, "detail": detail})
    value = None
    if activated > 0:
        pct = anomaly * 100.0 / activated
        value = f"{anomaly}/{activated}（{pct:.1f}%）"
    return MetricResult(
        key="anomaly_order_ratio",
        name="异常任务占比",
        method=("各事件任务空间哈希链上 OrderAnomalyPaused 事件数 ÷ "
                "OrderActivated 事件数，跨事件聚合；可下钻到逐事件计数"),
        unit="占比",
        sample_count=len([s for s in spaces if s[1] is not None]),
        excluded_count=excluded,
        missing_policy="快照缺失或加载失败的事件排除并逐条留痕，不计入分母",
        time_bound=_TIME_BOUND_ALL,
        value=value,
        available=True,
        unavailable_reason=(
            "" if activated > 0 else "全部可及事件链上均无已激活订单，暂无分母"),
        samples=tuple(samples),
    )


def _shortfall_freq(ledger) -> MetricResult:
    base = dict(
        key="material_shortfall_freq",
        name="物资缺口频次",
        method=("物资台账到场快照中存在缺口行（missing > 0）的快照数 ÷ "
                "快照总数，跨事件聚合"),
        unit="占比",
        missing_policy="仅统计台账内已有到场快照的事件；无快照事件不计入",
        time_bound="物资台账内全部到场快照（含快照 updated_at）",
    )
    snapshots = list(getattr(ledger, "snapshots", {}).values()) if ledger else []
    if ledger is None:
        return MetricResult(**base, sample_count=0, excluded_count=0,
                            value=None, available=False,
                            unavailable_reason="物资台账未接入面板上下文。")
    total = len(snapshots)
    if total == 0:
        # 已接入但尚无样本：结构可用，不算「无数据源」
        return MetricResult(**base, sample_count=0, excluded_count=0,
                            value=None, available=True)
    hit = 0
    samples: list[dict[str, str]] = []
    for snap in snapshots:
        lines = [ln for sq in snap.squads for ln in sq.lines]
        short = [ln for ln in lines if (ln.missing or 0) > 0]
        if short:
            hit += 1
        amount = sum(ln.missing for ln in short)
        samples.append({
            "event_id": snap.event_id,
            "detail": (
                f"{'存在缺口' if short else '无缺口'}："
                f"{len(short)} 行缺 {amount:g}"
                + (f"（{snap.updated_at}）" if snap.updated_at else "")),
        })
    pct = hit * 100.0 / total
    return MetricResult(**base, sample_count=total, excluded_count=0,
                        value=f"{hit}/{total}（{pct:.1f}%）", available=True,
                        samples=tuple(samples))


def _arrival_avg(spaces) -> MetricResult:
    policy = ("仅统计启动镜像与签到事件双时间戳齐备的事件；P16-T15 前捕获的"
              "事件无时间戳载荷，排除并计数，不回推猜测")
    durations: list[float] = []
    excluded = 0
    included = 0
    samples: list[dict[str, str]] = []
    for event_id, space, note in spaces:
        if space is None:
            excluded += 1
            samples.append({"event_id": event_id,
                            "detail": f"排除——{note}"})
            continue
        start_ts = None
        checkins = []
        for e in space.events:
            payload = getattr(e, "payload", {}) or {}
            if e.kind == "FireEventStarted":
                start_ts = _parse_iso(str(payload.get("at", "")))
            elif e.kind == "PersonCheckedIn":
                ts = _parse_iso(str(payload.get("at", "")))
                if ts is not None:
                    checkins.append(ts)
        if start_ts is None or not checkins:
            excluded += 1
            reason = ("缺启动时间戳（T15 前）" if start_ts is None
                      else "签到事件无时间戳（T15 前捕获）")
            samples.append({"event_id": event_id,
                            "detail": f"排除——{reason}"})
            continue
        first = min(checkins)
        minutes = (first - start_ts).total_seconds() / 60.0
        included += 1
        durations.append(minutes)
        samples.append({
            "event_id": event_id,
            "detail": f"首名队员于启动后 {minutes:.1f} 分钟签到",
        })
    value = None
    reason = ""
    if included > 0:
        avg = sum(durations) / len(durations)
        value = f"平均 {avg:.1f} 分钟（n={included}）"
    else:
        reason = ("现有事件均缺双时间戳（T15 起新捕获的事件才具备），"
                  "待新事件产生后自动纳入")
    return MetricResult(
        key="avg_arrival_duration",
        name="队伍平均到场时长",
        method=("每事件取「FireEventStarted.at → 最早 PersonCheckedIn.at」"
                "为首支队伍到场时长，跨事件算术平均"),
        unit="分钟",
        sample_count=included,
        excluded_count=excluded,
        missing_policy=policy,
        time_bound=_TIME_BOUND_ALL,
        value=value,
        available=True,
        unavailable_reason=reason,
        samples=tuple(samples),
    )


def compute_trend_metrics(registry, ledger=None, load_event_space=None) -> tuple[MetricResult, ...]:
    """计算 doc 05 §7 指标集：可算的算，不可算的如实交代。只读，零写入。"""
    spaces = _load_spaces(registry, load_event_space)
    computed = (_arrival_avg(spaces), _anomaly_ratio(spaces),
                _shortfall_freq(ledger))
    unavailable = tuple(
        MetricResult(
            key=m["key"], name=m["name"],
            method="（暂无公开口径——无事实源时不虚构计算方法）",
            unit="", sample_count=0, excluded_count=0,
            missing_policy="整体无数据源，等待事实接入后定义口径",
            time_bound="不适用", value=None, available=False,
            unavailable_reason=m["reason"],
        )
        for m in _UNAVAILABLE_METRICS
    )
    return computed + unavailable
