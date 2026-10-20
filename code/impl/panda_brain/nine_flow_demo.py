"""Explicit fictional continuation, only on an isolated copy of the existing demo."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import json
from . import nine_flow as flow
from .resource_demo import DEPUTY

def extend(state):
    root=state.registry_path.parent;marker=root/'nine-flow-demo.json'
    if marker.exists():return json.loads(marker.read_text(encoding='utf-8'))
    if not getattr(state,'resource_demo',{}).get('simulated') or state.active_event_id!='fire-001':raise ValueError('仅允许在明确模拟的原演示副本追加。')
    before=[asdict(e) for e in state.workspace.events]
    unchanged={p.name:p.read_bytes() for p in root.glob('*.json')}
    deputy,error=state.member_login(DEPUTY,'123456');assert not error,error
    people=flow.candidates(state)
    verifier_id=next(p for p in people if p!=DEPUTY)
    verifier,error=state.member_login(verifier_id,'123456');assert not error,error
    receiver=people[verifier_id]
    from .review_sources import records
    original=records(state)
    water=[x['reference'] for x in original if x['kind'] in {'formal_task_release','formal_task_feedback','formal_supply_receipt'}]
    material=[x['reference'] for x in original if x['kind']=='task_material_inputs']
    maps=[x['reference'] for x in original if x['kind']=='map_annotation']
    refs=list(dict.fromkeys(water+material+maps))[:25] or [original[0]['reference']]
    stamp=datetime.now(timezone.utc)
    deadline=(stamp+timedelta(hours=2)).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    scenes=[
        ('模拟补充初报与早期处置','模拟观察员报告：东岭演练火点位于北纬30.004、东经110.003；沿现有模拟配送路线设置保障点并完成饮水任务安排。此次新增初报不回填原上游报警，也不证明初起火处置已完成。','ongoing','原上游报警原文、初起火处置任务及现场核验未接入。','演示副总指挥在本轮演练复核时补齐来源及处置依据。'),
        ('模拟态势复核','对照当前模拟火点、保障点、路线和作业范围，饮水运输路线与任务相符；气象、火势变化和专业处置意见仍缺原始资料，保持待核。','ongoing','缺气象实测、火势变化与具名专业意见。','演示副总指挥会同专业人员补充资料后重新研判，不从地图草稿推定专业结论。'),
        ('模拟力量与保障核对','原名册120人，累计到场报告32人；到场不等于可派。原饮水实收120瓶，餐食30份与照明4套缺口仍存在，不重复收货。','ongoing','可派状态与人力需求待复核；餐食30份、照明4套待补。',f'演示副总指挥及模拟后勤负责人于{deadline}前核对可派和补给安排，由{receiver}跟进回执。'),
        ('模拟饮水任务部署复核','引用原正式发布版本、执行队长接收、执行条件与独立核验记录，核对目标、分工、时限、完成标准。此次仅核对原饮水配送的部署范围。','completed','',''),
        ('模拟执行与整体目标复核','原饮水配送已由指定人员核验，120瓶实收依据齐全；餐食照明另行跟进。整场火情的扑救目标尚无独立现场核验，不能由饮水任务关闭推定达成。','ongoing','整场火情的扑救执行与目标核验资料待补。','演示副总指挥向相应现场负责人收集正式扑救任务、异常处置和整体核验记录。'),
        ('模拟清理看守安排','本轮模拟安排保障点周边巡查与看守，要求报告热点、复查时间、交接范围和下一班接收人；目前只形成安排，未收到清理结果和接班确认。','ongoing','清理复查结果、看守分工回执及接班确认待补。',f'由{receiver}在{deadline}前跟踪模拟巡查反馈并核对接班记录。'),
        ('模拟险情与救援适用性核对','本轮新增虚构巡查反馈：在当前模拟保障点巡查范围内未发现伤困人员，暂未发生转运。仅此范围和本次巡查不适用救援转运；若出现新险情须重新办理。','not_applicable','',''),
        ('模拟内部通报留证','演练约定：内部保障通报由演示副总指挥编制，指定核验人审核并确认系统内模拟接收。稿件v1：到场报告32人、饮水实收120瓶、餐食30份及照明4套仍缺；接收范围为本次演练执行队长。此记录与后续本人核验共同留存审核及模拟接收依据；未发送外部消息、未作对外新闻发布。','completed','',''),
        ('模拟阶段交接与复盘接收','阶段交接清单：原饮水任务已核验；火情仍进行中。原资料缺失、专业意见、可派复核、餐食照明、扑救整体核验、清理看守回执列入遗留，由指定接收人本人确认后继续跟踪；复盘负责人沿同一事件生成新阶段报告。','handover','原始来源和专业资料待核；可派及需求复核；餐食30份、照明4套待补；整体扑救与清理看守未核验。',f'由{receiver}接收本次阶段遗留，演示副总指挥统筹；于{deadline}复核进展，新增办理记录后重新核验。'),
    ]
    obs=[]
    # All source observations precede the checks, so later work checks do not
    # accidentally invalidate earlier ones; a subsequent real fact still does.
    for i,(subject,feedback,result,remaining,followup) in enumerate(scenes):
        obs.append(flow.observe(state,deputy,step=i,subject=subject,feedback=feedback,
            location='东岭虚构火场及模拟保障点；本次新增演练过程',refs=refs,
            occurred_at=(stamp+timedelta(microseconds=i)).isoformat(),expected_version=0,
            request_id='nine-demo-observation-'+str(i),data_kind='simulated'))
    for i,(subject,feedback,result,remaining,followup) in enumerate(scenes):
        values=dict(scope=subject+'（本轮新增模拟范围）',feedback=feedback,
            basis='核对本环节新增模拟记录与原任务/物资/标绘依据；不把新增演练过程当原历史或真实现场。',
            remaining=remaining,followup=followup,verifier_id=verifier_id,result=result)
        row=flow.submit(state,deputy,step=i,values=values,refs=[obs[i].id]+refs,
            expected_version=0,request_id='nine-demo-submit-'+str(i),data_kind='simulated')
        flow.verify(state,verifier,step=i,proposal_ref=row.id,expected_version=1,
            request_id='nine-demo-verify-'+str(i),decision='accept')
    assert before==[asdict(e) for e in state.workspace.events[:len(before)]]
    assert not state.workspace.verify_audit_chain()
    for name,value in unchanged.items():assert (root/name).read_bytes()==value,name
    state._pending_review_actor=('owner',state.registry.library[DEPUTY].name,state.active_event_id,deputy)
    rid=state.handle_report_generate(stage=True)
    state.handle_report_edit(rid,state.review_ledger.reports[rid].content_md)
    result=dict(simulated=True,event_id=state.active_event_id,original_audit_count=len(before),
        audit_count=state.workspace.version,original_audit_preserved=True,report=rid,
        status=[x['status'] for x in flow.summary(state)],receiver=receiver)
    marker.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
