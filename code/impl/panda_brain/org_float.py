"""Shared full-width organisation view; real event projections and original actions."""
import secrets
from .workbench import esc
from .org_model import POSITION_SLOTS,SLOT_INDEX
from . import homepage as home, unified_ui as ui


def projected_rows(state,event_id):
    rows={r['phone']:dict(r) for r in home.person_rows_for_group(state.registry,state.workspace,event_id)}
    for p in state.event_group_people(event_id).values():
        person=state.registry.library.get(p.phone)
        slot=SLOT_INDEX.get(p.pos_code)
        previous=rows.get(p.phone,{})
        rows[p.phone]=dict(previous,name=p.name,phone=p.phone,unit=p.unit,
            home_position=person.home_position if person else '',pos_code=p.pos_code,
            slot_name=slot.name if slot else '',group=home.group_of_slot(p.pos_code) if slot else '',
            status_word=('已到场' if p.arrived and previous.get('status_word','未到场') in {'未到场','待到场','预编入'} else previous.get('status_word','已到场' if p.arrived else '未到场')),
            state_basis=p.presence_basis,is_arrived=p.arrived)
    for row in rows.values(): row.setdefault('is_arrived',row['status_word']=='已到场')
    return sorted(rows.values(),key=lambda r:(r['name'],r['phone']))


def panel(key,title,body):
    return ('<section class="org-float" id="org-'+key+'" aria-label="'+title+'" hidden>'
        '<header class="org-float-head"><h2>'+title+'</h2><button type="button" data-org-close aria-label="收起'+title+'">收起</button></header>'
        '<div class="org-float-body">'+body+'</div></section>')


def render(state,token,*,event_id,viewer_phone,generated_at):
    record=state.registry.events[event_id]
    people=state.temporary_people(event_id)
    groups=state.event_group_people(event_id)
    pending=[p for p in people.values() if not p.pos_code]
    assigned=[p for p in people.values() if p.pos_code]
    member=state.member_session(token)
    arrivals={p.phone:p.arrived_at for p in people.values() if p.arrived_at}
    arrivals.update({s.phone:s.arrived_at for s in state.member_sessions.values() if s.bound_event_id==event_id and s.arrived_at})
    canvas,details=home.build_org_canvas(home.org_layout(),registry=state.registry,workspace=state.workspace,
        event_id=event_id,viewer_role='commander',viewer_phone=viewer_phone,arrivals=arrivals,
        generated_at=generated_at,projected_rows=projected_rows(state,event_id))
    split=canvas.index('<div class="hp-zoombar">')
    diagram,zoom=canvas[:split],canvas[split:]
    def form(action,content,label):
        return ('<form method="post" action="'+action+'" data-org-action>'
            + ''.join('<input type="hidden" name="'+k+'" value="'+esc(v)+'">' for k,v in
                dict(event_id=event_id,request_id=secrets.token_urlsafe(18)).items())
            +content+'<button class="lz-btn" type="submit">'+label+'</button><p class="org-submit-message" role="status"></p></form>')
    options=''.join('<option value="'+s.pos_code+'">'+esc(s.name)+'</option>' for s in POSITION_SLOTS)
    pool='<p>待安排 <b>'+str(len(pending))+'</b> 人 · 已编组 <b>'+str(len(assigned))+'</b> 人</p>'
    pool+='<p class="lz-note">本事件已有临时人员，登记、岗位安排和到场分别记录。</p>'
    pool+='<div class="org-filter"><button type="button" data-org-filter="all">全部</button><button type="button" data-org-filter="pending">待安排</button><button type="button" data-org-filter="assigned">已安排</button></div>'
    pool+='<div class="org-table-scroll"><table class="lz-table"><thead><tr><th>姓名</th><th>单位</th><th>岗位</th><th>到场</th><th>联系电话</th></tr></thead><tbody>'
    for p in sorted(people.values(),key=lambda p:(p.name,p.phone)):
        slot=SLOT_INDEX.get(groups.get(p.phone,p).pos_code)
        pool+='<tr data-org-person="'+p.phone+'" data-arranged="'+('assigned' if p.pos_code else 'pending')+'">'+''.join('<td>'+esc(v)+'</td>' for v in
            (p.name,p.unit,slot.name if slot else '待安排','已确认到场' if p.arrived_at else '未确认到场',p.phone))+'</tr>'
    pool+='</tbody></table></div>'
    if not people:pool+='<p>暂无待安排临时人员；当前库为空。</p>'
    pool+='<button class="lz-btn" type="button" data-org-open="arrange">打开调组安排</button>'
    choices=''.join('<label class="org-check"><input type="checkbox" name="phone" value="'+p.phone+'">'+esc(p.name+' · '+p.unit)+'</label>' for p in pending)
    arrange='<h3>初次岗位安排</h3>'+(form('/temporary/assign',choices+'<label>编入岗位<select class="lz-input" name="pos_code">'+options+'</select></label>','确认编组') if pending else '<p>暂无待安排临时人员。</p>')
    movable=[p for p in groups.values() if p.pos_code]
    select=''.join('<option value="'+p.phone+'">'+esc(p.name+' · '+SLOT_INDEX[p.pos_code].name)+'</option>' for p in sorted(movable,key=lambda p:p.name))
    arrange+='<h3>按人调组</h3>'+(form('/personnel/transfer','<label>当前人员<select class="lz-input" name="phone">'+select+'</select></label><label>目标岗位<select class="lz-input" name="to_pos_code">'+options+'</select></label>','确认调组') if movable else '<p>暂无已安排人员。</p>')
    arrange+='<p class="lz-note">由本事件副总指挥办理。调组改变本事件岗位安排，到场、队伍归属与任务状态继续按原记录核对。</p>'
    buttons='<button class="org-primary" type="button" data-org-open="pool" aria-controls="org-pool" aria-expanded="false">临时岗位人员库 <small>'+str(len(people))+' 人 · 待安排 '+str(len(pending))+'</small></button>'
    buttons+='<button class="org-primary" type="button" data-org-open="arrange" aria-controls="org-arrange" aria-expanded="false">调组安排 <small>初次安排 / 按人调组</small></button>'
    for key,label in [('details','节点详情'),('records','全部人员'),('tools','图形工具'),('related','其他人员功能')]:
        buttons+='<button type="button" data-org-open="'+key+'" aria-controls="org-'+key+'" aria-expanded="false">'+label+'</button>'
    related=''.join('<button type="button" class="lz-btn lz-btn2" data-org-url="'+path+'?event_id='+esc(event_id)+'">'+label+'</button>' for path,label in ui.CHILDREN['/'] if path!='/personnel/arrangements')
    related+='<iframe id="org-related-frame" title="人员功能子模块" hidden></iframe>'
    html=('<link rel="stylesheet" href="/static/org-float.css">'+home._org_css()
        +'<div class="org-meta"><div><span>人员架构 · 当前事件</span><h1>'+esc(record.name)+'</h1></div><p>'+esc(event_id)+' · '+esc(record.status_word())+'</p></div>'
        +'<div id="org-stage" class="org-stage"><div class="hp-left">'+diagram
        +'<nav class="org-rail" aria-label="人员架构功能">'+buttons+'</nav>'
        +panel('pool','临时岗位人员库',pool)+panel('arrange','调组安排',arrange)
        +panel('details','节点详情',details)+panel('records','全部人员与到场',ui.event_records(state,'/'))
        +panel('tools','图形工具',zoom+'<p>点击结构图节点查看人员；可缩放、恢复自适应或滚动主图。</p><button class="lz-btn lz-btn2" type="button" data-org-refresh>刷新结构图</button>')
        +panel('related','其他人员功能',related)+'</div></div><script src="/static/org-float.js" defer></script>')
    return html
