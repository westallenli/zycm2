"""Explicit same-event publication references and audited local execution gates."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from . import audited_records as ar
from .formal_task_receipts import published
from .authorization import Action
from .task_recipients import recipient_route
from .task_sop import flow_issue
from .models import OrderStatus

KIND='formal_task_relations'
CONTROL='formal_task_control'
FIELDS=('dependencies','mutex','collaboration')
RUNNING={'running','partial','done_pending_verify'}


def reference(item):
    record,data,task=item
    return dict(order_id=task['order_id'],publication_ref=record.id,draft_version=data['draft_version'])


def _validate_ref(ref,tasks):
    if (not isinstance(ref,dict) or set(ref)!={'order_id','publication_ref','draft_version'}
            or type(ref['draft_version']) is not int or ref['draft_version']<1):
        raise ValueError('关联须明确引用本事件任务发布版本。')
    item=tasks.get(ref['order_id'])
    if item is None or ref!=reference(item): raise ValueError('关联任务版本或事件不一致。')
    return item


def history(space):
    tasks=published(space); results=[]
    for index,record,value in ar.records(space,KIND):
        if record.source!='named-deputy-task-relations' or set(value)!={'task','dependencies','mutex','collaboration','basis'}:
            raise ValueError('任务关联来源不完整。')
        source=_validate_ref(value['task'],tasks)
        if record.reference.object_id!=value['task']['order_id']:
            raise ValueError('关联对象不一致。')
        prior=ar.prefix(space,index)
        from .task_release import _authority
        _authority(prior,record.actor_id,'meeting','')
        refs=[value['task']]
        ar.text(value['basis'])
        for field in FIELDS:
            if not isinstance(value[field],list): raise ValueError('关联须为明确列表。')
            keys=[]
            for ref in value[field]:
                item=_validate_ref(ref,tasks)
                if ref['order_id']==value['task']['order_id']: raise ValueError('不能关联自身。')
                keys.append(ref['order_id']); refs.append(ref)
                if field=='collaboration' and item[2]['parent_id']!=source[2]['task_id']:
                    raise ValueError('独立协作必须引用本总任务拆分的关联子任务。')
            if len(keys)!=len(set(keys)): raise ValueError('关联重复。')
        if any(ref['publication_ref'] not in prior.object_revisions for ref in refs):
            raise ValueError('任务关联来源断链。')
        results.append((record,value))
    return tuple(results)


def current(space):
    return {r.reference.object_id:(r,v) for r,v in history(space)}


def _live(space,item):
    record,data,task=item
    if space.orders[task['order_id']].status in {OrderStatus.SUPERSEDED,OrderStatus.CANCELLED,OrderStatus.EXPIRED}:
        raise ValueError('关联任务已失效，须重新核对版本。')
    from .task_release import _bundle
    _bundle(space,data['decision_id'],data['draft_version'])
    issue=flow_issue(space,task['order_id'],space.event_id)
    if issue: raise ValueError(issue)


def _cycles(values):
    graph={key:[ref['order_id'] for ref in value['dependencies']] for key,value in values.items()}
    done=set(); active=set()
    def visit(key):
        if key in active: raise ValueError('任务依赖存在环路。')
        if key in done: return
        active.add(key)
        for other in graph.get(key,[]): visit(other)
        active.remove(key); done.add(key)
    for key in graph: visit(key)


def record(space,auth,token,*,task,dependencies,mutex,collaboration,basis,expected_version,request_id):
    actor=auth.require(token,Action.PUBLISH_TASK,event_id=space.event_id)
    tasks=published(space); source=_validate_ref(task,tasks)
    values=dict(task=task,dependencies=dependencies,mutex=mutex,collaboration=collaboration,basis=ar.text(basis))
    if any(not isinstance(values[field],list) for field in FIELDS): raise ValueError('关联须为明确列表。')
    old=current(space)
    # Exact authenticated replay has no new reminders, flow updates or saves.
    if any(r.request_id==request_id for r in space.object_revisions.values()):
        return ar.append(space,kind=KIND,object_id=task['order_id'],source='named-deputy-task-relations',
                         actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    involved=[task]+[ref for field in FIELDS for ref in values[field]]
    if task['order_id'] in old:
        involved += [ref for field in FIELDS for ref in old[task['order_id']][1][field]]
    involved += [value['task'] for _,value in old.values() if any(ref['order_id']==task['order_id'] for field in FIELDS for ref in value[field])]
    for ref in involved:
        item=_validate_ref(ref,tasks); _live(space,item)
        if space.command_flows[ref['order_id']].stage in RUNNING|{'closed'}:
            raise ValueError('执行中、待核验或已关闭任务不能改变关联；先按既有控制规则处理。')
    revision=ar.append(space,kind=KIND,object_id=task['order_id'],source='named-deputy-task-relations',
                       actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    latest=current(space); _cycles({k:v for k,(r,v) in latest.items() if space.orders[k].status!=OrderStatus.SUPERSEDED})
    from dataclasses import replace
    affected={ref['order_id'] for ref in involved}
    if task['order_id'] in old:
        affected.update(ref['order_id'] for field in FIELDS for ref in old[task['order_id']][1][field])
    for oid in affected:
        flow=space.command_flows[oid]
        if flow.conditions_confirmed_at:
            flow=replace(flow,conditions_confirmed_at='',conditions_note='')
            space.command_flows[oid]=flow
            space.append_event('CommandFlowChanged',order_id=oid,event_id=space.event_id,flow=asdict(flow),relation_ref=revision.id)
    space.append_event('TaskRelationReminderRecorded',relation_ref=revision.id,affected_orders=sorted(affected),
                       actor=actor,notification='local-only-not-sent')
    return revision


def gate(space,order_id,ledger=None):
    tasks=published(space); item=tasks.get(order_id)
    if item is None: raise ValueError('没有本事件正式任务。')
    _live(space,item); latest=current(space)
    issues=[]; refs=[]
    _cycles({k:v for k,(r,v) in latest.items() if space.orders[k].status!=OrderStatus.SUPERSEDED})
    row=latest.get(order_id)
    if row:
        record,value=row; refs.append(record.id)
        for field in FIELDS:
            for ref in value[field]:
                try: _live(space,_validate_ref(ref,tasks))
                except ValueError as error: issues.append(str(error)); continue
                from .formal_task_feedback import verified
                if field=='dependencies' and (space.command_flows[ref['order_id']].stage!='closed' or ledger is None or not verified(space,ledger,ref['order_id'])):
                    issues.append('依赖任务尚未由指定核验负责人关闭：'+ref['order_id'])
    # A mutual exclusion declared from either side protects both tasks.
    for oid,(record,value) in latest.items():
        if space.orders[oid].status==OrderStatus.SUPERSEDED: continue
        others=[]
        if oid==order_id: others=[ref['order_id'] for ref in value['mutex']]
        elif any(ref['order_id']==order_id for ref in value['mutex']): others=[oid]
        if others: refs.append(record.id)
        for other in others:
            try: _live(space,tasks[other])
            except ValueError as error: issues.append(str(error)); continue
            if space.command_flows[other].stage in RUNNING:
                issues.append('互斥任务仍在执行或待核验：'+other)
    return tuple(dict.fromkeys(issues)),tuple(sorted(set(refs)))


def control_history(space):
    tasks=published(space); results=[]
    for index,record,value in ar.records(space,CONTROL):
        if record.source!='named-formal-task-control' or set(value)!={'task','action','basis','relation_refs'}:
            raise ValueError('执行控制来源不一致。')
        item=_validate_ref(value['task'],tasks)
        if (record.actor_id!=item[2]['recipient_id'] or record.reference.object_id!=item[2]['order_id']
                or value['action'] not in {'conditions','begin'}):
            raise ValueError('执行控制人或动作不一致。')
        prior=ar.prefix(space,index)
        if value['task']['publication_ref'] not in prior.object_revisions or any(ref not in prior.object_revisions for ref in value['relation_refs']):
            raise ValueError('执行关联来源断链。')
        ar.text(value['basis']); results.append((record,value))
    return tuple(results)


def control(space,registry,ledger,actor,*,task,action,basis,expected_version,request_id):
    tasks=published(space); item=_validate_ref(task,tasks); _live(space,item)
    publication,data,spec=item
    route=recipient_route(space,space.event_id,spec['spec']['responsible_group_id'],registry=registry,ledger=ledger)
    if actor!=spec['recipient_id'] or actor!=route.leader_id or actor not in registry.library:
        raise PermissionError('仅本版正确现任接收人确认条件和开始。')
    if action not in {'conditions','begin'}: raise ValueError('未知执行动作。')
    from .formal_task_receipts import history as receipts
    receipts(space)
    control_history(space)
    old=next((r for r in space.object_revisions.values() if r.request_id==request_id),None)
    if old:
        oldvalue=json.loads(old.content_json)
        values=dict(task=task,action=action,basis=ar.text(basis),relation_refs=oldvalue.get('relation_refs',[]))
        return ar.append(space,kind=CONTROL,object_id=task['order_id'],source='named-formal-task-control',actor=actor,
                         values=values,expected_version=expected_version,request_id=request_id)
    issues,refs=gate(space,task['order_id'],ledger)
    if issues: raise ValueError('；'.join(issues))
    from .task_release import _bundle,task_gate
    bundle=_bundle(space,data['decision_id'],data['draft_version'])
    draft=next(t for t in bundle.tasks if t.id==spec['task_id'])
    issues,support,_=task_gate(space,bundle,draft,registry=registry,ledger=ledger)
    if issues or set(support)!=set(spec['support_refs']): raise ValueError('本版专业/安全依据已变化，须重新核对。')
    flow=space.command_flows[task['order_id']]
    at=datetime.now(timezone.utc).isoformat()
    if flow.stage!='acked_not_started': raise ValueError('须先由正确接收人接收本版且尚未开始。')
    if action=='conditions': updated=flow.confirm_conditions(at=at,by=actor,note=ar.text(basis))
    else:
        previous=[v for r,v in control_history(space) if r.reference.object_id==task['order_id'] and v['action']=='conditions']
        if not previous or previous[-1]['relation_refs']!=list(refs): raise ValueError('关联已变化，须重新确认执行条件。')
        updated=flow.begin(at=at,by=actor)
    revision=ar.append(space,kind=CONTROL,object_id=task['order_id'],source='named-formal-task-control',actor=actor,
                       values=dict(task=task,action=action,basis=ar.text(basis),relation_refs=list(refs)),
                       expected_version=expected_version,request_id=request_id)
    space.command_flows[task['order_id']]=updated
    if action=='begin': space.orders[task['order_id']].status=OrderStatus.ACTIVE
    space.append_event('CommandFlowChanged',order_id=task['order_id'],event_id=space.event_id,flow=asdict(updated),control_ref=revision.id)
    return revision
