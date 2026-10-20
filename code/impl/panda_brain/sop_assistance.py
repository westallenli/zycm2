"""Nine-work-area automatic checks over audited records, not field certification.

Pure reports reuse domain projections. Only the background follow-up service writes
its own journal; reads, questions and replies never mutate orders or appointments.
"""
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
from . import sop, task_sop, execution_sop, cleanup_sop, emergency_sop, publication_sop, handover_sop
from .authorization import Role
from .event_authorization import event_grants
from .task_recipients import recipient_route

RULE_VERSION = 'sop-assistance-1'
CHECKS = ('火情来源、位置、初报与启动依据', '资料归属、来源引用与变化',
          '人员到场、可派报告与供给依据', '任务内容、版本与接收依据',
          '当前版执行反馈、异常与历史版本', '清理标绘来源、任务关联与反馈',
          '紧急/异常线索及人员保障记录', '沟通来源、接收范围与发布制度接入情况',
          '任务结果、物资去向与整改遗留')
DECISIONS = ('安排本事件处置', '选择研判方向与方案', '安排力量与保障', '编制或发布任务',
             '处理执行异常', '安排清理、看守与现场核验', '决定应急协调事项',
             '确认发布制度及职责', '决定现场验收与交接')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def checked_workspace(state, eid):
    record = state.registry.events.get(eid)
    if (record is None or record.status not in {'active', 'upgraded', 'closed'} or state.active_event_id != eid
            or state.workspace.event_id != eid or state.workspace.verify_audit_chain()):
        raise ValueError('事件归属或审计校验失败，智能参谋不沿用旧核对结果。')
    event_grants(state.workspace)
    return record


def _text_rows(value):
    """Display the checked domain projection, never infer truth from its prose."""
    if is_dataclass(value): value = asdict(value)
    if isinstance(value, str): return [value] if value.strip() else []
    if isinstance(value, dict):
        return [s for k,v in value.items() if k not in {'state','missing','deputy','source_context'} for s in _text_rows(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _text_rows(v)]
    return []


def _gaps(value):
    if is_dataclass(value): value = asdict(value)
    if isinstance(value, dict):
        return list(value.get('missing', ())) + [s for k,v in value.items() if k != 'missing' for s in _gaps(v)]
    if isinstance(value, (list, tuple)): return [s for v in value for s in _gaps(v)]
    return []


def report(state, eid):
    record = checked_workspace(state, eid); w = state.workspace
    views = [sop.early_response_view(w,record), sop.situation_view(w,record),
             sop.force_view(w,record,state.ledger), task_sop.deployment_view(w,record,state.ledger),
             execution_sop.execution_view(w,record,ledger=state.ledger), cleanup_sop.cleanup_view(w,record),
             emergency_sop.emergency_view(w,record,state.ledger), publication_sop.publication_view(w,record),
             handover_sop.handover_view(w,record,state.ledger,state.review_ledger)]
    steps = []
    for i, view in enumerate(views):
        rows = list(dict.fromkeys(_text_rows(view)))
        gaps = list(dict.fromkeys(_gaps(view)))
        unavailable = view.state in {'依据待核对', '未进入事件'}
        steps.append(dict(step=i, check=CHECKS[i], record_state=view.state,
            status='核对受阻' if unavailable else '已自动核对记录', available=not unavailable,
            facts=rows, gaps=gaps, findings=[], decision=DECISIONS[i],
            source=dict(event_id=eid,workspace_version=w.version,rule_version=RULE_VERSION,
                        snapshot_sha256=digest(asdict(view)))))
    candidates=[]
    duty=next((g.subject_id for g in state._duty_grants if g.subject_id in state.registry.library), '')
    deputy=next((g.subject_id for g in event_grants(w) if g.role==Role.DEPUTY and g.subject_id in state.registry.library), '')

    def finding(step,key,text,ok,*,recipient='',team='',scope='',question='',kind='data',value=None):
        item=dict(key=key,text=text,status='记录匹配' if ok else '资料缺失或冲突',kind=kind,
                  basis=digest(dict(key=key,scope=scope,value=value)),recipient=recipient,team=team)
        steps[step]['findings'].append(item)
        if not ok and question and recipient and steps[step]['available']:
            candidates.append(dict(key=key,step=step,recipient=recipient,team=team,scope=scope,
                question=question,basis=item['basis'],kind=kind))

    starts=[e for e in w.events if e.kind=='FireEventStarted' and e.payload.get('event_id')==eid]
    finding(0,'fire-start','启动操作者与时间已自动对照审计' if starts else '未找到启动审计依据',
            bool(starts) and all(e.payload.get('operator') and e.payload.get('at') for e in starts),kind='exception')
    alarms=[e for e in w.events if e.kind in {'ExternalAlarmConfirmed','ExternalFireEventReceived'} and e.payload.get('event_id')==eid]
    finding(0,'fire-source','已关联上游来源记录' if alarms else '上游来源记录缺失',bool(alarms),recipient=duty,
            question='请补充本事件原始报警来源、报告编号和资料依据；未知项目请说明无法取得。')
    coords=all(type(v) in (float,int) and math.isfinite(v) for v in (record.lat,record.lon))
    coords=coords and -90<=record.lat<=90 and -180<=record.lon<=180
    finding(0,'fire-position','事件坐标字段有效' if coords else '缺少有效灾情坐标',coords,recipient=duty,
            value=[record.lat,record.lon] if coords else None,question='请提供本事件定位来源、经纬度及发生时间；不要以地图默认中心代替火点。')
    finding(0,'fire-initial-report','已读取初报内容' if record.initial_report.strip() else '初报内容未记录',
            bool(record.initial_report.strip()),recipient=duty,question='请补充本事件已掌握的初报内容、来源与发生时间；无法取得请说明。')
    refs=[ref for c in w.claims.values() for ref in c.evidence_ids]
    refs += [ref for c in w.intel_items.values() for ref in c.evidence_ids]
    missing_refs=sorted(set(refs)-set(w.evidence))
    finding(1,'evidence-links','已自动检查资料引用；缺失 '+str(len(missing_refs))+' 项',not missing_refs,kind='exception',value=missing_refs)
    from .source_review import source_review_feed
    feed=source_review_feed(w,eid,include_all=True)
    inconsistent=[row['id'] for row in feed['reviews'] if row.get('disposition') in {'same_time_conflict','source_kind_conflict','condition_source_kind_conflict'}]
    finding(1,'observed-source-conflicts',f'已自动整理历史来源冲突留证 {len(inconsistent)} 项；当前变化与异议分别判断',True,value=inconsistent)
    for row in feed['observations']:
        complete=all(row.get(key) for key in ('source_record_id','collected_at','received_at'))
        finding(1,'observation-origin:'+row['evidence_id'],'观测 '+row['evidence_id']+(' 的来源编号和采集/接收时间完整' if complete else ' 的来源编号或时间缺失'),complete,
            recipient=duty,value=[row.get(key) for key in ('source_record_id','collected_at','received_at')],
            question='请补充观测 '+row['evidence_id']+' 的来源编号、采集/接收时间与来源依据；未知时请说明。')
    finding(1,'observation-coverage','已接入数值观测记录' if feed['observations'] else '尚无本事件数值观测资料',bool(feed['observations']),recipient=duty,
            question='请补充已掌握的本事件气象/环境观测、采集时间与来源；没有授权资料或无法取得时请说明。')
    challenged=[c.id for c in w.claims.values() if getattr(c.status,'value',c.status)=='challenged']
    finding(1,'challenged-facts','存在异议的事实 '+str(len(challenged))+' 项',not challenged,kind='exception',value=challenged)
    changed=[c.change_id for c in w.situation_changes.values() if c.event_id==eid and c.open_for_review]
    finding(1,'situation-changes','已发现待处理态势变化 '+str(len(changed))+' 项',not changed,kind='decision',value=changed)
    from .personnel_groups import group_people
    from . import squad_readiness, temporary_group_readiness
    if steps[2]['available']:
        unknown=views[2].needs.unknown_group_count if views[2].needs else 0
        finding(2,'force-ungrouped',f'组别待核定 {unknown} 人，未计入下方分类统计',not unknown,kind='decision',value=unknown)
        for row in views[2].needs.rows if views[2].needs else ():
            finding(2,'force-count:'+row.key,f'{row.label}：来源申报到场 {row.arrived_reported} 人、报告待命可派 {row.standby_reported} 人；所需人数尚待决策',
                    row.standby_reported<=row.arrived_reported,kind='exception',value=[row.arrived_reported,row.standby_reported])
        people=group_people(w,eid,ledger=state.ledger)
        readiness=squad_readiness.readiness_state(w,eid,ledger=state.ledger) | temporary_group_readiness.readiness_state(w,eid,ledger=state.ledger)
        def report_team(p):return p.source_id if p.origin=='fixed_squad' else p.pos_code
        for team in sorted({report_team(p) for p in people.values() if p.arrived and p.phone not in readiness}):
            route=recipient_route(w,eid,team,registry=state.registry,ledger=state.ledger)
            pending=sorted(p.phone for p in people.values() if report_team(p)==team and p.arrived and p.phone not in readiness)
            finding(2,'readiness:'+team,f'本组 {len(pending)} 名已报到场人员尚无可派状态报告',False,
                    recipient=route.leader_id,team=team,value=pending,
                    question='请补充本组已报到场人员的当前可派状态、来源与时间；信息回复不会代替正式人员状态登记。')
        finding(2,'force-source-comparison','已自动比较本次人员/任务/态势与原需求复核所用资料',
                views[2].needs is not None and views[2].needs.state=='current',kind='decision',
                value=views[2].needs.changed_sources if views[2].needs else [])
    formal={e.payload.get('order',{}).get('id') for e in w.events if e.kind=='FormalExecutionOrderPublished'}
    from .task_release import preflight
    for bundle in w.task_drafts.values():
        if bundle.event_id!=eid:continue
        try:
            issues=preflight(w,bundle,registry=state.registry,ledger=state.ledger)
        except (ValueError,PermissionError) as exc:
            issues=('本版发布依据不可核对：'+str(exc),)
        finding(3,'release-conditions:'+bundle.id,
            f'草案 {bundle.id} 当前版 {bundle.version}：'+('已记录的发布必要条件匹配' if not issues else '发布条件未齐备：'+'；'.join(issues)),
            not issues,kind='decision',scope=str(bundle.version),value=list(issues))
        steps[3]['facts'].extend(f'智能参谋已整理草案内容：{task.spec.title}；目标 {task.spec.objective}；主责组 {task.spec.responsible_group_id}；完成标准 {task.spec.completion_standard}；核验人 {task.spec.verifier_id}。'
                                for task in bundle.tasks)
    for oid in sorted(w.orders):
        issue=task_sop.flow_issue(w,oid,eid)
        finding(3,'task-version:'+oid,('任务 '+oid+'：'+issue) if issue else '任务 '+oid+' 的当前版本与审计匹配',not issue,kind='exception')
        if issue: continue
        flow=w.command_flows[oid]; current, historical, invalid=task_sop.receipt_groups(flow)
        finding(4,'task-receipts:'+oid,f'任务 {oid}：当前版回执 {len(current)} 条，历史版 {len(historical)} 条，归属或来源缺项 {invalid} 条',not invalid,kind='exception')
        if (oid in formal and getattr(w.orders[oid].status,'value',w.orders[oid].status)!='superseded'
                and not flow.closed_at and (not current or not flow.acked_at or not flow.acked_by)):
            route=recipient_route(w,eid,flow.unit,registry=state.registry,ledger=state.ledger)
            finding(4,'task-feedback:'+oid,f'任务 {oid} 版本 {flow.content_version} 的反馈/接收依据待补',False,
                recipient=route.leader_id,team=flow.unit,scope=str(flow.content_version),
                question=f'请补充任务 {oid} 当前内容版本 {flow.content_version} 的实际进展、困难和来源时间；信息回复不会自动生成任务接收回执或完成核验。')
    finding(5,'cleanup-map-sources',f'已自动核对清理标绘：当前 {len(views[5].marks)} 项，撤回历史 {len(views[5].withdrawn)} 项',
            bool(views[5].marks),kind='capability')
    finding(6,'emergency-signals',f'已汇集紧急/异常/支援线索 {len(views[6].signals)} 条；是否解除需对应处置依据',
            not views[6].signals,kind='decision')
    finding(7,'publication-capability','正式通报/新闻制度、稿件审批和送达接口尚未接入',False,kind='capability')
    finding(8,'handover-rectifications',f'已自动对照复盘遗留：未闭环 {len(views[8].open_rects)} 项',not views[8].open_rects,kind='decision')
    return dict(event_id=eid,revision=w.version,rule_version=RULE_VERSION,steps=steps,candidates=candidates,
                notice='核对结果基于已接入记录；来源自报、模拟资料与现场独立核验分别呈现。记录核对完成不代表任务完成。')
