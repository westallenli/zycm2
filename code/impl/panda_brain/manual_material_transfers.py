"""Named event transfer reports with an audit mirror; trusted entry contract."""

from dataclasses import asdict

from .authorization import Action, AuthorizationPolicy
from .callouts import callout_state
from .event_authorization import event_grants
from .fire_events import ONBOARDABLE_STATUSES
from .material_transfers import MaterialTransferService, _history


RECORDED = 'MaterialTransferStageReported'


def _require_mirror(workspace, record, stage, *, repair=False):
    payload = {'event_id': record.reference.event_id,
               'transfer_record_id': record.id, 'stage': stage,
               'record': asdict(record), 'data_kind': 'reported'}
    matches = [entry for entry in workspace.events
               if entry.kind == RECORDED
               and entry.payload.get('transfer_record_id') == record.id]
    if matches:
        if len(matches) != 1 or matches[0].payload != payload:
            raise ValueError('调拨流水与事件审计镜像不一致。')
    elif repair:
        workspace.append_event(RECORDED, **payload)
    else:
        raise ValueError('调拨上一阶段缺少事件审计镜像，请先恢复原请求。')


def record(workspace, ledger, registry, *, token, resolve_subject,
           event_id, stage, transfer_id, request_id, evidence,
           item_id='', from_squad='', to_squad='', quantity=None,
           expected_version=None, occurred_at='', purpose='transfer',
           return_for='', damaged_quantity=0):
    """Resolve actor from a live session and record one reported stage."""
    actor = resolve_subject(token)
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('调拨与当前事件任务空间不一致。')
    event = registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('当前事件不接受调拨报告。')
    if not isinstance(evidence, str) or not evidence.strip() or evidence != evidence.strip():
        raise ValueError('调拨操作须填写原始依据。')
    if stage not in {'approved', 'sent', 'received'}:
        raise ValueError('调拨阶段不受支持。')
    if stage == 'approved':
        AuthorizationPolicy(event_grants(workspace)).require(
            actor, Action.CONFIRM_MATERIAL_TRANSFER, event_id=event_id)
    selected = callout_state(workspace, event_id)
    service = MaterialTransferService(workspace, ledger, event_id)
    history = _history(ledger, event_id, transfer_id)
    from json import loads
    selected_item = item_id if stage == 'approved' else (
        loads(history[0].content_json)['item_id'] if history else '')
    if ((any(r.reference.event_id == event_id and r.reference.object_kind == 'material_loss'
            and 'inventory_basis' in loads(r.content_json) for r in ledger.flow_records.values())
            or any(r.reference.event_id==event_id for r in ledger.supply_receipts.values())
            or any(e.kind=='ObjectRevisionRecorded' and e.payload.get('revision',{}).get('reference',{}).get('object_kind')=='formal_supply_receipt' for e in workspace.events))
            and not any(r.reference.event_id == event_id and r.request_id == request_id
                        for r in ledger.transfer_records.values())):
        from .material_inventory_projection import project
        project(workspace, ledger, event_id=event_id, item_id=selected_item)
    if stage == 'approved':
        if purpose not in {'transfer', 'loan', 'return'}:
            raise ValueError('调拨、借用或归还用途不正确。')
        if damaged_quantity != 0:
            raise ValueError('损坏数量只能在归还实收时报告。')
        if from_squad not in selected or to_squad not in selected:
            raise ValueError('调拨双方须是本事件已选中队伍。')
        if purpose == 'return':
            original = _history(ledger, event_id, return_for)
            for prior in original:
                prior_stage = ('approved' if prior.reference.version == 1 else
                               'sent' if prior.reference.version == 2 else 'received')
                _require_mirror(workspace, prior, prior_stage)
        result = service.approve(
            transfer_id=transfer_id, item_id=item_id, from_squad=from_squad,
            to_squad=to_squad, quantity=quantity, actor_id=actor,
            source={'transfer': '人工调拨确认：', 'loan': '人工借用确认：',
                    'return': '人工归还确认：'}[purpose] + evidence,
            request_id=request_id,
            occurred_at=occurred_at, purpose=purpose, return_for=return_for,
        )
    else:
        if purpose != 'transfer' or return_for or (stage == 'sent' and damaged_quantity != 0):
            raise ValueError('交出与实收阶段沿用批准时的借还关系。')
        if not history:
            raise ValueError('调拨须先有本事件批准记录。')
        for prior in history:
            if prior.request_id == request_id:
                continue  # Same-request retry repairs its missing mirror below.
            prior_stage = 'approved' if prior.reference.version == 1 else 'sent' if prior.reference.version == 2 else 'received'
            _require_mirror(workspace, prior, prior_stage)
        from json import loads

        approved = loads(history[0].content_json)
        squad_id = approved['from_squad'] if stage == 'sent' else approved['to_squad']
        squad = ledger.squads.get(squad_id)
        person = registry.library.get(actor)
        callout = selected.get(squad_id)
        if (squad is None or callout is None or callout.leader_phone != actor
                or squad.leader_phone != actor or person is None or not person.is_leader):
            raise PermissionError('仅本事件相应中队的具名现任队长可报告交出或实收。')
        if stage == 'sent':
            result = service.send(
                transfer_id=transfer_id, actor_id=actor,
                source='人工交出报告：' + evidence, request_id=request_id,
                expected_version=expected_version, occurred_at=occurred_at,
            )
        else:
            result = service.receive(
                transfer_id=transfer_id, quantity=quantity, actor_id=actor,
                source='人工实收报告：' + evidence, request_id=request_id,
                expected_version=expected_version, occurred_at=occurred_at,
                damaged_quantity=damaged_quantity,
            )
    _require_mirror(workspace, result, stage, repair=True)
    return result
