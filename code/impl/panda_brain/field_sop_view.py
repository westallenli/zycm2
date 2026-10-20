"""现场指挥待办的界面片段（服务端渲染，无新权限、无写操作）。

现场口径（2026-10-20 用户更正）：菜单上的字要少。能点的地方就放按钮，
过程说明收起来，要点才看。

因此默认只渲染三样：
    标题 + 一个动作按钮 + 谁办（一行小字）
"为什么排在这 / 怎么算完 / 办完接什么 / 来源"全部放进折叠的「依据」。

视觉一致性：复用沙盘既有的 `.sop-next`（左下当前建议）、`.sop-path`（步骤横条）、
`.lz-btn`（按钮），只新增 `.field-*` 一组，写在 static/field-sop.css。
"""
from __future__ import annotations

from .workbench import esc

_PRIORITY_CLASS = {
    "立即处置": "field-im",
    "必须先解决": "field-mf",
    "当前应推进": "field-ad",
    "持续跟踪": "field-tr",
}

#: 按钮上只写动作，不写"点击查看详情"这类废话。
_ACTION = {
    "立即处置": "立即处置",
    "必须先解决": "去补齐",
    "当前应推进": "去核对",
    "持续跟踪": "看进展",
}

#: 环节状态压到两个字。
_STEP_STATE = {"done": "完成", "now": "当前", "blocked": "卡住", "todo": "未办"}

#: 两字优先级标签。同一件事在常驻条、抽屉、环节面板里必须读起来一致，
#: 所以面板里也带上，只是压到两个字，不再重复整句。
_PRIORITY_SHORT = {
    "立即处置": "立即",
    "必须先解决": "先解",
    "当前应推进": "推进",
    "持续跟踪": "跟踪",
}

#: 办理入口 → 页面名（只在折叠依据里出现，界面上不显示裸路径）。
_ENTRY_LABEL = {
    "/materials": "人员与物资",
    "/tasks/formal": "正式任务",
    "/tasks/feedback": "执行反馈",
    "/tasks/release": "任务发布",
    "/sand": "沙盘",
    "/review": "复盘",
}


def _action(item) -> str:
    return _ACTION.get(item.priority, "去办理")


def _short_who(who: str) -> str:
    """把"待指定（负责人尚未明确…）"压成"待指定"，长解释留给依据。"""
    head = who.split("（")[0].split("(")[0].strip()
    return head or who.strip()


def _short_title(title: str, limit: int = 18) -> str:
    """标题过长时截断并加省略号——完整标题在依据里仍有原文。"""
    text = title.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_next(items, *, entry: str = "#") -> str:
    """常驻条：一行标题 + 一个按钮 + 一行小字。"""
    if not items:
        return ('<div data-sop-recommendation><span>现在该办</span>'
                '<b>暂无待办</b><small>没有已登记的待办</small></div>')
    head = items[0]
    total = len(items)
    trigger = (f' <button type="button" class="field-more" data-field-open>全部 {total} 条 ›</button>'
               if total > 1 else "")
    return (
        '<div data-sop-recommendation>'
        '<span>现在该办</span>'
        f'<b>{esc(_short_title(head.title))}</b>'
        f'<small>{esc(_short_who(head.who))} · {esc(head.priority)}{trigger}</small>'
        f'<a class="{_button_class(head)}" href="{esc(head.entry or entry)}">{esc(_action(head))}</a>'
        '</div>'
    )


def render_steps(states: dict[int, str], *, basis: dict[int, str] | None = None,
                 workflow_href: str = "") -> str:
    """七环节：一排小胶囊。状态只靠圆点颜色 + 当前项反白，不占文字。

    workflow_href 非空时，在条尾挂一个"九步走"入口——九步走属于全系统，
    沙盘只给链接，不在这里重复它的内容。
    """
    from .field_sop import STEP_SHORT, STEPS

    basis = basis or {}
    cells = []
    for number, name, _ in STEPS:
        state = states.get(number, "todo")
        label = _STEP_STATE.get(state, "未办")
        pressed = "true" if state == "now" else "false"
        hint = f"{number:02d} {name}：{label}" + (f"。{basis[number]}" if basis.get(number) else "")
        cells.append(
            f'<li><button type="button" class="field-pill" data-field-step="{number}"'
            f' data-state="{esc(state)}" aria-pressed="{pressed}"'
            f' aria-controls="field-step-panel" aria-expanded="false"'
            f' title="{esc(hint)}" aria-label="{esc(hint)}">'
            '<span class="dot" aria-hidden="true"></span>'
            f'<span class="n">{number:02d}</span>{esc(STEP_SHORT.get(number, name))}</button></li>'
        )
    tail = (f'<a class="field-wf-link" href="{esc(workflow_href)}"'
            ' title="九步走属于全系统，不在沙盘重复">全系统九步走 ›</a>') if workflow_href else ""
    return ('<nav class="field-ribbon" aria-label="现场指挥七环节">'
            '<div>现场指挥流程<small>可并行 · 紧急可插入</small></div>'
            '<ol class="field-step-row">' + "".join(cells) + '</ol>'
            + tail + '</nav>')


def _button_class(item) -> str:
    """立即处置和必须先解决用橙色按钮，其余用蓝色——一眼分得出"这个急"。"""
    if item.priority in ("立即处置", "必须先解决"):
        return "lz-btn field-btn-warn"
    return "lz-btn"


def _render_item(item, *, compact: bool = False) -> str:
    cls = _PRIORITY_CLASS.get(item.priority, "field-tr")
    who = _short_who(item.who)
    five = (
        ("为什么在这", item.why),
        ("怎么算完", item.done),
        ("办完接什么", item.nxt),
        ("现在点什么", f"去「{_ENTRY_LABEL.get(item.entry, '对应办理页')}」办理"),
        ("谁办", item.who),
    )
    rows = "".join(f'<dt>{esc(label)}</dt><dd>{esc(value)}</dd>' for label, value in five)
    source = f'<p class="field-src">来源：{esc(item.source or "本事件记录")}</p>' if item.source else ""
    # compact：只在某个环节的面板里用。环节号已经是上下文，不再重复显示；
    # 但优先级要补回来，否则同一件事在抽屉和面板里读起来不一致。
    step = "" if compact else f'<span class="field-step">{item.step:02d}</span>'
    tag = (f'<span class="field-tag {cls}">{esc(_PRIORITY_SHORT.get(item.priority, ""))}</span>'
           if compact else "")
    return (
        f'<article class="field-todo-item {cls}">'
        f'<div class="field-row"><h4>{esc(_short_title(item.title))}</h4>'
        f'{tag}<span class="field-who">{esc(who)}</span>{step}</div>'
        f'<div class="field-act"><a class="{_button_class(item)}" href="{esc(item.entry or "#")}">'
        f'{esc(_action(item))}</a></div>'
        f'<details class="field-basis"><summary>依据</summary>'
        f'<dl class="field-five">{rows}</dl>{source}</details>'
        '</article>'
    )


class _DictItem:
    """把 ranked_view 产出的 dict 包成和 field_sop.Item 一样的读取面。"""

    __slots__ = ("priority", "step", "title", "why", "who", "entry", "done", "nxt",
                 "due", "source", "over_due")

    def __init__(self, data: dict) -> None:
        self.priority = data.get("priority", "持续跟踪")
        self.step = data.get("step", 6)
        self.title = data.get("title", "")
        self.why = data.get("why", "")
        self.who = data.get("who", "")
        self.entry = data.get("entry", "")
        self.done = data.get("done", "")
        self.nxt = data.get("next", "")
        self.due = data.get("due", "")
        self.source = data.get("source", "")
        self.over_due = bool(data.get("over_due"))


def _as_items(rows):
    for row in rows:
        yield row if hasattr(row, "nxt") else _DictItem(row)


def render_step_panels(items=None, *, workflow_href: str = "") -> str:
    """七环节逐项面板：骨架由服务端出，待办行由前端从"指挥待办"的同一份数据填入。

    只留四样：环节名、一句干什么、待办列表、底部两个按钮（下一步／九步走）。
    完整依据与"做完的标志"只放在 title 提示里，不占版面。
    数据源统一为 `command_sop.queue()` 的行，避免沙盘上出现两套待办。
    """
    from .field_sop import STEP_BASIS, STEP_DONE, STEP_ENTRY, STEP_PURPOSE, STEP_SHORT, STEPS

    sections = []
    for number, name, _ in STEPS:
        # 每步一个固定入口：哪怕这一步没有待办，也能点进去办事。
        entry_label, entry_href = STEP_ENTRY.get(number, ("", ""))
        go = (f'<a class="field-sp-btn" href="{esc(entry_href)}">{esc(entry_label)}</a>'
              if entry_href else "")
        # 下一步做成按钮，点了直接翻到那一节；最后一节没有"下一步"，只留九步走入口。
        nxt = (f'<button type="button" class="field-sp-btn2" data-field-step-next="{number + 1}">'
               f'下一步：{esc(STEP_SHORT.get(number + 1, ""))} →</button>') if number < 7 else ""
        wf = (f'<a class="field-sp-btn2" href="{esc(workflow_href)}">九步走</a>'
              if workflow_href else "")
        tip = f'{STEP_PURPOSE[number]}。做完的标志：{STEP_DONE[number]}。{STEP_BASIS[number]}'
        sections.append(
            f'<section class="field-sp-item" data-step-panel="{number}" hidden>'
            f'<header class="field-sp-head"><b>第 {number} 环节 · {esc(name)}</b>'
            f'<span data-step-count="{number}">—</span></header>'
            f'<p class="field-sp-purpose" title="{esc(tip)}">'
            f'{esc(STEP_PURPOSE[number])}</p>'
            f'<p class="field-sp-done">做完的标志：{esc(STEP_DONE[number])}</p>'
            f'<div class="field-sp-list" data-step-list="{number}">'
            '<p class="field-empty">正在读取指挥待办…</p></div>'
            f'<div class="field-sp-actions">{go}{nxt}{wf}</div>'
            '</section>')

    return ('<aside class="field-step-panel" id="field-step-panel" data-field-step-panel hidden>'
            '<header class="field-sp-bar"><b>环节内容</b>'
            '<span>取自指挥待办 · 点开不代表完成</span>'
            '<button type="button" data-field-step-close aria-label="收起">×</button></header>'
            '<div class="field-sp-body">' + "".join(sections) + '</div></aside>')


def step_states(items) -> dict[int, str]:
    """七环节状态：只标能从记录证明的。

    - 有"立即处置／必须先解决"待办的环节 → 卡住
    - 排第一那条所在的环节 → 当前
    - 其余一律"未办"

    刻意**不推断"已完成"**：本项目要求"没有条件的步骤不伪造完成"，
    而完成需要业务记录佐证，本模块只读待办汇总，拿不到充分证据就不标。
    """
    from .field_sop import IMMEDIATE, MUST_FIX

    states = {number: "todo" for number in range(1, 8)}
    for item in items:
        if item.priority in (IMMEDIATE, MUST_FIX):
            states[item.step] = "blocked"
    if items:
        states[items[0].step] = "now"
    return states


def build(state, *, workflow_href: str = "/workflow/guide") -> dict:
    """给沙盘页出骨架。

    待办数据一律来自"指挥待办"（`command_sop.queue()` 的行），由前端填入同一份，
    因此沙盘上只有一套待办；本模块不再自己从复盘遗留里另算一套。

    保留 `state` 参数只为兼容现有调用点与后续替换数据源。
    """
    del state  # 当前不需要服务端算待办；保留形参便于后续接同一份队列
    return {
        "steps_html": render_steps({}, workflow_href=workflow_href),
        "step_panels": render_step_panels(workflow_href=workflow_href),
        "drawer": "",
        "next": "",
        "note": "",
    }


def render_drawer(view: dict, *, title: str = "现在该办什么") -> str:
    """右侧抽屉：分组小标题 + 紧凑待办。默认收起。"""
    blocks = []
    for group in view.get("groups") or []:
        items = list(_as_items(group.get("items") or []))
        if not items:
            continue
        cls = _PRIORITY_CLASS.get(group["priority"], "field-tr")
        blocks.append(
            f'<section class="field-group {cls}">'
            f'<header><b>{esc(group["priority"])}</b><em>{len(items)}</em></header>'
            + "".join(_render_item(item) for item in items)
            + '</section>'
        )
    body = "".join(blocks) or '<p class="field-empty">暂无已登记的待办</p>'
    return (
        '<aside class="field-todo" id="field-todo-drawer" data-field-todo hidden>'
        f'<header class="field-todo-bar"><b>{esc(title)}</b>'
        f'<span>{view.get("total", 0)} 条</span>'
        '<button type="button" data-field-close aria-label="收起">×</button></header>'
        f'<div class="field-todo-body">{body}</div>'
        '<footer class="field-todo-foot">排序不改权限；打开不等于完成</footer></aside>'
    )
