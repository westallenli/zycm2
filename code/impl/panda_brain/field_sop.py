"""现场指挥小 SOP：七环节 + 四级待办排序（只读，纯函数优先）。

设计约束（来自 2026-10-20 用户确认的口径）：

1. 不新增第四套业务状态。本模块只读已有记录，产出的"优先级"是**排序键**，
   不写回、不落盘、不参与任何门禁判定。
2. 关键前置条件只影响"排到第几"和"提示什么"；真正阻断发布的仍是既有门禁。
   本模块不拦任何写操作。
3. "已下达"不等于"已执行"。第 5 环节的状态只从既有接收/回执记录读，
   本模块不自行推断执行状态。
4. 参谋只汇集，不代替研判。第 1、2 环节的完成依据必须是人给出的记录。

数据来源是 `review_pending.business_pending()` 已经算好的待办文本——那是被
项目现有测试覆盖过的汇总口径。本模块在它之上做三件事：分级、排序、补齐五要素。
刻意不重新发明数据来源，避免同一件事出现两套规则。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 七环节。第三项是"本单位完成后应形成的记录"，用于界面上的完成依据提示。
STEPS: tuple[tuple[int, str, str], ...] = (
    (1, "侦察与核实",
     "明确哪里着火、范围和变化、受威胁对象、现场关键缺项；安排人员补查。"
     "完成依据：具名侦察任务已下达，且收到带来源的回报。"),
    (2, "态势与风险研判",
     "汇集侦察、气象、地形、人员及保障情况，判断当前重点与限制。"
     "完成依据：指挥员已明确处置目标，并列出哪些行动受影响。"),
    (3, "确定方案",
     "比较可行方案，确认目标、任务区域、力量安排和执行条件。"
     "完成依据：形成当前有效方案；有关键缺项则指出补齐入口。"),
    (4, "编组与任务下达",
     "把方案落到具体队伍、负责人、区域、任务和回报要求。"
     "完成依据：经既有审核与发布功能下达，生成待接收、待就位事项。"),
    (5, "接收与执行确认",
     "确认队伍是否收到、是否具备执行条件、是否开始行动。"
     "完成依据：区分未接收／准备中／执行中／困难；已下达不算已执行。"),
    (6, "持续跟踪与调整",
     "同时跟踪火情变化、任务进展、人员安全和保障；处置异常。"
     "完成依据：新情况已触发重新研判、补查、增援或任务调整，并留痕。"),
    (7, "效果核验与后续安排",
     "核实任务结果和剩余问题，安排清理、看守及交接。"
     "完成依据：完成的事项有核验依据，遗留继续保留并汇总。"),
)

STEP_NAMES: dict[int, str] = {no: name for no, name, _ in STEPS}
STEP_BASIS: dict[int, str] = {no: basis for no, _, basis in STEPS}

#: 每一环节的固定入口：没有待办时也要能点进去，不能只剩"下一步"。
STEP_ENTRY: dict[int, tuple[str, str]] = {
    1: ("去安排补查", "/sand#cmd-map"),
    2: ("看最新情况", "/sand#cmd-situation"),
    3: ("去看方案建议", "/sand#plan-rail"),
    4: ("去下达任务", "/sand/task-release"),
    5: ("看接收与执行", "/sand/task-receipts"),
    6: ("看异常与调整", "/sand#cmd-advisor"),
    7: ("去核验与交接", "/sand/task-feedback"),
}


STEP_SHORT: dict[int, str] = {
    1: "侦察核实", 2: "态势研判", 3: "确定方案", 4: "编组下达",
    5: "接收执行", 6: "跟踪调整", 7: "核验后续",
}

#: 面板上一句话说明这一步在干什么（现场话，不念文件）。
#: 完整说明仍在 STEPS 的第三项里，放在 title 提示里备查，不占版面。
STEP_PURPOSE: dict[int, str] = {
    1: "搞清楚火在哪、多大、威胁谁",
    2: "判断先顾哪里、哪些地方去不了",
    3: "定下怎么打，缺什么先列出来",
    4: "派谁去、去哪、干什么",
    5: "确认队伍收到、能开始干",
    6: "盯着火和队伍，有变化就改",
    7: "确认干完了，剩下的事交代清楚",
}

#: 每一步"做完的标志"——一两句，能对照着说"这步算完了"。
#: 不写流程术语，写能看见的结果。
STEP_DONE: dict[int, str] = {
    1: "有回报，写清来源和时间",
    2: "说清重点，指出哪些行动受影响",
    3: "方案定了，缺项列出来了",
    4: "任务发出，收件人明确",
    5: "队伍确认收到并开始干",
    6: "新情况已处理并留下记录",
    7: "结果有人核验，遗留有人接",
}

#: 办完之后去哪（两个字，面板底部一行）。
STEP_NEXT: dict[int, str] = {
    1: "研判", 2: "定方案", 3: "下达任务", 4: "等接收",
    5: "跟踪", 6: "核验", 7: "汇总九步走",
}

#: 四级优先级。顺序即排序次序，越靠前越先处理。
IMMEDIATE = "立即处置"
MUST_FIX = "必须先解决"
ADVANCE = "当前应推进"
TRACKING = "持续跟踪"
PRIORITIES: tuple[str, ...] = (IMMEDIATE, MUST_FIX, ADVANCE, TRACKING)
PRIORITY_INDEX: dict[str, int] = {name: i for i, name in enumerate(PRIORITIES)}

#: 每一级"为什么排在这里"的固定解释。界面上直接给指挥员看，不写成日志腔。
PRIORITY_REASON: dict[str, str] = {
    IMMEDIATE: "已报告的人员险情、紧急撤离，或可能让当前行动不再适用的重大变化。",
    MUST_FIX: "阻碍当前关键行动的资料、方案、责任或执行条件缺项，不补齐就没法往下派。",
    ADVANCE: "条件已具备，等你确认、下达、核对或核验。",
    TRACKING: "在等回报或常规更新；暂时不需要你动手，但要看住。",
}

#: 办完之后接什么。按环节给默认值；具体任务可被文本里的下一步覆盖。
NEXT_BY_STEP: dict[int, str] = {
    1: "回报收到后进入核对：缺什么、哪些行动受影响，转入第 2 环节研判。",
    2: "研判有结论后进入第 3 环节，确定当前有效方案。",
    3: "方案确定后进入第 4 环节，编组并下达任务。",
    4: "下达后进入第 5 环节，跟踪队伍是否接收、是否具备执行条件。",
    5: "执行开始后进入第 6 环节，持续跟踪并处置异常。",
    6: "结果明确后进入第 7 环节，核验效果并安排清理看守与交接。",
    7: "证据齐备后向全系统九步走汇总，遗留事项继续保留。",
}

#: 待办文本前缀 → 办理入口。入口用现有页面，不新造路由。
ENTRY_BY_KIND: dict[str, str] = {
    "保障待跟进": "/materials",
    "保障资料待核对": "/materials",
    "待完成任务": "/tasks/formal",
    "环节遗留": "/sand",
    "未知": "/sand",
}

_KIND_PREFIXES: tuple[str, ...] = ("保障待跟进", "保障资料待核对", "待完成任务", "环节遗留")

#: 立刻处置的关键词。命中即压到最高优先级，不看其他条件。
_IMMEDIATE_WORDS: tuple[str, ...] = (
    "险情", "紧急撤离", "撤离需求", "人员被困", "被困", "受伤", "失联",
    "重大变化", "不再适用", "风向突变", "火线突变",
)

#: "缺项／依据不足"类信号。出现"缺口"即算必须先解决——缺口是派任务前须补齐的
#: 条件，不按"当前/预计"再细分一次，避免同一件事两套判定。
_MUST_FIX_WORDS: tuple[str, ...] = (
    "依据待补", "资料待核对", "缺少", "缺项", "缺口", "待补", "责任待", "待指定",
    "未核", "无依据",
)

#: 任务已下达但还没接收/开始的状态词。
_ADVANCE_WORDS: tuple[str, ...] = (
    "未接收", "待接收", "准备中", "待就位", "已发布", "待核验", "待开始",
)

#: 正在执行、只需看住的状态词。
_TRACKING_WORDS: tuple[str, ...] = ("执行中", "跟踪", "等待回报", "常规更新", "跟进中")


@dataclass(frozen=True)
class Item:
    """一条待办。五要素齐全才允许上界面。"""

    priority: str
    step: int
    title: str
    why: str
    who: str
    entry: str
    done: str
    nxt: str
    kind: str = "未知"
    due: str = ""
    source: str = ""
    raw: str = ""

    @property
    def over_due(self) -> bool:
        """只有写明了时限，才谈得上逾期；没有时限一律不算逾期。"""
        return bool(self.due) and "逾期" in self.due

    def as_dict(self) -> dict[str, object]:
        return {
            "priority": self.priority, "step": self.step, "step_name": STEP_NAMES.get(self.step, ""),
            "title": self.title, "why": self.why, "who": self.who, "entry": self.entry,
            "done": self.done, "next": self.nxt, "kind": self.kind, "due": self.due,
            "source": self.source, "raw": self.raw, "over_due": self.over_due,
        }


def split_pending(text: str) -> tuple[str, dict[str, str]]:
    """把一条待办文本拆成（类型, 字段字典）。

    现有汇总口径固定用「；」分字段、「：」分隔名值，例如：
        待完成任务：清理看守；执行中；责任：张三；指定核验人：李四；…；来源：记录 12（当前发布第2版）
    第一段是标题，其余按第一个「：」切成名值；切不出名值的整段作为附注。
    """
    body = text.strip()
    kind = "未知"
    for prefix in _KIND_PREFIXES:
        if body.startswith(prefix + "：") or body.startswith(prefix + ":"):
            kind = prefix
            body = body[len(prefix) + 1:]
            break
    segments = [seg.strip() for seg in body.split("；") if seg.strip()]
    title = segments[0] if segments else body
    fields: dict[str, str] = {}
    notes: list[str] = []
    for seg in segments[1:]:
        for sep in ("：", ":"):
            if sep in seg:
                name, _, value = seg.partition(sep)
                name, value = name.strip(), value.strip()
                if name and value:
                    fields.setdefault(name, value)
                break
        else:
            notes.append(seg)
    if notes:
        fields.setdefault("附注", "；".join(notes))
    return kind, {"title": title, **fields}


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def classify(text: str) -> str:
    """判定优先级。顺序敏感：立即处置 > 必须先解决 > 当前应推进 > 持续跟踪。"""
    if _contains_any(text, _IMMEDIATE_WORDS):
        return IMMEDIATE
    if _contains_any(text, _MUST_FIX_WORDS):
        return MUST_FIX
    if _contains_any(text, _ADVANCE_WORDS):
        return ADVANCE
    if _contains_any(text, _TRACKING_WORDS):
        return TRACKING
    # 兜底：既没说缺什么，也没说在等什么，按"要看住"处理，不冒充可推进。
    return TRACKING


def _step_for(kind: str, text: str) -> int:
    """判断这条待办属于哪个环节。只做粗分，宁可保守也不硬塞。"""
    if kind == "待完成任务":
        # 只看冒号后面的正文："待完成任务"这个词本身含"完成"，
        # 不剥掉前缀会把所有任务都误判成"核验环节"。
        body = text.split("：", 1)[-1]
        if _contains_any(body, ("接收", "就位", "未开始", "待开始")):
            return 5
        if _contains_any(body, ("核验", "关闭", "已完成")):
            return 7
        return 6
    if kind == "环节遗留":
        if _contains_any(text, ("清理", "看守", "交接", "验收", "发布", "信息")):
            return 7
        return 6
    if kind in ("保障待跟进", "保障资料待核对"):
        # 保障缺口是"派任务之前必须先看清"的条件，归到方案与下达之间。
        return 3
    return 6


def _who_from(fields: dict[str, str]) -> str:
    for name in ("任务责任", "责任", "指定核验人／接收人", "指定核验人", "跟进与接收", "负责人"):
        value = fields.get(name, "").strip()
        if value and value not in {"待指定", "待明确"}:
            return value
    return "待指定（负责人尚未明确，不能默认由指挥员代办）"


def _done_from(fields: dict[str, str], priority: str) -> str:
    if priority == IMMEDIATE:
        return "险情或重大变化已有人核实并留痕，且相关行动已按新情况重评。"
    if priority == MUST_FIX:
        detail = fields.get("附注", "") or fields.get("来源", "")
        return "缺项已补齐并留下来源" + (f"（{detail}）" if detail else "") + "；补齐前不据此下达任务。"
    if priority == ADVANCE:
        return "对应业务记录已实际推进（接收、回执、核验各按其既有门禁留痕），不是仅打开页面。"
    return "回报或更新已到达并核对；无新情况则保持跟踪。"


def _next_from(fields: dict[str, str], step: int) -> str:
    for name in ("下一步", "跟进与接收"):
        value = fields.get(name, "").strip()
        if value and value not in {"待明确", "待指定"}:
            return value
    return NEXT_BY_STEP.get(step, NEXT_BY_STEP[6])


def make_item(text: str) -> Item:
    """把一条待办文本转成带五要素的条目。"""
    kind, fields = split_pending(text)
    priority = classify(text)
    step = _step_for(kind, text)
    return Item(
        priority=priority,
        step=step,
        title=fields.get("title", "") or text.strip(),
        why=PRIORITY_REASON[priority],
        who=_who_from(fields),
        entry=ENTRY_BY_KIND.get(kind, ENTRY_BY_KIND["未知"]),
        done=_done_from(fields, priority),
        nxt=_next_from(fields, step),
        kind=kind,
        due=fields.get("时间要求", "") or fields.get("期限", ""),
        source=fields.get("来源", ""),
        raw=text,
    )


def rank(items: list[Item]) -> list[Item]:
    """排序：优先级 → 逾期优先 → 环节靠前 → 进入顺序稳定。

    不按创建时间排——用户明确要求"不能靠创建时间一直排下去"。同一优先级内
    按环节先后，再按进入顺序，保证多次渲染结果一致。
    """
    order = {id(item): index for index, item in enumerate(items)}
    return sorted(
        items,
        key=lambda item: (
            PRIORITY_INDEX.get(item.priority, len(PRIORITIES)),
            0 if item.over_due else 1,
            item.step,
            order.get(id(item), 0),
        ),
    )


def collect(state) -> list[Item]:
    """从现有汇总口径读待办并分级排序。不写任何状态。"""
    from .review_pending import business_pending

    texts = business_pending(state)
    return rank([make_item(text) for text in texts])


def grouped(items: list[Item]) -> list[tuple[str, list[Item]]]:
    """按四级分组，保持空组不输出。"""
    result: list[tuple[str, list[Item]]] = []
    for priority in PRIORITIES:
        bucket = [item for item in items if item.priority == priority]
        if bucket:
            result.append((priority, bucket))
    return result


def ranked_view(items: list[Item], *, head: int = 3) -> dict[str, object]:
    """界面口径：默认只突出前 head 条，其余折叠。"""
    ordered = rank(items)
    return {
        "total": len(ordered),
        "head": [item.as_dict() for item in ordered[:head]],
        "rest": [item.as_dict() for item in ordered[head:]],
        "groups": [
            {"priority": priority, "reason": PRIORITY_REASON[priority],
             "items": [item.as_dict() for item in bucket]}
            for priority, bucket in grouped(ordered)
        ],
    }
