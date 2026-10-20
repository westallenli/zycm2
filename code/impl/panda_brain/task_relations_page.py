"""Named relation editing and condition/start forms; no external notification."""
import json, secrets
from urllib.parse import parse_qs
from . import task_relations as tr
from . import audited_records as ar
from .formal_task_receipts import published
from .authorization import Role
from .event_authorization import event_grants
from .models import OrderStatus
from .workbench import esc


def body(state,token,*,only_event=None):
    actor=state.authorization_subject(token); member=state.member_session(token)
    sections=['<p>关联本事件明确任务版本。依赖须核验关闭；互斥任务不能同时执行。独立协作在草案中拆为关联子任务。关联变化须重新确认条件；提醒仅在平台留痕，未发送通知。</p>']
    for eid,event in sorted(state.registry.events.items()):
        if only_event is not None and eid!=only_event:continue
        if event.status=='registered': continue
        space,_,_=state._appointment_space(eid)
        tasks=published(space); current=tr.current(space); controls=tr.control_history(space)
        deputy=any(g.subject_id==actor and g.role==Role.DEPUTY for g in event_grants(space))
        for oid,item in tasks.items():
            publication,data,task=item
            if not deputy and actor not in {task['recipient_id'],task['verifier_id']}: continue
            flow=space.command_flows[oid]
            row=current.get(oid); value=row[1] if row else {field:[] for field in tr.FIELDS}
            sections.append('<section class="lz-card" style="overflow-wrap:anywhere">'
                f'<h2>{esc(event.name)} · {esc(task["spec"]["title"])} · 版本{data["draft_version"]}</h2>'
                f'<p>接收人：{esc(task["recipient_id"])}；阶段：{esc(flow.stage_word)}。</p>'
                f'<p>执行条件确认：{esc(flow.conditions_confirmed_at or "未确认")}；依据：{esc(flow.conditions_note or "未报告")}。</p>'
                '<p><a href="/tasks/feedback">执行反馈与核验关闭</a></p>')
            for field,label in zip(tr.FIELDS,('依赖','互斥','协作子任务')):
                names=['%s（v%s）'%(tasks[ref['order_id']][2]['spec']['title'],ref['draft_version']) for ref in value[field]]
                sections.append(f'<p>{label}：{esc("、".join(names) or "未指定")}。</p>')
            live=space.orders[oid].status not in {OrderStatus.SUPERSEDED,OrderStatus.CANCELLED,OrderStatus.COMPLETED}
            if event.status=='closed' or not live:
                sections.append('<p>已关闭或失效，仅保留历史。</p></section>'); continue
            common=dict(event_id=eid,task=ar.dump(tr.reference(item)),request_id=secrets.token_urlsafe(18),csrf=member.custody_csrf)
            def hidden(values):
                return ''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())
            if deputy and flow.stage not in tr.RUNNING|{'closed'}:
                sections.append('<details><summary>修改依赖、互斥及协作</summary><form method="post" action="/tasks/relations">'
                    +hidden(common|dict(action='relations',expected_version=row[0].reference.version if row else 0)))
                for field,label in zip(tr.FIELDS,('依赖任务','互斥任务','独立协作子任务')):
                    sections.append(f'<fieldset><legend>{label}</legend>')
                    selected={ref['order_id'] for ref in value[field]}
                    for other,target in tasks.items():
                        if other==oid or space.orders[other].status==OrderStatus.SUPERSEDED: continue
                        if field=='collaboration' and target[2]['parent_id']!=task['task_id']: continue
                        checked=' checked' if other in selected else ''
                        sections.append(f'<label><input type="checkbox" name="{field}" value="{esc(ar.dump(tr.reference(target)))}"{checked}>'
                            f'{esc(target[2]["spec"]["title"])} · v{target[1]["draft_version"]}</label>')
                    sections.append('</fieldset>')
                sections.append('<label>关联变更依据<input class="lz-input" name="basis" required aria-label="关联变更依据"></label>'
                    '<button class="lz-btn">保存本版关联</button></form></details>')
            if actor==task['recipient_id'] and flow.stage=='acked_not_started':
                issues,_=tr.gate(space,oid,state.ledger)
                if issues: sections.append('<p>'+esc('；'.join(issues))+'</p>')
                count=max((r.reference.version for r,v in controls if r.reference.object_id==oid),default=0)
                for action,label in (('conditions','确认当前执行条件'),('begin','明确报告开始执行')):
                    sections.append('<form method="post" action="/tasks/relations">'+hidden(common|dict(action=action,expected_version=count,request_id=secrets.token_urlsafe(18)))
                        +f'<label>{label}依据<input class="lz-input" name="basis" required aria-label="{label}依据"></label>'
                        +f'<button class="lz-btn">{label}</button></form>')
            sections.append('<details><summary>关联与提醒历史</summary>'+''.join(
                f'<p>第{r.reference.version}版 · {esc(r.actor_id)} · {esc(v["basis"])} · 提醒未发送</p>'
                for r,v in tr.history(space) if r.reference.object_id==oid)+'</details></section>')
    return ''.join(sections)


def post(handler,state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    member=state.member_session(handler._member_cookie())
    if member is None: handler._respond(403,'<h1>403</h1>'); return
    try: length=request_length(handler,16384)
    except ValueError: handler._respond(413,'<h1>413</h1>'); return
    try:
        if handler.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded': raise ValueError('表单格式不正确。')
        handler.connection.settimeout(15)
        form=parse_qs(handler.rfile.read(length).decode('utf-8'),keep_blank_values=True,encoding='utf-8',errors='strict',max_num_fields=128)
        base={'csrf','event_id','task','expected_version','request_id','action','basis'}
        if set(form)-base-set(tr.FIELDS) or not base<=set(form): raise ValueError('字段缺失或不受支持。')
        def one(key):
            if len(form[key])!=1: raise ValueError('字段重复。')
            return form[key][0]
        if not secrets.compare_digest(one('csrf').encode(),member.custody_csrf.encode()): raise PermissionError('会话变化。')
        raw=one('expected_version')
        if not raw.isascii() or not raw.isdigit() or len(raw)>9: raise ValueError('版本不正确。')
        values=dict(task=json.loads(one('task')),expected_version=int(raw),basis=one('basis'),request_id=one('request_id'))
        action=one('action')
        if action=='relations': values.update({field:[json.loads(ref) for ref in form.get(field,[])] for field in tr.FIELDS})
        elif any(field in form for field in tr.FIELDS): raise ValueError('执行报告不能修改关联。')
        state.handle_task_relations(one('event_id'),member_token=handler._member_cookie(),action=action,**values)
    except PermissionError: handler._respond(403,'<h1>403</h1>'); return
    except (ValueError,PanelActionError) as error:
        handler._respond(409,'<h1>409</h1><p>'+esc(str(error))+'</p>'); return
    handler._redirect('/tasks/relations')
