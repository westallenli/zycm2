"""指挥工作台沙盘页（P18，docs/18_沙盘指挥页开发设计_V1.0）。

排版定版（§3）：全屏真实地图主区 + 左侧工具栏 + 右侧「参谋对话框／方案推演区」
+ 底部对象条 + 抽屉承载完整业务表单。角色三档不变：
commander 全量作业 / member 只读＋问询 / public 公共屏仅已发布。

红线（§2.3）：
- 权力链 AdvisorProposal → GeneralDecision → ExecutionOrder → Policy/Approval → Executor；
- 正式态势不被草稿/候选/推演静默改写；
- 无坐标不猜测上图，底图不可用必须显式说明；
- 前台零 AI 自述（助手只说业务结果）。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from .workbench import esc, pending_counts
from . import sandtable, grounded_inquiry, deepseek_qa
from .map_annotations import (
    AnnotationKind,
    AnnotationStatus,
    AnnotationSource,
    kind_label,
    status_word,
)
from .models import (
    GENERAL_ENTRY_ID,
    DecisionType,
    FeasibilityStatus,
    InformationGapStatus,
    TERMINAL_ORDER_STATUSES,
)
from . import sand_sim
from . import tactical_map as tm
from . import situation_monitor as sm
from . import offline_resources as _offline_resources
from . import local_satellite as _local_satellite
from . import intelligence as _intelligence
from .sandtable import STAGES
from .sand_shell import render_shell_navigation
from .sop import render_early_response, render_situation, render_force, force_review_cue
from .task_sop import render_deployment
from .execution_sop import render_execution
from .cleanup_sop import render_cleanup
from .emergency_sop import render_emergency
from .publication_sop import render_publication
from .handover_sop import render_handover
from .radio_followups import followup_board
from .source_review import render_block as render_source_review

_COMM_PENDING = "通信状态：待接入"
_ALARM_PENDING = "未读 P0/P1 告警：—（告警通道待接入）"

#: 决断类型前台词。
_DECISION_WORDS = {
    DecisionType.ADOPT: "采纳",
    DecisionType.REJECT: "否决",
    DecisionType.MODIFY: "修改",
    DecisionType.DEFER: "暂缓",
    DecisionType.PROPOSE: "自提",
    DecisionType.REQUEST_RESEARCH: "要求补查",
    DecisionType.PAUSE: "暂停",
    DecisionType.CANCEL: "取消",
}

_FEASIBILITY_WORDS = {
    FeasibilityStatus.FEASIBLE: "可行",
    FeasibilityStatus.INFEASIBLE: "不可行",
    FeasibilityStatus.UNKNOWN: "尚未定论",
}

_REVERSIBILITY_WORDS = {
    "reversible": "可逆",
    "partially_reversible": "部分可逆",
    "irreversible": "不可逆",
}

_OPTION_STATUS_WORDS = {
    "active": "在议",
    "affected": "受态势影响 · 需复核",
    "rejected": "已否决",
}

# 前台只出中文；枚举原值仅作内部标识，不外泄到页面。
_ORDER_STATUS_WORDS = {
    "draft": "草稿", "pending_confirm": "待签发", "confirmed": "已签发",
    "active": "执行中", "paused": "已暂停", "completed": "已完成",
    "failed": "失败", "cancelled": "已取消", "expired": "已过期",
    "superseded": "已被新命令替代",
}
_QUALITY_WORDS = {
    "low": "低", "medium": "中", "high": "高",
    "short": "短期", "continuous": "持续", "long": "长期",
}
_SUBJECT_WORDS = {
    "route:R1": "原路线 R1", "route:R2": "备选路线 R2", "water:W1": "水源 W1",
    "weather:wind": "风力风向", "fire:trend": "火势趋势",
    "team:T1": "一分队", "team:T2": "预备队",
}
_ENTRY_WORDS = {"local-general-entry": "本机指挥端"}


def _order_word(v: object) -> str:
    v = str(getattr(v, "value", v))
    return _ORDER_STATUS_WORDS.get(v, v)


def _quality_word(v: object) -> str:
    v = str(v)
    return _QUALITY_WORDS.get(v, v)


def _subject_word(v: object) -> str:
    v = str(v)
    return _SUBJECT_WORDS.get(v, v)


def _entry_word(v: object) -> str:
    v = str(v)
    return _ENTRY_WORDS.get(v, v)


def _chip(text: str, tone: str = "") -> str:
    cls = f" lz-chip lz-chip-{tone}" if tone else " lz-chip"
    return f'<span class="{cls.strip()}">{esc(text)}</span>'


# ---------------------------------------------------------------------------
# 顶部状态条


def _data_time(state) -> str:
    ws = state.workspace
    for ev in reversed(getattr(ws, "_events", ())):
        stamp = getattr(ev, "at", None) or getattr(ev, "ts", None)
        if stamp:
            return str(stamp)
    return "—"


def _top_strip(state, event, stage_view, viewer_word: str) -> str:
    if event is not None:
        cells = [
            ("事件", f"{event.name}（{event.report_no or event.event_id}）"),
            ("响应等级", getattr(event, "level", "") or "未定级"),
            ("事件状态", event.status_word()),
        ]
    else:
        cells = [("事件", "—（未进入事件任务空间）")]
    counts = pending_counts(state.workspace)
    cells += [
        ("当前阶段", stage_view.current + ("／异常处置" if stage_view.overlay else "")),
        ("待批申请", str(counts["questions"])),
        ("待你拍板", str(len([o for o in state.workspace.options.values()
                            if getattr(o, "status", "") == "active"]))),
        ("异常", "1" if stage_view.overlay else "0"),
    ]
    items = "".join(
        f'<div class="strip-cell"><span class="k">{esc(k)}</span><b>{esc(v)}</b></div>'
        for k, v in cells
    )
    # 首屏只留作战关键格；次要信息收进悬停提示与「信息全景」抽屉，避免两行状态墙。
    return (
        '<div class="sand-strip" title="'
        + esc(f"数据截至 {_data_time(state)}｜当前登录 {viewer_word}｜{_COMM_PENDING}")
        + '">' + items + "</div>"
    )


# ---------------------------------------------------------------------------
# 地图载荷与脚本

#: 标绘状态 → 图层状态过滤器（定稿 §10.1：08 草稿与待核验、11 历史撤回）。
_STATE_FILTER = {
    "published": "published",
    "verified": "published",
    "pending_review": "candidate",
    "draft": "draft",
    "withdrawn": "history",
}

#: 常驻标签优先级（定稿：默认不超过 12 个，核心标签不得重叠）。
_TIP_ORDER = (
    "fire_area", "main_fireline", "firebreak", "team_position", "team_route",
    "command_post", "protected_target", "hazard_zone", "spread_direction",
    "hotspot", "water_source", "supply_point", "medical_point", "drone_position",
    "evacuation_route", "control_line", "staging_point", "reserve_point",
    "drone_coverage", "comm_blind_zone", "road_block", "wind_marker",
    "lookout_post", "drone_pad", "burned_area",
)
_TIP_LIMIT = 3

_SOURCE_WORDS = {
    "manual": "人工标绘",
    "ai-suggestion": "后台候选（待核验）",
    "geojson-import": "文件导入（待归类）",
}


def _visible_marks(state, role: str):
    allowed = sandtable.visible_statuses('public' if role == 'captain' else role)
    owned = set(getattr(state, '_business_team_ids', ()) or ())
    owned.update(state.ledger.squads[sid].name for sid in tuple(owned) if sid in state.ledger.squads)
    return [
        m for m in sorted(state.workspace.annotations.values(), key=lambda x: x.id)
        if m.status in allowed
        and (role != 'captain' or not m.detail('team_id') or m.detail('team_id') in owned)
    ]


def _stamp(text: object) -> str:
    """审计时间只为阅读而格式化（存仍存 ISO）。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("T", " ")
    if "+" in s:
        s = s.split("+")[0]
    return s[:16]


def _field_rows(mark, group: str, *, skip: set[str] = frozenset()) -> list:
    """按事实/判断/业务动作分栏输出（前台全中文，受控值经词表转译）。"""
    rows: list[list[str]] = []
    seen: set[str] = set()
    for key in sorted(mark.details or {}):
        if key in {'manual_style','manual_shape','manual_color'}:
            continue
        if tm.field_group(key) != group:
            continue
        value = str(mark.details[key]).strip()
        if not value:
            continue
        label = tm.detail_word(key)
        if label in skip or label in seen:
            continue
        seen.add(label)
        rows.append([label, tm.detail_display(key, value)[:80]])
    return rows


def _gap_geometry(mark) -> list[dict[str, float | int]]:
    geometry = []
    for gap in mark.gap_records():
        try:
            raw_index = float(gap["segment_index"])
            fraction = float(gap["fraction"])
            width = float(gap["width_m"])
        except (ValueError, TypeError, KeyError):
            continue
        if not (math.isfinite(raw_index) and raw_index.is_integer()):
            continue
        index = int(raw_index)
        if not (1 <= index < len(mark.geo_points) and math.isfinite(fraction)
                and math.isfinite(width) and 0 < fraction < 1 and width > 0):
            continue
        a, b = mark.geo_points[index - 1:index + 1]
        meters = math.hypot((b[0] - a[0]) * 111320,
                            (b[1] - a[1]) * 111320 *
                            math.cos(math.radians((a[0] + b[0]) / 2)))
        if meters <= 0 or width / (2 * meters) >= min(fraction, 1 - fraction):
            continue
        geometry.append({"segment_index": index, "fraction": fraction,
                         "width_m": width})
    return geometry


def _mark_payload(mark, role: str, *, tip: bool) -> dict:
    """单个对象的地图载荷 + 专属信息窗字段（前台全中文）。"""
    kind = mark.kind.value
    spec = tm.kind_spec(kind)
    status = mark.status.value
    conflicts = mark.conflict_level
    fresh = mark.freshness_status or tm.freshness_of(mark).value
    facts = [["来源", _SOURCE_WORDS.get(mark.source.value, "人工标绘")]]
    facts += _field_rows(mark, "fact")
    if mark.geo_points:
        facts.append(["坐标", f"{mark.geo_points[0][0]:.5f}, {mark.geo_points[0][1]:.5f}"
                      + ("｜共 %d 个顶点" % len(mark.geo_points) if len(mark.geo_points) > 1 else "")])
    judges = _field_rows(mark, "judge")
    if conflicts:
        judges.append(["路线冲突", tm.conflict_word(conflicts)])
    if fresh and fresh != tm.FreshnessStatus.OK.value:
        judges.append(["时效", tm.fresh_word(fresh)])
    actions = [["责任单位", mark.responsible_unit]] if mark.responsible_unit else []
    if mark.responsible_person:
        actions.append(["责任人", mark.responsible_person])
    actions += _field_rows(mark, "action", skip={"责任单位", "责任人"})
    if mark.related_order_id:
        actions.append(["关联命令", mark.related_order_id])
    if mark.related_plan_id:
        actions.append(["关联方案", mark.related_plan_id])
    audit = [["版本", f"v{mark.version}"]]
    if mark.supersedes_id:
        audit.append(["修订自", mark.supersedes_id])
    for label, value in (("创建端", _entry_word(mark.created_by)),
                        ("创建时间", _stamp(mark.created_at)),
                        ("更新时间", _stamp(mark.updated_at)),
                        ("核验时间", _stamp(mark.verified_at)),
                        ("发布时间", _stamp(mark.published_at)),
                        ("撤回时间", _stamp(mark.withdrawn_at)),
                        ("撤回理由", mark.withdraw_reason)):
        if value:
            audit.append([label, str(value)[:60]])
    if mark.verification_record.get("by"):
        audit.append(["核验人", _entry_word(mark.verification_record["by"])])
    if mark.publication_record.get("by"):
        audit.append(["发布人", _entry_word(mark.publication_record["by"])])
    style = tm.belt_style(kind, mark.details)
    from .manual_shapes import presentation as manual_presentation
    manual = manual_presentation(mark)
    payload = {
        "id": mark.id,
        "kind": kind,
        "manual": manual,
        "kind_word": manual.get('label') or kind_label(mark.kind),
        "legacy": spec.tier == "legacy",
        "title": mark.title,
        "label": f"{manual.get('label') or kind_label(mark.kind)}·{mark.title}",
        "short": mark.title[:14],
        "geometry": mark.geometry,
        "glyph": tm.glyph_of(kind),
        "color": manual.get('stroke') or tm.color_of(kind),
        "layer": spec.layer,
        "layer_word": tm.layer_word(spec.layer),
        "state": _STATE_FILTER.get(status, "draft"),
        "status_word": status_word(mark.status),
        "published": status == "published",
        "candidate": status == "pending_review" or mark.source == AnnotationSource.AI_SUGGESTION,
        "verified": status == "verified",
        "draft": status == "draft",
        "withdrawn": status == "withdrawn",
        "weight": style["weight"],
        "dash": style["dash"],
        "arrows": bool(style["arrows"]),
        "route_type": mark.detail("route_type"),
        "route_label": (mark.detail("team_id") if mark.detail("route_type")
                        in {"plan", "confirmed"} else ""),
        "belt_m": style.get("belt_m"),
        "pts": [[round(float(a), 6), round(float(b), 6)] for a, b in mark.geo_points],
        "note": (mark.note or "")[:200],
        "tip": tip,
        "facts": facts,
        "judges": judges,
        "actions": actions,
        "audit": audit,
        "gaps": [tm.gap_view(g) for g in mark.gap_records()],
        "gap_geometry": _gap_geometry(mark),
        "conflict": tm.conflict_word(mark.conflict_level) if mark.conflict_level else "",
        "conflict_badge": mark.conflict_level in {"blocked", "decision"},
        "fresh": tm.fresh_word(fresh),
        "heading": mark.detail("heading"),
        "demo": mark.demo_notice or ("演示数据·非现场实时数据" if mark.is_demo else ""),
        "position_label": tm.positioning_word(mark.positioning_status)
        if mark.positioning_status else "",
        "version": mark.version,
    }
    if mark.created_by == '事件资料自动投影':
        payload['projection'] = True
        payload['facts'][0] = ['来源', '火情事件原始资料（自动投影，位置待核）']
        payload['audit'].append(['资料引用', '、'.join(mark.source_ref)])
    return payload


def _marks_payload(state, role: str) -> list:
    marks = []
    tips = 0
    order = {k: i for i, k in enumerate(_TIP_ORDER)}
    visible = _visible_marks(state, role)
    ranked = sorted(
        visible,
        key=lambda m: (order.get(m.kind.value, 99), m.id),
    )
    tip_ids = set()
    for m in ranked:
        if tips >= _TIP_LIMIT:
            break
        spec = tm.kind_spec(m.kind.value)
        if m.status.value not in {"published", "verified"} or not spec.default_on:
            continue
        tip_ids.add(m.id)
        tips += 1
    for m in visible:
        if not m.geo_points:
            continue  # 未录地理坐标的标绘不上真实图（不做自动投影）
        payload = _mark_payload(m, 'public' if role == 'captain' else role, tip=m.id in tip_ids)
        if role != "public":
            source_ref = m.detail("source_ref")
            evidence_rows = []
            if source_ref:
                evidence_rows.append(["来源编号", source_ref])
                evidence = next((e for e in state.workspace.evidence.values()
                                 if e.source.split("（", 1)[0].strip() == source_ref), None)
                if evidence:
                    evidence_rows.extend([["证据编号", evidence.id],
                                          ["来源记录", evidence.content[:160]]])
                else:
                    evidence_rows.append(["原始内容", "未入本事件证据库，待核对"])
            command_rows = []
            flows = sorted(state.workspace.command_flows.values(), key=lambda f: f.order_id)
            for flow in flows:
                if flow.route_id == m.id or m.related_order_id == flow.order_id:
                    command_rows.extend([["命令编号", flow.order_id],
                                         ["目标", flow.target],
                                         ["执行阶段", flow.stage_word],
                                         ["回执", f"{len(flow.receipts)} 条"]])
                    break
            payload["linked_evidence"] = evidence_rows
            payload["linked_command"] = command_rows
        marks.append(payload)
    if role == 'commander' and getattr(state, 'active_event_id', ''):
        from .intelligence import fire_projection
        try:
            projected = fire_projection(state, state.active_event_id)
            from .initial_map import collect as initial_map_collect
            for mark in initial_map_collect(state, state.active_event_id)['marks']:
                marks.append(_mark_payload(mark, role, tip=False))
            if projected:
                marks.append(_mark_payload(projected, role, tip=True))
        except (ValueError, KeyError):
            pass
    return marks


def _layers_payload() -> list:
    """6 组业务图层 + 固定默认开关（定稿 §10.1）。"""
    return [
        {"key": s.key, "label": s.label, "on": bool(s.default_on), "render": s.render_layer}
        for s in tm.LAYERS
    ]


def _geom_table() -> dict:
    return {k.value: tm.geometry_of(k.value) for k in AnnotationKind}


# 方案叠加专用色（甲/乙/丙……）。与目标图例、危险区红区分开，避免误读成正式态势。
SIM_COLORS = ("#245B78", "#2F7D68", "#B77A1A", "#8A5A44", "#55606B", "#4B6FA8")

def _build_sims(state, role: str):
    """方案推演几何 + 时间轴推演（地图载荷与方案卡结论共用单一事实源）。

    公共屏不下发方案/推演载荷（仅指挥员与成员的业务面可见）。
    方案色由后端一次定下，保证方案卡上的色点与地图叠加线同色。
    """
    if role in {"public", "captain"}:
        return [], {"caliber": sand_sim.CALIBER, "steps": [], "rings": [], "targets": []}
    marks = _marks_payload(state, role)
    sims = []
    letters = "ABCDEF"
    for idx, o in enumerate(sorted(state.workspace.options.values(), key=lambda x: x.id)):
        if getattr(o, "status", "") not in {"active", "affected"}:
            continue
        shapes = getattr(o, "sim_shapes", ()) or ()
        if not shapes:
            continue
        sims.append({
            "id": o.id,
            "letter": letters[idx] if idx < len(letters) else "?",
            "title": o.title,
            "color": SIM_COLORS[idx % len(SIM_COLORS)],
            "shapes": [
                {
                    "geom": str(s.get("geom", "line")),
                    "pts": [[float(a), float(b)] for a, b in (s.get("pts") or ())],
                    "label": str(s.get("label", "")),
                }
                for s in shapes
            ],
        })
    proj = sand_sim.enrich_sims(marks, sims)
    return sims, proj


def _map_sources(state, center):
    status = _offline_resources.view_status(state)
    satellite = None
    if not status['error'] and not status['resource']:
        try:
            satellite = _local_satellite.description(state, center)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, _local_satellite.sqlite3.Error):
            status['error'] = '本机离线地图暂不可读取，请核对文件；不使用旧资源替代。'
    return status, satellite


def _map_payload(state, role: str) -> str:
    event = None
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    if registry is not None and active_id:
        event = registry.events.get(active_id)
    lat = getattr(event, "lat", None) if event is not None else None
    lon = getattr(event, "lon", None) if event is not None else None
    marks = _marks_payload(state, role)
    sims, proj = _build_sims(state, role)
    center = [float(lat), float(lon)] if lat is not None and lon is not None else None
    offline_status, satellite = _map_sources(state, center)
    local_map = offline_status["resource"]
    payload = {
        "center": center,
        "marks": marks,
        "sims": sims,
        "proj": proj,
        "layers": _layers_payload(),
        "geom": _geom_table(),
        "conflicts": [] if role in {"public", "captain"} else [
            c.as_row() for c in sorted(
                state.workspace.tactical_conflicts(),
                key=lambda x: (-x.level_rank, x.route_id),
            )
        ],
        # 底图默认源：有本地离线包则优先离线（无在线依赖），否则高德。
        "base": ("unavailable" if offline_status["error"] else "local" if local_map else "satellite" if satellite else "gaode"),
        "satellite": satellite,
        # The restored drill opens on its fireground; parent-tile fallback keeps
        # the original resolution warning instead of hiding the scene at level 8.
        "initialZoom": 13 if getattr(state, 'fire_drill_scene', None) and center else satellite['zoom'] if satellite and satellite['zoom'] is not None else 13 if center else 9,
        "viewContext": getattr(event, 'report_no', '') if getattr(state, 'fire_drill_scene', None) else '',
        "offlineError": offline_status["error"],
        "offline": (dict(url="/offlinemap/{z}/{x}/{y}.png",
            note="具名报告的资源授权；坐标质量未获实地核验。",
            max=local_map[1]['manifest']['max_zoom'])
            if local_map else None),
        # 叠加偏好按事件分键存在浏览器里，不能串到另一场火上。
        "event": active_id or "",
    }
    return json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")


_CMD_MAP_JS = """
(function(){
  function boot(){
  var box=document.getElementById("cmd-map");
  if(!box)return;
  var DATA={center:null,marks:[],sims:[],proj:null};
  try{var raw=document.getElementById("cmd-map-data");if(raw){DATA=JSON.parse(raw.textContent);}}catch(e){}
  function banner(msg,cls){
    var b=document.getElementById("cmd-map-banner");
    if(!b)return;b.className="cmd-mapbanner "+(cls||"");b.textContent=msg;b.style.display="block";
  }
  function bottom(html){var el=document.getElementById("cmd-bottom-info");if(el){el.innerHTML=html;document.dispatchEvent(new CustomEvent("sand:object-selected"));}}
  if(typeof L==="undefined"){
    box.innerHTML="";
    banner("本地地图组件未能加载，请核对静态资源。地图作业暂不可用，台账与表格仍可用；本页不以示意图冒充真实地图。","err");
    return;
  }
  var haveCenter=!!(DATA.center&&DATA.center.length===2);
  if(!haveCenter&&(!DATA.marks||DATA.marks.length===0)){
    banner("当前无事件坐标，也没有已录入地理坐标的标绘——在「火情登记」补录事件坐标，或用左侧绘制工具在配准后上图。","warn");
  }
  var map=L.map(box,{zoomControl:true});
  window.__cmdMap=map;   // 可观测性：供自动化验证与排查
  if(haveCenter){map.setView(DATA.center,DATA.initialZoom==null?13:DATA.initialZoom);}else{map.setView([32.0,110.0],DATA.initialZoom==null?9:DATA.initialZoom);}
  var BASES={
    local:{n:"本地离线瓦片包",u:"/offlinemap/{z}/{x}/{y}.png",a:"本地离线瓦片包（按来源定精度，无在线依赖）",max:19,note:"离线包的来源、坐标系和覆盖范围尚未经核对；不能据此认定标绘精度。"},
    gaode:{n:"高德地图（在线）",u:"https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",a:"底图 © 高德地图",max:18,sub:["1","2","3","4"],note:"高德底图采用国内偏移坐标，与地理标绘可能相差数十至数百米；仅供示意。"},
    gaode_img:{n:"高德影像（在线）",u:"/sand/online-imagery/{z}/{x}/{y}.jpg?event_id="+encodeURIComponent(DATA.event),a:"影像 © 高德地图",max:18,note:"高德影像采用国内偏移坐标，与地理标绘可能相差数十至数百米；仅供示意。"},
    opentopo:{n:"开源地形（在线，非影像）",u:"https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",a:"© <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap 贡献者</a>、SRTM | © <a href='https://opentopomap.org/'>OpenTopoMap</a> (<a href='https://creativecommons.org/licenses/by-sa/3.0/'>CC BY-SA 3.0</a>)",max:17,sub:["a","b","c"],note:"开源地形由 OSM 和 SRTM 绘制，非航拍影像；标绘位置尚未经独立控制点核对。"},
    osm:{n:"开放街道地图（在线）",u:"https://tile.openstreetmap.org/{z}/{x}/{y}.png",a:"底图 © 开放街道地图贡献者",max:18,note:"道路与地物来自开放地图；标绘位置尚未经独立控制点核对。"}
  };
  var tiles=null,tileFails=0;
  if(DATA.satellite){
    BASES.satellite={n:"本机离线卫星影像",u:DATA.satellite.url,a:"用户提供的 Google 卫星影像",max:DATA.satellite.max,min:DATA.satellite.min,note:DATA.satellite.note};
    if(DATA.base==="satellite"&&(DATA.satellite.zoom==null||DATA.satellite.zoom<(haveCenter?13:9))){banner(DATA.satellite.note,"warn");}
  }
  var baseGeneration=0,connectionTimer=null,baseReason='';
  function setBase(key,reason){
    var generation=++baseGeneration;
    clearTimeout(connectionTimer);baseReason=reason||'';
    if(DATA.offlineError){
      if(tiles){map.removeLayer(tiles);tiles=null;}
      var unavailableNote=document.getElementById("cmd-base-notice");
      if(unavailableNote){unavailableNote.textContent=DATA.offlineError;}
      var unavailableCredit=document.getElementById("cmd-base-credit");
      if(unavailableCredit){unavailableCredit.textContent="底图停止供图";}
      return;
    }
    if(DATA.offline){BASES.local.u=DATA.offline.url;BASES.local.note=DATA.offline.note;BASES.local.max=DATA.offline.max;}
    var b=BASES[key]||BASES.gaode;
    if(tiles){map.removeLayer(tiles);}
    var op={maxZoom:b.max||18,attribution:b.a};
    if(key==="satellite"){op.minZoom=b.min;op.noWrap=true;}
    if(b.sub){op.subdomains=b.sub;}
    var offline=key==="satellite"||key==="local";
    tiles=key==="satellite"&&window.createOfflineMapLayer?window.createOfflineMapLayer(L,b.u,op):L.tileLayer(b.u,op);
    var layer=tiles;
    window.__cmdBaseLayer=layer;window.__cmdBaseKey=key;
    function active(){return generation===baseGeneration&&tiles===layer;}
    function announce(message,kind){
      if(!active())return;
      var note=document.getElementById("cmd-base-notice");
      if(note)note.textContent=message;
      banner(message,kind);
    }
    function loaded(){return Object.values(layer._tiles||{}).filter(function(t){return t.current&&t.el.complete&&t.el.naturalWidth>0;});}
    function fallback(){
      if(!active())return;
      clearTimeout(connectionTimer);
      var localKey=DATA.offline?"local":DATA.satellite?"satellite":null;
      // Leaflet finishes its tile-ready bookkeeping after firing load. Remove
      // the old layer on the next turn so that callback still has a live map.
      if(!offline&&localKey){setTimeout(function(){if(active())setBase(localKey,b.n+"未能加载，已回到本地底图。 ");},0);}
      else announce(b.n+"在当前区域无法显示；请核对网络或补充该区域地图。标绘和火情坐标保留。","err");
    }
    function offlineNotice(){
      if(!active()||!offline)return;
      var rows=loaded(),lower=rows.filter(function(t){return Number(t.el.dataset.nativeZoom)<Number(t.el.dataset.requestedZoom);});
      var message=baseReason+b.note;
      if(lower.length){message=baseReason+"当前放大显示已有离线原图（第"+Math.min.apply(null,lower.map(function(t){return Number(t.el.dataset.nativeZoom);}))+"级）；缺少第"+map.getZoom()+"级高清瓦片，清晰度未增加。";}
      announce(message,lower.length||baseReason?"warn":"");
    }
    tileFails=0;
    var credit=document.getElementById("cmd-base-credit");if(credit)credit.innerHTML=b.a||"";
    if(offline)announce(baseReason+b.note,baseReason?"warn":"");
    else announce(b.n+"连接中…","warn");
    layer.on("loading",function(){tileFails=0;});
    layer.on("tileerror",function(){if(active())tileFails+=1;});
    layer.on("load",function(){
      if(!active())return;
      if(!loaded().length){fallback();return;}
      clearTimeout(connectionTimer);
      if(offline){offlineNotice();return;}
      announce(b.n+"已连接。"+(tileFails?"部分瓦片缺失。":"")+b.note,tileFails?"warn":"");
    });
    layer.on("tileload",function(){if(offline)offlineNotice();});
    layer.addTo(map);
    if(!offline)connectionTimer=setTimeout(function(){if(active()&&!loaded().length)fallback();},8000);
    document.querySelectorAll("[data-base-key]").forEach(function(r){
      if(r.tagName==="INPUT"){r.checked=(r.getAttribute("data-base-key")===key);}
    });
  }
  // Tab-scoped, event-scoped view preference. Synthetic demo movement is transient.
  var viewKey="poju-map-view-v1:"+DATA.event+(DATA.viewContext?":"+DATA.viewContext:""),savedView=null,preferredBase=DATA.base||"gaode";
  try{savedView=JSON.parse(sessionStorage.getItem(viewKey)||"null");}catch(e){}
  if(savedView&&BASES[savedView.base]&&Array.isArray(savedView.center)&&savedView.center.length===2&&
      savedView.center.every(Number.isFinite)&&Math.abs(savedView.center[0])<=90&&Math.abs(savedView.center[1])<=180&&
      Number.isInteger(savedView.zoom)&&savedView.zoom>=0&&savedView.zoom<=BASES[savedView.base].max){
    preferredBase=savedView.base;map.setView(savedView.center,savedView.zoom,{animate:false});
  }
  function saveView(){if(window.__cmdPresentationDemo)return;
    var c=map.getCenter();try{sessionStorage.setItem(viewKey,JSON.stringify({base:preferredBase,center:[c.lat,c.lng],zoom:map.getZoom()}));}catch(e){}
  }
  setBase(preferredBase);map.on('moveend',saveView);
  document.querySelectorAll("[data-base-key]").forEach(function(r){
    r.addEventListener("change",function(){if(r.checked){preferredBase=r.getAttribute("data-base-key");setBase(preferredBase);saveView();}});
  });

  // —— P19 战术态势渲染：6 组业务图层 × 状态过滤 + 专用符号（定稿 §10）
  var LAY={};(DATA.layers||[]).forEach(function(g){LAY[g.key]=L.layerGroup().addTo(map);});
  var HIST=L.layerGroup().addTo(map);
  var LAYERON={};(DATA.layers||[]).forEach(function(g){LAYERON[g.key]=!!g.on;});
  var HISTON=false;
  var STATEF={published:true,candidate:true,draft:true};
  var ITEMS=[],selectedId='';
  var objectKey='poju:intelligence-display:v1:'+(DATA.event||'');
  var OBJECTMODE='all',OBJECTVIS={},OBJECTSNAP=null;
  try{var savedObjects=JSON.parse(sessionStorage.getItem(objectKey)||'null');if(savedObjects&&['all','none'].includes(savedObjects.mode)){OBJECTMODE=savedObjects.mode;OBJECTVIS=savedObjects.objects||{};}}catch(e){}
  function objectVisible(id){return Object.prototype.hasOwnProperty.call(OBJECTVIS,id)?OBJECTVIS[id]:OBJECTMODE!=='none';}
  function saveObjects(){try{sessionStorage.setItem(objectKey,JSON.stringify({mode:OBJECTMODE,objects:OBJECTVIS}));}catch(e){}}
  function escx(s){
    return String((s===null||s===undefined)?"":s).replace(/[&<>"]/g,function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});
  }
  function rowsHtml(title,rows){
    if(!rows||!rows.length)return "";
    var h='<div class="cmd-infog"><b>'+escx(title)+'</b>';
    rows.forEach(function(r){
      h+='<span class="k">'+escx(r[0])+'</span><span class="v">'+escx(r[1])+'</span>';
    });
    return h+'</div>';
  }
  function statusWord(m){
    if(m.verified&&!m.published)return "已核验·待发布";
    return m.status_word||"";
  }
  function detailHtml(m){
    var h='<div class="cmd-infotop"><b>'+escx(m.label)+'</b>'
      +'<span class="cmd-chip">'+escx(statusWord(m))+'</span>'
      +'<span class="cmd-chip">'+escx(m.layer_word)+'</span>';
    if(m.legacy)h+='<span class="cmd-chip warn">通用标绘·待归类</span>';
    if(m.conflict)h+='<span class="cmd-chip bad">'+escx(m.conflict)+'</span>';
    if(m.fresh&&m.fresh!=="正常")h+='<span class="cmd-chip warn">'+escx(m.fresh)+'</span>';
    h+='</div>';
    h+=rowsHtml("事实",m.facts);
    h+=rowsHtml("判断",m.judges);
    h+=rowsHtml("业务动作",m.actions);
    h+=rowsHtml("关联命令执行",m.linked_command);
    h+=rowsHtml("关联来源证据",m.linked_evidence);
    (m.gaps||[]).forEach(function(g){h+=rowsHtml("隔火带缺口子记录（随本对象展示）",g);});
    h+=rowsHtml("核验与审计",m.audit);
    if(m.position_label)h+='<div class="cmd-infodemo">定位状态：'+escx(m.position_label)+'</div>';
    if(m.demo)h+='<div class="cmd-infodemo">'+escx(m.demo)+'</div>';
    if(m.note)h+='<div class="cmd-infonote">备注：'+escx(m.note)+'</div>';
    return h;
  }
  var SYM={
    hotspot:'<path d="M12 2c3 4 5 6 5 9a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 1-3-1-5 1-8z"/>',
    team:'<circle cx="12" cy="7" r="3"/><circle cx="5" cy="9" r="2.3"/><circle cx="19" cy="9" r="2.3"/><path d="M7 21v-5a5 5 0 0 1 10 0v5zM1 19v-4a4 4 0 0 1 5-3v7zM18 19v-7a4 4 0 0 1 5 3v4z"/>',
    hq:'<rect x="3" y="10" width="18" height="10" rx="1"/><path d="M12 2v8" stroke="currentColor" stroke-width="2" fill="none"/><path d="M12 2l6 2-6 2z"/>',
    water:'<path d="M12 3c4 5 6 7 6 10a6 6 0 0 1-12 0c0-3 2-5 6-10z"/>',
    supply:'<rect x="3" y="7" width="18" height="13" rx="1"/><path d="M3 11h18M11 7v13" stroke="#fff" stroke-width="1.4" fill="none"/>',
    medical:'<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M12 8v8M8 12h8" stroke="#fff" stroke-width="2.6" fill="none"/>',
    drone:'<circle cx="5" cy="5" r="3"/><circle cx="19" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M7 7l2 2M17 7l-2 2M7 17l2-2M17 17l-2-2" stroke="currentColor" stroke-width="1.6" fill="none"/>',
    pad:'<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9 8v8M15 8v8M9 12h6" stroke="currentColor" stroke-width="2" fill="none"/>',
    target:'<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="11" r="2.6"/>',
    staging:'<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7l5 9H7z"/>',
    reserve:'<path d="M12 3l9 9-9 9-9-9z"/>',
    block:'<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2.4"/><path d="M6 18L18 6" stroke="currentColor" stroke-width="2.4" fill="none"/>',
    wind:'<path d="M4 12h12M12 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2.4"/>',
    lookout:'<path d="M12 5c6 0 9 7 9 7s-3 7-9 7-9-7-9-7 3-7 9-7z" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3"/>',
    line:'',text:''
  };
  function symIcon(m,pt){
    var inner=SYM[m.glyph];
    var html;
    if(inner===undefined||inner===""){
      html='<span class="cmd-symtxt">'+escx(m.short||m.title)+'</span>';
    }else{
      var rot="";
      var hd=parseFloat(m.heading||"");
      if(m.glyph==="drone"&&!isNaN(hd))rot=' style="transform:rotate('+hd+'deg)"';
      html='<span class="cmd-symrot"'+rot+'><svg viewBox="0 0 24 24" width="26" height="26" '
        +'fill="currentColor" style="color:'+escx(m.color)+'">'+inner+'</svg></span>';
    }
    return L.divIcon({className:"cmd-sym map-semantic-symbol map-state-"+m.state+(m.presentation?" map-display-"+m.presentation:""),html:'<span class="map-symbol-disc" style="--symbol-color:'+escx(m.color)+'">'+html+'</span>',iconSize:[32,32],iconAnchor:[16,16]});
  }
  function bearing(a,b){
    var dLon=(b[1]-a[1])*Math.cos((a[0]+b[0])*Math.PI/360);
    return Math.atan2(dLon,b[0]-a[0])*180/Math.PI;
  }
  function arrowIcon(deg,color){
    return L.divIcon({className:"cmd-arrow",iconSize:[24,24],iconAnchor:[12,12],
      html:'<span class="cmd-arrowin" style="transform:rotate('+deg.toFixed(1)+'deg);border-bottom-color:'+color+'"></span>'});
  }
  function styleFor(m){
    var st={color:m.color,weight:m.weight||3,opacity:.95,dashArray:"",fillOpacity:.18};
    if(m.dash)st.dashArray=m.dash;
    if(m.geometry==="polygon"){
      if(m.glyph==="fire_area"){st.weight=3;st.fillOpacity=.22;st.className="cmd-fill-fire";}
      else if(m.glyph==="hazard"){st.weight=2;st.dashArray="6 4";st.fillOpacity=.16;st.className="cmd-fill-hatch";}
      else if(m.glyph==="burned"){st.weight=2;st.dashArray="3 5";st.fillOpacity=.14;st.className="cmd-fill-hatch2";}
      else if(m.glyph==="coverage"){st.weight=2;st.dashArray="8 5";st.fillOpacity=.3;st.color="#5C6B73";}
      else if(m.glyph==="area"){st.weight=2;st.fillOpacity=.14;}
      else {st.weight=2;st.dashArray="6 4";st.fillOpacity=.1;}
    }
    if(m.state==="candidate"){st.opacity=.85;st.dashArray="7 6";st.fillOpacity=.14;}
    else if(m.state==="draft"){st.color="#647382";st.opacity=.62;st.weight=Math.max(2,(m.weight||3)-1);st.dashArray="5 7";st.fillOpacity=.07;}
    else if(m.state==="history"){st.color="#8A94A0";st.opacity=.35;st.weight=2;st.dashArray="2 6";st.fillOpacity=.05;}
    else if(m.verified&&!m.published){st.opacity=.72;st.fillOpacity=.13;}
    if(m.kind==="team_route"||m.kind==="evacuation_route"){
      st.weight=Math.max(6,st.weight);
      if(m.route_type==="plan"){st.dashArray="14 8";st.opacity=.72;}
      else if(m.route_type==="actual"&&m.state==="published"){st.color="#176A5D";st.dashArray="";}
    }
    return st;
  }
  function beltPaths(m){
    var g=(m.gap_geometry||[])[0],pts=m.pts;
    if(!g||!pts||g.segment_index<1||g.segment_index>=pts.length)return {paths:[pts],gap:null};
    var i=g.segment_index,a=pts[i-1],b=pts[i],cos=Math.cos((a[0]+b[0])*Math.PI/360);
    var meters=Math.hypot((b[0]-a[0])*111320,(b[1]-a[1])*111320*cos);
    if(!meters||g.width_m<=0||g.width_m>=meters)return {paths:[pts],gap:null};
    var half=g.width_m/(2*meters),lo=g.fraction-half,hi=g.fraction+half;
    if(lo<=0||hi>=1)return {paths:[pts],gap:null};
    function lerp(t){return [a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t];}
    return {paths:[pts.slice(0,i).concat([lerp(lo)]),[lerp(hi)].concat(pts.slice(i))],
      gap:lerp(g.fraction)};
  }
  function buildParts(m,presentation){
    var parts=[],st=styleFor(m);
    if(m.manual&&m.manual.version==='reference-v1'){
      st.color=m.manual.stroke;st.fillColor=m.manual.fill;st.weight=3;st.fillOpacity=.5;
      st.opacity=m.state==='history'?.35:1;
      st.dashArray=m.state==='draft'?'6 4':st.dashArray;
    }
    function filledArrow(method){
      var manual=m.manual&&m.manual.version==='reference-v1',color=manual?m.manual.stroke:m.state==='history'?'#667788':m.state==='draft'?'#647382':'#007EAF';
      var geo=function(){return window.PojuWideArrow[method](map,m.pts);};
      var outline=L.polygon(geo(),{color:'#FFFFFF',weight:5,fill:false,opacity:m.state==='history'?.35:.95,interactive:false,className:'cmd-route-outline'});
      var arrow=L.polygon(geo(),{color:color,weight:manual?2.5:1.4,fillColor:manual?m.manual.fill:color,fillOpacity:m.state==='history'?.12:manual?.82:.76,opacity:m.state==='history'?.35:1,
        dashArray:m.state==='candidate'&&m.presentation!=='up'&&m.presentation!=='edit'?'6 4':'',className:'cmd-route-area'});
      [outline,arrow].forEach(function(p){p._cmdArrowUpdate=function(){p.setLatLngs(geo());};});
      outline.on('add',function(){this.bringToBack();});parts.push(arrow,outline);
    }
    if(m.kind==="arrow"&&window.PojuWideArrow){
      filledArrow(m.manual&&m.manual.shape==='arrow'?'referenceGeometry':'geometry');
    }else if((m.kind==="team_route"||m.kind==="evacuation_route")&&m.route_type!=="actual"&&window.PojuWideArrow){
      filledArrow('routeGeometry');
      if(m.route_label)parts.push(L.marker(m.pts[0],{icon:L.divIcon({className:"cmd-route-label",html:'<span>'+escx(m.route_label)+'</span>',iconSize:[100,22],iconAnchor:[0,27]}),interactive:false}));
    }else if(m.manual&&m.manual.shape==='point'){
      parts.push(L.circleMarker(m.pts[0],{...st,radius:9,fillOpacity:.8}));
    }else if(m.manual&&m.manual.shape==='text'){
      parts.push(L.marker(m.pts[0],{icon:L.divIcon({className:'cmd-manual-text',html:'<span style="color:'+m.manual.fill+'">'+escx(m.title)+'</span>',iconSize:[160,32],iconAnchor:[0,16]})}));
    }else if(m.geometry==="point"||m.glyph==="drone"){
      var mk=L.marker(m.pts[0],{icon:symIcon(m,m.pts[0]),interactive:true});
      parts.push(mk);
    }else if(m.geometry==="polygon"){
      parts.push(L.polygon(m.pts,st));
    }else{
      if(m.glyph==="belt"||m.kind==="firebreak"||m.kind==="isolation"){
        if((m.gaps||[]).length&&!(m.gap_geometry||[]).length)st.dashArray="6 5";
        var belt=beltPaths(m);
        belt.paths.forEach(function(path){
          parts.push(L.polyline(path,{className:"cmd-firebreak-band",color:"#C8A27A",weight:Math.min(14,Math.max(8,m.weight||12)),opacity:.75,lineCap:"butt",dashArray:m.dash||((m.gaps||[]).length&&!belt.gap?"6 5":"")}));
          parts.push(L.polyline(path,{color:st.color,weight:2,opacity:st.opacity,dashArray:st.dashArray||""}));
        });
        if(belt.gap)parts.push(L.marker(belt.gap,{icon:L.divIcon({className:"cmd-gap",iconSize:[20,20],iconAnchor:[10,10],html:'<span class="cmd-gapin">×</span>'}),interactive:false}));
      }else{
        parts.push(L.polyline(m.pts,st));
      }
      if(m.arrows){
        var end=m.pts.length-1;
        parts.push(L.marker(m.pts[end],{icon:arrowIcon(bearing(m.pts[end-1],m.pts[end]),st.color),interactive:false}));
        if(m.route_label)parts.push(L.marker(m.pts[0],{icon:L.divIcon({className:"cmd-route-label",html:'<span>'+escx(m.route_label)+'</span>',iconSize:[100,22],iconAnchor:[0,27]}),interactive:false}));
      }
    }
    if(m.conflict_badge){
      var bp=m.pts[Math.floor(m.pts.length/2)];
      parts.push(L.marker(bp,{icon:L.divIcon({className:"cmd-badge",iconSize:[54,18],iconAnchor:[27,9],
        html:'<span class="cmd-badgein">'+escx(m.conflict||"冲突")+'</span>'}),interactive:false}));
    }
    if(presentation)return parts;
    var tipHtml=escx(m.short||m.title)+(m.verified&&!m.published?"（已核验·待发布）":"");
    parts.forEach(function(p,idx){
      if(p.options.interactive!==false&&(p instanceof L.Polyline||p instanceof L.Marker)){
        p.bindTooltip(tipHtml,{permanent:!!m.tip&&idx===0&&!m.route_label&&m.manual?.shape!=='text'&&window.innerWidth>=700,direction:"top",offset:[0,-12],className:"cmd-marktip"});
        p.on("click",function(){selectedId=m.id;bottom(detailHtml(m));});
      }
    });
    return parts;
  }
  window.__cmdSuggestionParts=function(s){
    var original=ITEMS.find(function(it){return it.m.id===s.id;});
    var m=Object.assign({glyph:s.kind==='hotspot'?'hotspot':'line',color:'#C8482F'},original?original.m:{},s.render||{},
      {id:s.id,title:s.title,short:s.title,kind:s.kind,geometry:s.geometry,pts:s.points,state:s.demo&&s.display_state||'candidate',published:false,verified:false,presentation:s.mode});
    return buildParts(m,true);
  };
  (DATA.marks||[]).forEach(function(m){
    ITEMS.push({m:m,parts:buildParts(m)});
  });
  function layerAllowed(m){
    if(m.state==="history")return HISTON;
    if(!STATEF[m.state])return false;
    return LAYERON[m.layer]!==false && !!LAY[m.layer];
  }
  function refresh(){
    Object.keys(LAY).forEach(function(k){LAY[k].clearLayers();});
    HIST.clearLayers();
    ITEMS.forEach(function(it){
      if(window.__cmdPresentationDemo || window.__cmdPresentationIds?.has(it.m.id) || !layerAllowed(it.m)||!objectVisible(it.m.id))return;
      var host=(it.m.state==="history")?HIST:(LAY[it.m.layer]||HIST);
      it.parts.forEach(function(p){p._cmdArrowUpdate?.();p.addTo(host);});
    });
    document.dispatchEvent(new CustomEvent('sand:map-filter-change'));
  }
  refresh();
  window.__cmdPresentationRefresh=refresh;
  window.__cmdDisplaySnapshot=function(){return {mode:OBJECTMODE,objects:Object.assign({},OBJECTVIS),layers:Object.assign({},LAYERON),states:Object.assign({},STATEF),history:HISTON};};
  window.__cmdDisplayRestore=function(s){OBJECTMODE=s.mode;OBJECTVIS=s.objects;LAYERON=s.layers;STATEF=s.states;HISTON=s.history;saveObjects();refresh();};
  window.__cmdIsFireKind=function(kind){return ['fire_area','main_fireline','hotspot'].includes(kind);};
  window.__cmdClearKnown=function(){ITEMS.forEach(function(it){var keep=it.m.state!=='history'&&window.__cmdIsFireKind(it.m.kind);OBJECTVIS[it.m.id]=keep;if(keep){LAYERON[it.m.layer]=true;STATEF[it.m.state]=true;}});saveObjects();if(typeof setMode==='function')setMode(null);window.PojuWideArrow?.clear();window.__cmdSimulationClear?.();refresh();};
  map.on('zoomend',function(){map.eachLayer(function(p){p._cmdArrowUpdate?.();});});
  window.__cmdPresentationAllowed=function(id){
    var item=ITEMS.find(function(i){return i.m.id===id;});
    return item?objectVisible(id)&&layerAllowed(item.m):OBJECTMODE!=='none';
  };
  window.__cmdObjectVisible=function(id){var item=ITEMS.find(function(i){return i.m.id===id;});return !!item&&objectVisible(id)&&layerAllowed(item.m);};
  window.__cmdIntelligenceDisplay=function(action,id){
    if(action==='restore'){
      if(OBJECTSNAP){OBJECTMODE=OBJECTSNAP.mode;OBJECTVIS=OBJECTSNAP.objects;LAYERON=OBJECTSNAP.layers;STATEF=OBJECTSNAP.states;HISTON=OBJECTSNAP.history;OBJECTSNAP=null;}
      var restorePlan=document.getElementById('cmd-sim-restore');if(restorePlan)restorePlan.click();
    }else if(action==='all'||action==='none'){
      OBJECTSNAP={mode:OBJECTMODE,objects:Object.assign({},OBJECTVIS),layers:Object.assign({},LAYERON),states:Object.assign({},STATEF),history:HISTON};OBJECTMODE=action;OBJECTVIS={};
      if(action==='all'){Object.keys(LAYERON).forEach(function(k){LAYERON[k]=true;});STATEF.published=STATEF.candidate=STATEF.draft=true;}
      if(action==='none'){var clearPlan=document.getElementById('cmd-sim-clear');if(clearPlan)clearPlan.click();}
    }else{
      var item=ITEMS.find(function(i){return i.m.id===id;});if(!item)return;
      OBJECTVIS[id]=!window.__cmdObjectVisible(id);
      if(OBJECTVIS[id]){LAYERON[item.m.layer]=true;if(item.m.state==='history')HISTON=true;else STATEF[item.m.state]=true;}
    }
    saveObjects();refresh();document.dispatchEvent(new CustomEvent('intelligence:display-changed'));
  };
  window.__cmdIntelligenceLocate=function(id){
    var item=ITEMS.find(function(i){return i.m.id===id;});if(!item||!item.m.pts.length)return;
    if(!window.__cmdObjectVisible(id))window.__cmdIntelligenceDisplay('toggle',id);
    map.panTo(item.m.pts[0]);selectedId=id;bottom(detailHtml(item.m));
  };
  window.__cmdApplyMarks=function(marks){
    var previous={};ITEMS.forEach(function(it){previous[it.m.id]=it;});
    ITEMS=(marks||[]).map(function(m){
      var old=previous[m.id];
      return old&&JSON.stringify(old.m)===JSON.stringify(m)?old:{m:m,parts:buildParts(m)};
    });
    DATA.marks=marks||[];
    refresh();
    if(selectedId){
      var selected=ITEMS.find(function(it){return it.m.id===selectedId;});
      var info=document.getElementById('cmd-bottom-info');
      if(info)info.innerHTML=selected?detailHtml(selected.m):'<p>选中对象已不可用，请核对当前事件及权限。</p>';
      if(!selected)selectedId='';
    }
  };
  HIST.addTo(map);
  document.querySelectorAll("[data-cmd-layer]").forEach(function(cb){
    cb.addEventListener("change",function(){
      var k=cb.getAttribute("data-cmd-layer");
      if(k==="history"){HISTON=cb.checked;}
      else{STATEF[k]=cb.checked;}
      refresh();
    });
  });
  document.querySelectorAll("[data-layer-group]").forEach(function(cb){
    cb.addEventListener("change",function(){
      LAYERON[cb.getAttribute("data-layer-group")]=cb.checked;
      refresh();
    });
  });
  var allPts=[];
  (DATA.marks||[]).forEach(function(m){m.pts.forEach(function(p){allPts.push(p);});});
  if(!haveCenter&&allPts.length){map.fitBounds(L.latLngBounds(allPts),{padding:[30,30]});}

  // —— 冲突台账（独立记录；关闭图层、点已查看、刷新页面都不能解除）
  var CF=DATA.conflicts||[];
  var cfEl=document.getElementById("cmd-conflict-note");
  if(cfEl){
    if(CF.length){
      var bad=CF.filter(function(c){return c.level==="blocked"||c.level==="decision";}).length;
      cfEl.textContent="路线冲突 "+CF.length+" 条 · 发布阻断 "+bad+" 条 · 查看详情";
      cfEl.addEventListener("click",function(){
        bottom(rowsHtml("路线冲突（记录仍有效）",CF.map(function(c){
          return [c.route+" · "+c.level_word,c.summary];
        })));
      });
      cfEl.style.display="block";
    }else{
      cfEl.textContent="";cfEl.style.display="none";
    }
  }

  // —— P20 变化卡定位：只居中/高亮目标对象并打开详情，不改图层与方案状态
  window.__cmdFocus=function(id){
    var it=null;
    (ITEMS||[]).forEach(function(x){if(x.m&&x.m.id===id)it=x;});
    if(!it)return false;
    selectedId=id;
    var pts=it.m.pts||[];
    if(pts.length===1){map.panTo(pts[0]);if(map.getZoom()<14)map.setZoom(14);}
    else if(pts.length){map.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:15});}
    try{bottom(detailHtml(it.m));}catch(e){}
    return false;
  };


  // —— 图上直接绘制：工具→预览→保存草稿或提交核验（定稿 §11.2）
  var FORM_KIND=document.getElementById("cmd-anno-kind");
  var FORM_GEO=document.getElementById("cmd-anno-geo");
  var FORM_TITLE=document.getElementById("cmd-anno-title");
  var GEOM=DATA.geom||{fire_point:"point",headquarters:"point",water:"point",material:"point",
            fireline:"line",isolation:"line",route:"line",arrow:"line",area:"polygon"};
  function need(k){var g=GEOM[k]||"point";return g==="point"?1:(g==="line"?2:3);}
  var mode=null,tempPts=[],tempLayer=null;
  function showFields(k){
    // 只提交当前类型的专属字段：其余隐藏 fieldset 的输入全部禁用，
    // 否则一个表单会把 25 类的默认下拉值一起带上去（假字段）。
    document.querySelectorAll("[data-fields-for]").forEach(function(fs){
      var on=fs.getAttribute("data-fields-for")===k;
      fs.style.display=on?"":"none";
      fs.querySelectorAll("input,select,textarea").forEach(function(el){
        if(on){el.removeAttribute("disabled");}else{el.setAttribute("disabled","disabled");}
      });
    });
  }
  function setMode(k){
    mode=k;tempPts=[];
    if(k){var ms=document.querySelector('[name=d_manual_style]'),sh=document.querySelector('[name=d_manual_shape]'),co=document.querySelector('[name=d_manual_color]');if(ms)ms.value='reference-v1';if(sh)sh.value='';if(co)co.value='red';}
    if(tempLayer){map.removeLayer(tempLayer);tempLayer=null;}
    document.querySelectorAll(".cmd-tool[data-draw-kind]").forEach(function(b){
      b.classList.toggle("on",b.getAttribute("data-draw-kind")===k);
    });
    var hint=document.getElementById("cmd-mode-hint");
    var btn=k?document.querySelector('.cmd-tool[data-draw-kind="'+k+'"]'):null;
    if(hint){hint.textContent=k?("绘制中："+(btn?btn.textContent:k)
      +(GEOM[k]==="point"?"（在图上单击落点）":"（单击加顶点，至少 "+need(k)+" 个；双击或点「完成绘制」结束）")):"";}
    var fin=document.getElementById("cmd-finish");
    if(fin){fin.style.display=(k&&GEOM[k]!=="point")?"":"none";}
    if(k){showFields(k);}
  }
  document.querySelectorAll(".cmd-tool[data-draw-kind]").forEach(function(b){
    b.addEventListener("click",function(){setMode(b.getAttribute("data-draw-kind")===mode?null:b.getAttribute("data-draw-kind"));});
  });
  document.addEventListener("sand:draw-kind",function(e){if(e.detail==='manual-clear'){setMode(null);return;}if(FORM_KIND&&FORM_KIND.querySelector('option[value="'+e.detail+'"]'))setMode(e.detail);});
  document.querySelectorAll("[data-group-tab]").forEach(function(tab){
    tab.addEventListener("click",function(){
      var name=tab.getAttribute("data-group-tab");
      var box=tab.parentNode.querySelector(".cmd-groupbtns");
      var open=box.hasAttribute("hidden");
      document.querySelectorAll(".cmd-groupbtns").forEach(function(b){b.setAttribute("hidden","");});
      document.querySelectorAll(".cmd-grptab").forEach(function(b){b.classList.remove("on");});
      if(open){box.removeAttribute("hidden");tab.classList.add("on");}
    });
  });
  var undoBtn=document.getElementById("cmd-undo");
  if(undoBtn){undoBtn.addEventListener("click",function(){tempPts.pop();redrawTemp();});}
  function redrawTemp(){
    if(tempLayer){map.removeLayer(tempLayer);tempLayer=null;}
    if(!mode||tempPts.length===0)return;
    if(GEOM[mode]==="polygon"&&tempPts.length>=3){tempLayer=L.polygon(tempPts,{color:"#FF653B",dashArray:"6 5",fillOpacity:.35}).addTo(map);}
    else if(tempPts.length>=2){tempLayer=L.polyline(tempPts,{color:"#FF653B",dashArray:"6 5",weight:4,opacity:1}).addTo(map);}
    else{tempLayer=L.circleMarker(tempPts[0],{radius:7,color:"#FF653B",fillOpacity:.8}).addTo(map);}
  }
  function fillForm(pts){
    if(mode==="arrow"&&window.PojuWideArrow){window.PojuWideArrow.prepare(map,pts,function(coords){if(FORM_GEO)FORM_GEO.value=coords.map(function(p){return p[0].toFixed(6)+","+p[1].toFixed(6);}).join(";");});}
    if(FORM_GEO&&mode!=="arrow"){FORM_GEO.value=pts.map(function(p){return p[0].toFixed(5)+","+p[1].toFixed(5);}).join(";");}
    if(FORM_KIND&&mode){FORM_KIND.value=mode;showFields(mode);}
    var d=document.getElementById("cmd-anno-float");
    if(d){d.hidden=false;}
    document.dispatchEvent(new CustomEvent("sand:drawing-ready"));
    var pv=document.getElementById("cmd-anno-preview");
    if(pv){pv.textContent="预览顶点 "+pts.length+" 个（坐标已自动回填）";}
    if(FORM_TITLE){FORM_TITLE.focus();}
  }
  var cancelBtn=document.getElementById("cmd-anno-cancel");
  if(cancelBtn){cancelBtn.addEventListener("click",function(){
    if(window.PojuWideArrow)window.PojuWideArrow.clear();
    // 取消绘制：不生成对象、不写业务审计
    var f=document.getElementById("cmd-anno-float");if(f){f.hidden=true;}
    if(FORM_GEO){FORM_GEO.value="";}
    setMode(null);
  });}
  function finish(force){
    if(!mode)return;
    if(tempPts.length<need(mode)&&!force){
      banner("顶点不足："+(document.querySelector('.cmd-tool[data-draw-kind="'+mode+'"]')?
        document.querySelector('.cmd-tool[data-draw-kind="'+mode+'"]').textContent:mode)
        +"至少需要 "+need(mode)+" 个顶点，当前 "+tempPts.length+" 个。","warn");
      return;
    }
    fillForm(tempPts);
    setMode(null);
  }
  var finBtn=document.getElementById("cmd-finish");
  if(finBtn){finBtn.addEventListener("click",function(){finish(false);});}
  map.on("dblclick",function(){if(mode&&GEOM[mode]!=="point"){finish(false);}});
  map.on("click",function(ev){
    if(!mode)return;
    var p=[ev.latlng.lat,ev.latlng.lng];
    if(GEOM[mode]==="point"){tempPts=[p];fillForm(tempPts);setMode(null);return;}
    tempPts.push(p);redrawTemp();
  });
  var annoForm=document.getElementById("sand-anno-form");
  if(annoForm){annoForm.addEventListener("submit",function(){
    if(window.PojuWideArrow)window.PojuWideArrow.clear();
    if(tempLayer){map.removeLayer(tempLayer);tempLayer=null;}
  });}
  if(FORM_KIND){FORM_KIND.addEventListener("change",function(){showFields(FORM_KIND.value);});}
  if(FORM_KIND){showFields(FORM_KIND.value);}

  // —— 推演公共层：目标圈 + 预测火线。整张图只画一份，与具体哪套方案无关；
  //    这样切方案时只有方案自己的对象在变，不会被重复的红色火线糊住。
  var ringLayer=L.layerGroup(), projLayer=L.layerGroup();
  (((DATA.proj||{}).rings)||[]).forEach(function(r){
    L.circle(r.c,{radius:r.r_m,color:"#C08A1A",weight:2,dashArray:"6 5",opacity:.7,fillOpacity:.05})
      .bindPopup("目标圈 · 风险半径 "+r.r_m+" 米（规则推演·非实测）").addTo(ringLayer);
  });
  (((DATA.proj||{}).targets)||[]).forEach(function(t){
    var c=t.inside?"#B84A42":"#2F7D68";
    L.circleMarker(t.pt,{radius:7,color:c,fillColor:c,fillOpacity:.9,weight:2})
      .bindPopup(t.label+" · "+(t.inside?"圈内·有风险":"圈外（目标圈推演）"))
      .addTo(ringLayer);
  });
  (((DATA.proj||{}).steps)||[]).forEach(function(st,i){
    var op=Math.max(.45,.95-i*.18);
    (st.lines||[]).forEach(function(ln){
      L.polyline(ln,{color:"#B84A42",weight:2,opacity:op,dashArray:"4 6"})
        .bindPopup("起火后 "+st.hours+" 小时 · 预测火线（规则推演，非实测）").addTo(projLayer);
    });
    (st.spots||[]).forEach(function(cp){
      L.circle(cp,{radius:st.r_m,color:"#B84A42",weight:2,opacity:op,dashArray:"4 6",fillOpacity:.05})
        .bindPopup("起火后 "+st.hours+" 小时 · 火点最大蔓延半径（规则推演，非实测）").addTo(projLayer);
    });
  });
  var ringCb=document.getElementById("cmd-layer-rings");
  var projCb=document.getElementById("cmd-layer-proj");
  function flipSwitch(cb,layer){
    if(!cb)return;
    if(cb.checked){layer.addTo(map);}else{map.removeLayer(layer);}
  }
  if(ringCb){ringCb.addEventListener("change",function(){flipSwitch(ringCb,ringLayer);saveSim();});}
  if(projCb){projCb.addEventListener("change",function(){flipSwitch(projCb,projLayer);saveSim();});}

  // —— 方案叠加：默认一次只看一套（上图新一套自动下掉旧的一套）；
  //    要看方案之间的差别才开「多方案对比」。上图只改显示，绝不写回态势。
  var SIMON={}, COMPARE=false, SNAP=null;
  var LSKEY="panda-cmd-sim:"+(DATA.event||"default");
  function allBoxes(){return document.querySelectorAll("[data-cmd-layer],[data-layer-group]");}
  function boxKey(cb){
    var s=cb.getAttribute("data-cmd-layer");
    return s?("state:"+s):("group:"+cb.getAttribute("data-layer-group"));
  }
  function setBox(sel,on){
    var cb=document.querySelector(sel);
    if(!cb)return;
    if(cb.checked!==!!on){
      cb.checked=!!on;
      // 必须发 change：图层的真实开关是 STATEF/LAYERON，只改 checked 不会重绘。
      cb.dispatchEvent(new Event('change',{bubbles:true}));
    }
  }
  function kick(cb,on){
    if(!cb||cb.checked===!!on)return;
    cb.checked=!!on;
    cb.dispatchEvent(new Event('change',{bubbles:true}));
  }
  function snapDisplay(){
    var o={};
    allBoxes().forEach(function(cb){o[boxKey(cb)]=cb.checked;});
    o.rings=!!(ringCb&&ringCb.checked);
    o.proj=!!(projCb&&projCb.checked);
    return o;
  }
  function applyDisplay(o){
    if(!o)return;
    allBoxes().forEach(function(cb){
      var k=boxKey(cb);
      if(o[k]!==undefined)kick(cb,o[k]);
    });
    kick(ringCb,o.rings);
    kick(projCb,o.proj);
  }
  function findSim(id){
    var r=null;
    (DATA.sims||[]).forEach(function(s){if(s.id===id)r=s;});
    return r;
  }
  function buildSimLayer(sim){
    var g=L.layerGroup();
    var col=sim.color||"#55606B";
    (sim.shapes||[]).forEach(function(sh){
      var w=sh.verdict?sh.verdict.word:"";
      var col2=w==="来不及"?"#B84A42":(w==="勉强"?"#C08A1A":col);
      var lay;
      if(sh.geom==="point"){lay=L.circleMarker(sh.pts[0],{radius:8,color:col2,dashArray:"6 5",fillOpacity:.5,weight:3});}
      else if(sh.geom==="polygon"){lay=L.polygon(sh.pts,{color:col2,weight:3,dashArray:"8 6",fillOpacity:.12});}
      else{lay=L.polyline(sh.pts,{color:col2,weight:3,dashArray:"8 6"});}
      lay.bindPopup("方案"+sim.letter+" 推演："+(sh.label||sim.title)
        +(w?(" · "+w+"：火至 "+sh.verdict.arrive_h+" 小时 · 作业 "+sh.verdict.work_h+" 小时（规则推演·非实测）"):"")
        +"（推演层，非正式态势）");
      lay.addTo(g);
    });
    return g;
  }
  function restyle(){
    var multi=COMPARE&&Object.keys(SIMON).length>1;
    Object.keys(SIMON).forEach(function(id){
      SIMON[id].eachLayer(function(l){
        if(l.setStyle)l.setStyle({opacity:multi?.72:.95,fillOpacity:multi?.06:.12});
      });
    });
  }
  function dropLayer(id){
    if(!SIMON[id])return;
    map.removeLayer(SIMON[id]);
    delete SIMON[id];
  }
  function simAdd(id){
    var sim=findSim(id);
    if(!sim)return;
    if(!SNAP)SNAP=snapDisplay();
    if(!COMPARE){Object.keys(SIMON).forEach(function(k){if(k!==id)dropLayer(k);});}
    if(!SIMON[id])SIMON[id]=buildSimLayer(sim);
    SIMON[id].addTo(map);
    kick(projCb,true);
    syncStrip();
  }
  function simRemove(id){
    dropLayer(id);
    if(!Object.keys(SIMON).length){
      kick(projCb,false);
      if(SNAP){applyDisplay(SNAP);SNAP=null;}
    }
    syncStrip();
  }
  // 全部下图＝回到原始态势视图：推演层、目标圈、影像覆盖、候选/草稿显示一并撤掉。
  // 只动显示开关，不删任何一条标注数据；条上「恢复上图前显示」可把开关还原。
  function simClearAll(){
    Object.keys(SIMON).forEach(function(k){dropLayer(k);});
    kick(projCb,false);
    kick(ringCb,false);
    syncStrip();
  }
  window.__cmdSimulationClear=simClearAll;
  function setCompare(on){
    COMPARE=!!on;
    if(!COMPARE){
      var ids=Object.keys(SIMON);
      ids.slice(0,Math.max(0,ids.length-1)).forEach(function(k){dropLayer(k);});
    }
    restyle();
    syncStrip();
  }
  function saveSim(){
    try{localStorage.setItem(LSKEY,JSON.stringify({ids:Object.keys(SIMON),compare:COMPARE}));}catch(e){}
  }
  function syncStrip(){
    var el=document.getElementById("cmd-simstrip");
    document.querySelectorAll("[data-sim]").forEach(function(b){
      var on=!!SIMON[b.getAttribute("data-sim")];
      var t=b.querySelector(".sim-t");
      if(t)t.textContent=on?"下图":"上图";
      b.classList.toggle("on",on);
      b.setAttribute("aria-pressed",on?"true":"false");
    });
    document.querySelectorAll(".cmd-plan-card").forEach(function(c){
      var cid=(c.getAttribute("id")||"").replace("cmd-plan-","");
      c.classList.toggle("on",!!SIMON[cid]);
    });
    if(!el)return;
    var ids=Object.keys(SIMON);
    if(!ids.length){
      if(SNAP){
        // 刚做过「全部下图」：条子必须留着，否则司令不知道是谁关了图层、也找不到还原入口。
        el.style.display="flex";
        el.innerHTML='<b>已恢复原始态势视图</b>'
          +'<span class="dim">推演已下图；现场标绘与影像保持</span>'
          +'<button type="button" class="lz-btn lz-btn2" id="cmd-sim-restore">恢复上图前显示</button>';
        var rs0=document.getElementById("cmd-sim-restore");
        if(rs0){rs0.addEventListener("click",function(){applyDisplay(SNAP);SNAP=null;syncStrip();});}
        saveSim();
        return;
      }
      el.style.display="none";el.innerHTML="";saveSim();return;
    }
    var names=ids.map(function(id){
      var s=findSim(id)||{};
      return '<span class="sim-chip" style="border-color:'+(s.color||"#55606B")+'">'
        +'<i style="background:'+(s.color||"#55606B")+'"></i>'
        +escx(s.letter||"?")+" · "+escx(s.title||"")+"</span>";
    }).join("");
    el.style.display="flex";
    el.innerHTML="<b>已上图</b>"+names
      +'<label class="sim-cmp"><input type="checkbox" id="cmd-sim-compare"'
      +(COMPARE?" checked":"")+">多方案对比</label>"
      +'<button type="button" class="lz-btn lz-btn2" id="cmd-sim-clear">全部下图（恢复原始态势）</button>'
      +(SNAP?'<button type="button" class="lz-btn lz-btn2" id="cmd-sim-restore">恢复上图前显示</button>':"");
    var cmp=document.getElementById("cmd-sim-compare");
    if(cmp){cmp.addEventListener("change",function(){setCompare(cmp.checked);});}
    var cl=document.getElementById("cmd-sim-clear");
    if(cl){cl.addEventListener("click",simClearAll);}
    var rs=document.getElementById("cmd-sim-restore");
    if(rs){rs.addEventListener("click",function(){applyDisplay(SNAP);SNAP=null;syncStrip();});}
    restyle();
    saveSim();
  }
  document.querySelectorAll("[data-sim]").forEach(function(b){
    b.addEventListener("click",function(){
      var id=b.getAttribute("data-sim");
      if(SIMON[id]){simRemove(id);}else{simAdd(id);}
    });
    var sim=findSim(b.getAttribute("data-sim"));
    var card=b.closest?b.closest(".cmd-plan-card"):null;
    if(sim&&card){
      card.setAttribute("data-plan-color",sim.color||"#55606B");
      card.style.setProperty("--plan-color",sim.color||"#55606B");
    }
  });
  try{
    var saved=JSON.parse(localStorage.getItem(LSKEY)||"null");
    if(saved&&saved.compare){COMPARE=true;}
    if(saved&&saved.ids&&saved.ids.length){
      var list=saved.ids.slice();
      if(!COMPARE&&list.length>1){list=list.slice(list.length-1);}
      list.forEach(function(id){if(findSim(id))simAdd(id);});
    }
  }catch(e){}
  syncStrip();
  }
  function tabs(){
    document.querySelectorAll(".cmd-tab").forEach(function(b){
      b.addEventListener("click",function(){
        var side=document.getElementById("cmd-side");
        if(!side)return;
        side.setAttribute("data-tab",b.getAttribute("data-cmdtab")||"advisor");
        document.querySelectorAll(".cmd-tab").forEach(function(x){x.classList.toggle("on",x===b);});
      });
    });
  }
  if(document.readyState==="loading"){
    document.addEventListener("DOMContentLoaded",function(){tabs();boot();});
  }else{tabs();boot();}
})();
"""


def _map_shell(state, role: str) -> str:
    role_word = {
        "commander": "指挥员",
        "member": "现场人员",
        "public": "公共屏 · 仅已发布内容",
    }.get(role, role)
    layer_rows = []
    for spec in tm.LAYERS:
        layer_rows.append(
            f'<label class="cmd-layerrow"><input type="checkbox" '
            f'{"checked" if spec.default_on else ""} data-layer-group="{spec.key}">'
            f'{esc(spec.label)}<span class="lg-idx">渲染 {spec.render_layer:02d}</span></label>'
        )
    state_rows = []
    for key, name, on in (("published", "正式态势（含已核验）", True),
                          ("candidate", "待核验", True),
                          ("draft", "草稿", True),
                          ("history", "历史与已撤回", False)):
        state_rows.append(
            f'<label class="cmd-layerrow"><input type="checkbox" {"checked" if on else ""} '
            f'data-cmd-layer="{key}">{esc(name)}</label>'
        )
    # 底图来源：离线包存在则默认离线；在线源均标注用途，不隐藏偏移事实。
    event = state.registry.events.get(getattr(state, 'active_event_id', ''))
    center = ([float(event.lat), float(event.lon)]
              if event and event.lat is not None and event.lon is not None else None)
    offline_status, satellite = _map_sources(state, center)
    offline = bool(offline_status["resource"])
    base_opts = ([("local", "本地离线瓦片包")] if offline else []) + ([("satellite", "本机离线卫星影像")] if satellite else []) + [
        ("gaode", "高德地图（在线）"),
        ("gaode_img", "高德影像（在线）"),
        ("opentopo", "开源地形（在线，非影像）"),
        ("osm", "开放街道地图（在线）"),
    ]
    cur = "unavailable" if offline_status["error"] else "local" if offline else "satellite" if satellite else "gaode"
    base_rows = (
        '<div class="lg-row dim">底图来源</div>'
        + "".join(
            f'<label class="cmd-layerrow"><input type="radio" name="cmd-base" '
            f'data-base-key="{k}"{" checked" if k == cur else ""}>{esc(n)}</label>'
            for k, n in base_opts
        )
        + (
            '<div class="lg-row dim">当前事件授权资源就绪；在离线资料入口完整缓存后可断网查看。</div>'
            if offline
            else '<div class="lg-row dim">本机离线卫星影像已接入；缺图区域需补充对应瓦片。</div>' if satellite
            else '<div class="lg-row dim">当前没有有效授权离线底图；请由指定副指挥长在离线资料入口导入并声明许可。</div>'
        )
        + ('<div class="lg-row dim">' + esc(offline_status['error']) + '</div>' if offline_status['error'] else '')
        + '<div class="lg-row dim" id="cmd-base-notice">'
        + (esc(satellite['note']) if satellite else '离线包的来源、坐标系和覆盖范围尚未经核对；不能据此认定标绘精度。' if offline
           else '高德底图采用国内偏移坐标，与地理标绘可能相差数十至数百米；仅供示意。')
        + '</div>'
        + '<div class="lg-row dim" id="cmd-base-credit">'
        + ('用户提供的 Google 卫星影像' if satellite else '本地离线瓦片包（按来源定精度，无在线依赖）' if offline else '底图 © 高德地图')
        + '</div>'
    )
    tools = ""
    if role == "commander":
        groups = []
        for label, kinds in tm.TOOLBAR_GROUPS:
            btns = []
            for kind in kinds:
                gid = ' id="cmd-draw-point"' if kind == "hotspot" else ""
                btns.append(
                    f'<button type="button" class="cmd-tool"{gid} '
                    f'data-draw-kind="{kind}" '
                    f'title="{esc(tm.kind_spec(kind).rule or "图上绘制" + tm.kind_word(kind))}">'
                    f'{esc(tm.kind_word(kind))}</button>'
                )
            groups.append(
                f'<div class="cmd-toolgroup" data-toolgroup="{esc(label)}">'
                f'<button type="button" class="cmd-grptab" data-group-tab="{esc(label)}">'
                f'{esc(label)}</button>'
                f'<div class="cmd-groupbtns" hidden>{"".join(btns)}</div></div>'
            )
        tools = (
            '<div class="cmd-toolgroups">' + "".join(groups) + "</div>"
            '<div class="cmd-toolgroup cmd-toolacts">'
            '<button type="button" class="cmd-tool" id="cmd-undo">撤销点</button>'
            '<button type="button" class="cmd-tool" id="cmd-finish" style="display:none">完成绘制</button>'
            "</div>"
        )
    defs = (
        '<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
        '<defs>'
        '<pattern id="cmd-pat-hatch" width="8" height="8" patternTransform="rotate(45)" '
        'patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="rgba(211,84,0,.10)"/>'
        '<line x1="0" y1="0" x2="0" y2="8" stroke="rgba(184,74,66,.55)" stroke-width="2.4"/></pattern>'
        '<pattern id="cmd-pat-hatch2" width="9" height="9" patternTransform="rotate(-45)" '
        'patternUnits="userSpaceOnUse"><rect width="9" height="9" fill="rgba(127,79,36,.10)"/>'
        '<line x1="0" y1="0" x2="0" y2="9" stroke="rgba(95,60,30,.5)" stroke-width="2"/></pattern>'
        '</defs></svg>'
    )
    conflict_note = (
        '<button type="button" class="cmd-conflictnote" id="cmd-conflict-note" style="display:none"></button>'
        if role != "public" else ""
    )
    return (
        '<section class="cmd-mapwrap">'
        + (f'<div class="cmd-mapbar">{tools}</div>' if tools else "")
        + '<div class="cmd-maparea">'
        + defs
        + '<div class="cmd-mapbanner" id="cmd-map-banner" style="display:none"></div>'
        + f'<div id="cmd-map" class="cmd-map" aria-label="指挥调度真实地图（{esc(role_word)}）"></div>'
        + '<details class="cmd-maplegend"><summary>图例/图层</summary>'
        + '<div class="cmd-maplegend-body">'
        + f'<div class="cmd-maprole">{esc(role_word)}</div>'
        + '<div class="lg-row dim">业务图层（6 组）</div>'
        + "".join(layer_rows)
        + '<div class="lg-row dim">状态图层</div>'
        + "".join(state_rows)
        + base_rows
        + '<div class="lg-row"><span class="lg-swatch" style="background:#C0392B"></span>实线实心＝已发布</div>'
        + '<div class="lg-row"><span class="lg-swatch" style="background:#8E1B14;opacity:.6"></span>实线较高透明＝已核验·待发布</div>'
        + '<div class="lg-row"><span class="lg-swatch" style="background:#C07A1A"></span>橙虚线＝待核验</div>'
        + '<div class="lg-row"><span class="lg-swatch" style="background:#647382"></span>灰虚线＝草稿</div>'
        + '<div class="lg-row"><span class="lg-hatch"></span>斜纹＝危险区/过火区/推演</div>'
        + '<div class="lg-row"><span class="lg-belt"></span>宽带＝隔火带（现场宽度看米制字段）</div>'
        + ("" if role == "public" else
           '<label class="cmd-layerrow"><input type="checkbox" id="cmd-layer-proj">预测火线（推演）</label>'
           '<label class="cmd-layerrow"><input type="checkbox" id="cmd-layer-rings">目标圈（推演）</label>'
           f'<div class="lg-row dim">{esc(sand_sim.CALIBER)}</div>')
        + "</div></details>"
        + conflict_note
        + ("" if role == "public" else '<div class="cmd-simstrip" id="cmd-simstrip" style="display:none"></div>')
        + '<div class="cmd-modehint" id="cmd-mode-hint"></div>'
        + _anno_float_form(role)
        + "</div>"
        f'<script type="application/json" id="cmd-map-data">{_map_payload(state, role)}</script>'
        '<link rel="stylesheet" href="/static/leaflet/leaflet.css">'
        '<script src="/static/leaflet/leaflet.js"></script>'
        '<script src="/static/offline-map.js"></script>'
        '<script src="/static/wide-arrow.js"></script>'
        f"<script>{_CMD_MAP_JS}</script>"
        "</section>"
    )


# ---------------------------------------------------------------------------
# 左侧工具栏（入口锚点；完整表单都在抽屉）


def _tools_rail(state, role: str) -> str:
    def item(href: str, icon: str, label: str, drawer: str) -> str:
        return (
            f'<a class="cmd-railbtn" href="#{drawer}" onclick="var d=document.getElementById(\'{drawer}\');'
            f'if(d)d.open=true" title="{esc(label)}"><span class="i">{icon}</span>'
            f'<span class="t">{esc(label)}</span></a>'
        )

    items = []
    if role == "commander":
        items += [
            item("#drawer-annotate", "✎", "标绘作业", "drawer-annotate"),
            item("#drawer-measure", "∑", "测算", "drawer-measure"),
            item("#drawer-imagery", "✈", "无人机影像", "drawer-imagery"),
            item("#drawer-intent", "⌘", "指令草稿", "drawer-intent"),
        ]
    items += [
        item("#drawer-qa", "?", "问询记录", "drawer-qa"),
        item("#drawer-fourcat", "☰", "信息全景", "drawer-fourcat"),
    ]
    return (
        '<nav class="cmd-rail" id="cmd-rail">'
        + "".join(items)
        + "</nav>"
    )


def _field_control(key: str, *, required: bool) -> str:
    """按对象类型生成专属输入（中文标签 + 受控下拉），不再共用“名称+备注”。"""
    label = tm.detail_word(key)
    hint = "提交核验必填" if required else "发布前补充"
    opts = tm.controlled_options(key)
    if opts:
        rows = "".join(f'<option value="{esc(v)}">{esc(w)}</option>' for v, w in opts)
        return (
            f'<label class="cmd-fld"><span>{esc(label)}<i>{hint}</i></span>'
            f'<select class="lz-input" name="d_{esc(key)}">{rows}</select></label>'
        )
    if key in {"basis", "affected", "task", "purpose", "reason", "release_cond",
               "endpoint_basis", "watch_content", "bypass", "alt_comm"}:
        return (
            f'<label class="cmd-fld"><span>{esc(label)}<i>{hint}</i></span>'
            f'<textarea class="lz-input" name="d_{esc(key)}" rows="2"></textarea></label>'
        )
    numeric = key in {"planned_width", "actual_width", "gap_width_m", "completion",
                      "supply_amount", "stock", "beds", "speed"}
    ph = _FIELD_PLACEHOLDER.get(key, "无来源时写待核实")
    return (
        f'<label class="cmd-fld"><span>{esc(label)}<i>{hint}</i></span>'
        f'<input class="lz-input" type="{"number" if numeric else "text"}" '
        f'name="d_{esc(key)}" placeholder="{esc(ph)}"></label>'
    )


_FIELD_PLACEHOLDER = {
    "observed_at": "例 2026-08-27 14:20",
    "checked_at": "例 2026-08-27 15:00",
    "reported_at": "例 2026-08-27 15:05",
    "captured_at": "例 2026-08-27 14:40",
    "found_at": "例 2026-08-27 14:55",
    "measured_at": "例 2026-08-27 15:00",
    "formed_at": "例 2026-08-27 14:50",
    "burned_at": "例 2026-08-27 上午",
    "active_since": "例 2026-08-27 13:30",
    "safety_confirm_at": "例 2026-08-27 14:10",
    "gap_reported_at": "例 2026-08-27 15:10",
    "last_report": "无回报时留空",
    "plan_time": "例 2026-08-27 16:00 前",
    "source_ref": "例 现场报告-12",
    "gap_source": "例 无人机影像-3",
    "planned_width": "米",
    "actual_width": "米（已开设必填）",
    "gap_width_m": "米",
    "completion": "例 60%",
    "opacity": "例 0.35",
    "direction": "例 东北",
    "heading": "例 45（度）",
    "speed": "无实测请留空",
    "supply_amount": "无来源请留空（显示待核实）",
    "stock": "无来源请留空（显示待核实）",
    "beds": "无来源请留空（显示待核实）",
}


def _kind_fieldset(kind: str) -> str:
    spec = tm.kind_spec(kind)
    keys = tm.kind_field_keys(kind)
    fields = "".join(
        _field_control(key, required=key in spec.verify_fields) for key in keys
    )
    rule = f'<p class="cmd-fldrule">{esc(spec.rule)}</p>' if spec.rule else ""
    return (
        f'<div class="cmd-fields" data-fields-for="{esc(kind)}" style="display:none">'
        f'<b>{esc(spec.label)}专属字段</b>{rule}{fields}</div>'
    )


def _anno_float_form(role: str) -> str:
    """图上绘制后的旁侧信息窗：按类型显示专属字段，坐标由绘制自动回填。

    前台不出现任何坐标输入框（对齐 2026-08-26 口径）；
    核验链与坐标补录仍在「标绘作业」抽屉。
    """
    if role != "commander":
        return ""
    groups = []
    for label, kinds in tm.TOOLBAR_GROUPS:
        options = "".join(
            f'<option value="{esc(k)}">{esc(tm.kind_word(k))}</option>' for k in kinds
        )
        groups.append(f'<optgroup label="{esc(label)}">{options}</optgroup>')
    kind_options = "".join(groups) + '<optgroup label="参考图通用图形"><option value="fire_point">点</option><option value="route">折线/曲线</option><option value="area">圆/椭圆/矩形/多边形</option><option value="arrow">宽尾弯箭头</option><option value="text">文字</option></optgroup>'
    fieldsets = "".join(
        _kind_fieldset(k) for _, kinds in tm.TOOLBAR_GROUPS for k in kinds
    )
    return (
        '<div class="cmd-anno-float" id="cmd-anno-float" hidden>'
        '<form method="post" action="/sand/annotate" id="sand-anno-form">'
        '<b>标绘信息（坐标已由绘制自动回填）</b>'
        '<p class="cmd-anno-prev" id="cmd-anno-preview">尚未选点</p>'
        f'<select name="kind" class="lz-input" id="cmd-anno-kind">{kind_options}</select>'
        '<input type="hidden" name="geo" id="cmd-anno-geo" value="">'
        '<input type="hidden" name="d_manual_style" value="reference-v1">'
        '<input type="hidden" name="d_manual_shape" value="">'
        '<label class="manual-style-fields">新标绘颜色<select name="d_manual_color">'
        '<option value="red">亮红橙</option><option value="yellow">亮黄</option>'
        '<option value="purple">亮紫</option><option value="cyan">亮蓝</option></select></label>'
        '<input name="title" class="lz-input" id="cmd-anno-title" '
        'placeholder="名称，如 东线重点火线" required maxlength="40">'
        + fieldsets
        + '<label class="cmd-fld"><span>备注<i>可空</i></span>'
          '<input name="note" class="lz-input" maxlength="500" '
          'placeholder="只记原文，不能替代上面的必填项"></label>'
        '<div class="cmd-anno-float-acts">'
        '<button class="lz-btn" name="action" value="draft">保存草稿</button>'
        '<button class="lz-btn lz-btn2" name="action" value="verify">提交核验</button>'
        '<button type="button" class="lz-btn lz-btn2" id="cmd-anno-cancel">取消</button></div>'
        '</form></div>'
    )


# ---------------------------------------------------------------------------
# 参谋对话框（右侧上）


def _goal_line(state, role="commander") -> str:
    goals = getattr(state.workspace, "goal_frames", {}) or {}
    latest = None
    for g in sorted(goals.values(), key=lambda x: x.id):
        latest = g
    if latest is None:
        hint = '展开“设置目标与其他操作”填写。' if role == 'commander' else '目标由指挥员登记。'
        return f'<p class="cmd-goal dim">已自动汇集当前事件资料；处置目标待补充——{hint}</p>'
    mark = "已确认" if getattr(latest, "confirmed_by", None) else "待司令确认"
    return (
        f'<p class="cmd-goal"><b>目标：</b>{esc(latest.statement)[:80]} '
        f'<span class="lz-chip {"lz-chip-ok" if latest.confirmed_by else "lz-chip-warn"}">{mark}</span></p>'
    )


def _advisor_cards(state, role: str, stage_view) -> str:
    """按消息类型输出参谋业务卡（docs/18 §7）。只述业务结果，零 AI 自述。"""
    ws = state.workspace
    commander = role == "commander"
    out: list[str] = []

    if stage_view.overlay:
        blocked = [
            o for o in ws.orders.values()
            if o.status.value == "paused"
        ]
        pause_forms = ""
        if commander:
            pause_forms = "".join(
                f'<form method="post" action="/pause" class="lz-inlineform">'
                f'<input type="hidden" name="order_id" value="{esc(o.id)}">'
                f'<input type="hidden" name="reason" value="指挥员处置异常命令">'
                f'<button class="lz-btn lz-btn2">标记处置 {esc(o.id)}</button></form>'
                for o in blocked[:2]
            )
        out.append(
            '<div class="cmd-msg cmd-msg-danger"><p class="t">【异常报告】存在受阻／暂停命令</p>'
            f'<p>相关命令已冻结等待重新研判；恢复或改令须经指挥员处置。</p>{pause_forms}</div>'
        )

    open_gaps = [
        g for g in ws.information_gaps.values()
        if g.status == InformationGapStatus.OPEN
    ]
    for gap in open_gaps[:3]:
        actions = ""
        if commander:
            actions = (
                f'<a class="lz-btn lz-btn2" href="#drawer-imagery" '
                f'onclick="var d=document.getElementById(\'drawer-imagery\');if(d)d.open=true">申请补充影像</a> '
                f'<form method="post" action="/sand/gap-defer" class="lz-inlineform">'
                f'<input type="hidden" name="gap_id" value="{esc(gap.id)}">'
                f'<button class="lz-btn lz-btn2">按现有资料继续</button></form>'
            )
        out.append(
            f'<div class="cmd-msg cmd-msg-warn"><p class="t">【信息缺口】{esc(gap.subject)}</p>'
            f'<p class="dim">{esc((gap.reason or "")[:90])}</p>'
            f'<div class="acts">{actions}</div></div>'
        )

    cand_n = sum(
        1 for m in ws.annotations.values()
        if m.status == AnnotationStatus.PENDING_REVIEW
        or (m.source == AnnotationSource.AI_SUGGESTION and m.status == AnnotationStatus.DRAFT)
    )
    if cand_n:
        out.append(
            '<div class="cmd-msg cmd-msg-info"><p class="t">【待核验候选】'
            f'{cand_n} 条后台候选待处理</p>'
            '<p class="dim">候选以虚线上图，未经核验不进入正式态势。</p>'
            f'<div class="acts"><a class="lz-btn lz-btn2" href="#drawer-annotate" '
            f'onclick="var d=document.getElementById(\'drawer-annotate\');if(d)d.open=true">去核验</a></div></div>'
        )

    briefs = getattr(ws, "decision_briefs", {}) or {}
    if briefs:
        brief = max(briefs.values(), key=lambda b: b.source_version)
        rec = (brief.recommendation or "").strip()
        out.append(
            '<div class="cmd-msg cmd-msg-info"><p class="t">【判断汇报】最新决策简报</p>'
            + (f"<p>{esc(rec[:110])}</p>" if rec else "")
            + f'<p class="dim">议题：{esc(brief.question[:70])}</p>'
            + (f'<div class="acts"><a class="lz-btn lz-btn2" href="#cmd-plans">查看方案区</a></div>' if role != "public" else "")
            + "</div>"
        )

    active_opts = [o for o in ws.options.values() if getattr(o, "status", "") == "active"]
    if active_opts:
        out.append(
            '<div class="cmd-msg cmd-msg-info"><p class="t">【方案汇报】'
            f'已形成 {len(active_opts)} 套在议方案</p>'
            '<div class="acts"><a class="lz-btn lz-btn2" href="#cmd-plans">比较并推演</a></div></div>'
        )

    decisions = sorted(getattr(ws, "decisions", {}).values(), key=lambda d: d.id)
    for d in decisions[-2:]:
        word = _DECISION_WORDS.get(d.decision_type, str(d.decision_type.value))
        out.append(
            f'<div class="cmd-msg"><p class="t">【决断记录】{word}'
            f'{(" · " + d.option_id) if d.option_id else ""}</p>'
            f'<p class="dim">{esc(d.reason[:90])}</p></div>'
        )

    terminal = set(TERMINAL_ORDER_STATUSES)
    live_orders = [
        o for o in ws.orders.values() if o.status not in terminal
    ]
    for o in live_orders[-3:]:
        out.append(
            f'<div class="cmd-msg"><p class="t">【执行反馈】{esc(o.id)}</p>'
            f'<p class="dim">状态：{esc(_order_word(o.status))}</p></div>'
        )

    for entry in list(getattr(state, "advisor_log", []) or [])[-4:]:
        out.append(
            f'<div class="cmd-msg cmd-msg-dim"><p class="t">【系统通知】{esc(entry.get("type", ""))}</p>'
            f'<p class="dim">{esc(str(entry.get("text", ""))[:110])}</p></div>'
        )

    if not out:
        out.append(
            '<div class="cmd-msg cmd-msg-dim"><p class="t">【就绪】</p>'
            "<p>当前事件资料已自动汇集；可补充目标、追问依据或要求生成预案。"
            "信息缺口会持续提示。</p></div>"
        )
    return "".join(out)


def _situation_block(state, role: str) -> str:
    """P20 态势复核区（docs/20 §8.1）：数据截至/上次复核/待复核变化。

    只述业务结果；定位只高亮不移动视角之外的东西；
    复核与处理动作仅指挥员可用；公共屏不出本区块。
    """
    if role == "public":
        return ""
    ws = state.workspace
    feed = ws.situation_feed()
    review_advice = grounded_inquiry.current_change_review_advice(
        ws, getattr(state, 'active_event_id', ''))
    advice_feed = grounded_inquiry.current_change_review_feed(
        ws, getattr(state, 'active_event_id', ''))
    commander = role == "commander"
    force_cue = force_review_cue(state) if commander else None
    chips = [
        '<span class="lz-chip" id="cmd-sit-data-at">数据截至 '
        + (_stamp(feed["latest_data_at"]) or "—") + '</span>',
        '<span class="lz-chip" id="cmd-sit-review-at">上次复核 '
        + (_stamp(feed["latest_review_at"]) or "尚未复核") + '</span>',
        '<span id="cmd-sit-count" class="lz-chip'
        + (' lz-chip-warn"' if feed["pending_count"] else '"')
        + '>待复核变化 ' + str(feed["pending_count"]) + ' 项</span>',
        '<span id="cmd-sit-needs" class="lz-chip lz-chip-danger"'
        + ('' if feed["needs_commander_count"] else ' hidden')
        + '>需指挥员处理 ' + str(feed["needs_commander_count"]) + ' 项</span>',
    ]
    actions = ""
    if commander:
        actions = (
            '<form method="post" action="/sand/review-current" class="lz-inlineform">'
            '<button class="lz-btn lz-btn2">我已看最新情况</button></form>'
        )
    cards: list[str] = []
    for row in feed["changes"][:4]:
        acts = ""
        if row["locate"]:
            token = esc(json.dumps(row["locate"]))
            acts += (
                '<a class="lz-btn lz-btn2" href="#cmd-map" '
                f'onclick="var f=window.__cmdFocus;if(f)f({token});return false;">定位</a> '
            )
        if commander:
            cid = esc(row["id"])
            acts += (
                '<form method="post" action="/sand/change-action" class="lz-inlineform">'
                f'<input type="hidden" name="change_id" value="{cid}">'
                '<input type="hidden" name="action" value="ack">'
                '<button class="lz-btn lz-btn2">已阅</button></form> '
                '<form method="post" action="/sand/change-action" class="lz-inlineform">'
                f'<input type="hidden" name="change_id" value="{cid}">'
                '<input type="hidden" name="action" value="continue">'
                '<input name="reason" class="lz-input" placeholder="按现有资料继续的原因" required>'
                '<button class="lz-btn lz-btn2">继续</button></form>'
            )
        cand = (
            '<p class="dim">关联候选：' + esc("、".join(row["candidates"])) + '</p>'
            if row["candidates"] else ""
        )
        opt = (
            '<p class="dim">受影响方案：' + esc("、".join(row["options"])) + '</p>'
            if row["options"] else ""
        )
        notice = f'<p class="dim">{esc(row["notice"])}</p>' if row["notice"] else ""
        advice = review_advice.get(row['id'])
        advice_html = (
            f'<p class="dim">建议（待人工核查）：{esc(advice)}'
            f'依据：本事件变化记录 {esc(row["id"])}；不自动变更方案或命令。</p>'
            if advice else ''
        )
        tone = "" if row["severity"] == "notice" else "cmd-msg-warn"
        if row["needs_commander"]:
            tone = "cmd-msg-danger"
        cards.append(
            f'<div class="cmd-msg {tone}">'
            f'<p class="t">【{esc(row["kind_word"])}】{esc(row["severity_word"])}'
            f' · {esc(row["status_word"])}</p>'
            f'<p>{esc(row["reason"])}</p>{cand}{opt}{notice}{advice_html}'
            f'<p class="dim">发现时间 {_stamp(row["detected"])}</p>'
            f'<div class="acts">{acts}</div></div>'
        )
    kinds = ("hotspot", "spread_direction", "hazard_zone", "road_block", "wind_marker")
    kind_opts = "".join(
        f'<option value="{k}">{esc(tm.kind_word(k))}</option>' for k in kinds
    )
    pos_opts = "".join(
        f'<option value="{p.value}">{esc(tm.positioning_word(p.value))}</option>'
        for p in tm.PositioningStatus
    )
    observe = (
        '<details class="cmd-observe"><summary>上报现场观测</summary>'
        '<form method="post" action="/sand/observe" class="cmd-compose-row">'
        f'<select name="kind" class="lz-input">{kind_opts}</select>'
        '<input name="title" class="lz-input" placeholder="名称，如：东南坡新增烟点" required>'
        '<input name="geo" class="lz-input" placeholder="纬度,经度（多个顶点用分号分隔）" required>'
        '<input name="observed_at" class="lz-input" placeholder="观测时间，如 2026-08-28 13:40" required>'
        '<input name="source_ref" class="lz-input" placeholder="来源编号，如 无人机影像-9" required>'
        f'<select name="positioning_status" class="lz-input">{pos_opts}</select>'
        '<input name="note" class="lz-input" placeholder="补充说明（可留空）">'
        '<button class="lz-btn lz-btn2">提交待核验</button>'
        '</form><p class="dim">观测只会形成草稿或待核验候选与变化提醒，不会直接发布上图；'
        '定位不明时同样如实标注。</p></details>'
    )
    live = (
        '<div class="cmd-sitlive" id="cmd-sit-live" hidden data-feed="/sand/situation-feed" '
        f'data-base="{feed["pending_count"]}" '
        f'data-advice-version="{advice_feed["version"]}"></div>'
    )
    force_html = ''
    if force_cue:
        force_html = (
            f'<p class="lz-note" id="cmd-force-review" data-state="{esc(force_cue["state"])}"'
            + (' hidden' if not force_cue['message'] else '')
            + f'>{esc(force_cue["message"])}</p>'
        )
    return (
        '<section class="cmd-situation" id="cmd-situation">'
        '<header class="cmd-situation-head"><b>最新情况</b></header>'
        + force_html
        + chips[-1]
        + live
        + '<div class="cmd-situation-acts">' + actions + '</div>'
        + __import__('panda_brain.situation_review_ui', fromlist=['render']).render(state)
        + '<p class="cmd-situation-back"><a class="lz-btn" href="/workflow/guide">回九步走</a></p>'
        + '<details class="command-review-evidence"><summary>变化记录与完整依据</summary>'
        + "".join(chips[:-1])
        + render_source_review(state, role)
        + ("".join(cards) or '<p class="sand-empty">页面载入时暂无态势变化记录。可展开下方现场观测补充信息；无记录不代表无风险。轮询有新变化时请刷新查看完整卡片。</p>')
        + '</details>'
        + observe
        + "</section>"
        + _SITUATION_POLL_JS
    )


_SITUATION_POLL_JS = """
<script>
(function(){
  var el=document.getElementById("cmd-sit-live");
  if(!el)return;
  var dataAt=document.getElementById("cmd-sit-data-at");
  var reviewAt=document.getElementById("cmd-sit-review-at");
  var countChip=document.getElementById("cmd-sit-count");
  var needsChip=document.getElementById("cmd-sit-needs");
  var forceReview=document.getElementById("cmd-force-review");
  function stamp(raw){
    return raw?String(raw).replace("T"," ").split("+")[0].slice(0,16):"";
  }
  var base=parseInt(el.getAttribute("data-base")||"0",10);
  var adviceVersion=el.getAttribute("data-advice-version")||"";
  var url=el.getAttribute("data-feed");
  var unavailable=false;
  function showUnavailable(){
    unavailable=true;
    if(countChip)countChip.textContent="待复核变化 待重新核对";
    if(needsChip)needsChip.hidden=true;
    if(forceReview){forceReview.hidden=false;forceReview.textContent="人力需求状态待重新核对；当前页面可能过期。";}
    show(["态势更新暂不可用；当前页面可能过期，请刷新核对。"],true);
  }
  function show(lines,more){
    el.replaceChildren();
    lines.forEach(function(line){
      var p=document.createElement("p");p.textContent=line;el.appendChild(p);
    });
    if(more){
      var a=document.createElement("a");
      a.href="#cmd-situation";a.textContent="刷新页面查看完整变化";
      a.addEventListener("click",function(e){e.preventDefault();location.reload();});
      el.appendChild(a);
    }
    el.hidden=!lines.length;
  }
  function tick(){
    fetch(url,{credentials:"same-origin"}).then(function(r){
      if(!r.ok){showUnavailable();return null;}
      return r.json();
    }).then(function(d){
      if(!d||typeof d.pending_count!=="number")return;
      var wasUnavailable=unavailable;
      unavailable=false;
      if(dataAt)dataAt.textContent="数据截至 "+(stamp(d.latest_data_at)||"—");
      if(reviewAt)reviewAt.textContent="上次复核 "+(stamp(d.latest_review_at)||"尚未复核");
      if(countChip){
        countChip.textContent="待复核变化 "+d.pending_count+" 项";
        countChip.classList.toggle("lz-chip-warn",d.pending_count>0);
      }
      if(needsChip){
        var needs=Number(d.needs_commander_count)||0;
        needsChip.hidden=needs<=0;
        needsChip.textContent="需指挥员处理 "+needs+" 项";
      }
      if(forceReview&&d.force_review){
        forceReview.dataset.state=d.force_review.state;
        forceReview.textContent=d.force_review.message||"";
        forceReview.hidden=!d.force_review.message;
      }
      var newer=d.pending_count>base;
      base=d.pending_count;
      var advice=d.advice||{};
      if(typeof advice.version==="string"&&
          (advice.version!==adviceVersion||wasUnavailable)){
        adviceVersion=advice.version;
        var items=Array.isArray(advice.items)?advice.items:[];
        var lines=items.slice(0,3).map(function(item){
          return "变化 "+item.id+"（"+item.severity+"）："+item.reason
            +"；核查建议："+item.advice+" 依据：本事件变化记录 "+item.id;
        });
        if(lines.length){show(lines,true);return;}
        show([],false);
      }
      if(newer){
        show(["态势有新的待复核变化（"+d.pending_count+" 项）"],true);
      }
    }).catch(function(){showUnavailable();});
  }
  setInterval(tick,30000);
})();
</script>
"""


def _advisor_panel(state, role: str, stage_view) -> str:
    ws = state.workspace
    counts = pending_counts(ws)
    commander = role == "commander"
    compose = ""
    if commander:
        compose = (
            '<div class="cmd-compose" id="cmd-compose">'
            '<form method="post" action="/sand/goal" class="cmd-compose-row">'
            '<input name="statement" class="lz-input" placeholder="输入目标，如：保护东线村庄，控制火线不过青岗河" required>'
            '<button class="lz-btn lz-btn2">设为目标</button></form>'
            '<div class="cmd-compose-grid">'
            '<form method="post" action="/sand/ask" class="cmd-compose-row">'
            '<input name="question" class="lz-input" placeholder="询问当前态势…" required>'
            '<button class="lz-btn lz-btn2">询问</button></form>'
            '<form method="post" action="/sand/intent" class="cmd-compose-row">'
            '<input name="text" class="lz-input" placeholder="下达命令意图，如：派出二队开设隔离带" required>'
            '<button class="lz-btn lz-btn2">命令</button></form>'
            "</div>"
            '<form method="post" action="/sand/restaff-sand" class="cmd-compose-row">'
            '<input name="question" class="lz-input" placeholder="要求参谋重新研判并出方案（可留空）">'
            '<button class="lz-btn lz-btn2">生成预案</button></form>'
            "</div>"
        )
    else:
        compose = (
            '<div class="cmd-compose">'
            '<form method="post" action="/sand/ask" class="cmd-compose-row">'
            '<input name="question" class="lz-input" placeholder="向参谋询问当前态势…" required>'
            '<button class="lz-btn lz-btn2">询问</button></form>'
            '<p class="lz-note">命令与决断仅指挥员可用。</p></div>'
        )
    notice = esc(getattr(state, "intent_notice", "") or "")
    notice_html = f'<p class="lz-alert">{notice}</p>' if notice else ""
    return (
        '<section class="cmd-advisor" id="cmd-advisor">'
        '<header class="cmd-advisor-head"><b>智能参谋</b>'
        f'<span class="dim">阶段 {esc(stage_view.current)}'
        f'｜待答 {counts["questions"]}｜在途命令 {counts["orders"]}</span></header>'
        + _goal_line(state, role)
        + (_intelligence.panel(state) if role == 'commander' else '')
        + notice_html
        + _situation_block(state, role)
        + '<div class="cmd-thread">' + _advisor_cards(state, role, stage_view) + '</div>'
        + compose
        + "</section>"
    )


# ---------------------------------------------------------------------------
# 方案推演区（右下）


def _latest_feasibility(ws) -> dict[str, object]:
    best: dict[str, object] = {}
    for v in getattr(ws, "feasibility_verdicts", {}).values():
        cur = best.get(v.option_id)
        if cur is None or v.source_version >= cur.source_version:
            best[v.option_id] = v
    return best


def _plan_panel(state, role: str) -> str:
    ws = state.workspace
    commander = role == "commander"
    event_id = getattr(state, "active_event_id", "")
    if not event_id or ws.event_id != event_id:
        option_impacts = {}
        impact_error = False
    else:
        try:
            option_impacts = grounded_inquiry.current_option_impacts(ws, event_id)
            impact_error = False
        except ValueError:
            option_impacts = {}
            impact_error = True
    feas = _latest_feasibility(ws)
    _sims, _proj = _build_sims(state, role)
    summaries = {s["id"]: s["summary"] for s in _sims}

    briefs = getattr(ws, "decision_briefs", {}) or {}
    ctx = ""
    if briefs:
        brief = max(briefs.values(), key=lambda b: b.source_version)
        ctx = (
            f'<p class="cmd-planctx">依据：简报 {esc(brief.id)} · 数据版本 v{brief.source_version}｜'
            f'推荐：{esc((brief.recommendation or "—")[:60])}</p>'
        )
    else:
        hint = ('可前往<a href="#cmd-advisor">参谋对话</a>生成预案。' if commander
                else '等待指挥员组织研判；可前往<a href="#cmd-advisor">参谋对话</a>问询当前情况。')
        ctx = f'<p class="cmd-planctx dim sand-empty">尚无决策简报。{hint}</p>'

    ordered = sorted(
        (o for o in ws.options.values() if getattr(o, "status", "") in {"active", "affected"}),
        key=lambda o: o.id,
    )
    letters = "ABCDEF"
    cards = []
    for idx, opt in enumerate(ordered[:6]):
        letter = letters[idx] if idx < len(letters) else "?"
        status_word_opt = _OPTION_STATUS_WORDS.get(opt.status, opt.status)
        verdict = feas.get(opt.id)
        feas_word = "尚未评估"
        blocking: tuple[str, ...] = ()
        if verdict is not None:
            feas_word = _FEASIBILITY_WORDS.get(verdict.status, str(verdict.status.value))
            blocking = tuple(getattr(verdict, "blocking_conditions", ()) or ())
        rev_word = _REVERSIBILITY_WORDS.get(
            getattr(opt.reversibility, "value", ""), str(getattr(opt.reversibility, "value", ""))
        )
        detail_items = "".join(f"<li>{esc(_subject_word(x))}</li>" for x in opt.preconditions) or "<li>—（未登记前置条件）</li>"
        risk_items = "".join(f"<li>{esc(x)}</li>" for x in opt.risks) or "<li>未登记</li>"
        nogo_items = "".join(f"<li>{esc(_subject_word(x))}</li>" for x in opt.no_go_conditions) or "<li>未登记</li>"
        claim_items = "".join(f"<li>{esc(_subject_word(x))}</li>" for x in opt.claim_subjects) or "<li>—</li>"
        blocking_html = (
            f'<p class="cmd-planblock">阻塞项：{"；".join(esc(_subject_word(b)) for b in blocking[:3])}</p>'
            if blocking else ""
        )
        impacts = option_impacts.get(opt.id, ())
        if impact_error:
            impact_html = ('<p class="cmd-plan-impact">方案变化来源未通过本事件审计核对；'
                           '请先核查，不能据此判断当前适用性。</p>')
        elif impacts:
            impact_html = (
                '<p class="cmd-plan-impact">本方案有已记录的态势变化影响：'
                + '；'.join(f'{esc(change.change_id)}（{esc(change.status_word)}，'
                           f'{esc(change.reason)}）' for change in impacts[:3])
                + ('；另有变化未展开' if len(impacts) > 3 else '')
                + '。请复核方案前提，并核对已有可行性评估是否覆盖变化；'
                  '此提示不代表专业结论。</p>'
            )
        else:
            impact_html = ''

        # 「上图」是只读查看动作：指挥员与现场人员都能用，也不藏进折叠区；
        # 采纳/否决/暂缓三张表单仍只给指挥员。
        actions = ""
        if role in ("commander", "member"):
            if getattr(opt, "sim_shapes", ()):
                actions += (
                    f'<div class="cmd-plan-foot">'
                    f'<button type="button" class="lz-btn lz-btn2 cmd-simbtn" data-sim="{esc(opt.id)}" '
                    'aria-pressed="false" '
                    'title="在地图上只叠加本方案的推演对象（虚线并标注推演，非正式态势）；'
                    '换上图另一套会自动下掉当前这套">'
                    '<span class="sim-t">上图</span></button>'
                    '<span class="dim">只改显示，不改态势</span></div>'
                )
            else:
                actions += (
                    '<div class="cmd-plan-foot"><button type="button" class="lz-btn lz-btn2" disabled '
                    "title=\"该方案无可上图的推演对象\">暂无可上图对象</button></div>"
                )
        if commander and opt.status == "active":
            risk_box = ""
            if opt.risks:
                risk_box = (
                    f'<label class="cmd-riskok"><input type="checkbox" name="accept_risks" required> '
                    f"知悉并接受全部风险（共 {len(opt.risks)} 项，见详情）</label>"
                )
            actions += (
                '<details class="cmd-plan-acts"><summary>决断操作</summary>'
                '<div class="cmd-plan-actions">'
                + f'<form method="post" action="/sand/plan-decision" class="lz-inlineform cmd-adoptform">'
                f'<input type="hidden" name="option_id" value="{esc(opt.id)}">'
                f'<input type="hidden" name="action" value="adopt">'
                f'<input name="reason" class="lz-input" placeholder="决断理由（可空）">'
                f"{risk_box}"
                f'<button class="lz-btn">采纳为决断基础</button></form>'
                f'<form method="post" action="/sand/plan-decision" class="lz-inlineform">'
                f'<input type="hidden" name="option_id" value="{esc(opt.id)}">'
                f'<input type="hidden" name="action" value="reject">'
                f'<input name="reason" class="lz-input" placeholder="否决原因（必填）" required>'
                f'<button class="lz-btn lz-btn2 lz-danger">否决</button></form>'
                f'<form method="post" action="/sand/plan-decision" class="lz-inlineform">'
                f'<input type="hidden" name="option_id" value="{esc(opt.id)}">'
                f'<input type="hidden" name="action" value="defer">'
                f'<input name="reason" class="lz-input" placeholder="暂缓原因（必填）" required>'
                f'<button class="lz-btn lz-btn2">暂缓</button></form>'
                "</div></details>"
            )
        elif commander and opt.status == "affected":
            actions += (
                '<p class="lz-note">该方案受态势变化影响：复核前提或要求补查后方可采纳'
                "（补查入口见上方信息缺口卡）。</p>"
            )

        summ = summaries.get(opt.id)
        if summ:
            verdict_html = (
                f'<p class="cmd-plan-verdict">推演结论：{esc(summ)}'
                '<span class="dim">（规则推演·非实测）</span></p>'
            )
        else:
            verdict_html = (
                '<p class="cmd-plan-verdict dim">该方案暂无可上图的推演对象，只能看文字结论。</p>'
            )

        cards.append(
            f'<article class="cmd-plan-card" id="cmd-plan-{esc(opt.id)}">'
            f'<header class="cmd-plan-head"><b>{letter} · {esc(opt.title)}</b>'
            f'<span class="lz-chip {"lz-chip-reg" if opt.status == "active" else "lz-chip-warn"}">{esc(status_word_opt)}</span></header>'
            f'<p class="cmd-plan-sum">{esc((opt.summary or "")[:96])}</p>'
            f'<ul class="cmd-plan-meta"><li>可行性：{esc(feas_word)}</li>'
            f"<li>风险 {len(opt.risks)}｜禁行 {len(opt.no_go_conditions)}"
            f"｜成本 {esc(_quality_word(opt.cost_estimate))}｜周期 {esc(_quality_word(opt.time_estimate))}｜{esc(rev_word)}</li></ul>"
            f"{blocking_html}"
            f"{impact_html}"
            f"{verdict_html}"
            f'<details class="cmd-plan-more"><summary>详情</summary>'
            f"<h5>前置条件</h5><ul>{detail_items}</ul>"
            f"<h5>风险</h5><ul>{risk_items}</ul>"
            f"<h5>禁止条件</h5><ul>{nogo_items}</ul>"
            f"<h5>依据主张</h5><ul>{claim_items}</ul>"
            f"</details>"
            + actions
            + "</article>"
        ) or ""
    cards_html = "".join(cards) or (
        '<p class="dim cmd-noplans sand-empty">尚无在议方案。'
        + ('可在参谋对话中生成预案，或<a href="#drawer-selfopt">自提方案</a>。' if commander
           else '当前没有可查看的方案；方案形成后可在此查看。') + '</p>'
    )

    footer = ""
    if commander:
        footer = (
            '<details class="cmd-selfopt" id="drawer-selfopt">'
            "<summary>＋ 自提方案</summary>"
            '<form method="post" action="/sand/self-option" class="lz-formgrid">'
            '<input name="title" class="lz-input" placeholder="方案名称" required>'
            '<input name="summary" class="lz-input" placeholder="一句话概述" required>'
            '<input name="risks" class="lz-input" placeholder="风险（逗号分隔，可空）">'
            '<input name="no_go_conditions" class="lz-input" placeholder="禁行条件（逗号分隔，可空）">'
            '<input name="preconditions" class="lz-input" placeholder="前置条件（逗号分隔，可空）">'
            '<select name="reversibility" class="lz-input">'
            '<option value="reversible">可逆</option>'
            '<option value="partially_reversible">部分可逆</option>'
            '<option value="irreversible">不可逆</option></select>'
            '<button class="lz-btn lz-btn2">提交自提方案</button></form></details>'
        )

    decisions = sorted(getattr(ws, "decisions", {}).values(), key=lambda d: d.id)
    dec_rows = "".join(
        f"<li>{esc(_DECISION_WORDS.get(d.decision_type, d.decision_type.value))}"
        f"{(' · ' + d.option_id) if d.option_id else ''} —— {esc(d.reason[:44])}</li>"
        for d in decisions[-4:]
    ) or "<li>尚无决断记录。</li>"

    return (
        '<section class="cmd-plans" id="cmd-plans">'
        '<header class="cmd-plans-head"><b>应急预案 · 方案比较与推演</b>'
        f'<span class="dim">在议 {len(ordered)} 套</span></header>'
        + (
            '<p class="cmd-plan-hint">上图只调整显示。采纳前请核对风险与禁止条件。</p>'
            if role in ("commander", "member") else ""
        )
        + ctx
        + f'<div class="cmd-plan-list">{cards_html}</div>'
        + footer
        + f'<details class="cmd-decisions"><summary>决断历史（{len(decisions)}）</summary>'
        f"<ul>{dec_rows}</ul>"
        "<p class='lz-note'>采纳仅生成指挥员决断记录；执行命令须经命令链确认后另行下达。</p></details>"
        "</section>"
    )


# ---------------------------------------------------------------------------
# 底部对象条 + 抽屉


_FOLLOWUP_OWNER_SOURCES = {
    'event_appointment': '本事件指定副总指挥任命',
    'task_recorded_recipient': '任务当前版记录的接收人（现任身份待核）',
    'task_recorded_verifier': '任务当前版记录的核验负责人',
}


def _followup_view(state, role: str, viewer_id: str):
    if role == 'public':
        return None
    event_id = getattr(state, 'active_event_id', '')
    registry = getattr(state, 'registry', None)
    if (not event_id or getattr(state.workspace, 'event_id', '') != event_id
            or registry is None or event_id not in registry.events):
        return None
    try:
        board = followup_board(
            state.workspace, event_id,
            now=datetime.now(timezone.utc).isoformat())
    except ValueError:
        return (), ('本事件提示来源审计待核对，暂不展示催办内容。',)
    prompts = board.prompts
    if role == 'member':
        prompts = tuple(prompt for prompt in prompts if viewer_id
                        and prompt.owner_id == viewer_id)
        return prompts, ()
    return prompts, board.issues


def _followup_drawer(view, registry) -> str:
    prompts, issues = view if view is not None else ((), ('请先进入当前事件任务空间。',))
    def person_label(subject_id):
        record = getattr(registry, 'library', {}).get(subject_id)
        name = getattr(record, 'name', '') if record is not None else ''
        return f'{name}（{subject_id}）' if name else subject_id
    cards = ''.join(
        '<div class="cmd-followup-card">'
        f'<b>{esc("任务" if prompt.subject_kind == "task" else "设备")} '
        f'{esc(prompt.subject_id)} · 待处理</b>'
        f'<p>{esc(prompt.suggested_question)}</p>'
        f'<p class="dim">处理人 {esc(person_label(prompt.owner_id))} · 统筹 {esc(person_label(prompt.coordinator_id))}</p>'
        f'<p class="dim">责任来源：{esc("／".join(_FOLLOWUP_OWNER_SOURCES.get(source, source) for source in prompt.owner_sources))}</p>'
        f'<p class="dim">依据：{esc("；".join(prompt.evidence_refs))}</p>'
        '</div>' for prompt in prompts
    ) or '<p class="dim">当前没有可路由的待核对追问。</p>'
    gaps = ''.join(f'<li>{esc(issue)}</li>' for issue in issues)
    return (
        f'<details class="cmd-drawer cmd-followups" id="drawer-followups"><summary>待核对追问（{len(prompts)}）</summary>'
        '<p class="lz-note">只读提示；尚未发送通知或对讲语音。查看不等于处理完成。模拟通话和位置仍待核对。</p>'
        + cards + (f'<ul class="cmd-followup-gaps">{gaps}</ul>' if gaps else '')
        + '</details>'
    )


def _followup_summary(view, role: str) -> str:
    if role == 'public':
        return ''
    count = len(view[0]) if view is not None else 0
    return (f'<a class="sand-followup-pill" href="#drawer-followups" '
            f'aria-label="待核对追问 {count} 项，查看依据">'
            f'待核对追问 {count} 项 · 查看依据</a>')


def _bottom_bar(state, role: str) -> str:
    return (
        '<footer class="cmd-bottom" id="cmd-bottom">'
        '<span class="cmd-bottom-label">当前对象</span>'
        '<span id="cmd-bottom-info">点击地图对象查看详情；候选与草稿不会自动成为正式态势。</span>'
        "</footer>"
    )


def _drawers(state, role: str, *, measure_result, measure_error, measure_saved,
             ask_error: str, ask_question: str = '', followups=None,
             viewer_id: str = '', source_page=None, source_error: str = '',
             source_url: str = '', deepseek_preview=None) -> str:
    """完整业务表单收进抽屉（docs/18 §3.4）：DOM 常驻、默认折叠，
    保证既有表单提交路径与字符串钉不破坏。"""
    commander = role == "commander"
    fourcat_inner = _four_category_block(state, role)
    drawers = [
        f'<details class="cmd-drawer" id="drawer-fourcat"><summary>信息全景（事实/判断/建议/指令）</summary>'
        f'<div class="cmd-fourcat">{fourcat_inner}</div></details>',
    ]
    if role != 'public':
        drawers.append(_followup_drawer(followups, getattr(state, 'registry', None)))
    if commander:
        drawers.append(
            f'<details class="cmd-drawer" id="drawer-annotate"><summary>标绘作业（含核验链）</summary>'
            f"{_tools_card(state, role)}</details>"
        )
        drawers.append(
            f'<details class="cmd-drawer" id="drawer-measure"><summary>自由区域测算</summary>'
            f"{_measure_card(measure_result, measure_error, measure_saved)}</details>"
        )
        drawers.append(
            f'<details class="cmd-drawer" id="drawer-imagery"><summary>无人机图像导入与配准</summary>'
            f"{_imagery_card(state, role)}</details>"
        )
        drawers.append(
            f'<details class="cmd-drawer" id="drawer-intent"><summary>语音/文字指令草稿</summary>'
            f"{_intent_card(state, role)}</details>"
        )
    if role != "public":
        drawers.append(
            f'<details class="cmd-drawer" id="drawer-qa"'
            f'{" open" if source_page or source_error or deepseek_preview or ask_error else ""}><summary>态势问询记录与官方资料</summary>'
            f"{_qa_card(state, role, ask_error, viewer_id, ask_question, source_page, source_error, source_url, deepseek_preview)}</details>"
        )
    return '<div class="cmd-drawers">' + "".join(drawers) + "</div>"


# ---------------------------------------------------------------------------
# 四类信息全景（保留 doc 03 §4 口径与原右栏内容，收进抽屉）


def _four_category_block(state, role: str) -> str:
    ws = state.workspace
    event = None
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    if registry is not None and active_id:
        event = registry.events.get(active_id)
    ledger = getattr(state, "ledger", None)
    stage_view = sandtable.derive_stage(ws, event=event, ledger=ledger)
    allowed = sandtable.visible_statuses(role)
    by_status: dict[str, int] = {}
    visible_marks = [
        m for m in sorted(ws.annotations.values(), key=lambda x: x.id)
        if m.status in allowed
    ]
    for mark in ws.annotations.values():
        key = status_word(mark.status)
        by_status[key] = by_status.get(key, 0) + 1
    situation_rows = "".join(
        f"<li>{esc(k)} <b>{v}</b> 条</li>" for k, v in sorted(by_status.items())
    ) or "<li>尚无标绘。</li>"
    mark_links = "".join(
        f"<li>{esc(kind_label(m.kind))}·{esc(m.title)} "
        f'<span class="dim">[{esc(status_word(m.status))}]</span>'
        + ("" if m.geo_points else ' <span class="dim">（未录坐标，未上图）</span>')
        + "</li>"
        for m in visible_marks[:12]
    ) or "<li>尚无标绘。</li>"
    gaps = getattr(ws, "information_gaps", {}) or {}
    open_gaps = [
        g for g in gaps.values() if getattr(g.status, "value", str(g.status)) == "open"
    ]
    gap_rows = "".join(
        f"<li>{esc(getattr(g, 'subject', '') or g.id)}</li>" for g in open_gaps[:5]
    ) or "<li>无未关闭缺口。</li>"
    options = getattr(ws, "options", {}) or {}
    decisions = getattr(ws, "decisions", {}) or {}
    orders = getattr(ws, "orders", {}) or {}
    reports = getattr(ws, "reports", {}) or {}
    order_rows = "".join(
        f"<li>{esc(o.id)} · {esc(_order_word(o.status))}</li>"
        for o in list(orders.values())[-4:]
    ) or "<li>无命令。</li>"

    ai_candidates = [
        m
        for m in ws.annotations.values()
        if m.source == AnnotationSource.AI_SUGGESTION
        and m.status == AnnotationStatus.PENDING_REVIEW
        and role in {"commander", "member"}
    ]
    cand_cards = []
    for mark in ai_candidates:
        verify_forms = ""
        if role == "commander":
            verify_forms = (
                '<form method="post" action="/sand/candidate" class="lz-inlineform">'
                f'<input type="hidden" name="annotation_id" value="{esc(mark.id)}">'
                '<button name="action" value="verify" class="lz-btn lz-btn2">确认为正式态势</button>'
                '<button name="action" value="withdraw" class="lz-btn lz-btn2 lz-danger">驳回</button></form>'
            )
        cand_cards.append(
            f'<div class="cand-card"><b>【待核验·待确认】{esc(mark.title)}</b>'
            f'<span class="dim">置信度字段：演示数据未携带</span>'
            f'{verify_forms}</div>'
        )
    candidate_html = (
        "<h4>待核验候选标注（后台分析任务送达）</h4>"
        + ("".join(cand_cards) if cand_cards else "<p>暂无待处理候选。</p>")
        + '<p class="lz-note">分析与候选生成为后台任务结果，前台只显示待处理业务卡、'
        "不显示分析过程；草稿/候选以虚线半透明显示，绝不被当作正式态势。</p>"
    )
    if role == "commander":
        candidate_html += (
            '<form method="post" action="/sand/ai-candidate">'
            '<button class="lz-btn lz-btn2">模拟后台送达一条候选（演示数据）</button></form>'
        )
    active_options = [
        o for o in options.values() if getattr(o, "status", "") == "active"
    ]
    option_rows = "".join(
        f'<div class="cand-card"><b>{esc(o.title)}</b>'
        f'<span class="dim">{esc((o.summary or "")[:60])}</span>'
        f'<span class="dim">风险 {len(o.risks)} 项'
        + (" · 有禁用条件" if o.no_go_conditions else "")
        + " · 待指挥员决断</span></div>"
        for o in active_options[:3]
    )
    options_html = (
        "<h4>方案比较（备选方案）</h4>"
        + (option_rows if option_rows else "<p>暂无在议备选方案。</p>")
        + f'<p class="lz-note">备选方案 <b>{len(active_options)}</b> 个 · 已定案 <b>{len(decisions)}</b> 次；'
        "采纳与否属决断权，经指挥员确认后才进入命令链下达。"
    )

    judge_rows = (
        f"<p>当前阶段判定：<b>{esc(stage_view.current)}</b>"
        + ("／<b>异常处置</b>覆盖态" if stage_view.overlay else "")
        + f" · 数据截至 {_data_time(state)}</p>"
        + '<p class="lz-note">判断由规则推导生成，依据见「现场阶段」条件表；'
        "问询结论见态势问询记录（仅检索，不改状态）。</p>"
    )

    alarm_html = f"<p class='dim'>{esc(_ALARM_PENDING)}</p>"
    advice_block = (
        ""
        if role == "public"
        else (
            "<h3>建议 · 待指挥员决断</h3>"
            + candidate_html + options_html
        )
    )
    left = _left_facts(state, stage_view)
    return (
        left
        + '<h3>事实 · 观测与证据</h3>'
        "<h4>图上标绘统计</h4>"
        f"<ul>{situation_rows}</ul>"
        "<h4>对象列表</h4>"
        f"<ul>{mark_links}</ul>"
        f"<h4>告警</h4>{alarm_html}"
        f"<h4>信息缺口</h4><ul>{gap_rows}</ul>"
        + "<h3>判断 · 分析与推断</h3>"
        + judge_rows
        + advice_block
        + "<h3>指令 · 正式命令</h3>"
        f"<h4>命令与执行反馈</h4><ul>{order_rows}</ul>"
        f"<p class='dim'>执行报告 {len(reports)} 份。</p>"
        '<p class="lz-note">信息四类口径：事实/判断/建议/指令（规范 03·§4）；'
        "建议须经指挥员决断才可能成为指令。</p>"
    )


def _left_facts(state, stage_view) -> str:
    """原左栏阶段条件/目标/决策历史，作为全景抽屉开头的事实底座。"""
    ws = state.workspace
    goals = getattr(ws, "goal_frames", {}) or {}
    goal_rows = "".join(
        f"<li>{esc(getattr(g, 'statement', '') or getattr(g, 'objective', '') or g.id)}"
        f'{"" if getattr(g, "confirmed_by", None) else _chip("待确认", "warn")}</li>'
        for g in list(goals.values())[-3:]
    ) or "<li>尚无目标框记录。</li>"
    decisions = getattr(ws, "decisions", {}) or {}
    decision_rows = "".join(
        f'<li><b>{esc(d.reason[:40])}</b> <span class="dim">（{_entry_word(d.decided_by)}）</span></li>'
        for d in list(decisions.values())[-5:]
    ) or "<li>尚无决策。</li>"
    cond_rows = "".join(
        f'<tr><td>{esc(c.stage)}</td><td>{"✓" if c.met else "—"}</td>'
        f"<td>{esc(c.evidence)}</td></tr>"
        for c in stage_view.conditions
    )
    overlay_html = (
        '<p class="lz-alert">存在受阻/异常命令：进入<b>异常处置</b>覆盖态，'
        "相关命令已冻结等待重新研判。</p>"
        if stage_view.overlay
        else ""
    )
    return (
        f'<h3>现场阶段（{esc(stage_view.current)}）</h3>'
        f'<table class="lz-table"><thead><tr><th>阶段</th><th>满足</th><th>证据</th></tr></thead>'
        f"<tbody>{cond_rows}</tbody></table>{overlay_html}"
        f"<h3>总体目标</h3><ul>{goal_rows}</ul>"
        f"<h3>决策历史</h3><ul>{decision_rows}</ul>"
    )


# ---------------------------------------------------------------------------
# 既有业务卡片（原样保留，置于抽屉内）


def _tools_card(state, role: str) -> str:
    if role != "commander":
        return ""
    marks = sorted(state.workspace.annotations.values(), key=lambda m: m.id)

    def _geo_cell(m) -> str:
        current = (
            " ".join(f"{la:.5f},{lo:.5f}" for la, lo in m.geo_points[:3])
            if m.geo_points else "未录入"
        )
        return (
            f'<span class="dim mono">{esc(current)}</span>'
            '<form method="post" action="/sand/annotate-geo" class="lz-inlineform">'
            f'<input type="hidden" name="annotation_id" value="{esc(m.id)}">'
            '<input name="geo" class="lz-input mono" style="min-width:180px" '
            'placeholder="纬度,经度；多条用分号">'
            '<button class="lz-btn lz-btn2">补录</button></form>'
        )

    status_forms = "".join(
        f"<tr><td>{esc(m.title)}</td><td>{esc(kind_label(m.kind))}</td>"
        f'<td>{_chip(status_word(m.status), "reg" if m.status in (AnnotationStatus.DRAFT, AnnotationStatus.PENDING_REVIEW) else "ok")}</td>'
        f'<td><form method="post" action="/sand/annotate-status" class="lz-inlineform">'
        f'<input type="hidden" name="annotation_id" value="{esc(m.id)}">'
        '<select name="target" class="lz-input">'
        + "".join(
            f'<option value="{s.value}">{esc(status_word(s))}</option>'
            for s in AnnotationStatus
        )
        + '</select> <button class="lz-btn lz-btn2">流转</button></form></td>'
        f"<td>{_geo_cell(m)}</td></tr>"
        for m in marks[-10:]
    ) or '<tr><td colspan="5" class="sand-empty">尚无标绘。可从地图工具绘制对象；新对象先进入草稿核验链。</td></tr>'
    return (
        '<section class="sand-card"><h3>标绘作业（指挥员）</h3>'
        '<p class="lz-note">选工具→地图落点→填名称保存。请核对来源与位置，再提交核验。</p>'
        "<h4>核实状态与位置</h4>"
        f'<table class="lz-table"><thead><tr><th>标绘</th><th>类型</th><th>状态</th><th>流转</th><th>地理坐标</th></tr></thead>'
        f"<tbody>{status_forms}</tbody></table></section>"
    )


def _measure_card(result: dict | None, error: str = "", saved_id: str = "") -> str:
    result_html = ""
    if error:
        result_html = f'<p class="lz-alert">测算失败：{esc(error)}</p>'
    if saved_id:
        result_html += (
            f'<p class="lz-alert ok">已存为业务对象草稿 <b>{esc(saved_id)}</b>——'
            "走既有核验链：提交核验 → 指挥员确认 → 发布共享（规范 03·§11.2）。</p>"
        )
    if result is not None:
        result_html = (
            f'<p class="sand-measure-result">顶点 {result["vertices"]} 个 · '
            f'面积约 <b>{result["area_m2"]:,.1f} 平方米</b> · 周长约 '
            f'<b>{result["perimeter_m"]:,.1f} 米</b>'
            '<span class="lz-chip lz-chip-warn">示意值 · 非实测</span></p>'
            + result_html
        )
    type_options = "".join(
        f'<option value="{esc(t)}">{esc(t)}</option>'
        for t in (
            "待核查区", "火场范围", "危险区", "过火区", "重点保护区", "隔离带",
        )
    )
    return (
        '<section class="sand-card"><h3>自由区域测算（临时，指挥员）</h3>'
        '<form method="post" action="/sand/measure" class="lz-inlineform">'
        '<input name="points" class="lz-input mono" style="min-width:340px" '
        'placeholder="任意轨迹坐标 x,y x,y …（松手自动闭合→此处以坐标串提交）">'
        f'<select name="area_type" class="lz-select">{type_options}</select>'
        '<button class="lz-btn lz-btn2">计算面积与周长</button>'
        '<button class="lz-btn lz-btn2" name="save" value="1">计算并存为草稿</button></form>'
        '<p class="lz-note">结果为示意值。保存后先进入草稿，核实后才能发布。</p>'
        f"{result_html}</section>"
    )


def _imagery_card(state, role: str) -> str:
    """无人机影像卡（doc 03 §9）：导入/配准/分析/候选处理。"""
    ledger = getattr(state, "imagery", None)
    if ledger is None:
        return ""
    event_id = getattr(state, "active_event_id", "")
    images = ledger.for_event(event_id)
    commander = role == "commander"
    rows = []
    for image in images:
        reg_chip = (
            '<span class="lz-chip lz-chip-ok">已配准</span>'
            if image.registered
            else '<span class="lz-chip lz-chip-warn">未配准证据</span>'
        )
        origin = (
            f' <a class="mono" href="/uploads/{esc(image.file_path)}" target="_blank">看原图</a>'
            if commander and image.file_path
            else ""
        )
        actions = ""
        if commander:
            if image.status == "awaiting_registration":
                actions = (
                    '<form method="post" action="/sand/image-status" class="lz-inlineform">'
                    f'<input type="hidden" name="image_id" value="{esc(image.image_id)}">'
                    '<input type="hidden" name="target" value="located">'
                    '<input name="spatial_range" class="lz-input" placeholder="空间范围（四至坐标或覆盖描述）" style="width:230px">'
                    '<button class="lz-btn lz-btn2">提交人工配准</button></form>'
                )
            elif image.status == "located":
                actions = (
                    '<form method="post" action="/sand/image-status" class="lz-inlineform">'
                    f'<input type="hidden" name="image_id" value="{esc(image.image_id)}">'
                    '<input type="hidden" name="target" value="overlaid">'
                    '<button class="lz-btn lz-btn2">叠加到地图</button></form>'
                )
            elif image.status == "overlaid":
                actions = (
                    '<form method="post" action="/sand/image-analyze" class="lz-inlineform">'
                    f'<input type="hidden" name="image_id" value="{esc(image.image_id)}">'
                    '<button class="lz-btn lz-btn2">模拟后台分析送达候选（演示数据）</button></form>'
                )
            elif image.status == "candidate_generated":
                actions = (
                    '<form method="post" action="/sand/image-decision" class="lz-inlineform">'
                    f'<input type="hidden" name="image_id" value="{esc(image.image_id)}">'
                    '<button name="decision" value="confirm" class="lz-btn lz-btn2">确认为正式态势</button>'
                    '<button name="decision" value="reject" class="lz-btn lz-btn2 lz-danger">驳回</button></form>'
                )
        rows.append(
            f'<tr><td class="mono">{esc(image.image_id)}</td>'
            f'<td>{esc(image.source_system)}<br><span class="dim">{esc(image.collected_at)}</span></td>'
            f'<td><span class="lz-chip lz-chip-reg">{esc(image.status_word())}</span>{reg_chip}</td>'
            f'<td>{esc(image.spatial_range or "—")}<br><span class="dim">{esc(image.geo_precision or "")}</span></td>'
            f"<td>{actions}{origin}</td></tr>"
        )
    table_body = "".join(rows) if rows else \
        '<tr><td colspan="5">本事件尚无影像记录。</td></tr>'
    upload_form = ""
    if commander:
        upload_form = (
            '<form method="post" action="/sand/image-upload" '
            'enctype="multipart/form-data" class="lz-formgrid">'
            '<input type="file" name="file" accept=".jpg,.jpeg,.png" required>'
            '<input name="source_system" class="lz-input" placeholder="来源系统（如 XX 型无人机）" required>'
            '<input name="collected_by" class="lz-input" placeholder="采集单位">'
            '<input name="collected_at" class="lz-input" placeholder="采集时间 ISO（如 2026-08-24T09:30:00+08:00）" required>'
            '<select name="has_spatial" class="lz-input">'
            '<option value="0">无空间信息 → 待配准</option>'
            '<option value="1">有空间信息 → 自动定位</option></select>'
            '<input name="spatial_range" class="lz-input" placeholder="空间范围（有空间信息时必填）">'
            '<input name="geo_precision" class="lz-input" placeholder="定位精度（选填）">'
            '<button class="lz-btn">导入图像</button></form>'
            '<p class="lz-note">支持 JPG/PNG；GeoTIFF/正射/热成像/视频流为后续预留。'
            "未配准图像只能作未配准证据——不得用于正式火线/路线判断。</p>"
        )
    return (
        '<section class="sand-card"><h3>无人机图像导入与配准</h3>'
        + upload_form
        + '<p class="lz-note">图像分析尚未接入；分析按钮只用于演示。</p>'
        + '<table class="lz-table"><thead><tr><th>编号</th><th>来源/采集时间</th>'
        '<th>状态</th><th>空间范围/精度</th><th>操作</th></tr></thead>'
        f"<tbody>{table_body}</tbody></table>"
        + "</section>"
    )


def _qa_card(state, role: str, error: str = "", viewer_id: str = '',
             ask_question: str = '', source_page=None, source_error: str = '',
             source_url: str = '', deepseek_preview=None) -> str:
    if role == "public":
        return ""  # Public display must not expose private query history or forms.
    journal = getattr(state, 'inquiry_journal', None)
    if journal is not None:
        actor = GENERAL_ENTRY_ID if role == 'commander' else viewer_id
        log = journal.for_viewer(getattr(state, 'active_event_id', '') or '', actor) if actor else []
    else:
        log = getattr(state, "sand_qa_log", []) or []
    rows = "".join(
        f'<div class="qa-item"><p class="q">问：{esc(q)}</p>'
        f"<p class='a'>结论：{esc(a.conclusion)}</p>"
        f'<p class="meta dim">数据时间 {esc(a.data_time) or "—"} · 来源 '
        f'{esc("、".join(a.sources)) if a.sources else "—"} · 位置 {esc(a.location)} · '
        f'置信度 {esc(a.confidence)}'
        + (f" · 未确认：{esc(a.unconfirmed)}" if a.unconfirmed else "")
        + "</p></div>"
        for q, a in reversed(log[-5:])
    ) or "<p class='dim sand-empty'>暂无问询记录。可在上方输入问题；回答仅检索已有资料。</p>"
    if role == 'commander' and journal is not None:
        buttons = ''.join(
            '<form method="post" action="/sand/model-preview#qa">'
            f'<input type="hidden" name="inquiry_id" value="{esc(r["id"])}">'
            f'<button class="lz-btn lz-btn2">预览模型分析：{esc(r["question"][:60])}</button></form>'
            for r in reversed(journal.records[-20:])
            if r['event_id'] == getattr(state, 'active_event_id', '')
            and r['actor_id'] == GENERAL_ENTRY_ID and not r.get('model_meta')
            and deepseek_qa.is_eligible_topic(r['answer'].get('topic', ''))
        )
        rows += '<h4>选择本地问答用于模型分析</h4><p><a class="lz-link" href="/models">模型连接设置：国内云模型 / 本地算力</a></p>' + (buttons or '<p>暂无可选记录。</p>')
    if deepseek_preview is not None and role == 'commander':
        exact = json.dumps(deepseek_preview['request_body'], ensure_ascii=False, indent=2)
        rows += (
            '<div class="qa-item"><h4>模型逐次请求预览</h4>'
            f'<p>模型来源：{esc(deepseek_preview.get("model_label", "DeepSeek"))}；'
            f'模式：{"本地算力" if deepseek_preview.get("mode") == "local" else "国内云模型"}。</p>'
            f'<p>目标：{esc(deepseek_preview["endpoint"])}。以下 JSON 是本次实际请求体；'
            '来源编号已匿名化。请逐字段核对，确认后才会发送。</p>'
            f'<pre class="prewrap">{esc(exact)}</pre>'
            '<form method="post" action="/sand/model-confirm#qa">'
            f'<input type="hidden" name="preview_token" value="{esc(deepseek_preview["token"])}">'
            '<button class="lz-btn lz-btn2">确认本次外发并提问</button></form>'
            '<p>不确认可直接离开；预览 5 分钟后失效。</p></div>'
        )
    source_result = ""
    if source_page is not None:
        source_result = (
            '<div class="qa-item"><h4>官方网页摘录（待人工核对）</h4>'
            f'<p>标题：{esc(source_page.title)}</p>'
            f'<p>链接：<a href="{esc(source_page.url)}" target="_blank" rel="noopener noreferrer">{esc(source_page.url)}</a></p>'
            f'<p>页面自报日期：{esc(source_page.stated_date) or "未找到"}；'
            f'读取时间：{esc(source_page.fetched_at)}</p>'
            f'<p class="prewrap">{esc(source_page.excerpt)}</p>'
            '<p class="lz-note">仅核对链接属于官方域名并展示网页摘录；发布日期和正文需人工对照原网页。'
            '本结果不进入当前事件事实、问答引用或正式方法库。</p></div>'
        )
    return (
        '<section class="sand-card" id="qa"><h3>态势问询（文字或输入法语音转文字）</h3>'
        '<form method="post" action="/sand/ask" class="lz-inlineform">'
        f'<input name="question" value="{esc(ask_question)}" class="lz-input" style="min-width:360px" '
        'placeholder="如：现在队伍多少人 / 图上有哪些火点 / 物资缺口如何" required>'
        '<button class="lz-btn lz-btn2">提问</button></form>'
        + (f'<p class="lz-alert">问询失败：{esc(error)}</p>' if error else "")
        + rows
        + '<h4>按次读取官方网页</h4>'
        + '<form method="post" action="/sand/source-check" class="lz-inlineform">'
        + f'<input type="url" name="source_url" value="{esc(source_url)}" class="lz-input" '
        'style="min-width:360px" placeholder="粘贴 gov.cn 官方网页 HTTPS 链接" required>'
        '<button class="lz-btn lz-btn2">读取并核对</button></form>'
        + (f'<p class="lz-alert">资料读取失败：{esc(source_error)}</p>' if source_error else "")
        + source_result
        + '<p class="lz-note">仅发送你提交的完整网页链接；不附带事件问句或人员、地图、物资记录。'
        '当前只读取 HTML；PDF 请使用已核验的本地标准文本。</p>'
        + f'<p class="lz-note">{esc(sandtable.SUPPORTED_TOPICS_NOTE)}。'
        "回答仅检索数据、不改变任何状态；命令类输入请走指令入口分流。</p></section>"
    )


def _intent_card(state, role: str) -> str:
    notice = esc(getattr(state, "intent_notice", "") or "")
    notice_html = f'<p class="lz-alert">{notice}</p>' if notice else ""
    form = (
        '<form method="post" action="/sand/intent" class="lz-inlineform">'
        '<input name="text" class="lz-input" style="min-width:360px" '
        'placeholder="如：现在队伍多少人 ｜ 派出二队在阴坡开设隔离带" required>'
        '<button class="lz-btn lz-btn2">发送</button></form>'
    )
    drafts_html = ""
    if role == "commander":
        drafts = getattr(state, "voice_drafts", {}) or {}
        rows = "".join(
            f"<tr><td><code>{esc(d['id'])}</code></td>"
            f"<td>{esc(d['raw_text'])}</td>"
            f"<td>{esc(d['unit_hint'] or '未解析')}</td>"
            f"<td>{esc(d['area_hint'] or '未解析')}</td>"
            f"<td>{esc(d['status'])}</td>"
            f"<td>{esc(d['option_id'] or '—')}</td>"
            + (
                '<td><form method="post" action="/sand/draft-confirm" '
                'class="lz-inlineform">'
                f'<input type="hidden" name="draft_id" value="{esc(d["id"])}">'
                '<button class="lz-btn lz-btn2">确认转方案</button></form>'
                '<form method="post" action="/sand/draft-discard" '
                'class="lz-inlineform">'
                f'<input type="hidden" name="draft_id" value="{esc(d["id"])}">'
                '<button class="lz-btn lz-btn2">作废</button></form></td>'
                if d["status"] == "待确认" else "<td>—</td>"
            )
            + "</tr>"
            for d in reversed(list(drafts.values()))
        ) or (
            '<tr><td colspan="7" class="dim">暂无指令草稿。</td></tr>'
        )
        drafts_html = (
            '<table class="lz-table"><thead><tr><th>编号</th><th>原文</th>'
            "<th>执行单位提示</th><th>区域提示</th><th>状态</th>"
            "<th>方案号</th><th>操作</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            '<p class="lz-note">请核对内容。确认草稿不会直接发布任务。</p>'
        )
    else:
        drafts_html = (
            '<p class="lz-note">成员无命令权限：命中命令意图的输入会被拒绝，'
            "可改为向指挥员口头建议。</p>"
        )
    return (
        '<section class="sand-card" id="intent">'
        "<h3>指令草稿（文字或输入法语音转文字）</h3>"
        + notice_html + form
        + '<p class="lz-note">命令先存为草稿，确认后进入方案与发布流程。</p>'
        + drafts_html
        + "</section>"
    )


# ---------------------------------------------------------------------------
# 页面样式（作用域限 .cmd-page 内，独立于面板壳）


_CMD_CSS = """
.cmd-page{--cmd-side-w:380px;display:flex;flex-direction:column;gap:8px}
.cmd-page .sand-strip{flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px}
.cmd-main{display:grid;grid-template-columns:46px minmax(0,1fr) var(--cmd-side-w);gap:8px;align-items:stretch;height:calc(100dvh - 226px);min-height:460px}
.cmd-main-public{grid-template-columns:46px minmax(0,1fr)}
.cmd-rail{display:flex;flex-direction:column;gap:4px;background:#FFFFFF;border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:6px 4px;height:fit-content}
.cmd-railbtn{display:flex;flex-direction:column;align-items:center;gap:2px;padding:7px 2px;border-radius:6px;color:#24303B;text-decoration:none;font-size:12px}
.cmd-railbtn:hover{background:#EEF3F8}
.cmd-railbtn .i{font-size:16px;line-height:1}
.cmd-railbtn .t{display:none}
.cmd-mapwrap{position:relative;display:flex;flex-direction:column;background:#FFFFFF;border:1px solid var(--line,#D7DDE3);border-radius:8px;overflow:hidden;min-height:520px}
.cmd-mapbar{display:flex;flex-wrap:wrap;gap:4px;padding:6px 8px;background:#F7FAFC;border-bottom:1px solid var(--line,#D7DDE3)}
.cmd-maparea{position:relative;flex:1;min-height:0}
.cmd-map{position:absolute;inset:0;z-index:1}
.cmd-mapbanner{position:absolute;top:8px;left:50%;transform:translateX(-50%);z-index:1000;max-width:86%;background:#FFF7E8;border:1px solid #E3C88F;color:#5A4718;padding:6px 12px;border-radius:6px;font-size:13px}
.cmd-mapbanner.err{background:#FDECEC;border-color:#E3A6A6;color:#6B1F1F}
.cmd-maplegend{position:absolute;right:10px;top:10px;z-index:1000}
.cmd-maplegend>summary{cursor:pointer;list-style:none;background:rgba(255,255,255,.94);border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:4px 9px;font-size:12px}
.cmd-maplegend>summary::-webkit-details-marker{display:none}
.cmd-maplegend-body{margin-top:4px;background:rgba(255,255,255,.96);border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:6px 9px;font-size:12px;display:flex;flex-direction:column;gap:3px}
.cmd-maprole{font-weight:600}
.cmd-anno-float{position:absolute;right:10px;top:44px;z-index:1100;width:232px;background:#fff;border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;box-shadow:0 6px 18px rgba(31,45,58,.18)}
.cmd-anno-float[hidden]{display:none}
.cmd-anno-float b{font-size:13px;color:#1F2D3A}
.cmd-anno-float-acts{display:flex;gap:6px}
.lz-btn.on{background:#1F5FA8;border-color:#1F5FA8;color:#fff}
.lg-row{display:flex;align-items:center;gap:6px}
.lg-swatch{width:14px;height:4px;border-radius:2px;display:inline-block}
.cmd-toolgroup{display:flex;flex-direction:row;flex-wrap:wrap;gap:4px}
.cmd-tool{background:#fff;border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer}
.cmd-tool.on{background:#1F5FA8;color:#fff;border-color:#1F5FA8}
.cmd-toolgroups{display:flex;flex-direction:row;flex-wrap:wrap;gap:4px;align-items:flex-start}
.cmd-toolgroup[data-toolgroup]{display:flex;flex-direction:column;gap:3px}
.cmd-grptab{background:#F2F6FA;border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;font-weight:600;color:#25415C}
.cmd-grptab.on{background:#245B78;color:#fff;border-color:#245B78}
.cmd-groupbtns{display:flex;flex-direction:column;gap:3px;padding-left:2px}
.cmd-groupbtns[hidden]{display:none}
.cmd-toolacts{flex-direction:row;align-items:center}
.cmd-sym{background:transparent;border:0}
.cmd-symrot{display:block;line-height:0;transform-origin:50% 50%}
.cmd-symtxt{display:inline-block;background:rgba(255,255,255,.9);border:1px solid #94A3B1;border-radius:4px;padding:0 4px;font-size:11px;color:#33475B}
.cmd-arrow{background:transparent;border:0}
.cmd-arrowin{display:block;width:0;height:0;border-left:10px solid transparent;border-right:10px solid transparent;border-bottom:19px solid #B84A42;opacity:.95;transform-origin:50% 50%}
.cmd-route-label{background:transparent;border:0;white-space:nowrap}
.cmd-route-label span{display:inline-block;background:#fff;border:1px solid #647382;border-radius:3px;padding:1px 4px;font-size:11px;color:#263746}
.cmd-badge{background:transparent;border:0}
.cmd-badgein{display:inline-block;background:#B84A42;color:#fff;border-radius:4px;padding:1px 5px;font-size:11px;white-space:nowrap}
.cmd-gap{background:transparent;border:0}
.cmd-gapin{display:block;width:18px;height:18px;line-height:16px;text-align:center;border:2px solid #B84A42;border-radius:50%;background:#fff;color:#B84A42;font-size:13px;font-weight:700}
.cmd-fill-hatch{fill:url(#cmd-pat-hatch)}
.cmd-fill-hatch2{fill:url(#cmd-pat-hatch2)}
.cmd-marktip{background:rgba(255,255,255,.94);border:1px solid #C8D2DC;border-radius:4px;padding:1px 5px;font-size:11px;color:#22384C;box-shadow:none}
.cmd-infotop{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px}
.cmd-chip{display:inline-block;border:1px solid #C8D2DC;border-radius:10px;padding:0 7px;font-size:11px;color:#33475B;background:#F5F8FB}
.cmd-chip.warn{border-color:#D9A441;color:#8A5A0B;background:#FFF8EA}
.cmd-chip.bad{border-color:#B84A42;color:#8A2B25;background:#FDF2F1}
.cmd-infog{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;font-size:12px;margin:4px 0;padding:5px 6px;border:1px dashed #DCE3EA;border-radius:6px}
.cmd-infog b{grid-column:1/-1;font-size:12px;color:#25415C;margin-bottom:2px}
.cmd-infog .k{color:#61748A;white-space:nowrap}
.cmd-infog .v{color:#22384C;word-break:break-all}
.cmd-infodemo{font-size:11px;color:#8A5A0B;background:#FFF8EA;border:1px solid #EBD9AE;border-radius:5px;padding:2px 6px;margin:4px 0}
.cmd-infonote{font-size:12px;color:#41505E;margin-top:4px}
.cmd-conflictnote{position:absolute;left:10px;top:10px;z-index:1000;max-width:min(560px,72%);background:rgba(253,242,241,.96);border:1px solid #E2A9A4;border-radius:6px;padding:5px 9px;font-size:12px;color:#8A2B25;cursor:pointer;text-align:left}
.cmd-situation{border:1px solid #D8E0E8;border-radius:8px;padding:8px 10px;margin:0 0 8px;background:#FBFCFD}
.cmd-situation #cmd-sit-needs[hidden]{display:none}
.cmd-situation-head{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:6px}
.cmd-situation-acts{margin:2px 0 6px}
.cmd-sitlive{background:#FDF3E4;border:1px solid #E4C27E;color:#6B4A0E;border-radius:6px;padding:4px 8px;font-size:12px;margin-bottom:6px}
.cmd-observe{margin-top:6px;font-size:12px}
.cmd-observe summary{cursor:pointer;color:#33586F}
.cmd-observe form{display:grid;gap:5px;margin-top:6px}
.cmd-fld{display:flex;flex-direction:column;gap:2px;margin:4px 0}
.cmd-fld span{font-size:11px;color:#41505E;display:flex;gap:6px;align-items:baseline}
.cmd-fld span i{font-style:normal;font-size:10px;color:#9A6A15}
.cmd-fields{border:1px solid #E1E7ED;border-radius:6px;padding:6px 7px;margin:6px 0;background:#FAFCFE;max-height:230px;overflow-y:auto}
.cmd-fields b{font-size:12px;color:#25415C}
.cmd-fldrule{font-size:11px;color:#61748A;margin:2px 0 4px;line-height:1.45}
.cmd-anno-prev{font-size:11px;color:#25415C;margin:3px 0}
.lg-hatch{width:16px;height:10px;display:inline-block;border:1px solid #B84A42;background:repeating-linear-gradient(45deg,#F6D9D5 0 3px,#fff 3px 6px)}
.lg-belt{width:16px;height:10px;display:inline-block;border:1px solid #7A4B2A;background:#E8D3BC}
.lg-idx{color:#93A3B4;font-size:10px;margin-left:4px}
@media (max-width:760px){.cmd-toolgroups{flex-direction:row}.cmd-mapbar{overflow-x:auto}.cmd-groupbtns{flex-direction:row;flex-wrap:wrap}}
.cmd-modehint{position:absolute;left:10px;bottom:10px;z-index:1000;background:rgba(255,255,255,.95);border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:4px 10px;font-size:12px}
.cmd-side{display:flex;flex-direction:column;gap:8px;min-height:0}
.cmd-tabs{display:flex;gap:4px}
.cmd-tab{flex:1;background:#fff;border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:5px 0;font-size:13px;cursor:pointer;color:#33475B}
.cmd-tab.on{background:#245B78;border-color:#245B78;color:#fff;font-weight:600}
.cmd-side[data-tab="plans"] #cmd-advisor{display:none}
.cmd-side:not([data-tab="plans"]) #cmd-plans{display:none}
.cmd-advisor,.cmd-plans{background:#FFFFFF;border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;min-height:0;flex:1 1 0}
.cmd-advisor-head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--line,#D7DDE3);padding-bottom:6px;margin-bottom:6px}
.cmd-goal{margin:4px 0;font-size:13px}
.cmd-thread{overflow-y:auto;flex:1 1 auto;display:flex;flex-direction:column;gap:8px;padding-right:2px;min-height:120px}
.cmd-msg{border:1px solid var(--line,#D7DDE3);border-left:4px solid #7C8DA0;border-radius:6px;padding:7px 9px;font-size:13px}
.cmd-msg .t{font-weight:600;margin-bottom:2px}
.cmd-msg .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;align-items:center}
.cmd-msg-info{border-left-color:#2E6FA8}
.cmd-msg-warn{border-left-color:#C08A1A;background:#FFFBF0}
.cmd-msg-danger{border-left-color:#B84A42;background:#FFF5F4}
.cmd-msg-dim{border-left-color:#A9B4BF;color:#41505E}
.cmd-compose{border-top:1px solid var(--line,#D7DDE3);padding-top:8px;display:flex;flex-direction:column;gap:6px}
.cmd-compose-row{display:flex;gap:6px}
.cmd-compose-row input{flex:1;min-width:0}
.cmd-compose-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.cmd-planctx{font-size:12px;color:#41505E;margin:2px 0 6px}
.cmd-plan-hint{font-size:11.5px;color:#647382;margin:2px 0 6px;line-height:1.5}
.cmd-plan-list{overflow-y:auto;flex:1 1 auto;display:flex;flex-direction:column;gap:8px;padding-right:2px;min-height:100px}
.cmd-plan-card{border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:8px 10px}
.cmd-plan-card.on{border-color:var(--plan-color,#245B78);box-shadow:0 0 0 1px var(--plan-color,#245B78) inset}
.cmd-plan-card[data-plan-color] .cmd-plan-head>b::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--plan-color,#55606B);margin-right:6px}
.cmd-plan-foot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:6px}
.cmd-plan-foot .dim{font-size:11px;color:#647382}
.cmd-simbtn .sim-t{font-weight:600}
.cmd-simbtn.on{background:#245B78;border-color:#245B78;color:#FFFFFF}
.cmd-simstrip{position:absolute;right:10px;bottom:10px;z-index:1000;display:flex;align-items:center;gap:8px;flex-wrap:wrap;max-width:min(760px,88%);background:rgba(255,255,255,.96);border:1px solid var(--line,#D7DDE3);border-radius:6px;padding:5px 9px;font-size:12px;box-shadow:0 1px 4px rgba(20,30,40,.12)}
.cmd-simstrip>b{font-size:12px;white-space:nowrap}
.sim-chip{display:inline-flex;align-items:center;gap:5px;border:1px solid #55606B;border-radius:11px;padding:1px 8px;background:#FFFFFF;white-space:nowrap}
.sim-chip i{width:8px;height:8px;border-radius:50%;display:inline-block}
.sim-cmp{display:inline-flex;align-items:center;gap:4px;cursor:pointer;white-space:nowrap}
.cmd-plan-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.cmd-plan-sum{margin:4px 0;font-size:13px}
.cmd-plan-meta{list-style:none;margin:0 0 4px;padding:0;font-size:12px;color:#41505E;display:flex;gap:12px;flex-wrap:wrap}
.cmd-plan-verdict{font-size:12px;color:#41505E;margin:2px 0 4px}
.cmd-planblock{font-size:12px;color:#8A3B34;margin:0 0 4px}
.cmd-plan-impact{font-size:12px;color:#754A0E;background:#FFF7E8;border-left:3px solid #C48427;padding:5px 7px;margin:0 0 6px}
.cmd-plan-more summary{cursor:pointer;font-size:12px;color:#2E6FA8}
.cmd-plan-more h5{margin:6px 0 2px;font-size:12px}
.cmd-plan-more ul{margin:0 0 4px;padding-left:18px;font-size:12px}
.cmd-plan-actions{display:flex;flex-direction:column;gap:6px;margin-top:6px}
.cmd-plan-acts>summary{cursor:pointer;font-size:12px;color:#2E6FA8;margin-top:4px}
.cmd-adoptform{display:flex;flex-direction:column;gap:4px}
.cmd-riskok{font-size:12px;display:flex;gap:6px;align-items:center}
.cmd-decisions{margin-top:6px}
.cmd-decisions>summary{cursor:pointer;font-size:12.5px;color:#41505E}
.cmd-decisions ul{margin:4px 0 0;padding-left:18px;font-size:12px;color:#41505E}
.cmd-selfopt summary{cursor:pointer;font-size:13px;color:#2E6FA8;margin-top:6px}
.cmd-bottom{background:#FFFFFF;border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:8px 12px;font-size:13px;display:flex;gap:10px;align-items:center}
.cmd-bottom-label{font-weight:600;white-space:nowrap}
.cmd-followup-card{border:1px solid var(--line,#D7DDE3);border-left:3px solid #C08A1A;border-radius:6px;padding:7px 9px;margin:6px 0;overflow-wrap:anywhere}
.cmd-followup-card p{font-size:12px;margin:4px 0}
.cmd-followup-gaps{font-size:12px;color:#8A3B34;padding-left:20px}
.cmd-drawers{display:flex;flex-wrap:wrap;gap:6px;align-items:flex-start}
.cmd-drawer{background:#FFFFFF;border:1px solid var(--line,#D7DDE3);border-radius:8px;padding:6px 10px;flex:0 1 auto}
.cmd-drawer[open]{flex:1 1 100%}
.cmd-drawer>summary{cursor:pointer;font-weight:600;font-size:12.5px}
.cmd-fourcat h3{margin:10px 0 4px}
@media (min-width:1280px){.cmd-page{--cmd-side-w:340px}}
@media (max-width:1199px){
  .cmd-main{grid-template-columns:46px minmax(0,1fr);height:auto}
  .cmd-side{grid-column:1/-1;flex-direction:column}
  .cmd-advisor,.cmd-plans{min-height:340px}
}
@media (max-width:900px){
  .cmd-main{grid-template-columns:minmax(0,1fr)}
  .cmd-rail{flex-direction:row;flex-wrap:wrap}
  .cmd-side{flex-direction:column}
  .cmd-mapwrap{min-height:420px}
  .cmd-simstrip{left:10px;right:10px;bottom:8px;max-width:none}
  .cmd-bottom{flex-wrap:wrap}
}
"""


# ---------------------------------------------------------------------------
# 组装


def _runtime_strip(state, role, token):
    host = getattr(state, 'simulation_host', None)
    if host is None or role == 'public':
        return ''
    view = host.view()
    if view is None:
        return ''
    controls = ''
    if role == 'commander' and token:
        csrf = state.model_session(token).csrf
        controls = (
            '<form class="sand-runtime-actions" method="post" action="/sand/runtime">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="event_id" value="{esc(view["event_id"])}">'
            f'<input type="hidden" name="expected_version" value="{view["revision"]}">'
            '<button name="action" value="refresh_now">立即刷新</button>'
            '<button name="action" value="play">播放</button>'
            '<button name="action" value="pause">暂停模拟</button>'
            '<button name="action" value="single_step">单步</button>'
            '<label>周期 <select name="minutes" aria-label="刷新周期">'
            + ''.join(f'<option value="{n}"{" selected" if n == view["refresh_minutes"] else ""}>{n} 分钟</option>'
                      for n in (3, 5, 10, 30, 60))
            + '</select></label><button name="action" value="set_refresh_minutes">应用周期</button></form>')
    payload = json.dumps(view, ensure_ascii=False).replace('<', '\\u003c')
    return (f'<section class="sand-runtime" id="sand-runtime" data-event="{esc(view["event_id"])}">'
        '<div class="sand-runtime-line"><strong>匿名模拟</strong><span data-runtime-state>已暂停</span>'
        f'<span data-runtime-period>刷新 {view["refresh_minutes"]} 分钟</span>'
        '<span data-runtime-received>最近接收：未接收</span></div>'
        '<details><summary>模拟控制与观测</summary>' + controls
        + '<p>每分钟一批；暂停只停止模拟。全部观测待核，不代表现场实时数据。</p>'
        '<div data-runtime-observations></div></details>'
        '<p data-runtime-notice role="status" aria-live="polite"></p>'
        f'<script type="application/json" data-runtime-initial>{payload}</script></section>')


def render_sand_page(
    state,
    *,
    role: str = "commander",
    viewer_word: str = "指挥员",
    measure_result: dict | None = None,
    measure_error: str = "",
    measure_saved: str = "",
    ask_error: str = "",
    ask_question: str = "",
    source_page=None,
    source_error: str = "",
    source_url: str = "",
    deepseek_preview=None,
    draft_owner: str = "",
    draft_event: str = "",
    viewer_id: str = "",
    simulation_token: str | None = None,
    viewer_token: str | None = None,
) -> str:
    """指挥工作台整页（docs/18 V1.0 排版）。role 决定可见性与操作面。"""
    if role == 'captain':
        from . import business_modules as business
        event = state.registry.events.get(state.active_event_id)
        stage_view = sandtable.derive_stage(state.workspace, event=event, ledger=state.ledger)
        return ('<div class="cmd-page" id="cmd-page" data-sand-role="captain">'
                f'<style>{_CMD_CSS}</style>'
                + _top_strip(state, event, stage_view, viewer_word)
                + '<div class="cmd-main cmd-main-public">' + _tools_rail(state, 'public')
                + _map_shell(state, 'captain') + '</div>'
                + '<section id="cmd-plans">' + business.overview(state)
                + business.captain_tasks(state, viewer_token)
                + business.captain_actions() + '</section>'
                + render_shell_navigation(role='public', event_name=event.name,
                    data_time=_data_time(state), has_event=True,
                    contains_demo=any(m.is_demo or m.demo_notice for m in _visible_marks(state, role)))
                + '<link rel="stylesheet" href="/static/sand-shell.css">'
                + '<script src="/static/sand-shell.js"></script></div>')
    event = None
    registry = getattr(state, "registry", None)
    active_id = getattr(state, "active_event_id", "")
    if registry is not None and active_id:
        event = registry.events.get(active_id)
    ledger = getattr(state, "ledger", None)
    stage_view = sandtable.derive_stage(state.workspace, event=event, ledger=ledger)
    followups = _followup_view(state, role, viewer_id)
    # 现场指挥待办（七环节小 SOP）：只读计算；任何异常退化为空态，不影响本页其他功能。
    from . import field_sop_view as _field_sop
    field = _field_sop.build(state) if role != 'public' else None
    live = None
    if role == 'commander' and event is not None:
        from .live_context import event_context
        try:
            live, _ = event_context(state, active_id)
        except ValueError:
            pass  # Existing SOP remains available; the feed fails closed independently.
    parts = [
        f'<div class="cmd-page" id="cmd-page" data-sand-role="{esc(role)}" '
        f'data-draft-owner="{esc(draft_owner if role != "public" else "")}" '
        f'data-draft-event="{esc(draft_event if role != "public" else "")}">',
        f"<style>{_CMD_CSS}</style>",
        _top_strip(state, event, stage_view, viewer_word),
        _runtime_strip(state, role, simulation_token),
        '<div class="cmd-main' + (' cmd-main-public' if role == "public" else "") + '">',
        _tools_rail(state, role),
        _map_shell(state, role),
        (
            "" if role == "public" else
            '<aside class="cmd-side" id="cmd-side" data-tab="advisor">'
            '<div class="cmd-tabs" role="tablist">'
            '<button type="button" class="cmd-tab on" data-cmdtab="advisor">对话</button>'
            '<button type="button" class="cmd-tab" data-cmdtab="plans">方案</button>'
            "</div>"
            + _advisor_panel(state, role, stage_view)
            + _plan_panel(state, role)
            + "</aside>"
        ),
        "</div>",
        _bottom_bar(state, role),
        _drawers(
            state, role,
            measure_result=measure_result,
            measure_error=measure_error,
            measure_saved=measure_saved,
            ask_error=ask_error,
            ask_question=ask_question,
            source_page=source_page,
            source_error=source_error,
            source_url=source_url,
            deepseek_preview=deepseek_preview,
            followups=followups,
            viewer_id=viewer_id,
        ),
        '<link rel="stylesheet" href="/static/field-sop.css">',
        ('<div class="field-dock">' + field['steps_html'] + '</div>' if field else ""),
        (field['step_panels'] if field else ""),
        '<script src="/static/field-sop.js"></script>',
        render_shell_navigation(
            role=role,
            live_context=live,
            event_name=event.name if event else "未进入事件任务空间",
            data_time=_data_time(state),
            contains_demo=(getattr(state, 'simulation_host', None) is not None
                           or any(m.is_demo or m.demo_notice for m in _visible_marks(state, role))),
            pending_summary_html=_followup_summary(followups, role),
            has_event=event is not None,
            early_response_html=render_early_response(state, role),
            situation_html=render_situation(state, role),
            force_html=render_force(state, role),
            deployment_html=render_deployment(state, role),
            execution_html=render_execution(state, role),
            cleanup_html=render_cleanup(state, role),
            emergency_html=render_emergency(state, role),
            publication_html=render_publication(state, role),
            handover_html=render_handover(state, role),
            show_workflow=False,
        ),
        '<link rel="stylesheet" href="/static/sand-shell.css">',
        '<script src="/static/sand-drafts.js"></script>',
        '<script src="/static/sand-shell.js"></script>',
        '<script src="/static/sand-runtime.js"></script>',
        '<script src="/static/source-review.js"></script>',
        '<script src="/static/live-context.js"></script>',
        '<link rel="stylesheet" href="/static/intelligence.css">',
        __import__('panda_brain.sand_command', fromlist=['render']).render(state, role, viewer_id),
        '<link rel="stylesheet" href="/static/sand-command.css">',
        '<script src="/static/sand-command.js"></script>',
        '<link rel="stylesheet" href="/static/map-brief.css">',
        '<script src="/static/map-demo.js"></script>',
        '<script src="/static/map-brief.js"></script>',
        '<script src="/static/command-sop.js"></script>',
        __import__('panda_brain.emergency_ui', fromlist=['map_assets']).map_assets(state),
        '<link rel="stylesheet" href="/static/manual-tools.css">',
        '<script src="/static/manual-tools.js"></script>',
        "</div>",
    ]
    return "".join(parts)


STAGE_NAMES = STAGES  # re-export for tests/nav
