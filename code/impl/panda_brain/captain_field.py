"""Event-scoped native captain field duties; reports never verify or dispatch."""
from datetime import datetime,timezone
from html import escape
import json,re
from . import audited_records as ar,business_modules as business,captain_mobile as mobile

REPLY='CaptainTeamCompletionReported'
SHORTAGE='CaptainMaterialShortageReported'

def now():return datetime.now(timezone.utc).isoformat()

def context(state,token,eid=None):
    member=state.member_session(token)
    if not member or business.role(state,token)!='captain':raise PermissionError('需要本事件队长账号。')
    eid=eid or member.selected_event_id
    if eid not in business.events(state,token) or state.workspace.event_id!=eid or state.active_event_id!=eid:raise PermissionError('当前事件权限或上下文已变化。')
    if state.registry.events[eid].status not in {'active','upgraded'} or state.workspace.verify_audit_chain():raise ValueError('事件已结束或来源待核对。')
    return member,eid,set(business.teams(state,token,eid))

def teams(state,token):
    member,eid,owned=context(state,token);snapshot=state.ledger.snapshots.get(eid)
    out=[]
    for sid in sorted(owned):
        squad=state.ledger.squads.get(sid)
        if squad is None:continue
        arrived=snapshot.squads.get(sid) if snapshot else None
        out.append(dict(id=sid,name=squad.name,arrived=bool(arrived and arrived.arrival_id),full=bool(arrived and set(arrived.arrived_phones)==set(squad.member_phones))))
    return out

def reports(space,kind=None):
    if space.verify_audit_chain():raise ValueError('队伍回报审计链不一致。')
    out=[];seen=set()
    for e in space.events:
        if e.kind not in {REPLY,SHORTAGE} or kind and e.kind!=kind:continue
        v=e.payload
        expected={'event_id','team_id','actor','request_id','data_kind','version_ref','command_id','reply'} if e.kind==REPLY else {'event_id','team_id','actor','request_id','data_kind','material'}
        if set(v)!=expected or v['event_id']!=space.event_id or v['data_kind']!='reported' or not v['team_id'] or not v['actor'] or v['request_id'] in seen:raise ValueError('队伍回报来源或请求编号异常。')
        seen.add(v['request_id']);out.append(dict(v,id=e.id,time=e.recorded_at,kind=e.kind))
    return out

def commands(state,token):
    from .formal_task_reads import current_publications
    from .task_sop import flow_issue
    from .offline_tasks import task_state
    _,eid,owned=context(state,token);space=state.workspace;out=[];rows=reports(space,REPLY)
    for oid,(publication,data,task) in current_publications(space).items():
        group=task['spec']['responsible_group_id']
        if group not in owned:continue
        issue=flow_issue(space,oid,eid)
        if issue:continue
        flow=space.command_flows.get(oid);status=space.orders[oid].status.value
        ref=ar.digest(dict(publication=publication.id,draft_version=data['draft_version'],content_version=flow.content_version if flow else 0))
        content=task['content'];key='formal:'+oid;matches=[r for r in rows if r['command_id']==key and r['version_ref']==ref]
        out.append(dict(id=key,team_id=group,team_name=team_name(state,group),title=task['spec']['title'],
            content=content.get('content') or task['spec']['objective'],location=content.get('location') or '',
            time_requirement=content.get('time_requirement') or '未登记',completion_standard=task['spec']['completion_standard'],
            version_ref=ref,can_reply=status in {'confirmed','active'},replies=matches,
            status='已提交完成回复，待上级核验' if matches else {'confirmed':'上级已下达','active':'执行中','completed':'已核验完成','cancelled':'已取消','expired':'已过期'}.get(status,status),source=publication.id))
    for tid,t in task_state(space,eid).items():
        if t.group_id not in owned or t.state=='superseded':continue
        key='offline:'+tid;ref=ar.digest(dict(id=tid,version=t.version,content=t.content,group=t.group_id));matches=[r for r in rows if r['command_id']==key and r['version_ref']==ref]
        out.append(dict(id=key,team_id=t.group_id,team_name=team_name(state,t.group_id),title=t.content[:48],content=t.content,
            location='',time_requirement=t.due_at or '未登记',completion_standard='',version_ref=ref,
            can_reply=t.state not in {'closed','cancelled','expired'},replies=matches,status='已提交完成回复，待上级核验' if matches else '已登记上级命令',source=tid))
    return out

def team_name(state,sid):
    squad=state.ledger.squads.get(sid)
    if squad:return squad.name
    from .temporary_group_readiness import leader_state
    return str(sid)

def checked_text(value,label,limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError(label+'不正确。')
    return value.strip()

def append(state,kind,values):
    request=checked_text(values['request_id'],'请求编号',100)
    for old in reports(state.workspace):
        if old['request_id']==request:
            if old['kind']!=kind or any(old.get(k)!=v for k,v in values.items()):raise ValueError('请求编号已用于其他内容。')
            return dict(ok=True,message='该回报已提交。',record_id=old['id'])
    event=state.workspace.append_event(kind,**values)
    state.persist_if_db();state._persist_active_task_snapshot()
    return dict(ok=True,message='已提交到电脑端。',record_id=event.id)

def shortage(state,token,value):
    if set(value)!={'team_id','material','request_id'}:raise ValueError('只需填写缺少的物资名称。')
    actor,eid,owned=context(state,token)
    if value['team_id'] not in owned:raise PermissionError('只能报告本队缺物。')
    values=dict(event_id=eid,team_id=value['team_id'],actor=actor.phone,request_id=value['request_id'],data_kind='reported',material=checked_text(value['material'],'物资名称',80))
    return append(state,SHORTAGE,values)

def reply(state,token,value):
    if set(value)!={'command_id','version_ref','reply','request_id'}:raise ValueError('命令回复字段不正确。')
    actor,eid,owned=context(state,token)
    old=next((r for r in reports(state.workspace,REPLY) if r['request_id']==value['request_id']),None)
    if old:
        if old['actor']!=actor.phone or old['team_id'] not in owned or any(old.get(k)!=v for k,v in value.items()):raise ValueError('请求编号已用于其他内容。')
        return dict(ok=True,message='该回复已提交。',record_id=old['id'])
    command=next((c for c in commands(state,token) if c['id']==value['command_id']),None)
    if not command or not command['can_reply'] or command['version_ref']!=value['version_ref']:raise PermissionError('此命令不属于本队、已失效或已变更，请刷新核对。')
    values=dict(event_id=eid,team_id=command['team_id'],actor=actor.phone,request_id=value['request_id'],data_kind='reported',command_id=command['id'],version_ref=command['version_ref'],reply=checked_text(value['reply'],'完成回复',2000))
    return append(state,REPLY,values)

def arrival(state,token,value):
    if set(value)!={'team_id','qr_id','request_id'}:raise ValueError('扫码到场不接受人数、缺员或补到字段。')
    actor,eid,owned=context(state,token);sid=value['team_id'];mobile.qr_context(state,eid,value['qr_id']);checked_text(value['request_id'],'请求编号',100)
    if sid not in owned or sid not in state.ledger.squads:raise PermissionError('扫码只能确认本人队伍到场。')
    selected=next((c for c in state.captain_callouts(token) if c.event_id==eid and c.squad_id==sid),None)
    if not selected:raise PermissionError('本事件尚未召集该队。')
    squad=state.ledger.squads[sid];snap=state.ledger.snapshots.get(eid);prior=snap.squads.get(sid) if snap else None
    if prior and prior.arrival_id:
        if set(prior.arrived_phones)!=set(squad.member_phones):raise ValueError('本队历史到场记录与满编规则不一致，请在电脑端核对。')
        return dict(ok=True,message='本队已整队到场，无需重复登记。')
    state.handle_captain_scan(eid,captain_token=token,squad_id=sid,qr_id=value['qr_id'])
    state.handle_captain_arrival(eid,captain_token=token,squad_id=sid,mode='initial',phones=[],request_id=value['request_id'])
    return dict(ok=True,message='本队已按满编确认到场。')

def scene(state,token):
    from .sandview import _map_payload
    from .initial_map import collect
    from .sand_command import weather_snapshot
    _,eid,_=context(state,token);m=json.loads(_map_payload(state,'captain'));initial=collect(state,eid)
    marks=[dict(id=row['id'],title=row['title'],kind=row['kind'],points=row['pts'],geometry=row['geometry'],color=row['color'],detail=row['note'],status=row['status_word'],source='、'.join(str(x[1]) for x in row['facts'] if x[0]=='来源'),time='') for row in m['marks'] if row['pts']]
    seen={m['id'] for m in marks}
    for mark in initial['marks']:
        if mark.id not in seen and mark.geo_points:marks.append(dict(id=mark.id,title=mark.title,kind=mark.kind.value,points=[list(p) for p in mark.geo_points],geometry=mark.geometry,color='#d85832' if mark.kind.value in {'danger_zone','fire_line','fire_point'} else '#1876cb',detail=mark.note,status='模拟资料·待核对',source=mark.detail('report_source'),time=mark.detail('observed_at')))
    source=m.get('satellite') or m.get('offline') or {};zoom=source.get('zoom') or m['initialZoom']
    return dict(center=m['center'],marks=marks,zoom=zoom,min_zoom=source.get('min',1),max_zoom=source.get('max',18),
        map_available=bool(source) and not m['offlineError'],map_notice=m['offlineError'] or source.get('note') or '本地底图未接入。',
        notice=initial['notice'],weather=weather_snapshot(state.workspace,eid),revision=state.workspace.version,
        updated_at=state.workspace.events[-1].recorded_at if state.workspace.events else '',event_name=state.registry.events[eid].name,map_revision=ar.digest(source))

def state_view(state,token):
    member,eid,_=context(state,token)
    return dict(ok=True,event_id=eid,event_name=state.registry.events[eid].name,user=member.name,
        events=[dict(id=e,name=state.registry.events[e].name) for e in business.events(state,token)],teams=teams(state,token),
        scene=scene(state,token),commands=commands(state,token),shortages=[r for r in reports(state.workspace,SHORTAGE) if r['team_id'] in context(state,token)[2]],
        materials=sorted({i.name for i in state.ledger.items.values()}))

def ask(state,token,question):
    _,eid,owned=context(state,token);q=checked_text(question,'问题',1000);view=state_view(state,token);event=state.registry.events[eid];rows=[];sources=[]
    from .captain_reports import rows as field_rows
    if any(k in q for k in ('上报','现场','情况','火情','火势','道路','危险','风险')):
        reports=field_rows(state.workspace)
        rows += ['队伍上报（待核实）：'+team_name(state,r['team_id'])+'；'+r['content']+f"；附照片{len(r['photos'])}张（未自动识图）；"+r['time'] for r in reports[-20:]]
        sources += ['field:'+r['id'] for r in reports[-20:]]
        if not reports and '上报' in q:rows.append('当前事件暂无队伍现场信息上报。')
    if any(k in q for k in ('火情','火势','起火','着火','火点','火场位置')):
        rows.append(event.name+'；位置：'+(event.location or '未登记')+'；初报：'+(event.initial_report or '暂无记录'));sources.append('event:'+eid)
    if any(k in q for k in ('风速','风向','风力','大风','天气','气象','温度','湿度')):
        weather=view['scene']['weather'];rows.append(weather['notice']+'；'+('；'.join(f"{r['metric']}：{r['value']}{r['unit']}（{r['collected_at']}）" for r in weather['readings'].values()) or '暂无可追溯气象值'));sources.append('weather:'+eid)
    if any(k in q for k in ('队伍','人员','力量','到场')):
        snapshot=state.ledger.snapshots.get(eid);arrived=list(snapshot.squads.values()) if snapshot else [];rows.append(f'本事件已有{len(arrived)}支队伍登记到场；本队：'+'、'.join(t['name']+('已到场' if t['arrived'] else '未扫码到场') for t in view['teams']));sources.append('arrival:'+eid)
    if any(k in q for k in ('命令','任务','指令')):
        rows+=['本队命令：'+c['title']+'；'+c['content']+'；'+c['status'] for c in view['commands']];sources += [c['source'] for c in view['commands']]
        if not view['commands']:rows.append('当前没有下达给本队的正式或已登记命令。')
    if any(k in q for k in ('物资','缺','水','补给','装备')):
        from .material_inventory_reads import read_event
        balances=read_event(state.workspace,state.ledger,eid)
        rows += ['本事件账面可用：'+state.ledger.items[i].name+' '+('待核对' if v.book_available is None else str(v.book_available))+' '+state.ledger.items[i].unit for i,v in balances.items()]
        rows += ['本队缺物回报：'+r['material']+'（'+r['time']+'，尚未据此推定数量或配送）' for r in view['shortages'][-6:]]
        if not any('物资' in r or '账面' in r or '缺物' in r for r in rows):rows.append('暂无可核对的物资账面信息。')
        sources.append('materials:'+eid)
    if any(k in q for k in ('火点','风险','危险','水源','沙盘','地图','部署')):
        keywords=[k for k in ('火点','火线','风险','危险','水源','队伍','物资') if k in q]
        selected=[m for m in view['scene']['marks'] if not keywords or any(k in m['title']+m['detail'] for k in keywords) or any(k in q for k in ('风险','危险')) and m.get('kind') in {'danger_zone','hazard','danger_area'}][:10]
        rows += [m['title']+'：'+m['detail']+'；'+m['status']+'；登记坐标 '+str(m['points'][0]) for m in selected];sources += ['地图资料：'+m['title'] for m in selected]
        if not selected:rows.append('暂无与所问位置或风险对应的已登记态势信息。')
    if not rows:rows=['现有资料无法回答该问题。可查询当前火情、气象、到场队伍、本队命令、物资和风险点。']
    labels={'event:':'本事件登记资料','field:':'本事件队伍现场上报记录（待核实）','weather:':'本事件气象观测记录','arrival:':'本事件队伍到场记录','materials:':'本事件物资账面与缺物记录'}
    references=list(dict.fromkeys(sources));shown=[]
    for ref in references:
        label=next((v for k,v in labels.items() if ref.startswith(k)),ref if ref.startswith('地图资料：') else '本队命令登记记录')
        if label not in shown:shown.append(label)
    return dict(ok=True,answer='\n'.join(rows),sources=shown,references=references,updated_at=view['scene']['updated_at'],notice='仅查询本事件已登记资料；不推定现场实时情况。')

def report_section(state,token,kind,*,admin=False):
    if not state.active_event_id:return ''
    try:
        role=business.role(state,token);allowed=admin or role in {'duty','deputy'}
        if not allowed and role!='captain':return ''
        space=state._appointment_space(state.active_event_id)[0];owned=set(business.teams(state,token,state.active_event_id)) if not allowed else None
        rows=[r for r in reports(space,kind) if owned is None or r['team_id'] in owned]
    except PermissionError:return ''
    if not rows:return ''
    heading='队伍物资缺乏回报' if kind==SHORTAGE else '队伍命令完成回复（待上级核验）'
    return '<section class="lz-card"><h2>'+heading+'</h2>'+''.join('<p>'+escape(team_name(state,r['team_id']))+' · '+escape(r.get('material') or r.get('command_id',''))+' · '+escape(r.get('reply',''))+' · '+escape(r['time'])+'</p>' for r in rows)+'</section>'
