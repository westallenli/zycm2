"""Captain-only QR roster loading and reported squad arrival.

Loading a roster is separate from reporting attendance. The material flow
record is the versioned source for arrived people and the one-time carry list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .callouts import callout_state
from .fire_events import EventRegistry, ONBOARDABLE_STATUSES, utc_now_iso
from .material_flows import MaterialFlowService
from .org_model import SLOT_INDEX
from .record_contracts import ObjectReference


ROSTER_LOADED = "SquadRosterLoaded"
ARRIVAL_REPORTED = "SquadArrivalReported"
SOURCE = "captain-qr-report"


@dataclass(frozen=True)
class LoadedRoster:
    event_id: str
    squad_id: str
    captain_phone: str
    qr_id: str
    members: tuple[dict, ...]
    loaded_at: str

    @property
    def phones(self) -> tuple[str, ...]:
        return tuple(member["phone"] for member in self.members)


def loaded_rosters(workspace, event_id: str) -> dict[str, LoadedRoster]:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("队伍名册与事件审计链不匹配。")
    loaded = {}
    for event in workspace.events:
        if event.kind != ROSTER_LOADED:
            continue
        payload = event.payload
        members = payload.get("members")
        if (payload.get("event_id") != event_id or not payload.get("squad_id")
                or not payload.get("captain_phone") or not payload.get("qr_id")
                or not payload.get("loaded_at")
                or not isinstance(members, list) or not members
                or any(not isinstance(m, dict) or not m.get("phone")
                       or m.get("pos_code", "") not in ("", *SLOT_INDEX)
                       for m in members)):
            raise ValueError("队伍名册载入记录不合法。")
        phones = tuple(m["phone"] for m in members)
        if (len(phones) != len(set(phones))
                or payload["captain_phone"] not in phones
                or payload["squad_id"] in loaded):
            raise ValueError("队伍名册成员或队长记录冲突。")
        loaded[payload["squad_id"]] = LoadedRoster(
            event_id, payload["squad_id"], payload["captain_phone"],
            payload["qr_id"], tuple(dict(m) for m in members),
            payload["loaded_at"],
        )
    return loaded


def _captain(workspace, *, token, event_id, squad_id, resolve_subject,
             ledger, library) -> str:
    actor = resolve_subject(token)
    selected = callout_state(workspace, event_id).get(squad_id)
    squad = ledger.squads.get(squad_id)
    person = library.get(actor)
    if (selected is None or selected.leader_phone != actor
            or squad is None or squad.leader_phone != actor
            or person is None or not person.is_leader):
        raise PermissionError("仅本事件已选中队伍的现任队长可扫码或报到场。")
    return actor


def scan_roster(workspace, *, token, event_id: str, squad_id: str, qr_id: str,
                registry, ledger, resolve_subject) -> LoadedRoster:
    """Verify event QR and freeze this squad's long-term roster; no arrival."""
    actor = _captain(
        workspace, token=token, event_id=event_id, squad_id=squad_id,
        resolve_subject=resolve_subject, ledger=ledger, library=registry.library,
    )
    qr = registry.qrs.get((qr_id or "").strip())
    record = registry.events.get(event_id)
    if (qr is None or qr.event_id != event_id
            or EventRegistry.qr_status(qr) != "有效"
            or record is None or record.status not in ONBOARDABLE_STATUSES):
        raise ValueError("二维码无效、已过期、已撤销或不属于本进行中事件。")
    loaded = loaded_rosters(workspace, event_id).get(squad_id)
    if loaded is not None:
        return loaded  # Repeat scan cannot replace a frozen event roster.
    snapshot = ledger.snapshots.get(event_id)
    if snapshot is not None and squad_id in snapshot.squads:
        raise ValueError("该队已有旧到场快照，须先核对迁移，不能重复载入名册。")
    squad = ledger.squads[squad_id]
    phones = tuple(squad.member_phones)
    configured_slots = tuple(code for code in squad.position_slots if code in SLOT_INDEX)
    if len(configured_slots) != len(squad.position_slots):
        raise ValueError("中队配置含未知组织槽位，不能冻结本事件名册。")
    roster = registry.rosters.get(event_id)
    if (not phones or len(phones) != len(set(phones))
            or actor not in phones or any(p not in registry.library for p in phones)):
        raise ValueError("长期中队成员或队长资料不完整，不能载入本事件名册。")
    payload = {
        "event_id": event_id, "squad_id": squad_id, "captain_phone": actor,
        "qr_id": qr.qr_id, "loaded_at": utc_now_iso(),
        "members": [
            {"phone": phone, "name": registry.library[phone].name,
             "unit": registry.library[phone].unit,
             "pos_code": (
                 roster.entries[phone].pos_code
                 if roster is not None and phone in roster.entries
                 and roster.entries[phone].pos_code in SLOT_INDEX
                 else configured_slots[0] if len(configured_slots) == 1 else ""
             )}
            for phone in phones
        ],
        "data_kind": "reported",
    }
    workspace.append_event(ROSTER_LOADED, **payload)
    return loaded_rosters(workspace, event_id)[squad_id]


def report_arrival(workspace, *, token, event_id: str, squad_id: str,
                   mode: str, phones: list[str], request_id: str,
                   registry, ledger, resolve_subject):
    """Initial report uses missing phones; supplement uses newly arrived phones."""
    actor = _captain(
        workspace, token=token, event_id=event_id, squad_id=squad_id,
        resolve_subject=resolve_subject, ledger=ledger, library=registry.library,
    )
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件已不接受队伍到场记录。")
    loaded = loaded_rosters(workspace, event_id).get(squad_id)
    if loaded is None or loaded.captain_phone != actor:
        raise ValueError("请先由队长扫码载入该队本事件名册。")
    if mode not in {"initial", "supplement"}:
        raise ValueError("未知到场确认方式。")
    if not isinstance(request_id, str) or not request_id.strip() or request_id != request_id.strip():
        raise ValueError("到场请求编号不能为空。")
    submitted = tuple(phones or ())
    if len(submitted) != len(set(submitted)) or any(p not in loaded.phones for p in submitted):
        raise ValueError("所选人员不在该队名册或重复。")
    if mode == "supplement" and not submitted:
        raise ValueError("请选择实际补到的人员。")
    snapshot = ledger.snapshots.get(event_id)
    squad_snapshot = snapshot.squads.get(squad_id) if snapshot else None
    previous_phones = set(squad_snapshot.arrived_phones) if squad_snapshot else set()

    # Retry after ledger save but before workspace save must reuse the versioned
    # record and complete its audit mirror, rather than double-counting carry.
    prior = next((r for r in ledger.flow_records.values()
                  if r.reference.event_id == event_id and r.request_id == request_id), None)
    if prior is not None:
        content = json.loads(prior.content_json)
        if mode == "initial":
            arrived = set(loaded.phones) - set(submitted)
        else:
            predecessor = next((r for r in ledger.flow_records.values()
                                if r.reference.event_id == event_id
                                and r.reference.object_kind == "team_arrival"
                                and r.reference.object_id == squad_id
                                and r.reference.version == prior.reference.version - 1), None)
            if predecessor is None:
                raise ValueError("补到记录缺少前一版本。")
            arrived = set(json.loads(predecessor.content_json)["arrived_phones"]) | set(submitted)
        if (prior.reference.object_kind != "team_arrival"
                or prior.reference.object_id != squad_id or prior.actor_id != actor
                or prior.source != SOURCE
                or set(content["arrived_phones"]) != arrived
                or (mode == "initial") != (prior.reference.version == 1)):
            raise ValueError("到场请求编号已用于不同内容。")
        flow = prior
    else:
        if mode == "initial":
            arrived = set(loaded.phones) - set(submitted)
            if actor not in arrived:
                raise ValueError("队长本人扫码确认时不能被列为缺员。")
        else:
            arrived = previous_phones | set(submitted)
        if mode == "initial" and squad_snapshot is not None:
            raise ValueError("该队已确认初次到场，不能重复计入随队物资。")
        if mode == "supplement":
            if squad_snapshot is None or not squad_snapshot.arrival_id:
                raise ValueError("该队尚未确认初次到场，不能登记补到。")
            if any(p in previous_phones for p in submitted):
                raise ValueError("补到人员中包含已经到场者。")
        current_version = max(
            (r.reference.version for r in ledger.flow_records.values()
             if r.reference.event_id == event_id
             and r.reference.object_kind == "team_arrival"
             and r.reference.object_id == squad_id), default=0,
        )
        if mode == "initial" and current_version != 0:
            raise ValueError("该队已有到场历史，不能再次初次确认。")
        if mode == "supplement" and current_version == 0:
            raise ValueError("缺少初次到场历史。")
        flow = MaterialFlowService(ledger, event_id).record_arrival(
            ObjectReference(event_id, "team_arrival", squad_id, current_version + 1),
            arrived_phones=tuple(sorted(arrived)), expected_version=current_version,
            actor_id=actor, source=SOURCE, request_id=request_id,
            occurred_at=utc_now_iso(), reason="队长报告后补到" if current_version else "",
        )
    if not any(e.kind == ARRIVAL_REPORTED and e.payload.get("flow_record_id") == flow.id
               for e in workspace.events):
        workspace.append_event(
            ARRIVAL_REPORTED, event_id=event_id, squad_id=squad_id,
            actor=actor, mode=mode, flow_record_id=flow.id,
            arrived_phones=list(sorted(arrived)), data_kind="reported",
        )
    return flow
