"""Explicit, isolated fictional demonstration. Never loaded by ordinary launchers."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
from . import resource_directory as directory

DUTY='13900000119'
DEPUTY='13900000120'
SECRET='resource-demo-local'


def open_state(root, *, secret=None):
    from .auth import GeneralEntryAuth
    from .workspace import ScenarioWorkspace
    from .panel import PanelState
    from . import workbench
    root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=True)
    workbench.SNAPSHOT_DIR=root/'snapshots'
    # 指挥员入口口令：默认沿用本模块的演示常量；显式传入时以传入为准
    # （开发版用 123456，与老交付的 resource-demo-local 区分）。
    auth=GeneralEntryAuth(secret=secret or SECRET)
    state=PanelState(ScenarioWorkspace('resource-demo',general_auth=auth),auth=auth,
        registry_path=root/'registry.json',materials_path=root/'materials.json',reviews_path=root/'reviews.json',
        imagery_path=root/'imagery.json',imports_path=root/'imports.json',alarm_inbox_path=root/'alarms.json',upload_dir=root/'uploads',duty_subjects=(DUTY,))
    marker=root/'resource-demo.json'
    if marker.exists():
        state.resource_demo=json.loads(marker.read_text(encoding='utf-8'))
        from .persistence import load_workspace
        eid=state.resource_demo['event_id'];state.active_event_id=eid
        state.db_path=Path(state.registry.events[eid].snapshot_path)
        state.workspace=load_workspace(state.db_path,general_auth=auth)
    from .fire_drill import load_scene
    load_scene(state)
    return state


def seed(root, *, fire_scene=False, template_id='qinglan'):
    from .demo_templates import template,relocate,label,NOTICE
    selected=template(template_id)
    if template_id!='qinglan' and not fire_scene:raise ValueError('秦岭模板仅用于明确的林火模拟。')
    root=Path(root).resolve()
    if root.exists() and any(root.iterdir()): raise ValueError('演示种子只允许写入全新空目录。')
    state=open_state(root)
    from .models import PersonRecord
    # Exactly 120 identities; two named operators also belong to the reserve squadrons.
    rows=[]
    for i in range(120):
        team=i//10; city=['示范甲市','示范乙市','示范丙市'][team//4]
        unit=city+['森林消防支队','应急救援中心'][(team%4)//2]
        name='演示'+['林','陈','张','王','赵','周','吴','郑','沈','许'][i%10]+str(i+1).zfill(3)
        phone='1390000'+str(i+1).zfill(4)
        if phone==DUTY:name='演示值班员'
        if phone==DEPUTY:name='演示副总指挥'
        rows.append([name,phone,city,unit,unit+['一中队','二中队'][team%2], '队长' if i%10==0 else '队员','是' if i%10==0 else '否'])
    # Bootstrap only the duty identity; all directory imports pass the named daily API.
    state.registry.import_person_library([PersonRecord(name='演示值班员',phone=DUTY,unit=rows[118][3],home_position='队员')]);state._save_registry()
    duty,error=state.member_login(DUTY,'123456')
    if error:raise ValueError(error)
    def change(op,details):
        n=len(state.ledger.daily_resource_records)
        return state.handle_daily_resources(member_token=duty,operation=op,details=details,basis='虚构演示资料，非真实名录或现场事实',expected_version=n,request_id='demo-daily-'+str(n))
    content=directory.csv_text(directory.PEOPLE_HEADERS,rows)
    change('directory_import',dict(data_type='person_directory',content=content,content_sha256=hashlib.sha256(content.encode()).hexdigest()))
    for name,unit,scope in [('背负式水泵','台','standard'),('消防水带','盘','standard'),('防护装备','套','standard'),('饮用水','瓶','public'),('应急餐食','份','public'),('照明灯组','套','extra')]:
        change('catalog',dict(data_type='item',row=dict(name=name,unit=unit,quantity='0',category='演示物资',spec='演示规格')))
        iid=next(i.item_id for i in state.ledger.items.values() if i.name==name)
        change('metadata',dict(kind='items',identifier=iid,values=dict(scope=scope,rate_per_person_hour=None,basis='演示任务估算参数')))
    items={i.name:i.item_id for i in state.ledger.items.values()}
    squads=sorted(state.ledger.squads.values(),key=lambda s:min(s.member_phones))
    for s in squads:
        for name,count in [('背负式水泵',2),('消防水带',20),('防护装备',10)]:
            change('catalog',dict(data_type='carry',row=dict(squad=s.name,item_name=name,quantity=str(count),unit=state.ledger.items[items[name]].unit)))
    for i,name in enumerate(['北山前置物资库' if fire_scene else '东岭前置物资库','县域应急储备库' if fire_scene else '北山应急储备库','城南综合物资库','西河联勤物资库']):
        name=label(name,template_id)
        change('catalog',dict(data_type='supply_point',row=dict(name='演示'+name,location='示范甲市虚构保障点'+str(i+1),org='演示应急保障中心',contact_name='演示库管'+str(i+1),contact_phone='1390000100'+str(i+1))))
        point=next(p for p in state.ledger.points.values() if p.name=='演示'+name)
        for material,qty in [('饮用水',1000+i*500),('应急餐食',300+i*100),('照明灯组',8+i*4)]:
            change('catalog',dict(data_type='stock',row=dict(point=point.name,item_name=material,quantity=str(qty))))
        latitude,longitude=relocate((30.10 if fire_scene else 30)+(i+1)*.025,(103.53 if fire_scene else 110)+(i+1)*.015,template_id)
        change('metadata',dict(kind='points',identifier=point.point_id,values=dict(latitude=latitude,longitude=longitude,lead_hours=.5+i*.5,basis='虚构坐标和运输时间，仅供演示')))
    for i,(name,service) in enumerate([('山泉配送','饮用水'),('青禾供水','饮用水'),('暖食保障','应急餐食'),('惠民餐饮','应急餐食'),('明途照明','照明灯组'),('顺达装备','照明灯组、饮用水')]):
        change('catalog',dict(data_type='company',row=dict(name='演示'+name+'公司',services=service,region='示范甲市及周边',contact_name='演示联络员'+str(i+1),contact_phone='1390000200'+str(i+1),source='虚构外协名录，仅供演示',source_updated_at=datetime.now(timezone.utc).isoformat())))
        c=next(c for c in state.ledger.companies.values() if c.name=='演示'+name+'公司')
        latitude,longitude=relocate((30.14 if fire_scene else 30.04)+i*.018,(103.56 if fire_scene else 110.03)+i*.01,template_id)
        change('metadata',dict(kind='companies',identifier=c.company_id,values=dict(latitude=latitude,longitude=longitude,lead_hours=.75+i*.2,basis='虚构公司坐标及送达估算')))
    if fire_scene:
        from .fire_demo import confirmed_demo_message
        message=confirmed_demo_message(source_record_id='QINGLAN-CONTINUOUS-DEMO' if template_id=='qinglan' else 'QINLING-NORTH-DEMO',
            timestamp=datetime.now(timezone.utc).isoformat(),report_no='DEMO-QINGLAN-CONTINUOUS' if template_id=='qinglan' else 'DEMO-QINLING-NORTH')
        if template_id!='qinglan':
            message['data'].update(name=selected['event_name'],lat=selected['anchor'][0],lon=selected['anchor'][1],
                location='秦岭北侧候选区域（真实影像上的虚构演练）',adminRegion='秦岭北侧山地（演示区域，行政归属未核）',remark=NOTICE)
        state.handle_confirmed_fire_event(message)
        event=next(iter(state.registry.events.values()))
    else:
        event=state.registry.create_event(name='东岭森林火情（人员物资演示）',report_no='DEMO-RESOURCE-20261017',discovered_at=datetime.now(timezone.utc).isoformat(),source='虚构演示火情',location='示范甲市东岭虚构火场')
    eid=event.event_id
    for index,s in enumerate(squads[:4],1):
        for phone in s.member_phones: state.registry.add_roster_entry(eid,phone,pos_code='pjjz-'+str(index))
    state._save_registry()
    state.handle_event_action(eid,'start',duty_token=duty)
    deputy,error=state.member_login(DEPUTY,'123456')
    if error:raise ValueError(error)
    state.handle_deputy_appointment(eid,duty_token=duty,subject_id=DEPUTY,evidence='模拟任命用于演示',request_id='demo-appoint')
    state.handle_select_squads(eid,deputy_token=deputy,squad_ids=[s.squad_id for s in squads[:4]],channel='phone',request_id='demo-callouts')
    qr=next(q.qr_id for q in state.registry.qrs.values() if q.event_id==eid)
    for s in squads[:3]:
        captain,error=state.member_login(s.leader_phone,'123456')
        if error:raise ValueError(error)
        state.handle_captain_scan(eid,captain_token=captain,squad_id=s.squad_id,qr_id=qr)
        state.handle_captain_arrival(eid,captain_token=captain,squad_id=s.squad_id,mode='initial',phones=[],request_id='demo-arrival-'+s.squad_id)
    state.login(SECRET);state.handle_enter_event(eid);state.db_path=Path(state.registry.events[eid].snapshot_path)
    # Group bindings are generated by the standard captain arrival flow below.
    from .models import ExecutionOrder,OrderStatus
    from .tactical_map import CommandFlow
    from .persistence import save_workspace
    space=state.workspace
    space.orders['demo-frontline']=ExecutionOrder('demo-frontline','demo-decision','demo-option',label(('北山林火' if fire_scene else '东岭')+'现场保障（模拟任务）',template_id),status=OrderStatus.CONFIRMED,confirmed_by=DEPUTY)
    flow=CommandFlow(order_id='demo-frontline',event_id=eid,content_version=1,target='保障当前已到场30人，预测未来4小时用量（模拟）')
    space.command_flows['demo-frontline']=flow;space.append_event('CommandFlowOpened',order_id='demo-frontline',event_id=eid,flow=asdict(flow));save_workspace(space,state.db_path)
    # A real domain supply receipt is seeded, so prediction starts from traceable public inventory.
    seed_receipt(state,eid,deputy,qr,squads,items['饮用水'],120)
    for name,demand,future in [('饮用水',60,240),('应急餐食',30,90),('照明灯组',4,6)]:
        iid=items[name];view=state.task_material_inputs(eid,member_token=deputy,order_id='demo-frontline',item_id=iid)
        state.handle_task_material_inputs(eid,member_token=deputy,order_id='demo-frontline',item_id=iid,expected_version=view.version,expected_source_digest=view.source_digest,
            material_relation_source='模拟'+event.name+'现场保障任务',force_count=30,force_source='模拟签到三支中队共30人',duration_hours=4,duration_source='模拟未来4小时作业',
            demand_quantity=demand,projected_consumption=future,usage_source='虚构演示用量，未经专业核定',usage_kind='simulated',evidence='演示缺口场景',occurred_at='',reason='',request_id='demo-demand-'+iid)
    latitude,longitude=relocate(30.11 if fire_scene else 30,103.55 if fire_scene else 110,template_id)
    state.handle_resource_planning(eid,member_token=deputy,expected_version=0,request_id='demo-planning',latitude=latitude,longitude=longitude,planning_hours=4,buffer_hours=1,basis='虚构保障接收点和预测时段')
    marker=dict(simulated=True,event_id=eid,waiting_squad=squads[3].squad_id,called_squads=[s.squad_id for s in squads[:4]],created_at=datetime.now(timezone.utc).isoformat(),scenario='人员库、扫码签到、到场标配、物资预警联动演示')
    (root/'resource-demo.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2),encoding='utf-8');state.resource_demo=marker
    from .resource_directory_page import KINDS,columns,data_rows,xlsx
    samples=root.parent/'示例表';samples.mkdir(exist_ok=True)
    for kind,label in KINDS.items():
        for blank in (False,True):
            values=[] if blank else data_rows(state,kind);suffix='空白模板' if blank else '模拟示例'
            (samples/(label+'_'+suffix+'.xlsx')).write_bytes(xlsx(columns(kind),values))
            (samples/(label+'_'+suffix+'.csv')).write_text(directory.csv_text(columns(kind),values),encoding='utf-8-sig')
    return state


def seed_receipt(state,eid,deputy,qr,squads,iid,quantity):
    """Follow normal named contact -> review -> release -> dispatch -> receipt gates."""
    from .models import Option
    from .task_drafts import PlanTaskBlueprint,TaskDraftSpec,TaskCommonContent
    from .material_supply_contacts import _digest
    from .material_supply_execution import inspect
    logistics='13900000118'; reviewer='13900000117'
    destination='北山前指保障点（模拟）' if state.registry.events[eid].lat is not None else '演示东岭保障点'
    if '秦岭' in state.registry.events[eid].name:destination='秦岭前指保障点（模拟）'
    # These reserve squad members serve as temporary field staff in this demo.
    # Remove them from the uncalled reserve roster via daily maintenance before check-in.
    reserve=next(s for s in state.ledger.squads.values() if logistics in s.member_phones)
    # Preserve the 120-person library; reserve squad retains its captain and eight members.
    duty,_=state.member_login(DUTY,'123456')
    details=dict(name=reserve.name,squad_id=reserve.squad_id,member_phones=[p for p in reserve.member_phones if p not in {logistics,reviewer}],leader_phone=reserve.leader_phone,headcount_planned=10,contact='模拟两人临时支援前指')
    state.handle_daily_resources(member_token=duty,operation='squad',details=details,basis='模拟后勤及专业核对人员从预备中队抽调',expected_version=len(state.ledger.daily_resource_records),request_id='demo-reserve-adjust')
    tokens={}
    for phone in (logistics,reviewer):
        tokens[phone],error=state.member_login(phone,'123456')
        if error:raise ValueError(error)
        state.handle_temporary_scan(eid,member_token=tokens[phone],qr_id=qr)
        state.handle_temporary_assign(eid,deputy_token=deputy,phones=[phone],pos_code='log-material',request_id='demo-assign-'+phone)
        state.handle_temporary_arrival(eid,member_token=tokens[phone])
    state.handle_temporary_group_leader(eid,deputy_token=deputy,pos_code='log-material',phone=logistics,evidence='模拟物资供应组长指定',request_id='demo-log-leader')
    scope='模拟物资运输核对'
    state.handle_professional_duty(eid,deputy_token=deputy,subject_id=reviewer,professional_scope=scope,active=True,expected_version=0,evidence='模拟职责指定',reason='演示补给流程',request_id='demo-reviewer')
    point=next(iter(state.ledger.points.values()));token=tokens[logistics]
    digest=state.material_supply_contacts(eid,member_token=token,item_id=iid,source_kind='point',source_id=point.point_id).current_source_digest
    at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat();need=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    contact=state.handle_material_supply_contact(eid,member_token=token,item_id=iid,source_kind='point',source_id=point.point_id,expected_source_digest=digest,available_quantity=1000,promised_delivery_at=at,evidence='虚构电话联系，仅供演示',request_id='demo-contact',expected_version=0)
    admin=state.login(SECRET)
    state.workspace.add_option(Option('demo-water-plan','模拟饮水保障','演示公共物资实收',(),(),task_blueprint=PlanTaskBlueprint(main=TaskDraftSpec(title='模拟饮水配送',objective='配送公共饮用水',completion_standard='模拟收货清点',responsible_group_id=squads[0].squad_id,verifier_id=squads[1].leader_phone,content=TaskCommonContent(time_requirement='模拟2小时内',feedback_requirement='模拟具名收货',safety_measures='模拟运输安全')))))
    # 注意：这个选项是演示任务蓝本，种子里后面还要用它生成收货记录，不能在这里删。
    # 它不出现在"方案建议"里，由 run_my_demo 启动时清理（那时种子已跑完）。
    decision=state.handle_plan_decision('demo-water-plan','adopt');bundle=state.workspace.task_drafts_for_decision(decision,event_id=eid);task=bundle.tasks[0].id
    state.handle_material_supply_draft(eid,session_token=admin,decision_id=decision,task_id=task,item_id=iid,expected_bundle_version=bundle.version,expected_bundle_digest=_digest(asdict(bundle)),expected_item_digest=_digest(asdict(state.ledger.items[iid])),expected_version=0,requested_quantity=quantity,quantity_basis='模拟120瓶',needed_by=need,contact_id=contact.id,destination_hint=destination,evidence='模拟配送单',request_id='demo-attachment')
    bundle=state.workspace.task_drafts_for_decision(decision,event_id=eid)
    state.handle_task_review_assessment(eid,deputy_token=deputy,decision_id=decision,task_id=task,expected_draft_version=bundle.version,expected_review_version=0,category='routine',reason='模拟日常配送',basis_ref='演示分类',request_id='demo-assessment')
    for purpose,payload in [('rules',dict(category='routine',required_scopes=[],safety_conditions=['模拟运输条件已核对'])),('safety',{'模拟运输条件已核对':True})]:
        state.handle_task_release_action(eid,member_token=tokens[reviewer],action='evidence',decision_id=decision,task_id=task,draft_version=bundle.version,purpose=purpose,scope=scope,payload=payload,evidence='模拟核对',reason='演示通过',expected_version=0,request_id='demo-'+purpose)
    state.handle_material_supply_execution(eid,member_token=token,action='verify_stock',point_id=point.point_id,item_id=iid,quantity=1000,basis='虚构库存清点演示',expected_stock_version=0,expected_source_digest=digest,request_id='demo-stock')
    state.handle_task_release_action(eid,member_token=deputy,action='publish',decision_id=decision,draft_version=bundle.version,reason='模拟审定配送',request_id='demo-publish')
    oid=next(iter(inspect(state.ledger,state.supply_event_spaces())[2]))
    state.handle_material_supply_execution(eid,member_token=token,action='dispatch',order_id=oid,quantity=quantity,basis='模拟交出',expected_version=1,request_id='demo-dispatch')
    state.handle_material_supply_receipt(eid,member_token=token,order_id=oid,squad_id=squads[0].squad_id,location=destination,quantity=quantity,damaged_quantity=0,basis='虚构收货记录，仅供演示',expected_version=0,request_id='demo-receipt')


def panel(state,token=None,*,public=False):
    if not getattr(state,'resource_demo',None):return ''
    from .workbench import esc
    from .resource_pages import table
    from .event_qr import qr_svg,entry_url
    eid=state.resource_demo['event_id'];snapshot=state.ledger.snapshots.get(eid)
    arrived={sid:s for sid,s in (snapshot.squads.items() if snapshot else [])}
    waiting=state.ledger.squads[state.resource_demo['waiting_squad']]
    accounts=[('日常维护',DUTY),('火情总览',DEPUTY),('待到场队长',waiting.leader_phone)]
    title='北山林火连贯演示 · 人员与保障' if getattr(state,'fire_drill_scene',None) else '人员与扫码签到演示'
    if (getattr(state,'fire_drill_scene',None) or {}).get('template_id')=='qinling-north-v1':title='秦岭林火连贯演示 · 人员与保障'
    out='<section class="lz-card resource-section" style="margin:20px;padding:20px;border:1px solid #b8cede;border-radius:12px;background:#fff;color:#16334b"><h2>'+title+'</h2><p>虚构演示数据；人员、队伍、物资均使用同一套名册。</p>'
    if public:
        out+=table(('演示身份','账号','密码'),[(role,phone,'123456') for role,phone in accounts])
        return out+'<p>登录待到场队长后，进入“队长扫码与到场确认”。</p></section>'
    member=state.member_session(token)
    visible=None if member is None or member.phone in {DUTY,DEPUTY} else {s.squad_id for s in state.ledger.squads.values() if member.phone in s.member_phones}
    out+=table(('中队','名册人数','签到到场','标配'),[(s.name,len(s.member_phones),len(arrived[s.squad_id].arrived_phones) if s.squad_id in arrived else '待扫码确认','默认齐全 · 已计入' if s.squad_id in arrived else '到场后计入') for s in (state.ledger.squads[sid] for sid in state.resource_demo['called_squads']) if visible is None or s.squad_id in visible])
    qrs=[q for q in state.registry.qrs.values() if q.event_id==eid and state.registry.qr_status(q)=='有效']
    if qrs:
        qr=qrs[-1];out+='<details open><summary>本火情签到二维码 · '+esc(qr.qr_id)+'</summary><div style="width:140px">'+qr_svg(qr.qr_id,entry_url=entry_url(state,eid,qr.qr_id))+'</div><p>二维码编号：<b>'+esc(qr.qr_id)+'</b>。登录后由本人确认载入名册，再确认到场才计人数和标配。</p></details>'
    else:out+='<p>签到码已到期，请值班员在火情详情补发。</p>'
    out+='<p><a class="lz-btn" href="/personnel/arrival">队长扫码与到场确认</a> <a class="lz-btn lz-btn2" href="/materials/event">查看本火情人员与物资</a></p></section>'
    return out
