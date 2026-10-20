# -*- coding: utf-8 -*-
"""一键构建「鄂西北山地」模拟场景 → impl/data/demo/。

产物（供 启动熊猫智脑.bat 复制为工作副本）：
  demo_hanjiang.db        战役工作区（参谋提案/方案/决断/命令/执行反馈/标绘全链）
  event_registry.json      火情事件 + 名册 + 成员账号
  materials.json           物资档案/中队/物资点/到场快照/核对结果
  reviews.json             复盘台账（问题/假设/整改/经验/负责人）
  alarm_inbox.json         外部报警收件箱（待确认演示）
  imagery.json             影像候选台账（后台送达演示）

纪律：所有数据都经页面同一批公开 handler 写入（真哈希链、真状态机），
不伪造状态、不绕过校验；缺哪步就如实少哪步。可反复重跑，幂等重建。

用法：  python -X utf8 tools/build_demo_scenario.py
"""
from __future__ import annotations

import math
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

IMPL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(IMPL))

OUT = IMPL / "data" / "demo"
DB = OUT / "demo_hanjiang.db"

from panda_brain.auth import GeneralEntryAuth                      # noqa: E402
from panda_brain.campaign_runner import DRILL_SECRET, run_campaign  # noqa: E402
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD, PersonRecord  # noqa: E402
from panda_brain.panel import PanelState                             # noqa: E402

PEOPLE = [
    PersonRecord(name="张三", phone="13912340001", unit="县防火办",
                 home_position="扑救一组（一分区）", is_leader=True),
    PersonRecord(name="李四", phone="13912340002", unit="林场护站",
                 home_position="扑救二组（二分区）"),
    PersonRecord(name="王五", phone="13912340003", unit="县消防队",
                 home_position="扑救三组（三分区）", is_leader=True),
]

# ---------------------------------------------------------------------------
# P05 战术态势演示场景：23 个地图对象 + 1 个隔火带缺口子记录
#
# 状态分布固定：已发布 10・已核验 7・待核验 4・草稿 2。
# 鄂西北山地地形图仅用于选取演示视角；标绘坐标为人工模拟，未经控制点核对。
# 时间按构建时刻回推，
# 以免演示对象一打开就全部“已失效”。

LAT0, LON0 = 32.095, 110.22
SCALE = 0.45
KOS = 1.0 / math.cos(math.radians(LAT0))
STATUS_FLOW = ["pending_review", "verified", "published"]


def _t(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")


def _iso(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).astimezone().isoformat(
        timespec="seconds")


def _pt(dlat: float, dlon: float) -> str:
    return f"{LAT0 + SCALE * dlat:.5f},{LON0 + SCALE * dlon:.5f}"


def _geo(dlat: float, dlon: float) -> tuple[float, float]:
    return (round(LAT0 + SCALE * dlat, 5), round(LON0 + SCALE * dlon, 5))


def _line(*pairs) -> str:
    return ";".join(_pt(a, b) for a, b in pairs)


def _ring(dlat: float, dlon: float, n: int = 10, r: float = 0.012) -> str:
    """不规则闭合环：顶点数 8–12、半径按固定三角函数变化，不用圆或正多边形。"""
    pts = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        rr = r * (1.0 + 0.34 * math.sin(3.0 * a) + 0.17 * math.cos(5.0 * a))
        pts.append((dlat + rr * math.sin(a), dlon + rr * math.cos(a) * KOS))
    return _line(*pts)


DEMO_NOTE = "演示数据·位置为演示近似位置·非现场实时数据"

#: (编号, 类型, 名称, 目标状态, 坐标, 专属字段, 责任单位)
MARKS = (
    ("F-01", "fire_area", "初始研判火场范围", "pending_review",
     _ring(0.014, 0.026, 10, 0.012),
     {"demo_code": "F-01", "basis": "卫星热点两处相邻高温点 + 无人机一号航拍判读",
      "observed_at": _t(150), "source_ref": "卫星热点-0827-01"},
     "县防火办"),
    ("F-02", "main_fireline", "东线重点火线", "published",
     _line((0.018, 0.030), (0.024, 0.038), (0.030, 0.044), (0.036, 0.052)),
     {"demo_code": "F-02", "priority": "重点",
      "observed_at": _t(35), "team": "扑救一分队",
      "source_ref": "现场报告-12"},
     "扑救一分队"),
    ("F-03", "hotspot", "南坡热点", "published",
     _pt(0.021, 0.034),
     {"demo_code": "F-03", "found_at": _t(95), "hot_status": "confirmed",
      "source_ref": "无人机影像-3", "unit": "扑救一分队"},
     "扑救一分队"),
    ("F-04", "hotspot", "北线烟点", "pending_review",
     _pt(0.046, 0.010),
     {"demo_code": "F-04", "found_at": _t(40), "hot_status": "suspected",
      "source_ref": "现场报告-13"},
     "扑救二分队"),
    ("F-05", "spread_direction", "东北蔓延方向", "published",
     _line((0.030, 0.044), (0.042, 0.058)),
     {"demo_code": "F-05", "direction": "东北",
      "basis": "演示研判：东北侧坡面与风向待现场核对",
      "decided_at": _t(30), "team": "扑救一分队", "source_ref": "现场研判-02"},
     "扑救一分队"),
    ("F-06", "hazard_zone", "东北侧危险区", "pending_review",
     _ring(0.040, 0.056, 7, 0.006),
     {"demo_code": "F-06", "risk_type": "飞火 + 陡坡",
      "risk_level": "high", "basis": "演示研判：坡向和风险边界待现场核对",
      "formed_at": _t(28), "affected": "模拟保护目标、东线火线北段"},
     "扑救一分队"),
    ("W-01", "firebreak", "东侧计划隔火带", "draft",
     _line((0.036, 0.062), (0.030, 0.070), (0.022, 0.076)),
     {"demo_code": "W-01", "planned_width": "30", "belt_status": "planned",
      "team": "扑救一分队", "belt_form": "开放带",
      "source_ref": "人工标绘-01"},
     "扑救一分队"),
    ("W-02", "firebreak", "北侧已开设隔火带", "verified",
     _line((0.052, 0.014), (0.056, 0.026), (0.052, 0.038)),
     {"demo_code": "W-02", "planned_width": "30", "actual_width": "28",
      "belt_status": "opened", "belt_form": "开放带", "completion": "80%",
      "team": "扑救二分队", "source_ref": "现场报告-11",
      "gap_ref": "W-03", "gap_status": "存在缺口（待核验）",
      "gap_segment": "2 号至 3 号节点之间",
      "gap_segment_index": "2", "gap_fraction": "0.5",
      "gap_width_m": "120", "gap_reported_at": _t(20),
      "gap_source": "无人机影像-4"},
     "扑救二分队"),
    ("W-04", "control_line", "南线封控线", "published",
     _line((-0.004, 0.010), (-0.002, 0.026), (-0.006, 0.040)),
     {"demo_code": "W-04", "reason": "控制无关人员进入南坡作业区",
      "unit": "县消防队", "active_since": _t(200),
      "release_cond": "南线解除并经前指确认", "checked_at": _t(60),
      "commander_confirm": "司令确认（演示）", "source_ref": "前指通知-05"},
     "县消防队"),
    ("T-01", "team_position", "扑救一分队当前位置", "verified",
     _pt(0.026, 0.036),
     {"demo_code": "T-01", "team_id": "扑救一分队", "leader": "张三",
      "task": "东线清理与看守", "reported_at": _t(18),
      "comm_status": "畅通", "position_status": "非实时回报", "risk_level": "high"},
     "扑救一分队"),
    ("T-02", "team_route", "扑救一分队确认路线", "published",
     _line((0.004, -0.025), (0.012, -0.008), (0.020, 0.020), (0.026, 0.036)),
     {"demo_code": "T-02", "team_id": "扑救一分队", "route_type": "confirmed",
      "purpose": "开进东线并接管北段", "start": "前线指挥部",
      "end": "东线 3 号桩", "plan_time": "今日 16:30 前",
      "route_status": "confirmed", "commander_confirm": "司令确认（演示）",
      "reported_at": _t(50), "source_ref": "命令确认-01"},
     "扑救一分队"),
    ("T-07", "team_route", "扑救一分队回报轨迹", "verified",
     _line((0.004, -0.025), (0.011, -0.005), (0.018, 0.017), (0.024, 0.030)),
     {"demo_code": "T-07", "team_id": "扑救一分队", "route_type": "actual",
      "purpose": "模拟回报的已行进路段", "start": "前线指挥部",
      "end": "东线作业点", "reported_at": _t(18),
      "source_ref": "现场回报-轨迹01"},
     "扑救一分队"),
    ("T-03", "team_position", "扑救二分队当前位置", "verified",
     _pt(0.050, 0.028),
     {"demo_code": "T-03", "team_id": "扑救二分队", "leader": "李四",
      "task": "北侧隔火带抢修与缺口封堵", "reported_at": _t(22),
      "comm_status": "待接入", "position_status": "非实时回报"},
     "扑救二分队"),
    ("T-04", "team_route", "扑救二分队计划路线", "draft",
     _line((0.044, 0.004), (0.048, 0.016), (0.052, 0.026), (0.041, 0.055)),
     {"demo_code": "T-04", "team_id": "扑救二分队", "route_type": "plan",
      "purpose": "转场封堵 W-03 缺口", "start": "前沿集结点",
      "end": "北侧隔火带缺口", "plan_time": "待前指排定"},
     "扑救二分队"),
    ("T-05", "reserve_point", "预备队待命点", "published",
     _pt(0.010, -0.030),
     {"demo_code": "T-05", "team_id": "扑救三分队", "leader": "王五",
      "standby_status": "待命", "reported_at": _t(25), "unit": "县消防队",
      "source_ref": "指挥部登记-02"},
     "县消防队"),
    ("T-06", "staging_point", "前沿集结点", "published",
     _pt(0.044, 0.004),
     {"demo_code": "T-06", "purpose": "北线前进集结", "unit": "扑救二分队",
      "open_status": "启用", "safety_status": "safe",
      "safety_confirm_at": _t(15), "source_ref": "现场报告-10"},
     "扑救二分队"),
    ("S-01", "command_post", "前线指挥部", "published",
     _pt(0.004, -0.025),
     {"demo_code": "S-01", "level": "前线级", "leader": "指挥长（演示）",
      "comm_status": "畅通", "active_since": _t(300),
      "source_ref": "指挥部登记-01"},
     "县防火办"),
    ("S-02", "supply_point", "南线物资前置点", "verified",
     _pt(-0.008, -0.023),
     {"demo_code": "S-02", "category": "水带、水泵、消耗品",
      "unit": "县防火办", "availability": "可用",
      "source_ref": "物资保障登记-03"},
     "县防火办"),
    ("S-03", "medical_point", "南线医疗救护点", "verified",
     _pt(-0.010, -0.018),
     {"demo_code": "S-03", "capability": "现场急救与转运",
      "open_status": "开放", "unit": "县人民医院",
      "source_ref": "医疗保障登记-01"},
     "县人民医院"),
    ("S-04", "water_source", "河谷模拟取水点", "published",
     _pt(0.000, -0.035),
     {"demo_code": "S-04", "water_type": "河流取水点", "draw_status": "可取水",
      "unit": "扑救一分队", "source_ref": "现场报告-07"},
     "扑救一分队"),
    ("S-05", "drone_position", "无人机一号", "verified",
     _pt(0.030, 0.050),
     {"demo_code": "S-05", "drone_id": "演示-鄂西北01号",
      "operator": "飞行组", "heading": "45", "reported_at": _t(12),
      "source_ref": "飞行组回报-06"},
     "飞行组"),
    ("E-01", "drone_coverage", "无人机一号粗略影像覆盖", "pending_review",
     _ring(0.032, 0.048, 4, 0.008),
     {"demo_code": "E-01", "image_id": "IMG-DEMO-0001",
      "captured_at": _t(15), "positioning": "rough",
      "transform": "人工平移 +120 米／缩放 1.08／旋转 15 度",
      "opacity": "0.35", "source_ref": "无人机影像-4"},
     "飞行组"),
    ("R-01", "protected_target", "东侧模拟保护目标", "published",
     _pt(0.038, 0.066),
     {"demo_code": "R-01", "target_type": "演示假设的居民点",
      "protection_level": "一级", "risk_status": "受威胁",
      "unit": "村组干部", "source_ref": "事件基础资料-01"},
     "村组干部"),
)

#: 旧式通用标绘的兼容与「待人工归类」提示由单元测试覆盖，
#: 不进演示首屏（首屏固定 22 个地图对象 + 1 个缺口子记录）。



def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 1) 战役工作区（参谋链全量：提案→决断→命令→执行→核验）
    print("== 运行战役脚本（确定性）==")
    result = run_campaign()
    ws = result.workspace
    print(f"   waves={result.waves_executed} orders={len(result.order_ids)} "
          f"verified={result.verification_passed} 事件链={len(ws.events)}")

    auth = GeneralEntryAuth(secret=DRILL_SECRET)
    state = PanelState(
        ws,
        db_path=DB,
        auth=auth,
        registry_path=OUT / "event_registry.json",
        materials_path=OUT / "materials.json",
        reviews_path=OUT / "reviews.json",
        imagery_path=OUT / "imagery.json",
        alarm_inbox_path=OUT / "alarm_inbox.json",
        duty_subjects=(PEOPLE[0].phone,),
    )
    state.login(DRILL_SECRET)   # 与页面登录同一入口（持凭证，事件空间解锁可重建）

    # 2) 长期人员库 + 事件 + 名册 + 签到（与页面按钮同一 handler 序列）
    print("== 建档：人员库 / 火情事件 / 名册 / 入岗 ==")
    state.registry.import_person_library(PEOPLE)
    ws.import_person_library(PEOPLE)
    duty_token, duty_error = state.member_login(PEOPLE[0].phone, DEMO_INITIAL_PASSWORD)
    if duty_token is None:
        raise RuntimeError(f"演示值班人员登录失败：{duty_error}")
    state.handle_create_event(
        name="鄂西北山地林火（模拟）", report_no="演示-鄂西北01号",
        discovered_at=_iso(8 * 60), source="演示导入",
        location="深河乡周边山地（演示视角，非现场定位）", lat="32.1", lon="110.2",
    )
    event_id = sorted(state.registry.events)[-1]
    state.active_event_id = event_id
    state.handle_event_action(event_id, "start", duty_token=duty_token)
    ws = state.workspace   # 启动处置后切到事件任务空间：后续直接调用一律用当前空间
    state.handle_deputy_appointment(
        event_id, duty_token=duty_token, subject_id=PEOPLE[1].phone,
        evidence="演示线下指定记录-001", request_id="demo-deputy-appointment",
        occurred_at=_iso(7 * 60),
    )
    state.handle_event_action(event_id, "issue-qr")
    deputy_token, deputy_error = state.member_login(PEOPLE[1].phone, DEMO_INITIAL_PASSWORD)
    if deputy_token is None:
        raise RuntimeError(f"演示副指挥长登录失败：{deputy_error}")
    state.handle_personnel_management(event_id, member_token=deputy_token,
        operation="add_roster", details={"phones": [person.phone for person in PEOPLE]},
        basis="模拟副指挥长明确名册", expected_version=0, request_id="demo-named-roster")
    for version, (phone, pos) in enumerate((("13912340001", "pjjz-1"), ("13912340003", "pjjz-3"))):
        state.handle_personnel_management(event_id, member_token=deputy_token,
            operation="assign_roster", details={"phone":phone, "pos_code":pos},
            basis="模拟副指挥长明确岗位", expected_version=version, request_id="demo-named-position-"+phone)
    ws = state.workspace
    ws.scan_check_in("pjjz-1", phone="13912340001")   # 张三 已到场
    ws.scan_check_in("pjjz-3", phone="13912340003")   # 王五 到场…
    ws.check_out("pjjz-3", phone="13912340003")        # …后撤离（八态演示）
    print(f"   事件 {event_id}；名册 {len(PEOPLE)} 人；李四未到场（演示待指派态）")

    # 3) 标绘：P19 战术态势演示对象（经页面同一批 handler，真闸门真审计）
    print("== 标绘（P05 战术态势：23 个地图对象 + 1 个隔火带缺口子记录）==")
    code_ids: dict[str, str] = {}
    n_marks = 0
    for code, kind, title, target, geo, details, unit in MARKS:
        aid = state.handle_sand_annotate(
            kind=kind, title=title, points="", geo_text=geo,
            note="演示数据（位置为演示近似位置，非现场实时数据）",
            details=details, responsible_unit=unit, is_demo=True,
            demo_notice=DEMO_NOTE,
        )
        code_ids[code] = aid
        n_marks += 1
        if target == "draft":
            continue
        # 沿合法流转逐步推进：草稿→待核验→已核验→已发布
        for step in STATUS_FLOW[:STATUS_FLOW.index(target) + 1]:
            state.handle_sand_annotation_status(aid, step)
    counts: dict[str, int] = {}
    for mark in ws.annotations.values():
        counts[mark.status.value] = counts.get(mark.status.value, 0) + 1
    # 不调用 publish_verified_annotations：已核验就该停在“已核验·待发布”，
    # 否则状态分布与定稿 §4.1 不一致。
    gaps = sum(len(m.gap_records()) for m in ws.annotations.values())
    print(f"   地图对象 {n_marks} 个，状态分布 {counts}，缺口子记录 {gaps} 条")

    # 3.6) 命令战术层夹具：下达→送达→接收→条件确认→开始→回执
    print("== 命令战术层与冲突记录（演示）==")
    from panda_brain.models import Evidence, SourceTrustLevel

    order_id = next((oid for oid, o in sorted(ws.orders.items())
                     if o.status.value in {"confirmed", "active", "paused"}), "")
    if order_id:
        route_id = code_ids.get("T-02", "")
        ws.open_command_flow(
            order_id, event_id=event_id, task_level="high_risk",
            target="东侧模拟火线清理与看守，不越过北侧脊线", unit="扑救一分队",
            route_id=route_id, start_time=_t(40), deadline=_t(-120),
            safety_cond="风速大于 15 米/时或出现飞火即停",
            stop_cond="模拟火线越过北侧脊线立即撤",
            receipt_need="执行中每 10 分钟一次；异常 3 分钟内补充",
        )
        ws.advance_command_flow(order_id, "pending_validation", actor="参谋部", at=_t(38))
        ws.advance_command_flow(order_id, "pending_approval", actor="参谋部", at=_t(37))
        ws.advance_command_flow(order_id, "pending_confirm", actor="参谋部", at=_t(36))
        ws.confirm_command_flow(order_id, actor="local-general-entry", at=_t(35))
        ws.command_delivery(order_id, delivered_at=_t(34), viewed_at=_t(33))
        ws.command_ack(order_id, by="张三", result="accepted_ready", at=_t(32))
        ws.confirm_execution_conditions(
            order_id, by="张三", note="现场条件：风 3 级、通信畅通、撤离路已清", at=_t(31))
        ws.begin_command_execution(order_id, by="张三", at=_t(30))
        ws.record_command_receipt(
            order_id, kind="stage_done", by="张三", at=_t(8),
            where="东线 3 号桩附近", content="开通 400 米，剩余约 260 米",
            personnel="12 人在位，无伤亡", risk="北段烟大、能见度低",
            need="请求水带补充", source="现场报告-12", request_id="demo-r1")
        ev = Evidence.create(
            id="ev-demo-source-01", source="现场报告-12（演示）",
            content="东线已开通 400 米，剩余约 260 米；北段能见度低。",
            trust_level=SourceTrustLevel.AUTHORIZED)
        ws.record_evidence(ev)
    added = ws.refresh_conflict_records()
    flow = ws.command_flow(order_id) if order_id else None
    print(f"   命令 {order_id or '无'}；战术阶段 {flow.stage_word if flow else '—'}；"
          f"回执 {len(flow.receipts) if flow else 0} 条；冲突记录 {len(added)} 条")

    # 3.5) 方案地图推演层：三套在议方案各带一套演示几何，
    # 前台「查看推演」以虚线+「推演」标注叠加，绝不等同正式态势。
    SIM_LIBRARY = (
        (("line", (_geo(0.020, -0.030), _geo(0.028, 0), _geo(0.020, 0.030)), "北侧阻隔线"),
         ("point", (_geo(0.024, 0),), "一分队部署点")),
        (("polygon", (_geo(0.005, 0.020), _geo(0.020, 0.045),
                      _geo(0.035, 0.020), _geo(0.020, 0.008)), "东线合围区"),),
        (("line", (_geo(-0.010, -0.035), _geo(0, -0.010), _geo(-0.010, 0.015)), "西段防线"),
         ("line", (_geo(-0.020, 0.005), _geo(-0.012, 0.030)), "南段防线")),
    )
    n_sim = 0
    for i, opt in enumerate(sorted(ws.options.values(), key=lambda o: o.id)):
        if getattr(opt, "status", "") not in {"active", "affected"}:
            continue
        opt.sim_shapes = tuple(
            {"geom": g, "pts": tuple(tuple(round(c, 5) for c in pt) for pt in pts), "label": lb}
            for g, pts, lb in SIM_LIBRARY[i % len(SIM_LIBRARY)]
        )
        n_sim += 1
    print(f"   方案推演层 {n_sim} 套（演示几何，非实测）")

    # 4) 物资：档案/中队/携带/物资点 + 核对（制造真实缺口）
    print("== 物资底账 + 到场快照 + 缺口 ==")
    for name, cat, unit, qty in (
        ("高压水泵", "装备", "台", 4), ("水带", "装备", "盘", 8),
        ("水枪", "装备", "支", 4), ("灭火水袋", "消耗品", "个", 20),
    ):
        # Trusted synthetic seed construction, not a runtime write surface.
        state.ledger.upsert_item(name=name, category=cat, unit=unit, default_carry_qty=qty)
    state.ledger.add_squad(name="扑救一分队", member_phones=("13912340001",),
                           headcount_planned=12, leader_phone="13912340001", library=state.registry.library)
    state.ledger.add_squad(name="扑救三分队", member_phones=("13912340003",),
                           headcount_planned=10, leader_phone="13912340003", library=state.registry.library)
    ledger = state.ledger
    items = {it.name: it.item_id for it in ledger.items.values()}
    squads = sorted(ledger.squads.values(), key=lambda s: s.squad_id)
    for squad in squads:
        for iname in ("高压水泵", "水带", "水枪"):
            ledger.set_carry_line(squad.squad_id, items[iname])
    ledger.add_supply_point(name="林场物资库", location="林场所院内",
                            contact_name="库管员老周", supplies={items["高压水泵"]: 2})
    ledger.add_supply_point(name="前指临时料堆", location="前指东侧",
                            supplies={items["水带"]: None})
    # 到场快照：与入岗确认同一公开域 API（刷码链路由 panel 自动调，构建器补执一步）
    state.ledger.generate_event_snapshot(
        event_id,
        {"13912340001": squads[0].squad_id, "13912340003": squads[1].squad_id},
    )
    for squad, iname, usable in ((squads[0], "高压水泵", 3), (squads[0], "水带", 5)):
        ledger.check_line(event_id, squad.squad_id, items[iname], qty_usable=usable,
                          reason="历史匿名演示差异；不作为当前具名流水来源")
    ledger.save(state.materials_path)
    print("   2 中队历史匿名快照；未绑定具名当前来源，正式数量保持待核对")

    # 5) 外部报警收件箱：留 2 条待确认（演示「核对→确认建档/拒绝」）
    print("== 外部报警收件箱 ==")
    inbox = state.alarm_inbox
    _t0 = "2026-08-25T09:05:00+08:00"
    r1 = inbox.ingest_message({
        "messageId": "demo-sat-1", "messageType": "FIRE_EVENT", "schemaVersion": "1.0",
        "sourceSystem": "DEMO_CONSOLE", "sourceRecordId": "sat-2026-0825-01", "sourceVersion": 1,
        "eventTime": _t0, "operation": "CREATE",
        "data": {
            "name": "卫星热点·东线东侧", "discoveredAt": _t0,
            "location": "东线飞火方向 2.1km", "status": "registered", "level": "疑似",
            "initialReport": "【演示数据】卫星监测到 2 处相邻高温点，过火面积待核。",
            "keyTargets": [], "remark": "演示数据",
        },
    }, actor="demo-builder")
    _t1 = "2026-08-25T09:20:00+08:00"
    r2 = inbox.ingest_message({
        "messageId": "demo-iot-1", "messageType": "FIRE_EVENT", "schemaVersion": "1.0",
        "sourceSystem": "FOREST_IOT", "sourceRecordId": "iot-77-0825", "sourceVersion": 1,
        "eventTime": _t1, "operation": "CREATE",
        "data": {
            "name": "林区烟感报警", "discoveredAt": _t1,
            "location": "北线 3 号桩附近", "status": "registered", "level": "预警",
            "initialReport": "【演示数据】烟雾浓度 4.2mg/m³ 持续 6 分钟，超阈值。",
            "keyTargets": [], "remark": "演示数据",
        },
    }, actor="demo-builder")
    assert r1["code"] == "PENDING_CONFIRM" and r2["code"] == "PENDING_CONFIRM", (r1, r2)
    print("   2 条待确认（火情登记页可现场演示核对→建档）")

    # 6) 复盘：扫描产候选 + 登记链（结论级流转留给复盘负责人现场演示）
    print("== 复盘台账 ==")
    state.handle_review_owner_assign("13912340002")   # 李四＝复盘负责人
    state.handle_review_generate()
    state.handle_review_hyp_new(
        statement="隔离带开口位置偏东，未覆盖飞火方向",
        correlation="强", evidence="东线飞火点标绘 + 隔离带计划线走向",
        to_verify="调取航飞时间线与飞火点发现时间比对",
    )
    state.handle_review_rect_new(
        measures="演示候选：复核隔离带选线与下风侧封控条件",
        owner_unit="防火办", owner_person="张三", priority="高",
        verify_method="规程条文 + 演练复核",
    )
    state.handle_review_exp_new(
        content="演示候选：遇飞火时核对下风侧风险与现场安全条件",
        applicability="植被茂密、有飞火源的坡面火",
    )
    print("   负责人李四已指派；扫描候选 + 假设/整改/经验已登记")

    # 7) 影像台账：一条未配准演示影像（人工粗略覆盖，链接到 E-01）
    #    不新增地图对象：首屏固定 23 个地图对象 + 1 条隔火带缺口子记录。
    img = state.imagery.add_image(
        event_id=event_id,
        source_system="演示-鄂西北01号无人机", collected_by="飞行组",
        collected_at=_t(15), uploaded_at=_t(14), image_type="jpg",
        has_spatial_info=False,
        note="演示数据：普通航拍照片，未配准前不给精确坐标；粗略覆盖区见 E-01",
    )
    if code_ids.get("E-01"):
        state.imagery.link_annotation(img.image_id, code_ids["E-01"])
    print(f"== 影像台账：{img.image_id}（未配准・已关联 E-01）==")

    # 8) P20 态势自动发现演示波次（docs/20）：确定性扫描 + 一条演示观测。
    #    不生成复核批次：待复核变化留给指挥员现场点「复核当前态势」。
    scan_added = ws.situation_scan(event_id=event_id)
    obs = ws.register_observation(
        kind="hotspot", title="东南坡新增烟点",
        geo_points=(_geo(0.0135, 0.0188),),
        observed_at=_t(9), source_ref="无人机影像-9",
        positioning_status="rough",
        note="演示数据：模拟飞行组目视回报，定位为人工粗略覆盖",
        is_demo=True, event_id=event_id,
    )
    print(f"== P20：自动发现 {len(scan_added)} 项变化；"
          f"演示观测 {obs['annotation_id']}（{obs['status_word']}，"
          f"{len(obs['change_ids'])} 项变化）==")

    print("== 持久化 ==")
    state._save_registry()
    state._save_ledger()
    state._save_reviews()
    state._save_imagery()
    state._save_alarm_inbox()
    state.persist_if_db()
    print("\n== 产物 ==")
    for f in sorted(OUT.iterdir()):
        print(f"   {f.name:24s} {f.stat().st_size // 1024} KB")
    print(f"\n事件链长：{len(ws.events)}（DB 内全部经 append_event 哈希链）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
