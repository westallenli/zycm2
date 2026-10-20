"""One event's fixed organization view and person-level group changes.

Fixed squad membership and captain-reported attendance remain source facts.
The transfer log changes only this event's displayed group assignment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .authorization import Action
from .captain_arrivals import loaded_rosters
from .fire_events import ONBOARDABLE_STATUSES, utc_now_iso
from .org_model import SLOT_INDEX
from .temporary_personnel import temporary_state

TRANSFERRED = "PersonGroupTransferred"


@dataclass(frozen=True)
class GroupPerson:
    phone: str
    name: str
    unit: str
    origin: str  # temporary | fixed_squad | legacy_binding
    source_id: str
    pos_code: str
    arrived: bool
    presence_basis: str
    transfer_count: int = 0


def group_people(workspace, event_id: str, *, ledger) -> dict[str, GroupPerson]:
    """Project confirmed event sources without guessing missing squad groups."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("组织人员记录与事件审计链不匹配。")
    people: dict[str, GroupPerson] = {}
    for person in temporary_state(workspace, event_id).values():
        people[person.phone] = GroupPerson(
            person.phone, person.name, person.unit, "temporary", "",
            person.pos_code, bool(person.arrived_at),
            "本人申报到场" if person.arrived_at else "尚未申报到场",
        )
    loaded = loaded_rosters(workspace, event_id)
    snapshot = ledger.snapshots.get(event_id)
    for squad_id, roster in loaded.items():
        squad_snapshot = snapshot.squads.get(squad_id) if snapshot else None
        arrived_phones = set(squad_snapshot.arrived_phones) if squad_snapshot else set()
        if arrived_phones - set(roster.phones):
            raise ValueError("固定中队到场名单与冻结名册冲突。")
        for member in roster.members:
            phone = member["phone"]
            if phone in people:
                raise ValueError("同一人员同时出现在临时区或多个固定中队。")
            code = member.get("pos_code", "")
            if code not in ("", *SLOT_INDEX):
                raise ValueError("固定中队冻结名册含未知组别。")
            people[phone] = GroupPerson(
                phone, member["name"], member["unit"], "fixed_squad", squad_id,
                code, phone in arrived_phones,
                "队长申报到场" if phone in arrived_phones else "尚未由队长申报到场",
            )
    legacy: dict[str, object] = {}
    for binding in workspace.roster_bindings.values():
        phone = binding.person.phone
        if phone in people:
            # Older individual onboarding is not allowed to silently overwrite
            # a frozen captain roster or a temporary registration.
            raise ValueError("旧个人入岗与临时区或固定中队名册重叠，需先核对迁移。")
        previous = legacy.get(phone)
        if previous is not None and previous.status == "active" and binding.status == "active":
            raise ValueError("同一人员存在多个旧活动入岗记录。")
        if (previous is None or binding.status == "active"
                or previous.status != "active"
                and binding.bound_at_version >= previous.bound_at_version):
            legacy[phone] = binding
    for phone, binding in legacy.items():
        if binding.pos_code not in SLOT_INDEX:
            raise ValueError("旧个人入岗记录含未知组别。")
        active = binding.status == "active"
        people[phone] = GroupPerson(
            phone, binding.person.name, binding.person.unit, "legacy_binding",
            binding.id, binding.pos_code, active,
            "旧个人刷码入岗" if active else "已办理离岗",
        )
    requests: set[str] = set()
    for event in workspace.events:
        if event.kind != TRANSFERRED:
            continue
        data = event.payload
        phone = data.get("phone")
        person = people.get(phone)
        request_id = data.get("request_id")
        if (data.get("event_id") != event_id or person is None
                or not isinstance(request_id, str) or not request_id
                or request_id in requests or data.get("origin") != person.origin
                or data.get("source_id") != person.source_id
                or data.get("from_pos_code") != person.pos_code
                or data.get("to_pos_code") not in SLOT_INDEX
                or data["to_pos_code"] == person.pos_code
                or not isinstance(data.get("actor"), str) or not data["actor"]
                or not isinstance(data.get("transferred_at"), str)
                or not data["transferred_at"]):
            raise ValueError("按人调组审计记录不合法。")
        requests.add(request_id)
        people[phone] = replace(
            person, pos_code=data["to_pos_code"],
            transfer_count=person.transfer_count + 1,
        )
    return people


def transfer(workspace, *, token, event_id: str, phone: str,
             to_pos_code: str, request_id: str, registry, ledger,
             authorization) -> dict:
    actor = authorization.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件未启动或已关闭，不能按人调组。")
    if to_pos_code not in SLOT_INDEX:
        raise ValueError("目标组别必须来自现有组织架构。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("调组请求编号不能为空。")
    prior = next((event.payload for event in workspace.events
                  if event.kind == TRANSFERRED
                  and event.payload.get("event_id") == event_id
                  and event.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("phone") != phone
                or prior.get("to_pos_code") != to_pos_code):
            raise ValueError("调组请求编号已用于不同内容。")
        return prior
    person = group_people(workspace, event_id, ledger=ledger).get(phone)
    if person is None:
        raise ValueError("所选人员未进入本事件临时区或固定中队名册。")
    if person.origin == "temporary" and not person.pos_code:
        raise ValueError("临时人员须先完成初次编组，再使用按人调组入口。")
    if person.pos_code == to_pos_code:
        raise ValueError("目标组与当前组相同，无需调组。")
    payload = dict(
        event_id=event_id, phone=phone, origin=person.origin,
        source_id=person.source_id, from_pos_code=person.pos_code,
        to_pos_code=to_pos_code, actor=actor, request_id=request_id,
        transferred_at=utc_now_iso(), data_kind="reported",
        delivery="in_app_available",
    )
    workspace.append_event(TRANSFERRED, **payload)
    return payload
