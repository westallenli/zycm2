"""Read-only firefighting execution evidence; reports never verify outcomes."""
import json
from dataclasses import asdict, dataclass

from . import tactical_map as tm
from .task_sop import flow_issue, receipt_groups
from .workbench import esc


@dataclass(frozen=True)
class ExecutionTask:
    order_id: str
    facts: tuple[str, ...]
    current: tuple[str, ...] = ()
    historical: tuple[str, ...] = ()
    attention: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionView:
    state: str
    tasks: tuple[ExecutionTask, ...] = ()
    simulated: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


_ATTENTION_KINDS = {'cannot_execute', 'exec_abnormal', 'personnel_safety', 'comm_abnormal',
                    'task_paused', 'cannot_stop', 'personnel_abnormal', 'comm_lost_pending'}
_WITHDRAW_STATES = {'withdraw_noticed', 'awaiting_stop', 'withdraw_running', 'withdraw_done'}


def _receipt_text(receipt):
    return (f'版本 {receipt["task_version"]} · {tm.receipt_word(receipt["kind"])}；'
            f'报告人 {receipt["by"]}；时间 {receipt["at"]}；来源原文 {receipt["source"]}；'
            f'反馈 {receipt.get("content") or "未记录"}；'
            f'报告位置（待核查） {receipt.get("where") or "未记录"}；'
            f'人员情况 {receipt.get("personnel") or "未记录"}；'
            f'风险/困难 {receipt.get("risk") or "未记录"}；支援需求 {receipt.get("need") or "未记录"}。')


def execution_view(workspace, record, *, work_label='组织扑救',ledger=None) -> ExecutionView:
    if record is None:
        return ExecutionView('未进入事件', missing=('请先选择事件任务空间。',))
    if not workspace.event_id or workspace.event_id != record.event_id or workspace.verify_audit_chain():
        return ExecutionView('依据待核对', missing=('任务空间归属或审计链不一致，不能汇总执行依据。',))
    tasks, simulated = [], []
    from .formal_task_reads import cards as formal_cards
    try:formal=formal_cards(workspace,ledger)
    except ValueError:return ExecutionView('依据待核对',missing=('正式发布、回执或核验来源待核对。',))
    missing = [f'任务尚未逐项明确关联{work_label}；下列为本事件任务上下文，不能汇成本项完成率。',
               '原始对讲音频/转写证据、位置核查、困难处置与专业复核尚未完整关联；来源文字不等于证据已核实。',
               '各正式任务依本版指定核验人分别核验；整场目标另见本环节办理与交接。' if formal else '当前尚无完整的指定核验负责人及整体目标核验依据。']
    for order in workspace.orders.values():
        if order.id in formal:
            row=formal[order.id]
            tasks.append(ExecutionTask(order.id,row['facts'],row['current'],missing=row['missing']))
            continue
        facts = [f'任务 {order.id}：{order.scope}。']
        issue = flow_issue(workspace, order.id, record.event_id)
        if issue:
            tasks.append(ExecutionTask(order.id, tuple(facts), missing=(issue,)))
            continue
        flow = workspace.command_flows[order.id]
        current, historical, invalid = receipt_groups(flow)
        gaps, attention, changes = [], [], []
        facts.extend((f'当前内容版本 {flow.content_version}；目标 {flow.target or "未记录"}；执行单位 {flow.unit or "未记录"}。',
                      f'执行阶段：{flow.stage_word}；控制状态：{flow.control_word}。',
                      f'本版开始执行记录：{flow.started_at or "未记录"}。',
                      '阶段与回执仅反映既有记录；报告完成仍需指定负责人核验。'))
        if flow.control_state != tm.CommandControlState.NORMAL.value:
            attention.append(f'本版控制状态记录为“{flow.control_word}”，请核对对应处置依据。')
        # revise() retains old reason fields; only the matching control state makes
        # them current. Never resurrect a previous version's pause or withdrawal.
        if flow.control_state == tm.CommandControlState.PAUSED.value:
            attention.append(f'暂停原因：{flow.pause_reason or "未记录"}；约定重评时间：{flow.pause_reassess_at or "未记录"}。')
        if flow.control_state in _WITHDRAW_STATES:
            attention.append(f'撤回原因：{flow.withdraw_reason or "未记录"}；通知、停止及撤离状态须分别核对。')
        for receipt in current:
            if receipt['kind'] in _ATTENTION_KINDS or receipt.get('risk') or receipt.get('need'):
                attention.append('本版曾报告困难/异常/支援事项，是否解除须核对处置依据：' + _receipt_text(receipt))
        if not current:
            gaps.append('暂无来源完整的当前版执行回执；没有记录不等于现场未行动或已失联。')
        if invalid:
            gaps.append('存在未明确绑定事件、任务、版本或来源不全的回执（可能含旧撤回回执），未纳入本版/历史反馈。')
        for change in flow.content_history:
            version = change.get('task_version', '')
            if (change.get('event_id') != record.event_id or change.get('order_id') != order.id
                    or not isinstance(version, str) or not version.isascii() or not version.isdigit()
                    or len(version) > len(str(flow.content_version)) or str(int(version)) != version
                    or not 1 <= int(version) < flow.content_version
                    or not all(change.get(key) for key in ('revised_by', 'revised_at', 'reason'))):
                gaps.append('有修订历史缺少本事件/任务、版本或操作者依据，未纳入变更记录。')
                continue
            changes.append(f'内容版本 {version} → {int(version) + 1}；记录人 {change["revised_by"]}；'
                           f'时间 {change["revised_at"]}；理由 {change["reason"]}。')
        if flow.closed_at:
            facts.append(f'旧关闭记录：{flow.closed_at}；{tm.close_results().get(flow.close_result, "结果待核对")}；'
                         '不证明新版指定核验授权或整体目标已完成。')
        tasks.append(ExecutionTask(order.id, tuple(facts), tuple(map(_receipt_text, current)),
            tuple(map(_receipt_text, historical)), tuple(attention), tuple(changes), tuple(dict.fromkeys(gaps))))
    attempt_words = {'succeeded': '模拟成功', 'failed': '模拟失败', 'timeout': '模拟超时', 'partial': '模拟部分完成'}
    for report in workspace.reports.values():
        snapshots = [event.payload.get('report') for event in workspace.events
                     if event.kind == 'ExecutionSimulated' and event.payload.get('report', {}).get('id') == report.id]
        if (report.order_id not in workspace.orders or not snapshots
                or json.dumps(snapshots[-1], sort_keys=True) != json.dumps(asdict(report), sort_keys=True)):
            missing.append('有旧执行报告缺少对应任务或模拟审计依据，未纳入展示。')
            continue
        simulated.append(f'{report.id} · 任务 {report.order_id} · {attempt_words.get(report.status, "模拟结果待核对")}；'
                         f'{"；".join(report.observations) or "无观测描述"}；'
                         '仅为旧模拟执行记录，缺少现场报告人/时间/任务内容版本，不计作现场进展或核验依据。')
    if any(key not in workspace.orders for key in workspace.command_flows):
        missing.append('存在无对应任务的执行记录，未纳入视图。')
    if not tasks:
        missing.append('暂无本事件任务依据；不能推定现场没有扑救行动。')
    focus = ('有执行事项待核对' if any(task.attention for task in tasks) else
             '待核对执行依据' if tasks else '待补执行资料')
    return ExecutionView(focus, tuple(tasks), tuple(simulated), tuple(dict.fromkeys(missing)))


def render_execution(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    view = execution_view(state.workspace, registry.events.get(event_id) if registry and event_id else None,ledger=getattr(state,'ledger',None))

    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(row)}</li>' for row in rows) or '<li>暂无记录。</li>') + '</ul>'

    cards = []
    for task in view.tasks:
        sections = ''.join(f'<h5>{label}</h5>{items(rows)}' for label, rows in (
            ('困难、异常与支援事项（须核对处置）', task.attention),
            (f'当前版现场反馈 · {len(task.current)} 条', task.current),
            (f'历史版反馈 · {len(task.historical)} 条（不能替代本版）', task.historical),
            ('任务变更记录', task.changes), ('本任务资料提醒', task.missing)))
        cards.append(f'<details class="sop-execution-task"><summary>{esc(task.order_id)} · 执行与反馈依据</summary>'
                     + items(task.facts) + sections + '</details>')
    return (f'<div class="sop-execution" data-sop-state="{esc(view.state)}">'
        f'<p><b>当前重点：{esc(view.state)}</b> · 每次载入核对当前事件记录</p>'
        '<h4>现在做什么</h4><p>核对当前版任务进展、现场反馈来源、位置、困难支援、异常与变更，跟进处置和核验依据。</p>'
        '<h4>为什么做</h4><p>及时发现执行与部署的差异，让困难和突发险情有据可查；报告进展、专业判断和核验关闭分别处理。</p>'
        '<h4>由谁做</h4><p>执行队长/组长报告进展、困难和人员情况；指定副总指挥组织协调，必要专业意见由相应人员提供，'
        '指定核验负责人核对完成依据。具体责任人及处置结果仍须逐任务核实。</p>'
        '<h4>已有信息与依据</h4>' + (''.join(cards) or '<p>暂无可汇总的执行依据。</p>')
        + '<details class="sop-simulated-execution"><summary>旧模拟执行记录（非现场反馈）</summary>'
        + items(view.simulated) + '</details><h4>还缺什么</h4>' + items(view.missing)
        + '<p class="lz-note">无新回报不自动判断拒绝、失联或安全；普通缺项不阻断其他工作。异常解除及专业安全条件须有对应依据。</p>'
        '<h4>从哪里操作</h4><p>下方“打开信息全景”查看既有命令和事实；现场观测可从“态势研判”的参谋对话入口记录。'
        '本批汇总只读，完整任务回执、对讲证据及异常处置入口仍待后续接入。</p>'
        '<h4>怎样才算完成</h4><p>须核对实际执行、困难/异常处置、必要专业意见及指定负责人核验，并确认整体目标。'
        '自报完成、模拟成功、旧关闭记录或所有子任务自报完成均不能直接判定本项完成。</p></div>')
