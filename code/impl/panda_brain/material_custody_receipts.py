"""Named receipts after borrower departure; receipt never settles loan liability."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math

from . import material_closeout_corrections as corrections
from .fire_events import ONBOARDABLE_STATUSES
from .material_transfers import _history, loan_status
from .material_withdrawals import (withdrawal_record, validate_withdrawal_links,
                                   _mirror, _require_sources_mirrored)
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
from .temporary_group_readiness import leader_state

KIND = 'loan_custody_receipt'
GROUP = 'log-material'
SOURCE = '人工撤离后装备实收：'


@dataclass(frozen=True)
class CustodyReceiptSummary:
    event_id: str
    loan_transfer_id: str
    withdrawal_id: str
    item_id: str
    departed_quantity: float
    received_quantity: float
    damaged_quantity: float
    unreceived_quantity: float
    version: int
    records: tuple[ObjectRevision, ...]
    effective_records: tuple[ObjectRevision, ...]
    conclusion: str = '实收报告待核验；借用业务责任未结清，未更新长期底账或现场可用量'


def require_receiver(workspace, ledger, *, event_id, actor):
    if leader_state(workspace, event_id, ledger=ledger).get(GROUP) != actor:
        raise PermissionError('仅本事件物资供应组现任具名组长可办理撤离后装备接收。')


def require_closeout_event(registry, event_id):
    """Approved existing material receipts/decisions only; no combat reopening."""
    event = registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES | {'closed'}:
        raise ValueError('当前事件不接受既有物资接收、核验或结清。')


def _number(value, *, positive=False):
    if (type(value) not in {float, int} or not math.isfinite(value)
            or value < 0 or positive and value <= 0):
        raise ValueError('实收须为有限正数，损坏须为有限非负数。')
    return float(value)


def _text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('须填写不带首尾空白的原始依据、位置、保管对象和请求号。')
    return value


def summary(workspace, ledger, *, event_id, loan_transfer_id):
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('接收记录与事件或审计链不一致。')
    loan = _history(ledger, event_id, loan_transfer_id)
    if not loan or json.loads(loan[0].content_json).get('purpose') != 'loan':
        raise ValueError('须引用本事件原借用流水。')
    first = json.loads(loan[0].content_json)
    departure = withdrawal_record(ledger, event_id=event_id,
                                  squad_id=first['to_squad'])
    if departure is None:
        raise ValueError('借入队伍尚未报告撤离，沿用原事件内归还流程。')
    validate_withdrawal_links(ledger)
    _require_sources_mirrored(workspace, ledger, event_id, first['to_squad'])
    _mirror(workspace, departure, repair=False)
    custody = next((row for row in json.loads(departure.content_json).get('borrowed_custody', ())
                    if row['loan_transfer_id'] == loan_transfer_id), None)
    if custody is None:
        raise ValueError('撤离记录缺少该笔未归还装备的保管报告。')
    outstanding = loan_status(ledger, event_id=event_id,
                              loan_transfer_id=loan_transfer_id).outstanding
    if not math.isclose(custody['quantity'], outstanding, rel_tol=0, abs_tol=1e-9):
        raise ValueError('撤离保管数量与原借用未归还数量不一致。')
    records = sorted((r for r in workspace.object_revisions.values()
                      if r.reference.object_kind == KIND
                      and r.reference.object_id == loan_transfer_id),
                     key=lambda r: r.reference.version)
    mirrors = [entry.payload for entry in workspace.events
               if entry.kind == 'ObjectRevisionRecorded'
               and entry.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND
               and entry.payload['revision']['reference'].get('object_id') == loan_transfer_id]
    if len(records) != len(mirrors):
        raise ValueError('接收记录与审计镜像数量不一致。')
    received = damaged = 0.0
    requests = set()
    prefix_rows = []
    for version, row in enumerate(records, 1):
        content = json.loads(row.content_json)
        if (row.reference != ObjectReference(event_id, KIND, loan_transfer_id, version)
                or workspace.object_revisions.get(row.id) != row
                or row.data_kind != 'reported' or not row.source.startswith(SOURCE)
                or not row.source[len(SOURCE):].strip()
                or row.request_id in requests
                or mirrors.count({'revision_id': row.id, 'revision': asdict(row)}) != 1
                or set(content) - corrections.FIELDS != {'withdrawal_id', 'item_id', 'receiver_group', 'quantity',
                                    'damaged_quantity', 'location', 'custodian'}
                or content['withdrawal_id'] != departure.id
                or content['item_id'] != first['item_id']
                or content['receiver_group'] != GROUP):
            raise ValueError('接收版本、来源或审计引用不一致。')
        amount = _number(content['quantity'], positive='corrects_id' not in content)
        broken = _number(content['damaged_quantity'])
        _text(content['location'])
        _text(content['custodian'])
        if broken > amount:
            raise ValueError('损坏数量不得超过本次实收。')
        if 'corrects_id' in content:
            from . import audited_records as ar
            index = next(i for i, e in enumerate(workspace.events) if e.payload.get('revision_id') == row.id)
            prior = ar.prefix(workspace, index)
            require_receiver(prior, ledger, event_id=event_id, actor=row.actor_id)
            prior_rows = [r for r in prefix_rows if r.id in prior.object_revisions]
            if len(prior_rows) != version - 1 or content['corrects_id'] not in {r.id for r in corrections.effective(prior_rows)}:
                raise ValueError('更正批次与当时当前有效版本不一致。')
            corrections.custody_protect(prior, ledger, loan_transfer_id)
        prefix_rows.append(row)
        effective = corrections.effective(prefix_rows)
        received = math.fsum(json.loads(r.content_json)['quantity'] for r in effective)
        damaged = math.fsum(json.loads(r.content_json)['damaged_quantity'] for r in effective)
        if received > custody['quantity'] + 1e-9:
            raise ValueError('更正时累计实收超过原撤离保管数量。')
        requests.add(row.request_id)
    if received > custody['quantity'] + 1e-9:
        raise ValueError('累计实收超过撤离时未归还保管数量。')
    return CustodyReceiptSummary(event_id, loan_transfer_id, departure.id,
                                first['item_id'], custody['quantity'], received,
                                damaged, max(custody['quantity'] - received, 0.0),
                                len(records), tuple(records), corrections.effective(records))


def record(workspace, ledger, registry, *, event_id, token, resolve_subject,
           loan_transfer_id, quantity, damaged_quantity, location, custodian,
           evidence, request_id, expected_version, occurred_at='',
           corrects_id='', correction_reason=''):
    actor = resolve_subject(token)
    require_receiver(workspace, ledger, event_id=event_id, actor=actor)
    require_closeout_event(registry, event_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('接收版本须为非负整数。')
    amount = _number(quantity, positive=not corrects_id)
    broken = _number(damaged_quantity)
    if broken > amount:
        raise ValueError('损坏数量不得超过本次实收。')
    _text(evidence)
    _text(request_id)
    current = summary(workspace, ledger, event_id=event_id,
                      loan_transfer_id=loan_transfer_id)
    content = dict(withdrawal_id=current.withdrawal_id, item_id=current.item_id,
                   receiver_group=GROUP, quantity=amount, damaged_quantity=broken,
                   location=_text(location), custodian=_text(custodian))
    if corrects_id:
        content.update(corrects_id=_text(corrects_id), correction_reason=_text(correction_reason))
    elif correction_reason:
        raise ValueError('更正理由缺少明确原批次。')
    times = RecordTimes(occurred_at=occurred_at,
                        recorded_at=datetime.now(timezone.utc).isoformat())
    for previous in workspace.object_revisions.values():
        if previous.request_id == request_id:
            if (previous in current.records and previous.actor_id == actor
                    and previous.source == SOURCE + evidence
                    and previous.times.occurred_at == occurred_at
                    and previous.reference.version == expected_version + 1
                    and json.loads(previous.content_json) == content):
                return previous
            raise ValueError('接收请求编号已用于不同内容。')
    if expected_version != current.version:
        raise ValueError('接收版本已变化，请刷新后核对。')
    if corrects_id:
        if corrects_id not in {r.id for r in current.effective_records}:
            raise ValueError('须引用当前有效实收批次。')
        corrections.custody_protect(workspace, ledger, loan_transfer_id)
        replaced = next(r for r in current.effective_records if r.id == corrects_id)
        previous_amount = json.loads(replaced.content_json)['quantity']
        if amount > current.unreceived_quantity + previous_amount + 1e-9:
            raise ValueError('更正后累计实收超过原撤离保管数量。')
    from .material_custody_settlements import summary as settlement_summary
    if not corrects_id and settlement_summary(workspace, ledger, event_id=event_id,
                          loan_transfer_id=loan_transfer_id).settled:
        raise ValueError('已结清；须通过追加更正引用当前实收批次并填写理由。')
    if not corrects_id and amount > current.unreceived_quantity + 1e-9:
        raise ValueError('本次实收超过仍待接收数量。')
    revision = ObjectRevision(ObjectReference(event_id, KIND, loan_transfer_id,
                                               current.version + 1),
                              times, SOURCE + evidence, 'reported', actor,
                              request_id, json.dumps(content, ensure_ascii=False))
    return workspace._record_object_revision(revision, expected_version=expected_version)
