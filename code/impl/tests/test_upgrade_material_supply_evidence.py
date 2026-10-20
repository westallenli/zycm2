"""P08-05a: stock and company records remain read-only supply leads."""

import unittest

from panda_brain.material_supply_evidence import review_supply_leads
from panda_brain.materials import MaterialsLedger


class SupplyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = MaterialsLedger()
        self.ledger.upsert_item(name="高压水泵", unit="台")

    def test_unknown_zero_insufficient_and_closed_points_do_not_prove_supply(self):
        unknown = self.ledger.add_supply_point(name="未知库存", supplies={"mat-001": None})
        zero = self.ledger.add_supply_point(name="零库存", supplies={"mat-001": 0})
        short = self.ledger.add_supply_point(name="数量不足", supplies={"mat-001": 2})
        enough_on_paper = self.ledger.add_supply_point(name="账面数量足", supplies={"mat-001": 10})
        closed = self.ledger.add_supply_point(name="暂停开放", supplies={"mat-001": 10})
        closed.open_status = "关闭"
        before = self.ledger.to_dict()

        review = review_supply_leads(self.ledger, item_id="mat-001", required_quantity=3)
        by_id = {point.point_id: point for point in review.points}
        self.assertIn("库存数量未知或无效", by_id[unknown.point_id].blockers)
        self.assertIn("登记库存为零", by_id[zero.point_id].blockers)
        self.assertIn("登记数量不足", by_id[short.point_id].blockers)
        self.assertIn("库存来源及当前时效待核实", by_id[enough_on_paper.point_id].blockers)
        self.assertIn("物资点未登记为开放", by_id[closed.point_id].blockers)
        for point in review.points:
            self.assertIn("送达时间待核实", point.blockers)
        self.assertEqual("可补给性待核实", review.conclusion)
        self.assertEqual(before, self.ledger.to_dict())

    def test_positive_stock_and_load_time_are_still_not_delivery_evidence(self):
        point = self.ledger.add_supply_point(name="县库", supplies={"mat-001": 10})
        point.eta_load = "2026-09-30T13:00:00+08:00"
        result = review_supply_leads(self.ledger, item_id="mat-001")
        self.assertIn("正式缺口数量待核实", result.points[0].blockers)
        self.assertIn("送达时间待核实", result.points[0].blockers)
        self.assertEqual("本地维护（演示数据）", result.points[0].source)
        self.assertEqual(point.eta_load, result.points[0].eta_load)

    def test_company_listing_is_unmatched_unverified_lead(self):
        company = self.ledger.add_external_company(
            name="甲公司", source="模拟表", recorded_by="后勤甲", services="水带",
        )
        result = review_supply_leads(self.ledger, item_id="mat-001")
        self.assertEqual((company.company_id,), tuple(c.company_id for c in result.companies))
        self.assertIn("清单不证明该物资可供", result.companies[0].blockers)
        self.assertIn("供应能力与数量待联系核实", result.companies[0].blockers)

    def test_invalid_item_and_quantity_fail_closed(self):
        self.ledger.add_supply_point(name="县库", supplies={"mat-001": 10})
        self.assertEqual((), review_supply_leads(self.ledger, item_id="other").points)
        for quantity in (0, -1, float("nan"), True):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                review_supply_leads(self.ledger, item_id="mat-001", required_quantity=quantity)


if __name__ == "__main__":
    unittest.main()
