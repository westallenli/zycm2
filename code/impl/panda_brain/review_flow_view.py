"""Plain-language five-step UI. Opaque references stay in hidden form fields."""
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from html import escape as esc
from urllib.parse import urlencode

from . import review_flow as flow, review_meeting as meeting, review_causality as causal

STEPS=(('timeline','还原过程'),('issues','分析问题与建议'),('rect','落实整改'),('exp','总结经验'),('report','输出报告'))
ACTIONS=dict(received='确认接收',accepted_ready='已接收',conditions='核对执行条件',begin='开始执行',
    submit='提交环节办理',accept='本人核验确认',ongoing='仍在办理',not_applicable='本次不适用',handover='遗留已接收',
    progress='报告进展',completed='报告完成',verify='核验完成',reserve='审定补给',dispatch='发出物资',
    normal='正常',draft='草稿',confirmed='已确认',reported_pending_verification='待核实报告',
    simulated='模拟',reported='人员报告',observed='观测',inferred='分析推测',unknown='未知')


def human(value):
    text=flow.clean(value)
    for code,word in ACTIONS.items():
        text=re.sub(r'(?<![A-Za-z_])'+re.escape(code)+r'(?![A-Za-z_])',word,text)
    return text


def stamp(value):
    if not value:return '未记录'
    try:return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S')
    except ValueError:return '时间待核'


def hidden(key,value):return f'<input type="hidden" name="{esc(key)}" value="{esc(str(value),quote=True)}">'
def field(label,name,value='',*,required=True,area=False,kind='text'):
    attrs=f'name="{name}" class="lz-input"'+(' required' if required else '')
    item=(f'<textarea {attrs} rows="3">{esc(str(value))}</textarea>' if area else
          f'<input {attrs} type="{kind}" value="{esc(str(value),quote=True)}">')
    return '<label>'+esc(label)+item+'</label>'
def form(route,body):return f'<form method="post" action="/review/{route}" class="rf-form">{body}</form>'
def button(label,name='',value=''):
    return f'<button class="lz-btn"'+(f' name="{name}" value="{value}"' if name else '')+'>'+esc(label)+'</button>'
def card(title,body):return '<section class="rf-card"><h3>'+esc(title)+'</h3>'+body+'</section>'
def para(label,value):return '<p><b>'+esc(label)+'：</b>'+esc(human(value or '尚未记录'))+'</p>'


def row_title(row):
    action=row['payload'].get('action','')
    return f'记录 {row["sequence"]} · {row["label"]}'+(' · '+ACTIONS.get(action,action) if action else '')


def evidence(state,refs):
    rows=flow.sources(state); by_ref={r['reference']:r for r in rows}
    by_ref.update({f'audit#{r["sequence"]}':r for r in rows})
    # Audit IDs and revision IDs may refer to the same recorded action.
    for ev in state.workspace.events:
        row=next((r for r in rows if r['sequence']==ev.sequence),None)
        if row:by_ref[ev.id]=row
    links=[]
    for ref in dict.fromkeys(refs):
        row=by_ref.get(ref)
        if row:
            links.append(f'<a href="/review?tab=timeline#record-{row["sequence"]}">'+esc(row_title(row))+'</a>')
    return '<div class="rf-evidence">'+(' · '.join(links) if links else '原始依据尚待补充')+'</div>'


def source_select(state,name='source',selected='',label='选择原始依据',blank=False):
    options='<option value="">暂不选择</option>' if blank else '<option value="" disabled selected>请选择一条原始记录</option>'
    options+=''.join(f'<option value="{r["sequence"]}"'+(' selected' if str(r['sequence'])==str(selected) else '')+'>'
        +esc(row_title(r)+' · '+human(r['summary'])[:90])+'</option>' for r in reversed(flow.sources(state)))
    return '<label>'+esc(label)+f'<select name="{name}" class="lz-select"'+('' if blank else ' required')+'>'+options+'</select></label>'


def task_chains(state):
    rows=flow.sources(state); cards=''
    releases=[r for r in rows if r['kind']=='formal_task_release']
    for release in releases:
        for task in release['payload'].get('tasks',[]):
            oid=task.get('order_id');spec=task.get('spec',{})
            related=[r for r in rows if r['sequence']>=release['sequence'] and oid and (
                r['payload'].get('order_id')==oid or r['payload'].get('task',{}).get('order_id')==oid)]
            body=para('指令',spec.get('objective') or spec.get('title'))
            version=release['payload'].get('draft_version','未记录')
            order=state.workspace.orders.get(oid)
            replaced=bool(order and order.status.value=='superseded')
            body+=para('命令版本',f'第{version}版；'+('已被后续版本替代，以下为原版过程' if replaced else '当前有效版本'))
            recipient=state.registry.library.get(task.get('recipient_id'))
            verifier=state.registry.library.get(task.get('verifier_id'))
            body+=para('执行与核验',(recipient.name if recipient else '执行人未记录')+'；核验人 '+(verifier.name if verifier else '未记录'))
            body+=evidence(state,[release['reference']])
            for label,kinds,actions in (
                ('接收',{'formal_task_receipt'},set()),('执行',{'formal_task_control'},{'conditions','begin'}),
                ('进展',{'formal_task_feedback'},{'progress'}),('完成报告',{'formal_task_feedback'},{'completed'}),
                ('结果核验',{'formal_task_feedback'},{'verify'})):
                matches=[r for r in related if r['kind'] in kinds and (not actions or r['payload'].get('action') in actions)]
                body+=para(label,'；'.join(human(r['summary']) for r in matches) if matches else '暂无对应记录，不能推定已完成')
                if matches:body+=evidence(state,[r['reference'] for r in matches])
            cards+=card(spec.get('title') or '任务过程',body)
    return cards or '<p>尚无可串联的正式任务，已有现场记录见下方。</p>'


def timeline(state,owner,token,privileged):
    from .review_sources import arrival_total
    rows=flow.sources(state)
    from .nine_flow_view import overview
    body=overview(state)+card('先看任务怎样完成',f'<p>累计到场报告 {arrival_total(state)} 人；以下按原始任务串联，现场和物资记录在下方逐条查看。</p>'
        '<p>发生时间缺失的记录按录入顺序放置；补录不会变成当时已知信息。</p>')+task_chains(state)
    # Known times first, unknown times retain audit order in a separate section.
    known=[r for r in rows if r['occurred_at']];unknown=[r for r in rows if not r['occurred_at']]
    known.sort(key=lambda r:(datetime.fromisoformat(r['occurred_at']),r['sequence']))
    def entry(r):
        raw='<details><summary>核对原始记录</summary><pre>'+esc(json.dumps(r['payload'],ensure_ascii=False,indent=2))+'</pre></details>'
        return ('<article class="rf-record" id="record-'+str(r['sequence'])+'"><header><b>'+esc(row_title(r))+'</b>'
            +'<span class="rf-badge">'+esc(r['data_kind'])+'</span></header><p>'+esc(human(r['summary']))+'</p>'
            +'<small>发生 '+esc(stamp(r['occurred_at']))+' · 录入 '+esc(stamp(r['recorded_at']))+'</small>'+raw+'</article>')
    body+=card('过程记录', '<p>按发生时间查看已知过程；发生时间未记录的资料另列，避免把录入先后当作现场先后。</p>'
        +''.join(entry(r) for r in known)+'<h4>发生时间未记录 · 按录入顺序</h4>'+''.join(entry(r) for r in unknown))
    if privileged:
        from .reviewview import _tab_timeline
        # Historical map implementation remains available within this step.
        body+='<details class="rf-card"><summary>查看当时地图与已知资料</summary>'+_tab_timeline(state)+'</details>'
    if owner:
        body+=card('补充现场情况',form('flow-note',field('这条情况说的是什么','subject')
            +field('谁提供了情况','provided_by')+field('具体情况或核实结果','feedback',area=True)
            +source_select(state,'source',label='关联原始记录（可不选）',blank=True)
            +field('实际发生时间（可留空，填写时含时区）','occurred_at',required=False)
            +hidden('request_id','flow-note-'+secrets.token_hex(12))+button('保存补充情况')))
    return body


def model_card(state,token):
    from . import model_connections as models
    config=state.sand_deputy_models.get((token,state.active_event_id),models.Session())
    enabled=config.selected.enabled if config.mode!='local' or config.local else False
    status='当前会话模型已启用' if enabled else '尚未启用模型；现有资料可查看，智能分析尚未运行'
    return (card('让智能体分析本次过程','<p>'+status+'</p><p>分析当前事件的过程记录，给出问题、原因解释与改进建议。结果需负责人核实。</p>'
        +form('flow-analyze',button('分析当前过程'))
        +'<details><summary>模型连接</summary><p>沿用当前会话已配置的模型。以下可设置 DeepSeek；Key 仅保存在会话内存，退出后需重新配置。点击分析会将本事件过程摘要发送至所选模型。</p>'
        +form('flow-model',hidden('model_revision',config.revision)
            +field('模型名称','model',config.cloud.model)+field('API Key（已配置可留空）','api_key',required=False,kind='password')
            +button('保存并启用','action','save')+button('清除并停用','action','disable'))+'</details>'))


def decision_form(state,kind,record):
    key=record.issue_id if kind=='issue' else record.hyp_id
    return ('<details><summary>补充核实并作出结论</summary>'+form('flow-confirm',hidden('kind',kind)+hidden('target_id',key)
        +source_select(state)+field('核实结果及判断理由','reason',area=True)
        +field('还可能有什么解释','alternatives',required=False)
        +field('仍缺哪些事实（未查清则保留待核）','missing',required=False)
        +( '<label>原因层次<select name="layer"><option value="direct">直接原因</option><option value="deep">更深层原因</option></select></label>' if kind=='hypothesis' else '')
        +button('确认原因' if kind=='hypothesis' else '确认问题','decision','confirmed')
        +button('判定不成立','decision','rejected'))+'</details>')


def assessment_history(state,kind,key):
    rows=[r for r in state.workspace.object_revisions.values() if r.reference.event_id==state.active_event_id
        and r.reference.object_kind==causal.KIND and r.reference.object_id==f'{kind}:{key}']
    if not rows:return ''
    body=''
    for row in sorted(rows,key=lambda r:r.reference.version,reverse=True):
        data=json.loads(row.content_json)
        body+=para('核实记录 '+str(row.reference.version),data.get('meeting_finding') or '尚未形成核实结论')
        body+=para('其他解释',data.get('alternatives'))+para('待补事实',data.get('missing_info') or '此版本未登记缺项')
        body+=evidence(state,[*data.get('support_refs',[]),*data.get('counter_refs',[])])
    return '<details><summary>核实记录（'+str(len(rows))+'）</summary>'+body+'</details>'


def issues(state,owner,token):
    body=model_card(state,token) if owner else ''
    analysis=flow.latest(state)
    if analysis:
        value=json.loads(analysis.content_json);expired=flow.stale(state,analysis)
        intro=para('过程总结',value['overview'])+('<p class="rf-warning">已有新资料，这份分析需要重新运行。原分析和核实记录继续保留。</p>' if expired else '<p>以下是智能体建议，尚未确认。</p>')
        body+=card('本轮分析',intro)
        for f in value['findings']:
            content=''.join(para(label,f[key]) for label,key in (
                ('观察到什么','facts'),('影响','impact'),('可能为什么','cause'),('还可能是什么情况','alternatives'),
                ('还需核实','missing'),('建议怎样改','recommendation'),('怎样验收','acceptance')))
            content+='<b>判断依据</b>'+evidence(state,f['support_refs'])
            if f['counter_refs']:content+='<b>需要同时考虑的记录</b>'+evidence(state,f['counter_refs'])
            issue_key='ISSUE-'+state.active_event_id+'-flow-'+__import__('hashlib').sha256((analysis.id+f['key']).encode()).hexdigest()[:16]
            if owner and not expired and issue_key not in state.review_ledger.issues:
                content+=form('flow-decide',hidden('analysis_ref',analysis.id)+hidden('finding',f['key'])
                    +field('负责人核实情况与判断理由','reason',area=True)
                    +button('确认问题，原因继续核实','decision','confirmed')
                    +button('登记为待核实','decision','pending')+button('判定不成立','decision','rejected'))
            body+=card(f['title'],content)
    elif not owner:
        body+=card('本轮分析','<p>负责人尚未运行智能分析。</p>')
    for issue in state.review_ledger.issues.values():
        confirmed=causal.current_confirmation(state,'issue',issue.issue_id)
        word='已确认问题' if confirmed else '已判定不成立' if issue.review_status=='rejected' else '待核实问题'
        content='<span class="rf-badge">'+word+'</span>'+para('发生了什么',issue.description)+para('影响',issue.affected)
        content+=evidence(state,issue.evidence_ids)+para('负责人核实意见',issue.note)
        content+=assessment_history(state,'issue',issue.issue_id)
        if owner:content+=decision_form(state,'issue',issue)
        for hyp in state.review_ledger.hypotheses.values():
            if issue.issue_id not in hyp.related_issues:continue
            verified=causal.current_confirmation(state,'hypothesis',hyp.hyp_id)
            content+=para('已核实原因' if verified else '可能原因（待核实）',hyp.statement)
            unresolved=hyp.unresolved_verifications()
            if unresolved:content+=para('还需要核实','；'.join(unresolved))
            content+=evidence(state,hyp.evidence_ids)
            content+=assessment_history(state,'hypothesis',hyp.hyp_id)
            if owner:content+=decision_form(state,'hypothesis',hyp)
        saved=flow.link(state,issue.issue_id)
        if saved:content+=para('改进建议',saved.get('recommendation'))
        if confirmed:
            content+='<p><a href="/review?tab=rect">安排整改并跟踪验收 →</a></p>'
        body+=card(issue.title,content)
    return body or card('分析问题与建议','<p>尚无问题记录，先还原过程，再由负责人发起分析。</p>')


def member_options(state,name,label):
    return '<label>'+label+f'<select class="lz-select" name="{name}" required><option value="">请选择</option>'+''.join(
        '<option value="'+esc(p.phone)+'">'+esc(p.name+' · '+p.unit)+'</option>'
        for p in state.registry.library.values())+'</select></label>'


def rects(state,owner):
    body=''
    if owner:
        for issue in state.review_ledger.issues.values():
            if not causal.current_confirmation(state,'issue',issue.issue_id):continue
            tasks=[t for t in state.review_ledger.rects.values() if t.source_issue_id==issue.issue_id]
            if tasks:continue
            body+=card('安排整改：'+issue.title,form('flow-rect-draft',hidden('issue_id',issue.issue_id)
                +'<p>原因尚未查清时，也可先针对已确认的问题采取改进措施。</p>'+button('形成整改草案')))
    for task in state.review_ledger.rects.values():
        issue=state.review_ledger.issues.get(task.source_issue_id)
        title=issue.title if issue else '整改事项'
        content='<span class="rf-badge">'+esc(task.status_word())+'</span>'+('<span class="rf-warning">已逾期</span>' if task.is_overdue() else '')
        content+=para('具体措施',task.measures)+para('责任人',task.owner_unit+' '+task.owner_person)
        content+=para('完成期限',task.deadline)+para('验收标准',task.acceptance_standard)+para('验收人',task.verifier_name)
        for row in task.feedback_records:
            content+=para('落实反馈 · '+row['provided_by'],row['content'])+evidence(state,row['evidence_refs'])
        for row in task.verification_records:
            content+=para('验收通过' if row['outcome']=='passed' else '验收未通过',row['finding'])+evidence(state,row['evidence_refs'])
        if owner and task.status=='draft':
            content+=form('flow-rect-publish',hidden('rect_id',task.rect_id)
                +field('具体整改措施','measures',human(task.measures),area=True)
                +member_options(state,'responsible_phone','谁负责落实')+field('完成期限','deadline',kind='date')
                +field('怎样判断整改到位','acceptance_standard',task.acceptance_standard,area=True)
                +member_options(state,'verifier_phone','谁负责验收')+button('审核并发布整改'))
        if owner and task.status=='in_progress':
            content+=form('rect-feedback',hidden('rect_id',task.rect_id)+hidden('provided_by',task.owner_person)
                +field('反馈人承担什么职责','duty')+field('落实情况','content',area=True)
                +source_select(state,'evidence',label='落实依据')+button('保存进展','completed','0')+button('报告完成，提交验收','completed','1'))
        if owner and task.status=='awaiting_verify':
            content+=form('rect-verify',hidden('rect_id',task.rect_id)+hidden('verifier',task.verifier_name)
                +field('按验收标准核对后的结果','finding',area=True)+source_select(state,'evidence',label='验收依据')
                +button('验收通过，关闭整改','outcome','passed')+button('不通过，继续整改','outcome','failed'))
        body+=card(title,content)
    return card('整改进度','<p>负责人集中登记责任人的落实反馈和验收人的核实结果。报告发布后，未完成整改继续跟踪。</p>')+(body or '<p>暂无整改事项；先在第二步确认问题。</p>')


def experiences(state,owner):
    body=''
    for task in state.review_ledger.rects.values():
        if task.status!='closed' or any(e.source_rect_id==task.rect_id for e in state.review_ledger.experiences.values()):continue
        if owner:body+=card('从已验收整改提炼经验',para('已完成的措施',task.measures)
            +form('flow-exp-analyze',hidden('source_kind','rect')+hidden('source_id',task.rect_id)+button('让智能体提炼经验'))
            +'<details><summary>自行记录经验</summary>'+form('exp-propose',hidden('source_kind','rect')+hidden('source_id',task.rect_id)+button('建立待完善草案'))+'</details>')
    for issue in state.review_ledger.issues.values():
        if not owner or not causal.current_confirmation(state,'issue',issue.issue_id) or any(e.source_issue_id==issue.issue_id for e in state.review_ledger.experiences.values()):continue
        body+=card('总结问题教训：'+issue.title,form('flow-exp-analyze',hidden('source_kind','issue')+hidden('source_id',issue.issue_id)+button('让智能体总结教训'))
            +'<details><summary>自行记录教训</summary>'+form('exp-propose',hidden('source_kind','issue')+hidden('source_id',issue.issue_id)+button('建立待完善草案'))+'</details>')
    for exp in state.review_ledger.experiences.values():
        content='<span class="rf-badge">'+esc(exp.status_word())+'</span>'+para('具体做法或教训',exp.content)
        if exp.source_rect_id:
            content+='<p>来自已验收整改 · <a href="/review?tab=rect">核对措施与验收结果</a></p>'
        else:
            content+='<p>来自已核实问题或原因 · <a href="/review?tab=issues">核对问题与判断依据</a></p>'
        content+=para('适用情况',exp.applicability)+para('使用限制',exp.limitations)+evidence(state,exp.evidence_ids)
        if owner and exp.status!='expired':
            content+='<details><summary>完善或修订经验</summary>'+form('flow-exp-revise',hidden('exp_id',exp.exp_id)
                +field('具体做法或教训','content',human(exp.content),area=True)
                +field('以后遇到什么情况可使用','applicability',exp.applicability)
                +field('什么情况下不能直接使用','limitations',exp.limitations)
                +field('修订说明','reason')+source_select(state,'evidence',label='选择经验依据')+button('保存待确认版本'))+'</details>'
            content+=form('exp-decide',hidden('exp_id',exp.exp_id)+field('确认或停用理由','reason')
                +(button('确认收录经验','target','confirmed') if exp.status=='candidate' else
                    source_select(state,'evidence',label='反例依据')+button('停用并保留历史','target','expired')))
        body+=card('经验与教训',content)
    return body or card('总结经验','<p>尚无经验。先确认问题或完成整改，再提炼有事实依据的做法和教训。</p>')


def report_preview(text):
    from .review_exports import blocks
    result=[]
    for kind,value in blocks(text):
        if kind=='heading':
            level,title=value
            result.append(f'<h{min(level+2,6)}>'+esc(human(title))+f'</h{min(level+2,6)}>')
        elif kind=='table':
            result.append('<div class="rf-table"><table>'+''.join('<tr>'+''.join('<td>'+esc(human(cell))+'</td>' for cell in row)+'</tr>' for row in value)+'</table></div>')
        elif kind=='text':
            result.append('<p>'+esc(human(re.sub(r'^\s*[-*>]\s*','',value)))+'</p>')
    return ''.join(result)


def reports(state,owner,selected):
    from .review_reports import freshness_error
    rows=sorted(state.review_ledger.reports.values(),key=lambda r:r.version,reverse=True)
    active=state.registry.events[state.active_event_id].status!='closed'
    body=card('生成结构化报告','<p>包含事件概况、过程还原、问题与原因、整改落实、经验教训及待核实事项。</p>'
        +(form('report-generate',(hidden('stage','1') if active else '')+button('生成阶段复盘草稿' if active else '生成复盘报告草稿')) if owner else '')
        +('<p>当前事件仍在活动，阶段草稿可编辑和导出；最终审核发布需事件结束。</p>' if active else ''))
    body+='<div class="rf-versions">'+''.join('<a href="/review?'+esc(urlencode(dict(tab='report',rid=r.report_id)),quote=True)+'">'
        +f'第 {r.version} 版 · '+esc(r.status_word())+'</a>' for r in rows)+'</div>'
    report=next((r for r in rows if r.report_id==selected),rows[0] if rows else None)
    if not report:return body
    warning=freshness_error(state,report)
    content=para('报告范围','阶段复盘（非最终结案）' if report.stage_report else '事件复盘')
    content+=para('生成时间',stamp(report.created_at))+para('审核状态',report.status_word())
    if warning:content+='<p class="rf-warning">'+esc(warning)+'</p>'
    content+='<p>'+ ' · '.join('<a download href="/export/review?'+esc(urlencode(dict(
        event_id=state.active_event_id,report_id=report.report_id,format=fmt)),quote=True)+'">下载 '+label+'</a>'
        for fmt,label in (('docx','Word'),('pdf','PDF')))+'</p>'
    if report.source_snapshot_json:
        content+='<details><summary>核对这份报告的生成依据</summary><p><a href="/review/report-source?'+esc(urlencode(dict(event_id=state.active_event_id,report_id=report.report_id)),quote=True)+'">生成时原始资料</a></p><p>此处固定展示本版本资料；后续状态在整改步骤查看。</p></details>'
    content+='<details><summary>当前整改状态（后续更新，不改写报告正文）</summary>'+''.join(
        para(t.status_word(),t.measures) for t in state.review_ledger.rects.values())+'<a href="/review?tab=rect">查看落实与验收</a></details>'
    content+='<div class="rf-report">'+report_preview(report.content_md)+'</div>'
    if owner and report.status in {'ai_draft','edited','rejected'}:
        content+='<details><summary>编辑报告正文</summary>'+form('report-edit',hidden('report_id',report.report_id)
            +field('报告正文','content_md',report.content_md,area=True)+button('保存人工编辑'))+'</details>'
    if owner and not active and not warning:
        targets={'edited':('pending_review','提交审核'),
            'pending_review':('published','审核通过并发布')}
        if report.status in targets:
            code,label=targets[report.status];content+=form('report-status',hidden('report_id',report.report_id)+hidden('target',code)+button(label))
    body+=card(f'第 {report.version} 版报告',content)
    return body


def render(state,*,role,viewer_word,tab,message,selected_report,review_token,review_deputy):
    state=meeting.scoped_state(state)
    owner=role=='owner';privileged=role in {'owner','commander'} or review_deputy
    # Old bookmarked pages converge into the five steps; no parallel UI survives.
    tab={'summary':'timeline','root':'issues','analysis':'issues','common':'issues','trend':'issues'}.get(tab,tab)
    if tab not in dict(STEPS):tab='timeline'
    event=state.registry.events[state.active_event_id]
    nav='<nav class="rf-steps" aria-label="复盘步骤">'+''.join(
        '<a class="'+('active' if code==tab else '')+'" href="/review?tab='+code+'"><small>'+str(i)+'</small>'+label+'</a>'
        for i,(code,label) in enumerate(STEPS,1))+'</nav>'
    if not privileged:
        main=card('复盘资料','<p>本事件复盘由指定负责人集中操作。详细过程和分析仅获准人员可查看。</p>')
    elif tab=='timeline':main=timeline(state,owner,review_token,privileged)
    elif tab=='issues':main=issues(state,owner,review_token)
    elif tab=='rect':main=rects(state,owner)
    elif tab=='exp':main=experiences(state,owner)
    else:main=reports(state,owner,selected_report)
    if privileged:
        # Evidence options use sequence numbers for readability. Existing domain
        # handlers need immutable IDs; map only their evidence selects here.
        refs={str(r['sequence']):r['reference'] for r in flow.sources(state)}
        def map_select(m):
            return re.sub(r'value="(\d+)"',lambda n:'value="'+esc(refs.get(n[1],n[1]),quote=True)+'"',m[0])
        main=re.sub(r'<select name="evidence".*?</select>',map_select,main,flags=re.S)
    if owner:
        fields=meeting.form_fields(state,review_token)
        main=re.sub(r'(<form\b[^>]*action="/review/[^"]+"[^>]*>)',lambda m:m[1]+fields,main)
    if review_deputy and not meeting.owner(state):
        main=meeting.meeting_card(state,review_token,deputy=True)+main
    person=state.registry.library.get(meeting.owner(state))
    header='<header class="rf-header"><div><small>破局智援参谋 · 单事件复盘</small><h2>'+esc(event.name)+'</h2></div><p>负责人：'+esc(person.name if person else '待指定')+'<br>'+esc(viewer_word)+'</p></header>'
    idx=[k for k,_ in STEPS].index(tab)
    footer='<div class="rf-next">'+('<a href="/review?tab='+STEPS[idx-1][0]+'">← '+STEPS[idx-1][1]+'</a>' if idx else '<span></span>')
    footer+=('<a href="/review?tab='+STEPS[idx+1][0]+'">下一步：'+STEPS[idx+1][1]+' →</a>' if idx<4 else '')+'</div>'
    return '<link rel="stylesheet" href="/static/review-flow.css"><div class="rf-page">'+header+nav+(
        '<p role="status" class="lz-alert">'+esc(message)+'</p>' if message else '')+main+footer+'</div>'


def markdown(state):
    """Readable body; exact immutable references remain in report source snapshot."""
    state=meeting.scoped_state(state);event=state.registry.events[state.active_event_id]
    from .review_sources import arrival_total
    lines=['# 本次火情复盘报告','','## 一、事件概况',f'- 事件：{event.name}',
        f'- 累计到场报告：{arrival_total(state)} 人（不等于当前可派人数）',
        '- 文档状态：系统草稿，待负责人审核。',
        '- 资料边界：仅依据本事件已登记资料；模拟记录不代表真实现场。',
        '','## 二、过程还原','发生时间和录入时间分别列示；缺失发生时间不据录入先后推断现场先后。']
    for r in flow.sources(state):
        lines.append(f'- 记录 {r["sequence"]}：{r["label"]}；{human(r["summary"])}；发生 {stamp(r["occurred_at"])}；录入 {stamp(r["recorded_at"])}；{r["data_kind"]}')
    from .review_replay import replay,plan_comparisons
    snapshot,_=replay(state.workspace,state.active_event_id)
    lines+=['- '+human(row) for row in plan_comparisons(snapshot)]
    for feedback in state.workspace.reports.values():
        lines.append('- 执行反馈：'+human('；'.join(getattr(feedback,'observations',()) or getattr(feedback,'notes',()) or (str(feedback),))))
    from .nine_flow import markdown as nine_markdown
    lines+=['',nine_markdown(state),'','## 三、问题与原因分析']
    analysis=flow.latest(state)
    if analysis:
        value=json.loads(analysis.content_json)
        lines+=['智能体过程总结：'+human(value['overview']),
            '分析资料已更新，以下旧建议须重新核对。' if flow.stale(state,analysis) else '以下模型建议不等于已确认结论。']
        for f in value['findings']:
            lines.extend([f'### {human(f["title"])}',f'事实：{human(f["facts"])}',f'影响：{human(f["impact"])}',
                f'可能原因：{human(f["cause"])}',f'其他解释：{human(f["alternatives"])}',
                f'改进建议：{human(f["recommendation"])}',f'待核实：{human(f["missing"]) or "由负责人核对"}',
                '依据：'+ '、'.join('记录 '+str(n) for n in f['evidence'])])
    else:lines.append('尚未运行当前事件的模型分析；下列为已有人工/模拟会议记录。')
    for issue in state.review_ledger.issues.values():
        lines += [f'### {human(issue.title)}',
            '负责人结论：'+('已确认问题' if causal.current_confirmation(state,'issue',issue.issue_id) else human(issue.status_word())),
            human(issue.description)]
        for hyp in state.review_ledger.hypotheses.values():
            if issue.issue_id in hyp.related_issues:
                basis=causal.assessments(state).get('hypothesis:'+hyp.hyp_id)
                data=json.loads(basis.content_json) if basis else {}
                verified=causal.current_confirmation(state,'hypothesis',hyp.hyp_id)
                status=('负责人已核实'+{'direct':'直接','deep':'深层'}.get(data.get('causal_layer'),'')+'原因：' if verified else
                    '旧确认已过期，待重新核实：' if hyp.confirmed_assessment_ref else '可能原因（待核实）：')
                lines.append(status+human(hyp.statement))
                lines+=['其他解释：'+human(data.get('alternatives') or hyp.counter_examples),
                    '核实记录：'+human(data.get('meeting_finding') or '尚未核实')]
                lines+=['负责人意见：'+human(line) for line in hyp.history[-3:]]
    lines+=['','## 四、整改措施与落实进度']
    for t in state.review_ledger.rects.values():
        lines += [f'- 措施：{human(t.measures)}；状态：{t.status_word()}；责任：{t.owner_unit} {t.owner_person}；期限：{t.deadline or "待确定"}；验收标准：{human(t.acceptance_standard) or "待确定"}；验收人：{t.verifier_name or "待确定"}']
        lines += ['  - 落实反馈：'+human(r['content']) for r in t.feedback_records]
        lines += ['  - '+('验收通过：' if r['outcome']=='passed' else '验收未通过，退回整改：')+human(r['finding']) for r in t.verification_records]
    if not state.review_ledger.rects:lines.append('暂无整改安排。')
    lines+=['','## 五、经验与教训']
    for e in state.review_ledger.experiences.values():
        lines += [f'- {human(e.content)}；{e.status_word()}；适用：{human(e.applicability)}；条件：{human("；".join(e.condition_tags))}；限制：{human(e.limitations)}']
    if not state.review_ledger.experiences:lines.append('尚无已登记经验。')
    lines+=['','## 六、待核实与后续事项']
    pending=[human(i.title)+'：'+human(i.to_verify or '待负责人核实') for i in state.review_ledger.issues.values() if not causal.current_confirmation(state,'issue',i.issue_id) and i.review_status!='rejected']
    pending += [human(h.statement)+'：'+'；'.join(human(x) for x in h.unresolved_verifications()) for h in state.review_ledger.hypotheses.values() if h.status=='pending']
    pending += [human(t.measures)+'：'+t.status_word() for t in state.review_ledger.rects.values() if t.status!='closed']
    from .review_pending import business_pending
    pending += [human(text) for text in business_pending(state)]
    lines += ['- '+p for p in pending] or ['当前无已登记待办，不代表未记录事项已完成。']
    lines+=['','## 七、审核与资料说明','报告草稿由负责人补充、审核；未完成整改不因报告生成而关闭。原始记录与生成时来源快照独立保存，可按过程中的记录序号核对。']
    return '\n'.join(lines)
