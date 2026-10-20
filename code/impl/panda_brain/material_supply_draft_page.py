"""Existing command drafting authority, reported supply evidence and frozen history."""
from dataclasses import asdict
import hashlib
import hmac
from html import escape
import json
import secrets
from urllib.parse import urlencode

from . import material_supply_contacts as contacts
from . import material_supply_drafts as drafts

e = lambda value: escape(str(value), quote=True)


def csrf(state, token):
    return hmac.new(state._review_form_secret, ('supply-draft:' + str(token or '')).encode(),
                    hashlib.sha256).hexdigest()


def require_page(state, token, event_id, *, for_write=False):
    if not state.command_mode(token):
        raise PermissionError('补给任务编制需要有效的指挥入口会话。')
    if not event_id or event_id != state.active_event_id or state.workspace.event_id != event_id:
        raise PermissionError('当前事件已变化，请保留输入并重新核对。')
    event = state.registry.events.get(event_id)
    allowed = {'active', 'upgraded'} if for_write else {'active', 'upgraded', 'closed'}
    if event is None or event.status not in allowed:
        raise PermissionError('当前事件不接受新的补给草案附件。')
    space, _, _ = state._appointment_space(event_id)
    if space.verify_audit_chain():
        raise ValueError('事件审计来源待核对。')
    return space


def selection(state, query):
    query = query or {}
    keys = {'event_id', 'decision_id', 'task_id', 'item_id'}
    if set(query) - keys or any(len(value) != 1 for value in query.values()):
        raise ValueError('草案查询字段重复或不受支持。')
    result = tuple(query.get(key, [state.active_event_id or '' if key == 'event_id' else ''])[0]
                   for key in ('event_id', 'decision_id', 'task_id', 'item_id'))
    if bool(result[1]) != bool(result[2]) or result[3] and not result[2]:
        raise ValueError('请先选择本事件草案及任务。')
    return result


def link(event_id, decision_id, task_id, item_id=''):
    return '/supply-draft?' + urlencode(dict(event_id=event_id, decision_id=decision_id,
                                            task_id=task_id, item_id=item_id))


def _quantity(value):
    return '未知' if value is None else f'{value:g}'


def _history(view):
    rows = []
    for record in reversed(view.records):
        data = json.loads(record.content_json)
        rows.append(
            f'<li data-supply-record="{e(record.id)}" data-request="{e(record.request_id)}" '
            f'data-attachment-version="{record.reference.version}" '
            f'data-base-bundle-version="{data["base_bundle_version"]}" '
            f'data-base-bundle-digest="{e(data["base_bundle_digest"])}" '
            f'data-item-digest="{e(data["item_digest"])}">'
            f'<b>附件第 {record.reference.version} 版</b> · 人工需求 {_quantity(data["requested_quantity"])} '
            f'{e(data["item_snapshot"]["unit"])}；关联草案 v{data["bundle_snapshot"]["version"]}'
            f'<p>人工数量依据：{e(data["quantity_basis"] or "待核")}</p>'
            f'<p>需求期限：{e(data["needed_by"] or "未知")}；接收提示：{e(data["destination_hint"] or "待定")}</p>'
            f'<p>库内优先理由：{e(data["internal_priority_reason"] or "待核")}；'
            f'编制依据：{e(record.source)}；记录时间：{e(record.times.recorded_at)}</p>'
            '<details><summary>完整冻结任务、物资与联系依据</summary><pre>'
            + e(json.dumps(data, ensure_ascii=False, indent=2)) + '</pre></details></li>')
    return '<details open><summary>逐版附件与冻结依据</summary><ol>' + (
        ''.join(rows) or '<li>尚未编制此任务物资的补给附件。</li>') + '</ol></details>'


def _form(state, token, space, bundle, task_id, item_id, view):
    item = state.ledger.items.get(item_id)
    if (state.registry.events[bundle.event_id].status not in {'active', 'upgraded'}
            or bundle.status != 'draft' or item is None or not any(t.id == task_id for t in bundle.tasks)):
        return '<p>当前仅可核对冻结历史，不开放新附件。</p>'
    snapshot = asdict(item)
    drafts._item(snapshot, item_id)
    selected, internal = [], []
    all_contacts = contacts._records(space, state.ledger, bundle.event_id)
    for record in all_contacts:
        data = json.loads(record.content_json)
        if data['item_id'] != item_id:
            continue
        latest = max((r for r in all_contacts if r.reference.object_id == record.reference.object_id),
                     key=lambda r: r.reference.version)
        if latest != record:
            continue
        try:
            valid = contacts._digest(contacts._current_snapshot(state.ledger, item_id,
                data['source_kind'], data['source_id'])) == data['source_digest']
        except ValueError:
            valid = False
        label = ('库内点' if data['source_kind'] == 'point' else '外部公司') + ' · ' + data['source_snapshot']['source']['name']
        label += f' · 可供量 {_quantity(data["available_quantity"])} · 送达 {data["promised_delivery_at"] or "未知"}'
        option = f'<option value="{e(record.id)}"{" disabled" if not valid else ""}>{e(label + ("（来源已变，待核）" if not valid else ""))}</option>'
        selected.append(option)
        if data['source_kind'] == 'point':
            internal.append(option)
    hidden = dict(event_id=bundle.event_id, decision_id=bundle.decision_id, task_id=task_id,
        item_id=item_id, csrf=csrf(state, token), expected_bundle_version=bundle.version,
        expected_bundle_digest=contacts._digest(asdict(bundle)),
        expected_item_digest=contacts._digest(snapshot), expected_version=view.version,
        request_id=secrets.token_urlsafe(18))
    return ('<h3>编制下一版补给附件</h3><form class="supply-draft-form" method="post" action="/supply-draft/save">'
        + ''.join(f'<input type="hidden" name="{key}" value="{e(value)}">' for key, value in hidden.items())
        + '<div class="supply-draft-fields"><label>人工需求量（未知留空，零须写依据）'
        '<input class="lz-input" type="number" min="0" step="any" name="requested_quantity"></label>'
        '<label>人工数量依据<textarea class="lz-input" name="quantity_basis" maxlength="200"></textarea></label>'
        '<label>需求期限（未知留空；明确时须含时区）<input class="lz-input" name="needed_by" '
        'placeholder="例如 2026-10-10T18:00:00+08:00" maxlength="50"></label>'
        '<label>接收对象或位置提示（待核文字）<input class="lz-input" name="destination_hint" maxlength="200"></label>'
        '<label>供给联系报告<select class="lz-input" name="contact_id"><option value="">来源待核</option>'
        + ''.join(selected) + '</select></label><label>库内联系依据（可多选）'
        '<input type="hidden" name="internal_contact_ids" value="">'
        '<select class="lz-input" name="internal_contact_ids" multiple size="3">' + ''.join(internal) + '</select></label>'
        '<label>库内无货、不足或时效理由<textarea class="lz-input" name="internal_priority_reason" maxlength="200"></textarea></label>'
        '<label>本次编制依据<textarea class="lz-input" name="evidence" required maxlength="600"></textarea></label></div>'
        '<p class="lz-note">保存会修订整份任务草案，旧版分类须重做；人工申报与联系可供量不会代填自动缺口。</p>'
        '<button class="lz-btn" type="submit">保存补给附件</button></form>')


def main(state, token, event_id, decision_id='', task_id='', item_id=''):
    space = require_page(state, token, event_id)
    identity = f'data-event="{e(event_id)}" data-decision="{e(decision_id)}" data-task="{e(task_id)}" data-item="{e(item_id)}"'
    start = f'<main id="supply-draft-page" {identity}><section class="lz-card"><h2>编制补给任务附件</h2>'
    start += f'<p>{e(state.registry.events[event_id].name)} · {e(event_id)}</p>'
    start += '<p class="lz-note">人工需求、自动缺口和联系可供量分开核对。草案未发布，不占用或增加库存；正式发布仍须专业、安全及具名依据。</p></section>'
    try:
        history = drafts._records(space, state.ledger, event_id)
        bundles = {b.decision_id: space.task_drafts_for_decision(b.decision_id, event_id=event_id)
                   for b in space.task_drafts.values()}
        tasks = {(b.decision_id, t.id): t.spec.title or ('总任务' if not t.parent_id else '关联子任务')
                 for b in bundles.values() if b for t in b.tasks}
        for record in history:
            data = json.loads(record.content_json)
            tasks.setdefault((data['decision_id'], data['task_id']), '已移除任务（冻结历史）')
        if not decision_id:
            start += '<section class="lz-card"><h3>选择草案任务</h3><ul>' + ''.join(
                f'<li><a href="{e(link(event_id, d, t))}">{e(name)} · {e(t)}</a></li>'
                for (d, t), name in tasks.items()) + '</ul></section>'
        else:
            if (decision_id, task_id) not in tasks:
                raise ValueError('草案任务归属不一致。')
            bundle = bundles[decision_id]
            choices = {i.item_id: (i.name, i.unit) for i in state.ledger.items.values()}
            for record in history:
                data = json.loads(record.content_json)
                if data['decision_id'] == decision_id and data['task_id'] == task_id:
                    item = data['item_snapshot']
                    choices.setdefault(item['item_id'], (item['name'] + '（历史资料）', item['unit']))
            start += '<section class="lz-card"><h3>' + e(tasks[(decision_id, task_id)]) + '</h3>'
            start += '<form method="get" action="/supply-draft" class="supply-draft-selector">'
            start += ''.join(f'<input type="hidden" name="{key}" value="{e(value)}">'
                for key, value in dict(event_id=event_id, decision_id=decision_id, task_id=task_id).items())
            start += '<label>所需物资<select class="lz-input" name="item_id"><option value="">请选择物资</option>'
            start += ''.join(f'<option value="{e(identifier)}"{" selected" if identifier == item_id else ""}>'
                            f'{e(name)} · {e(unit)}</option>' for identifier, (name, unit) in choices.items())
            start += '</select></label><button class="lz-btn lz-btn2" type="submit">核对附件及联系依据</button></form></section>'
            if item_id:
                if item_id not in choices:
                    raise ValueError('物资资料不在当前或历史中。')
                view = drafts.review(space, state.ledger, event_id=event_id,
                    decision_id=decision_id, task_id=task_id, item_id=item_id)
                start += f'<section class="lz-card supply-draft-card"><h3>{e(choices[item_id][0])}</h3>'
                start += f'<p>当前草案 v{bundle.version}；附件 v{view.version}；审计续接 {len(view.continued_by)} 次</p>'
                start += '<ul>' + ''.join(f'<li>{e(value)}</li>' for value in view.blockers) + '</ul>'
                start += _history(view) + _form(state, token, space, bundle, task_id, item_id, view) + '</section>'
    except (ValueError, KeyError, TypeError):
        # Discard the partial body: no quantities or form from an invalid source.
        start = start.split('</section>', 1)[0] + '</section>'
        start += '<section class="lz-card"><h3>任务、联系或审计来源待核对</h3><p>未展示附件数量或开放编制。</p></section>'
    return start + '<p class="supply-draft-message" role="status" aria-live="polite"></p></main>'


def body(state, token, *selection):
    return ('<style>#supply-draft-page{min-width:0;overflow-wrap:anywhere}'
        '#supply-draft-page pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}'
        '.supply-draft-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}'
        '.supply-draft-fields label{display:grid;gap:6px;min-width:0}'
        '#supply-draft-page .lz-input{width:100%;max-width:none;min-width:0;box-sizing:border-box}'
        '.supply-draft-fields textarea{min-height:72px}.supply-draft-selector{display:flex;flex-wrap:wrap;gap:12px;align-items:end}'
        '#supply-draft-page li{margin:12px 0}.supply-draft-message{white-space:pre-wrap}'
        '@media(max-width:600px){.supply-draft-fields{grid-template-columns:1fr}}</style>'
        + main(state, token, *selection) + '<script src="/static/supply-draft.js" defer></script>')


def submit(state, token, fields):
    keys = {'event_id', 'decision_id', 'task_id', 'item_id', 'csrf', 'expected_bundle_version',
        'expected_bundle_digest', 'expected_item_digest', 'expected_version', 'request_id',
        'requested_quantity', 'quantity_basis', 'needed_by', 'destination_hint', 'contact_id',
        'internal_contact_ids', 'internal_priority_reason', 'evidence'}
    if set(fields) != keys or any(len(v) != 1 for k, v in fields.items() if k != 'internal_contact_ids'):
        raise ValueError('附件字段缺失、重复或不受支持。')
    value = lambda key: fields[key][0]
    if not secrets.compare_digest(value('csrf').encode(), csrf(state, token).encode()):
        raise PermissionError('当前会话表单校验失效，请保留原输入。')
    space = require_page(state, token, value('event_id'), for_write=True)
    drafts._records(space, state.ledger, value('event_id'))
    ids = fields['internal_contact_ids']
    if ids.count('') != 1 or len(ids) > 21:
        raise ValueError('库内联系引用数量或格式不正确。')
    versions = []
    for key in ('expected_bundle_version', 'expected_version'):
        raw = value(key)
        if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise ValueError('任务或附件版本不正确。')
        versions.append(int(raw))
    for key, limit in (('quantity_basis', 200), ('internal_priority_reason', 200),
                       ('destination_hint', 200), ('evidence', 600), ('needed_by', 50)):
        if len(value(key)) > limit:
            raise ValueError('附件文字超过本表单支持范围。')
    try:
        quantity = None if value('requested_quantity') == '' else float(value('requested_quantity'))
    except ValueError as error:
        raise ValueError('人工需求量格式不正确。') from error
    return state.handle_material_supply_draft(value('event_id'), session_token=token,
        decision_id=value('decision_id'), task_id=value('task_id'), item_id=value('item_id'),
        expected_bundle_version=versions[0], expected_version=versions[1],
        expected_bundle_digest=value('expected_bundle_digest'), expected_item_digest=value('expected_item_digest'),
        requested_quantity=quantity, quantity_basis=value('quantity_basis'), needed_by=value('needed_by'),
        destination_hint=value('destination_hint'), evidence=value('evidence'), request_id=value('request_id'),
        contact_id=value('contact_id'), internal_contact_ids=tuple(i for i in ids if i),
        internal_priority_reason=value('internal_priority_reason'))
