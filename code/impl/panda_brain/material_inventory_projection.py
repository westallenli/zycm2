"""Read-only reported inventory: existing journals, audited sources and conservation.

This projection does not establish demand, independent stock verification, supply
receipts, reservations or a new custody owner. Rear receipts remain withdrawn.
"""
from copy import copy
from dataclasses import dataclass
import json
import math

from .material_transfers import _arrival, _history, transfer_balance, transfer_in_transit, return_damaged
from .material_withdrawals import (WithdrawalException, withdrawal_record,
    validate_withdrawal_links, _require_sources_mirrored, review_withdrawal)


@dataclass(frozen=True)
class InventoryException:
    squad_id: str
    withdrawal_id: str
    item_id: str
    kind: str
    quantity: float
    location: str
    custodian: str


@dataclass(frozen=True)
class EventInventory:
    event_id: str
    item_id: str
    arrival_base: float
    team_available: float
    in_transit: float
    losses: float
    return_damage: float
    carried_out: float
    retained: float
    handed_over: float
    rear_received: float
    rear_damaged: float
    rear_unreceived: float
    arrival_refs: tuple[str, ...]
    loss_refs: tuple[str, ...]
    transfer_refs: tuple[str, ...]
    withdrawal_refs: tuple[str, ...]
    custody_refs: tuple[str, ...]
    exceptions: tuple[InventoryException, ...]
    team_balances: tuple = ()
    consumed: float = 0.0
    damaged: float = 0.0
    supply_dispatched: float = 0.0
    supply_received: float = 0.0
    supply_damaged: float = 0.0
    supply_in_transit: float = 0.0
    supply_refs: tuple[str, ...] = ()

    @property
    def book_available(self):
        # Locations/custodians are reports, not a new inventory owner or proof of dispatchability.
        return None if self.retained or self.handed_over else self.team_available


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError('Inventory quantity must be finite and nonnegative')
    return float(value)


def project(workspace, ledger, *, event_id, item_id):
    """Validate both source directions before publishing any event quantity."""
    try:
        return _project(workspace, ledger, event_id=event_id, item_id=item_id)
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('Inventory source is incomplete or malformed') from error


def _project(workspace, ledger, *, event_id, item_id):
    from .captain_arrivals import ARRIVAL_REPORTED
    from .manual_material_reports import REPORTED as LOSS_REPORTED
    from .manual_material_transfers import RECORDED as TRANSFER_REPORTED
    from .material_withdrawals import REPORTED as WITHDRAWAL_REPORTED
    from .material_gap_evidence import _loss_history
    from .material_custody_settlements import summary as custody_summary

    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('Inventory event or audit source disagrees')
    if item_id not in ledger.items:
        raise ValueError('Inventory material catalog is missing')
    from .radio_material_reports import history as radio_material_history
    radio_material_history(workspace, ledger, event_id)
    from .material_supply_receipts import history as supply_history, order_history
    supply_rows=supply_history(workspace,ledger)
    supply_orders=order_history(ledger,event_id)
    # Existing validators inspect all journal entries; constrain their read to this event.
    scoped = copy(ledger)
    scoped.snapshots = {k: v for k, v in ledger.snapshots.items() if k == event_id}
    for name in ('flow_records', 'transfer_records', 'withdrawal_records','supply_receipts'):
        records = getattr(ledger, name)
        filtered = {k: r for k, r in records.items() if r.reference.event_id == event_id}
        if any(k != r.id for k, r in filtered.items()):
            raise ValueError('Inventory journal key disagrees with record identity')
        setattr(scoped, name, filtered)
    snapshot = scoped.snapshots.get(event_id)
    if snapshot is None or snapshot.event_id != event_id or not snapshot.squads:
        raise ValueError('Inventory arrival snapshot is missing')
    for squad_id in snapshot.squads:
        squad = _arrival(scoped, event_id, squad_id)
        for line in squad.lines.values():
            _number(line.qty_registered)
            _number(line.qty_usable)
            for name in ('consumed', 'damaged', 'missing'):
                _number(getattr(line, name))
        _require_sources_mirrored(workspace, scoped, event_id, squad_id)
    for record in scoped.flow_records.values():
        content = json.loads(record.content_json)
        squad_id = record.reference.object_id if record.reference.object_kind == 'team_arrival' else content['squad_id']
        if (record.reference.object_kind not in {'team_arrival', 'material_loss'}
                or record.data_kind != 'reported' or squad_id not in snapshot.squads):
            raise ValueError('Inventory flow belongs to an unsupported source')
    # Reject a deleted journal record whose event mirror survived, including a missing last stage.
    source_maps = {ARRIVAL_REPORTED: ('flow_record_id', scoped.flow_records),
                   LOSS_REPORTED: ('flow_record_id', scoped.flow_records),
                   TRANSFER_REPORTED: ('transfer_record_id', scoped.transfer_records),
                   WITHDRAWAL_REPORTED: ('withdrawal_record_id', scoped.withdrawal_records)}
    positions = {}
    from . import audited_records as ar
    for index,record,value in ar.records(workspace,'formal_supply_receipt'):
        positions[record.id]=workspace.events[index].sequence
    for entry in workspace.events:
        if entry.kind not in source_maps or entry.payload.get('event_id') != event_id:
            continue
        key, records = source_maps[entry.kind]
        identifier = entry.payload.get(key)
        if identifier not in records or identifier in positions:
            raise ValueError('Inventory mirror is orphaned or duplicated')
        positions[identifier] = entry.sequence
    # Frozen loss sources must be exactly the team's item sources already present
    # in the event audit, including zero-effect approval stages and older versions.
    from .material_loss_sources import sources, loss_amounts
    for record in scoped.flow_records.values():
        value = json.loads(record.content_json)
        if record.reference.object_kind != 'material_loss' or 'inventory_basis' not in value:
            continue
        cut = copy(scoped)
        cut.flow_records = {key: r for key, r in scoped.flow_records.items()
                            if positions[key] < positions[record.id]}
        cut.transfer_records = {key: r for key, r in scoped.transfer_records.items()
                                if positions[key] < positions[record.id]}
        cut.supply_receipts = {key:r for key,r in scoped.supply_receipts.items() if positions[key]<positions[record.id]}
        if value['inventory_basis'] != sources(cut, event_id, value['squad_id'], value['item_id']):
            raise ValueError('Loss quantity basis is not its complete prior event source cut')
    transfers = []
    for identifier in sorted({r.reference.object_id for r in scoped.transfer_records.values()}):
        history = _history(scoped, event_id, identifier)
        first = json.loads(history[0].content_json)
        if (first['from_squad'] not in snapshot.squads or first['to_squad'] not in snapshot.squads
                or first['item_id'] not in ledger.items):
            raise ValueError('Transfer teams or item are outside this event inventory')
        seq = [positions[r.id] for r in history]
        if seq != sorted(seq) or len(set(seq)) != len(seq):
            raise ValueError('Transfer audit stages are out of order')
        for squad_id in (first['from_squad'], first['to_squad']):
            if positions[snapshot.squads[squad_id].arrival_id] >= seq[0]:
                raise ValueError('Transfer approval precedes team arrival')
        if first['item_id'] == item_id:
            transfers.extend(r.id for r in history)
    loss_refs, consistent = _loss_history(scoped, event_id, item_id, snapshot)
    if not consistent:
        raise ValueError('Inventory loss revisions disagree with snapshot')
    validate_withdrawal_links(scoped)
    balances = [transfer_balance(scoped, event_id=event_id, squad_id=squad_id, item_id=item_id)
                for squad_id in snapshot.squads]
    withdrawn = []
    carried = retained = handed = rear_received = rear_damaged = rear_unreceived = 0.0
    custody_refs, exception_sources = [], []
    for squad_id in snapshot.squads:
        departure = withdrawal_record(scoped, event_id=event_id, squad_id=squad_id)
        if departure is None:
            continue
        content = json.loads(departure.content_json)
        exceptions = tuple(WithdrawalException(**value) for value in content['exceptions'])
        exception_sources.extend(InventoryException(squad_id, departure.id, value.item_id,
            value.kind, value.quantity, value.location, value.custodian)
            for value in exceptions if value.item_id == item_id)
        review_withdrawal(workspace, scoped, event_id=event_id, squad_id=squad_id, exceptions=exceptions)
        departure_seq = positions[departure.id]
        # Every frozen source stage must already exist when departure was reported.
        for record in scoped.flow_records.values():
            data = json.loads(record.content_json)
            owner = record.reference.object_id if record.reference.object_kind == 'team_arrival' else data['squad_id']
            if owner == squad_id and positions[record.id] >= departure_seq:
                raise ValueError('Team inventory changed after its departure')
        for record in scoped.transfer_records.values():
            data = json.loads(record.content_json)
            if squad_id in (data['from_squad'], data['to_squad']) and positions[record.id] >= departure_seq:
                raise ValueError('Transfer source follows team departure')
        for record in scoped.supply_receipts.values():
            if json.loads(record.content_json)['squad_id']==squad_id and positions[record.id]>=departure_seq:
                raise ValueError('Supply receipt source follows team departure')
        for line in content['lines']:
            for name in ('on_site_before', 'carried_out', 'retained', 'handed_over'):
                _number(line[name])
            if line['item_id'] == item_id:
                carried += line['carried_out']
                retained += line['retained']
                handed += line['handed_over']
                withdrawn.append(departure.id)
        for entry in content.get('borrowed_custody', ()):
            loan = json.loads(_history(scoped, event_id, entry['loan_transfer_id'])[0].content_json)
            if loan['item_id'] != item_id:
                continue
            current = custody_summary(workspace, scoped, event_id=event_id,
                                      loan_transfer_id=entry['loan_transfer_id'])
            received = current.receipt_summary
            rear_received += received.received_quantity
            rear_damaged += received.damaged_quantity
            rear_unreceived += received.unreceived_quantity
            custody_refs.extend(r.id for r in (*received.records, *current.verifications))
            if current.settlement:
                custody_refs.append(current.settlement.id)
    base = math.fsum(b.arrival_base for b in balances)
    available = math.fsum(b.available for b in balances)
    losses = math.fsum(b.losses for b in balances)
    received_supply=math.fsum(v['received_quantity'] for r,v in supply_rows if v['item_id']==item_id)
    damaged_supply=math.fsum(v['damaged_quantity'] for r,v in supply_rows if v['item_id']==item_id)
    dispatched_supply=math.fsum(v['dispatched'] for v in supply_orders.values() if v['item_id']==item_id)
    supply_transit=dispatched_supply-received_supply
    if supply_transit < -1e-9:raise ValueError('Supply actual receipts exceed dispatch')
    transit = transfer_in_transit(scoped, event_id=event_id, item_id=item_id)+max(supply_transit,0.)
    damaged = return_damaged(scoped, event_id=event_id, item_id=item_id)
    loss_totals = [loss_amounts(scoped, event_id, squad_id, item_id)
                   for squad_id in snapshot.squads]
    consumed_loss = math.fsum(value[0] for value in loss_totals)
    damaged_loss = math.fsum(value[1] for value in loss_totals)
    parts = (available, losses, transit, damaged, carried, retained, handed,damaged_supply)
    if not math.isclose(base+dispatched_supply, math.fsum(parts), abs_tol=1e-9, rel_tol=0):
        raise ValueError('Reported inventory does not conserve the arrival baseline')
    return EventInventory(event_id, item_id, base, available, transit, losses, damaged,
        carried, retained, handed, rear_received, rear_damaged, rear_unreceived,
        tuple(sorted(s.arrival_id for s in snapshot.squads.values())), loss_refs,
        tuple(transfers), tuple(sorted(set(withdrawn))), tuple(sorted(set(custody_refs))),
        tuple(exception_sources), tuple(balances), consumed_loss, damaged_loss,
        dispatched_supply,received_supply,damaged_supply,max(supply_transit,0.),
        tuple(sorted(r.id for r,v in supply_rows if v['item_id']==item_id)))
