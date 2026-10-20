"""P16-T21 验收：全程验收主线十项（doc 11 §8）对象级取证。

十项检查全部从真实战役跑链产物读取事实；本测试保证十项持续通过、
报告确定性可复现、Markdown 记录完整。
"""

from __future__ import annotations

import unittest

from panda_brain.acceptance import run_ten_item_acceptance
from panda_brain.campaign_runner import run_campaign


class TenItemAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_campaign()
        cls.report = run_ten_item_acceptance(cls.result)

    def test_all_ten_items_pass(self) -> None:
        failed = [f"{i.no}.{i.title}" for i in self.report.items if not i.passed]
        self.assertEqual([], failed)
        self.assertEqual(10, len(self.report.items))
        self.assertTrue(self.report.all_passed)

    def test_every_item_carries_object_evidence(self) -> None:
        for item in self.report.items:
            self.assertTrue(item.evidence, f"第{item.no}项无证据行")

    def test_report_is_deterministic_across_runs(self) -> None:
        again = run_ten_item_acceptance(self.result)
        self.assertEqual(
            [(i.no, i.title, i.passed, i.evidence) for i in self.report.items],
            [(i.no, i.title, i.passed, i.evidence) for i in again.items],
        )

    def test_markdown_record_shape(self) -> None:
        md = self.report.markdown("2026-08-24T20:00:00+08:00")
        self.assertIn("# 验收记录：全程验收主线十项（规范 11·§8）", md)
        self.assertIn("hanjiang-72h", md)
        self.assertIn("十项全部通过", md)
        for no in range(1, 11):
            self.assertIn(f"## {no}. ", md)
        # 关键证据词抽查
        self.assertIn("recon-alpha", md)
        workspace = self.result.workspace
        paused = next(e for e in workspace.events if e.kind == 'OrderAnomalyPaused')
        order_id = paused.payload['order_id']
        self.assertIn(order_id, workspace.orders)
        self.assertIn(order_id, md)
        failed_verification = next(v for v in workspace.verifications.values() if not v.passed)
        self.assertIn(failed_verification.id, md)


if __name__ == "__main__":
    unittest.main()
