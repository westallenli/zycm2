"""P16-T19 到场人员八态状态机（定版 04 号 §4.1）。

状态与色板严格取自定版：
    预编入 #647382 → 待到场 → 已到场 #2F7D68 → 执行中 #245B78
    → 已撤离 → 已归建；旁挂 暂离/轮休 #B77A1A · 失联 #B84A42（可恢复）。

推导纪律（不伪造）：
- 派生态只从链上事实读出——预编入/待到场看注册表名册是否已指派岗位，
  已到场/已撤离看任务空间签到绑定（RosterBinding）；
- 人工判定态（执行中/暂离轮休/失联/归建）由指挥员显式标记，逐条入哈希链
  （PersonStateMarked），「执行中」必须引用一条真实存在且处于
  confirmed/active/paused 的执行命令，拒绝挂空号；
- 失联/通信中断的自动判定待通信系统接入，当前仅指挥员人工判定口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PersonRecord, OrderStatus
from .org_model import SLOT_INDEX

STATE_COLORS: dict[str, str] = {
    "预编入": "#647382",
    "待到场": "#647382",
    "已到场": "#2F7D68",
    "执行中": "#245B78",
    "暂离/轮休": "#B77A1A",
    "已撤离": "#647382",
    "已归建": "#2F7D68",
    "失联": "#B84A42",
}

#: 允许作为「执行中」依据的命令状态（终态订单不得再支撑执行中）。
_EXECUTABLE_ORDER_STATUSES = frozenset(
    {OrderStatus.CONFIRMED, OrderStatus.ACTIVE, OrderStatus.PAUSED}
)

MARK_KIND = "PersonStateMarked"


@dataclass(frozen=True)
class PersonState:
    """一名人员在本事件内的八态结论及其依据。"""

    phone: str
    name: str
    unit: str
    pos_code: str            # 名册指派或绑定槽位；可为空
    slot_name: str           # 岗位中文名（可解析时）
    state: str               # STATE_COLORS 键
    basis: str               # 人话依据（哪个事实/哪条标记）


def mark_person_state(
    workspace,
    action: str,
    *,
    phone: str,
    operator: str,
    order_id: str = "",
    note: str = "",
) -> None:
    """指挥员人工判定：把一次状态标记写进本事件哈希链。

    action ∈ executing / standby / resume_duty / lost_contact /
             contact_restored / returned_to_unit
    fail-closed：未知动作、执行中挂不存在或终态命令——一律 ValueError，
    不写半截事实。（名册归属校验在 panel 层完成，那里同时持有注册表。）
    """
    if action not in {
        "executing", "standby", "resume_duty",
        "lost_contact", "contact_restored", "returned_to_unit",
    }:
        raise ValueError(f"未知的人员状态动作：{action}")
    if action == "executing":
        order = workspace.orders.get(order_id)
        if order is None:
            raise ValueError(f"引用的执行命令不存在：{order_id}")
        if order.status not in _EXECUTABLE_ORDER_STATUSES:
            raise ValueError(
                f"命令 {order_id} 当前状态 {order.status.value} 不能支撑「执行中」。")
    from .fire_events import utc_now_iso

    payload: dict = {
        "action": action,
        "phone": phone,
        "operator": operator,
        "at": utc_now_iso(),
    }
    if order_id:
        payload["order_id"] = order_id
    if note:
        payload["note"] = note
    workspace.append_event(MARK_KIND, **payload)


def derive_personnel_states(workspace, roster_entries=()) -> list[PersonState]:
    """八态推导：名册 ∪ 签到绑定的全体人员，逐人给出状态与依据。

    roster_entries: RosterEntry 可迭代对象 (registry.rosters[event_id].entries.values())，
    用于 预编入/待到场 与姓名单位补全。派生纯只读，人工标记从链上事件回放。
    """
    entries_by_phone = {}
    for entry in roster_entries or ():
        entries_by_phone[getattr(entry, "phone", "")] = entry
    bindings_by_phone: dict[str, object] = {}
    for binding in workspace.roster_bindings.values():
        bindings_by_phone[binding.person.phone] = binding

    # -- 回放人工标记时间线 ---------------------------------------------------
    overrides: dict[str, dict] = {}   # phone -> {"state":..., "basis":..., "order_id":...}
    for event in getattr(workspace, "events", ()):
        kind = getattr(event, "kind", "")
        if kind == "PersonCheckedOut":
            # 离岗是链上事实：清除执行中/暂离/失联等旁挂，回落已撤离
            data = getattr(event, "payload", {}) or {}
            cur = overrides.get(data.get("phone", ""), {})
            if cur.get("state") in {"执行中", "暂离/轮休", "失联"}:
                cur.pop("state", None)
                cur.pop("basis", None)
                if cur:
                    overrides[data.get("phone", "")] = cur
            continue
        if kind != MARK_KIND:
            continue
        data = getattr(event, "payload", {}) or {}
        phone = data.get("phone", "")
        action = data.get("action", "")
        cur = overrides.get(phone, {})
        if action == "executing":
            order = workspace.orders.get(data.get("order_id", ""))
            if order is None or order.status not in _EXECUTABLE_ORDER_STATUSES:
                continue   # 命令已被撤/完结 → 执行中依据消失，回落派生态
            cur["state"] = "执行中"
            cur["basis"] = f"指挥员标记执行命令 {data.get('order_id', '')}"
        elif action == "standby":
            cur["state"] = "暂离/轮休"
            cur["basis"] = "指挥员标记暂离/轮休"
        elif action == "resume_duty":
            for k in ("执行中", "暂离/轮休"):
                if cur.get("state") == k:
                    cur.pop("state", None)
                    cur.pop("basis", None)
        elif action == "lost_contact":
            cur["state"] = "失联"
            cur["basis"] = "指挥员标记通信失联"
        elif action == "contact_restored":
            if cur.get("state") == "失联":
                cur.pop("state", None)
                cur.pop("basis", None)
        elif action == "returned_to_unit":
            cur["state"] = "已归建"
            cur["basis"] = "指挥员标记已归建"
        if cur:
            overrides[phone] = cur

    rows: list[PersonState] = []
    phones = sorted(set(entries_by_phone) | set(bindings_by_phone))
    for phone in phones:
        entry = entries_by_phone.get(phone)
        binding = bindings_by_phone.get(phone)
        person: PersonRecord | None = getattr(binding, "person", None)
        name = (person.name if person else "") or getattr(entry, "name", "") or ""
        unit = (person.unit if person else "") or getattr(entry, "unit", "") or ""
        if name and not unit:
            lib = workspace.person_library.get(phone)
            if lib is not None:
                name, unit = lib.name, lib.unit
        pos_code = ""
        if entry is not None and getattr(entry, "pos_code", ""):
            pos_code = entry.pos_code
        if binding is not None and binding.status == "active":
            pos_code = binding.pos_code
        slot_name = SLOT_INDEX[pos_code].name if pos_code in SLOT_INDEX else ""

        # 派生基态（链上事实）
        if binding is not None and binding.status == "checked_out":
            base, basis = "已撤离", "已办理离岗（PersonCheckedOut）"
        elif binding is not None and binding.status == "active":
            base, basis = "已到场", f"刷码入岗 {pos_code or ''}".strip()
        elif pos_code:
            base, basis = "待到场", "名册已指派岗位，尚未刷码"
        else:
            base, basis = "预编入", "已在事件名册，岗位未指派"

        ov = overrides.get(phone) or {}
        state = ov.get("state") or base
        if ov.get("state"):
            basis = ov.get("basis", "")
        rows.append(PersonState(
            phone=phone, name=name or "—", unit=unit,
            pos_code=pos_code, slot_name=slot_name,
            state=state, basis=basis,
        ))
    return rows
