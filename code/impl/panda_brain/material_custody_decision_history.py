"""Validate each historical decision against its immutable business prefix."""
from dataclasses import replace
import json

from . import audited_records as ar
from . import material_custody_receipts as receipts
from . import material_closeout_corrections as corrections


def verification_view(workspace, ledger, received, rows, cutoff=0):
    from . import material_custody_settlements as decisions
    latest = {}
    fields = {'receipt_id', 'receipt_version', 'withdrawal_id', 'item_id',
              'receiver_group', 'result', 'conclusion'}
    for row in rows:
        value = json.loads(row.content_json)
        if (set(value) - corrections.FIELDS != fields
                or value['withdrawal_id'] != received.withdrawal_id
                or value['item_id'] != received.item_id
                or value['receiver_group'] != receipts.GROUP
                or not isinstance(value['result'], str) or value['result'] not in decisions.RESULTS):
            raise ValueError('核验字段、原借用或结论引用不一致。')
        receipts._text(value['conclusion'])
        index = next(i for i, e in enumerate(workspace.events) if e.payload.get('revision_id') == row.id)
        prior = ar.prefix(workspace, index)
        batch_view = receipts.summary(prior, ledger, event_id=received.event_id,
                                      loan_transfer_id=received.loan_transfer_id)
        if (decisions._version(value['receipt_version']) != batch_view.version
                or value['receipt_id'] not in {r.id for r in batch_view.effective_records}):
            raise ValueError('核验须引用当时有效实收批次及完整版本。')
        if 'corrects_id' in value:
            receipts._text(value['correction_reason'])
            old = latest.get(value['receipt_id'])
            if old is None or value['corrects_id'] != old.id:
                raise ValueError('核验更正须引用本批当时最新核验。')
            receipts.require_receiver(prior, ledger, event_id=received.event_id, actor=row.actor_id)
            corrections.custody_protect(prior, ledger, received.loan_transfer_id)
        elif corrections.FIELDS & value.keys():
            raise ValueError('核验更正来源与理由不完整。')
        latest[value['receipt_id']] = row
    latest = {key: row for key, row in latest.items()
              if decisions._sequence(workspace, row) > cutoff}
    effective = tuple(latest[r.id] for r in received.effective_records if r.id in latest)
    missing = tuple(r.id for r in received.effective_records if r.id not in latest)
    failed = tuple(r.id for r in received.effective_records if r.id in latest
                   and json.loads(latest[r.id].content_json)['result'] != 'passed')
    return effective, missing, failed


def summary(workspace, ledger, *, event_id, loan_transfer_id):
    from . import material_custody_settlements as d
    received = replace(receipts.summary(workspace, ledger, event_id=event_id,
                                       loan_transfer_id=loan_transfer_id),
                       conclusion='实收报告汇总；旧版保留，当前有效核验与结清见独立记录。')
    verifications = d._history(workspace, event_id=event_id, loan_transfer_id=loan_transfer_id,
                              kind=d.VERIFICATION, source=d.VERIFY_SOURCE)
    settlements = d._history(workspace, event_id=event_id, loan_transfer_id=loan_transfer_id,
                            kind=d.SETTLEMENT, source=d.SETTLE_SOURCE)
    cutoff = 0
    last = None
    for row in settlements:
        seq = d._sequence(workspace, row)
        index = next(i for i, e in enumerate(workspace.events) if e.sequence == seq)
        prior = ar.prefix(workspace, index)
        receipt_cut = receipts.summary(prior, ledger, event_id=event_id, loan_transfer_id=loan_transfer_id)
        verify_cut = tuple(r for r in verifications if d._sequence(workspace, r) < seq)
        effective, missing, failed = verification_view(prior, ledger, receipt_cut, verify_cut, cutoff)
        value = json.loads(row.content_json)
        if value.get('action') == 'invalidate':
            if (set(value) != {'action', 'corrects_id', 'correction_reason', 'receipt_version', 'verification_version'}
                    or last is None or value['corrects_id'] != last.id
                    or value['receipt_version'] != receipt_cut.version
                    or value['verification_version'] != len(verify_cut)):
                raise ValueError('结清更正来源或当时版本不一致。')
            receipts._text(value['correction_reason'])
            if any(d._sequence(workspace, r) > d._sequence(workspace, last)
                   for r in receipt_cut.records + verify_cut):
                raise ValueError('原结清已因来源变更失效，不能重复使旧结论失效。')
            receipts.require_receiver(prior, ledger, event_id=event_id, actor=row.actor_id)
            corrections.custody_protect(prior, ledger, loan_transfer_id)
            cutoff = seq
            last = None
            continue
        historical = d.CustodySettlementSummary(receipt_cut, len(verify_cut), row.reference.version - 1,
            verify_cut, effective, missing, failed, None, False, '')
        d._ready(historical)
        d._handling(receipt_cut, value.get('handling_evidence'), value.get('handling_conclusion'))
        if value != d._settlement_content(historical, value['handling_evidence'], value['handling_conclusion']):
            raise ValueError('结清冻结来源或数量与当时业务前缀不一致。')
        # A second explicit settlement needs an intervening affected correction.
        if last is not None and d._sequence(workspace, last) > max(
                [d._sequence(workspace, r) for r in receipt_cut.records + verify_cut], default=0):
            raise ValueError('当前结清仍有效，不能重复新建结清。')
        last = row
    effective, missing, failed = verification_view(workspace, ledger, received, verifications, cutoff)
    current = d.CustodySettlementSummary(received, len(verifications), len(settlements),
        verifications, effective, missing, failed, None, False,
        '借用业务责任未结清；未收继续挂账，受影响批次须重核并明确结清。', settlements)
    if last is not None:
        last_seq = d._sequence(workspace, last)
        changed = any(d._sequence(workspace, r) > last_seq for r in received.records + verifications)
        if not changed:
            current = replace(current, settlement=last, settled=True,
                conclusion='已具名确认整笔业务结清；原借还数量、损坏和撤离报告保留，未更新长期底账或现场可用量。')
    return current
