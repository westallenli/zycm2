"""Named progress and verification forms with explicit frozen supply totals."""
import secrets
from urllib.parse import parse_qs
from . import formal_task_feedback as service
from .formal_task_receipts import published
from .workbench import esc


def body(state,token,*,only_event=None):
    from .emergency_ui import task_header,task_is_history,split_tasks,person
    actor=state.authorization_subject(token);member=state.member_session(token)
    active=[];old=[]
    for eid,event in sorted(state.registry.events.items()):
        if only_event is not None and eid!=only_event:continue
        if event.status=='registered':continue
        space,_,_=state._appointment_space(eid);rows=service.history(space,state.ledger)
        for oid,(pub,data,task) in published(space).items():
            if actor not in {task['recipient_id'],task['verifier_id'],pub.actor_id}:continue
            flow=space.command_flows[oid]
            from .task_sop import flow_issue
            issue=flow_issue(space,oid,eid)
            if issue:raise ValueError(issue)
            historical=task_is_history(space,oid)
            card=['<section class="lz-card emergency-task" data-emergency-task="'+esc(oid)+'">',task_header(state,space,oid,task)]
            card.append('<p>要做：'+esc(task['spec']['objective'])+'</p>')
            detail=('<details><summary>任务详情</summary><p>完成要求：'+esc(task['spec']['completion_standard'])
                    +'</p><p>核实负责人：'+esc(person(state,task['verifier_id']))+'</p><p>执行条件：'
                    +esc(flow.conditions_note or '尚未确认')+'</p><p>内容版本：'+str(data['draft_version'])+'</p></details>')
            card.append(detail)
            supply=service.supply_snapshot(space,state.ledger,task|dict(publication_ref=pub.id))
            if supply:
                card.append('<details><summary>本任务物资</summary>')
                for row in supply:
                    item=state.ledger.items.get(row['item_id'])
                    card.append('<p>'+esc(item.name if item else '待核实物资')+f'：已收 {row["received"]:g}，待收 {row["outstanding"]:g}，损坏 {row["damaged"]:g}。</p>')
                card.append('</details>')
            if not historical and event.status!='closed' and flow.stage in {'running','partial','done_pending_verify'}:
                actions=(['verify'] if actor==task['verifier_id'] and flow.stage=='done_pending_verify' else [])
                if actor==task['recipient_id'] and flow.stage!='done_pending_verify':actions+=['progress','difficulty','resolved','partial','completed']
                labels=dict(progress='报告进展',difficulty='报告困难',resolved='困难已处理',partial='部分完成',completed='报告完成',verify='核实结果并结束任务')
                for action in actions:
                    values=dict(csrf=member.custody_csrf,event_id=eid,order_id=oid,publication_ref=pub.id,action=action,
                        expected_version=max((r.reference.version for r,v in rows if r.reference.object_id==oid),default=0),request_id=secrets.token_urlsafe(18))
                    hidden=''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())
                    card.append('<form method="post" action="/tasks/feedback">'+hidden+'<label>实际情况<textarea class="lz-input" name="basis" required placeholder="简要说明现场情况"></textarea></label>')
                    if action=='verify':
                        card.append('<label>整体目标情况（总任务填写）<input class="lz-input" name="overall_basis"></label>'
                            '<label>损坏处理情况（有损坏填写）<input class="lz-input" name="damage_basis"></label>')
                    card.append('<button class="lz-btn">'+labels[action]+'</button></form>')
            card.append('<details><summary>办理记录</summary>'+''.join('<p>'+esc(service.LABELS[v['action']])+' · '+esc(v['basis'])+' · '+esc(person(state,r.actor_id))+'</p>' for r,v in rows if r.reference.object_id==oid)+'</details></section>')
            (old if historical else active).append(''.join(card))
    from .captain_field import report_section,REPLY
    return split_tasks(active,old)+report_section(state,token,REPLY)


def post(handler,state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    member=state.member_session(handler._member_cookie())
    if member is None:handler._respond(403,'<h1>403</h1>');return
    try:length=request_length(handler,16384)
    except ValueError:handler._respond(413,'<h1>413</h1>');return
    try:
        if handler.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':raise ValueError('表单格式不正确。')
        handler.connection.settimeout(15)
        form=parse_qs(handler.rfile.read(length).decode('utf-8'),keep_blank_values=True,encoding='utf-8',errors='strict',max_num_fields=32)
        base={'csrf','event_id','order_id','publication_ref','action','basis','expected_version','request_id'}
        if set(form)-base-{'overall_basis','damage_basis'} or not base<=set(form) or any(len(v)!=1 for v in form.values()):raise ValueError('字段缺失、重复或不受支持。')
        values={k:v[0] for k,v in form.items()}
        if not secrets.compare_digest(values.pop('csrf').encode(),member.custody_csrf.encode()):raise PermissionError('会话变化。')
        raw=values['expected_version']
        if not raw.isascii() or not raw.isdigit() or len(raw)>9:raise ValueError('版本不正确。')
        values['expected_version']=int(raw)
        state.handle_formal_task_feedback(values.pop('event_id'),member_token=handler._member_cookie(),**values)
    except PermissionError:handler._respond(403,'<h1>403</h1>');return
    except (ValueError,PanelActionError) as error:handler._respond(409,'<h1>409</h1><p>'+esc(str(error))+'</p>');return
    handler._redirect('/tasks/feedback')
