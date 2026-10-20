"""P19 沙盘战术态势符号改造回归钉（docs/19_沙盘战术态势符号改造最终定稿_V1.0）。

覆盖定稿第 12 节的可量化项：
- 对象注册表：P0 15 类、P1 10 类、几何与图层归属；
- 几何与字段闸门：不规则火场、隔火带宽度、实际轨迹来源、未配准影像、
  责任单位/来源/时间缺失、备注不得替代必填字段；
- 状态分层：标绘状态与路线/冲突/时效状态互不覆盖，禁止跳跃流转；
- 冲突与四级决断：三级/二级不得发布，主备同阻升级，关闭图层不解除；
- 命令战术层：接收/条件/开始/回执不跳跃，超时只提醒，撤回须回执，关闭须闭合；
- 图层与页面：6 组业务图层 + 12 层渲染层映射、公共屏隔离、前台全中文；
- 审计与兼容：动作入哈希链，旧式通用标绘保留原类型且不自动获得战术语义。
"""

from __future__ import annotations
from tests.duty_support import duty_token

import json
import math
import re
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

from panda_brain import tactical_map as tm
from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
    TACTICAL_KINDS,
    from_geojson,
    to_geojson,
)
from panda_brain import sandview
from panda_brain.models import AdvisorRole, Option
from panda_brain.panel import PanelActionError, PanelState
from panda_brain.sandview import _marks_payload, render_sand_page
from panda_brain.workspace import ScenarioWorkspace

SECRET = "p19-secret"


def _state(tmp: Path, secret: str = SECRET, test: unittest.TestCase | None = None) -> PanelState:
    auth = GeneralEntryAuth(secret=secret)
    if test is not None:
        # 事件 start 会把任务空间落地到 wb.SNAPSHOT_DIR；不重定向的话
        # 每次跑 p19 都会污染共享的 impl/snapshots/fire-001.db。
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        test.addCleanup(patcher.stop)
    state = PanelState(
        ScenarioWorkspace("p19-domain", general_auth=auth),
        auth=auth,
        registry_path=tmp / "reg.json",
        materials_path=tmp / "mat.json",
        imagery_path=tmp / "img.json",
        reviews_path=tmp / "rev.json",
    )
    state.login(secret)
    state.handle_create_event(
        name="P19 测试火情", report_no="测试-P19-01",
        discovered_at=_iso(120), source="测试", location="测试北坡",
        lat="33.21", lon="107.55",
    )
    event_id = sorted(state.registry.events)[-1]
    state.active_event_id = event_id
    state.handle_event_action(event_id, "start", duty_token=duty_token(state))
    return state


def _iso(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).astimezone().isoformat(
        timespec="seconds")


def _ago(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")


def _ring(lat: float, lon: float, n: int = 10, r: float = 0.012):
    return tm.irregular_ring(lat, lon, n, r)


def _mark(ws, *, ann_id, kind, title, geo, status=AnnotationStatus.DRAFT,
          details=None, unit="扑救一分队", source=AnnotationSource.MANUAL, **kw):
    mark = MapAnnotation(
        id=ann_id, kind=kind, points=(), title=title, status=status,
        geo_points=tuple(geo), details=dict(details or {}),
        responsible_unit=unit, source=source, **kw,
    )
    ws.record_annotation(mark)
    return mark


def _seed_mark(ws, *, ann_id, kind, title, geo, details=None, unit="扑救一分队",
               status=AnnotationStatus.PUBLISHED, **kw):
    """测试夹具：直接放入共享态势（带核验/发布记录），只用于只读计算类断言。

    写入类断言一律走 handle_* 与状态机，不从这里走。
    """
    mark = MapAnnotation(
        id=ann_id, kind=kind, points=(), title=title, status=status,
        geo_points=tuple(geo), details=dict(details or {}),
        responsible_unit=unit, verified_at=_ago(10), published_at=_ago(9),
        verification_record={"by": "fixture", "at": _ago(10), "conclusion": "已核验",
                             "basis": "测试夹具"},
        publication_record={"by": "fixture", "at": _ago(9), "conclusion": "已发布"},
        **kw,
    )
    ws.annotations[ann_id] = mark
    return mark


FIRE_AREA_FIELDS = {
    "basis": "卫星热点两处 + 无人机航拍判读", "observed_at": _ago(150),
    "source_ref": "卫星热点-01", "range_status": "研判中",
}
FIRELINE_FIELDS = {
    "priority": "重点", "observed_at": _ago(30), "team": "扑救一分队",
    "source_ref": "现场报告-12",
}


class RegistryTest(unittest.TestCase):
    """§12.1 对象注册表与图层归属。"""

    def test_p0_p1_counts_are_exact(self):
        specs = list(tm.KIND_SPECS.values())
        self.assertEqual(15, len([s for s in specs if s.tier == "P0"]))
        self.assertEqual(10, len([s for s in specs if s.tier == "P1"]))
        self.assertEqual(11, len([s for s in specs if s.tier == "legacy"]))
        self.assertEqual(25, len(TACTICAL_KINDS))

    def test_every_kind_is_registered_in_enum(self):
        for spec in tm.KIND_SPECS.values():
            self.assertIn(spec.kind, {k.value for k in AnnotationKind})

    def test_six_business_layers_map_to_fixed_render_layers(self):
        self.assertEqual(6, len(tm.LAYERS))
        self.assertEqual(12, len(tm.RENDER_LAYERS))
        mapping = {s.key: s.render_layer for s in tm.LAYERS}
        self.assertEqual({"fire": 3, "control": 4, "force": 5, "support": 6,
                          "risk": 7, "evidence": 2}, mapping)
        default_on = {s.key: s.default_on for s in tm.LAYERS}
        self.assertEqual(
            {"fire": True, "control": True, "force": True, "support": True,
             "risk": False, "evidence": False}, default_on)

    def test_hazard_zone_is_p0_but_defaults_off(self):
        spec = tm.kind_spec("hazard_zone")
        self.assertEqual("P0", spec.tier)
        self.assertEqual("risk", spec.layer)
        self.assertFalse(spec.default_on)

    def test_geometry_mapping(self):
        self.assertEqual("polygon", tm.geometry_of("fire_area"))
        self.assertEqual("line", tm.geometry_of("firebreak"))
        self.assertEqual("point", tm.geometry_of("drone_position"))
        # 点或面 / 点或线：真实渲染按面/线处理，单点也合法
        self.assertEqual("polygon", tm.geometry_of("protected_target"))
        self.assertEqual("line", tm.geometry_of("wind_marker"))

    def test_symbol_sizes_and_widths(self):
        belt = tm.belt_style("firebreak", {"planned_width": "30", "belt_status": "planned"})
        self.assertGreaterEqual(belt["weight"], 8)
        self.assertLessEqual(belt["weight"], 14)
        self.assertEqual(4, tm.belt_style("main_fireline")["weight"])
        self.assertGreaterEqual(tm.belt_style("team_route")["weight"], 3)
        self.assertTrue(tm.belt_style("team_route")["arrows"])

    def test_freshness_windows_are_fixed(self):
        self.assertEqual(30, tm.kind_spec("hazard_zone").freshness_minutes)
        self.assertEqual(30, tm.kind_spec("evacuation_route").freshness_minutes)
        self.assertEqual(30, tm.kind_spec("wind_marker").freshness_minutes)
        self.assertEqual(60, tm.kind_spec("road_block").freshness_minutes)
        self.assertEqual(60, tm.kind_spec("lookout_post").freshness_minutes)
        for kind in ("comm_blind_zone", "control_line", "staging_point", "drone_pad"):
            self.assertEqual(120, tm.kind_spec(kind).freshness_minutes, kind)
        self.assertIsNone(tm.kind_spec("reserve_point").freshness_minutes)
        self.assertIsNone(tm.kind_spec("burned_area").freshness_minutes)

    def test_unknown_kind_fails_closed(self):
        with self.assertRaises(tm.TacticalError):
            tm.kind_spec("tank_battalion")


class GeometryGateTest(unittest.TestCase):
    """§12.2 几何与字段闸门。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = ScenarioWorkspace("p19-geo")

    def test_polygon_needs_three_distinct_vertices(self):
        with self.assertRaises(tm.TacticalError):
            tm.validate_geometry("hazard_zone", ((33.2, 107.5), (33.21, 107.51)))

    def test_self_intersecting_polygon_rejected(self):
        bow = ((33.20, 107.50), (33.22, 107.54), (33.22, 107.50), (33.20, 107.54))
        with self.assertRaises(tm.TacticalError):
            tm.validate_geometry("hazard_zone", bow)

    def test_collinear_zero_area_rejected(self):
        line = ((33.20, 107.50), (33.21, 107.51), (33.22, 107.52))
        with self.assertRaises(tm.TacticalError):
            tm.validate_geometry("hazard_zone", line)

    def test_closed_ring_is_accepted_without_duplicate_vertex(self):
        ring = list(_ring(33.21, 107.55, 9, 0.01))
        ring.append(ring[0])
        cleaned = tm.validate_geometry("fire_area", ring)
        self.assertEqual(9, len(cleaned))

    def test_out_of_range_coordinates_rejected(self):
        with self.assertRaises(tm.TacticalError):
            tm.validate_geometry("hotspot", ((91.0, 107.5),))

    def test_fire_area_must_be_irregular(self):
        circle = tuple(
            (round(33.21 + 0.01 * math.sin(2 * math.pi * i / 10), 5),
             round(107.55 + 0.01 * math.cos(2 * math.pi * i / 10) / math.cos(math.radians(33.21)), 5))
            for i in range(10)
        )
        self.assertFalse(tm.irregular_ok(circle))
        ellipse = tuple(
            (round(33.21 + 0.014 * math.sin(2 * math.pi * i / 10), 5),
             round(107.55 + 0.006 * math.cos(2 * math.pi * i / 10)
                   / math.cos(math.radians(33.21)), 5))
            for i in range(10)
        )
        self.assertFalse(tm.irregular_ok(ellipse))
        self.assertTrue(tm.irregular_ok(_ring(33.21, 107.55)))

    def test_point_kind_keeps_single_vertex(self):
        mark = _mark(self.ws, ann_id="ann-001", kind=AnnotationKind.HOTSPOT,
                     title="南坡热点", geo=((33.214, 107.556),),
                     details=dict(FIRE_AREA_FIELDS))
        self.assertEqual(1, len(mark.geo_points))


class FieldGateTest(unittest.TestCase):
    """§12.2 / §12.6 字段、来源、权限与真实性。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace

    def _draft(self, kind, title, geo, details, **kw):
        return self.state.handle_sand_annotate(
            kind=kind, title=title, points="", geo_text=_geo_text(geo),
            details=details, **kw)

    def test_fire_area_without_basis_cannot_submit_verification(self):
        aid = self._draft("fire_area", "初始研判火场范围", _ring(33.224, 107.55),
                          {"observed_at": _ago(150), "source_ref": "卫星热点-01"})
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "pending_review")
        self.assertIn("研判依据", str(ctx.exception))
        self.assertEqual(AnnotationStatus.DRAFT, self.ws.annotations[aid].status)

    def test_note_cannot_replace_required_field(self):
        aid = self._draft("fire_area", "初始研判火场范围", _ring(33.224, 107.55),
                          {"basis": "见备注", "observed_at": _ago(150),
                           "source_ref": "卫星热点-01"},
                          note="依据都写在备注里")
        with self.assertRaises(PanelActionError):
            self.state.handle_sand_annotation_status(aid, "pending_review")

    def test_fire_area_needs_eight_vertices_to_verify(self):
        aid = self._draft("fire_area", "规则圆火场", _ring(33.224, 107.55, 5, 0.01),
                          dict(FIRE_AREA_FIELDS))
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "pending_review")
        self.assertIn("顶点", str(ctx.exception))

    def test_firebreak_width_and_publication_rules(self):
        # 实际宽度高于计划宽度：提交核验即拒（不等到发布）
        bad = self._draft("firebreak", "宽度异常隔火带",
                          ((33.23, 107.57), (33.22, 107.58), (33.21, 107.59)),
                          {"planned_width": "30", "actual_width": "45",
                           "belt_status": "opened", "team": "扑救一分队",
                           "belt_form": "开放带", "source_ref": "现场报告-11"})
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(bad, "pending_review")
        self.assertIn("实际宽度高于计划宽度", str(ctx.exception))
        self.assertEqual(AnnotationStatus.DRAFT, self.ws.annotations[bad].status)
        # 已开设但没有实际宽度：可以核验，不能发布
        aid = self._draft("firebreak", "已开设但缺宽度",
                          ((33.24, 107.60), (33.23, 107.61)),
                          {"planned_width": "30", "belt_status": "opened",
                           "team": "扑救二分队", "belt_form": "开放带",
                           "source_ref": "现场报告-12"})
        for step in ("pending_review", "verified"):
            self.state.handle_sand_annotation_status(aid, step)
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "published")
        self.assertIn("缺少实际宽度", str(ctx.exception))

    def test_team_route_without_team_cannot_be_formal(self):
        aid = self._draft("team_route", "一分队路线",
                          ((33.214, 107.556), (33.22, 107.566)),
                          {"route_type": "plan", "purpose": "开进", "start": "前指",
                           "end": "东线", "plan_time": "今日 16:00 前",
                           "source_ref": "现场报告-1"})
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "pending_review")
        self.assertIn("小队编号", str(ctx.exception))

    def test_actual_track_without_source_is_rejected(self):
        """无真实来源的实际轨迹：一律不得登记（提交核验即拦）。"""
        aid = self._draft("team_route", "一分队实际轨迹",
                          ((33.214, 107.556), (33.22, 107.566)),
                          {"team_id": "扑救一分队", "route_type": "actual",
                           "purpose": "开进", "start": "前指", "end": "东线"})
        created = 0
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "pending_review")
            created += 1
        self.assertEqual(0, created)
        self.assertIn("没有真实来源", str(ctx.exception))
        self.assertEqual(AnnotationStatus.DRAFT, self.ws.annotations[aid].status)

    def test_unregistered_imagery_cannot_create_formal_object(self):
        aid = self._draft("drone_coverage", "无人机一号粗略影像覆盖",
                          _ring(33.226, 107.57, 4, 0.006),
                          {"image_id": "IMG-1", "captured_at": _ago(15),
                           "positioning": "rough", "transform": "平移 +120 米",
                           "unit": "飞行组", "source_ref": "无人机影像-4"})
        self.state.handle_sand_annotation_status(aid, "pending_review")
        self.state.handle_sand_annotation_status(aid, "verified")
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "published")
        self.assertIn("未配准", str(ctx.exception))

    def test_numbers_without_source_are_not_allowed(self):
        aid = self._draft("supply_point", "南线物资前置点", ((33.202, 107.562),),
                          {"category": "水带", "unit": "县防火办",
                           "availability": "可用", "stock": "200",
                           "source_ref": ""})
        self.state.handle_sand_annotation_status(aid, "pending_review")
        self.state.handle_sand_annotation_status(aid, "verified")
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "published")
        self.assertIn("没有来源", str(ctx.exception))

    def test_missing_unit_blocks_publication(self):
        aid = self._draft("command_post", "前线指挥部", ((33.214, 107.556),),
                          {"level": "前线级", "leader": "指挥长",
                           "comm_status": "畅通", "active_since": _ago(300),
                           "source_ref": "指挥部登记-01"},
                          responsible_unit="")
        self.state.handle_sand_annotation_status(aid, "pending_review")
        self.state.handle_sand_annotation_status(aid, "verified")
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(aid, "published")
        self.assertIn("责任单位", str(ctx.exception))

    def test_title_and_note_length_limits(self):
        with self.assertRaises(PanelActionError):
            self._draft("hotspot", "过" * 41, ((33.21, 107.55),),
                        {"found_at": _ago(10), "hot_status": "confirmed",
                         "source_ref": "现场报告-1"})

    def test_publish_requires_the_general_entry(self):
        """无指挥入口凭据时发布必须失败（权限 fail-closed）。"""
        auth = GeneralEntryAuth(secret="p19-other")
        ws = ScenarioWorkspace("p19-noperm", general_auth=auth)   # 未登录＝未解锁
        _seed_mark(
            ws, ann_id="ann-099", kind=AnnotationKind.HOTSPOT, title="南坡热点",
            geo=((33.221, 107.566),), status=AnnotationStatus.VERIFIED,
            details={"found_at": _ago(10), "hot_status": "confirmed",
                     "source_ref": "无人机影像-3", "unit": "扑救一分队"},
        )
        with self.assertRaises(PermissionError):
            ws.set_annotation_status("ann-099", AnnotationStatus.PUBLISHED)
        self.assertEqual(AnnotationStatus.VERIFIED, ws.annotations["ann-099"].status)

    def test_other_kind_fields_are_rejected(self):
        """表单残留不得污染对象：其他类型的专属字段一律拒收。"""
        with self.assertRaises(PanelActionError) as ctx:
            self._draft("fire_area", "干净火场范围", _ring(33.224, 107.55),
                        dict(FIRE_AREA_FIELDS, belt_status="planned",
                             positioning="located", block_level="full"))
        self.assertIn("不适用字段", str(ctx.exception))
        self.assertEqual(0, len([m for m in self.ws.annotations.values()
                                 if m.title == "干净火场范围"]))

    def test_legacy_kind_keeps_type_and_gains_no_semantics(self):
        aid = self._draft("area", "旧式区域标绘", ((33.21, 107.55), (33.22, 107.56),
                                                   (33.215, 107.57)), {})
        mark = self.ws.annotations[aid]
        self.assertEqual(AnnotationKind.AREA, mark.kind)
        self.assertFalse(mark.tactical)
        self.state.handle_sand_annotation_status(aid, "pending_review")
        self.assertEqual(AnnotationStatus.PENDING_REVIEW, self.ws.annotations[aid].status)


def _geo_text(pts) -> str:
    return ";".join(f"{a:.5f},{b:.5f}" for a, b in pts)


class StatusLayerTest(unittest.TestCase):
    """§12.1 状态分层：标绘状态与路线/冲突/时效状态互不覆盖。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace

    def test_illegal_transitions_are_rejected(self):
        aid = self.state.handle_sand_annotate(
            kind="hotspot", title="北线烟点", points="",
            geo_text=_geo_text(((33.22, 107.56),)),
            details={"found_at": _ago(10), "hot_status": "suspected",
                     "source_ref": "现场报告-2"})
        with self.assertRaises(PanelActionError):
            self.state.handle_sand_annotation_status(aid, "published")
        self.assertEqual(AnnotationStatus.DRAFT, self.ws.annotations[aid].status)

    def test_route_and_conflict_status_are_separate_fields(self):
        mark = MapAnnotation(
            id="ann-050", kind=AnnotationKind.EVACUATION_ROUTE, points=(),
            title="北坡撤离路线", status=AnnotationStatus.DRAFT,
            geo_points=((33.22, 107.55), (33.23, 107.56)),
            route_status=tm.RouteStatus.CONFIRMED.value,
            conflict_level=tm.ConflictLevel.REVIEW.value,
            freshness_status=tm.FreshnessStatus.NEEDS_REVIEW.value,
        )
        self.ws.record_annotation(mark)
        stored = self.ws.annotations["ann-050"]
        self.assertEqual(AnnotationStatus.DRAFT, stored.status)
        self.assertEqual("confirmed", stored.route_status)
        self.assertEqual("review", stored.conflict_level)
        self.assertEqual("needs_review", stored.freshness_status)

    def test_unknown_controlled_value_is_rejected(self):
        with self.assertRaises(Exception):
            MapAnnotation(id="x", kind=AnnotationKind.EVACUATION_ROUTE, points=(),
                          title="坏路线", geo_points=((1.0, 1.0), (2.0, 2.0)),
                          route_status="teleported")

    def test_published_edit_requires_revision(self):
        aid = self.state.handle_sand_annotate(
            kind="hotspot", title="南坡热点", points="",
            geo_text=_geo_text(((33.221, 107.566),)),
            details={"found_at": _ago(10), "hot_status": "confirmed",
                     "source_ref": "无人机影像-3", "unit": "扑救一分队"},
            responsible_unit="扑救一分队")
        for step in ("pending_review", "verified", "published"):
            self.state.handle_sand_annotation_status(aid, step)
        with self.assertRaises(PanelActionError):
            self.state.handle_sand_annotation_edit(aid, note="改一个字")
        new_id = self.state.handle_sand_annotation_edit(
            aid, title="南坡热点（修订）", as_revision=True,
            details={"found_at": _ago(2)})
        self.assertNotEqual(aid, new_id)
        self.assertEqual(AnnotationStatus.DRAFT, self.ws.annotations[new_id].status)
        self.assertEqual(aid, self.ws.annotations[new_id].supersedes_id)
        self.assertEqual(AnnotationStatus.PUBLISHED, self.ws.annotations[aid].status)

    def test_stale_base_version_is_rejected(self):
        aid = self.state.handle_sand_annotate(
            kind="hotspot", title="北线烟点", points="",
            geo_text=_geo_text(((33.23, 107.57),)),
            details={"found_at": _ago(10), "hot_status": "suspected",
                     "source_ref": "现场报告-2"})
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_edit(
                aid, base_version="99", details={"found_at": _ago(1)})
        self.assertIn("版本", str(ctx.exception))

    def test_freshness_marks_needs_review_without_deleting(self):
        mark = _seed_mark(
            self.ws, ann_id="ann-060", kind=AnnotationKind.ROAD_BLOCK,
            title="东岭路段阻断点", geo=((33.22, 107.56),),
            details={"road": "东岭道", "block_level": "partial",
                     "found_at": _ago(300), "source_ref": "现场报告-9",
                     "checked_at": _ago(300)},
        )
        self.assertEqual(tm.FreshnessStatus.EXPIRED, tm.freshness_of(mark, now=_ago(0)))
        self.assertEqual(AnnotationStatus.PUBLISHED, self.ws.annotations["ann-060"].status)


class ConflictTest(unittest.TestCase):
    """§12.3 路线冲突与四级决断。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace

    def _evac(self, ann_id, pts, *, route_type="primary", audience="东岭村",
              status=AnnotationStatus.DRAFT):
        return _mark(
            self.ws, ann_id=ann_id, kind=AnnotationKind.EVACUATION_ROUTE,
            title=f"{audience}撤离路线", geo=pts, status=status,
            details={"start": "村口", "end": "集结点", "audience": audience,
                     "unit": "县消防队", "checked_at": _ago(5),
                     "source_ref": "现场报告-5", "route_type": route_type,
                     "endpoint_basis": "集结点已确认", "order_ref": "order-1"},
        )

    def _hazard(self, ann_id, center, *, status=AnnotationStatus.PUBLISHED):
        return _seed_mark(
            self.ws, ann_id=ann_id, kind=AnnotationKind.HAZARD_ZONE,
            title="东北侧危险区", geo=_ring(center[0], center[1], 8, 0.008),
            status=status,
            details={"risk_type": "飞火", "risk_level": "high",
                     "basis": "坡向与风场", "formed_at": _ago(20),
                     "affected": "东岭村", "source_ref": "火势研判-2"},
        )

    def test_route_crossing_published_hazard_is_blocked(self):
        self._hazard("ann-101", (33.220, 107.560))
        self._evac("ann-100", ((33.218, 107.556), (33.222, 107.564), (33.226, 107.570)))
        found = self.ws.tactical_conflicts(now=_ago(0))
        self.assertTrue(found)
        self.assertIn(tm.ConflictLevel.BLOCKED.value, {c.level for c in found})

    def test_blocked_route_cannot_be_published(self):
        self._hazard("ann-111", (33.220, 107.560))
        route = self._evac("ann-110", ((33.218, 107.556), (33.222, 107.564),
                                       (33.226, 107.570)))
        self.state.handle_sand_annotation_status(route.id, "pending_review")
        self.state.handle_sand_annotation_status(route.id, "verified")
        with self.assertRaises(PanelActionError) as ctx:
            self.state.handle_sand_annotation_status(route.id, "published")
        self.assertIn("不得发布", str(ctx.exception))
        self.assertEqual(AnnotationStatus.VERIFIED, self.ws.annotations[route.id].status)

    def test_pending_hazard_within_50m_is_review(self):
        self._hazard("ann-121", (33.220, 107.560), status=AnnotationStatus.PENDING_REVIEW)
        ring = _ring(33.220, 107.560, 8, 0.008)
        self._evac("ann-120", (ring[0], ring[1]))
        levels = {c.level for c in self.ws.tactical_conflicts(now=_ago(0))}
        self.assertIn(tm.ConflictLevel.REVIEW.value, levels)

    def test_both_routes_blocked_escalates_to_decision(self):
        self._hazard("ann-131", (33.220, 107.560))
        self._evac("ann-130", ((33.218, 107.556), (33.222, 107.564), (33.226, 107.570)))
        self._evac("ann-132", ((33.216, 107.558), (33.221, 107.563), (33.225, 107.569)),
                   route_type="backup")
        levels = {c.level for c in self.ws.tactical_conflicts(now=_ago(0))}
        self.assertIn(tm.ConflictLevel.DECISION.value, levels)

    def test_same_key_does_not_duplicate_open_records(self):
        self._hazard("ann-141", (33.220, 107.560))
        self._evac("ann-140", ((33.218, 107.556), (33.222, 107.564), (33.226, 107.570)))
        first = self.ws.refresh_conflict_records(now=_ago(0))
        second = self.ws.refresh_conflict_records(now=_ago(0))
        self.assertGreater(len(first), 0)
        self.assertEqual((), second)
        self.assertEqual(len(first), len(self.ws.conflict_records))

    def test_conflict_is_not_cleared_by_viewing_or_hiding_layers(self):
        self._hazard("ann-151", (33.220, 107.560))
        self._evac("ann-150", ((33.218, 107.556), (33.222, 107.564), (33.226, 107.570)))
        self.ws.refresh_conflict_records(now=_ago(0))
        still_open = [r for r in self.ws.conflict_records.values()
                      if r.status == tm.ConflictStatus.OPEN.value]
        self.assertTrue(still_open)
        with self.assertRaises(ValueError):
            self.ws.resolve_conflict(still_open[0].conflict_id, by="x",
                                     resolution="已查看", reason="")
        closed = self.ws.resolve_conflict(still_open[0].conflict_id, by="local-general-entry",
                                          resolution="路线已修订", reason="改走南侧山脊路")
        self.assertEqual(tm.ConflictStatus.RESOLVED.value, closed.status)
        # 关闭后原记录仍可追溯
        self.assertIn(still_open[0].conflict_id, self.ws.conflict_records)

    def test_staging_point_inside_published_hazard_is_flagged(self):
        self._hazard("ann-161", (33.220, 107.560))
        _mark(self.ws, ann_id="ann-160", kind=AnnotationKind.STAGING_POINT,
              title="前沿集结点", geo=((33.2200, 107.5600),),
              details={"purpose": "前进集结", "unit": "扑救一分队",
                       "open_status": "启用", "safety_status": "safe",
                       "safety_confirm_at": _ago(5), "source_ref": "现场报告-7"})
        relations = {c.conflict_type for c in self.ws.tactical_conflicts(now=_ago(0))}
        self.assertIn("staging_in_hazard", relations)


class CommandFlowTest(unittest.TestCase):
    """§12.4 命令、接收、回执、异常与关闭。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace
        self.order_id = self._make_order()

    def _make_order(self) -> str:
        from panda_brain.models import (
            AdvisorRole, DecisionActor, DecisionType, GeneralDecision, Option,
        )
        ws = self.ws
        opt = Option(id="opt-p19", title="封堵北段缺口", summary="封堵 W-03 缺口",
                     evidence_ids=(), claim_subjects=("风力风向",),
                     status="active", preconditions=())
        ws.options[opt.id] = opt
        decision = GeneralDecision(
            id="decision-p19", decision_type=DecisionType.ADOPT,
            actor_role=DecisionActor.COMMANDER, option_id=opt.id,
            reason="先封下风侧", scope="北段", source_version=ws.version,
            decided_by="local-general-entry",
        )
        ws.decisions[decision.id] = decision
        order = ws.draft_order(decision.id, drafted_by="DispatchChiefAgent",
                               drafter_role=AdvisorRole.CHIEF_ADVISOR)
        ws.submit_order_for_confirmation(order.id)
        ws.confirm_order(order.id, actor_role=DecisionActor.COMMANDER,
                         confirmed_by="local-general-entry")
        return order.id

    def _open_flow(self, task_level="high_risk") -> tm.CommandFlow:
        flow = self.ws.open_command_flow(
            self.order_id, event_id=self.state.active_event_id,
            task_level=task_level, target="封堵北段缺口", unit="扑救一分队",
            route_id="", start_time=_ago(30), deadline=_ago(-60),
            safety_cond="风速大于 15 米/时停止", stop_cond="火线越过北岭线即撤",
            receipt_need="执行中每 10 分钟",
        )
        self.ws.advance_command_flow(self.order_id, "pending_validation", actor="参谋部")
        self.ws.advance_command_flow(self.order_id, "pending_approval", actor="参谋部")
        self.ws.advance_command_flow(self.order_id, "pending_confirm", actor="参谋部")
        return flow

    def test_stage_machine_has_ten_states_in_order(self):
        stages = [s.value for s in tm.CommandStage]
        self.assertEqual(10, len(stages))
        self.assertEqual("draft", stages[0])
        self.assertEqual("closed", stages[-1])

    def test_cannot_skip_ack_and_conditions(self):
        self._open_flow()
        with self.assertRaises(tm.TacticalError):
            self.ws.begin_command_execution(self.order_id, by="张三")
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        with self.assertRaises(tm.TacticalError):
            self.ws.begin_command_execution(self.order_id, by="张三")
        self.ws.command_delivery(self.order_id, delivered_at=_ago(20))
        self.ws.command_ack(self.order_id, by="张三", result="accepted_ready", at=_ago(19))
        with self.assertRaises(tm.TacticalError):
            self.ws.begin_command_execution(self.order_id, by="张三")
        self.ws.confirm_execution_conditions(self.order_id, by="张三", note="风 3 级、通信畅通")
        flow = self.ws.begin_command_execution(self.order_id, by="张三")
        self.assertEqual(tm.CommandStage.RUNNING.value, flow.stage)

    def test_delivery_alone_is_not_ack(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        flow = self.ws.command_delivery(self.order_id, delivered_at=_ago(5))
        self.assertEqual(tm.CommandStage.CONFIRMED_UNACK.value, flow.stage)
        self.assertEqual("", flow.acked_at)
        with self.assertRaises(tm.TacticalError):
            self.ws.command_delivery(self.order_id)

    def test_ack_without_actor_is_rejected(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        with self.assertRaises(tm.TacticalError):
            self.ws.command_ack(self.order_id, by="", result="accepted_ready")

    def test_timeouts_only_produce_notices(self):
        self._open_flow(task_level="urgent")
        flow = self.ws.command_flow(self.order_id)
        self.assertEqual((), flow.timeouts(_ago(0)))     # 未发送 → 不计时
        overdue = tm.CommandFlow(order_id="o", task_level="urgent", sent_at=_ago(0))
        self.assertIn("首次提醒", overdue.timeouts(_ago(-3))[0])
        self.assertIn("升级责任单位", overdue.timeouts(_ago(-6))[0])
        self.assertIn("重新组织判断", overdue.timeouts(_ago(-9))[0])
        # 超时不自动判定拒收/失联，也不改主状态
        self.assertEqual(tm.CommandStage.DRAFT.value, overdue.stage)
        self.assertEqual(0, len(overdue.receipts))

    def test_resend_keeps_history_and_rejects_changed_content(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        flow = self.ws.resend_command(self.order_id, by="指挥员")
        self.assertEqual(1, flow.resend_count)
        self.assertEqual(2, flow.version)
        with self.assertRaises(tm.TacticalError):
            self.ws.resend_command(self.order_id, by="指挥员", changed=True)

    def test_revise_creates_new_version_and_resets_ack(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        self.ws.command_delivery(self.order_id, delivered_at=_ago(20))
        self.ws.command_ack(self.order_id, by="张三", result="accepted_ready", at=_ago(19))
        flow = self.ws.revise_command(self.order_id, by="指挥员", note="执行单位改为二分队",
                                      unit="扑救二分队")
        self.assertEqual(2, flow.version)
        self.assertEqual(tm.CommandStage.PENDING_CONFIRM.value, flow.stage)
        self.assertEqual("", flow.acked_at)
        with self.assertRaises(tm.TacticalError):
            self.ws.revise_command(self.order_id, by="指挥员", note="")

    def test_pause_requires_reason_and_reassessment(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        self.ws.command_delivery(self.order_id, delivered_at=_ago(20))
        self.ws.command_ack(self.order_id, by="张三", result="accepted_ready", at=_ago(19))
        self.ws.confirm_execution_conditions(self.order_id, by="张三", note="条件已核")
        self.ws.begin_command_execution(self.order_id, by="张三")
        with self.assertRaises(tm.TacticalError):
            self.ws.pause_command(self.order_id, reason="", reassess_at=_ago(-30), by="张三")
        with self.assertRaises(tm.TacticalError):
            self.ws.pause_command(self.order_id, reason="等待水带", reassess_at="", by="张三")
        flow = self.ws.pause_command(self.order_id, reason="等待水带补充",
                                     reassess_at=_ago(-30), by="张三")
        self.assertEqual(tm.CommandControlState.PAUSED.value, flow.control_state)

    def test_withdraw_needs_execution_ack(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        self.ws.command_delivery(self.order_id, delivered_at=_ago(20))
        self.ws.command_ack(self.order_id, by="张三", result="accepted_ready", at=_ago(19))
        self.ws.confirm_execution_conditions(self.order_id, by="张三", note="条件已核")
        self.ws.begin_command_execution(self.order_id, by="张三")
        self.ws.request_command_withdraw(self.order_id, reason="火线转向，先撤人", by="指挥员")
        with self.assertRaises(tm.TacticalError):
            self.ws.command_withdraw_ack(self.order_id, kind="stage_done", by="张三",
                                         source="现场报告-9")
        flow = self.ws.command_withdraw_ack(self.order_id, kind="cannot_stop",
                                            by="张三", source="现场报告-9")
        self.assertEqual(tm.CommandControlState.EXEC_ABNORMAL.value, flow.control_state)

    def test_receipt_is_idempotent_and_cannot_fabricate_position(self):
        self._run_to_executing()
        first = self.ws.record_command_receipt(
            self.order_id, kind="running", by="张三", at=_ago(5),
            where="北段", content="开通 200 米", source="现场报告-12",
            request_id="r-1")
        again = self.ws.record_command_receipt(
            self.order_id, kind="running", by="张三", at=_ago(4),
            where="北段", content="开通 200 米", source="现场报告-12",
            request_id="r-1")
        self.assertEqual(len(first.receipts), len(again.receipts))
        self.assertEqual(1, len([r for r in again.receipts if r.get("request_id") == "r-1"]))
        with self.assertRaises(tm.TacticalError):
            self.ws.record_command_receipt(self.order_id, kind="running", by="张三",
                                           where="北段", source="")

    def test_close_requires_completion_evidence(self):
        self._run_to_executing()
        with self.assertRaises(tm.TacticalError):
            self.ws.close_command(self.order_id, result="completed", by="指挥员")
        self.ws.record_command_receipt(self.order_id, kind="all_done", by="张三",
                                       at=_ago(1), where="北段", content="缺口已封堵",
                                       source="现场报告-13", request_id="r-2")
        flow = self.ws.close_command(self.order_id, result="completed", by="指挥员")
        self.assertEqual(tm.CommandStage.CLOSED.value, flow.stage)
        self.assertEqual("已完成", tm.close_results()[flow.close_result])

    def test_abnormal_close_needs_three_parts(self):
        self._run_to_executing()
        self.ws.record_command_receipt(self.order_id, kind="partial_done", by="张三",
                                       at=_ago(1), content="完成一半",
                                       source="现场报告-14", request_id="r-3")
        with self.assertRaises(tm.TacticalError):
            self.ws.close_command(self.order_id, result="abnormal_close", by="指挥员")
        with self.assertRaises(tm.TacticalError):
            self.ws.close_command(self.order_id, result="abnormal_closed", by="指挥员",
                                  facts="只剩事实")
        flow = self.ws.close_command(
            self.order_id, result="abnormal_closed", by="指挥员",
            facts="夜间风增", handling="已撤至安全线", outcome="剩余 120 米未完成")
        self.assertEqual(tm.CommandControlState.ABNORMAL_CLOSED.value, flow.control_state)
        self.assertNotIn("已完成", flow.stage_word)

    def _run_to_executing(self):
        self._open_flow()
        self.ws.confirm_command_flow(self.order_id, actor="local-general-entry")
        self.ws.command_delivery(self.order_id, delivered_at=_ago(20))
        self.ws.command_ack(self.order_id, by="张三", result="accepted_ready", at=_ago(19))
        self.ws.confirm_execution_conditions(self.order_id, by="张三", note="条件已核")
        self.ws.begin_command_execution(self.order_id, by="张三")


class PageTest(unittest.TestCase):
    """§12.5 / §12.6 页面、图层、公共屏与前台中文。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace
        self._seed()

    def _seed(self):
        self.state.handle_sand_annotate(
            kind="fire_area", title="初始研判火场范围", points="",
            geo_text=_geo_text(_ring(33.224, 107.55, 10, 0.012)),
            details=dict(FIRE_AREA_FIELDS), responsible_unit="县防火办",
            is_demo=True, demo_notice="演示数据·非现场实时数据",
        )
        self.state.handle_sand_annotate(
            kind="main_fireline", title="东线重点火线", points="",
            geo_text=_geo_text(((33.215, 107.590), (33.224, 107.604), (33.235, 107.620))),
            details=dict(FIRELINE_FIELDS), responsible_unit="扑救一分队",
            is_demo=True,
        )
        first = sorted(self.ws.annotations)[0]
        second = sorted(self.ws.annotations)[1]
        for aid in (first, second):
            for step in ("pending_review", "verified", "published"):
                self.state.handle_sand_annotation_status(aid, step)
        self.state.handle_sand_annotate(
            kind="firebreak", title="东侧计划隔火带", points="",
            geo_text=_geo_text(((33.247, 107.637), (33.238, 107.652))),
            details={"planned_width": "30", "belt_status": "planned",
                     "team": "扑救一分队", "source_ref": "人工标绘-01"},
        )

    def _payload(self, role="commander"):
        html = render_sand_page(self.state, role=role)
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S).group(1)
        return html, json.loads(raw.replace("<\\/", "</"))

    def test_payload_carries_layers_geometry_and_tactical_fields(self):
        _, data = self._payload()
        self.assertEqual(6, len(data["layers"]))
        self.assertEqual({"fire", "control", "force", "support", "risk", "evidence"},
                         {l["key"] for l in data["layers"]})
        self.assertIn("fire_area", data["geom"])
        mark = next(m for m in data["marks"] if m["kind"] == "fire_area")
        self.assertEqual("fire", mark["layer"])
        self.assertEqual("火情态势", mark["layer_word"])
        self.assertEqual("已发布", mark["status_word"])
        self.assertTrue(any(f[0] == "来源编号" for f in mark["facts"]))
        self.assertTrue(any(f[0] == "研判依据" for f in mark["judges"]))
        self.assertTrue(any(f[0] == "责任单位" for f in mark["actions"]))
        self.assertIn("演示", mark["demo"])

    def test_labels_are_capped_at_twelve(self):
        _, data = self._payload()
        self.assertLessEqual(sum(1 for m in data["marks"] if m["tip"]), 12)

    def test_toolbar_is_grouped_and_single_tool_active(self):
        html, _ = self._payload()
        for label, _ in tm.TOOLBAR_GROUPS:
            self.assertIn(f'data-toolgroup="{label}"', html)
        self.assertIn('data-draw-kind="firebreak"', html)
        self.assertIn('data-draw-kind="drone_position"', html)
        self.assertIn("保存草稿", html)
        self.assertIn("提交核验", html)

    def test_per_kind_fieldsets_exist_for_every_tactical_kind(self):
        html, _ = self._payload()
        for _, kinds in tm.TOOLBAR_GROUPS:
            for kind in kinds:
                self.assertIn(f'data-fields-for="{kind}"', html, kind)

    def test_no_english_enum_or_backend_words_in_front(self):
        html, _ = self._payload()
        visible = re.sub(r'<script.*?</script>', "", html, flags=re.S)
        visible = re.sub(r'<style.*?</style>', "", visible, flags=re.S)
        visible = re.sub(r'<[^>]+>', " ", visible)
        for word in ("fire_area", "pending_review", "published", "draft", "polygon",
                     "Adopted", "Recommend", "LLM", "Agent", "model", "geojson",
                     "blocked", "needs_review"):
            self.assertNotIn(word, visible, word)

    def _visible_text(self, html: str) -> str:
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
        return re.sub(r"<[^>]+>", " ", text)

    def test_map_first_layout_is_pinned(self):
        """地图主区必须真占主画面：工具组默认折叠、地图行贴满首屏、页壳为沙盘拓宽。

        这三条 CSS 丢一条就会把地图压回半屏（2026-08-28 浏览器实测教训）。
        """
        from panda_brain import panel as _pn

        html, _ = self._payload()
        self.assertIn(".cmd-groupbtns[hidden]{display:none}", html)
        self.assertIn("height:calc(100dvh - 226px)", html)
        shell = _pn._light_wrap(title="壳", body=html, nav="", user="")
        self.assertIn(".lz-wrap:has(.cmd-page){padding-bottom:12px}", shell)
        self.assertIn(".lz-wrap:has(.sand-page),.lz-wrap:has(.cmd-page){max-width:1680px}", shell)

    def test_public_screen_sees_only_published_and_no_internal_state(self):
        html, data = self._payload(role="public")
        states = {m["state"] for m in data["marks"]}
        self.assertTrue(states.issubset({"published"}), states)
        self.assertEqual([], data["sims"])
        self.assertEqual([], data["conflicts"])
        visible = self._visible_text(html)
        self.assertNotIn('id="cmd-anno-float"', html)
        self.assertNotIn('id="cmd-conflict-note"', html)
        self.assertNotIn("保存草稿", visible)
        self.assertNotIn("提交核验", visible)
        self.assertNotIn("路线冲突", visible)
        self.assertIn("公共屏", visible)

    def test_member_cannot_see_tactical_tools(self):
        html, data = self._payload(role="member")
        visible = self._visible_text(html)
        self.assertNotIn("保存草稿", visible)
        self.assertNotIn("专属字段", visible)
        self.assertNotIn('action="/sand/annotate"', html)

    def test_gap_child_record_shows_with_parent(self):
        aid = self.state.handle_sand_annotate(
            kind="firebreak", title="北侧已开设隔火带", points="",
            geo_text=_geo_text(((33.252, 107.514), (33.256, 107.526), (33.252, 107.538))),
            details={"planned_width": "30", "actual_width": "28", "belt_status": "opened",
                     "belt_form": "开放带", "team": "扑救二分队",
                     "source_ref": "现场报告-11", "gap_ref": "W-03",
                     "gap_status": "存在缺口（待核验）", "gap_segment": "2 号至 3 号节点之间",
                     "gap_width_m": "120", "gap_reported_at": _ago(20),
                     "gap_source": "无人机影像-4"},
            responsible_unit="扑救二分队")
        for step in ("pending_review", "verified"):
            self.state.handle_sand_annotation_status(aid, step)
        marks = _marks_payload(self.state, "commander")
        parent = next(m for m in marks if m["id"] == aid)
        self.assertEqual(1, len(parent["gaps"]))
        self.assertEqual("W-03", parent["gaps"][0][0][1])
        # 子记录不单独计作地图对象
        self.assertEqual(4, len(marks))

    def test_conflict_note_and_badge_are_business_words(self):
        _seed_mark(self.ws, ann_id="ann-200", kind=AnnotationKind.HAZARD_ZONE,
                   title="东北侧危险区", geo=_ring(33.220, 107.560, 8, 0.008),
                   details={"risk_type": "飞火", "risk_level": "high", "basis": "坡向",
                            "formed_at": _ago(20), "affected": "东岭村",
                            "source_ref": "火势研判-2"})
        route = MapAnnotation(
            id="ann-201", kind=AnnotationKind.EVACUATION_ROUTE, points=(),
            title="东岭村撤离路线", status=AnnotationStatus.DRAFT,
            geo_points=((33.218, 107.556), (33.222, 107.564), (33.226, 107.570)),
            details={"start": "村口", "end": "集结点", "audience": "东岭村",
                     "unit": "县消防队", "checked_at": _ago(5),
                     "source_ref": "现场报告-5", "endpoint_basis": "集结点已确认",
                     "order_ref": "order-1"},
        )
        self.ws.record_annotation(route)
        self.ws.refresh_conflict_records(now=_ago(0))
        html, data = self._payload()
        self.assertIn("cmd-conflict-note", html)
        self.assertTrue(data["conflicts"])
        self.assertTrue(any("三级阻断" in c["level_word"] or "二级复核" in c["level_word"]
                            for c in data["conflicts"]))


class PlanOverlayTest(unittest.TestCase):
    """方案叠加的取消与互斥（2026-08-28 对齐）。

    口径：一次只看一套，换上图自动下掉前一套；推演公共层（预测火线/目标圈）
    全图只画一份；「全部下图」只拨显示开关，不删任何标注数据；
    上图是只读查看动作，现场人员也能用；公共屏无任何叠加能力。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace
        for idx, (oid, pts) in enumerate((
            ("opt-a", ((33.22, 107.55), (33.23, 107.57))),
            ("opt-b", ((33.24, 107.58), (33.25, 107.60))),
            ("opt-c", ((33.26, 107.61), (33.27, 107.63))),
        )):
            opt = Option(
                id=oid, title=f"{oid} 号方案", summary="概述", evidence_ids=(),
                claim_subjects=(), risks=(), proposer_role=AdvisorRole.OPTION,
            )
            opt.sim_shapes = ({"geom": "line", "pts": pts, "label": f"作业线{idx}"},)
            self.ws.add_option(opt)
        self.js = sandview._CMD_MAP_JS

    def _payload(self, role="commander"):
        html = render_sand_page(self.state, role=role)
        raw = re.search(
            r'<script type="application/json" id="cmd-map-data">(.*?)</script>',
            html, re.S).group(1)
        return html, json.loads(raw.replace("<\\/", "</"))

    def test_every_plan_gets_one_overlay_button(self):
        html, data = self._payload()
        for oid in ("opt-a", "opt-b", "opt-c"):
            self.assertIn(f'data-sim="{oid}"', html)
        self.assertEqual(3, len(data["sims"]))
        self.assertNotIn("查看推演", html)
        self.assertNotIn("关闭推演", html)

    def test_plan_colors_are_distinct_and_shared_with_card(self):
        _, data = self._payload()
        colors = [s["color"] for s in data["sims"]]
        self.assertEqual(3, len(set(colors)))
        for c in colors:
            self.assertIn(c, sandview.SIM_COLORS)
        self.assertIn('card.setAttribute("data-plan-color"', self.js)
        self.assertIn(".cmd-plan-card[data-plan-color]", sandview._CMD_CSS)

    def test_projection_layers_are_drawn_once_not_per_plan(self):
        """预测火线属于态势，不能每上图一套就重画一份（旧版糊图的根因）。"""
        self.assertEqual(1, self.js.count("(DATA.proj||{}).steps"))
        start = self.js.index("function buildSimLayer")
        body = self.js[start:self.js.index("  function restyle", start)]
        self.assertNotIn("DATA.proj", body)

    def test_switching_plan_drops_the_previous_one_by_default(self):
        self.assertIn(
            "if(!COMPARE){Object.keys(SIMON).forEach(function(k){if(k!==id)dropLayer(k);});}",
            self.js,
        )
        self.assertIn("function setCompare", self.js)
        self.assertIn("多方案对比", self.js)

    def test_clear_all_only_moves_display_switches(self):
        start = self.js.index("function simClearAll")
        body = self.js[start:self.js.index("function setCompare", start)]
        for sel in ('[data-layer-group="evidence"]', '[data-cmd-layer="candidate"]',
                    '[data-cmd-layer="draft"]', '[data-cmd-layer="history"]'):
            self.assertIn(sel, body)
        self.assertIn("kick(projCb,false)", body)
        # 必须走 change 事件：沙盘图层的真开关是 STATEF/LAYERON，
        # 只改 checkbox.checked 会出现「勾消了、图上还画着」的假恢复。
        sb = self.js[self.js.index("function setBox"):self.js.index("function applyDisplay")]
        self.assertIn("dispatchEvent(new Event('change',{bubbles:true}))", sb)
        # 纯前端动作：不发请求、不碰标绘与命令链。
        for banned in ("fetch(", "/sand/", "XMLHttpRequest"):
            self.assertNotIn(banned, body)
        self.assertIn('type="button" class="lz-btn lz-btn2" id="cmd-sim-clear"', self.js)

    def test_strip_stays_after_clear_all_with_restore_entry(self):
        """全部下图后叠加条不能消失：要让司令看见是谁关的图层，并给还原入口。"""
        start = self.js.index("if(!ids.length){")
        branch = self.js[start:self.js.index('el.style.display="none"', start)]
        self.assertIn("if(SNAP){", branch)
        self.assertIn("已恢复原始态势视图", branch)
        self.assertIn('id="cmd-sim-restore"', branch)
        self.assertIn("未删除任何数据", branch)

    def test_overlay_state_is_remembered_per_event(self):
        _, data = self._payload()
        self.assertEqual(self.state.active_event_id, data["event"])
        self.assertIn('var LSKEY="panda-cmd-sim:"+(DATA.event||"default");', self.js)
        self.assertIn("localStorage.setItem(LSKEY", self.js)
        self.assertIn("localStorage.getItem(LSKEY", self.js)

    def test_member_can_put_a_plan_on_the_map_without_decision_forms(self):
        html, data = self._payload(role="member")
        self.assertIn('data-sim="opt-a"', html)
        self.assertEqual(3, len(data["sims"]))
        self.assertNotIn("/sand/plan-decision", html)
        self.assertNotIn("采纳为决断基础", html)
        cmd_html, _ = self._payload(role="commander")
        self.assertIn("采纳为决断基础", cmd_html)
        self.assertIn("/sand/plan-decision", cmd_html)

    def test_public_screen_has_no_overlay_hook(self):
        html, data = self._payload(role="public")
        self.assertEqual([], data["sims"])
        self.assertNotIn("data-sim=", html)
        self.assertNotIn('id="cmd-simstrip"', html)

    def test_strip_and_hint_are_present_for_the_workbench_only(self):
        cmd_html, _ = self._payload()
        mem_html, _ = self._payload(role="member")
        pub_html, _ = self._payload(role="public")
        for html in (cmd_html, mem_html):
            self.assertIn('id="cmd-simstrip"', html)
            self.assertIn('class="cmd-plan-hint"', html)
            self.assertIn("一次只看一套", html)
            self.assertIn("预测火线（推演）", html)
        self.assertNotIn('id="cmd-simstrip"', pub_html)
        self.assertNotIn('class="cmd-plan-hint"', pub_html)
        self.assertNotIn('id="cmd-layer-proj"', pub_html)


class AuditTest(unittest.TestCase):
    """§12.7 审计与兼容。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace

    def test_every_lifecycle_act_is_on_the_hash_chain(self):
        aid = self.state.handle_sand_annotate(
            kind="hotspot", title="南坡热点", points="",
            geo_text=_geo_text(((33.221, 107.566),)),
            details={"found_at": _ago(10), "hot_status": "confirmed",
                     "source_ref": "无人机影像-3", "unit": "扑救一分队"},
            responsible_unit="扑救一分队")
        for step in ("pending_review", "verified", "published"):
            self.state.handle_sand_annotation_status(aid, step)
        kinds = [e.kind for e in self.ws.events]
        for expected in ("AnnotationRecorded", "AnnotationStatusChanged"):
            self.assertIn(expected, kinds)
        self.assertEqual((), self.ws.verify_audit_chain())
        mark = self.ws.annotations[aid]
        self.assertTrue(mark.verified_at)
        self.assertTrue(mark.published_at)
        self.assertEqual("指挥端核验动作", mark.verification_record["basis"])

    def test_failed_gate_writes_no_success_event(self):
        before = len(self.ws.events)
        aid = self.state.handle_sand_annotate(
            kind="hotspot", title="北线烟点", points="",
            geo_text=_geo_text(((33.23, 107.57),)),
            details={"found_at": _ago(1), "hot_status": "confirmed",
                     "source_ref": "现场报告-2", "unit": "扑救一分队"},
            responsible_unit="扑救一分队")
        for step in ("pending_review", "verified", "published"):
            self.state.handle_sand_annotation_status(aid, step)
        with self.assertRaises(PanelActionError):
            self.state.handle_sand_annotation_edit(aid, note="直接改已发布对象")
        self.assertNotIn("AnnotationUpdated", [e.kind for e in self.ws.events])
        self.assertEqual(AnnotationStatus.PUBLISHED, self.ws.annotations[aid].status)
        self.assertGreater(len(self.ws.events), before)

    def test_geojson_export_prefers_geo_coordinates(self):
        mark = MapAnnotation(
            id="ann-300", kind=AnnotationKind.HOTSPOT, points=(), title="南坡热点",
            geo_points=((33.221, 107.566),), details={"found_at": _ago(1),
                                                       "hot_status": "confirmed",
                                                       "source_ref": "无人机影像-3"},
        )
        text = to_geojson([mark])
        data = json.loads(text)
        self.assertEqual([107.566, 33.221], data["features"][0]["geometry"]["coordinates"])
        self.assertEqual("wgs84", data["features"][0]["properties"]["crs"])
        self.assertEqual("hotspot", data["features"][0]["properties"]["kind"])

    def test_geojson_import_stays_draft_and_unclassified(self):
        text = json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [860.0, 286.0]},
                "properties": {"kind": "fire_point", "title": "导入火点"},
            }],
        })
        marks = from_geojson(text)
        self.assertEqual(1, len(marks))
        self.assertEqual(AnnotationStatus.DRAFT, marks[0].status)
        self.assertEqual(AnnotationSource.GEOJSON_IMPORT, marks[0].source)
        self.assertFalse(marks[0].tactical)

    def test_persistence_round_trip_keeps_tactical_fields(self):
        from panda_brain.persistence import load_workspace, save_workspace

        aid = self.state.handle_sand_annotate(
            kind="firebreak", title="北侧已开设隔火带", points="",
            geo_text=_geo_text(((33.252, 107.514), (33.256, 107.526), (33.252, 107.538))),
            details={"planned_width": "30", "actual_width": "28", "belt_status": "opened",
                     "belt_form": "开放带", "team": "扑救二分队",
                     "source_ref": "现场报告-11", "gap_ref": "W-03",
                     "gap_status": "存在缺口（待核验）", "gap_segment": "2 号至 3 号节点之间",
                     "gap_width_m": "120", "gap_reported_at": _ago(20),
                     "gap_source": "无人机影像-4"},
            responsible_unit="扑救二分队", is_demo=True)
        for step in ("pending_review", "verified"):
            self.state.handle_sand_annotation_status(aid, step)
        self.ws.refresh_conflict_records(now=_ago(0))
        db = self.tmp / "p19.db"
        save_workspace(self.ws, db)
        loaded = load_workspace(db)
        mark = loaded.annotations[aid]
        self.assertEqual("opened", mark.details["belt_status"])
        self.assertEqual(AnnotationStatus.VERIFIED, mark.status)
        self.assertEqual(1, len(mark.gap_records()))
        self.assertEqual((), loaded.verify_audit_chain())


class DemoSceneTest(unittest.TestCase):
    """§12.1 演示场景数量与真实性（读已构建的 data/demo 种子）。"""

    DEMO_DB = Path(__file__).resolve().parents[1] / "data" / "demo" / "demo_hanjiang.db"

    @unittest.skipUnless(DEMO_DB.exists(), "演示场景未构建（运行 tools/build_demo_scenario.py）")
    def test_demo_scene_counts_and_honesty(self):
        from panda_brain.persistence import load_workspace

        ws = load_workspace(self.DEMO_DB)
        marks = list(ws.annotations.values())
        # 23 个战术对象（含独立实际轨迹）+ 1 条 P20 演示观测候选。
        self.assertEqual(24, len(marks))
        counts: dict[str, int] = {}
        for m in marks:
            counts[m.status.value] = counts.get(m.status.value, 0) + 1
        self.assertEqual({"published": 10, "verified": 7, "pending_review": 4,
                          "draft": 3}, counts)
        # P20 演示观测：后台候选，带演示与定位限制，绝不直接进入正式态势
        obs = [m for m in marks if m.source is AnnotationSource.AI_SUGGESTION]
        self.assertEqual(1, len(obs))
        self.assertEqual(AnnotationStatus.DRAFT, obs[0].status)
        self.assertEqual("rough", obs[0].positioning_status)
        self.assertTrue(obs[0].is_demo)
        # 23 个地图对象 + 1 条缺口子记录 + 1 条 P20 观测候选
        gaps = [g for m in marks for g in m.gap_records()]
        self.assertEqual(1, len(gaps))
        routes = {m.detail("route_type"): m for m in marks
                  if m.kind is AnnotationKind.TEAM_ROUTE}
        self.assertEqual({"plan", "confirmed", "actual"}, set(routes))
        self.assertNotEqual(routes["confirmed"].geo_points, routes["actual"].geo_points)
        self.assertTrue(routes["actual"].detail("source_ref"))
        self.assertTrue(gaps[0]["segment_index"] and gaps[0]["fraction"])
        self.assertTrue(all(32.08 < lat < 32.14 and 110.17 < lon < 110.27
                            for m in marks for lat, lon in m.geo_points))
        # 火场范围：8–12 个不规则顶点
        area = next(m for m in marks if m.kind is AnnotationKind.FIRE_AREA)
        self.assertTrue(8 <= len(area.geo_points) <= 12)
        self.assertTrue(tm.irregular_ok(area.geo_points))
        # 必备对象齐备
        kinds = {m.kind.value for m in marks}
        for kind in ("main_fireline", "hotspot", "spread_direction", "hazard_zone",
                     "firebreak", "team_position", "team_route", "command_post",
                     "water_source", "supply_point", "medical_point", "drone_position",
                     "drone_coverage", "protected_target", "control_line",
                     "staging_point", "reserve_point"):
            self.assertIn(kind, kinds, kind)
        # F-03/F-04 定稿热点 + P20 演示观测热点
        self.assertEqual(3, len([m for m in marks if m.kind is AnnotationKind.HOTSPOT]))
        # 全部标注为演示数据，且没有编造数量
        self.assertTrue(all(m.is_demo for m in marks))
        self.assertTrue(all("演示数据" in (m.demo_notice or "演示数据") for m in marks))
        for m in marks:
            for key in ("stock", "beds", "supply_amount", "speed"):
                if m.detail(key):
                    self.assertTrue(m.detail("source_ref"), f"{m.id} 有数量无来源")
        # 影像覆盖区必须带限制说明
        cov = next(m for m in marks if m.kind is AnnotationKind.DRONE_COVERAGE)
        self.assertEqual("rough", cov.details["positioning"])
        # 命令 / 接收 / 回执 / 独立核验 / 来源夹具齐备
        self.assertEqual(1, len(ws.command_flows))
        flow = list(ws.command_flows.values())[0]
        self.assertTrue(flow.acked_at)
        self.assertTrue(flow.receipts)
        self.assertTrue(ws.verifications)
        self.assertTrue(ws.evidence)
        self.assertEqual((), ws.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
