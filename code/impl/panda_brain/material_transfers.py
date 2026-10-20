"""Event-scoped transfer journal and quantity projection (P08-06a).

Trusted internal API only. Callers must authenticate actors before passing
their identifiers; no HTTP route or dispatch side effect is provided here.
Arrival baselines remain frozen. A sent amount leaves the source immediately,
stays in transit, and enters the destination only after a receipt report.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .authorization import Action, AuthorizationPolicy
from .event_authorization import event_grants
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes


KIND = 'material_transfer'


def _positive(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError('Transfer quantity must be finite and positive')
    return float(value)


def _arrival(ledger, event_id, squad_id):
    snapshot = ledger.snapshots.get(event_id)
    squad = snapshot.squads.get(squad_id) if snapshot and snapshot.event_id == event_id else None
    if squad is None or not squad.arrival_id or squad_id not in ledger.squads:
        raise ValueError('Both teams need versioned arrivals in this event')
    record = ledger.flow_records.get(squad.arrival_id)
    if (record is None or record.id != squad.arrival_id
            or record.reference != ObjectReference(event_id, 'team_arrival', squad_id, 1)):
        raise ValueError('Team arrival source is missing or mismatched')
    try:
        baseline = json.loads(record.content_json)['carry_baseline']
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('Team arrival baseline is missing') from exc
    if baseline != {item_id: line.qty_registered for item_id, line in squad.lines.items()}:
        raise ValueError('Team arrival baseline disagrees with snapshot')
    return squad


def _history(ledger, event_id, transfer_id):
    records = sorted((record for key, record in ledger.transfer_records.items()
                      if record.reference.event_id == event_id
                      and record.reference.object_id == transfer_id),
                     key=lambda record: record.reference.version)
    if not records:
        return ()
    if [record.reference.version for record in records] != list(range(1, len(records) + 1)):
        raise ValueError('Transfer stages are missing or duplicated')
    first = json.loads(records[0].content_json)
    expected_keys = {'item_id', 'from_squad', 'to_squad', 'quantity'}
    if (records[0].reference.object_kind != KIND or first.get('stage') != 'approved'
            or not expected_keys <= first.keys()
            or first['from_squad'] == first['to_squad']):
        raise ValueError('Transfer approval is incomplete')
    amount = _positive(first['quantity'])
    if amount != first['quantity']:
        raise ValueError('Transfer quantity changed')
    purpose = first.get('purpose', 'transfer')
    return_for = first.get('return_for', '')
    if (purpose not in {'transfer', 'loan', 'return'}
            or (purpose == 'return') != bool(return_for)
            or (return_for and not isinstance(return_for, str))):
        raise ValueError('Transfer loan relation is invalid')
    received = 0.0
    for index, record in enumerate(records):
        content = json.loads(record.content_json)
        if (record.id not in ledger.transfer_records
                or ledger.transfer_records[record.id] != record
                or record.reference.object_kind != KIND or record.data_kind != 'reported'
                or any(content.get(key) != first[key] for key in expected_keys)
                or content.get('purpose', 'transfer') != purpose
                or content.get('return_for', '') != return_for):
            raise ValueError('Transfer stage ownership or source is inconsistent')
        stage = 'approved' if index == 0 else 'sent' if index == 1 else 'received'
        if content.get('stage') != stage:
            raise ValueError('Transfer stage order is invalid')
        if stage == 'received':
            receipt = _positive(content.get('received_quantity'))
            damage = content.get('damaged_quantity', 0)
            if (type(damage) not in {int, float} or not math.isfinite(damage)
                    or damage < 0 or damage > receipt
                    or (purpose != 'return' and damage != 0)):
                raise ValueError('Transfer receipt damage is invalid')
            received += receipt
            if received > amount + 1e-9:
                raise ValueError('Transfer receipts exceed amount sent')
    return tuple(records)


@dataclass(frozen=True)
class TransferBalance:
    event_id: str
    squad_id: str
    item_id: str
    arrival_base: float
    received: float
    sent: float
    losses: float
    available: float


@dataclass(frozen=True)
class LoanStatus:
    event_id: str
    loan_transfer_id: str
    borrowed: float
    return_approved: float
    returned: float
    damaged_on_return: float
    outstanding: float


def loan_status(ledger, *, event_id: str, loan_transfer_id: str) -> LoanStatus:
    """Outstanding stays open until the lending captain receives the return."""
    history = _history(ledger, event_id, loan_transfer_id)
    if not history or json.loads(history[0].content_json).get('purpose') != 'loan':
        raise ValueError('Referenced borrowing transfer is missing')
    borrowed = sum(json.loads(record.content_json)['received_quantity']
                   for record in history[2:]) if len(history) >= 2 else 0.0
    approved = returned = damaged = 0.0
    return_ids = {record.reference.object_id for record in ledger.transfer_records.values()
                  if record.reference.event_id == event_id}
    for return_id in return_ids:
        if return_id == loan_transfer_id:
            continue
        stages = _history(ledger, event_id, return_id)
        first = json.loads(stages[0].content_json)
        if first.get('purpose') != 'return' or first.get('return_for') != loan_transfer_id:
            continue
        approved += first['quantity']
        returned += sum(json.loads(record.content_json)['received_quantity']
                        for record in stages[2:])
        damaged += sum(json.loads(record.content_json).get('damaged_quantity', 0)
                       for record in stages[2:])
    if approved > borrowed + 1e-9 or returned > approved + 1e-9:
        raise ValueError('Return requests exceed received borrowing quantity')
    return LoanStatus(event_id, loan_transfer_id, borrowed, approved, returned,
                      damaged, max(borrowed - returned, 0.0))


def transfer_balance(ledger, *, event_id: str, squad_id: str, item_id: str,
                     include_withdrawal: bool = True) -> TransferBalance:
    """Project one team's book amount; this does not mutate arrival snapshots."""
    squad = _arrival(ledger, event_id, squad_id)
    from .material_gap_evidence import _loss_history

    _, losses_match = _loss_history(ledger, event_id, item_id, ledger.snapshots[event_id])
    if not losses_match:
        raise ValueError('Loss report history disagrees with the current snapshot')
    line = squad.lines.get(item_id)
    base = 0.0 if line is None else (line.qty_usable if line.qty_usable is not None
                                     else line.qty_registered)
    from .material_loss_sources import loss_amounts
    losses = (math.fsum(loss_amounts(ledger, event_id, squad_id, item_id)) if line is None
              else line.consumed + line.damaged + line.missing)
    if (not math.isfinite(base) or not math.isfinite(losses) or base < 0 or losses < 0
            or (line is not None and (line.qty_usable != line.qty_registered
                                      or line.missing != 0))):
        raise ValueError('Arrival and loss quantities are inconsistent')
    from .material_supply_receipts import amounts
    received,_,_=amounts(ledger,event_id,squad_id,item_id)
    sent = 0.0
    transfer_ids = {record.reference.object_id for record in ledger.transfer_records.values()
                    if record.reference.event_id == event_id}
    for transfer_id in transfer_ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] != item_id or len(history) < 2:
            continue
        quantity = first['quantity']
        if first['from_squad'] == squad_id:
            sent += quantity
        if first['to_squad'] == squad_id:
            received += sum(json.loads(record.content_json)['received_quantity']
                            - json.loads(record.content_json).get('damaged_quantity', 0)
                            for record in history[2:])
    available = base + received - sent - losses
    if available < -1e-9:
        raise ValueError('Transfer projection overdraws team inventory')
    if include_withdrawal:
        from .material_withdrawals import withdrawal_record

        departed = withdrawal_record(ledger, event_id=event_id, squad_id=squad_id)
        if departed is not None:
            lines = json.loads(departed.content_json)['lines']
            recorded = next((line for line in lines if line['item_id'] == item_id), None)
            # A team never owning this catalog item need not have a withdrawal
            # row for it. Any arrival or flow still requires its frozen row.
            if ((recorded is None and (line is not None or any((base, received, sent, losses))))
                    or (recorded is not None and not math.isclose(
                        recorded['on_site_before'], available, abs_tol=1e-9))):
                raise ValueError('Withdrawn team inventory disagrees with current material history')
            available = 0.0
    return TransferBalance(event_id, squad_id, item_id, base, received, sent, losses,
                           max(available, 0.0))


def transfer_in_transit(ledger, *, event_id: str, item_id: str) -> float:
    """Sent minus actually received, never counted as on-site available."""
    transfer_ids = {record.reference.object_id for record in ledger.transfer_records.values()
                    if record.reference.event_id == event_id}
    total = 0.0
    for transfer_id in transfer_ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] == item_id and len(history) >= 2:
            total += first['quantity'] - sum(
                json.loads(record.content_json)['received_quantity'] for record in history[2:])
    return max(total, 0.0)


def return_damaged(ledger, *, event_id: str, item_id: str) -> float:
    """Damaged equipment received back is physical stock but not usable stock."""
    total = 0.0
    ids = {record.reference.object_id for record in ledger.transfer_records.values()
           if record.reference.event_id == event_id}
    for transfer_id in ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] == item_id and first.get('purpose') == 'return':
            total += sum(json.loads(record.content_json).get('damaged_quantity', 0)
                         for record in history[2:])
    return total


class MaterialTransferService:
    def __init__(self, workspace, ledger, event_id: str):
        if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
            raise ValueError('Transfer workspace event or audit is invalid')
        self.workspace = workspace
        self.ledger = ledger
        self.event_id = event_id

    def _require_approved_stage(self, history):
        if not history:
            raise ValueError('Transfer has no approval')
        AuthorizationPolicy(event_grants(self.workspace)).require(
            history[0].actor_id, Action.CONFIRM_MATERIAL_TRANSFER,
            event_id=self.event_id,
        )

    def _write(self, transfer_id, version, content, *, expected_version,
               actor_id, source, request_id, occurred_at=''):
        ref = ObjectReference(self.event_id, KIND, transfer_id, version)
        if type(expected_version) is not int or expected_version != version - 1:
            raise ValueError('Transfer expected version is invalid')
        record = ObjectRevision(
            ref, RecordTimes(occurred_at=occurred_at,
                             recorded_at=datetime.now(timezone.utc).isoformat()),
            source, 'reported', actor_id, request_id,
            json.dumps(content, ensure_ascii=False),
        )
        for previous in self.ledger.transfer_records.values():
            if previous.reference.event_id == self.event_id and previous.request_id == request_id:
                if (previous.reference == ref and previous.actor_id == actor_id
                        and previous.source == source and previous.content_json == record.content_json
                        and previous.times.occurred_at == occurred_at):
                    return previous
                raise ValueError('Transfer request ID conflicts with an earlier stage')
        history = _history(self.ledger, self.event_id, transfer_id)
        if len(history) != expected_version:
            raise ValueError('Stale or skipped transfer stage')
        self.ledger.transfer_records[record.id] = record
        return record

    def approve(self, *, transfer_id, item_id, from_squad, to_squad, quantity,
                actor_id, source, request_id, occurred_at='', purpose='transfer',
                return_for=''):
        """Only an audited appointed deputy can confirm a proposed transfer."""
        AuthorizationPolicy(event_grants(self.workspace)).require(
            actor_id, Action.CONFIRM_MATERIAL_TRANSFER, event_id=self.event_id)
        quantity = _positive(quantity)
        if item_id not in self.ledger.items or from_squad == to_squad:
            raise ValueError('Transfer item and distinct teams are required')
        _arrival(self.ledger, self.event_id, from_squad)
        _arrival(self.ledger, self.event_id, to_squad)
        from .material_withdrawals import withdrawal_record

        if (withdrawal_record(self.ledger, event_id=self.event_id, squad_id=from_squad)
                or withdrawal_record(self.ledger, event_id=self.event_id, squad_id=to_squad)):
            raise ValueError('Withdrawn teams cannot start a new material transfer')
        if purpose not in {'transfer', 'loan', 'return'} or (purpose == 'return') != bool(return_for):
            raise ValueError('Borrowing or return purpose is invalid')
        if purpose == 'return':
            loan = _history(self.ledger, self.event_id, return_for)
            if not loan or json.loads(loan[0].content_json).get('purpose') != 'loan':
                raise ValueError('Return must reference a borrowing transfer in this event')
            original = json.loads(loan[0].content_json)
            if (original['item_id'] != item_id or original['from_squad'] != to_squad
                    or original['to_squad'] != from_squad):
                raise ValueError('Return item and teams must reverse the borrowing')
        content = dict(stage='approved', item_id=item_id, from_squad=from_squad,
                       to_squad=to_squad, quantity=quantity, purpose=purpose)
        if return_for:
            content['return_for'] = return_for
        if purpose == 'return':
            status = loan_status(self.ledger, event_id=self.event_id,
                                 loan_transfer_id=return_for)
            existing = _history(self.ledger, self.event_id, transfer_id)
            retry = bool(existing and existing[0].request_id == request_id)
            if not retry and status.return_approved + quantity > status.borrowed + 1e-9:
                raise ValueError('Return approvals exceed received borrowing')
        return self._write(transfer_id, 1, content, expected_version=0,
                           actor_id=actor_id, source=source, request_id=request_id,
                           occurred_at=occurred_at)

    def send(self, *, transfer_id, actor_id, source, request_id,
             expected_version=1, occurred_at=''):
        """A source captain reports actual handover; only now does stock leave."""
        history = _history(self.ledger, self.event_id, transfer_id)
        self._require_approved_stage(history)
        if len(history) != 1:
            if len(history) >= 2 and history[1].request_id == request_id:
                content = json.loads(history[1].content_json)
                return self._write(transfer_id, 2, content, expected_version=expected_version,
                                   actor_id=actor_id, source=source, request_id=request_id,
                                   occurred_at=occurred_at)
            raise ValueError('Transfer needs an approved, unsent stage')
        content = json.loads(history[0].content_json)
        from_squad = content['from_squad']
        if self.ledger.squads[from_squad].leader_phone != actor_id:
            raise PermissionError('Only the source team captain can report handover')
        _arrival(self.ledger, self.event_id, content['to_squad'])
        balance = transfer_balance(self.ledger, event_id=self.event_id,
                                   squad_id=from_squad, item_id=content['item_id'])
        if balance.available + 1e-9 < content['quantity']:
            raise ValueError('Transfer exceeds current source inventory')
        content['stage'] = 'sent'
        return self._write(transfer_id, 2, content, expected_version=expected_version,
                           actor_id=actor_id, source=source, request_id=request_id,
                           occurred_at=occurred_at)

    def receive(self, *, transfer_id, quantity, actor_id, source, request_id,
                expected_version, occurred_at='', damaged_quantity=0):
        """Destination captain reports actual receipt; partial receipts accumulate."""
        history = _history(self.ledger, self.event_id, transfer_id)
        self._require_approved_stage(history)
        if len(history) < 2:
            raise ValueError('Unsent materials cannot be received')
        first = json.loads(history[0].content_json)
        if self.ledger.squads[first['to_squad']].leader_phone != actor_id:
            raise PermissionError('Only the receiving team captain can report receipt')
        quantity = _positive(quantity)
        if (type(damaged_quantity) not in {int, float}
                or not math.isfinite(damaged_quantity) or damaged_quantity < 0
                or damaged_quantity > quantity
                or (first.get('purpose') != 'return' and damaged_quantity != 0)):
            raise ValueError('Only a return receipt may record bounded damage')
        content = dict(first, stage='received', received_quantity=quantity)
        if first.get('purpose') == 'return':
            content['damaged_quantity'] = float(damaged_quantity)
        # Retry with the same request must return the original receipt.
        for previous in history[2:]:
            if previous.request_id == request_id:
                return self._write(transfer_id, previous.reference.version, content,
                                   expected_version=previous.reference.version - 1,
                                   actor_id=actor_id, source=source, request_id=request_id,
                                   occurred_at=occurred_at)
        received = sum(json.loads(record.content_json)['received_quantity'] for record in history[2:])
        if received + quantity > first['quantity'] + 1e-9:
            raise ValueError('Receipt exceeds amount in transit')
        return self._write(transfer_id, len(history) + 1, content,
                           expected_version=expected_version,
                           actor_id=actor_id, source=source, request_id=request_id,
                           occurred_at=occurred_at)


def validate_transfer_links(ledger):
    """Reject malformed persisted stage histories and orphaned team links."""
    transfer_ids = {(record.reference.event_id, record.reference.object_id)
                    for record in ledger.transfer_records.values()}
    for key, record in ledger.transfer_records.items():
        if key != record.id or record.reference.object_kind != KIND:
            raise ValueError('Transfer record identity is invalid')
    for event_id, transfer_id in transfer_ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if first['item_id'] not in ledger.items:
            raise ValueError('Transfer item is missing')
        _arrival(ledger, event_id, first['from_squad'])
        _arrival(ledger, event_id, first['to_squad'])
    for event_id, transfer_id in transfer_ids:
        first = json.loads(_history(ledger, event_id, transfer_id)[0].content_json)
        if first.get('purpose') == 'return':
            loan = _history(ledger, event_id, first['return_for'])
            if not loan or json.loads(loan[0].content_json).get('purpose') != 'loan':
                raise ValueError('Return borrowing link is missing')
            original = json.loads(loan[0].content_json)
            if (original['item_id'] != first['item_id']
                    or original['from_squad'] != first['to_squad']
                    or original['to_squad'] != first['from_squad']):
                raise ValueError('Return borrowing link does not reverse the same item and teams')
            loan_status(ledger, event_id=event_id, loan_transfer_id=first['return_for'])
        for squad_id in (first['from_squad'], first['to_squad']):
            transfer_balance(ledger, event_id=event_id, squad_id=squad_id,
                             item_id=first['item_id'])
