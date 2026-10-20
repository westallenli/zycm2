"""Named stock verification and actual dispatch; GET never changes stock."""
import json, math, secrets
from urllib.parse import parse_qs
from . import material_supply_execution as supply
from . import material_supply_contacts as contacts
from .org_model import mask_phone
from .workbench import esc


def body(state,token):
    actor=state.authorization_subject(token);member=state.member_session(token)
    sections=['<p>物资供应组现任组长具名核实库存及依据。各事件补给单共用核实可用量；正式发布占用，实际交出扣减，取消仅释放尚未交出量。公司供货承诺单列；交出还不是接收队实收。</p>']
    visible=0
    for eid,event in sorted(state.registry.events.items()):
        if event.status=='registered':continue
        space,_,_=state._appointment_space(eid)
        leader=False
        try:contacts.require_leader(space,state.ledger,event_id=eid,actor=actor);leader=True
        except PermissionError:pass
        if not leader and eid not in state.deputy_events(token):continue
        visible+=1
        ordered,stocks,orders=state.material_supply_execution_view(eid,member_token=token)
        writable=leader and event.status in {'active','upgraded'}
        def hidden(values):
            return ''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())
        def common():return dict(event_id=eid,csrf=member.custody_csrf,request_id=secrets.token_urlsafe(18))
        sections.append(f'<section style="overflow-wrap:anywhere"><h2>{esc(event.name)}</h2>')
        for pid,point in sorted(state.ledger.points.items()):
            for iid in sorted(point.supplies):
                item=state.ledger.items.get(iid)
                if item is None:raise ValueError('物资点登记物资缺失，请核对来源。')
                current=stocks.get((pid,iid));quantity=current['quantity'] if current else None
                held=math.fsum(v['quantity']-v['dispatched'] for v in orders.values()
                    if v['source_kind']=='point' and v['source_id']==pid and v['item_id']==iid and not v['cancelled'])
                sections.append(f'<article class="lz-card" data-stock="{esc(pid)}:{esc(iid)}"><h3>{esc(point.name)} · {esc(item.name)}</h3>'
                    f'<p>核实存量：{esc("未知" if quantity is None else f"{quantity:g}")} {esc(item.unit)}；未交出占用：{held:g}；'
                    f'可用：{esc("未知" if quantity is None else f"{quantity-held:g}")}。</p>')
                if current:sections.append(f'<p>核实版本{current["stock_version"]} · 依据：{esc(current["basis"])}。</p>')
                if writable:
                    snapshot=contacts._current_snapshot(state.ledger,iid,'point',pid)
                    sections.append('<form method="post" action="/material-supply/execution">'+hidden(common()|dict(action='verify_stock',point_id=pid,item_id=iid,
                        expected_stock_version=current['stock_version'] if current else 0,expected_source_digest=contacts._digest(snapshot)))
                        +'<label>本次核实实际存量<input class="lz-input" name="quantity" inputmode="decimal" required aria-label="本次核实实际存量"></label>'
                        +'<label>库存核实依据<input class="lz-input" name="basis" required aria-label="库存核实依据"></label>'
                        +'<button class="lz-btn">记录核实库存</button></form>')
                sections.append('</article>')
        sections.append('<h3>本事件正式补给单</h3>')
        for oid,value in orders.items():
            if value['event_id']!=eid:continue
            item=value['source_snapshot']['item'];source=value['source_snapshot']['source']
            local=space.object_revisions[value['record_id']].reference.version
            sections.append(f'<article class="lz-card" data-supply-order="{esc(oid)}"><h4>{esc(source["name"])} · {esc(item["name"])}</h4>'
                f'<p>{"物资点核实库存" if value["source_kind"]=="point" else "公司供货承诺（不计物资点库存）"}；'
                f'发布量：{value["quantity"]:g}；累计实际交出：{value["dispatched"]:g}；'
                f'{"已取消未交出余量" if value["cancelled"] else "待交出："+format(value["quantity"]-value["dispatched"],"g")}。</p>')
            if writable and not value['cancelled'] and value['dispatched']<value['quantity']:
                for action,label in (('dispatch','记录本批实际交出'),('cancel','取消未交出余量')):
                    fields=common()|dict(action=action,order_id=oid,expected_version=local)
                    if action=='cancel':fields['quantity']=0
                    sections.append('<form method="post" action="/material-supply/execution">'+hidden(fields)
                        + ('<label>本批实际交出数量<input class="lz-input" name="quantity" inputmode="decimal" required aria-label="本批实际交出数量"></label>' if action=='dispatch' else '')
                        +f'<label>{label}依据<input class="lz-input" name="basis" required aria-label="{label}依据"></label><button class="lz-btn">{label}</button></form>')
            sections.append('<details><summary>具名执行历史</summary>'+''.join(
                f'<p>{esc({"reserve":"正式发布占用","dispatch":"实际交出","cancel":"取消余量"}[v["action"]])} · '
                f'{esc(mask_phone(r.actor_id))} · {esc(v["basis"])} · {esc(r.times.recorded_at)}</p>'
                for r,v in ordered if r.reference.object_kind==supply.ORDER and r.reference.object_id==oid)+'</details></article>')
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
        form=parse_qs(handler.rfile.read(length).decode('utf-8'),keep_blank_values=True,encoding='utf-8',errors='strict',max_num_fields=128)
        if any(len(v)!=1 for v in form.values()):raise ValueError('字段重复。')
        values={k:v[0] for k,v in form.items()};base={'csrf','action','event_id','basis','quantity','request_id'}
        extra={'point_id','item_id','expected_stock_version','expected_source_digest'} if values.get('action')=='verify_stock' else {'order_id','expected_version'}
        if set(values)!=base|extra:raise ValueError('字段缺失或不受支持。')
        if not secrets.compare_digest(values.pop('csrf').encode(),member.custody_csrf.encode()):raise PermissionError('会话变化。')
        version='expected_stock_version' if values['action']=='verify_stock' else 'expected_version';raw=values[version]
        if not raw.isascii() or not raw.isdigit() or len(raw)>9:raise ValueError('版本须为非负整数。')
        raw_amount=values['quantity']
        if not raw_amount or raw_amount!=raw_amount.strip() or len(raw_amount)>64:raise ValueError('数量须为明确有限非负数。')
        values[version]=int(raw);values['quantity']=supply.number(float(raw_amount))
        state.handle_material_supply_execution(values.pop('event_id'),member_token=handler._member_cookie(),**values)
    except PermissionError:handler._respond(403,'<h1>403</h1>');return
    except (ValueError,PanelActionError,UnicodeError) as error:handler._respond(409,'<h1>409</h1><p>'+esc(str(error))+'</p>');return
    handler._redirect('/material-supply/execution')
