"""Front-command workbench renderers (P15 T4) — the map-first surfaces.

Pure rendering helpers over the live workspace: every renderer takes
(PanelState, session_token, query) plus data and returns an HTML fragment;
panel.py only wires routes. Mutations live in PanelState and flow through the
SAME workspace gates as every other command act — nothing here writes.

Visual baseline: design_demos/ui_c_战术地图.html (direction C) — sand ground
#ddd6c4, ink text #2f3327, signal red #a63c2a, floating panels
rgba(247,244,235,.94), bottom process bar. The SVG base map is a SCHEMATIC
sketch (contours / river / road are drawn, not surveyed); the dynamic layer is
derived deterministically from workspace data and always carries the honest
"示意图 · 非实测坐标" badge.
"""

from __future__ import annotations

import html as _html
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .fire_calculus import (
    estimate_from_roster_headcount,
    load_material_baseline,
    material_requirements,
)
from .models import TERMINAL_ORDER_STATUSES, PersonRecord
from .org_model import POSITION_SLOTS, SLOT_INDEX, mask_phone, normalize_phone
from .situation_score import default_scorer

#: Material baseline used by the pump-relay requirement list (existing config).
MATERIAL_BASELINE_PATH = Path(__file__).resolve().parents[1] / "config" / "material_baseline.yaml"
#: Snapshot directory for the drill console (created on first save).
SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"

#: Fixed drill parameters of the staff estimation card: the handbook worked
#: example (500 m fireline spreading at 100 m/h, crews fold 30 heads each).
DRILL_FIRELINE_M = 500.0
DRILL_SPREAD_M_H = 100.0
CREW_STANDARD_SIZE = 30


# ---------------------------------------------------------------------------
# Plain-words layer (白话层)


_PLAIN_WORDS: dict[str, str] = {
    "事实账": "事实账（claim）：系统当前采信的每条战场事实，带来源与置信度，可被新观测推翻",
    "请示": "请示：参谋部或执行环节卡住时向你提的问题，答复前相关动作挂起",
    "决断简报": "决断简报：参谋部把问题、方案对比和推荐压缩成一页，供你拍板",
    "对比矩阵": "对比矩阵：把各方案的前提满足情况、成本、可逆性并排比较的表",
    "目标框": "目标框：本仗要达成什么、边界在哪，你确认后方案才能往下走",
    "记忆候选": "记忆候选：复盘发现的待沉淀经验，经你确认后才算数",
    "审计链": "审计链：全部事件按顺序哈希串联，改过任何一条都会被查出",
    "预备队": "预备队：留着机动补救的力量；条令要求一线投入越多、预备队备得越足",
    "态势评分": "态势评分：按固定权重把风、火、路、水条件折算成的 0～100 严重度分",
}


def esc(value) -> str:
    return _html.escape(str(value), quote=True)


def plain_note(*keys: str) -> str:
    """Small-print jargon translations rendered under view titles."""
    items = " ".join(
        f"<b>{esc(key)}</b>＝{esc(_PLAIN_WORDS[key])}" for key in keys if key in _PLAIN_WORDS
    )
    return f'<div class="plain-note">白话：{items}</div>' if items else ""


# ---------------------------------------------------------------------------
# Pure inference over workspace state


def infer_process_step(workspace) -> int:
    """①组织匹配→②态势共享→③资源调配→④任务下达→⑤复盘归档。

    The current step is inferred from the deepest trace the workspace already
    carries (reviews > live orders > briefs > roster bindings). Pure function,
    testable without HTTP. Advisory only — never a gate.
    """
    if workspace.reviews:
        return 5
    if any(o.status not in TERMINAL_ORDER_STATUSES for o in workspace.orders.values()):
        return 4
    if workspace.decision_briefs:
        return 3
    if any(b.status == "active" for b in workspace.roster_bindings.values()):
        return 2
    return 1


def combat_headcount(workspace) -> int:
    """Assault + air members currently checked in — crew-folding input."""
    count = 0
    for binding in workspace.roster_bindings.values():
        slot = SLOT_INDEX.get(binding.pos_code)
        if binding.status == "active" and slot is not None and slot.kind in {"assault", "air"}:
            count += 1
    return count


def pending_counts(workspace) -> dict[str, int]:
    """Counts behind the map's right-top pending card (links to live views)."""
    return {
        "questions": sum(1 for q in workspace.general_questions.values() if q.status == "open"),
        "goals": sum(1 for g in workspace.goal_frames.values() if g.confirmed_by is None),
        "orders": sum(
            1 for o in workspace.orders.values() if o.status not in TERMINAL_ORDER_STATUSES
        ),
    }


_BASELINE_CACHE = None


def _baseline():
    global _BASELINE_CACHE
    if _BASELINE_CACHE is None:
        _BASELINE_CACHE = load_material_baseline(MATERIAL_BASELINE_PATH)
    return _BASELINE_CACHE


def staff_estimate(workspace) -> dict:
    """Deterministic 参谋测算 inputs: roster heads folded into standard crews.

    Fireline length and spread rate are FIXED drill parameters (the handbook
    worked example); crews come from assault+air attendance folded by 30. The
    pump-relay material list uses the SAME crew count, hours rounded up from
    the estimate (0 when unwinnable, dropping consumables).
    """
    headcount = combat_headcount(workspace)
    estimate = estimate_from_roster_headcount(
        headcount, DRILL_FIRELINE_M, DRILL_SPREAD_M_H
    )
    materials = None
    if estimate.crews > 0:
        hours = math.ceil(estimate.hours_required) if estimate.hours_required else 0
        materials = material_requirements(
            _baseline(), "pump-relay", crews=estimate.crews, hours=hours
        )
    return {"headcount": headcount, "estimate": estimate, "materials": materials}


# ---------------------------------------------------------------------------
# Snapshots (drill console)


def snapshot_stamp() -> str:
    """UTC stamp safe for Windows filenames (no colons)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def scenario_slug(scenario_id: str) -> str:
    """Filename-safe slug, same discipline as the report export route."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scenario_id)


def list_snapshots(directory: str | Path | None = None) -> list[dict]:
    """*.db snapshots newest-first (mtime desc); empty when dir absent."""
    folder = Path(directory) if directory is not None else SNAPSHOT_DIR
    if not folder.exists():
        return []
    entries = [
        {"name": path.name, "mtime": path.stat().st_mtime, "size": path.stat().st_size}
        for path in folder.glob("*.db")
    ]
    return sorted(entries, key=lambda entry: entry["mtime"], reverse=True)


def parse_roster_csv(text: str) -> tuple[list[PersonRecord], list[str]]:
    """Parse pasted CSV rows ``姓名,手机号,单位,本职岗位,是否队长``.

    Returns (records, row_errors). All-or-nothing is enforced upstream: any
    row error blocks the whole batch so partial rosters never slip in.
    """
    records: list[PersonRecord] = []
    errors: list[str] = []
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            errors.append(f"第{lineno}行：至少需要「姓名,手机号」两列。")
            continue
        name, phone_raw = parts[0], parts[1]
        unit = parts[2] if len(parts) > 2 else ""
        home_position = parts[3] if len(parts) > 3 else ""
        leader_flag = parts[4] if len(parts) > 4 else ""
        if not name:
            errors.append(f"第{lineno}行：姓名不能为空。")
            continue
        try:
            phone = normalize_phone(phone_raw)
        except ValueError as exc:
            errors.append(f"第{lineno}行：{exc}")
            continue
        records.append(
            PersonRecord(
                name=name,
                phone=phone,
                unit=unit,
                home_position=home_position,
                specialty="",
                is_leader=leader_flag in {"是", "队长", "Y", "y", "yes", "true", "1"},
            )
        )
    return records, errors


# ---------------------------------------------------------------------------
# 态势一张图 (/map)


_STEP_LABELS = ("组织匹配", "态势共享", "资源调配", "任务下达", "复盘归档")
_STEP_NUMERALS = ("①", "②", "③", "④", "⑤")

_UNIT_MARKS = {
    # pos_code -> fixed schematic anchor (x, y); one glyph per group on duty.
    "pjjz-1": (430, 370),
    "pjjz-2": (690, 470),
    "pjjz-3": (935, 400),
    "pjjz-4": (1080, 300),
    "air-op": (640, 150),
    "mobile-support": (240, 175),
}
_HQ_MARK = (95, 95)
_FIRE_LABEL_XY = (945, 185)


def render_process_bar(step: int) -> str:
    """Bottom five-step bar; done steps ticked, current step highlighted."""
    bits = []
    for index in range(5):
        number = index + 1
        cls = "done" if number < step else ("on" if number == step else "")
        mark = " ✓" if number < step else ""
        bits.append(
            f'<span class="wstep {cls}">'
            f"{_STEP_NUMERALS[index]} {_STEP_LABELS[index]}{mark}</span>"
        )
    return '<div class="wsteps">' + "".join(bits) + "</div>"


def _svg_base() -> str:
    """Static schematic underlay: contours, river W1, road R1, protected town."""
    return (
        '<g stroke="#c9c1aa" fill="none" stroke-width="1.5">'
        '<path d="M-50,180 Q250,120 550,190 T1450,150"/>'
        '<path d="M-50,320 Q300,240 650,330 T1450,280"/>'
        '<path d="M-50,520 Q350,430 750,530 T1450,470"/>'
        '<path d="M-50,690 Q400,620 850,700 T1450,640"/>'
        '<path d="M200,-50 Q260,200 180,420 T300,810"/>'
        '<path d="M900,-50 Q830,260 960,480 T880,810"/>'
        "</g>"
        '<path d="M60,780 Q110,600 85,420 Q70,280 130,40" stroke="#6f93ac" '
        'fill="none" stroke-width="7" opacity=".85"/>'
        '<text x="48" y="742" fill="#4a6d8c" font-size="14">W1 水源</text>'
        '<path d="M-20,560 L340,535 L720,585 L1080,555 L1420,575" stroke="#8b8672" '
        'stroke-width="4" stroke-dasharray="14 8" fill="none"/>'
        '<text x="1150" y="542" fill="#5d6149" font-size="14">R1 道路（通行受限）</text>'
        '<rect x="1180" y="380" width="26" height="26" fill="#5d6149"/>'
        '<text x="1214" y="398" fill="#2f3327" font-size="15" font-weight="bold">'
        "青岗镇（保护目标）</text>"
    )


def _svg_fire_layer(workspace) -> str:
    """Burned-area sketch anchored to fire-related claims (deterministic)."""
    fire_claims = [
        claim
        for subject, claim in sorted(workspace.claims.items())
        if subject.lower().startswith("fire")
    ]
    parts = [
        '<path d="M540,180 L760,215 L920,175 L1030,245 L880,315 L660,270 Z" '
        'fill="#c99f83" opacity=".8"/>',
        '<path d="M540,180 L760,215 L920,175 L1030,245 L880,315 L660,270 Z" '
        'fill="none" stroke="#a63c2a" stroke-width="4"/>',
        '<circle cx="925" cy="205" r="11" fill="#a63c2a"/>',
        '<circle cx="925" cy="205" r="20" fill="none" stroke="#a63c2a" stroke-width="2" '
        'opacity=".5"/>',
        '<text x="600" y="255" fill="#7a4636" font-size="13">火烧迹地</text>',
    ]
    x, y = _FIRE_LABEL_XY
    if fire_claims:
        tspans = "".join(
            f'<tspan x="{x}" dy="{18 if i else 0}">'
            f"{esc(c.subject)}={esc(c.value)} · {_status_word(c.status.value)}"
            f" {c.confidence:.2f}</tspan>"
            for i, c in enumerate(fire_claims[:3])
        )
        parts.append(
            f'<text x="{x}" y="{y}" fill="#a63c2a" font-size="14" font-weight="bold">'
            "火情标注（据事实账）</text>"
        )
        parts.append(f'<text fill="#a63c2a" font-size="12.5">{tspans}</text>')
    else:
        parts.append(
            f'<text x="{x}" y="{y}" fill="#a63c2a" font-size="13" font-weight="bold">'
            "尚无火相关事实记录</text>"
        )
    return "".join(parts)


def _status_word(status: str) -> str:
    return {
        "verified": "已核实",
        "observed": "已观测",
        "hypothesis": "假设",
        "challenged": "受质疑",
        "superseded": "已被取代",
        "unknown": "未知",
    }.get(status, status)


def _svg_units(workspace) -> str:
    """One glyph per assault/air/reserve group with attendance, HQ fixed."""
    counts = Counter(
        b.pos_code for b in workspace.roster_bindings.values() if b.status == "active"
    )
    parts = []
    for code, (x, y) in sorted(_UNIT_MARKS.items()):
        slot = SLOT_INDEX.get(code)
        present = counts.get(code, 0)
        if slot is None or present <= 0:
            continue
        reserve = slot.kind == "reserve"
        color = "#8b8672" if reserve else "#4a5240"
        text_fill = "#5d6149" if reserve else "#33402a"
        parts.append(
            f'<g class="unit"><rect x="{x}" y="{y}" width="16" height="16" '
            f'fill="{color}"/><text x="{x + 22}" y="{y + 13}" font-size="13" '
            f'font-weight="bold" fill="{text_fill}">{esc(slot.name)}·{present}人</text></g>'
        )
    hx, hy = _HQ_MARK
    parts.append(
        f'<g><rect x="{hx}" y="{hy}" width="20" height="20" fill="none" '
        f'stroke="#33402a" stroke-width="4"/>'
        f'<text x="{hx + 27}" y="{hy + 16}" font-size="14" font-weight="bold" '
        f'fill="#33402a">前指</text></g>'
    )
    return "".join(parts)


def _legend() -> str:
    items = (
        ('#a63c2a', "火线/火头"),
        ('#c99f83', "火烧迹地"),
        ('#4a5240', "作战队伍"),
        ('#8b8672', "预备力量"),
        ('#6f93ac', "水源"),
    )
    spans = "".join(
        f'<span><i class="lg" style="background:{color}"></i>{esc(label)}</span>'
        for color, label in items
    )
    return f'<div class="wlegend">{spans}</div>'


def _map_summary_panel(workspace) -> str:
    """Left-top floating panel: situation digest reusing the overview scorer."""
    score = default_scorer().score(workspace.claims)
    total = f"{score.total:.0f}" if score.level == "available" else "—"
    claim_rows = "".join(
        f'<div class="wtodo" style="cursor:default"><span class="wdot amber"></span>'
        f'<span class="tx mono">{esc(c.subject)}={esc(c.value)} · '
        f"{_status_word(c.status.value)} {c.confidence:.2f}</span></div>"
        for subject, c in sorted(workspace.claims.items())[:5]
    ) or '<div class="wtodo"><span class="tx">尚无事实记录。</span></div>'
    orders_live = pending_counts(workspace)["orders"]
    return (
        '<div class="wpanel p-sum"><h3>态势研判</h3><div class="body">'
        f'<div class="metrics"><div class="m hot"><div class="v">{esc(total)}</div>'
        '<div class="l">态势评分</div></div>'
        f'<div class="m"><div class="v">{len(workspace.claims)}</div><div class="l">事实条目</div></div>'
        f'<div class="m"><div class="v">{combat_headcount(workspace)}</div><div class="l">一线实到</div></div>'
        f'<div class="m"><div class="v">{orders_live}</div><div class="l">在途命令</div></div>'
        '</div><div class="claim-digest">' + claim_rows + "</div></div></div>"
    )


def _map_todo_panel(workspace) -> str:
    """Right-top floating card linking into the existing decision views."""
    counts = pending_counts(workspace)
    rows = (
        (
            "red",
            f"待答请示 · {counts['questions']} 项",
            "/questions",
        ),
        (
            "amber",
            f"待确认目标框 · {counts['goals']} 项",
            "/brief",
        ),
        (
            "green",
            f"在途命令 · {counts['orders']} 项",
            "/orders",
        ),
    )
    body = "".join(
        f'<a class="wtodo" href="{href}"><span class="wdot {tone}"></span>'
        f"<span class=\"tx\">{esc(label)}</span><span class=\"go\">查看 →</span></a>"
        for tone, label, href in rows
    )
    total = sum(counts.values())
    return (
        f'<div class="wpanel p-todo"><h3>待你决断 · {total} 项</h3>{body}</div>'
    )


def render_map_view(state, session_token=None, query=None) -> str:
    """态势一张图: schematic SVG + floating panels + process bar. Read-only."""
    ws = state.workspace
    step = infer_process_step(ws)
    parts = [
        plain_note("事实账", "态势评分", "预备队"),
        '<div class="tactical-stage">',
        '<svg class="mapbase" viewBox="0 0 1400 760" preserveAspectRatio="xMidYMid slice">',
        '<rect width="1400" height="760" fill="#ddd6c4"/>',
        _svg_base(),
        _svg_fire_layer(ws),
        _svg_units(ws),
        "</svg>",
        _map_summary_panel(ws),
        _map_todo_panel(ws),
        '<div class="wbadge">示意图 · 非实测坐标</div>',
        _legend(),
        render_process_bar(step),
        "</div>",
        f'<p class="note">流程推断：当前处于第 {step} 步'
        f"（{_STEP_NUMERALS[step - 1]} {_STEP_LABELS[step - 1]}），由工作区痕迹推定，仅作引导。</p>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# 编成视图 (/org)


def _org_slot_rows(slot: dict, command_mode: bool) -> str:
    filled = slot["filled"]
    tone = "chip-ok" if filled else "chip-dim"
    state_word = "有到岗" if filled else "空缺"
    rows = [
        f'<li class="slotrow"><span class="chip {tone}">{esc(slot["name"])} '
        f'{state_word} {filled}/{slot["max_headcount"]}</span></li>'
    ]
    for member in slot["members"]:
        phone = member["phone"] if command_mode else mask_phone(member["phone"])
        leader = ' <span class="chip chip-ok">队长</span>' if member["is_leader"] else ""
        unit = f" · {esc(member['unit'])}" if member.get("unit") else ""
        rows.append(
            f'<li class="memberline">{esc(member["name"])} '
            f'<span class="mono">{esc(phone)}</span>{unit}{leader}</li>'
        )
    return "".join(rows)


def _org_node(node, nodes_by_code: dict, command_mode: bool) -> str:
    children = sorted(
        (n for n in nodes_by_code.values() if n["parent"] == node["node_code"]),
        key=lambda n: n["node_code"],
    )
    inner = "".join(_org_node(child, nodes_by_code, command_mode) for child in children)
    slot_rows = "".join(_org_slot_rows(slot, command_mode) for slot in node["slots"])
    return (
        f'<li><b class="nodename">{esc(node["name"])}</b>'
        f"<ul>{slot_rows}{inner}</ul></li>"
    )


def _org_command_forms(state) -> str:
    """Check-in / check-out / library import — command mode only."""
    ws = state.workspace
    chart = ws.org_chart()
    filled_by_pos = {}
    for node in chart["nodes"]:
        for slot in node["slots"]:
            filled_by_pos[slot["pos_code"]] = slot["filled"]

    library_options = "".join(
        f'<option value="{esc(person.phone)}">'
        f"{esc(person.name)} {esc(person.phone)}（{esc(person.unit or '未填单位')}）</option>"
        for _, person in sorted(ws.person_library.items())
    )
    slot_options = "".join(
        f'<option value="{esc(slot.pos_code)}">'
        f"{esc(slot.name)}（{filled_by_pos.get(slot.pos_code, 0)}/{slot.max_headcount}）</option>"
        for slot in POSITION_SLOTS
    )
    active_bindings = sorted(
        (b for b in ws.roster_bindings.values() if b.status == "active"),
        key=lambda b: b.id,
    )
    checkout_options = "".join(
        f'<option value="{esc(b.pos_code)}|{esc(b.person.phone)}">'
        f"{esc(b.person.name)} @ {esc(SLOT_INDEX[b.pos_code].name)}</option>"
        for b in active_bindings
    )
    checkout_form = (
        "<form class='stack' method='post' action='/drill/checkout'>"
        "<label class='fld'>选择在岗人员离岗<select name='binding'>"
        f"{checkout_options}</select></label>"
        "<button type='submit'>确认离岗</button></form>"
        if checkout_options
        else '<p class="note">当前无在岗人员。</p>'
    )
    return (
        "<section><h2>指挥操作 · 花名册</h2>"
        "<form class='stack' method='post' action='/drill/checkin'>"
        "<label class='fld'>人员库成员（模拟扫码）<select name='phone'>"
        f"{library_options}</select></label>"
        "<label class='fld'>岗位槽位<select name='pos_code'>"
        f"{slot_options}</select></label>"
        "<button type='submit'>模拟扫码签到</button></form>"
        + checkout_form
        + "</section>"
        "<section><h2>常备人员库导入</h2>"
        '<p class="note">粘贴 CSV 行，列序：姓名,手机号,单位,本职岗位,是否队长（是否队长填 是/否）。'
        "任一行有误则整批不导入。</p>"
        "<form class='stack' method='post' action='/drill/import-roster'>"
        "<label class='fld'>CSV 行<textarea name='roster_csv' rows='6' "
        "placeholder='张三,13912342876,县防火办,扑救员,是'></textarea></label>"
        "<button type='submit'>导入人员库</button></form></section>"
    )


def render_org_view(state, session_token=None, query=None) -> str:
    """编成视图: doctrine org tree with slot states; forms only in command mode."""
    ws = state.workspace
    command_mode = state.command_mode(session_token)
    chart = ws.org_chart()
    posture = ws.force_posture_report()
    nodes_by_code = {node["node_code"]: node for node in chart["nodes"]}
    root = next(node for node in chart["nodes"] if node["parent"] is None)
    posture_line = "；".join(posture["notes"])
    parts = [
        "<section><h2>编成架构树</h2>",
        plain_note("预备队"),
        f'<p class="note">实到 {chart["total_members"]} 人 · 已用槽位 '
        f'{chart["filled_slots"]}/{chart["total_slots"]}。灰＝空缺，绿＝有到岗。'
        f"电话对观察态脱敏。</p>"
        f'<p class="note">力量格局提示（参谋参考，非门槛）：{esc(posture_line)}</p>',
        '<div class="orgtree"><ul>',
        _org_node(root, nodes_by_code, command_mode),
        "</ul></div>",
    ]
    if command_mode:
        parts.append(_org_command_forms(state))
    else:
        parts.append('<p class="note">锁定态：编成只读可见，签到/导入需入口认证。</p>')
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 演练控制台 (/drill) — command-mode only (panel gates GET with 404)


def render_drill_view(state, session_token=None, query=None) -> str:
    """新建演练 / 快照保存装载 / 参谋测算卡."""
    ws = state.workspace
    card = staff_estimate(ws)
    estimate = card["estimate"]
    suppressible = estimate.verdict == "suppressible"
    verdict_chip = (
        '<span class="chip chip-ok">可合围</span>'
        if suppressible
        else '<span class="chip chip-bad">无法合围</span>'
    )
    material_html = ""
    if card["materials"] is not None:
        mats = card["materials"]
        equipment_rows = "".join(
            f"<tr><td>{esc(entry['item'])}</td><td>{esc(entry['unit'])}</td>"
            f"<td class='num'>{entry['qty']}</td></tr>"
            for entry in mats["equipment"]
        )
        consumable_rows = "".join(
            f"<tr><td>{esc(entry['item'])}（每小时消耗）</td><td>{esc(entry['unit'])}</td>"
            f"<td class='num'>{entry['qty']}</td></tr>"
            for entry in mats["consumables"]
        )
        material_html = (
            f"<p class='note'>水泵接力物资需求（{mats['crews']} 个分队同数配备，"
            f"依据：{esc(mats['basis'])}）</p>"
            "<table><tr><th>装备/消耗</th><th>单位</th><th class='num'>数量</th></tr>"
            + equipment_rows
            + consumable_rows
            + "</table>"
        )
    snapshots = list_snapshots()
    snapshot_rows = "".join(
        f"<tr><td class='mono'>{esc(entry['name'])}</td>"
        f"<td class='num'>{entry['size']}</td>"
        "<td><form class='inline' method='post' action='/drill/load'>"
        f"<input type='hidden' name='snapshot' value='{esc(entry['name'])}'>"
        "<button type='submit'>装载</button></form></td></tr>"
        for entry in snapshots
    )
    snapshot_table = (
        "<table><tr><th>快照文件（新→旧）</th><th class='num'>字节</th><th>操作</th></tr>"
        + snapshot_rows
        + "</table>"
        if snapshots
        else '<p class="note">尚无快照。保存后此处按时间倒序列出 impl/snapshots/*.db。</p>'
    )
    parts = [
        "<section><h2>演练控制台</h2>",
        plain_note("预备队", "审计链"),
        f'<div class="row"><span class="kv">当前场景</span>'
        f'<span class="mono id">{esc(ws.scenario_id)}</span>'
        f"<span class='dim'>版本 v{ws.version} · 花名册实到 "
        f"{ws.org_chart()['total_members']} 人</span></div>",
        "<form class='inline' method='post' action='/drill/reset'>"
        "<button type='submit'>新建演练（重置为 ForestFireScenario 工作区）</button>"
        "<span class='dim'> 当前工作区即刻被替换，未保存内容丢失；请先存快照。</span></form>",
        "</section>",
        "<section><h2>参谋测算卡</h2>",
        f'<div class="row"><span class="kv">测算口径</span>'
        f"<span class='mono dim'>火线 {DRILL_FIRELINE_M:g} 米 · 蔓延 "
        f"{DRILL_SPREAD_M_H:g} 米/小时 · 分队折算 {CREW_STANDARD_SIZE} 人/个</span></div>",
        f'<div class="row"><span class="kv">一线实到</span>'
        f"<span>{card['headcount']} 人 → {estimate.crews} 个分队</span>{verdict_chip}</div>",
        f'<div class="score">{esc(estimate.explanation)}</div>',
        material_html,
        "</section>",
        "<section><h2>快照管理</h2>",
        "<form class='inline' method='post' action='/drill/save'>"
        "<button type='submit'>保存快照到 impl/snapshots/</button></form><p></p>",
        snapshot_table,
        "</section>",
    ]
    return "".join(parts)
