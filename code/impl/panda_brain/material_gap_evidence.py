"""Read-only evidence boundary for formal material shortage assessment (M03).

This module records which existing inputs can be traced to the event. It does
not turn the drill baseline, task prose, or roster attendance into a formal
material demand or forecast.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .models import OrderStatus
from .task_sop import flow_issue
from .material_inventory_projection import EventInventory


def _arrival_matches(ledger, event_id: str, squad_id: str, squad) -> bool:
    record = ledger.flow_records.get(squad.arrival_id)
    if record is None or record.id != squad.arrival_id:
        return False
    ref = record.reference
    if (ref.event_id != event_id or ref.object_kind != "team_arrival"
            or ref.object_id != squad_id or ref.version != 1):
        return False
    try:
        baseline = json.loads(record.content_json)["carry_baseline"]
    except (ValueError, KeyError, TypeError):
        return False
    return baseline == {item_id: line.qty_registered
                        for item_id, line in squad.lines.items()}


def _loss_history(ledger, event_id: str, item_id: str, snapshot) -> tuple[tuple[str, ...], bool]:
    """Replay report revisions for one item; a match proves ledger consistency only."""
    by_object: dict[str, list] = {}
    for key, record in ledger.flow_records.items():
        ref = record.reference
        if ref.event_id != event_id or ref.object_kind != "material_loss":
            continue
        if key != record.id or record.data_kind != "reported":
            return (), False
        by_object.setdefault(ref.object_id, []).append(record)

    totals = {squad_id: {"consumed": 0.0, "damaged": 0.0}
              for squad_id in snapshot.squads}
    refs: list[str] = []
    legacy_totals = {squad_id: 0.0 for squad_id in snapshot.squads}
    for records in by_object.values():
        records.sort(key=lambda record: record.reference.version)
        try:
            values = [json.loads(record.content_json) for record in records]
        except (ValueError, TypeError):
            return (), False
        if not all(isinstance(value, dict) for value in values):
            return (), False
        if not any(value.get("item_id") == item_id for value in values):
            continue
        if [record.reference.version for record in records] != list(range(1, len(records) + 1)):
            return (), False
        first = values[0]
        squad_id, kind = first.get("squad_id"), first.get("kind")
        if squad_id not in snapshot.squads or kind not in totals[squad_id]:
            return (), False
        line = snapshot.squads[squad_id].lines.get(item_id)
        for value in values:
            amount = value.get("amount")
            if ((value.get("squad_id"), value.get("item_id"), value.get("kind"))
                    != (squad_id, item_id, kind)
                    or type(amount) not in {int, float} or not math.isfinite(amount)
                    or amount < 0):
                return (), False
        if any(not isinstance(value.get("reason"), str)
               or not value["reason"].strip() for value in values[1:]):
            return (), False
        try:
            from .material_loss_sources import validate_basis
            for record, value in zip(records, values):
                if 'inventory_basis' in value:
                    validate_basis(ledger, record)
                elif line is None or value['amount'] > line.qty_registered:
                    return (), False
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return (), False
        if 'inventory_basis' not in values[-1]:
            legacy_totals[squad_id] += values[-1]['amount']
        totals[squad_id][kind] += float(values[-1]["amount"])
        refs.extend(record.id for record in records)

    for squad_id, squad in snapshot.squads.items():
        line = squad.lines.get(item_id)
        expected = totals[squad_id]
        try:
            from .material_loss_sources import transfer_amounts
            received, sent = transfer_amounts(ledger, event_id, squad_id, item_id)
        except (ValueError, KeyError, TypeError):
            return (), False
        if line is None:
            if legacy_totals[squad_id] or sum(expected.values()) > received - sent + 1e-9:
                return (), False
            continue
        if (line.qty_usable is None or line.qty_usable != line.qty_registered
                or line.missing != 0 or legacy_totals[squad_id] > line.qty_registered
                or expected["consumed"] + expected["damaged"] > line.qty_registered + received - sent + 1e-9
                or not math.isclose(line.consumed, expected["consumed"], abs_tol=1e-9)
                or not math.isclose(line.damaged, expected["damaged"], abs_tol=1e-9)):
            return (), False
    return tuple(sorted(refs)), True


@dataclass(frozen=True)
class GapInputAssessment:
    event_id: str
    item_id: str
    order_id: str
    task_ref: str | None
    arrival_refs: tuple[str, ...]
    loss_refs: tuple[str, ...]
    registered_on_site: float | None
    book_available: float | None
    missing: tuple[str, ...]
    current_status: str = "待核实"
    projected_status: str = "待核实"
    current_gap: None = None
    projected_gap: None = None
    inventory: EventInventory | None = None
    input_ref: str | None = None
    force_count: int | None = None
    duration_hours: float | None = None
    task_inputs: dict | None = None


def assess_gap_inputs(workspace, ledger, *, event_id: str, item_id: str,
                      order_id: str) -> GapInputAssessment:
    """Inspect event-bound task and arrival evidence without computing demand.

    An audited command and current named input reports supply versioned context.
    Reported force, duration and usage quantities do not establish professional
    usage rules, so both formal shortage conclusions remain pending.
    """
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        return GapInputAssessment(
            event_id, item_id, order_id, None, (), (), None, None,
            ("事件归属或审计链待核对",),
        )

    missing: list[str] = []
    task_ref = None
    order = workspace.orders.get(order_id)
    flow = workspace.command_flows.get(order_id)
    if (order is not None and order.status in {OrderStatus.CONFIRMED, OrderStatus.ACTIVE}
            and order.confirmed_by and flow is not None
            and not flow_issue(workspace, order_id, event_id)):
        task_ref = f"{order_id}@{flow.content_version}"
    else:
        missing.append("本事件已确认且审计一致的当前版任务")

    arrival_refs: tuple[str, ...] = ()
    loss_refs: tuple[str, ...] = ()
    registered_on_site = None
    book_available = None
    inventory = None
    snapshot = ledger.snapshots.get(event_id)
    if item_id not in ledger.items:
        missing.append("物资档案")
    if snapshot is None or snapshot.event_id != event_id or not snapshot.squads:
        missing.append("本事件到场物资快照")
    elif any(not _arrival_matches(ledger, event_id, squad_id, squad)
             for squad_id, squad in snapshot.squads.items()):
        missing.append("到场物资快照的版本化来源")
    else:
        arrival_refs = tuple(sorted(squad.arrival_id for squad in snapshot.squads.values()))
        if item_id in ledger.items:
            try:
                from .material_inventory_reads import read_event
                inventory = read_event(workspace, ledger, event_id, extra_items=(item_id,))[item_id]
                arrival_refs, loss_refs = inventory.arrival_refs, inventory.loss_refs
                registered_on_site = inventory.arrival_base
                book_available = inventory.book_available
                if inventory.exceptions:
                    missing.append("留置/移交位置及可调度归属待核对，未并入队伍账面可用")
            except ValueError:
                arrival_refs = loss_refs = ()
                registered_on_site = None
                missing.append("调拨、撤离与数量守恒来源待核对")

    input_missing = [
        "本版任务与该物资的需求对应关系",
        "本版任务实际投入力量及来源",
        "本版任务预计作业时长及来源",
    ]
    inputs = None
    input_ref = None
    if task_ref:
        try:
            from .task_material_inputs import review
            reported = review(workspace, ledger, event_id=event_id, order_id=order_id, item_id=item_id)
            inputs = reported.current
            if inputs:
                input_ref = reported.records[-1].id
                input_missing.remove("本版任务与该物资的需求对应关系")
                if inputs['force_count'] is not None:
                    input_missing.remove("本版任务实际投入力量及来源")
                if inputs['duration_hours'] is not None:
                    input_missing.remove("本版任务预计作业时长及来源")
            elif reported.records:
                missing.append("任务或库存来源已变化，任务物资输入须重新评估")
        except (ValueError, KeyError, TypeError, AttributeError):
            missing.append("任务物资输入来源待核对")
    missing.extend(input_missing)
    missing.append("经专业核定的物资用量依据")
    return GapInputAssessment(event_id, item_id, order_id, task_ref, arrival_refs, loss_refs,
                              registered_on_site, book_available, tuple(missing), inventory=inventory,
                              input_ref=input_ref, force_count=inputs['force_count'] if inputs else None,
                              duration_hours=inputs['duration_hours'] if inputs else None, task_inputs=inputs)
