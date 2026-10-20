"""Read-only SOP navigation; no business completion or permission decisions."""
from html.parser import HTMLParser

from .workbench import esc

WORK_ITEMS = (
    ("早期处置", "drawer-fourcat", "查看事件事实与信息缺口，核对已有处置记录。"),
    ("态势研判", "cmd-advisor", "查看态势复核与相关证据，再由指挥员决定后续行动。"),
    ("力量调用", "materials", "查看人员与物资；登记数量不等于当前可派数量。"),
    ("任务部署", "cmd-plans", "查看方案与现有决断入口；采纳不等于执行完成。"),
    ("组织扑救", "drawer-fourcat", "查看正式命令与执行记录，结合现场反馈复核。"),
    ("清理看守", "drawer-fourcat", "查看命令与反馈；报告完成后仍需核验。"),
    ("应急救援", "materials", "随时查看人员与保障资源，不受其他工作项顺序限制。"),
    ("信息发布", "", "暂无独立信息发布入口；对外发布须按职责另行确认。"),
    ("验收交接", "review", "查看处置记录与复盘；进入复盘不代表验收已完成。"),
)


class _SopStateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.state = ""

    def handle_starttag(self, tag, attrs):
        if not self.state:
            self.state = next((value for name, value in attrs if name == "data-sop-state"), "") or ""


def _current_focus(fragment: str) -> str:
    parser = _SopStateParser()
    parser.feed(fragment)
    parser.close()
    return parser.state or "状态待核对"


def render_shell_navigation(*, role: str, event_name: str, data_time: str,
                            contains_demo: bool, has_event: bool = True,
                            early_response_html: str = "", situation_html: str = "",
                            force_html: str = "", deployment_html: str = "",
                            execution_html: str = "", cleanup_html: str = "",
                            emergency_html: str = "", publication_html: str = "",
                            handover_html: str = "",
                            pending_summary_html: str = "", live_context=None,
                            show_workflow: bool = True) -> str:
    source = "含模拟数据 · 非现场实时态势" if contains_demo else "来源与核实状态见对象详情"
    meta = (f'<div class="sand-shell-meta"><b>{esc(event_name)}</b>'
            f'{pending_summary_html if role != "public" else ""}'
            f'<span>数据截至 {esc(data_time)}</span><span>{source}</span></div>')
    if role == "public":
        return meta  # Do not introduce unpublished workflow or action entry points.
    step_html = (early_response_html, situation_html, force_html, deployment_html,
                 execution_html, cleanup_html, emergency_html, publication_html, handover_html)
    from .sop_guide import guide, recommendation_html
    # 九步走归全系统：沙盘既不再渲染它的底部导航，也不再渲染按九项算出来的"当前建议"。
    # show_workflow=False 时连这九次片段解析都省掉，避免每次渲染沙盘重复计算。
    states = [_current_focus(fragment) for fragment in step_html] if show_workflow else []
    buttons = []
    if show_workflow:
        for i, (label, _, _) in enumerate(WORK_ITEMS):
            focus = _current_focus(step_html[i]) if role == "commander" else ""
            hint = f"{label}，当前重点：{focus}。只读提示，不代表完成"
            status_attrs = (f' data-sop-state="{esc(focus)}" title="{esc(hint)}"'
                            f' aria-label="{esc(hint)}"') if focus else ""
            status_html = f'<small class="sand-step-state">{esc(focus)}</small>' if focus else ""
            buttons.append(
                f'<button type="button" data-sand-step="{i}" aria-controls="sand-work-detail"'
                f' aria-pressed="false"{status_attrs}><span>{i + 1:02d}</span>{esc(label)}'
                f'{status_html}</button>')
    entries = []
    if show_workflow:
        from .live_context import blocks_for_step
        for i, (label, target, explanation) in enumerate(WORK_ITEMS):
            action = ""
            if target in {"materials", "review"}:
                word = '人员与物资' if target == 'materials' else '复盘记录'
                action = f'<a class="lz-btn lz-btn2" data-sand-related="{i}" href="/{target}">打开{word}</a>'
            elif target:
                word = {'drawer-fourcat': '信息全景', 'cmd-advisor': '智能参谋', 'cmd-plans': '方案与行动'}[target]
                action = (f'<button type="button" class="lz-btn lz-btn2" '
                          f'data-sand-open="{target}">打开{word}</button>')
            entries.append(f'<section id="sand-work-{i}" data-sand-step-content="{i}" hidden><h3>{label}</h3>'
                           + (f'<div data-sop-summary="{i}">{guide(i, step_html[i])}</div>' if role == 'commander' else f'<p>{explanation}</p>')
                           + f'<details id="sop-basis-{i}" class="sop-basis" data-sop-basis="{i}"><summary>查看完整依据与原办理入口</summary><p>{explanation}</p>'
                           + ('<p class="lz-note">既有详细概览按页面载入；下方同事件联动区及底部工作状态自动核对当前记录。需要操作时先复核当前版本。</p>'
                              if role == 'commander' and live_context else '')
                           + (step_html[i] if role == 'commander' else '')
                           + (blocks_for_step(live_context, i) if role == 'commander' and live_context else '')
                           + f'{action}</details></section>')
    event_hint = ''
    if not has_event:
        event_hint = ('<p class="sand-empty">尚未进入事件任务空间。'
                      + ('请从<a href="/events">火情登记</a>选择并进入事件。' if role == 'commander'
                         else '请联系指挥员确认当前事件。') + '</p>')

    def workflow_nav(items: list[str]) -> str:
        """九步走属于全系统，不属于沙盘；沙盘改为在七环节条上给一个链接。"""
        if not show_workflow:
            return ""
        return ('<nav class="sand-workflow" aria-label="九项并行处置工作">'
                '<div><b>灭火工作流程</b><small>按序了解 · 可并行办理</small></div>'
                '<div class="sand-work-items">' + "".join(items) + '</div></nav>')

    detail = ""
    if show_workflow:
        detail = ('<section id="sand-work-detail" hidden>'
                  + '<p class="sand-work-empty sand-empty">从底部九项工作中选择一项，查看相关记录与操作。</p>'
                  + event_hint + "".join(entries)
                  + '<p class="lz-note">各项工作可并行。当前为操作导航，不自动判定完成；'
                  '所需条件与责任须结合业务记录和专业核实。</p></section>')
    else:
        # 沙盘不再重复九步导航，但保留隐藏标记：
        # 全系统九步走仍是唯一入口，这些标记只用于识别"本页属于九步框架"，界面不显示。
        detail = (''.join(f'<span data-sand-step="{number}" hidden></span>' for number in range(1, 10))
                  + '<div class="sand-workflow" data-workflow-entry="/workflow/guide" hidden></div>')

    return (meta + (f'<aside class="sop-next" aria-label="当前建议">{recommendation_html(states)}</aside>'
                    if role == 'commander' and show_workflow else '')
            + detail
            + workflow_nav(buttons))
