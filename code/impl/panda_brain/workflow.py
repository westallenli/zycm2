"""Event-scoped navigation modes. Session preferences never confer business authority."""
import re
from . import business_modules as business
from . import sop_guide
from . import assistant_view
from .sand_shell import WORK_ITEMS
from .workbench import esc

RENDERERS = ('render_early_response', 'render_situation', 'render_force',
             'render_deployment', 'render_execution', 'render_cleanup',
             'render_emergency', 'render_publication', 'render_handover')

def closed_event(state,token):
    """Historical read scope from the original audited appointment only."""
    member=state.member_session(token)
    if member is None:return ''
    eid=member.selected_event_id or state.active_event_id
    record=state.registry.events.get(eid)
    if not record or record.status!='closed':return ''
    from .event_authorization import event_grants
    from .authorization import Role
    space,_,_=state._appointment_space(eid)
    return eid if any(g.subject_id==member.phone and g.role in {Role.DEPUTY,Role.REVIEW_OWNER} for g in event_grants(space)) else ''


def context(state, token):
    member = state.member_session(token)
    event_id = business.selected_event(state, token)
    historical=closed_event(state,token) if not event_id else ''
    event_id=event_id or historical
    if member is None or not event_id or (not historical and not business.role(state, token)):
        raise PermissionError('请先登录并确认本人职责范围内的事件。')
    record = state.registry.events[event_id]
    if record.status not in {'active', 'upgraded', 'closed'}:
        raise PermissionError('当前事件尚不可进入工作流程。')
    return member, record


def fragments(state):
    from . import sandview
    return [getattr(sandview, name)(state, 'commander') for name in RENDERERS]


def preference(member, event_id):
    return member.workflow_preferences.get(event_id, {'mode': 'direct', 'step': 0, 'node': 0})


def destination(step, node, event_id=''):
    native = {
        3:('/sand#cmd-plans','/sand/task-review','/sand/task-release','/sand/task-receipts'),
        4:('/sand/task-receipts','/sand/task-feedback','/sand/task-feedback'),
        5:('/sand#cmd-advisor','/sand/task-feedback','/workflow/records?step=5'),
        6:('/workflow/records?step=6','/materials/event','/workflow/records?step=6'),
    }
    if step in native:return native[step][node]
    target = sop_guide.PATHS[step][node][3]
    if target.startswith('/'):
        from .unified_ui import ALIASES
        if target == '/events' and event_id:
            return '/events/' + event_id + '/details'
        return ALIASES.get(target, target)
    if target in {'basis', 'draft'}:
        return '/sand#sop-basis-' + str(step)
    return '/sand#' + target


def form(member, event_id, mode, label, *, step=0, node=0, target='', switch=False, aria_label=''):
    fields = dict(event_id=event_id, csrf=member.custody_csrf, mode=mode,
                  step=str(step), node=str(node), target=target)
    return ('<form method="post" action="/workflow/mode"'
            + (' data-workflow-switch' if switch else '') + '>'
            + ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">'
                      for key, value in fields.items())
            + '<button class="lz-btn" type="submit"'+(' aria-label="'+esc(aria_label)+'"' if aria_label else '')+'>'+esc(label)+'</button></form>')


def choice(state, token):
    member, record = context(state, token)
    pref = preference(member, record.event_id)
    cards = []
    for mode, title, hint, target in (
        ('guide', '九步走引导', '跟着步骤走，缺什么系统提醒', '/workflow/guide'),
        ('direct', '功能直达', '直接选要办的事', '/workflow/functions')):
        fields = dict(event_id=record.event_id, csrf=member.custody_csrf, mode=mode,
                      step=str(pref['step']), node=str(pref['node']), target=target)
        hidden = ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">'
                         for key, value in fields.items())
        # 整张卡就是一个提交按钮：不用再在卡里找那个"进入"按钮。
        cards.append(
            '<form method="post" action="/workflow/mode" class="wf-card">' + hidden
            + f'<button type="submit" aria-label="{esc(title)}：{esc(hint)}">'
            + f'<b>{esc(title)}</b><span>{esc(hint)}</span></button></form>')
    note = '两种方式数据互通，可随时切换；查看或选择环节不代表已完成'
    return ('<section class="workflow-intro" data-workflow-choice>'
            f'<p class="wf-state">{esc(record.status_word())}</p>'
            f'<h1>{esc(record.name)}</h1>'
            f'<p class="wf-meta" title="{esc(note)}">{esc(record.location)} · {esc(record.event_id)}</p>'
            '</section>'
            '<div class="workflow-choices">' + ''.join(cards) + '</div>'
            '<div class="wf-foot">'
            '<a class="lz-btn lz-btn2" href="/events?list=1">其它事件</a>'
            '<a class="lz-btn lz-btn2" href="/workflow/inbox">待我补充</a></div>')


def guide_fragment(state, token, parts=None, assistance=None, *, step_override=None, action_renderer=None):
    member, record = context(state, token)
    pref = preference(member, record.event_id)
    if step_override is not None:pref=dict(pref,step=step_override,node=0)
    step = pref['step']
    if record.status=='closed':
        from .nine_flow_view import overview
        return overview(state)+'<p><a class="lz-btn" href="/workflow/records?step='+str(step)+'">查看本环节历史办理与交接</a></p>'
    parts = parts if parts is not None else fragments(state)
    def action(i, target, label):
        node = next(j for j, path in enumerate(sop_guide.PATHS[i]) if path[3] == target and path[4] == label)
        if action_renderer:return action_renderer(node,label)
        return form(member, record.event_id, 'guide', label, step=i, node=node, target=destination(i, node, record.event_id))
    original = sop_guide.guide(step, parts[step], action_renderer=action, phase_override=pref['node'])
    assistance = assistance or assistant_view.current(state,record.event_id)
    def manual(node,title):
        if assistance['steps'][step].get('work_record',{}).get('value'):
            return '<a class="lz-btn" href="/workflow/records?step='+str(step)+'">办理／核验</a>'
        if action_renderer:return action_renderer(node,'办理：'+title)
        return form(member,record.event_id,'guide','办理',step=step,node=node,
                    target=destination(step,node,record.event_id),aria_label='办理：'+title)
    return (assistant_view.render(state,assistance,step,original,manual)
        + _gap_assign_block(state, member, record, step, assistance)
        + '<p><a class="lz-btn lz-btn2" href="/workflow/records?step='+str(step)+'">本环节办理、核验与交接</a></p>')


def _gap_assign_block(state, member, record, step, assistance):
    """第 3 步：看到缺口就派给具名组长，派完显示"已派给谁、什么时候补"。"""
    if step != 2:
        return ''
    gaps = [str(g) for g in (assistance['steps'][step].get('gaps') or []) if str(g).strip()]
    if not gaps:
        return ''
    try:
        from .nine_flow import candidates
        people = [(phone, name) for phone, name in candidates(state).items() if phone != member.phone]
    except Exception:  # noqa: BLE001 — 取不到候选就只显示缺口，不给派单
        people = []
    if not people:
        return ''
    assigned = {}
    try:
        for row in state.workspace.gap_assignments():
            assigned[str(row.get('gap_text', ''))] = row
    except Exception:  # noqa: BLE001
        assigned = {}
    library = state.registry.library
    def who(phone):
        row = library.get(phone)
        return row.name if row else phone
    import secrets as _secrets
    opts = ''.join(f'<option value="{esc(phone)}">{esc(name)}</option>' for phone, name in people)
    due_opts = ''.join(f'<option value="{esc(word)}">{esc(word)}</option>'
                       for word in ('2 小时内', '4 小时内', '今晚', '明早'))
    rows = []
    for text in gaps[:8]:
        done = assigned.get(text)
        if done:
            rows.append('<li class="gap-done"><span>' + esc(text) + '</span>'
                        '<b>已派给 ' + esc(who(done.get('owner_phone', ''))) + ' · '
                        + esc(done.get('due_at', '')) + ' 前补齐</b></li>')
            continue
        rows.append(
            '<li><span>' + esc(text) + '</span>'
            '<form method="post" action="/workflow/assign-gap" class="gap-assign">'
            f'<input type="hidden" name="csrf" value="{esc(member.custody_csrf)}">'
            f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
            f'<input type="hidden" name="request_id" value="gap-{_secrets.token_hex(8)}">'
            f'<input type="hidden" name="gap_text" value="{esc(text)}">'
            f'<select class="lz-select" name="owner">{opts}</select>'
            f'<select class="lz-select" name="due">{due_opts}</select>'
            '<button class="lz-btn">派给他</button></form></li>')
    return ('<p class="gap-title">缺口派单（看到缺口就派人）</p>'
            '<ul class="gap-list">' + ''.join(rows) + '</ul>')


def guide_page(state, token, step_override=None):
    member, record = context(state, token)
    pref = preference(member, record.event_id)
    if step_override is not None:
        pref = dict(pref, step=step_override, node=0)
    step = pref['step']
    parts = fragments(state)
    assistance = assistant_view.current(state,record.event_id)
    states = assistant_view.status_labels(assistance)
    cards = []
    for i, (title, _, _) in enumerate(WORK_ITEMS):
        cards.append('<section class="workflow-step '+assistance['steps'][i]['phase_status']+(' selected' if i == step else '')+'" data-phase-status="'+assistance['steps'][i]['phase_status']+'">'
                     + f'<span>{i + 1:02d}</span><h2>{title}</h2><p>{esc(states[i])}</p>'
                     + ('<a class="lz-btn" href="/workflow/records?step='+str(i)+'">查看历史记录</a>' if record.status=='closed' else
                        f'<a class="lz-btn" href="/workflow/guide?step={i}">查看本环节</a>') + '</section>')
    return ('<section class="workflow-intro workflow-guide-intro"><p>' + esc(record.name) + '</p><h1>九步走引导</h1>'
            '<p class="workflow-legend"><span class="legend-completed">绿色：当前范围有核验依据</span><span class="legend-pending">灰色：待核验或仍在办理</span><span>蓝框：当前查看；模拟资料单独标注</span></p></section>'
            '<div class="workflow-overview">' + ''.join(cards) + '</div>'
            + f'<section class="lz-card workflow-current" data-workflow-current="{step}"><h2>{step + 1:02d} · {WORK_ITEMS[step][0]}</h2>'
            + '<div data-workflow-guide>' + guide_fragment(state, token, parts, assistance, step_override=step) + '</div></section>')


def functions_page(state, token):
    _, record = context(state, token)
    return ('<section class="workflow-intro"><p>' + esc(record.name) + '</p><h1>功能直达</h1>'
            '<p>选择需要使用的业务模块。</p></section><div class="workflow-functions">'
            + ''.join('<a class="lz-card" href="' + route + '"><h2>' + title + '</h2><span>进入模块 →</span></a>'
                      for route, title in business.MODULES.items()) + '</div>')


def bar(state, token):
    member, record = context(state, token)
    pref = preference(member, record.event_id)
    mode, step, node = pref['mode'], pref['step'], pref['node']
    if mode == 'guide':
        from .panel import _deputy_event_context
        with _deputy_event_context(state, '/workflow/guide', member_token=token):
            assistance = assistant_view.current(state,record.event_id)
            state_word = assistant_view.status_labels(assistance)[step]
        instruction = '智能体持续核对资料；请处理异常与需要决定的事项。'
        body = (f'<div class="workflow-status"><small>{esc(record.name)} · 当前环节</small>'
                f'<strong>{step + 1:02d} · {WORK_ITEMS[step][0]}</strong><span data-workflow-state>{esc(state_word)}</span></div>'
                + '<p class="workflow-instruction"><span class="workflow-instruction-full">' + esc(instruction)
                + '</span><span class="workflow-instruction-compact">自动核对与补问</span></p>'
                + '<a class="lz-btn lz-btn2" href="/workflow/guide">返回九步走</a>'
                + form(member, record.event_id, 'direct', '功能直达', step=step, node=node, switch=True))
    else:
        body = ('<strong>' + esc(record.name) + '</strong><a href="/workflow/functions">功能直达</a>'
                + form(member, record.event_id, 'guide', '九步走引导', step=step, node=node, switch=True))
    runtime = getattr(state,'assistant_runtime',None)
    pending = len([q for q in runtime.questions(record.event_id,recipient=member.phone) if q['status']=='pending']) if runtime else 0
    if pending:
        body += '<a class="workflow-inbox-link" href="/workflow/inbox">待我补充 '+str(pending)+'</a>'
    return f'<aside class="workflow-bar {mode}" data-workflow-bar data-workflow-mode="{mode}" data-workflow-event="{esc(record.event_id)}" aria-label="事件使用方式">{body}</aside>'


def decorate(state, route, html, token):
    if 'data-workflow-bar' in html or '<body' not in html or 'data-sand-role="public"' in html:
        return html
    from .unified_ui import parent
    if not parent(route) and not route.startswith('/workflow'):
        return html
    try:
        toolbar = '' if 'data-workflow-choice' in html else bar(state, token)
        if route=='/workflow/records':
            _,record=context(state,token)
            toolbar=('<aside class="workflow-bar direct" data-workflow-bar data-workflow-mode="records" data-workflow-event="'+esc(record.event_id)+'" aria-label="事件办理记录">'
                '<strong>'+esc(record.name)+'</strong><a class="lz-btn lz-btn2" href="/workflow/guide">返回九步走</a>'
                '<a class="lz-btn lz-btn2" href="/review?tab=timeline">查看五步复盘</a></aside>')
    except PermissionError:
        return html
    # Sand retains its original evidence/actions for explicitly requested destinations.
    # The global guide owns the overview; the map no longer defaults to a SOP window.
    html = html.replace('<body', '<body data-global-workflow="true"', 1)
    header = re.search(r'</header>', html)
    if header:
        html = html[:header.end()] + toolbar + html[header.end():]
    else:
        html = re.sub(r'(<body[^>]*>)', r'\1' + toolbar, html, count=1)
    assets = '<link rel="stylesheet" href="/static/workflow.css"><script src="/static/workflow.js" defer></script>'
    if route=='/sand':
        from . import sop_sand
        panel=sop_sand.shell(state,token)
        html=re.sub(r'(<div[^>]*\bid="cmd-page"[^>]*>)',lambda match:match[0]+panel,html,count=1)
    return html.replace('</head>', assets + '</head>', 1)


def select(state, token, fields):
    member, record = context(state, token)
    expected = {'event_id', 'csrf', 'mode', 'step', 'node', 'target'}
    if set(fields) != expected or any(len(values) != 1 for values in fields.values()):
        raise ValueError('使用方式字段无效。')
    value = {name: values[0] for name, values in fields.items()}
    if value['csrf'] != member.custody_csrf or value['event_id'] != record.event_id:
        raise PermissionError('事件或本人会话已变化，请重新进入事件。')
    if value['mode'] not in {'guide', 'direct'} or not value['step'].isdigit() or not value['node'].isdigit():
        raise ValueError('使用方式或环节无效。')
    step, node = int(value['step']), int(value['node'])
    if step not in range(9) or node not in range(len(sop_guide.PATHS[step])):
        raise ValueError('环节或操作节点无效。')
    target = value['target']
    if target not in {'', '/workflow/guide', '/workflow/functions', destination(step, node, record.event_id)}:
        raise ValueError('只能进入当前环节对应的系统功能。')
    # Commit only after all validation; not persisted as an appointment or business result.
    member.workflow_preferences[record.event_id] = dict(mode=value['mode'], step=step, node=node)
    return target or ('/workflow/guide' if value['mode'] == 'guide' else '/workflow/functions')
