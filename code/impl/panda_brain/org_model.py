"""Force-organization model for the front-command workbench (P15 T1).

Two-tier roster discipline mirrors field practice:

- 常备人员库 (standing person library): long-lived registry keyed by phone,
  imported from the customer's existing roster.
- 火情花名册 (task roster): who occupies which position slot of THIS fire,
  created by QR scan check-in and consumed by command tools.

The built-in template reproduces 《陕西省森林草原火灾扑救前线指挥部组织架构图》
so doctrine, the workbench org view and the roster share one structural truth.
The chart carries the 29 line groups counted in the phase-2 PRD plus the three
news-publicity subgroups shown under the spokesperson node; all are encoded.

Posture rules are advisory staff checks (参谋提示), never power gates:
- 动态补充预备队规则: reserve headcount >= ceil(30% of committed combat headcount).
- 力量调用配比: auxiliary support >= 3x committed combat (专业:辅助 = 1:3).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .models import RosterBinding

RESERVE_RATIO_FLOOR = 0.3
PROFESSIONAL_AUX_TARGET = 3.0


@dataclass(frozen=True)
class CommandNode:
    node_code: str
    name: str
    parent: str | None


@dataclass(frozen=True)
class PositionSlot:
    pos_code: str
    name: str
    domain: str  # coordination | news | tech | ops | security | logistics
    kind: str    # assault | air | reserve | professional | security | logistics
    max_headcount: int


COMMAND_NODES: tuple[CommandNode, ...] = (
    CommandNode("joint", "联合指挥部/工作组", None),
    CommandNode("general", "前线总指挥（属地行政首长、总负责）", "joint"),
    CommandNode("dispatch-chief", "调度长（综合协调）", "general"),
    CommandNode("spokesperson", "新闻发言人", "general"),
    CommandNode("dep-tech", "副总指挥·专业技术支持", "general"),
    CommandNode("dep-ops", "副总指挥·灭火指挥调度", "general"),
    CommandNode("dep-security", "副总指挥·火场维稳管控", "general"),
    CommandNode("dep-logistics", "副总指挥·后勤综合保障", "general"),
)

POSITION_SLOTS: tuple[PositionSlot, ...] = (
    # 调度长——综合协调
    PositionSlot("xz-coord", "综合协调组", "coordination", "logistics", 6),
    # 新闻发言人
    PositionSlot("news-publish", "宣发材料组", "news", "logistics", 4),
    PositionSlot("news-media", "媒体服务组", "news", "logistics", 4),
    PositionSlot("news-sentiment", "舆情研判组", "news", "logistics", 4),
    # 副总指挥·专业技术支持
    PositionSlot("tech-expert", "专家支持组", "tech", "professional", 6),
    PositionSlot("tech-resource", "资源调配组", "tech", "professional", 5),
    PositionSlot("tech-info", "信息收集组", "tech", "professional", 5),
    PositionSlot("tech-weather", "气象支持组", "tech", "professional", 4),
    PositionSlot("tech-plan", "方案编制组", "tech", "professional", 5),
    PositionSlot("tech-assess", "灾情评估组", "tech", "professional", 4),
    PositionSlot("tech-monitor", "火情监测组", "tech", "professional", 5),
    PositionSlot("tech-record", "行动记录组", "tech", "professional", 4),
    # 副总指挥·灭火指挥调度
    PositionSlot("pjjz-1", "扑救一组（一分区）", "ops", "assault", 12),
    PositionSlot("pjjz-2", "扑救二组（二分区）", "ops", "assault", 12),
    PositionSlot("pjjz-3", "扑救三组（三分区）", "ops", "assault", 12),
    PositionSlot("pjjz-4", "扑救四组（四分区）", "ops", "assault", 12),
    PositionSlot("air-op", "空中作业组", "ops", "air", 8),
    PositionSlot("mobile-support", "机动支援组", "ops", "reserve", 15),
    PositionSlot("guide", "向导组", "ops", "professional", 4),
    PositionSlot("supervisor", "扑救督导组", "ops", "professional", 4),
    # 副总指挥·火场维稳管控
    PositionSlot("sec-case", "火案侦查组", "security", "security", 5),
    PositionSlot("sec-order", "秩序维护组", "security", "security", 6),
    PositionSlot("sec-guard", "现场警戒组", "security", "security", 6),
    PositionSlot("sec-traffic", "交通保障组", "security", "security", 5),
    # 副总指挥·后勤综合保障
    PositionSlot("log-water", "供水组", "logistics", "logistics", 5),
    PositionSlot("log-power", "供电组", "logistics", "logistics", 5),
    PositionSlot("log-comm", "通讯保障组", "logistics", "logistics", 6),
    PositionSlot("log-material", "物资供应组", "logistics", "logistics", 6),
    PositionSlot("log-medical", "医疗组", "logistics", "logistics", 6),
    PositionSlot("log-stay", "食宿组", "logistics", "logistics", 5),
    PositionSlot("log-public", "群众生活组", "logistics", "logistics", 5),
    PositionSlot("log-site", "场地组", "logistics", "logistics", 4),
)

SLOT_INDEX: dict[str, PositionSlot] = {slot.pos_code: slot for slot in POSITION_SLOTS}
NODE_INDEX: dict[str, CommandNode] = {node.node_code: node for node in COMMAND_NODES}

_DOMAIN_PARENT: dict[str, str] = {
    "coordination": "dispatch-chief",
    "news": "spokesperson",
    "tech": "dep-tech",
    "ops": "dep-ops",
    "security": "dep-security",
    "logistics": "dep-logistics",
}

_COMBAT_KINDS = frozenset({"assault", "air"})
_RESERVE_KINDS = frozenset({"reserve"})


def normalize_phone(raw: str) -> str:
    """Strip separators; keep digits only after an optional +86 prefix."""
    phone = (raw or "").replace("-", "").replace(" ", "").strip()
    if phone.startswith("+86"):
        phone = phone[3:]
    if not phone.isdigit() or not 6 <= len(phone) <= 15:
        raise ValueError(f"手机号格式不合法：{raw!r}")
    return phone


def mask_phone(phone: str) -> str:
    if len(phone) < 7:
        return "***"
    return f"{phone[:3]}****{phone[-4:]}"


def slot_domain_parent(pos_code: str) -> str:
    slot = SLOT_INDEX.get(pos_code)
    if slot is None:
        raise KeyError(f"未知岗位槽位：{pos_code}")
    return _DOMAIN_PARENT[slot.domain]


def template_summary() -> dict[str, int]:
    """Structural counts of the built-in doctrine template."""
    domains = Counter(slot.domain for slot in POSITION_SLOTS)
    return {"nodes": len(COMMAND_NODES), "slots": len(POSITION_SLOTS), **dict(domains)}


def force_posture(active_bindings: tuple[RosterBinding, ...]) -> dict[str, object]:
    """Advisory force-posture report over currently active bindings.

    Counts persons, not slots, because every member checks in individually.
    """
    committed = reserve = auxiliary = 0
    for binding in active_bindings:
        slot = SLOT_INDEX.get(binding.pos_code)
        if slot is None:
            continue
        if slot.kind in _COMBAT_KINDS:
            committed += 1
        elif slot.kind in _RESERVE_KINDS:
            reserve += 1
        else:
            auxiliary += 1
    required_reserve = math.ceil(committed * RESERVE_RATIO_FLOOR) if committed else 0
    aux_ratio_ok = (auxiliary / committed) >= PROFESSIONAL_AUX_TARGET if committed else True
    reserve_ratio_ok = not committed or reserve >= required_reserve
    notes: list[str] = []
    if committed and not reserve_ratio_ok:
        notes.append(
            f"预备队不足：一线投入 {committed} 人，机动支援组在位 {reserve} 人，"
            f"按'每投入100人预备≥30人'规则至少应备 {required_reserve} 人。"
        )
    if committed and not aux_ratio_ok:
        notes.append(
            f"辅助力量配比偏低：作战 {committed} 人对辅助 {auxiliary} 人，"
            "条令建议专业与辅助按 1:3 配置。"
        )
    if not notes:
        notes.append("力量格局符合条令参考线。" if committed else "尚无人员到岗。")
    return {
        "committed": committed,
        "reserve": reserve,
        "auxiliary": auxiliary,
        "required_reserve_min": required_reserve,
        "reserve_ratio_ok": reserve_ratio_ok,
        "aux_ratio_ok": aux_ratio_ok,
        "notes": tuple(notes),
    }
