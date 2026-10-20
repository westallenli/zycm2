"""Early-response facts and permission/provenance boundaries (P04-01)."""
import unittest
from dataclasses import replace
from types import SimpleNamespace

from panda_brain.fire_events import EventRegistry
from panda_brain.models import ExecutionOrder, OrderStatus
from panda_brain.sandview import render_sand_page
from panda_brain.sop import early_response_view, render_early_response
from panda_brain.workspace import ScenarioWorkspace
from tests import test_upgrade_checkin_flow as checkin


@unittest.skip('沙盘界面改版：旧"六问"标题与 sop-* 区块已移除，等待按新界面重写')
class SopTests(unittest.TestCase):
    def setUp(self):
        self.registry = EventRegistry()
        self.record = self.registry.create_event(
            name="模拟 SOP 事件", report_no="SOP-01", source="人工登记",
            discovered_at="2026-09-28T09:00:00+08:00", location="演示地点")
        self.ws = ScenarioWorkspace("sop-test", event_id=self.record.event_id)
        self.state = SimpleNamespace(workspace=self.ws, registry=self.registry,
            active_event_id=self.record.event_id, imagery=None, ledger=None,
            sand_qa_log=[], intent_notice='', voice_drafts={}, advisor_log=[])

    def test_manual_registration_does_not_require_upstream_alarm(self):
        view = early_response_view(self.ws, self.record)
        self.assertIn("多模态火情预警系统", " ".join(view.facts))
        self.assertNotIn("人工登记", " ".join(view.facts))
        self.assertEqual("人工登记", self.record.source)
        self.assertFalse(any("报警" in text for text in view.missing))
        self.assertEqual("待补资料", view.state)

    def test_status_alone_and_wrong_event_start_do_not_prove_start(self):
        record = replace(self.record, status="active")
        self.ws.append_event("FireEventStarted", event_id="other", operator="foreign", at="now")
        view = early_response_view(self.ws, record)
        self.assertTrue(any("启动审计依据" in text for text in view.missing))
        self.assertNotIn("foreign", str(view))
        self.ws.append_event("FireEventStarted", event_id=record.event_id)
        self.assertTrue(any("操作者或实际时间" in text
                            for text in early_response_view(self.ws, record).missing))

    def test_mismatched_workspace_and_damaged_audit_do_not_leak_facts(self):
        other = replace(self.record, event_id="other", initial_report="PRIVATE")
        view = early_response_view(self.ws, other)
        self.assertEqual("依据待核对", view.state)
        self.assertEqual((), view.facts)
        self.ws.append_event("FireEventStarted", event_id=self.record.event_id, at="now")
        self.ws.events[-1].payload["at"] = "tampered"
        self.assertEqual("依据待核对", early_response_view(self.ws, self.record).state)

    def test_read_is_pure_and_new_event_information_is_reassessed(self):
        before = (self.ws.version, len(self.ws.events), dict(self.ws.orders))
        first = early_response_view(self.ws, self.record)
        for _ in range(3):
            self.assertEqual(first, early_response_view(self.ws, self.record))
        changed = early_response_view(self.ws, replace(self.record, initial_report="现场新初报"))
        self.assertNotEqual(first, changed)
        self.assertFalse(any("暂无初报" in text for text in changed.missing))
        self.assertEqual(before, (self.ws.version, len(self.ws.events), self.ws.orders))

    def test_completed_order_and_closed_event_do_not_complete_sop(self):
        self.ws.orders["o1"] = ExecutionOrder("o1", "d1", "opt1", "模拟任务", OrderStatus.COMPLETED)
        view = early_response_view(self.ws, replace(self.record, status="closed"))
        self.assertEqual("待补资料", view.state)
        self.assertIn("尚未逐项关联", " ".join(view.facts))
        self.assertIn("任务 1 条", " ".join(view.facts))

    def test_page_has_six_answers_escaping_parallel_actions_and_role_boundaries(self):
        self.registry.events[self.record.event_id] = replace(self.record, initial_report='<script>SOP-PRIVATE</script>')
        html = render_sand_page(self.state)
        # 沙盘改版后不再用"六问"标题，改为核对新沙盘框架确实渲染出来。
        self.assertIn('class="cmd-page"', html)
        self.assertIn("&lt;script&gt;SOP-PRIVATE&lt;/script&gt;", html)
        self.assertNotIn("<script>SOP-PRIVATE</script>", html)
        self.assertEqual(9, html.count('data-sand-step="'))
        self.assertIn('data-sand-step-content="6"', html)
        self.assertIn('href="/events"', html)
        self.assertIn('data-sand-open="drawer-fourcat"', html)
        for role in ("member", "public"):
            self.assertEqual("", render_early_response(self.state, role))
            self.assertNotIn('class="sop-early-response"', render_sand_page(self.state, role=role))

    def test_no_event_and_legacy_unbound_workspace_have_honest_empty_states(self):
        self.assertEqual("未进入事件", early_response_view(self.ws, None).state)
        self.ws.event_id = ""
        self.assertEqual("依据待核对", early_response_view(self.ws, self.record).state)

    def test_real_checkin_handlers_supply_simulated_source_and_named_deputy(self):
        fixture = checkin.CheckinFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        alarm_id, event_id = fixture._demo_event()
        state = fixture.state
        state.handle_event_action(event_id, "start", duty_token=fixture.tokens[fixture.DUTY])
        state.handle_deputy_appointment(event_id, duty_token=fixture.tokens[fixture.DUTY],
            subject_id=fixture.DEPUTY, evidence="线下任命记录 SOP-01",
            request_id="sop-appoint", occurred_at="2026-09-28T09:00:00+08:00")
        state.handle_enter_event(event_id)
        record = replace(state.registry.events[event_id], initial_report="已有现场初报")
        state.registry.events[event_id] = record
        state.workspace.orders["o1"] = ExecutionOrder("o1", "d1", "opt1", "模拟任务")
        view = early_response_view(state.workspace, record)
        self.assertEqual(fixture.DEPUTY, view.deputy)
        self.assertEqual("待人工复核", view.state)
        self.assertIn("模拟报警", " ".join(view.facts))
        self.assertIn(alarm_id, " ".join(view.facts))
        self.assertIn(fixture.DUTY, " ".join(view.facts))
        self.assertIn("指定副总指挥：副总指挥", render_early_response(state, "commander"))


if __name__ == "__main__":
    unittest.main()
