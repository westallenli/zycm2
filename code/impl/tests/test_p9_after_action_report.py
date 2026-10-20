"""P9-T3 acceptance tests: deterministic after-action report."""

from __future__ import annotations
import unittest

from panda_brain.after_action_report import render_after_action_report
from panda_brain.campaign_runner import run_campaign


class P9AfterActionReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_campaign()
        cls.workspace = cls.result.workspace
        assert cls.workspace is not None

    def test_same_workspace_renders_byte_identical(self) -> None:
        first = render_after_action_report(self.workspace, title="战役级演练报告")
        second = render_after_action_report(self.workspace, title="战役级演练报告")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("# 战役级演练报告\n"))

    def test_report_contains_all_fixed_sections_and_facts(self) -> None:
        text = render_after_action_report(self.workspace, title="战役级演练报告")
        for marker in (
            "## 一、决断与执行时间线",
            "## 二、各日复盘发现",
            "## 三、经验沉淀（受控记忆）",
            "## 四、编排质量旗标",
            "## 五、收束态势",
            "审计链：完整",
        ):
            self.assertIn(marker, text)
        # The drill's key facts appear on the record.
        self.assertIn("**timeout**", text)
        self.assertIn("异常暂停", text)
        self.assertIn("最终独立核验：**通过**", text)
        self.assertIn("fire:trend = `controlled`（verified）", text)
        # Event-payload keys must render as real ids, never placeholder None.
        self.assertNotIn("`None`", text)
        self.assertIn("起草 `order-", text)
        self.assertIn("审批 `order-", text)

    def test_empty_workspace_report_is_well_formed(self) -> None:
        from panda_brain.workspace import ScenarioWorkspace

        workspace = ScenarioWorkspace("empty-drill")
        text = render_after_action_report(workspace, title="空演练")
        self.assertIn("（本战役无决断记录）", text)
        self.assertIn("（无复盘记录）", text)


if __name__ == "__main__":
    unittest.main()
