"""Event-independent, named daily maintenance; legacy event journals stay intact."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


def event_spaces(state):
    """Missing closed/retired snapshots never replace required journal mirrors."""
    from .panel import PanelActionError
    spaces={}; unavailable=[]
    for eid,event in state.registry.events.items():
        if event.status=='registered': continue
        if event.status=='closed' and (not event.snapshot_path or not Path(event.snapshot_path).is_file()):
            unavailable.append(eid);continue
        try: spaces[eid]=state._appointment_space(eid)[0]
        except (PanelActionError,OSError,ValueError) as exc:
            raise ValueError('物资来源事件空间待核对：'+eid) from exc
    for eid,event in state.registry.archived_test_events.items():
        if not event.snapshot_path or not Path(event.snapshot_path).is_file():
            unavailable.append(eid); continue
        try: spaces[eid]=state._appointment_space(eid,history_only=True)[0]
        except (PanelActionError,OSError,ValueError) as exc:
            raise ValueError('历史物资来源事件空间待核对：'+eid) from exc
    return spaces,unavailable


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def journal(ledger):
    previous = ''; requests = set(); anchor = 0; metadata = {}
    for number, entry in enumerate(ledger.daily_resource_records, 1):
        value = {k:v for k,v in entry.items() if k != 'hash'}
        if (entry.get('hash') != digest(value) or entry.get('sequence') != number
                or entry.get('previous') != previous or entry.get('request_id') in requests
                or type(entry.get('catalog_sequence')) is not int
                or not anchor <= entry['catalog_sequence'] <= len(ledger.catalog_records)
                or entry.get('operation') not in {'catalog','people','squad','metadata','import','directory_import','status'}
                or not entry.get('actor') or not entry.get('basis')
                or entry.get('authority') not in {'duty','material-maintainer'}):
            raise ValueError('日常维护审计链或资料来源异常，请保留原记录核对。')
        if datetime.fromisoformat(entry['recorded_at']).tzinfo is None:
            raise ValueError('日常维护时间须有时区。')
        for change in entry['changes']:
            if change['table'].startswith('metadata:'):
                kind=change['table'].split(':',1)[1]
                target=metadata.setdefault(kind,{})
                if target.get(change['key'])!=change['before']: raise ValueError('位置或预测参数版本不衔接。')
                target[change['key']]=change['after']
        requests.add(entry['request_id']); previous = entry['hash']; anchor = entry['catalog_sequence']
    if metadata != ledger.resource_metadata: raise ValueError('位置或预测参数缺少具名维护来源。')
    return tuple(ledger.daily_resource_records)


def catalog_changes(ledger, anchor, latest):
    """Interleave daily edits with original catalog versions without rewriting either."""
    from .material_catalog import TABLES
    for entry in journal(ledger):
        if entry['catalog_sequence'] != anchor: continue
        for change in entry['changes']:
            if change['table'] not in TABLES: continue
            key = (change['table'], change['key'])
            if key in latest and latest[key] != change['before']:
                raise ValueError('日常维护与原资料版本不衔接。')
            latest[key] = change['after']


def authority(state, token, operation):
    actor = state.authorization_subject(token)
    try:
        state.duty_subject(token)
        return actor, 'duty'
    except PermissionError:
        if operation in {'people','squad'}:
            raise PermissionError('独立人员库维护由值班员办理；副指挥原事件人员安排入口仍保留。')
        from .material_maintenance_authority import require
        require(state.ledger,event_spaces(state)[0],state.registry,actor)
        return actor, 'material-maintainer'


def permissions(state, token):
    result = {'people':False, 'materials':False}
    for key, op in [('people','people'),('materials','catalog')]:
        try: authority(state, token, op); result[key] = True
        except PermissionError: pass
    return result


def source_digest(state):
    from .material_catalog import snapshot
    return digest(dict(catalog=snapshot(state.ledger),people={k:asdict(v) for k,v in state.registry.library.items()},
        squads={k:asdict(v) for k,v in state.ledger.squads.items()},metadata=state.ledger.resource_metadata,
        legacy_versions=len(state.ledger.catalog_records)))


def _people(state, registry, text):
    from .workbench import parse_roster_csv
    lines = text.splitlines()
    if lines and lines[0].lstrip('\ufeff').split(',')[:2] == ['姓名','手机号']:
        text = '\n'.join(lines[1:])
    people, errors = parse_roster_csv(text)
    if errors or not people: raise ValueError('人员资料未保存：'+'；'.join(errors or ['空表']))
    if len({p.phone for p in people}) != len(people): raise ValueError('人员资料存在重复手机号。')
    # Once called out, the personnel/leader identity belongs to the event snapshot.
    for eid, event in registry.events.items():
        if event.status not in {'active','upgraded'}: continue
        from .callouts import callout_state
        frozen = callout_state(state._appointment_space(eid)[0], eid)
        for call in frozen.values():
            for person in people:
                old = registry.library.get(person.phone)
                if old and (person.phone == call.leader_phone or person.phone in getattr(call,'member_phones',())) and person != old:
                    raise ValueError('活动火情已使用该人员资料，请保留现场身份并在事件结束后更新。')
    before = {p.phone: asdict(registry.library[p.phone]) if p.phone in registry.library else None for p in people}
    registry.import_person_library(people)
    return [dict(table='people', key=p.phone, before=before[p.phone], after=asdict(p)) for p in people]


def _metadata(ledger, details):
    if set(details) != {'kind','identifier','values'}: raise ValueError('定位或预测参数字段不正确。')
    kind, identifier, values = details['kind'], details['identifier'], details['values']
    sources = {'points': ledger.points, 'companies': ledger.companies, 'items': ledger.items}
    if kind not in sources or identifier not in sources[kind]: raise ValueError('资料对象不存在。')
    if not isinstance(values, dict): raise ValueError('参数须为对象。')
    if kind in {'points','companies'}:
        if set(values) != {'latitude','longitude','lead_hours','basis'}: raise ValueError('位置与运输参数字段不正确。')
        for key, bound in [('latitude',90),('longitude',180)]:
            v = values[key]
            if v is not None and (type(v) not in {int,float} or not math.isfinite(v) or abs(v)>bound):
                raise ValueError('经纬度须有效，未知留空。')
        if (values['latitude'] is None) != (values['longitude'] is None): raise ValueError('经纬度须成对填写。')
        v = values['lead_hours']
        if v is not None and (type(v) not in {int,float} or not math.isfinite(v) or v<0): raise ValueError('运输提前量须为非负小时，未知留空。')
    else:
        if set(values) != {'scope','rate_per_person_hour','basis'} or values['scope'] not in {'standard','public','extra'}:
            raise ValueError('请选择队伍标配、公共保障或额外需求。')
        v = values['rate_per_person_hour']
        if v is not None and (type(v) not in {int,float} or not math.isfinite(v) or v<0): raise ValueError('用量参数须为非负有限数，未知留空。')
    if not isinstance(values['basis'],str) or not values['basis'].strip(): raise ValueError('请说明定位、运输或估算参数的来源；演示资料须标模拟。')
    target = ledger.resource_metadata.setdefault(kind,{})
    before = deepcopy(target.get(identifier)); target[identifier] = deepcopy(values)
    return [dict(table='metadata:'+kind,key=identifier,before=before,after=deepcopy(values))]


def record(state, token, *, operation, details, basis, expected_version, request_id, expected_source_digest=None):
    from . import material_catalog as catalog
    actor, role = authority(state, token, operation)
    rows = journal(state.ledger)
    if not isinstance(basis,str) or not basis.strip() or not isinstance(request_id,str) or not request_id.strip():
        raise ValueError('须明确修改依据及请求号。')
    if not isinstance(details,dict) or operation not in {'catalog','people','squad','metadata','import','directory_import','status'}:
        raise ValueError('日常维护动作或字段不受支持。')
    prior = next((r for r in rows if r['request_id']==request_id),None)
    if prior:
        if (prior['actor'],prior['operation'],prior['details'],prior['basis'],prior['sequence']) != (actor,operation,details,basis,expected_version+1):
            raise ValueError('日常维护请求号已用于不同内容。')
        return prior
    if type(expected_version) is not int or expected_version != len(rows): raise ValueError('日常资料版本已变化，请刷新核对。')
    if expected_source_digest is not None and expected_source_digest != source_digest(state):
        raise ValueError('日常库或原维护入口的资料已变化，请保留输入并重新预览。')
    spaces, _ = event_spaces(state)
    catalog.inspect(state.ledger, spaces)
    ledger, registry = deepcopy(state.ledger), deepcopy(state.registry)
    before = catalog.snapshot(ledger); changes = []
    if operation == 'directory_import':
        from .resource_directory import import_data
        if set(details)!={'data_type','content','content_sha256'} or hashlib.sha256(details['content'].encode()).hexdigest()!=details['content_sha256']:
            raise ValueError('导入资料摘要不一致。')
        if details['data_type'] in {'person_directory','squad_directory'}: authority(state,token,'people')
        changes=import_data(state,ledger,registry,details['data_type'],details['content'],actor)
    elif operation == 'status':
        from .resource_directory import set_status
        if details.get('kind') in {'people','squads'}: authority(state,token,'people')
        changes=set_status(state,ledger,registry,details,actor)
    elif operation == 'catalog':
        if set(details) != {'data_type','row'}: raise ValueError('资料字段不正确。')
        catalog._apply(ledger, details['data_type'], details['row'], actor)
    elif operation == 'import':
        if set(details) != {'data_type','content','content_sha256'} or hashlib.sha256(details['content'].encode()).hexdigest()!=details['content_sha256']:
            raise ValueError('导入资料摘要不一致。')
        if details['data_type'] == 'people':
            authority(state,token,'people'); changes = _people(state,registry,details['content'])
        else:
            _, mapped, errors = catalog.preview(details['data_type'],details['content'],ledger,actor)
            if errors: raise ValueError('导入未保存：'+'；'.join(errors))
            for row in mapped: catalog._apply(ledger,details['data_type'],row,actor)
    elif operation == 'people':
        if set(details) != {'csv_text'}: raise ValueError('人员导入字段不正确。')
        changes = _people(state,registry,details['csv_text'])
    elif operation == 'squad':
        if set(details) != {'name','squad_id','member_phones','leader_phone','headcount_planned','contact'}: raise ValueError('队伍资料字段不正确。')
        sid = details['squad_id']; old = ledger.squads.get(sid)
        if sid and not old: raise ValueError('队伍不存在。')
        if old:
            from .callouts import callout_state
            for eid,space in spaces.items():
                if state.registry.events.get(eid) and state.registry.events[eid].status in {'active','upgraded'} and sid in callout_state(space,eid):
                    raise ValueError('活动火情已使用该队伍编制，请保留现场记录。')
        value = dict(details); value.pop('squad_id')
        sq = ledger.add_squad(**value, position_slots=old.position_slots if old else (), squad_id=sid or None, library=registry.library)
        changes=[dict(table='squads',key=sq.squad_id,before=asdict(old) if old else None,after=asdict(sq))]
    else: changes = _metadata(ledger,details)
    after = catalog.snapshot(ledger)
    for table in catalog.TABLES:
        for key in sorted(set(before[table])|set(after[table])):
            if before[table].get(key)!=after[table].get(key):
                changes.append(dict(table=table,key=key,before=before[table].get(key),after=after[table].get(key)))
    if not changes or all(c['before']==c['after'] for c in changes): raise ValueError('资料内容没有变化。')
    value=dict(sequence=len(rows)+1,previous=rows[-1]['hash'] if rows else '',actor=actor,authority=role,
               operation=operation,details=deepcopy(details),basis=basis,request_id=request_id,
               recorded_at=datetime.now(timezone.utc).isoformat(),catalog_sequence=len(ledger.catalog_records),changes=changes)
    value['hash']=digest(value); ledger.daily_resource_records.append(value)
    journal(ledger); catalog.inspect(ledger,spaces)
    ledger.save(state.materials_path)
    if registry.library != state.registry.library: registry.save(state.registry_path)
    state.ledger, state.registry = ledger, registry
    return value


def standard_line(ledger, squad_id, item_id):
    return any(l.squad_id==squad_id and l.item_id==item_id for l in ledger.carry_lines.values())


def scope(ledger,item_id):
    configured=ledger.resource_metadata.get('items',{}).get(item_id,{}).get('scope')
    return configured or ('standard' if any(l.item_id==item_id for l in ledger.carry_lines.values()) else 'public')
