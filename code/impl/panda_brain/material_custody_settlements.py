"""Named verification and explicit settlement, separate from inventory accounting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json

from . import material_custody_receipts as receipts
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes

VERIFICATION = 'loan_custody_verification'
SETTLEMENT = 'loan_custody_settlement'
VERIFY_SOURCE = '人工撤离后装备核验：'
SETTLE_SOURCE = '人工撤离后借用结清：'
RESULTS = {'passed', 'failed', 'unknown'}


@dataclass(frozen=True)
class CustodySettlementSummary:
    receipt_summary: receipts.CustodyReceiptSummary
    verification_version: int
    settlement_version: int
    verifications: tuple[ObjectRevision, ...]
    effective_verifications: tuple[ObjectRevision, ...]
    unverified_receipt_ids: tuple[str, ...]
    not_passed_receipt_ids: tuple[str, ...]
    settlement: ObjectRevision | None
    settled: bool
    conclusion: str
    settlements: tuple[ObjectRevision, ...] = ()


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError('业务版本须为非负整数。')
    return value


def _history(workspace, *, event_id, loan_transfer_id, kind, source):
    rows = sorted((r for r in workspace.object_revisions.values()
                   if r.reference.object_kind == kind
                   and r.reference.object_id == loan_transfer_id),
                  key=lambda r: r.reference.version)
    mirrors = [e for e in workspace.events if e.kind == 'ObjectRevisionRecorded'
               and e.payload.get('revision', {}).get('reference', {}).get('object_kind') == kind
               and e.payload['revision']['reference'].get('object_id') == loan_transfer_id]
    if len(rows) != len(mirrors):
        raise ValueError('核验/结清记录与审计镜像数量不一致。')
    requests = set()
    for version, row in enumerate(rows, 1):
        if (row.reference != ObjectReference(event_id, kind, loan_transfer_id, version)
                or workspace.object_revisions.get(row.id) != row
                or row.data_kind != 'reported' or not row.source.startswith(source)
                or not row.source[len(source):].strip() or row.request_id in requests
                or sum(e.payload == {'revision_id': row.id, 'revision': asdict(row)}
                       for e in mirrors) != 1):
            raise ValueError('核验/结清版本、来源或审计引用不一致。')
        requests.add(row.request_id)
    return tuple(rows)


def _sequence(workspace, record):
    return next(e.sequence for e in workspace.events
                if e.kind == 'ObjectRevisionRecorded' and e.payload.get('revision_id') == record.id)


def _settlement_content(current, handling_evidence, handling_conclusion):
    received = current.receipt_summary
    return dict(withdrawal_id=received.withdrawal_id, item_id=received.item_id,
                receiver_group=receipts.GROUP, receipt_version=received.version,
                verification_version=current.verification_version,
                receipt_ids=[r.id for r in received.effective_records],
                verification_ids=[r.id for r in current.effective_verifications],
                received_quantity=received.received_quantity,
                damaged_quantity=received.damaged_quantity,
                handling_evidence=handling_evidence, handling_conclusion=handling_conclusion)


def _ready(current):
    if (current.receipt_summary.unreceived_quantity > 1e-9
            or not current.receipt_summary.records
            or current.unverified_receipt_ids or current.not_passed_receipt_ids):
        raise ValueError('须全数实收且所有批次的最新核验通过，才可明确确认整笔结清。')


def _handling(received, evidence, conclusion):
    for value in (evidence, conclusion):
        if not isinstance(value, str) or value != value.strip():
            raise ValueError('损坏处理依据与结论须为不带首尾空白的文本。')
    if received.damaged_quantity > 0:
        receipts._text(evidence)
        receipts._text(conclusion)


def summary(workspace, ledger, *, event_id, loan_transfer_id):
    from .material_custody_decision_history import summary as current_summary
    return current_summary(workspace, ledger, event_id=event_id, loan_transfer_id=loan_transfer_id)


def _start(workspace, ledger, registry, *, event_id, token, resolve_subject,
           loan_transfer_id, expected_version, expected_receipt_version):
    actor = resolve_subject(token)
    receipts.require_receiver(workspace, ledger, event_id=event_id, actor=actor)
    receipts.require_closeout_event(registry, event_id)
    _version(expected_version)
    _version(expected_receipt_version)
    return actor, summary(workspace, ledger, event_id=event_id, loan_transfer_id=loan_transfer_id)


def _replay(workspace, *, records, actor, source, content, request_id,
            expected_version, occurred_at):
    receipts._text(request_id)
    RecordTimes(occurred_at=occurred_at)
    for previous in workspace.object_revisions.values():
        if previous.request_id == request_id:
            if (previous in records and previous.actor_id == actor and previous.source == source
                    and previous.times.occurred_at == occurred_at
                    and previous.reference.version == expected_version + 1
                    and json.loads(previous.content_json) == content):
                return previous
            raise ValueError('核验/结清请求编号已用于不同内容。')
    return None


def _append(workspace, *, event_id, loan_transfer_id, kind, actor, source,
            request_id, expected_version, occurred_at, content):
    record = ObjectRevision(ObjectReference(event_id, kind, loan_transfer_id, expected_version + 1),
        RecordTimes(occurred_at=occurred_at, recorded_at=datetime.now(timezone.utc).isoformat()),
        source, 'reported', actor, request_id, json.dumps(content, ensure_ascii=False))
    return workspace._record_object_revision(record, expected_version=expected_version)


def verify(workspace, ledger, registry, *, event_id, token, resolve_subject,
           loan_transfer_id, receipt_id, result, conclusion, evidence, request_id,
           expected_version, expected_receipt_version, occurred_at='',
           corrects_id='', correction_reason=''):
    actor, current = _start(workspace, ledger, registry, event_id=event_id, token=token,
        resolve_subject=resolve_subject, loan_transfer_id=loan_transfer_id,
        expected_version=expected_version, expected_receipt_version=expected_receipt_version)
    if not isinstance(result, str) or result not in RESULTS:
        raise ValueError('核验结果须为 passed、failed 或 unknown。')
    if not isinstance(receipt_id, str) or receipt_id not in {r.id for r in current.receipt_summary.records}:
        raise ValueError('须引用本事件、本笔借用的具体实收批次。')
    content = dict(receipt_id=receipt_id, receipt_version=expected_receipt_version,
        withdrawal_id=current.receipt_summary.withdrawal_id, item_id=current.receipt_summary.item_id,
        receiver_group=receipts.GROUP, result=result, conclusion=receipts._text(conclusion))
    if corrects_id:
        content.update(corrects_id=receipts._text(corrects_id), correction_reason=receipts._text(correction_reason))
    elif correction_reason:
        raise ValueError('核验更正理由缺少原核验。')
    source = VERIFY_SOURCE + receipts._text(evidence)
    previous = _replay(workspace, records=current.verifications, actor=actor, source=source,
        content=content, request_id=request_id, expected_version=expected_version, occurred_at=occurred_at)
    if previous:
        return previous
    if receipt_id not in {r.id for r in current.receipt_summary.effective_records}:
        raise ValueError('旧实收批次已被更正，须核验当前有效批次。')
    if corrects_id:
        latest = next((r for r in current.effective_verifications if json.loads(r.content_json)['receipt_id'] == receipt_id), None)
        if latest is None or latest.id != corrects_id:
            raise ValueError('须引用本批当前有效核验。')
        from .material_closeout_corrections import custody_protect
        custody_protect(workspace, ledger, loan_transfer_id)
    elif current.settled:
        raise ValueError('已结清；须明确引用原核验并填写更正理由。')
    if (expected_version != current.verification_version
            or expected_receipt_version != current.receipt_summary.version):
        raise ValueError('实收或核验版本已变化，请刷新核对。')
    return _append(workspace, event_id=event_id, loan_transfer_id=loan_transfer_id,
        kind=VERIFICATION, actor=actor, source=source, content=content,
        request_id=request_id, expected_version=expected_version, occurred_at=occurred_at)


def settle(workspace, ledger, registry, *, event_id, token, resolve_subject,
           loan_transfer_id, evidence, request_id, expected_version,
           expected_receipt_version, expected_verification_version,
           handling_evidence='', handling_conclusion='', occurred_at=''):
    actor, current = _start(workspace, ledger, registry, event_id=event_id, token=token,
        resolve_subject=resolve_subject, loan_transfer_id=loan_transfer_id,
        expected_version=expected_version, expected_receipt_version=expected_receipt_version)
    _version(expected_verification_version)
    old = next((r for r in current.settlements if r.request_id == request_id), None)
    if old:
        content = json.loads(old.content_json)
        content.update(handling_evidence=handling_evidence, handling_conclusion=handling_conclusion)
    else:
        _handling(current.receipt_summary, handling_evidence, handling_conclusion)
        content = _settlement_content(current, handling_evidence, handling_conclusion)
    content.update(receipt_version=expected_receipt_version,
                   verification_version=expected_verification_version)
    source = SETTLE_SOURCE + receipts._text(evidence)
    previous = _replay(workspace, records=current.settlements,
        actor=actor, source=source, content=content, request_id=request_id,
        expected_version=expected_version, occurred_at=occurred_at)
    if previous:
        return previous
    if current.settled:
        raise ValueError('当前结清仍有效；须先明确更正来源或使原结论失效。')
    if (expected_version != current.settlement_version
            or expected_receipt_version != current.receipt_summary.version
            or expected_verification_version != current.verification_version):
        raise ValueError('实收、核验或结清版本已变化，请刷新核对。')
    _ready(current)
    return _append(workspace, event_id=event_id, loan_transfer_id=loan_transfer_id,
        kind=SETTLEMENT, actor=actor, source=source, content=content,
        request_id=request_id, expected_version=expected_version, occurred_at=occurred_at)


def invalidate(workspace, ledger, registry, *, event_id, token, resolve_subject,
               loan_transfer_id, corrects_id, correction_reason, evidence, request_id,
               expected_version, expected_receipt_version, expected_verification_version,
               occurred_at=''):
    actor, current = _start(workspace, ledger, registry, event_id=event_id, token=token,
        resolve_subject=resolve_subject, loan_transfer_id=loan_transfer_id,
        expected_version=expected_version, expected_receipt_version=expected_receipt_version)
    _version(expected_verification_version)
    content = dict(action='invalidate', corrects_id=receipts._text(corrects_id),
        correction_reason=receipts._text(correction_reason), receipt_version=expected_receipt_version,
        verification_version=expected_verification_version)
    source = SETTLE_SOURCE + receipts._text(evidence)
    old = _replay(workspace, records=current.settlements, actor=actor, source=source, content=content,
        request_id=request_id, expected_version=expected_version, occurred_at=occurred_at)
    if old:
        return old
    if (not current.settled or current.settlement.id != corrects_id
            or expected_version != current.settlement_version
            or expected_receipt_version != current.receipt_summary.version
            or expected_verification_version != current.verification_version):
        raise ValueError('须引用当前有效结清及当前实收、核验版本。')
    from .material_closeout_corrections import custody_protect
    custody_protect(workspace, ledger, loan_transfer_id)
    return _append(workspace, event_id=event_id, loan_transfer_id=loan_transfer_id,
        kind=SETTLEMENT, actor=actor, source=source, content=content, request_id=request_id,
        expected_version=expected_version, occurred_at=occurred_at)
