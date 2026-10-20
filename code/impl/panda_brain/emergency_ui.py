"""Plain, read-only emergency views over the existing task and audit records."""
from html import escape
import json


def person(state, identity):
    library = getattr(getattr(state, 'registry', None), 'library', None) or {}
    row = library.get(identity)
    return row.name if row else '待指定'


def team(state, identity):
    # 组织架构未加载时按"待指定"处理，不能让整页崩掉。
    squads = getattr(getattr(state, 'ledger', None), 'squads', None) or {}
    squad = squads.get(identity)
    return squad.name if squad else ''


def progress(flow, status):
    status=getattr(status,'value',status)
    if status=='superseded':return '旧任务，已被新任务替代'
    if status in {'cancelled','withdrawn'}:return '任务已撤回'
    control=getattr(flow,'control_state','')
    if control=='verified':return '核实通过，任务已结束'
    if control in {'paused','suspended'}:return '已暂停，等待处理'
    if control=='exec_abnormal':return '执行遇到困难，需要处理'
    if control in {'cannot_execute','personnel_safety','comm_abnormal'}:return '执行有异常，请查看现场回报'
    stage=getattr(flow,'stage','')
    return {'running':'正在执行，等待现场回报','partial':'已报部分完成，继续跟进',
        'done_pending_verify':'队长已报完成，等待负责人核实','closed':'任务已结束',
        'received':'队长已接收，等待开始','ready':'队长已接收，等待开始','acked_not_started':'队长已接收，等待开始',
        'delivered':'已送达，等待队长接收','viewed':'队长已查看，等待接收'}.get(stage,
        '已登记完成，查看核实记录' if status=='completed' else
        '队长已接收，等待开始' if getattr(flow,'acked_at','') else
        '已下达，等待队长接收' if status=='confirmed' else '任务待确认')


def task_view(state, space):
    from .formal_task_receipts import published
    publications=published(space)
    out=[]
    for oid,order in space.orders.items():
        entry=publications.get(oid)
        spec=entry[2]['spec'] if entry else {}
        option=space.options.get(order.option_id)
        title=spec.get('title') or (option.title if option else '待确认任务')
        flow=space.command_flows.get(oid)
        status=getattr(order.status,'value',order.status)
        recipient=entry[2]['recipient_id'] if entry else ''
        group=spec.get('responsible_group_id','')
        who=team(state,group) or (person(state,recipient) if recipient else '执行队伍待指定')
        out.append(dict(id=oid,title=title,who=who,state=progress(flow,status),
            history=status in {'superseded','completed','cancelled','withdrawn','failed'} or getattr(flow,'control_state','')=='verified',
            version=entry[1]['draft_version'] if entry else 0))
    return out


def overview(state):
    rows=task_view(state,state.workspace)
    # 任务卡只留三样：干什么、派给谁、现在到哪一步。编号属于后台，不在这里出现。
    cards=''.join('<article class="emergency-task-summary"><b>'+escape(r['title'])+'</b><span>'
        +escape(r['who'])+'</span><p>'+escape(r['state'])+'</p></article>' for r in rows)
    return '<details class="emergency-overview"><summary>全部任务 · '+str(len(rows))+' 项</summary>'+cards+'</details>'


def task_header(state, space, oid, task):
    order=space.orders[oid];flow=space.command_flows.get(oid)
    spec=task['spec']
    who=team(state,spec.get('responsible_group_id','')) or person(state,task['recipient_id'])
    return ('<header class="emergency-task-head"><h2>'+escape(spec['title'])+'</h2><b>'
        +escape(who)+'</b><p class="emergency-progress">'+escape(progress(flow,order.status))+'</p></header>')


def task_is_history(space,oid):
    return (getattr(space.orders[oid].status,'value',space.orders[oid].status) in
            {'superseded','completed','cancelled','withdrawn','failed'}
            or getattr(space.command_flows.get(oid),'control_state','')=='verified')


def split_tasks(active, history):
    return (''.join(active) or '<p class="emergency-empty">当前没有需要你办理的任务。</p>') + (
        '<details class="emergency-task-history"><summary>已结束与旧任务 · '+str(len(history))+' 项</summary>'
        +''.join(history)+'</details>' if history else '')


def assets():
    return '<link rel="stylesheet" href="/static/emergency-ui.css"><script src="/static/emergency-ui.js" defer></script>'


def map_assets(state):
    try:words={r['id']:r['title'] for r in task_view(state,state.workspace)}
    except (ValueError,KeyError):words={}
    words.update({key:row.title for key,row in state.workspace.annotations.items()})
    payload=json.dumps(words,ensure_ascii=False).replace('<','\\u003c')
    return '<script type="application/json" id="emergency-display-names">'+payload+'</script>'+assets()


def decorate(state,route,html):
    """Only the map/its direct task pages receive this presentation layer."""
    if route=='/sand':return html
    from .unified_ui import CHILD_LABELS
    label=CHILD_LABELS.get(route,'任务办理')
    words={}
    for row in task_view(state,state.workspace):words[row['id']]=row['title']
    for key,row in state.ledger.items.items():words[key]=row.name
    for key,row in state.ledger.squads.items():words[key]=row.name
    for key,row in state.workspace.options.items():words[key]=row.title
    for key,row in state.workspace.decisions.items():
        option=state.workspace.options.get(row.option_id)
        words[key]=(option.title if option else '已登记方案')+'的决定'
    for saved in state.workspace.task_drafts.values():
        for task in saved.tasks:words[task.id]=task.spec.title or '待命名任务'
    from .formal_task_receipts import published
    for _,_,task in published(state.workspace).values():
        for key in ('recipient_id','verifier_id'):
            identity=task[key]
            if identity in state.registry.library:words[identity]=person(state,identity)
    payload=json.dumps(words,ensure_ascii=False).replace('<','\\u003c')
    people=json.dumps({key:row.name for key,row in state.registry.library.items()},ensure_ascii=False).replace('<','\\u003c')
    header='<section class="emergency-page-title"><h1>'+escape(label)+'</h1><p>选择当前事项，按提示办理。</p></section>'
    html=html.replace('<div class="lz-wrap">','<div class="lz-wrap" data-emergency-page="'+escape(route,quote=True)+'">'+header,1)
    return html+ '<script type="application/json" id="emergency-display-names">'+payload+'</script><script type="application/json" id="emergency-people">'+people+'</script>'+assets()
