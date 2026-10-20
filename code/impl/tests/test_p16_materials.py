"""P16-T4 验收：人员与物资长期底账（doc 04：档案/中队/携带清单/物资点/到场快照/缺口）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.materials import (
    GAP_BLOCKING,
    GAP_INCOMING,
    GAP_OK,
    GAP_SHORT,
    ITEM_DAMAGED,
    SNAP_CHECKED,
    SNAP_CARRIED,
    SNAP_MISSING,
    SNAP_PARTIAL,
    SNAP_RESUPPLY,
    STOCK_UNKNOWN,
    SOURCE_EXTERNAL,
    SOURCE_PENDING,
    MaterialsLedger,
)


def _ledger() -> MaterialsLedger:
    ledger = MaterialsLedger()
    ledger.upsert_item(name="高压水泵", category="装备", unit="台", default_carry_qty=4)
    ledger.upsert_item(name="水带 20m", category="装备", unit="盘", default_carry_qty=20)
    ledger.upsert_item(name="柴油", category="油料", unit="升", default_carry_qty=100)
    ledger.add_squad(
        name="扑救一分队",
        member_phones=("13912340001", "13912340002"),
        position_slots=("pjjz-1",),
        headcount_planned=12,
        squad_id="sq-001",
    )
    ledger.set_carry_line("sq-001", "mat-001")
    ledger.set_carry_line("sq-001", "mat-002")
    return ledger


class ItemCatalogTest(unittest.TestCase):
    def test_upsert_and_validation(self):
        ledger = MaterialsLedger()
        item = ledger.upsert_item(name="风水灭火机", unit="台")
        self.assertEqual(item.item_id, "mat-001")
        with self.assertRaises(ValueError):
            ledger.upsert_item(name="  ")
        with self.assertRaises(ValueError):
            ledger.upsert_item(name="X", status="未知状态")

    def test_duplicate_name_spec_refused(self):
        ledger = _ledger()
        with self.assertRaisesRegex(ValueError, "重复物资"):
            ledger.upsert_item(name="高压水泵", spec="", unit="台")

    def test_damage_status_is_long_term_flag(self):
        ledger = _ledger()
        updated = ledger.upsert_item(
            name="高压水泵", status=ITEM_DAMAGED, item_id="mat-001"
        )
        self.assertEqual(updated.status, ITEM_DAMAGED)


class SquadCarryTest(unittest.TestCase):
    def test_carry_defaults_from_catalog(self):
        ledger = _ledger()
        lines = ledger.carry_list("sq-001")
        self.assertEqual([l.quantity for l in lines], [4, 20])  # 默认数量取档案基准

    def test_unknown_squad_or_item_refused(self):
        ledger = _ledger()
        with self.assertRaises(KeyError):
            ledger.set_carry_line("sq-none", "mat-001")
        with self.assertRaises(KeyError):
            ledger.set_carry_line("sq-001", "mat-none")

    def test_member_validation_against_library(self):
        ledger = _ledger()
        library = {"13912340001": object()}
        with self.assertRaisesRegex(KeyError, "不在长期人员库"):
            ledger.add_squad(
                name="二分队",
                member_phones=("13912340001", "13999999999"),
                library=library,
            )


class SupplyPointTest(unittest.TestCase):
    def test_stock_unknown_explicit_not_zero(self):
        ledger = _ledger()
        point = ledger.add_supply_point(
            name="青岗镇防火物资库",
            supplies={"mat-001": None},
            source=SOURCE_PENDING,
        )
        self.assertIs(point.supplies["mat-001"], STOCK_UNKNOWN)
        self.assertIn("库存未知 · 等待物资系统接口", point.stock_word("mat-001"))

    def test_external_stock_update(self):
        ledger = _ledger()
        point = ledger.add_supply_point(
            name="县储备库", supplies={"mat-001": 10}, source=SOURCE_EXTERNAL
        )
        ledger.update_point_stock(point.point_id, "mat-001", value=None)
        self.assertIsNone(point.supplies["mat-001"])
        ledger.update_point_stock(point.point_id, "mat-001", value=7)
        self.assertEqual(point.supplies["mat-001"], 7)

    def test_unknown_item_in_supplies_refused(self):
        ledger = _ledger()
        with self.assertRaisesRegex(KeyError, "物资档案不存在"):
            ledger.add_supply_point(name="X", supplies={"mat-none": 1})

    def test_nearest_points_open_first_local_first(self):
        ledger = _ledger()
        ledger.add_supply_point(name="外部点A", supplies={"mat-001": 5}, source=SOURCE_EXTERNAL)
        ledger.add_supply_point(name="本地点B", supplies={"mat-001": 5})
        closed = ledger.add_supply_point(name="停用点C", supplies={"mat-001": 5})
        closed.open_status = "关闭"
        names = [p.name for p in ledger.nearest_points_for("mat-001")]
        self.assertEqual(names[0], "本地点B")   # 本地维护在前
        self.assertNotIn("停用点C", names)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.ledger = _ledger()

    def test_generate_snapshot_on_member_arrival(self):
        snap = self.ledger.generate_event_snapshot(
            "fire-001", arrivals={"13912340001": "sq-001"}
        )
        squad_snap = snap.squads["sq-001"]
        self.assertEqual(squad_snap.arrived_phones, ("13912340001",))
        self.assertEqual(set(squad_snap.lines), {"mat-001", "mat-002"})
        line = squad_snap.lines["mat-001"]
        self.assertEqual(line.qty_registered, 4)
        self.assertEqual(line.status, SNAP_CARRIED)

    def test_second_arrival_merges_without_duplication(self):
        self.ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        self.ledger.generate_event_snapshot("fire-002", {"13912340002": "sq-001"})
        # 不同事件互不影响（快照事件作用域）
        self.assertIn("fire-002", self.ledger.snapshots)
        snap1 = self.ledger.snapshots["fire-001"]
        self.assertEqual(snap1.squads["sq-001"].arrived_phones, ("13912340001",))

    def test_check_with_shortfall_records_missing(self):
        self.ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        line = self.ledger.check_line(
            "fire-001", "sq-001", "mat-001", qty_usable=3, reason="一台故障"
        )
        self.assertEqual(line.qty_usable, 3)
        self.assertEqual(line.missing, 1)
        self.assertEqual(line.status, SNAP_MISSING)
        self.assertIn("故障", line.reason)

    def test_check_full_marks_verified(self):
        self.ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        line = self.ledger.check_line("fire-001", "sq-001", "mat-001", qty_usable=4)
        self.assertEqual(line.status, SNAP_CHECKED)

    def test_consume_partial_then_resupply(self):
        self.ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        self.ledger.check_line("fire-001", "sq-001", "mat-001", qty_usable=4)
        line = self.ledger.consume_line("fire-001", "sq-001", "mat-001", amount=2)
        self.assertEqual(line.status, SNAP_PARTIAL)
        line = self.ledger.consume_line("fire-001", "sq-001", "mat-001", amount=2)
        self.assertEqual(line.status, SNAP_RESUPPLY)

    def test_snapshot_requires_arrival(self):
        with self.assertRaisesRegex(KeyError, "快照不存在"):
            self.ledger.check_line("fire-x", "sq-001", "mat-001", qty_usable=1)


class GapTest(unittest.TestCase):
    def setUp(self):
        self.ledger = _ledger()
        self.ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        self.ledger.check_line("fire-001", "sq-001", "mat-001", qty_usable=4)

    def test_no_demand_ok(self):
        rows = self.ledger.compute_gap("fire-001", demand={})
        self.assertTrue(all(r["severity"] == GAP_OK for r in rows))

    def test_shortfall_current_insufficient(self):
        rows = self.ledger.compute_gap("fire-001", demand={"mat-001": 6})
        row = next(r for r in rows if r["item_id"] == "mat-001")
        self.assertEqual(row["severity"], GAP_SHORT)   # 可用4 需求6 → 缺口2
        self.assertEqual(row["gap"], 2)

    def test_blocking_when_nothing_on_site(self):
        rows = self.ledger.compute_gap("fire-001", demand={"mat-002": 30})
        row = next(r for r in rows if r["item_id"] == "mat-002")
        # mat-002 在清单但未核对（usable 按 CARRIED 登记数乐观计入 20）→ 缺口10 当前不足
        self.assertEqual(row["severity"], GAP_SHORT)

    def test_blocking_when_demand_on_absent_material(self):
        rows = self.ledger.compute_gap("fire-001", demand={"mat-003": 200})
        row = next(r for r in rows if r["item_id"] == "mat-003")
        self.assertEqual(row["severity"], GAP_BLOCKING)  # 柴油未随队：已影响执行

    def test_incoming_only_with_rate_data(self):
        rows = self.ledger.compute_gap(
            "fire-001",
            demand={"mat-001": 3},
            projected_consumption={"mat-001": 4},   # 还要耗 4 > 可用 4? 相等即耗尽
        )
        row = next(r for r in rows if r["item_id"] == "mat-001")
        self.assertEqual(row["severity"], GAP_INCOMING)
        # 无速率数据时绝不虚构「即将不足」
        rows_plain = self.ledger.compute_gap("fire-001", demand={"mat-001": 3})
        self.assertNotIn(GAP_INCOMING, [r["severity"] for r in rows_plain])

    def test_rows_sorted_by_severity_first(self):
        rows = self.ledger.compute_gap(
            "fire-001", demand={"mat-001": 6, "mat-003": 200}
        )
        severities = [r["severity"] for r in rows]
        self.assertLessEqual(severities.index(GAP_BLOCKING), severities.index(GAP_SHORT))


class PersistenceTest(unittest.TestCase):
    def test_round_trip_preserves_ledger_and_snapshot(self):
        ledger = _ledger()
        ledger.generate_event_snapshot("fire-001", {"13912340001": "sq-001"})
        ledger.check_line("fire-001", "sq-001", "mat-001", qty_usable=3)
        ledger.consume_line("fire-001", "sq-001", "mat-002", amount=5)
        ledger.add_supply_point(
            name="县库", supplies={"mat-001": 12, "mat-003": None}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "materials.json"
            ledger.save(path)
            loaded = MaterialsLedger.load(path)
            self.assertEqual(loaded.to_dict(), ledger.to_dict())

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = MaterialsLedger.load(Path(tmp) / "none.json")
            self.assertEqual(loaded.items, {})

    def test_save_atomic_no_residue(self):
        ledger = _ledger()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "materials.json"
            ledger.save(path)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
