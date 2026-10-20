"""Captain-reported current readiness, separate from event attendance.

Only an explicit standby report counts as currently deployable. A loaded
roster, arrival report, or old personal state never supplies that conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from .callouts import callout_state
from .captain_arrivals import loaded_rosters
from .fire_events import ONBOARDABLE_STATUSES, utc_now_iso


REPORTED = "SquadReadinessReported"
STATUSES = {
    "standby": "待命可派",
    "executing": "执行中",
    "resting": "轮休",
    "unavailable": "暂时无法执行",
    "evacuated": "已撤离",
}
ACTIVE_ORDER_STATUSES = {"confirmed", "active", "paused"}


@dataclass(frozen=True)
class Readiness:
    phone: str
    squad_id: str
    status: str
    basis: str
    actor: str
    reported_at: str
    order_id: str = ""


def readiness_state(workspace, event_id: str, *, ledger) -> dict[str, Readiness]:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("队内人员状态与事件审计链不匹配。")
    rosters = loaded_rosters(workspace, event_id)
    snapshot = ledger.snapshots.get(event_id)
    current: dict[str, Readiness] = {}
    requests: set[str] = set()
    for event in workspace.events:
        if event.kind != REPORTED:
            continue
        data = event.payload
        squad_id = data.get("squad_id")
        roster = rosters.get(squad_id)
        arrival = snapshot.squads.get(squad_id) if snapshot else None
        phones = data.get("phones")
        request_id = data.get("request_id")
        if (data.get("event_id") != event_id or roster is None
                or arrival is None or not arrival.arrival_id
                or data.get("actor") != roster.captain_phone
                or not isinstance(phones, list) or not phones
                or len(phones) != len(set(phones))
                or any(phone not in arrival.arrived_phones for phone in phones)
                or data.get("status") not in STATUSES
                or not isinstance(data.get("basis"), str)
                or not data["basis"].strip()
                or not isinstance(data.get("reported_at"), str)
                or not data["reported_at"]
                or not isinstance(request_id, str) or not request_id
                or request_id in requests or data.get("data_kind") != "reported"):
            raise ValueError("队内人员状态报告不合法。")
        order_id = data.get("order_id", "")
        if (not isinstance(order_id, str)
                or (data["status"] == "executing" and not order_id)
                or (data["status"] != "executing" and order_id)):
            raise ValueError("执行中状态必须且只能关联现有任务。")
        requests.add(request_id)
        for phone in phones:
            previous = current.get(phone)
            if previous is not None and previous.status == "evacuated":
                raise ValueError("已撤离人员须先重新核实到场，不能直接恢复可派状态。")
            current[phone] = Readiness(
                phone, squad_id, data["status"], data["basis"],
                data["actor"], data["reported_at"], order_id,
            )
    for phone, item in tuple(current.items()):
        if item.status == "executing":
            order = workspace.orders.get(item.order_id)
            if order is None or order.status.value not in ACTIVE_ORDER_STATUSES:
                current.pop(phone)  # Task ended: no automatic return to standby.
    return current


def report(workspace, *, token, event_id: str, squad_id: str, phones: list[str],
           status: str, basis: str, order_id: str, request_id: str,
           registry, ledger, resolve_subject) -> dict:
    actor = resolve_subject(token)
    selected = callout_state(workspace, event_id).get(squad_id)
    squad = ledger.squads.get(squad_id)
    person = registry.library.get(actor)
    if (selected is None or selected.leader_phone != actor or squad is None
            or squad.leader_phone != actor or person is None or not person.is_leader):
        raise PermissionError("仅本事件已选中队伍的现任队长可报告队内状态。")
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件已不接受队内人员状态报告。")
    if status not in STATUSES:
        raise ValueError("请选择有效的队内人员状态。")
    if not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        raise ValueError("请填写本次人员状态报告依据。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("状态报告请求编号不能为空。")
    submitted = list(phones or ())
    if not submitted or len(submitted) != len(set(submitted)):
        raise ValueError("请明确勾选本次更新的人员，不能重复。")
    prior = next((e.payload for e in workspace.events if e.kind == REPORTED
                  and e.payload.get("event_id") == event_id
                  and e.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("squad_id") != squad_id
                or prior.get("phones") != submitted or prior.get("status") != status
                or prior.get("basis") != basis or prior.get("order_id", "") != order_id):
            raise ValueError("状态报告请求编号已用于不同内容。")
        return prior
    roster = loaded_rosters(workspace, event_id).get(squad_id)
    snapshot = ledger.snapshots.get(event_id)
    arrival = snapshot.squads.get(squad_id) if snapshot else None
    if roster is None or arrival is None or not arrival.arrival_id:
        raise ValueError("须先由队长扫码载入名册并确认本队到场。")
    if any(phone not in roster.phones or phone not in arrival.arrived_phones
           for phone in submitted):
        raise ValueError("只能更新本队已申报到场人员。")
    if status == "executing":
        order = workspace.orders.get(order_id)
        if order is None or order.status.value not in ACTIVE_ORDER_STATUSES:
            raise ValueError("执行中须关联本事件仍有效的任务。")
    elif order_id:
        raise ValueError("非执行中状态不能关联任务。")
    current = readiness_state(workspace, event_id, ledger=ledger)
    if any(current.get(phone) is not None
           and current[phone].status == "evacuated" for phone in submitted):
        raise ValueError("已撤离人员须先重新核实到场，不能直接恢复可派状态。")
    payload = dict(
        event_id=event_id, squad_id=squad_id, phones=submitted, status=status,
        basis=basis, actor=actor, reported_at=utc_now_iso(),
        order_id=order_id, request_id=request_id, data_kind="reported",
    )
    workspace.append_event(REPORTED, **payload)
    return payload
