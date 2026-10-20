"""Map annotations for the sandtable (P16-A, doc 07/08 demo alignment).

The sandtable is the commander's shared factual surface. Annotations are
business objects, not decorations: every mark carries a kind (the six quick
tools from the phase-3 requirement — fire point, HQ, water, material point,
landing zone, isolation line — plus generic line/polygon/arrow/text), a
verification lifecycle (draft -> pending_review -> verified -> published),
a source (manual / ai-suggestion / geojson-import), and provenance.

Discipline carried over from the reference doctrine:
- An AI-suggested mark can never enter the shared picture by itself; it is
  born ``pending_review`` and needs an authorized verification act.
- Import is not verification: GeoJSON features land as ``draft``.
- Geometry is schematic SVG canvas coordinates (0..1000 x 0..640); lengths
  derived from it are indicative only and must be labeled as such in UI.

Zero third-party dependencies; deterministic parsing; explicit errors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum

from . import tactical_map as tm
from .tactical_map import TacticalError

#: SVG canvas bounds used by the workbench renderer. Keep in sync with
#: workbench._svg_base() (viewBox 0 0 1400 760).
CANVAS_WIDTH = 1400
CANVAS_HEIGHT = 760


class AnnotationError(ValueError):
    """Raised for invalid annotation data; message is safe to display."""


class AnnotationKind(str, Enum):
    """Quick tools first (PRD FUN-13), then generic drawing kinds.

    P19：战术态势注册表提供 15 类 P0 + 10 类 P1（定稿 §3）；
    下方的旧式通用类型保留不变——只借符号显示，不自动获得战术语义。
    """

    # —— 旧式通用标绘（兼容层，不删不改） --------------------------------
    FIRE_POINT = "fire_point"        # 火点（certainty 区分疑似/已确认）
    HEADQUARTERS = "headquarters"    # 指挥部
    WATER = "water"                  # 水源
    MATERIAL = "material"            # 物资点
    LANDING_ZONE = "landing_zone"    # 起降点
    ISOLATION = "isolation"          # 隔离带（线，示意带宽在渲染层表达）
    FIRELINE = "fireline"            # 火线（线）
    ROUTE = "route"                  # 行军路线（线）
    ARROW = "arrow"                  # 箭头/方向（线）
    AREA = "area"                    # 区域：过火区/管控区/危险区（面）
    TEXT = "text"                    # 文字标注（单点+文字）

    # —— P19 P0 核心战术对象（15 类） ------------------------------------
    FIRE_AREA = "fire_area"                  # 初始火场范围（不规则面）
    MAIN_FIRELINE = "main_fireline"          # 当前重点火线
    HOTSPOT = "hotspot"                      # 火点/热点
    SPREAD_DIRECTION = "spread_direction"    # 蔓延方向
    HAZARD_ZONE = "hazard_zone"              # 危险区
    FIREBREAK = "firebreak"                  # 隔火带（中心线 + 米制宽度）
    TEAM_POSITION = "team_position"          # 小队当前位置
    TEAM_ROUTE = "team_route"                # 小队行进路线
    COMMAND_POST = "command_post"            # 前线指挥部
    WATER_SOURCE = "water_source"            # 水源地
    SUPPLY_POINT = "supply_point"            # 物资保障点
    MEDICAL_POINT = "medical_point"          # 医疗救护点
    DRONE_POSITION = "drone_position"        # 无人机位置
    DRONE_COVERAGE = "drone_coverage"        # 无人机影像覆盖区
    PROTECTED_TARGET = "protected_target"    # 重点保护目标

    # —— P19 P1 按需对象（10 类） ----------------------------------------
    EVACUATION_ROUTE = "evacuation_route"    # 撤离路线
    COMM_BLIND_ZONE = "comm_blind_zone"      # 通信盲区
    CONTROL_LINE = "control_line"            # 封控线
    ROAD_BLOCK = "road_block"                # 道路阻断点
    STAGING_POINT = "staging_point"          # 集结点
    RESERVE_POINT = "reserve_point"          # 预备队待命点
    BURNED_AREA = "burned_area"              # 过火区
    WIND_MARKER = "wind_marker"              # 风向风速
    LOOKOUT_POST = "lookout_post"            # 观察哨
    DRONE_PAD = "drone_pad"                  # 无人机起降点


#: P19 战术类型（P0+P1）——受逐类字段与发布闸门约束；旧式类型不受新闸门约束。
TACTICAL_KINDS: frozenset["AnnotationKind"] = frozenset(
    k for k in AnnotationKind if tm.is_tactical(k.value)
)


_KIND_GEOMETRY = {
    AnnotationKind.FIRE_POINT: "point",
    AnnotationKind.HEADQUARTERS: "point",
    AnnotationKind.WATER: "point",
    AnnotationKind.MATERIAL: "point",
    AnnotationKind.LANDING_ZONE: "point",
    AnnotationKind.TEXT: "point",
    AnnotationKind.ISOLATION: "line",
    AnnotationKind.FIRELINE: "line",
    AnnotationKind.ROUTE: "line",
    AnnotationKind.ARROW: "line",
    AnnotationKind.AREA: "polygon",
}

for _k in AnnotationKind:
    _KIND_GEOMETRY.setdefault(_k, tm.geometry_of(_k.value))

#: GeoJSON import/export mapping (deterministic, no silent guessing).
GEOJSON_KINDS: dict[str, str] = {kind.value: kind.value for kind in AnnotationKind}


class AnnotationStatus(str, Enum):
    DRAFT = "draft"                  # 草稿：仅本机可见的作业数据
    PENDING_REVIEW = "pending_review"  # 待核验
    VERIFIED = "verified"            # 已核验（授权核实动作）
    PUBLISHED = "published"          # 已发布进入共享态势
    WITHDRAWN = "withdrawn"          # 已撤回（保留历史，不物理删除）


class AnnotationSource(str, Enum):
    MANUAL = "manual"
    AI_SUGGESTION = "ai-suggestion"
    GEOJSON_IMPORT = "geojson-import"


_CERTAINTY_VALUES = ("suspected", "confirmed")
_ROUTE_STATUS_VALUES = tuple(s.value for s in tm.RouteStatus)
_POSITIONING_VALUES = tuple(s.value for s in tm.PositioningStatus)
_CONFLICT_LEVELS = tuple(s.value for s in tm.ConflictLevel)
_FRESHNESS_VALUES = tuple(s.value for s in tm.FreshnessStatus)


class GeometryError(AnnotationError):
    """兼容旧入口：几何/战术字段错误统一为可显示的标绘错误。"""


def geometry_kind(kind: AnnotationKind) -> str:
    return _KIND_GEOMETRY[kind]


@dataclass
class MapAnnotation:
    """One mark on the sandtable. Status changes create audit events; the
    object itself keeps provenance fields immutable after creation."""

    id: str
    kind: AnnotationKind
    points: tuple[tuple[float, float], ...]
    title: str
    status: AnnotationStatus = AnnotationStatus.DRAFT
    certainty: str | None = None       # fire_point only: suspected / confirmed
    note: str = ""
    source: AnnotationSource = AnnotationSource.MANUAL
    created_by: str = "local-general-entry"
    layer: str = field(default="maneuver")  # evidence / maneuver / admin
    #: 可选地理坐标（纬度, 经度），与 points 顶点一一对应；
    #: 未录入则该标绘仅在示意沙盘图层，绝不上真实底图（不伪造叠加）。
    geo_points: tuple[tuple[float, float], ...] = ()
    # —— P19 战术语义、业务属性与核验审计（定稿 §5.1）——
    event_id: str = ""
    details: dict[str, str] = field(default_factory=dict)
    basis: tuple[str, ...] = ()
    source_ref: tuple[str, ...] = ()
    responsible_unit: str = ""
    responsible_person: str = ""
    related_goal_id: str = ""
    related_plan_id: str = ""
    related_order_id: str = ""
    image_id: str = ""
    map_version: str = ""
    valid_from: str = ""
    valid_until: str = ""
    created_at: str = ""
    updated_at: str = ""
    verified_at: str = ""
    published_at: str = ""
    verification_record: dict[str, str] = field(default_factory=dict)
    publication_record: dict[str, str] = field(default_factory=dict)
    version: int = 1
    supersedes_id: str = ""
    withdraw_reason: str = ""
    route_status: str = ""
    conflict_level: str = ""
    freshness_status: str = ""
    positioning_status: str = ""
    is_demo: bool = False
    demo_notice: str = ""
    withdrawn_at: str = ""

    def __post_init__(self) -> None:
        # P18：geo 权威标绘——图上直接绘制只产真实经纬度，
        # 不反向伪造示意画布顶点（不做投影猜测）。
        geo_backed = bool(self.geo_points) and not self.points
        validate_annotation(
            self.kind, self.points, certainty=self.certainty,
            geo_backed=geo_backed,
        )
        validate_geo_points(self.points, self.geo_points)
        self._validate_tactical()

    def _validate_tactical(self) -> None:
        """P19 约束：受控词、逐类几何、标题与备注长度。"""
        if self.kind in TACTICAL_KINDS:
            if self.geo_points:
                try:
                    tm.validate_geometry(self.kind.value, self.geo_points)
                except TacticalError as exc:
                    raise AnnotationError(str(exc)) from exc
            elif self.points:
                raise AnnotationError(
                    f"{tm.kind_word(self.kind.value)}必须有地理坐标才能上图，"
                    "示意图顶点不能充当战术位置"
                )
        if self.route_status and self.route_status not in _ROUTE_STATUS_VALUES:
            raise AnnotationError(f"未知路线状态：{self.route_status}")
        if self.positioning_status and self.positioning_status not in _POSITIONING_VALUES:
            raise AnnotationError(f"未知定位状态：{self.positioning_status}")
        if self.conflict_level and self.conflict_level not in _CONFLICT_LEVELS:
            raise AnnotationError(f"未知冲突等级：{self.conflict_level}")
        if self.freshness_status and self.freshness_status not in _FRESHNESS_VALUES:
            raise AnnotationError(f"未知时效状态：{self.freshness_status}")
        title = (self.title or "").strip()
        if not 1 <= len(title) <= 40:
            raise AnnotationError("名称去空格后必须为 1 至 40 字")
        if len(self.note or "") > 500:
            raise AnnotationError("备注最长 500 字")
        for key, value in (self.details or {}).items():
            if not isinstance(value, str):
                raise AnnotationError(f"专属字段「{tm.detail_word(key)}」只能是文本")

    @property
    def geometry(self) -> str:
        return geometry_kind(self.kind)

    @property
    def tactical(self) -> bool:
        return self.kind in TACTICAL_KINDS

    @property
    def spec(self):
        return tm.kind_spec(self.kind.value)

    @property
    def layer_group(self) -> str:
        return tm.kind_spec(self.kind.value).layer

    def detail(self, key: str) -> str:
        return str((self.details or {}).get(key, "") or "").strip()

    def display_label(self) -> str:
        return f"{tm.kind_word(self.kind.value)}·{self.title}"

    def gap_records(self) -> tuple[dict[str, str], ...]:
        """隔火带缺口子记录：随父对象展示，不单独计作地图对象（定稿 §4.2）。"""
        if self.kind is not AnnotationKind.FIREBREAK or not self.detail("gap_status"):
            return ()
        return ({
            "ref": self.detail("gap_ref") or "缺口",
            "status": self.detail("gap_status"),
            "segment": self.detail("gap_segment"),
            "segment_index": self.detail("gap_segment_index"),
            "fraction": self.detail("gap_fraction"),
            "width_m": self.detail("gap_width_m"),
            "reported_at": self.detail("gap_reported_at"),
            "source": self.detail("gap_source"),
        },)

    def with_status(self, status: AnnotationStatus) -> "MapAnnotation":
        return replace(self, status=status)

    def with_geo_points(
        self, geo_points: tuple[tuple[float, float], ...]
    ) -> "MapAnnotation":
        return replace(self, geo_points=tuple(geo_points))

    def with_details(self, **changes: str) -> "MapAnnotation":
        """修改专属字段：版本递增；已发布对象必须走修订草稿。"""
        merged = dict(self.details)
        merged.update({k: str(v) for k, v in changes.items() if str(v).strip()})
        for key in [k for k, v in merged.items() if not str(v).strip()]:
            merged.pop(key)
        return replace(self, details=merged, version=self.version + 1)

    def as_revision(self, revision: "MapAnnotation") -> "MapAnnotation":
        """已发布对象的修订草稿：原版本保留历史，不静默改写。"""
        return replace(revision, supersedes_id=self.id, version=self.version + 1)


def validate_annotation(
    kind: AnnotationKind,
    points: tuple[tuple[float, float], ...],
    *,
    certainty: str | None = None,
    geo_backed: bool = False,
) -> None:
    if not isinstance(kind, AnnotationKind):
        raise AnnotationError(f"未知的标绘类型：{kind!r}")
    if geo_backed:
        # 几何由地理坐标承载；仅定性字段仍需校验。
        if certainty is not None:
            if kind is not AnnotationKind.FIRE_POINT:
                raise AnnotationError("只有火点可以标注疑似/已确认")
            if certainty not in _CERTAINTY_VALUES:
                raise AnnotationError(f"火点定性只支持 {'/'.join(_CERTAINTY_VALUES)}")
        return
    need = {"point": 1, "line": 2, "polygon": 3}[geometry_kind(kind)]
    if len(points) < need:
        raise AnnotationError(
            f"{kind.value} 需要至少 {need} 个顶点，当前 {len(points)} 个"
        )
    for x, y in points:
        if not (0 <= x <= CANVAS_WIDTH and 0 <= y <= CANVAS_HEIGHT):
            raise AnnotationError(
                f"坐标 ({x:.0f},{y:.0f}) 超出沙盘范围 "
                f"(0..{CANVAS_WIDTH}, 0..{CANVAS_HEIGHT})"
            )
    if certainty is not None:
        if kind is not AnnotationKind.FIRE_POINT:
            raise AnnotationError("只有火点可以标注疑似/已确认")
        if certainty not in _CERTAINTY_VALUES:
            raise AnnotationError(f"火点定性只支持 {'/'.join(_CERTAINTY_VALUES)}")


def validate_geo_points(
    points: tuple[tuple[float, float], ...],
    geo_points: tuple[tuple[float, float], ...],
) -> None:
    """地理坐标校验（doc 03 §6）：可为空；非空则必须与顶点一一对应。

    P18 例外：geo 权威标绘（图上绘制）允许无示意图顶点，
    此时几何完全由地理坐标承载，同样不做任何投影。
    """
    if not geo_points:
        return
    if not points:
        return
    if len(geo_points) != len(points):
        raise AnnotationError(
            f"地理坐标 {len(geo_points)} 个与标绘顶点 {len(points)} 个不一致："
            "每个顶点必须显式给定经纬度，不做自动投影"
        )
    for lat, lon in geo_points:
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise AnnotationError(
                f"经纬度 ({lat}, {lon}) 超出有效范围（纬度 ±90，经度 ±180）"
            )


#: Statuses an authorized verification act may move a mark into.
_VERIFY_ALLOWED = {
    AnnotationStatus.DRAFT: {AnnotationStatus.PENDING_REVIEW, AnnotationStatus.VERIFIED},
    # pending_review -> withdrawn：doc 03 §10.2 智脑候选「已驳回（保留原因）」支线。
    AnnotationStatus.PENDING_REVIEW: {AnnotationStatus.VERIFIED, AnnotationStatus.DRAFT, AnnotationStatus.WITHDRAWN},
    AnnotationStatus.VERIFIED: {AnnotationStatus.PUBLISHED, AnnotationStatus.PENDING_REVIEW},
    AnnotationStatus.PUBLISHED: {AnnotationStatus.WITHDRAWN},
    AnnotationStatus.WITHDRAWN: set(),
}


def can_transition(current: AnnotationStatus, target: AnnotationStatus) -> bool:
    return target in _VERIFY_ALLOWED[current]


def indicative_length_m(points: tuple[tuple[float, float], ...]) -> float:
    """Schematic length of a line annotation in metres.

    The canvas is an indication surface, not a projected CRS; the scale below
    exists so route/isolation marks can show ONE comparable number. Renderers
    must label derived lengths as 示意值.
    """
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return round(total * 4.0, 1)


# ---------------------------------------------------------------------------
# GeoJSON round-trip (import is NOT verification — everything lands as draft)


def to_geojson(annotations) -> str:
    """导出标绘。优先使用地理坐标（[经度, 纬度]），只有示意图顶点时
    才导出示意坐标，并在 properties 里标明坐标系——不做任何投影。
    """
    features = []
    for ann in annotations:
        geom_kind = ann.geometry
        geo = tuple(ann.geo_points or ())
        if geo:
            raw = [[round(lon, 6), round(lat, 6)] for lat, lon in geo]
            crs = "wgs84"
        else:
            raw = [[round(x, 1), round(y, 1)] for x, y in ann.points]
            crs = "schematic"
        coords = raw
        if geom_kind == "point":
            coords = coords[0]          # GeoJSON Point: flat [x, y]
        elif geom_kind == "polygon":
            coords = [coords + [coords[0]]]  # closed ring
        properties = {
            "kind": ann.kind.value,
            "status": ann.status.value,
            "title": ann.title,
            "source": ann.source.value,
            "id": ann.id,
            "crs": crs,
        }
        if ann.certainty is not None:
            properties["certainty"] = ann.certainty
        if ann.note:
            properties["note"] = ann.note
        if ann.tactical:
            properties["layer"] = ann.layer_group
            properties["version"] = ann.version
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": _GEOJSON_GEOM[geom_kind], "coordinates": coords},
                "properties": properties,
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False)


_GEOJSON_GEOM = {"point": "Point", "line": "LineString", "polygon": "Polygon"}
_GEOM_FROM_GEOJSON = {v: k for k, v in _GEOJSON_GEOM.items()}


def from_geojson(text: str, *, id_prefix: str = "ann") -> tuple[MapAnnotation, ...]:
    """Strictly parse a FeatureCollection into draft annotations.

    Unknown kinds, mismatched geometry, or malformed coordinates raise
    AnnotationError listing the offending feature index. Nothing is silently
    dropped: ingestion is not verification, but it is also not lossy.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnnotationError(f"GeoJSON 解析失败：{exc}") from exc
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise AnnotationError("GeoJSON 必须是 type=FeatureCollection 的对象")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise AnnotationError("FeatureCollection.features 必须是非空数组")

    result: list[MapAnnotation] = []
    for index, feature in enumerate(features):
        where = f"第 {index + 1} 个要素"
        if not isinstance(feature, dict):
            raise AnnotationError(f"{where}：不是有效的 Feature 对象")
        props = feature.get("properties") or {}
        kind_value = props.get("kind")
        if kind_value not in GEOJSON_KINDS:
            known = ", ".join(sorted(GEOJSON_KINDS))
            raise AnnotationError(f"{where}：kind={kind_value!r} 不受支持（{known}）")
        kind = AnnotationKind(kind_value)
        geometry = feature.get("geometry") or {}
        geom_type = geometry.get("type")
        if geom_type not in _GEOM_FROM_GEOJSON:
            raise AnnotationError(f"{where}：几何类型 {geom_type!r} 不受支持")
        expected_geom = geometry_kind(kind)
        if _GEOM_FROM_GEOJSON[geom_type] != expected_geom:
            raise AnnotationError(
                f"{where}：kind={kind.value} 需要 {expected_geom} 几何，"
                f"实际是 {_GEOM_FROM_GEOJSON[geom_type]}"
            )
        coords = geometry.get("coordinates")
        flat = _flatten_coordinates(coords, expected_geom)
        if flat is None:
            raise AnnotationError(f"{where}：坐标结构不合法")
        certainty = props.get("certainty")
        if certainty is not None and certainty not in _CERTAINTY_VALUES:
            raise AnnotationError(f"{where}：certainty={certainty!r} 不合法")
        try:
            ann = MapAnnotation(
                id=f"{id_prefix}-{len(result) + 1:03d}",
                kind=kind,
                points=tuple(flat),
                title=str(props.get("title") or kind.value),
                status=AnnotationStatus.DRAFT,  # import is not verification
                certainty=certainty,
                note=str(props.get("note") or ""),
                source=AnnotationSource.GEOJSON_IMPORT,
                created_by="geojson-import",
            )
        except AnnotationError as exc:
            raise AnnotationError(f"{where}：{exc}") from exc
        result.append(ann)
    return tuple(result)


def _flatten_coordinates(coords, geom_kind: str) -> list[tuple[float, float]] | None:
    """Normalize GeoJSON coordinates to a flat [(x, y), ...] list."""
    def pair(item):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            return None
        x, y = item[0], item[1]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        return (float(x), float(y))

    if geom_kind == "point":
        p = pair(coords)
        return [p] if p else None
    if isinstance(coords, list) and all(isinstance(c, (list, tuple)) for c in coords):
        pts = []
        for c in coords:
            p = pair(c)
            if p is None:
                return None
            pts.append(p)
        if geom_kind == "polygon":
            # accept both open and closed rings; store without closing dup
            if len(pts) >= 2 and pts[0] == pts[-1]:
                pts = pts[:-1]
        return pts
    return None


def status_word(status: AnnotationStatus) -> str:
    return {
        AnnotationStatus.DRAFT: "草稿",
        AnnotationStatus.PENDING_REVIEW: "待核验",
        AnnotationStatus.VERIFIED: "已核验",
        AnnotationStatus.PUBLISHED: "已发布",
        AnnotationStatus.WITHDRAWN: "已撤回",
    }[status]


def kind_label(kind: AnnotationKind) -> str:
    """前台中文类型名：P19 统一走战术注册表，旧类型保留旧词。"""
    value = getattr(kind, "value", str(kind))
    legacy = {
        "fire_point": "火点",
        "headquarters": "指挥部",
        "water": "水源",
        "material": "物资点",
        "landing_zone": "起降点",
        "isolation": "隔离带",
        "fireline": "火线",
        "route": "行军路线",
        "arrow": "箭头",
        "area": "区域",
        "text": "文字",
    }
    if value in legacy:
        return legacy[value]
    return tm.kind_word(value)
