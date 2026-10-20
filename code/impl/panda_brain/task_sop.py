"""Read-only task deployment SOP; legacy status is not V2 acceptance."""
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from . import tactical_map as tm
from .authorization import Role
from .event_authorization import event_grants
from .task_drafts import COMMON_LABELS, TASK_LABELS
from . import task_review
from .task_recipients import recipient_route
from .workbench import esc


@dataclass(frozen=True)
class TaskCard:
    order_id: str
    facts: tuple[str, ...]
    current_receipts: tuple[str, ...] = ()
    historical_receipts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentView:
    state: str
    tasks: tuple[TaskCard, ...] = ()
    missing: tuple[str, ...] = ()


_ORDER_WORDS = dict(draft='草稿', pending_confirm='待确认', confirmed='已确认',
                    active='执行中', paused='已暂停', completed='已完成', failed='失败',
                    cancelled='已取消', expired='已过期', superseded='已替代')


def _audited(workspace, flow):
    records = [e.payload.get('flow') for e in workspace.events
               if e.kind in {'CommandFlowOpened', 'CommandFlowChanged'}
               and e.payload.get('order_id') == flow.order_id
               and e.payload.get('event_id') == flow.event_id]
    # JSON normalizes tuples/lists across persistence round trips.
    return bool(records) and json.dumps(records[-1], sort_keys=True) == json.dumps(asdict(flow), sort_keys=True)


def flow_issue(workspace, order_id, event_id):
    """Shared provenance boundary for readonly task SOP views."""
    flow = workspace.command_flows.get(order_id)
    if flow is None:
        return '尚无任务执行层内容、版本和接收依据。'
    if flow.order_id != order_id or flow.event_id != event_id:
        return '执行记录未明确绑定本事件及本任务，未纳入内容和回执汇总。'
    if type(flow.content_version) is not int or flow.content_version < 1:
        return '旧执行记录缺少明确内容版本，不能推定当前版接收或回执。'
    if not _audited(workspace, flow):
        return '任务内容与最新版本审计记录不一致，不能确认为当前版依据。'
    return ''


def receipt_groups(flow):
    """Partition a trusted flow's receipts; never infer a missing version."""
    current, historical, invalid = [], [], 0
    for receipt in flow.receipts:
        version = receipt.get('task_version', '')
        valid = (receipt.get('event_id') == flow.event_id and receipt.get('order_id') == flow.order_id
                 and isinstance(version, str) and version.isascii() and version.isdigit()
                 and len(version) <= len(str(flow.content_version))
                 and str(int(version)) == version and 1 <= int(version) <= flow.content_version
                 and receipt.get('source') and receipt.get('by') and receipt.get('at')
                 and receipt.get('kind') in {kind.value for kind in tm.ReceiptType})
        if not valid:
            invalid += 1
            continue
        (current if int(version) == flow.content_version else historical).append(receipt)
    return tuple(current), tuple(historical), invalid


def deployment_view(workspace, record, ledger=None) -> DeploymentView:
    if record is None:
        return DeploymentView('未进入事件', missing=('请先选择事件任务空间。',))
    if not workspace.event_id or workspace.event_id != record.event_id or workspace.verify_audit_chain():
        return DeploymentView('依据待核对', missing=('任务空间归属或审计链不一致，不能汇总部署依据。',))
    cards = []
    from .formal_task_reads import cards as formal_cards
    try:formal=formal_cards(workspace,ledger)
    except ValueError:return DeploymentView('依据待核对',missing=('正式发布、回执或核验来源待核对。',))
    for order in workspace.orders.values():
        if order.id in formal:
            row=formal[order.id]
            cards.append(TaskCard(order.id,row['facts'],row['current'],missing=row['missing']))
            continue
        facts = [f'任务 {order.id}：{order.scope}；旧命令状态：{_ORDER_WORDS.get(order.status, "状态待核对")}。',
                 '旧状态仅为既有记录，不证明新版任务部署或核验闭环已完成。']
        gaps, current, historical = [], [], []
        flow = workspace.command_flows.get(order.id)
        issue = flow_issue(workspace, order.id, record.event_id)
        if issue:
            gaps.append(issue)
        else:
            facts.extend((f'当前内容版本 {flow.content_version}；执行阶段：{flow.stage_word}；控制状态：{flow.control_word}。',
                          f'目标：{flow.target or "未记录"}；执行单位：{flow.unit or "未记录"}。',
                          f'开始要求：{flow.start_time or "未记录"}；截止要求：{flow.deadline or "未记录"}。',
                          f'安全事项：{flow.safety_cond or "未记录"}；停止条件：{flow.stop_cond or "未记录"}。',
                          f'反馈要求：{flow.receipt_need or "未记录"}。',
                          f'送达记录：{flow.delivered_at or "未记录"}；已阅记录：{flow.viewed_at or "未记录"}；均不等于接收。'))
            if flow.route_id:
                mark = workspace.annotations.get(flow.route_id)
                facts.append(f'路线引用：{flow.route_id}；'
                             + ('本事件有对应标绘，适用性仍须核对。' if mark and mark.event_id == record.event_id
                                else '本事件未找到对应标绘，请核对引用。'))
            else:
                facts.append('未引用路线；非空间任务不强制填写地图位置。')
            if flow.acked_at and flow.acked_by and flow.ack_result:
                facts.append(f'当前版接收记录：{tm.receive_word(flow.ack_result)}；{flow.acked_by}；{flow.acked_at}。')
            else:
                gaps.append('当前内容版本暂无完整接收记录；送达、已阅和历史版回执不能替代。')
            for field, label in (('target', '目标'), ('unit', '执行单位'),
                                 ('safety_cond', '安全事项'), ('receipt_need', '反馈要求')):
                if not getattr(flow, field).strip():
                    gaps.append(f'本版未记录{label}。')
            if not flow.start_time and not flow.deadline:
                gaps.append('本版未记录时间要求。')
            current_records, historical_records, invalid = receipt_groups(flow)
            if invalid:
                gaps.append('存在事件、任务、版本或来源信息不完整的回执，未纳入本版/历史版依据。')
            for records, output in ((current_records, current), (historical_records, historical)):
                for receipt in records:
                    output.append(f'内容版本 {receipt["task_version"]} · {tm.receipt_word(receipt["kind"])}；'
                                  f'{receipt["by"]}；{receipt["at"]}；来源 {receipt["source"]}；{receipt.get("content", "")}')
            if flow.closed_at:
                facts.append(f'既有关闭记录：{flow.closed_at}；'
                             f'{tm.close_results().get(flow.close_result, "结果待核对")}；'
                             '尚不能证明已由本任务指定核验负责人确认。')
        cards.append(TaskCard(order.id, tuple(facts), tuple(current), tuple(historical), tuple(dict.fromkeys(gaps))))
    missing = ['尚未正式发布的旧命令不作为本版任务部署完成依据。' if formal else '主责/关联子任务、独立完成标准、专业意见/集体研究和指定核验负责人尚未完整关联。',
               '审批链依任务类别而定；既有审批、执行条件确认或自报完成不自动补成专业复核。']
    if any(key not in workspace.orders for key in workspace.command_flows):
        missing.append('存在无对应任务的执行记录，未纳入部署视图。')
    if not cards:
        missing.append('暂无本事件任务记录；方案采纳不代表任务已部署或被接收。')
    return DeploymentView('待补任务资料' if not cards else '待核对部署依据', tuple(cards), tuple(missing))


def _draft_content_fields(prefix, content, *, task=None):
    rows = []
    for name, label in COMMON_LABELS.items():
        value = getattr(content, name)
        field = (f'<label class="task-draft-field"><span>{label}</span>'
                 f'<input class="lz-input" name="{esc(prefix)}{name}" '
                 f'value="{esc(value)}" aria-label="{label}"></label>')
        if task is not None:
            checked = ' checked' if name in task.spec.common_fields else ''
            field = (f'<div class="task-draft-field-group">{field}'
                     f'<label class="task-draft-ref"><input type="checkbox" '
                     f'name="ref__{esc(task.id)}__{name}" value="1"{checked}> '
                     f'引用共同内容</label></div>')
        rows.append(field)
    return ''.join(rows)


def release_preflight(bundle, grants, reports=None):
    """Describe known blockers; an unreviewed draft can never be publishable."""
    issues = []
    if bundle.status != 'draft' or not bundle.tasks:
        issues.append('当前对象不是可核对的任务草案。')
    deputies = [grant.subject_id for grant in grants
                if grant.role == Role.DEPUTY and grant.event_id == bundle.event_id]
    if len(deputies) != 1:
        issues.append('本事件尚无可核对的唯一指定副总指挥。')
    for index, task in enumerate(bundle.tasks):
        label = '总任务' if index == 0 else f'子任务 {index}'
        for name in ('title', 'objective', 'completion_standard',
                     'responsible_group_id', 'verifier_id'):
            if not getattr(task.spec, name).strip():
                issues.append(f'{label}：{TASK_LABELS[name]}未填写。')
        content = bundle.resolved_content(task.id)
        for name in ('time_requirement', 'feedback_requirement'):
            if not getattr(content, name).strip():
                issues.append(f'{label}：{COMMON_LABELS[name]}未填写。')
    reports = reports or {}
    for index, task in enumerate(bundle.tasks):
        label = '总任务' if index == 0 else f'子任务 {index}'
        record, current = reports.get(task.id, (None, False))
        if not current:
            issues.append(f'{label}：当前版任务分类依据未记录。')
        else:
            category = json.loads(record.content_json)['category']
            issues.append(f'{label}：已报告{task_review.CATEGORIES[category]}，分类依据尚待专业规则核对。')
            if category == 'professional':
                issues.append(f'{label}：需专业意见，尚无经授权人员的当前版签审依据。')
            elif category == 'major':
                issues.append(f'{label}：重大会商，尚无当前版集体研究及专业签审依据。')
            elif category == 'undetermined':
                issues.append(f'{label}：分类未确定，不能进入发布。')
    issues.append('必要安全门禁清单尚未专业确认，不能核定发布条件。')
    return tuple(issues)


def checked_release_preflight(workspace, bundle, *, registry=None, ledger=None):
    """Reconcile event, draft and audit evidence before showing release blockers."""
    if workspace.event_id != bundle.event_id or workspace.verify_audit_chain():
        raise ValueError('任务草案与事件审计链不一致。')
    current = workspace.task_drafts_for_decision(bundle.decision_id, event_id=bundle.event_id)
    if current != bundle:
        raise ValueError('任务草案已变化，请重新核对。')
    if registry is not None and ledger is not None and any(
            r.reference.object_kind == 'task_release_evidence' for r in workspace.object_revisions.values()):
        from .task_release import preflight
        return preflight(workspace, bundle, registry=registry, ledger=ledger)
    grants = event_grants(workspace)
    reports = {task.id: task_review.assessment_for(workspace, bundle, task.id)
               for task in bundle.tasks}
    issues = list(release_preflight(bundle, grants, reports))
    if ledger is not None:
        from .material_supply_drafts import preflight
        issues.extend(preflight(workspace, ledger, bundle))
    elif any(r.reference.object_kind == 'material_supply_draft'
             for r in workspace.object_revisions.values()):
        issues.append('补给草案来源底账未载入，冻结联系及人工数量待核对。')
    for index, task in enumerate(bundle.tasks):
        label = '总任务' if index == 0 else f'子任务 {index}'
        if registry is None or ledger is None:
            issues.append(f'{label}：当前执行组接收人尚未核对。')
            continue
        route = recipient_route(workspace, bundle.event_id,
                                task.spec.responsible_group_id,
                                registry=registry, ledger=ledger)
        if route.issue:
            issues.append(f'{label}：{route.issue}')
        else:
            issues.append(f'{label}：拟接收人 {route.leader_id}，仅路由预检，未投递。')
    return tuple(issues)


def _render_draft(bundle, issues, state=None):
    source = json.loads(bundle.source_option_json)
    shared = _draft_content_fields('common__', bundle.common)
    groups = leaders = []
    if state is not None:
        try:
            from .group_choices import groups_with_people, leader_choices
            groups = groups_with_people(state, bundle.event_id)
            leaders = leader_choices(state)
        except Exception:  # noqa: BLE001 — 取不到就退回手填，不阻断编辑
            groups = leaders = []
    task_fields = []
    gaps = []
    for index, task in enumerate(bundle.tasks):
        spec = task.spec
        role_word = '总任务' if index == 0 else f'关联子任务 {index}'
        for name in ('objective', 'completion_standard', 'responsible_group_id', 'verifier_id'):
            if not getattr(spec, name).strip():
                gaps.append(f'{role_word}：{TASK_LABELS[name]}待填写')
        def _field(name, label):
            value = getattr(spec, name)
            field_name = f'task__{esc(task.id)}__{name}'
            # 主责组只列有人的组；核验负责人只列名册里的队长/组长。
            options = groups if name == 'responsible_group_id' else leaders if name == 'verifier_id' else []
            if options:
                opts = (''.join(f'<option value="{esc(key)}"'
                                + (' selected' if key == value else '') + f'>{esc(text)}</option>'
                                for key, text in options))
                if not value:
                    opts = '<option value="">请选择</option>' + opts
                return (f'<label class="task-draft-field"><span>{label}</span>'
                        f'<select class="lz-select" name="{field_name}" aria-label="{role_word}{label}">'
                        f'{opts}</select></label>')
            return (f'<label class="task-draft-field"><span>{label}</span>'
                    f'<input class="lz-input" name="{field_name}" '
                    f'value="{esc(value)}" aria-label="{role_word}{label}"></label>')

        text_fields = ''.join(_field(name, label) for name, label in TASK_LABELS.items())
        remove = (f'<button class="lz-btn lz-btn2" type="submit" name="remove_child" '
                  f'value="{esc(task.id)}">移除子任务</button>' if index else '')
        task_fields.append(
            f'<fieldset class="task-draft-task"><legend>{role_word}</legend>'
            f'<p class="lz-note">{esc(task.id)} · '
            + ('主责与整体目标须单独核对。' if index == 0 else '仅独立执行并反馈的协作工作保留为子任务。')
            + '</p><p><a href="/supply-draft?'
            + esc(urlencode(dict(
                event_id=bundle.event_id, decision_id=bundle.decision_id, task_id=task.id)))
            + '">编制本任务补给附件</a></p><div class="task-draft-grid">' + text_fields
            + _draft_content_fields(f'task__{task.id}__', spec.content, task=task)
            + '</div>' + remove + '</fieldset>')
    evidence_ids = source.get('evidence_ids') or []
    source_text = (f'采纳决断 {bundle.decision_id}；方案 {bundle.option_id}；'
                   f'来源证据引用 {len(evidence_ids)} 项；录入 {bundle.recorded_at}。')
    estimate = source.get('time_estimate')
    source_detail = (f'方案摘要：{source.get("summary") or "未记录"}；'
                     f'风险 {len(source.get("risks") or [])} 项；'
                     f'估时 {estimate if estimate not in (None, "", "unspecified") else "未记录"}（非下达时间）。')
    def source_items(values):
        return '<ul>' + (''.join(f'<li>{esc(value)}</li>' for value in values)
                       or '<li>未记录</li>') + '</ul>'
    source_lists = (
        '<details class="task-draft-source"><summary>查看原方案依据与风险</summary>'
        '<h5>证据引用</h5>' + source_items(evidence_ids)
        + '<h5>已知风险</h5>' + source_items(source.get('risks') or [])
        + '<h5>禁行条件</h5>' + source_items(source.get('no_go_conditions') or [])
        + '</details>')
    missing = ''.join(f'<li>{esc(item)}</li>' for item in gaps)
    preflight = ''.join(f'<li>{esc(item)}</li>' for item in issues)
    return (
        f'<details class="task-draft-bundle" id="{esc(bundle.id)}">'
        f'<summary>草案 v{bundle.version} · {esc(source.get("title") or bundle.option_id)}'
        f' · 总任务 + {len(bundle.tasks) - 1} 项子任务</summary>'
        f'<p class="lz-note">{esc(source_text)} {esc(source_detail)}</p>'
        '<p class="lz-note">草案未复核、未发布；来源证据引用和推演图形不等于现场核实。</p>'
        + source_lists
        + (f'<ul class="task-draft-gaps">{missing}</ul>' if missing else
           '<p class="lz-note">关键编制字段已填，仍须核对来源与专业复核。</p>')
        + ('<h5>发布依据预检 · 当前不可发布</h5>' if issues else
           '<h5>发布依据预检 · 本版依据齐备，须具名副指挥长确认发布</h5>') + '<ul class="task-draft-gaps">'
        + preflight + '</ul>'
        + '<form method="post" action="/sand/task-draft" class="task-draft-form">'
        f'<input type="hidden" name="decision_id" value="{esc(bundle.decision_id)}">'
        f'<input type="hidden" name="expected_version" value="{bundle.version}">'
        '<h5>共同内容</h5><div class="task-draft-grid">' + shared + '</div>'
        + ''.join(task_fields)
        + '<div class="task-draft-actions">'
        '<button class="lz-btn lz-btn2" type="submit" name="action" value="add">新增子任务</button>'
        '<button class="lz-btn" type="submit" name="action" value="save">保存草案</button>'
        '</div></form></details>')


def render_deployment(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    view = deployment_view(state.workspace, registry.events.get(event_id) if registry and event_id else None,getattr(state,'ledger',None))
    draft_html = ''
    if registry and event_id in registry.events and state.workspace.event_id == event_id and not state.workspace.verify_audit_chain():
        try:
            bundles = [state.workspace.task_drafts_for_decision(bundle.decision_id, event_id=event_id)
                       for bundle in state.workspace.task_drafts.values()]
            draft_html = ''.join(
                _render_draft(bundle, checked_release_preflight(
                    state.workspace, bundle, registry=registry, ledger=state.ledger), state)
                for bundle in bundles if bundle is not None)
        except ValueError:
            draft_html = '<p class="lz-note">草案依据待核对，暂不展示或编辑。</p>'
    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(x)}</li>' for x in rows) or '<li>暂无记录。</li>') + '</ul>'
    cards = ''.join(
        f'<details class="sop-task"><summary>{esc(task.order_id)} · 任务内容与版本依据</summary>'
        + items(task.facts)
        + f'<h5>当前版执行回执 · {len(task.current_receipts)} 条</h5>' + items(task.current_receipts)
        + f'<h5>历史版执行回执 · {len(task.historical_receipts)} 条（不能替代本版）</h5>' + items(task.historical_receipts)
        + '<h5>本任务资料提醒</h5>' + items(task.missing) + '</details>' for task in view.tasks)
    return (
        f'<div class="sop-deployment" data-sop-state="{esc("草案待编制" if draft_html and view.state == "待补任务资料" else view.state)}">'
        f'<p><b>当前重点：{esc("草案待编制" if draft_html and view.state == "待补任务资料" else view.state)}</b> · 每次载入核对当前事件记录</p>'
        '<h4>现在做什么</h4><p>核对任务目标、分工、地点时间、安全保障、反馈要求和内容版本，检查对应接收及复核依据。</p>'
        '<h4>为什么做</h4><p>让执行方收到正确版本，区分方案决策、任务下达、送达、接收与执行反馈。</p>'
        '<h4>由谁做</h4><p>已批准方案内常规任务由指定副总指挥审定发布；需重新专业判断的先取得专业意见，重大决策按集体研究要求处理。'
        '执行方接收和报告，指定核验负责人确认关闭；具体人员及审批依据需逐任务核对。</p>'
        '<h4>方案任务草案</h4>' + (draft_html or '<p>暂无本事件可核对的采纳草案。</p>')
        + '<h4>既有执行记录</h4>' + (cards or '<p>暂无可汇总的执行依据。</p>')
        + '<h4>还缺什么</h4>' + items(view.missing)
        + '<p class="lz-note">资料提醒不阻断其他工作；精确专业门禁待审，不把所有任务套进同一审批链。</p>'
        '<h4>从哪里操作</h4><p>在上方采纳草案中集中编制总/子任务；“打开方案与行动”查看原方案与决断。草案保存不发布。'
        '<a href="/tasks/release">具名复核与正式发布</a>、<a href="/tasks/formal">正确接收人回执</a>须本人身份；核验关闭另查本版依据。</p>'
        '<h4>怎样才算完成</h4><p>须核对部署内容、必要复核、正确执行方对当前版的接收以及相应依据，由人确认。'
        '采纳、送达、已阅、自报完成或旧关闭状态均不能直接判定本项完成。</p></div>'
    )
