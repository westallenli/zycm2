"""An explicit new rehearsal of the retained Qinglan scene, never a data migration.

Spatial geometry comes from the morning scene. Named tasks, receipts and review
are new fictional records, so the old browser-only animation is not fake history.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import json
from . import audited_records as ar

SCHEMA = 'qinglan-fire-rehearsal-v1'


def load_scene(state):
    path = state.registry_path.parent/'fire-drill-scene.json'
    if not path.exists():
        return
    scene = json.loads(path.read_text(encoding='utf-8'))
    from .demo_templates import template
    original = template(scene.get('template_id','qinglan'))
    if (scene.get('rehearsal_schema') != SCHEMA or scene.get('origin_digest') != ar.digest(original)
            or scene.get('data_kind') != 'simulated' or len(scene.get('records', [])) != len(original['records'])
            or scene.get('anchor') != original['anchor'] or scene.get('event_name') != original['event_name']
            or scene.get('event_id') not in state.registry.events):
        raise ValueError('灭火重演资料与原模拟场景不一致。')
    for row, source in zip(scene['records'], original['records']):
        if any(row.get(k) != source.get(k) for k in ('id','kind','points','title','unit','details')):
            raise ValueError('原灭火场景几何或对象被替换，停止加载。')
    state.fire_drill_scene = scene


def _source(state, object_id, subject, feedback, refs, occurred_at, *, points=None):
    from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
    now = datetime.now(timezone.utc).isoformat()
    value = dict(subject=subject, feedback=feedback, evidence_refs=list(refs), data_kind='simulated')
    if points is not None:value['points'] = points
    row = ObjectRevision(ObjectReference(state.active_event_id,'demo_field_observation',object_id,1),
        RecordTimes(occurred_at=occurred_at,recorded_at=now),SCHEMA,'simulated',
        '13900000120','drill-source-'+object_id,ar.dump(value))
    state.workspace._record_object_revision(row,expected_version=0)
    state.persist_if_db()
    return row


def seed(root, *, template_id='qinglan'):
    from .resource_demo import seed as resources, DEPUTY
    from .demo_templates import template,label
    original=template(template_id)
    area=label('北山',template_id)
    state = resources(root, fire_scene=True, template_id=template_id)
    eid=state.active_event_id
    deputy,error=state.member_login(DEPUTY,'123456')
    if error:raise ValueError(error)
    state.login('resource-demo-local')
    scene=deepcopy(original);base=datetime.now(timezone.utc)-timedelta(minutes=35)
    original_time=datetime.fromisoformat(scene['recorded_at'])
    scene.update(event_id=eid,rehearsal_schema=SCHEMA,origin_digest=ar.digest(original),recorded_at=base.isoformat())
    if template_id!='qinglan':scene['template_id']=template_id
    scene['team_mapping']=dict(zip(('扑救一分队（演示）','扑救二分队（演示）','县消防队（演示）'),state.resource_demo['called_squads'][:3]))
    for row in scene['records']:
        row['observed_at']=(base+(datetime.fromisoformat(row['observed_at'])-original_time)).isoformat()
        row['basis']+='；原模拟场景的新一次重演，不是旧现场历史。'
    for row in scene['messages']:
        if row.get('time'):
            row['time']=(base+(datetime.fromisoformat(row['time'])-original_time)).isoformat()
    path=state.registry_path.parent/'fire-drill-scene.json'
    path.write_text(json.dumps(scene,ensure_ascii=False,indent=2),encoding='utf-8')
    load_scene(state)
    state.resource_demo['scenario']=label('青岚县北山林火：侦查、部署、变更、执行、核验、保障、九步及复盘同源模拟',template_id)
    (state.registry_path.parent/'resource-demo.json').write_text(json.dumps(state.resource_demo,ensure_ascii=False,indent=2),encoding='utf-8')
    refs=[]
    for row in scene['records']:
        source=_source(state,'scene-'+row['id'],row['title'],row['basis']+'；来源 '+row['source'],refs[:1],row['observed_at'],points=row['points'])
        refs.append(source.id)
    # The three original visual alternatives remain available to the minute demo.
    # These task options also give the persisted rehearsal explicit decision bases.
    from .models import Option
    from .task_drafts import PlanTaskBlueprint,TaskDraftSpec,TaskCommonContent
    squads=[state.ledger.squads[s] for s in state.resource_demo['called_squads'][:3]]
    content=TaskCommonContent(location=label('青岚县北山林区原模拟火场及计划控制线',template_id),
        time_requirement='本轮模拟作业时段；变化时立即暂停并复核',feedback_requirement='按当前版本报告位置、困难、进展和完成证据',
        safety_measures='模拟核对撤离通道、通信和观察条件；不能作为真实安全认证',
        firefighting_method='模拟外围分段控制，专业方法仅供流程演示',support_measures='饮水实收120瓶；餐食照明缺口继续跟踪')
    def spec(title,objective,group,verifier):
        return TaskDraftSpec(title=title,objective=objective,completion_standard='模拟现场回报与原范围逐项对应，由另一位指定人员核对',
            responsible_group_id=group.squad_id,verifier_id=verifier.leader_phone,content=content)
    blueprint=PlanTaskBlueprint(main=spec(area+'林火分段控制（模拟）','按已核查通道分段衔接外围计划线，不以单点处置推定全场结束',squads[0],squads[1]),
        children=(spec(area+'侦查与通道核对（模拟）','复核火线、热点、通道与重点目标，报告不确定项',squads[1],squads[2]),
                  spec('残火复查与看守安排（模拟）','清理指定巡查范围并确认本班看守，后续交班继续跟踪',squads[2],squads[0])))
    for code,title,summary,risk in [
        ('A','外围合围与分段推进','沿外围计划线分段衔接，依据新回报复核后向内推进','原通道可能受阻，必须复核'),
        ('B','重点目标保护与侧翼部署','优先保护输电走廊和值守站，侧翼控制','目标周边通道和人员待核'),
        ('C','先侦查核实与分批部署','先补查通道，保留预备和保障力量，分批进入','等待资料可能影响部署时机')]:
        state.workspace.add_option(Option('qinglan-plan-'+code,title+'（模拟）',summary,tuple(refs[:3]),(),
            risks=(risk,),preconditions=('仅适用于本轮虚构地形与资料','专业意见和必要安全条件逐项核对'),
            cost_estimate='按演示三队分工，不是专业用量结论',time_estimate='按阶段回报推进，未知不估算',
            no_go_conditions=('新通道未经核对不得视为可用',),task_blueprint=blueprint if code=='A' else None))
    state.persist_if_db()
    decision=state.handle_plan_decision('qinglan-plan-A','adopt',accept_risks=True)
    professional='13900000117';scope='模拟林火任务核对'
    state.handle_professional_duty(eid,deputy_token=deputy,subject_id=professional,professional_scope=scope,
        active=True,expected_version=0,evidence='仅用于本轮模拟签审流程，非真实专业资质',reason='虚构演练角色',request_id='drill-professional')
    pro,error=state.member_login(professional,'123456')
    if error:raise ValueError(error)
    def publish(version):
        b=state.workspace.task_drafts_for_decision(decision,event_id=eid)
        for task in b.tasks:
            tag=f'{version}-{task.id}'
            from .task_review import assessment_for
            prior,_=assessment_for(state.workspace,b,task.id)
            state.handle_task_review_assessment(eid,deputy_token=deputy,decision_id=decision,task_id=task.id,
                expected_draft_version=b.version,expected_review_version=prior.reference.version if prior else 0,category='professional',
                reason='模拟林火作业，保留必要专业核对',basis_ref=refs[0],request_id='drill-category-'+tag)
            for purpose,payload in [('rules',dict(category='professional',required_scopes=[scope],safety_conditions=['演示通道与撤离条件已核对'])),
                ('safety',{'演示通道与撤离条件已核对':True}),('opinion',dict(conclusion='pass'))]:
                from .task_release import EVIDENCE,_key
                evidence_version=sum(r.reference.object_kind==EVIDENCE and r.reference.object_id==_key(task.id,purpose,scope)
                    for r in state.workspace.object_revisions.values())
                state.handle_task_release_action(eid,member_token=pro,action='evidence',decision_id=decision,task_id=task.id,
                    draft_version=b.version,purpose=purpose,scope=scope,payload=payload,evidence='本轮虚构演练的具名核对',
                    reason='模拟签审',expected_version=evidence_version,request_id='drill-'+purpose+'-'+tag)
        state.handle_task_release_action(eid,member_token=deputy,action='publish',decision_id=decision,draft_version=b.version,
            reason='模拟副指挥审定当前版，执行方须重新接收',request_id='drill-publish-'+str(version))
        from .formal_task_receipts import published
        return [item for item in published(state.workspace).values() if item[1]['decision_id']==decision and item[1]['draft_version']==version]
    def start(item,tag):
        from .task_relations import reference
        publication,data,task=item;oid=task['order_id']
        token,error=state.member_login(task['recipient_id'],'123456')
        if error:raise ValueError(error)
        state.handle_formal_task_receipt(eid,member_token=token,order_id=oid,publication_ref=publication.id,
            expected_version=0,action='received',basis='模拟队长确认接收本版目标、路线及反馈要求',request_id=tag+'-receive')
        for i,action in enumerate(('conditions','begin')):
            state.handle_task_relations(eid,member_token=token,action=action,task=reference(item),
                basis='模拟核对本版通道、撤离及协同条件后开始',expected_version=i,request_id=tag+'-'+action)
        return token
    first=publish(1)
    from .formal_task_receipts import published
    water=next(item for item in published(state.workspace).values() if item[1]['decision_id']!=decision)
    water_token=start(water,'drill-water')
    for i,action in enumerate(('progress','completed','verify')):
        verifier,error=state.member_login(water[2]['verifier_id'],'123456')
        if error:raise ValueError(error)
        state.handle_formal_task_feedback(eid,member_token=verifier if action=='verify' else water_token,
            order_id=water[2]['order_id'],publication_ref=water[0].id,expected_version=i,action=action,
            basis='【模拟】'+area+'前指饮水120瓶已按原配送记录实收，未重复收货；餐食照明另行跟进',
            overall_basis='只核验本饮水配送范围，不代表扑救目标或其他保障完成' if action=='verify' else '',
            request_id='drill-water-feedback-'+action)
    main=next(item for item in first if not item[2]['parent_id'])
    token=start(main,'drill-v1')
    state.handle_formal_task_feedback(eid,member_token=token,order_id=main[2]['order_id'],publication_ref=main[0].id,
        expected_version=0,action='difficulty',basis='【模拟】侦查D-02报告原通道受阻；暂停沿原路线推进，请求核查备用通道',request_id='drill-blocked')
    obstacle=_source(state,'blocked-route','原通道受阻与命令变更','模拟侦查报告D-02：原通道受阻；备用路线只作候选。人工复核后形成第2版，原回执不能替代新回执。',refs[:3],(base+timedelta(minutes=12)).isoformat())
    b=state.workspace.task_drafts_for_decision(decision,event_id=eid)
    specs={t.id:replace(t.spec,content=replace(t.spec.content,location=area+'备用通道（本轮模拟已复核）及原分段控制范围')) for t in b.tasks}
    state.workspace.revise_task_drafts(decision,event_id=eid,expected_version=b.version,common=b.common,specs=specs)
    state.persist_if_db()
    current=publish(2)
    # Independent child tasks finish first; overall verification cannot skip them.
    current.sort(key=lambda item:not bool(item[2]['parent_id']))
    for n,item in enumerate(current):
        token=start(item,'drill-v2-'+str(n));publication,data,task=item
        for i,action in enumerate(('progress','completed','verify')):
            verifier,error=state.member_login(task['verifier_id'],'123456')
            if error:raise ValueError(error)
            state.handle_formal_task_feedback(eid,member_token=verifier if action=='verify' else token,
                order_id=task['order_id'],publication_ref=publication.id,expected_version=i,action=action,
                basis='【模拟】'+task['spec']['title']+'：已按第2版复核范围回报；实际段/缺口与后续看守另行跟踪',
                overall_basis='【模拟】已逐项核对本版侦查、分段作业与本班看守；只核验该作业范围，火情继续活动' if action=='verify' and not task['parent_id'] else '',
                request_id='drill-feedback-'+str(n)+'-'+action)
    _review_and_steps(state,deputy,refs,obstacle,base)
    from .initial_map import collect
    assert collect(state,eid)['enabled']
    result=dict(schema=SCHEMA,simulated=True,event_id=eid,event_name=scene['event_name'],source_count=len(scene['records']),
        audit_count=state.workspace.version,original_scene_digest=scene['origin_digest'],
        firefighting_orders=[item[2]['order_id'] for item in current],original_route_order=main[2]['order_id'])
    (state.registry_path.parent/'fire-drill.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return state


def _review_and_steps(state,deputy,refs,obstacle,base):
    from . import nine_flow,review_meeting as meeting,review_closure as closure,review_causality as causal
    from .resource_demo import DEPUTY
    from .review_domain import ReviewIssue
    eid=state.active_event_id
    from .demo_templates import label
    template_id=state.fire_drill_scene.get('template_id','qinglan')
    meeting_owner=DEPUTY
    state.handle_review_owner_assign(meeting_owner,deputy_token=deputy,evidence='本轮模拟会议由副指挥集中记录',request_id='drill-review-owner')
    state._pending_review_actor=('owner',state.registry.library[DEPUTY].name,eid,deputy)
    note=meeting.record_note(state,deputy,dict(subject=label('北山通道变化复盘',template_id),provided_by='模拟侦查队与执行队',
        feedback='原通道受阻后，第1版停止沿用；第2版重新核对、发布、接收和核验。后续还需加强部署前通道核对。',
        evidence_refs=' '.join([obstacle.id]+refs[:3]),occurred_at=(base+timedelta(minutes=20)).isoformat(),request_id='drill-meeting'))
    issue='ISSUE-'+eid+'-qinglan-route'
    state.review_ledger.add_issue(ReviewIssue(issue,'原部署通道变化需重新核对（模拟）','作战部署','通道复核',eid,
        '模拟侦查回报与新旧任务版本','原通道受阻使旧部署条件失效；备用路线须复核并重新接收。',evidence_ids=(obstacle.id,)))
    state._save_reviews();state._mirror_review('ReviewIssuesGenerated',issue_ids=[issue])
    assessment=causal.record_assessment(state,deputy,dict(target_kind='issue',target_id=issue,support_refs=obstacle.id,
        counter_refs='',counter_note='后续已按新版办理，不应把本次变更解释为全部任务失败。',alternatives='通道可能在原部署后变化；未查清变化时间，不能据此定责。',
        missing_info='',verify_targets='本轮模拟侦查及版本记录；实际变化原因未认定',
        meeting_ref=note.id,meeting_finding='确认模拟流程中的通道变化问题，原因仍待核实',causal_layer='undetermined',expected_object_version='0',request_id='drill-issue-assessment'))
    state.handle_review_issue_status(issue,'confirmed',reason='仅确认本轮模拟通道变化与重核问题，不认定真实责任',assessment_ref=assessment.id)
    rect=closure.create_rect_draft(state,deputy,issue,'')
    responsible=state.registry.library['13900000118'];checker=state.registry.library['13900000117']
    closure.publish_rect(state,deputy,rect.rect_id,measures='部署前逐项核查主备通道；变化后修订版本、重新专业核对、接收，再开始执行。',
        owner_unit=responsible.unit,owner_person=responsible.name,deadline='2026-10-31',acceptance_standard='模拟主备通道核对清单及新版回执、独立核验均可追溯',
        verifier_name=checker.name,responsible_phone=responsible.phone,verifier_phone=checker.phone)
    closure.record_rect_feedback(state,deputy,rect.rect_id,provided_by=responsible.name,duty='模拟通道核对清单落实',
        content='模拟清单已补充并对照第2版任务及回执；真实通行情况仍待实地核实。',evidence=obstacle.id,completed=True)
    closure.record_rect_verification(state,deputy,rect.rect_id,verifier=checker.name,outcome='passed',
        finding='模拟独立验收的是核对措施和版本链，不是实际通道安全或整场灭火完成。',evidence=obstacle.id+';'+note.id)
    exp=closure.propose_experience(state,deputy,source_kind='rect',source_id=rect.rect_id)
    closure.revise_experience(state,deputy,exp.exp_id,content='通道条件变化后暂停沿用旧部署；修订、复核、重新接收和结果核验要引用同一版本。',
        applicability=label('本轮北山林火的通道变更模拟',template_id),limitations='不能代替真实地形、通道或消防专业判断',condition_tags='适用场景=通道变更',evidence=obstacle.id,reason='保留来源与适用边界')
    closure.decide_experience(state,deputy,exp.exp_id,target='confirmed',reason='只作为模拟流程经验收录')
    candidates=nine_flow.candidates(state);verifier_id=next(p for p in candidates if p!=DEPUTY)
    verifier,error=state.member_login(verifier_id,'123456')
    if error:raise ValueError(error)
    steps=[
        ('预警与初期处置','已接模拟上游报警，核对坐标、初报和重点目标，值班启动并指定副指挥。','completed',''),
        ('火线与通道研判','沿早上22条模拟空间资料研判，发现原通道受阻，比较三方案并核对备用路线。','completed',''),
        ('力量与保障调用','120人名册，32人已到场，另1队待扫码；饮水120瓶实收，可派状态须另报。','ongoing','餐食30份、照明4套缺口及可派状态待跟进。'),
        ('部署及版本变更','模拟方案A形成主/子任务；第1版通道受阻，第2版重新专业核对、发布和接收。','completed',''),
        ('分段控制与回报核验','第2版侦查、分段作业和本班看守均已模拟回报，由指定的另一位负责人核对；仅限任务范围。','completed',''),
        ('残火复查与看守','本班模拟巡查已回报；下一班接替及持续复查继续跟踪。','ongoing','下一班看守接班确认及后续复查结果。'),
        ('险情与救援核对','当前模拟巡查范围未发现伤困人员，不适用转运；新险情须重核，不推断整场无风险。','not_applicable',''),
        ('内部通报','模拟内部通报v1留存本次部署、变更、回报及保障遗留，由指定人员核对模拟接收；未发送对外新闻。','completed',''),
        ('阶段交接与复盘','交接本班任务结果、餐食照明、可派复核及下一班看守；事件继续活动，生成阶段复盘。','handover','餐食照明、可派复核、下一班看守与真实专业资料。')]
    observations=[]
    for i,(subject,feedback,result,remaining) in enumerate(steps):
        observations.append(nine_flow.observe(state,deputy,step=i,subject=subject+'（模拟）',feedback=feedback,
            location=label('青岚县北山原模拟火场及保障点',template_id),refs=[obstacle.id]+refs[:3],occurred_at=(base+timedelta(minutes=23+i)).isoformat(),
            expected_version=0,request_id='drill-step-observe-'+str(i),data_kind='simulated'))
    for i,(subject,feedback,result,remaining) in enumerate(steps):
        values=dict(scope=subject+'，仅本轮模拟范围',feedback=feedback,basis='本环节模拟记录、当前任务版本与具名回执；不代替真实专业验收',
            remaining=remaining,followup='由演示副指挥和指定接收人在下一次演练复核时跟进' if remaining else '',verifier_id=verifier_id,result=result)
        row=nine_flow.submit(state,deputy,step=i,values=values,refs=[observations[i].id,obstacle.id],expected_version=0,request_id='drill-step-submit-'+str(i),data_kind='simulated')
        nine_flow.verify(state,verifier,step=i,proposal_ref=row.id,expected_version=1,request_id='drill-step-verify-'+str(i),decision='accept')
    rid=state.handle_report_generate(stage=True);state.handle_report_edit(rid,state.review_ledger.reports[rid].content_md)
