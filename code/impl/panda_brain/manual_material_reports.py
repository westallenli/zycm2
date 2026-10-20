"""Authenticated manual material loss reports for an arrived fixed squad."""
from dataclasses import asdict

from .callouts import callout_state
from .fire_events import ONBOARDABLE_STATUSES
from .material_flows import MaterialFlowService
from .record_contracts import ObjectReference


REPORTED = 'ManualMaterialLossReported'
SOURCE_PREFIX = '人工报告：'


def report(workspace, ledger, registry, *, token, resolve_subject,
           event_id: str, squad_id: str, item_id: str, loss_id: str,
           kind: str, amount: float, expected_version: int,
           evidence: str, occurred_at: str, reason: str, request_id: str):
    """Record a named human report; it is not independent verification."""
    actor = resolve_subject(token)
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('物资报告与当前事件审计链不一致。')
    from .radio_material_reports import history as radio_material_history
    radio_material_history(workspace, ledger, event_id)
    event = registry.events.get(event_id)
    selected = callout_state(workspace, event_id).get(squad_id)
    squad = ledger.squads.get(squad_id)
    person = registry.library.get(actor)
    if (selected is None or selected.leader_phone != actor or squad is None
            or squad.leader_phone != actor or person is None or not person.is_leader):
        raise PermissionError('仅本事件已选中队伍的现任队长可报告本队物资。')
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('事件已不接受现场物资报告。')
    snapshot = ledger.snapshots.get(event_id)
    arrival = snapshot.squads.get(squad_id) if snapshot else None
    if arrival is None or not arrival.arrival_id:
        raise ValueError('须先确认本队到场。')
    if not isinstance(evidence, str) or not evidence.strip() or evidence != evidence.strip():
        raise ValueError('请填写本次人工报告的原始依据。')
    history = (record for record in ledger.flow_records.values()
               if record.reference.event_id == event_id
               and record.reference.object_kind == 'material_loss'
               and record.reference.object_id == loss_id)
    if any(record.data_kind != 'reported' or not record.source.startswith(SOURCE_PREFIX)
           for record in history):
        raise ValueError('此耗损记录不属于人工报告入口，不能在此更正。')
    reference = ObjectReference(event_id, 'material_loss', loss_id, expected_version + 1)
    # New reports need complete event sources. Original retries can repair their own
    # missing mirror and must retain the original frozen inventory basis.
    if ((any(r.reference.event_id == event_id for r in ledger.transfer_records.values())
            or any(r.reference.event_id==event_id for r in ledger.supply_receipts.values())
            or any(e.kind=='ObjectRevisionRecorded' and e.payload.get('revision',{}).get('reference',{}).get('object_kind')=='formal_supply_receipt' for e in workspace.events))
            and not any(r.reference.event_id == event_id and r.request_id == request_id
                        for r in ledger.flow_records.values())):
        from .material_inventory_projection import project
        project(workspace, ledger, event_id=event_id, item_id=item_id)
    revision = MaterialFlowService(ledger, event_id).record_loss(
        reference, squad_id=squad_id, item_id=item_id, kind=kind, amount=amount,
        expected_version=expected_version, actor_id=actor,
        source=SOURCE_PREFIX + evidence, request_id=request_id,
        occurred_at=occurred_at, reason=reason,
    )
    payload = {'event_id': event_id, 'flow_record_id': revision.id,
               'record': asdict(revision), 'data_kind': 'reported'}
    mirrors = [entry for entry in workspace.events
               if entry.kind == REPORTED
               and entry.payload.get('flow_record_id') == revision.id]
    if mirrors:
        if len(mirrors) != 1 or mirrors[0].payload != payload:
            raise ValueError('物资报告与事件审计镜像不一致。')
    else:
        workspace.append_event(REPORTED, **payload)
    return revision
