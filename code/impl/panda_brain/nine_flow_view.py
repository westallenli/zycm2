"""Readable work/evidence/verification page, reachable from every work area."""
import json,secrets
from html import escape as esc
from . import nine_flow as flow
from .sand_shell import WORK_ITEMS
from .review_flow_view import hidden,field,evidence,stamp,human,row_title

LINKS=(
    (('/events/{event}/details','火情资料'),('/sand','现场处置')),
    (('/sand#cmd-advisor','智能参谋'),('/sand#cmd-situation','态势复核')),
    (('/materials/event','本火情人员与物资'),('/personnel/arrangements','岗位与人员安排')),
    (('/sand#cmd-plans','任务部署'),('/sand/task-receipts','当前版本接收')),
    (('/sand/task-feedback','执行反馈与核验'),('/sand','现场地图')),
    (('/sand/task-receipts','清理看守任务'),('/sand/task-feedback','复查反馈')),
    (('/sand#cmd-advisor','险情与态势'),('/materials/event','救援保障')),
    (('/events/{event}/details','火情来源'),),
    (('/sand/task-feedback','任务核验'),('/materials/closeout','物资收尾'),('/materials/custody','撤离接收'),('/review?tab=rect','遗留整改')))

def url(step):return '/workflow/records?step='+str(step)
def form(member,eid,step,action,body):
    return ('<form method="post" action="/workflow/records" class="nine-form">'
        +''.join(hidden(k,v) for k,v in dict(event_id=eid,csrf=member.custody_csrf,step=step,action=action,
            request_id='nine-'+secrets.token_hex(16)).items())+body+'</form>')
def options(values,selected=''):
    return ''.join('<option value="'+esc(k,quote=True)+'"'+(' selected' if k==selected else '')+'>'+esc(v)+'</option>' for k,v in values.items())

def overview(state):
    return ('<section class="rf-card"><h3>九步过程与交接</h3><p>查看各环节本次范围、具名核验和未完成事项；模拟记录保持模拟性质。</p>'
        +'<ul>'+''.join('<li><a href="'+url(x['step'])+'">'+str(x['step']+1).zfill(2)+' '+esc(WORK_ITEMS[x['step']][0])+'</a>：'+esc(x['status'])+'</li>' for x in flow.summary(state))
        +'</ul></section>')

def page(state,token,step=0,message=''):
    member,record=flow._member(state,token)
    if type(step) is not int or step not in range(9):raise ValueError('未知工作环节。')
    rows=flow.summary(state);row=rows[step];r,v=row['revision'],row['value']
    deputy=record.event_id in state.deputy_events(token);active=record.status in {'active','upgraded'}
    body=['<section class="lz-card nine-flow">']
    body.append('<h1>九步办理与交接</h1>')
    body.append('<nav class="nine-steps" aria-label="九步办理">'+''.join('<a href="'+url(i)+'"'+(' aria-current="step"' if i==step else '')+'>'+str(i+1).zfill(2)+' '+esc(WORK_ITEMS[i][0])+'</a>' for i in range(9))+'</nav>')
    body.append('<p class="nine-meta" title="'+esc(flow.GUIDANCE[step])+'">'+str(step+1).zfill(2)+' · '+esc(WORK_ITEMS[step][0])+' · <b>'+esc(row['status'])+'</b></p>')
    what,did,you,finish=flow.GUIDE[step]
    body.append('<div class="nine-guide">'
        '<p><i>干什么</i><b>'+esc(what)+'</b></p>'
        '<p><i>参谋已做</i><b class="ok">'+esc(did)+'</b></p>'
        '<p><i>你要做</i><b class="me">'+esc(you)+'</b></p>'
        '<p><i>做完标志</i><b>'+esc(finish)+'</b></p></div>')
    if message:body.append('<p role="status" class="nine-message">'+esc(message)+'</p>')

    # ——— 一个编号清单，只列要办的事；已办完的标"已完成" ———
    try:
        state.duty_subject(token); is_duty=True
    except PermissionError:
        is_duty=False
    actor_role='值班员' if is_duty else ('副指挥长' if deputy else '其他人员')
    owner=flow.GUIDE_OWNER[step]
    can_act=(actor_role==owner)
    verifier_now=bool(can_act and active and v and v['action']=='submit' and member.phone==v['verifier_id'])
    body.append('<p class="nine-who">这一步由 <b>'+esc(owner)+'</b> 完成'
                +('' if can_act else '；你现在是'+esc(actor_role)+'，这一步不需要你操作')+'</p>')
    acts=[]
    def add(title,status,action='',done=False):acts.append((title,status,action,done))
    if can_act and v and v['action']=='submit' and active and member.phone==v['verifier_id']:
        add('<b>要你确认这一版</b><small>看完内容，点确认或退回</small>','等你确认',
            form(member,record.event_id,step,'verify',hidden('proposal_ref',r.id)+hidden('expected_version',r.reference.version)
                +'<button class="lz-btn" name="decision" value="accept">确认通过</button> '
                +'<button class="lz-btn lz-btn2" name="decision" value="return">退回补充</button>'))
    elif can_act and v and v['action']=='submit':
        who=state.registry.library.get(v['verifier_id'])
        add('<b>已请 '+esc(who.name if who else '对方')+' 确认</b>'
            '<small>他用自己的账号点确认后才算完，代点不算</small>','等他确认')
    left_html=('' if not (can_act and v and v.get('remaining')) else
               '<p class="nine-left"><b>还没解决（系统记录）：</b>'+esc(v['remaining'])+'</p>')

    observations=[(p,x) for p,x in flow.history(state.workspace,flow.OBS) if x['step']==step]
    observe_html=''
    if can_act and active and deputy:
        from .review_flow import sources
        source_rows=[x for x in reversed(sources(state)) if x['kind']!=flow.CHECK]
        choices=''.join('<option value="'+esc(x['reference'],quote=True)+'"'+(' selected' if i==0 else '')+'>'+esc(row_title(x)+' · '+human(x['summary'])[:70])+'</option>' for i,x in enumerate(source_rows))
        select='<label>引用原始依据（可多选）<select class="lz-select" name="refs" multiple size="5" required>'+choices+'</select></label>'
        nature='<label>资料性质<select class="lz-select" name="data_kind">'+options(dict(reported='具名人员报告',simulated='模拟演练资料'), 'simulated' if getattr(state,'resource_demo',{}).get('simulated') else 'reported')+'</select></label>'
        own=''
        if observations:
            own='<label>本环节依据<select class="lz-select" name="refs" multiple size="4" required>'+''.join('<option value="'+p.id+'"'+(' selected' if i==0 else '')+'>'+esc(x['subject'])+'</option>' for i,(p,x) in enumerate(observations))+'</select></label>'
        # 能预填的不留空：标题给下拉、位置取事件地点、时间取当前、正文给框架。
        from datetime import datetime,timedelta,timezone
        now=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='minutes')
        head=str(step+1).zfill(2)+' '+WORK_ITEMS[step][0]
        titles=list(dict.fromkeys([head+' · 现场情况',head+' · 办理动作','协调与支援','异常与困难']))
        subject_field=('<label>记录标题<select class="lz-select" name="subject">'
            +''.join('<option value="'+esc(t,quote=True)+'">'+esc(t)+'</option>' for t in titles)+'</select></label>')
        frame='现场情况：\n办理动作：\n结果：'
        hint='<p class="nine-hint">照着三行填内容；依据已默认选好一条；点最下面的「保存过程记录」。</p>'
        # "登记过程记录"是参谋/专家补账用的，不再占待办位；收进折叠。
        observe_html=('<details class="nine-fold"><summary>补充过程记录（参谋／专家用）</summary>'
            +form(member,record.event_id,step,'observe',
                hidden('expected_version',0)+hint+subject_field+field('实际情况、办理动作与结果','feedback',frame,area=True)
                +field('位置或适用范围','location',record.location)
                +field('实际发生时间（含时区，可留空）','occurred_at',now,required=False)+select+nature
                +'<button class="lz-btn">保存过程记录</button>')+'</details>')
        if observations:
            # 简化：内容全部由系统带（隐藏字段），人只需要选"下给谁"和"结果"，点一下。
            scope_text=v['scope'] if v else head+' 本环节范围'
            carried=(hidden('scope',scope_text)
                +hidden('feedback',v['feedback'] if v else frame)
                +hidden('basis',v['basis'] if v else '结论依据：')
                +hidden('remaining',v['remaining'] if v else '')
                +hidden('followup',v['followup'] if v else '')
                +hidden('data_kind','simulated' if getattr(state,'resource_demo',{}).get('simulated') else 'reported')
                +''.join(hidden('refs',p.id) for p,_ in observations[:1]))
            add('<b>请人确认这一版</b><small>选一个人，他看到后会点确认或退回</small>','待发出',
            '<details class="nine-act"><summary class="lz-btn">去请人确认</summary>'+form(member,record.event_id,step,'submit',
                hidden('expected_version',flow.versions(state,flow.CHECK,'step-'+str(step)))
                +hidden('source_digest',flow.digest(state))+carried
                +'<p class="nine-sum">要确认的内容：<b>'+esc(scope_text)+'</b></p>'
                +'<label>请谁确认<select class="lz-select" name="verifier_id">'
                +options({p:n for p,n in flow.candidates(state).items() if p!=member.phone})+'</select></label>'
                +'<label>这一版的结果<select class="lz-select" name="result">'
                +options({k:s for k,s in flow.RESULTS.items() if k!='handover' or step==8},v['result'] if v else 'ongoing')+'</select></label>'
                +'<button class="lz-btn">发出，等对方确认</button>')+'</details>')
    elif not active:body.append('<p class="lz-note">事件已结束，记录只读。</p>')

    # ——— 主操作：就是上面"你要做"那句话对应的事，一个按钮直达 ———
    if can_act and active and not verifier_now:
        label,href=flow.ACTION_GUIDE[step]
        body.append('<div class="nine-do"><a class="lz-btn" href="'
            +esc(href.format(event=record.event_id),quote=True)+'">'+esc(label)+'</a></div>')

    inner=[]
    if acts:
        inner.append('<ol class="nine-todo">')
        for index,(title,status,action,done) in enumerate(acts,1):
            attr=' class="done"' if done else ''
            # 展开式表单单独占一行；行内按钮（核验/退回）才留在右侧。
            block=action if '<details' in action else ''
            inline='' if block else action
            inner.append(f'<li{attr}><div class="nine-todo-row"><i>{index}</i><div>{title}</div>'
                         f'<em>{esc(status)}</em>{inline}</div>{block}</li>')
        inner.append('</ol>')
    if observe_html:inner.append(observe_html)
    if left_html:inner.append(left_html)
    if inner:
        if verifier_now:
            # 轮到你确认时，按钮直接摊开，不折。
            body.extend(inner)
        else:
            body.append('<details class="nine-fold"><summary>记录与确认（参谋／专家用）</summary>'
                        +''.join(inner)+'</details>')
    # 没有额外记录时不写"没有待你处理的事"——上面已经有"你要做"和主按钮。

    # ——— 详细记录全部收进折叠 ———
    if v:
        detail=['<details class="nine-fold"><summary>本版办理记录 · 版本 '+str(r.reference.version)+'</summary>']
        for key,label in [('scope','本次核对范围'),('feedback','现场情况、动作与结果'),('basis','核对依据'),('remaining','尚未完成／遗留事项'),('followup','跟进责任、期限与接收安排')]:
            detail.append('<p><b>'+label+'：</b>'+esc(v[key] or '无')+'</p>')
        verifier=state.registry.library.get(v['verifier_id'])
        proposal=state.workspace.object_revisions.get(v['proposal_ref']) if v['proposal_ref'] else r
        submitter=state.registry.library.get(proposal.actor_id) if proposal else None
        detail.append('<p>办理登记者：'+esc(submitter.name if submitter else '待核对')+'；指定核验人'+('／遗留接收人' if v['result']=='handover' else '')+'：'+esc(verifier.name if verifier else '待核对')+'；记录 '+esc(stamp(r.times.recorded_at))+'</p>')
        detail.append(evidence(state,v['refs'])+'</details>')
        body.append(''.join(detail))
    body.append('<details class="nine-fold"><summary>本环节过程与依据 · '+str(len(observations))+' 条</summary>')
    if not observations:body.append('<p class="lz-note">暂无本环节补充。</p>')
    for p,x in observations:
        body.append('<article class="nine-record"><h4>'+esc(x['subject'])+' · '+('模拟资料' if p.data_kind=='simulated' else '具名报告')+'</h4><p>'+esc(x['feedback'])+'</p><p>'+esc(x['location'])+'；发生 '+esc(stamp(p.times.occurred_at))+'</p>'+evidence(state,[p.id]+x['refs'])+'</article>')
    body.append('</details>')
    history=[(p,x) for p,x in flow.history(state.workspace,flow.CHECK) if x['step']==step]
    if history:
        body.append('<details class="nine-fold"><summary>历史办理与核验 · '+str(len(history))+' 条</summary>'+''.join('<p>版本 '+str(p.reference.version)+' · '+esc(dict(submit='提交办理',accept='本人核验确认',**{'return':'退回补充'})[x['action']])+' · '+esc(flow.RESULTS[x['result']])+' · '+esc(stamp(p.times.recorded_at))+'</p>' for p,x in reversed(history))+'</details>')
    body.append('<div class="nine-links">'+''.join('<a class="lz-btn lz-btn2" href="'+esc(p.format(event=record.event_id),quote=True)+'">'+esc(label)+'</a>' for p,label in LINKS[step])
                +'<a class="lz-btn lz-btn2" href="/workflow/guide">返回九步走</a></div>')
    body.append('</section>')
    return ('<link rel="stylesheet" href="/static/nine-flow.css">'
            '<link rel="stylesheet" href="/static/nine-todo.css">'+''.join(body))

def post(state,token,form):
    # Reject duplicate scalars and unexpected fields rather than picking a value.
    if any(len(v)!=1 for k,v in form.items() if k!='refs'):raise ValueError('办理字段不能重复。')
    f={k:v[0] for k,v in form.items() if k!='refs'}
    member,event=flow._member(state,token,write=True)
    if f.get('csrf')!=member.custody_csrf or f.get('event_id')!=event.event_id:raise PermissionError('事件或本人会话已变化，请刷新。')
    common={'event_id','csrf','step','action','request_id','expected_version'}
    action=f.get('action')
    required=({'subject','feedback','location','occurred_at','refs','data_kind'} if action=='observe' else
        set(flow.FIELDS)|{'refs','data_kind','source_digest'} if action=='submit' else {'proposal_ref','decision'} if action=='verify' else set())
    if not required or set(form)!=common|required:raise ValueError('办理字段不完整或含未知字段。')
    if not f['step'].isascii() or not f['step'].isdigit() or int(f['step']) not in range(9):raise ValueError('工作环节无效。')
    if not f['expected_version'].isascii() or not f['expected_version'].isdigit():raise ValueError('办理版本无效。')
    step=int(f['step']);kw=dict(step=step,expected_version=int(f['expected_version']),request_id=f['request_id'])
    if action=='observe':flow.observe(state,token,**kw,**{k:f[k].strip() for k in ('subject','feedback','location','occurred_at','data_kind')},refs=form['refs'])
    elif action=='submit':flow.submit(state,token,**kw,values={k:f[k].strip() for k in flow.FIELDS},refs=form['refs'],data_kind=f['data_kind'],expected_source_digest=f['source_digest'])
    else:flow.verify(state,token,**kw,proposal_ref=f['proposal_ref'],decision=f['decision'])
    return url(step)
