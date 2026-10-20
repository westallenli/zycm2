"""P16-T5 验收：人员与物资页（doc 04 UI 接线——底账维护/到场快照/缺口建议）。"""

from __future__ import annotations
from tests.personnel_support import personnel_submit
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture

import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path

from panda_brain import tactical_map as tm
from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import ExecutionOrder, OrderStatus, PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.record_contracts import ObjectReference, ObjectRevision, RecordTimes
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-mat-secret"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _http(method: str, url: str, form: dict | None = None, cookie: str = ""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8")


def _cookie_of(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith(f"{name}="):
            value = part.strip().split("=", 1)[1]
            if value:
                return f"{name}={value}"
    return ""


class MaterialsPageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p16-mat", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
        )
        # 张三本职=扑救一组；李四无本职。两人都入长期库 + 工作区由桥接补齐。
        state.registry.import_person_library(
            [
                PersonRecord(
                    name="张三", phone="13912340001", unit="县防火办",
                    home_position="扑救一组（一分区）",
                ),
                PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
            ]
        )
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00", "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST",
                f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action},
                cookie=self.cmd,
            )
        personnel_submit(self.state, _http,
            "POST",
            f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"},
            cookie=self.cmd,
        )

    def _onboard_member(self, phone: str) -> str:
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": phone, "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        return member

    def test_gate_anonymous_redirects(self):
        status, headers, _ = _http("GET", f"{self.base}/materials")
        self.assertEqual(303, status)
        self.assertIn("/login", headers.get("Location", ""))

    def test_commander_cannot_borrow_named_logistics_write_rights(self):
        before = self.state.ledger.to_dict()
        for route, form in (("item", {"name": "越权物资"}), ("squad", {"name": "越权队"}),
                            ("carry", {"squad_id": "sq-001", "item_id": "mat-001"}),
                            ("point", {"name": "越权库"})):
            self.assertEqual(403, _http("POST", f"{self.base}/materials/{route}", form, cookie=self.cmd)[0])
        self.assertEqual(before, self.state.ledger.to_dict())
        self._seed_ledger()
        self.state.ledger.add_supply_point(name="县储备库", supplies={"mat-001": None})
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        for needle in ("物资档案", "中队与随队携带清单", "物资点", "库存未知"):
            self.assertIn(needle, body)

    def test_legacy_checkin_keeps_current_quantities_pending(self):
        self._seed_ledger()
        # 成员刷码确认 → 自动生成到场快照（张三属扑救一分队）
        self._onboard_member("13912340001")
        snapshot = self.state.ledger.snapshots.get("fire-001")
        self.assertIsNotNone(snapshot)
        self.assertIn("sq-001", snapshot.squads)
        lines = snapshot.squads["sq-001"].lines
        self.assertEqual({"mat-001", "mat-002"}, set(lines))
        self.assertEqual(lines["mat-001"].qty_registered, 4)

        # Historical unversioned check-in remains visible, but cannot back current quantities.
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("本次火情到场情况", body)
        self.assertIn("缺口与补给建议", body)
        self.assertIn("物资流水来源待核对，演练数量暂不展示", body)
        self.assertNotIn("演练账面可用 4", body)
        self.assertIn("当前不足与预计不足均待核实", body)
        self.assertNotIn("最近补给点", body)

        self.state.ledger.add_supply_point(name="县储备库", supplies={"mat-001": None})
        _, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertIn("县储备库", body)
        self.assertIn("库存未知", body)
        self.assertIn("演练数量暂不展示", body)

    def test_legacy_field_check_cannot_override_current_report_chain(self):
        self._seed_ledger()
        self._onboard_member("13912340001")
        before = self.state.ledger.to_dict()
        st, _, body = _http("POST", f"{self.base}/materials/check",
            {"event_id": "fire-001", "squad_id": "sq-001", "item_id": "mat-001", "qty_usable": "3"}, cookie=self.cmd)
        self.assertEqual(403, st)
        self.assertEqual(before, self.state.ledger.to_dict())

    def test_commander_reads_one_task_item_evidence_without_writes(self):
        self._seed_ledger()
        self._onboard_member("13912340001")
        workspace = self.state.workspace
        workspace.orders["order-1"] = ExecutionOrder(
            "order-1", "decision-1", "option-1", "扑救北段",
            status=OrderStatus.CONFIRMED, confirmed_by="commander",
        )
        flow = tm.CommandFlow(
            order_id="order-1", event_id="fire-001", content_version=1,
            target="扑救北段",
        )
        workspace.command_flows["order-1"] = flow
        workspace.append_event("CommandFlowOpened", order_id="order-1",
                               event_id="fire-001", flow=asdict(flow))
        before_version = workspace.version
        before_ledger = self.state.ledger.to_dict()
        query = "gap_order=order-1&gap_item=mat-001"
        st, _, body = _http("GET", f"{self.base}/materials?{query}", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("正式缺口依据核对", body)
        self.assertIn("order-1@1", body)
        self.assertIn("账面可用 待核对", body)
        self.assertIn("到场物资快照的版本化来源", body)
        self.assertIn("经专业核定的物资用量依据", body)
        self.assertIn("当前不足 待核实", body)
        self.assertIn("查看来源编号", body)
        self.assertEqual(before_version, workspace.version)
        self.assertEqual(before_ledger, self.state.ledger.to_dict())

        record = ObjectRevision(
            ObjectReference("fire-001", "team_arrival", "sq-001", 1),
            RecordTimes(recorded_at="2026-09-30T08:00:00+08:00"),
            "队长报告", "reported", "captain", "arrival-req-1",
            json.dumps({"squad_id": "sq-001", "carry_baseline": {
                item_id: line.qty_registered for item_id, line in
                self.state.ledger.snapshots["fire-001"].squads["sq-001"].lines.items()
            }, "arrived_phones": list(self.state.ledger.snapshots["fire-001"].squads["sq-001"].arrived_phones)}),
        )
        self.state.ledger.flow_records[record.id] = record
        squad_snapshot = self.state.ledger.snapshots["fire-001"].squads["sq-001"]
        squad_snapshot.arrival_id = record.id
        from panda_brain.captain_arrivals import ARRIVAL_REPORTED
        workspace.append_event(ARRIVAL_REPORTED, event_id="fire-001", squad_id="sq-001",
                               actor="captain", mode="initial", flow_record_id=record.id,
                               arrived_phones=list(squad_snapshot.arrived_phones), data_kind="reported")
        from panda_brain.persistence import save_workspace
        save_workspace(workspace, self.state.registry.events["fire-001"].snapshot_path)
        for line in squad_snapshot.lines.values():
            line.qty_usable = line.qty_registered
        _, _, body = _http("GET", f"{self.base}/materials?{query}", cookie=self.cmd)
        self.assertIn("账面可用 4 台", body)
        self.assertIn(record.id, body)

        st, _, body = _http(
            "GET", f"{self.base}/materials?gap_order=unknown&gap_item=mat-001",
            cookie=self.cmd,
        )
        self.assertEqual(200, st)
        self.assertIn("请选择当前事件中的任务与物资", body)
        self.assertNotIn("事件 fire-001 · 任务", body)

    def test_member_bound_to_other_event_sees_no_current_event_details(self):
        self._seed_ledger()
        member = self._onboard_member("13912340001")
        line = self.state.ledger.snapshots["fire-001"].squads["sq-001"].lines["mat-001"]
        line.reason = "仅旧事件现场细节"
        session = self.state.member_session(member.split("=", 1)[1])
        session.bound_event_id = "fire-other"
        st, _, body = _http(
            "GET", f"{self.base}/materials?gap_order=order-1&gap_item=mat-001",
            cookie=member,
        )
        self.assertEqual(200, st)
        self.assertNotIn("仅旧事件现场细节", body)
        self.assertNotIn("正式缺口依据核对", body)
        self.assertIn("尚未进入任何事件任务空间", body)

    def test_supply_leads_are_read_only_and_never_shown_as_available(self):
        self._seed_ledger()
        self.state.workspace.orders["order-1"] = ExecutionOrder(
            "order-1", "decision-1", "option-1", "北段扑救",
            status=OrderStatus.CONFIRMED, confirmed_by="commander",
        )
        self.state.ledger.add_supply_point(
            name="县库", supplies={"mat-001": None},
        )
        self.state.ledger.add_external_company(
            name="甲公司", source="模拟表", recorded_by="后勤甲", services="水带",
        )
        before_version = self.state.workspace.version
        before_ledger = self.state.ledger.to_dict()
        st, _, body = _http(
            "GET", f"{self.base}/materials?gap_order=order-1&gap_item=mat-001",
            cookie=self.cmd,
        )
        self.assertEqual(200, st)
        self.assertIn("供给候选依据（只读）", body)
        self.assertIn("县库", body)
        self.assertIn("库存数量未知或无效", body)
        self.assertIn("送达时间待核实", body)
        self.assertIn("甲公司", body)
        self.assertIn("清单不证明该物资可供", body)
        self.assertIn("可补给性待核实", body)
        self.assertEqual(before_version, self.state.workspace.version)
        self.assertEqual(before_ledger, self.state.ledger.to_dict())

        member = self._member_readonly_session()
        _, _, member_body = _http(
            "GET", f"{self.base}/materials?gap_order=order-1&gap_item=mat-001",
            cookie=member,
        )
        self.assertNotIn("供给候选依据（只读）", member_body)

    def test_member_is_read_only(self):
        member = self._member_readonly_session()
        st, _, body = _http("GET", f"{self.base}/materials", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn("正式缺口依据核对", body)
        # 维护动作被拒（403），且不产生数据
        st, _, body = _http(
            "POST", f"{self.base}/materials/item",
            {"name": "越权物资"}, cookie=member,
        )
        self.assertEqual(403, st)
        self.assertEqual({}, self.state.ledger.items)

    def _member_readonly_session(self) -> str:
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340002", "password": DEMO_INITIAL_PASSWORD},
        )
        return _cookie_of(headers, "member_session")

    def _seed_ledger(self):
        # Domain fixtures do not impersonate a runtime grant.
        ledger = self.state.ledger
        ledger.upsert_item(name="高压水泵", category="装备", unit="台", default_carry_qty=4)
        ledger.upsert_item(name="水带 20m", unit="盘", default_carry_qty=20)
        ledger.add_squad(name="扑救一分队", member_phones=("13912340001",), headcount_planned=12)
        ledger.set_carry_line("sq-001", "mat-001")
        ledger.set_carry_line("sq-001", "mat-002")
        ledger.save(self.state.materials_path)


if __name__ == "__main__":
    unittest.main()
