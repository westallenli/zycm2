"""Result-first assistance; original decisions remain in their named-role modules."""
from .workbench import esc
from . import sop_assistance as sa
from . import sop_progress

WORDS={'pending':'已发送系统内补问，等待相关人员答复','answered':'已自动收录具名补充，原记录字段仍保持',
       'escalated':'相关人员无法补齐，需协调处理','resolved':'资料问题已重新核对解除','cancelled':'接收职责或资料版本已变化，原补问停止'}


def current(state,eid):
    report=sa.report(state,eid)
    runtime=getattr(state,'assistant_runtime',None)
    report['questions']=runtime.questions(eid) if runtime else []
    report['auto_followups']=bool(runtime and runtime.enabled)
    report['service_error']=(runtime.journal.error or runtime.policy_error or runtime.errors.get(eid,'')) if runtime else ''
    for q in report['questions']:
        if q['status']=='answered':
            for finding in report['steps'][q['step']]['findings']:
                if finding['key']==q['key'] and finding['basis']==q['basis']:
                    finding['status']='已收到具名补充'
                    finding['text']+='；已收到具名补充并自动收录（来源自报，原字段尚未更新）'
    return sop_progress.attach(state,report)


def states(report):
    return ['已完成' if step['complete'] else '待完成' for step in report['steps']]


def status_labels(report):
    labels=[]
    for step in report['steps']:
        work=step.get('work_record')
        if work and work['value']:
            labels.append(work['status'] if not work['value']['result']=='completed' or step['complete'] or work['stale'] else work['status']+' · 原业务待核对')
        elif step['complete']:
            labels.append('已完成')
        elif not step['available'] or report['service_error']:
            labels.append('待完成 · 项数待核对')
        else:
            pending={(row['title'],row.get('detail','')) for row in step['actions']+step['manual'] if not row['done']}
            labels.append('待完成 · '+str(len(pending))+'项待办')
    return labels


def counts(report,step):
    qs=[q for q in report['questions'] if q['step']==step and q['status'] in {'pending','answered','escalated'}]
    return len(qs)


def _evidence(state,report,step,original):
    item=report['steps'][step];qs=[q for q in report['questions'] if q['step']==step]
    matching=[f for f in item['findings'] if f['status'] in {'记录匹配','已收到具名补充'}]
    failed=[f for f in item['findings'] if f['status']=='资料缺失或冲突']
    def entries(rows):return '<ul>'+''.join('<li>'+esc(row)+'</li>' for row in rows)+'</ul>'
    html=('<div class="assistant-report assistant-details">'
        '<div class="assistant-result"><b>'+esc(item['status'])+'</b><p>'+esc(item['check'])+'</p>'
        '<small>正常资料无需逐项人工重查。记录核对不等于现场核实或任务完成。</small></div>')
    if report['service_error']:html+='<p class="assistant-error" role="status">'+esc(report['service_error'])+'</p>'
    html+='<section class="assistant-checks"><h3>核对结果</h3>'
    supplied=sum(f['status']=='已收到具名补充' for f in matching)
    html+='<p>'+str(len(matching))+' 项已有记录或具名补充 · '+str(len(failed))+' 项缺项、冲突或接入缺口'+(' · 已收到具名补充 '+str(supplied)+' 项' if supplied else '')+'</p>'
    if failed:html+=entries([f['text'] for f in failed])
    if matching:html+='<details data-assist-details="matched"><summary>展开正常核对结果与补充来源</summary>'+entries([f['text'] for f in matching])+'</details>'
    html+='</section><section class="assistant-progress"><h3>正在协助</h3>'
    active=[q for q in qs if q['status'] in {'pending','answered','escalated'}]
    if active:
        for q in active:
            person=state.registry.library.get(q['recipient'])
            html+='<div class="assistant-request"><b>'+esc(person.name if person else '职责人员')+'</b> · '+esc(WORDS[q['status']])
            html+='<p>'+esc(q['question'])+'</p>'
            if q.get('reply'):
                reply=q['reply'];html+='<details data-assist-details="'+esc(q['id'])+'"><summary>补充资料与来源</summary><p>'+esc(reply['content'])+'</p><p>来源：'+esc(reply['source'])+'；发生时间：'+esc(reply['occurred_at'] or '未提供')+'；系统收到：'+esc(reply['reported_at'])+'</p><p>具名信息答复；不作为任务接收、现场核验或自动放行。</p></details>'
            html+='</div>'
    else:
        needs=[q for q in report['candidates'] if q['step']==step]
        html+='<p>'+('自动补问尚未启用，请联系维护人员。' if needs and not report['auto_followups'] else
                     '正在后台处理资料补问，发出后在此更新。' if needs else '智能体持续对照新资料；暂没有可定向补问的缺项。')+'</p>'
    html+='</section><section class="assistant-decisions"><h3>需要您决定</h3>'
    matters=[f['text'] for f in failed if f['kind'] in {'exception','decision'}]
    escalated=[q for q in active if q['status']=='escalated']
    if matters or escalated:
        html+=entries(matters+['相关人员报告无法取得资料：'+q['question'] for q in escalated])
    else:html+='<p>无需重复核对正常资料；需要开展本项工作时，再进入对应功能作决定。</p>'
    html+='</section><details data-assist-details="basis"><summary>核对依据与未接入事项</summary>'
    html+='<p>'+esc(report['notice'])+'</p>'+entries(item['facts'])+entries(item['gaps'])
    source=item['source'];html+='<p>事件 '+esc(source['event_id'])+' · 记录版本 '+str(source['workspace_version'])+' · 核对规则 '+esc(source['rule_version'])+'</p><p>本次资料快照摘要：'+esc(source['snapshot_sha256'])+'</p></details>'
    html+='<details data-assist-details="actions" class="assistant-original"><summary>'+esc(item['decision'])+' · 打开办理入口</summary>'+original+'</details></div>'
    return html


def render(state,report,step,original,action_renderer=None):
    """把这一环节收敛成一个编号待办清单：能收集的标"已收集"，要人办的给按钮。"""
    item=report['steps'][step]
    work=item.get('work_record')
    rows=[]
    seen=set()
    # 九步每步只保留"你要做"那一个动作。未完成项归左侧指挥待办，
    # 不在这一步里重复列出——否则会出现"上面让你定重点、下面让你去核验"。
    for row in item.get('source_actions',item['actions']):
        if not row['done']:
            continue
        if row['title'] in seen:
            continue
        seen.add(row['title'])
        rows.append(dict(title=row['title'],status='已收集',done=True,owner='',button=''))
    pending=[row for row in rows if not row['done']]
    done=[row for row in rows if row['done']]
    out=[f'<div class="assistant-report assistant-compact" data-assistant-report="{step}">']
    # 每一步先讲清楚：干什么、参谋已经做完什么、你要做什么、做完什么标志。
    try:
        from .nine_flow import GUIDE
        what,did,you,finish=GUIDE[step]
        out.append('<div class="nine-guide">'
                   f'<p><i>干什么</i><b>{esc(what)}</b></p>'
                   f'<p><i>参谋已做</i><b class="ok">{esc(did)}</b></p>'
                   f'<p><i>你要做</i><b class="me">{esc(you)}</b></p>'
                   f'<p><i>做完标志</i><b>{esc(finish)}</b></p></div>')
    except Exception:  # noqa: BLE001 — 说明书缺失不应阻断页面
        pass
    # 上面"你要做"写的是哪件事，这里就给哪件事的按钮，不许对不上。
    try:
        from .nine_flow import ACTION_GUIDE
        act_label,act_href=ACTION_GUIDE[step]
        act_event=(item.get('source') or {}).get('event_id','')
        out.append('<div class="nine-do"><a class="lz-btn" href="'
                   + esc(act_href.format(event=act_event)) + '">' + esc(act_label) + '</a></div>')
    except Exception:  # noqa: BLE001
        pass
    # 第 3 步（力量调用）要具体说缺什么——空口"去核缺口"等于没说。
    if step == 2:
        raw_gaps = item.get('gaps') or []
        lines = []
        for gap in raw_gaps[:6]:
            if isinstance(gap, (tuple, list)) and len(gap) >= 2:
                text = f'{gap[0]}：{gap[1]}'
            else:
                text = str(gap)
            if text.strip():
                lines.append(f'<li>{esc(text)}</li>')
        if lines:
            out.append('<p class="assistant-remind">物资与力量缺口</p>'
                       '<ul class="assistant-gap">' + ''.join(lines) + '</ul>')
    # 次要去处：比如第 3 步"调外援、让公司供货"，不做成第二个主按钮。
    try:
        from .nine_flow import EXTRA_GUIDE
        extra = EXTRA_GUIDE.get(step)
        if extra:
            extra_label, extra_href = extra
            out.append('<p class="assistant-extra"><a href="' + esc(extra_href) + '">'
                       + esc(extra_label) + ' →</a></p>')
    except Exception:  # noqa: BLE001
        pass
    chip=status_labels(report)[step]
    def _norm(text):return str(text).replace(' ','').replace('·','').replace('，','').replace(',','')
    # 状态标签和下面的提醒条目是同一句话时只留一处，避免"同一件事说两遍"。
    if not any(_norm(row['title']) in _norm(chip) for row in pending):
        out.append('<div class="assistant-result"><b class="assistant-phase '
                   + item['phase_status'] + '">' + esc(chip) + '</b></div>')
    if report['service_error']:
        out.append('<p class="assistant-error" role="status">' + esc(report['service_error']) + '</p>')
    if pending:
        out.append('<p class="assistant-remind">系统提醒（要你处理的）</p>')
        out.append('<ol class="assistant-todo">')
        for index,row in enumerate(pending,1):
            owner=('<small>' + esc(row['owner']) + '</small>') if row['owner'] else ''
            out.append(f'<li><i>{index}</i><div><b>{esc(row["title"])}</b>{owner}</div>'
                       f'<em>{esc(row["status"])}</em>{row["button"]}</li>')
        out.append('</ol>')
    # 不再输出"已收集完，无需你操作"——每步都有"你要做"，
    # 说"无需操作"会和上面的主按钮自相矛盾。
    if done:
        # 已收集的收成一行，不占版面；要追溯再展开。
        out.append('<details class="assistant-collected"><summary>已收集 '
                   + str(len(done)) + ' 项</summary><ul>')
        for row in done:
            out.append(f'<li>{esc(row["title"])}</li>')
        out.append('</ul></details>')
    if work and work['value']:
        v=work['value']
        out.append('<details class="assistant-original"><summary>本次办理结果</summary>'
                   f'<p>{esc(v["scope"])}</p><p>{esc(v["feedback"])}</p>')
        if v['remaining']:
            out.append(f'<p><b>仍需办理：</b>{esc(v["remaining"])}</p><p>{esc(v["followup"])}</p>')
        out.append('</details>')
    out.append('<details data-assist-details="full" class="assistant-evidence">'
               '<summary>查看依据、补问与原办理入口</summary>'
               + _evidence(state,report,step,original) + '</details></div>')
    return ''.join(out)


def inbox(state,token):
    from .workflow import context
    member,record=context(state,token);runtime=getattr(state,'assistant_runtime',None)
    questions=runtime.questions(record.event_id,recipient=member.phone) if runtime else []
    html='<section class="workflow-intro"><h1>待我补充</h1><p>'+esc(record.name)+' · 智能参谋发起的系统内资料补问</p></section><p>补充信息不会自动生成指挥命令、正式回执或任务完成记录。无法取得的资料可直接说明，由智能参谋汇总协调。</p>'
    if runtime and (runtime.journal.error or runtime.policy_error):html+='<p role="status">'+esc(runtime.journal.error or runtime.policy_error)+'</p>'
    for q in reversed(questions):
        html+='<section class="lz-card assistant-inbox-card"><h2>'+esc(WORDS[q['status']])+'</h2><p>'+esc(q['question'])+'</p>'
        if q['status']=='pending':
            fields=dict(event_id=record.event_id,csrf=member.custody_csrf,question_id=q['id'],expected_version=q['version'])
            html+='<form method="post" action="/workflow/assistance-reply">'+''.join('<input type="hidden" name="'+k+'" value="'+esc(v)+'">' for k,v in fields.items())
            html+='<label>补充结果<select name="outcome"><option value="provided">提供资料</option><option value="unavailable">目前无法取得</option></select></label><label>资料内容或无法取得的原因<textarea name="content" required maxlength="2000"></textarea></label><label>来源依据<input name="source" required maxlength="400"></label><label>发生时间（未知可留空）<input name="occurred_at" placeholder="例如 2026-10-13T15:00:00+08:00"></label><button class="lz-btn" type="submit">提交补充信息</button></form>'
        elif q.get('reply'):html+='<p>'+esc(q['reply']['content'])+'</p><p>来源：'+esc(q['reply']['source'])+'</p>'
        html+='</section>'
    if not questions:html+='<p>当前事件没有发给你的资料补问。</p>'
    return html+'<p><a href="/workflow/guide">返回九步走</a></p>'
