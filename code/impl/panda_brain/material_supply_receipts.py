"""Named actual supply receipts with immutable dispatch cuts and team custody.

Global stock operations reconcile all owner events. Field inventory validates
its own receipt and dispatch audit mirrors; no external stock is inferred.
"""
import json,math
from . import audited_records as ar
from . import material_closeout_corrections as corrections
from . import material_supply_execution as execution
from . import material_supply_contacts as contacts

KIND='formal_supply_receipt'
SOURCE='named-logistics-actual-supply-receipt'
FIELDS={'order_id','publication_ref','dispatch_refs','arrival_ref','item_id','squad_id','location','received_quantity','damaged_quantity','basis'}


def order_history(ledger,event_id):
    """Ledger-only source contract; actual event authority is checked in history."""
    grouped={};states={}
    for key,record in ledger.supply_records.items():
        if record.reference.event_id!=event_id or record.reference.object_kind!=execution.ORDER:continue
        if key!=record.id or record.source!=execution.SOURCE or record.data_kind!='reported':raise ValueError('补给交出来源身份不一致。')
        grouped.setdefault(record.reference.object_id,[]).append(record)
    for oid,rows in grouped.items():
        rows.sort(key=lambda r:r.reference.version)
        if [r.reference.version for r in rows]!=list(range(1,len(rows)+1)):raise ValueError('补给交出来源版本断裂。')
        first=json.loads(rows[0].content_json);sent=0.;cancelled=False
        if first.get('action')!='reserve' or first.get('order_id')!=oid or execution.number(first['dispatched_quantity'])!=0:raise ValueError('补给交出缺少发布占用来源。')
        amount=execution.number(first['quantity'],positive=True);dispatch=[]
        stable={'order_id','publication_ref','task_id','item_id','source_kind','source_id','quantity','stock_ref','attachment_ref','contact_ref','source_snapshot'}
        contacts._validate_snapshot(first['source_snapshot'],first['item_id'],first['source_kind'],first['source_id'])
        for index,record in enumerate(rows):
            value=json.loads(record.content_json)
            if any(value.get(k)!=first[k] for k in stable) or value.get('action') not in {'reserve','dispatch','cancel'}:raise ValueError('补给交出冻结来源发生变化。')
            ar.text(value['basis'])
            if not index:continue
            if cancelled or value['action']=='reserve':raise ValueError('已取消或重复占用不能再交出。')
            if value['action']=='cancel':
                if execution.number(value['dispatched_quantity'])!=0:raise ValueError('取消余量不能冒充交出。')
                cancelled=True
            else:
                sent+=execution.number(value['dispatched_quantity'],positive=True);dispatch.append(record.id)
                if sent>amount+1e-9:raise ValueError('补给累计交出超过发布量。')
        states[oid]=dict(first,dispatched=sent,dispatch_refs=dispatch,cancelled=cancelled,records=tuple(rows))
    return states


def ledger_history(ledger,event_id,*,historical=False):
    from .material_transfers import _arrival
    orders=order_history(ledger,event_id);grouped={};checked=[]
    for key,record in ledger.supply_receipts.items():
        if record.reference.event_id!=event_id:continue
        value=json.loads(record.content_json)
        if (key!=record.id or record.reference.object_kind!=KIND or record.source!=SOURCE or record.data_kind!='reported'
                or set(value)-corrections.FIELDS!=FIELDS or value['order_id']!=record.reference.object_id):raise ValueError('补给实收身份或字段不一致。')
        ar.text(value['basis']);ar.text(value['location']);received=execution.number(value['received_quantity'],positive='corrects_id' not in value);damage=execution.number(value['damaged_quantity'])
        if damage>received:raise ValueError('实收损坏不能超过本批实收。')
        order=orders.get(value['order_id'])
        if order is None or (value['publication_ref'],value['item_id'])!=(order['publication_ref'],order['item_id']):raise ValueError('补给实收缺少正确正式交出来源。')
        arrival=_arrival(ledger,event_id,value['squad_id'])
        if arrival.arrival_id!=value['arrival_ref']:raise ValueError('补给接收队到场来源不一致。')
        refs=value['dispatch_refs']
        if (not isinstance(refs,list) or not refs or any(not isinstance(key,str) for key in refs)
                or len(set(refs))!=len(refs) or refs!=order['dispatch_refs'][:len(refs)]):raise ValueError('实收冻结交出来源断裂。')
        grouped.setdefault(value['order_id'],[]).append((record,value))
    for oid,rows in grouped.items():
        rows.sort(key=lambda row:row[0].reference.version);total=0.
        if [r.reference.version for r,v in rows]!=list(range(1,len(rows)+1)):raise ValueError('补给分批实收版本断裂。')
        prefix=[]
        for record,value in rows:
            prefix.append(record)
            current=corrections.effective(prefix)
            total=math.fsum(json.loads(r.content_json)['received_quantity'] for r in current)
            dispatched=math.fsum(json.loads(ledger.supply_records[key].content_json)['dispatched_quantity'] for key in value['dispatch_refs'])
            if total>dispatched+1e-9:raise ValueError('累计实收超过已实际交出数量。')
            checked.append((record,value))
    if historical:return tuple(checked)
    result=[]
    for oid,rows in grouped.items():
        result.extend((r,json.loads(r.content_json)) for r in corrections.effective([r for r,v in rows]))
    return tuple(result)


from .read_scope import validated_read


@validated_read
def history(space,ledger,*,historical=False):
    """Both journal directions, historical authors and complete prior dispatch cuts."""
    from .task_release import releases,_authority
    from . import material_supply_drafts as drafts
    eid=space.event_id;receipts=ledger_history(ledger,eid,historical=True)
    mirrored=ar.records(space,KIND)
    if {r.id for i,r,v in mirrored}!={r.id for r,v in receipts}:raise ValueError('补给实收账与事件镜像不一致。')
    orders=order_history(ledger,eid);local=ar.records(space,execution.ORDER)
    if {r.id for i,r,v in local}!={r.id for order in orders.values() for r in order['records']}:raise ValueError('补给交出账与事件镜像不一致。')
    positions={r.id:i for i,r,v in local};receipt_positions={r.id:i for i,r,v in mirrored}
    for index,record,value in local:
        if ledger.supply_records.get(record.id)!=record:raise ValueError('补给交出审计镜像不一致。')
        prior=ar.prefix(space,index)
        if value['action']=='reserve':
            _authority(prior,record.actor_id,'meeting','');publications=releases(prior)
            publication=next((v for r,v in publications if r.id==value['publication_ref']),None)
            attachment=prior.object_revisions.get(value['attachment_ref']);contact=prior.object_revisions.get(value['contact_ref'])
            if (publication is None or attachment is None or contact is None or attachment.reference.object_kind!=drafts.KIND
                    or contact.reference.object_kind!=contacts.KIND or value['task_id'] not in {t['task_id'] for t in publication['tasks']}):raise ValueError('补给正式发布或附件来源断裂。')
            attach=json.loads(attachment.content_json);lead=json.loads(contact.content_json)
            if (attach['task_id'],attach['item_id'],attach['requested_quantity'],attach['contact_id'])!=(value['task_id'],value['item_id'],value['quantity'],contact.id):raise ValueError('补给附件数量来源不一致。')
            if lead['source_snapshot']!=value['source_snapshot'] or lead['source_id']!=value['source_id'] or lead['source_kind']!=value['source_kind']:raise ValueError('补给联系来源不一致。')
            drafts._records(prior,ledger,eid);contacts._records(prior,ledger,eid)
        else:contacts.require_leader(prior,ledger,event_id=eid,actor=record.actor_id)
    for index,record,value in mirrored:
        if ledger.supply_receipts.get(record.id)!=record:raise ValueError('补给实收审计镜像不一致。')
        contacts.require_leader(ar.prefix(space,index),ledger,event_id=eid,actor=record.actor_id)
        if 'corrects_id' in value:
            previous=ledger.supply_receipts.get(value['corrects_id'])
            if previous is None or previous.reference.object_id!=record.reference.object_id:
                raise ValueError('补给更正须引用本笔原批次。')
            corrections.supply_protect(ar.prefix(space,index),ledger,previous)
        expected=[r.id for r in orders[value['order_id']]['records'] if json.loads(r.content_json)['action']=='dispatch' and positions[r.id]<index]
        if value['dispatch_refs']!=expected:raise ValueError('实收未冻结全部既有实际交出来源。')
        prior_batches=[r.reference.version for r,v in receipts if r.reference.object_id==value['order_id'] and receipt_positions[r.id]<index]
        if sorted(prior_batches)!=list(range(1,record.reference.version)):raise ValueError('补给分批实收审计顺序不一致。')
        from .material_withdrawals import withdrawal_record
        departure=withdrawal_record(ledger,event_id=eid,squad_id=value['squad_id'])
        if departure:
            entry=next((i for i,e in enumerate(space.events) if e.payload.get('withdrawal_record_id')==departure.id),None)
            if entry is None or entry<=index:raise ValueError('接收队已撤离或撤离来源缺失，不能计入现场。')
    if historical:return tuple((r,v) for i,r,v in mirrored)
    active={r.id for r,v in ledger_history(ledger,eid)}
    return tuple((r,v) for i,r,v in mirrored if r.id in active)


def amounts(ledger,event_id,squad_id,item_id):
    rows=[(r,v) for r,v in ledger_history(ledger,event_id) if (v['squad_id'],v['item_id'])==(squad_id,item_id)]
    good=math.fsum(v['received_quantity']-v['damaged_quantity'] for r,v in rows)
    damage=math.fsum(v['damaged_quantity'] for r,v in rows)
    return good,damage,tuple(sorted(r.id for r,v in rows))


def record(space,ledger,spaces,actor,*,order_id,squad_id,location,quantity,damaged_quantity,basis,expected_version,request_id,corrects_id='',correction_reason=''):
    contacts.require_leader(space,ledger,event_id=space.event_id,actor=actor)
    execution.inspect(ledger,spaces);history(space,ledger)
    order=order_history(ledger,space.event_id).get(order_id)
    if order is None:raise ValueError('本事件没有该正式补给单。')
    from .material_transfers import _arrival
    from .material_withdrawals import withdrawal_record
    arrival=_arrival(ledger,space.event_id,squad_id)
    old=next((r for r in space.object_revisions.values() if r.request_id==request_id),None)
    refs=json.loads(old.content_json).get('dispatch_refs') if old else order['dispatch_refs']
    values=dict(order_id=order_id,publication_ref=order['publication_ref'],dispatch_refs=refs,arrival_ref=arrival.arrival_id,item_id=order['item_id'],
        squad_id=squad_id,location=ar.text(location),received_quantity=execution.number(quantity,positive=not corrects_id),damaged_quantity=execution.number(damaged_quantity),basis=ar.text(basis))
    if corrects_id:
        values.update(corrects_id=ar.text(corrects_id),correction_reason=ar.text(correction_reason))
    elif correction_reason:raise ValueError('补给更正理由缺少原批次。')
    if old:return ar.append(space,kind=KIND,object_id=order_id,source=SOURCE,actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    if corrects_id:
        previous=next((r for r,v in history(space,ledger) if r.id==corrects_id and r.reference.object_id==order_id),None)
        if previous is None:raise ValueError('须引用本笔当前有效实收批次。')
        corrections.supply_protect(space,ledger,previous)
    if withdrawal_record(ledger,event_id=space.event_id,squad_id=squad_id):raise ValueError('接收队已撤离，不能新增现场实收。')
    result=ar.append(space,kind=KIND,object_id=order_id,source=SOURCE,actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    ledger.supply_receipts[result.id]=result;history(space,ledger)
    from .material_inventory_projection import project
    project(space,ledger,event_id=space.event_id,item_id=order['item_id'])
    return result
