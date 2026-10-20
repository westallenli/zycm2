"""P15 T1 — force organization: template, QR check-in, org chart, posture, persistence."""

from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from panda_brain.models import PersonRecord
from panda_brain.org_model import (
    COMMAND_NODES,
    POSITION_SLOTS,
    SLOT_INDEX,
    mask_phone,
    normalize_phone,
    template_summary,
)
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace


def _person(name: str, phone: str, *, leader: bool = False, unit: str = "演练大队") -> PersonRecord:
    return PersonRecord(name=name, phone=phone, unit=unit, home_position="扑救员", is_leader=leader)


class OrgTemplateTest(unittest.TestCase):
    def test_template_matches_doctrine_chart(self):
        summary = template_summary()
        # 29 line groups counted by the phase-2 PRD + 3 news subgroups on the chart.
        self.assertEqual(summary["slots"], 32)
        self.assertEqual(summary["nodes"], len(COMMAND_NODES))
        domains = Counter(slot.domain for slot in POSITION_SLOTS)
        self.assertEqual(
            dict(domains),
            {"coordination": 1, "news": 3, "tech": 8, "ops": 8, "security": 4, "logistics": 8},
        )
        codes = [slot.pos_code for slot in POSITION_SLOTS]
        self.assertEqual(len(codes), len(set(codes)), "岗位编码必须唯一")

    def test_ops_branch_carries_assault_air_reserve(self):
        kinds = Counter(slot.kind for slot in POSITION_SLOTS if slot.domain == "ops")
        self.assertEqual(kinds["assault"], 4)   # 扑救一组~四组（一分区~四分区）
        self.assertEqual(kinds["air"], 1)       # 空中作业组
        self.assertEqual(kinds["reserve"], 1)   # 机动支援组（预备队）
        for code in ("pjjz-1", "pjjz-2", "pjjz-3", "pjjz-4", "air-op", "mobile-support"):
            self.assertIn(code, SLOT_INDEX)

    def test_phone_normalization_and_masking(self):
        self.assertEqual(normalize_phone("+86 139-1234-2876"), "13912342876")
        with self.assertRaises(ValueError):
            normalize_phone("not-a-phone")
        self.assertEqual(mask_phone("13912342876"), "139****2876")
        self.assertEqual(mask_phone("12345"), "***")


class ScanCheckInTest(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace("p15-org-drill")
        self.ws.import_person_library(
            [
                _person("张三", "13900000001", leader=True),
                _person("李四", "13900000002"),
                _person("王五", "13900000003"),
                _person("赵六", "13900000004"),
                _person("钱七", "13900000005", leader=True),
                _person("孙八", "13900000006"),
            ]
        )

    def test_unknown_slot_rejected(self):
        with self.assertRaises(KeyError):
            self.ws.scan_check_in("no-such-slot", phone="13900000001")

    def test_unregistered_phone_told_to_register_first(self):
        with self.assertRaises(KeyError) as ctx:
            self.ws.scan_check_in("pjjz-1", phone="19999999999")
        self.assertIn("未登记常备人员库", str(ctx.exception))

    def test_check_in_binds_person_and_lands_in_org_chart(self):
        binding = self.ws.scan_check_in("pjjz-1", phone="13900000001")
        self.assertEqual(binding.status, "active")
        chart = self.ws.org_chart()
        ops_node = next(n for n in chart["nodes"] if n["node_code"] == "dep-ops")
        slot = next(s for s in ops_node["slots"] if s["pos_code"] == "pjjz-1")
        self.assertEqual(slot["filled"], 1)
        member = slot["members"][0]
        self.assertEqual(member["name"], "张三")
        self.assertEqual(member["phone"], "13900000001")  # 指挥模式可见全号
        self.assertTrue(member["is_leader"])
        self.assertEqual(chart["total_members"], 1)
        kinds = [e.kind for e in self.ws.events]
        self.assertIn("PersonCheckedIn", kinds)
        self.assertEqual(self.ws.verify_audit_chain(), ())

    def test_duplicate_report_rejected_one_person_one_position(self):
        self.ws.scan_check_in("pjjz-1", phone="13900000002")
        with self.assertRaises(ValueError) as ctx:
            self.ws.scan_check_in("pjjz-2", phone="13900000002")
        self.assertIn("一人一岗", str(ctx.exception))

    def test_leader_conflict_rejected(self):
        self.ws.scan_check_in("pjjz-3", phone="13900000005")
        with self.assertRaises(ValueError) as ctx:
            self.ws.scan_check_in("pjjz-3", phone="13900000001")
        self.assertIn("队长", str(ctx.exception))

    def test_capacity_full_rejected(self):
        for index, phone in enumerate(
            ("13900000002", "13900000003", "13900000004", "13900000006"), start=1
        ):
            self.ws.scan_check_in("supervisor", phone=phone)
        ws2 = ScenarioWorkspace("p15-capacity")
        ws2.import_person_library([_person(f"队员{index}", f"1380000000{index}") for index in range(1, 6)])
        for index in range(1, 5):  # supervisor 上限 4
            ws2.scan_check_in("supervisor", phone=f"1380000000{index}")
        with self.assertRaises(ValueError) as ctx:
            ws2.scan_check_in("supervisor", phone="13800000005")
        self.assertIn("岗位已满", str(ctx.exception))

    def test_check_out_then_rebind(self):
        first = self.ws.scan_check_in("guide", phone="13900000002")
        departed = self.ws.check_out("guide", phone="13900000002")
        self.assertEqual(departed.status, "checked_out")
        self.assertIsNotNone(departed.checked_out_at_version)
        chart = self.ws.org_chart()
        guide_node = next(n for n in chart["nodes"] if n["node_code"] == "dep-ops")
        slot = next(s for s in guide_node["slots"] if s["pos_code"] == "guide")
        self.assertEqual(slot["filled"], 0)  # 节点回到灰色空缺
        rebound = self.ws.scan_check_in("guide", phone="13900000003")
        self.assertNotEqual(rebound.id, first.id)
        self.assertIn("PersonCheckedOut", [e.kind for e in self.ws.events])


class ForcePostureTest(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace("p15-posture")
        self.ws.import_person_library(
            [_person(f"队员{index}", f"137000000{index:02d}") for index in range(1, 10)]
        )

    def test_reserve_floor_advisory(self):
        self.ws.scan_check_in("pjjz-1", phone="13700000001")
        report = self.ws.force_posture_report()
        self.assertEqual(report["committed"], 1)
        self.assertFalse(report["reserve_ratio_ok"])  # 有投入无预备
        self.assertTrue(any("预备队不足" in note for note in report["notes"]))
        # 一线 2 人 → 至少 ceil(0.6)=1 人预备
        self.ws.scan_check_in("pjjz-2", phone="13700000002")
        report = self.ws.force_posture_report()
        self.assertEqual(report["required_reserve_min"], 1)
        self.ws.scan_check_in("mobile-support", phone="13700000003")
        report = self.ws.force_posture_report()
        self.assertTrue(report["reserve_ratio_ok"])

    def test_aux_ratio_advisory(self):
        self.ws.scan_check_in("pjjz-1", phone="13700000001")
        self.ws.scan_check_in("mobile-support", phone="13700000002")
        report = self.ws.force_posture_report()
        self.assertFalse(report["aux_ratio_ok"])  # 作战2 辅助0，远低于 1:3

    def test_empty_roster_is_neutral(self):
        report = self.ws.force_posture_report()
        self.assertEqual(report["committed"], 0)
        self.assertTrue(report["reserve_ratio_ok"])
        self.assertTrue(report["aux_ratio_ok"])


class RosterPersistenceTest(unittest.TestCase):
    def test_snapshot_roundtrip_preserves_library_and_bindings(self):
        ws = ScenarioWorkspace("p15-persist")
        ws.import_person_library(
            [
                _person("张三", "13900000001", leader=True),
                _person("李四", "13900000002"),
                _person("王五", "13900000003"),
            ]
        )
        ws.scan_check_in("pjjz-1", phone="13900000001")
        ws.scan_check_in("pjjz-1", phone="13900000002")
        ws.scan_check_in("log-medical", phone="13900000003")
        before_chart = ws.org_chart()
        before_posture = ws.force_posture_report()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "snapshot.db"
            save_workspace(ws, db_path)
            restored = load_workspace(db_path)

        self.assertEqual(set(restored.person_library), set(ws.person_library))
        self.assertEqual(
            restored.person_library["13900000001"].name,
            "张三",
        )
        self.assertEqual(set(restored.roster_bindings), set(ws.roster_bindings))
        self.assertEqual(restored.org_chart(), before_chart)
        self.assertEqual(restored.force_posture_report(), before_posture)
        self.assertEqual(restored.verify_audit_chain(), ())


if __name__ == "__main__":
    unittest.main()
