"""Cleanup/watch SOP: audited map context and versioned reports, no completion inference."""
from dataclasses import asdict, dataclass

from . import tactical_map as tm
from .execution_sop import ExecutionView, execution_view
from .map_annotations import AnnotationKind, AnnotationStatus, status_word
from .map_history import latest_annotation_reference, resolve_annotation_reference
from .workbench import esc


@dataclass(frozen=True)
class CleanupView:
    state: str
    execution: ExecutionView
    marks: tuple[tuple[str, ...], ...] = ()
    withdrawn: tuple[tuple[str, ...], ...] = ()
    missing: tuple[str, ...] = ()


_MAP_KINDS = {AnnotationKind.HOTSPOT, AnnotationKind.FIRE_POINT,
              AnnotationKind.BURNED_AREA, AnnotationKind.LOOKOUT_POST}
_DETAILS = ('found_at', 'hot_status', 'burned_at', 'recheck_status', 'refire_risk',
            'unit', 'person_or_unit', 'open_status', 'last_report')


def cleanup_view(workspace, record) -> CleanupView:
    execution = execution_view(workspace, record, work_label='清理/看守')
    if execution.state in {'未进入事件', '依据待核对'}:
        return CleanupView(execution.state, execution, missing=execution.missing)
    marks, withdrawn = [], []
    missing = list(execution.missing) + [
        '清理范围、看守分工/交接、复查要求及指定核验负责人尚未完整关联。',
        '暂无结构化的复燃发现—处置—复查闭环；热点、过火区或复燃风险文字不证明复燃已发生或已解除。']
    for key, mark in workspace.annotations.items():
        if mark.kind not in _MAP_KINDS:
            continue
        if mark.event_id != record.event_id or mark.id != key:
            missing.append('有热点/过火区/观察哨归属不一致，未纳入本事件依据。')
            continue
        try:
            reference = latest_annotation_reference(workspace, event_id=record.event_id, annotation_id=mark.id)
            snapshot = resolve_annotation_reference(workspace, reference) if reference else None
            matched = snapshot is not None and asdict(snapshot) == asdict(mark)
        except (ValueError, KeyError, TypeError):
            matched = False
        if not matched:
            missing.append(f'标绘 {mark.id} 缺少一致的当前历史/审计依据，未纳入清理看守资料。')
            continue
        demo = '模拟标绘 · ' if mark.is_demo or mark.demo_notice else ''
        rows = [f'{mark.id} · {demo}{tm.kind_word(mark.kind.value)} · {mark.title}；{status_word(mark.status)}。',
                f'标绘内容版本 {mark.version}；历史记录序号 {reference.version}；记录更新时间 {mark.updated_at or mark.created_at or "未记录"}。',
                f'来源引用原文：{"、".join(mark.source_ref) or "未记录"}；依据：{"、".join(mark.basis) or "未记录"}。']
        rows.extend(f'{tm.detail_word(field)}：{tm.detail_display(field, mark.details[field])}。'
                    for field in _DETAILS if mark.details.get(field))
        if mark.note:
            rows.append(f'记录备注：{mark.note}。')
        rows.append('已记录地理位置，现场适用性仍需核对。' if mark.geo_points else '仅有示意位置，不能作为现场定位依据。')
        if mark.related_order_id:
            rows.append(f'任务引用 {mark.related_order_id}：' + (
                '本事件存在对应任务，但引用未带任务内容版本，不能替代当前版回执。'
                if mark.related_order_id in workspace.orders else '本事件未找到对应任务，请核对引用。'))
        else:
            rows.append('尚未明确关联清理/看守任务。')
        verification = mark.verification_record
        if verification:
            rows.append(f'标绘核验记录：{verification.get("by") or "核验人未记录"}；'
                        f'{verification.get("at") or "时间未记录"}；{verification.get("conclusion") or "结论未记录"}；'
                        '仅为标绘核验，不代表清理/看守任务核验。')
        if mark.status == AnnotationStatus.WITHDRAWN:
            rows.append(f'已撤回，保留历史参考；撤回时间 {mark.withdrawn_at or "未记录"}；'
                        f'原因 {mark.withdraw_reason or "未记录"}；撤回不代表风险已消除。')
            withdrawn.append(tuple(rows))
        else:
            marks.append(tuple(rows))
            if mark.status in {AnnotationStatus.DRAFT, AnnotationStatus.PENDING_REVIEW}:
                missing.append(f'标绘 {mark.id} 尚未核验/发布；不能视为已确认现场结论。')
            if not mark.source_ref:
                missing.append(f'标绘 {mark.id} 缺少来源引用。')
    if not marks:
        missing.append('暂无可核对的当前热点/过火区/观察哨记录；不等于无残火、无复燃风险或可撤离。')
    focus = '有清理看守资料待核对' if marks or execution.tasks else '待补清理看守资料'
    return CleanupView(focus, execution, tuple(marks), tuple(withdrawn), tuple(dict.fromkeys(missing)))


def render_cleanup(state, role: str) -> str:
    if role != 'commander':
        return ''
    registry = getattr(state, 'registry', None)
    event_id = getattr(state, 'active_event_id', '')
    view = cleanup_view(state.workspace, registry.events.get(event_id) if registry and event_id else None)

    def items(rows):
        return '<ul>' + (''.join(f'<li>{esc(row)}</li>' for row in rows) or '<li>暂无记录。</li>') + '</ul>'

    maps = ''.join(f'<details class="sop-cleanup-mark"><summary>{esc(rows[0])}</summary>{items(rows[1:])}</details>'
                   for rows in view.marks)
    archived = ''.join(items(rows) for rows in view.withdrawn) or '<p>暂无记录。</p>'
    tasks = []
    for task in view.execution.tasks:
        sections = ''.join(f'<h5>{label}</h5>{items(rows)}' for label, rows in (
            ('需核对的困难/异常/支援', task.attention),
            (f'当前版反馈 · {len(task.current)} 条', task.current),
            (f'历史版反馈 · {len(task.historical)} 条（不能替代本版）', task.historical),
            ('任务变更', task.changes), ('资料提醒', task.missing)))
        tasks.append(f'<details class="sop-cleanup-task"><summary>{esc(task.order_id)} · 本事件任务（分类待核对）</summary>'
                     + items(task.facts) + sections + '</details>')
    return (f'<div class="sop-cleanup" data-sop-state="{esc(view.state)}">'
        f'<p><b>当前重点：{esc(view.state)}</b> · 每次载入核对当前事件记录</p>'
        '<h4>现在做什么</h4><p>核对清理范围、看守分工、交接和复查要求，结合热点、过火区、观察哨及任务反馈跟进异常。</p>'
        '<h4>为什么做</h4><p>扑救进展或自报完成不代表残火已清、看守已落实；复燃等异常须保留来源、处置和核实依据。</p>'
        '<h4>由谁做</h4><p>承担清理/看守的队长或组长报告执行、异常及交接情况；指定副总指挥组织协调，'
        '相应专业人员提供必要意见，指定核验负责人核对结果。实际责任人、范围和交接安排须有任务依据。</p>'
        '<h4>已有信息与依据</h4><h5>当前热点、过火区与观察哨</h5>'
        + (maps or '<p>暂无可汇总的当前标绘依据。</p>')
        + '<details class="sop-cleanup-withdrawn"><summary>已撤回标绘（历史参考）</summary>' + archived + '</details>'
        + '<h5>本事件任务与反馈（尚未确认清理/看守分类）</h5>'
        + (''.join(tasks) or '<p>暂无可核对的任务记录。</p>')
        + '<details class="sop-cleanup-simulated"><summary>旧模拟执行记录（非现场反馈）</summary>'
        + items(view.execution.simulated) + '</details><h4>还缺什么</h4>' + items(view.missing)
        + '<p class="lz-note">不按任务名称关键词自动分类，不从热点数量推算复燃次数；普通缺项不锁定其他工作。</p>'
        '<h4>从哪里操作</h4><p>下方“打开信息全景”查看现有标绘与命令；通过任务部署核对当前版本，'
        '通过态势研判的参谋对话入口记录新观测。本批汇总只读，专门的清理看守编制、交接和核验入口仍待后续。</p>'
        '<h4>怎样才算完成</h4><p>须有明确清理/看守任务、执行反馈、异常处置、必要复查和交接依据，并由指定负责人核验。'
        '报告完成、标绘已核验/已复查、旧关闭或未发现新热点都不能直接结项或批准撤离；具体安全条件待专业审核。</p></div>')
