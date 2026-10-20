"""P19 战术态势内核（docs/19_沙盘战术态势符号改造最终定稿_V1.0）。

单一职责：对象注册表、受控词、逐类字段与发布闸门、时效复核、空间冲突
计算、符号与图层元数据。纯规则、零第三方依赖、确定性、fail-closed。

分工边界：
- 本模块不认识 HTTP，也不写审计；渲染与路由分别由 sandview / panel 调用；
- `geo_points` 是真实地图唯一权威位置（[纬度, 经度]），本模块只按地理坐标
  计算米制地表距离，绝不做投影猜测，绝不编造数值；
- 旧式通用标绘（火点/火线/隔离带/区域/路线/箭头/文字…）保留原类型，只借用
  符号显示，不自动获得新战术语义（定稿 §5.3 旧对象映射）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

__all__ = [
    "TacticalError",
    "RouteStatus",
    "FreshnessStatus",
    "ConflictLevel",
    "ConflictStatus",
    "PositioningStatus",
    "CommandStage",
    "CommandControlState",
    "TaskLevel",
    "ReceiveResult",
    "ReceiptType",
    "KIND_SPECS",
    "kind_spec",
    "spec_for",
    "geometry_of",
    "legacy_display_kind",
    "is_tactical",
    "detail_word",
    "kind_word",
    "route_word",
    "fresh_word",
    "conflict_word",
    "positioning_word",
    "layer_word",
    "stage_word",
    "control_word",
    "task_word",
    "receive_word",
    "receipt_word",
    "belt_style",
    "validate_geometry",
    "verify_gate",
    "publish_gate",
    "freshness_of",
    "detect_conflicts",
    "ConflictRecord",
    "CommandFlow",
    "ground_distance_m",
]


class TacticalError(ValueError):
    """战术态势规则错误；消息可直接显示给指挥员。"""


# ---------------------------------------------------------------------------
# 受控枚举（接口/数据库用英文值，前台一律经 * 词表转中文）


class RouteStatus(str, Enum):
    """撤离/小队路线状态（定稿 §7.1），不是标绘状态。"""

    PROPOSED = "proposed"
    PENDING_CHECK = "pending_check"
    CONFIRMED = "confirmed"
    RESTRICTED = "restricted"
    CLOSED = "closed"
    COMPLETED = "completed"


class FreshnessStatus(str, Enum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    EXPIRED = "expired"


class ConflictLevel(str, Enum):
    NOTICE = "notice"
    REVIEW = "review"
    BLOCKED = "blocked"
    DECISION = "decision"


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class PositioningStatus(str, Enum):
    LOCATED = "located"
    ROUGH = "rough"
    UNREGISTERED = "unregistered"


class CommandStage(str, Enum):
    """命令主状态（定稿 §6.2），十态，不许跳跃。"""

    DRAFT = "draft"
    PENDING_VALIDATION = "pending_validation"
    PENDING_APPROVAL = "pending_approval"
    PENDING_CONFIRM = "pending_confirm"
    CONFIRMED_UNACK = "confirmed_unack"
    ACKED_NOT_STARTED = "acked_not_started"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE_PENDING_VERIFY = "done_pending_verify"
    CLOSED = "closed"


class CommandControlState(str, Enum):
    """异常/控制状态：与主状态正交，不覆盖主状态。"""

    NORMAL = "normal"
    ACK_OVERDUE = "ack_overdue"
    PAUSED = "paused"
    WITHDRAW_NOTICED = "withdraw_noticed"
    AWAITING_STOP = "awaiting_stop"
    WITHDRAW_RUNNING = "withdraw_running"
    WITHDRAW_DONE = "withdraw_done"
    AWAITING_REPLACEMENT = "awaiting_replacement"
    EXEC_ABNORMAL = "exec_abnormal"
    UNVERIFIABLE = "unverifiable"
    ABNORMAL_CLOSED = "abnormal_closed"


class TaskLevel(str, Enum):
    URGENT = "urgent"
    HIGH_RISK = "high_risk"
    NORMAL = "normal"


class ReceiveResult(str, Enum):
    ACCEPTED_READY = "accepted_ready"
    ACCEPTED_CONDITIONAL = "accepted_conditional"
    NOT_YET = "not_yet"
    REJECT_REORGANIZE = "reject_reorganize"


class ReceiptType(str, Enum):
    ARRIVED = "arrived"
    RUNNING = "running"
    STAGE_DONE = "stage_done"
    ALL_DONE = "all_done"
    PARTIAL_DONE = "partial_done"
    CANNOT_EXECUTE = "cannot_execute"
    EXEC_ABNORMAL = "exec_abnormal"
    PERSONNEL_SAFETY = "personnel_safety"
    COMM_ABNORMAL = "comm_abnormal"
    TASK_PAUSED = "task_paused"
    WITHDRAW_ACK = "withdraw_ack"
    STOPPED = "stopped"
    RETREATING = "retreating"
    CANNOT_STOP = "cannot_stop"
    PERSONNEL_ABNORMAL = "personnel_abnormal"
    COMM_LOST_PENDING = "comm_lost_pending"


_WORDS: dict[str, dict[str, str]] = {
    "route_status": {
        RouteStatus.PROPOSED.value: "建议路线",
        RouteStatus.PENDING_CHECK.value: "待核查",
        RouteStatus.CONFIRMED.value: "已确认",
        RouteStatus.RESTRICTED.value: "通行受限",
        RouteStatus.CLOSED.value: "已封闭",
        RouteStatus.COMPLETED.value: "已完成撤离",
    },
    "freshness": {
        FreshnessStatus.OK.value: "正常",
        FreshnessStatus.NEEDS_REVIEW.value: "需要复核",
        FreshnessStatus.EXPIRED.value: "已失效",
    },
    "conflict_level": {
        ConflictLevel.NOTICE.value: "一级提示",
        ConflictLevel.REVIEW.value: "二级复核",
        ConflictLevel.BLOCKED.value: "三级阻断",
        ConflictLevel.DECISION.value: "四级决断",
    },
    "positioning": {
        PositioningStatus.LOCATED.value: "已定位",
        PositioningStatus.ROUGH.value: "人工粗略覆盖",
        PositioningStatus.UNREGISTERED.value: "未配准",
    },
    "command_stage": {
        CommandStage.DRAFT.value: "草稿",
        CommandStage.PENDING_VALIDATION.value: "待校验",
        CommandStage.PENDING_APPROVAL.value: "待审批",
        CommandStage.PENDING_CONFIRM.value: "待确认",
        CommandStage.CONFIRMED_UNACK.value: "已确认待接收",
        CommandStage.ACKED_NOT_STARTED.value: "已接收待开始",
        CommandStage.RUNNING.value: "执行中",
        CommandStage.PARTIAL.value: "部分完成",
        CommandStage.DONE_PENDING_VERIFY.value: "已完成待核验",
        CommandStage.CLOSED.value: "已关闭",
    },
    "command_control": {
        CommandControlState.NORMAL.value: "正常",
        CommandControlState.ACK_OVERDUE.value: "接收逾期",
        CommandControlState.PAUSED.value: "暂停",
        CommandControlState.WITHDRAW_NOTICED.value: "撤回通知已发送",
        CommandControlState.AWAITING_STOP.value: "等待停止确认",
        CommandControlState.WITHDRAW_RUNNING.value: "撤回执行中",
        CommandControlState.WITHDRAW_DONE.value: "撤回完成",
        CommandControlState.AWAITING_REPLACEMENT.value: "待替代",
        CommandControlState.EXEC_ABNORMAL.value: "执行异常",
        CommandControlState.UNVERIFIABLE.value: "无法核验",
        CommandControlState.ABNORMAL_CLOSED.value: "异常关闭",
    },
    "task_level": {
        TaskLevel.URGENT.value: "紧急",
        TaskLevel.HIGH_RISK.value: "高风险",
        TaskLevel.NORMAL.value: "普通",
    },
    "receive_result": {
        ReceiveResult.ACCEPTED_READY.value: "已接收，具备执行条件",
        ReceiveResult.ACCEPTED_CONDITIONAL.value: "已接收，部分条件待补充",
        ReceiveResult.NOT_YET.value: "暂不能接收",
        ReceiveResult.REJECT_REORGANIZE.value: "无法执行，请求重新组织",
    },
    "receipt_type": {
        ReceiptType.ARRIVED.value: "已到达",
        ReceiptType.RUNNING.value: "执行中",
        ReceiptType.STAGE_DONE.value: "阶段完成",
        ReceiptType.ALL_DONE.value: "全部完成",
        ReceiptType.PARTIAL_DONE.value: "部分完成",
        ReceiptType.CANNOT_EXECUTE.value: "无法执行",
        ReceiptType.EXEC_ABNORMAL.value: "执行异常",
        ReceiptType.PERSONNEL_SAFETY.value: "人员安全异常",
        ReceiptType.COMM_ABNORMAL.value: "通信异常",
        ReceiptType.TASK_PAUSED.value: "任务暂停",
        ReceiptType.WITHDRAW_ACK.value: "已收到撤回",
        ReceiptType.STOPPED.value: "已停止执行",
        ReceiptType.RETREATING.value: "正在回撤",
        ReceiptType.CANNOT_STOP.value: "无法立即停止",
        ReceiptType.PERSONNEL_ABNORMAL.value: "人员状态异常",
        ReceiptType.COMM_LOST_PENDING.value: "通信中断待确认",
    },
    "belt_status": {
        "planned": "计划",
        "opened": "已开设",
        "repairing": "抢修中",
    },
    "route_type": {
        "plan": "计划路线",
        "confirmed": "确认路线",
        "actual": "实际轨迹",
        "evacuation": "撤离路线",
        "primary": "主路线",
        "backup": "备用路线",
    },
    "block_level": {
        "partial": "部分阻断",
        "full": "完全阻断",
    },
    "risk_level": {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
    },
    "yes_no": {"valid": "有效", "invalid": "无效", "pending": "待核实"},
    "hot_status": {"confirmed": "已确认", "suspected": "疑似"},
    "safety_status": {"safe": "安全", "confirmed": "已确认安全", "unsafe": "担心不安全"},
    "safety_check": {"passed": "检查通过", "failed": "检查未通过", "pending": "待检查"},
    "open_status": {"open": "开放", "closed": "关闭", "enabled": "启用"},
    "standby_status": {"standby": "待命", "moving": "转场中", "engaged": "已投入"},
    "availability": {"available": "可用", "limited": "部分可用", "unavailable": "不可用"},
    "draw_status": {"available": "可取水", "dry": "无水", "blocked": "无法取水"},
    "support_status": {"ready": "保障到位", "pending": "待保障"},
    "recheck_status": {"checked": "已复查", "pending": "待复查"},
    "risk_status": {"threatened": "受威胁", "safe": "暂无威胁", "burning": "已受火"},
    "position_status": {"reported": "人工回报", "realtime": "实时定位", "last_known": "最后已知位置"},
    "comm_status": {"ok": "畅通", "down": "中断", "pending": "待接入"},
    "belt_form": {"open": "开放带", "closed": "闭合环", "open_belt": "开放带",
                  "closed_ring": "闭合环"},
    "range_status": {"spreading": "蔓延中", "stable": "趋稳", "controlled": "已控制",
                     "checking": "研判中"},
    "priority": {"high": "重点", "medium": "一般", "low": "观察"},
    "protection_level": {"level1": "一级", "level2": "二级", "level3": "三级"},
}


def _word(table: str, value: object) -> str:
    if value is None or value == "":
        return "待核实"
    return _WORDS[table].get(str(value), str(value))


def route_word(v: object) -> str:
    return _word("route_status", v)


def fresh_word(v: object) -> str:
    return _word("freshness", v)


def conflict_word(v: object) -> str:
    return _word("conflict_level", v)


def positioning_word(v: object) -> str:
    return _word("positioning", v)


def stage_word(v: object) -> str:
    return _word("command_stage", v)


def control_word(v: object) -> str:
    return _word("command_control", v)


def task_word(v: object) -> str:
    return _word("task_level", v)


def receive_word(v: object) -> str:
    return _word("receive_result", v)


def receipt_word(v: object) -> str:
    return _word("receipt_type", v)


def belt_word(v: object) -> str:
    return _word("belt_status", v)


def route_type_word(v: object) -> str:
    return _word("route_type", v)


def block_word(v: object) -> str:
    return _word("block_level", v)


def risk_word(v: object) -> str:
    return _word("risk_level", v)


# ---------------------------------------------------------------------------
# 图层：6 组业务图层 + 12 层渲染层（定稿 §10.1）


@dataclass(frozen=True)
class LayerSpec:
    key: str
    label: str
    render_layer: int
    default_on: bool
    scope: str  # business / state


LAYERS: tuple[LayerSpec, ...] = (
    LayerSpec("fire", "火情态势", 3, True, "business"),
    LayerSpec("control", "防控作业", 4, True, "business"),
    LayerSpec("force", "兵力行动", 5, True, "business"),
    LayerSpec("support", "指挥保障", 6, True, "business"),
    LayerSpec("risk", "风险环境", 7, False, "business"),
    LayerSpec("evidence", "证据影像", 2, False, "business"),
)

RENDER_LAYERS: tuple[tuple[int, str], ...] = (
    (1, "基础地图"),
    (2, "无人机影像证据"),
    (3, "火情态势"),
    (4, "防控作业"),
    (5, "兵力行动"),
    (6, "指挥保障"),
    (7, "风险环境和重点保护目标"),
    (8, "草稿与待核验"),
    (9, "执行观察和回执"),
    (10, "方案推演"),
    (11, "历史撤回"),
    (12, "当前选中高亮"),
)

LAYER_LABELS = {s.key: s.label for s in LAYERS}


def layer_word(v: object) -> str:
    if not v:
        return "待归类"
    return LAYER_LABELS.get(str(v), str(v))


# ---------------------------------------------------------------------------
# 对象注册表：P0 十五类 + P1 十类


@dataclass(frozen=True)
class KindSpec:
    kind: str
    label: str
    geometry: str            # point / line / polygon / point_or_line / point_or_polygon
    tier: str                # P0 / P1 / legacy
    layer: str
    glyph: str
    color: str
    default_on: bool
    verify_fields: tuple[str, ...] = ()
    publish_fields: tuple[str, ...] = ()
    freshness_minutes: int | None = None
    publish_needs_unit: bool = True
    rule: str = ""


def _spec(**kw) -> KindSpec:
    kw.setdefault("default_on", kw.get("tier") == "P0")
    if kw["kind"] == "hazard_zone":
        kw["default_on"] = False          # 危险区属火情核心，但位于风险环境层，默认关
    if kw["kind"] in {"drone_coverage", "evacuation_route", "control_line"}:
        kw["default_on"] = False
    return KindSpec(**kw)


_P0 = (
    _spec(kind="fire_area", label="火场范围", geometry="polygon", tier="P0",
          layer="fire", glyph="fire_area", color="#C0392B",
          verify_fields=("basis", "observed_at", "source_ref"),
          publish_fields=("range_status",),
          rule="8 至 12 个不规则顶点；不得用圆或规则多边形冒充；发布需研判依据与核验记录"),
    _spec(kind="main_fireline", label="当前重点火线", geometry="line", tier="P0",
          layer="fire", glyph="fireline", color="#8E1B14",
          verify_fields=("priority", "observed_at", "team", "source_ref"),
          rule="独立于火场边界；不得复制火场轮廓充当正式火线"),
    _spec(kind="hotspot", label="火点/热点", geometry="point", tier="P0",
          layer="fire", glyph="hotspot", color="#D35400",
          verify_fields=("found_at", "hot_status", "source_ref"),
          rule="待核验热点不得渲染成已确认火点"),
    _spec(kind="spread_direction", label="蔓延方向", geometry="line", tier="P0",
          layer="fire", glyph="arrow", color="#E4572E",
          verify_fields=("direction", "basis", "decided_at"),
          rule="只表达研判方向，不等同于实际火线"),
    _spec(kind="hazard_zone", label="危险区", geometry="polygon", tier="P0",
          layer="risk", glyph="hazard", color="#D35400",
          verify_fields=("risk_type", "risk_level", "basis", "formed_at"),
          publish_fields=("affected",), freshness_minutes=30,
          rule="独立建模，不能自动复制火场范围"),
    _spec(kind="firebreak", label="隔火带", geometry="line", tier="P0",
          layer="control", glyph="belt", color="#7A4B2A",
          verify_fields=("planned_width", "belt_status", "team"),
          publish_fields=("belt_form",),
          rule="保存中心线与米制宽度；已开设无实际宽度不得发布；实际宽度不得大于计划宽度"),
    _spec(kind="team_position", label="小队当前位置", geometry="point", tier="P0",
          layer="force", glyph="team", color="#1F5C4C",
          verify_fields=("team_id", "leader", "task", "reported_at", "comm_status"),
          rule="无实时来源不显示实时位置，回报时间必须显式"),
    _spec(kind="team_route", label="小队行进路线", geometry="line", tier="P0",
          layer="force", glyph="route", color="#245B78",
          verify_fields=("team_id", "route_type", "purpose", "start", "end"),
          publish_fields=("plan_time",),
          rule="未绑定小队不能保存为正式路线；实际轨迹必须有真实来源"),
    _spec(kind="command_post", label="前线指挥部", geometry="point", tier="P0",
          layer="support", glyph="hq", color="#245B78",
          verify_fields=("level", "leader", "comm_status", "active_since"),
          rule="指挥层级与通信状态必须显式"),
    _spec(kind="water_source", label="水源地", geometry="point", tier="P0",
          layer="support", glyph="water", color="#2E86AB",
          verify_fields=("water_type", "draw_status", "unit"),
          rule="无实测供水量显示待核实，不得编造数字"),
    _spec(kind="supply_point", label="物资保障点", geometry="point", tier="P0",
          layer="support", glyph="supply", color="#B77A1A",
          verify_fields=("category", "unit", "availability"),
          rule="无来源库存显示待核实"),
    _spec(kind="medical_point", label="医疗救护点", geometry="point", tier="P0",
          layer="support", glyph="medical", color="#2F7D55",
          verify_fields=("capability", "open_status", "unit"),
          rule="无来源床位与人数显示待核实"),
    _spec(kind="drone_position", label="无人机位置", geometry="point", tier="P0",
          layer="evidence", glyph="drone", color="#4A4E69",
          verify_fields=("drone_id", "operator", "heading", "reported_at"),
          rule="不显示无来源电量、高度、链路质量"),
    _spec(kind="drone_coverage", label="无人机影像覆盖区", geometry="polygon", tier="P0",
          layer="evidence", glyph="coverage", color="#5C6B73",
          verify_fields=("image_id", "captured_at", "positioning", "transform"),
          publish_needs_unit=False,
          rule="未配准不得生成正式对象；人工粗略覆盖必须常驻限制说明"),
    _spec(kind="protected_target", label="重点保护目标", geometry="point_or_polygon", tier="P0",
          layer="support", glyph="target", color="#1D3557",
          verify_fields=("target_type", "protection_level", "risk_status", "unit"),
          rule="保护等级与风险状态必须显式"),
)

_P1 = (
    _spec(kind="evacuation_route", label="撤离路线", geometry="line", tier="P1",
          layer="control", glyph="evac", color="#2A9D8F", default_on=False,
          verify_fields=("start", "end", "audience", "unit", "checked_at", "source_ref"),
          publish_fields=("endpoint_basis",),
          freshness_minutes=30,
          rule="穿过已发布危险区为三级阻断，不得发布；主备同时受阻升级四级"),
    _spec(kind="comm_blind_zone", label="通信盲区", geometry="polygon", tier="P1",
          layer="risk", glyph="hazard", color="#8E7C39", default_on=False,
          verify_fields=("found_by", "found_at", "affected", "source_ref"),
          freshness_minutes=120,
          rule="不能由回报超时自动生成，也不等同于失联"),
    _spec(kind="control_line", label="封控线", geometry="line", tier="P1",
          layer="control", glyph="control", color="#6D597A", default_on=False,
          verify_fields=("reason", "unit", "active_since", "release_cond"),
          freshness_minutes=120,
          rule="发布需命令或司令确认记录"),
    _spec(kind="road_block", label="道路阻断点", geometry="point", tier="P1",
          layer="risk", glyph="block", color="#9B2226", default_on=False,
          verify_fields=("road", "block_level", "found_at", "source_ref"),
          freshness_minutes=60,
          rule="完全阻断必须有现场或有效外部依据"),
    _spec(kind="staging_point", label="集结点", geometry="point", tier="P1",
          layer="force", glyph="staging", color="#3A6B5E", default_on=False,
          verify_fields=("purpose", "unit", "open_status", "safety_status"),
          freshness_minutes=120,
          rule="进入已发布危险区不能发布"),
    _spec(kind="reserve_point", label="预备队待命点", geometry="point", tier="P1",
          layer="force", glyph="reserve", color="#4F6D7A", default_on=False,
          verify_fields=("team_id", "leader", "standby_status", "reported_at"),
          rule="不等于实时位置；待命状态变化即需复核"),
    _spec(kind="burned_area", label="过火区", geometry="polygon", tier="P1",
          layer="risk", glyph="burned", color="#7F4F24", default_on=False,
          verify_fields=("burned_at", "basis", "recheck_status", "unit"),
          rule="不能自动代替当前火场范围"),
    _spec(kind="wind_marker", label="风向风速", geometry="point_or_line", tier="P1",
          layer="risk", glyph="wind", color="#41729F", default_on=False,
          verify_fields=("direction", "measured_at", "source_ref"),
          freshness_minutes=30,
          rule="风速无实测来源不填数字；过期标记复核"),
    _spec(kind="lookout_post", label="观察哨", geometry="point", tier="P1",
          layer="risk", glyph="lookout", color="#5A7D9A", default_on=False,
          verify_fields=("person_or_unit", "open_status", "last_report"),
          freshness_minutes=60,
          rule="回报不能自动变成正式火线"),
    _spec(kind="drone_pad", label="无人机起降点", geometry="point", tier="P1",
          layer="evidence", glyph="pad", color="#556B2F", default_on=False,
          verify_fields=("pad_type", "unit", "support_status", "safety_check"),
          freshness_minutes=120,
          rule="不能由无人机位置自动生成；安全检查通过才能发布"),
)

#: 旧式通用标绘：保留原类型，仅供兼容与「待人工归类」。
_LEGACY = (
    KindSpec("fire_point", "火点（旧）", "point", "legacy", "fire", "hotspot", "#B84A42", True,
             rule="通用点标注，不自动获得战术字段"),
    KindSpec("fireline", "火线（旧）", "line", "legacy", "fire", "fireline", "#B84A42", True,
             rule="通用线标注，不自动发布为正式火线"),
    KindSpec("isolation", "隔离带（旧）", "line", "legacy", "control", "belt", "#8A5A44", True,
             rule="通用线标注，不自动获得隔火带宽度语义"),
    KindSpec("water", "水源（旧）", "point", "legacy", "support", "water", "#245B78", True),
    KindSpec("material", "物资点（旧）", "point", "legacy", "support", "supply", "#B77A1A", True),
    KindSpec("landing_zone", "起降点（旧）", "point", "legacy", "evidence", "pad", "#2F7D68", True),
    KindSpec("headquarters", "指挥部（旧）", "point", "legacy", "support", "hq", "#2F7D68", True),
    KindSpec("route", "路线（旧）", "line", "legacy", "force", "line", "#245B78", True),
    KindSpec("arrow", "箭头（旧）", "line", "legacy", "fire", "arrow", "#245B78", True),
    KindSpec("area", "区域（旧）", "polygon", "legacy", "risk", "area", "#B77A1A", True),
    KindSpec("text", "文字（旧）", "point", "legacy", "support", "text", "#55606B", True),
)

KIND_SPECS: dict[str, KindSpec] = {s.kind: s for s in (*_P0, *_P1, *_LEGACY)}

#: 仅用于显示层：旧对象借用同类战术符号，不改变 kind、不升级语义。
_LEGACY_GLYPH = {
    "fire_point": "hotspot",
    "fireline": "fireline",
    "isolation": "belt",
    "water": "water",
    "material": "supply",
    "headquarters": "hq",
    "landing_zone": "pad",
    "arrow": "arrow",
}

#: 定稿 §10.2 显示规则的几何宽度（像素，典型桌面缩放）。
_PIXEL_WIDTH = {
    "fireline": 4,
    "main_fireline": 4,
    "belt": 12,
    "firebreak": 12,
    "route": 3,
    "team_route": 3,
    "evacuation_route": 3,
    "evac": 3,
    "spread_direction": 3,
}


def kind_spec(kind: str) -> KindSpec:
    spec = KIND_SPECS.get(str(kind))
    if spec is None:
        raise TacticalError(f"未登记的对象类型：{kind!r}")
    return spec


def spec_for(mark) -> KindSpec:
    return kind_spec(_kind_value(mark))


def _kind_value(mark) -> str:
    k = getattr(mark, "kind", "")
    return getattr(k, "value", str(k))


def is_tactical(kind: str) -> bool:
    return kind_spec(kind).tier in {"P0", "P1"}


def geometry_of(kind: str) -> str:
    g = kind_spec(kind).geometry
    return {"point_or_line": "line", "point_or_polygon": "polygon"}.get(g, g)


def legacy_display_kind(kind: str) -> str:
    """旧类型的显示替代（只影响符号/颜色，不影响语义与状态）。"""
    return _LEGACY_GLYPH.get(kind, "")


def kind_word(kind: str) -> str:
    try:
        spec = kind_spec(kind)
    except TacticalError:
        return str(kind)
    return spec.label


def glyph_of(kind: str) -> str:
    """渲染用符号键（旧类型只借符号，不改语义）。"""
    return kind_spec(kind).glyph


def color_of(kind: str) -> str:
    return kind_spec(kind).color


def detail_word(key: str) -> str:
    return DETAIL_LABELS.get(key, key)


def belt_style(kind: str, details: dict[str, str] | None = None) -> dict[str, object]:
    """战术线型的像素样式：重点火线 ≥4px，路线 ≥3px，隔火带 8–14px。"""
    d = details or {}
    kind_ = str(kind)
    out: dict[str, object] = {
        "weight": _PIXEL_WIDTH.get(kind_, 3),
        "dash": "",
        "arrows": kind_ in {"spread_direction", "team_route", "evacuation_route"},
    }
    if kind_ in {"firebreak", "isolation"}:
        status = d.get("belt_status", "")
        out["weight"] = min(14, max(8, int(_PIXEL_WIDTH.get("belt", 12))))
        out["dash"] = {"planned": "10 6", "repairing": "3 5"}.get(status, "")
        out["belt_m"] = _float_or_none(d.get("actual_width") or d.get("planned_width"))
    return out


def detail_display(key: str, value: object) -> str:
    """专属字段取值转中文（只影响显示，不改存储值）。"""
    table_name = {
        "hot_status": "hot_status", "safety_status": "safety_status",
        "safety_check": "safety_check", "open_status": "open_status",
        "standby_status": "standby_status", "availability": "availability",
        "draw_status": "draw_status", "support_status": "support_status",
        "recheck_status": "recheck_status", "risk_status": "risk_status",
        "position_status": "position_status", "comm_status": "comm_status",
        "belt_form": "belt_form", "range_status": "range_status",
        "priority": "priority", "protection_level": "protection_level",
        "belt_status": "belt_status", "route_type": "route_type",
        "route_status": "route_status", "block_level": "block_level",
        "risk_level": "risk_level", "positioning": "positioning",
    }.get(key)
    text = str(value if value is not None else "").strip()
    if not table_name:
        return text
    return _WORDS[table_name].get(text, text)


def controlled_display(key: str, value: object) -> str:
    """受控值 → 中文显示（未知值原样输出，不谎译）。"""
    table = {
        "belt_status": "belt_status",
        "route_type": "route_type",
        "block_level": "block_level",
        "risk_level": "risk_level",
        "positioning": "positioning",
        "positioning_status": "positioning",
        "route_status": "route_status",
        "conflict_level": "conflict_level",
        "freshness_status": "freshness",
    }.get(key)
    if not table:
        return str(value)
    return _WORDS[table].get(str(value), str(value))


def gap_view(gap: dict[str, str]) -> list[list[str]]:
    """隔火带缺口子记录的中文展示（随父对象，不单独上图）。"""
    return [
        ["编号", str(gap.get("ref") or "缺口")],
        ["状态", str(gap.get("status") or "待核实")],
        ["区段", str(gap.get("segment") or "待核实")],
        ["宽度", (str(gap["width_m"]) + " 米") if gap.get("width_m") else "待核实"],
        ["回报时间", str(gap.get("reported_at") or "待核实")],
        ["来源", str(gap.get("source") or "待核实")],
    ]


def detail_value_ok(text: object) -> bool:
    """字段值可用：非空且不是“见备注”类回避词。"""
    v = str(text or "").strip()
    return bool(v) and v not in _NOTE_EVASION


#: 定稿 §5.2：事实 / 判断 / 业务动作必须分栏，不能合写一句话。
FACT_FIELDS = frozenset({
    "demo_code", "source_ref", "observed_at", "captured_at", "found_at", "formed_at",
    "reported_at", "measured_at", "checked_at", "verified_at", "burned_at",
    "active_since", "actual_width", "planned_width", "completion", "gap_segment",
    "gap_segment_index", "gap_fraction",
    "gap_width_m", "gap_reported_at", "gap_source", "safety_confirm_at",
    "last_report", "position",
})
JUDGE_FIELDS = frozenset({
    "basis", "range_status", "priority", "hot_status", "direction", "decided_at",
    "risk_type", "risk_level", "affected", "belt_status", "belt_form", "gap_status",
    "position_status", "comm_status", "availability", "open_status", "safety_status",
    "standby_status", "recheck_status", "refire_risk", "risk_status", "protection_level",
    "block_level", "draw_status", "support_status", "safety_check", "watch_content",
})
ACTION_FIELDS = frozenset({
    "unit", "team", "team_id", "leader", "task", "purpose", "audience", "route_type",
    "start", "end", "plan_time", "order_ref", "endpoint_basis", "category",
    "capability", "water_type", "pad_type", "person_or_unit", "operator", "level",
    "reason", "release_cond", "road", "bypass", "alt_comm", "found_by",
    "commander_confirm", "image_id", "drone_id", "transform", "opacity",
})
_FIELD_GROUPS = {"fact": "事实", "judge": "判断", "action": "业务动作"}


def field_group(key: str) -> str:
    if key in JUDGE_FIELDS:
        return "judge"
    if key in ACTION_FIELDS:
        return "action"
    return "fact"


def field_group_word(group: str) -> str:
    return _FIELD_GROUPS.get(group, "事实")


#: 首版工具栏收敛为五组（定稿 §11.1）；同一时间只激活一个绘制工具。
TOOLBAR_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("火情", ("fire_area", "main_fireline", "hotspot", "spread_direction")),
    ("作业", ("firebreak", "evacuation_route", "control_line")),
    ("兵力", ("team_position", "team_route", "staging_point", "reserve_point")),
    ("保障", ("command_post", "water_source", "supply_point", "medical_point",
              "protected_target")),
    ("环境与证据", ("hazard_zone", "road_block", "comm_blind_zone", "burned_area",
                    "wind_marker", "lookout_post", "drone_position", "drone_coverage",
                    "drone_pad")),
)


#: 逐类额外提供的作业字段（不放宽闸门，只是少手填）。
KIND_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
    "firebreak": ("actual_width", "completion", "gap_ref", "gap_status", "gap_segment",
                  "gap_segment_index", "gap_fraction", "gap_width_m", "gap_reported_at",
                  "gap_source"),
    "evacuation_route": ("route_status", "safety_confirm_at", "plan_time", "order_ref"),
    "team_route": ("route_status", "reported_at", "risk_level", "order_ref"),
    "drone_coverage": ("opacity",),
    "hazard_zone": ("checked_at",),
    "road_block": ("bypass",),
    "wind_marker": ("speed",),
    "staging_point": ("safety_confirm_at",),
    "control_line": ("checked_at", "order_ref"),
    "team_position": ("position_status", "risk_level"),
}

#: 所有类型都可以登记的通用作业字段（不入必填闸门）。
COMMON_DETAIL_FIELDS = frozenset({
    "demo_code", "source_ref", "unit", "team", "checked_at", "commander_confirm",
    "order_ref", "related_plan", "note_ref", "manual_style", "manual_shape", "manual_color",
})


def allowed_detail_keys(kind: str) -> frozenset[str]:
    """本类型允许登记的专属字段（写入前校验，不吃表单残留的无关字段）。

    必填/发布字段 + 本类额外作业字段 + 通用字段 + 数量类字段；
    数量字段虽然允许录入，但没有来源时在发布闸门就会被拦下（不编造数据）。
    """
    spec = kind_spec(kind)
    if spec.tier == "legacy":
        return frozenset(DETAIL_LABELS)
    return (frozenset(spec.verify_fields) | frozenset(spec.publish_fields)
            | frozenset(KIND_EXTRA_FIELDS.get(kind, ()))
            | COMMON_DETAIL_FIELDS | NUMERIC_FIELDS)


def kind_field_keys(kind: str) -> tuple[str, ...]:
    """信息窗输入顺序：必填 → 发布补充 → 额外作业字段。"""
    spec = kind_spec(kind)
    keys: list[str] = []
    for key in list(spec.verify_fields) + list(spec.publish_fields):
        if key not in keys:
            keys.append(key)
    for key in KIND_EXTRA_FIELDS.get(kind, ()):
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def controlled_options(key: str) -> tuple[tuple[str, str], ...]:
    """受控下拉选项（中文显示，内部英文值）。"""
    table = {
        "belt_status": "belt_status",
        "route_type": "route_type",
        "block_level": "block_level",
        "risk_level": "risk_level",
        "positioning": "positioning",
    }.get(key)
    if not table:
        return ()
    return tuple(_WORDS[table].items())


def _float_or_none(text: object) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 字段中文名（前台标签，绝不显示英文键）

DETAIL_LABELS: dict[str, str] = {
    "basis": "研判依据", "observed_at": "观测时间", "source_ref": "来源编号",
    "range_status": "范围状态", "checked_at": "核查时间", "priority": "重点等级",
    "team": "责任小队", "found_at": "发现时间", "hot_status": "热点状态",
    "direction": "风向", "decided_at": "研判时间", "risk_type": "风险类型",
    "risk_level": "风险等级", "formed_at": "形成时间", "affected": "影响对象",
    "planned_width": "计划宽度（米）", "actual_width": "实际宽度（米）",
    "belt_status": "隔火带状态", "belt_form": "带形", "completion": "完成比例",
    "gap_ref": "缺口编号", "gap_status": "缺口状态", "gap_segment": "缺口区段",
    "gap_segment_index": "缺口所在边序号（从 1 起）", "gap_fraction": "缺口在该边的位置（0-1）",
    "gap_width_m": "缺口宽度（米）", "gap_reported_at": "缺口回报时间",
    "gap_source": "缺口来源", "team_id": "小队编号", "leader": "负责人",
    "task": "任务", "reported_at": "回报时间", "position_status": "位置状态",
    "comm_status": "通信状态", "route_type": "路线类型", "purpose": "用途",
    "start": "起点", "end": "终点", "plan_time": "计划时间", "order_ref": "关联命令",
    "audience": "适用对象", "endpoint_basis": "终点依据",
    "safety_confirm_at": "安全确认时间", "level": "指挥层级",
    "active_since": "启用时间", "water_type": "水源类型", "draw_status": "取水状态",
    "supply_amount": "供水量", "unit": "责任单位", "category": "保障类别",
    "availability": "可用状态", "stock": "库存", "capability": "救护能力",
    "open_status": "开放状态", "beds": "床位数", "drone_id": "无人机编号",
    "operator": "操作单位", "heading": "航向", "image_id": "影像编号",
    "captured_at": "采集时间", "positioning": "定位状态", "transform": "覆盖变换",
    "opacity": "透明度", "target_type": "目标类型", "protection_level": "保护等级",
    "risk_status": "风险状态", "found_by": "发现方式", "alt_comm": "替代通信方式",
    "reason": "封控原因", "release_cond": "解除条件", "road": "道路名称",
    "block_level": "阻断程度", "bypass": "绕行提示", "safety_status": "安全状态",
    "standby_status": "待命状态", "burned_at": "过火时间", "recheck_status": "复查状态",
    "refire_risk": "复燃风险", "speed": "风速", "measured_at": "测量时间",
    "position": "位置", "person_or_unit": "责任人或单位", "last_report": "最后回报时间",
    "watch_content": "观察内容", "pad_type": "点位类型", "support_status": "保障状态",
    "safety_check": "安全检查状态", "commander_confirm": "司令确认",
    "demo_code": "演示编号", "route_status": "路线状态",
    "capacity": "通行能力", "personnel_count": "人数",
}

#: 数值类字段：无来源编号时禁止登记具体数字（红线：不编造数据）。
NUMERIC_FIELDS = frozenset({
    "supply_amount", "stock", "beds", "speed", "personnel_count", "capacity",
})
#: 备注不能替代必填字段；“待核实”一类占位词同样不算已填（防止绕过闸门）。
_NOTE_EVASION = frozenset({
    "见备注", "详见备注", "备注", "略", "无", "-", "n/a", "N/A", "待定",
    "待核实", "待补充", "待填写", "未知", "暂无", "不清", "不确定",
})


# ---------------------------------------------------------------------------
# 几何校验（地理坐标，米制地表距离；不做投影，不补造顶点）


def ground_distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _plane(pt: tuple[float, float], origin: tuple[float, float]) -> tuple[float, float]:
    lat0, lon0 = origin
    mx = 111320.0 * math.cos(math.radians(lat0))
    return ((pt[1] - lon0) * mx, (pt[0] - lat0) * 110540.0)


def _seg_point_dist_m(p, a, b) -> float:
    o = a
    px, py = _plane(p, o)
    ax, ay = _plane(a, o)
    bx, by = _plane(b, o)
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segs_intersect(p1, p2, p3, p4) -> bool:
    d1, d2 = _cross(p3, p4, p1), _cross(p3, p4, p2)
    d3, d4 = _cross(p1, p2, p3), _cross(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _point_in_ring(pt, ring) -> bool:
    x, y = pt
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1) if y2 != y1 else x1
            if x < xin:
                inside = not inside
    return inside


def irregular_ring(lat: float, lon: float, n: int = 10, radius_deg: float = 0.012,
                   ) -> tuple[tuple[float, float], ...]:
    """确定性不规则闭合环（顶点数 n，半径按固定三角函数变化）。

    只用于代绘/演示的示意轮廓：不用随机数，不假装实测；
    半径变化保证不会被当成圆或正多边形（见 irregular_ok）。
    """
    pts: list[tuple[float, float]] = []
    kx = 1.0 / max(0.2, math.cos(math.radians(lat)))
    for i in range(n):
        a = 2.0 * math.pi * i / n
        rr = radius_deg * (1.0 + 0.34 * math.sin(3.0 * a) + 0.17 * math.cos(5.0 * a))
        pts.append((round(lat + rr * math.sin(a), 5),
                    round(lon + rr * math.cos(a) * kx, 5)))
    return tuple(pts)


def poly_length_m(pts) -> float:
    return round(sum(ground_distance_m(a, b) for a, b in zip(pts, pts[1:])), 1)


def poly_area_m2(ring) -> float:
    """球面近似面积（平方米）；输入为去重后的开环顶点。"""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(lat0))
    plane = [((lon - ring[0][1]) * mx, (lat - ring[0][0]) * 110540.0) for lat, lon in ring]
    s = 0.0
    n = len(plane)
    for i in range(n):
        x1, y1 = plane[i]
        x2, y2 = plane[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return round(abs(s) / 2.0, 1)


def validate_geometry(kind: str, geo_points) -> tuple[tuple[float, float], ...]:
    """战术类型的几何闸门：点数、不同顶点、闭合、自相交、零面积。"""
    spec = kind_spec(kind)
    geo = tuple((float(a), float(b)) for a, b in (geo_points or ()))
    if not geo:
        raise TacticalError(f"{spec.label}缺少地理坐标：不在图上猜测位置")
    for lat, lon in geo:
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise TacticalError(f"经纬度 ({lat}, {lon}) 超出有效范围（纬度 ±90，经度 ±180）")
    geom = spec.geometry
    if geom == "point":
        return (geo[0],)
    if geom == "point_or_line":
        if len(geo) == 1:
            return geo
        return _checked_line(kind, geo)
    if geom == "point_or_polygon":
        if len(geo) == 1:
            return geo
        return _checked_poly(kind, geo)
    if geom == "line":
        return _checked_line(kind, geo)
    return _checked_poly(kind, geo)


def _checked_line(kind: str, geo):
    distinct = list(dict.fromkeys(geo))
    if len(distinct) < 2:
        raise TacticalError(f"{kind_spec(kind).label}至少需要 2 个不同节点，当前 {len(distinct)} 个")
    return tuple(distinct)


def _checked_poly(kind: str, geo):
    label = kind_spec(kind).label
    ring = list(geo)
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]                    # 服务端可补闭合首点，不补造缺失顶点
    if len(dict.fromkeys(ring)) < 3:
        raise TacticalError(f"{label}至少需要 3 个不同顶点，当前 {len(dict.fromkeys(ring))} 个")
    closed = ring + [ring[0]]
    for i, (a, b) in enumerate(zip(closed, closed[1:])):
        for j, (c, d) in enumerate(zip(closed, closed[1:])):
            if abs(i - j) <= 1 or (i == 0 and j == len(closed) - 2):
                continue
            if _segs_intersect(a, b, c, d):
                raise TacticalError(f"{label}边界自相交，不能保存")
    if poly_area_m2(ring) <= 0.0:
        raise TacticalError(f"{label}面积为零（顶点共线），不能保存")
    return tuple(ring)


def irregular_ok(geo, *, min_vertices: int = 8, max_vertices: int = 12,
                 tolerance: float = 0.02) -> bool:
    """火场范围/危险区：顶点数落在区间，且不是圆/椭圆/正多边形这类规则轮廓。

    判据：将各顶点换算为米制平面坐标后拟合中心二次曲线 Ax²+Bxy+Cy²=1；
    拟合残差几乎为零＝顶点全落在同一圆锥截线上（圆、椭圆、正多边形），
    不能当作真实火场的不规则轮廓。本判据只用于“是不是规则图形”的把关，
    不代表任何现场安全结论。
    """
    pts = list(geo or ())
    n = len(pts)
    if n < min_vertices or n > max_vertices:
        return False
    lat0 = sum(p[0] for p in pts) / n
    lon0 = sum(p[1] for p in pts) / n
    kx = 111320.0 * math.cos(math.radians(lat0))
    plane = [((lon - lon0) * kx, (lat - lat0) * 110540.0) for lat, lon in pts]
    if poly_area_m2([(p[0], p[1]) for p in pts]) <= 0.0:
        return False
    rows = [[x * x, x * y, y * y] for x, y in plane]
    scale = max((abs(v) for row in rows for v in row), default=0.0)
    if scale <= 0.0:
        return False
    rows = [[v / scale for v in row] for row in rows]
    normal = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    rhs = [sum(r[i] for r in rows) for i in range(3)]
    coef = _solve3(normal, rhs)
    if coef is None:
        return True                       # 退化（共线/奇异）：不是规则轮廓，但几何已另有闸门
    resid = [sum(c * v for c, v in zip(coef, row)) - 1.0 for row in rows]
    rms = math.sqrt(sum(v * v for v in resid) / n)
    return rms >= tolerance


def _solve3(matrix, vector):
    """3×3 线性方程组高斯消元；奇异时返回 None（不猜测解）。"""
    a = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        a[col] = [v / pv for v in a[col]]
        for r in range(3):
            if r == col:
                continue
            factor = a[r][col]
            a[r] = [vr - factor * vc for vr, vc in zip(a[r], a[col])]
    return [a[i][3] for i in range(3)]


# ---------------------------------------------------------------------------
# 逐类字段校验与发布闸门


def _missing_details(mark, keys) -> list[str]:
    """必填专属字段检查；备注不能替代字段（写“见备注”视为缺失）。"""
    details = dict(getattr(mark, "details", {}) or {})
    missing = []
    for k in keys:
        v = str(details.get(k, "") or "").strip()
        if not v or v in _NOTE_EVASION:
            missing.append(k)
    return missing


def _num_fields(mark) -> list[str]:
    """数值字段无来源编号 → 视为编造数据。"""
    details = dict(getattr(mark, "details", {}) or {})
    src = str(details.get("source_ref", "") or "").strip()
    bad = []
    for k in NUMERIC_FIELDS:
        v = str(details.get(k, "") or "").strip()
        if not v:
            continue
        if _float_or_none(v) is None:
            bad.append(k)
            continue
        if not src:
            bad.append(k)
    return bad


def kind_specific_errors(mark, spec: KindSpec, *, stage: str) -> list[str]:
    """逐类专属规则（定稿 §5.4、§7.3）；返回中文错误列表。"""
    d = {k: str(v or "").strip() for k, v in dict(getattr(mark, "details", {}) or {}).items()}
    errs: list[str] = []
    kind = spec.kind

    if kind == "fire_area" and stage in {"verify", "publish"}:
        if len(getattr(mark, "geo_points", ()) or ()) < 8:
            errs.append("火场范围顶点少于 8 个，不规则范围不能上图")
        elif not irregular_ok(mark.geo_points):
            errs.append("火场范围顶点过于规则，不能用圆或规则多边形冒充")

    if kind == "firebreak" and stage in {"verify", "publish"}:
        planned, actual = _float_or_none(d.get("planned_width")), _float_or_none(d.get("actual_width"))
        if planned is not None and planned <= 0:
            errs.append("计划宽度必须大于 0 米")
        if planned is not None and actual is not None and actual > planned + 0.01:
            errs.append("实际宽度高于计划宽度，先修订计划宽度或重新标绘中心线")
        if stage == "publish" and d.get("belt_status") == "opened" and actual is None:
            errs.append("已开设隔火带缺少实际宽度，不能发布")

    if kind in {"team_route", "evacuation_route"} and stage in {"verify", "publish"}:
        if kind == "team_route" and not d.get("team_id"):
            errs.append("小队路线未绑定小队，不能保存为正式路线")
        if d.get("route_type") == "actual" and not d.get("source_ref"):
            errs.append("实际轨迹没有真实来源，不能登记")
        if d.get("route_type") == "confirmed" and not (d.get("order_ref") or d.get("commander_confirm")):
            errs.append("确认路线缺少关联命令或司令确认记录")

    if kind in {"control_line", "evacuation_route"} and stage == "publish":
        if not (d.get("order_ref") or d.get("commander_confirm")):
            errs.append(
                ("撤离路线" if kind == "evacuation_route" else "封控线")
                + "缺少关联命令或司令确认，不能发布"
            )
        if kind == "evacuation_route":
            for k in ("start", "end", "audience", "checked_at"):
                if not d.get(k):
                    errs.append(f"撤离路线缺少{DETAIL_LABELS.get(k, k)}")

    if kind == "drone_coverage":
        if stage == "verify" and not d.get("image_id"):
            errs.append("影像覆盖缺少影像编号")
        if stage == "publish" and d.get("positioning") != PositioningStatus.LOCATED.value:
            errs.append("影像未配准，不能生成正式对象；请保留为待核验或草稿")

    if kind in {"water_source", "supply_point", "medical_point"} and stage == "publish":
        bad = _num_fields(mark)
        if bad:
            errs.append("下列数量没有来源，只能显示待核实："
                        + "、".join(DETAIL_LABELS.get(b, b) for b in bad))

    if kind == "wind_marker" and stage == "publish":
        if d.get("speed") and not d.get("source_ref"):
            errs.append("风速没有实测来源，不得显示数字")

    if kind == "road_block" and stage == "publish":
        if d.get("block_level") == "full" and d.get("source_ref") in ("", None):
            errs.append("完全阻断缺少现场或有效外部依据，不能发布")

    if kind == "staging_point" and stage == "publish":
        if d.get("safety_status") not in {"safe", "confirmed"}:
            errs.append("集结点缺少安全确认，不能发布")

    if kind == "drone_pad" and stage == "publish":
        if d.get("safety_check") != "passed":
            errs.append("起降点安全检查未通过，不能发布")

    if kind == "hotspot" and stage == "publish" and d.get("hot_status") == "suspected":
        errs.append("疑似热点不能作为已确认火点发布")

    if errs:
        return errs
    return errs


def verify_gate(mark) -> tuple[str, ...]:
    """提交核验闸门（草稿 → 待核验）。"""
    spec = kind_spec(_kind_value(mark))
    if spec.tier == "legacy":
        return ()
    missing = _missing_details(mark, spec.verify_fields)
    errs = [f"{spec.label}提交核验缺少：{'、'.join(DETAIL_LABELS.get(m, m) for m in missing)}"] if missing else []
    return tuple(errs + kind_specific_errors(mark, spec, stage="verify"))


def publish_gate(mark, *, siblings=(), conflicts=(), now: str = "") -> tuple[str, ...]:
    """发布闸门：通用条件 + 逐类条件 + 冲突阻断（定稿 §5.5、§7.3、§8.1）。"""
    kind = _kind_value(mark)
    spec = kind_spec(kind)
    if spec.tier == "legacy":
        return ()
    errs: list[str] = []
    missing = _missing_details(mark, spec.publish_fields)
    if missing:
        errs.append(f"{spec.label}发布缺少：{'、'.join(DETAIL_LABELS.get(m, m) for m in missing)}")

    d = dict(getattr(mark, "details", {}) or {})
    if not str(d.get("source_ref", "") or "").strip() and not getattr(mark, "source", None):
        errs.append("缺少来源，不能发布")
    if spec.publish_needs_unit and not (
            str(getattr(mark, "responsible_unit", "") or "").strip()
            or str(d.get("unit", "") or "").strip()
            or str(d.get("team", "") or "").strip()):
        errs.append("缺少责任单位，不能发布")
    if not str(getattr(mark, "verified_at", "") or "").strip() and not dict(
            getattr(mark, "verification_record", {}) or {}).get("at"):
        errs.append("没有核验记录，不能发布")
    if not getattr(mark, "geo_points", ()):
        errs.append("没有地理坐标，不能发布到真实地图")

    fresh = freshness_of(mark, siblings=siblings, now=now)
    if fresh is FreshnessStatus.EXPIRED:
        errs.append("对象已超过复核周期，先重新核查再发布")

    blocked = [c for c in conflicts
               if c.route_id == getattr(mark, "id", "")
               and c.status == ConflictStatus.OPEN.value
               and c.level in {ConflictLevel.REVIEW.value,
                              ConflictLevel.BLOCKED.value,
                              ConflictLevel.DECISION.value}]
    if blocked:
        words = "、".join(conflict_word(c.level) for c in
                          {b.conflict_id: b for b in blocked}.values())
        errs.append(f"该路线存在{words}，按定稿不得发布（可保存草稿或提交核验）")
    return tuple(errs + kind_specific_errors(mark, spec, stage="publish"))


def freshness_of(mark, *, siblings=(), now: str = "") -> FreshnessStatus:
    """时效状态：只产生提示，不自动删除，不自动判定安全。"""
    spec = kind_spec(_kind_value(mark))
    minutes = spec.freshness_minutes
    if minutes is None:
        return FreshnessStatus.OK
    d = dict(getattr(mark, "details", {}) or {})
    stamp = (d.get("checked_at") or d.get("last_report") or d.get("reported_at")
             or d.get("measured_at") or getattr(mark, "updated_at", "") or "")
    t = _parse_time(stamp)
    n = _parse_time(now) if now else None
    if t is None:
        return FreshnessStatus.NEEDS_REVIEW
    if n is None:
        return FreshnessStatus.OK
    delta_min = (n - t).total_seconds() / 60.0
    if delta_min > minutes * 2:
        return FreshnessStatus.EXPIRED
    if delta_min > minutes:
        return FreshnessStatus.NEEDS_REVIEW
    return FreshnessStatus.OK


def _parse_time(text: object) -> datetime | None:
    """时间解析：统一去掉时区偏移后比较（本地时钟口径，不跨时区推算）。"""
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _naive(datetime.strptime(s[:26], fmt))
        except ValueError:
            continue
    try:
        return _naive(datetime.fromisoformat(s))
    except ValueError:
        return None


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    offset = value.utcoffset() or timedelta(0)
    return (value - offset).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 路线冲突计算（纯函数；失败必须由调用方显式标记，不得当作“无冲突”）


@dataclass
class ConflictRecord:
    """独立冲突记录（定稿 §5.6）。同一路线版本 + 触发对象版本 + 类型只留一条未关闭记录。"""

    conflict_id: str
    event_id: str
    route_id: str
    route_version: int
    trigger_annotation_id: str
    trigger_version: int
    conflict_type: str
    level: str
    relation: str
    detected_at: str
    status: str = ConflictStatus.OPEN.value
    level_rank: int = 0
    evidence_refs: tuple[str, ...] = ()
    decision_id: str = ""
    handled_by: str = ""
    handled_at: str = ""
    resolution_reason: str = ""
    summary: str = ""

    @property
    def level_word(self) -> str:
        return conflict_word(self.level)

    def as_row(self) -> dict[str, str]:
        return {
            "id": self.conflict_id,
            "route": self.route_id,
            "trigger": self.trigger_annotation_id,
            "level": self.level,
            "level_word": self.level_word,
            "relation": self.relation,
            "summary": self.summary or self.relation,
            "status": "未处理" if self.status == ConflictStatus.OPEN.value else "已关闭",
            "detected_at": self.detected_at,
        }


_RELATION_TEXT = {
    "cross_published_hazard": "穿过已发布危险区",
    "enter_pending_hazard": "进入待核验危险区",
    "near_hazard": "靠近危险区",
    "full_block_on_route": "经过已确认完全阻断点",
    "near_block": "靠近阻断点",
    "in_comm_blind": "经过通信盲区",
    "endpoint_invalid": "终点引用无效或已撤回",
    "no_valid_order": "缺少有效命令",
    "both_routes_blocked": "主备路线均受阻",
    "staging_in_hazard": "集结点位于已发布危险区",
}


def detect_conflicts(marks, *, now: str = "", threshold_near_m: float = 50.0,
                     threshold_notice_m: float = 150.0) -> tuple[ConflictRecord, ...]:
    """路线 × 风险对象的空间冲突扫描；等级按最高取值，所有命中关系保留。"""
    live = [m for m in marks if getattr(m, "status", None) is not None
            and str(getattr(m, "status", "").value if hasattr(getattr(m, "status", ""), "value")
                    else getattr(m, "status", "")) not in {"withdrawn"}]
    published_hazards, pending_hazards = [], []
    for m in live:
        if _kind_value(m) == "hazard_zone":
            st = _status_value(m)
            if st == "published":
                published_hazards.append(m)
            elif st == "pending_review":
                pending_hazards.append(m)
    blocks = [m for m in live if _kind_value(m) == "road_block"]
    blinds = [m for m in live if _kind_value(m) == "comm_blind_zone"]

    found: dict[tuple, ConflictRecord] = {}

    def add(route, trigger, ctype, level, relation, dist=None):
        key = (route_id_of(route), int(getattr(route, "version", 1) or 1),
               route_id_of(trigger), int(getattr(trigger, "version", 1) or 1), ctype)
        if key in found:
            return
        rank = _LEVEL_RANK[level]
        cid = f"cfl-{abs(hash(key)) % 100000:05d}"
        text = _RELATION_TEXT.get(ctype, ctype)
        if dist is not None:
            text += f"（最近距离约 {dist:.0f} 米）"
        found[key] = ConflictRecord(
            conflict_id=cid,
            event_id=str(getattr(route, "event_id", "") or ""),
            route_id=key[0], route_version=key[1],
            trigger_annotation_id=key[2], trigger_version=key[3],
            conflict_type=ctype, level=level, relation=relation or text,
            detected_at=now or "", level_rank=rank,
            evidence_refs=(key[2],),
            summary=text,
        )

    routes = [m for m in live if _kind_value(m) in {"evacuation_route", "team_route"}]
    for route in routes:
        pts = [tuple(p) for p in (getattr(route, "geo_points", ()) or ())]
        if len(pts) < 2:
            continue
        for hz in published_hazards:
            d = _route_polygon_dist(pts, hz)
            if d <= 0.0:
                add(route, hz, "cross_published_hazard", ConflictLevel.BLOCKED.value, "blocked")
            elif d <= threshold_near_m:
                add(route, hz, "near_hazard", ConflictLevel.REVIEW.value, "review", d)
            elif d <= threshold_notice_m:
                add(route, hz, "near_hazard", ConflictLevel.NOTICE.value, "notice", d)
        for hz in pending_hazards:
            d = _route_polygon_dist(pts, hz)
            if d <= threshold_near_m:
                add(route, hz, "enter_pending_hazard", ConflictLevel.REVIEW.value, "review", d)
        for blk in blocks:
            if _status_value(blk) not in {"published", "verified"}:
                continue
            bpt = tuple(blk.geo_points[0]) if getattr(blk, "geo_points", ()) else None
            if bpt is None:
                continue
            d = min(_seg_point_dist_m(bpt, a, b) for a, b in zip(pts, pts[1:]))
            full = str(dict(getattr(blk, "details", {}) or {}).get("block_level", "")) == "full"
            if full and d <= threshold_near_m:
                add(route, blk, "full_block_on_route", ConflictLevel.BLOCKED.value, "blocked", d)
            elif d <= threshold_notice_m:
                add(route, blk, "near_block", ConflictLevel.NOTICE.value, "notice", d)
        for bz in blinds:
            d = _route_polygon_dist(pts, bz)
            if d <= threshold_near_m:
                add(route, bz, "in_comm_blind", ConflictLevel.REVIEW.value, "review", d)
        dets = dict(getattr(route, "details", {}) or {})
        if _kind_value(route) == "evacuation_route" and _status_value(route) in {"published", "verified"}:
            if not (dets.get("order_ref") or dets.get("commander_confirm")):
                add(route, route, "no_valid_order", ConflictLevel.BLOCKED.value, "blocked")

    # 四级：同一适用对象/小队的主备路线同时受阻
    by_group: dict[str, dict[str, list[str]]] = {}
    for rec in found.values():
        if rec.level not in {ConflictLevel.BLOCKED.value, ConflictLevel.DECISION.value}:
            continue
        route = next((r for r in routes if route_id_of(r) == rec.route_id), None)
        if route is None:
            continue
        dets = dict(getattr(route, "details", {}) or {})
        grp = dets.get("audience") or dets.get("team_id") or rec.route_id
        rtype = "backup" if dets.get("route_type") == "backup" else "primary"
        slot = by_group.setdefault(grp, {"primary": [], "backup": []})
        if rec.route_id not in slot[rtype]:
            slot[rtype].append(rec.route_id)
    for grp, slot in by_group.items():
        if slot["primary"] and slot["backup"]:
            trigger = slot["primary"][0], slot["backup"][0]
            for rid in trigger:
                route = next((r for r in routes if route_id_of(r) == rid), None)
                if route is None:
                    continue
                key = (rid, int(getattr(route, "version", 1) or 1), "both", 1, "both_routes_blocked")
                if key in found:
                    continue
                found[key] = ConflictRecord(
                    conflict_id=f"cfl-{abs(hash(key)) % 100000:05d}",
                    event_id=str(getattr(route, "event_id", "") or ""),
                    route_id=rid, route_version=key[1],
                    trigger_annotation_id=trigger[1] if rid == trigger[0] else trigger[0],
                    trigger_version=1, conflict_type="both_routes_blocked",
                    level=ConflictLevel.DECISION.value, relation="decision",
                    detected_at=now or "", level_rank=_LEVEL_RANK[ConflictLevel.DECISION.value],
                    evidence_refs=trigger,
                    summary=f"{grp} 的主路线与备用路线同时受阻，需司令决断",
                )

    # 集结点落入已发布危险区（发布阻断由 publish_gate 复核，这里只登记事实）
    for st in [m for m in live if _kind_value(m) == "staging_point"]:
        if not getattr(st, "geo_points", ()):
            continue
        for hz in published_hazards:
            if _point_in_geo(tuple(st.geo_points[0]), hz):
                add(st, hz, "staging_in_hazard", ConflictLevel.BLOCKED.value, "blocked")
                break

    return tuple(sorted(found.values(), key=lambda c: (-c.level_rank, c.route_id, c.conflict_id)))


_LEVEL_RANK = {
    ConflictLevel.NOTICE.value: 1,
    ConflictLevel.REVIEW.value: 2,
    ConflictLevel.BLOCKED.value: 3,
    ConflictLevel.DECISION.value: 4,
}


def route_id_of(mark) -> str:
    return str(getattr(mark, "id", "") or "")


def _status_value(mark) -> str:
    st = getattr(mark, "status", "")
    return getattr(st, "value", str(st))


def _poly_ring(mark):
    return [tuple(p) for p in (getattr(mark, "geo_points", ()) or ())]


def _route_polygon_dist(pts, polygon) -> float:
    """路线到面的距离：0 表示穿过或进入。"""
    ring = _poly_ring(polygon)
    if len(ring) < 3:
        return 1e9
    closed = ring + [ring[0]]
    for a, b in zip(pts, pts[1:]):
        if _point_in_geo(a, polygon):
            return 0.0
        for c, d in zip(closed, closed[1:]):
            if _segs_intersect(a, b, c, d):
                return 0.0
    best = 1e9
    for a, b in zip(pts, pts[1:]):
        for c, d in zip(closed, closed[1:]):
            best = min(best, _seg_seg_dist(a, b, c, d))
    return best


def _point_in_geo(pt, polygon) -> bool:
    ring = _poly_ring(polygon)
    if len(ring) < 3:
        return False
    plane = [((lon - pt[1]) * 111320.0 * math.cos(math.radians(pt[0])),
              (lat - pt[0]) * 110540.0) for lat, lon in ring]
    return _point_in_ring((0.0, 0.0), plane)


def _seg_seg_dist(p1, p2, p3, p4) -> float:
    o = p1
    a = _plane(p1, o); b = _plane(p2, o); c = _plane(p3, o); d = _plane(p4, o)
    if _segs_intersect(p1, p2, p3, p4):
        return 0.0
    return min(
        _seg_point_dist_m(a, c, d),
        _seg_point_dist_m(b, c, d),
        _seg_point_dist_m(c, a, b),
        _seg_point_dist_m(d, a, b),
    )


def worst_level(conflicts) -> str:
    best, rank = "", 0
    for c in conflicts:
        if c.level_rank > rank:
            best, rank = c.level, c.level_rank
    return best


# ---------------------------------------------------------------------------
# 命令流：主状态 + 正交控制状态 + 送达/接收/条件/回执 + 超时提醒


#: 接收超时阈值（定稿 §9.2；演示默认值，只产生提醒，不自动判定失联）
ACK_TIMEOUTS: dict[str, tuple[int, int, int]] = {
    TaskLevel.URGENT.value: (2, 5, 8),
    TaskLevel.HIGH_RISK.value: (5, 10, 15),
    TaskLevel.NORMAL.value: (10, 20, 30),
}

#: 回执时限（分钟，定稿 §9.4）
RECEIPT_WINDOWS: dict[str, tuple[int, int, int, int, int, int]] = {
    # task_level: (接收, 开始确认, 首次现场, 执行中间隔, 高风险区间隔, 完成提交)
    TaskLevel.URGENT.value: (2, 3, 5, 5, 2, 5),
    TaskLevel.HIGH_RISK.value: (5, 6, 10, 10, 3, 10),
    TaskLevel.NORMAL.value: (10, 15, 20, 20, 5, 20),
}

_STAGE_ORDER = [s.value for s in CommandStage]
_ALLOWED_NEXT: dict[str, set[str]] = {
    CommandStage.DRAFT.value: {CommandStage.PENDING_VALIDATION.value},
    CommandStage.PENDING_VALIDATION.value: {CommandStage.PENDING_APPROVAL.value, CommandStage.DRAFT.value},
    CommandStage.PENDING_APPROVAL.value: {CommandStage.PENDING_CONFIRM.value, CommandStage.DRAFT.value},
    CommandStage.PENDING_CONFIRM.value: {CommandStage.CONFIRMED_UNACK.value, CommandStage.DRAFT.value},
    CommandStage.CONFIRMED_UNACK.value: {CommandStage.ACKED_NOT_STARTED.value},
    CommandStage.ACKED_NOT_STARTED.value: {CommandStage.RUNNING.value},
    CommandStage.RUNNING.value: {CommandStage.PARTIAL.value, CommandStage.DONE_PENDING_VERIFY.value},
    CommandStage.PARTIAL.value: {CommandStage.RUNNING.value, CommandStage.DONE_PENDING_VERIFY.value},
    CommandStage.DONE_PENDING_VERIFY.value: {CommandStage.CLOSED.value, CommandStage.RUNNING.value},
    CommandStage.CLOSED.value: set(),
}

_CLOSE_RESULTS = {
    "completed": "已完成", "partial": "部分完成", "closed_unfinished": "未完成关闭",
    "cancelled": "已取消", "abnormal_closed": "异常关闭", "unverifiable": "无法核验",
}


@dataclass
class CommandFlow:
    """挂在既有命令（ExecutionOrder）之上的战术执行层记录。

    既有 OrderStatus 仍是权力链的事实来源；本记录只补充定稿要求的细粒度
    阶段、控制状态、送达/接收/条件/回执，绝不替代或绕过既有审批闸门。
    """

    order_id: str
    event_id: str = ""
    task_level: str = TaskLevel.NORMAL.value
    stage: str = CommandStage.DRAFT.value
    control_state: str = CommandControlState.NORMAL.value
    version: int = 1
    base_version: int = 0
    target: str = ""
    unit: str = ""
    route_id: str = ""
    start_time: str = ""
    deadline: str = ""
    safety_cond: str = ""
    stop_cond: str = ""
    receipt_need: str = ""
    conflict_level: str = ""
    sent_at: str = ""
    delivered_at: str = ""
    viewed_at: str = ""
    acked_at: str = ""
    acked_by: str = ""
    ack_result: str = ""
    conditions_confirmed_at: str = ""
    conditions_note: str = ""
    started_at: str = ""
    last_receipt_at: str = ""
    pause_reason: str = ""
    pause_reassess_at: str = ""
    withdraw_reason: str = ""
    closed_at: str = ""
    close_result: str = ""
    close_note: str = ""
    resend_count: int = 0
    overdue_marks: tuple[str, ...] = ()
    receipts: tuple[dict[str, str], ...] = ()
    history: tuple[dict[str, str], ...] = ()

    # Zero means legacy/unknown, not a reconstructed content version.
    content_version: int = 0
    content_history: tuple[dict[str, str], ...] = ()

    def _check_content_version(self, expected_version: int | None) -> None:
        if self.content_version == 0 and expected_version is None:
            return
        if (self.content_version < 1 or type(expected_version) is not int
                or expected_version != self.content_version):
            raise TacticalError('任务内容版本缺失或已过期，请核对当前任务')

    # -- 只读派生 -------------------------------------------------------
    @property
    def stage_word(self) -> str:
        return stage_word(self.stage)

    @property
    def control_word(self) -> str:
        return control_word(self.control_state)

    @property
    def task_word(self) -> str:
        return task_word(self.task_level)

    @property
    def can_start(self) -> bool:
        return bool(self.acked_at) and bool(self.conditions_confirmed_at)

    def timeouts(self, now: str) -> tuple[str, ...]:
        """只产生提醒/升级事项，不自动判定拒收、失败或失联。"""
        out: list[str] = []
        n = _parse_time(now)
        if n is None or not self.sent_at or self.acked_at:
            return ()
        t = _parse_time(self.sent_at)
        if t is None:
            return ()
        waited = (n - t).total_seconds() / 60.0
        first, escalate, reorg = ACK_TIMEOUTS.get(self.task_level, ACK_TIMEOUTS["normal"])
        if waited >= reorg:
            out.append(f"命令 {self.order_id} 已 {waited:.0f} 分钟未被接收，进入重新组织判断（不自动改派）")
        elif waited >= escalate:
            out.append(f"命令 {self.order_id} 已 {waited:.0f} 分钟未被接收，升级责任单位")
        elif waited >= first:
            out.append(f"命令 {self.order_id} 已 {waited:.0f} 分钟未被接收，首次提醒")
        return tuple(out)

    def receipt_gap_ok(self, now: str, *, in_high_risk: bool = False) -> bool | None:
        n, last = _parse_time(now), _parse_time(self.last_receipt_at)
        if n is None or last is None:
            return None
        gap = (n - last).total_seconds() / 60.0
        limit = RECEIPT_WINDOWS.get(self.task_level, RECEIPT_WINDOWS["normal"])
        need = limit[4] if in_high_risk else limit[3]
        return gap <= need

    # -- 转移（全部 fail-closed） ---------------------------------------
    def moved_to(self, stage: str, *, actor: str, at: str, note: str = "") -> "CommandFlow":
        stage = str(stage)
        if self.content_version and stage in {
                CommandStage.ACKED_NOT_STARTED.value, CommandStage.RUNNING.value,
                CommandStage.PARTIAL.value, CommandStage.DONE_PENDING_VERIFY.value,
                CommandStage.CLOSED.value}:
            raise TacticalError('版本化任务须通过接收、执行、回执或核验关闭入口推进')
        if stage not in _STAGE_ORDER:
            raise TacticalError(f"未知命令阶段：{stage}")
        if stage == self.stage:
            raise TacticalError("命令已处于该阶段，重复提交未产生任何变化")
        if stage not in _ALLOWED_NEXT.get(self.stage, set()):
            raise TacticalError(
                f"命令不允许从「{self.stage_word}」直接进入「{stage_word(stage)}」"
            )
        return replace(self, stage=stage,
                       history=self.history + ({"at": at, "by": actor,
                                                "do": f"进入{stage_word(stage)}", "note": note},))

    def record_delivery(self, *, at: str, delivered: str = "", viewed: str = "") -> "CommandFlow":
        kw: dict[str, str] = {}
        if delivered:
            kw["delivered_at"] = delivered
        if viewed:
            kw["viewed_at"] = viewed
        if not kw:
            raise TacticalError("送达记录必须包含送达时间或查看时间：已发送不等于已接收")
        return replace(self, **kw)

    def record_ack(self, *, at: str, by: str, result: str,
                   expected_version: int | None = None) -> "CommandFlow":
        self._check_content_version(expected_version)
        if result not in {e.value for e in ReceiveResult}:
            raise TacticalError(f"未知接收结果：{result}")
        if not by:
            raise TacticalError("接收必须记录接收人")
        if self.stage != CommandStage.CONFIRMED_UNACK.value:
            raise TacticalError("只有「已确认待接收」的命令可以登记接收")
        flow = replace(self, acked_at=at, acked_by=by, ack_result=result,
                       history=self.history + ({"at": at, "by": by,
                                                "do": f"接收：{receive_word(result)}"},))
        if result in {ReceiveResult.ACCEPTED_READY.value, ReceiveResult.ACCEPTED_CONDITIONAL.value}:
            flow = replace(flow, stage=CommandStage.ACKED_NOT_STARTED.value)
        return flow

    def confirm_conditions(self, *, at: str, by: str, note: str) -> "CommandFlow":
        if not note.strip():
            raise TacticalError("执行条件确认必须写明条件内容")
        if not self.acked_at:
            raise TacticalError("命令未被执行单位接收，不能确认开始执行")
        return replace(self, conditions_confirmed_at=at, conditions_note=note,
                       history=self.history + ({"at": at, "by": by, "do": "执行条件确认"},))

    def begin(self, *, at: str, by: str) -> "CommandFlow":
        if self.stage != CommandStage.ACKED_NOT_STARTED.value:
            raise TacticalError("只有「已接收待开始」的命令可以开始执行")
        if not self.conditions_confirmed_at:
            raise TacticalError("执行条件未确认，不能进入执行中")
        return replace(self, stage=CommandStage.RUNNING.value, started_at=at,
                       last_receipt_at=at,
                       history=self.history + ({"at": at, "by": by, "do": "开始执行"},))

    def add_receipt(self, *, kind: str, by: str, at: str, where: str = "",
                    content: str = "", personnel: str = "", risk: str = "",
                    need: str = "", source: str = "", request_id: str = "",
                    expected_version: int | None = None) -> "CommandFlow":
        self._check_content_version(expected_version)
        if kind not in {e.value for e in ReceiptType}:
            raise TacticalError(f"未知回执类型：{kind}")
        if not by or not at:
            raise TacticalError("回执必须包含回报人和时间")
        if not source:
            raise TacticalError("回执缺少来源，不能入账")
        if self.content_version and (not isinstance(request_id, str) or not request_id.strip()):
            raise TacticalError('版本化回执必须携带幂等请求编号')
        for idem in self.receipts:
            if request_id and idem.get("request_id") == request_id:
                if self.content_version:
                    fields = dict(kind=kind, by=by, where=where, content=content,
                                  personnel=personnel, risk=risk, need=need, source=source,
                                  task_version=str(self.content_version))
                    if any(idem.get(key) != value for key, value in fields.items()):
                        raise TacticalError('回执请求编号已被不同内容使用')
                return self                     # 幂等：重复提交不产生重复回执
        if self.content_version and self.stage not in {
                CommandStage.RUNNING.value, CommandStage.PARTIAL.value,
                CommandStage.DONE_PENDING_VERIFY.value}:
            raise TacticalError('当前版本尚未执行或已关闭，回执不能推进状态')
        rec = {
            "kind": kind, "kind_word": receipt_word(kind), "by": by, "at": at,
            "where": where, "content": content, "personnel": personnel, "risk": risk,
            "need": need, "source": source, "request_id": request_id,
        }
        if self.content_version:
            rec.update(task_version=str(self.content_version), event_id=self.event_id,
                       order_id=self.order_id)
        flow = replace(self, receipts=self.receipts + (rec,),
                       last_receipt_at=at,
                       history=self.history + ({"at": at, "by": by, "do": f"回执：{receipt_word(kind)}"},))
        if kind in {ReceiptType.ALL_DONE.value, ReceiptType.STAGE_DONE.value}:
            flow = replace(flow, stage=CommandStage.DONE_PENDING_VERIFY.value)
        elif kind == ReceiptType.PARTIAL_DONE.value:
            flow = replace(flow, stage=CommandStage.PARTIAL.value)
        elif kind in {ReceiptType.EXEC_ABNORMAL.value, ReceiptType.PERSONNEL_SAFETY.value,
                      ReceiptType.COMM_ABNORMAL.value}:
            flow = replace(flow, control_state=CommandControlState.EXEC_ABNORMAL.value)
        return flow

    def pause(self, *, reason: str, reassess_at: str, at: str, by: str) -> "CommandFlow":
        if not reason.strip():
            raise TacticalError("暂停必须填写原因")
        if not reassess_at.strip():
            raise TacticalError("暂停必须填写重新评估时间")
        return replace(
            self, control_state=CommandControlState.PAUSED.value,
            pause_reason=reason, pause_reassess_at=reassess_at,
            history=self.history + ({"at": at, "by": by, "do": "暂停", "note": reason},))

    def resume(self, *, at: str, by: str, note: str = "") -> "CommandFlow":
        if self.control_state is not CommandControlState.PAUSED.value:
            raise TacticalError("命令当前不是暂停状态")
        return replace(self, control_state=CommandControlState.NORMAL.value,
                       pause_reason="", pause_reassess_at="",
                       history=self.history + ({"at": at, "by": by, "do": "恢复执行", "note": note},))

    def request_withdraw(self, *, reason: str, at: str, by: str) -> "CommandFlow":
        if not reason.strip():
            raise TacticalError("撤回必须填写原因")
        return replace(self, control_state=CommandControlState.WITHDRAW_NOTICED.value,
                       withdraw_reason=reason,
                       history=self.history + ({"at": at, "by": by, "do": "撤回通知已发送", "note": reason},))

    def withdraw_ack(self, *, kind: str, by: str, at: str, source: str = "") -> "CommandFlow":
        allowed = {ReceiptType.WITHDRAW_ACK.value, ReceiptType.STOPPED.value,
                   ReceiptType.RETREATING.value, ReceiptType.CANNOT_STOP.value,
                   ReceiptType.PERSONNEL_ABNORMAL.value, ReceiptType.COMM_LOST_PENDING.value}
        if kind not in allowed:
            raise TacticalError("撤回确认只能使用撤回类回执")
        if self.control_state not in {CommandControlState.WITHDRAW_NOTICED.value,
                                      CommandControlState.AWAITING_STOP.value,
                                      CommandControlState.WITHDRAW_RUNNING.value}:
            raise TacticalError("命令未处于撤回流程，不能登记撤回确认")
        if not source:
            raise TacticalError("撤回回执缺少来源，不能入账")
        nxt = {
            ReceiptType.WITHDRAW_ACK.value: CommandControlState.AWAITING_STOP.value,
            ReceiptType.STOPPED.value: CommandControlState.WITHDRAW_RUNNING.value,
            ReceiptType.RETREATING.value: CommandControlState.WITHDRAW_DONE.value,
            ReceiptType.CANNOT_STOP.value: CommandControlState.EXEC_ABNORMAL.value,
            ReceiptType.PERSONNEL_ABNORMAL.value: CommandControlState.EXEC_ABNORMAL.value,
            ReceiptType.COMM_LOST_PENDING.value: CommandControlState.AWAITING_STOP.value,
        }[kind]
        flow = replace(self, control_state=nxt,
                       receipts=self.receipts + ({"kind": kind, "kind_word": receipt_word(kind),
                                                  "by": by, "at": at, "source": source,
                                                  "withdraw": "1"},),
                       history=self.history + ({"at": at, "by": by, "do": f"撤回回执：{receipt_word(kind)}"},))
        return flow

    def resend(self, *, at: str, by: str, changed: bool = False) -> "CommandFlow":
        if changed:
            raise TacticalError("命令内容、路线、执行单位、任务等级或依据版本已变化，必须建立修订版")
        return replace(self, version=self.version + 1, resend_count=self.resend_count + 1,
                       sent_at=at,
                       history=self.history + ({"at": at, "by": by, "do": "重新发送（内容与版本不变）"},))

    def revise(self, *, at: str, by: str, note: str,
               expected_version: int | None = None, **changes) -> "CommandFlow":
        self._check_content_version(expected_version)
        if not note.strip():
            raise TacticalError("修订命令必须写明修订理由")
        content_fields = {'task_level', 'base_version', 'target', 'unit', 'route_id',
                          'start_time', 'deadline', 'safety_cond', 'stop_cond',
                          'receipt_need', 'conflict_level'}
        if set(changes) - content_fields:
            raise TacticalError('修订只允许任务内容字段，不能修改身份、回执或状态')
        if self.content_version and self.stage == CommandStage.CLOSED.value:
            raise TacticalError('已关闭任务不能直接修订')
        if not any(getattr(self, key) != value for key, value in changes.items()):
            return self
        snapshots = self.content_history
        if self.content_version:
            snapshots += ({**{key: str(getattr(self, key)) for key in content_fields},
                           'event_id': self.event_id, 'order_id': self.order_id,
                           'task_version': str(self.content_version),
                           'revised_at': at, 'revised_by': by, 'reason': note},)
        fresh = replace(self, version=self.version + 1, acked_at="", acked_by="",
                        ack_result="", conditions_confirmed_at="", conditions_note="",
                        started_at="", last_receipt_at="", sent_at="",
                        delivered_at="", viewed_at="",
                        content_version=self.content_version + 1 if self.content_version else 0,
                        content_history=snapshots,
                        stage=CommandStage.PENDING_CONFIRM.value,
                        control_state=CommandControlState.NORMAL.value,
                        history=self.history + ({"at": at, "by": by,
                                                  "do": (f"修订内容 v{self.content_version + 1}" if self.content_version
                                                         else f"修订 v{self.version + 1}"),
                                                  "note": note},))
        return replace(fresh, **changes)

    def close(self, *, result: str, at: str, by: str, facts: str = "", handling: str = "",
              outcome: str = "", note: str = "",
              expected_version: int | None = None) -> "CommandFlow":
        self._check_content_version(expected_version)
        if result not in _CLOSE_RESULTS:
            raise TacticalError(f"未知关闭结果：{result}")
        if self.stage not in {CommandStage.DONE_PENDING_VERIFY.value, CommandStage.PARTIAL.value,
                              CommandStage.RUNNING.value}:
            raise TacticalError("命令尚未回执完成，不能关闭")
        if result in {"completed", "partial"}:
            done = [r for r in self.receipts
                    if r.get("kind") in {ReceiptType.ALL_DONE.value, ReceiptType.PARTIAL_DONE.value,
                                          ReceiptType.STAGE_DONE.value}
                    and (not self.content_version or (
                        r.get('task_version') == str(self.content_version)
                        and r.get('event_id') == self.event_id
                        and r.get('order_id') == self.order_id))]
            if not done:
                raise TacticalError("没有完成或部分完成回执，不能正常关闭")
            if not self.history:
                raise TacticalError("缺少核验或人员状态确认记录，不能正常关闭")
        if result in {"abnormal_closed", "unverifiable"}:
            if not (facts and handling and outcome):
                raise TacticalError("异常关闭必须同时具备事实、处置、结果三部分记录")
        if result == "completed" and self.control_state not in {
                CommandControlState.NORMAL.value, CommandControlState.WITHDRAW_DONE.value,
                CommandControlState.ABNORMAL_CLOSED.value}:
            raise TacticalError("存在未处理异常或撤回流程，不能按已完成关闭")
        return replace(self, stage=CommandStage.CLOSED.value, closed_at=at,
                       close_result=result, close_note=note,
                       control_state=(CommandControlState.ABNORMAL_CLOSED.value
                                      if result == "abnormal_closed" else self.control_state),
                       history=self.history + ({"at": at, "by": by,
                                                "do": f"关闭：{_CLOSE_RESULTS[result]}"},))


def close_results() -> dict[str, str]:
    return dict(_CLOSE_RESULTS)
