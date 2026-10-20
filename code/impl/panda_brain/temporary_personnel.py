"""Event-scoped temporary personnel, separate from fixed squad rosters.

Registration, presence and assignment are distinct audit facts. None of them
changes a fixed squad arrival, material carry list or deployable headcount.
"""

from __future__ import annotations

from dataclasses import dataclass

from .authorization import Action
from .fire_events import EventRegistry, ONBOARDABLE_STATUSES, utc_now_iso
from .org_model import SLOT_INDEX

REGISTERED = "TemporaryPersonRegistered"
ARRIVED = "TemporaryPersonArrived"
ASSIGNED = "TemporaryPersonnelAssigned"


@dataclass(frozen=True)
class TemporaryPerson:
    phone: str
    name: str
    unit: str
    registered_at: str
    arrived_at: str = ""
    pos_code: str = ""
    assigned_at: str = ""


def temporary_state(workspace, event_id: str) -> dict[str, TemporaryPerson]:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("临时人员记录与事件审计链不匹配。")
    people: dict[str, TemporaryPerson] = {}
    for event in workspace.events:
        if event.kind not in {REGISTERED, ARRIVED, ASSIGNED}:
            continue
        data = event.payload
        if data.get("event_id") != event_id:
            raise ValueError("临时人员记录归属冲突。")
        if event.kind == REGISTERED:
            phone = data.get("phone")
            if (not isinstance(phone, str) or not phone
                    or phone in people or not isinstance(data.get("name"), str)
                    or not data["name"] or not isinstance(data.get("unit"), str)
                    or not isinstance(data.get("registered_at"), str)
                    or not data["registered_at"]):
                raise ValueError("临时人员登记记录不合法。")
            people[phone] = TemporaryPerson(
                phone, data["name"], data["unit"], data["registered_at"]
            )
        elif event.kind == ARRIVED:
            phone = data.get("phone")
            person = people.get(phone)
            if (person is None or person.arrived_at
                    or not isinstance(data.get("arrived_at"), str)
                    or not data["arrived_at"] or data.get("actor") != phone):
                raise ValueError("临时人员到场记录不合法。")
            people[phone] = TemporaryPerson(
                person.phone, person.name, person.unit, person.registered_at,
                data["arrived_at"], person.pos_code, person.assigned_at,
            )
        else:
            pos_code = data.get("pos_code")
            phones = data.get("phones")
            if (pos_code not in SLOT_INDEX or not isinstance(phones, list)
                    or not phones or len(phones) != len(set(phones))
                    or not isinstance(data.get("actor"), str)
                    or not data["actor"]
                    or not isinstance(data.get("assigned_at"), str)
                    or not data["assigned_at"]):
                raise ValueError("临时人员编组记录不合法。")
            for phone in phones:
                person = people.get(phone)
                if person is None or person.pos_code:
                    raise ValueError("临时人员重复编组或未登记。")
                people[phone] = TemporaryPerson(
                    person.phone, person.name, person.unit, person.registered_at,
                    person.arrived_at, pos_code, data["assigned_at"],
                )
    return people


def _open_event(registry, event_id: str) -> None:
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件未启动或已关闭，不能登记或编组临时人员。")


def register(workspace, *, token, event_id: str, qr_id: str,
             registry, ledger, resolve_subject) -> TemporaryPerson:
    phone = resolve_subject(token)
    _open_event(registry, event_id)
    qr = registry.qrs.get((qr_id or "").strip())
    if (qr is None or qr.event_id != event_id
            or EventRegistry.qr_status(qr) != "有效"):
        raise ValueError("二维码无效、已过期、已撤销或不属于本事件。")
    if any(phone in squad.member_phones for squad in ledger.squads.values()):
        raise ValueError("固定中队成员须走队长名册流程，不能登记为临时人员。")
    roster = registry.rosters.get(event_id)
    if roster is not None and any(entry.phone == phone for entry in roster.entries.values()):
        raise ValueError("已有本事件固定岗位名册记录，不能重复登记为临时人员。")
    people = temporary_state(workspace, event_id)
    if phone in people:
        return people[phone]
    person = registry.library[phone]
    workspace.append_event(
        REGISTERED, event_id=event_id, phone=phone, name=person.name,
        unit=person.unit, qr_id=qr.qr_id, registered_at=utc_now_iso(),
        data_kind="reported",
    )
    return temporary_state(workspace, event_id)[phone]


def report_arrival(workspace, *, token, event_id: str, registry,
                   resolve_subject) -> TemporaryPerson:
    phone = resolve_subject(token)
    _open_event(registry, event_id)
    person = temporary_state(workspace, event_id).get(phone)
    if person is None:
        raise ValueError("请先本人扫码登记进入本事件临时人员区。")
    if person.arrived_at:
        return person
    workspace.append_event(
        ARRIVED, event_id=event_id, phone=phone, actor=phone,
        arrived_at=utc_now_iso(), data_kind="reported",
    )
    return temporary_state(workspace, event_id)[phone]


def assign(workspace, *, token, event_id: str, phones: list[str],
           pos_code: str, request_id: str, registry, authorization) -> dict:
    actor = authorization.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
    _open_event(registry, event_id)
    if pos_code not in SLOT_INDEX:
        raise ValueError("只能编入现有组织架构中的组。")
    if not request_id or request_id != request_id.strip():
        raise ValueError("编组请求编号不能为空。")
    submitted = list(phones or [])
    if not submitted or len(submitted) != len(set(submitted)):
        raise ValueError("请选择一个或多个不重复的临时人员。")
    prior = next((e.payload for e in workspace.events
                  if e.kind == ASSIGNED and e.payload.get("event_id") == event_id
                  and e.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("pos_code") != pos_code
                or prior.get("phones") != submitted):
            raise ValueError("编组请求编号已用于不同内容。")
        return prior
    people = temporary_state(workspace, event_id)
    if any(phone not in people or people[phone].pos_code for phone in submitted):
        raise ValueError("所选人员未登记或已经编组；本入口仅用于初次编组。")
    payload = dict(
        event_id=event_id, phones=submitted, pos_code=pos_code,
        actor=actor, request_id=request_id, assigned_at=utc_now_iso(),
        data_kind="reported", delivery="in_app_available",
    )
    workspace.append_event(ASSIGNED, **payload)
    return payload
