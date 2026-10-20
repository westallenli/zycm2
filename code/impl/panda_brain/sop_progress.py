"""Read-only mandatory-action progress. A checked field is not a completed work area.

Reuse named, current-version business records. Work-area completion sources that
are not explicitly linked remain unknown; no checkbox or new completion write API.
"""
from . import sop_assistance as sa
from .authorization import Role
from .event_authorization import event_grants
from .task_recipients import recipient_route

LABELS={
    'fire-start':'核对启动记录','fire-source':'核对火情来源','fire-position':'核对灾情坐标',
    'fire-initial-report':'核对初报内容','evidence-links':'核对资料引用','challenged-facts':'核对事实异议',
    'situation-changes':'核对态势变化','observed-source-conflicts':'核对来源冲突留证',
    'observation-coverage':'核对观测资料','force-source-comparison':'比较力量需求依据',
    'force-ungrouped':'核对人员组别','cleanup-map-sources':'核对清理标绘',
    'emergency-signals':'核对应急线索','publication-capability':'核对发布依据接入',
    'handover-rectifications':'核对交接遗留事项'}


def _label(finding,state):
    key=finding['key']
    if key in LABELS:return LABELS[key]
    prefix,_,oid=key.partition(':')
    if prefix=='readiness':
        team=state.ledger.squads.get(oid)
        return '核对'+(team.name if team else oid)+'可派状态'
    if prefix=='force-count':return '核对'+{'combat':'作战力量','communication':'通信保障','medical':'医疗保障','other_support':'其他保障'}.get(oid,oid)+'记录'
    if prefix=='observation-origin':return '核对观测来源与时间'
    if prefix=='release-conditions':return '核对草案发布条件'
    if prefix=='task-version':return '核对任务当前版本'
    if prefix=='task-receipts':return '核对本版任务回执'
    if prefix=='task-feedback':return '核对本版执行反馈'
    return '核对本环节资料'


def _business(state):
    """Evaluate explicit publication/receipt/verification provenance, not prose."""
    from .formal_task_receipts import published,history
    from .formal_task_feedback import verified
    w=state.workspace;eid=w.event_id
    result=dict(drafted=False,published=False,received=False,verified=False,refs=[])
    bundles=[b for b in w.task_drafts.values() if b.event_id==eid]
    try:
        # Same content requirements as task_release.task_gate; merely creating an
        # empty draft does not finish the drafting action.
        result['drafted']=bool(bundles) and all(w.task_drafts_for_decision(b.decision_id,event_id=eid)==b
            and bool(b.tasks) and all(all(getattr(t.spec,key).strip() for key in
                ('title','objective','completion_standard','responsible_group_id','verifier_id'))
                and t.spec.verifier_id in state.registry.library
                and all(getattr(b.resolved_content(t.id),key).strip() for key in ('time_requirement','feedback_requirement'))
                for t in b.tasks) for b in bundles)
        tasks=published(w);receipts=history(w);live=[]
        complete=result['drafted']
        for bundle in bundles:
            from .task_release import preflight
            if preflight(w,bundle,registry=state.registry,ledger=state.ledger):complete=False
            actual=[(oid,item) for oid,item in tasks.items() if item[1]['decision_id']==bundle.decision_id
                    and item[1]['draft_version']==bundle.version
                    and getattr(w.orders[oid].status,'value',w.orders[oid].status) not in {'superseded','cancelled','expired'}]
            if {item[2]['task_id'] for oid,item in actual}!={t.id for t in bundle.tasks}:complete=False
            live.extend(actual)
        result['published']=bool(live) and complete
        received=result['published']
        for oid,(publication,data,task) in live:
            route=recipient_route(w,eid,task['spec']['responsible_group_id'],registry=state.registry,ledger=state.ledger)
            source=[r for r,v in receipts if r.reference.object_id==oid and v['action']=='received'
                    and v['publication_ref']==publication.id and v['draft_version']==data['draft_version']]
            flow=w.command_flows.get(oid)
            if (not source or flow is None or sa.task_sop.flow_issue(w,oid,eid)
                    or flow.ack_result!='accepted_ready' or flow.acked_by!=task['recipient_id']
                    or route.leader_id!=task['recipient_id'] or task['recipient_id'] not in state.registry.library):received=False
            result['refs'].extend(r.id for r in source);result['refs'].append(publication.id)
        result['received']=bool(live) and received
        result['verified']=result['received'] and all(verified(w,state.ledger,oid) for oid,item in live)
    except (ValueError,KeyError,TypeError,PermissionError):
        result.update(published=False,received=False,verified=False)
    return result


def attach(state,report):
    sa.checked_workspace(state,report['event_id'])
    grants=event_grants(state.workspace)
    deputy=next((state.registry.library[g.subject_id].name for g in grants
                 if g.role==Role.DEPUTY and g.subject_id in state.registry.library),'指定副指挥长')
    business=_business(state)
    obligations=(
        [('核验早期处置任务与结果',False,'相应核验负责人',2)],
        [('确认本次研判专业意见',False,'相应专业人员',2)],
        [('核定力量需求',any(f['key']=='force-source-comparison' and f['status']=='记录匹配' for f in report['steps'][2]['findings']),deputy,0),
         ('确认调用安排与保障落实',False,deputy+'／后勤负责人',2)],
        [('编制本版任务草案',business['drafted'],deputy,0),
         ('复核并正式发布本版任务',business['published'],deputy+'／审定人员',2),
         ('明确接收当前任务版本',business['received'],'当前版执行队长／组长',3)],
        [('核验正式任务结果',business['verified'],'本版指定核验负责人',2),
         ('核验整体扑救目标',False,'指挥负责人',2)],
        [('核验清理复查结果',False,'指定核验负责人',2),('核验看守交接',False,'交接负责人',2)],
        [('核验救援结果与必要交接',False,'指定核验负责人',2)],
        [('核对发布制度与具名职责',False,'发布责任人待明确',1),('核验本版稿件审批与发布回执',False,'制度明确的发布责任人',2)],
        [('核验整体目标',False,'指挥负责人／指定核验人',0),
         ('核对撤离物资去向与接收',False,'后勤／接收负责人',1),('核验交接遗留事项',False,'交接负责人',2)])
    for i,step in enumerate(report['steps']):
        auto=[];manual=[]
        for f in step['findings']:
            qs=[q for q in report['questions'] if q['step']==i and q['key']==f['key'] and q['basis']==f['basis']]
            status='已核对' if f['status']=='记录匹配' else '已补充' if f['status']=='已收到具名补充' else '待补资料'
            if any(q['status']=='pending' for q in qs):status='补问中'
            if any(q['status']=='escalated' for q in qs):status='需人工协调'
            if not step['available'] or report['service_error']:status='待重新核对'
            auto.append(dict(title=_label(f,state),status=status,done=status=='已核对',kind='auto',detail=f['text']))
            duplicate=f['key'] in {'force-source-comparison','publication-capability','cleanup-map-sources'} or f['key'].startswith('release-conditions:')
            if f['status']=='资料缺失或冲突' and f['kind'] in {'exception','decision'} and not duplicate:
                manual.append(dict(title=_label(f,state),done=False,status='待人工核对',owner=deputy,node=1 if i==2 else 0,detail=f['text']))
            for q in qs:
                if q['status']=='escalated':
                    manual.append(dict(title='协调补充：'+_label(f,state),done=False,status='待协调',owner=deputy,node=0,detail=q.get('reply',{}).get('content','资料无法取得')))
        if not auto:
            valid=step['available'] and not report['service_error']
            auto=[dict(title='核对本环节记录与来源',status='已核对' if valid else '待重新核对',done=valid,kind='auto',detail=step['check'])]
        required=[]
        for title,done,owner,node in obligations[i]:
            row=dict(title=title,done=bool(done),status='已完成' if done else '待核对',owner=owner,node=node,
                     detail='复用原办理记录。' if done else '尚缺明确关联本环节的当前有效办理或完成依据。')
            required.append(row)
            if not done:manual.append(row)
        step['actions']=auto+[dict(row,kind='business') for row in required if row['done']]
        step['manual']=manual
        step['complete']=bool(auto and required) and step['available'] and not report['service_error'] and all(r['done'] for r in auto+required) and not manual
        step['phase_status']='completed' if step['complete'] else 'pending'
        step['completion_refs']=business['refs'] if i in {3,4,8} else []
    # Named work checks replace the previous permanently-False placeholders.
    # Original source gaps stay available as diagnostics, not proof of completion.
    from .nine_flow import summary
    for item,work in zip(report['steps'],summary(state)):
        v=work['value'];r=work['revision'];i=work['step']
        item['work_record']=dict(status=work['status'],stale=work['stale'],value=v,
            reference=r.id if r else '',simulated=bool(r and r.data_kind=='simulated'))
        # Existing formal transactions remain mandatory, even after a human check.
        gate=(business['received'] if i==3 else business['verified'] if i==4 and v and v['result']=='completed' else True)
        item['complete']=work['done'] and gate and item['available'] and not report['service_error']
        item['phase_status']='completed' if item['complete'] else 'pending'
        if not v:
            item['manual'].append(dict(title='提交本环节办理并由指定人员核验',done=False,status='待办理',
                owner='指定副指挥长／本环节核验人',node=0,detail='已有单项任务不代表本环节范围已核对。'))
        if v:
            item['source_actions']=item['actions']
            item['legacy_manual']=item['manual']
            item['manual']=[] if item['complete'] else [dict(title=work['status'],done=False,
                status='待办理',owner='指定核验人／跟进负责人',node=0,
                detail=v['remaining'] or ('原始依据变化，须重新提交核验。' if work['stale'] else '请核对当前办理版本。'))]
            if item['complete']:
                item['completion_refs']=list(dict.fromkeys(item['completion_refs']+[r.id]+v['refs']))
                item['actions']=[dict(title='本次范围与具名核验',status=work['status'],done=True,kind='business',detail=v['basis'])]
    return report
