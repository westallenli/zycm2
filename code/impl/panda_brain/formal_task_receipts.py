"""Named local receipts for formal publications; reading never acknowledges."""
from dataclasses import asdict
from datetime import datetime, timezone
import json

from . import audited_records as ar
from . import task_release as release
from .task_recipients import recipient_route
from .task_sop import flow_issue
from .models import OrderStatus

KIND = 'formal_task_receipt'
SOURCE = 'named-formal-task-receipt'
ACTIONS = {'delivered', 'viewed', 'received'}


def published(workspace):
    return {task['order_id']: (record, value, task)
            for record, value in release.releases(workspace) for task in value['tasks']}


def history(workspace):
    tasks = published(workspace)
    results = []
    for index, record, value in ar.records(workspace, KIND):
        item = tasks.get(record.reference.object_id)
        if item is None:
            raise ValueError('回执缺少正式任务来源。')
        publication, bundle, task = item
        if (record.source != SOURCE or set(value) != {'order_id', 'publication_ref', 'draft_version', 'action', 'basis'}
                or value['order_id'] != record.reference.object_id or value['publication_ref'] != publication.id
                or value['draft_version'] != bundle['draft_version'] or value['action'] not in ACTIONS
                or record.actor_id != task['recipient_id']
                or publication.id not in ar.prefix(workspace, index).object_revisions):
            raise ValueError('回执身份、事件或任务版本不一致。')
        ar.text(value['basis']); results.append((record, value))
    return tuple(results)


def record(workspace, registry, ledger, actor, *, order_id, publication_ref, expected_version,
           action, basis, request_id):
    ar.text(actor); ar.text(basis)
    tasks = published(workspace)
    item = tasks.get(order_id)
    if item is None: raise ValueError('本事件没有该正式任务。')
    publication, data, task = item
    if publication.id != publication_ref: raise ValueError('发布版本已变化。')
    route = recipient_route(workspace, workspace.event_id, task['spec']['responsible_group_id'], registry=registry, ledger=ledger)
    if actor != task['recipient_id'] or actor != route.leader_id or actor not in registry.library:
        raise PermissionError('仅本版正确现任队长/组长可报告接收。')
    if action not in ACTIONS: raise ValueError('未知接收动作。')
    history(workspace)
    values = dict(order_id=order_id, publication_ref=publication_ref, draft_version=data['draft_version'], action=action, basis=basis)
    for old in workspace.object_revisions.values():
        if old.request_id == request_id:
            return ar.append(workspace, kind=KIND, object_id=order_id, source=SOURCE, actor=actor,
                values=values, expected_version=expected_version, request_id=request_id)
    if workspace.orders[order_id].status != OrderStatus.CONFIRMED or flow_issue(workspace, order_id, workspace.event_id):
        raise ValueError('旧版、已关闭或坏来源任务不能接收。')
    bundle = release._bundle(workspace, data['decision_id'], data['draft_version'])
    draft_task = next(t for t in bundle.tasks if t.id == task['task_id'])
    issues, refs, _ = release.task_gate(workspace, bundle, draft_task, registry=registry, ledger=ledger)
    if issues or set(refs) != set(task['support_refs']):
        raise ValueError('本版复核依据已变化，请由副指挥长重新核对发布。')
    flow = workspace.command_flows[order_id]
    at = datetime.now(timezone.utc).isoformat()
    if action == 'received':
        updated = flow.record_ack(at=at, by=actor, result='accepted_ready', expected_version=1)
    else:
        if getattr(flow, 'delivered_at' if action == 'delivered' else 'viewed_at'):
            raise ValueError('本版已报告该状态。')
        updated = flow.record_delivery(at=at, **({'delivered': at} if action == 'delivered' else {'viewed': at}))
    revision = ar.append(workspace, kind=KIND, object_id=order_id, source=SOURCE, actor=actor,
        values=values, expected_version=expected_version, request_id=request_id)
    workspace.command_flows[order_id] = updated
    workspace.append_event('CommandFlowChanged', order_id=order_id, event_id=workspace.event_id, flow=asdict(updated), receipt_ref=revision.id)
    return revision


def body(state, token, *, only_event=None):
    from .workbench import esc
    from .emergency_ui import task_header,task_is_history,split_tasks,person
    import secrets
    actor=state.authorization_subject(token);active=[];old=[]
    for event_id,event in sorted(state.registry.events.items()):
        if only_event is not None and event_id!=only_event:continue
        if event.status=='registered':continue
        space,_,_=state._appointment_space(event_id);records=history(space)
        for order_id,(publication,data,task) in published(space).items():
            if actor not in {task['recipient_id'],task['verifier_id'],publication.actor_id}:continue
            flow=space.command_flows[order_id];historical=task_is_history(space,order_id)
            count=max((r.reference.version for r,_ in records if r.reference.object_id==order_id),default=0)
            card=['<section class="lz-card emergency-task" data-emergency-task="'+esc(order_id)+'">',task_header(state,space,order_id,task),
                '<p>要做：'+esc(task['spec']['objective'])+'</p>',
                '<details><summary>任务详情</summary><p>完成要求：'+esc(task['spec']['completion_standard'])+'</p><p>核实负责人：'
                +esc(person(state,task['verifier_id']))+'</p><p>送达：'+esc(flow.delivered_at or '未报告')+'；查看：'
                +esc(flow.viewed_at or '未报告')+'；接收：'+esc(flow.acked_at or '未报告')+'</p></details>']
            if not historical and actor==task['recipient_id'] and event.status!='closed' and space.orders[order_id].status==OrderStatus.CONFIRMED and not flow.acked_at:
                # 一键档：三个动作各自成表，说明按动作预置，收件人只点一下。
                # 仍走原有隐藏字段（事件/任务/发布版本/期望版本/请求号/CSRF），审计与幂等不变。
                preset={'delivered':'任务已送达，等待接收。',
                        'viewed':'已查看本版任务内容与要求。',
                        'received':'确认接收本版任务，按发布要求执行。'}
                pairs=[(key,label) for key,label in
                    (('delivered','任务已送达'),('viewed','我已查看'),('received','确认接收任务'))
                    if not (key=='delivered' and flow.delivered_at or key=='viewed' and flow.viewed_at)]
                quick=[]
                for key,label in pairs:
                    values=dict(event_id=event_id,order_id=order_id,publication_ref=publication.id,
                        expected_version=count,request_id=secrets.token_urlsafe(18),csrf=state.member_session(token).custody_csrf)
                    hidden=''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())
                    quick.append('<form method="post" action="/tasks/formal" class="lz-inlineform">'+hidden
                        +f'<input type="hidden" name="action" value="{key}">'
                        +f'<input type="hidden" name="basis" value="{esc(preset[key])}">'
                        +f'<button class="{"lz-btn" if key=="received" else "lz-btn2"}" type="submit">{label}</button></form>')
                card.append('<div class="lz-actions">'+''.join(quick)+'</div>')
                # 需要自己写说明时再展开（原表单原样保留，校验不变）
                manual_values=dict(event_id=event_id,order_id=order_id,publication_ref=publication.id,
                    expected_version=count,request_id=secrets.token_urlsafe(18),csrf=state.member_session(token).custody_csrf)
                manual_hidden=''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in manual_values.items())
                choices=''.join(f'<option value="{key}">{label}</option>' for key,label in
                    (('delivered','任务已送达'),('viewed','我已查看'),('received','确认接收任务'))
                    if not (key=='delivered' and flow.delivered_at or key=='viewed' and flow.viewed_at))
                card.append('<details><summary>改一项或手工填写</summary>'
                    '<form method="post" action="/tasks/formal">'+manual_hidden
                    +'<label>我要报告<select class="lz-input" name="action">'+choices+'</select></label>'
                    '<label>说明实际情况<input class="lz-input" name="basis" required></label>'
                    '<button class="lz-btn" type="submit">确认提交</button></form></details>')
            card.append('<details><summary>办理记录</summary>'+''.join('<p>'+esc({'delivered':'已送达','viewed':'已查看','received':'已接收'}[v['action']])+' · '+esc(v['basis'])+' · '+esc(person(state,r.actor_id))+'</p>' for r,v in records if r.reference.object_id==order_id)+'</details></section>')
            (old if historical else active).append(''.join(card))
    return split_tasks(active,old)
