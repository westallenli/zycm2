"""Actual receiving team and custody location are mandatory, never inferred."""
import secrets,math
from urllib.parse import parse_qs
from . import material_supply_receipts as receipts
from . import material_supply_contacts as contacts
from .material_withdrawals import withdrawal_record
from .org_model import mask_phone
from .workbench import esc


def body(state,token):
    actor=state.authorization_subject(token);member=state.member_session(token);visible=0
    sections=['<p>物资供应组现任组长报告分批实收，明确接收队伍及保管位置。批准量不入可用；已交出未收到的数量继续在途。仅良好实收计入接收队余额，损坏单列。收货不会自动接收、开始或关闭任务。</p>']
    for eid,event in sorted(state.registry.events.items()):
        if event.status=='registered':continue
        space,_,_=state._appointment_space(eid);leader=False
        try:contacts.require_leader(space,state.ledger,event_id=eid,actor=actor);leader=True
        except PermissionError:pass
        if not leader and eid not in state.deputy_events(token):continue
        visible+=1;state.material_supply_execution_view(eid,member_token=token)
        rows=receipts.history(space,state.ledger);all_rows=receipts.history(space,state.ledger,historical=True);orders=receipts.order_history(state.ledger,eid)
        snapshot=state.ledger.snapshots.get(eid)
        squads=[sid for sid in snapshot.squads if not withdrawal_record(state.ledger,event_id=eid,squad_id=sid)] if snapshot else []
        sections.append(f'<section style="overflow-wrap:anywhere"><h2>{esc(event.name)}</h2>')
        if event.status=='closed':
            sections.append('<p>事件已关闭：仅补登记本事件既有交出单的实际接收；不新增交出或作战任务。</p>')
            if leader:
                sections.append('<p><a href="/materials/closeout">返回关闭事件物资收尾 →</a></p>')
                sections.append(f'<p><a href="/material-custody?event_id={esc(eid)}">办理本事件撤离后装备接收、核验与结清 →</a></p>')
        for oid,order in orders.items():
            batches=[(r,v) for r,v in rows if v['order_id']==oid];total=math.fsum(v['received_quantity'] for r,v in batches);damage=math.fsum(v['damaged_quantity'] for r,v in batches)
            remaining=order['dispatched']-total
            sections.append(f'<article class="lz-card" data-supply-order="{esc(oid)}"><h3>{esc(order["source_snapshot"]["item"]["name"])} · {esc(order["source_snapshot"]["source"]["name"])}</h3>'
                f'<p>发布量：{order["quantity"]:g}；实际交出：{order["dispatched"]:g}；已实收：{total:g}；实收损坏：{damage:g}；良好实收：{total-damage:g}；仍在途：{remaining:g}。</p>')
            if leader and event.status in {'active','upgraded','closed'} and remaining>0 and squads:
                hidden=dict(event_id=eid,csrf=member.custody_csrf,order_id=oid,expected_version=sum(r.reference.object_id==oid for r,v in all_rows),request_id=secrets.token_urlsafe(18))
                sections.append('<form method="post" action="/material-supply/receipts">'+''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in hidden.items())
                    +'<label>本批接收队伍<select class="lz-input" name="squad_id" required aria-label="本批接收队伍"><option value="">请选择实际接收队伍</option>'
                    +''.join(f'<option value="{esc(sid)}">{esc(state.ledger.squads[sid].name)}</option>' for sid in squads)+'</select></label>'
                    +'<label>实际保管位置<input class="lz-input" name="location" required aria-label="实际保管位置"></label>'
                    +'<label>本批实际收到数量（含损坏）<input class="lz-input" name="quantity" inputmode="decimal" required aria-label="本批实际收到数量（含损坏）"></label>'
                    +'<label>其中损坏数量（无损坏填0）<input class="lz-input" name="damaged_quantity" inputmode="decimal" required aria-label="其中损坏数量（无损坏填0）"></label>'
                    +'<label>本批实收依据<input class="lz-input" name="basis" required aria-label="本批实收依据"></label><button class="lz-btn">记录本批实收</button></form>')
            for r,v in batches:
                sections.append(f'<p>第{r.reference.version}批 · {esc(state.ledger.squads[v["squad_id"]].name)} · 保管位置：{esc(v["location"])}；'
                    f'实收{v["received_quantity"]:g}、损坏{v["damaged_quantity"]:g}；具名报告人：{esc(mask_phone(r.actor_id))}；依据：{esc(v["basis"])}。</p>')
                if leader:
                    hidden=dict(event_id=eid,csrf=member.custody_csrf,order_id=oid,expected_version=sum(r.reference.object_id==oid for r,v in all_rows),request_id=secrets.token_urlsafe(18),corrects_id=r.id)
                    sections.append('<details><summary>追加更正本批实收</summary><p>填写本批更正后的总数；旧版保留。后续耗损、调拨、撤离或任务已使用来源时拒绝直接更正。</p><form method="post" action="/material-supply/receipts">'
                        +''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in hidden.items())
                        +''.join(f'<label>{label}<input class="lz-input" name="{key}" value="{esc(str(v[source]))}" required></label>' for key,source,label in (('squad_id','squad_id','实际接收队伍编号'),('location','location','保管位置'),('quantity','received_quantity','更正后实收总数'),('damaged_quantity','damaged_quantity','其中损坏')))
                        +'<label>本次更正依据<input class="lz-input" name="basis" required></label><label>更正理由<textarea class="lz-input" name="correction_reason" required></textarea></label><button class="lz-btn">追加实收更正</button></form></details>')
            history=[(r,v) for r,v in all_rows if v['order_id']==oid]
            sections.append('<details><summary>全部实收旧版与更正来源</summary>'+''.join(f'<p>记录{esc(r.id)}；具名报告人{esc(mask_phone(r.actor_id))}；记录时间{esc(r.times.recorded_at)}；{esc(r.content_json)}</p>' for r,v in history)+'</details>')
            if batches:
                from .material_inventory_projection import project
                inventory=project(space,state.ledger,event_id=eid,item_id=order['item_id'])
                for sid in sorted({v['squad_id'] for r,v in batches}):
                    balance=next(b for b in inventory.team_balances if b.squad_id==sid)
                    sections.append(f'<p>{esc(state.ledger.squads[sid].name)}当前队伍账面余额：{balance.available:g} {esc(order["source_snapshot"]["item"]["unit"])}。</p>')
            sections.append('</article>')
        sections.append('</section>')
    if not visible:raise PermissionError('没有本事件后勤或副指挥长职责。')
    return ''.join(sections)


def post(handler,state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    member=state.member_session(handler._member_cookie())
    if member is None:handler._respond(403,'<h1>403</h1>');return
    try:length=request_length(handler,16384)
    except ValueError:handler._respond(413,'<h1>413</h1>');return
    try:
        if handler.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':raise ValueError('表单格式不正确。')
        handler.connection.settimeout(15)
        form=parse_qs(handler.rfile.read(length).decode('utf-8'),keep_blank_values=True,encoding='utf-8',errors='strict',max_num_fields=32)
        required={'event_id','csrf','order_id','expected_version','request_id','squad_id','location','quantity','damaged_quantity','basis'}
        if set(form) not in (required,required|{'corrects_id','correction_reason'}) or any(len(v)!=1 for v in form.values()):raise ValueError('字段缺失、重复或不受支持。')
        values={k:v[0] for k,v in form.items()}
        if not secrets.compare_digest(values.pop('csrf').encode(),member.custody_csrf.encode()):raise PermissionError('会话已变化。')
        raw=values['expected_version']
        if not raw.isascii() or not raw.isdigit() or len(raw)>9:raise ValueError('批次版本须为非负整数。')
        values['expected_version']=int(raw)
        from .material_supply_execution import number
        for key in ('quantity','damaged_quantity'):
            raw=values[key]
            if not raw or raw!=raw.strip() or len(raw)>64:raise ValueError('实际数量须明确填写，无损坏填0。')
            values[key]=number(float(raw),positive=key=='quantity' and not values.get('corrects_id'))
        state.handle_material_supply_receipt(values.pop('event_id'),member_token=handler._member_cookie(),**values)
    except PermissionError:handler._respond(403,'<h1>403</h1>');return
    except (ValueError,PanelActionError,UnicodeError) as error:handler._respond(409,'<h1>409</h1><p>'+esc(str(error))+'</p>');return
    handler._redirect('/material-supply/receipts')
