"""P16 T3 验收：首页组织架构原图渲染（02 号定版：原图完整显示 + 动态叠加侧栏）。"""

from __future__ import annotations

import unittest

from panda_brain.fire_events import EventRegistry
from panda_brain.homepage import (
    COMM_PENDING_NOTE,
    attendance_by_slot,
    mask_for_viewer,
    person_rows_for_group,
    render_homepage_view,
    viewer_detail,
)
from panda_brain.models import PersonRecord
from panda_brain.org_model import mask_phone
from panda_brain.workspace import ScenarioWorkspace

_LIBRARY = [
    PersonRecord(
        name="张三", phone="13912340001", unit="县防火办",
        home_position="扑救一组（一分区）", is_leader=True,
    ),
    PersonRecord(name="李四", phone="13912340002", unit="林场的护站", home_position=""),
    PersonRecord(name="王五", phone="13912340003", unit="县消防队", home_position="扑救督导"),
]

_ARRIVALS = {"13912340001": "2026-08-23T10:00:00+08:00"}


class HomepageFixture(unittest.TestCase):
    """公共夹具：注册表（事件+名册）与已入场的工作区（张三已签到 pjjz-1）。"""

    def setUp(self):
        self.registry = EventRegistry()
        self.registry.import_person_library(_LIBRARY)
        record = self.registry.create_event(
            name="汉江上游山火",
            report_no="HC-01",
            discovered_at="2026-08-23T08:00:00+08:00",
            source="页面录入",
            location="青岗县",
        )
        self.event_id = record.event_id
        self.registry.add_roster_entry(self.event_id, "13912340001")   # → pjjz-1
        self.registry.add_roster_entry(self.event_id, "13912340002")   # 岗位未解析
        self.registry.add_roster_entry(self.event_id, "13912340003")   # → supervisor

        self.workspace = ScenarioWorkspace("p16-home")
        self.workspace.import_person_library(_LIBRARY)
        self.workspace.scan_check_in("pjjz-1", phone="13912340001")

    def render(self, **overrides):
        kwargs = dict(
            event_record=self.registry.events[self.event_id],
            registry=self.registry,
            workspace=self.workspace,
            event_id=self.event_id,
            arrivals=_ARRIVALS,
            generated_at="2026-08-23T10:05:00+08:00",
        )
        kwargs.update(overrides)
        return render_homepage_view(**kwargs)


class MaskRuleTest(unittest.TestCase):
    """doc 02 §4.1 脱敏矩阵：指挥员全显 / 本人全显 / 他人打码 / 公共屏全遮。"""

    def test_commander_sees_full(self):
        self.assertEqual(
            mask_for_viewer("13912340002", viewer_role="commander"), "13912340002"
        )

    def test_member_self_full_other_masked(self):
        self.assertEqual(
            mask_for_viewer("13912340001", viewer_role="member", viewer_phone="13912340001"),
            "13912340001",
        )
        other = mask_for_viewer(
            "13912340002", viewer_role="member", viewer_phone="13912340001"
        )
        self.assertEqual(other, mask_phone("13912340002"))
        self.assertIn("*", other)

    def test_public_fully_masked_no_digits(self):
        shown = mask_for_viewer("13912340002", viewer_role="public")
        self.assertNotIn("13912340002", shown)
        self.assertTrue(all(ch.isdigit() is False for ch in shown))


class AttendanceAggregationTest(HomepageFixture):
    def test_totals_expected_vs_arrived(self):
        summary = attendance_by_slot(self.registry, self.workspace, self.event_id)
        self.assertTrue(summary["configured"])
        self.assertEqual(summary["totals"], {"expected": 2, "arrived": 1})
        self.assertEqual(summary["unassigned"], 1)  # 李四岗位未解析

    def test_per_slot_missing_names(self):
        summary = attendance_by_slot(self.registry, self.workspace, self.event_id)
        by_code = {slot["pos_code"]: slot for slot in summary["slots"]}
        self.assertAlmostEqual(by_code["pjjz-1"]["rate"], 1.0)
        self.assertEqual(by_code["pjjz-1"]["missing_names"], [])
        self.assertEqual(by_code["supervisor"]["missing_names"], ["王五"])
        self.assertAlmostEqual(by_code["supervisor"]["rate"], 0.0)

    def test_out_of_roster_binding_flagged_with_reason(self):
        # 李四在名册但岗位未解析；若被直接绑到槽位，须如实标「名册岗位未解析却已绑定」
        self.workspace.scan_check_in("log-water", phone="13912340002")
        summary = attendance_by_slot(self.registry, self.workspace, self.event_id)
        reasons = {(e["name"], e["reason"]) for e in summary["out_of_roster"]}
        self.assertIn(("李四", "名册岗位未解析却已绑定"), reasons)

    def test_binding_not_on_roster_flagged(self):
        from panda_brain.models import PersonRecord

        stranger = PersonRecord(name="赵六", phone="13912349999", unit="外队")
        self.workspace.import_person_library([stranger])
        self.workspace.scan_check_in("pjjz-2", phone="13912349999")
        summary = attendance_by_slot(self.registry, self.workspace, self.event_id)
        reasons = [e["reason"] for e in summary["out_of_roster"]]
        self.assertIn("不在本事件名册", reasons)


class PersonRowsTest(HomepageFixture):
    def test_rows_union_and_status(self):
        rows = person_rows_for_group(self.registry, self.workspace, self.event_id)
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_name["张三"]["status_word"], "已到场")
        self.assertEqual(by_name["李四"]["status_word"], "未到场")
        self.assertEqual(by_name["王五"]["slot_name"], "扑救督导组")

    def test_rows_sorted_by_name_deterministic(self):
        again = person_rows_for_group(self.registry, self.workspace, self.event_id)
        self.assertEqual(
            [r["name"] for r in person_rows_for_group(self.registry, self.workspace, self.event_id)],
            [r["name"] for r in again],
        )


class ViewerDetailTest(HomepageFixture):
    def test_bound_member_detail(self):
        detail = viewer_detail(
            self.registry, self.workspace,
            viewer_phone="13912340001", arrivals=_ARRIVALS,
        )
        self.assertTrue(detail["bound"])
        self.assertEqual(detail["slot_name"], "扑救一组（一分区）")
        self.assertIn("灭火指挥调度", detail["group"])
        self.assertEqual(detail["arrival"], _ARRIVALS["13912340001"])
        self.assertIn("待接入", detail["comm"])

    def test_unknown_identity_returns_none(self):
        self.assertIsNone(
            viewer_detail(self.registry, self.workspace, viewer_phone="13999999999")
        )

    def test_unbound_member_shows_not_arrived(self):
        detail = viewer_detail(
            self.registry, self.workspace, viewer_phone="13912340003"
        )
        self.assertFalse(detail["bound"])
        self.assertEqual(detail["arrival"], "")


class RenderTest(HomepageFixture):
    """渲染层：自绘结构完整、空态诚实、脱敏贯穿、只读确定。"""

    def test_self_drawn_structure_complete_without_image(self):
        html = self.render()
        # P17-P2：零图片依赖；结构与连线全部自绘
        self.assertNotIn("<img", html)
        self.assertNotIn("org_chart", html)
        self.assertIn("<svg", html)                       # 连线层
        for label in (
            "联合指挥部/工作组", "前线总指挥", "调度长（综合协调）",
            "新闻发言人", "副总指挥·专业技术支持", "副总指挥·灭火指挥调度",
            "副总指挥·火场维稳管控", "副总指挥·后勤综合保障",
            "扑救一组（一分区）", "医疗组", "舆情研判组",
        ):
            self.assertIn(label, html)
        self.assertIn("aria-label=\"放大\"", html)       # 缩放控制（移动端）

    def test_attendance_summary_rendered(self):
        html = self.render()
        self.assertIn("到场总览", html)
        self.assertIn("实到 <b>1</b>", html)
        self.assertIn("应到 <b>2</b>", html)
        self.assertIn("王五", html)          # 未到场名单点名
        self.assertIn("岗位未解析", html)     # 李四待指派提示

    def test_roster_missing_empty_state_honest(self):
        fresh = self.registry.create_event(
            name="空名册事件", report_no="R-X",
            discovered_at="2026-08-23T09:00:00+08:00", source="", location="L",
        )
        html = self.render(event_id=fresh.event_id)
        self.assertIn("本事件名册未配置", html)

    def test_member_self_card_full_phone_and_arrival(self):
        html = self.render(viewer_role="member", viewer_phone="13912340001")
        self.assertIn("我的岗位信息", html)
        self.assertIn("13912340001", html)   # 本人完整号码
        self.assertIn("到场时间", html)

    def test_member_view_masks_others_phones(self):
        html = self.render(viewer_role="member", viewer_phone="13912340001")
        self.assertNotIn("13912340002", html)
        self.assertNotIn("13912340003", html)
        self.assertIn(mask_phone("13912340002"), html)

    def test_commander_view_shows_all_phones(self):
        html = self.render(viewer_role="commander")
        self.assertIn("13912340002", html)
        self.assertIn("13912340003", html)

    def test_comm_pending_note_always_present_not_faked(self):
        html = self.render()
        self.assertIn(COMM_PENDING_NOTE, html)
        self.assertNotIn("在线", html.replace("未接入", ""))  # 不虚构通信状态词

    def test_no_ai_self_narration_box(self):
        html = self.render()
        self.assertNotIn("智脑正在", html)
        self.assertNotIn("参谋部正在收集", html)

    def test_pure_and_deterministic(self):
        version_before = self.workspace.version
        first = self.render()
        second = self.render()
        self.assertEqual(first, second)
        self.assertEqual(self.workspace.version, version_before)


if __name__ == "__main__":
    unittest.main()
