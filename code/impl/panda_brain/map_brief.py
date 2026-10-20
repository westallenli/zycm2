"""Map presentation derived from audited local sources; never verifies or issues tasks."""
import json
import math
from . import audited_records as ar

KIND = 'map_presentation'
SOURCE = 'named-map-presentation'


def points(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 200:
        raise ValueError('请提供 1–200 个有效坐标点。')
    result = []
    for p in value:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in p)
                or not -90 <= p[0] <= 90 or not -180 <= p[1] <= 180):
            raise ValueError('坐标须为有效 WGS84 纬度、经度。')
        result.append(list(p))
    return result


def history(space):
    rows = {}
    for _, record, value in ar.records(space, KIND):
        if record.source != SOURCE or set(value) != {'basis', 'action', 'title', 'points', 'reason'}:
            raise ValueError('地图显示记录来源不一致。')
        rows.setdefault(record.reference.object_id, []).append(dict(value,
            version=record.reference.version, actor=record.actor_id, time=record.times.recorded_at))
    return rows


def suggestions(marks, saved):
    from .sandview import _mark_payload
    result = []
    for m in marks:
        if not m.geo_points or m.status.value != 'pending_review':
            continue
        geometry = points(m.geo_points)
        base = dict(id=m.id, title=m.title, points=geometry, kind=m.kind.value,
            geometry=m.geometry, time=m.detail('observed_at') or m.updated_at or m.created_at,
            source='、'.join(m.source_ref) or m.created_by or '来源待核对', demo=m.is_demo,
            layer=m.layer_group, basis_text=m.detail('basis') or m.note, unit=m.responsible_unit,
            source_label=m.detail('report_source') or '、'.join(m.source_ref) or m.created_by)
        basis = ar.digest(base)
        edits = saved.get(m.id, [])
        last = edits[-1] if edits else None
        # Use the same semantic rendering contract as manual/source annotations.
        payload = _mark_payload(m, 'commander', tip=False)
        render = {k: payload[k] for k in ('glyph', 'color', 'weight', 'dash', 'arrows',
            'route_type', 'route_label', 'gap_geometry', 'gaps', 'heading')}
        result.append(dict(base, basis=basis, render=render, original=dict(title=m.title, points=geometry),
            title=last['title'] if last else m.title, points=last['points'] if last else geometry,
            mode=last['action'] if last else 'preview', version=last['version'] if last else 0,
            changed=bool(last and last['basis'] != basis), history=edits))
    return result


def collect(state, token, report, marks, recon):
    from .formal_task_feedback import history as feedback_history, verified, LABELS
    from .situation_monitor import ChangeStatus
    eid = state.active_event_id
    by_id = {m.id:m for m in marks}
    fire = by_id.get('intel-fire-'+eid)
    items = []
    def add(identity, category, title, detail, *, mark=None, actor='', time='', severity='notice',
            state_label='', action='', source='', history_rows=None, task_area=False):
        pts = points(mark.geo_points) if mark and mark.geo_points else []
        row = dict(id=identity, category=category, title=title, detail=detail, points=pts,
            object_id=mark.id if mark else '', actor=actor, time=time, severity=severity,
            state=state_label, action=action, source=source, history=history_rows or [],
            location='关联任务区域' if pts and task_area else '来源登记位置' if pts else '位置待补',
            position_time=(mark.detail('observed_at') or mark.updated_at or mark.created_at) if mark else '')
        row['revision'] = ar.digest(row)
        items.append(row)
    for gap in report['gaps']:
        # Only explicit object references; general force/material gaps stay in the overview.
        mark = fire if gap['id'] in {'fire-report', 'environment'} else by_id.get(gap['id'].removeprefix('position-'))
        add('gap:'+gap['id'], 'pending', gap['text'], gap['text'], mark=mark,
            state_label='待补充', action='补充来源资料后自动重新核对', source=gap['id'])
    seen_changes = set()
    for change in state.workspace.situation_changes.values():
        if change.event_id != eid or change.status not in {ChangeStatus.NEW.value, ChangeStatus.ACKNOWLEDGED.value}:
            continue
        if change.fingerprint in seen_changes:
            continue
        seen_changes.add(change.fingerprint)
        linked = next((by_id[k] for k in change.affected_annotation_ids if k in by_id), None)
        add('change:'+change.fingerprint, 'urgent' if change.severity=='decision' else 'pending',
            change.kind_word, change.reason, mark=linked, time=change.observed_at or change.detected_at,
            severity=change.severity, state_label='待处理' if change.status=='new' else '已阅·待处理',
            action='由原职责人员在态势复核中处理', source='、'.join(change.source_refs))
    # Freshness cues are derived and do not pretend a new alarm has been dispatched.
    existing = {i['detail'] for i in items}
    for change in report['changes']:
        if change['text'] not in existing:
            add('cue:'+change['id'], 'pending', '资料需复核', change['text'],
                state_label='提醒', action=change['advice'], source=change['id'])
    for row in recon:
        if row['status'] != 'reported':
            continue
        add('recon:'+row['id'], 'reply', '侦查新回报', row['report'],
            mark=by_id.get(row['annotation_id']), actor=row['actor'], time=row['observed_at'],
            state_label='已回报·待核验', action='核对回报依据；有位置的回报已形成建议标绘', source=row['id'])
    runtime = getattr(state, 'assistant_runtime', None)
    if runtime:
        if runtime.journal.error:
            add('followup-source-error','pending','补问来源待核对',runtime.journal.error,
                state_label='来源不可用',action='核对补问留痕来源',source='assistant-followups')
        else:
            member=state.member_session(token)
            for question in runtime.questions(eid,recipient=None if eid in state.deputy_events(token) else member.phone):
                reply=question.get('reply')
                if not reply or question['status'] not in {'answered','escalated'}:
                    continue
                # A textual information reply never creates a task receipt or completion.
                oid=question['key'].removeprefix('task-feedback:') if question['key'].startswith('task-feedback:') else ''
                flow=state.workspace.command_flows.get(oid)
                linked=next((m for m in marks if oid and (m.related_order_id==oid or (flow and flow.route_id==m.id))),None)
                add('followup:'+question['id'],'pending' if reply['outcome']=='unavailable' else 'reply',
                    '资料暂无法取得' if reply['outcome']=='unavailable' else '资料补问新回复',reply['content'],
                    mark=linked,actor=reply['by'],time=reply['occurred_at'] or reply['reported_at'],task_area=True,
                    state_label='信息回复·待核对',action='核对来源；信息回复不替代任务回执或核验',source=reply['source'])
    from .formal_task_reads import current_publications
    tasks = current_publications(state.workspace)
    groups = {}
    for record, value in feedback_history(state.workspace, state.ledger):
        groups.setdefault(record.reference.object_id, []).append((record,value))
    for oid, rows in groups.items():
        if oid not in tasks:
            continue
        record, value = rows[-1]
        task = tasks[oid][2]
        flow = state.workspace.command_flows.get(oid)
        linked = next((m for m in marks if m.related_order_id==oid or (flow and flow.route_id==m.id)), None)
        closed = value['action']=='verify' and verified(state.workspace,state.ledger,oid)
        difficult = bool(flow and flow.control_state=='exec_abnormal')
        pending = value['action']=='completed' or (value['action']=='verify' and not closed)
        add('task:'+oid, 'done' if closed else 'urgent' if difficult else 'verify' if pending else 'reply',
            '已核验完成' if closed else '执行困难' if difficult else '已报完成·待核验' if pending else '队长新回报',
            value['basis'], mark=linked, actor=record.actor_id, time=record.times.recorded_at,
            severity='decision' if difficult else 'notice', task_area=True,
            state_label='已核验完成' if closed else '已报完成·待核验' if pending else LABELS[value['action']],
            action='核验负责人：'+task['verifier_id'] if pending else '执行负责人：'+task['recipient_id'],
            source=oid, history_rows=[dict(text=LABELS[v['action']]+'：'+v['basis'],actor=r.actor_id,time=r.times.recorded_at) for r,v in rows])
    items.extend(report.get('initial_scene',{}).get('items',[]))
    return dict(items=items, suggestions=suggestions(marks, history(state.workspace)),
        initial_scene=report.get('initial_scene',dict(enabled=False)),
        note='自动汇集已接入记录；位置为来源登记位置，非实时追踪。')


def write(state, token, values):
    from .sand_command import context
    from .intelligence import collect as intelligence_collect
    member,eid = context(state,token)
    if eid not in state.deputy_events(token):
        raise PermissionError('建议标绘由当前火情指定副指挥调整。')
    if values['event_id'] != eid or values['action'] not in {'up','down','edit'}:
        raise ValueError('标绘事件或动作无效。')
    if not values['expected_version'].isascii() or not values['expected_version'].isdigit():
        raise ValueError('标绘版本无效。')
    title=values['title'].strip(); reason=values['reason'].strip()
    if not title or len(title)>100 or not reason or len(reason)>600:
        raise ValueError('请填写名称及调整依据。')
    pts=points(json.loads(values['points']))
    payload=dict(basis=values['basis'],action=values['action'],title=title,points=pts,reason=reason)
    # Exact retries are idempotent, including after a later source revision.
    for old in state.workspace.object_revisions.values():
        if old.request_id==values['request_id']:
            return ar.append(state.workspace,kind=KIND,object_id=values['id'],source=SOURCE,
                actor=member.phone,values=payload,expected_version=int(values['expected_version']),request_id=values['request_id'])
    _,marks=intelligence_collect(state,eid)
    current=next((s for s in suggestions(marks,history(state.workspace)) if s['id']==values['id']),None)
    if not current or current['basis']!=values['basis']:
        raise ValueError('来源已变化，请重新读取后核对；原修改保留。')
    minimum={'point':1,'line':2,'polygon':3}.get(current['geometry'],1)
    if len(pts)<minimum or (current['geometry']=='point' and len(pts)!=1):
        raise ValueError('坐标点数量不符合对象类型。')
    if values['action']!='edit' and (pts!=current['points'] or title!=current['title']):
        raise ValueError('上图或下图不能同时改写几何，请使用修改。')
    result=ar.append(state.workspace,kind=KIND,object_id=values['id'],source=SOURCE,actor=member.phone,
        values=payload,expected_version=int(values['expected_version']),request_id=values['request_id'])
    state.persist_if_db();state._persist_active_task_snapshot()
    return result
