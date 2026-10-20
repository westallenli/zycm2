"""Unified command interaction. Grounded data, named reconnaissance, no implicit orders."""
import json
import math
import secrets
import time
from dataclasses import asdict
from urllib.parse import parse_qs

from . import audited_records as ar, intelligence, unified_ui
from .event_authorization import event_grants
from .authorization import Role
from .workbench import esc

KIND = 'recon_request'
SOURCE = 'named-reconnaissance'


def revoke_model_bindings(state, owner):
    """Settings changes/logout clear delegation and any preview retaining that provider."""
    state.sand_model_bindings = {eid:(dict(owner='',revision=-1,id='',disabled=True) if b['owner']==owner else b)
        for eid,b in state.sand_model_bindings.items()}
    state.deepseek_previews = {k:p for k,p in state.deepseek_previews.items() if p.get('binding_owner')!=owner}


def configured_model(state, eid, token=None):
    """Return an internal adapter, not configuration or credentials for the deputy."""
    if token is not None and (token, eid) in state.sand_deputy_models:
        from .deputy_models import provider
        return provider(state, token, eid)
    binding=state.sand_model_bindings.get(eid)
    if binding is not None:
        if binding.get('disabled'):
            raise ValueError('本火情模型调用授权已停用，请管理员重新配置并授权。')
        if not state.session_valid(binding['owner']):
            raise ValueError('管理员配置会话已失效，请管理员重新授权本火情模型调用。')
        session=state.model_connections.get(binding['owner'])
        if session is None or session.revision!=binding['revision']:
            raise ValueError('管理员模型配置已变化，请重新授权。')
        from .model_connections import SafeProvider
        connection=session.selected
        if not connection.enabled or (connection.vendor!='local' and not connection.api_key):
            raise ValueError('管理员已停用该模型，请核对配置。')
        return SafeProvider(connection),binding['id'],binding['owner']
    enhancer=state._build_enhancer()
    if enhancer is None:
        raise ValueError('模型未启用，请管理员在模型连接设置中配置并授权当前火情；现场查询仍可使用。')
    return enhancer.provider,'backend-config',''


def configure_model_binding(state, token, fields):
    """Only the original administrator settings gate may delegate an enabled profile."""
    session=state.model_session(token)
    if not state.command_mode(token) or not secrets.compare_digest(fields.get('csrf',''),session.csrf):
        raise PermissionError('模型授权仅管理员本人可办理。')
    if set(fields)!={'csrf','action','event_id','expected_revision','ack'}:
        raise ValueError('模型授权字段不完整或不受支持。')
    eid=fields['event_id'];event=state.registry.events.get(eid)
    if (eid!=state.active_event_id or state.workspace.event_id!=eid or event is None
            or event.status not in {'active','upgraded'} or state.workspace.verify_audit_chain()):
        raise ValueError('请先进入有效火情，再授权该火情的模型调用。')
    if fields['expected_revision']!=str(session.revision):
        raise ValueError('模型配置版本已变化，请重新核对。')
    if fields['action']=='stop-sand':
        old=state.sand_model_bindings.get(eid)
        state.sand_model_bindings[eid]=dict(owner='',revision=-1,id='',disabled=True)
        if old:state.deepseek_previews={k:p for k,p in state.deepseek_previews.items() if p.get('binding_id')!=old['id']}
        return
    if fields['action']!='share-sand' or fields['ack']!='1':
        raise ValueError('请明确确认允许本火情副指挥按次调用。')
    connection=session.selected
    if not connection.enabled or (connection.vendor!='local' and not connection.api_key):
        raise ValueError('请先保存并允许使用有效模型配置；云模型还需要 Key。')
    from .model_connections import validate_url
    validate_url(connection.base_url,connection.vendor)
    state.sand_model_bindings[eid]=dict(owner=token,revision=session.revision,id=secrets.token_urlsafe(24))


def render_model_binding(state, token):
    session=state.model_session(token);eid=state.active_event_id
    event=state.registry.events.get(eid)
    if not event or event.status not in {'active','upgraded'}:
        return '<section class="model-settings"><h2>智能参谋调用授权</h2><p>先进入有效火情后配置。</p></section>'
    binding=state.sand_model_bindings.get(eid)
    status='已授权本火情；每次分析仍需副指挥确认发送内容。' if binding and not binding.get('disabled') else '本火情尚未授权会话模型调用。'
    hidden=(f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">'
        f'<input type="hidden" name="event_id" value="{esc(eid)}">'
        f'<input type="hidden" name="expected_revision" value="{session.revision}">')
    return (f'<section class="lz-card model-settings" data-sand-model-admin><h2>当前火情 · 智能参谋调用授权</h2><p>{esc(event.name)} · {esc(eid)}</p><p>{status}</p>'
        '<p>只有管理员配置连接和凭证。指定副指挥只确认本次发送内容与调用，不取得 Key 或配置权限。授权随管理员退出、会话到期、重启或修改配置失效。</p>'
        '<form method="post" action="/models">'+hidden+'<label><input type="checkbox" name="ack" value="1" required>允许当前火情指定副指挥按次确认调用当前选定模型</label>'
        '<button name="action" value="share-sand">授权本火情智能参谋调用</button></form>'
        '<form method="post" action="/models">'+hidden+'<input type="hidden" name="ack" value="0">'
        '<button name="action" value="stop-sand">停止本火情模型调用授权</button></form></section>')


def context(state, token):
    member = state.member_session(token)
    eid = state.active_event_id
    if (member is None or not unified_ui.can_read(state, token)
            or not eid or state.workspace.event_id != eid
            or state.registry.events[eid].status not in {'active', 'upgraded'}
            or state.workspace.verify_audit_chain()):
        raise PermissionError('需要当前有效火情的具名会话。')
    return member, eid


def recipients(state):
    people = {g.subject_id for g in state._duty_grants}
    people.update(g.subject_id for g in event_grants(state.workspace) if g.role == Role.DEPUTY)
    from .task_recipients import recipient_route
    for team in state.ledger.squads:
        try:
            people.add(recipient_route(state.workspace, state.active_event_id, team,
                registry=state.registry, ledger=state.ledger).leader_id)
        except (ValueError, KeyError, PermissionError):
            continue
    return {p: state.registry.library[p].name for p in sorted(people) if p in state.registry.library}


def recon_history(space):
    result = {}
    for index, record, row in ar.records(space, KIND):
        if record.source != SOURCE or set(row) != {'status', 'need', 'area', 'recipient', 'deadline',
                'basis', 'report', 'observed_at', 'geo', 'kind', 'annotation_id'}:
            raise ValueError('侦查记录来源或字段不一致。')
        old = result.get(record.reference.object_id)
        if old is None:
            if (row['status'] != 'sent' or record.reference.version != 1
                    or row['report'] or row['annotation_id']
                    or not any(g.role == Role.DEPUTY and g.subject_id == record.actor_id
                        for g in event_grants(ar.prefix(space, index)))):
                raise ValueError('侦查请求缺少当时副指挥确认依据。')
        elif (old['status'] != 'sent' or row['status'] != 'reported'
                or record.actor_id != old['recipient']
                or any(row[k] != old[k] for k in ('need', 'area', 'recipient', 'deadline', 'basis'))):
            raise ValueError('侦查回报与具名请求不一致。')
        result[record.reference.object_id] = dict(row, id=record.reference.object_id,
            version=record.reference.version, actor=record.actor_id, time=record.times.recorded_at)
    return result


def model_status(state, eid, token=None):
    try:
        provider, _, _ = configured_model(state, eid, token)
        connection = getattr(provider, 'connection', None)
        label = connection.label if connection is not None else '后台模型'
        return dict(ready=True, label=label, timeout_seconds=getattr(connection, 'timeout', 30),
            notice=label+'已启用；选择“大模型支撑”并点击发送即可提问，实际连通以回复为准。')
    except (ValueError, PermissionError):
        return dict(ready=False, label='', notice='大模型未就绪：请当前副指挥从火情页面进入“大模型配置（Key）”配置并启用。现场资料查询仍可使用。')


def weather_snapshot(workspace, eid):
    """Read existing event observations only; never fetch or invent weather."""
    from datetime import datetime
    from .observation_context import current_context
    metrics={'temperature_c','humidity_pct','wind_speed_m_s','wind_direction_deg'}
    rows=[]
    for row in current_context(workspace,eid)['rows']:
        if row['kind']!='观测结论' or len(row['metrics'])!=1 or row['metrics'][0] not in metrics:
            continue
        claim=row['claim'];metric=row['metrics'][0]
        refs=[r for r in row['evidence'] if r['metric']==metric and r['collected_at']]
        if not refs:continue
        latest=max(refs,key=lambda r:datetime.fromisoformat(r['collected_at']))
        value=claim['value']
        if not isinstance(value,dict) or type(value.get('value')) not in (int,float) or not math.isfinite(value['value']):continue
        rows.append(dict(metric=metric,value=value['value'],unit=value.get('unit',''),
            station=claim['subject'].rsplit(':',1)[0],source=latest['source'],
            collected_at=latest['collected_at'],data_kind=latest['data_kind'],status=str(claim['status'])))
    rows.sort(key=lambda r:datetime.fromisoformat(r['collected_at']),reverse=True)
    station=rows[0]['station'] if rows else ''
    readings={}
    for row in rows:
        if row['station']==station and row['metric'] not in readings:readings[row['metric']]=row
    return dict(station=station,readings=readings,available=bool(readings),
        collected_at=rows[0]['collected_at'] if rows else '',
        source='、'.join(dict.fromkeys(r['source'] for r in readings.values())),
        notice=('模拟观测 · 待核对' if any(r['data_kind']=='simulated' for r in readings.values()) else
                '来源观测 · 待核对' if readings else '气象资料未接入'))


def summary(state, token):
    member, eid = context(state, token)
    report, marks = intelligence.collect(state, eid)
    rows = list(recon_history(state.workspace).values())
    deputy = eid in state.deputy_events(token)
    from .map_brief import collect as map_brief
    visible_rows = [r for r in rows if deputy or r['recipient'] == member.phone]
    brief=map_brief(state, token, report, marks, visible_rows)
    from .command_sop import queue, demo_plans
    return dict(event_id=eid, version=state.workspace.version, basis=report['fingerprint'],
        map_brief=brief, command_sop=queue(state,brief,visible_rows), demo_plans=demo_plans(brief),
        deputy=deputy, actor=member.phone, recipients=recipients(state) if deputy else {},
        gaps=report['gaps'], changes=report['changes'],weather=weather_snapshot(state.workspace,eid),
        requests=[r for r in rows if deputy or r['recipient'] == member.phone],
        plans=[dict(id=o.id, title=o.title, summary=o.summary, status=o.status,
            risks=list(o.risks), preconditions=list(o.preconditions), mapped=bool(o.sim_shapes),
            review_reports=[r['id'] for r in rows if r['status']=='reported'])
            for o in sorted(state.workspace.options.values(),key=lambda o:o.id) if o.status in {'active', 'affected'}][:3],
        model=model_status(state, eid, token), data_time=report['revision'], source_note=report['external_note'])


def write_recon(state, token, values):
    member, eid = context(state, token)
    if values['event_id'] != eid:
        raise PermissionError('请求不属于当前火情。')
    expected = values['expected_version']
    if not expected.isascii() or not expected.isdigit() or len(expected) > 9:
        raise ValueError('请求版本无效。')
    object_id = values['id']
    if not object_id or len(object_id) > 100:
        raise ValueError('请求编号无效。')
    action = values['action']
    fields = ('need', 'area', 'recipient', 'deadline', 'basis', 'report', 'observed_at', 'geo', 'kind')
    if any(len(values[k]) > (2000 if k == 'report' else 600) for k in fields):
        raise ValueError('内容过长。')
    # Retry of the exact acknowledged POST returns its old result, even after a new version.
    for _, record, saved in ar.records(state.workspace, KIND):
        if record.request_id == values['request_id']:
            if (record.reference.object_id == object_id and record.actor_id == member.phone
                    and record.reference.version == int(expected) + 1
                    and all(saved[k] == values[k] for k in fields)):
                return object_id
            raise ValueError('请求号已用于其他内容。')
    rows = recon_history(state.workspace)
    row = {k: values[k].strip() for k in fields}
    row.update(status='sent', annotation_id='')
    if action == 'send':
        if eid not in state.deputy_events(token):
            raise PermissionError('侦查请求须由本事件指定副指挥确认。')
        if object_id in rows or int(expected) != 0 or values['ack'] != '1':
            raise ValueError('请确认当前新请求。')
        if (not row['need'] or not row['area'] or not row['deadline']
                or row['recipient'] not in recipients(state) or row['report'] or row['geo'] or row['observed_at'] or row['kind']):
            raise ValueError('请明确收集内容、区域、责任人和回报要求。')
        if summary(state, token)['basis'] != row['basis']:
            raise ValueError('现场资料已变化，请保留填写内容并重新核对后确认。')
    elif action == 'report':
        old = rows.get(object_id)
        if (old is None or old['recipient'] != member.phone or member.phone not in recipients(state)):
            raise PermissionError('只有仍具有当前职责的指定责任人可以回报。')
        if old['status'] != 'sent' or old['version'] != int(expected):
            raise ValueError('请求已回报或版本已变化。')
        if any(old[k] != row[k] for k in ('need', 'area', 'recipient', 'deadline', 'basis')):
            raise ValueError('不能修改原始侦查请求。')
        if not row['report'] or not row['observed_at']:
            raise ValueError('请填写实际回报内容与观测时间。')
        from .record_contracts import explicit_time
        explicit_time(row['observed_at'])
        row['status'] = 'reported'
        if row['geo']:
            if row['kind'] not in {'hotspot','fire_area','main_fireline','team_position','water_source','hazard_zone'}:
                raise ValueError('有位置的回报请明确对象类型；不能把所有资料都标成火点。')
            row['annotation_id'] = state.handle_sand_observe(kind=row['kind'],
                title='侦查回报：' + row['need'][:36], geo_text=row['geo'],
                observed_at=row['observed_at'], source_ref=object_id, note=row['report'],
                reporter=member.phone, details={'source_ref': object_id})
    else:
        raise ValueError('未知侦查操作。')
    ar.append(state.workspace, kind=KIND, object_id=object_id, source=SOURCE,
        actor=member.phone, values=row, expected_version=int(expected), request_id=values['request_id'])
    state.persist_if_db()
    state._persist_active_task_snapshot()
    return object_id


def image_bounds(value):
    """Explicit WGS84 south,west,north,east; descriptions never become geometry."""
    try:
        parts = [float(x) for x in value.replace('，', ',').split(',')]
        if (len(parts) != 4 or not all(math.isfinite(x) for x in parts)
                or not -90 <= parts[0] < parts[2] <= 90
                or not -180 <= parts[1] < parts[3] <= 180):
            return None
        return [[parts[0], parts[1]], [parts[2], parts[3]]]
    except (ValueError, AttributeError):
        return None


def imagery_payload(state, role):
    if role == 'public':
        return []
    return [dict(id=i.image_id, title=i.source_system, time=i.collected_at,
        bounds=image_bounds(i.spatial_range), registered=i.registered,
        url='/uploads/' + i.file_path if i.file_path else '', status=i.status_word(), basis=ar.digest(asdict(i)))
        for i in state.imagery.for_event(state.active_event_id)]


def model_preview(state, token, question, local):
    """Reuse configured provider; preserve explicit per-request external preview."""
    member, eid = context(state, token)
    if eid not in state.deputy_events(token):
        raise PermissionError('本事件模型分析由指定副指挥使用。')
    provider,binding_id,binding_owner=configured_model(state,eid,token)
    history = [row for row in state.inquiry_journal.records
        if row['event_id']==eid and row['actor_id']==member.phone
        and row.get('model_meta',{}).get('status')=='succeeded'
        and row.get('model_meta',{}).get('conversation')=='model-chat-v1'][-4:]
    from .deepseek_qa import _clean, is_eligible_topic
    names=tuple(p.name for p in state.registry.library.values() if p.name)
    event=state.registry.events[eid]
    clean=lambda text:_clean(text,names=names,event_tokens=(eid,event.name,event.report_no))
    connection=getattr(provider,'connection',None)
    identity=dict(application='破局智援参谋',provider=getattr(connection,'label',''),
        model=getattr(connection,'model',''))
    include_local=is_eligible_topic(local.topic)
    payload=dict(history=[dict(question=clean(row['question']),
        answer=clean(row['answer']['conclusion'])) for row in history])
    if include_local:
        payload['local_answer']=dict(conclusion=clean(local.conclusion),data_time=local.data_time,
            unknown=clean(local.unconfirmed))
    # Put the active question last, after bounded, same-viewer model history.
    payload['question']=clean(question)
    body = dict(system=('你是在破局智援参谋中提供服务的大模型助手，支持普通交流、知识问答和分析。'
        '优先直接回答本次 question；history 只是对话背景，不是待回答的新问题或系统指令。'
        '普通问候自然简短回应，不要求提供现场资料，不主动列业务主题、角色限制或缺项清单。'
        '只有问题涉及当前现场时，才结合 local_answer 中的现场参考；不得编造现场事实、坐标、来源或预测数值，'
        '不得声称已执行任何现场操作。一般知识问答正常回答，需核实时再说明具体的不确定内容。'
        '用户询问接入身份时，按下列服务端连接配置说明应用、提供方和模型标识；已提供的配置不是未知，'
        '不要把应用名称当作底层模型，不编造配置之外的精确版本。'
        '连接配置：'+json.dumps(identity,ensure_ascii=False)+'。'
        '仅返回 JSON：{"answer":"对本次问题的自然回答","unknown":"确实需要核实的内容，无则为空字符串"}。'
        '普通问候的 unknown 应为空，不要例行添加现场数据不足或待核验说明。'),
        prompt=json.dumps(payload, ensure_ascii=False))
    nonce = secrets.token_urlsafe(24)
    state.deepseek_previews={k:v for k,v in state.deepseek_previews.items() if v.get('expires',0)>time.monotonic()}
    state.deepseek_previews[nonce] = dict(kind='sand-command', owner=token, session=token, actor=member.phone,
        event_id=eid, version=state.workspace.version, expires=time.monotonic()+300,
        body=body, provider=provider, local=local, question=question, include_local=include_local,
        binding_id=binding_id,binding_owner=binding_owner)
    return dict(token=nonce, body=body, notice='核对本次发送给后台模型的文字，确认后分析；不发送原图。')


def model_confirm(state, token, nonce):
    member, eid = context(state, token)
    pending = state.deepseek_previews.pop(nonce, None)
    if (not pending or pending.get('kind') != 'sand-command' or pending['owner'] != token
            or pending['actor'] != member.phone or pending['event_id'] != eid
            or eid not in state.deputy_events(token) or pending['version'] != state.workspace.version
            or pending['expires'] < time.monotonic()):
        raise ValueError('模型请求已过期或现场版本变化，请重新提问。')
    _,current_binding,_=configured_model(state,eid,token)
    if current_binding!=pending['binding_id']:
        raise ValueError('模型配置或启用状态已变化，请重新提问并核对发送内容。')
    try:
        raw = pending['provider'].complete(**pending['body'])
        parsed = json.loads(raw)
        if (set(parsed) != {'answer', 'unknown'} or not all(isinstance(v, str) for v in parsed.values())
                or not parsed['answer'].strip() or any(len(v) > 12000 for v in parsed.values())):
            raise ValueError('模型返回结构不正确。')
    except Exception as exc:
        from .model_connections import ConnectionUnavailable
        if isinstance(exc, ConnectionUnavailable):
            raise ValueError('模型分析失败：'+str(exc)+'（原问题已保留，可重新发送。）') from None
        raise ValueError('模型分析失败；原问题与本地回答保留，可重试或继续人工办理。') from None
    from .sandtable import InquiryAnswer
    local = pending['local']
    include_local=pending.get('include_local',False)
    answer = InquiryAnswer('model_analysis', parsed['answer'],
        local.data_time if include_local else '', local.sources if include_local else (), '', '模型回答', parsed['unknown'])
    from .grounded_inquiry import validate_sources
    citations=validate_sources(answer,state.workspace,state.ledger,eid,'member',
        actor_id=member.phone,deputy=True,review_ledger=state.review_ledger,registry=state.registry,state=state)
    state.inquiry_journal.append(event_id=eid, actor_id=member.phone, role='member',
        question=pending['question'], answer=answer, citations=citations, context_id='',
        workspace_version=state.workspace.version,
        model_meta={'provider': getattr(pending['provider'], 'name', 'configured'), 'conversation':'model-chat-v1',
            'request_sha256': ar.digest(pending['body']), 'status': 'succeeded'})
    return asdict(answer)


def post(handler, state):
    from .http_rejection import request_length
    from .durable_storage import action
    from .panel import PanelActionError
    token = handler._member_cookie()
    try:
        member, eid = context(state, token)
        length = request_length(handler, 20000)
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise ValueError('请求格式无效。')
        fields = parse_qs(handler.rfile.read(length).decode('utf-8'), keep_blank_values=True,
            errors='strict', max_num_fields=20)
        if any(len(v) != 1 for v in fields.values()):
            raise ValueError('不接受重复字段。')
        v = {k: row[0] for k, row in fields.items()}
        if (v.get('event_id') != eid or not secrets.compare_digest(v.get('csrf', ''), member.custody_csrf)):
            raise PermissionError('会话或火情已变化，请保留输入并重新登录核对。')
        op = v.get('op')
        base = {'event_id', 'csrf', 'op'}
        if op == 'map-presentation':
            if set(v) != base | {'id','basis','action','title','points','reason','expected_version','request_id'}:
                raise ValueError('建议标绘字段缺失或不受支持。')
            from .map_brief import write
            with action(state):
                revision = write(state, token, v)
            result = dict(id=revision.reference.object_id, message='显示调整已保存；未替代专业核验或发布。')
        elif op == 'recon':
            if set(v) != base | {'id', 'expected_version', 'action', 'need', 'area', 'recipient',
                    'deadline', 'basis', 'report', 'observed_at', 'geo', 'kind', 'ack', 'request_id'}:
                raise ValueError('侦查字段缺失或不受支持。')
            with action(state):
                object_id = write_recon(state, token, v)
            result = dict(message='侦查请求已确认派发。' if v['action']=='send' else '回报已收录；待核验资料不等于任务完成。', id=object_id)
        elif op == 'image-align':
            if set(v) != base | {'image_id', 'bounds', 'image_basis', 'request_id'}:
                raise ValueError('影像配准字段无效。')
            if eid not in state.deputy_events(token):
                raise PermissionError('影像配准须由当前指定副指挥确认。')
            bounds = image_bounds(v['bounds'])
            if bounds is None:
                raise ValueError('请提供有效的南、 西、 北、 东四至坐标。')
            with action(state):
                image = state.imagery.get(v['image_id'])
                if image.event_id != eid:
                    raise PermissionError('影像不属于当前火情。')
                values = dict(image_id=image.image_id, bounds=v['bounds'], basis=v['image_basis'])
                matches = [r for _,r,_ in ar.records(state.workspace,'image_alignment') if r.request_id==v['request_id']]
                if matches:
                    if matches[0].actor_id != member.phone or matches[0].content_json != ar.dump(values):
                        raise ValueError('请求号已用于其他配准。')
                else:
                    if ar.digest(asdict(image)) != v['image_basis']:
                        raise ValueError('影像版本已变化，请重新读取后对齐。')
                    from dataclasses import replace
                    state.imagery.images[image.image_id] = replace(image, spatial_range=v['bounds'],
                        status='overlaid' if image.status in {'uploaded','awaiting_registration','located'} else image.status,
                        geo_precision='人工四至范围对齐，非正射精度核验')
                    count = sum(r.reference.object_id==image.image_id for _,r,_ in ar.records(state.workspace,'image_alignment'))
                    ar.append(state.workspace, kind='image_alignment',object_id=image.image_id,
                        source='named-image-alignment',actor=member.phone,values=values,
                        expected_version=count,request_id=v['request_id'])
                    state._save_imagery();state.persist_if_db();state._persist_active_task_snapshot()
            result = dict(message='影像范围已确认，可在图层中查看实际图片；人工对齐不代表精度已核验。')
        elif op == 'model-confirm':
            if set(v) != base | {'preview_token'}:
                raise ValueError('模型确认字段无效。')
            result = dict(answer=model_confirm(state, token, v['preview_token']))
        elif op == 'ask':
            if set(v) not in (base | {'question'}, base | {'question', 'mode'}) or not v['question'].strip() or len(v['question']) > 2000:
                raise ValueError('请输入 1–2000 字的问题。')
            mode = v.get('mode', 'scene')
            if mode not in {'scene', 'model'}:
                raise ValueError('请选择现场信息查询或大模型支撑。')
            if mode == 'model' and eid not in state.deputy_events(token):
                raise PermissionError('大模型支撑由当前火情的指定副指挥使用。')
            q = v['question'].strip()
            try:
                answer = state.handle_grounded_sand_ask(q, role='member', member_token=token)
            except PanelActionError:
                if mode == 'scene':
                    raise
                from .sandtable import InquiryAnswer
                answer = InquiryAnswer('unknown', '现有资料不足，需核实适用条件。', '', (), '', '未知', '')
            result = dict(answer=asdict(answer), mode=mode)
            if mode == 'model':
                try:
                    result['preview'] = model_preview(state, token, q, answer)
                except (ValueError, PermissionError) as exc:
                    result['model_notice'] = str(exc)
        else:
            raise ValueError('未知操作。')
        handler._respond_json(dict(ok=True, **result))
    except PermissionError as exc:
        handler._respond_json(dict(ok=False, error=str(exc)), status=403)
    except (ValueError, PanelActionError, UnicodeError) as exc:
        handler._respond_json(dict(ok=False, error=str(exc)), status=409)
    except OSError:
        handler._respond_json(dict(ok=False, error='保存中断，请保留内容并刷新核对，不要重复派发。'), status=503)


def render(state, role, viewer):
    if role == 'public':
        return ''
    return ('<section id="sand-unified-assistant" hidden>'
        '<div class="command-chat-modes" role="tablist" aria-label="问询方式">'
        '<button type="button" role="tab" data-chat-mode="scene" aria-selected="true">现场信息查询</button>'
        '<button type="button" role="tab" data-chat-mode="model" aria-selected="false">大模型支撑</button></div>'
        '<form id="sand-chat-form"><label for="sand-chat-input">向智能参谋提问或下达操作</label>'
        '<textarea id="sand-chat-input" name="question" maxlength="2000" rows="2" required placeholder="例如：比较三个方案；切换卫星图；显示方案二"></textarea>'
        '<button type="submit">发送</button></form>'
        '<p id="sand-chat-status" role="status" aria-live="polite">请输入问题。</p>'
        '<div id="sand-chat-log" role="log" aria-live="polite"></div>'
        '<details class="command-chat-help"><summary>使用说明与问答记录</summary>'
        '<p id="sand-chat-mode-note">查询系统已有现场资料，也可输入地图显示操作。语音请使用输入法。</p>'
        '<p id="sand-model-state">正在核对大模型连接与本火情授权…</p>'
        '<a href="#cmd-advisor">现场资料</a> · <a href="#drawer-qa">问答记录</a> · <a href="#drawer-intent">指令草稿与确认</a></details></section>'
        '<section id="sand-recon-panel" hidden>'
        '<div id="sand-recon-gaps"></div><div id="sand-recon-requests"></div></section>')
