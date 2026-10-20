"""Event-bound arrival/loss recording; trusted internal API, no HTTP authority.

The application supplies a verified actor and event. Changes and their revision
records are saved together by MaterialsLedger.save; SQLite is not part of that
transaction. Unknown occurrence times stay unknown.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import math

from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
from .materials import EventMaterialsSnapshot, SquadSnapshot, SnapshotLine, SNAP_CARRIED, SNAP_PARTIAL


class MaterialFlowService:
    def __init__(self, ledger, event_id):
        ObjectReference(event_id, 'team_arrival', 'scope', 1)
        self.ledger = ledger
        self.event_id = event_id

    def _prepare(self, reference, content, *, expected_version, actor_id, source,
                 request_id, occurred_at='', reason=''):
        if reference.event_id != self.event_id:
            raise ValueError('Material record belongs to another event')
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('Expected version must be a nonnegative integer')
        record = ObjectRevision(reference, RecordTimes(occurred_at=occurred_at,
            recorded_at=datetime.now(timezone.utc).isoformat()), source, 'reported',
            actor_id, request_id, json.dumps(dict(content, reason=reason), ensure_ascii=False))
        history = []
        for previous in self.ledger.flow_records.values():
            if previous.reference.event_id != self.event_id:
                continue
            if previous.request_id == request_id:
                previous_content = json.loads(previous.content_json)
                if previous.reference.object_kind == 'team_arrival':
                    previous_content.pop('carry_baseline', None)
                if previous.reference.object_kind == 'material_loss':
                    previous_content.pop('inventory_basis', None)
                    previous_content.pop('inventory_scope', None)
                if (previous.reference == reference and previous.actor_id == actor_id
                        and previous.source == source and previous_content == json.loads(record.content_json)
                        and previous.times.occurred_at == occurred_at):
                    return previous, True
                raise ValueError('Material request ID conflicts with an earlier record')
            if (previous.reference.object_kind, previous.reference.object_id) == (reference.object_kind, reference.object_id):
                history.append(previous)
        current = max(history, key=lambda r: r.reference.version) if history else None
        version = current.reference.version if current else 0
        if expected_version != version or reference.version != version + 1:
            raise ValueError('Stale or skipped material record version')
        if version and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError('Corrections require a reason')
        return record, False

    def record_arrival(self, reference, *, arrived_phones, expected_version,
                       actor_id, source, request_id, occurred_at='', reason=''):
        if reference.object_kind != 'team_arrival':
            raise ValueError('A team-arrival reference is required')
        squad_id = reference.object_id
        if not isinstance(arrived_phones, (tuple, list)) or any(
                not isinstance(p, str) or not p.strip() or p != p.strip() for p in arrived_phones):
            raise ValueError('Personnel must be an explicit list of stable identifiers')
        phones = tuple(sorted(set(arrived_phones)))
        if not phones or any(not isinstance(p, str) or not p.strip() for p in phones):
            raise ValueError('An arrival requires explicitly reported personnel')
        record, retry = self._prepare(reference, {'squad_id': squad_id, 'arrived_phones': phones},
            expected_version=expected_version, actor_id=actor_id, source=source,
            request_id=request_id, occurred_at=occurred_at, reason=reason)
        if retry:
            return record
        from .material_withdrawals import withdrawal_record


        if withdrawal_record(self.ledger, event_id=self.event_id, squad_id=squad_id):
            raise ValueError('Withdrawn team cannot report a new arrival')
        snapshot = deepcopy(self.ledger.snapshots.get(self.event_id)) or EventMaterialsSnapshot(self.event_id)
        if snapshot.event_id != self.event_id:
            raise ValueError('Snapshot event identity mismatch')
        squad = snapshot.squads.get(squad_id)
        if expected_version == 0:
            if squad is not None:
                raise ValueError('Existing snapshot requires an explicit migration, not a second arrival')
            if squad_id not in self.ledger.squads:
                raise ValueError('Unknown squad')
            squad = SquadSnapshot(squad_id=squad_id, arrival_id=record.id)
            for line in self.ledger.carry_list(squad_id):
                if not math.isfinite(line.quantity) or line.quantity < 0:
                    raise ValueError('Carry quantities must be finite and nonnegative')
                squad.lines[line.item_id] = SnapshotLine(item_id=line.item_id,
                    qty_registered=line.quantity, qty_usable=line.quantity, status=SNAP_CARRIED)
        elif squad is None or not squad.arrival_id:
            raise ValueError('Recorded arrival snapshot is missing')
        squad.arrived_phones = phones
        snapshot.squads[squad_id] = squad
        snapshot.updated_at = record.times.recorded_at
        # Freeze the initial inventory in the record, not a pointer to master data.
        if expected_version == 0:
            content = json.loads(record.content_json)
            content['carry_baseline'] = {key: line.qty_registered for key, line in squad.lines.items()}
            from dataclasses import replace
            record = replace(record, content_json=json.dumps(content, ensure_ascii=False))
        self.ledger.flow_records[record.id] = record
        self.ledger.snapshots[self.event_id] = snapshot
        return record

    def record_loss(self, reference, *, squad_id, item_id, kind, amount, expected_version,
                    actor_id, source, request_id, occurred_at='', reason=''):
        if reference.object_kind != 'material_loss' or kind not in {'consumed', 'damaged'}:
            raise ValueError('Only consumption or damage reports are supported')
        if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
            raise ValueError('Loss quantity must be finite and nonnegative')
        content = dict(squad_id=squad_id, item_id=item_id, kind=kind, amount=float(amount))
        record, retry = self._prepare(reference, content, expected_version=expected_version,
            actor_id=actor_id, source=source, request_id=request_id, occurred_at=occurred_at, reason=reason)
        if retry:
            return record
        from .resource_maintenance import standard_line, scope
        kit = standard_line(self.ledger, squad_id, item_id)
        extra = kit and scope(self.ledger, item_id) in {'public', 'extra'}
        if kit and not extra:
            raise ValueError('消防队伍自带标配由外部系统维护，本系统不统计耗损或提出补充；原历史记录保留。')
        from .material_withdrawals import withdrawal_record

        if withdrawal_record(self.ledger, event_id=self.event_id, squad_id=squad_id):
            raise ValueError('Withdrawn team cannot report new on-site loss')
        snapshot = deepcopy(self.ledger.snapshots.get(self.event_id))
        if snapshot is None or snapshot.event_id != self.event_id:
            raise ValueError('Current event arrival snapshot is missing')
        squad = snapshot.squads.get(squad_id)
        if squad is None or not squad.arrival_id:
            raise ValueError('Loss requires a recorded team arrival')
        latest = {}
        for previous in self.ledger.flow_records.values():
            ref = previous.reference
            if ref.event_id == self.event_id and ref.object_kind == 'material_loss':
                if ref.object_id not in latest or latest[ref.object_id].reference.version < ref.version:
                    latest[ref.object_id] = previous
        previous = latest.get(reference.object_id)
        if previous:
            old = json.loads(previous.content_json)
            if (old['squad_id'], old['item_id'], old['kind']) != (squad_id, item_id, kind):
                raise ValueError('A correction cannot change loss ownership or type')
            if kit and old.get('inventory_scope') != 'public_extra':
                raise ValueError('原消防队伍自带标配耗损仅保留历史，不能转换为额外物资耗损。')
        if extra:
            record = replace(record, content_json=json.dumps(
                dict(json.loads(record.content_json), inventory_scope='public_extra'), ensure_ascii=False))
        latest[reference.object_id] = record
        totals = {'consumed': 0.0, 'damaged': 0.0}
        for item in latest.values():
            value = json.loads(item.content_json)
            if (value['squad_id'], value['item_id']) == (squad_id, item_id):
                totals[value['kind']] += value['amount']
        line = squad.lines.get(item_id)
        from .material_transfers import transfer_balance
        from .material_loss_sources import sources, validate_basis

        current = transfer_balance(self.ledger, event_id=self.event_id,
                                   squad_id=squad_id, item_id=item_id)
        if extra:
            if any(r.reference.event_id == self.event_id and json.loads(r.content_json).get('item_id') == item_id
                   and squad_id in (json.loads(r.content_json).get('from_squad'), json.loads(r.content_json).get('to_squad'))
                   for r in self.ledger.transfer_records.values()):
                raise ValueError('该物资含队伍标配调拨，请先区分额外物资保管来源，不能扣减自带标配。')
            from .material_supply_receipts import ledger_history
            actual_extra = sum(v['received_quantity'] - v['damaged_quantity']
                for r,v in ledger_history(self.ledger,self.event_id)
                if v['item_id'] == item_id and v['squad_id'] == squad_id)
            extra_losses = sum(v['amount'] for v in (json.loads(r.content_json) for r in latest.values())
                               if v.get('inventory_scope') == 'public_extra' and v['squad_id'] == squad_id and v['item_id'] == item_id)
            if extra_losses > actual_extra + 1e-9:
                raise ValueError('额外物资耗损超过具名实收来源；不得扣减消防队伍自带标配。')
        if current.available + current.losses - sum(totals.values()) < -1e-9:
            raise ValueError('Reported loss exceeds inventory after actual transfers')
        content = json.loads(record.content_json)
        content['inventory_basis'] = sources(self.ledger, self.event_id, squad_id, item_id)
        record = replace(record, content_json=json.dumps(content, ensure_ascii=False))
        validate_basis(self.ledger, record)
        if line is not None:
            line.consumed, line.damaged = totals['consumed'], totals['damaged']
            line.status = SNAP_PARTIAL if sum(totals.values()) else SNAP_CARRIED
        snapshot.updated_at = record.times.recorded_at
        self.ledger.flow_records[record.id] = record
        self.ledger.snapshots[self.event_id] = snapshot
        return record


def validate_arrival_links(ledger):
    """Refuse missing/mismatched arrival links on load, not an integrity signature."""
    for event_id, snapshot in ledger.snapshots.items():
        for squad_id, squad in snapshot.squads.items():
            if not squad.arrival_id:
                continue
            initial = ledger.flow_records.get(squad.arrival_id)
            if (initial is None or initial.reference != ObjectReference(event_id, 'team_arrival', squad_id, 1)
                    or snapshot.event_id != event_id or squad.squad_id != squad_id):
                raise ValueError('Arrival snapshot and material history disagree')
    for record in ledger.flow_records.values():
        ref = record.reference
        value = json.loads(record.content_json)
        snapshot = ledger.snapshots.get(ref.event_id)
        squad = snapshot.squads.get(value.get('squad_id')) if snapshot else None
        if ref.object_kind not in {'team_arrival', 'material_loss'} or squad is None or not squad.arrival_id:
            raise ValueError('Material record has no corresponding team arrival')
        if ref.object_kind == 'material_loss' and 'inventory_basis' in value:
            from .material_loss_sources import validate_basis
            validate_basis(ledger, record)
