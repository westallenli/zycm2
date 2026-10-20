"""复盘与经验页渲染层（P16-T7，05 号定版 §14 布局）。

纯函数渲染。可见性：commander/owner 全 Tab + 候选登记入口；member 只读；公共屏仅已发布。
结论级动作（A-2，P17-P5 起）：假设核验/整改流转/经验确认/报告编辑与定版仅 owner 会话可操作。
标签页经 query 参数切换；时间轴拖动联动为后续增强，当前如实标注。
"""

from __future__ import annotations

from . import review_domain as rd
from .org_model import mask_phone
from .workbench import esc

_TABS = (
    ("summary", "本次复盘"),
    ("timeline", "时间线回放"),
    ("issues", "问题分析"),
    ("root", "根因分析"),
    ("analysis", "证据核查"),
    ("common", "共性规律"),
    ("rect", "整改闭环"),
    ("exp", "经验沉淀"),
    ("report", "报告版本"),
    ("trend", "趋势统计"),   # 指挥员专属：跨事件聚合（doc 05 §7）
)

_SEVERITY_CLASS = {"关键": "rv-sev-critical", "重要": "rv-sev-major",
                   "一般": "rv-sev-minor", "观察项": "rv-sev-watch"}
_STATUS_CHIP = {
    "pending": "lz-chip-warn", "confirmed": "lz-chip-ok",
    "revised": "lz-chip-active", "rejected": "lz-chip-dim",
    "rectified": "lz-chip-active", "closed": "lz-chip-dim",
}


def _tabs_nav(active: str, commander: bool = True) -> str:
    visible = [
        (key, label) for key, label in _TABS
        if key != "trend" or commander
    ]
    items = "".join(
        f'<a class="rv-tab{" on" if key == active else ""}" '
        f'href="/review?tab={key}">{esc(label)}</a>'
        for key, label in visible
    )
    return f'<nav class="rv-tabs">{items}</nav>'


def _top_bar(state, event, viewer_word: str) -> str:
    if event is not None:
        cells = [
            ("事件", f"{event.name}"),
            ("编号", event.report_no or event.event_id),
            ("事件状态", event.status_word()),
        ]
    else:
        cells = [("事件", "—（未进入事件任务空间）")]
    counts = rd_count_summary(state)
    cells += [
        ("复盘状态", f"候选问题 {counts['pending']} 条待核验 · "
                     f"根因假设待验证 {counts['hyp_pending']} 项 · "
                     f"整改在办 {counts['rect_open']} 项"),
        ("数据截至", _data_time(state)),
        ("当前登录", viewer_word),
        ("告警", "未读 P0/P1 告警：—（告警通道待接入）"),
    ]
    items = "".join(
        f'<div class="strip-cell"><span class="k">{esc(k)}</span><b>{esc(v)}</b></div>'
        for k, v in cells
    )
    return f'<div class="sand-strip">{items}</div>'


def rd_count_summary(state) -> dict:
    ledger = state.review_ledger
    pending = sum(1 for i in ledger.issues.values() if i.review_status == "pending")
    rect_open = sum(1 for r in ledger.rects.values() if r.status not in {"done", "closed"})
    hyp_pending = sum(
        1 for h in ledger.hypotheses.values() if h.status == "pending"
    )
    return {"pending": pending, "rect_open": rect_open,
            "hyp_pending": hyp_pending}


def _data_time(state) -> str:
    ws = state.workspace
    for ev in reversed(getattr(ws, "_events", ())):
        stamp = getattr(ev, "at", None) or getattr(ev, "ts", None)
        if stamp:
            return str(stamp)
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    event = registry.events.get(active_id) if registry is not None and active_id else None
    latest = getattr(ws.events[-1], 'recorded_at', '') if ws.events else ''
    return latest or (getattr(event, "updated_at", "") if event else "") or "—"


# ---------------------------------------------------------------------------
# 各标签页


def _tab_summary(state, event, detailed=False) -> str:
    from .review_sources import card
    ws = state.workspace
    digest = rd.event_digest(event, ws)
    digest_rows = "".join(
        f'<tr><td>{esc(name)}</td><td><b>{esc(value)}</b></td>'
        f'<td>{esc(status)}</td></tr>'
        for name, value, status in digest
    )
    plan_rows = "".join(
        f'<tr><td>{esc(row["item"])}</td><td>{esc(row["plan"])}</td>'
        f'<td>{esc(row["actual"])}</td><td>{esc(row["deviation"])}</td></tr>'
        for row in rd.plan_actual_rows(digest)
    )
    replay_n = len(rd.build_timeline(ws))
    return (
        '<section class="rv-main"><h3>事件摘要</h3>'
        '<p class="lz-note">数据状态口径：系统记录＝审计/注册表原始字段；'
        "已发生·时间未记录＝审计链有序号但无时钟源；缺失（未补写）＝"
        "系统没有该数据，绝不事后编造。</p>"
        f'<table class="lz-table"><thead><tr><th>里程碑</th><th>值</th><th>数据状态</th></tr></thead>'
        f"<tbody>{digest_rows}</tbody></table>"
        "<h3>计划与实际对比</h3>"
        f'<table class="lz-table"><thead><tr><th>事项</th><th>计划</th><th>实际</th><th>偏差</th></tr></thead>'
        f"<tbody>{plan_rows}</tbody></table>"
        f'<p class="lz-note">差异只表偏差，不等同责任（规范 05·§2）；'
        f"时间线共 {replay_n} 条可在「时间线回放」查看。</p></section>"
        + (card(state) if ws.event_id and detailed else '')
    )


def _tab_timeline(state) -> str:
    if state.workspace.event_id:
        from .review_replay import replay, render_snapshot
        snapshot, marks = replay(state.workspace, state.active_event_id)
        options = ''.join(f'<option value="{r["sequence"]}">#{r["sequence"]} · {esc(r["recorded_at"] or "录入时间未记录")} · {esc(rd._TIMELINE_LABELS.get(r["kind"], r["kind"]))}</option>' for r in snapshot['timeline'])
        return ('<section class="rv-main" id="review-replay" style="overflow-wrap:anywhere" data-event="'+esc(state.active_event_id)+'">'
            '<h3>本事件当时已知状态回放 · 审计链</h3><label>审计位置 '
            f'<input type="range" id="review-sequence" min="0" max="{state.workspace.version}" value="{state.workspace.version}"></label>'
            f'<select class="lz-select" id="review-entry"><option value="0">起点（无已知记录）</option>{options}</select>'
            '<label>系统已知截至（可空；须含时区）<input class="lz-input" id="review-known-at" placeholder="2026-10-07T09:00:00+08:00"></label>'
            '<button class="lz-btn lz-btn2" type="button" id="review-load">读取此位置</button>'
            '<p id="review-replay-notice" role="status">当前为页面载入时快照，正在连接历史地图。</p>'
            '<p>历史坐标图只展示当时已登记几何；未加载底图，坐标系与定位精度须另核。无几何不补点，非现场导航图。</p>'
            '<div id="review-history-map" style="height:320px;max-width:100%"></div>'
            '<div id="review-history-content">'+render_snapshot(snapshot)+'</div></section>'
            '<link rel="stylesheet" href="/static/leaflet/leaflet.css">'
            '<script src="/static/leaflet/leaflet.js"></script><script src="/static/review-replay.js"></script>')
    entries = rd.build_timeline(state.workspace)
    rows = "".join(
        f'<tr><td class="mono">#{e.sequence}</td><td>{esc(e.time or "—")}</td>'
        f"<td>{esc(e.label)}</td><td>{esc(e.actor or '—')}</td>"
        f"<td class='mono'>{esc(e.object_ref or '—')}</td></tr>"
        for e in reversed(entries[-30:])
    )
    empty = ('<tr><td colspan="5">审计链暂无记录。</td></tr>') if not entries else ""
    return (
        '<section class="rv-main"><h3>时间线回放（审计链最近 30 条）</h3>'
        '<p class="lz-note">录入时间不代表发生时间，缺失时如实显示「—」；'
        "点击联动当时地图态势的能力待接入，当前不做假联动。</p>"
        f'<table class="lz-table"><thead><tr><th>序列</th><th>系统录入时间</th><th>事项</th>'
        f"<th>操作者</th><th>关联对象</th></tr></thead><tbody>{rows}{empty}</tbody></table>"
        '<p class="lz-note">底部可拖动时间轴（联动地图/火线版本/队伍位置）待接入。</p></section>'
    )


def _issue_card(issue: rd.ReviewIssue, commander: bool, *, bound=False, assessment=None) -> str:
    sev_cls = _SEVERITY_CLASS.get(issue.severity, "")
    chip_cls = _STATUS_CHIP.get(issue.review_status, "lz-chip-reg")
    evidence = "、".join(issue.evidence_ids) or "—"
    actions = ""
    if commander and bound:
        basis = assessment.id if assessment else ''
        if issue.review_status in {'pending', 'revised', 'confirmed'}:
            actions = (
                '<form method="post" action="/review/issue-status" class="lz-inlineform">'
                f'<input type="hidden" name="issue_id" value="{esc(issue.issue_id)}">'
                f'<input type="hidden" name="assessment_ref" value="{esc(basis)}">'
                '<input name="reason" class="lz-input" required placeholder="负责人核实理由及反证处理">'
                '<button name="target" value="confirmed" class="lz-btn lz-btn2">确认问题</button>'
                + ('<button name="target" value="rejected" class="lz-btn lz-btn2">判定不成立</button>'
                   if issue.review_status != 'confirmed' else '') + '</form>')
        if issue.review_status in {'confirmed', 'rejected'}:
            actions += (
                '<form method="post" action="/review/issue-reopen" class="lz-inlineform">'
                f'<input type="hidden" name="issue_id" value="{esc(issue.issue_id)}">'
                '<input name="reason" class="lz-input" required placeholder="新证据与重开理由">'
                '<button class="lz-btn lz-btn2">新证据重开</button></form>')
    elif commander:
        options = "".join(
            f'<option value="{code}">{esc(word)}</option>'
            for code, word in rd.REVIEW_STATUS_WORDS.items()
        )
        actions = (
            '<form method="post" action="/review/issue-status" class="lz-inlineform">'
            f'<input type="hidden" name="issue_id" value="{esc(issue.issue_id)}">'
            f'<select name="target" class="lz-input">{options}</select>'
            '<input name="reason" class="lz-input" placeholder="原因（否决/关闭必填）" style="width:200px">'
            '<button class="lz-btn lz-btn2">流转</button></form>'
            '<form method="post" action="/review/issue-reopen" class="lz-inlineform">'
            f'<input type="hidden" name="issue_id" value="{esc(issue.issue_id)}">'
            '<input name="reason" class="lz-input" placeholder="新证据内容（重开必填）" style="width:200px">'
            '<button class="lz-btn lz-btn2">新证据重开</button></form>'
        )
    return (
        f'<article class="issue-card {sev_cls}">'
        f'<header><b>{esc(issue.title)}</b> '
        f'<span class="lz-chip {chip_cls}">{esc(issue.status_word())}</span> '
        f'<span class="lz-chip lz-chip-reg">{esc(issue.severity)}</span> '
        f'<span class="lz-chip lz-chip-dim">{esc(issue.category)}·{esc(issue.stage)}</span></header>'
        f'<p>{esc(issue.description)}</p>'
        f'<p class="dim">触发依据：{esc(issue.trigger_basis)} · 影响对象：{esc(issue.affected or "—")} · '
        f'影响范围推断：{esc(issue.impact_scope)} · 置信度：{esc(issue.confidence)}'
        f'（置信度独立于核验状态） · 分析版本：{esc(issue.analysis_version)}</p>'
        f'<p class="dim">证据：{esc(evidence)}</p>'
        f'<p class="dim">待核验项：{esc(issue.to_verify or "无")}</p>'
        + (f'<p class="lz-note">最新核查依据：{esc(assessment.id if assessment else "未登记")}；'
           + ('新版本待负责人重新判断。' if issue.confirmed_assessment_ref and assessment
              and issue.confirmed_assessment_ref != assessment.id else '结论仍须人工核实。')
           + '</p>' if bound else '')
        + (f'<p class="dim">备注：{esc(issue.note)}</p>' if issue.note else "")
        + actions + "</article>"
    )


def _tab_issues(state, commander: bool) -> str:
    ledger = state.review_ledger
    issues = rd.sort_issues(list(ledger.issues.values()))
    from .review_causality import assessments
    current = assessments(state) if state.workspace.event_id else {}
    cards = "".join(_issue_card(i, commander, bound=bool(state.workspace.event_id),
                   assessment=current.get(f'issue:{i.issue_id}')) for i in issues)
    if not cards:
        cards = (
            '<p class="dim">尚无问题卡片。'
            + ("可点击「按规则生成候选」从真实偏差中产出。" if commander else "")
            + "</p>"
        )
    gen_btn = (
        '<form method="post" action="/review/generate" class="lz-inlineform">'
        '<button class="lz-btn lz-btn2">按规则生成候选（只读真实数据）</button></form>'
        f'<p class="lz-note">规则口径 {esc(rd.RULE_VERSION)}：异常命令 / 未关闭缺口 / '
        "岗位未解析 / 草稿标绘四类触发；产出一律「待核验」，系统不定责、不自动发布。</p>"
        if commander else ""
    )
    return (
        f'<section class="rv-main"><h3>问题卡片（默认排序：严重度→范围→编号）</h3>'
        f"{gen_btn}{cards}</section>"
    )


def _tab_analysis(state, owner_mode: bool) -> str:
    """P12-01: show versioned evidence review without promoting a candidate to fact."""
    if not state.workspace.event_id or state.workspace.event_id != state.active_event_id:
        return '<section class="rv-main"><h3>证据核查</h3><p>请先进入已绑定的本事件任务空间。</p></section>'
    import json
    import secrets
    from .review_causality import assessments

    current = assessments(state)
    candidates = ([('issue', item.issue_id, item.title, item.evidence_ids)
                   for item in rd.sort_issues(list(state.review_ledger.issues.values()))]
                  + [('hypothesis', item.hyp_id, item.statement, item.evidence_ids)
                     for item in state.review_ledger.hypotheses.values()])
    rows = []
    for kind, candidate_id, title, hints in candidates:
        previous = current.get(f'{kind}:{candidate_id}')
        data = json.loads(previous.content_json) if previous else {}
        version = previous.reference.version if previous else 0
        label = '问题' if kind == 'issue' else '根因假设'
        def field(name):
            value = data.get(name, '')
            return esc('；'.join(value) if isinstance(value, list) else value)
        form = ''
        if owner_mode:
            form = (
                '<form method="post" action="/review/assessment" class="lz-formgrid">'
                f'<input type="hidden" name="target_kind" value="{kind}">'
                f'<input type="hidden" name="target_id" value="{esc(candidate_id)}">'
                f'<input type="hidden" name="expected_object_version" value="{version}">'
                f'<input type="hidden" name="request_id" value="assessment-{secrets.token_hex(12)}">'
                f'<label>支持证据编号（分号分隔）<input class="lz-input" name="support_refs" value="{field("support_refs")}"></label>'
                f'<label>反证编号（分号分隔）<input class="lz-input" name="counter_refs" value="{field("counter_refs")}"></label>'
                f'<label>反证说明或暂无反证说明<textarea class="lz-input" name="counter_note">{field("counter_note")}</textarea></label>'
                f'<label>其他解释（未知请写待核）<textarea class="lz-input" name="alternatives" required>{field("alternatives")}</textarea></label>'
                f'<label>缺失信息（无须说明依据）<textarea class="lz-input" name="missing_info">{field("missing_info")}</textarea></label>'
                f'<label>核实对象及职责<textarea class="lz-input" name="verify_targets" required>{field("verify_targets")}</textarea></label>'
                '<label>原因层级（问题可留待判）<select class="lz-input" name="causal_layer">'
                + ''.join(f'<option value="{code}" {"selected" if data.get("causal_layer", "undetermined") == code else ""}>{label}</option>'
                          for code, label in (('undetermined','待判'),('direct','直接原因'),('deep','深层原因')))
                + '</select></label>'
                f'<label>会议原始记录版本编号<input class="lz-input" name="meeting_ref" value="{field("meeting_ref")}"></label>'
                f'<label>会议核实反馈（须关联记录）<textarea class="lz-input" name="meeting_finding">{field("meeting_finding")}</textarea></label>'
                '<button class="lz-btn">保存待核记录新版本</button></form>'
            )
        details = ''
        if previous:
            history = sorted((revision for revision in state.workspace.object_revisions.values()
                              if revision.reference.event_id == state.active_event_id
                              and revision.reference.object_kind == 'review_causal_assessment'
                              and revision.reference.object_id == f'{kind}:{candidate_id}'),
                             key=lambda revision: revision.reference.version)
            history_html = ''.join(
                f'<li>v{revision.reference.version} · {esc(revision.times.recorded_at)} · '
                f'{esc(revision.id)} · 支持：{esc("；".join(json.loads(revision.content_json).get("support_refs", [])) or "—")} · '
                f'反证：{esc("；".join(json.loads(revision.content_json).get("counter_refs", [])) or "—")}</li>'
                for revision in history)
            details = (f'<p>当前核查版本 v{version}：{esc(previous.id)}；状态：待验证。</p>'
                       f'<p>支持：{field("support_refs") or "—"}；反证：{field("counter_refs") or "—"}；'
                       f'反证说明：{field("counter_note") or "—"}</p>'
                       f'<p>其他解释：{field("alternatives")}；缺失信息：{field("missing_info") or "—"}；'
                       f'核实对象：{field("verify_targets")}</p>'
                       f'<p>原因层级：{esc({"direct":"直接原因","deep":"深层原因"}.get(data.get("causal_layer"), "待判"))}；'
                       f'会议依据：{field("meeting_ref") or "—"}；核实反馈：{field("meeting_finding") or "—"}</p>'
                       f'<details><summary>核查版本历史（{len(history)}）</summary><ul>{history_html}</ul></details>')
        rows.append(f'<article class="issue-card"><h4>{label} {esc(candidate_id)}：{esc(title)}</h4>'
                    f'<p class="dim">候选原有线索：{esc("、".join(hints) or "—")}；须逐项核对原始依据。</p>'
                    + details + form + '</article>')
    return ('<section class="rv-main"><h3>本事件问题与根因证据核查</h3>'
            '<p class="lz-note">支持、反证、其他解释、缺失信息和核实对象分开记录。'
            '会议反馈先在上方集中登记，再引用其不可变版本；每次补充形成新版本。'
            '所有核查记录保持待验证，不自动认定因果或责任。</p>'
            + (''.join(rows) or '<p>尚无本事件问题或根因候选。</p>') + '</section>')
def _tab_common(state) -> str:
    n_events = len(getattr(getattr(state, "registry", None), "events", {}) or {})
    return (
        '<section class="rv-main"><h3>共性规律（多次火情对比）</h3>'
        f'<p class="dim">当前注册表事件数：{n_events}。样本不足（需要多次火情数据），'
        "共性聚合暂不产出，避免用单一事件虚构规律。</p>"
        '<p class="lz-note">样本筛选/质量降权/聚合维度将随多事件数据积累启用。</p></section>'
    )


def _rect_row(task: rd.RectTask, commander: bool, owner_mode: bool = False) -> str:
    overdue = ' <span class="lz-chip lz-chip-danger">超期</span>' if task.is_overdue() else ""
    recurrence = (
        ' <span class="lz-chip lz-chip-warn">整改后复现待评估</span>'
        if task.history and any(h.startswith("复现标记") for h in task.history)
        else ""
    )
    history = "".join(f'<div class="dim mono">{esc(h)}</div>' for h in task.history[-4:])
    actions = ""
    if owner_mode:
        options = "".join(
            f'<option value="{code}">{esc(word)}</option>'
            for code, word in rd.RECT_STATUS_WORDS.items()
        )
        actions = (
            '<form method="post" action="/review/rect-status" class="lz-inlineform">'
            f'<input type="hidden" name="rect_id" value="{esc(task.rect_id)}">'
            f'<select name="target" class="lz-input">{options}</select>'
            '<input name="reason" class="lz-input" placeholder="原因（验证未通过等）" style="width:180px">'
            '<button class="lz-btn lz-btn2">流转</button></form>'
        )
    return (
        f'<article class="issue-card"><header><b>{esc(task.rect_id)}</b>'
        f'<span class="lz-chip lz-chip-reg">{esc(task.status_word())}</span>'
        f'{overdue}{recurrence}</header>'
        f"<p>{esc(task.measures)}</p>"
        f'<p class="dim">关联：{esc("、".join(task.related_ids) or "—")} · '
        f'责任：{esc(task.owner_unit)} {esc(task.owner_person)} · 优先级 {esc(task.priority)} · '
        f'期限 {esc(task.deadline or "未设置")} · 验证方式 {esc(task.verify_method or "未设置")}</p>'
        f"{history}{actions}</article>"
    )


def _tab_rect(state, event_id: str, commander: bool, owner_mode: bool = False) -> str:
    ledger = state.review_ledger
    tasks = sorted(ledger.rects.values(), key=lambda t: t.rect_id)
    rows = "".join(_rect_row(t, commander, owner_mode) for t in tasks) or \
        '<p class="dim">尚无整改任务。</p>'
    issue_options = "".join(
        f'<option value="{esc(i.issue_id)}">{esc(i.issue_id)} {esc(i.title[:16])}</option>'
        for i in ledger.issues.values()
    )
    form = ""
    if commander:
        form = (
            '<form method="post" action="/review/rect-new" class="lz-formgrid">'
            f'<select name="related_id" class="lz-input">{issue_options}'
            '<option value="">（不关联问题）</option></select>'
            '<input name="measures" class="lz-input" placeholder="整改措施" required>'
            '<input name="owner_unit" class="lz-input" placeholder="责任单位">'
            '<input name="owner_person" class="lz-input" placeholder="责任人">'
            '<select name="priority" class="lz-input"><option>高</option><option selected>中</option><option>低</option></select>'
            '<input name="deadline" class="lz-input" placeholder="完成期限 YYYY-MM-DD">'
            '<input name="verify_method" class="lz-input" placeholder="验证方式（用什么事实判定已整改）">'
            '<button class="lz-btn">新建整改任务</button></form>'
            '<p class="lz-note">整改由负责人发布；系统不自动生成整改（规范 05·§9 审核关卡）。'
            + ("" if owner_mode else "状态流转属结论级动作，由复盘负责人执行。")
            + '</p>'
        )
    return (
        f'<section class="rv-main"><h3>整改闭环</h3>{form}{rows}</section>'
    )


def _exp_card(exp: rd.Experience, owner_mode: bool = False) -> str:
    actions = ""
    if owner_mode and exp.status == "candidate":
        actions = (
            '<form method="post" action="/review/exp-status" class="lz-inlineform">'
            f'<input type="hidden" name="exp_id" value="{esc(exp.exp_id)}">'
            '<button name="target" value="confirmed" class="lz-btn lz-btn2">确认入正式经验库</button>'
            '<button name="target" value="expired" class="lz-btn lz-btn2">标为失效</button></form>'
        )
    return (
        f'<article class="issue-card"><header><b>{esc(exp.content)}</b>'
        f'<span class="lz-chip lz-chip-reg">{esc(exp.status_word())}</span></header>'
        f'<p class="dim">适用条件：{esc(exp.applicability or "—")} · 来源事件 {esc(exp.source_event_id)} · '
        f'确认人 {esc(exp.confirmed_by or "—")}</p>{actions}</article>'
    )


def _tab_exp(state, commander: bool, owner_mode: bool = False) -> str:
    ledger = state.review_ledger
    exps = sorted(ledger.experiences.values(), key=lambda e: e.exp_id)
    cards = "".join(_exp_card(e, owner_mode) for e in exps) or \
        '<p class="dim">尚无经验条目。经验必须经负责人确认才进正式经验库。</p>'
    form = ""
    if commander:
        form = (
            '<form method="post" action="/review/exp-new" class="lz-formgrid">'
            '<input name="content" class="lz-input" placeholder="经验内容" required>'
            '<input name="applicability" class="lz-input" placeholder="适用条件">'
            '<button class="lz-btn">新增经验候选</button></form>'
        )
    return f'<section class="rv-main"><h3>经验沉淀（三态）</h3>{form}{cards}</section>'


# ---------------------------------------------------------------------------
# 根因分析（doc 05 §5）

_HYP_STATUS_CHIP = {
    "pending": "lz-chip-warn", "verified": "lz-chip-ok",
    "rejected": "lz-chip-dim",
}


def _tab_root(state, commander: bool, owner_mode: bool = False) -> str:
    ledger = state.review_ledger
    order = {"pending": 0, "verified": 1, "rejected": 2}
    hyps = sorted(
        ledger.hypotheses.values(),
        key=lambda h: (order.get(h.status, 9), h.hyp_id),
    )
    from .review_causality import assessments
    current = assessments(state) if state.workspace.event_id else {}
    cards = "".join(_hyp_card(h, owner_mode,
                    assessment=current.get(f'hypothesis:{h.hyp_id}'),
                    bound=bool(state.workspace.event_id)) for h in hyps) or \
        '<p class="dim">尚无根因假设。可在下方表单登记，或在「问题分析」执行规则扫描生成同期关联候选。</p>'
    form = ""
    if commander:
        form = (
            '<form method="post" action="/review/hyp-new" class="lz-formgrid">'
            '<input name="statement" class="lz-input" placeholder="原因假设（一句话，只表述相关）" required>'
            '<select name="correlation" class="lz-input">'
            '<option>弱</option><option>中</option><option>强</option></select>'
            '<input name="evidence" class="lz-input" placeholder="支持证据（审计序号/id，分号分隔）">'
            '<input name="counter_examples" class="lz-input" placeholder="反例（不支持的样本/事件，可空）">'
            '<input name="related_issues" class="lz-input" placeholder="关联问题编号（逗号分隔，可空）">'
            '<textarea name="to_verify" class="lz-input" rows="2" '
            'placeholder="待验证事实（每行一条，补足后才能判已验证）"></textarea>'
            '<button class="lz-btn">登记根因假设（待验证）</button></form>'
        )
    return (
        '<section class="rv-main"><h3>根因分析（非定责）</h3>'
        '<p class="lz-note">三类结论边界：<b>事实</b>＝有证据支撑确实发生；'
        "<b>因果假设</b>＝规则推断、需证据验证；<b>责任认定</b>＝永不自动生成，"
        "仅复盘负责人手工形成。</p>"
        '<p class="lz-note">因果纪律：只表述「相关」不写成因果；'
        "「待验证事实」逐条补足后状态机才允许标「已验证」；"
        "判定不成立必须附依据，重开留痕。</p>"
        f"{form}{cards}</section>"
    )


def _hyp_card(hyp: rd.RootCauseHypothesis, owner_mode: bool = False, *,
              assessment=None, bound=False) -> str:
    ev = "、".join(hyp.evidence_ids) or "—"
    causal_mark = (
        "" if hyp.status == "verified"
        else '<span class="lz-chip lz-chip-dim">相关·非因果</span>'
    )
    resolved_set = set(hyp.resolved_items)
    tv_rows = "".join(
        f"<li>☑ {esc(i)}</li>" for i in hyp.to_verify if i in resolved_set
    ) + "".join(
        "<li>☐ " + esc(i)
        + (
            '<form method="post" action="/review/hyp-resolve" class="lz-inlineform">'
            f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
            f'<input type="hidden" name="item" value="{esc(i)}">'
            '<input name="answer" class="lz-input" placeholder="核实结论" required>'
            '<button class="lz-btn lz-btn2">记核实</button></form>'
            if owner_mode and hyp.status == "pending" else ""
        )
        + "</li>"
        for i in hyp.unresolved_verifications()
    ) or "<li>—（未列出待验证事实）</li>"
    actions = ""
    if owner_mode and hyp.status == "pending":
        actions = (
            '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
            f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
            '<button name="target" value="verified" class="lz-btn lz-btn2">标记已验证</button>'
            '</form>'
            '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
            f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
            '<input name="reason" class="lz-input" placeholder="判定不成立的依据" required>'
            '<button name="target" value="rejected" class="lz-btn lz-btn2">判定不成立</button>'
            '</form>'
        )
    elif owner_mode and hyp.status == "verified":
        actions = (
            '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
            f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
            '<input name="reason" class="lz-input" placeholder="反证说明" required>'
            '<button name="target" value="rejected" class="lz-btn lz-btn2">反证推翻</button>'
            '</form>'
        )
    elif owner_mode and hyp.status == "rejected":
        actions = (
            '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
            f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
            '<input name="reason" class="lz-input" placeholder="重开原因（新证据）" required>'
            '<button name="target" value="pending" class="lz-btn lz-btn2">新证据重开</button>'
            '</form>'
        )
    if bound and owner_mode:
        basis = assessment.id if assessment else ''
        actions = ''
        if hyp.status in {'pending', 'verified'}:
            actions = (
                '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
                f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
                f'<input type="hidden" name="assessment_ref" value="{esc(basis)}">'
                '<input name="reason" class="lz-input" required placeholder="负责人因果核实理由及其他解释处理">'
                '<button name="target" value="verified" class="lz-btn lz-btn2">确认根因</button>'
                '<button name="target" value="rejected" class="lz-btn lz-btn2">判定不成立</button>'
                '</form>')
        if hyp.status == 'rejected':
            actions = (
                '<form method="post" action="/review/hyp-status" class="lz-inlineform">'
                f'<input type="hidden" name="hyp_id" value="{esc(hyp.hyp_id)}">'
                '<input name="reason" class="lz-input" required placeholder="新证据与重开理由">'
                '<button name="target" value="pending" class="lz-btn lz-btn2">新证据重开</button>'
                '</form>')
    rel = "、".join(hyp.related_issues) or "—"
    log = "".join(f"<li>{esc(l)}</li>" for l in hyp.verification_log[-3:])
    return (
        f'<article class="issue-card"><header><code>{esc(hyp.hyp_id)}</code> '
        f'{esc(hyp.statement)} '
        f'<span class="lz-chip {_HYP_STATUS_CHIP.get(hyp.status, "lz-chip-dim")}">'
        f'{esc(hyp.status_word())}</span>'
        f'<span class="lz-chip lz-chip-reg">相关·{esc(hyp.correlation)}</span>'
        f'{causal_mark}'
        f'<span class="lz-chip lz-chip-dim">置信度 {esc(hyp.confidence)}</span></header>'
        f'<p class="dim">支持证据：{esc(ev)} · 反例：{esc(hyp.counter_examples or "—")} · '
        f'关联问题：{esc(rel)} · 来源：{esc(hyp.origin)}'
        + (f" · 备注：{esc(hyp.note)}" if hyp.note else "")
        + '</p>'
        + (f'<p class="lz-note">最新核查依据：{esc(assessment.id if assessment else "未登记")}；'
           + ('新版本待负责人重新判断。' if hyp.confirmed_assessment_ref and assessment
              and hyp.confirmed_assessment_ref != assessment.id else '结论仍须人工核实。')
           + '</p>' if bound else '')
        + '<p class="dim">待验证事实：</p><ul>' + tv_rows + '</ul>'
        + (f'<p class="dim">核实记录（最近三条）：</p><ul>{log}</ul>' if log else '')
        + actions + '</article>'
    )


# ---------------------------------------------------------------------------
# 报告导出（doc 05 §12：AI 草稿 → 人工编辑 → 待审核 → 已发布；本层产 AI 草稿）


_ISSUE_BUCKET = {
    "指挥": "指挥问题",
    "作战": "作战问题",
    "信息": "物资通信及其他问题",
    "资源后勤": "物资通信及其他问题",
    "通信": "物资通信及其他问题",
    "安全协同": "物资通信及其他问题",
}


def export_review_markdown(state) -> str:
    if state.workspace.event_id:
        from .review_flow_view import markdown
        return markdown(state)
    """复盘报告 Markdown（固定章节，缺数据如实标「无记录」，不补写）。"""
    ws = state.workspace
    from .review_meeting import scoped_state
    state = scoped_state(state)
    ledger = state.review_ledger
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    event = registry.events.get(active_id) if registry is not None and active_id else None
    from . import review_causality
    import json
    bound = bool(ws.event_id)
    assessments = review_causality.assessments(state) if bound else {}

    def refs(values):
        return '、'.join(values) or '—'

    def assessment_lines(kind, object_id):
        revision = assessments.get(f'{kind}:{object_id}')
        if revision is None:
            return ['  - 证据核查：未登记，保持待核。']
        data = json.loads(revision.content_json)
        return [
            f'  - 核查版本：`{revision.id}`；录入人 {revision.actor_id}；时刻 {revision.times.recorded_at}',
            f'  - 支持证据：{refs(data.get("support_refs", ()))}；反证：{refs(data.get("counter_refs", ()))}',
            f'  - 反证说明：{data.get("counter_note") or "未登记"}；其他解释：{data.get("alternatives") or "待核"}',
            f'  - 缺失信息：{data.get("missing_info") or "本次核查未列出"}；核实对象：{data.get("verify_targets") or "待核"}',
            f'  - 会议版本：{data.get("meeting_ref") or "未关联"}；会议核实反馈：{data.get("meeting_finding") or "待核"}',
        ]

    def stale(kind, item):
        positive = item.review_status == 'confirmed' if kind == 'issue' else item.status == 'verified'
        return bound and positive and not review_causality.current_confirmation(
            state, kind, item.issue_id if kind == 'issue' else item.hyp_id)
    lines: list[str] = []
    broken = ws.verify_audit_chain()
    lines.append(f"# 火情复盘报告：{getattr(event, 'name', '') or ws.scenario_id}")
    lines.append("")
    lines.append("- 生成时报告状态：**系统草稿（缺证据处标未核实，需人工编辑后审核）**")
    lines.append(
        "- 审计链：" + ("完整" if not broken else f"**断链于序号 {broken}**")
    )
    if event is not None:
        lines.append(
            f"- 事件：{event.name}（{event.report_no or event.event_id}），"
            f"状态 {event.status_word()}"
        )
    else:
        lines.append("- 事件：—（未进入事件任务空间，无事件概况）")

    # 一、事件概况
    lines.extend(["", "## 一、事件概况", ""])
    for name, value, status in rd.event_digest(event, ws):
        lines.append(f"- {name}：{value}（{status}）")

    # 二、组织到场
    lines.extend(["", "## 二、组织到场", ""])
    if bound:
        from .review_sources import arrival_total
        lines.append(f'- 累计到场报告：{arrival_total(state)} 人（原始报告去重；不等于当前可派人数）。')
        for audit in ws.events:
            if audit.kind == 'SquadArrivalReported':
                squad_id = audit.payload.get('squad_id', '')
                squad = state.ledger.squads.get(squad_id)
                lines.append(f'- 中队 {squad.name if squad else squad_id}：本次报告到场 '
                    f'{len(set(audit.payload.get("arrived_phones", ())))} 人；来源 `{audit.id}` / 审计 #{audit.sequence}。')
            elif audit.kind == 'TemporaryPersonArrived':
                lines.append(f'- 临时支援到场报告：1 人；来源 `{audit.id}` / 审计 #{audit.sequence}。')
    bindings = getattr(ws, "roster_bindings", {}) or {}
    active_b = [b for b in bindings.values() if getattr(b, "status", "") == "active"]
    if active_b:
        for b in active_b:
            person = getattr(b, "person", None)
            name = getattr(person, "name", "") or "?"
            slot = getattr(b, "pos_code", "") or "岗位未解析"
            lines.append(f"- {name} · {slot}")
    else:
        lines.append("（无入岗绑定记录；到场报告与岗位绑定分别核对）" if bound else "（无入岗绑定记录）")

    # 三、态势变化
    lines.extend(["", "## 三、态势变化（标绘核验链）", ""])
    marks = sorted(getattr(ws, "annotations", {}).values(), key=lambda m: m.id)
    if marks:
        from .map_annotations import status_word as _sw
        for mark in marks:
            certainty = f"/{'疑似' if mark.certainty == 'suspected' else '已确认'}" if mark.certainty else ""
            lines.append(
                f"- `{mark.id}` {mark.title}（{_sw(mark.status)}{certainty}，来源 {mark.source.value}）"
            )
    else:
        lines.append("（图上无标绘记录）")

    # 四、决策时间线与命令
    lines.extend(["", "## 四、决断与命令时间线", ""])
    timeline = rd.build_timeline(ws)
    decision_kinds = {"GeneralDecisionRecorded", "OrderConfirmed", "FormalExecutionOrderPublished", "OrderActivated",
                      "OrderAnomalyPaused", "OrderCancelled"}
    relevant = [e for e in timeline if e.kind in decision_kinds]
    if relevant:
        for entry in relevant:
            when = entry.time or rd.NO_TIME_NOTE
            lines.append(
                f"- #{entry.sequence} {entry.label}（{when}；操作者 {entry.actor or '—'}；"
                f"{entry.object_ref or '—'}）"
            )
    else:
        lines.append("（无决断/命令记录）")

    # 五、异常与计划/实际差异
    lines.extend(["", "## 五、异常与计划/实际差异", ""])
    anomalies = [e for e in timeline if e.kind in {"OrderAnomalyPaused", "OrderPaused"}]
    lines.append(f"- 异常暂停次数：{len(anomalies)}")
    digest = rd.event_digest(event, ws)
    for row in rd.plan_actual_rows(digest):
        lines.append(
            f"- {row['item']}：计划 {row['plan']}；实际 {row['actual']}；"
            f"偏差 {row['deviation']}"
        )
    if bound:
        from .review_replay import replay, plan_comparisons
        comparisons = plan_comparisons(replay(ws, active_id)[0])
        lines.extend(f'- 明确关联计划核对：{row}' for row in comparisons)

    # 六~八、问题（按 AI 候选 / 人工确认分列，再按主题分组）
    issues = rd.sort_issues(list(ledger.issues.values()))
    pending = [i for i in issues if i.review_status == "pending" or stale('issue', i)]
    settled = [i for i in issues if i not in pending]
    lines.extend(["", "## 六、候选问题（未核实，待负责人核验）", ""])
    if pending:
        for issue in pending:
            lines.append(
                f"- [{issue.severity}] {issue.title}（证据：{'、'.join(issue.evidence_ids) or '—'}；"
                f"置信度 {issue.confidence}）"
            )
            lines.append(f'  - 编号 `{issue.issue_id}`；待核验：{issue.to_verify or "未列出"}'
                         + ('；旧确认已过期，待重新核实。' if stale('issue', issue) else ''))
            lines.extend(assessment_lines('issue', issue.issue_id))
    else:
        lines.append("（无待核验候选）")
    lines.extend(["", "## 七、人工确认结论与否决记录", ""])
    buckets: dict[str, list] = {}
    for issue in settled:
        buckets.setdefault(_ISSUE_BUCKET.get(issue.category, "其他"), []).append(issue)
    if settled:
        for bucket_name in ("指挥问题", "作战问题", "物资通信及其他问题"):
            items = buckets.get(bucket_name) or []
            lines.append(f"### {bucket_name}")
            if not items:
                lines.append("（无）")
            for issue in items:
                lines.append(
                    f"- [{issue.status_word()}] {issue.title}"
                    + (f"——{issue.note}" if issue.note else "")
                )
                lines.append(f'  - 编号 `{issue.issue_id}`；证据 {refs(issue.evidence_ids)}；'
                             f'结论依据 {issue.confirmed_assessment_ref or "未登记"}；'
                             f'核实人 {issue.updated_by or "未登记"}；时刻 {issue.updated_at or "未登记"}')
                lines.extend(assessment_lines('issue', issue.issue_id))
            lines.append("")
    else:
        lines.append("（尚无人工确认结论）")

    # 九、整改任务
    lines.extend(["", "## 八、整改任务", ""])
    rects = sorted(ledger.rects.values(), key=lambda t: t.rect_id)
    if rects:
        for task in rects:
            overdue = "【超期】" if task.is_overdue() else ""
            lines.append(
                f"- `{task.rect_id}`{overdue}{task.measures}"
                f"（{task.status_word()}；责任 {task.owner_unit or '—'}"
                f" {task.owner_person or '—'}；期限 {task.deadline or '未设置'}）"
            )
            lines.append(f'  - 来源问题 {task.source_issue_id or refs(task.related_ids)}；根因 {task.source_hyp_id or "—"}；'
                         f'核查版本 {refs(task.source_assessment_refs)}')
            lines.append(f'  - 验收标准：{task.acceptance_standard or "未登记"}；'
                         f'验证方式：{task.verify_method or "未登记"}；指定验收人：{task.verifier_name or "未登记"}；'
                         f'发布人/时刻：{task.published_by or "未登记"}/{task.published_at or "未登记"}')
            for row in task.feedback_records:
                feedback_kind = {'completed': '报告完成（待验收）', 'progress': '进展'}.get(row.get('kind'), '待核')
                lines.append(f'  - 责任人反馈（{row.get("reported_at", "")}）：{row.get("provided_by", "")} '
                             f'{row.get("duty", "")}；{feedback_kind}；{row.get("content", "")}；'
                             f'证据 {refs(row.get("evidence_refs", ()))}；集中录入 {row.get("recorded_by", "")}')
            for row in task.verification_records:
                outcome = {'passed': '通过', 'failed': '未通过，退回整改'}.get(row.get('outcome'), '待核')
                lines.append(f'  - 指定验收（{row.get("verified_at", "")}）：{row.get("verifier", "")}；'
                             f'{outcome}；{row.get("finding", "")}；证据 {refs(row.get("evidence_refs", ()))}；'
                             f'集中录入 {row.get("recorded_by", "")}')
    else:
        lines.append("（无整改任务）")

    # 十、经验沉淀
    lines.extend(["", "## 九、经验沉淀", ""])
    exps = sorted(ledger.experiences.values(), key=lambda e: e.exp_id)
    if exps:
        for exp in exps:
            lines.append(
                f"- `[{exp.status_word()}]` {exp.content}"
                f"（适用条件：{exp.applicability or '—'}；确认人 {exp.confirmed_by or '—'}）"
            )
            lines.append(f'  - 编号 `{exp.exp_id}` v{exp.version}；来源事件 {exp.source_event_id}；'
                         f'来源整改/问题/根因 {exp.source_rect_id or "—"}/{exp.source_issue_id or "—"}/{exp.source_hyp_id or "—"}')
            lines.append(f'  - 证据：{refs(exp.evidence_ids)}；条件标签：{refs(exp.condition_tags)}；'
                         f'适用限制：{exp.limitations or "未登记"}；失效条件：{exp.expiry_condition or "未登记"}；'
                         f'反例：{refs(exp.counter_refs)}')
    else:
        lines.append("（无经验条目）")

    # 十、根因假设（仅在存在假设时输出本章；同态确定性不变）
    hyp_all = sorted(ledger.hypotheses.values(), key=lambda h: h.hyp_id)
    if hyp_all:
        lines.extend(["", "## 十、根因核查与待验证假设", ""])
        for h in hyp_all:
            unres_n = len(h.unresolved_verifications())
            extra = (
                f"未核实待验证事实 {unres_n} 条" if unres_n
                else ("待验证事实已全部核实" if h.to_verify else "未列出待验证事实")
            )
            basis = assessments.get(f'hypothesis:{h.hyp_id}')
            layer = json.loads(basis.content_json).get('causal_layer') if basis else None
            label = ('旧确认已过期，待重新核实' if stale('hypothesis', h) else
                     {'direct': '负责人已核实直接原因', 'deep': '负责人已核实深层原因'}.get(layer, '已验证（历史记录，层级未登记）')
                     if h.status == 'verified' else
                     '不成立' if h.status == 'rejected' else '待验证，相关线索·非因果结论')
            lines.append(
                f"- `{h.hyp_id}` [{label}｜相关性记录·{h.correlation}｜"
                f"置信度 {h.confidence}] {h.statement}"
                f"（证据：{'、'.join(h.evidence_ids) or '—'}；{extra}）"
                + (f"——负责人判断依据：{h.note}" if h.note else "")
            )
            lines.append(f'  - 关联问题：{refs(h.related_issues)}；反例：{h.counter_examples or "未登记"}；'
                         f'未核实事实：{refs(h.unresolved_verifications())}；'
                         f'确认核查版本：{h.confirmed_assessment_ref or "未登记"}')
            lines.extend(assessment_lines('hypothesis', h.hyp_id))
            lines.extend(f'  - 核实记录：{row}' for row in h.verification_log)
            lines.extend(f'  - 负责人判断记录：{row}' for row in h.history)
        lines.append("")
        lines.append("待验证假设不得当作因果结论；已核实原因依负责人具名核查版本记录，系统不自动定责。")

    lines.extend(['', '## 十一、会议补充记录（报告待核，不自动确认）', ''])
    meetings = sorted((row for row in ws.object_revisions.values()
                       if row.reference.event_id == active_id
                       and row.reference.object_kind == 'review_meeting_record'),
                      key=lambda row: (row.times.recorded_at, row.id))
    for row in meetings:
        data = json.loads(row.content_json)
        record_type = {'plan': '原始计划基准报告', 'actual': '实际里程碑报告',
                       'feedback': '会议反馈'}.get(data.get('record_type'), '会议反馈')
        lines.append(f'- `{row.id}` {data.get("subject", "")}；提供人 {data.get("provided_by", "")}；'
                     f'反馈：{data.get("feedback", "")}；证据 {data.get("evidence_refs") or "—"}；'
                     f'类型 {record_type}；计划时刻 {data.get("planned_at") or "未登记"}；'
                     f'关联计划版本 {data.get("plan_reference") or "未关联"}；'
                     f'发生 {row.times.occurred_at or "未知"}；录入 {row.times.recorded_at} / {row.actor_id}')
    if not meetings:
        lines.append('（无会议补充记录，尚未核实）')

    lines.extend(['', '## 十二、任务执行反馈（报告不等于核验完成）', ''])
    execution_reports = sorted(ws.reports.values(), key=lambda row: row.id)
    for row in execution_reports:
        status = getattr(row.status, 'value', row.status)
        status_word = {'succeeded': '报告执行成功', 'failed': '报告失败', 'partial': '报告部分完成',
                       'timeout': '报告超时', 'permission_denied': '权限拒绝',
                       'unknown_outcome': '执行结果未知'}.get(status, str(status))
        lines.append(f'- `{row.id}` 关联命令 {row.order_id}；{status_word}；'
                     f'反馈：{refs(row.observations)}；执行契约 {row.action_contract_id or "未关联"}')
    if not execution_reports:
        lines.append('（无任务执行反馈记录）')

    lines.extend([
        "",
        "---",
        "本文档由系统按审计链与复盘底账自动汇总（系统草稿）；定责结论仅由复盘负责人手工形成。",
        "",
    ])
    if bound:
        from .review_sources import markdown
        lines.append(markdown(state))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 组装


def _report_tab(state, commander: bool, selected_id: str,
                owner_mode: bool = False) -> str:
    ledger = state.review_ledger
    reports = sorted(
        ledger.reports.values(),
        key=lambda r: (r.event_id, -r.version),
    )
    rows = []
    for report in reports:
        actions = ""
        if owner_mode:
            if report.status in {"ai_draft", "edited"}:
                actions += (
                    f' <a href="/review?tab=report&rid={esc(report.report_id)}">编辑</a>'
                    '<form method="post" action="/review/report-status" class="lz-inlineform">'
                    f'<input type="hidden" name="report_id" value="{esc(report.report_id)}">'
                    '<input type="hidden" name="target" value="pending_review">'
                    '<button class="lz-btn lz-btn2">提交审核</button></form>'
                )
            if report.status == "pending_review":
                actions += (
                    '<form method="post" action="/review/report-status" class="lz-inlineform">'
                    f'<input type="hidden" name="report_id" value="{esc(report.report_id)}">'
                    '<button name="target" value="published" class="lz-btn lz-btn2">发布</button>'
                    '<button name="target" value="edited" class="lz-btn lz-btn2">退回修改</button></form>'
                )
            if report.status == "published":
                actions += (
                    '<form method="post" action="/review/report-status" class="lz-inlineform">'
                    f'<input type="hidden" name="report_id" value="{esc(report.report_id)}">'
                    '<input type="hidden" name="target" value="archived">'
                    '<button class="lz-btn lz-btn2">归档</button></form>'
                )
            actions += f' <a href="/review?tab=report&rid={esc(report.report_id)}">查看</a>'
        elif commander:
            # 指挥员可查看，无结论级动作（A-2）
            actions += f' <a href="/review?tab=report&rid={esc(report.report_id)}">查看</a>'
        rows.append(
            f'<tr><td class="mono">{esc(report.report_id)}</td>'
            f'<td><span class="lz-chip lz-chip-reg">{esc(report.status_word())}</span></td>'
            f'<td class="dim">{esc(report.edited_at or report.created_at)}</td>'
            f'<td>{actions}</td></tr>'
        )
    table_body = "".join(rows) if rows else \
        '<tr><td colspan="4">尚无报告版本。</td></tr>'
    event = state.registry.events.get(state.active_event_id)
    stage = bool(state.workspace.event_id and event and event.status != 'closed')
    gen_btn = (
        '<form method="post" action="/review/report-generate" class="lz-inlineform">'
        + ('<input type="hidden" name="stage" value="1">' if stage else '')
        + '<button class="lz-btn lz-btn2">'
        + ('生成本阶段复盘草稿（不结束火情）' if stage else '按当前数据生成新版本（系统草稿）')
        + '</button></form>'
        if commander else ""
    )
    preview = ""
    selected = ledger.reports.get(selected_id)
    if selected is not None:
        from .review_reports import freshness_error, delivery_links
        source_notice = (freshness_error(state, selected) if state.workspace.event_id else '')
        source_info = (
            f'<p>报告范围：{"阶段复盘（非最终结案）" if selected.stage_report else "事件复盘"}</p>'
            f'<p>生成时来源：工作区 v{selected.source_workspace_version} / 审计 #{selected.source_audit_sequence}；'
            f'指纹 <span class="mono">{esc(selected.source_digest or "历史版本未保存")}</span>。'
            f'审核发布人 {esc(selected.reviewed_by or "尚未发布")}；'
            f'审核时间 {esc(selected.reviewed_at or "—")}。</p>'
            + (f'<p class="lz-note">{esc(source_notice)} 本页正文保留生成时内容。</p>' if source_notice else '')
        )
        edit_box = ""
        recovery = ''
        if owner_mode and state.workspace.event_id and not source_notice:
            from .review_word import card
            recovery = card(selected)
        if selected.imported_from_report_id:
            source_info += (f'<p>Word 回收来源：{esc(selected.imported_from_report_id)}；'
                            f'原文件指纹 {esc(selected.imported_file_sha256)}。回收正文仍须负责人审核。</p>')
        if owner_mode and selected.status in {"ai_draft", "edited"}:
            edit_box = (
                '<form method="post" action="/review/report-edit">'
                f'<input type="hidden" name="report_id" value="{esc(selected.report_id)}">'
                '<textarea name="content_md" rows="16" style="width:100%;white-space:pre-wrap">'
                f'{esc(selected.content_md)}</textarea>'
                '<button class="lz-btn" style="margin-top:6px">保存人工编辑</button></form>'
            )
        preview = (
            f'<h3>预览：{esc(selected.report_id)}（{esc(selected.status_word())}）</h3>'
            + source_info
            + (delivery_links(state, selected) if commander else '')
            + edit_box
            + recovery
            + f'<pre style="white-space:pre-wrap;background:#F7F9FA;padding:10px;border:1px solid #E1E6EB">{esc(selected.content_md)}</pre>'
            + "".join(
                f'<div>{esc(h)}</div>' for h in selected.history[-5:]
            )
        )
    return (
        '<section style="min-width:0;overflow-wrap:anywhere"><h3>报告版本（新证据 → 新版本；历史不覆盖）</h3>'
        + gen_btn
        + '<table class="lz-table"><thead><tr><th>编号</th><th>状态</th><th>最后变更</th><th>操作</th></tr></thead>'
        + f'<tbody>{table_body}</tbody></table>'
        + '<p class="lz-note">状态机：系统草稿 → 人工编辑 → 待审核 → 已发布 → 已归档；'
        + "发布/归档后内容不可变。</p>"
        + preview
        + '</section>'
    )


def _ask_card(state, role: str, error: str = "", *, viewer='', token=None) -> str:
    """复盘问询卡（doc 05 §11）：只读问答，七字段展示。"""
    from panda_brain.review_domain import REVIEW_ASK_NOTE
    log = getattr(state, "review_qa_log", []) or []
    if state.workspace.event_id:
        from .review_inquiry import history, source_links
        log = history(state, 'commander' if role in {'owner', 'commander'} else 'member', viewer)
    rows = []
    for question, ans in reversed(log[-5:]):
        refs = (
            f"<div class='dim mono'>引用事件：{esc('、'.join(ans.references))}</div>"
            if ans.references else ""
        )
        ev = (
            f"<div class='dim mono'>证据链接：{esc(' '.join(ans.evidence))}</div>"
            if ans.evidence else ""
        )
        details = ''
        if ans.source_digest:
            sections = (('记录事实', ans.facts), ('记录分析与解释', ans.analysis), ('待核实与缺失', ans.unverified))
            details = ''.join('<h4>'+label+'</h4><ul>'+''.join('<li>'+esc(value)+'</li>' for value in values)+'</ul>'
                              for label, values in sections if values)
            ev = '<p>本次回答来源：'+source_links(ans)+'</p>'
        rows.append(
            '<div class="sand-card"><b>' + esc(question) + "</b>"
            f"<p>{esc(ans.conclusion)}</p>"
            + details
            + f"<p class='dim'>数据范围：{esc(ans.scope)}</p>"
            + refs + ev
            + f"<p class='dim'>统计口径：{esc(ans.statistics)}</p>"
            + (f"<p class='lz-note'>未纳入：{esc(ans.excluded)}</p>" if ans.excluded else "")
            + f"<p class='dim'>置信度：{esc(ans.confidence)}</p></div>"
        )
    body = "".join(rows) or "<p class='dim'>暂无问询记录。</p>"
    from .review_meeting import form_fields
    fields = form_fields(state, token) if state.workspace.event_id else ''
    return (
        '<section class="sand-card" id="rvqa" style="overflow-wrap:anywhere"><h3>复盘问询（只读；文字输入）</h3>'
        '<form method="post" action="/review/ask" class="lz-inlineform">'
        + fields
        + '<label for="rv-question">问题文字（可使用系统或输入法听写）</label>'
        '<textarea id="rv-question" name="question" aria-describedby="rv-input-note" class="lz-input" style="min-width:0;width:100%;max-width:520px" '
        'maxlength="2000" placeholder="如：本次任务延误有哪些依据 / 哪些证据尚待核实" required></textarea>'
        '<label><input type="checkbox" name="follow_up" value="1">作为上次问题的追问</label>'
        '<button class="lz-btn lz-btn2">提问</button></form>'
        '<p class="lz-note" id="rv-input-note">点击输入框后，可用现有听写功能输入文字；请核对、校正后点击“提问”。</p>'
        '<p class="rv-ask-error" role="alert"></p>'
        + (f'<p class="lz-alert">问询失败：{esc(error)}</p>' if error else "")
        + body
        + f'<p class="lz-note">{esc(REVIEW_ASK_NOTE.replace("共性对比、", "") if state.workspace.event_id else REVIEW_ASK_NOTE)}</p></section>'
        + '''<script>(()=>{const card=document.getElementById('rvqa'),form=card.querySelector('form');
        form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button'),err=card.querySelector('.rv-ask-error');
        button.disabled=true;err.textContent='';try{const response=await fetch(form.action,{method:'POST',headers:{'X-Review-Ask':'1'},body:new URLSearchParams(new FormData(form))});
        const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.message||'问询失败，请重试');
        window.history.replaceState(null,'','/review?tab=summary#rvqa');location.reload();}catch(error){err.textContent=error.message+'；输入已保留。';button.disabled=false;}});})();</script>'''
    )


def _tab_trend(state) -> str:
    """趋势与统计（doc 05 §7）：口径公开、可下钻、不做黑盒综合评分。

    指挥员专属（跨事件聚合）；只读计算，零写入。
    """
    from pathlib import Path

    from .persistence import load_workspace
    from .trend_stats import compute_trend_metrics

    def _loader(event_id: str):
        record = state.registry.events.get(event_id)
        if record is None or not getattr(record, "snapshot_path", ""):
            return None
        path = Path(record.snapshot_path)
        if not path.exists():
            return None
        try:
            return load_workspace(path, general_auth=state.auth)
        except Exception:  # noqa: BLE001 - 单快照损坏按缺失处理，不拖垮整体
            return None

    metrics = compute_trend_metrics(
        state.registry,
        ledger=getattr(state, "ledger", None),
        load_event_space=_loader,
    )
    cards: list[str] = []
    for m in metrics:
        if m.available:
            value_html = (
                f'<strong>{esc(m.value)}</strong> {esc(m.unit)}'
                if m.value is not None
                else '<span class="lz-chip lz-chip-warn">样本不足，暂不可算</span>'
            )
        else:
            value_html = (
                '<span class="lz-chip">暂无数据源</span>'
                f'<span class="lz-note">{esc(m.unavailable_reason)}</span>'
            )
        drill_rows = "".join(
            f"<tr><td>{esc(s['event_id'])}</td><td>{esc(s['detail'])}</td></tr>"
            for s in m.samples[:12]
        )
        drill = (
            '<details class="rv-drill"><summary>下钻到事实</summary>'
            f'<table class="rv-table"><thead><tr><th>事件</th>'
            f"<th>明细</th></tr></thead><tbody>{drill_rows}</tbody></table></details>"
            if drill_rows else ""
        )
        cards.append(
            '<div class="rv-card">'
            f"<h4>{esc(m.name)}　{value_html}</h4>"
            '<table class="rv-table rv-kv"><tbody>'
            f"<tr><th>计算方法</th><td>{esc(m.method)}</td></tr>"
            f"<tr><th>样本数量</th><td>纳入 {m.sample_count}；排除 {m.excluded_count}</td></tr>"
            f"<tr><th>缺失处理</th><td>{esc(m.missing_policy)}</td></tr>"
            f"<tr><th>时间边界</th><td>{esc(m.time_bound)}</td></tr>"
            "</tbody></table>" + drill + "</div>"
        )
    return (
        '<section class="sand-col sand-main rv-trend">'
        '<h3>趋势与统计口径</h3>'
        '<p class="lz-note">口径公开：每个指标公布计算方法、样本数量、'
        "缺失处理与时间边界；不做黑盒综合评分；无数据源的指标如实标注，"
        "绝不编造数值。本页为只读聚合，零写入。</p>"
        + "".join(cards) + "</section>"
    )


_BRAIN_CONSTRAINTS = (
    "✗ 自动定责", "✗ 假设写成事实", "✗ 自动发布整改",
    "✗ 经验直入知识库", "✗ 事后反指责", "✗ 无证据补写",
)


def _permissions_card(state, is_commander: bool, role: str) -> str:
    """权限与审核卡（doc 05 §9）：矩阵、智脑硬约束、负责人与维护人员管理。"""
    ledger = state.review_ledger
    if role == "public":
        return (
            '<h3>公共屏口径</h3>'
            '<p class="lz-note">仅展示已发布内容；未发布草稿一律不可见。</p>'
            '<h3>自动分析硬约束</h3><ul>'
            + "".join(f'<li>{esc(t)}</li>' for t in _BRAIN_CONSTRAINTS)
            + "</ul>"
        )
    registry = getattr(state, "registry", None)
    maint_phones = sorted(getattr(registry, "maintenance_phones", ()) or ())
    maint_names = []
    for p in maint_phones:
        person = registry.library.get(p) if registry is not None else None
        maint_names.append(
            f"{person.name}（{mask_phone(p)}）" if person else mask_phone(p))
    owner_line = (
        f"{esc(ledger.owner_name)}（{mask_phone(ledger.owner_phone)}）"
        if ledger.owner_phone else '<span class="lz-dim">未指派</span>'
    )
    if state.workspace.event_id:
        role_word = {'commander':'指挥员（只读）','owner':'复盘负责人','member':'具名人员（只读）'}.get(role,role)
        return ('<h3>权限与审核</h3><p>当前登录身份：'+esc(role_word)+'</p>'
            '<p>指定副总指挥：首次指定本事件负责人。复盘负责人：集中记录、核实与确认发布。'
            '其他会话无复盘写权；历史明细限指挥员、指定副总指挥与负责人读取。公共屏入口未开放。</p>'
            '<p>本事件复盘负责人：'+owner_line+'</p><h4>审核关卡</h4>'
            '<p>事实提供人及业务职责随会议报告保留；待核报告不自动成为根因结论、任务关闭或物资结清。</p>'
            '<h4>自动分析硬约束</h4><ul>'
            +''.join('<li>'+esc(t)+'</li>' for t in _BRAIN_CONSTRAINTS)+'</ul>')
    manage_html = ""
    if is_commander:
        library = registry.library if registry is not None else {}
        lib_options = "".join(
            f'<option value="{esc(p)}">{esc(rec.name)}·{mask_phone(p)}</option>'
            for p, rec in sorted(library.items())
        )
        revoke_opts = "".join(
            f'<option value="{esc(p)}">{esc(maint_names[i])}</option>'
            for i, p in enumerate(maint_phones)
        )
        manage_html = (
            '<details><summary class="lz-dim">管理（指挥员）</summary>'
            '<form method="post" action="/review/owner" class="lz-inlineform">'
            f'<select class="lz-select" name="phone" style="width:auto">{lib_options}</select>'
            '<button class="lz-btn lz-btn2">指派复盘负责人</button></form>'
            '<form method="post" action="/review/maintainer" class="lz-inlineform">'
            f'<select class="lz-select" name="phone" style="width:auto">{lib_options}</select>'
            '<button class="lz-btn lz-btn2">授予维护人员</button></form>'
            + (f'<form method="post" action="/review/maintainer" class="lz-inlineform">'
               f'<input type="hidden" name="revoke" value="1">'
               f'<select class="lz-select" name="phone" style="width:auto">{revoke_opts}</select>'
               '<button class="lz-btn lz-btn2">撤销维护授权</button></form>' if revoke_opts else "")
            + "</details>"
        )
    role_word = {"commander": "指挥员", "owner": "复盘负责人",
                 "member": "到场人员"}.get(role, role)
    return (
        '<h3>权限与审核（规范 05·§9）</h3>'
        '<p class="lz-note">当前登录身份：<b>' + esc(role_word) + '</b></p>'
        '<div class="lz-dim" style="font-size:12px">'
        '复盘负责人：结论级动作全权 · 指挥员：查看/问询/指派负责人/登记候选输入 · '
        '授权维护人员：查看/问询/建议 · 到场人员：本人相关/问询 · 公共屏：已发布摘要</div>'
        '<ul style="font-size:12px"><li>复盘负责人：' + owner_line + '</li>'
        '<li>授权维护人员：'
        + (esc("、".join(maint_names)) if maint_names else '<span class="lz-dim">无</span>')
        + "</li></ul>"
        '<h4 style="margin:6px 0 2px">审核关卡</h4>'
        '<ul style="font-size:12px"><li>候选问题 → 负责人核验 → 确认/修订/否决</li>'
        '<li>根因假设候选 → 证据验证 → 确认或降为待验证</li>'
        '<li>经验候选 → 负责人确认 → 进正式经验库</li>'
        '<li>定责 → 仅负责人手工形成（本系统不提供自动定责入口）</li>'
        '<li>整改任务 → 负责人发布</li></ul>'
        '<h4 style="margin:6px 0 2px">自动分析硬约束</h4><ul style="font-size:12px">'
        + "".join(f"<li>{esc(t)}</li>" for t in _BRAIN_CONSTRAINTS)
        + "</ul>"
        '<p class="lz-note" style="font-size:11px">A-2 已严格落实：假设核验/整改流转/经验确认/'
        '报告编辑与定版六类结论级动作仅复盘负责人本人会话可执行，'
        '指挥员等其他会话一律 403；指挥员可指派负责人并登记候选输入。</p>'
        + manage_html
    )


def render_review_page(
    state,
    *,
    role: str = "commander",
    viewer_word: str = "指挥员",
    tab: str = "summary",
    message: str = "",
    selected_report: str = "",
    review_token=None,
    review_deputy=False,
    inquiry_viewer='',
) -> str:
    if state.workspace.event_id:
        from .review_flow_view import render
        return render(state,role=role,viewer_word=viewer_word,tab=tab,message=message,
            selected_report=selected_report,review_token=review_token,review_deputy=review_deputy)
    from .review_meeting import scoped_state
    from .review_closure import recommendations
    from .review_closure_view import rect_tab, exp_tab
    recs = (recommendations(state) if state.workspace.event_id
            and tab in {'summary', 'exp'} and role in {'commander', 'owner'} else ())
    state = scoped_state(state)
    ws = state.workspace
    event = None
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    if registry is not None and active_id:
        event = registry.events.get(active_id)
    commander = role in {"commander", "owner"}   # 可见全部门 Tab 与候选登记入口
    is_commander = role == "commander"             # 仅指挥员可见的管理表单
    owner_mode = role == "owner"                  # A-2：六类结论级动作仅负责人
    bound = bool(ws.event_id)
    valid_tabs = {key for key, _ in _TABS} - ({'common','trend'} if bound else set())
    if tab not in valid_tabs or (tab == "trend" and not commander):
        tab = "summary"
    body_by_tab = {
        "summary": lambda: _tab_summary(state, event, commander or review_deputy),
        "timeline": lambda: _tab_timeline(state),
        "issues": lambda: _tab_issues(state, owner_mode if bound else commander),
        "root": lambda: _tab_root(state, commander, owner_mode),
        "analysis": lambda: _tab_analysis(state, owner_mode),
        "common": lambda: _tab_common(state),
        "rect": lambda: rect_tab(state, owner_mode) if bound else _tab_rect(state, active_id, commander, owner_mode),
        "exp": lambda: exp_tab(state, owner_mode, recs) if bound else _tab_exp(state, commander, owner_mode),
        "report": lambda: _report_tab(state, commander, selected_report, owner_mode),
        "trend": lambda: _tab_trend(state),
    }
    main = body_by_tab[tab]()
    if bound and tab == 'summary' and recs:
        matched = [row for row in recs if row['matched']]
        main = ('<section class="rv-main"><h3>后续事件经验条件提示</h3>'
                f'<p>{len(matched)} 条已确认经验条件匹配；{len(recs)-len(matched)} 条有差异待人工核对。'
                '<a href="/review?tab=exp">查看依据、条件差异与限制</a>。提示不形成命令。</p></section>'
                + main)
    if bound and not owner_mode:
        import re
        main = re.sub(r'<form\b[^>]*>.*?</form>', '', main, flags=re.S)
    # 右侧栏：待核验项速览
    pending = [i for i in state.review_ledger.issues.values() if i.review_status == "pending"]
    side_rows = "".join(
        f'<li>{esc(i.title)}<span class="lz-chip lz-chip-warn">待核验</span></li>'
        for i in pending[:5]
    ) or "<li>无待核验项。</li>"
    sidebar = (
        '<aside class="sand-col sand-right rv-side"><h3>待核验速览</h3>'
        f"<ul>{side_rows}</ul>"
        + _permissions_card(state, is_commander, role)
        + "</aside>"
    )
    if bound:
        import re
        from .review_meeting import form_fields, meeting_card
        fields = form_fields(state,review_token)
        main = re.sub(r'(<form\b[^>]*action="/review/[^"]+"[^>]*>)', lambda m:m.group(1)+fields, main)
        main = meeting_card(state,review_token,owner_mode=owner_mode,deputy=review_deputy)+main
        sidebar = re.sub(r'<form\b[^>]*>.*?</form>', '', sidebar, flags=re.S)
        sidebar = sidebar.replace('指挥员：查看/问询/指派负责人/登记候选输入', '指定副总指挥：指定负责人；指挥员：只读/问询')
    msg_html = f'<p class="lz-alert">{esc(message)}</p>' if message else ""
    ask_card = _ask_card(state, role, viewer=inquiry_viewer, token=review_token)
    export_link = (
        '<p class="lz-note">报告导出：<a href="/export/review" download>'
        "下载当前汇总 Markdown（系统草稿）</a> · "
        '<a href="/review?tab=report">选择报告版本下载 Word/PDF</a>。</p>'
        if commander else ""
    )
    return (
        '<div class="sand-page">'
        + _top_bar(state, event, viewer_word)
        + (_tabs_nav(tab, commander).replace('<a class="rv-tab" href="/review?tab=common">共性规律</a>','').replace('<a class="rv-tab" href="/review?tab=trend">趋势统计</a>','') if bound else _tabs_nav(tab, commander))
        + export_link
        + msg_html
        + f'<div class="rv-grid"><div class="rv-col-main">{main}</div>{sidebar}</div>'
        + ask_card
        + "</div>"
    )
