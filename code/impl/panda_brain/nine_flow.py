"""Event-scoped work records and independent checks, sharing the audit ledger.

These are named reports of work and its verification, never automatic professional
approval or a replacement for formal task, inventory or event-close transactions.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time
from .durable_storage import durable_action
from . import audited_records as ar

OBS='workflow_observation'
CHECK='workflow_checkpoint'
SOURCE='named-nine-area-work'
RESULTS=dict(completed='本次范围已核验',ongoing='仍在办理',not_applicable='本次不适用',handover='已接收遗留事项')
FIELDS=('scope','feedback','basis','remaining','followup','verifier_id','result')
GUIDANCE=(
    '记录来源、位置、初报、处置动作及结果；新补充资料保留录入时间。',
    '记录现场变化、采用的资料、具名专业意见和下一步建议。',
    '分别核对到场、可派、需求、调用及保障缺口；有缺口请填写跟进责任。',
    '引用当前正式发布版本、执行人接收依据；核对尚未部署的工作。',
    '区分单项任务结果与整体扑救目标，记录异常、结果和核验范围。',
    '记录清理范围、复查结果、看守分工、交接对象与未消除事项。',
    '记录险情对象、位置、救援结果和转运交接；无救援事项须说明不适用依据。',
    '留存内部通报或对外新闻的制度依据、稿件版本、编制/审核/发布职责及接收范围和回执；此处不代发。',
    '核对任务结果、撤离条件、物资去向及遗留清单；遗留交接由指定接收人本人确认。')

#: 新手说明书：每一步回答四件事——干什么／参谋已做／你要做／做完的标志。
#: 写法要求：能念给别人听，不出现专业术语；"你要做"必须是一个能点的动作。
GUIDE={
 0:('把报警和到场第一步记下来，后面才有据可查',
    '核对上游报警、坐标、初报内容和重点目标',
    '确认不是误报，启动值班并指定副指挥',
    '事件变成"进行中"，值班和副指挥都有名有姓'),
 1:('弄清楚火在哪、往哪烧、先保什么',
    '汇总侦查回报、气象、地形、受威胁对象，并标出还缺什么',
    '定下先顾哪里、哪些地方暂时去不了',
    '写清当前重点，并指出哪些行动受影响'),
 2:('算清楚有多少人、多少装备、还缺多少',
    '核对到场人数、可派人数、需求量和保障缺口',
    '决定要不要调外援或让公司供货（同意／不同意）',
    '缺口都有人认领，或者明确说明"这次不补"'),
 3:('把方案变成给谁、去哪、干什么',
    '按方案拟好任务草案，算好该下给哪个队',
    '同意下达，或者改一下再下',
    '任务已经发出去，队长收到'),
 4:('盯着队伍干到哪，出问题就调',
    '汇总各队回执、进度和异常',
    '遇到困难时决定增援、调整还是先停',
    '任务有结果，异常有处置记录'),
 5:('火灭了不算完，要看住清理过的火场',
    '汇总清理范围、复查结果和看守分工',
    '定下谁看守、守到什么时候、交给谁',
    '有人看守，交接对象明确'),
 6:('有人受伤、被困或要紧急撤离，按这条走',
    '汇总险情对象、位置和可用的救援资源',
    '决定派谁去救、伤员往哪送',
    '人员有结果，转运有接收方'),
 7:('对外说什么、由谁来说',
    '拟好通报或新闻稿草案和发布依据',
    '审核后按职责发布（系统不代发）',
    '有版本、有审核人、有接收范围'),
 8:('确认干完了，把剩下的交代清楚',
    '核对任务结果、撤离条件、物资去向和遗留清单',
    '确认验收，并指定遗留事项的接收人',
    '结果核验通过，遗留有人接'),
}

#: 每一步由谁完成。第 1 步是值班员的活；任命之后副指挥长接手其余八步。
GUIDE_OWNER={0:'值班员',1:'副指挥长',2:'副指挥长',3:'副指挥长',4:'副指挥长',
             5:'副指挥长',6:'副指挥长',7:'副指挥长',8:'副指挥长'}

#: 每一步"你要做"对应的那一个动作，也是页面顶部的主按钮。
#: 上面写什么，底下第一个按钮就是什么——不许出现"要你做 A，给你 B"。
ACTION_GUIDE={
 0:('启动值班、指定副指挥','/events/{event}/details'),
 1:('看最新情况、定重点','/sand#cmd-situation'),
 2:('去核力量与缺口','/materials/event'),
 3:('进沙盘：下达任务','/sand'),
 4:('进沙盘：组织扑救','/sand'),
 5:('去安排看守与交接','/sand/task-receipts'),
 6:('去调度救援','/materials/event'),
 7:('去审核发布','/events/{event}/details'),
 8:('去验收、交接遗留','/materials/closeout'),
}

#: 某一步还有第二个去处（次要）。第 3 步的"调外援/让公司供货"就在这里。
EXTRA_GUIDE={
 2:('调外援、让公司供货','/material-supply'),
 8:('物资收尾与撤离接收','/materials/closeout'),
}

def history(space,kind):
    """Validate mirrors, versions and actors even on read; never fall back silently."""
    if not space.event_id or space.verify_audit_chain():raise ValueError('事件审计链待核对。')
    objects={r.id:r for r in space.object_revisions.values() if r.reference.object_kind==kind}
    rows=[];versions={};requests=set();prior=set();clocks={}
    for e in space.events:
        raw=e.payload.get('revision') if e.kind=='ObjectRevisionRecorded' else None
        if raw and raw['reference']['object_kind']==kind:
            r=objects.get(e.payload.get('revision_id'))
            if r is None or e.payload!={'revision_id':r.id,'revision':asdict(r)}:raise ValueError('环节记录与审计镜像不一致。')
            v=json.loads(r.content_json);ref=r.reference
            at=explicit_time(r.times.recorded_at)
            if (ref.event_id!=space.event_id or r.source!=SOURCE or r.data_kind not in {'reported','simulated'}
                or ref.version!=versions.get(ref.object_id,0)+1 or r.request_id in requests
                or at>explicit_time(e.recorded_at) or at<clocks.get(ref.object_id,at)
                or type(v.get('step')) is not int or v['step'] not in range(9)):
                raise ValueError('环节记录归属、版本或来源不一致。')
            if not isinstance(v.get('refs'),list) or not v['refs'] or any(x not in prior for x in v['refs']):
                raise ValueError('环节记录缺少同事件前序依据。')
            if kind==CHECK:
                if (set(v)!={'step','action','refs','proposal_ref','source_digest','scope','feedback','basis','remaining','followup','verifier_id','result'}
                    or ref.object_id!='step-'+str(v['step']) or v['result'] not in RESULTS
                    or v['action'] not in {'submit','accept','return'}):raise ValueError('环节核验内容不完整。')
                previous=[(p,x) for p,x in rows if x['step']==v['step']]
                if v['action']!='submit':
                    if not previous or previous[-1][1]['action']!='submit':raise ValueError('须先提交本版办理记录。')
                    p,x=previous[-1]
                    if (v['proposal_ref']!=p.id or r.actor_id!=x['verifier_id'] or r.actor_id==p.actor_id
                        or any(v[k]!=x[k] for k in (*FIELDS,'source_digest','refs'))
                        or r.data_kind!=p.data_kind):raise ValueError('核验人、办理版本或原始依据不一致。')
                elif v['proposal_ref'] or r.actor_id==v['verifier_id']:raise ValueError('登记者与核验人须分开。')
            versions[ref.object_id]=ref.version;clocks[ref.object_id]=at;requests.add(r.request_id);rows.append((r,v))
        prior.add(e.id)
        if raw:prior.add(e.payload['revision_id'])
    if len(rows)!=len(objects):raise ValueError('环节记录缺少审计镜像。')
    return rows

def digest(state):
    # Follow-up checkpoints and report lifecycles do not invalidate each other.
    from .review_reports import REPORT_EVENTS
    events=[asdict(e) for e in state.workspace.events if e.kind not in REPORT_EVENTS and not (
        e.kind=='ObjectRevisionRecorded' and e.payload['revision']['reference']['object_kind'] in
        {CHECK,'review_flow_analysis','review_flow_link'})]
    return ar.digest(events)

def versions(state,kind,object_id):
    return sum(r.reference.object_id==object_id for r,v in history(state.workspace,kind))

def latest(state):
    result={}
    for r,v in history(state.workspace,CHECK):result[v['step']]=(r,v)
    return result

def summary(state):
    current=digest(state);out=[];saved=latest(state)
    for step in range(9):
        r,v=saved.get(step,(None,None))
        stale=bool(v and v['source_digest']!=current)
        status=('资料变化，需重核' if stale else '待指定人员核验' if v and v['action']=='submit' else
                '已退回补充' if v and v['action']=='return' else RESULTS[v['result']] if v else '尚无环节办理记录')
        if r and r.data_kind=='simulated':status='模拟 · '+status
        out.append(dict(step=step,revision=r,value=v,status=status,stale=stale,
            done=bool(v and not stale and v['action']=='accept' and v['result'] in {'completed','not_applicable','handover'})))
    return out

def _member(state,token,*,write=False):
    from .workflow import context
    member,record=context(state,token)
    if state.workspace.event_id!=record.event_id or state.active_event_id!=record.event_id:raise PermissionError('事件已变化。')
    if write and record.status not in {'active','upgraded'}:raise PermissionError('已结束事件只可查看历史办理记录。')
    return member,record

def candidates(state):
    """Existing event leaders/deputy; nomination conveys only this record's check."""
    from .personnel_groups import group_people
    from .event_authorization import event_grants
    people=group_people(state.workspace,state.active_event_id,ledger=state.ledger)
    phones={p.phone for p in people.values() if p.phone in state.registry.library and state.registry.library[p.phone].is_leader}
    from .authorization import Role
    phones.update(g.subject_id for g in event_grants(state.workspace) if g.role==Role.DEPUTY)
    return {p:state.registry.library[p].name for p in sorted(phones) if p in state.registry.library}

def _save(state,row,expected):
    candidate=deepcopy(state.workspace);candidate._record_object_revision(row,expected_version=expected)
    from .persistence import save_workspace
    if state.db_path:save_workspace(candidate,state.db_path)
    target=state.registry.events[state.active_event_id].snapshot_path
    if target and str(target)!=str(state.db_path):save_workspace(candidate,target)
    state.workspace=candidate
    return row

def _append(state,token,kind,object_id,value,expected,request_id,*,data_kind='reported',occurred_at=''):
    member,_=_member(state,token,write=True)
    if type(expected) is not int or expected<0:raise ValueError('版本须为非负整数。')
    ar.text(request_id)
    content=ar.dump(value)
    old=next((r for r in state.workspace.object_revisions.values() if r.request_id==request_id),None)
    if old:
        if (old.reference!=ObjectReference(state.active_event_id,kind,object_id,expected+1) or old.actor_id!=member.phone
            or old.content_json!=content or old.data_kind!=data_kind or old.times.occurred_at!=occurred_at):raise ValueError('请求号已用于不同内容。')
        return old
    if versions(state,kind,object_id)!=expected:raise ValueError('办理版本已变化，请刷新后再提交。')
    row=ObjectRevision(ObjectReference(state.active_event_id,kind,object_id,expected+1),
        RecordTimes(occurred_at=occurred_at,recorded_at=datetime.now(timezone.utc).isoformat()),SOURCE,
        data_kind,member.phone,request_id,content)
    return _save(state,row,expected)

def _refs(state,refs):
    from .review_sources import records
    allowed={r['reference'] for r in records(state)}
    if not isinstance(refs,list) or not refs or len(refs)>30 or len(refs)!=len(set(refs)) or any(r not in allowed for r in refs):
        raise ValueError('请选择本事件已有原始依据。')
    return refs

def _nature(state,refs,data_kind):
    if data_kind!='simulated' and any(state.workspace.object_revisions[r].data_kind=='simulated' for r in refs if r in state.workspace.object_revisions):
        raise ValueError('包含模拟依据的记录须标记模拟。')

def _text(value,required=True):
    if not isinstance(value,str) or len(value)>4000 or value!=value.strip() or (required and not value):raise ValueError('请完整填写办理范围、情况与依据，单项不超过4000字。')
    return value

@durable_action
def observe(state,token,*,step,subject,feedback,location,refs,occurred_at,expected_version,request_id,data_kind='reported'):
    member,record=_member(state,token,write=True)
    if record.event_id not in state.deputy_events(token):raise PermissionError('仅指定副指挥长可登记环节补充。')
    if type(step) is not int or step not in range(9) or data_kind not in {'reported','simulated'}:raise ValueError('环节或资料性质无效。')
    if occurred_at:explicit_time(occurred_at)
    refs=_refs(state,refs);_nature(state,refs,data_kind)
    value=dict(step=step,subject=_text(subject),feedback=_text(feedback),location=_text(location),refs=refs)
    return _append(state,token,OBS,'observation-'+request_id,value,expected_version,request_id,data_kind=data_kind,occurred_at=occurred_at)

@durable_action
def submit(state,token,*,step,values,refs,expected_version,request_id,data_kind='reported',expected_source_digest=None):
    member,record=_member(state,token,write=True)
    if record.event_id not in state.deputy_events(token):raise PermissionError('仅指定副指挥长可提交本环节办理记录。')
    if type(step) is not int or step not in range(9) or data_kind not in {'reported','simulated'} or set(values)!=set(FIELDS):raise ValueError('办理字段无效。')
    for k in FIELDS:_text(values[k],required=k not in {'remaining','followup'})
    if values['result'] not in RESULTS or (values['result']=='handover' and step!=8):raise ValueError('办理结果无效。')
    if values['verifier_id']==member.phone or values['verifier_id'] not in candidates(state):raise ValueError('须指定另一位本事件负责人核对。')
    if values['remaining'] and not values['followup']:raise ValueError('未完成事项须明确跟进责任、期限或接收安排。')
    if values['result']=='ongoing' and not values['remaining']:raise ValueError('仍在办理须写明未完成事项。')
    if values['result']=='completed' and values['remaining']:raise ValueError('有未完成事项时请选择仍在办理或遗留交接。')
    if values['result']=='handover' and (not values['remaining'] or not values['followup']):raise ValueError('交接须列明遗留事项及责任，核验人为本次接收人。')
    refs=_refs(state,refs)
    old=next((r for r in state.workspace.object_revisions.values() if r.request_id==request_id),None)
    if old and (old.reference.object_kind!=CHECK or old.reference.object_id!='step-'+str(step)):
        raise ValueError('请求号已用于不同记录。')
    current_digest=json.loads(old.content_json).get('source_digest') if old and old.reference.object_kind==CHECK else digest(state)
    if expected_source_digest is not None and expected_source_digest!=current_digest:
        raise ValueError('打开表单后原始资料已变化，请刷新并重新核对办理结果。')
    observations={r.id for r,v in history(state.workspace,OBS) if v['step']==step}
    if not observations.intersection(refs):raise ValueError('须先登记本环节现场或办理情况，并引用该记录。')
    # Keep simulated provenance through every dependent work record.
    _nature(state,refs,data_kind)
    value=dict(step=step,action='submit',refs=refs,proposal_ref='',source_digest=current_digest,**values)
    # Exact retries must retain the digest captured by that request.
    if old:value['source_digest']=json.loads(old.content_json)['source_digest']
    return _append(state,token,CHECK,'step-'+str(step),value,expected_version,request_id,data_kind=data_kind)

@durable_action
def verify(state,token,*,step,proposal_ref,expected_version,request_id,decision):
    member,_=_member(state,token,write=True)
    if decision not in {'accept','return'}:raise ValueError('核验动作无效。')
    rows=history(state.workspace,CHECK)
    proposal=next(((r,v) for r,v in rows if r.id==proposal_ref),None)
    if proposal is None or proposal[1]['step']!=step or proposal[1]['action']!='submit':raise ValueError('待核验办理记录不存在。')
    r,v=proposal
    if member.phone!=v['verifier_id'] or member.phone==r.actor_id:raise PermissionError('仅本版指定核验人／接收人本人可确认或退回。')
    value=v|dict(action=decision,proposal_ref=r.id)
    old=next((r for r in state.workspace.object_revisions.values() if r.request_id==request_id),None)
    if not old:
        if latest(state).get(step,(None,None))[0]!=r:raise ValueError('办理版本已变化，请刷新。')
        if decision=='accept' and v['source_digest']!=digest(state):raise ValueError('原始依据已变化，请重新提交办理记录后核验。')
    return _append(state,token,CHECK,'step-'+str(step),value,expected_version,request_id,data_kind=r.data_kind)

def markdown(state):
    from .sand_shell import WORK_ITEMS
    rows=summary(state)
    lines=['### 九步办理与交接']
    for row in rows:
        v=row['value'];lines.append(f'- {row["step"]+1:02d} {WORK_ITEMS[row["step"]][0]}：{row["status"]}')
        if v:
            lines.extend(f'  - {label}：{v[key] or "无"}' for key,label in [('scope','核对范围'),('feedback','情况与结果'),('basis','核验依据'),('remaining','未完成事项'),('followup','跟进与接收')])
            r=row['revision'];proposal=state.workspace.object_revisions.get(v['proposal_ref']) if v['proposal_ref'] else r
            submitter=state.registry.library.get(proposal.actor_id) if proposal else None
            verifier=state.registry.library.get(v['verifier_id'])
            lines.append('  - 办理登记者：'+(submitter.name if submitter else '具名人员')+'；指定核验人／接收人：'+(verifier.name if verifier else '具名人员')+'；办理结果以该版本记录为准。')
    return '\n'.join(lines)
