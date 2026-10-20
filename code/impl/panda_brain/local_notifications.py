"""Duty-prepared local temporary-person notices; no external send occurs.

These event facts are linked to later individual QR registrations by event and
authenticated phone. Preparing a notice never registers, arrives or assigns a
person, and an older self-registration remains valid without a notice fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from .authorization import Action
from .callouts import CHANNELS
from .fire_events import ONBOARDABLE_STATUSES, utc_now_iso

PREPARED = "TemporaryLocalNoticePrepared"
ROLES = {"local_leader": "属地主管领导", "assistant": "属地协助人员"}


@dataclass(frozen=True)
class LocalNotice:
    event_id: str
    phone: str
    name: str
    unit: str
    local_role: str
    channel: str
    prepared_by: str
    prepared_at: str
    request_id: str


def notice_state(workspace, event_id: str) -> dict[tuple[str, str], LocalNotice]:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("属地临时人员通知记录与事件审计链不匹配。")
    notices: dict[tuple[str, str], LocalNotice] = {}
    roles: dict[str, str] = {}
    requests: set[str] = set()
    for event in workspace.events:
        if event.kind != PREPARED:
            continue
        data = event.payload
        phone, channel = data.get("phone"), data.get("channel")
        request_id = data.get("request_id")
        if (data.get("event_id") != event_id
                or not isinstance(phone, str) or not phone
                or channel not in CHANNELS or data.get("local_role") not in ROLES
                or not isinstance(request_id, str) or not request_id
                or request_id in requests
                or not isinstance(data.get("name"), str) or not data["name"]
                or not isinstance(data.get("unit"), str)
                or not isinstance(data.get("prepared_by"), str)
                or not data["prepared_by"]
                or not isinstance(data.get("prepared_at"), str)
                or not data["prepared_at"]
                or data.get("data_kind") != "simulated"
                or data.get("transport_state") != "not_sent"
                or data.get("delivery_state") != "not_delivered"
                or (phone, channel) in notices
                or phone in roles and roles[phone] != data["local_role"]):
            raise ValueError("属地临时人员模拟通知记录不合法。")
        requests.add(request_id)
        roles[phone] = data["local_role"]
        notices[(phone, channel)] = LocalNotice(
            event_id, phone, data["name"], data["unit"],
            data["local_role"], channel, data["prepared_by"],
            data["prepared_at"], request_id,
        )
    return notices


def prepare(workspace, *, token, event_id: str, phone: str,
            local_role: str, channel: str, request_id: str,
            registry, ledger, authorization) -> dict:
    actor = authorization.require(
        token, Action.PREPARE_LOCAL_NOTIFICATION, event_id=event_id
    )
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件未启动或已关闭，不能准备属地人员模拟通知。")
    if local_role not in ROLES:
        raise ValueError("请选择属地主管领导或属地协助人员类别。")
    if channel not in CHANNELS:
        raise ValueError("模拟通知方式只能选择电话或微信。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("模拟通知请求编号不能为空。")
    prior = next((event.payload for event in workspace.events
                  if event.kind == PREPARED
                  and event.payload.get("event_id") == event_id
                  and event.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("prepared_by") != actor or prior.get("phone") != phone
                or prior.get("local_role") != local_role
                or prior.get("channel") != channel):
            raise ValueError("模拟通知请求编号已用于不同内容。")
        return prior
    person = registry.library.get(phone)
    if person is None:
        raise ValueError("属地人员须先进入可信长期人员库，不能匿名生成模拟通知。")
    if any(phone in squad.member_phones for squad in ledger.squads.values()):
        raise ValueError("固定中队成员须走队长召集流程。")
    roster = registry.rosters.get(event_id)
    if roster is not None and phone in roster.entries:
        raise ValueError("该人员已有本事件固定岗位名册记录，不能重复准备临时通知。")
    existing = notice_state(workspace, event_id)
    if (phone, channel) in existing:
        raise ValueError("该人员在本事件此通道已有模拟通知记录。")
    if any(n.phone == phone and n.local_role != local_role for n in existing.values()):
        raise ValueError("同一人员的属地类别与已有模拟通知不一致。")
    payload = dict(
        event_id=event_id, phone=phone, name=person.name, unit=person.unit,
        local_role=local_role, channel=channel, prepared_by=actor,
        prepared_at=utc_now_iso(), request_id=request_id,
        data_kind="simulated", transport_state="not_sent",
        delivery_state="not_delivered",
    )
    workspace.append_event(PREPARED, **payload)
    return payload
