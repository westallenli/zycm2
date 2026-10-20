"""Daily resource library and event resources, with server-scoped passive advice."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from .workbench import esc
from . import resource_maintenance as maintenance
from . import resource_advisor as advisor

STYLE='''<style>
.resource-switch{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:4px 0 12px}
.resource-switch a{display:block;padding:12px 16px;border:1px solid #bed3e6;border-radius:8px;color:#164b79;background:#f4f9ff;text-decoration:none;font-size:16px;font-weight:700}
.resource-switch small{display:block;font-size:13px;line-height:1.8;font-weight:400;color:#52697c;margin-top:6px}
.resource-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.resource-grid>*{min-width:0}
.resource-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:16px 0}.resource-form label{display:flex;flex-direction:column;gap:6px;min-width:0;font-size:14px}
.resource-form input,.resource-form select,.resource-form textarea{width:100%;min-width:0;box-sizing:border-box}.resource-wide{grid-column:1/-1}
.resource-section{scroll-margin-top:80px;overflow-wrap:anywhere}.resource-section details{margin:12px 0}.resource-section table{min-width:480px}
.resource-alert{border-left:4px solid #d37b10}.resource-critical{border-left-color:#c43c30}.resource-muted{color:#53667a;font-size:13px;line-height:1.7}
.resource-metrics{display:flex;gap:20px;flex-wrap:wrap;margin:14px 0}.resource-metrics b{display:block;font-size:23px;color:#183c61}
.resource-workspace .lz-card{padding:14px 16px;margin:0 0 12px}.resource-workspace h1{font-size:21px;margin:0}.resource-workspace h2{font-size:16px;margin:0}.resource-workspace h3{font-size:14px;margin:12px 0 6px}
.resource-heading,.resource-toolbar,.resource-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.resource-heading{justify-content:space-between;margin-bottom:10px}.resource-heading small{font-size:12px;color:#53667a;font-weight:400}
.resource-toolbar .lz-btn{padding:5px 12px;font-size:13px}.resource-toolbar a{font-size:13px}.resource-actions{flex-wrap:nowrap;gap:6px}.resource-actions button{border:1px solid #cbd8e3;border-radius:5px;padding:4px 9px;background:#fff;color:#174b78;cursor:pointer;white-space:nowrap;font:inherit;font-size:12px}
.resource-table-wrap{overflow:auto;max-width:100%}.resource-table-scroll{max-height:360px;scrollbar-gutter:stable}.resource-workspace .lz-table{margin:0;font-size:13px}.resource-workspace .lz-table th,.resource-workspace .lz-table td{padding:7px 9px;vertical-align:middle}.resource-table-scroll th{position:sticky;top:0;z-index:1;background:#eef5fc}
.resource-workspace .lz-table th{white-space:nowrap}.resource-workspace .resource-section{min-width:0}.resource-workspace .resource-grid{gap:12px}.resource-workspace .resource-grid>.lz-card{margin:0 0 12px}.resource-event .resource-table-scroll{max-height:270px}
.resource-workspace .lz-table td{white-space:nowrap}.resource-has-actions th:last-child,.resource-has-actions td:last-child{position:sticky;right:0;background:#fff;box-shadow:-3px 0 4px #183c6110}.resource-has-actions th:last-child{z-index:2;background:#eef5fc}#resource-arrived-teams td:nth-child(3){white-space:normal;min-width:220px;max-width:300px}
.resource-dialog{width:min(760px,calc(100% - 32px));max-height:85vh;padding:20px;border:1px solid #b9cadb;border-radius:12px;color:#183c61;background:#fff}.resource-dialog::backdrop{background:rgba(15,35,55,.35)}.resource-dialog .resource-heading h2{font-size:18px}.resource-dialog .resource-close{border:0;background:none;color:#52697c;font-size:20px;cursor:pointer}.resource-dialog .resource-form{margin:12px 0 0}.resource-tabs{display:flex;flex-wrap:wrap;gap:14px;font-size:14px}.resource-subtitle{font-size:12px;color:#53667a;margin-top:4px}.resource-signal{font-weight:600;color:#9a3d31}
@media(max-width:650px){.resource-grid,.resource-form{grid-template-columns:minmax(0,1fr)}.resource-switch a{padding:10px;font-size:14px}.resource-wide{grid-column:auto}.resource-workspace .lz-card{padding:12px}.resource-heading{align-items:flex-start}.resource-toolbar{gap:6px}.resource-workspace .lz-table{font-size:12px}.resource-dialog{padding:16px}}
</style>'''


def switch():
    return ('<nav class="resource-switch" aria-label="人员物资工作区">'
        '<a href="/materials/daily">日常数据维护</a>'
        '<a href="/materials/event">本火情人员与物资</a></nav>')


def table(heads,rows,*,actions=None,scroll=False):
    rows=list(rows)
    if actions is not None and len(actions)!=len(rows): raise ValueError('表格操作与资料行不一致。')
    headers=list(heads)+(['操作'] if actions is not None else [])
    body=''.join('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in row)+('<td>'+actions[index]+'</td>' if actions is not None else '')+'</tr>' for index,row in enumerate(rows))
    return '<div class="resource-table-wrap'+(' resource-table-scroll' if scroll else '')+'"><table class="lz-table'+(' resource-has-actions' if actions is not None else '')+'"><thead><tr>'+''.join('<th>'+esc(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+(body or '<tr><td colspan="'+str(len(headers))+'">暂无资料。</td></tr>')+'</tbody></table></div>'


def hidden(values):
    return ''.join('<input type="hidden" name="'+esc(k)+'" value="'+esc(str(v))+'">' for k,v in values.items())


def field(name,label,value='',*,kind='text',required=False,readonly=False):
    return '<label>'+esc(label)+'<input class="lz-input" type="'+kind+'" name="'+esc(name)+'" value="'+esc(str(value))+'"'+(' required' if required else '')+(' readonly' if readonly else '')+'></label>'


def select(name,label,values):
    return '<label>'+esc(label)+'<select class="lz-select" name="'+name+'">'+''.join('<option value="'+esc(str(k))+'">'+esc(str(v))+'</option>' for k,v in values)+'</select></label>'


def base_form(state,token,operation,body,*,extra=None,action='/materials/daily/save',label='保存具名修改'):
    member=state.member_session(token)
    data=dict(csrf=member.custody_csrf,operation=operation,expected_version=len(state.ledger.daily_resource_records),request_id=secrets.token_urlsafe(18),expected_source_digest=maintenance.source_digest(state))
    data.update(extra or {})
    return ('<form method="post" action="'+action+'" class="resource-form">'+hidden(data)+body+
        field('basis','资料来源 / 修改依据',required=True)+'<div class="resource-wide"><button class="lz-btn" type="submit">'+label+'</button></div></form>')


def daily(state,token):
    from .resource_directory_tables import daily as render
    return render(state,token)


def company_directory(state,*,editable=False,token=None):
    from .resource_directory import enabled
    out='<section class="lz-card resource-section" id="companies"><div class="resource-heading"><h2>外协公司名录</h2></div>'+table(
        ('公司','物资 / 服务','服务区域','联系人','电话'),[
            (c.name,c.services,c.region,c.contact_name,c.contact_phone) for c in state.ledger.companies.values() if enabled(state.ledger,'companies',c.company_id)])
    if editable:
        from .material_catalog_page import FIELDS
        out+='<details><summary>新增或更新外协公司</summary>'+base_form(state,token,'catalog',
            ''.join(field(k,label,required=k in {'name','source'}) for k,label in FIELDS['company']),extra={'data_type':'company'})+'</details>'
    return out+'</section>'


def advice(state,event_id):
    from .resource_advice_page import body
    return body(state,event_id)


def event_page(state,token,*,admin=False):
    from . import business_modules as business
    eid=state.active_event_id; event=state.registry.events.get(eid)
    if not event or event.status=='registered': return STYLE+switch()+'<section class="lz-card"><h1>本火情人员与物资</h1><p>请先选择已启动火情。</p><a href="/events">进入火情事件</a></section>'
    if not admin and eid not in business.events(state,token): raise PermissionError('无权查看本火情。')
    role=business.role(state,token); planner=False
    try: advisor.require_planner(state,token,eid); planner=True
    except PermissionError: pass
    detailed=admin or role in {'duty','deputy'} or planner
    out=[STYLE,'<div class="resource-workspace resource-event">',switch(),'<section class="lz-card"><h1>本火情人员与物资</h1><div class="resource-subtitle">'+esc(event.name)+'</div></section>']
    if detailed: out.append(advice(state,eid))
    snapshot=state.ledger.snapshots.get(eid)
    arrival_pending=False
    if snapshot is not None:
        from .material_inventory_reads import read_event
        try: read_event(state.workspace,state.ledger,eid)
        except ValueError:
            snapshot=None; arrival_pending=True
    owned=None if detailed else set(business.teams(state,token,eid))
    out.append('<div class="resource-grid"><section class="lz-card resource-section"><div class="resource-heading"><h2>本火情人员</h2>')
    from .org_float import projected_rows
    people=projected_rows(state,eid)
    listed={p['phone'] for p in people}
    from .material_withdrawals import withdrawal_record
    for sid, squad in (snapshot.squads.items() if snapshot else []):
        for phone in sorted(squad.arrived_phones):
            if phone in listed: continue
            person=state.registry.library.get(phone)
            if person is None: continue
            withdrawn=withdrawal_record(state.ledger,event_id=eid,squad_id=sid)
            people.append(dict(phone=phone,name=person.name,unit=person.unit,
                group=state.ledger.squads[sid].name if sid in state.ledger.squads else sid,
                slot_name='岗位待安排',status_word='已报告撤离' if withdrawn else '已确认到场；可派状态待报告',is_arrived=not bool(withdrawn)))
            listed.add(phone)
    allowed=None if detailed else business.phones(state,token,eid)
    visible=[p for p in people if allowed is None or p['phone'] in allowed]
    arrived=[p for p in visible if p.get('is_arrived')]
    pending=[p for p in visible if not p.get('is_arrived')]
    out.append('<small>已到场 <b data-resource-arrived="'+str(len(arrived))+'">'+str(len(arrived))+'</b> 人</small></div>')
    out.append(table(('姓名','单位','本次岗位','当前状态'),[(p['name'],p['unit'],p['slot_name'] or p['group'],p['status_word']) for p in arrived],scroll=True))
    if pending:out.append('<details><summary>尚未到场或已离场 · '+str(len(pending))+' 人</summary>'+table(('姓名','单位','状态'),[(p['name'],p['unit'],p['status_word']) for p in pending])+'</details>')
    out.append('</section><section class="lz-card resource-section" id="resource-arrived-teams"><div class="resource-heading"><h2>已到场队伍与标配</h2></div>'+table(
        ('队伍','到场人数','随队标配','状态'),[
            (state.ledger.squads[sid].name if sid in state.ledger.squads else sid,len(s.arrived_phones),
             '；'.join(state.ledger.items[iid].name+' × '+format(line.qty_registered,'g')+' '+state.ledger.items[iid].unit for iid,line in s.lines.items()),
             '齐全') for sid,s in (snapshot.squads.items() if snapshot else []) if owned is None or sid in owned])+'</section></div>')
    if arrival_pending: out.append('<section class="lz-card">'+table(('资料状态','内容'),[('待核','登记到场物资来源待核')])+'</section>')
    if detailed: out.append(company_directory(state))
    if planner:
        rows=advisor.context_rows(state.workspace,state.ledger); member=state.member_session(token)
        values=rows[-1][2] if rows else {}
        out.append('<section class="lz-card resource-section"><details><summary>保障接收位置与预测时段依据</summary><form method="post" action="/materials/planning" class="resource-form">'+hidden(dict(
            csrf=member.custody_csrf,event_id=eid,expected_version=len(rows),request_id=secrets.token_urlsafe(18)))+
            ''.join(field(k,label,'' if values.get(k) is None else values[k]) for k,label in [('latitude','保障接收点纬度（WGS84）'),('longitude','保障接收点经度（WGS84）'),('planning_hours','预测未来小时（空则沿用任务预计时长）'),('buffer_hours','提前预警缓冲小时（未知留空）')])+
            field('basis','位置与时段来源',values.get('basis',''),required=True)+'<button class="lz-btn">保存具名依据</button></form></details></section>')
    out.append('<section class="lz-card resource-section"><details><summary>人员安排、物资办理与历史记录</summary><div style="display:flex;flex-wrap:wrap;gap:12px">')
    from .unified_ui import CHILDREN
    links=[('/personnel/arrival','扫码签到与到场确认'),('/personnel/groups','编组与调组'),('/sand/task-materials','任务物资需求依据')]+list(CHILDREN['/materials'])
    out.extend('<a class="lz-btn lz-btn2" href="'+url+'">'+esc(label)+'</a>' for url,label in links if url not in {'/materials/records','/materials/maintenance-authority'})
    out.append('</div></details></section>')
    if detailed: out.append('<script src="/static/resource-adviser.js" defer></script>')
    from .captain_field import report_section,SHORTAGE
    out.append(report_section(state,token,SHORTAGE,admin=admin))
    return ''.join(out)+'</div>'


def post(handler,state,route):
    from .http_rejection import request_length
    from . import panel
    try:
        token=handler._member_cookie(); member=state.member_session(token)
        if member is None: raise PermissionError('需要本人有效登录。')
        content_type=handler.headers.get('Content-Type','')
        size=request_length(handler,4*1024*1024); handler.connection.settimeout(15)
        raw=handler.rfile.read(size)
        if len(raw)!=size: raise ValueError('表单传送不完整。')
        if content_type.startswith('multipart/form-data;'):
            from email.parser import BytesParser
            from email.policy import default
            from .document_tables import extract
            message=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\n\r\n').encode()+raw)
            if not message.is_multipart() or message.defects: raise ValueError('文件表单边界不正确。')
            fields={}; file=None; filename=''
            for part in message.iter_parts():
                name=part.get_param('name',header='content-disposition')
                if not name or name in fields or name=='file' and file is not None: raise ValueError('字段不能重复。')
                if name=='file': file=part.get_payload(decode=True);filename=part.get_filename() or ''
                else: fields[name]=part.get_payload(decode=True).decode('utf-8')
                if len(fields)>24: raise ValueError('字段过多。')
            if route!='/materials/daily/preview' or fields.get('operation')!='directory_import' or 'content' in fields: raise ValueError('文件仅用于预览导入。')
            if not filename.lower().endswith(('.csv','.xlsx')): raise ValueError('请选择CSV或Excel .xlsx单表文件。')
            fields['content']=extract(filename,file)
        elif content_type.split(';')[0]=='application/x-www-form-urlencoded':
            parsed=parse_qs(raw.decode('utf-8'),keep_blank_values=True,encoding='utf-8',errors='strict',max_num_fields=24)
            if any(len(v)!=1 for v in parsed.values()): raise ValueError('字段不能重复。')
            fields={k:v[0] for k,v in parsed.items()}
        else: raise ValueError('表单格式不正确。')
        if not secrets.compare_digest(fields.pop('csrf','').encode(),member.custody_csrf.encode()): raise PermissionError('本人表单会话已变化。')
        version=fields.pop('expected_version','')
        if not version.isascii() or not version.isdecimal() or len(version)>9: raise ValueError('版本不正确。')
        request_id=fields.pop('request_id','')
        def number(value): return None if value=='' else float(value)
        if route=='/materials/planning':
            eid=fields.pop('event_id','')
            from .business_modules import selected_event
            if eid!=selected_event(state,token): raise PermissionError('当前火情已变化，请刷新核对。')
            if set(fields)!={'latitude','longitude','planning_hours','buffer_hours','basis'}: raise ValueError('预测字段不正确。')
            values={k:(v if k=='basis' else number(v)) for k,v in fields.items()}
            state.handle_resource_planning(eid,member_token=token,expected_version=int(version),request_id=request_id,**values)
            handler._redirect('/materials/event'); return
        operation=fields.pop('operation',''); basis=fields.pop('basis','')
        expected_source_digest=fields.pop('expected_source_digest','')
        if not expected_source_digest: raise ValueError('缺少预览资料版本。')
        if route=='/materials/daily/confirm':
            if set(fields)!={'details'}: raise ValueError('确认字段不正确。')
            details=json.loads(fields['details'])
        elif operation in {'person_edit','squad_edit'}:
            from .resource_directory import PEOPLE_HEADERS,SQUAD_HEADERS,csv_text
            keys=['name','phone','city','unit','squad','position','leader'] if operation=='person_edit' else ['name','phones','leader','count','contact']
            if set(fields)!=set(keys): raise ValueError('资料字段不正确。')
            kind='person_directory' if operation=='person_edit' else 'squad_directory'
            content=csv_text(PEOPLE_HEADERS if operation=='person_edit' else SQUAD_HEADERS,[[fields[k] for k in keys]])
            operation='directory_import';details=dict(data_type=kind,content=content,content_sha256=hashlib.sha256(content.encode()).hexdigest())
        elif operation=='status':
            if set(fields)!={'kind','identifier','enabled'} or fields['enabled'] not in {'true','false'}: raise ValueError('停用字段不正确。')
            details=fields|{'enabled':fields['enabled']=='true'}
        elif operation=='catalog':
            kind=fields.pop('data_type',''); details={'data_type':kind,'row':fields}
        elif operation=='people': details=fields
        elif operation=='squad':
            details=fields
            details['member_phones']=[x.strip() for x in fields.get('member_phones','').split(',') if x.strip()]
            details['headcount_planned']=int(fields.get('headcount_planned','0'))
        elif operation=='metadata':
            kind=fields.pop('metadata_kind',''); identifier=fields.pop('identifier',''); parameter_basis=fields.pop('parameter_basis','')
            expected={'scope','rate_per_person_hour'} if kind=='items' else {'latitude','longitude','lead_hours'}
            if set(fields)!=expected: raise ValueError('参数字段不正确。')
            details=dict(kind=kind,identifier=identifier,values={k:(v if k=='scope' else number(v)) for k,v in fields.items()}|{'basis':parameter_basis})
        elif operation in {'import','directory_import'}:
            if set(fields)!={'data_type','content'}: raise ValueError('导入字段不正确。')
            details=fields|dict(content_sha256=hashlib.sha256(fields['content'].encode()).hexdigest())
        else: raise ValueError('维护动作不受支持。')
        maintenance.authority(state,token,operation)
        if route=='/materials/daily/preview':
            from .material_catalog import preview
            actor=state.authorization_subject(token)
            if operation=='directory_import':
                from copy import deepcopy
                from .resource_directory import import_data
                if details['data_type'] in {'person_directory','squad_directory'}: maintenance.authority(state,token,'people')
                import_data(state,deepcopy(state.ledger),deepcopy(state.registry),details['data_type'],details['content'],actor)
                import csv,io
                parsed_rows=list(csv.reader(io.StringIO(details['content'])))
                rows=[dict(zip(parsed_rows[0],r)) for r in parsed_rows[1:] if any(r)]
            elif operation=='people':
                from copy import deepcopy
                changes=maintenance._people(state,deepcopy(state.registry),details['csv_text'])
                rows=[c['after'] for c in changes]
            elif operation=='import':
                _,rows,errors=preview(details['data_type'],details['content'],state.ledger,actor)
                if errors: raise ValueError('；'.join(errors))
            else: raise ValueError('预览动作不正确。')
            heads=list(rows[0]) if rows else []
            content=(STYLE+switch()+'<section class="lz-card"><h1>导入预览</h1><p>共 '+str(len(rows))+' 行，尚未保存。确认后追加原文、来源及修改记录。</p>'+table(heads,[[r.get(k,'') for k in heads] for r in rows])+
                '<form method="post" action="/materials/daily/confirm">'+hidden(dict(csrf=member.custody_csrf,operation=operation,basis=basis,expected_version=version,request_id=request_id,expected_source_digest=expected_source_digest,details=json.dumps(details,ensure_ascii=False)))+'<button class="lz-btn">确认导入</button></form></section>')
            handler._respond(200,panel._light_wrap(title='日常资料导入预览',body=content,nav='',user=panel._topbar_logout_form('/logout/member','登出'))); return
        state.handle_daily_resources(member_token=token,operation=operation,details=details,basis=basis,expected_version=int(version),request_id=request_id,expected_source_digest=expected_source_digest)
    except PermissionError as exc: handler._respond(403,'<h1>无权保存</h1><p>'+esc(str(exc))+'</p>'); return
    except (ValueError,KeyError,TypeError,UnicodeError,panel.PanelActionError) as exc:
        handler._respond(409,'<h1>资料未保存</h1><p>'+esc(str(exc))+'</p><a href="/materials/daily">返回核对</a>'); return
    except OSError:
        handler._respond(503,'<h1>保存中断</h1><p>请保留输入，刷新核对原记录后再提交。</p>'); return
    handler._redirect('/materials/daily')
