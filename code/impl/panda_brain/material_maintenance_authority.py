"""Explicit cross-event grants from the event's named materials group leader."""
import json
from . import audited_records as ar
from .material_supply_contacts import require_leader
from .authorization import RoleGrant,Role,Action,AuthorizationPolicy

KIND='material_maintenance_grant'
SOURCE='named-logistics-long-term-maintenance-authority'
SCOPE='long_term_material_catalog'


def journal(ledger):
    records=ledger.maintenance_records;ordered=[]
    for key,record in records.items():
        value=json.loads(record.content_json)
        if (key!=record.id or record.reference.object_kind!=KIND or record.source!=SOURCE or record.data_kind!='reported'
                or set(value)!={'subject_id','scope','active','basis','reason','grant_version','sequence','previous'}
                or value['scope']!=SCOPE or type(value['active']) is not bool or record.reference.object_id!='maint-'+ar.digest(value['subject_id'])
                or type(value['sequence']) is not int or type(value['grant_version']) is not int):
            raise ValueError('长期物资维护授权来源不一致。')
        for field in ('subject_id','basis','reason'):ar.text(value[field])
        ordered.append((record,value))
    ordered.sort(key=lambda pair:pair[1]['sequence']);previous='';versions={};requests=set()
    for sequence,(record,value) in enumerate(ordered,1):
        subject=value['subject_id']
        if value['sequence']!=sequence or value['previous']!=previous or value['grant_version']!=versions.get(subject,0)+1 or record.request_id in requests:
            raise ValueError('跨事件维护授权流水断链。')
        versions[subject]=value['grant_version'];previous=record.id;requests.add(record.request_id)
    return tuple(ordered)


def inspect(ledger,spaces):
    ordered=journal(ledger);mirrors={}
    for eid,space in spaces.items():
        if space.event_id!=eid:raise ValueError('维护授权事件归属不一致。')
        for index,record,value in ar.records(space,KIND):
            if ledger.maintenance_records.get(record.id)!=record:raise ValueError('维护授权事件镜像缺少长期流水。')
            require_leader(ar.prefix(space,index),ledger,event_id=eid,actor=record.actor_id)
            if record.id in mirrors:raise ValueError('维护授权来源重复。')
            mirrors[record.id]=record
    if set(mirrors)!=set(ledger.maintenance_records):raise ValueError('长期维护授权缺少具名事件来源，不回退旧授权。')
    return ordered


def require(ledger,spaces,registry,actor):
    rows=inspect(ledger,spaces)
    latest={v['subject_id']:(r,v) for r,v in rows}
    if actor not in registry.library or actor not in latest or not latest[actor][1]['active']:
        raise PermissionError('需要物资供应组长明确授予且尚未撤销的具名长期物资维护权。')
    AuthorizationPolicy((RoleGrant(actor,Role.LOGISTICS_ADMIN),)).require(actor,Action.MAINTAIN_MATERIALS)
    return latest[actor][0]


def at_revision(rows,spaces,record):
    """Latest subject grant at the historical write, using audit order for ties."""
    from .record_contracts import explicit_time
    instant=explicit_time(record.times.recorded_at); prior=[]
    owner=spaces[record.reference.event_id]
    indexes={e.payload.get('revision_id'):i for i,e in enumerate(owner.events) if e.kind=='ObjectRevisionRecorded'}
    if record.id not in indexes: raise ValueError('资料写入没有事件审计位置。')
    for grant,value in rows:
        if value['subject_id']!=record.actor_id: continue
        when=explicit_time(grant.times.recorded_at)
        if when<instant or when==instant and grant.reference.event_id==record.reference.event_id and indexes.get(grant.id,len(owner.events))<indexes[record.id]:
            prior.append((grant,value))
        elif when==instant and grant.reference.event_id!=record.reference.event_id:
            raise ValueError('跨事件同刻授权顺序无法核定，拒绝复用旧权。')
    if not prior: raise ValueError('资料写入当时尚无有效具名授权。')
    grant,value=max(prior,key=lambda pair:pair[1]['sequence'])
    if not value['active']: raise ValueError('资料写入当时最新授权已撤销。')
    return grant


def record(space,ledger,spaces,registry,actor,*,subject_id,active,basis,reason,expected_version,request_id):
    require_leader(space,ledger,event_id=space.event_id,actor=actor)
    if subject_id not in registry.library:raise ValueError('被授权人必须是可信长期人员库中的具名人员。')
    if type(active) is not bool or type(expected_version) is not int or expected_version<0:raise ValueError('授撤状态与版本须明确。')
    ar.text(basis);ar.text(reason)
    rows=inspect(ledger,spaces);oid='maint-'+ar.digest(subject_id)
    old=next((r for r in ledger.maintenance_records.values() if r.request_id==request_id),None)
    if old:
        value=json.loads(old.content_json)
        if (old.actor_id!=actor or old.reference.event_id!=space.event_id or value['subject_id']!=subject_id
                or value['active']!=active or value['basis']!=basis or value['reason']!=reason or value['grant_version']!=expected_version+1):
            raise ValueError('授权请求号已用于不同内容。')
        return old
    count=sum(v['subject_id']==subject_id for r,v in rows)
    if count!=expected_version:raise ValueError('长期维护授权版本已变化。')
    latest=next((v for r,v in reversed(rows) if v['subject_id']==subject_id),None)
    if latest is not None and latest['active']==active or latest is None and not active:raise ValueError('授权状态未变化或尚无授权可撤销。')
    local=sum(r.reference.object_id==oid for _,r,v in ar.records(space,KIND))
    value=dict(subject_id=subject_id,scope=SCOPE,active=active,basis=basis,reason=reason,grant_version=count+1,
        sequence=len(rows)+1,previous=rows[-1][0].id if rows else '')
    revision=ar.append(space,kind=KIND,object_id=oid,source=SOURCE,actor=actor,values=value,expected_version=local,request_id=request_id)
    ledger.maintenance_records[revision.id]=revision
    inspect(ledger,spaces|{space.event_id:space})
    return revision
