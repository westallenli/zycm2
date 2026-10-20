"""Own-session, current-fire model configuration for an appointed deputy."""
from dataclasses import replace
import json
import secrets
from urllib.parse import parse_qs, quote

from . import model_connections as models
from .workbench import esc

ROUTE = '/events/model-settings'


def authority(state, token):
    from .sand_command import context
    member, eid = context(state, token)
    if eid not in state.deputy_events(token):
        raise PermissionError('大模型配置仅当前火情的指定副指挥可办理。')
    return member, eid


def session(state, token):
    member, eid = authority(state, token)
    return state.sand_deputy_models.get((token, eid), models.Session(csrf=member.custody_csrf))


def provider(state, token, eid):
    _, current = authority(state, token)
    if current != eid:
        raise PermissionError('模型配置不属于当前火情。')
    value = state.sand_deputy_models[(token, eid)]
    connection = value.selected
    if not connection.enabled or not connection.api_key:
        raise ValueError('本火情大模型未启用，请从火情页面进入“大模型配置（Key）”保存Key并启用。')
    from .audited_records import digest
    return models.SafeProvider(connection), 'deputy-' + digest(dict(owner=token, event=eid, revision=value.revision)), token


def forget(state, token):
    state.sand_deputy_models = {k:v for k,v in state.sand_deputy_models.items() if k[0] != token}
    state.deepseek_previews = {k:v for k,v in state.deepseek_previews.items() if v.get('owner') != token}


def change(state, token, fields):
    member, eid = authority(state, token)
    common = {'csrf', 'event_id', 'expected_revision', 'action'}
    action = fields.get('action')
    expected = common | ({'api_key', 'model', 'timeout'} if action == 'save' else set())
    if set(fields) != expected or fields.get('event_id') != eid:
        raise ValueError('模型表单字段或火情已变化，请重新打开配置页。')
    if not secrets.compare_digest(fields.get('csrf', ''), member.custody_csrf):
        raise PermissionError('模型表单已失效，请重新打开配置页。')
    key = (token, eid)
    with state._model_connections_lock:
        current = session(state, token)
        if fields.get('expected_revision') != str(current.revision):
            raise ValueError('模型配置版本已变化，请重新打开页面核对。')
        connection = current.cloud
        if action == 'save':
            connection = models.profile('deepseek', models.CLOUDS['deepseek'][1], fields['model'],
                fields['api_key'] or connection.api_key, connection.enabled, fields['timeout'])
            updated = replace(current, cloud=connection, mode='cloud', revision=current.revision+1,
                cloud_status='配置已保存，尚未测试。')
        elif action in {'enable', 'disable', 'clear'}:
            if action == 'enable' and not connection.api_key:
                raise ValueError('请先填写并保存 DeepSeek API Key，再启用。')
            connection = replace(connection, enabled=action == 'enable',
                api_key='' if action == 'clear' else connection.api_key)
            updated = replace(current, cloud=connection, mode='cloud', revision=current.revision+1,
                cloud_status='凭证已清除。' if action == 'clear' else current.cloud_status)
        elif action == 'test':
            if not connection.enabled or not connection.api_key:
                raise ValueError('请先保存Key并启用大模型，再测试连接。')
            state.sand_deputy_models.setdefault(key, current)
            updated = None
        else:
            raise ValueError('未知模型配置操作。')
        if updated is not None:
            state.sand_deputy_models[key] = updated
            state.deepseek_previews = {k:v for k,v in state.deepseek_previews.items()
                if not (v.get('owner') == token and v.get('event_id') == eid)}
            return
    # Connectivity tests contain no event data. Never restore credentials after
    # concurrent logout, reassignment, disable, or another configuration change.
    try:
        models.SafeProvider(connection).probe()
        status = '连接测试通过：服务可达，Key 验证通过。实际问答请进入“大模型支撑”。'
    except models.ConnectionUnavailable as exc:
        status = '连接测试失败：' + str(exc)
    except Exception:
        status = '连接测试未完成，请稍后再试或核对服务状态。'
    with state._model_connections_lock:
        authority(state, token)
        if state.sand_deputy_models.get(key) is current:
            state.sand_deputy_models[key] = replace(current, cloud_status=status)


def event_entry(state, token=None, *, admin=False):
    href = '/models'
    if not admin:
        from .business_modules import selected_event
        try:
            eid = selected_event(state, token)
            if not eid or eid not in state.deputy_events(token):
                return ''
        except PermissionError:
            return ''
        href = ROUTE + '?event_id=' + quote(eid, safe='')
    return ('<div class="event-model-entry" style="display:flex;align-items:center;justify-content:space-between;'
        'gap:12px;flex-wrap:wrap;padding:14px 16px;margin:0 0 18px;background:#eef5ff;border:2px solid #2474ed;border-radius:10px">'
        '<span><b>大模型支撑</b><br><small>填写Key、测试连接、启用或停用</small></span>'
        '<a data-model-config-entry href="'+href+'" style="display:inline-block;background:#1769dc;color:#fff;'
        'font-size:17px;font-weight:700;padding:12px 18px;border-radius:8px;text-decoration:none">大模型配置（Key）</a></div>')


def decorate(state, page, token=None, *, admin=False):
    entry = event_entry(state, token, admin=admin)
    return page.replace('<div class="lz-wrap">', '<div class="lz-wrap">'+entry, 1) if entry else page


def page(state, token, *, error=''):
    from .panel import _light_wrap, _light_nav, _COMMANDER_LIGHT_NAV, _topbar_logout_form
    member, eid = authority(state, token)
    value = session(state, token)
    hidden = (f'<input type="hidden" name="csrf" value="{esc(member.custody_csrf)}">'
        f'<input type="hidden" name="event_id" value="{esc(eid)}">'
        f'<input type="hidden" name="expected_revision" value="{value.revision}">')
    def form(contents):
        return '<form method="post" action="'+ROUTE+'">'+hidden+contents+'</form>'
    body = ('<style>.deputy-model-page{max-width:760px;margin:auto}.deputy-model-page h1{font-size:26px}'
        '.deputy-model-page label{display:block;margin:14px 0}.deputy-model-page input{display:block;box-sizing:border-box;'
        'width:100%;padding:13px;background:white;color:#173253;border:1px solid #91a9c5;border-radius:7px}'
        '.deputy-model-page input[type=password]{border:2px solid #2474ed;font-size:17px}'
        '.deputy-model-page button{padding:11px 16px;margin:6px 8px 6px 0;border:1px solid #2875df;border-radius:7px;'
        'background:#fff;color:#1257b4;cursor:pointer;font:inherit}.deputy-model-page button[value=save],'
        '.deputy-model-page button[value=enable]{background:#1769dc;color:white}'
        '.deputy-model-status{padding:12px;background:#eef5ff;border-radius:8px}.deputy-model-page [role=alert]{color:#a22525}'
        '#model-test-result{padding:12px;border:1px solid #b7cde7;border-radius:7px;overflow-wrap:anywhere}'
        '#model-test-result[data-state=pending]{background:#fff4d4}#model-test-result[data-state=success]{background:#e8f5eb;color:#165b28}'
        '#model-test-result[data-state=error]{background:#fff0ed;color:#a22525}.deputy-model-page button:disabled{opacity:.6;cursor:wait}'
        '@media(max-width:600px){.deputy-model-page h1{font-size:22px}.deputy-model-page button{padding:11px}}</style>'
        '<section class="lz-card deputy-model-page"><h1>大模型配置（Key）</h1>'
        f'<p>{esc(state.registry.events[eid].name)} · 当前火情 / 本次登录</p>'
        + (f'<p role="alert">{esc(error)}</p>' if error else '')
        + '<div class="deputy-model-status" role="status"><b>'
        + ('大模型已启用' if value.cloud.enabled and value.cloud.api_key else '大模型已停用')
        + '</b> · '+('Key已配置' if value.cloud.api_key else '尚未配置Key')+'<br><span id="model-test-summary">'+esc(value.cloud_status)+'</span></div>'
        + form('<label for="deputy-api-key"><b>DeepSeek API Key</b>'
            '<input id="deputy-api-key" name="api_key" type="password" autocomplete="new-password" maxlength="4096" '
            'placeholder="在这里粘贴Key；已配置时留空保留"></label>'
            '<p>先保存Key，再启用和测试连接。Key只保存在本次登录中，退出或重启后需重新填写。</p>'
            '<details><summary>模型名称与超时</summary>'
            f'<label>模型名称<input name="model" value="{esc(value.cloud.model)}" required maxlength="160"></label>'
            f'<label>超时（秒）<input name="timeout" value="{value.cloud.timeout}" type="number" min="1" max="60" required></label>'
            '<p>服务：DeepSeek 官方 API</p></details><button name="action" value="save">保存Key配置</button>')
        + form('<button name="action" value="enable">启用大模型</button><button name="action" value="disable">停用大模型</button>'
            f'<button name="action" value="test" data-model-test data-timeout="{value.cloud.timeout}" formaction="{ROUTE}#model-test-result">测试连接</button><button name="action" value="clear">清除Key</button>')
        + f'<p id="model-test-result" role="status" aria-live="polite">{esc(error or value.cloud_status)}</p>'
        + '<p>连接测试只检查服务和Key；选择“大模型支撑”并点击发送后直接问询。</p>'
        '<p><a href="/events">返回火情页面</a> · <a href="/sand">进入沙盘</a></p></section><script src="/static/model-test.js"></script>')
    return _light_wrap(title='大模型配置（Key）', body=body,
        nav=_light_nav(_COMMANDER_LIGHT_NAV, '/events'), body_class=' lz-model-page',
        user=f'<span>副指挥长 {esc(member.name)}</span>'+_topbar_logout_form('/logout/member', '退出登录'))


def post(handler, state):
    from .http_rejection import request_length
    from .panel import _deputy_event_context
    token = handler._member_cookie()
    try:
        length = request_length(handler, 14000)
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise ValueError('模型设置表单格式无效。')
        values = parse_qs(handler.rfile.read(length).decode('utf-8'), keep_blank_values=True,
            errors='strict', max_num_fields=12)
        if any(len(v) != 1 for v in values.values()):
            raise ValueError('模型设置不接受重复字段。')
        fields = {k:v[0] for k,v in values.items()}
        with state._event_authority_lock, _deputy_event_context(state, '/sand/model-settings', member_token=token):
            change(state, token, fields)
        handler._redirect(ROUTE+'?event_id='+quote(fields['event_id'], safe='')+('#model-test-result' if fields.get('action')=='test' else ''))
    except PermissionError:
        handler._respond(403, '<h1>403</h1><p>请使用当前火情的副指挥账号重新登录，并从火情页打开模型配置。</p>')
    except (ValueError, UnicodeError) as exc:
        try:
            with state._event_authority_lock, _deputy_event_context(state, '/sand/model-settings', member_token=token):
                html = page(state, token, error=str(exc))
            handler._respond(422, html)
        except PermissionError:
            handler._respond(403, '<h1>403</h1>')
