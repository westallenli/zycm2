"""Materials & squads long-term ledger (P16-T4 domain) — 人员与物资底账.

对齐口径：页面设计定版 ``04_人员与物资_V0.2.md``。

定位纪律：
- 本模块是「长期基础资源」层：物资档案、中队与随队携带清单、物资点库存；
- 每次火情只生成「到场快照」（事件作用域），不反向修改长期档案（§9 数据边界）；
- 外部物资系统未接入时库存显式「未知」，不编造有货（§7）；
- 缺口分级中「即将不足」需要消耗速率数据；演示阶段无速率数据时只输出
  正常／当前不足／已影响执行三档，不伪造推算（诚实降档优于虚构分级）。

纯领域层：无 HTTP、无 HTML；持久化为独立 JSON（与事件注册表分库，互不耦合）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

#: 物资可用状态（长期档案维度）。
ITEM_USABLE = "可用"
ITEM_DAMAGED = "损坏"
ITEM_MAINTENANCE = "维护"

#: 到场物资状态机（doc 04 §4.2）。
SNAP_RESERVED = "预登记"
SNAP_CARRIED = "随队携带"
SNAP_UNCHECKED = "已到场待核对"
SNAP_CHECKED = "已核对可用"
SNAP_IN_USE = "使用中"
SNAP_PARTIAL = "部分消耗"
SNAP_DAMAGED = "损坏"
SNAP_MISSING = "缺失"
SNAP_RESUPPLY = "待补给"
SNAP_RETURNED = "已归还"

#: 缺口分级（doc 04 §6.1）。
GAP_OK = "正常"
GAP_INCOMING = "即将不足"
GAP_SHORT = "当前不足"
GAP_BLOCKING = "已影响执行"

#: 物资点数据来源状态（doc 04 §7.1）。
SOURCE_LOCAL = "local"        # 本地维护（演示资源点数据）
SOURCE_EXTERNAL = "external"  # 外部接口
SOURCE_PENDING = "pending"    # 未接入：「物资点库存接口：待接入」

STOCK_UNKNOWN = None  # 库存未知：外部系统未接入时显式为 None，不用 0 冒充


def utc_now_iso() -> str:
    from .fire_events import utc_now_iso as _now

    return _now()


# ---------------------------------------------------------------------------
# 长期档案记录


# ---------------------------------------------------------------------------
# 长期档案记录


@dataclass(frozen=True)
class MaterialItem:
    """物资档案（doc 04 §2.3）。"""

    item_id: str
    name: str
    category: str = ""
    spec: str = ""
    unit: str = ""
    default_carry_qty: float = 0.0
    status: str = ITEM_USABLE          # 可用 / 损坏 / 维护
    valid_until: str = ""              # 有效期（ISO 或空）
    owner: str = ""
    last_inventory_at: str = ""


@dataclass(frozen=True)
class Squad:
    """中队（doc 04 §2.2）：成员以手机号关联长期人员库，岗位集合关联编制槽位。"""

    squad_id: str
    name: str
    headcount_planned: int = 0
    member_phones: tuple[str, ...] = ()
    position_slots: tuple[str, ...] = ()   # pos_code 集合
    leader_phone: str = ""
    contact: str = ""


@dataclass(frozen=True)
class CarryLine:
    """中队随队物资固定携带清单行（doc 04 §3.1）：每次到场默认携带数量。"""

    squad_id: str
    item_id: str
    quantity: float = 0.0
    unit: str = ""


@dataclass
class SupplyPoint:
    """物资点（doc 04 §2.4 / §7）：库存未知显式 STOKE_UNKNOWN，不编造。"""

    point_id: str
    name: str
    location: str = ""
    org: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    supplies: dict = field(default_factory=dict)  # item_id -> float | STOCK_UNKNOWN
    open_status: str = "开放"
    eta_load: str = ""                 # 预计装运时间
    source: str = SOURCE_LOCAL
    updated_at: str = ""

    def stock_word(self, item_id: str) -> str:
        if item_id not in self.supplies:
            return "无此物资"
        value = self.supplies[item_id]
        if value is STOCK_UNKNOWN:
            return "库存未知 · 等待物资系统接口"
        return f"{value:g}"

    def source_word(self) -> str:
        """数据来源状态展示（doc 04 §7.1）。"""
        return {
            SOURCE_LOCAL: "本地维护（演示数据）",
            SOURCE_EXTERNAL: "外部接口",
            SOURCE_PENDING: "物资点库存接口：待接入",
        }.get(self.source, self.source)


@dataclass(frozen=True)
class ExternalCompany:
    """Catalog entry only; listed services are unverified, never stock or ETA."""

    company_id: str
    name: str
    services: str = ""
    region: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    source: str = ""
    source_updated_at: str = ""
    recorded_by: str = ""
    recorded_at: str = ""
    verification_state: str = "unverified"


# ---------------------------------------------------------------------------
# 事件到场快照（事件作用域，不改长期档案）


@dataclass
class SnapshotLine:
    """快照物资行：登记数量为长期清单基准，其余为本次差异事实。"""

    item_id: str
    qty_registered: float = 0.0
    qty_usable: float | None = None    # None ＝ 未核对
    consumed: float = 0.0
    damaged: float = 0.0
    missing: float = 0.0
    reason: str = ""                   # 差异原因（人工登记）
    status: str = SNAP_CARRIED


@dataclass
class SquadSnapshot:
    squad_id: str
    arrived_phones: tuple[str, ...] = ()
    lines: dict = field(default_factory=dict)   # item_id -> SnapshotLine
    arrival_id: str = ''  # Initial versioned arrival record; empty for legacy data.


@dataclass
class EventMaterialsSnapshot:
    event_id: str
    squads: dict = field(default_factory=dict)  # squad_id -> SquadSnapshot
    updated_at: str = ""

    def all_lines(self):
        for squad in self.squads.values():
            yield squad, squad.lines


# ---------------------------------------------------------------------------
# 底账存储


class MaterialsLedger:
    def __init__(self) -> None:
        self.items: dict[str, MaterialItem] = {}
        self.squads: dict[str, Squad] = {}
        self.carry_lines: dict[str, CarryLine] = {}      # "squad|item" -> line
        self.points: dict[str, SupplyPoint] = {}
        self.companies: dict[str, ExternalCompany] = {}
        self.snapshots: dict[str, EventMaterialsSnapshot] = {}
        self.flow_records = {}  # ObjectRevision.id -> arrival/loss revision
        self.transfer_records = {}  # ObjectRevision.id -> event transfer stage
        self.withdrawal_records = {}  # ObjectRevision.id -> named team material departure
        self.supply_records = {}  # Audited stock verification and formal supply execution; shared across events.
        self.supply_receipts = {}  # Named actual receipt batches; never rewrite arrival baselines.
        self.maintenance_records = {}  # Named cross-event materials maintenance grants/revocations.
        self.catalog_records = {}  # Immutable named catalog changes and document import versions.
        self.daily_resource_records = []  # Event-independent named maintenance, append only.
        self.resource_metadata = {}  # Source-labelled coordinates and public/extra usage assumptions.
        self._seq = {"mat": 0, "sq": 0, "pt": 0, "co": 0}

    # -- 编号 -----------------------------------------------------------------

    def _next(self, kind: str) -> str:
        self._seq[kind] += 1
        return f"{kind}-{self._seq[kind]:03d}"

    # -- 物资档案 --------------------------------------------------------------

    def upsert_item(
        self,
        *,
        name: str,
        category: str = "",
        spec: str = "",
        unit: str = "",
        default_carry_qty: float = 0.0,
        status: str = ITEM_USABLE,
        valid_until: str = "",
        owner: str = "",
        item_id: str | None = None,
    ) -> MaterialItem:
        if not (name or "").strip():
            raise ValueError("物资名称不能为空。")
        if status not in {ITEM_USABLE, ITEM_DAMAGED, ITEM_MAINTENANCE}:
            raise ValueError(f"未知物资状态：{status}")
        if default_carry_qty < 0:
            raise ValueError("默认携带数量不能为负。")
        if item_id is None:
            item_id = self._next("mat")
        existing = self.items.get(item_id)
        final_id = item_id
        if existing is None and any(
            i.name == name.strip() and i.spec == spec.strip()
            for i in self.items.values()
        ):
            raise ValueError(f"重复物资：{name}（{spec or '无规格'}）。")
        item = MaterialItem(
            item_id=final_id,
            name=name.strip(),
            category=category.strip(),
            spec=spec.strip(),
            unit=unit.strip(),
            default_carry_qty=float(default_carry_qty),
            status=status,
            valid_until=valid_until,
            owner=owner,
            last_inventory_at=(existing.last_inventory_at if existing else ""),
        )
        self.items[final_id] = item
        return item

    def mark_inventory(self, item_id: str, *, at: str) -> MaterialItem:
        item = self._require_item(item_id)
        updated = MaterialItem(**{**asdict(item), "last_inventory_at": at})
        self.items[item_id] = updated
        return updated

    def _require_item(self, item_id: str) -> MaterialItem:
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"未知物资：{item_id}")
        return item

    # -- 中队与携带清单 ---------------------------------------------------------

    def add_squad(
        self,
        *,
        name: str,
        member_phones=(),
        position_slots=(),
        headcount_planned: int = 0,
        leader_phone: str = "",
        contact: str = "",
        library: dict | None = None,
        squad_id: str | None = None,
    ) -> Squad:
        if not (name or "").strip():
            raise ValueError("中队名称不能为空。")
        phones = tuple(member_phones or ())
        if library is not None:
            unknown = [p for p in phones if p not in library]
            if unknown:
                raise KeyError(f"成员不在长期人员库：{', '.join(sorted(unknown))}")
            if leader_phone and (
                leader_phone not in phones or not library[leader_phone].is_leader
            ):
                raise ValueError("队长须是该中队在库成员，且人员档案已标记队长。")
        if squad_id is None:
            squad_id = self._next("sq")
        squad = Squad(
            squad_id=squad_id,
            name=name.strip(),
            headcount_planned=int(headcount_planned),
            member_phones=phones,
            position_slots=tuple(position_slots or ()),
            leader_phone=leader_phone,
            contact=contact,
        )
        self.squads[squad_id] = squad
        return squad

    def set_carry_line(
        self, squad_id: str, item_id: str, *, quantity: float | None = None
    ) -> CarryLine:
        """设置/更新携带清单行；缺省数量取物资档案默认携带数量（§3 基准语义）。"""
        squad = self.squads.get(squad_id)
        if squad is None:
            raise KeyError(f"未知中队：{squad_id}")
        item = self._require_item(item_id)
        if quantity is None:
            quantity = item.default_carry_qty
        if quantity < 0:
            raise ValueError("携带数量不能为负。")
        line = CarryLine(
            squad_id=squad_id, item_id=item_id, quantity=float(quantity), unit=item.unit
        )
        self.carry_lines[f"{squad_id}|{item_id}"] = line
        return line

    def set_squad_leader(self, squad_id: str, phone: str, *, library: dict) -> Squad:
        """Explicit long-term captain binding; never infer it from the first member."""
        squad = self.squads.get(squad_id)
        if squad is None:
            raise KeyError(f"未知中队：{squad_id}")
        person = library.get(phone)
        if not phone or phone not in squad.member_phones or person is None or not person.is_leader:
            raise ValueError("队长须是该中队在库成员，且人员档案已标记队长。")
        updated = replace(squad, leader_phone=phone)
        self.squads[squad_id] = updated
        return updated

    def carry_list(self, squad_id: str) -> list[CarryLine]:
        from .resource_directory import enabled
        return sorted(
            (l for k,l in self.carry_lines.items() if l.squad_id == squad_id and enabled(self,'carry',k) and enabled(self,'items',l.item_id)),
            key=lambda l: l.item_id,
        )

    # -- 物资点 -----------------------------------------------------------------

    def add_supply_point(
        self,
        *,
        name: str,
        location: str = "",
        org: str = "",
        contact_name: str = "",
        contact_phone: str = "",
        supplies: dict | None = None,
        source: str = SOURCE_LOCAL,
        point_id: str | None = None,
    ) -> SupplyPoint:
        if not (name or "").strip():
            raise ValueError("物资点名称不能为空。")
        if source not in {SOURCE_LOCAL, SOURCE_EXTERNAL, SOURCE_PENDING}:
            raise ValueError(f"未知数据来源：{source}")
        if point_id is None:
            point_id = self._next("pt")
        clean_supplies = {
            item_id: (STOCK_UNKNOWN if value is None else float(value))
            for item_id, value in (supplies or {}).items()
        }
        unknown_items = [k for k in clean_supplies if k not in self.items]
        if unknown_items:
            raise KeyError(f"物资档案不存在：{', '.join(sorted(unknown_items))}")
        point = SupplyPoint(
            point_id=point_id,
            name=name.strip(),
            location=location.strip(),
            org=org.strip(),
            contact_name=contact_name.strip(),
            contact_phone=contact_phone.strip(),
            supplies=clean_supplies,
            source=source,
            updated_at=utc_now_iso(),
        )
        self.points[point_id] = point
        return point

    def update_point_stock(
        self, point_id: str, item_id: str, *, value: float | None
    ) -> SupplyPoint:
        point = self.points.get(point_id)
        if point is None:
            raise KeyError(f"未知物资点：{point_id}")
        self._require_item(item_id)
        point.supplies[item_id] = STOCK_UNKNOWN if value is None else float(value)
        point.updated_at = utc_now_iso()
        return point

    # -- 外部公司资料：仅名录，不代表已核实供应能力 -----------------------------

    def add_external_company(
        self, *, name: str, source: str, recorded_by: str,
        services: str = "", region: str = "", contact_name: str = "",
        contact_phone: str = "", source_updated_at: str = "",
        company_id: str | None = None,
    ) -> ExternalCompany:
        """Trusted domain entry; caller must verify actor before any write surface."""
        for label, value in (("公司名称", name), ("资料来源", source),
                             ("记录人", recorded_by)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}不能为空。")
        for value in (services, region, contact_name, contact_phone, source_updated_at):
            if not isinstance(value, str):
                raise ValueError("公司资料字段必须是文本。")
        if source_updated_at:
            from .record_contracts import explicit_time
            explicit_time(source_updated_at)
        if any(item.name == name.strip() for item in self.companies.values()):
            raise ValueError("同名公司已在清单中；更新须走后续留痕维护流程。")
        if company_id is None:
            company_id = self._next("co")
        elif not isinstance(company_id, str) or not company_id.strip() or company_id in self.companies:
            raise ValueError("公司编号无效或已存在。")
        company = ExternalCompany(
            company_id=company_id, name=name.strip(), services=services.strip(),
            region=region.strip(), contact_name=contact_name.strip(),
            contact_phone=contact_phone.strip(), source=source.strip(),
            source_updated_at=source_updated_at, recorded_by=recorded_by.strip(),
            recorded_at=utc_now_iso(),
        )
        self.companies[company_id] = company
        return company

    # -- 到场快照（doc 04 §4）----------------------------------------------------

    def generate_event_snapshot(
        self, event_id: str, arrivals: dict
    ) -> EventMaterialsSnapshot:
        """按到场人员自动生成事件物资快照：中队任一成员到场即载入其完整清单。

        ``arrivals``：phone -> 中队编号（由刷码匹配所属中队后传入）。
        快照只属于本事件；不反向修改长期档案（§4 / §9）。
        """
        squad_phones: dict[str, list[str]] = {}
        for phone, squad_id in arrivals.items():
            current = self.snapshots.get(event_id)
            if current and squad_id in current.squads and current.squads[squad_id].arrival_id:
                raise ValueError('Versioned team arrival must use the material flow service')
            if squad_id not in self.squads:
                raise KeyError(f"手机号 {phone} 归属的中队不存在：{squad_id}")
            squad_phones.setdefault(squad_id, []).append(phone)

        snapshot = self.snapshots.get(event_id) or EventMaterialsSnapshot(
            event_id=event_id, updated_at=utc_now_iso()
        )
        for squad_id, phones in sorted(squad_phones.items()):
            squad_snap = snapshot.squads.get(squad_id) or SquadSnapshot(squad_id=squad_id)
            merged = sorted(set(squad_snap.arrived_phones) | set(phones))
            squad_snap.arrived_phones = tuple(merged)
            for line in self.carry_list(squad_id):
                existing = squad_snap.lines.get(line.item_id)
                if existing is None:
                    squad_snap.lines[line.item_id] = SnapshotLine(
                        item_id=line.item_id,
                        qty_registered=line.quantity,
                        status=SNAP_CARRIED,
                    )
            snapshot.squads[squad_id] = squad_snap
        snapshot.updated_at = utc_now_iso()
        self.snapshots[event_id] = snapshot
        return snapshot

    def _require_snapshot_line(self, event_id: str, squad_id: str, item_id: str):
        snapshot = self.snapshots.get(event_id)
        if snapshot is None or squad_id not in snapshot.squads:
            raise KeyError("该事件的到场快照不存在或中队未到场。")
        squad_snap = snapshot.squads[squad_id]
        line = squad_snap.lines.get(item_id)
        if line is None:
            raise KeyError("该物资不在本中队携带清单。")
        return snapshot, squad_snap, line

    def check_line(
        self, event_id: str, squad_id: str, item_id: str, *, qty_usable: float, reason: str = ""
    ) -> SnapshotLine:
        """现场核对：登记实际可用数量；不足部分按差异入账并转待补给。"""
        _, _, line = self._require_snapshot_line(event_id, squad_id, item_id)
        if self.snapshots[event_id].squads[squad_id].arrival_id:
            raise ValueError('Versioned arrival needs no second material confirmation')
        line.qty_usable = float(qty_usable)
        shortfall = max(line.qty_registered - qty_usable, 0.0)
        if shortfall > 0:
            line.missing = shortfall
            line.reason = reason or "现场核对短缺"
            line.status = SNAP_MISSING
        else:
            line.status = SNAP_CHECKED
        return line

    def consume_line(
        self, event_id: str, squad_id: str, item_id: str, *, amount: float
    ) -> SnapshotLine:
        """执行消耗：部分消耗或耗尽转待补给判定入口。"""
        _, _, line = self._require_snapshot_line(event_id, squad_id, item_id)
        if self.snapshots[event_id].squads[squad_id].arrival_id:
            raise ValueError('Versioned losses must use the material flow service')
        if amount < 0:
            raise ValueError("消耗数量不能为负。")
        base = line.qty_usable if line.qty_usable is not None else line.qty_registered
        line.consumed += float(amount)
        if base and line.consumed >= base:
            line.status = SNAP_RESUPPLY
        elif line.consumed > 0:
            line.status = SNAP_PARTIAL
        return line

    # -- 缺口计算（doc 04 §6）-----------------------------------------------------

    def compute_gap(
        self,
        event_id: str,
        demand: dict,
        *,
        projected_consumption: dict | None = None,
        resupply_available: dict | None = None,
        workspace=None,
    ) -> list[dict]:
        """按需求字典计算缺口行；严重程度四档（§6.1），缺速率数据时不输出「即将不足」。

        ``demand``：item_id -> 需求量（据战法/火线测算，调用方产出，
        可直接用 fire_calculus.material_requirements 的结果折算）；
        ``projected_consumption``：可选 item_id -> 预计还将消耗量（有消耗速率数据时提供）；
        ``resupply_available``：可选 item_id -> 最近物资点可补给量（未知传 None）。
        """
        if workspace is not None:
            from .material_inventory_reads import drill_gap_rows
            return drill_gap_rows(workspace, self, event_id, demand,
                                  projected_consumption=projected_consumption,
                                  resupply_available=resupply_available)
        # Historical compatibility API without an audit workspace. Current UI
        # and inquiry callers must use the checked read model above.
        snapshot = self.snapshots.get(event_id)
        rows: dict[str, dict] = {}

        def blank(item_id: str) -> dict:
            return {
                "item_id": item_id,
                "name": self.items[item_id].name if item_id in self.items else item_id,
                "unit": self.items[item_id].unit if item_id in self.items else "",
                "demand": 0.0,
                "on_site": 0.0,
                "usable": 0.0,
                "consumed": 0.0,
                "damaged": 0.0,
                "missing": 0.0,
                "gap": None,
                "severity": GAP_OK,
                "resupply_available": (resupply_available or {}).get(item_id, "—"),
            }

        for item_id, qty in demand.items():
            row = rows.setdefault(item_id, blank(item_id))
            row["demand"] += float(qty)

        if snapshot is not None:
            for _squad, lines in snapshot.all_lines():
                for item_id, line in lines.items():
                    row = rows.setdefault(item_id, blank(item_id))
                    row["on_site"] += line.qty_registered
                    row["consumed"] += line.consumed
                    row["damaged"] += line.damaged
                    row["missing"] += line.missing
                    if line.qty_usable is not None:
                        row["usable"] += max(line.qty_usable - line.damaged - line.missing, 0.0) if _squad.arrival_id else line.qty_usable
                    elif line.status == SNAP_CARRIED:
                        row["usable"] += line.qty_registered  # 未核对时按登记数乐观计入并在名称侧标注

        for item_id, row in rows.items():
            if any(record.reference.event_id == event_id
                   and record.reference.object_kind == "material_transfer"
                   and json.loads(record.content_json).get("item_id") == item_id
                   for record in self.transfer_records.values()):
                from .material_transfers import transfer_in_transit, return_damaged

                damaged_return = return_damaged(self, event_id=event_id, item_id=item_id)
                row["damaged"] += damaged_return
                row["usable"] = max(
                    row["usable"] - transfer_in_transit(self, event_id=event_id, item_id=item_id)
                    - damaged_return,
                    0.0,
                )
            if self.withdrawal_records:
                from .material_withdrawals import withdrawal_record

                departed = sum(
                    next((line['on_site_before']
                          for line in json.loads(record.content_json)['lines']
                          if line['item_id'] == item_id), 0.0)
                    for record in self.withdrawal_records.values()
                    if record.reference.event_id == event_id
                    and withdrawal_record(self, event_id=event_id,
                                          squad_id=record.reference.object_id) is not None
                )
                row["usable"] = max(row["usable"] - departed, 0.0)
            if row["demand"] <= 0:
                row["severity"] = GAP_OK
                row["gap"] = 0.0
                continue
            available = max(row["usable"] - row["consumed"], 0.0)
            gap = max(row["demand"] - available, 0.0)
            row["gap"] = gap
            if row["usable"] == 0 and row["on_site"] == 0:
                row["severity"] = GAP_BLOCKING
            elif gap > 0:
                row["severity"] = GAP_BLOCKING if available == 0 else GAP_SHORT
            else:
                projected = (projected_consumption or {}).get(item_id)
                if projected is not None and projected >= available:
                    row["severity"] = GAP_INCOMING
                else:
                    row["severity"] = GAP_OK
        ordered = sorted(rows.values(), key=lambda r: r["item_id"])
        severity_rank = {GAP_BLOCKING: 0, GAP_INCOMING: 1, GAP_SHORT: 2, GAP_OK: 3}
        return sorted(ordered, key=lambda r: severity_rank[r["severity"]])

    def nearest_points_for(self, item_id: str) -> list[SupplyPoint]:
        """可供应该物资的物资点（开放优先、本地来源在前）；不做距离伪造排序。"""
        candidates = [
            p for p in self.points.values() if p.open_status == "开放" and item_id in p.supplies
        ]
        return sorted(candidates, key=lambda p: (p.source != SOURCE_LOCAL, p.point_id))

    # -- 持久化 -------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "flow_records": {key: asdict(value) for key, value in self.flow_records.items()},
            "transfer_records": {key: asdict(value) for key, value in self.transfer_records.items()},
            "withdrawal_records": {key: asdict(value) for key, value in self.withdrawal_records.items()},
            "supply_records": {key: asdict(value) for key, value in self.supply_records.items()},
            "supply_receipts": {key: asdict(value) for key, value in self.supply_receipts.items()},
            "maintenance_records": {key: asdict(value) for key, value in self.maintenance_records.items()},
            "catalog_records": {key: asdict(value) for key, value in self.catalog_records.items()},
            "daily_resource_records": self.daily_resource_records,
            "resource_metadata": self.resource_metadata,
            "seq": dict(self._seq),
            "items": {k: asdict(v) for k, v in self.items.items()},
            "squads": {k: asdict(v) for k, v in self.squads.items()},
            "carry_lines": {k: asdict(v) for k, v in self.carry_lines.items()},
            "points": {k: asdict(v) for k, v in self.points.items()},
            "companies": {k: asdict(v) for k, v in self.companies.items()},
            "snapshots": {
                eid: {
                    "event_id": s.event_id,
                    "updated_at": s.updated_at,
                    "squads": {
                        sid: {
                            "squad_id": sq.squad_id,
                            "arrival_id": sq.arrival_id,
                            "arrived_phones": list(sq.arrived_phones),
                            "lines": {iid: asdict(l) for iid, l in sq.lines.items()},
                        }
                        for sid, sq in s.squads.items()
                    },
                }
                for eid, s in self.snapshots.items()
            },
        }

    def save(self, path: str | Path) -> Path:
        from .durable_storage import before_write
        before_write(path)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "MaterialsLedger":
        target = Path(path)
        ledger = cls()
        if not target.exists():
            return ledger
        payload = json.loads(target.read_text(encoding="utf-8"))
        ledger._seq = dict(payload["seq"])
        ledger._seq.setdefault("co", 0)
        from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
        for key, raw in payload.get('flow_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material history record identity mismatch')
            ledger.flow_records[key] = record
        for key, raw in payload.get('transfer_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material transfer record identity mismatch')
            ledger.transfer_records[key] = record
        for key, raw in payload.get('withdrawal_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material withdrawal record identity mismatch')
            ledger.withdrawal_records[key] = record
        for key, raw in payload.get('supply_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material supply record identity mismatch')
            ledger.supply_records[key] = record
        for key, raw in payload.get('supply_receipts', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material supply receipt identity mismatch')
            ledger.supply_receipts[key] = record
        for key, raw in payload.get('maintenance_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key:
                raise ValueError('Material maintenance grant identity mismatch')
            ledger.maintenance_records[key] = record
        from .material_maintenance_authority import journal
        journal(ledger)
        for key, raw in payload.get('catalog_records', {}).items():
            raw = dict(raw)
            raw['reference'] = ObjectReference(**raw['reference'])
            raw['times'] = RecordTimes(**raw['times'])
            record = ObjectRevision(**raw)
            if record.id != key or record.reference.object_kind != 'material_catalog_change':
                raise ValueError('Material catalog change identity mismatch')
            ledger.catalog_records[key] = record
        ledger.daily_resource_records = payload.get('daily_resource_records', [])
        ledger.resource_metadata = payload.get('resource_metadata', {})
        from .resource_maintenance import journal
        journal(ledger)
        for key, raw in payload["items"].items():
            ledger.items[key] = MaterialItem(**raw)
        for key, raw in payload["squads"].items():
            raw["member_phones"] = tuple(raw["member_phones"])
            raw["position_slots"] = tuple(raw["position_slots"])
            ledger.squads[key] = Squad(**raw)
        for key, raw in payload["carry_lines"].items():
            ledger.carry_lines[key] = CarryLine(**raw)
        for key, raw in payload["points"].items():
            supplies = {
                iid: (STOCK_UNKNOWN if v is None else float(v))
                for iid, v in raw["supplies"].items()
            }
            raw["supplies"] = supplies
            ledger.points[key] = SupplyPoint(**raw)
        for key, raw in payload.get("companies", {}).items():
            company = ExternalCompany(**raw)
            if (company.company_id != key or company.verification_state != "unverified"
                    or not company.name or not company.source or not company.recorded_by):
                raise ValueError("外部公司清单记录不完整或核实状态不受支持。")
            ledger.companies[key] = company
        for eid, raw in payload["snapshots"].items():
            snap = EventMaterialsSnapshot(event_id=raw["event_id"], updated_at=raw["updated_at"])
            for sid, sq_raw in raw["squads"].items():
                squad_snap = SquadSnapshot(
                    squad_id=sq_raw["squad_id"], arrived_phones=tuple(sq_raw["arrived_phones"]),
                    arrival_id=sq_raw.get('arrival_id', ''),
                )
                for iid, line_raw in sq_raw["lines"].items():
                    squad_snap.lines[iid] = SnapshotLine(**line_raw)
                snap.squads[sid] = squad_snap
            ledger.snapshots[eid] = snap
        from .material_flows import validate_arrival_links
        validate_arrival_links(ledger)
        from .material_transfers import validate_transfer_links

        validate_transfer_links(ledger)
        from .material_withdrawals import validate_withdrawal_links

        validate_withdrawal_links(ledger)
        from .material_supply_receipts import ledger_history
        for event_id in {r.reference.event_id for r in ledger.supply_receipts.values()}:
            ledger_history(ledger, event_id)
        return ledger
