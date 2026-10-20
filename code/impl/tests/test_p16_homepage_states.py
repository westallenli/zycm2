"""P16-A3 验收：首页名册面板离场/撤离呈现 + 角标灰对齐 00 号色板。

口径：02 号 §3.3 流转（已到场 → 离场/撤离 → 已撤离）+ doc 04 §4.1 八态
判定态如实叠加；预编入/待到场仍显示「未到场」（02 号 §3.2 词汇不变）。
角标「无人」灰由 #8A97A5 偏差值改齐色板唯一灰 #647382。
"""

from __future__ import annotations

import unittest

from panda_brain.fire_events import EventRegistry
from panda_brain.homepage import (
    _org_css,
    attendance_by_slot,
    person_rows_for_group,
    render_homepage_view,
    viewer_detail,
)
from panda_brain.models import PersonRecord
from panda_brain.personnel_states import mark_person_state
from panda_brain.workspace import ScenarioWorkspace

_LIBRARY = [
    PersonRecord(
        name="张三", phone="13912340001", unit="县防火办",
        home_position="扑救一组（一分区）", is_leader=True,
    ),
    PersonRecord(name="李四", phone="13912340002", unit="林场的护站", home_position=""),
    PersonRecord(name="王五", phone="13912340003", unit="县消防队", home_position="扑救督导"),
]


class HomepageStatesFixture(unittest.TestCase):
    """公共夹具：名册三人，张三已签到 pjjz-1。"""

    def setUp(self):
        self.registry = EventRegistry()
        self.registry.import_person_library(_LIBRARY)
        record = self.registry.create_event(
            name="汉江上游山火",
            report_no="HC-A3",
            discovered_at="2026-08-25T08:00:00+08:00",
            source="页面录入",
            location="青岗县",
        )
        self.event_id = record.event_id
        self.registry.add_roster_entry(self.event_id, "13912340001")   # → pjjz-1
        self.registry.add_roster_entry(self.event_id, "13912340002")   # 岗位未解析
        self.registry.add_roster_entry(self.event_id, "13912340003")   # → supervisor
        self.workspace = ScenarioWorkspace("p16-a3")
        self.workspace.import_person_library(_LIBRARY)
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")

    def rows(self):
        return {
            r["name"]: r
            for r in person_rows_for_group(self.registry, self.workspace, self.event_id)
        }

    def render(self, **overrides):
        kwargs = dict(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            arrivals={"13912340001": "2026-08-25T09:00:00+08:00"},
            generated_at="2026-08-25T10:00:00+08:00",
        )
        kwargs.update(overrides)
        return render_homepage_view(**kwargs)


class DeparturePresentationTest(HomepageStatesFixture):
    def test_checkout_shows_departed_with_basis(self):
        self.workspace.check_out("pjjz-1", phone="13912340001")
        row = self.rows()["张三"]
        self.assertEqual("已撤离", row["status_word"])
        self.assertIn("PersonCheckedOut", row["state_basis"])
        html = self.render()
        self.assertIn(">已撤离</span>", html)
        self.assertIn("依据：", html)   # 芯片 title 携带依据

    def test_checkout_drops_from_arrived_and_annotates_missing_list(self):
        before = attendance_by_slot(self.registry, self.workspace, self.event_id)
        self.assertEqual(1, before["totals"]["arrived"])
        self.workspace.check_out("pjjz-1", phone="13912340001")
        summary = attendance_by_slot(self.registry, self.workspace, self.event_id)
        self.assertEqual(0, summary["totals"]["arrived"])   # 已撤离不计实到
        slot = next(s for s in summary["slots"] if s["pos_code"] == "pjjz-1")
        self.assertIn("张三（已撤离）", slot["missing_names"])

    def test_commander_marks_overlay_on_home_rows(self):
        cases = {
            "standby": ("暂离/轮休", "#B77A1A"),
            "lost_contact": ("失联", "#B84A42"),
            "returned_to_unit": ("已归建", "#2F7D68"),
        }
        for action, (word, tone) in cases.items():
            with self.subTest(action=action):
                fresh = ScenarioWorkspace("p16-a3-m")
                fresh.import_person_library(_LIBRARY)
                fresh.scan_check_in("pjjz-1", phone="13912340001")
                mark_person_state(
                    fresh, action, phone="13912340001", operator="指挥员")
                rows = {
                    r["name"]: r
                    for r in person_rows_for_group(
                        self.registry, fresh, self.event_id)
                }
                self.assertEqual(word, rows["张三"]["status_word"])
                html = render_homepage_view(
                    event_record=self.registry.events[self.event_id],
                    registry=self.registry,
                    workspace=fresh,
                    event_id=self.event_id,
                    generated_at="2026-08-25T10:00:00+08:00",
                )
                self.assertIn(f">{word}</span>", html)

    def test_plain_vocabulary_unchanged_for_base_states(self):
        rows = self.rows()
        self.assertEqual("已到场", rows["张三"]["status_word"])   # 刷码绑定
        self.assertEqual("未到场", rows["李四"]["status_word"])   # 名册岗位未解析
        self.assertEqual("未到场", rows["王五"]["status_word"])   # 名册已指派待刷码


class BadgePaletteTest(unittest.TestCase):
    def test_badge_none_grey_aligned_to_palette(self):
        css = _org_css()
        self.assertIn(".hp-badge-none{background:#647382;}", css)
        self.assertNotIn("8A97A5", css)


class MemberSelfCardTest(HomepageStatesFixture):
    def test_me_card_shows_departed_not_not_arrived(self):
        self.workspace.check_out("pjjz-1", phone="13912340001")
        detail = viewer_detail(
            self.registry, self.workspace, viewer_phone="13912340001")
        self.assertEqual("已撤离", detail["state_word"])
        html = self.render(
            viewer_role="member", viewer_phone="13912340001")
        self.assertIn(">已撤离</span>", html)

    def test_active_member_keeps_binary_word(self):
        detail = viewer_detail(
            self.registry, self.workspace, viewer_phone="13912340001")
        self.assertEqual("", detail["state_word"])   # 无判定/离岗事实 → 二元口径
        self.assertTrue(detail["bound"])


class DeterminismTest(HomepageStatesFixture):
    def test_render_pure_given_same_facts(self):
        self.workspace.check_out("pjjz-1", phone="13912340001")
        a = self.render(viewer_role="commander")
        b = self.render(viewer_role="commander")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
