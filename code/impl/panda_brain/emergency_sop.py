"""Always-accessible emergency SOP: reported signals and force evidence only."""
from dataclasses import dataclass

from . import tactical_map as tm
from .execution_sop import ExecutionView, execution_view
from .force_needs import SOURCE_LABELS
from .sop import ForceView, force_view
from .task_sop import flow_issue, receipt_groups
from .workbench import esc


@dataclass(frozen=True)
class EmergencyView:
    state: str
    execution: ExecutionView
    force: ForceView
    signals: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


_SIGNAL_KINDS = {'personnel_safety', 'personnel_abnormal', 'comm_abnormal',
                 'comm_lost_pending', 'cannot_stop', 'exec_abnormal', 'cannot_execute'}


def emergency_view(workspace, record, ledger) -> EmergencyView:
    execution = execution_view(workspace, record, work_label='应急救援')
    force = force_view(workspace, record, ledger)
    if execution.state in {'未进入事件', '依据待核对'}:
        return EmergencyView(execution.state, execution, force, missing=execution.missing)
    signals = []
    for order in workspace.orders.values():
        if flow_issue(workspace, order.id, record.event_id):
            continue
        flow = workspace.command_flows[order.id]
        if flow.task_level == tm.TaskLevel.URGENT.value:
            signals.append(f'任务 {order.id} · 当前内容版本 {flow.content_version}：任务等级记录为紧急；'
                           f'执行阶段 {flow.stage_word}。等级记录不证明救援事件已成立或紧急处置仍在进行。')
        if flow.control_state != tm.CommandControlState.NORMAL.value:
            signals.append(f'任务 {order.id} · 当前内容版本 {flow.content_version}：控制状态记录为{flow.control_word}，'
                           '请核对原因和处置依据，不直接判定为救援事件。')
        current, _, _ = receipt_groups(flow)
        for receipt in current:
            if receipt['kind'] in _SIGNAL_KINDS or receipt.get('risk') or receipt.get('need'):
                signals.append(f'任务 {order.id} · 版本 {flow.content_version} 曾报告{tm.receipt_word(receipt["kind"])}；'
                    f'报告人 {receipt["by"]}；时间 {receipt["at"]}；来源原文 {receipt["source"]}；'
                    f'内容 {receipt.get("content") or "未记录"}；风险/困难 {receipt.get("risk") or "未记录"}；'
                    f'支援需求 {receipt.get("need") or "未记录"}。是否解除须核对处置依据。')
    missing = list(execution.missing) + list(force.missing) + [
        '救援对象、具体位置、任务分工、所需装备和专业保障能力尚未完整关联；人数或岗位名称不能证明救援能力适配。',
        '必要安全条件及专业意见须按具体救援任务核对；本页不提供安全放行结论。',
        '通知送达、实际救援、转运/交接和指定负责人核验仍需各自依据。']
    if not signals:
        missing.append('暂无可核对的本版紧急/异常/支援线索；不能据此判断现场无险情或不需要救援。')
    focus = '有应急相关记录待核对' if signals else '待补应急救援依据'
    return EmergencyView(focus, execution, force, tuple(signals), tuple(dict.fromkeys(missing)))


def render_emergency(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    record = registry.events.get(event_id) if registry and event_id else None
    view = emergency_view(state.workspace, record, getattr(state, 'ledger', None))
    person = registry.library.get(view.force.deputy) if registry and view.force.deputy else None
    deputy = person.name if person else ('已登记，姓名待核对' if view.force.deputy else '待登记')

    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(row)}</li>' for row in rows) or '<li>暂无记录。</li>') + '</ul>'

    support = '<p>人员保障依据暂不可用，未知人数不显示为零；已有任务反馈仍可核对。</p>'
    if view.force.needs is not None:
        needs = view.force.needs
        support = items(tuple(f'{row.label}：申报到场 {row.arrived_reported} 人；报告待命可派 {row.standby_reported} 人；'
                              '人数来自报告，任务适配待核对。' for row in needs.rows))
        support += f'<p>人力需求资料状态：{esc(view.force.state)}；需核对来源：{esc("、".join(SOURCE_LABELS[k] for k in needs.changed_sources) or "当前输入未变化")}。</p>'
        if needs.latest_review:
            support += (f'<p>上次人力需求复核：{esc(needs.latest_review["reviewed_at"])}；'
                        f'{esc(needs.latest_review["note"])}；不等于本次救援资源已核定或已调派。</p>')
        support += '<details class="sop-emergency-people"><summary>人员到场与待命报告依据</summary>' + items(view.force.reports) + '</details>'
    tasks = []
    for task in view.execution.tasks:
        sections = ''.join(f'<h5>{label}</h5>{items(rows)}' for label, rows in (
            ('困难/异常与处置提醒', task.attention),
            (f'当前版反馈 · {len(task.current)} 条', task.current),
            (f'历史版反馈 · {len(task.historical)} 条（不能替代本版）', task.historical),
            ('任务变更', task.changes), ('资料提醒', task.missing)))
        tasks.append(f'<details class="sop-emergency-task"><summary>{esc(task.order_id)} · 本事件任务（救援关联待核对）</summary>'
                     + items(task.facts) + sections + '</details>')
    return (f'<div class="sop-emergency" data-sop-state="{esc(view.state)}">'
        '<p><b>随时可进入，不等待其他工作项完成。</b></p>'
        f'<p>当前重点：{esc(view.state)} · 每次载入核对当前事件记录</p>'
        '<h4>现在做什么</h4><p>核对紧急事项、人员安全和通信异常，明确救援对象、位置与支援需求，衔接任务、人员和保障依据。</p>'
        '<h4>为什么做</h4><p>让紧急事项及时进入处置视野，分清现场报告、已确认安排和实际执行；不把到场人数或历史回执当成救援完成。</p>'
        f'<h4>由谁做</h4><p>现场队长/组长报告险情和需求；指定副总指挥：{esc(deputy)}，组织协调与资源安排；'
        '相应专业人员核对必要安全条件，执行方反馈，由指定核验负责人确认结果。通用指挥登录不获得调配权限。</p>'
        '<h4>已有信息与依据</h4><details class="sop-emergency-signals"><summary>本版紧急/异常/支援线索（待核对）</summary>'
        + items(view.signals) + '</details><h5>本事件人员与保障供给</h5>' + support
        + '<p class="lz-note">到场和待命均为申报，未独立核验；医疗/通信岗位分类不等于专业资质或某次救援可用能力。</p>'
        + (''.join(tasks) or '<p>暂无可核对的任务记录。</p>')
        + '<details class="sop-emergency-simulated"><summary>旧模拟执行记录（非现场救援）</summary>'
        + items(view.execution.simulated) + '</details><h4>还缺什么</h4>' + items(view.missing)
        + '<p class="lz-note">没有回报不自动判定失联或安全；资料缺项不锁定其他工作，也不代表必要安全条件已满足。</p>'
        '<h4>从哪里操作</h4><p>下方“打开人员与物资”核对保障；指定副总指挥通过<a href="/login">本人登录入口</a>进入已有选队/编组页面。'
        '任务反馈见组织扑救，现场观测可从态势研判的参谋对话记录。本批汇总只读，专门救援编制、调派与交接入口待后续。</p>'
        '<h4>怎样才算完成</h4><p>须核对救援任务、实际执行、人员情况、异常处置和必要交接，并由指定负责人核验。'
        '已读、通知、到场、自报完成、旧关闭或模拟成功都不能直接判定救援完成。</p></div>')
