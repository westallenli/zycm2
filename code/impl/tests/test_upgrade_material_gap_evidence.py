"""P08-04b: formal shortage inputs are evidence-gated and read-only."""

import json
import unittest
from dataclasses import asdict, replace

from panda_brain import tactical_map as tm
from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.material_flows import MaterialFlowService
from panda_brain.captain_arrivals import ARRIVAL_REPORTED
from panda_brain.manual_material_reports import REPORTED
from panda_brain.materials import (
    EventMaterialsSnapshot, MaterialItem, MaterialsLedger, SnapshotLine, SquadSnapshot, Squad,
)
from panda_brain.models import ExecutionOrder, OrderStatus
from panda_brain.record_contracts import ObjectReference, ObjectRevision, RecordTimes
from panda_brain.workspace import ScenarioWorkspace


class MaterialGapEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace("gap-demo", event_id="fire-a")
        self.ledger = MaterialsLedger()
        self.ledger.items["pump"] = MaterialItem("pump", "水泵", unit="台")
        self.ledger.squads["sq-1"] = Squad("sq-1", "匿名队伍")
        arrival = ObjectRevision(
            ObjectReference("fire-a", "team_arrival", "sq-1", 1),
            RecordTimes(recorded_at="2026-09-30T08:00:00+08:00"),
            "队长报告", "reported", "captain", "arrival-req-1",
            json.dumps({"squad_id": "sq-1", "carry_baseline": {"pump": 10}, "arrived_phones": []}),
        )
        self.ledger.flow_records[arrival.id] = arrival
        self.ws.append_event(ARRIVAL_REPORTED, event_id="fire-a", squad_id="sq-1",
                             actor="captain", mode="initial", flow_record_id=arrival.id,
                             arrived_phones=[], data_kind="reported")
        self.ledger.snapshots["fire-a"] = EventMaterialsSnapshot(
            "fire-a", squads={"sq-1": SquadSnapshot(
                "sq-1", arrival_id=arrival.id,
                lines={"pump": SnapshotLine("pump", qty_registered=10, qty_usable=10)},
            )},
        )

    def record_loss(self, *args, **kwargs):
        record = MaterialFlowService(self.ledger, "fire-a").record_loss(*args, **kwargs)
        self.ws.append_event(REPORTED, event_id="fire-a", flow_record_id=record.id,
                             record=asdict(record), data_kind="reported")
        return record

    def assess(self, **changes):
        args = dict(event_id="fire-a", item_id="pump", order_id="order-1")
        args.update(changes)
        return assess_gap_inputs(self.ws, self.ledger, **args)

    def confirmed_task(self, *, audited=True, version=1):
        self.ws.orders["order-1"] = ExecutionOrder(
            "order-1", "decision-1", "option-1", "北段扑救",
            status=OrderStatus.CONFIRMED, confirmed_by="commander",
        )
        flow = tm.CommandFlow(order_id="order-1", event_id="fire-a",
                              content_version=version, target="北段扑救")
        self.ws.command_flows["order-1"] = flow
        if audited:
            self.ws.append_event("CommandFlowOpened", order_id="order-1",
                                 event_id="fire-a", flow=asdict(flow))

    def test_event_boundary_fails_closed_without_inventory_or_task_leak(self):
        self.confirmed_task()
        result = self.assess(event_id="fire-b")
        self.assertIsNone(result.task_ref)
        self.assertEqual((), result.arrival_refs)
        self.assertIsNone(result.book_available)
        self.assertEqual(("事件归属或审计链待核对",), result.missing)

    def test_unconfirmed_or_unaudited_task_is_not_accepted(self):
        self.confirmed_task(audited=False)
        self.assertIsNone(self.assess().task_ref)
        self.ws.append_event("CommandFlowOpened", order_id="order-1",
                             event_id="fire-a", flow=asdict(self.ws.command_flows["order-1"]))
        self.ws.orders["order-1"].status = OrderStatus.DRAFT
        self.assertIsNone(self.assess().task_ref)

    def test_audited_task_and_arrival_are_inputs_but_not_formal_gap(self):
        self.confirmed_task(version=2)
        before_events = self.ws.events
        before_ledger = self.ledger.to_dict()
        result = self.assess()
        self.assertEqual("order-1@2", result.task_ref)
        self.assertEqual((next(iter(self.ledger.flow_records)),), result.arrival_refs)
        self.assertEqual(10, result.registered_on_site)
        self.assertEqual(10, result.book_available)
        self.assertIn("经专业核定的物资用量依据", result.missing)
        self.assertEqual("待核实", result.current_status)
        self.assertEqual("待核实", result.projected_status)
        self.assertIsNone(result.current_gap)
        self.assertIsNone(result.projected_gap)
        self.assertEqual(before_events, self.ws.events)
        self.assertEqual(before_ledger, self.ledger.to_dict())

    def test_losses_change_book_amount_but_no_precise_shortage(self):
        self.confirmed_task()
        consumed = self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 1),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=3,
            expected_version=0, actor_id="captain", source="队长报告",
            request_id="loss-req-1",
        )
        damaged = self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-2", 1),
            squad_id="sq-1", item_id="pump", kind="damaged", amount=2,
            expected_version=0, actor_id="captain", source="队长报告",
            request_id="loss-req-2",
        )
        result = self.assess()
        self.assertEqual(5, result.book_available)
        self.assertEqual({consumed.id, damaged.id}, set(result.loss_refs))
        self.assertIsNone(result.current_gap)
        self.assertIsNone(result.projected_gap)

        corrected = self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 2),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=1,
            expected_version=1, actor_id="captain", source="队长更正",
            request_id="loss-req-3", reason="首报数量有误",
        )
        result = self.assess()
        self.assertEqual(7, result.book_available)
        self.assertEqual({consumed.id, corrected.id, damaged.id}, set(result.loss_refs))

    def test_unbacked_snapshot_loss_and_revision_gap_hide_book_amount(self):
        self.confirmed_task()
        line = self.ledger.snapshots["fire-a"].squads["sq-1"].lines["pump"]
        line.consumed = 3
        result = self.assess()
        self.assertIsNone(result.book_available)
        self.assertEqual((), result.loss_refs)
        self.assertIn("调拨、撤离与数量守恒来源待核对", result.missing)

        line.consumed = 0  # Repair the deliberately unbacked snapshot before a new report.
        first = self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 1),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=3,
            expected_version=0, actor_id="captain", source="队长报告",
            request_id="loss-req-1",
        )
        self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 2),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=1,
            expected_version=1, actor_id="captain", source="队长更正",
            request_id="loss-req-2", reason="数量更正",
        )
        del self.ledger.flow_records[first.id]
        self.assertIsNone(self.assess().book_available)

    def test_correction_cannot_change_report_ownership(self):
        self.confirmed_task()
        self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 1),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=3,
            expected_version=0, actor_id="captain", source="队长报告",
            request_id="loss-req-1",
        )
        corrected = self.record_loss(
            ObjectReference("fire-a", "material_loss", "loss-1", 2),
            squad_id="sq-1", item_id="pump", kind="consumed", amount=1,
            expected_version=1, actor_id="captain", source="队长更正",
            request_id="loss-req-2", reason="数量更正",
        )
        changed = json.loads(corrected.content_json)
        changed["kind"] = "damaged"
        self.ledger.flow_records[corrected.id] = replace(
            corrected, content_json=json.dumps(changed),
        )
        self.assertIsNone(self.assess().book_available)
        self.assertIn("调拨、撤离与数量守恒来源待核对", self.assess().missing)

    def test_legacy_arrival_and_missing_item_stay_pending(self):
        self.confirmed_task()
        self.ledger.snapshots["fire-a"].squads["sq-1"].arrival_id = ""
        result = self.assess(item_id="unknown")
        self.assertEqual((), result.arrival_refs)
        self.assertIsNone(result.book_available)
        self.assertIn("物资档案", result.missing)
        self.assertIn("到场物资快照的版本化来源", result.missing)

    def test_mismatched_arrival_baseline_is_not_traceable(self):
        self.confirmed_task()
        line = self.ledger.snapshots["fire-a"].squads["sq-1"].lines["pump"]
        line.qty_registered = 11
        result = self.assess()
        self.assertEqual((), result.arrival_refs)
        self.assertIsNone(result.book_available)
        self.assertIn("到场物资快照的版本化来源", result.missing)


if __name__ == "__main__":
    unittest.main()
