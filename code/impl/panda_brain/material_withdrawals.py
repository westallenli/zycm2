"""Team material withdrawal drafts and named, audited departure reports."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .callouts import callout_state
from .fire_events import ONBOARDABLE_STATUSES
from .material_transfers import _arrival, _history, loan_status, transfer_balance
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes


KIND = 'team_material_withdrawal'
REPORTED = 'TeamMaterialWithdrawalReported'
SOURCE_PREFIX = '人工撤离物资报告：'


@dataclass(frozen=True)
class WithdrawalException:
    item_id: str
    quantity: float
    kind: str  # retained or handed_over
    location: str
    custodian: str


@dataclass(frozen=True)
class BorrowedCustody:
    loan_transfer_id: str
    quantity: float
    location: str
    custodian: str


@dataclass(frozen=True)
class WithdrawalLine:
    item_id: str
    on_site_before: float
    carried_out: float
    retained: float
    handed_over: float


@dataclass(frozen=True)
class WithdrawalDraft:
    event_id: str
    squad_id: str
    lines: tuple[WithdrawalLine, ...]
    exceptions: tuple[WithdrawalException, ...]
    unresolved_transfers: tuple[str, ...]
    open_loans: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class SettledLenderLoan:
    loan_transfer_id: str
    quantity: float
    settlement_id: str


@dataclass(frozen=True)
class WithdrawalReview:
    draft: WithdrawalDraft
    settled_lender_loans: tuple[SettledLenderLoan, ...]
    unsettled_lender_loans: tuple[str, ...]
    borrowed_loans: tuple[tuple[str, float], ...]
    existing: ObjectRevision | None
    can_report: bool


def project_withdrawal(ledger, *, event_id: str, squad_id: str,
                       exceptions: tuple[WithdrawalException, ...] = ()) -> WithdrawalDraft:
    """Partition currently available stock; this is not a withdrawal report.

    Outstanding loans and transfer stages remain separate facts. The caller must
    not apply these projected quantities to event stock until an authenticated
    team departure and custody report is persisted and audited.
    """
    squad = _arrival(ledger, event_id, squad_id)
    if not isinstance(exceptions, (tuple, list)):
        raise ValueError('Withdrawal exceptions must be an explicit sequence')
    exceptions = tuple(exceptions)
    by_item: dict[str, dict[str, float]] = {}
    seen = set()
    for entry in exceptions:
        if not isinstance(entry, WithdrawalException):
            raise ValueError('Withdrawal exception entry is invalid')
        if (entry.kind not in {'retained', 'handed_over'}
                or entry.item_id not in ledger.items
                or type(entry.quantity) not in {int, float}
                or not math.isfinite(entry.quantity) or entry.quantity <= 0
                or not isinstance(entry.location, str) or not entry.location.strip()
                or entry.location != entry.location.strip()
                or not isinstance(entry.custodian, str) or not entry.custodian.strip()
                or entry.custodian != entry.custodian.strip()):
            raise ValueError('Withdrawal exception needs item, quantity, location and custodian')
        key = (entry.item_id, entry.kind, entry.location, entry.custodian)
        if key in seen:
            raise ValueError('Duplicate withdrawal exception must be consolidated')
        seen.add(key)
        totals = by_item.setdefault(entry.item_id, {'retained': 0.0, 'handed_over': 0.0})
        totals[entry.kind] += entry.quantity

    item_ids = set(squad.lines) | set(by_item)
    from .material_supply_receipts import ledger_history
    item_ids.update(v['item_id'] for r,v in ledger_history(ledger,event_id) if v['squad_id']==squad_id)
    transfer_ids = {record.reference.object_id for record in ledger.transfer_records.values()
                    if record.reference.event_id == event_id}
    histories = {}
    for transfer_id in transfer_ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if squad_id not in {first['from_squad'], first['to_squad']}:
            continue
        histories[transfer_id] = history
        item_ids.add(first['item_id'])

    lines = []
    for item_id in sorted(item_ids):
        if item_id not in ledger.items:
            raise ValueError('Withdrawal item is missing from the material catalog')
        available = transfer_balance(ledger, event_id=event_id,
                                     squad_id=squad_id, item_id=item_id,
                                     include_withdrawal=False).available
        amounts = by_item.get(item_id, {'retained': 0.0, 'handed_over': 0.0})
        excepted = amounts['retained'] + amounts['handed_over']
        if excepted > available + 1e-9:
            raise ValueError('Withdrawal exceptions exceed current on-site quantity')
        lines.append(WithdrawalLine(
            item_id, available, max(available - excepted, 0.0),
            amounts['retained'], amounts['handed_over'],
        ))

    unresolved = []
    open_loans = []
    for transfer_id, history in sorted(histories.items()):
        first = json.loads(history[0].content_json)
        received = sum(json.loads(record.content_json)['received_quantity']
                       for record in history[2:])
        if len(history) == 1 or first['quantity'] - received > 1e-9:
            unresolved.append(transfer_id)
        if first.get('purpose') == 'loan':
            status = loan_status(ledger, event_id=event_id,
                                 loan_transfer_id=transfer_id)
            if status.outstanding > 1e-9:
                open_loans.append((transfer_id, status.outstanding))
    return WithdrawalDraft(event_id, squad_id, tuple(lines), exceptions,
                           tuple(unresolved), tuple(open_loans))


def withdrawal_record(ledger, *, event_id: str, squad_id: str):
    records = [record for record in ledger.withdrawal_records.values()
               if record.reference.event_id == event_id
               and record.reference.object_id == squad_id]
    if len(records) > 1:
        raise ValueError('Team withdrawal is duplicated')
    if not records:
        return None
    record = records[0]
    if (record.id not in ledger.withdrawal_records
            or ledger.withdrawal_records[record.id] != record
            or record.reference != ObjectReference(event_id, KIND, squad_id, 1)):
        raise ValueError('Team withdrawal record identity is invalid')
    return record


def _mirror(workspace, record, *, repair):
    payload = {'event_id': record.reference.event_id,
               'withdrawal_record_id': record.id,
               'record': asdict(record), 'data_kind': 'reported'}
    matches = [entry for entry in workspace.events
               if entry.kind == REPORTED
               and entry.payload.get('withdrawal_record_id') == record.id]
    if matches:
        if len(matches) != 1 or matches[0].payload != payload:
            raise ValueError('Withdrawal record and event mirror disagree')
    elif repair:
        workspace.append_event(REPORTED, **payload)
    else:
        raise ValueError('Withdrawal event mirror is missing')


def _require_sources_mirrored(workspace, ledger, event_id, squad_id):
    """A departure report must not promote unaudited ledger stages to fact."""
    from .captain_arrivals import ARRIVAL_REPORTED
    from .manual_material_reports import REPORTED as LOSS_REPORTED
    from .manual_material_transfers import _require_mirror as require_transfer_mirror
    from .material_supply_receipts import history as supply_history
    supply_history(workspace,ledger)

    for record in ledger.flow_records.values():
        ref = record.reference
        if ref.event_id != event_id:
            continue
        content = json.loads(record.content_json)
        if ref.object_kind == 'team_arrival' and ref.object_id == squad_id:
            expected = {'event_id': event_id, 'squad_id': squad_id,
                        'actor': record.actor_id,
                        'mode': 'initial' if ref.version == 1 else 'supplement',
                        'flow_record_id': record.id,
                        'arrived_phones': content['arrived_phones'],
                        'data_kind': 'reported'}
            kind = ARRIVAL_REPORTED
        elif ref.object_kind == 'material_loss' and content.get('squad_id') == squad_id:
            expected = {'event_id': event_id, 'flow_record_id': record.id,
                        'record': asdict(record), 'data_kind': 'reported'}
            kind = LOSS_REPORTED
        else:
            continue
        matches = [entry for entry in workspace.events
                   if entry.kind == kind
                   and entry.payload.get('flow_record_id') == record.id]
        if len(matches) != 1 or matches[0].payload != expected:
            raise ValueError('Withdrawal source record lacks a matching event audit mirror')

    transfer_ids = {record.reference.object_id for record in ledger.transfer_records.values()
                    if record.reference.event_id == event_id}
    for transfer_id in transfer_ids:
        history = _history(ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        if squad_id not in {first['from_squad'], first['to_squad']}:
            continue
        for index, record in enumerate(history):
            stage = 'approved' if index == 0 else 'sent' if index == 1 else 'received'
            require_transfer_mirror(workspace, record, stage)


def _validate_settled_lender_loans(ledger, draft, entries):
    """Ledger-only shape/quantity check; workspace provenance is checked in review."""
    if not isinstance(entries, (tuple, list)):
        raise ValueError('Settled lending references must be an explicit sequence')
    outstanding = dict(draft.open_loans)
    seen = set()
    for entry in entries:
        if (not isinstance(entry, SettledLenderLoan)
                or entry.loan_transfer_id not in outstanding or entry.loan_transfer_id in seen
                or type(entry.quantity) not in {int, float} or not math.isfinite(entry.quantity)
                or not math.isclose(entry.quantity, outstanding[entry.loan_transfer_id], abs_tol=1e-9, rel_tol=0)
                or not isinstance(entry.settlement_id, str) or not entry.settlement_id.strip()
                or entry.settlement_id != entry.settlement_id.strip()):
            raise ValueError('Settled lending reference or historical quantity is invalid')
        first = json.loads(_history(ledger, draft.event_id, entry.loan_transfer_id)[0].content_json)
        if first['from_squad'] != draft.squad_id:
            raise ValueError('Only a lending team can reference its settled loan')
        seen.add(entry.loan_transfer_id)
    return tuple(entries)


def _validate_borrowed_custody(ledger, draft, entries, settled_lender_loans=()):
    """Keep open loans as liabilities while the borrowing team leaves with them."""
    if not isinstance(entries, (tuple, list)):
        raise ValueError('Borrowed custody must be an explicit sequence')
    entries = tuple(entries)
    settled = _validate_settled_lender_loans(ledger, draft, settled_lender_loans)
    settled_ids = {entry.loan_transfer_id for entry in settled}
    outstanding = {key: amount for key, amount in draft.open_loans if key not in settled_ids}
    if len(entries) != len(outstanding):
        raise ValueError('Each open loan needs one explicit custody report')
    covered = set()
    carried_by_item = {}
    for entry in entries:
        if (not isinstance(entry, BorrowedCustody)
                or not isinstance(entry.loan_transfer_id, str)
                or entry.loan_transfer_id not in outstanding
                or entry.loan_transfer_id in covered
                or type(entry.quantity) not in {int, float}
                or not math.isfinite(entry.quantity)
                or not math.isclose(entry.quantity,
                                    outstanding[entry.loan_transfer_id],
                                    rel_tol=0, abs_tol=1e-9)
                or not isinstance(entry.location, str) or not entry.location.strip()
                or entry.location != entry.location.strip()
                or not isinstance(entry.custodian, str) or not entry.custodian.strip()
                or entry.custodian != entry.custodian.strip()):
            raise ValueError('Open loan custody needs exact quantity, location and custodian')
        first = json.loads(_history(ledger, draft.event_id,
                                    entry.loan_transfer_id)[0].content_json)
        if first['to_squad'] != draft.squad_id:
            raise ValueError('Only the borrowing team may report custody on departure')
        carried_by_item[first['item_id']] = (
            carried_by_item.get(first['item_id'], 0.0) + entry.quantity)
        covered.add(entry.loan_transfer_id)
    for item_id, amount in carried_by_item.items():
        line = next((line for line in draft.lines if line.item_id == item_id), None)
        if line is None or line.carried_out + 1e-9 < amount:
            raise ValueError('Borrowed equipment must remain in carried-out quantity')
    return entries


def require_captain(workspace, ledger, registry, *, event_id, squad_id, actor):
    selected = callout_state(workspace, event_id).get(squad_id)
    squad = ledger.squads.get(squad_id)
    person = registry.library.get(actor)
    if (selected is None or squad is None or person is None
            or selected.leader_phone != actor or squad.leader_phone != actor
            or not person.is_leader):
        raise PermissionError('Only the currently selected team captain may report departure')


def review_withdrawal(workspace, ledger, *, event_id, squad_id, exceptions=(),
                      _repair_original_mirror=False):
    """Recheck each lender settlement without rewriting the historical draft."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('Withdrawal event or audit is invalid')
    validate_withdrawal_links(ledger)
    _require_sources_mirrored(workspace, ledger, event_id, squad_id)
    draft = project_withdrawal(ledger, event_id=event_id, squad_id=squad_id, exceptions=exceptions)
    settled, unsettled, borrowed = [], [], []
    from .material_custody_settlements import summary as settlement_summary
    for loan_id, amount in draft.open_loans:
        first = json.loads(_history(ledger, event_id, loan_id)[0].content_json)
        if first['to_squad'] == squad_id:
            borrowed.append((loan_id, amount))
            continue
        if withdrawal_record(ledger, event_id=event_id, squad_id=first['to_squad']) is None:
            unsettled.append(loan_id)
            continue
        current = settlement_summary(workspace, ledger, event_id=event_id, loan_transfer_id=loan_id)
        if current.settled:
            settled.append(SettledLenderLoan(loan_id, amount, current.settlement.id))
        else:
            unsettled.append(loan_id)
    existing = withdrawal_record(ledger, event_id=event_id, squad_id=squad_id)
    if existing is not None:
        frozen = json.loads(existing.content_json).get('settled_lender_loans', ())
        _mirror(workspace, existing, repair=_repair_original_mirror and not frozen)
        if frozen != [asdict(entry) for entry in settled] and (frozen or settled):
            raise ValueError('Withdrawal settlement references no longer match verified sources')
        departure_seq = next(e.sequence for e in workspace.events
                             if e.kind == REPORTED and e.payload.get('withdrawal_record_id') == existing.id)
        for entry in settled:
            settlement_seq = next(e.sequence for e in workspace.events
                                  if e.kind == 'ObjectRevisionRecorded'
                                  and e.payload.get('revision_id') == entry.settlement_id)
            if settlement_seq >= departure_seq:
                raise ValueError('Settlement must precede the lending team departure report')
    return WithdrawalReview(draft, tuple(settled), tuple(unsettled), tuple(borrowed), existing,
                            not existing and not unsettled and not draft.unresolved_transfers)


def report_withdrawal(workspace, ledger, registry, *, token, resolve_subject,
                      event_id: str, squad_id: str, exceptions=(), borrowed_custody=(),
                      evidence: str, request_id: str, occurred_at: str = ''):
    """Record a named captain's departure report and audit mirror on copies."""
    actor = resolve_subject(token)
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('Withdrawal event or audit is invalid')
    event = registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('Event no longer accepts team material reports')
    require_captain(workspace, ledger, registry, event_id=event_id, squad_id=squad_id, actor=actor)
    if (not isinstance(evidence, str) or not evidence.strip()
            or evidence != evidence.strip() or not isinstance(request_id, str)
            or not request_id.strip()):
        raise ValueError('Withdrawal needs an original basis and request ID')
    review = review_withdrawal(workspace, ledger, event_id=event_id, squad_id=squad_id,
                              exceptions=exceptions, _repair_original_mirror=True)
    draft = review.draft
    if draft.unresolved_transfers:
        raise ValueError('Settle open transfers before team withdrawal')
    if review.unsettled_lender_loans:
        raise ValueError('Lending team loans need full receipt, verification and explicit settlement')
    custody = _validate_borrowed_custody(ledger, draft, borrowed_custody, review.settled_lender_loans)
    reference = ObjectReference(event_id, KIND, squad_id, 1)
    content = asdict(draft)
    if custody:
        content['borrowed_custody'] = [asdict(entry) for entry in custody]
    if review.settled_lender_loans:
        content['settled_lender_loans'] = [asdict(entry) for entry in review.settled_lender_loans]
    content_json = json.dumps(content, ensure_ascii=False)
    source = SOURCE_PREFIX + evidence
    existing = withdrawal_record(ledger, event_id=event_id, squad_id=squad_id)
    if existing is not None:
        if (existing.request_id != request_id or existing.actor_id != actor
                or existing.source != source
                or json.loads(existing.content_json) != json.loads(content_json)
                or existing.times.occurred_at != occurred_at):
            raise ValueError('Team withdrawal already has a different report')
        _mirror(workspace, existing, repair=True)
        return existing
    record = ObjectRevision(
        reference, RecordTimes(occurred_at=occurred_at,
                               recorded_at=datetime.now(timezone.utc).isoformat()),
        source, 'reported', actor, request_id, content_json,
    )
    ledger.withdrawal_records[record.id] = record
    _mirror(workspace, record, repair=True)
    return record


def validate_withdrawal_links(ledger):
    """Reject unsupported or changed withdrawal snapshots on JSON reload."""
    for key, record in ledger.withdrawal_records.items():
        ref = record.reference
        if (key != record.id or ref.object_kind != KIND or ref.version != 1
                or record.data_kind != 'reported'
                or not record.source.startswith(SOURCE_PREFIX)):
            raise ValueError('Withdrawal record identity or source is invalid')
        if withdrawal_record(ledger, event_id=ref.event_id, squad_id=ref.object_id) != record:
            raise ValueError('Withdrawal record is duplicated')
        try:
            content = json.loads(record.content_json)
            exceptions = tuple(WithdrawalException(**value)
                               for value in content['exceptions'])
            custody = tuple(BorrowedCustody(**value)
                            for value in content.get('borrowed_custody', ()))
            settled = tuple(SettledLenderLoan(**value)
                            for value in content.get('settled_lender_loans', ()))
            draft = project_withdrawal(
                ledger, event_id=ref.event_id, squad_id=ref.object_id,
                exceptions=exceptions,
            )
            if (draft.unresolved_transfers
                    or _validate_borrowed_custody(ledger, draft, custody, settled) != custody
                    or {key: value for key, value in content.items()
                        if key not in {'borrowed_custody', 'settled_lender_loans'}} != json.loads(json.dumps(asdict(draft)))
                    or (bool(custody) != ('borrowed_custody' in content))
                    or (bool(settled) != ('settled_lender_loans' in content))
                    or (settled and content['settled_lender_loans'] != [asdict(value) for value in settled])
                    or (custody and content['borrowed_custody'] != [asdict(value) for value in custody])):
                raise ValueError('Withdrawal no longer matches event material history')
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Withdrawal content is incomplete') from exc
