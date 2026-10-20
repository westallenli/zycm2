"""Frozen quantity sources for arrival or actually received transfer items.

Ledger checks validate source identities and quantities. Event audit order is
checked separately by the inventory projection; timestamps are not ordering.
"""
import json
import math
from copy import copy


def loss_items(ledger, event_id, squad_id):
    """Original arrival items plus good receipts; approvals and transit add none."""
    from .material_transfers import _arrival, _history
    arrival = _arrival(ledger, event_id, squad_id)
    items = set(arrival.lines)
    from .material_supply_receipts import ledger_history
    for record,value in ledger_history(ledger,event_id):
        if value['squad_id']==squad_id and value['received_quantity']>value['damaged_quantity']:
            items.add(value['item_id'])
    for identifier in {r.reference.object_id for r in ledger.transfer_records.values()
                       if r.reference.event_id == event_id}:
        history = _history(ledger, event_id, identifier)
        first = json.loads(history[0].content_json)
        if first['to_squad'] == squad_id and any(
                json.loads(r.content_json)['received_quantity']
                - json.loads(r.content_json).get('damaged_quantity', 0) > 0 for r in history[2:]):
            items.add(first['item_id'])
    if any(item not in ledger.items for item in items):
        raise ValueError('Loss item is missing from the material catalog')
    return tuple(sorted(items))


def loss_amounts(ledger, event_id, squad_id, item_id):
    """Latest report totals; callers validate the complete loss history first."""
    latest = {}
    for record in ledger.flow_records.values():
        ref = record.reference
        if ref.event_id != event_id or ref.object_kind != 'material_loss':
            continue
        content = json.loads(record.content_json)
        if (content['squad_id'], content['item_id']) != (squad_id, item_id):
            continue
        old = latest.get(ref.object_id)
        if old is None or old.reference.version < ref.version:
            latest[ref.object_id] = record
    values = [json.loads(r.content_json) for r in latest.values()]
    return tuple(math.fsum(v['amount'] for v in values if v['kind'] == kind)
                 for kind in ('consumed', 'damaged'))


def sources(ledger, event_id, squad_id, item_id):
    from .material_transfers import _arrival, _history
    arrival = _arrival(ledger, event_id, squad_id)
    if item_id not in loss_items(ledger, event_id, squad_id):
        raise ValueError('Loss item has no arrival or good actual receipt source')
    transfers = []
    for transfer_id in sorted({r.reference.object_id for r in ledger.transfer_records.values()
                               if r.reference.event_id == event_id}):
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] == item_id and squad_id in (first['from_squad'], first['to_squad']):
            transfers.extend(r.id for r in history)
    losses = []
    for key, record in ledger.flow_records.items():
        ref = record.reference
        if ref.event_id != event_id or ref.object_kind != 'material_loss':
            continue
        value = json.loads(record.content_json)
        if (value['squad_id'], value['item_id']) == (squad_id, item_id):
            if key != record.id:
                raise ValueError('Loss source key disagrees with its identity')
            losses.append(record.id)
    result=dict(arrival_ref=arrival.arrival_id, transfer_refs=sorted(transfers), loss_refs=sorted(losses))
    from .material_supply_receipts import ledger_history
    supply_refs=sorted(r.id for r,v in ledger_history(ledger,event_id,historical=True) if v['item_id']==item_id)
    if supply_refs:result['supply_refs']=supply_refs
    return result


def transfer_amounts(ledger, event_id, squad_id, item_id):
    from .material_transfers import _history
    received, sent = [], []
    for transfer_id in sorted({r.reference.object_id for r in ledger.transfer_records.values()
                               if r.reference.event_id == event_id}):
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] != item_id or len(history) < 2:
            continue
        if first['from_squad'] == squad_id:
            sent.append(first['quantity'])
        if first['to_squad'] == squad_id:
            received.extend(json.loads(r.content_json)['received_quantity']
                            - json.loads(r.content_json).get('damaged_quantity', 0)
                            for r in history[2:])
    from .material_supply_receipts import amounts
    good,_,_=amounts(ledger,event_id,squad_id,item_id)
    return math.fsum(received)+good, math.fsum(sent)


def validate_basis(ledger, record):
    """A new revision may replace only its own quantity at its frozen source cut."""
    from .material_transfers import _arrival
    value = json.loads(record.content_json)
    amount = value.get('amount')
    if (record.reference.object_kind != 'material_loss' or record.data_kind != 'reported'
            or value.get('kind') not in {'consumed', 'damaged'}
            or type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0):
        raise ValueError('Loss report quantity or identity is invalid')
    basis = value['inventory_basis']
    event_id, squad_id, item_id = record.reference.event_id, value['squad_id'], value['item_id']
    arrival = _arrival(ledger, event_id, squad_id)
    if (not isinstance(basis, dict) or set(basis) not in ({'arrival_ref', 'transfer_refs', 'loss_refs'},{'arrival_ref', 'transfer_refs', 'loss_refs','supply_refs'})
            or basis['arrival_ref'] != arrival.arrival_id or item_id not in ledger.items):
        raise ValueError('Loss inventory basis is invalid')
    scoped = copy(ledger)
    scoped.transfer_records = {}
    scoped.supply_receipts = {}
    refs=basis.get('supply_refs',[])
    if not isinstance(refs,list) or any(not isinstance(key,str) for key in refs) or refs!=sorted(set(refs)):
        raise ValueError('Supply receipt basis references must be unique source identifiers')
    for key in refs:
        source=ledger.supply_receipts.get(key)
        if source is None or source.id!=key or source.reference.event_id!=event_id or json.loads(source.content_json)['item_id']!=item_id:
            raise ValueError('Loss supply receipt basis is missing or belongs elsewhere')
        scoped.supply_receipts[key]=source
    latest, versions = {}, {}
    for field, records in (('transfer_refs', ledger.transfer_records), ('loss_refs', ledger.flow_records)):
        refs = basis[field]
        if (not isinstance(refs, list) or any(not isinstance(key, str) for key in refs)
                or refs != sorted(set(refs)) or record.id in refs):
            raise ValueError('Loss basis references must be unique source identifiers')
        for key in refs:
            source = records.get(key)
            if source is None or source.id != key or source.reference.event_id != event_id or source.data_kind != 'reported':
                raise ValueError('Loss basis source is missing or belongs elsewhere')
            content = json.loads(source.content_json)
            if field == 'transfer_refs':
                if content['item_id'] != item_id or squad_id not in (content['from_squad'], content['to_squad']):
                    raise ValueError('Loss basis transfer belongs elsewhere')
                scoped.transfer_records[key] = source
            else:
                ref = source.reference
                amount = content.get('amount')
                if (ref.object_kind != 'material_loss' or (content['squad_id'], content['item_id']) != (squad_id, item_id)
                        or content['kind'] not in {'consumed', 'damaged'}
                        or type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0):
                    raise ValueError('Loss basis report is invalid')
                versions.setdefault(ref.object_id, []).append(ref.version)
                previous = latest.get(ref.object_id)
                if previous is None or previous.reference.version < ref.version:
                    latest[ref.object_id] = source
    for identifier, sequence in versions.items():
        if sorted(sequence) != list(range(1, max(sequence)+1)):
            raise ValueError('Loss basis report revisions are incomplete')
    prior = latest.get(record.reference.object_id)
    if (prior.reference.version if prior else 0) != record.reference.version - 1:
        raise ValueError('Loss basis does not include its preceding revision')
    received, sent = transfer_amounts(scoped, event_id, squad_id, item_id)
    line = arrival.lines.get(item_id)
    if line is None and received <= 0:
        raise ValueError('Loss outside arrival list requires a frozen good receipt')
    amounts = [json.loads(r.content_json)['amount'] for r in latest.values()]
    previous_amount = json.loads(prior.content_json)['amount'] if prior else 0
    before = (line.qty_registered if line else 0) + received - sent - math.fsum(amounts)
    if before < -1e-9 or value['amount'] - previous_amount > before + 1e-9:
        raise ValueError('Loss revision exceeds inventory at its frozen source cut')
