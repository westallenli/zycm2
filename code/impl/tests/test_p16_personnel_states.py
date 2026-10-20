"""P16-T19 验收：到场人员八态状态机（定版 04 号 §4.1）。

覆盖：派生态（预编入/待到场/已到场/已撤离）从名册与签到事实推导；
人工判定态（执行中须挂真实在效命令、暂离轮休、失联、归建）逐条入哈希链；
标记可恢复、离岗清执行类旁挂；终态命令使「执行中」依据消失回落派生态；
HTTP 页面渲染与权限门。
"""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import OrderStatus
from panda_brain.models import ExecutionOrder
from panda_brain.org_model import POSITION_SLOTS
from panda_brain.panel import PanelState, make_server
from panda_brain.personnel_states import (
    STATE_COLORS,
    derive_personnel_states,
    mark_person_state,
)
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p19-secret"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _http(method, url, form=None, cookie=""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _order(order_id="ord-001", status=OrderStatus.ACTIVE):
    return ExecutionOrder(
        id=order_id, decision_id="dec-x", option_id="opt-x",
        scope="演示", status=status)


def _entry(phone, pos_code=""):
    return types.SimpleNamespace(phone=phone, pos_code=pos_code)


class DerivationMatrix(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace("p19")
        self.ws.import_person_library([
            __import__("panda_brain.models", fromlist=["PersonRecord"]).PersonRecord(
                name="张三", phone="13912340001", unit="林场哨所"),
        ])
        self.slot = POSITION_SLOTS[0].pos_code

    def _states(self, entries=()):
        return {r.phone: r for r in derive_personnel_states(self.ws, entries)}

    def test_pre_enrolled_and_awaiting(self):
        rows = self._states([_entry("13912340001")])
        self.assertEqual("预编入", rows["13912340001"].state)
        self.assertEqual("#647382", STATE_COLORS["预编入"])
        rows = self._states([_entry("13912340001", self.slot)])
        self.assertEqual("待到场", rows["13912340001"].state)

    def test_checked_in_and_executing(self):
        self.ws.scan_check_in(self.slot, phone="13912340001")
        rows = self._states()
        self.assertEqual("已到场", rows["13912340001"].state)
        self.assertEqual("#2F7D68", STATE_COLORS["已到场"])
        self.ws.orders["ord-001"] = _order()
        mark_person_state(self.ws, "executing", phone="13912340001",
                          operator="指挥员", order_id="ord-001")
        rows = self._states()
        self.assertEqual("执行中", rows["13912340001"].state)

    def test_standby_and_resume(self):
        self.ws.scan_check_in(self.slot, phone="13912340001")
        mark_person_state(self.ws, "standby", phone="13912340001", operator="指挥员")
        self.assertEqual("暂离/轮休", self._states()["13912340001"].state)
        mark_person_state(self.ws, "resume_duty", phone="13912340001", operator="指挥员")
        self.assertEqual("已到场", self._states()["13912340001"].state)

    def test_lost_contact_and_restore(self):
        self.ws.scan_check_in(self.slot, phone="13912340001")
        mark_person_state(self.ws, "lost_contact", phone="13912340001", operator="指挥员")
        self.assertEqual("失联", self._states()["13912340001"].state)
        self.assertEqual("#B84A42", STATE_COLORS["失联"])
        mark_person_state(self.ws, "contact_restored", phone="13912340001", operator="指挥员")
        self.assertEqual("已到场", self._states()["13912340001"].state)

    def test_checkout_clears_marks_then_returned_to_unit(self):
        self.ws.scan_check_in(self.slot, phone="13912340001")
        self.ws.orders["ord-001"] = _order()
        mark_person_state(self.ws, "executing", phone="13912340001",
                          operator="指挥员", order_id="ord-001")
        mark_person_state(self.ws, "standby", phone="13912340001", operator="指挥员")
        self.ws.check_out(self.slot, phone="13912340001")
        rows = self._states([_entry("13912340001")])
        self.assertEqual("已撤离", rows["13912340001"].state)   # 离岗清除旁挂
        mark_person_state(self.ws, "returned_to_unit", phone="13912340001", operator="指挥员")
        self.assertEqual("已归建", self._states()[("13912340001")].state)

    def test_terminal_order_drops_executing_basis(self):
        self.ws.scan_check_in(self.slot, phone="13912340001")
        self.ws.orders["ord-001"] = _order(status=OrderStatus.ACTIVE)
        mark_person_state(self.ws, "executing", phone="13912340001",
                          operator="指挥员", order_id="ord-001")
        self.assertEqual("执行中", self._states()["13912340001"].state)
        # 命令随后被取消：执行中依据消失，回落已到场（不伪造仍在执行）
        self.ws.orders["ord-001"] = _order(status=OrderStatus.CANCELLED)
        self.assertEqual("已到场", self._states()["13912340001"].state)

    def test_fail_closed_marks(self):
        self.ws.orders["ord-001"] = _order(status=OrderStatus.COMPLETED)  # 终态
        with self.assertRaises(ValueError):
            mark_person_state(self.ws, "executing", phone="13912340001",
                              operator="指挥员", order_id="ord-404")
        with self.assertRaises(ValueError):
            mark_person_state(self.ws, "executing", phone="13912340001",
                              operator="指挥员", order_id="ord-001")
        with self.assertRaises(ValueError):
            mark_person_state(self.ws, "dance", phone="13912340001", operator="x")
        self.assertEqual((), self.ws.verify_audit_chain())


class RosterPageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = PanelState(
            ScenarioWorkspace("p19-e2e", general_auth=GeneralEntryAuth(secret=_SECRET)),
            auth=GeneralEntryAuth(secret=_SECRET),
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        rec = self.state.registry.create_event(
            name="八态演练事件", report_no="RPT-P19", discovered_at="2026-08-24T09:00:00Z",
            source="演示数据", location="某山林场")
        self.event_id = rec.event_id
        from panda_brain.models import PersonRecord
        person = PersonRecord(name="张三", phone="13912340001", unit="林场哨所")
        self.state.registry.import_person_library([person])
        self.state.workspace.import_person_library([person])
        self.state.registry.add_roster_entry(
            self.event_id, "13912340001", pos_code=POSITION_SLOTS[0].pos_code,
            added_by="GENERAL_ENTRY_ID")
        from tests.duty_support import duty_token
        self.state.login(_SECRET)
        self.state.handle_event_action(self.event_id, "start", duty_token=duty_token(self.state,phone=person.phone))
        self.state.handle_enter_event(self.event_id)
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        form = urllib.parse.urlencode({"username": "admin", "secret": _SECRET}).encode()
        req = urllib.request.Request(f"{self.base}/login", data=form, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        raw = ""
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
        self.cmd = raw.split(";")[0]

    def _roster_page(self):
        st, body = _http("GET", f"{self.base}/events/{self.event_id}/roster",
                         cookie=self.cmd)
        self.assertEqual(200, st)
        return body

    def test_page_renders_eight_state_card_and_mark_flows(self):
        body = self._roster_page()
        self.assertIn("到场人员状态", body)
        self.assertIn("待到场", body)          # 名册已指派未刷码
        # 刷码入岗后变已到场
        member,error=self.state.member_login("13912340001","123456")
        self.assertFalse(error)
        qr=next(iter(self.state.registry.qrs))
        self.state.member_scan(member,qr)
        self.state.member_confirm(member,qr)
        body = self._roster_page()
        self.assertIn("已到场", body)
        # 标记暂离 → 页面反映 + 操作按钮切换
        st, _, = None, None
        from tests.personnel_support import named_actor
        token=named_actor(self.state,self.event_id)
        data = urllib.parse.urlencode(dict(csrf=self.state.member_session(token).custody_csrf,
            event_id=self.event_id,operation="mark_state",expected_version="0",request_id="eight-state-named",
            basis="匿名副指挥长记录状态",phone="13912340001",action="standby",order_id="",note="具名状态报告")).encode()
        req = urllib.request.Request(
            f"{self.base}/personnel/manage", data=data,
            method="POST")
        req.add_header("Cookie", "member_session="+token)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            st = resp.status
            resp.read()
        except urllib.error.HTTPError as exc:
            st = exc.code
            exc.read()
        self.assertIn(st, (302, 303))
        body = self._roster_page()
        self.assertIn("暂离/轮休", body)
        self.assertIn("恢复在场", body)
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        kinds = [e.kind for e in self.state.workspace.events]
        self.assertIn("PersonStateMarked", kinds)

    def test_anonymous_forbidden(self):
        st, _ = _http("POST", f"{self.base}/events/{self.event_id}/person/state",
                      {"phone": "13912340001", "action": "standby"})
        self.assertEqual(403, st)

    def test_timeline_label_registered(self):
        from panda_brain.review_domain import _TIMELINE_LABELS
        self.assertEqual("人员八态标记", _TIMELINE_LABELS.get("PersonStateMarked"))


if __name__ == "__main__":
    unittest.main()
