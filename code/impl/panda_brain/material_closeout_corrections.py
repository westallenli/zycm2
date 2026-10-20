"""Append-only batch replacement and protection of downstream business facts."""
import json

from . import audited_records as ar

FIELDS = {'corrects_id', 'correction_reason'}


def effective(rows):
    current = {}
    for record in rows:
        value = json.loads(record.content_json)
        target = value.get('corrects_id')
        if target is not None:
            ar.text(target)
            ar.text(value.get('correction_reason'))
            if target not in current:
                raise ValueError('更正须引用本笔当前有效批次，不得越版或重复替换。')
            del current[target]
        elif FIELDS & value.keys():
            raise ValueError('更正来源与理由须同时明确。')
        current[record.id] = record
    return tuple(current.values())


def contains(value, identifiers):
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(contains(json.loads(v) if k == 'content_json' and isinstance(v, str) else v, identifiers)
                   for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return any(contains(v, identifiers) for v in value)
    return False


def protect(workspace, identifiers, *, ignored_kinds=()):
    """Receipt/decision history is allowed; external frozen references are not."""
    identifiers = set(identifiers)
    for event in workspace.events:
        value = event.payload
        kind = value.get('revision', {}).get('reference', {}).get('object_kind')
        if kind in ignored_kinds:
            continue
        if contains(value, identifiers):
            raise ValueError('后续业务已使用该来源；须保留已发生事实，不能直接更正。')


def custody_protect(workspace, ledger, loan_id):
    from .material_custody_receipts import KIND
    from .material_custody_settlements import VERIFICATION, SETTLEMENT
    kinds = (KIND, VERIFICATION, SETTLEMENT)
    refs = {r.id for r in workspace.object_revisions.values()
            if r.reference.object_kind in kinds and r.reference.object_id == loan_id}
    protect(workspace, refs, ignored_kinds=kinds)
    for record in ledger.withdrawal_records.values():
        if record.reference.event_id == workspace.event_id and contains(json.loads(record.content_json), refs):
            raise ValueError('借出队撤离已使用结清来源，不能抹除或替换该事实。')


def supply_protect(workspace, ledger, target):
    from .material_supply_receipts import KIND
    value = json.loads(target.content_json)
    protect(workspace, {target.id}, ignored_kinds=(KIND,))
    index = next(i for i, e in enumerate(workspace.events) if e.payload.get('revision_id') == target.id)
    for event in workspace.events[index + 1:]:
        for key, rows in (('flow_record_id', ledger.flow_records),
                          ('transfer_record_id', ledger.transfer_records),
                          ('withdrawal_record_id', ledger.withdrawal_records)):
            record = rows.get(event.payload.get(key))
            if record is None:
                continue
            data = json.loads(record.content_json)
            if record.reference.object_kind == 'team_material_withdrawal':
                used = record.reference.object_id == value['squad_id']
            else:
                used = data.get('item_id') == value['item_id'] and value['squad_id'] in {
                    data.get('squad_id'), data.get('from_squad'), data.get('to_squad')}
            if used:
                raise ValueError('该队后续耗损、调拨或撤离已使用实收余额，不能直接更正。')
