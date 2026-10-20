"""P12 bound-event review cards. Forms receive event/version/CSRF in reviewview."""

from html import escape as esc

from .review_causality import current_confirmation


def _form(route, body):
    return f'<form method="post" action="/review/{route}" class="lz-formgrid">{body}</form>'


def _hidden(key, value):
    return f'<input type="hidden" name="{key}" value="{esc(str(value), quote=True)}">'


def rect_tab(state, owner_mode):
    owner_forms = ''
    if owner_mode:
        for hyp in sorted(state.review_ledger.hypotheses.values(), key=lambda h: h.hyp_id):
            if not current_confirmation(state, 'hypothesis', hyp.hyp_id):
                continue
            for issue_id in hyp.related_issues:
                if current_confirmation(state, 'issue', issue_id):
                    owner_forms += _form('rect-draft',
                        _hidden('issue_id', issue_id) + _hidden('hyp_id', hyp.hyp_id)
                        + f'<span>已核实问题 {esc(issue_id)} · 原因 {esc(hyp.hyp_id)}</span>'
                        + '<button class="lz-btn">生成待审核整改草案</button>')
    cards = ''
    for task in sorted(state.review_ledger.rects.values(), key=lambda t: t.rect_id):
        body = (f'<article class="issue-card"><header><b>{esc(task.rect_id)}</b> '
                f'<span class="lz-chip">{esc(task.status_word())}</span>'
                + (' <span class="lz-chip lz-chip-danger">超期</span>' if task.is_overdue() else '')
                + '</header>'
                f'<p>{esc(task.measures)}</p><p class="dim">来源问题 {esc(task.source_issue_id or "—")}；'
                f'原因 {esc(task.source_hyp_id or "—")}；来源核查版本 '
                f'{esc("、".join(task.source_assessment_refs) or "—")}</p>'
                f'<p class="dim">责任 {esc(task.owner_unit or "—")}/{esc(task.owner_person or "—")}；'
                f'期限 {esc(task.deadline or "—")}；验收标准 {esc(task.acceptance_standard or "—")}；'
                f'指定验收人 {esc(task.verifier_name or "—")}</p>')
        for row in task.feedback_records:
            body += (f'<p class="dim">责任人反馈：{esc(row["provided_by"])} · {esc(row["kind"])} · '
                     f'{esc(row["content"])} · 依据 {esc("、".join(row["evidence_refs"]) or "—")}</p>')
        for row in task.verification_records:
            body += (f'<p class="dim">指定验收：{esc(row["verifier"])} · {esc(row["outcome"])} · '
                     f'{esc(row["finding"])} · 依据 {esc("、".join(row["evidence_refs"]))}</p>')
        if owner_mode and task.status == 'draft':
            roster = sorted(state.registry.library.values(), key=lambda p: p.phone)
            options = ''.join(f'<option value="{esc(p.phone)}">{esc(p.name)} ({esc(p.phone)})</option>' for p in roster)
            body += _form('rect-publish', _hidden('rect_id', task.rect_id)
                + '<input name="measures" class="lz-input" placeholder="具体措施" required>'
                + '<input name="owner_unit" class="lz-input" placeholder="责任单位" required>'
                + '<input name="owner_person" class="lz-input" placeholder="责任人姓名，与号码一致" required>'
                + f'<select name="responsible_phone" class="lz-input">{options}</select>'
                + '<input name="deadline" type="date" class="lz-input" required>'
                + '<input name="acceptance_standard" class="lz-input" placeholder="可核验的验收标准" required>'
                + '<input name="verifier_name" class="lz-input" placeholder="指定验收人姓名，与号码一致" required>'
                + f'<select name="verifier_phone" class="lz-input">{options}</select>'
                + '<button class="lz-btn">负责人审核发布</button>')
        if owner_mode and task.status == 'in_progress':
            body += _form('rect-feedback', _hidden('rect_id', task.rect_id)
                + _hidden('provided_by', task.owner_person)
                + '<input name="duty" class="lz-input" placeholder="业务职责" required>'
                + '<textarea name="content" class="lz-input" placeholder="进展或完成情况" required></textarea>'
                + '<input name="evidence" class="lz-input" placeholder="本事件依据 id，完成报告必填">'
                + '<button name="completed" value="0" class="lz-btn">提交进展</button>'
                + '<button name="completed" value="1" class="lz-btn">报告完成，待验收</button>')
        if owner_mode and task.status == 'awaiting_verify':
            body += _form('rect-verify', _hidden('rect_id', task.rect_id)
                + _hidden('verifier', task.verifier_name)
                + '<textarea name="finding" class="lz-input" placeholder="按验收标准核对的结果" required></textarea>'
                + '<input name="evidence" class="lz-input" placeholder="本事件验收依据 id" required>'
                + '<button name="outcome" value="passed" class="lz-btn">验收通过并关闭</button>'
                + '<button name="outcome" value="failed" class="lz-btn">未通过，退回整改</button>')
        body += ''.join(f'<p class="dim">{esc(line)}</p>' for line in task.history[-5:])
        cards += body + '</article>'
    return ('<section class="rv-main"><h3>整改闭环</h3>'
            '<p class="lz-note">草案由已核实问题与原因生成；负责人单账号集中登记责任人反馈及指定验收人核实结果，记录提供人与登记人。事件结束不代替整改关闭。</p>'
            + owner_forms + (cards or '<p class="dim">尚无本事件整改草案。</p>') + '</section>')


def exp_tab(state, owner_mode, recommendations):
    forms = ''
    if owner_mode:
        sources = []
        sources.extend(('rect', t.rect_id) for t in state.review_ledger.rects.values() if t.status == 'closed')
        sources.extend(('issue', i.issue_id) for i in state.review_ledger.issues.values()
                       if current_confirmation(state, 'issue', i.issue_id))
        sources.extend(('hypothesis', h.hyp_id) for h in state.review_ledger.hypotheses.values()
                       if current_confirmation(state, 'hypothesis', h.hyp_id))
        for kind, source_id in sources:
            forms += _form('exp-propose', _hidden('source_kind', kind) + _hidden('source_id', source_id)
                           + f'<span>已核实来源 {esc(kind)}:{esc(source_id)}</span>'
                           + '<button class="lz-btn">生成经验候选</button>')
    cards = ''
    for exp in sorted(state.review_ledger.experiences.values(), key=lambda e: e.exp_id):
        cards += (f'<article class="issue-card"><header><b>{esc(exp.exp_id)} v{exp.version}</b> '
                  f'<span class="lz-chip">{esc(exp.status_word())}</span></header>'
                  f'<p>{esc(exp.content)}</p><p class="dim">适用：{esc(exp.applicability or "—")}；'
                  f'条件标签：{esc("、".join(exp.condition_tags) or "—")}；限制：{esc(exp.limitations or "—")}；'
                  f'来源事件 {esc(exp.source_event_id)}；来源记录 '
                  f'{esc(exp.source_rect_id or exp.source_issue_id or exp.source_hyp_id or "—")}；'
                  f'依据 {esc("、".join(exp.evidence_ids) or "—")}</p>')
        if owner_mode and exp.status != 'expired':
            cards += _form('exp-revise', _hidden('exp_id', exp.exp_id)
                + f'<textarea name="content" class="lz-input" required>{esc(exp.content)}</textarea>'
                + f'<input name="applicability" class="lz-input" value="{esc(exp.applicability, quote=True)}" placeholder="适用条件" required>'
                + f'<input name="limitations" class="lz-input" value="{esc(exp.limitations, quote=True)}" placeholder="限制/失效条件" required>'
                + f'<input name="condition_tags" class="lz-input" value="{esc(",".join(exp.condition_tags), quote=True)}" placeholder="适用主题=已核实值，多项逗号分隔" required>'
                + f'<input name="evidence" class="lz-input" value="{esc(";".join(exp.evidence_ids), quote=True)}" placeholder="本事件依据 id" required>'
                + '<input name="reason" class="lz-input" placeholder="修订理由" required>'
                + '<button class="lz-btn">修订为待确认版本</button>')
            if exp.status == 'candidate':
                cards += _form('exp-decide', _hidden('exp_id', exp.exp_id)
                    + '<input name="reason" class="lz-input" placeholder="确认理由" required>'
                    + '<button name="target" value="confirmed" class="lz-btn">负责人确认</button>')
            if exp.status == 'confirmed':
                cards += _form('exp-decide', _hidden('exp_id', exp.exp_id)
                    + '<input name="reason" class="lz-input" placeholder="反例或条件变化说明" required>'
                    + '<input name="evidence" class="lz-input" placeholder="本事件反例依据 id" required>'
                    + '<button name="target" value="expired" class="lz-btn">停用并留痕</button>')
        cards += ''.join(f'<p class="dim">{esc(line)}</p>' for line in exp.history[-5:]) + '</article>'
    rec_cards = ''
    for row in recommendations:
        exp = row['exp']
        rec_cards += (f'<article class="issue-card"><b>{esc(exp.exp_id)}：'
                      f'{"条件匹配，供人工复核" if row["matched"] else "条件有差异，暂不建议直接套用"}</b>'
                      f'<p>{esc(exp.content)}</p><p class="dim">已匹配：{esc("、".join(row["matched_tags"]) or "无")}；'
                      f'未匹配：{esc("、".join(row["missing_tags"]) or "无")}；'
                      f'来源事件 {esc(exp.source_event_id)}；现有依据版本 v{row["context_version"]}</p></article>')
    return ('<section class="rv-main"><h3>经验候选与复用提示</h3>'
            '<p class="lz-note">仅以当前事件经审计的已核实条件做精确匹配。提示不自动形成命令；反例或条件变化由负责人修订或停用。</p>'
            + (rec_cards or '<p class="dim">暂无可核对的历史经验提示。</p>')
            + forms + (cards or '<p class="dim">尚无本事件经验候选。</p>') + '</section>')
