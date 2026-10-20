"""Conservative disconnected reports: original envelope, explicit current recheck.

This is not offline authority or an automatic task publisher. Only existing
captain losses and executing-person feedback may be replayed, atomically with
their server receipt. Conflicts keep the original client draft unchanged.
"""
import re
import math
from dataclasses import asdict
from datetime import datetime,timezone
from . import audited_records as ar
from .authorization import Role
from .event_authorization import event_grants
from .record_contracts import explicit_time
from .offline_resources import current as current_map, loads

KIND='terminal_report'
SOURCE='named-terminal-original-report'
FIELDS={'schema_version','event_id','actor_id','request_id','created_at','context_digest','operation','details'}
SPECS={
    'material_loss':{'squad_id','item_id','loss_id','kind','amount','expected_version','evidence','occurred_at','reason'},
    'formal_feedback':{'order_id','publication_ref','expected_version','action','basis'},
    'offline_feedback':{'task_id','expected_version','action','basis'},
}
LABELS=dict(material_loss='本队物资消耗或损坏报告',formal_feedback='正式任务执行情况报告',offline_feedback='已登记线下任务执行情况报告')


def fingerprint(state,space):
    return ar.digest(dict(head=space.events[-1].hash if space.events else '',event=asdict(state.registry.events[space.event_id]),
        ledger=state.ledger.to_dict(),people={p:asdict(v) for p,v in state.registry.library.items()},
        roster=asdict(state.registry.rosters[space.event_id]) if space.event_id in state.registry.rosters else None))


def can_view(state,space,actor):
    if any(g.subject_id==actor for g in event_grants(space)):return True
    from .callouts import callout_state
    if any(c.leader_phone==actor for c in callout_state(space,space.event_id).values()):return True
    roster=state.registry.rosters.get(space.event_id)
    return bool(roster and actor in roster.entries or any(b.person.phone==actor and b.status=='active' for b in space.roster_bindings.values()))


def require_report(state,space,actor,operation,details):
    if operation not in SPECS or not isinstance(details,dict) or set(details)!=SPECS[operation]:raise ValueError('断线可补报范围或字段越界。')
    if operation=='material_loss':
        from .callouts import callout_state
        call=callout_state(space,space.event_id).get(details['squad_id']);squad=state.ledger.squads.get(details['squad_id']);person=state.registry.library.get(actor)
        if not call or call.leader_phone!=actor or not squad or squad.leader_phone!=actor or not person or not person.is_leader:
            raise PermissionError('恢复后须仍为本事件该队现任队长。')
    elif operation=='formal_feedback':
        from .formal_task_receipts import published
        from .task_recipients import recipient_route
        task=published(space).get(details['order_id'])
        if task is None:raise ValueError('原正式任务不存在。')
        route=recipient_route(space,space.event_id,task[2]['spec']['responsible_group_id'],registry=state.registry,ledger=state.ledger)
        if task[2]['recipient_id']!=actor or route.leader_id!=actor:raise PermissionError('恢复后须仍为本版正确执行负责人。')
        if details['action'] not in {'progress','difficulty','resolved','partial','completed'}:raise ValueError('核验关闭与发布不属于断线可补报动作。')
    else:
        from .offline_tasks import task_state
        task=task_state(space,space.event_id).get(details['task_id'])
        if task is None:raise ValueError('原线下任务不存在。')
        if task.recipient_id!=actor or not state._offline_leader(space,space.event_id,task.group_id,actor):raise PermissionError('恢复后执行职责已变化。')
        if details['action'] not in {'received','started','progress','difficulty','anomaly','resolved','completed'}:raise ValueError('不属于断线执行反馈。')


def context(state,event_id,token):
    actor=state.authorization_subject(token);space=state._appointment_space(event_id)[0]
    if state.registry.events[event_id].status not in {'active','upgraded'} or not can_view(state,space,actor):raise PermissionError('仅当前获本事件查看权的具名人员可准备离线资料。')
    templates=[]
    from .callouts import callout_state
    for sid,call in callout_state(space,event_id).items():
        squad=state.ledger.squads.get(sid);person=state.registry.library.get(actor)
        arrival=state.ledger.snapshots.get(event_id)
        if not squad or call.leader_phone!=actor or squad.leader_phone!=actor or not person or not person.is_leader or not arrival or sid not in arrival.squads:continue
        templates.append(dict(operation='material_loss',label=squad.name+'：消耗或损坏',fixed=dict(squad_id=sid,expected_version='0'),
            fields=[dict(key='item_id',label='物资',options=[dict(value=i.item_id,label=i.name+'（'+i.unit+'）') for i in state.ledger.items.values()]),
                dict(key='kind',label='报告类型',options=[dict(value='consumed',label='消耗'),dict(value='damaged',label='损坏')]),
                dict(key='amount',label='本次数量',type='number'),dict(key='evidence',label='原始报告依据'),
                dict(key='occurred_at',label='实际发生时间（含时区）',type='time'),dict(key='reason',label='报告说明')]))
    from .formal_task_receipts import published
    from .formal_task_feedback import history
    tasks=published(space);feedback=history(space,state.ledger)
    for oid,(r,v,t) in tasks.items():
        if t['recipient_id']!=actor or space.command_flows[oid].stage not in {'running','partial','done_pending_verify'}:continue
        details=dict(order_id=oid,publication_ref=r.id,expected_version=sum(q.reference.object_id==oid for q,w in feedback),action='progress',basis='准备离线报告')
        try:require_report(state,space,actor,'formal_feedback',details)
        except PermissionError:continue
        templates.append(dict(operation='formal_feedback',label='正式任务 '+oid,fixed={k:details[k] for k in ('order_id','publication_ref','expected_version')},
            fields=[dict(key='action',label='执行情况',options=[dict(value=a,label=l) for a,l in [('progress','进展'),('difficulty','困难'),('resolved','困难已处理'),('partial','部分完成'),('completed','完成待核验')]]),dict(key='basis',label='原始情况与依据')]))
    from .offline_tasks import task_state
    for tid,t in task_state(space,event_id).items():
        if t.recipient_id!=actor or t.state in {'closed','cancelled','expired','superseded'} or not state._offline_leader(space,event_id,t.group_id,actor):continue
        templates.append(dict(operation='offline_feedback',label='线下已登记任务 '+tid,fixed=dict(task_id=tid,expected_version=t.version),
            fields=[dict(key='action',label='执行情况',options=[dict(value=a,label=l) for a,l in [('received','接收'),('started','开始'),('progress','进展'),('difficulty','困难'),('anomaly','异常'),('resolved','困难已处理'),('completed','完成待核验')]]),dict(key='basis',label='原始情况与依据')]))
    selected=current_map(space);package=None
    if selected:
        r,v=selected;package=dict(revision=r.id,version=r.reference.version,sha256=v['sha256'],manifest=v['manifest'],tile_hashes=v['tile_hashes'],
            urls=[f'/offline/tiles/{event_id}/{r.id}/{key}.png' for key in sorted(v['tile_hashes'])])
    event=state.registry.events[event_id]
    return dict(schema_version=1,event_id=event_id,event_name=event.name,actor_id=actor,actor_name=state.registry.library[actor].name,
        issued_at=datetime.now(timezone.utc).isoformat(),context_digest=fingerprint(state,space),templates=templates,map=package,
        center=[event.lat,event.lon] if event.lat is not None and event.lon is not None else None)


def history(space):
    rows=ar.records(space,KIND)
    for index,r,v in rows:
        if (r.source!=SOURCE or set(v)!={'envelope','business_events'} or not isinstance(v['envelope'],dict)
                or r.actor_id!=v['envelope'].get('actor_id') or r.request_id!=v['envelope'].get('request_id')
                or v['envelope'].get('event_id')!=space.event_id or r.reference.version!=1
                or r.reference.object_id!='terminal-'+r.request_id):
            raise ValueError('终端原请求与具名回执不一致。')
        envelope(v['envelope'])
        if explicit_time(v['envelope']['created_at'])>explicit_time(r.times.recorded_at):raise ValueError('终端原稿时间晚于接收回执。')
        if not v['business_events']:raise ValueError('补报缺少实际业务落账。')
        business=[]
        for seq,digest in v['business_events']:
            if type(seq) is not int or not 1<=seq<=index or space.events[seq-1].hash!=digest:raise ValueError('终端补报业务来源断链。')
            business.append(space.events[seq-1])
        original=v['envelope'];d=original['details'];request=original['request_id']+'-business';matched=[]
        for event in business:
            payload=event.payload
            if original['operation']=='material_loss' and event.kind=='ManualMaterialLossReported':
                item=payload['record'];content=loads(item['content_json'])
                if (item['request_id']==request and item['actor_id']==original['actor_id']
                    and item['reference']['object_id']==d['loss_id'] and item['reference']['version']==int(d['expected_version'])+1
                    and item['times']['occurred_at']==d['occurred_at'] and content['reason']==d['reason']
                    and item['source']=='人工报告：'+d['evidence']
                    and all(content[k]==d[k] for k in ('squad_id','item_id','kind')) and content['amount']==float(d['amount'])):matched.append(event)
            elif original['operation']=='formal_feedback' and event.kind=='ObjectRevisionRecorded':
                item=payload['revision'];content=loads(item['content_json'])
                if (item['request_id']==request and item['actor_id']==original['actor_id'] and item['reference']['object_kind']=='formal_task_feedback'
                    and item['reference']['version']==d['expected_version']+1 and all(content[k]==d[k] for k in ('order_id','publication_ref','action','basis'))):matched.append(event)
            elif original['operation']=='offline_feedback' and event.kind=='OfflineTaskFeedbackRecorded':
                if (payload['request_id']==request and payload['actor']==original['actor_id'] and payload['event_id']==space.event_id
                    and payload['version']==d['expected_version'] and all(payload[k]==d[k] for k in ('task_id','action','basis'))):matched.append(event)
        if len(matched)!=1:raise ValueError('终端原稿与实际业务报告内容不一致。')
    return tuple(rows)


def envelope(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or type(value['schema_version']) is not int or value['schema_version']!=1:raise ValueError('终端原请求格式不正确。')
    if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):raise ValueError('原请求号不正确。')
    ar.text(value['actor_id']);ar.text(value['event_id'])
    if not isinstance(value['context_digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['context_digest']):raise ValueError('缺少准备离线时的完整版本依据。')
    explicit_time(value['created_at'])
    if value['operation'] not in SPECS or not isinstance(value['details'],dict) or set(value['details'])!=SPECS[value['operation']]:raise ValueError('断线报告类型或字段越界。')
    details=value['details']
    if value['operation']=='material_loss':
        if type(details['amount']) not in {str,int,float} or isinstance(details['amount'],str) and len(details['amount'])>64:
            raise ValueError('报告数量须为明确有限正数。')
        try:amount=float(details['amount'])
        except (ValueError,OverflowError):raise ValueError('报告数量须为明确有限正数。')
        if not math.isfinite(amount) or amount<=0:raise ValueError('报告数量须为明确有限正数。')
        if not isinstance(details['expected_version'],str):raise ValueError('物资原始版本格式不正确。')
    elif type(details['expected_version']) is not int or details['expected_version']<0:
        raise ValueError('任务原始版本须为明确非负整数。')
    if len(ar.dump(value).encode())>16*1024:raise ValueError('单份断线报告超过16KiB边界。')
    return value


def submit(state,event_id,token,value):
    value=envelope(value);actor=state.authorization_subject(token)
    if value['event_id']!=event_id or value['actor_id']!=actor:raise PermissionError('须由原具名人员重新登录并补报同一事件。')
    space,target,active=state._appointment_space(event_id,for_write=True)
    operation=value['operation'];details=value['details']
    require_report(state,space,actor,operation,details);rows=history(space)
    old=next((r for _,r,v in rows if r.request_id==value['request_id']),None)
    if old:
        if loads(old.content_json)['envelope']!=value:raise ValueError('原请求号已用于不同内容；不会覆盖原稿。')
        return old
    if explicit_time(value['created_at'])>datetime.now(timezone.utc):raise ValueError('终端记录时间晚于服务端接收时间，请保留原稿核对时钟。')
    if value['context_digest']!=fingerprint(state,space):raise ValueError('离线准备后的来源或版本已变化，原稿保留；请重新核对后另存新请求，不自动覆盖。')
    before=len(space.events);request=value['request_id']+'-business'
    if operation=='material_loss':state.handle_manual_material_loss(event_id,captain_token=token,request_id=request,**details)
    elif operation=='formal_feedback':state.handle_formal_task_feedback(event_id,member_token=token,request_id=request,**details)
    else:state.handle_offline_task_feedback(event_id,member_token=token,request_id=request,**details)
    space,target,active=state._appointment_space(event_id,for_write=True)
    events=[[e.sequence,e.hash] for e in space.events[before:]]
    if not events:raise ValueError('业务请求号已有其他来源，不能冒充新的终端补报。')
    receipt=ar.append(space,kind=KIND,object_id='terminal-'+value['request_id'],source=SOURCE,actor=actor,
        values=dict(envelope=value,business_events=events),expected_version=0,request_id=value['request_id'])
    history(space)
    from .persistence import save_workspace
    save_workspace(space,target)
    if active:state.workspace=space
    return receipt
