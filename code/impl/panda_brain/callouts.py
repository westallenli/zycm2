"""Event-scoped squad callout simulation; no phone/WeChat transport is used."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .authorization import Action, AuthorizationPolicy, Role, RoleGrant
from .event_authorization import EventAuthorizationService
from .record_contracts import explicit_time


SELECTED = "SquadCalloutSelected"
REPLIED = "SquadCalloutCaptainReplied"
SIMULATION_FAILED = "SquadCalloutSimulationFailed"
CHANNELS = {"phone": "电话", "wechat": "微信"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Callout:
    event_id: str
    squad_id: str
    leader_phone: str
    channel: str
    selected_by: str
    selected_at: str
    selection_request_id: str
    reply: dict | None = None
    simulation_failure: dict | None = None


def callout_state(workspace, event_id: str) -> dict[str, Callout]:
    """Project callouts from the verified append-only event chain."""
    if not event_id or workspace.event_id != event_id:
        raise ValueError("召集记录与事件任务空间不匹配。")
    if workspace.verify_audit_chain():
        raise ValueError("事件任务空间审计链校验失败。")
    records: dict[str, Callout] = {}
    failure_requests: set[str] = set()
    for event in workspace.events:
        payload = event.payload
        if event.kind == SELECTED:
            if payload.get("event_id") != event_id or payload.get("data_kind") != "simulated" or payload.get("transport_state") != "not_sent":
                raise ValueError("召集模拟通知来源不合法。")
            if (payload.get("channel") not in CHANNELS
                    or not isinstance(payload.get("squads"), list)
                    or not payload["squads"]):
                raise ValueError("召集模拟通知内容不合法。")
            for item in payload["squads"]:
                if (not isinstance(item, dict) or not item.get("squad_id")
                        or not item.get("leader_phone")):
                    raise ValueError("召集中队或队长标识不合法。")
                squad_id = item["squad_id"]
                if squad_id in records:
                    raise ValueError("同一中队重复召集。")
                records[squad_id] = Callout(
                    event_id, squad_id, item["leader_phone"], payload["channel"],
                    payload["selected_by"], payload["selected_at"], payload["request_id"],
                )
        elif event.kind == REPLIED:
            if payload.get("event_id") != event_id or payload.get("data_kind") != "simulated":
                raise ValueError("队长模拟回执来源不合法。")
            if (payload.get("source") != "in_app_demo_receipt"
                    or payload.get("receipt_state") != "acknowledged_in_app"
                    or payload.get("can_depart") not in {"yes", "no"}):
                raise ValueError("队长模拟回执内容不合法。")
            squad_id = payload.get("squad_id", "")
            existing = records.get(squad_id)
            if existing is None or existing.reply is not None or payload.get("reported_by") != existing.leader_phone:
                raise ValueError("队长模拟回执与召集记录不匹配。")
            records[squad_id] = Callout(
                existing.event_id, existing.squad_id, existing.leader_phone,
                existing.channel, existing.selected_by, existing.selected_at,
                existing.selection_request_id, dict(payload), existing.simulation_failure,
            )
        elif event.kind == SIMULATION_FAILED:
            squad_id = payload.get("squad_id", "")
            existing = records.get(squad_id)
            if (existing is None or existing.simulation_failure is not None
                    or existing.reply is not None
                    or payload.get("event_id") != event_id
                    or payload.get("recorded_by") != existing.selected_by
                    or payload.get("data_kind") != "simulated"
                    or payload.get("scenario_state") != "simulated_failure"
                    or payload.get("transport_state") != "not_sent"
                    or payload.get("delivery_state") != "not_delivered"
                    or not isinstance(payload.get("reason"), str)
                    or not payload["reason"].strip()
                    or not isinstance(payload.get("request_id"), str)
                    or not payload["request_id"]
                    or payload["request_id"] in failure_requests
                    or not isinstance(payload.get("recorded_at"), str)
                    or not payload["recorded_at"]):
                raise ValueError("召集演练失败记录不合法。")
            failure_requests.add(payload["request_id"])
            records[squad_id] = replace(existing, simulation_failure=dict(payload))
    return records


def record_simulated_failure(workspace, *, token: str, event_id: str,
                             squad_id: str, reason: str, request_id: str,
                             authorization: EventAuthorizationService) -> dict:
    """Record an exercise scenario; no transport attempt or delivery is claimed."""
    actor = authorization.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise ValueError("请填写演练通知失败原因。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("演练请求编号不能为空。")
    current = callout_state(workspace, event_id)
    selected = current.get(squad_id)
    if selected is None or selected.selected_by != actor:
        raise ValueError("只有原选队副总指挥可登记该队的模拟失败场景。")
    expected = dict(event_id=event_id, squad_id=squad_id, recorded_by=actor,
                    reason=reason, request_id=request_id,
                    data_kind="simulated", scenario_state="simulated_failure",
                    transport_state="not_sent", delivery_state="not_delivered")
    if selected.simulation_failure is not None:
        prior = selected.simulation_failure
        if all(prior.get(key) == value for key, value in expected.items()):
            return prior
        raise ValueError("该中队已有演练失败记录。")
    if selected.reply is not None:
        raise ValueError("队长已有模拟回执，不能再登记通知失败场景。")
    if any(e.kind == SIMULATION_FAILED and e.payload.get("request_id") == request_id
           for e in workspace.events):
        raise ValueError("演练请求编号已用于其他记录。")
    payload = {**expected, "recorded_at": _now()}
    workspace.append_event(SIMULATION_FAILED, **payload)
    return payload


def select_squads(workspace, *, token: str, event_id: str, squad_ids,
                  channel: str, request_id: str, authorization: EventAuthorizationService,
                  squads: dict, library: dict) -> dict:
    """Deputy selects only named squads; prepare a simulated, unsent notice."""
    actor = authorization.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
    if channel not in CHANNELS:
        raise ValueError("模拟通知方式只能选择电话或微信。")
    if not isinstance(request_id, str) or not request_id.strip() or request_id != request_id.strip():
        raise ValueError("召集请求编号不能为空。")
    submitted = tuple(squad_ids or ())
    wanted = tuple(dict.fromkeys(submitted))
    if not wanted or len(wanted) != len(submitted):
        raise ValueError("请明确选择至少一支队伍，不能重复选择。")
    selected = []
    for squad_id in wanted:
        squad = squads.get(squad_id)
        if squad is None:
            raise ValueError("所选中队不在长期队伍底账。")
        leader = squad.leader_phone
        person = library.get(leader)
        if not leader or leader not in squad.member_phones or person is None or not person.is_leader:
            raise ValueError(f"{squad.name} 未配置可核验的队长，不能生成通知。")
        selected.append({"squad_id": squad_id, "leader_phone": leader})
    current = callout_state(workspace, event_id)
    for event in workspace.events:
        if event.kind == SELECTED and event.payload.get("request_id") == request_id:
            prior = event.payload
            if (prior.get("event_id") == event_id and prior.get("selected_by") == actor
                    and prior.get("channel") == channel and prior.get("squads") == selected):
                return prior
            raise ValueError("召集请求编号已用于不同内容。")
    if any(item["squad_id"] in current for item in selected):
        raise ValueError("所选中队中已有召集记录，不能重复生成通知。")
    payload = {
        "event_id": event_id, "squads": selected, "channel": channel,
        "selected_by": actor, "selected_at": _now(), "request_id": request_id,
        "data_kind": "simulated", "transport_state": "not_sent",
    }
    workspace.append_event(SELECTED, **payload)
    return payload


def captain_reply(workspace, *, token: str, event_id: str, squad_id: str,
                  can_depart: str, expected_people: str, eta: str, reason: str,
                  request_id: str, resolve_subject, squads: dict, library: dict) -> dict:
    """Captain's in-app simulated reply; no delivery or actual arrival is inferred."""
    actor = resolve_subject(token)
    current = callout_state(workspace, event_id)
    callout = current.get(squad_id)
    if callout is None:
        raise ValueError("该中队尚未被选中召集。")
    squad = squads.get(squad_id)
    person = library.get(callout.leader_phone)
    if (squad is None or squad.leader_phone != callout.leader_phone
            or person is None or not person.is_leader):
        raise ValueError("中队队长资料已变更，需人工核对后再回执。")
    AuthorizationPolicy((RoleGrant(callout.leader_phone, Role.LEADER, event_id, squad_id),)).require(
        actor, Action.RECEIVE_CALLOUT, event_id=event_id, team_id=squad_id
    )
    if can_depart not in {"yes", "no"}:
        raise ValueError("请选择能否出动。")
    if not isinstance(request_id, str) or not request_id.strip() or request_id != request_id.strip():
        raise ValueError("回执请求编号不能为空。")
    reason = (reason or "").strip()
    if can_depart == "yes":
        try:
            people = int(expected_people)
        except (TypeError, ValueError) as exc:
            raise ValueError("可出动时请填写预计人数。") from exc
        if people <= 0:
            raise ValueError("预计人数必须大于零。")
        explicit_time(eta)
        if reason:
            raise ValueError("可出动回执不填写不能出动原因。")
    else:
        if not reason:
            raise ValueError("不能出动时必须填写原因。")
        if expected_people or eta:
            raise ValueError("不能出动时不得填写预计人数或到场时间。")
        people = 0
    expected = {
        "event_id": event_id, "squad_id": squad_id, "reported_by": actor,
        "can_depart": can_depart, "expected_people": people,
        "eta": eta, "reason": reason, "request_id": request_id,
        "data_kind": "simulated", "source": "in_app_demo_receipt",
        "receipt_state": "acknowledged_in_app",
    }
    if callout.reply is not None:
        old = callout.reply
        if all(old.get(key) == value for key, value in expected.items()):
            return old
        raise ValueError("该中队已有回执；变更须走后续更正流程。")
    if any(e.kind == REPLIED and e.payload.get("request_id") == request_id
           for e in workspace.events):
        raise ValueError("回执请求编号已用于其他回执。")
    payload = {**expected, "reported_at": _now()}
    workspace.append_event(REPLIED, **payload)
    return payload
