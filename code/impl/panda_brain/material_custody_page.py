"""Escaped, event-scoped receipt, verification and explicit settlement forms."""
import json
import secrets
from html import escape

from . import material_custody_receipts as receipts
from .material_transfers import _history, loan_status
from .material_withdrawals import withdrawal_record
from .org_model import mask_phone


def require_page(state, token, event_id):
    actor = state.authorization_subject(token)
    event = state.registry.events.get(event_id)
    if event is None or event.status not in {'active', 'upgraded', 'closed'}:
        raise PermissionError('当前事件不接受装备接收、核验或结清。')
    if event.status != 'closed' and event_id != state.active_event_id:
        raise PermissionError('当前事件已变化，请保留输入并重新核对事件。')
    space, _, _ = state._appointment_space(event_id)
    receipts.require_receiver(space, state.ledger, event_id=event_id, actor=actor)
    return state.member_session(token)


def submit(state, token, route, fields, member):
    operation = route.rsplit('/', 1)[-1]
    common = {'event_id', 'csrf', 'loan_transfer_id', 'evidence', 'request_id',
              'expected_version', 'occurred_at'}
    specific = {
        'report': {'quantity', 'damaged_quantity', 'location', 'custodian'},
        'verify': {'receipt_id', 'result', 'conclusion', 'expected_receipt_version'},
        'settle': {'expected_receipt_version', 'expected_verification_version',
                   'handling_evidence', 'handling_conclusion', 'confirm'},
    }
    specific['correct'] = specific['report'] | {'corrects_id', 'correction_reason'}
    specific['reverify'] = specific['verify'] | {'corrects_id', 'correction_reason'}
    specific['invalidate'] = {'corrects_id', 'correction_reason', 'expected_receipt_version', 'expected_verification_version'}
    if set(fields) != common | specific[operation] or any(len(v) != 1 for v in fields.values()):
        raise ValueError('接收核验结清字段缺失、重复或不受支持。')
    value = lambda key: fields[key][0]
    if not secrets.compare_digest(value('csrf').encode('utf-8'), member.custody_csrf.encode('utf-8')):
        raise PermissionError('表单会话或当前事件已变化，请保留输入并重新核对。')
    require_page(state, token, value('event_id'))
    def version(key):
        raw = value(key)
        if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise ValueError('业务版本不正确。')
        return int(raw)
    args = dict(member_token=token, loan_transfer_id=value('loan_transfer_id'),
                evidence=value('evidence'), request_id=value('request_id'),
                expected_version=version('expected_version'), occurred_at=value('occurred_at'))
    if operation in {'correct', 'reverify', 'invalidate'}:
        args.update(corrects_id=value('corrects_id'), correction_reason=value('correction_reason'))
    if operation in {'report', 'correct'}:
        result = state.handle_material_custody_receipt(value('event_id'), **args,
            quantity=float(value('quantity')), damaged_quantity=float(value('damaged_quantity')),
            location=value('location'), custodian=value('custodian'))
        message = '本次实收记录已核对；核验及整笔业务结清状态见当前卡片。'
    elif operation in {'verify', 'reverify'}:
        result = state.handle_material_custody_verification(value('event_id'), **args,
            receipt_id=value('receipt_id'), result=value('result'), conclusion=value('conclusion'),
            expected_receipt_version=version('expected_receipt_version'))
        message = '本批核验记录已核对；当前有效核验与业务结清状态见卡片。'
    elif operation == 'invalidate':
        result = state.handle_material_custody_invalidation(value('event_id'), **args,
            expected_receipt_version=version('expected_receipt_version'),
            expected_verification_version=version('expected_verification_version'))
        message = '原结清已保留并标记失效；须重新核验当前批次，再明确整笔结清。'
    else:
        if value('confirm') != 'yes':
            raise ValueError('须由本人明确勾选整笔业务结清确认。')
        result = state.handle_material_custody_settlement(value('event_id'), **args,
            expected_receipt_version=version('expected_receipt_version'),
            expected_verification_version=version('expected_verification_version'),
            handling_evidence=value('handling_evidence'), handling_conclusion=value('handling_conclusion'))
        message = '整笔业务结清已具名记录；损坏不计可用，未更新长期底账或现场可用量。'
    return result, message


def _form(member, event_id, loan_id, operation, version, **extra):
    fields = dict(event_id=event_id, csrf=member.custody_csrf,
                  loan_transfer_id=loan_id, expected_version=version,
                  request_id=secrets.token_urlsafe(18)) | extra
    return (f'<form class="custody-form" data-operation="{operation}" method="post" '
            f'action="/material-custody/{operation}">'
            + ''.join(f'<input type="hidden" name="{key}" value="{escape(str(value), quote=True)}">'
                      for key, value in fields.items()))


def _who(state, row):
    person = state.registry.library.get(row.actor_id)
    return (person.name + ' · ' if person else '') + mask_phone(row.actor_id)


def _provenance(state, row):
    e = lambda value: escape(str(value), quote=True)
    return (f'<p>记录 {e(row.id)}；办理人 {e(_who(state, row))}；依据：{e(row.source)}'
            f'；发生时间：{e(row.times.occurred_at or "未知")}；记录时间：{e(row.times.recorded_at)}</p>')


def card(state, token, event_id, loan_id):
    member = require_page(state, token, event_id)
    business = state.material_custody_settlement(event_id, member_token=token,
                                                loan_transfer_id=loan_id)
    view = business.receipt_summary
    original_balance = loan_status(state.ledger, event_id=event_id, loan_transfer_id=loan_id)
    original = _history(state.ledger, event_id, loan_id)[0]
    loan = json.loads(original.content_json)
    departure = withdrawal_record(state.ledger, event_id=event_id, squad_id=loan['to_squad'])
    custody = next(row for row in json.loads(departure.content_json)['borrowed_custody']
                   if row['loan_transfer_id'] == loan_id)
    e = lambda value: escape(str(value), quote=True)
    name = state.ledger.items[view.item_id].name
    effective = {json.loads(r.content_json)['receipt_id']: r for r in business.effective_verifications}
    labels = {'passed': '通过', 'failed': '未通过', 'unknown': '待核实'}
    rows = []
    for row in view.effective_records:
        data = json.loads(row.content_json)
        who = _who(state, row)
        verification = effective.get(row.id)
        result = json.loads(verification.content_json)['result'] if verification else None
        verification_form = ''
        if not business.settled:
            verification_form = (
                '<details class="custody-verification"><summary>登记或追加本批核验</summary>'
                + _form(member, event_id, loan_id, 'verify', business.verification_version,
                        expected_receipt_version=view.version, receipt_id=row.id)
                + '<label class="lz-label">本批核验结果<select class="lz-input" name="result" required>'
                  '<option value="">请选择</option><option value="passed">通过</option>'
                  '<option value="failed">未通过</option><option value="unknown">待核实</option></select></label>'
                  '<label class="lz-label">本批核验依据<textarea class="lz-input" name="evidence" required></textarea></label>'
                  '<label class="lz-label">本批核验结论<textarea class="lz-input" name="conclusion" required></textarea></label>'
                  '<label class="lz-label">核验发生时间（未知留空；已知须含时区）'
                  '<input class="lz-input" name="occurred_at"></label>'
                  '<button class="lz-btn" type="submit">登记本批核验</button></form></details>')
        history = [r for r in business.verifications if json.loads(r.content_json)['receipt_id'] == row.id]
        verification_history = ''.join(
            f'<li>核验 v{r.reference.version}：{labels[json.loads(r.content_json)["result"]]}；'
            f'结论：{e(json.loads(r.content_json)["conclusion"])}'
            + _provenance(state, r) + '</li>' for r in history)
        rows.append(
            f'<li class="custody-batch" data-receipt="{e(row.id)}">第 {row.reference.version} 批：实收 {data["quantity"]:g}，'
            f'其中损坏 {data["damaged_quantity"]:g}；接收报告人 {e(who)}'
            f'<p>报告位置：{e(data["location"])}；保管对象：{e(data["custodian"])}</p>'
            f'<p>依据：{e(row.source)}；发生时间：{e(row.times.occurred_at or "未知")}'
            f'；记录时间：{e(row.times.recorded_at)}</p>'
            f'<p><b>最新核验：{labels[result] if result else "尚未核验"}</b></p>'
            + ('<details><summary>本批核验历史与来源</summary><ol>' + verification_history + '</ol></details>' if history else '')
            + verification_form + correction_form(member, event_id, loan_id, row, view.version)
            + (verification_correction_form(member, event_id, loan_id, verification, view.version, business.verification_version) if verification else '') + '</li>')
    form = ''
    if not business.settled and view.unreceived_quantity > 1e-9:
        form = _form(member, event_id, loan_id, 'report', view.version)
        form += (
            '<label class="lz-label">本次实收数量<input class="lz-input" name="quantity" '
            'type="number" min="0" step="any" required></label>'
            '<label class="lz-label">其中损坏数量<input class="lz-input" name="damaged_quantity" '
            'type="number" min="0" step="any" value="0" required></label>'
            '<label class="lz-label">实际接收位置<input class="lz-input" name="location" required></label>'
            '<label class="lz-label">接收后的保管对象<input class="lz-input" name="custodian" required></label>'
            '<label class="lz-label">本次实收原始依据<textarea class="lz-input" name="evidence" required></textarea></label>'
            '<label class="lz-label">实际发生时间（已知时填含时区的 ISO 时间）'
            '<input class="lz-input" name="occurred_at"></label>'
            '<button class="lz-btn" type="submit">登记本次实收</button></form>')
    settlement = ''
    if business.settlement:
        data = json.loads(business.settlement.content_json)
        settlement = ('<details class="custody-settlement-source"><summary>整笔结清依据与冻结来源</summary>'
            + _provenance(state, business.settlement)
            + f'<p>损坏处理依据：{e(data["handling_evidence"] or "无损坏，无须填写")}；'
              f'处理结论：{e(data["handling_conclusion"] or "无损坏，无须填写")}</p>'
            + '<p>实收记录：' + e('、'.join(data['receipt_ids'])) + '</p>'
            + '<p>有效核验记录：' + e('、'.join(data['verification_ids'])) + '</p></details>')
    elif (view.records and view.unreceived_quantity <= 1e-9
          and not business.unverified_receipt_ids and not business.not_passed_receipt_ids):
        required = ' required' if view.damaged_quantity > 0 else ''
        settlement = ('<section class="custody-settlement"><h4>整笔结清确认</h4>'
            '<p>全部实收批次最新核验已通过；仍须本人明确确认。有损坏须留已核实的处理依据和结论，损坏不计可用。</p>'
            + _form(member, event_id, loan_id, 'settle', business.settlement_version,
                    expected_receipt_version=view.version,
                    expected_verification_version=business.verification_version)
            + '<label class="lz-label">整笔结清依据<textarea class="lz-input" name="evidence" required></textarea></label>'
            + f'<label class="lz-label">已核实的损坏处理依据<textarea class="lz-input" name="handling_evidence"{required}></textarea></label>'
            + f'<label class="lz-label">已核实的损坏处理结论<textarea class="lz-input" name="handling_conclusion"{required}></textarea></label>'
            + '<label class="lz-label">结清发生时间（未知留空；已知须含时区）<input class="lz-input" name="occurred_at"></label>'
            + '<label class="lz-label"><input type="checkbox" name="confirm" value="yes" required>本人已核对以上来源，明确确认整笔业务结清</label>'
            + '<button class="lz-btn" type="submit">确认整笔业务结清</button></form></section>')
    history_html = '<details><summary>全部实收旧版与更正来源</summary><ol>' + ''.join(
        '<li>' + _provenance(state, r) + '<p>' + e(r.content_json) + '</p></li>' for r in view.records) + '</ol></details>'
    history_html += '<details><summary>全部结清与失效历史</summary><ol>' + ''.join(
        '<li>' + _provenance(state, r) + '<p>' + e(r.content_json) + '</p></li>' for r in business.settlements) + '</ol></details>'
    history_html += '<details><summary>全部核验旧版与更正来源</summary><ol>' + ''.join(
        '<li>' + _provenance(state, r) + '<p>' + e(r.content_json) + '</p></li>' for r in business.verifications) + '</ol></details>'
    if business.settled:
        history_html += invalidation_form(member, event_id, loan_id, business)
    return (
        f'<article class="lz-card custody-card" data-loan="{e(loan_id)}">'
        f'<h3>{e(name)} · 借用 {e(loan_id)}</h3>'
        f'<p>原借还流水：已借实收 {original_balance.borrowed:g}；归还实收 {original_balance.returned:g}；'
        f'原流水未归还 {original_balance.outstanding:g}（历史数量，不能单独判断撤离后业务结清）</p>'
        f'<p>撤离保管 {view.departed_quantity:g}；累计实收 {view.received_quantity:g}；'
        f'累计损坏 {view.damaged_quantity:g}；<b>仍待接收 {view.unreceived_quantity:g}</b></p>'
        f'<p>核验情况：尚未核验 {len(business.unverified_receipt_ids)} 批；最新核验未通过或待核实 {len(business.not_passed_receipt_ids)} 批。</p>'
        f'<p class="custody-business-status lz-note">{e(business.conclusion)}</p>'
        f'<details><summary>原借用与撤离依据</summary><p>物资 {e(view.item_id)}；'
        f'原借出队 {e(loan["from_squad"])} → 借入队 {e(loan["to_squad"])}</p>'
        f'<p>原借用批准依据 {e(original.source)}；记录时间 {e(original.times.recorded_at)}</p>'
        f'<p>撤离记录 {e(departure.id)}；依据 {e(departure.source)}；'
        f'发生时间 {e(departure.times.occurred_at or "未知")}；'
        f'记录时间 {e(departure.times.recorded_at)}</p>'
        f'<p>撤离报告保管位置 {e(custody["location"])}；保管对象 {e(custody["custodian"])}'
        '（原报告，尚未独立核验）</p></details>'
        '<details class="custody-batches" open><summary>逐批实收与核验来源</summary><ol>'
        + (''.join(rows) or '<li>尚无实收报告。</li>') + '</ol></details>'
        + form + settlement + history_html + '<p class="custody-message" role="status" aria-live="polite"></p></article>')


def body(state, token, event_id):
    require_page(state, token, event_id)
    ids = set()
    for departure in state.ledger.withdrawal_records.values():
        if departure.reference.event_id == event_id:
            ids.update(row['loan_transfer_id'] for row in
                       json.loads(departure.content_json).get('borrowed_custody', ()))
    cards = []
    for loan_id in sorted(ids):
        try:
            cards.append(card(state, token, event_id, loan_id))
        except ValueError:
            cards.append('<article class="lz-card"><h3>借用 ' + escape(loan_id)
                         + '</h3><p>来源或审计待核对，暂不显示数量或开放接收、核验、结清操作。</p></article>')
    label = state.registry.events[event_id].name
    closeout_note = ('<p class="lz-note">事件已关闭：仅办理本事件既有借用装备的接收、核验和结清。'
                     '作战任务仍保持关闭。</p>' if state.registry.events[event_id].status == 'closed' else '')
    return (
        '<style>.custody-card{overflow-wrap:anywhere}.custody-card details{margin:12px 0}'
        '.custody-message{white-space:pre-wrap}.custody-form .lz-label{margin:8px 0}'
        '.custody-form textarea{display:block;min-height:80px;max-width:none}'
        '.custody-card ol{padding-left:20px}</style>'
        f'<main id="material-custody" data-event="{escape(event_id, quote=True)}">'
        '<div class="lz-card"><h2>撤离后借用装备接收与核验结清</h2><p>' + escape(label)
        + ' · ' + escape(event_id) + '</p><p class="lz-note">仅物资供应组现任组长本人办理。'
        '实收、损坏和仍待接收分别记录；全收且全部批次最新核验通过后，由组长另行明确确认整笔业务结清。'
        '登记不增加现场可用量，不更新长期底账；借出队收尾另行办理。</p></div>'
        + closeout_note
        + (''.join(cards) or '<div class="lz-card"><p>当前没有借入队撤离后的装备接收待办。</p></div>')
        + '</main><script src="/static/material-custody.js" defer></script>')


def correction_fields():
    return ('<label class="lz-label">更正理由<textarea class="lz-input" name="correction_reason" required></textarea></label>'
        '<label class="lz-label">本次更正依据<textarea class="lz-input" name="evidence" required></textarea></label>'
        '<label class="lz-label">发生时间（未知留空；已知须含时区）<input class="lz-input" name="occurred_at"></label>')


def correction_form(member, event_id, loan_id, row, version):
    data = json.loads(row.content_json)
    e = lambda value: escape(str(value), quote=True)
    return ('<details><summary>追加更正本批实收</summary><p>填写本批更正后的总数，旧版保留；受影响核验与整笔结清失效。后续业务已使用来源时拒绝直接更正。</p>'
        + _form(member, event_id, loan_id, 'correct', version, corrects_id=row.id)
        + ''.join(f'<label class="lz-label">{label}<input class="lz-input" name="{key}" value="{e(data[key])}" required></label>'
            for key, label in (('quantity', '本批更正后实收总数'), ('damaged_quantity', '其中损坏'), ('location', '实际位置'), ('custodian', '保管对象')))
        + correction_fields() + '<button class="lz-btn" type="submit">追加实收更正</button></form></details>')


def verification_correction_form(member, event_id, loan_id, row, receipt_version, version):
    data = json.loads(row.content_json)
    return ('<details><summary>追加更正本批核验</summary>'
        + _form(member, event_id, loan_id, 'reverify', version, corrects_id=row.id,
            receipt_id=data['receipt_id'], expected_receipt_version=receipt_version)
        + '<label class="lz-label">重新核验结果<select class="lz-input" name="result" required><option value="">请选择</option><option value="passed">通过</option><option value="failed">未通过</option><option value="unknown">待核实</option></select></label>'
        + '<label class="lz-label">更正后核验结论<textarea class="lz-input" name="conclusion" required></textarea></label>'
        + correction_fields() + '<button class="lz-btn" type="submit">追加核验更正</button></form></details>')


def invalidation_form(member, event_id, loan_id, business):
    return ('<details><summary>更正结清并使原结论失效</summary><p>保留原结清；当前各批核验须重新办理后才能再次明确结清。</p>'
        + _form(member, event_id, loan_id, 'invalidate', business.settlement_version,
            corrects_id=business.settlement.id, expected_receipt_version=business.receipt_summary.version,
            expected_verification_version=business.verification_version)
        + correction_fields() + '<button class="lz-btn" type="submit">记录结清更正，等待重验</button></form></details>')
