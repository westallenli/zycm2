"""Named duty intake, read-only external mock roster, and a reviewed handoff."""
import hashlib
import json
from pathlib import Path
from .workbench import esc

ROSTER_PATH = Path(__file__).resolve().parents[1] / 'config/provincial-emergency-demo.json'


def catalogue():
    raw = ROSTER_PATH.read_bytes()
    value = json.loads(raw)
    if (set(value) != {'schema','source','version','simulated','people'}
            or value['schema'] != 1 or value['simulated'] is not True
            or not value['source'] or not value['version'] or not value['people']):
        raise ValueError('模拟外部全省应急人员名单不可用。')
    seen=set()
    for row in value['people']:
        if (set(row) != {'subject_id','name','unit','position','region'}
                or any(not isinstance(v,str) or not v.strip() for v in row.values())
                or len(row['subject_id']) != 11 or not row['subject_id'].isascii()
                or not row['subject_id'].isdecimal() or row['subject_id'] in seen):
            raise ValueError('模拟人员名单字段或身份重复，请核对外部资料。')
        seen.add(row['subject_id'])
    return value,hashlib.sha256(raw).hexdigest()


def simulate(state,token,request_id):
    from .fire_demo import confirmed_demo_message
    from .fire_events import utc_now_iso
    actor=state.duty_subject(token)
    if not request_id or len(request_id)>128:
        raise ValueError('模拟接入请求号无效。')
    # Stable source identity and original timestamp make resubmission read-only.
    source='duty-demo-'+hashlib.sha256((actor+'|'+request_id).encode()).hexdigest()
    existing=next((a for a in state.alarm_inbox.alarms.values()
                   if a.source_system=='DEMO_CONSOLE' and a.external_no==source),None)
    if existing is not None:
        return {'event_id':existing.event_id,'duplicate':True}
    message=confirmed_demo_message(source_record_id=source,timestamp=utc_now_iso())
    result=state.handle_confirmed_fire_event(message)
    if result['code'] not in {'CREATED','DUPLICATE'}:
        raise ValueError('模拟报警接入失败：'+result['message'])
    return {'event_id':result['event_id'],'duplicate':result['code']=='DUPLICATE'}


def select_and_issue(state,event_id,token,subject_id,request_id):
    from .models import PersonRecord
    from .persistence import save_workspace
    actor=state.duty_subject(token)
    if not request_id or len(request_id)>128:
        raise ValueError('选定副总指挥请求号无效。')
    data,digest=catalogue()
    person=next((p for p in data['people'] if p['subject_id']==subject_id),None)
    if person is None:
        raise ValueError('请从当前全省应急人员名单选择副总指挥。')
    space,target,active=state._appointment_space(event_id,for_write=True)
    previous=next((e for e in space.events if e.kind=='ProvincialDeputySelected'
                   and e.payload.get('request_id')==request_id),None)
    if previous is not None:
        if (previous.payload.get('operator')!=actor or previous.payload.get('subject_id')!=subject_id
                or previous.payload.get('catalogue_digest')!=digest):
            raise ValueError('请求号已用于不同的选人资料。')
        qr=state.registry.qrs.get(previous.payload['qr_id'])
        if qr is None or qr.event_id!=event_id:
            raise ValueError('原选人记录与二维码不一致。')
        state.deputy_appointment(event_id)
        return {'event_id':event_id,'qr_id':qr.qr_id,'duplicate':True}
    if state.deputy_appointment(event_id) is not None:
        raise ValueError('本事件已有副总指挥，保留原指定；本入口不办理换任。')
    if subject_id not in state.registry.library:
        state.registry.import_person_library([PersonRecord(name=person['name'],phone=subject_id,unit=person['unit'])])
    basis=data['source']+'；版本 '+data['version']+'；选定 '+person['name']+'（'+subject_id+'）'
    state.handle_deputy_appointment(event_id,duty_token=token,subject_id=subject_id,
        evidence=basis,request_id=request_id)
    # Re-read the durable appointment; signing cannot precede successful selection.
    space,target,active=state._appointment_space(event_id,for_write=True)
    qr=state.registry.issue_qr(event_id,issued_by=actor)
    space.append_event('EventQRIssued',qr_id=qr.qr_id,operator=actor)
    space.append_event('ProvincialDeputySelected',event_id=event_id,subject_id=subject_id,
        operator=actor,request_id=request_id,qr_id=qr.qr_id,catalogue_source=data['source'],
        catalogue_version=data['version'],catalogue_digest=digest,
        selected_person=dict(person),data_kind='simulated')
    save_workspace(space,target)
    if active: state.persist_if_db()
    state._save_registry()
    return {'event_id':event_id,'qr_id':qr.qr_id,'duplicate':False}


def body(state,member,*,query=None,event_ids=None):
    import secrets
    from . import panel, business_modules as business
    role=business.role(state,next((t for t,s in state.member_sessions.items() if s is member),None))
    data,_=catalogue()
    csrf=member.custody_csrf
    def form(action,fields,label):
        return ('<form method="post" action="'+action+'">'
            + ''.join('<input type="hidden" name="'+k+'" value="'+esc(str(v))+'">' for k,v in dict(fields,csrf=csrf).items())
            + '<button class="lz-btn" type="submit">'+label+'</button></form>')
    out=['<section class="lz-card"><h2>火情事件</h2><p>模拟报警接入 → 启动处置 → 选择副总指挥 → 生成火情事件入岗二维码</p>'
        '<p class="lz-note">全省应急人员名单由外部维护，本页只供选择；当前接入和名单均为演示数据。</p>'
        +form('/events/alarm/simulate',{'request_id':secrets.token_urlsafe(18)},'模拟报警接入')+'</section>']
    def appointment_for(eid):
        try:
            return state.deputy_appointment(eid),''
        except (panel.PanelActionError,ValueError):
            # Old closed records can legitimately lack a task snapshot. Preserve
            # their history and QR state without blocking the whole event list.
            return None,'此事件历史任务记录待核对，原事件和原二维码记录保留。'
    completed=((query or {}).get('completed') or [''])[0]
    if (completed in state.registry.events and (event_ids is None or completed in event_ids)
            and state.registry.events[completed].status!='registered'
            and appointment_for(completed)[0] is not None
            and any(q.event_id==completed for q in state.registry.qrs.values())):
        out.append('<div class="lz-alert ok">副总指挥已选定，火情事件入岗二维码已生成。值班工作已完成，后续由副总指挥组织指挥。</div>')
    for record in sorted(state.registry.events.values(),key=lambda r:r.event_id,reverse=True):
        if event_ids is not None and record.event_id not in event_ids: continue
        eid=record.event_id
        out.append('<section class="lz-card duty-fire-card" data-event="'+esc(eid)+'" style="overflow-wrap:anywhere"><h3>'
            +esc(record.name)+'</h3><p>'+esc(record.location)+' · '+panel._event_status_chip(record)+'</p>'
            '<p><a class="lz-link" href="/events/'+esc(eid)+'/details">查看报警详情</a></p>')
        if record.status=='registered':
            out.append('<p>待值班人员启动处置。</p>'+form('/events/action',{'event_id':eid,'action':'start'},'启动处置'))
        else:
            appointment,history_note=appointment_for(eid)
            if history_note: out.append('<p class="lz-note">'+esc(history_note)+'</p>')
            if appointment is not None:
                p=json.loads(appointment.content_json); person=state.registry.library.get(p['subject_id'])
                out.append('<p>已指定副总指挥：'+esc(person.name if person else p['subject_id'])+'</p>'
                    +'<details><summary>查看指定依据与原记录</summary><p>'+esc(p['evidence'])+'</p><p>记录人：'+esc(appointment.actor_id)+'</p></details>')
            elif record.status in {'active','upgraded'} and not history_note:
                out.append('<p>启动完成，请从全省应急人员名单选择本事件副总指挥。</p><form method="post" action="/events/deputy-select">'
                    + ''.join('<input type="hidden" name="'+k+'" value="'+esc(v)+'">' for k,v in
                              dict(event_id=eid,csrf=csrf,request_id=secrets.token_urlsafe(18)).items())
                    + '<label class="lz-label">选择副总指挥<select class="lz-input" name="subject_id" required style="max-width:100%">'
                    '<option value="">请选择人员</option>'
                    + ''.join('<option value="'+esc(p['subject_id'])+'">'+esc(p['name']+' · '+p['unit']+' · '+p['position']+' · '+p['region'])+'</option>' for p in data['people'])
                    + '</select></label><button type="submit" class="lz-btn">确认副总指挥并生成二维码</button></form>')
            elif record.status=='closed': out.append('<p>事件已关闭，原记录保留。</p>')
            out.append(panel._event_qr_lines(state,eid,allow_revoke=False))
            if role!='duty': out.append(form('/events/select',{'event_id':eid},'进入事件'))
        out.append('</section>')
    return ''.join(out)
