"""态势问询意图分流层（doc 03《指挥调度沙盘》§13/§13.2 定版）。

文字输入与语音转写文本统一从这里过一遍分流：

- 问询 → 只检索数据，不改状态（下游接沙盘/复盘问询引擎）；
- 命令（指挥员）→ 转为命令草稿（显示执行单位、区域、状态），
  经指挥员确认后才进入既有指挥链（指挥员自提方案 → 决断 → ExecutionOrder）；
- 命令（其他人员）→ 无权限提示，绝不执行。

纪律：
- 分类是确定性词法规则，不猜语义；判不了命令就按问询处理，
  由问询引擎如实回答「无法回答」，不伪装理解。
- 语音识别（ASR）通道待接入——本层只消费文本，不假装能听；
  接入后 ASR 转写文本走同一入口、同一分流、同一审计。
"""

from __future__ import annotations

import re

#: 命令意图动词/指令名词（确定性匹配）；与疑问标记同现时视为问询。
COMMAND_VERBS: tuple[str, ...] = (
    "下达", "派出", "调派", "增援", "撤离", "转移", "撤收",
    "封锁", "封控", "点烧", "扑打", "合围", "清理火线",
    "吊桶洒水", "机降", "开设隔离带", "开挖隔离带",
)

QUESTION_MARKS: tuple[str, ...] = (
    "哪些", "多少", "几条", "几人", "什么", "为何", "为什么",
    "吗", "？", "?", "怎么", "如何", "哪里", "是否",
)

_AREA_PATTERN = re.compile(r"[在从由到向]([^\s，。；；,、？！?！]{2,12})")

#: 区域提示截断词：捕获串中出现即截断（提示值仍需人工核对）。
_AREA_CUT_VERBS: tuple[str, ...] = (
    "开设", "开挖", "构筑", "实施", "组织", "执行",
    "增援", "扑救", "拦截", "坚守", "清理", "搜索",
)


def classify_intent(text: str) -> str:
    """返回 ``"command"`` 或 ``"inquiry"``。

    规则与复盘问询引擎一致：命中命令动词且不含任何疑问标记 → 命令；
    其余（含空文本、含疑问标记、无信号自由文本）→ 按问询处理，
    由问询引擎如实回答或明说无法回答。永不自动执行任何命令。
    """
    text = (text or "").strip()
    if not text:
        return "inquiry"
    verb_hit = any(v in text for v in COMMAND_VERBS)
    asking = any(m in text for m in QUESTION_MARKS)
    if verb_hit and not asking:
        return "command"
    return "inquiry"


def extract_unit(text: str, known_units: tuple[str, ...] | list[str]) -> str:
    """从原文中按已知单位名精确匹配执行单位提示；取不到就空串，不猜。"""
    text = (text or "").strip()
    for name in sorted(known_units, key=len, reverse=True):
        name = (name or "").strip()
        if len(name) >= 2 and name in text:
            return name
    return ""


def extract_area_hint(text: str) -> str:
    """提取区域提示（「在/到/向 + 短语」首个命中）；仅作人工核对提示。

    捕获串内出现动作动词即截断（如「青岗梁开设隔离带」→「青岗梁」）；
    动词打头说明前置介词短语并非位置，如实返回空串，不猜。
    """
    match = _AREA_PATTERN.search((text or "").strip())
    if not match:
        return ""
    captured = match.group(1).strip("的")
    cut_at = len(captured)
    for verb in _AREA_CUT_VERBS:
        pos = captured.find(verb)
        if pos == 0:
            return ""
        if 0 < pos < cut_at:
            cut_at = pos
    result = captured[:cut_at].strip()
    return result if len(result) >= 2 else ""


def parse_command_fields(
    text: str, known_units: tuple[str, ...] | list[str] = (),
) -> dict[str, str]:
    """命令草稿字段解析：全部是提示值，需指挥员核对后才可进入指挥链。

    返回 ``{"unit": 执行单位提示或"", "area": 区域提示或""}``——
    空串在展示层一律标「未解析」，不用猜测值冒充结构化结果。
    """
    return {
        "unit": extract_unit(text, known_units),
        "area": extract_area_hint(text),
    }
