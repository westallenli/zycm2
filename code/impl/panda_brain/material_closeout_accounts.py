"""Verified event closeout linked to explicit, absolute catalog adjustments."""
from dataclasses import asdict
import json
import math
from . import audited_records as ar
from .material_supply_contacts import require_leader

KIND = 'material_closeout_account'
SOURCE = 'named-material-closeout-account'


def number(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError('核实数量须为明确非负有限数。')
    return float(value)


def facts(space, ledger, spaces, item_id, squad_id, point_id=''):
    from .material_inventory_projection import project
    from .material_loss_sources import loss_amounts
    from .material_withdrawals import withdrawal_record
    from .material_transfers import loan_status
    view = project(space, ledger, event_id=space.event_id, item_id=item_id)
    departure = withdrawal_record(ledger, event_id=space.event_id, squad_id=squad_id)
    if departure is None:
        raise ValueError('须先具名报告本队撤离，不能将现场余额当作长期可携行量。')
    value = json.loads(departure.content_json)
    line = next((v for v in value['lines'] if v['item_id'] == item_id), None)
    if line is None:
        raise ValueError('本队撤离没有该物资来源。')
    consumed, damaged = loss_amounts(ledger, space.event_id, squad_id, item_id)
    loans = []
    for r in ledger.transfer_records.values():
        v = json.loads(r.content_json)
        if (r.reference.event_id == space.event_id and r.reference.version == 1
                and v.get('purpose') == 'loan' and v['item_id'] == item_id
                and squad_id in (v['from_squad'], v['to_squad'])):
            loans.append(asdict(loan_status(ledger, event_id=space.event_id, loan_transfer_id=r.reference.object_id)))
    stock = None
    if point_id:
        from .material_supply_execution import inspect
        rows, stocks, orders = inspect(ledger, spaces)
        current = stocks.get((point_id, item_id))
        if current is None:
            raise ValueError('长期库存调整须先由物资供应组长核实该物资点的实物库存。')
        # inspect applies dispatches to the latest verified stock; do not deduct again.
        stock = current
    refs = set(view.arrival_refs + view.loss_refs + view.transfer_refs + view.withdrawal_refs
               + view.custody_refs + view.supply_refs)
    all_records = {}
    for name in ('flow_records', 'transfer_records', 'withdrawal_records', 'supply_receipts', 'supply_records'):
        all_records.update(getattr(ledger, name))
    all_records.update(space.object_revisions)
    if stock:
        refs.add(stock['record_id'])
    sources = {key: ar.digest(asdict(all_records[key])) for key in sorted(refs)}
    carry = ledger.carry_lines.get(squad_id+'|'+item_id)
    return dict(item_id=item_id, squad_id=squad_id, point_id=point_id,
        catalog_before=dict(carry=asdict(carry) if carry else None,
                            point=asdict(ledger.points[point_id]) if point_id else None),
        inventory=asdict(view), consumed=consumed, damaged=damaged,
        departure=asdict(departure), unreturned=loans, actual_custody=value.get('exceptions', [])
        + value.get('borrowed_custody', []), carry_ceiling=line['carried_out'], stock=stock, sources=sources)


def inspect(space, ledger):
    rows = ar.records(space, KIND)
    all_records = dict(space.object_revisions)
    for name in ('flow_records', 'transfer_records', 'withdrawal_records', 'supply_receipts', 'supply_records'):
        all_records.update(getattr(ledger, name))
    for index, r, v in rows:
        if r.source != SOURCE or set(v) != {'facts', 'lost', 'carry_quantity', 'basis', 'reason'}:
            raise ValueError('核实收尾资料字段或来源不一致。')
        require_leader(ar.prefix(space, index), ledger, event_id=space.event_id, actor=r.actor_id)
        for field in ('basis', 'reason'):
            ar.text(v[field])
        f = v['facts']
        if r.reference.object_id != 'closeout-' + ar.digest([f['squad_id'], f['item_id'], f['point_id']]):
            raise ValueError('核实收尾对象不一致。')
        if number(v['lost']) > number(f['carry_ceiling']) + 1e-9:
            raise ValueError('新增遗失须在撤出量内核实；现场已耗损不要重复登记。')
        if number(v['carry_quantity']) + v['lost'] > number(f['carry_ceiling']) + 1e-9:
            raise ValueError('清单不能装载未归还、留置、移交或已耗损量。')
        number(v['lost'])
        for key, digest in f['sources'].items():
            if key not in all_records or ar.digest(asdict(all_records[key])) != digest:
                raise ValueError('核实收尾的原始来源缺失或变化，不回退旧版。')
    return tuple(rows)


def record(space, ledger, spaces, registry, actor, *, item_id, squad_id, point_id='',
           lost, carry_quantity, basis, reason, expected_facts_digest, expected_version, request_id):
    if registry.events[space.event_id].status != 'closed':
        raise ValueError('须先关闭事件再核实收尾。')
    require_leader(space, ledger, event_id=space.event_id, actor=actor)
    inspect(space, ledger)
    oid = 'closeout-' + ar.digest([squad_id, item_id, point_id])
    old = next((r for _, r, v in ar.records(space, KIND) if r.request_id == request_id), None)
    from .resource_maintenance import standard_line
    if old is None and standard_line(ledger, squad_id, item_id):
        raise ValueError('消防队伍自带标配由外部维护，本系统不再新记标配耗损或据此调整下次固定清单；借还历史继续保留。')
    f = json.loads(old.content_json)['facts'] if old else facts(space, ledger, spaces, item_id, squad_id, point_id)
    if ar.digest(f) != expected_facts_digest:
        raise ValueError('收尾来源已变化，请刷新后重新核实。')
    if number(carry_quantity) + number(lost) > number(f['carry_ceiling']) + 1e-9:
        raise ValueError('清单不能装载未归还、留置、移交或已耗损量。')
    if number(lost) > f['carry_ceiling'] + 1e-9:
        raise ValueError('新增遗失须在撤出量内核实；现场已耗损不要重复登记。')
    r = ar.append(space, kind=KIND, object_id=oid, source=SOURCE, actor=actor,
        values=dict(facts=f, lost=number(lost), carry_quantity=number(carry_quantity),
                    basis=ar.text(basis), reason=ar.text(reason)),
        expected_version=expected_version, request_id=request_id)
    inspect(space, ledger)
    return r


def apply(ledger, spaces, details):
    if set(details) != {'event_id', 'closeout_ref'}:
        raise ValueError('底账调整须引用完整核实收尾记录。')
    space = spaces.get(details['event_id'])
    if space is None:
        raise ValueError('底账调整事件来源缺失。')
    rows = inspect(space, ledger)
    match = next(((r, v) for _, r, v in rows if r.id == details['closeout_ref']), None)
    if match is None:
        raise ValueError('收尾核实记录缺失。')
    r, v = match
    if next(rec for _, rec, val in reversed(rows) if rec.reference.object_id == r.reference.object_id).id != r.id:
        raise ValueError('须使用本对象最新核实版，不回退旧结论。')
    f = v['facts']
    from .resource_maintenance import standard_line
    carry = ledger.carry_lines.get(f['squad_id']+'|'+f['item_id'])
    if standard_line(ledger, f['squad_id'], f['item_id']) and carry.quantity != v['carry_quantity']:
        raise ValueError('不得用本次耗损或旧收尾核实扣减消防队伍下次标配；请由值班员依据外部维护资料更新副本。')
    if ar.dump(facts(space, ledger, spaces, f['item_id'], f['squad_id'], f['point_id'])) != ar.dump(f):
        raise ValueError('耗损、实收、核验或结清来源已变化，须追加核实后调整。')
    ledger.set_carry_line(f['squad_id'], f['item_id'], quantity=v['carry_quantity'])
    if f['point_id']:
        ledger.points[f['point_id']].supplies[f['item_id']] = f['stock']['quantity']
    return r
