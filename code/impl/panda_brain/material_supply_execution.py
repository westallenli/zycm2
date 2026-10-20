"""Shared verified point stock and explicit published supply reservations.

All quantities are named reports. Company promises are a separate source,
never a point stock balance. No receipt enters field inventory in this module.
"""
from dataclasses import asdict
import json,math
from . import audited_records as ar
from . import material_supply_contacts as contacts
from . import material_supply_drafts as drafts

STOCK='verified_point_stock'
ORDER='formal_supply_order'
SOURCE='named-material-supply-execution'
KINDS={STOCK,ORDER}


def number(value,*,positive=False):
    result=contacts._quantity(value)
    if result is None or positive and result<=0: raise ValueError('数量须明确，零不代替未知；本次交出须为正数。')
    return result


def _key(point,item): return 'stock-'+ar.digest([point,item])


def _mirror_history(ledger,spaces):
    """Reconcile the entire shared journal, including every owner's event audit."""
    records=ledger.supply_records
    if any(key!=r.id or r.reference.object_kind not in KINDS for key,r in records.items()):
        raise ValueError('共享补给流水身份或种类不一致。')
    checked={}
    for eid,space in spaces.items():
        if space.event_id!=eid: raise ValueError('补给来源事件不一致。')
        for kind in KINDS:
            for index,record,value in ar.records(space,kind):
                if record.id not in records or records[record.id]!=record:
                    raise ValueError('补给事件镜像缺少共享流水，不回退旧余额。')
                if record.source!=SOURCE: raise ValueError('补给来源不正确。')
                prior=ar.prefix(space,index)
                if kind==ORDER and value.get('action')=='reserve':
                    from .task_release import _authority
                    _authority(prior,record.actor_id,'meeting','')
                else:
                    contacts.require_leader(prior,ledger,event_id=eid,actor=record.actor_id)
                if record.id in checked: raise ValueError('共享补给来源重复。')
                checked[record.id]=(record,value)
    if set(checked)!=set(records): raise ValueError('共享补给流水的事件来源缺失，不能发布或扣减。')
    if any(type(value.get('sequence')) is not int for _,value in checked.values()):
        raise ValueError('共享补给流水序号不正确。')
    ordered=sorted(checked.values(),key=lambda pair:pair[1]['sequence'])
    previous=''; requests=set(); states={}; stocks={}
    for seq,(record,value) in enumerate(ordered,1):
        if (value.get('sequence')!=seq or type(value.get('sequence')) is not int
                or value.get('previous')!=previous or record.request_id in requests):
            raise ValueError('共享补给流水链断裂或请求重复。')
        ar.text(value.get('basis'));previous=record.id;requests.add(record.request_id)
        if record.reference.object_kind==STOCK:
            if set(value)!={'sequence','previous','basis','point_id','item_id','quantity','snapshot','stock_version'}:
                raise ValueError('库存核实内容不完整。')
            point,item=value['point_id'],value['item_id']; key=(point,item)
            contacts._validate_snapshot(value['snapshot'],item,'point',point)
            number(value['quantity'])
            if record.reference.object_id!=_key(point,item) or type(value['stock_version']) is not int or value['stock_version']!=stocks.get(key,{}).get('stock_version',0)+1:
                raise ValueError('库存核实共享版本不连续。')
            held=math.fsum(v['quantity']-v['dispatched'] for v in states.values()
                if v['source_kind']=='point' and (v['source_id'],v['item_id'])==key and not v['cancelled'])
            if value['quantity']+1e-9<held: raise ValueError('核实数量小于已占用库存。')
            stocks[key]=value|dict(record_id=record.id)
        else:
            if set(value)!={'sequence','previous','basis','action','order_id','publication_ref','task_id','item_id','source_kind','source_id',
                            'quantity','dispatched_quantity','stock_ref','attachment_ref','contact_ref','source_snapshot'}:
                raise ValueError('补给执行内容不完整。')
            amount=number(value['quantity'],positive=True); sent=number(value['dispatched_quantity'])
            oid=record.reference.object_id
            if value['order_id']!=oid or value['action'] not in {'reserve','dispatch','cancel'}:
                raise ValueError('补给动作或对象不正确。')
            old=states.get(oid)
            if value['action']=='reserve':
                if old is not None or sent: raise ValueError('补给重复占用或未交出即记交出。')
                owner=spaces[record.reference.event_id]
                prior=ar.prefix(owner,next(i for i,e in enumerate(owner.events) if e.kind=='ObjectRevisionRecorded' and e.payload.get('revision_id')==record.id))
                publication=prior.object_revisions.get(value['publication_ref'])
                attachment=prior.object_revisions.get(value['attachment_ref']);contact=prior.object_revisions.get(value['contact_ref'])
                if publication is None or attachment is None or contact is None:
                    raise ValueError('补给发布、附件或联系来源断链。')
                from .task_release import RELEASE,releases
                releases(prior);drafts._records(prior,ledger,prior.event_id);contacts._records(prior,ledger,prior.event_id)
                if publication.reference.object_kind!=RELEASE or attachment.reference.object_kind!=drafts.KIND or contact.reference.object_kind!=contacts.KIND:
                    raise ValueError('补给正式来源种类不一致。')
                pub=json.loads(publication.content_json);attach=json.loads(attachment.content_json);lead=json.loads(contact.content_json)
                if (value['task_id'] not in {t['task_id'] for t in pub['tasks']} or attach['task_id']!=value['task_id']
                        or attach['item_id']!=value['item_id'] or attach['requested_quantity']!=amount or attach['contact_id']!=contact.id
                        or lead['source_kind']!=value['source_kind'] or lead['source_id']!=value['source_id']
                        or lead['source_snapshot']!=value['source_snapshot'] or lead['item_id']!=value['item_id']):
                    raise ValueError('补给数量和冻结来源不一致。')
                key=(value['source_id'],value['item_id'])
                if value['source_kind']=='point':
                    stock=stocks.get(key)
                    if stock is None or stock['record_id']!=value['stock_ref']: raise ValueError('发布缺少当时最新具名核实库存。')
                    held=math.fsum(v['quantity']-v['dispatched'] for v in states.values()
                        if v['source_kind']=='point' and (v['source_id'],v['item_id'])==key and not v['cancelled'])
                    if amount>stock['quantity']-held+1e-9: raise ValueError('共享核实库存不足，不能跨单重复承诺。')
                elif value['source_kind']=='company':
                    if value['stock_ref']: raise ValueError('公司承诺不是物资点库存。')
                    promised=contacts._quantity(lead['available_quantity'])
                    held=math.fsum(v['dispatched'] if v['cancelled'] else v['quantity'] for v in states.values() if v['contact_ref']==contact.id)
                    if promised is None or amount>promised-held+1e-9: raise ValueError('公司本次承诺数量不足或已被占用。')
                else: raise ValueError('未知供应来源。')
                states[oid]=value|dict(dispatched=0.,cancelled=False,record_id=record.id,event_id=record.reference.event_id)
            else:
                if old is None or old['cancelled']: raise ValueError('补给未发布或已取消。')
                stable={'order_id','publication_ref','task_id','item_id','source_kind','source_id','quantity','stock_ref','attachment_ref','contact_ref','source_snapshot'}
                if any(value[k]!=old[k] for k in stable): raise ValueError('执行不能替换已发布的来源和数量。')
                if value['action']=='cancel':
                    if sent: raise ValueError('取消不冒充实物交出。')
                    states[oid]=old|dict(cancelled=True,record_id=record.id)
                else:
                    number(sent,positive=True)
                    if old['dispatched']+sent>amount+1e-9: raise ValueError('累计交出超过发布量。')
                    if old['source_kind']=='point':
                        key=(old['source_id'],old['item_id']);stock=stocks[key]
                        if sent>stock['quantity']+1e-9: raise ValueError('实际交出超过核实存量。')
                        stocks[key]=stock|dict(quantity=stock['quantity']-sent)
                    states[oid]=old|dict(dispatched=old['dispatched']+sent,record_id=record.id)
    return ordered,stocks,states


def inspect(ledger,spaces): return _mirror_history(ledger,spaces)


def _append(space,ledger,*,kind,object_id,actor,values,expected_version,request_id):
    count=len(ledger.supply_records)
    previous=max(ledger.supply_records.values(),key=lambda r:json.loads(r.content_json)['sequence']).id if count else ''
    # Freeze global ordering. The object revision is still local to its event.
    record=ar.append(space,kind=kind,object_id=object_id,source=SOURCE,actor=actor,
                     values=values|dict(sequence=count+1,previous=previous),expected_version=expected_version,request_id=request_id)
    ledger.supply_records[record.id]=record
    return record


def _replay(space,ledger,actor,kind,object_id,values,expected_version,request_id):
    old=next((r for r in ledger.supply_records.values() if r.request_id==request_id),None)
    if old:
        data=json.loads(old.content_json)
        return ar.append(space,kind=kind,object_id=object_id,source=SOURCE,actor=actor,
                         values=values|{k:data[k] for k in ('sequence','previous')},expected_version=expected_version,request_id=request_id)
    return None


def verify_stock(space,ledger,spaces,actor,*,point_id,item_id,quantity,basis,expected_stock_version,expected_source_digest,request_id):
    contacts.require_leader(space,ledger,event_id=space.event_id,actor=actor)
    ordered,stocks,states=inspect(ledger,spaces)
    if type(expected_stock_version) is not int or expected_stock_version<0: raise ValueError('共享版本须为非负整数。')
    old=next((r for r in ledger.supply_records.values() if r.request_id==request_id),None)
    snapshot=json.loads(old.content_json).get('snapshot') if old else contacts._current_snapshot(ledger,item_id,'point',point_id)
    if contacts._digest(snapshot)!=expected_source_digest: raise ValueError('登记资料已变化，请重新核实。')
    values=dict(point_id=point_id,item_id=item_id,quantity=number(quantity),snapshot=snapshot,
                stock_version=expected_stock_version+1,basis=ar.text(basis))
    key=_key(point_id,item_id)
    local=sum(r.reference.object_id==key and r.reference.object_kind==STOCK for r in space.object_revisions.values())
    replay=_replay(space,ledger,actor,STOCK,key,values,old.reference.version-1 if old else local,request_id)
    if replay:return replay
    if stocks.get((point_id,item_id),{}).get('stock_version',0)!=expected_stock_version: raise ValueError('共享核实库存已变化，请刷新。')
    record=_append(space,ledger,kind=STOCK,object_id=key,actor=actor,values=values,expected_version=local,request_id=request_id)
    inspect(ledger,spaces)
    return record


def publication_plan(space,ledger,bundle):
    """Every latest task/item attachment, never collapse several items into one."""
    checked=drafts._records(space,ledger,space.event_id);latest={}
    for record in checked:
        value=json.loads(record.content_json)
        if value['decision_id']==bundle.decision_id:latest[record.reference.object_id]=(record,value)
    plans=[];issues=[]
    for record,value in latest.values():
        view=drafts.review(space,ledger,event_id=space.event_id,decision_id=bundle.decision_id,task_id=value['task_id'],item_id=value['item_id'])
        issues.extend(issue for issue in view.blockers if issue!='人工数量及联系报告未做独立核验；正式发布专业、安全及具名门禁保持')
        if value['requested_quantity'] is None or value['requested_quantity']<=0:issues.append('正式补给须有明确正数；零需求不能占用库存。')
        if view.current and value['contact_id']:
            contact=space.object_revisions[value['contact_id']];lead=json.loads(contact.content_json)
            plans.append(dict(attachment=record,contact=contact,content=value,source=lead))
    return tuple(plans),tuple(dict.fromkeys(issues))


def reserve(space,ledger,spaces,publication,actor):
    # Reservation is the deputy's publication action. The contact and stock
    # retain their actual logistics authors; do not impersonate those authors.
    ordered,stocks,states=inspect(ledger,spaces)
    from .task_release import _bundle
    data=json.loads(publication.content_json);bundle=_bundle(space,data['decision_id'],data['draft_version'])
    plans,issues=publication_plan(space,ledger,bundle)
    if issues:raise ValueError('；'.join(issues))
    for plan in plans:
        lead=plan['source'];attachment=plan['attachment'];content=plan['content'];contact=plan['contact']
        contacts.require_leader(space,ledger,event_id=space.event_id,actor=contact.actor_id)
        stock=stocks.get((lead['source_id'],lead['item_id'])) if lead['source_kind']=='point' else None
        if lead['source_kind']=='point' and stock is None:raise ValueError('物资点尚无具名核实共享库存。')
        if stock and stock['snapshot']!=contacts._current_snapshot(ledger,lead['item_id'],'point',lead['source_id']):
            raise ValueError('物资点登记资料已变化，须重新核实库存。')
        oid='supply-'+ar.digest([publication.id,attachment.id])
        values=dict(action='reserve',order_id=oid,publication_ref=publication.id,task_id=content['task_id'],item_id=content['item_id'],
            source_kind=lead['source_kind'],source_id=lead['source_id'],quantity=content['requested_quantity'],dispatched_quantity=0.,
            stock_ref=stock['record_id'] if stock else '',attachment_ref=attachment.id,contact_ref=contact.id,
            source_snapshot=lead['source_snapshot'],basis='具名后勤联系及核实来源，经副指挥长审定发布：'+data['reason'])
        _append(space,ledger,kind=ORDER,object_id=oid,actor=actor,values=values,expected_version=0,request_id='reserve-'+publication.id+'-'+attachment.id)
        ordered,stocks,states=inspect(ledger,spaces)
    return tuple(states)


def act(space,ledger,spaces,actor,*,registry,order_id,action,quantity,basis,expected_version,request_id):
    contacts.require_leader(space,ledger,event_id=space.event_id,actor=actor)
    ordered,stocks,states=inspect(ledger,spaces);old=states.get(order_id)
    if old is None or old['event_id']!=space.event_id:raise ValueError('本事件没有该既有补给单。')
    if action not in {'dispatch','cancel'}:raise ValueError('未知补给执行动作。')
    stable={'order_id','publication_ref','task_id','item_id','source_kind','source_id','quantity','stock_ref','attachment_ref','contact_ref','source_snapshot'}
    values={k:old[k] for k in stable}|dict(action=action,dispatched_quantity=number(quantity,positive=action=='dispatch'),basis=ar.text(basis))
    if action=='cancel' and values['dispatched_quantity']:raise ValueError('取消只释放未交出量，不记交出。')
    replay=_replay(space,ledger,actor,ORDER,order_id,values,expected_version,request_id)
    if replay:return replay
    if action=='dispatch':
        from .task_release import releases,_bundle,task_gate
        publication=next((v for r,v in releases(space) if r.id==old['publication_ref']),None)
        if publication is None:raise ValueError('正式发布来源缺失。')
        bundle=_bundle(space,publication['decision_id'],publication['draft_version'])
        task=next(t for t in bundle.tasks if t.id==old['task_id'])
        issues,refs,_=task_gate(space,bundle,task,registry=registry,ledger=ledger)
        frozen=next(t for t in publication['tasks'] if t['task_id']==task.id)
        if issues or set(refs)!=set(frozen['support_refs']):
            raise ValueError('本版专业及安全依据已变化，请重新核对发布。')
    record=_append(space,ledger,kind=ORDER,object_id=order_id,actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    inspect(ledger,spaces)
    return record
