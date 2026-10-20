"""Named manual contact page. Catalog leads and promises never become stock."""
import json
import secrets
from html import escape
from urllib.parse import urlencode

from . import material_supply_contacts as contacts
from .org_model import mask_phone

e = lambda value: escape(str(value), quote=True)


def require_page(state, token, event_id, *, for_write=False):
    actor = state.authorization_subject(token)
    if not event_id or event_id != state.active_event_id:
        raise PermissionError('当前事件已变化，请保留输入并重新核对事件。')
    event = state.registry.events.get(event_id)
    allowed = {'active', 'upgraded'} if for_write else {'active', 'upgraded', 'closed'}
    if event is None or event.status not in allowed:
        raise PermissionError('当前事件不接受新的补给联系报告。')
    space, _, _ = state._appointment_space(event_id)
    contacts.require_leader(space, state.ledger, event_id=event_id, actor=actor)
    return state.member_session(token)


def selection(query, state):
    fields = query or {}
    if set(fields) - {'event_id', 'item_id', 'source'} or any(len(v) != 1 for v in fields.values()):
        raise ValueError('联系来源查询字段重复或不受支持。')
    event_id = fields.get('event_id', [state.active_event_id or ''])[0]
    item_id = fields.get('item_id', [''])[0]
    source = fields.get('source', [''])[0]
    if source:
        kind, separator, source_id = source.partition(':')
        if not separator or not item_id:
            raise ValueError('请先核对物资和联系来源。')
        contacts._object_id(item_id, kind, source_id)
    return event_id, item_id, source


def _link(event_id, item_id, source_kind, source_id):
    return '/material-supply?' + urlencode(dict(event_id=event_id, item_id=item_id,
                                               source=source_kind+':'+source_id))


def card(state, token, event_id, item_id, source_kind, source_id):
    member = require_page(state, token, event_id)
    view = state.material_supply_contacts(event_id, member_token=token, item_id=item_id,
                                         source_kind=source_kind, source_id=source_id)
    current = json.loads(view.current_source_json) if view.current_source_json else None
    frozen = json.loads(view.records[-1].content_json)['source_snapshot'] if view.records else None
    display = current or frozen
    if display is None:
        return '<article class="lz-card supply-contact-card"><h3>联系来源待核对</h3><p>当前资料缺失或无效，未开放报告。</p></article>'
    item, source = display['item'], display['source']
    who = lambda actor: ((state.registry.library[actor].name + '（' + mask_phone(actor) + '）')
                        if actor in state.registry.library else mask_phone(actor))
    rows = []
    for record in reversed(view.records):
        content = json.loads(record.content_json)
        snapshot = content['source_snapshot']
        quantity = content['available_quantity']
        changed = record.id in view.changed_source_refs
        rows.append(
            f'<li data-contact-record="{e(record.id)}" data-contact-request="{e(record.request_id)}" '
            f'data-report-version="{record.reference.version}" data-source-digest="{e(content["source_digest"])}">'
            f'<b>第 {record.reference.version} 次联系报告</b> · 可供量 '
            + ('未知' if quantity is None else f'{quantity:g} {e(snapshot["item"]["unit"])}')
            + f'<p>报告送达承诺：{e(content["promised_delivery_at"] or "未知")}</p>'
            f'<p>报告人 {e(who(record.actor_id))}；实际联系时间 {e(record.times.occurred_at or "未知")}'
            f'；记录时间 {e(record.times.recorded_at)}</p><p>依据：{e(record.source)}</p>'
            + ('<p class="lz-note">冻结来源已与当前资料不同；保留原联系历史，请重新核对。</p>' if changed else '')
            + '<details><summary>本次冻结物资与联系资料</summary>'
            f'<p>{e(snapshot["item"]["name"])} · 规格 {e(snapshot["item"]["spec"] or "未登记")}'
            f' · 单位 {e(snapshot["item"]["unit"] or "未登记")}；{e(snapshot["source"]["name"])}</p>'
            f'<pre>{e(json.dumps(snapshot, ensure_ascii=False, indent=2))}</pre></details></li>')
    form = ''
    if state.registry.events[event_id].status in {'active', 'upgraded'} and current:
        hidden = dict(event_id=event_id, csrf=member.custody_csrf, item_id=item_id,
            source_kind=source_kind, source_id=source_id, expected_source_digest=view.current_source_digest,
            expected_version=view.version, request_id=secrets.token_urlsafe(18))
        form = ('<section><h4>登记下一次人工联系</h4><form class="supply-contact-form" '
                'method="post" action="/material-supply/report">'
                + ''.join(f'<input type="hidden" name="{key}" value="{e(value)}">' for key, value in hidden.items())
                + '<div class="supply-fields"><label class="lz-label">报告可供量（未知留空；无货填0）'
                '<input class="lz-input" type="number" name="available_quantity" min="0" step="any"></label>'
                '<label class="lz-label">报告送达承诺（未知留空；已知须含时区）'
                '<input class="lz-input" name="promised_delivery_at" placeholder="例如 2026-10-10T18:00:00+08:00"></label>'
                '<label class="lz-label">实际联系时间（未知留空；已知须含时区）'
                '<input class="lz-input" name="occurred_at"></label>'
                '<label class="lz-label supply-evidence">原始联系依据'
                '<textarea class="lz-input" name="evidence" required maxlength="600"></textarea></label></div>'
                '<button type="submit" class="lz-btn">保存本次联系报告</button></form></section>')
    else:
        form = '<p class="lz-note">当前仅可核对已有联系历史，不开放新报告。</p>'
    return (
        f'<article class="lz-card supply-contact-card" data-contact-key="{e(view.object_id)}" '
        f'data-event="{e(event_id)}" data-item="{e(item_id)}" data-source-kind="{source_kind}" data-source="{e(source_id)}">'
        f'<h3>{e(item["name"])} · {e(source["name"])}</h3>'
        f'<p>{"库内物资点" if source_kind == "point" else "外部公司名录"} · {e(source_id)}</p>'
        f'<p class="lz-note">{e(view.conclusion)}</p><ul>'
        + ''.join(f'<li>{e(blocker)}</li>' for blocker in view.blockers) + '</ul>'
        + ('<details><summary>当前登记资料（供联系核对）</summary><pre>'
           + e(json.dumps(current, ensure_ascii=False, indent=2)) + '</pre></details>' if current else '')
        + '<details class="supply-history" open><summary>逐次联系报告与冻结来源</summary><ol>'
        + (''.join(rows) or '<li>尚无人工联系报告。</li>') + '</ol></details>'
        + form + '<p class="supply-message" role="status" aria-live="polite"></p></article>')


def body(state, token, event_id, item_id='', source=''):
    require_page(state, token, event_id)
    space, _, _ = state._appointment_space(event_id)
    history_error = False
    try:
        history = contacts._records(space, state.ledger, event_id)
    except ValueError:
        history, history_error = (), True
    item_choices = {item.item_id: (item.name, item.unit)
                    for item in state.ledger.items.values()}
    for record in history:
        item = json.loads(record.content_json)['source_snapshot']['item']
        item_choices.setdefault(item['item_id'], (item['name']+'（历史资料）', item['unit']))
    options = '<option value="">请选择物资</option>' + ''.join(
        f'<option value="{e(identifier)}"{" selected" if identifier == item_id else ""}>{e(name)} · {e(unit or "单位待核")}</option>'
        for identifier, (name, unit) in sorted(item_choices.items()))
    content = (
        '<div class="lz-card"><h2>人工补给联系</h2>'
        f'<p>{e(state.registry.events[event_id].name)} · {e(event_id)}</p>'
        '<p class="lz-note">先核对库内点；无货、不足或不能及时送达时再考虑外部公司。'
        '登记库存与公司名录只是线索，联系报告不是独立核验；可供量和送达承诺不等于到货。</p>'
        '<form method="get" action="/material-supply" class="supply-selector">'
        f'<input type="hidden" name="event_id" value="{e(event_id)}">'
        '<label class="lz-label">所需物资<select name="item_id" class="lz-input">' + options + '</select></label>'
        '<button class="lz-btn lz-btn2" type="submit">查看联系来源</button></form></div>')
    if history_error:
        content += '<article class="lz-card"><h3>联系来源或审计待核对</h3><p>未展示联系数量或开放报告，请核对原始资料。</p></article>'
    elif item_id:
        try:
            if item_id not in item_choices:
                raise ValueError('物资不在当前档案或本事件历史中。')
            def stock_word(value):
                try:
                    amount = contacts._quantity(value)
                    return '未知' if amount is None else f'{amount:g}'
                except ValueError:
                    return '待核对'
            sources = {('point', p.point_id): (p.name, '登记库存 ' +
                       stock_word(p.supplies[item_id]) + '；时效与送达待核')
                       for p in state.ledger.points.values() if item_id in p.supplies}
            sources.update({('company', c.company_id): (c.name, '名录线索，供应能力/数量/时间待联系核实')
                            for c in state.ledger.companies.values()})
            for record in history:
                data = json.loads(record.content_json)
                if data['item_id'] == item_id:
                    sources.setdefault((data['source_kind'], data['source_id']),
                        (data['source_snapshot']['source']['name'], '当前资料已移除，仅核对冻结历史'))
            for kind, title in (('point', '库内物资点'), ('company', '外部公司资料')):
                links = ''.join(f'<li><a href="{e(_link(event_id,item_id,kind,identifier))}">{e(name)}</a>'
                                f'<p class="lz-note">{e(note)}</p></li>'
                                for (k, identifier), (name, note) in sorted(sources.items()) if k == kind)
                content += f'<section class="lz-card supply-leads"><h3>{title}</h3><ul>{links or "<li>暂无登记或历史来源。</li>"}</ul></section>'
            if source:
                kind, _, identifier = source.partition(':')
                contacts._object_id(item_id, kind, identifier)
                content += card(state, token, event_id, item_id, kind, identifier)
        except ValueError:
            content += '<article class="lz-card"><h3>联系来源或审计待核对</h3><p>未展示联系数量或开放报告，请核对原始资料。</p></article>'
    return (
        '<style>.supply-contact-card,.supply-leads{min-width:0;overflow-wrap:anywhere}'
        '.supply-contact-card pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}'
        '.supply-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}'
        '.supply-fields .lz-input{width:100%;max-width:none;min-width:0;box-sizing:border-box}'
        '.supply-evidence{grid-column:1/-1}.supply-evidence textarea{min-height:90px}'
        '.supply-contact-card li{margin:10px 0}.supply-contact-card ol{padding-left:22px}'
        '.supply-contact-card details{margin:12px 0}.supply-message{white-space:pre-wrap}'
        '.supply-selector{display:flex;gap:12px;align-items:end;flex-wrap:wrap}'
        '@media(max-width:600px){.supply-fields{grid-template-columns:1fr}}</style>'
        f'<main id="material-supply" data-event="{e(event_id)}">' + content
        + '</main><script src="/static/material-supply.js" defer></script>')


def submit(state, token, fields, member):
    keys = {'event_id', 'csrf', 'item_id', 'source_kind', 'source_id', 'expected_source_digest',
            'available_quantity', 'promised_delivery_at', 'evidence', 'request_id',
            'expected_version', 'occurred_at'}
    if set(fields) != keys or any(len(v) != 1 for v in fields.values()):
        raise ValueError('联系字段缺失、重复或不受支持。')
    value = lambda key: fields[key][0]
    if (value('event_id') != state.active_event_id or
            not secrets.compare_digest(value('csrf').encode('utf-8'), member.custody_csrf.encode('utf-8'))):
        raise PermissionError('本人表单或当前事件已变化，请保留输入并重新核对。')
    raw = value('expected_version')
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        raise ValueError('联系记录版本不正确。')
    quantity = value('available_quantity')
    try:
        amount = None if quantity == '' else float(quantity)
    except ValueError as error:
        raise ValueError('可供量格式不正确，未知留空、无货填0。') from error
    return state.handle_material_supply_contact(value('event_id'), member_token=token,
        item_id=value('item_id'), source_kind=value('source_kind'), source_id=value('source_id'),
        expected_source_digest=value('expected_source_digest'), expected_version=int(raw),
        available_quantity=amount, promised_delivery_at=value('promised_delivery_at'),
        evidence=value('evidence'), request_id=value('request_id'), occurred_at=value('occurred_at'))
