"""Evidence-based, non-numeric initial force needs and reassessment cues.

The projection never invents a required headcount or publishes a deployment.
It compares event-bound source fingerprints with the last deputy review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .authorization import Action, Role
from .event_authorization import event_grants
from .fire_events import ONBOARDABLE_STATUSES, utc_now_iso
from .org_model import SLOT_INDEX


REVIEWED = "ForceNeedsReviewed"
SOURCES = ("event", "situation", "tasks", "personnel")
SOURCE_LABELS = {
    "event": "事件基础信息",
    "situation": "火情与态势资料",
    "tasks": "任务记录",
    "personnel": "到场、编组或可派报告",
}
CATEGORIES = (
    ("combat", "作战力量"),
    ("communication", "通信保障"),
    ("medical", "医疗保障"),
    ("other_support", "后勤及其他保障"),
)


@dataclass(frozen=True)
class NeedRow:
    key: str
    label: str
    arrived_reported: int
    standby_reported: int
    status: str = "需求人数待核定"


@dataclass(frozen=True)
class NeedView:
    state: str  # initial_pending | current | needs_reassessment
    fingerprint: str
    components: dict[str, str]
    changed_sources: tuple[str, ...]
    rows: tuple[NeedRow, ...]
    latest_review: dict | None
    unknown_group_count: int


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _category(pos_code: str) -> str:
    slot = SLOT_INDEX.get(pos_code)
    if slot is None:
        return "unknown"
    if slot.domain == "ops":
        return "combat"
    if pos_code == "log-comm":
        return "communication"
    if pos_code == "log-medical":
        return "medical"
    return "other_support"


def need_view(workspace, event_id: str, *, record, people: dict,
              readiness: dict) -> NeedView:
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError("人力需求研判与事件审计链不匹配。")
    if record is None or record.event_id != event_id:
        raise ValueError("人力需求研判须绑定明确事件。")
    event_facts = asdict(record)
    for storage_field in ("snapshot_path", "mission_space_id", "created_at", "updated_at"):
        event_facts.pop(storage_field, None)
    components = {
        "event": _digest(event_facts),
        "situation": _digest({
            "annotations": {k: asdict(v) for k, v in sorted(workspace.annotations.items())},
            "changes": {k: asdict(v) for k, v in sorted(workspace.situation_changes.items())},
            "intel": {k: asdict(v) for k, v in sorted(workspace.intel_items.items())},
            "assessments": [asdict(v) for v in workspace.assessments],
            "source_updates": [e.payload for e in workspace.events if e.kind in {
                'ObservationIntakeRecorded', 'SourceChannelReportRecorded'}
                and e.payload.get('event_id') == event_id],
        }),
        "tasks": _digest({k: asdict(v) for k, v in sorted(workspace.orders.items())}),
        "personnel": _digest({
            "people": {k: asdict(v) for k, v in sorted(people.items())},
            "readiness": {k: asdict(v) for k, v in sorted(readiness.items())},
        }),
    }
    fingerprint = _digest(components)
    deputy = next((grant.subject_id for grant in event_grants(workspace)
                   if grant.role == Role.DEPUTY and grant.event_id == event_id), "")
    reviews = []
    requests = set()
    for event in workspace.events:
        if event.kind != REVIEWED:
            continue
        data = event.payload
        saved = data.get("components")
        request_id = data.get("request_id")
        if (data.get("event_id") != event_id
                or not isinstance(saved, dict) or set(saved) != set(SOURCES)
                or any(not isinstance(saved[key], str) or len(saved[key]) != 64
                       for key in SOURCES)
                or data.get("fingerprint") != _digest(saved)
                or not deputy or data.get("actor") != deputy
                or not isinstance(data.get("note"), str) or not data["note"].strip()
                or not isinstance(data.get("reviewed_at"), str)
                or not data["reviewed_at"]
                or not isinstance(request_id, str) or not request_id
                or request_id in requests or data.get("data_kind") != "reported"):
            raise ValueError("人力需求复核记录不合法。")
        requests.add(request_id)
        reviews.append(data)
    latest = reviews[-1] if reviews else None
    if latest is None:
        state = "initial_pending"
        changed = SOURCES
    else:
        changed = tuple(key for key in SOURCES
                        if latest["components"][key] != components[key])
        state = "needs_reassessment" if changed else "current"
    rows = []
    for key, label in CATEGORIES:
        matching = [person for person in people.values()
                    if _category(person.pos_code) == key]
        rows.append(NeedRow(
            key, label,
            sum(person.arrived for person in matching),
            sum(person.arrived and person.phone in readiness
                and readiness[person.phone].status == "standby" for person in matching),
        ))
    unknown = sum(not person.pos_code for person in people.values())
    return NeedView(state, fingerprint, components, changed, tuple(rows), latest, unknown)


def review(workspace, *, token, event_id: str, note: str, expected_fingerprint: str,
           request_id: str, registry, people: dict, readiness: dict,
           authorization) -> dict:
    actor = authorization.require(token, Action.REVIEW_FORCE_NEEDS, event_id=event_id)
    record = registry.events.get(event_id)
    if record is None or record.status not in ONBOARDABLE_STATUSES:
        raise ValueError("事件已不接受人力需求复核记录。")
    if not isinstance(note, str) or not note.strip() or note != note.strip():
        raise ValueError("请填写初步需求或变化重评的复核意见。")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip():
        raise ValueError("复核请求编号不能为空。")
    prior = next((e.payload for e in workspace.events if e.kind == REVIEWED
                  and e.payload.get("event_id") == event_id
                  and e.payload.get("request_id") == request_id), None)
    if prior is not None:
        if (prior.get("actor") != actor or prior.get("note") != note
                or prior.get("fingerprint") != expected_fingerprint):
            raise ValueError("复核请求编号已用于不同内容。")
        return prior
    view = need_view(workspace, event_id, record=record,
                     people=people, readiness=readiness)
    if expected_fingerprint != view.fingerprint:
        raise ValueError("研判依据已变化，请重新查看后复核。")
    payload = dict(
        event_id=event_id, actor=actor, note=note,
        fingerprint=view.fingerprint, components=view.components,
        request_id=request_id, reviewed_at=utc_now_iso(), data_kind="reported",
    )
    workspace.append_event(REVIEWED, **payload)
    return payload
