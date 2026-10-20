"""Closeout verification and separately authorized catalog adjustment forms."""
import json
import secrets
from urllib.parse import urlencode, parse_qs
from . import material_closeout_accounts as service
from . import audited_records as ar
from .workbench import esc


def hidden(member, **values):
    values = dict(csrf=member.custody_csrf, request_id=secrets.token_urlsafe(18)) | values
    return ''.join(f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k,v in values.items())


def custody_text(facts):
    loans=['借用 '+str(row['loan_transfer_id'])+'：尚未归还 '+str(row['outstanding']) for row in facts['unreturned']]
    custody=[('留置' if row.get('kind')=='retained' else '移交' if row.get('kind')=='handed_over' else '借入物资保管')+' '+str(row['quantity'])+'；位置 '+row['location']+'；保管人 '+row['custodian'] for row in facts['actual_custody']]
    return '<p>未归还：'+esc('；'.join(loans) or '无已记录未还项')+'</p><p>实际保管：'+esc('；'.join(custody) or '无已记录留置或移交项')+'</p>'


def body(state, token, query=None):
    member = state.member_session(token)
    actor = state.authorization_subject(token)
    spaces = state.supply_event_spaces()
    out = ['<p>核实收尾后才调整长期清单。消耗、损坏、遗失、未归还与实际保管分别保留；登记库存不自动变成核实可用库存。</p>']
    granted = False
    try: state.require_material_maintenance(token); granted = True
    except PermissionError: pass
    ids = [eid for eid,e in state.registry.events.items() if e.status == 'closed']
    chosen = (query or {}).get('event_id', [])
    if chosen:
        if len(chosen) != 1 or chosen[0] not in ids: raise ValueError('须选择明确的关闭事件。')
        ids = chosen
    for eid in ids:
        space = spaces.get(eid)
        if space is None: raise ValueError('关闭事件快照缺失。')
        allowed = False
        try: service.require_leader(space,state.ledger,event_id=eid,actor=actor); allowed = True
        except PermissionError: pass
        if not (allowed or granted): continue
        rows = service.inspect(space,state.ledger)
        out.append('<h2>'+esc(state.registry.events[eid].name)+'</h2>')
        latest = {r.reference.object_id:(r,v) for _,r,v in rows}
        snapshot = state.ledger.snapshots.get(eid)
        if allowed and snapshot:
            for sid,squad in snapshot.squads.items():
                for iid in squad.lines:
                    for pid in [''] + [p.point_id for p in state.ledger.points.values() if iid in p.supplies]:
                        try: f = service.facts(space,state.ledger,spaces,iid,sid,pid)
                        except ValueError as error:
                            out.append('<p>待核：'+esc(str(error))+'</p>'); continue
                        oid = 'closeout-'+ar.digest([sid,iid,pid])
                        version = sum(r.reference.object_id==oid for _,r,v in rows)
                        out.append('<details><summary>'+esc(state.ledger.squads[sid].name+' · '+state.ledger.items[iid].name
                            + (' · '+state.ledger.points[pid].name if pid else ' · 随队清单'))+'</summary>'
                            f'<p>本队已报告消耗 {f["consumed"]:g}，损坏 {f["damaged"]:g}；撤出 {f["carry_ceiling"]:g}。未还和实际保管如下；后勤实收不自动归回本队。</p>'
                            +custody_text(f)+
                            '<form method="post" action="/materials/accounts">'+hidden(member,action='verify',event_id=eid,item_id=iid,squad_id=sid,point_id=pid,
                            expected_facts_digest=ar.digest(f),expected_version=version)+
                            '<label>撤出后另核遗失（已耗损不要重复计入）<input name="lost" type="number" min="0" step="any" required></label>'
                            '<label>已核下一次实际可携数量<input name="carry_quantity" type="number" min="0" step="any" required></label>'
                            '<label>核实依据<input name="basis" required></label><label>处理结论或追加更正理由<input name="reason" required></label>'
                            '<button class="lz-btn">追加具名核实版</button></form></details>')
        for oid,(r,v) in latest.items():
            out.append('<section class="lz-card"><h3>核实 v'+str(r.reference.version)+'</h3><p>'+esc(v['basis']+' · '+v['reason'])+'</p>'
                f'<p>新增核实遗失 {v["lost"]:g}；下一次可携 {v["carry_quantity"]:g}；旧版及未还债务保留。</p>')
            cat = [(rec,val) for rec,val in __import__('panda_brain.material_catalog',fromlist=['inspect']).inspect(state.ledger,spaces) if rec.reference.object_id==oid]
            used = any(val['details']['input'].get('closeout_ref')==r.id for rec,val in cat)
            current = ar.dump(service.facts(space,state.ledger,spaces,v['facts']['item_id'],v['facts']['squad_id'],v['facts']['point_id'])) == ar.dump(v['facts'])
            if used: out.append('<p>该核实版已经关联调整；再次更正须追加核实版。</p>')
            elif not current: out.append('<p role="alert">来源已变化，须由现任组长追加核实；旧结论不能复用。</p>')
            elif granted:
                out.append('<form method="post" action="/materials/accounts">'+hidden(member,action='adjust',event_id=eid,closeout_ref=r.id,
                    object_id=oid,expected_version=len(cat))+'<label>长期调整依据<input name="basis" required></label>'
                    '<button class="lz-btn">按当前核实版调整长期底账</button></form>')
            else: out.append('<p>待获有效长期维护权的具名人员处理。</p>')
            out.append('<details><summary>完整历史</summary>'+''.join('<p>'+esc(rec.actor_id+' · '+str(rec.reference.version)+' · '+val['reason'])+'</p>' for _,rec,val in rows if rec.reference.object_id==oid)+'</details></section>')
    return '<div style="overflow-wrap:anywhere;min-width:0">'+''.join(out)+'</div>'


def post(handler, state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    token = handler._member_cookie(); member = state.member_session(token)
    if member is None: handler._respond(403,'<h1>403</h1>'); return
    try:
        form = parse_qs(handler.rfile.read(request_length(handler,16384)).decode('utf-8'),keep_blank_values=True,errors='strict',max_num_fields=20)
        if any(len(v)!=1 for v in form.values()): raise ValueError('字段重复。')
        v = {k:val[0] for k,val in form.items()}
        common = {'csrf','action','event_id','expected_version','request_id','basis'}
        action = v.get('action')
        extra = {'item_id','squad_id','point_id','lost','carry_quantity','reason','expected_facts_digest'} if action=='verify' else {'closeout_ref','object_id'} if action=='adjust' else set()
        if set(v)!=common|extra or action not in {'verify','adjust'}: raise ValueError('收尾字段缺失或越界。')
        if not secrets.compare_digest(v.pop('csrf').encode(),member.custody_csrf.encode()): raise PermissionError('会话变化。')
        raw = v['expected_version']
        if not raw.isascii() or not raw.isdigit() or len(raw)>9: raise ValueError('版本不正确。')
        v['expected_version']=int(raw); v.pop('action'); eid=v.pop('event_id')
        if action=='verify':
            v['lost']=float(v['lost']); v['carry_quantity']=float(v['carry_quantity'])
            state.handle_material_closeout_account(eid,member_token=token,**v)
        else:
            ref=v.pop('closeout_ref')
            state.handle_material_catalog(member_token=token,operation='closeout',details=dict(event_id=eid,closeout_ref=ref),**v)
    except PermissionError: handler._respond(403,'<h1>403</h1>'); return
    except (ValueError, KeyError, TypeError, PanelActionError) as error:
        handler._respond(409,'<h1>收尾未保存</h1><p>'+esc(str(error))+'</p>'); return
    handler._redirect('/materials/accounts?'+urlencode(dict(event_id=eid)))
