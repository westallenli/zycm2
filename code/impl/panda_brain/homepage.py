"""Homepage renderer (P17-P2) — 首页组织架构「数据驱动自绘」.

对齐口径：``docs/页面设计定版/02_首页组织架构自绘_V0.3.md``
（V0.2 的「原图不可重绘」条款已按 DEVELOPMENT_PLAN_PAGES_V1 §P2 废止）。

核心约束（不可回退）：
- 组织结构唯一来源是 ``org_model.py`` 结构数据（COMMAND_NODES / POSITION_SLOTS /
  _DOMAIN_PARENT）；页面由这些数据自绘节点与连线，**零图片依赖**——运行时不读取、
  不引用 org_chart.png / org_anchors.json，锚点机制整体废除。
- 布局为确定性像素数学的纯函数：同一数据两次渲染逐字节一致；列间横坐标互不重叠，
  列内卡片纵向步距大于卡高，连线画在卡片层之下——无重叠遮挡。
- 到场状态来自长期名册 + 本次名册指派 + 有效签到绑定；八态判定态如实叠加
  （单一事实源 personnel_states.derive_personnel_states）；预编入/待到场仍显示
  「未到场」，不换词。
- 登录人按名册指派或签到绑定落入对应组：节点高亮并标注「姓名（我）」；
  仅名册指派未刷码时如实标注「（我·未到场）」。
- 无名册 / 未进入事件上下文 / 通信未接入均显式呈现，不做本地伪数据。

纯渲染约定与 workbench 相同：输入即数据（与 PanelState 解耦），输出 HTML 片段，
绝不改写工作区（GET 只读保证由调用方 version 比对兜底）。
"""

from __future__ import annotations

import html as _html
from collections import Counter

from .fire_events import group_of_slot
from .org_model import (
    COMMAND_NODES,
    NODE_INDEX,
    POSITION_SLOTS,
    SLOT_INDEX,
    _DOMAIN_PARENT,
    mask_phone,
)
from .personnel_states import STATE_COLORS, derive_personnel_states

#: 演示阶段通信数据未接入的诚实标注（doc 00 §6 / doc 02 V0.3 §5）。
COMM_PENDING_NOTE = "通信状态：待接入（未接入通信数据，不显示虚构状态）"

#: 到场状态用色（doc 02 §3.2）。
_STATUS_COLORS = {
    "已到场": "#2F7D68",
    "未到场": "#647382",
    "通信异常": "#B77A1A",
    "失联": "#B84A42",
}

#: 首页名册面板在 02 号 §3.2 二元口径之上叠加的状态词
#：（02 号 §3.3 离场流转 + doc 04 §4.1 指挥员判定态）。
#: 预编入/待到场仍显示为「未到场」，不换词，保持定版词汇。
_HOME_OVERRIDE_STATES = frozenset(
    {"执行中", "暂离/轮休", "失联", "已撤离", "已归建"}
)

# ---------------------------------------------------------------------------
# 自绘布局模型（确定性像素数学；V0.3 §6）

#: 六条线固定从左到右排序（调度长、发言人、专技、灭火、维稳、后勤）。
BRANCH_ORDER: tuple[str, ...] = (
    "dispatch-chief",
    "spokesperson",
    "dep-tech",
    "dep-ops",
    "dep-security",
    "dep-logistics",
)

_CARD_W = 178          # 组卡宽
_SLOT_H = 56           # 组卡高
_HEAD_H = 50           # 线首（副总指挥等）卡高
_TOP_W = 340           # 联合指挥部 / 前线总指挥宽卡
_TOP_H = 46            # 宽卡高
_GAP_X = 14            # 列间距
_COL_STEP = _CARD_W + _GAP_X
_ROW_STEP = _SLOT_H + 12   # 列内纵向步距（> 卡高 ⇒ 无重叠）
_PAD = 18              # 画布内边距
_JOINT_Y = _PAD
_GENERAL_Y = _JOINT_Y + _TOP_H + 20
_BUS_Y = _GENERAL_Y + _TOP_H + 16      # 横向分配母线 y
_HEAD_Y = _BUS_Y + 18                  # 线首行 y
_GROUP_TOP = _HEAD_Y + _HEAD_H + 14    # 各列首个组卡 y


def org_layout() -> dict:
    """由 org_model 结构数据生成确定性布局。

    返回 {"canvas_w", "canvas_h", "boxes", "links"}：
    - ``boxes``：全部节点矩形 [{kind, code, label, x, y, w, h}]；
      kind ∈ {top（联合指挥部/总指挥）, head（六条线线首）, slot（29 个组）}；
    - ``links``：SVG 直线段 [(x1, y1, x2, y2)]，全部落在卡片层之下。

    纯函数：无参数、读全局常量、输出确定——同一代码版本布局永不漂移。
    """
    columns: list[dict] = []
    for node_code in BRANCH_ORDER:
        domain = next(d for d, p in _DOMAIN_PARENT.items() if p == node_code)
        slots = [s for s in POSITION_SLOTS if s.domain == domain]
        columns.append({"code": node_code, "domain": domain, "slots": slots})

    canvas_w = _PAD * 2 + len(columns) * _COL_STEP - _GAP_X
    max_rows = max(len(c["slots"]) for c in columns)
    canvas_h = _GROUP_TOP + (max_rows - 1) * _ROW_STEP + _SLOT_H + _PAD

    def col_x(i: int) -> int:
        return _PAD + i * _COL_STEP

    top_x = (canvas_w - _TOP_W) // 2
    center = canvas_w // 2

    boxes: list[dict] = [
        {
            "kind": "top", "code": "joint",
            "label": NODE_INDEX["joint"].name,
            "x": top_x, "y": _JOINT_Y, "w": _TOP_W, "h": _TOP_H,
        },
        {
            "kind": "top", "code": "general",
            "label": NODE_INDEX["general"].name,
            "x": top_x, "y": _GENERAL_Y, "w": _TOP_W, "h": _TOP_H,
        },
    ]
    links: list[tuple[int, int, int, int]] = [
        (center, _JOINT_Y + _TOP_H, center, _GENERAL_Y),
        (center, _GENERAL_Y + _TOP_H, center, _BUS_Y),
        (col_x(0) + _CARD_W // 2, _BUS_Y,
         col_x(len(columns) - 1) + _CARD_W // 2, _BUS_Y),
    ]
    for i, col in enumerate(columns):
        cx = col_x(i) + _CARD_W // 2
        boxes.append(
            {
                "kind": "head", "code": col["code"],
                "label": NODE_INDEX[col["code"]].name,
                "x": col_x(i), "y": _HEAD_Y, "w": _CARD_W, "h": _HEAD_H,
            }
        )
        links.append((cx, _BUS_Y, cx, _HEAD_Y))
        for r, slot in enumerate(col["slots"]):
            boxes.append(
                {
                    "kind": "slot", "code": slot.pos_code,
                    "label": slot.name,
                    "x": col_x(i), "y": _GROUP_TOP + r * _ROW_STEP,
                    "w": _CARD_W, "h": _SLOT_H,
                }
            )
        if col["slots"]:
            last_y = _GROUP_TOP + (len(col["slots"]) - 1) * _ROW_STEP
            links.append((cx, _HEAD_Y + _HEAD_H, cx, last_y))
    return {
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "boxes": boxes,
        "links": links,
    }


def esc(value) -> str:
    return _html.escape(str(value), quote=True)


def _status_tone(status_word: str) -> str:
    """状态词 → 色板色：先查 02 号 §3.2 表，再回落八态色板，兜底次文字灰。"""
    if status_word in _STATUS_COLORS:
        return _STATUS_COLORS[status_word]
    return STATE_COLORS.get(status_word, "#647382")


def _derived_state_index(workspace, roster_entries=()) -> dict:
    """phone → PersonState（只读推导）；任何异常一律空表，页面退回二元口径。"""
    try:
        return {
            ps.phone: ps for ps in derive_personnel_states(workspace, roster_entries)
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 联系方式脱敏（doc 02 §4.1）


def mask_for_viewer(phone: str, *, viewer_role: str, viewer_phone: str = "") -> str:
    """指挥员完整；本人完整；其他到场人员中间四位打码；公共屏全部打码。"""
    phone = phone or ""
    if viewer_role == "commander":
        return phone
    if viewer_role == "member" and viewer_phone == phone:
        return phone
    if viewer_role == "public":
        return "＊＊＊＊＊＊＊＊"
    return mask_phone(phone)


# ---------------------------------------------------------------------------
# 到场聚合（纯函数）：长期名册 = 应到，活动花名册绑定 = 实到


def attendance_by_slot(registry, workspace, event_id: str) -> dict:
    """按编制槽位聚合应到/实到；名册未配置返回显式空态标记。

    返回字段：
    - ``configured``：事件是否已有名册；
    - ``slots``：按 POSITION_SLOTS 顺序 [{pos_code,name,group,expected,arrived,rate,missing}]，
      仅列出 expected>0 或 arrived>0 的槽位；
    - ``unassigned``：名册内本职未能解析岗位的人数；
    - ``out_of_roster``：已到场但不在名册（或岗位与名册指派不一致）的绑定列表——如实呈现，
      不静默吞掉；
    - ``totals``：{expected, arrived}。
    """
    roster = registry.rosters.get(event_id)
    configured = roster is not None and len(roster.entries) > 0
    entries = roster.entries if roster else {}

    expected: Counter = Counter()
    assigned: dict[str, str] = {}
    unassigned = 0
    for phone, entry in entries.items():
        if entry.pos_code is None:
            unassigned += 1
            continue
        expected[entry.pos_code] += 1
        assigned[phone] = entry.pos_code

    arrived: Counter = Counter()
    arrived_phones: set[str] = set()
    out_of_roster: list[dict] = []
    for binding in workspace.roster_bindings.values():
        if binding.status != "active":
            continue
        arrived[binding.pos_code] += 1
        arrived_phones.add(binding.person.phone)
        if binding.person.phone not in entries:
            reason = "不在本事件名册"
        elif entries[binding.person.phone].pos_code != binding.pos_code:
            reason = (
                "名册岗位未解析却已绑定"
                if entries[binding.person.phone].pos_code is None
                else "与名册指派岗位不一致"
            )
        else:
            continue
        slot_name = SLOT_INDEX[binding.pos_code].name if binding.pos_code in SLOT_INDEX else binding.pos_code
        out_of_roster.append(
            {
                "name": binding.person.name,
                "phone": binding.person.phone,
                "pos_code": binding.pos_code,
                "slot_name": slot_name,
                "reason": reason,
            }
        )

    # 已离场者不在实到内，但会落入「未到场名单」——带真实状态后缀，不冒充未刷码。
    offsite_word = {
        phone: ps.state
        for phone, ps in _derived_state_index(workspace, entries.values()).items()
        if ps.state in {"已撤离", "已归建"}
    }

    slots: list[dict] = []
    for slot in POSITION_SLOTS:
        exp = expected.get(slot.pos_code, 0)
        arr = arrived.get(slot.pos_code, 0)
        if exp == 0 and arr == 0:
            continue
        missing_phones = [
            phone
            for phone, entry in entries.items()
            if entry.pos_code == slot.pos_code and phone not in arrived_phones
        ]
        names = sorted(
            registry.library[phone].name
            + (f"（{offsite_word[phone]}）" if phone in offsite_word else "")
            for phone in missing_phones
            if phone in registry.library
        )
        rate = (arr / exp) if exp else None
        slots.append(
            {
                "pos_code": slot.pos_code,
                "name": slot.name,
                "group": group_of_slot(slot.pos_code),
                "expected": exp,
                "arrived": arr,
                "rate": rate,
                "missing_names": names,
            }
        )
    totals = {"expected": sum(expected.values()), "arrived": sum(arrived.values())}
    return {
        "configured": configured,
        "slots": slots,
        "unassigned": unassigned,
        "out_of_roster": out_of_roster,
        "totals": totals,
    }


def person_rows_for_group(registry, workspace, event_id: str) -> list[dict]:
    """右侧人员名单（doc 02 §5.1 字段）：名册 ∪ 实到绑定，按姓名排序。"""
    roster = registry.rosters.get(event_id)
    entries = roster.entries if roster else {}

    rows: dict[str, dict] = {}
    for phone, entry in entries.items():
        person = registry.library.get(phone)
        if person is None:
            continue
        slot_name = SLOT_INDEX[entry.pos_code].name if entry.pos_code in SLOT_INDEX else ""
        rows[phone] = {
            "name": person.name,
            "phone": phone,
            "unit": person.unit,
            "home_position": person.home_position,
            "pos_code": entry.pos_code or "",
            "slot_name": slot_name,
            "group": group_of_slot(entry.pos_code) if entry.pos_code else "",
            "status_word": "未到场",
            "arrival": "",
            "state_basis": "",
        }
    for binding in workspace.roster_bindings.values():
        if binding.status != "active":
            continue
        phone = binding.person.phone
        row = rows.setdefault(
            phone,
            {
                "name": binding.person.name,
                "phone": phone,
                "unit": binding.person.unit,
                "home_position": binding.person.home_position,
                "pos_code": "",
                "slot_name": "",
                "group": "",
                "status_word": "未到场",
                "arrival": "",
                "state_basis": "",
            },
        )
        row["status_word"] = "已到场"
        row["arrival"] = ""  # 到场时刻来自会话层 arrivals，纯函数不伪造时间
        if not row["slot_name"]:
            slot = SLOT_INDEX.get(binding.pos_code)
            row["pos_code"] = binding.pos_code
            row["slot_name"] = slot.name if slot else binding.pos_code
            row["group"] = group_of_slot(binding.pos_code) if slot else ""
    # 八态叠加（A3）：离场/撤离/判定态覆盖二元口径，依据随芯片 title 展示。
    states = _derived_state_index(workspace, entries.values())
    for phone, row in rows.items():
        ps = states.get(phone)
        if ps is not None and ps.state in _HOME_OVERRIDE_STATES:
            row["status_word"] = ps.state
            row["state_basis"] = ps.basis
    return sorted(rows.values(), key=lambda r: (r["name"], r["unit"]))


def viewer_detail(registry, workspace, *, viewer_phone: str, arrivals=None) -> dict | None:
    """当前登录人详情卡（doc 02 V0.3 §3.3）：找不到会话身份时返回 None。

    workspace 为 None（事件任务空间未在本地打开）时按未到场呈现，不跨空间读数。
    """
    person = registry.library.get(viewer_phone)
    if person is None:
        return None
    binding = None
    if workspace is not None:
        binding = next(
            (
                b
                for b in workspace.roster_bindings.values()
                if b.status == "active" and b.person.phone == viewer_phone
            ),
            None,
        )
    state_word = state_basis = ""
    if workspace is not None:
        # 本人八态（A3）：已撤离/判定态如实上卡，不再二元化
        match = next(
            (ps for ps in derive_personnel_states(workspace) if ps.phone == viewer_phone),
            None,
        )
        if match is not None and match.state in _HOME_OVERRIDE_STATES:
            state_word, state_basis = match.state, match.basis
    return {
        "name": person.name,
        "unit": person.unit,
        "home_position": person.home_position,
        "phone_full": person.phone,
        "bound": binding is not None,
        "state_word": state_word,
        "state_basis": state_basis,
        "slot_name": (
            SLOT_INDEX[binding.pos_code].name
            if binding is not None and binding.pos_code in SLOT_INDEX
            else ""
        ),
        "group": (
            group_of_slot(binding.pos_code)
            if binding is not None and binding.pos_code in SLOT_INDEX
            else ""
        ),
        "arrival": (arrivals or {}).get(viewer_phone, ""),
        # 通信状态无真实数据源：统一「待接入」，不显示虚构在线/离线
        "comm": COMM_PENDING_NOTE,
    }


def viewer_assignments(registry, workspace, event_id: str, *, viewer_phone: str) -> list[dict]:
    """当前登录人在本次事件的岗位落位：签到绑定优先，名册指派兜底。

    返回 [{"pos_code", "bound"}]，bound=True 表示存在有效绑定（已到场事实），
    False 表示仅有名册指派（未刷码）。两处都如实呈现，不互相冒充。
    """
    codes: list[dict] = []
    if workspace is not None and event_id:
        for b in workspace.roster_bindings.values():
            if b.status == "active" and b.person.phone == viewer_phone:
                codes.append({"pos_code": b.pos_code, "bound": True})
    if registry is not None and event_id:
        roster = registry.rosters.get(event_id)
        entry = roster.entries.get(viewer_phone) if roster else None
        if entry is not None and entry.pos_code:
            if not any(c["pos_code"] == entry.pos_code for c in codes):
                codes.append({"pos_code": entry.pos_code, "bound": False})
    return codes


# ---------------------------------------------------------------------------
# 自绘画布渲染


def _org_css() -> str:
    return (
        "<style>"
        ".hp-wrap{display:flex;gap:14px;align-items:flex-start;}"
        # 首页不受 1060 内容宽度限制：自绘图要铺得开（P17 反馈：左右空档）
        ".lz-wrap{max-width:1680px;}"
        ".hp-left{flex:1;min-width:0;}"
        ".hp-scroll{overflow:auto;border:1px solid #DDE3EA;border-radius:8px;"
        "background:#FBFCFE;max-width:100%;}"
        ".hp-stage{position:relative;overflow:hidden;margin:0 auto;}"
        ".hp-canvas{position:relative;transform-origin:top left;}"
        ".hp-links{position:absolute;left:0;top:0;z-index:0;}"
        ".hp-node{position:absolute;z-index:1;background:#fff;"
        "border:1px solid #C7D2DE;border-radius:6px;padding:5px 8px;"
        "box-sizing:border-box;display:flex;flex-direction:column;"
        "justify-content:center;gap:2px;cursor:pointer;overflow:hidden;}"
        ".hp-node:hover{border-color:#245B78;box-shadow:0 1px 4px rgba(36,91,120,.18);}"
        ".hp-node.on{outline:2px solid #245B78;}"
        ".hp-node.hp-me{border-color:#2F7D68;box-shadow:0 0 0 2px rgba(47,125,104,.22);}"
        ".hp-top{background:#F0F5F9;text-align:center;}"
        ".hp-head{background:#EDF3EE;}"
        ".hp-node-name{font-size:12px;line-height:1.25;color:#1F2D3A;font-weight:600;white-space:nowrap;}"
        ".hp-top .hp-node-name{font-size:13px;}"
        ".hp-node-meta{font-size:11px;color:#647382;display:flex;gap:6px;"
        "align-items:center;white-space:nowrap;}"
        ".hp-n-badge{font-size:10px;line-height:1;color:#fff;border-radius:3px;"
        "padding:2px 5px;font-family:var(--mono,monospace);pointer-events:none;}"
        ".hp-badge-ok{background:#2F7D68;}"
        ".hp-badge-part{background:#B77A1A;}"
        # 无人态灰对齐 doc 00 §3 色板唯一灰 #647382（原 #8A97A5 为偏差值，A3 定夺改齐）
        ".hp-badge-none{background:#647382;}"
        ".hp-me-tag{font-size:10px;color:#2F7D68;font-weight:600;white-space:nowrap;}"
        ".hp-zoombar{display:flex;gap:6px;padding:6px 2px;align-items:center;}"
        ".hp-zbtn{border:1px solid #C7D2DE;background:#fff;border-radius:4px;"
        "padding:2px 9px;font-size:12px;color:#33475B;cursor:pointer;}"
        ".hp-zbtn:hover{border-color:#245B78;color:#245B78;}"
        ".hp-zoomword{font-size:11px;color:#647382;}"
        # 有人/没人一眼可分：有人的组左侧绿边，没人的组变灰虚线
        ".hp-node.hp-live{border-left:3px solid #2F7D68;}"
        ".hp-node.hp-empty{opacity:.62;border-style:dashed;background:#F7F9FB;}"
        ".hp-canvas-note{color:#647382;font-size:12px;padding-top:4px;}"
        ".hp-side{width:340px;flex:none;display:flex;flex-direction:column;gap:10px;}"
        ".hp-card{background:#fff;border:1px solid #DDE3EA;border-radius:6px;padding:10px 12px;}"
        ".hp-card h3{font-size:13px;margin-bottom:6px;color:#1F2D3A;}"
        ".hp-chip{font-size:11px;border:1px solid;border-radius:99px;padding:0 7px;}"
        ".hp-table{width:100%;border-collapse:collapse;font-size:12px;}"
        ".hp-table th,.hp-table td{text-align:left;padding:3px 4px;border-bottom:1px solid #EDF1F5;}"
        ".hp-slot,.hp-missing{display:flex;justify-content:space-between;font-size:12px;padding:2px 0;}"
        ".hp-missing{color:#B77A1A;display:block;}"
        ".hp-total{font-size:13px;padding-bottom:4px;}"
        ".hp-foot{color:#647382;font-size:11px;padding-top:6px;}"
        ".hp-me-row{background:#F0F7F4;font-weight:600;}"
        ".kv,.row,.note{font-size:12px;color:#33475B;}"
        ".kv span{color:#647382;margin-right:6px;}"
        "@media (max-width:900px){.hp-wrap{flex-direction:column}.hp-side{width:auto}}"
        "</style>"
    )


def _panel_footer(generated_at: str) -> str:
    stamp = generated_at or "—"
    return (
        f'<div class="note">通信状态：待接入 · 数据更新 {esc(stamp)} · '
        "岗位结构以系统编制数据为准，本面板不重排不改写</div>"
    )


def _chip(text: str, color: str, basis: str = "") -> str:
    title = f' title="依据：{esc(basis)}"' if basis else ""
    return (
        f'<span class="hp-chip"{title} '
        f'style="color:{color};border-color:{color}">{esc(text)}</span>'
    )


def _slot_panel_html(box, rows, *, viewer_role: str, viewer_phone: str,
                     arrivals, generated_at: str) -> str:
    """编制槽位人员面板（doc 02 §5.1 字段；缺数据源字段如实标「—」）。"""
    if not rows:
        body = (
            '<p class="note">该组暂无名册与到场人员。'
            "请先在「火情事件」页配置名册。</p>"
        )
    else:
        trs = []
        for row in rows:
            me = row["phone"] == viewer_phone
            phone_shown = mask_for_viewer(
                row["phone"], viewer_role=viewer_role,
                viewer_phone=viewer_phone,
            )
            arrival = (arrivals or {}).get(row["phone"], "") or "—"
            tone = _status_tone(row["status_word"])
            trs.append(
                f'<tr class="{esc("hp-me-row") if me else ""}">'
                f'<td>{esc(row["name"])}</td>'
                f'<td>{esc(row["unit"] or "—")}</td>'
                f'<td>{esc(row["home_position"] or "—")}</td>'
                f'<td>{esc(row["group"] or "—")}—{esc(row["slot_name"] or "待指派")}</td>'
                f'<td>{_chip(row["status_word"], tone, row.get("state_basis", ""))}</td>'
                f'<td>{esc(arrival)}</td>'
                f'<td>—（无任务数据源）</td>'
                f'<td>{esc("待接入")}</td>'
                f'<td class="mono">{esc(phone_shown)}</td></tr>'
            )
        body = (
            '<table class="hp-table"><tr><th>姓名</th><th>单位</th>'
            "<th>固定岗位</th><th>工作组—本次岗位</th><th>状态</th>"
            "<th>到场时间</th><th>当前任务</th><th>通信</th><th>联系电话</th></tr>"
            + "".join(trs) + "</table>"
        )
    return (
        f'<p class="note">编制组：{esc(box["label"])}（{esc(box["code"])}）</p>'
        + body + _panel_footer(generated_at)
    )


def _node_panel_html(box, *, generated_at: str) -> str:
    return (
        f'<p class="note">指挥节点：{esc(box["label"])}（{esc(box["code"])}）。'
        "该节点无常设人员槽位，人员编制见下属各组；不虚构人数。</p>"
        + _panel_footer(generated_at)
    )


def _badge_class(stat) -> str:
    arr, exp = stat["arrived"], stat["expected"]
    if exp > 0 and arr >= exp:
        return "ok"
    if arr > 0:
        return "part"
    return "none"


def build_org_canvas(layout: dict, *, registry=None, workspace=None,
                     event_id: str = "", viewer_role: str = "commander",
                     viewer_phone: str = "", arrivals=None,
                     generated_at: str = "", detail_phones=None, projected_rows=None) -> tuple[str, str]:
    """返回 (自绘组织画布 HTML, 右侧联动卡 HTML)。

    - 节点为绝对定位卡片，连线为下层 SVG 直线段；位置全部来自 org_layout()；
    - 名册数据服务端预渲染为隐藏模板，点击仅做前端切换，不新增任何接口；
    - 当前登录人所在组节点描边高亮并在卡内标注「姓名（我）」。
    """
    summary = None
    if event_id and registry is not None and workspace is not None:
        summary = attendance_by_slot(registry, workspace, event_id)
    configured = bool(summary and summary["configured"])
    slot_stat = {s["pos_code"]: s for s in (summary or {}).get("slots", [])}
    all_rows = (
        person_rows_for_group(registry, workspace, event_id)
        if summary is not None else []
    )
    if projected_rows is not None:
        all_rows = list(projected_rows)
        configured = bool(all_rows)
        slot_stat = {slot.pos_code:dict(pos_code=slot.pos_code,
            expected=sum(r.get('pos_code')==slot.pos_code for r in all_rows),
            arrived=sum(r.get('pos_code')==slot.pos_code and r.get('is_arrived',False) for r in all_rows))
            for slot in POSITION_SLOTS if any(r.get('pos_code')==slot.pos_code for r in all_rows)}
    if detail_phones is not None:
        all_rows = [row for row in all_rows if row['phone'] in detail_phones]
    me_codes = {
        a["pos_code"]: a["bound"]
        for a in viewer_assignments(
            registry, workspace, event_id, viewer_phone=viewer_phone
        )
    } if viewer_phone else {}
    if projected_rows is not None and viewer_phone:
        me_codes = {r['pos_code']:r.get('is_arrived',False) for r in all_rows if r['phone']==viewer_phone and r['pos_code']}
    me_name = ""
    if viewer_phone and registry is not None:
        person = registry.library.get(viewer_phone)
        me_name = person.name if person else ""

    cw, ch = layout["canvas_w"], layout["canvas_h"]
    lines = "".join(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        'stroke="#C7D2DE" stroke-width="1.5"/>'
        for x1, y1, x2, y2 in layout["links"]
    )
    nodes: list[str] = []
    templates: list[str] = []
    for idx, box in enumerate(layout["boxes"]):
        cls = ["hp-node"]
        if box["kind"] == "top":
            cls.append("hp-top")
        elif box["kind"] == "head":
            cls.append("hp-head")
        meta_bits: list[str] = []
        if box["kind"] == "slot":
            stat = slot_stat.get(box["code"])
            if stat is not None:
                meta_bits.append(f"编制 {_SLOT_CAP[box['code']]}")
                meta_bits.append(
                    f'<span class="hp-n-badge hp-badge-{_badge_class(stat)}">'
                    f'{stat["arrived"]}/{stat["expected"]}</span>'
                )
                # 一眼分清：有人的组标绿边，没人的组变灰虚线。
                cls.append("hp-live" if stat.get("arrived") else "hp-empty")
            elif configured:
                meta_bits.append(f"编制 {_SLOT_CAP[box['code']]} · 本事件未排班")
            else:
                meta_bits.append(f"编制 {_SLOT_CAP[box['code']]} · 名册未配置")
            if box["code"] in me_codes:
                cls.append("hp-me")
                suffix = "" if me_codes[box["code"]] else " · 未到场"
                meta_bits.append(f'<span class="hp-me-tag">{esc(me_name)}（我{suffix}）</span>')
        elif box["kind"] == "head":
            count = sum(
                1 for b in layout["boxes"]
                if b["kind"] == "slot" and SLOT_INDEX[b["code"]].domain
                == next(d for d, p in _DOMAIN_PARENT.items() if p == box["code"])
            )
            meta_bits.append(f"下属 {count} 组")
            if configured:
                a, e = _branch_totals(slot_stat, box["code"])
                if e:
                    meta_bits.append(f"到场 {a}/{e}")
        inner = (
            f'<div class="hp-node-name">{esc(box["label"])}</div>'
            + (f'<div class="hp-node-meta">{"".join(meta_bits)}</div>' if meta_bits else "")
        )
        me_attr = ' data-me="1"' if "hp-me" in cls else ""
        nodes.append(
            f'<div class="{esc(" ".join(cls))}" role="button" tabindex="0"'
            f'{me_attr} data-panel="hp-panel-{idx}"'
            f' title="{esc(box["label"])}"'
            f' style="left:{box["x"]}px;top:{box["y"]}px;'
            f'width:{box["w"]}px;height:{box["h"]}px">{inner}</div>'
        )
        if box["kind"] == "slot":
            rows = [r for r in all_rows if r.get("pos_code") == box["code"]]
            inner_html = _slot_panel_html(
                box, rows, viewer_role=viewer_role,
                viewer_phone=viewer_phone, arrivals=arrivals,
                generated_at=generated_at,
            )
        else:
            inner_html = _node_panel_html(box, generated_at=generated_at)
        templates.append(
            f'<div class="hp-card" id="hp-panel-{idx}" hidden>{inner_html}</div>'
        )

    canvas = (
        f'<div class="hp-scroll"><div class="hp-stage">'
        f'<div class="hp-canvas" data-w="{cw}" data-h="{ch}" style="width:{cw}px;height:{ch}px">'
        f'<svg class="hp-links" width="{cw}" height="{ch}" '
        f'viewBox="0 0 {cw} {ch}" aria-hidden="true">{lines}</svg>'
        + "".join(nodes)
        + "</div></div></div>"
        '<div class="hp-zoombar">'
        '<button type="button" class="hp-zbtn" data-z="-" aria-label="缩小">−</button>'
        '<button type="button" class="hp-zbtn" data-z="reset" aria-label="恢复自适应">自适应</button>'
        '<button type="button" class="hp-zbtn" data-z="+" aria-label="放大">＋</button>'
        '<span class="hp-zoomword">正在自适应窗口缩放…</span></div>'
    )
    side_card = (
        '<section class="hp-card"><h3>编制组人员（点击上图节点切换）</h3>'
        '<div id="hp-group-body"><p class="note">尚未选择组织节点。</p></div></section>'
        '<div class="hp-templates" hidden>' + "".join(templates) + "</div>"
        + "<script>(function(){"
        "var wrap=document.querySelector('.hp-left');"
        "if(!wrap)return;"
        "var scroller=wrap.querySelector('.hp-scroll'),"
        "stage=wrap.querySelector('.hp-stage'),"
        "canvas=wrap.querySelector('.hp-canvas'),"
        "word=wrap.querySelector('.hp-zoomword');"
        "var w=+canvas.getAttribute('data-w'),h=+canvas.getAttribute('data-h'),z=1,manual=false;"
        "function apply(){canvas.style.transform='scale('+z+')';"
        "stage.style.width=(w*z)+'px';stage.style.height=(h*z)+'px';"
        "if(word)word.textContent='缩放 '+Math.round(z*100)+'%（'+(manual?'手动':'自适应窗口')+'）';}"
        "function fitOrg(){manual=false;"
        "var availW=scroller.clientWidth-2;"
        "var availH=window.innerHeight-stage.getBoundingClientRect().top-56;"
        "if(availH<320)availH=320;"
        "var s=Math.min(availW/w,availH/h);"
        # 默认至少 100%：宁可横向滚动，也不要把 32 个组缩成看不清的小字
        "z=Math.max(1,Math.min(2,s));apply();}"
        "fitOrg();"
        "window.addEventListener('resize',function(){if(!manual)fitOrg();});"
        "wrap.addEventListener('click',function(ev){"
        "var zbtn=ev.target.closest('.hp-zbtn');"
        "if(zbtn){var d=zbtn.getAttribute('data-z');"
        "if(d==='reset'){fitOrg();return;}"
        "manual=true;z=Math.max(0.4,Math.min(3,z+(d==='+'?0.25:-0.25)));apply();return;}"
        "var hit=ev.target.closest('.hp-node');if(!hit)return;"
        "var tpl=document.getElementById(hit.getAttribute('data-panel'));"
        "var body=document.getElementById('hp-group-body');"
        "if(!tpl||!body)return;"
        "var c=tpl.cloneNode(true);c.removeAttribute('hidden');c.removeAttribute('id');"
        "body.replaceChildren(c);"
        "canvas.querySelectorAll('.hp-node').forEach(function(n){"
        "n.classList.toggle('on',n===hit);});});"
        "wrap.addEventListener('keydown',function(ev){"
        "if(ev.key!=='Enter')return;"
        "var hit=ev.target.closest('.hp-node');if(!hit)return;"
        "hit.click();});})();</script>"
    )
    return canvas, side_card


def _branch_totals(slot_stat: dict, head_code: str) -> tuple[int, int]:
    """某条线下属组的到场合计（仅统计有名册数据的组）。"""
    domain = next(d for d, p in _DOMAIN_PARENT.items() if p == head_code)
    arrived = expected = 0
    for s in slot_stat.values():
        if SLOT_INDEX[s["pos_code"]].domain == domain:
            arrived += s["arrived"]
            expected += s["expected"]
    return arrived, expected


#: 编制上限速查（org_model POSITION_SLOTS 的只读投影，供节点直显）。
_SLOT_CAP: dict[str, int] = {s.pos_code: s.max_headcount for s in POSITION_SLOTS}

assert len(COMMAND_NODES) == 8, "自绘布局假定 8 个指挥节点"


# ---------------------------------------------------------------------------
# 渲染


def render_homepage_view(
    *,
    event_record=None,
    registry=None,
    workspace=None,
    event_id: str = "",
    viewer_phone: str = "",
    viewer_role: str = "commander",
    arrivals=None,
    generated_at: str = "",
    detail_phones=None,
) -> str:
    """首页：数据驱动自绘组织架构（V0.3），无任何图片依赖。"""
    layout = org_layout()
    canvas_html, group_card_html = build_org_canvas(
        layout,
        registry=registry,
        workspace=workspace,
        event_id=event_id,
        viewer_role=viewer_role,
        viewer_phone=viewer_phone,
        arrivals=arrivals,
        generated_at=generated_at,
        detail_phones=detail_phones,
    )
    parts = ['<div class="hp-wrap">', _org_css()]
    parts.append('<div class="hp-left">' + canvas_html)
    parts.append(
        '<div class="hp-canvas-note">组织架构由系统按岗位编制数据自绘'
        "（联合指挥部→前线总指挥→六条线→32 个一线组）；"
        "点击节点查看该组人员名单与到场概览。</div></div>"
    )

    parts.append('<div class="hp-side">')
    if group_card_html:
        parts.append(group_card_html)
    if viewer_role == "member":
        detail = viewer_detail(
            registry, workspace, viewer_phone=viewer_phone, arrivals=arrivals
        ) if registry is not None else None
        if detail is not None:
            if detail.get("state_word"):
                state_chip = _chip(
                    detail["state_word"],
                    _status_tone(detail["state_word"]),
                    detail.get("state_basis", ""),
                )
            else:
                state_chip = _chip(
                    "已到场" if detail["bound"] else "未到场",
                    "#2F7D68" if detail["bound"] else "#647382",
                )
            arrival_line = (
                f'<div class="kv"><span>到场时间</span><b>{esc(detail["arrival"] or "—")}</b></div>'
            )
            parts.append(
                '<section class="hp-card hp-me"><h3>我的岗位信息</h3>'
                f'<div class="row"><b>{esc(detail["name"])}</b>{state_chip}</div>'
                f'<div class="kv"><span>单位</span><b>{esc(detail["unit"] or "—")}</b></div>'
                f'<div class="kv"><span>固定岗位</span><b>{esc(detail["home_position"] or "—")}</b></div>'
                + (
                    f'<div class="kv"><span>本次岗位</span>'
                    f"<b>{esc(detail['group'])}—{esc(detail['slot_name'])}</b></div>"
                    if detail["bound"]
                    else ""
                )
                + arrival_line
                + f'<div class="kv"><span>联系电话</span><b>{esc(detail["phone_full"])}</b></div>'
                f'<div class="note">{esc(detail["comm"])}</div>'
                "</section>"
            )
        else:
            parts.append(
                '<section class="hp-card"><h3>我的岗位信息</h3>'
                '<p class="note">当前账号不在长期人员库：转人工补录。</p></section>'
            )

    if event_record is not None and registry is not None and workspace is not None:
        summary = attendance_by_slot(registry, workspace, event_id)
        totals = summary["totals"]
        if summary["configured"]:
            rate = (
                f"{totals['arrived']}/{totals['expected']}"
                if totals["expected"]
                else "—"
            )
            parts.append(
                '<section class="hp-card"><h3>到场总览</h3>'
                f'<div class="hp-total">实到 <b>{totals["arrived"]}</b> / 应到 '
                f"<b>{totals['expected']}</b>（{esc(rate)}）</div>"
            )
            if summary["unassigned"]:
                parts.append(
                    f'<p class="note">另有 {summary["unassigned"]} 人名册岗位未解析，'
                    "待指挥部指派。</p>"
                )
            for slot in summary["slots"]:
                color = "#2F7D68" if slot["arrived"] >= slot["expected"] else "#B77A1A"
                pct = f'{slot["rate"] * 100:.0f}%' if slot["rate"] is not None else "—"
                parts.append(
                    '<div class="hp-slot"><span class="nm">'
                    f'{esc(slot["name"])}</span>'
                    f'<span class="num">{slot["arrived"]}/{slot["expected"]}'
                    f'（{esc(pct)}）</span>{_chip("到场", color) if slot["arrived"] else _chip("未到场", "#647382")}</div>'
                )
                if slot["missing_names"] and detail_phones is None:
                    preview = "、".join(slot["missing_names"][:4])
                    more = f" 等{len(slot['missing_names'])}人" if len(slot["missing_names"]) > 4 else ""
                    parts.append(
                        f'<div class="hp-missing">未到场：{esc(preview)}{esc(more)}</div>'
                    )
            if summary["out_of_roster"]:
                parts.append('<p class="note hp-warn">名册外到场（需人工核实）：</p>')
                for extra in summary["out_of_roster"]:
                    if detail_phones is not None and extra['phone'] not in detail_phones:
                        continue
                    parts.append(
                        f'<div class="hp-missing">{esc(extra["name"])} — {esc(extra["reason"])}'
                        f'（{esc(extra["slot_name"])}）</div>'
                    )
            parts.append("</section>")
            # 右侧人员名单（doc 02 §5.1）：联系方式按观看者身份脱敏
            rows = person_rows_for_group(registry, workspace, event_id)
            parts.append(
                '<section class="hp-card"><h3>人员名单</h3>'
                '<table class="hp-table"><tr><th>姓名</th><th>岗位</th>'
                '<th>状态</th><th>联系电话</th></tr>'
            )
            for row in rows:
                if detail_phones is not None and row['phone'] not in detail_phones:
                    continue
                phone_shown = mask_for_viewer(
                    row["phone"], viewer_role=viewer_role, viewer_phone=viewer_phone
                )
                parts.append(
                    f'<tr><td>{esc(row["name"])}</td>'
                    f'<td>{esc(row["slot_name"] or "待指派")}</td>'
                    f'<td>{_chip(row["status_word"], _status_tone(row["status_word"]), row.get("state_basis", ""))}</td>'
                    f'<td class="mono">{esc(phone_shown)}</td></tr>'
                )
            parts.append("</table></section>")
        else:
            parts.append(
                '<section class="hp-card"><h3>到场总览</h3>'
                '<p class="note">本事件名册未配置：请指挥部先在「火情事件」页配置名册。</p></section>'
            )

    parts.append(f'<div class="hp-foot">{esc(COMM_PENDING_NOTE)}')
    if generated_at:
        parts.append(f'<span> · 数据更新 {esc(generated_at)}</span>')
    parts.append("</div></div></div>")
    return "".join(parts)
