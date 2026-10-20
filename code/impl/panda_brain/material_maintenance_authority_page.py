"""Named logistics authority board; grant never implies other duties."""
import secrets
from urllib.parse import parse_qs
from . import material_maintenance_authority as service
from .material_supply_contacts import require_leader
from .workbench import esc


def body(state,token):
    actor=state.authorization_subject(token);member=state.member_session(token);spaces=state.supply_event_spaces()
    rows=service.inspect(state.ledger,spaces);latest={v['subject_id']:(r,v) for r,v in rows}
    out=['<p><a href="/materials/records">长期物资资料维护与文件导入</a></p><p>物资供应组现任组长明确授予或撤销具名长期物资资料维护权，跨事件有效。依据和旧版保留；撤销立即停写。此权不包含现场发布、复盘维护或人员管理。</p>']
    current=latest.get(actor)
    out.append('<p>本人长期物资维护权：'+('有效' if current and current[1]['active'] else '未授予或已撤销')+'。</p>')
    for eid,event in sorted(state.registry.events.items()):
        if event.status in {'registered','closed'}:continue
        space=spaces[eid]
        try:require_leader(space,state.ledger,event_id=eid,actor=actor)
        except PermissionError:continue
        out.append(f'<section class="lz-card"><h2>{esc(event.name)} · 长期物资维护授权</h2>')
        for subject,person in sorted(state.registry.library.items()):
            prior=latest.get(subject);version=prior[1]['grant_version'] if prior else 0;active=prior[1]['active'] if prior else False
            values=dict(csrf=member.custody_csrf,event_id=eid,subject_id=subject,active='false' if active else 'true',expected_version=version,request_id=secrets.token_urlsafe(18))
            hidden=''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())
            out.append(f'<details><summary>{esc(person.name)} · {esc(subject[:3]+"****"+subject[-4:])} · {"有效" if active else "未授或已撤"} · v{version}</summary>'
                '<form method="post" action="/materials/maintenance-authority">'+hidden
                + '<label>授权依据<input class="lz-input" name="basis" required aria-label="授权依据"></label>'
                '<label>授撤理由<input class="lz-input" name="reason" required aria-label="授撤理由"></label>'
                f'<button class="lz-btn">{"撤销长期物资维护权" if active else "授予长期物资维护权"}</button></form></details>')
        out.append('</section>')
    out.append('<details><summary>具名授撤历史</summary>'+''.join(f'<p>{esc(v["subject_id"])} · {"授予" if v["active"] else "撤销"} · v{v["grant_version"]} · 组长{esc(r.actor_id)} · {esc(v["basis"])} · {esc(v["reason"])} · {esc(r.times.recorded_at)}</p>' for r,v in rows)+'</details>')
    return ''.join(out)


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
        if set(form)!={'csrf','event_id','subject_id','active','basis','reason','expected_version','request_id'} or any(len(v)!=1 for v in form.values()):raise ValueError('字段缺失、重复或不受支持。')
        values={k:v[0] for k,v in form.items()}
        if not secrets.compare_digest(values.pop('csrf').encode(),member.custody_csrf.encode()):raise PermissionError('会话变化。')
        raw=values['expected_version']
        if not raw.isascii() or not raw.isdigit() or len(raw)>9 or values['active'] not in {'true','false'}:raise ValueError('版本或授撤状态不正确。')
        values.update(expected_version=int(raw),active=values['active']=='true')
        state.handle_material_maintenance_authority(values.pop('event_id'),member_token=handler._member_cookie(),**values)
    except PermissionError:handler._respond(403,'<h1>403</h1>');return
    except (ValueError,PanelActionError) as error:handler._respond(409,'<h1>409</h1><p>'+esc(str(error))+'</p>');return
    handler._redirect('/materials/maintenance-authority')
