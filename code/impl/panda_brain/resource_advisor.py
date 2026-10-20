"""Event-scoped public/extra resource forecasts and grounded supply candidates."""
from datetime import datetime, timezone
import json
import math
from .resource_maintenance import scope, journal
from .resource_directory import enabled

KIND = 'resource_planning_context'


def require_planner(state, token, event_id):
    actor=state.authorization_subject(token)
    space,_,_=state._appointment_space(event_id)
    from .personnel_management import require
    try: require(space,actor)
    except PermissionError:
        from .material_supply_contacts import require_leader
        require_leader(space,state.ledger,event_id=event_id,actor=actor)
    return actor


def context_rows(space, ledger):
    from . import audited_records as ar
    rows=ar.records(space,KIND)
    for index,r,v in rows:
        if (r.source!='具名物资保障地点与时段' or r.reference.object_id!='planning-context'
                or set(v)!={'latitude','longitude','planning_hours','buffer_hours','basis'}):
            raise ValueError('物资保障位置与预测时段来源异常。')
        validate_context(v)
        prefix=ar.prefix(space,index)
        from .personnel_management import require
        try: require(prefix,r.actor_id)
        except PermissionError:
            from .material_supply_contacts import require_leader
            require_leader(prefix,ledger,event_id=space.event_id,actor=r.actor_id)
    return rows


def validate_context(values):
    if set(values)!={'latitude','longitude','planning_hours','buffer_hours','basis'}: raise ValueError('预测上下文字段不正确。')
    for key,bound in [('latitude',90),('longitude',180),('planning_hours',100000),('buffer_hours',100000)]:
        value=values[key]
        if value is not None and (type(value) not in {int,float} or not math.isfinite(value)
                or abs(value)>bound or key.endswith('hours') and value<0):
            raise ValueError('经纬度、时段或安全提前量不正确，未知留空。')
    if (values['latitude'] is None)!=(values['longitude'] is None): raise ValueError('保障接收点经纬度须成对填写。')
    if not isinstance(values['basis'],str) or not values['basis'].strip(): raise ValueError('须说明位置和预测时段来源。')


def record_context(state,token,event_id,*,expected_version,request_id,**values):
    from . import audited_records as ar
    from .persistence import save_workspace
    from copy import deepcopy
    actor=require_planner(state,token,event_id); validate_context(values)
    space,target,active=state._appointment_space(event_id,for_write=True)
    context_rows(space,state.ledger); candidate=deepcopy(space)
    r=ar.append(candidate,kind=KIND,object_id='planning-context',source='具名物资保障地点与时段',
        actor=actor,values=values,expected_version=expected_version,request_id=request_id)
    if candidate.events!=space.events:
        save_workspace(candidate,target)
        if active: state.workspace=candidate
    return r


def distance_km(destination, location):
    if destination is None or location.get('latitude') is None or location.get('longitude') is None: return None
    lat,lon=destination
    a,b=math.radians(lat),math.radians(location['latitude'])
    v=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(location['longitude']-lon)/2)**2
    return 6371.0088*2*math.asin(math.sqrt(min(1,max(0,v))))


def candidates(ledger,item_id,required,destination,deadline_hours=None,*,stocks=None,orders=None,stock_error=False):
    item=ledger.items[item_id]; points=[]; companies=[]
    def location(kind,identifier):
        meta=ledger.resource_metadata.get(kind,{}).get(identifier,{})
        d=distance_km(destination,meta)
        lead=meta.get('lead_hours')
        return dict(distance_km=d,lead_hours=lead,location_basis=meta.get('basis',''),
                    timely=None if lead is None or deadline_hours is None else lead<=deadline_hours)
    for p in ledger.points.values():
        if not enabled(ledger,'points',p.point_id): continue
        if stock_error: continue
        if p.open_status!='开放' or item_id not in p.supplies: continue
        qty=p.supplies[item_id]
        stock=(stocks or {}).get((p.point_id,item_id))
        quantity_basis='登记库存，当前实物待核'
        if stock is not None:
            held=math.fsum(v['quantity']-v['dispatched'] for v in (orders or {}).values()
                if v['source_kind']=='point' and v['source_id']==p.point_id and v['item_id']==item_id and not v['cancelled'])
            qty=max(0,stock['quantity']-held)
            quantity_basis='具名核实库存扣除已交出及未交出占用；版本 '+str(stock['stock_version'])
        if qty is not None and (type(qty) not in {int,float} or not math.isfinite(qty) or qty<=0): continue
        points.append(dict(id=p.point_id,name=p.name,quantity=qty,location=p.location,
            contact=p.contact_name,phone=p.contact_phone,updated_at=p.updated_at,
            sufficient=None if qty is None or required is None else qty>=required,
            source=p.source_word(),quantity_basis=quantity_basis,**location('points',p.point_id)))
    # Name/service matching is explicit; unmatched directory entries remain visible separately.
    for c in ledger.companies.values():
        if not enabled(ledger,'companies',c.company_id): continue
        if not item.name or item.name.casefold() not in c.services.casefold(): continue
        companies.append(dict(id=c.company_id,name=c.name,services=c.services,region=c.region,
            contact=c.contact_name,phone=c.contact_phone,source=c.source,updated_at=c.source_updated_at,
            quantity=None,**location('companies',c.company_id)))
    def rank(v):
        return (v.get('timely') is False,v.get('sufficient') is False,v['distance_km'] is None,
                v['distance_km'] if v['distance_km'] is not None else math.inf,v['id'])
    return sorted(points,key=rank),sorted(companies,key=rank)


def assess(state,event_id):
    from .material_inventory_reads import read_event
    from . import task_material_inputs as inputs
    from .task_sop import flow_issue
    from .models import OrderStatus
    if (state.active_event_id!=event_id or state.workspace.event_id!=event_id
            or event_id not in state.registry.events or state.workspace.verify_audit_chain()):
        raise ValueError('物资参谋与当前火情或审计来源不一致。')
    if state.ledger is None:raise ValueError('人员物资台账尚不可用，请核对原始资料。')
    journal(state.ledger)
    contexts=context_rows(state.workspace,state.ledger)
    context=contexts[-1][2] if contexts else {}
    destination=(context['latitude'],context['longitude']) if context.get('latitude') is not None else None
    record_rows=inputs._records(state.workspace,event_id)
    latest={}
    for r in record_rows: latest[r.reference.object_id]=r
    report_by_item={}; issues=[]
    for r in latest.values():
        v=json.loads(r.content_json); oid=v['order_id']; order=state.workspace.orders.get(oid)
        if (order is None or order.status not in {OrderStatus.CONFIRMED,OrderStatus.ACTIVE}
                or flow_issue(state.workspace,oid,event_id)):
            continue
        flow=state.workspace.command_flows.get(oid)
        if not flow or flow.content_version!=v['task_version']:
            issues.append('任务 '+oid+' 已改版，原物资需求需重评。'); continue
        report_by_item.setdefault(v['item_id'],[]).append((r,v))
    try: views=read_event(state.workspace,state.ledger,event_id,extra_items=report_by_item)
    except ValueError:
        views={}; issues.append('当前现场库存或到场来源待核实，不能把未知库存当作零。')
    from .material_supply_execution import inspect as inspect_supply
    stocks={}; orders={}; stock_error=False
    try:
        from .resource_maintenance import event_spaces
        spaces,unavailable=event_spaces(state)
        if unavailable: raise ValueError('历史来源空间缺失，库存完整性待核。')
        _,stocks,orders=inspect_supply(state.ledger,spaces)
    except ValueError:
        stock_error=True
        issues.append('共享库存或占用来源待核对，暂不推荐物资库，不回退登记数量。')
    rows=[]
    item_ids=set(report_by_item)|set(views)
    for iid in sorted(item_ids):
        item=state.ledger.items[iid]
        if scope(state.ledger,iid)=='standard': continue
        reports=report_by_item.get(iid,[]); view=views.get(iid)
        # Carry baselines belong to the independently provisioned teams, including after transfers.
        # Public/extra stock is accounted only when its receipt and ownership are traceable.
        available=None
        if view is not None and view.book_available is not None:
            from .material_withdrawals import withdrawal_record
            active_baseline=sum(b.arrival_base for b in view.team_balances if not withdrawal_record(
                state.ledger,event_id=event_id,squad_id=b.squad_id))
            latest_losses={}
            for record in state.ledger.flow_records.values():
                if record.reference.event_id==event_id and record.reference.object_kind=='material_loss':
                    v=json.loads(record.content_json)
                    old=latest_losses.get(record.reference.object_id)
                    if v['item_id']==iid and (old is None or record.reference.version>old[0].reference.version):
                        latest_losses[record.reference.object_id]=(record,v)
            historical_kit_loss=sum(v['amount'] for _,v in latest_losses.values()
                if v.get('inventory_scope')!='public_extra' and not withdrawal_record(state.ledger,event_id=event_id,squad_id=v['squad_id']))
            available=max(0.,view.book_available-active_baseline+historical_kit_loss)
            if scope(state.ledger,iid)=='public' and not any(l.item_id==iid for l in state.ledger.carry_lines.values()):
                available=view.book_available
        demand=0.; projected=0.; rate=0.; rate_known=bool(reports); missing=[]; sources=[]; known_demand=bool(reports)
        known_forecast=bool(reports); metadata=state.ledger.resource_metadata.get('items',{}).get(iid,{})
        for r,v in reports:
            sources.append(dict(id=r.id,order_id=v['order_id'],source=r.source,
                usage_kind=v['usage_kind'],recorded_at=r.times.recorded_at))
            need=v['demand_quantity']; future=v['projected_consumption']
            hours=context.get('planning_hours') if context.get('planning_hours') is not None else v['duration_hours']
            if need is None: known_demand=False
            else: demand+=need
            per=metadata.get('rate_per_person_hour')
            task_rate=(per*v['force_count'] if per is not None and v['force_count'] is not None else
                       future/hours if future is not None and hours and hours>0 else None)
            estimate=task_rate*hours if task_rate is not None and hours is not None else future
            if estimate is None: known_forecast=False
            else: projected+=max(need or 0,estimate)
            if task_rate is not None: rate+=task_rate
            else: rate_known=False
        if not reports: missing.append('缺少已确认任务的物资需求及持续时间依据')
        if not known_demand: missing.append('任务当前需求数量待核实')
        if not known_forecast: missing.append('预计用量、投入人数、时长或用量参数待补充')
        if available is None: missing.append('当前公共/额外物资可用数量待核实')
        current=max(0,demand-available) if known_demand and available is not None else None
        forecast=max(0,projected-available) if known_forecast and available is not None else None
        coverage=available/rate if available is not None and rate_known and rate>0 else None
        buffer=context.get('buffer_hours')
        points,companies=candidates(state.ledger,iid,current or forecast,destination,coverage,stocks=stocks,orders=orders,stock_error=stock_error)
        leads=[v['lead_hours'] for v in points+companies if v['lead_hours'] is not None]
        lead=min(leads) if leads else None
        urgent=forecast is not None and forecast>0 and coverage is not None and lead is not None and coverage<=lead+(buffer or 0)
        status=('当前短缺' if current is not None and current>0 else
                '预计短缺' if forecast is not None and forecast>0 or urgent else
                '依据待补' if missing else '当前估算充足')
        if destination is None: missing.append('保障接收点坐标待补，暂不声明就近')
        rows.append(dict(item_id=iid,name=item.name,unit=item.unit,scope=scope(state.ledger,iid),
            available=available,current_need=demand if known_demand else None,
            forecast_need=projected if known_forecast else None,current_gap=current,forecast_gap=forecast,
            coverage_hours=coverage,status=status,missing=missing,sources=sources,
            points=points,companies=companies,in_transit=view.in_transit if view else None,
            context_ref=contexts[-1][1].id if contexts else '',parameter_basis=metadata.get('basis',''),late_supply_risk=urgent))
    priorities={'当前短缺':0,'预计短缺':1,'依据待补':2,'当前估算充足':3}
    rows.sort(key=lambda r:(priorities[r['status']],r['item_id']))
    return dict(event_id=event_id,generated_at=datetime.now(timezone.utc).isoformat(),context=context,
        rows=rows,issues=issues,standard_excluded=True)
