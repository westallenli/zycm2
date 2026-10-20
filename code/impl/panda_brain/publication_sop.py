"""Publication SOP context; operational notices never confer news approval."""
from dataclasses import dataclass

from . import callouts, local_notifications
from .workbench import esc


@dataclass(frozen=True)
class PublicationView:
    state: str
    callouts: tuple[str, ...] = ()
    local_notices: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    work_records: tuple[str, ...] = ()


def _required_text(*values):
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('通知来源字段不完整。')


def _callout_rows(workspace, event_id):
    rows = []
    for item in callouts.callout_state(workspace, event_id).values():
        _required_text(item.squad_id, item.leader_phone, item.selected_by,
                       item.selected_at, item.selection_request_id)
        receipt = '尚无系统内模拟回执；不能据此判断实际是否知悉。'
        if item.reply:
            reply = item.reply
            _required_text(reply.get('reported_by'), reply.get('reported_at'), reply.get('request_id'))
            receipt = (f'系统内模拟回执：报告人 {reply["reported_by"]}；时间 {reply["reported_at"]}；'
                       '不证明电话/微信送达、实际出动或到场。')
        failure = ''
        if item.simulation_failure:
            failure = (f'演练假设通知失败：{item.simulation_failure["reason"]}；'
                       f'记录时间 {item.simulation_failure["recorded_at"]}；不是实际发送失败。')
        rows.append(f'中队 {item.squad_id}；通知对象标识 {item.leader_phone}；'
                    f'{callouts.CHANNELS[item.channel]}模拟通知，未发送、未送达；'
                    f'准备人标识 {item.selected_by}；准备时间 {item.selected_at}；'
                    f'请求编号 {item.selection_request_id}。{receipt}{failure}')
    return tuple(rows)


def publication_view(workspace, record) -> PublicationView:
    if record is None:
        return PublicationView('未进入事件', missing=('请先选择事件任务空间。',))
    if not workspace.event_id or workspace.event_id != record.event_id or workspace.verify_audit_chain():
        return PublicationView('依据待核对', missing=('事件归属或审计链不一致，不能汇总沟通依据。',))
    missing = [
        '内部通报的稿件、内容版本、接收范围、具名审核与实际送达依据尚未接入。',
        '正式对外新闻的发布制度、具名审核/发布责任人、核准稿件版本及发布回执尚未接入。',
        '未接入记录不代表线下未开展；需核对实际材料、时间、范围及依据。']
    selected, local = (), ()
    try:
        selected = _callout_rows(workspace, record.event_id)
    except (ValueError, KeyError, TypeError):
        missing.append('召集通知依据不完整或不一致，未纳入本视图；请核对原始记录。')
    try:
        local = tuple(
            f'{item.name}（{local_notifications.ROLES[item.local_role]}）；单位 {item.unit or "未记录"}；'
            f'{callouts.CHANNELS[item.channel]}模拟通知，未发送、未送达；'
            f'准备人标识 {item.prepared_by}；准备时间 {item.prepared_at}；请求编号 {item.request_id}。'
            '通知准备不代表本人已知悉、扫码登记或到场。'
            for item in local_notifications.notice_state(workspace, record.event_id).values())
    except (ValueError, KeyError, TypeError):
        missing.append('属地通知依据不完整或不一致，未纳入本视图；请核对原始记录。')
    focus = '已有沟通记录待核对' if selected or local else '待补发布制度与依据'
    from .nine_flow import history,OBS
    work=tuple(('模拟资料：' if r.data_kind=='simulated' else '具名登记材料：')+v['subject']+'；'+v['feedback']
        for r,v in history(workspace,OBS) if v['step']==7)
    if work:
        missing[0]='已接入本环节具名材料与核验留证；真实发送仍须核对原发布通道及送达证据。'
        focus='已有具名发布材料与接收依据待核对'
    return PublicationView(focus, selected, local, tuple(missing),work)


def render_publication(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    record = registry.events.get(event_id) if registry and event_id else None
    view = publication_view(state.workspace, record)

    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(row)}</li>' for row in rows)
                         or '<li>暂无可核对记录；不代表线下没有沟通。</li>') + '</ul>'

    return (f'<div class="sop-publication" data-sop-state="{esc(view.state)}">'
        f'<p>当前重点：{esc(view.state)} · 每次载入核对本事件记录</p>'
        '<h4>现在做什么</h4><p>区分内部通报与正式对外新闻，核对信息来源、内容版本、接收范围、审核和送达依据。</p>'
        '<h4>为什么做</h4><p>让接收方获得口径清楚、来源可追溯的信息，避免把通知准备、任务确认或模拟回执当作正式发布。</p>'
        '<h4>由谁做</h4><p>内部通报按本单位制度明确编制、审核和通知责任人；正式对外新闻按已确认制度明确审核及发布责任人。'
        '本项具名责任与制度尚待核实，组织中的新闻发言人或宣发岗位名称不等于已取得发布授权。'
        '指定副总指挥的任务发布权、复盘报告发布权及通用指挥登录均不能替代通报或新闻审批。</p>'
        '<h4>已有信息与依据</h4>'
        + ('<section><h5>本环节具名材料与核验留证</h5>'+items(view.work_records)+'<a href="/workflow/records?step=7">核对办理版本、审核与接收记录</a></section>' if view.work_records else '')
        +
        '<section class="sop-publication-internal"><h5>内部通报</h5>'
        '<p>尚无独立稿件、审核及真实发送记录接线。以下召集/属地通知仅作相关沟通上下文，不作为通报稿件或完成证据。</p></section>'
        '<section class="sop-publication-external"><h5>正式对外新闻</h5>'
        '<p>发布制度、具名授权、核准稿件与实际发布回执待核对；当前没有可用的对外新闻发布操作。'
        '地图共享、任务下达、长期库导入及复盘报告的“已发布”状态均不代表新闻已发布。</p></section>'
        '<details class="sop-publication-callouts"><summary>队伍召集通知依据（模拟）</summary>'
        + items(view.callouts) + '</details>'
        '<details class="sop-publication-local"><summary>属地人员通知依据（模拟）</summary>'
        + items(view.local_notices) + '</details><h4>还缺什么</h4>' + items(view.missing)
        + '<h4>从哪里操作</h4><p>从信息全景核对本事件资料，从人员物资核对召集上下文。'
        '指定副总指挥、值班人员通过本人登录使用已有通知准备入口；这些操作不发布通报或新闻。'
        '可在本环节办理页登记制度、稿件版本、审核及发布回执；真实发布在原通道办理。</p>'
        '<a class="lz-btn lz-btn2" href="/workflow/records?step=7">登记发布材料与本人核验</a> '
        '<button type="button" class="lz-btn lz-btn2" data-sand-open="drawer-fourcat">核对信息全景</button> '
        '<a class="lz-btn lz-btn2" data-sand-related="7" href="/materials">核对人员与物资</a>'
        '<h4>怎样才算完成</h4><p>由相应责任人核对所需信息的准确性、核准版本、接收范围和实际发布/送达依据，'
        '有更正时继续跟进。精确完成规则待制度明确；点击、已读、通知准备、模拟回执或任务审批均不判定本项完成。</p>'
        '<p class="lz-note">本页只读，不发送或自动审批。资料缺项不锁定其他工作。</p></div>')
