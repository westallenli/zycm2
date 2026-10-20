"""Named personnel management; preserves existing dispatch and group workflows."""
import secrets
from urllib.parse import parse_qs
from . import personnel_management as service
from .workbench import esc
from .org_model import POSITION_SLOTS, SLOT_INDEX

LABELS=dict(import_library='维护长期人员库',add_roster='选择加入本事件名册',assign_roster='安排本事件岗位',
            review_maintenance='复盘维护查看与建议安排',unlock_account='解除已锁定成员账号',mark_state='具名人员状态报告',squad='长期中队与队长安排')


def body(state,token,event_id=None):
    actor=state.authorization_subject(token); member=state.member_session(token)
    out=['<p>由本事件指定副指挥长具名决定人员安排。岗位安排不会自动到场、计入可派、授予专业资质或长期物资维护权。</p>']
    for eid,space in state.supply_event_spaces().items():
        if event_id is not None and eid != event_id: continue
        if state.registry.events[eid].status not in {'active','upgraded'}: continue
        try: service.require(space,actor)
        except PermissionError: continue
        rows=service.inspect(space)
        out.append('<h2>'+esc(state.registry.events[eid].name)+'</h2>')
        roster=state.registry.rosters.get(eid)
        out.append('<details><summary>当前人员库与本事件名册</summary>')
        for person in state.registry.library.values():
            entry=roster.entries.get(person.phone) if roster else None
            slot=SLOT_INDEX.get(entry.pos_code) if entry else None
            out.append('<p>'+esc(person.name+' · '+person.phone+' · '+person.unit+' · '+('队长档案' if person.is_leader else '人员档案')+' · '+(slot.name if slot else '已加入，岗位待明确' if entry else '未加入本事件'))+'</p>')
        out.append('</details>')
        for op,label in LABELS.items():
            version=sum(r.reference.object_id=='personnel-'+op for _,r,v in rows)
            hidden=dict(csrf=member.custody_csrf,event_id=eid,operation=op,expected_version=version,request_id=secrets.token_urlsafe(18))
            out.append('<details><summary>'+label+'</summary><form method="post" action="/personnel/manage">'
                +''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in hidden.items()))
            if op=='import_library': out.append('<label>人员CSV（姓名、手机号等原有字段）<textarea name="csv_text" required></textarea></label>')
            elif op=='add_roster': out.append('<label>选择具名人员<select multiple name="phones" required>'+''.join('<option value="'+esc(p.phone)+'">'+esc(p.name+' · '+p.phone)+'</option>' for p in state.registry.library.values())+'</select></label>')
            elif op=='squad':
                out.append('<label>已有中队编号（新增留空）<input name="squad_id"></label><label>中队名称<input name="name" required></label><label>成员<select multiple name="member_phones" required>'+''.join('<option value="'+esc(p.phone)+'">'+esc(p.name+' · '+p.phone)+'</option>' for p in state.registry.library.values())+'</select></label><label>队长手机号（须在所选成员中且已标记队长）<input name="leader_phone" required></label><label>计划人数<input type="number" min="0" name="headcount_planned" value="0" required></label><label>联系说明<input name="contact"></label><p>不替换活动事件已冻结的召集名单；到场及事件职责仍分别办理。</p>')
                out.append('<p>'+esc('；'.join(s.squad_id+' '+s.name+' 队长 '+s.leader_phone for s in state.ledger.squads.values()))+'</p>')
            else:
                out.append('<label>具名人员<select name="phone">'+''.join('<option value="'+esc(p.phone)+'">'+esc(p.name+' · '+p.phone)+'</option>' for p in state.registry.library.values())+'</select></label>')
                if op=='assign_roster':
                    out.append('<label>本事件岗位<select name="pos_code" required>'+''.join('<option value="'+esc(s.pos_code)+'">'+esc(s.name)+'</option>' for s in POSITION_SLOTS)+'</select></label><p>不替代专业职责、组长或复盘负责人专用指定入口。</p>')
                elif op=='review_maintenance': out.append('<label>安排<select name="active"><option value="true">授予</option><option value="false">撤销</option></select></label>')
                elif op=='mark_state': out.append('<label>状态<select name="action"><option value="standby">暂离轮休</option><option value="resume_duty">恢复在岗</option><option value="lost_contact">报告失联</option><option value="contact_restored">恢复联络</option><option value="returned_to_unit">已归建</option><option value="executing">执行中</option></select></label><label>有效命令编号（执行中必填）<input name="order_id"></label><label>说明<input name="note"></label>')
            out.append('<label>具名依据<input name="basis" required></label><button class="lz-btn">明确记录人员安排</button></form></details>')
        import json
        out.append('<details><summary>具名安排历史</summary>'+''.join('<p>'+esc(LABELS[v['operation']]+' · '+r.actor_id+' · '+v['basis']+' · v'+str(r.reference.version))+'</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'+esc(json.dumps(dict(原安排=v['before'],本次安排=v['after']),ensure_ascii=False,indent=2))+'</pre>' for _,r,v in rows)+'</details>')
    return '<div style="overflow-wrap:anywhere;min-width:0">'+''.join(out)+'</div>'


def post(handler,state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    token=handler._member_cookie(); member=state.member_session(token)
    if member is None: handler._respond(403,'<h1>403</h1>'); return
    try:
        f=parse_qs(handler.rfile.read(request_length(handler,256*1024)).decode('utf-8'),keep_blank_values=True,errors='strict',max_num_fields=2000)
        if any(len(v)!=1 for k,v in f.items() if k not in {'phones','member_phones'}): raise ValueError('字段重复。')
        v={k:vals if k in {'phones','member_phones'} else vals[0] for k,vals in f.items()}; op=v.get('operation')
        common={'csrf','event_id','operation','expected_version','request_id','basis'}
        fields={'import_library':{'csv_text'},'add_roster':{'phones'},'assign_roster':{'phone','pos_code'},'review_maintenance':{'phone','active'},'unlock_account':{'phone'},'mark_state':{'phone','action','order_id','note'},'squad':{'squad_id','name','member_phones','leader_phone','headcount_planned','contact'}}
        if op not in fields or set(v)!=common|fields[op]: raise ValueError('字段缺失或越界。')
        if not secrets.compare_digest(v.pop('csrf').encode(),member.custody_csrf.encode()): raise PermissionError('会话变化。')
        raw=v['expected_version']
        if not raw.isascii() or not raw.isdigit() or len(raw)>9: raise ValueError('版本不正确。')
        v['expected_version']=int(raw); details={k:v.pop(k) for k in fields[op]}
        if op=='squad':
            raw=details['headcount_planned']
            if not raw.isascii() or not raw.isdigit() or len(raw)>9: raise ValueError('计划人数不正确。')
            details['headcount_planned']=int(raw)
        if op=='review_maintenance':
            if details['active'] not in {'true','false'}: raise ValueError('安排不明确。')
            details['active']=details['active']=='true'
        state.handle_personnel_management(v.pop('event_id'),member_token=token,details=details,**v)
    except PermissionError: handler._respond(403,'<h1>403</h1>'); return
    except (ValueError,KeyError,TypeError,PanelActionError) as error: handler._respond(409,'<h1>人员安排未保存</h1><p>'+esc(str(error))+'</p>'); return
    handler._redirect('/personnel/manage')
