"""Versioned named inputs for M03; reported quantities are not professional rules."""
from copy import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math

from .authorization import Action, Role
from .event_authorization import event_grants
from .fire_events import ONBOARDABLE_STATUSES
from .material_inventory_reads import read_event, citation
from .models import OrderStatus
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time
from .task_sop import flow_issue

KIND='task_material_inputs'
SOURCE='任务物资输入报告：'
FIELDS=('material_relation_source','force_count','force_source','duration_hours','duration_source',
        'demand_quantity','projected_consumption','usage_source','usage_kind','reason')


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _text(value,*,optional=False):
    if not isinstance(value,str) or value!=value.strip() or not optional and not value:
        raise ValueError('任务物资关系、报告来源及依据须明确填写。')
    return value


def _quantity(value,*,integer=False):
    if value is None:
        return None
    if type(value) not in ({int} if integer else {int,float}):
        raise ValueError('实际人数须为非负整数；数量及时长须为有限非负数，未知留空。')
    try:
        valid=math.isfinite(value) and value>=0
    except OverflowError:
        valid=False
    if not valid:
        raise ValueError('人数、数量及时长须为有限非负数。')
    return value


def _validate_values(values):
    _text(values['material_relation_source'])
    for number,source,integer in (('force_count','force_source',True),
                                   ('duration_hours','duration_source',False)):
        amount=_quantity(values[number],integer=integer)
        _text(values[source],optional=amount is None)
    for name in ('demand_quantity','projected_consumption'):
        _quantity(values[name])
    _text(values['usage_source'],optional=all(values[x] is None for x in ('demand_quantity','projected_consumption')))
    if values['usage_kind'] not in {'simulated','unreviewed'}:
        raise ValueError('用量只能标模拟或未核定；文字依据不能冒充专业核定。')
    _text(values['reason'],optional=True)


def _object_id(order_id,item_id):
    return 'task-material-'+_digest([_text(order_id),_text(item_id)])


def _current_source(workspace,ledger,event_id,order_id,item_id):
    order=workspace.orders.get(order_id)
    if (workspace.event_id!=event_id or workspace.verify_audit_chain() or order is None
            or order.status not in {OrderStatus.CONFIRMED,OrderStatus.ACTIVE}
            or not order.confirmed_by or flow_issue(workspace,order_id,event_id)):
        raise ValueError('须有本事件已确认且审计一致的当前版任务；草案和线下补录不代替。')
    views=read_event(workspace,ledger,event_id,extra_items=(item_id,))
    return dict(order=asdict(order),flow=asdict(workspace.command_flows[order_id]),
                inventory=citation(views,ledger,event_id))


def _checked_records(workspace,event_id):
    if workspace.event_id!=event_id or workspace.verify_audit_chain():
        raise ValueError('任务物资输入与事件或审计链不一致。')
    records={k:r for k,r in workspace.object_revisions.items() if r.reference.object_kind==KIND}
    mirrors=[(i,e) for i,e in enumerate(workspace.events) if e.kind=='ObjectRevisionRecorded'
             and e.payload.get('revision',{}).get('reference',{}).get('object_kind')==KIND]
    if len(records)!=len(mirrors):
        raise ValueError('任务物资输入记录和镜像数量不一致。')
    versions={}; clocks={}; requests=set(); checked=[]
    for index,event in mirrors:
        record=records.get(event.payload.get('revision_id'))
        if record is None or event.payload!={'revision_id':record.id,'revision':asdict(record)}:
            raise ValueError('任务物资输入镜像不一致。')
        value=json.loads(record.content_json); ref=record.reference
        if (set(value)!=set(FIELDS)|{'order_id','item_id','task_version','source','source_digest'}
                or record.data_kind!='reported' or ref.event_id!=event_id
                or not record.source.startswith(SOURCE) or record.request_id in requests
                or ref.object_id!=_object_id(value['order_id'],value['item_id'])
                or ref.version!=versions.get(ref.object_id,0)+1):
            raise ValueError('任务物资输入归属、字段或版本不一致。')
        _text(record.source[len(SOURCE):]); _validate_values(value)
        if ref.version>1 and not value['reason']:
            raise ValueError('再次报告须说明更正或重评依据。')
        source=value['source']
        if (not isinstance(source,dict) or set(source)!={'order','flow','inventory'}
                or value['source_digest']!=_digest(source)
                or source['order'].get('id')!=value['order_id']
                or source['flow'].get('order_id')!=value['order_id']
                or source['flow'].get('event_id')!=event_id
                or type(value['task_version']) is not int or value['task_version']<1
                or source['flow'].get('content_version')!=value['task_version']
                or not isinstance(source['inventory'],str)
                or not source['inventory'].startswith(f'inventory:{event_id}:')):
            raise ValueError('任务物资输入冻结来源不一致。')
        prior_flows=[e.payload.get('flow') for e in workspace.events[:index]
                     if e.kind in {'CommandFlowOpened','CommandFlowChanged'}
                     and e.payload.get('order_id')==value['order_id'] and e.payload.get('event_id')==event_id]
        if not prior_flows or _json(prior_flows[-1])!=_json(source['flow']):
            raise ValueError('输入报告不对应报告时的任务版。')
        prefix=copy(workspace); prefix._events=list(workspace.events[:index])
        ids={e.payload.get('revision_id') for e in prefix.events if e.kind=='ObjectRevisionRecorded'}
        prefix.object_revisions={k:r for k,r in workspace.object_revisions.items() if k in ids}
        if not any(g.role==Role.DEPUTY and g.subject_id==record.actor_id and g.event_id==event_id
                   for g in event_grants(prefix)):
            raise ValueError('报告时没有本事件指定副总指挥职责。')
        recorded=explicit_time(record.times.recorded_at)
        if (recorded>explicit_time(event.recorded_at) or record.times.collected_at or record.times.received_at
                or ref.object_id in clocks and recorded < clocks[ref.object_id]
                or record.times.occurred_at and explicit_time(record.times.occurred_at)>recorded):
            raise ValueError('任务物资报告时间不一致。')
        versions[ref.object_id]=ref.version; clocks[ref.object_id]=recorded
        requests.add(record.request_id); checked.append(record)
    return tuple(checked)


def _records(workspace,event_id):
    try:
        return _checked_records(workspace,event_id)
    except (KeyError,TypeError,AttributeError,OverflowError) as error:
        raise ValueError('任务物资输入来源格式不完整，不能回退旧报告。') from error


@dataclass(frozen=True)
class InputReview:
    object_id:str
    version:int
    records:tuple
    source_digest:str|None
    state:str
    current:dict|None


def review(workspace,ledger,*,event_id,order_id,item_id):
    object_id=_object_id(order_id,item_id)
    records=tuple(r for r in _records(workspace,event_id) if r.reference.object_id==object_id)
    try:
        digest=_digest(_current_source(workspace,ledger,event_id,order_id,item_id))
    except (ValueError,KeyError,TypeError,AttributeError):
        digest=None
    value=json.loads(records[-1].content_json) if records else None
    current=value if value and value['source_digest']==digest else None
    state=('source_pending' if digest is None else 'missing' if value is None
           else 'current' if current else 'needs_reassessment')
    return InputReview(object_id,len(records),records,digest,state,current)


def record(workspace,ledger,registry,*,authorization,token,event_id,order_id,item_id,
           expected_version,expected_source_digest,evidence,occurred_at,request_id,**values):
    actor=authorization.require(token,Action.PUBLISH_TASK,event_id=event_id)
    event=registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('事件已不接受任务物资输入报告。')
    if set(values)!=set(FIELDS) or type(expected_version) is not int or expected_version<0:
        raise ValueError('任务物资输入字段或版本不正确。')
    _validate_values(values); _text(evidence); _text(request_id); _text(expected_source_digest)
    times=RecordTimes(occurred_at=occurred_at,recorded_at=datetime.now(timezone.utc).isoformat())
    if occurred_at and explicit_time(occurred_at)>explicit_time(times.recorded_at):
        raise ValueError('报告发生时间不能晚于记录时间。')
    records=_records(workspace,event_id); object_id=_object_id(order_id,item_id)
    prior=next((r for r in records if r.request_id==request_id),None)
    if prior:
        old=json.loads(prior.content_json)
        if (prior.reference.object_id==object_id and prior.reference.version==expected_version+1
                and prior.actor_id==actor and prior.source==SOURCE+evidence
                and prior.times.occurred_at==occurred_at and old['source_digest']==expected_source_digest
                and all(old[k]==v and type(old[k])==type(v) for k,v in values.items())):
            return prior
        raise ValueError('任务物资输入请求号已用于不同内容。')
    if any(r.request_id==request_id for r in workspace.object_revisions.values()):
        raise ValueError('请求号已用于其他记录。')
    current=review(workspace,ledger,event_id=event_id,order_id=order_id,item_id=item_id)
    if current.version!=expected_version or current.source_digest!=expected_source_digest:
        raise ValueError('任务、库存或报告版本已变化，请刷新后重评。')
    if expected_version and not values['reason']:
        raise ValueError('再次报告须填写更正或重评依据。')
    source=_current_source(workspace,ledger,event_id,order_id,item_id)
    content=dict(values,order_id=order_id,item_id=item_id,task_version=source['flow']['content_version'],
                 source=source,source_digest=_digest(source))
    revision=ObjectRevision(ObjectReference(event_id,KIND,object_id,expected_version+1),times,
                            SOURCE+evidence,'reported',actor,request_id,_json(content))
    return workspace._record_object_revision(revision,expected_version=expected_version)
