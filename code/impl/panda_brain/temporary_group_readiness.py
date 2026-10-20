"""Explicit group-leader authority and reported readiness for temporary staff.

Assignment and self-reported arrival never grant leadership or availability.
All authority is scoped to one event and one existing organization slot.
"""

from __future__ import annotations

from dataclasses import dataclass

from .authorization import Action, AuthorizationPolicy, Role, RoleGrant
from .event_authorization import event_grants
from .fire_events import ONBOARDABLE_STATUSES, utc_now_iso
from .org_model import SLOT_INDEX
from .personnel_groups import TRANSFERRED, group_people
from .squad_readiness import ACTIVE_ORDER_STATUSES, STATUSES
from .temporary_personnel import temporary_state


LEADER_ASSIGNED = "TemporaryGroupLeaderAssigned"
REPORTED = "TemporaryGroupReadinessReported"


@dataclass(frozen=True)
class GroupReadiness:
    phone: str
    pos_code: str
    status: str
    basis: str
    actor: str
    reported_at: str
    order_id: str = ""


def _leaders(workspace, event_id: str) -> dict[str, str]:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("临时组长授权与事件审计链不匹配。")
    deputy = next((g.subject_id for g in event_grants(workspace)
                   if g.role == Role.DEPUTY and g.event_id == event_id), "")
    leaders: dict[str, str] = {}
    requests: set[str] = set()
    for event in workspace.events:
        if event.kind == TRANSFERRED:
            data = event.payload
            code = data.get("from_pos_code")
            if leaders.get(code) == data.get("phone"):
                leaders.pop(code)
            continue
        if event.kind != LEADER_ASSIGNED:
            continue
        data = event.payload
        phone = data.get("phone")
        group = data.get("pos_code")
        request_id = data.get("request_id")
        if (data.get("event_id") != event_id or group not in SLOT_INDEX
                or not isinstance(phone, str) or not phone
                or not deputy or data.get("actor") != deputy
                or data.get("previous_phone", "") != leaders.get(group, "")
                or not isinstance(data.get("evidence"), str)
                or not data["evidence"].strip()
                or not isinstance(data.get("assigned_at"), str)
                or not data["assigned_at"]
                or not isinstance(request_id, str) or not request_id
                or request_id in requests or data.get("data_kind") != "reported"):
            raise ValueError("临时组长指定记录不合法。")
        requests.add(request_id)
        leaders[group] = phone
    return leaders


def leader_state(workspace, event_id: str, *, ledger) -> dict[str, str]:
    """Only a currently arrived temporary member in that group retains authority."""
    leaders = _leaders(workspace, event_id)
    people = group_people(workspace, event_id, ledger=ledger)
    return {code: phone for code, phone in leaders.items()
            if phone in people and people[phone].origin == "temporary"
            and people[phone].pos_code == code and people[phone].arrived}


def appoint(workspace, *, token, event_id: str, pos_code: str, phone: str,
            evidence: str, request_id: str, registry, ledger,
            authorization) -> dict:
    actor = authorization.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件已不接受组长指定。")
    if pos_code not in SLOT_INDEX:
        raise ValueError("组别须来自现有固定组织架构。")
    if not isinstance(evidence, str) or not evidence.strip() or evidence != evidence.strip():
        raise ValueError("组长指定须填写线下依据。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("组长指定请求编号不能为空。")
    prior = next((e.payload for e in workspace.events if e.kind == LEADER_ASSIGNED
                  and e.payload.get("event_id") == event_id
                  and e.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("phone") != phone
                or prior.get("pos_code") != pos_code
                or prior.get("evidence") != evidence):
            raise ValueError("组长指定请求编号已用于不同内容。")
        return prior
    person = group_people(workspace, event_id, ledger=ledger).get(phone)
    if (person is None or person.origin != "temporary"
            or person.pos_code != pos_code or not person.arrived):
        raise ValueError("组长须是本事件该组已申报到场的临时人员。")
    previous = _leaders(workspace, event_id).get(pos_code, "")
    if previous == phone:
        raise ValueError("此人已被指定为该组组长，无需重复指定。")
    payload = dict(
        event_id=event_id, pos_code=pos_code, phone=phone,
        previous_phone=previous, evidence=evidence, actor=actor,
        assigned_at=utc_now_iso(), request_id=request_id, data_kind="reported",
    )
    workspace.append_event(LEADER_ASSIGNED, **payload)
    return payload


def readiness_state(workspace, event_id: str, *, ledger) -> dict[str, GroupReadiness]:
    _leaders(workspace, event_id)
    people = temporary_state(workspace, event_id)
    group = group_people(workspace, event_id, ledger=ledger)
    leaders: dict[str, str] = {}
    current: dict[str, GroupReadiness] = {}
    requests: set[str] = set()
    for event in workspace.events:
        data = event.payload
        if event.kind == TRANSFERRED:
            code = data.get("from_pos_code")
            phone = data.get("phone")
            if leaders.get(code) == phone:
                leaders.pop(code)
            current.pop(phone, None)
            continue
        if event.kind == LEADER_ASSIGNED:
            leaders[data["pos_code"]] = data["phone"]
            continue
        if event.kind != REPORTED:
            continue
        pos_code = data.get("pos_code")
        phones = data.get("phones")
        request_id = data.get("request_id")
        order_id = data.get("order_id", "")
        if (data.get("event_id") != event_id or pos_code not in SLOT_INDEX
                or data.get("actor") != leaders.get(pos_code)
                or not isinstance(phones, list) or not phones
                or len(phones) != len(set(phones))
                or any(phone not in people for phone in phones)
                or data.get("status") not in STATUSES
                or not isinstance(data.get("basis"), str)
                or not data["basis"].strip()
                or not isinstance(data.get("reported_at"), str)
                or not data["reported_at"]
                or not isinstance(request_id, str) or not request_id
                or request_id in requests or data.get("data_kind") != "reported"
                or not isinstance(order_id, str)
                or (data["status"] == "executing" and not order_id)
                or (data["status"] != "executing" and order_id)):
            raise ValueError("临时组人员状态报告不合法。")
        requests.add(request_id)
        for phone in phones:
            person = group.get(phone)
            if person is None or person.pos_code != pos_code or not person.arrived:
                continue  # A later transfer invalidates the old group's readiness.
            previous = current.get(phone)
            if previous is not None and previous.status == "evacuated":
                raise ValueError("已撤离人员须先重新核实到场。")
            current[phone] = GroupReadiness(
                phone, pos_code, data["status"], data["basis"],
                data["actor"], data["reported_at"], order_id,
            )
    for phone, item in tuple(current.items()):
        if group[phone].pos_code != item.pos_code:
            current.pop(phone)
            continue
        if item.status == "executing":
            order = workspace.orders.get(item.order_id)
            if order is None or order.status.value not in ACTIVE_ORDER_STATUSES:
                current.pop(phone)
    return current


def report(workspace, *, token, event_id: str, pos_code: str, phones: list[str],
           status: str, basis: str, order_id: str, request_id: str,
           registry, ledger, resolve_subject) -> dict:
    actor = resolve_subject(token)
    leaders = leader_state(workspace, event_id, ledger=ledger)
    if leaders.get(pos_code) != actor:
        raise PermissionError("仅本事件当前组长本人可报告该组临时人员状态。")
    AuthorizationPolicy((RoleGrant(actor, Role.LEADER, event_id, pos_code),)).require(
        actor, Action.REPORT_TEAM, event_id=event_id, team_id=pos_code,
    )
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件已不接受临时组人员状态报告。")
    if status not in STATUSES or not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        raise ValueError("请选择有效状态并填写报告依据。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("状态报告请求编号不能为空。")
    submitted = list(phones or ())
    if not submitted or len(submitted) != len(set(submitted)):
        raise ValueError("请勾选本组需要更新的人员，不能重复。")
    prior = next((e.payload for e in workspace.events if e.kind == REPORTED
                  and e.payload.get("event_id") == event_id
                  and e.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("pos_code") != pos_code
                or prior.get("phones") != submitted or prior.get("status") != status
                or prior.get("basis") != basis or prior.get("order_id", "") != order_id):
            raise ValueError("状态报告请求编号已用于不同内容。")
        return prior
    people = group_people(workspace, event_id, ledger=ledger)
    if any(phone not in people or people[phone].origin != "temporary"
           or people[phone].pos_code != pos_code or not people[phone].arrived
           for phone in submitted):
        raise ValueError("只能报告本组已申报到场的临时人员。")
    if status == "executing":
        order = workspace.orders.get(order_id)
        if order is None or order.status.value not in ACTIVE_ORDER_STATUSES:
            raise ValueError("执行中须关联本事件仍有效的任务。")
    elif order_id:
        raise ValueError("非执行中状态不能关联任务。")
    current = readiness_state(workspace, event_id, ledger=ledger)
    if any(phone in current and current[phone].status == "evacuated"
           for phone in submitted):
        raise ValueError("已撤离人员须先重新核实到场。")
    payload = dict(
        event_id=event_id, pos_code=pos_code, phones=submitted, status=status,
        basis=basis, actor=actor, reported_at=utc_now_iso(),
        order_id=order_id, request_id=request_id, data_kind="reported",
    )
    workspace.append_event(REPORTED, **payload)
    return payload
