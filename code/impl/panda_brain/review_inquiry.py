"""Read-only, single-event review answers with frozen, viewer-scoped citations."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import review_domain as rd, review_meeting as meeting, review_causality as causality
from .review_reports import _json


def viewer_key(state, commander_token, member_token):
    # Match page precedence: the named owner remains the owner with dual cookies.
    member = state.member_session(member_token)
    if member and meeting.is_owner(state, member_token):
        return 'member:' + member.phone
    if state.command_mode(commander_token):
        return 'commander'
    return 'member:' + member.phone if member else ''


def history(state, role, viewer):
    return [(q, a) for q, a in state.review_qa_log
            if a.context_event_id == state.active_event_id
            and a.context_role == role and a.context_subject == viewer]


def answer(state, question, role, viewer='', *, follow_up=False, mask_phone_fn=None):
    state = meeting.scoped_state(state)
    from .review_replay import replay
    replay(state.workspace, state.active_event_id)  # Verify archived indexes against their audit copies.
    if role not in {'commander', 'member', 'owner'}:
        raise PermissionError('当前身份不能复盘问询。')
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError('问题须为 1 至 2000 字的文字或人工校正后的转写文本。')
    question = question.strip()
    privileged = role in {'commander', 'owner'}
    facts, analysis, unknown, records = [], [], [], {}
    focus = []
    inherited_scope = ''
    old = history(state, role, viewer)
    previous = old[-1][1] if follow_up and old else None
    topic, _ = rd._review_route(question)
    if any(word in question for word in ('任务', '命令', '延误', '延期', '调整', '变更', '回执')):
        topic = 'tasks'
    elif any(word in question for word in ('未核实', '待核', '证据', '依据')) and topic is None:
        topic = previous.topic if previous else 'evidence'
    if topic is None and previous:
        topic = previous.topic
    if topic not in {'issues','root','rect','exp','report','common'} and any(word in question for word in ('人员', '到场', '物资', '饮水', '饮用水', '保障', '地图', '态势')):
        topic = 'business_sources'
    requested = set(re.findall(r'(?i)(?:ISSUE-|HYP-|RECT-|EXP-|RPT-|offline-task-|task-|order-)[A-Za-z0-9_.:-]+', question))
    if not requested and previous:
        requested = set(previous.focus_ids)
    if any(
            identifier.lower().startswith(('offline-task-', 'task-', 'order-'))
            for identifier in requested):
        topic = 'tasks'
    if topic is None and requested:
        for name, candidate in (('issues', 'issues'), ('hypotheses', 'root'), ('rects', 'rect'),
                                ('experiences', 'exp'), ('reports', 'report')):
            if requested & set(getattr(state.review_ledger, name)):
                topic = candidate
                break

    def add(key, row, description):
        focus.append(key.split(':', 1)[-1])
        facts.append(description)
        records[key] = asdict(row) if privileged else {'id': key, 'record_summary': description}

    def selected(rows, identifier):
        rows = list(rows)
        if requested:
            rows = [row for row in rows if identifier(row) in requested]
        if len(rows) > 8:
            unknown.append(f'匹配 {len(rows)} 条，当前只展示前 8 条；请按编号缩小范围。')
        return rows[:8]

    def assessment(kind, row):
        rid = row.issue_id if kind == 'issue' else row.hyp_id
        current = causality.current_confirmation(state, kind, rid)
        basis = causality.assessments(state).get(f'{kind}:{rid}')
        data = json.loads(basis.content_json) if basis else {}
        if privileged and basis:
            records['revision:' + basis.id] = asdict(basis)
            refs = list(data.get('support_refs', ())) + list(data.get('counter_refs', ()))
            if data.get('meeting_ref'):
                refs.append(data['meeting_ref'])
            refs.extend(row.evidence_ids)
            for ref in refs:
                evidence = state.workspace.object_revisions.get(ref) or state.workspace.evidence.get(ref)
                if evidence is not None:
                    ref_event = getattr(getattr(evidence, 'reference', None), 'event_id', '') or getattr(evidence, 'event_id', '')
                    if ref_event and ref_event != state.active_event_id:
                        unknown.append(f'{rid} 引用 {ref} 不属于本事件，不读取其内容。')
                    else:
                        records['source:' + ref] = asdict(evidence)
                else:
                    audit = next((event for event in state.workspace.events
                                  if ref in {event.id, f'audit#{event.sequence}'}), None)
                    if audit:
                        records['source:' + ref] = asdict(audit)
                    else:
                        unknown.append(f'{rid} 引用 {ref} 尚无可核对原始记录。')
            for field, label in (('counter_note', '反证核查'), ('alternatives', '其他解释'),
                                 ('meeting_finding', '会议核实记录')):
                if data.get(field):
                    analysis.append(f'{rid} {label}：{data[field]}')
            if data.get('missing_info') and data['missing_info'] not in {'无', '无缺失'}:
                unknown.append(f'{rid} 缺失信息：{data["missing_info"]}')
            if data.get('verify_targets') and not current:
                unknown.append(f'{rid} 待核实对象：{data["verify_targets"]}')
        if not current and (row.review_status == 'confirmed' if kind == 'issue' else row.status == 'verified'):
            unknown.append(f'{rid} 旧确认已过期或没有最新核查依据，不能当作当前已核实结论。')
        return current, data

    if rd.has_command_intent(question):
        topic = 'command_refused'
        analysis.append('复盘问询只查询分析，不下达命令、修改结论或发布整改。')
    elif topic == 'common' or any(word in question for word in ('其他事件', '跨事件')):
        topic = 'common'
        unknown.append('本期只复盘本事件，不读取其他事件作共性、趋势或复现分析。')
    elif topic == 'business_sources' and privileged:
        from .review_sources import records as business_records
        groups = ({'personnel'} if any(w in question for w in ('人员', '到场')) else
                  {'maps', 'situation'} if any(w in question for w in ('地图', '态势')) else {'materials'})
        for row in [r for r in business_records(state) if r['group'] in groups][-8:]:
            ref = row['reference']
            facts.append(f'#{row["sequence"]} {row["label"]}；{row["summary"]}；{row["data_kind"]}')
            records[ref] = row
            focus.append(ref)
        unknown.append('仅按原始业务记录回答；模拟资料不代表真实现场，缺失的发生时间与状态不推断。')
    elif topic == 'business_sources':
        unknown.append('原始业务资料明细仅本事件指挥、指定副总指挥与复盘负责人可查。')
    elif topic in {'issues', 'root', 'evidence'}:
        if topic in {'issues', 'evidence'}:
            rows = list(state.review_ledger.issues.values())
            if not privileged:
                count = len(rows)
                rows = [row for row in rows if row.review_status in rd._MEMBER_VISIBLE_ISSUE_STATUS
                        and (row.review_status != 'confirmed' or causality.current_confirmation(state, 'issue', row.issue_id))]
                if count > len(rows):
                    unknown.append(f'{count-len(rows)} 条未核实或不可见问题未纳入当前账号回答。')
            for row in selected(rows, lambda r: r.issue_id):
                current, data = assessment('issue', row)
                status = row.status_word() if current or row.review_status != 'confirmed' else '旧确认待重新核实'
                add('issue:' + row.issue_id, row, f'{row.issue_id}：{row.title}；{status}；记录：{row.description}')
                if row.to_verify and not current:
                    unknown.append(f'{row.issue_id} 待核验：{row.to_verify}')
        if topic in {'root', 'evidence'}:
            rows = state.review_ledger.hypotheses.values()
            if not privileged:
                rows = [row for row in rows if causality.current_confirmation(state, 'hypothesis', row.hyp_id)]
            for row in selected(rows, lambda r: r.hyp_id):
                current, data = assessment('hypothesis', row)
                label = ({'direct': '负责人已核实直接原因', 'deep': '负责人已核实深层原因'}.get(data.get('causal_layer'), '负责人已核实')
                         if current else '原因线索待核实，不作因果认定')
                add('hyp:' + row.hyp_id, row, f'{row.hyp_id}：{row.statement}；{label}')
                if not current:
                    unknown.extend(f'{row.hyp_id} 待验证：{value}' for value in row.unresolved_verifications())
        analysis.append('这里只转述现存问题、核查与会议记录；系统不自动定责。')
    elif topic == 'tasks':
        from .offline_tasks import task_state
        tasks = task_state(state.workspace, state.active_event_id).values()
        actor = viewer.removeprefix('member:')
        if not privileged:
            tasks = [row for row in tasks if actor in {row.recipient_id, row.verifier_id}]
        for row in selected(tasks, lambda r: r.id):
            add('task:' + row.id, row, f'{row.id} v{row.version}：{row.content}；状态 {row.state}；'
                f'实际下达 {row.occurred_at}；录入 {row.recorded_at}；明确截止 {row.due_at or "未记录"}')
            for item in row.history[-8:]:
                basis = item.get('reason') or item.get('basis') or item.get('evidence')
                if basis:
                    analysis.append(f'{row.id} v{item.get("version", "未记录")} 具名报告/调整依据：{basis}；'
                                    f'发生 {item.get("occurred_at") or "未记录"}，录入 {item.get("recorded_at") or "未记录"}')
            if not row.due_at:
                unknown.append(f'{row.id} 无明确截止时刻，不能计算或认定延误。')
            if row.state != 'closed':
                unknown.append(f'{row.id} 尚未独立核验关闭；完成报告不等于核验完成。')
        if privileged:
            orders = [row for row in state.workspace.orders.values()
                      if not getattr(row, 'event_id', '') or row.event_id == state.active_event_id]
            for row in selected(orders, lambda r: r.id):
                add('order:' + row.id, row, f'{row.id}：执行命令状态 {getattr(row.status, "value", row.status)}')
                decision_id = getattr(row, 'decision_id', '')
                decision = state.workspace.decisions.get(decision_id)
                if decision:
                    records['decision:' + decision_id] = asdict(decision)
                    analysis.append(f'{row.id} 调整/决断仅按明确关联的 {decision_id} 记录核对，参见来源。')
                reports = [item for item in state.workspace.reports.values() if item.order_id == row.id]
                for item in reports[:8]:
                    records['execution-report:' + item.id] = asdict(item)
                    analysis.append(f'{row.id} 执行反馈 {item.id}：{getattr(item.status, "value", item.status)}；'
                                    + '；'.join(item.observations) + '；执行反馈不替代独立核验。')
                if len(reports) > 8:
                    unknown.append(f'{row.id} 另有执行反馈，请按命令来源记录核对完整历史。')
        analysis.append('状态、时差和困难报告不能单独证明延误原因；无明确关联依据不推断因果。')
    elif topic in {'rect', 'exp', 'report'}:
        mapping, field = {'rect': ('rects', 'rect_id'), 'exp': ('experiences', 'exp_id'), 'report': ('reports', 'report_id')}[topic]
        rows = list(getattr(state.review_ledger, mapping).values())
        if not privileged:
            rows = [row for row in rows if (bool(row.published_by) if topic == 'rect' else
                    row.status == 'confirmed' if topic == 'exp' else row.status == 'published')]
        for row in selected(rows, lambda r: getattr(r, field)):
            identifier = getattr(row, field)
            text = (f'{row.measures}；验收标准 {row.acceptance_standard or "未记录"}' if topic == 'rect'
                    else f'{row.content}；条件 {row.applicability}；限制 {row.limitations or "未记录"}' if topic == 'exp'
                    else f'v{row.version}；生成 {row.created_at}；审核 {row.reviewed_by or "未发布"}')
            add(topic + ':' + identifier, row, f'{identifier}：{row.status_word()}；{text}')
            if topic == 'rect' and row.status not in {'done', 'closed'}:
                unknown.append(f'{identifier} 仍开放；报告发布不替代整改验收。')
    elif topic == 'timeline' and privileged:
        for row in state.workspace.events[-8:]:
            add('audit:' + row.id, row, f'审计 #{row.sequence}：{row.kind}；录入 {row.recorded_at or "未记录"}')
        unknown.append('录入时刻不等于现场发生时刻，缺失时间不补造。')
    elif topic == 'timeline':
        unknown.append('完整审计时间线仅指挥员或复盘负责人可查阅；请按本人可见任务编号查询。')
    elif not requested:
        base = rd.answer_review_inquiry(question, ledger=state.review_ledger, workspace=state.workspace,
            event_id=state.active_event_id, role=role, materials_ledger=state.ledger, mask_phone_fn=mask_phone_fn)
        topic = base.topic
        inherited_scope = base.scope
        facts.append(base.conclusion.replace('共性对比、', '').replace('共性对比，', ''))
        records['answer:local'] = {'conclusion': base.conclusion, 'references': base.evidence,
                                  'scope': base.scope, 'excluded': base.excluded}
        unknown.append(base.excluded or '回答限本次读取的可见资料；无证据不补造事实。')
    if not facts and not analysis:
        unknown.append('本事件当前可见资料没有匹配记录；不代表现场未发生。')
    if requested and not focus:
        facts.clear()
        records.clear()
        unknown.append('本事件当前可见资料未找到指定编号，不借用其他记录回答。')
    if not privileged:
        # Original source records are restricted; member citations contain only visible summaries.
        mask = mask_phone_fn or (lambda value: value[:3] + '****' + value[-4:])
        def masked(value):
            return re.sub(r'(?<!\d)1\d{10}(?!\d)', lambda match: mask(match[0]), value)
        facts, analysis, unknown = ([masked(value) for value in values] for values in (facts, analysis, unknown))
        records = json.loads(masked(_json(records)))
    stamp = datetime.now(timezone.utc).isoformat()
    snapshot = _json(dict(schema='review-answer-source-v1', event_id=state.active_event_id,
                         role=role, workspace_version=state.workspace.version, read_at=stamp, records=records))
    return rd.ReviewAnswer(topic or 'none', '；'.join(facts[:4]) or '；'.join(analysis + unknown),
        f'本事件 {state.active_event_id}，读取工作区 v{state.workspace.version}，{stamp}；{inherited_scope}',
        (state.active_event_id,), tuple(records), '引用本次读取的记录；追问重新读取当前资料，旧回答来源保持原样。',
        '；'.join(dict.fromkeys(unknown)), '来源记录可核对，分析与待核项分列',
        context_event_id=state.active_event_id, context_role=role, context_subject=viewer,
        facts=tuple(facts), analysis=tuple(analysis), unverified=tuple(dict.fromkeys(unknown)),
        focus_ids=tuple(dict.fromkeys(focus)), source_snapshot_json=snapshot,
        source_digest=hashlib.sha256(snapshot.encode()).hexdigest(), answered_at=stamp)


def source_page(state, role, viewer, digest, ref):
    from .workbench import esc
    matches = [a for _, a in history(state, role, viewer) if a.source_digest == digest]
    if not matches:
        raise ValueError('回答来源不在本事件当前账号的问询记录中。')
    answer = matches[-1]
    if hashlib.sha256(answer.source_snapshot_json.encode()).hexdigest() != digest:
        raise ValueError('回答来源校验失败。')
    data = json.loads(answer.source_snapshot_json)
    if ref not in data['records']:
        raise ValueError('所选回答未引用此记录。')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>破局智援参谋 问询来源</title><style>body{margin:20px;overflow-wrap:anywhere}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>本次回答采用的来源</h1>'
            f'<p>事件 {esc(data["event_id"])}；工作区 v{data["workspace_version"]}；读取 {esc(data["read_at"])}</p>'
            f'<p>{esc(ref)}；指纹 {esc(digest)}</p><pre>'
            + esc(json.dumps(data['records'][ref], ensure_ascii=False, indent=2)) + '</pre></html>')


def source_links(answer):
    from .workbench import esc
    return ' · '.join('<a href="/review/inquiry-source?' + esc(urlencode(dict(
        event_id=answer.context_event_id, digest=answer.source_digest, ref=ref))) + '">' + esc(ref) + '</a>'
        for ref in answer.evidence)
