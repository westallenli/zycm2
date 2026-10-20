"""Explicitly scoped synthetic source bundle, projected as unverified suggestions.

The old system's example catalogue is reference material only. This reader never
runs that builder, restores an old database, publishes, or assigns a real team.
"""
from functools import lru_cache
from pathlib import Path
import copy
import hashlib
import json
from .map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus, AnnotationSource
from . import audited_records as ar

PATH = Path(__file__).resolve().parents[1] / 'config' / 'initial-map-demo.json'
NOTICE = '初期态势演示资料 · 非现场实测 · 待人工核对'


@lru_cache(maxsize=1)
def bundle():
    raw=PATH.read_bytes()
    if len(raw)>100000:
        raise ValueError('初期演示资料过大。')
    data=json.loads(raw.decode('utf-8'))
    if data.get('schema')!='initial-map-demo-v1' or data.get('data_kind')!='simulated':
        raise ValueError('初期演示资料格式不一致。')
    return data


def enabled(state,eid,data):
    if state.active_event_id!=eid or state.workspace.event_id!=eid or state.workspace.verify_audit_chain():
        raise ValueError('初期资料与当前事件不一致。')
    record=state.registry.events[eid]
    source=next((e.payload for e in reversed(state.workspace.events)
        if e.kind=='ExternalFireEventReceived' and e.payload.get('event_id')==eid),None)
    return bool(eid==data['event_id'] and record.name==data['event_name']
        and record.status in {'active','upgraded'} and [record.lat,record.lon]==data['anchor']
        and source and source.get('data_kind')=='simulated'
        and source.get('source_system')==data['source_system'])


def collect(state,eid):
    data=getattr(state,'fire_drill_scene',None) or bundle()
    if not enabled(state,eid,data):
        return dict(enabled=False,marks=[],sources=[],items=[],notice='',fingerprint='')
    from .map_brief import points
    marks=[];sources=[];by_code={}
    stamp=data['recorded_at']
    for row in data['records']:
        pts=points(row['points'])
        if row['id'] in by_code:
            raise ValueError('初期演示资料编号重复。')
        original=copy.deepcopy(row)
        source='demo-initial:'+data['version']+':'+row['id']+':'+ar.digest(original)
        identity='initial-demo-'+eid+'-'+row['id']
        details={str(k):str(v) for k,v in row['details'].items()}
        details.update(source_ref=source,report_source=row['source'],observed_at=row['observed_at'],basis=row['basis'])
        team_id=data.get('team_mapping',{}).get(row['unit'])
        team=state.ledger.squads.get(team_id) if team_id else None
        unit=team.name if team else row['unit']
        if team:details.update(original_team_alias=row['unit'],current_squad_id=team_id,current_team=unit)
        mark=MapAnnotation(id=identity,event_id=eid,kind=AnnotationKind(row['kind']),points=(),
            geo_points=tuple(tuple(p) for p in pts),title=row['title'],status=AnnotationStatus.PENDING_REVIEW,
            source=AnnotationSource.AI_SUGGESTION,source_ref=(source,),created_by='结构化演示资料自动整理',
            responsible_unit=unit,created_at=stamp,updated_at=stamp,details=details,
            is_demo=True,demo_notice=NOTICE,note=row['basis']+'；'+NOTICE)
        marks.append(mark);by_code[row['id']]=mark
        sources.append(dict(id=source,title=row['title'],text='【演示】'+row['basis']+'；来源 '+row['source']
            +'；观测 '+row['observed_at']+'；登记 '+stamp+'；'+NOTICE,original=original))
    items=[]
    for row in data['messages']:
        mark=by_code.get(row['object'])
        if mark is None:
            raise ValueError('演示回报缺少明确对象来源。')
        value=dict(id='initial-message:'+row['id'],category=row['category'],title=row['title'],
            detail=row['text'],object_id=mark.id,points=[list(p) for p in mark.geo_points],
            actor=row['actor'],time=row['time'],position_time=mark.detail('observed_at'),
            location='演示资料登记位置',state=row['state'],severity=row['severity'],action=row['action'],
            source=mark.source_ref[0],history=[],demo=True)
        value['revision']=ar.digest(value);items.append(value)
    return dict(enabled=True,marks=marks,sources=sources,items=items,notice=NOTICE,
        fingerprint=hashlib.sha256(ar.dump(data).encode()).hexdigest())
