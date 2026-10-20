"""Current-publication named execution reports and explicit verified closure."""
from dataclasses import asdict,replace
from datetime import datetime,timezone
import json
from . import audited_records as ar
from .formal_task_receipts import published
from .models import OrderStatus
from .task_recipients import recipient_route
from .task_sop import flow_issue

KIND='formal_task_feedback'
SOURCE='named-current-task-feedback'
LABELS=dict(progress='报告执行进展',difficulty='报告执行困难',resolved='报告困难已处理',partial='报告部分完成',completed='报告全部完成待核验',verify='核验并明确关闭')


def supply_snapshot(space,ledger,task):
    from .material_supply_receipts import history,order_history
    receipts=history(space,ledger);orders=order_history(ledger,space.event_id)
    results=[]
    for oid,value in sorted(orders.items()):
        values=[(r,json.loads(r.content_json)) for r in value['records']]
        if value['publication_ref']!=task['publication_ref'] or value['task_id']!=task['task_id']:continue
        rows=[(r,v) for r,v in receipts if v['order_id']==oid]
        dispatched=sum(v['dispatched_quantity'] for r,v in values if v['action']=='dispatch')
        received=sum(v['received_quantity'] for r,v in rows);damaged=sum(v['damaged_quantity'] for r,v in rows)
        results.append(dict(order_id=oid,item_id=value['item_id'],published=value['quantity'],dispatched=dispatched,received=received,
            damaged=damaged,good=received-damaged,outstanding=dispatched-received,cancelled=value['cancelled'],
            order_refs=[r.id for r,v in values],receipt_refs=[r.id for r,v in rows]))
    return results


def history(space,ledger):
    from .formal_task_receipts import history as receipts
    receipts(space)
    tasks=published(space);results=[]
    for index,record,value in ar.records(space,KIND):
        item=tasks.get(record.reference.object_id)
        if item is None:raise ValueError('执行反馈缺少正式任务来源。')
        publication,data,task=item
        if (record.source!=SOURCE or set(value)!={'order_id','publication_ref','draft_version','action','basis','overall_basis','damage_basis','supply','children'}
                or value['order_id']!=task['order_id'] or value['publication_ref']!=publication.id or value['draft_version']!=data['draft_version']
                or value['action'] not in LABELS or record.actor_id!=task['verifier_id' if value['action']=='verify' else 'recipient_id']):
            raise ValueError('执行反馈身份、任务或版本不一致。')
        ar.text(value['basis'])
        prior=ar.prefix(space,index)
        if publication.id not in prior.object_revisions:raise ValueError('反馈发布来源断链。')
        if value['action']=='verify':
            from copy import copy
            cut=copy(ledger)
            cut.supply_records={k:r for k,r in ledger.supply_records.items() if r.reference.event_id!=space.event_id or k in prior.object_revisions}
            cut.supply_receipts={k:r for k,r in ledger.supply_receipts.items() if r.reference.event_id!=space.event_id or k in prior.object_revisions}
            if value['supply']!=supply_snapshot(prior,cut,task|dict(publication_ref=publication.id)):
                raise ValueError('核验数量与当时物资来源不一致。')
            for row in value['supply']:
                if any(ref not in prior.object_revisions for ref in row['order_refs']+row['receipt_refs']):raise ValueError('核验物资来源断链。')
            if any(ref not in prior.object_revisions for ref in value['children']):raise ValueError('子任务核验来源断链。')
            previous=[v for r,v in results if r.reference.object_id==task['order_id']]
            if not previous or previous[-1]['action']!='completed':raise ValueError('缺少本版完成待核验报告。')
            if not task['parent_id']:ar.text(value['overall_basis'])
            elif value['overall_basis']:raise ValueError('子任务不替整体目标核验。')
            if any(row['damaged'] for row in value['supply']):ar.text(value['damage_basis'])
        elif value['overall_basis'] or value['damage_basis'] or value['supply'] or value['children']:
            raise ValueError('执行方不能冒充核验结论。')
        results.append((record,value))
    return tuple(results)


def verified(space,ledger,order_id):
    rows=[(r,v) for r,v in history(space,ledger) if r.reference.object_id==order_id]
    if not rows or rows[-1][1]['action']!='verify':return False
    task=published(space)[order_id][2]|dict(publication_ref=rows[-1][1]['publication_ref'])
    return space.command_flows[order_id].stage=='closed' and not flow_issue(space,order_id,space.event_id) and rows[-1][1]['supply']==supply_snapshot(space,ledger,task)


def record(space,registry,ledger,actor,*,order_id,publication_ref,expected_version,action,basis,request_id,overall_basis='',damage_basis=''):
    ar.text(actor);ar.text(basis)
    if action not in LABELS:raise ValueError('未知执行反馈动作。')
    item=published(space).get(order_id)
    if item is None or item[0].id!=publication_ref:raise ValueError('本事件正式发布版本已变化。')
    publication,data,task=item
    expected_actor=task['verifier_id' if action=='verify' else 'recipient_id']
    if actor!=expected_actor or actor not in registry.library:raise PermissionError('仅本版指定执行人或核验负责人可报告。')
    if action!='verify':
        route=recipient_route(space,space.event_id,task['spec']['responsible_group_id'],registry=registry,ledger=ledger)
        if route.leader_id!=actor:raise PermissionError('执行反馈须由本版正确现任队长/组长报告。')
    rows=history(space,ledger)
    old=next((r for r in space.object_revisions.values() if r.request_id==request_id),None)
    value=dict(order_id=order_id,publication_ref=publication_ref,draft_version=data['draft_version'],action=action,basis=basis,
        overall_basis=overall_basis,damage_basis=damage_basis,supply=[],children=[])
    if old:
        previous=json.loads(old.content_json);value.update(supply=previous.get('supply',[]),children=previous.get('children',[]))
        return ar.append(space,kind=KIND,object_id=order_id,source=SOURCE,actor=actor,values=value,expected_version=expected_version,request_id=request_id)
    from .task_relations import _live,control_history
    from .task_release import _bundle,task_gate
    _live(space,item);control_history(space)
    bundle=_bundle(space,data['decision_id'],data['draft_version']);draft=next(t for t in bundle.tasks if t.id==task['task_id'])
    issues,refs,_=task_gate(space,bundle,draft,registry=registry,ledger=ledger)
    if issues or set(refs)!=set(task['support_refs']):raise ValueError('本版专业/安全依据已变化，须重新核对发布。')
    flow=space.command_flows[order_id];at=datetime.now(timezone.utc).isoformat()
    if flow.stage not in {'running','partial','done_pending_verify'}:raise ValueError('须先明确接收、确认条件并开始，且任务未关闭。')
    if action=='verify':
        if flow.stage!='done_pending_verify':raise ValueError('须先由执行方报告全部完成待核验。')
        value['supply']=supply_snapshot(space,ledger,task|dict(publication_ref=publication_ref))
        if any(row['cancelled'] or abs(row['published']-row['received'])>1e-9 or row['outstanding']>1e-9 for row in value['supply']):
            raise ValueError('补给尚未全数实收或已取消，欠收不得按完成关闭。')
        if any(row['damaged'] for row in value['supply']):ar.text(damage_basis)
        if not task['parent_id']:
            ar.text(overall_basis)
            children=[oid for oid,(p,d,t) in published(space).items() if p.id==publication_ref and t['parent_id']==task['task_id']]
            if any(not verified(space,ledger,oid) for oid in children):raise ValueError('关联子任务尚未逐项由指定负责人核验关闭。')
            value['children']=[r.id for r,v in rows if r.reference.object_id in children and v['action']=='verify']
        elif overall_basis:raise ValueError('子任务不能代替整体目标核验。')
        updated=flow.close(result='completed',at=at,by=actor,note=basis,expected_version=1)
    else:
        if overall_basis or damage_basis:raise ValueError('执行方不能提交核验结论。')
        if flow.stage=='done_pending_verify':raise ValueError('已报告完成待核验，不能再回退执行进展。')
        if action=='completed' and flow.control_state!='normal':raise ValueError('执行困难尚未明确处理，不能报告全部完成。')
        if action=='resolved':
            if flow.control_state!='exec_abnormal':raise ValueError('没有待处理执行困难。')
            updated=replace(flow,control_state='normal',history=flow.history+({'at':at,'by':actor,'do':'具名困难处理','note':basis},))
        else:
            kind={'progress':'running','difficulty':'exec_abnormal','partial':'partial_done','completed':'all_done'}[action]
            updated=flow.add_receipt(kind=kind,by=actor,at=at,content=basis,source=SOURCE,request_id=request_id,expected_version=1)
    revision=ar.append(space,kind=KIND,object_id=order_id,source=SOURCE,actor=actor,values=value,expected_version=expected_version,request_id=request_id)
    space.command_flows[order_id]=updated
    if action=='verify':space.orders[order_id].status=OrderStatus.COMPLETED
    space.append_event('CommandFlowChanged',order_id=order_id,event_id=space.event_id,flow=asdict(updated),feedback_ref=revision.id)
    history(space,ledger)
    return revision
