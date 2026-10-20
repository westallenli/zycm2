"""P16-T13 验收：语音/文字意图分流层（doc 03《指挥调度沙盘》§13/§13.2 定版）。

域级：
- classify_intent 确定性分流（命令动词+无疑问标记→命令；疑问标记→问询）；
- parse_command_fields 只产提示值，取不到就空串不猜。

HTTP e2e：
- 成员发命令文本 → 拒绝且零写入（无草稿、无审计事件）；
- 指挥员命令文本 → 捕获为草稿（未执行）+ CommandDraftCaptured 入哈希链；
- 确认 → 转指挥员自提方案（既有认证门），CommandDraftConfirmed 入链，
  workspace.options 出现 opt-cmd-vNNN；作废留痕；
- 问询文本经同一路径只检索不改状态；
- 全程 verify_audit_chain 完整；复盘时间线出现中文标签。
"""

from __future__ import annotations
from tests.personnel_support import personnel_submit
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture

import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import intent_router as ir
from panda_brain import review_domain as rvd
from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-intent-secret"


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


class IntentRouterDomain(unittest.TestCase):
    def test_classify_command_vs_inquiry(self):
        self.assertEqual("command", ir.classify_intent("派出二队增援阴坡"))
        self.assertEqual("command", ir.classify_intent(
            "对东坡火线实施合围，直升机吊桶洒水"))
        # 疑问标记优先：即使带命令动词也按问询处理
        self.assertEqual("inquiry", ir.classify_intent("要不要派出二队？"))
        self.assertEqual("inquiry", ir.classify_intent("现在队伍多少人"))
        self.assertEqual("inquiry", ir.classify_intent(""))
        self.assertEqual("inquiry", ir.classify_intent("   "))

    def test_parse_fields_are_hints_not_fabrications(self):
        units = ("林场哨所", "二中队")
        fields = ir.parse_command_fields(
            "派出二中队在青岗梁开设隔离带", units)
        self.assertEqual("二中队", fields["unit"])
        self.assertEqual("青岗梁", fields["area"])
        empty = ir.parse_command_fields("立即行动", units)
        self.assertEqual("", empty["unit"])       # 匹配不到就空串
        self.assertEqual("", empty["area"])
        # 长单位名优先（避免「中队」类子串误命中）
        greedy = ir.parse_command_fields("调派二中队驰援", ("一中队", "二中队"))
        self.assertEqual("二中队", greedy["unit"])


class IntentPageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-intent", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        self.state.registry.import_person_library(
            [PersonRecord(name="李四", phone="13912340002",
                          unit="林场哨所", home_position="扑救二组（二分区）")]
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http("POST", f"{self.base}/events/action",
                  {"event_id": "fire-001", "action": action}, cookie=self.cmd)

    def _member_session(self) -> str:
        personnel_submit(self.state, _http, "POST", f"{self.base}/events/fire-001/roster/add",
              {"phones": "13912340002"}, cookie=self.cmd)
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340002", "password": DEMO_INITIAL_PASSWORD},
        )
        cookie = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=cookie)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=cookie)
        return cookie

    def _chain_kinds(self) -> list[str]:
        return [e.kind for e in self.state.workspace.events]

    def test_member_command_refused_with_zero_writes(self):
        member = self._member_session()
        before = len(self.state.workspace.events)
        st, headers, _ = _http(
            "POST", f"{self.base}/sand/intent",
            {"text": "派出二中队封锁垭口"},
            cookie=member,
        )
        self.assertEqual(303, st)
        self.assertIn("/sand#intent", headers.get("Location", ""))
        self.assertEqual(before, len(self.state.workspace.events))  # 零写入
        self.assertEqual({}, self.state.voice_drafts)
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        st, _, body = _http("GET", f"{base}/sand", cookie=member)
        self.assertEqual(200, st)
        self.assertIn("仅指挥员可用", body)

    def test_commander_command_captured_as_draft_and_audited(self):
        before = len(self.state.workspace.events)
        st, _, _ = _http(
            "POST", f"{self.base}/sand/intent",
            {"text": "派出林场哨所在青岗梁开设隔离带"},
            cookie=self.cmd,
        )
        self.assertEqual(303, st)
        drafts = self.state.voice_drafts
        self.assertEqual(1, len(drafts))
        draft = next(iter(drafts.values()))
        self.assertEqual("待确认", draft["status"])
        self.assertEqual("林场哨所", draft["unit_hint"])   # 已知单位词法匹配
        self.assertEqual("青岗梁", draft["area_hint"])
        kinds = self._chain_kinds()
        self.assertIn("CommandDraftCaptured", kinds)
        captured = [e for e in self.state.workspace.events
                    if e.kind == "CommandDraftCaptured"][0]
        self.assertEqual(draft["id"], captured.payload["draft_id"])
        self.assertNotIn("ExecutionOrderDrafted", kinds)     # 未执行
        self.assertEqual(before + 1, len(self.state.workspace.events))
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        # 页面渲染草稿卡与未解析纪律
        st, _, body = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertIn(draft["id"], body)
        self.assertIn("确认转方案", body)
        self.assertIn("绝不直接执行", body)

    def test_confirm_routes_into_existing_power_chain(self):
        _http("POST", f"{self.base}/sand/intent",
              {"text": "派出二中队增援阴坡"}, cookie=self.cmd)
        draft = next(iter(self.state.voice_drafts.values()))
        st, _, _ = _http("POST", f"{self.base}/sand/draft-confirm",
                         {"draft_id": draft["id"]}, cookie=self.cmd)
        self.assertEqual(303, st)
        updated = self.state.voice_drafts[draft["id"]]
        self.assertEqual("已转自提方案", updated["status"])
        option_id = updated["option_id"]
        self.assertTrue(option_id.startswith("opt-cmd-v"))
        self.assertIn(option_id, self.state.workspace.options)
        option = self.state.workspace.options[option_id]
        self.assertTrue(option.proposed_by_commander)        # 指挥员自提身份强制
        kinds = self._chain_kinds()
        self.assertIn("CommandDraftConfirmed", kinds)
        self.assertIn("CommanderOptionProposed", kinds)
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        # 重复确认拒绝
        st, _, body = _http("POST", f"{self.base}/sand/draft-confirm",
                            {"draft_id": draft["id"]}, cookie=self.cmd)
        self.assertEqual(409, st)

    def test_discard_leaves_audit_trail(self):
        _http("POST", f"{self.base}/sand/intent",
              {"text": "撤离东坡全部人员"}, cookie=self.cmd)
        draft = next(iter(self.state.voice_drafts.values()))
        st, _, _ = _http("POST", f"{self.base}/sand/draft-discard",
                         {"draft_id": draft["id"]}, cookie=self.cmd)
        self.assertEqual(303, st)
        self.assertEqual("已作废",
                         self.state.voice_drafts[draft["id"]]["status"])
        self.assertIn("CommandDraftDiscarded", self._chain_kinds())
        self.assertEqual((), self.state.workspace.verify_audit_chain())

    def test_inquiry_through_same_gate_only_reads(self):
        before = len(self.state.workspace.events)
        st, _, _ = _http("POST", f"{self.base}/sand/intent",
                         {"text": "现在图上有哪些已核验火点？"}, cookie=self.cmd)
        self.assertEqual(303, st)
        self.assertEqual(before, len(self.state.workspace.events))  # 只检索
        self.assertEqual({}, self.state.voice_drafts)
        self.assertIn("问询", self.state.intent_notice)

    def test_timeline_labels_render_on_review_page(self):
        _http("POST", f"{self.base}/sand/intent",
              {"text": "调派二中队转移受威胁群众"}, cookie=self.cmd)
        st, _, body = _http("GET", f"{self.base}/review?tab=timeline",
                            cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("指令草稿捕获（未执行）", body)
        entries = rvd.build_timeline(self.state.workspace)
        self.assertTrue(any(e.label == "指令草稿捕获（未执行）"
                            for e in entries))


if __name__ == "__main__":
    unittest.main()
