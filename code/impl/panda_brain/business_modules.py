"""Read scopes for the shared five modules; never grants action authority."""
from .workbench import esc
from .task_recipients import recipient_route

LABELS = {'duty': '值班人员', 'deputy': '副指挥长', 'captain': '队长／组长'}
MODULES = {'/events': '火情事件', '/': '人员架构', '/sand': '沙盘指挥',
           '/materials': '人员与物资', '/review': '复盘'}


def teams(state, token, event_id):
    """Only current leaders proven by this event's audited recipient route."""
    actor = state.authorization_subject(token)
    record = state.registry.events.get(event_id)
    if record is None or record.status not in {'active', 'upgraded'}:
        return ()
    from .temporary_group_readiness import leader_state
    space, _, _ = state._appointment_space(event_id)
    groups = set(state.ledger.squads) | set(leader_state(space, event_id, ledger=state.ledger))
    return tuple(sorted(group for group in groups if recipient_route(
        space, event_id, group, registry=state.registry, ledger=state.ledger).leader_id == actor))


def role(state, token):
    from .panel import PanelActionError
    try:
        actor = state.authorization_subject(token)
        try:
            state.duty_subject(token)
            return 'duty'
        except PermissionError:
            pass
        if state.deputy_events(token):
            return 'deputy'
        person = state.registry.library[actor]
        if person.is_leader and any(s.leader_phone == actor and actor in s.member_phones
                                    for s in state.ledger.squads.values()):
            return 'captain'
        for event_id, record in state.registry.events.items():
            if record.status in {'active', 'upgraded'}:
                try:
                    if teams(state, token, event_id):
                        return 'captain'
                except (ValueError, OSError, PanelActionError):
                    continue
    except PermissionError:
        pass
    return ''


def events(state, token, viewer_role=None):
    from .panel import PanelActionError
    viewer_role = viewer_role or role(state, token)
    if viewer_role == 'duty':
        return sorted(state.registry.events)
    if viewer_role == 'deputy':
        return state.deputy_events(token)
    if viewer_role != 'captain':
        return []
    allowed = []
    for event_id, record in sorted(state.registry.events.items()):
        if record.status not in {'active', 'upgraded'}:
            continue
        try:
            if teams(state, token, event_id):
                allowed.append(event_id)
        except (ValueError, OSError, PanelActionError):
            continue
    return allowed


def selected_event(state, token):
    member = state.member_session(token)
    if member is None:
        return ''
    allowed = events(state, token)
    if member.selected_event_id in allowed:
        return member.selected_event_id
    if state.active_event_id in allowed:
        return state.active_event_id
    started = [eid for eid in allowed if state.registry.events[eid].status in {'active', 'upgraded'}]
    return (started or allowed or [''])[0]


def phones(state, token, event_id):
    owned = set(teams(state, token, event_id))
    result = {phone for sid in owned if sid in state.ledger.squads
              for phone in state.ledger.squads[sid].member_phones}
    if owned - set(state.ledger.squads):
        result.update(p.phone for p in state.event_group_people(event_id).values()
                      if p.pos_code in owned)
    return frozenset(result)


def overview(state):
    event = state.registry.events.get(state.active_event_id)
    if event is None:
        return '<div class="lz-empty">暂无可进入事件。请在火情事件页查看。</div>'
    snapshot = state.ledger.snapshots.get(event.event_id)
    count = len({p for squad in snapshot.squads.values() for p in squad.arrived_phones}) if snapshot else None
    return ('<div class="lz-card"><h2>本事件概览</h2>'
            f'<p>{esc(event.name)} · {esc(event.event_id)} · {esc(event.status_word())}</p>'
            f'<p>位置：{esc(event.location)} · 已确认到场人数：{count if count is not None else "尚无到场快照"}</p>'
            '<p class="lz-note">详细名单、任务和物资记录仅显示本队。</p></div>')


def captain_actions():
    return ('<div class="lz-card"><h2>本队办理入口</h2>'
            '<p><a class="lz-link" href="/callouts">召集回执</a> · '
            '<a class="lz-link" href="/captain-arrival">扫码载入／到场确认</a> · '
            '<a class="lz-link" href="/tasks/formal">正式任务接收与执行</a> · '
            '<a class="lz-link" href="/tasks/offline">线下指令与反馈</a></p>'
            '<p>所有办理入口继续核对本人及本队职责。</p></div>')


def captain_people(state, token):
    from .org_model import mask_phone
    snapshot = state.ledger.snapshots.get(state.active_event_id)
    arrived = {phone for squad in snapshot.squads.values() for phone in squad.arrived_phones} if snapshot else set()
    rows = []
    for phone in sorted(phones(state, token, state.active_event_id)):
        person = state.registry.library.get(phone)
        if person is not None:
            rows.append(f'<tr><td>{esc(person.name)}</td><td>{esc(person.unit)}</td>'
                f'<td>{"已确认到场" if phone in arrived else "未确认到场"}</td>'
                f'<td>{esc(mask_phone(phone))}</td></tr>')
    return ('<div class="lz-card"><h2>本队名单</h2><p>名单来自本队固定编制或本事件临时编组，名单中有记录不代表已到场。</p>'
            '<div style="overflow-x:auto"><table class="lz-table"><tr><th>姓名</th><th>单位</th><th>到场</th><th>联系电话</th></tr>'
            + ''.join(rows) + '</table></div></div>')


def captain_materials(state, token):
    from .material_inventory_reads import read_event
    event_id = state.active_event_id
    owned = set(teams(state, token, event_id))
    balances = read_event(state.workspace, state.ledger, event_id)
    rows = []
    for iid, inventory in sorted(balances.items()):
        item = state.ledger.items[iid]
        for balance in inventory.team_balances:
            if balance.squad_id in owned:
                squad = state.ledger.squads.get(balance.squad_id)
                rows.append(f'<tr><td>{esc(squad.name if squad else balance.squad_id)}</td>'
                    f'<td>{esc(item.name)}</td><td>{balance.arrival_base:g}</td>'
                    f'<td>{balance.available:g}</td><td>{esc(item.unit)}</td></tr>')
    return (overview(state) + '<div class="lz-card"><h2>本队已报告物资</h2>'
            '<div style="overflow-x:auto"><table class="lz-table"><tr><th>队伍</th><th>物资</th>'
            '<th>到场基数</th><th>当前可用</th><th>单位</th></tr>' + ''.join(rows) + '</table></div>'
            + ('' if rows else '<p>尚无本队到场物资快照。</p>') + '</div>' + captain_actions())


def captain_tasks(state, token):
    from .offline_tasks import task_state
    actor = state.authorization_subject(token)
    owned = set(teams(state, token, state.active_event_id))
    tasks = task_state(state.workspace, state.active_event_id)
    rows = [f'<tr><td>{esc(t.id)}</td><td>{esc(t.content)}</td><td>{esc(t.state)}</td></tr>'
            for t in tasks.values() if t.recipient_id == actor and t.group_id in owned]
    return ('<div class="lz-card"><h2>本队线下指令记录</h2>'
            '<div style="overflow-x:auto"><table class="lz-table"><tr><th>编号</th><th>内容</th><th>状态</th></tr>'
            + ''.join(rows) + '</table></div>'
            + ('' if rows else '<p>暂无本队线下指令记录。</p>') + '</div>')


def captain_review(state, token):
    from .review_meeting import scoped_state
    projected = scoped_state(state)
    reports = projected.review_ledger.reports.values()
    published = sum(report.status == 'published' for report in reports)
    owner_word = '已指定' if projected.review_ledger.owner_phone else '尚未指定'
    return (overview(state) + '<div class="lz-card"><h2>复盘概览</h2>'
            f'<p>复盘负责人：{owner_word} · 已发布报告：{published} 份</p>'
            '<p>本队任务与反馈作为复盘依据。会议记录、原因确认和报告审核由指定复盘负责人办理。</p>'
            '</div>' + captain_tasks(state, token) + captain_actions())
