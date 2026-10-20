"""P17-P6 验收：五页端到端主线（DEVELOPMENT_PLAN_PAGES_V1 §P6 八步验收线）。

单条连续场景走完：
1 指挥员登录→手动登记→启动→名册；
2 外部报警进入→人工确认→同构火情；
3 成员登录→人员架构入组显示；
4 签到/离场实时反映；
5 沙盘四类态势→方案登记→指挥员决断→命令下达→执行反馈；
6 人员物资快照→现场核对→缺口识别；
7 复盘回放：事实→问题→根因→整改→经验→报告；
8 权限门禁、审计链完整、五页刷新重载。

领域级动作（决断/命令/执行反馈）按离线战役同径驱动 workspace API——
与 campaign_runner 完全同链，前台只断言业务结果可见。
"""

from __future__ import annotations
from tests.personnel_support import personnel_submit
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture
from tests.review_support import owner_token, submit

import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import (
    AttemptStatus,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    PersonRecord,
)
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace
from panda_brain import review_causality, review_meeting, review_closure

_SECRET = "p17-p6-e2e-secret"


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


class FivePageEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p17-p6", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            alarm_inbox_path=tmp / "alarms.json",
        )
        # 长期库：张三（扑救一组，成员+签到）、李四（复盘负责人）
        self.state.registry.import_person_library([
            PersonRecord(name="张三", phone="13912340001", unit="县防火办",
                         home_position="扑救一组（一分区）"),
            PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
        ])
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

    # -- 基础步骤 ---------------------------------------------------------------

    def _commander_login_and_event(self):
        st, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.assertEqual(303, st)
        self.assertIn("/events", headers.get("Location", ""))     # §7.2 登记权限首屏钉
        cmd = _cookie_of(headers, "panel_session")
        cmd += "; " + duty_cookie(self.state, phone="13912340001")

        received = receive_confirmed_fixture(self.state, {"name": "汉江上游山火", "report_no": "HC-P6",
             "discovered_at": "2026-08-25T08:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED", received["code"])
        for action in ("start", "issue-qr", "enter"):
            st, _, _ = _http(
                "POST", f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action}, cookie=cmd)
            self.assertEqual(303, st)
        return cmd

    def _seed_ledger_for_snapshot(self, cmd: str):
        # Domain fixtures do not impersonate a runtime grant.
        ledger = self.state.ledger
        ledger.upsert_item(name="高压水泵", category="装备", unit="台", default_carry_qty=4)
        ledger.upsert_item(name="水带 20m", unit="盘", default_carry_qty=20)
        ledger.add_squad(name="扑救一分队", member_phones=("13912340001",), headcount_planned=12)
        ledger.set_carry_line("sq-001", "mat-001")
        ledger.set_carry_line("sq-001", "mat-002")
        ledger.save(self.state.materials_path)

    def _onboard_zhang(self):
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        self.assertTrue(member)
        # 未刷码：首屏必须是登录页（§7.2 无入场事实不得进首页）
        st, headers, _ = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))
        import re as _re
        st, headers, body = _http(
            "POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        m_scan = _re.search(r"lz-alert[^>]*>([^<]{0,160})", body or "")
        st, headers, body = _http(
            "POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        if st != 303:
            m = _re.search(r"lz-alert[^>]*>([^<]{0,200})", body or "")
            self.fail(
                f"confirm st={st} loc={headers.get('Location')} "
                f"scan_alert={m_scan.group(1) if m_scan else ''} "
                f"alert={m.group(1) if m else (body or '')[:200]}")
        self.assertEqual("/", headers.get("Location", ""))
        return member

    def _owner_session(self, cmd: str) -> str:
        return 'member_session=' + owner_token(self.state)

    def _review_http(self, method, url, form=None, cookie=''):
        return submit(self.state,_http,method,url,form,cookie=cookie)

    # -- 八步验收主线 ------------------------------------------------------------

    def test_step1_commander_register_start_roster(self):
        cmd = self._commander_login_and_event()
        st, _, body = personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"}, cookie=cmd)
        self.assertEqual(303, st)
        entry = self.state.registry.rosters["fire-001"].entries["13912340001"]
        self.assertEqual("pjjz-1", entry.pos_code)   # 本职岗位自动解析

    def test_step2_confirmed_push_creates_event_without_start(self):
        cmd = self._commander_login_and_event()
        st, headers, _ = _http("POST", f"{self.base}/events/alarm/demo", {}, cookie=cmd)
        self.assertEqual(303, st)
        self.assertIn("alarm_event=fire-002", headers.get("Location", ""))
        fire2 = self.state.registry.events["fire-002"]
        self.assertEqual("多模态火情预警系统（演示数据）", fire2.source)
        self.assertEqual("registered", fire2.status)
        self.assertFalse(any(q.event_id == fire2.event_id for q in self.state.registry.qrs.values()))
        st, _, page = _http("GET", f"{self.base}/events/fire-002/details", cookie=cmd)
        self.assertEqual(200, st)
        self.assertIn("待启动处置", page)
        self.assertIn("DEMO_CONSOLE", page)

    def test_step3_4_member_lands_in_group_then_departure_reflects(self):
        cmd = self._commander_login_and_event()
        self._seed_ledger_for_snapshot(cmd)
        personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"}, cookie=cmd)
        member = self._onboard_zhang()
        # 入组显示：节点高亮 + 姓名（我）
        st, _, home = _http("GET", f"{self.base}/", cookie=member)
        self.assertEqual(200, st)
        self.assertIn('data-me="1"', home)
        self.assertIn("张三（我）", home)
        self.assertIn("已到场", home)
        # 离场：架构页与八态卡实时反映
        self.state.workspace.check_out("pjjz-1", phone="13912340001")
        st, _, home = _http("GET", f"{self.base}/", cookie=member)
        self.assertIn(">已撤离</span>", home)
        st, _, mats = _http("GET", f"{self.base}/materials", cookie=member)
        self.assertIn(">已撤离</span>", mats)

    def test_step5_sand_option_decision_order_feedback(self):
        cmd = self._commander_login_and_event()
        st, _, sand = _http("GET", f"{self.base}/sand", cookie=cmd)
        for header in ("事实 · 观测与证据", "判断 · 分析与推断",
                       "建议 · 待指挥员决断", "指令 · 正式命令"):
            self.assertIn(header, sand)
        # 方案：语音/文字指令捕获为草稿 → 确认为自提方案
        st, _, _ = _http(
            "POST", f"{self.base}/sand/intent",
            {"text": "派出二队在阴坡开设隔离带"}, cookie=cmd)
        self.assertEqual(303, st)
        draft = next(d for d in self.state.voice_drafts.values()
                     if d["status"] == "待确认")
        st, _, _ = _http(
            "POST", f"{self.base}/sand/draft-confirm",
            {"draft_id": draft["id"]}, cookie=cmd)
        self.assertEqual(303, st)
        option_id = draft["option_id"]
        self.assertTrue(option_id)
        # 决断→命令→反馈：域层全链（与离线战役同一 power chain）
        decision = self.state.workspace.record_decision(
            DecisionType.ADOPT, option_id,
            reason="阴坡火头威胁主沟，采纳隔离带方案",
            scope="灭火指挥调度", actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
        )
        order = self.state.workspace.issue_order(decision.id)
        self.state.workspace.record_execution(
            order.id, AttemptStatus.PARTIAL,
            observations=("二队到达阴坡", "水源点尚在铺设"))
        st, _, sand = _http("GET", f"{self.base}/sand", cookie=cmd)
        self.assertIn(option_id, sand)          # 方案卡在「建议」区
        self.assertIn(order.id, sand)           # 命令在「指令」区

    def test_step6_materials_snapshot_check_gap(self):
        cmd = self._commander_login_and_event()
        self._seed_ledger_for_snapshot(cmd)
        personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"}, cookie=cmd)
        self._onboard_zhang()
        st, _, mats = _http("GET", f"{self.base}/materials", cookie=cmd)
        self.assertIn("本次火情到场情况", mats)
        self.assertIn("高压水泵", mats)
        st, _, _ = _http(
            "POST", f"{self.base}/materials/check",
            {"event_id": "fire-001", "squad_id": "sq-001",
             "item_id": "mat-001", "qty_usable": "3", "reason": "一台故障"},
            cookie=cmd,
        )
        self.assertEqual(403, st)
        line = self.state.ledger.snapshots["fire-001"].squads["sq-001"].lines["mat-001"]
        self.assertIsNone(line.qty_usable)
        st, _, mats = _http("GET", f"{self.base}/materials", cookie=cmd)
        self.assertIn("缺口与补给建议", mats)
        self.assertIn("演练测算口径：火线", mats)

    def test_step7_review_chain_owner_signed_end_to_end(self):
        cmd = self._commander_login_and_event()
        self._seed_ledger_for_snapshot(cmd)
        personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"}, cookie=cmd)
        self._onboard_zhang()
        owner = self._owner_session(cmd)
        # 事实：草稿标绘 → 规则扫描产出候选问题
        _http("POST", f"{self.base}/sand/annotate",
              {"kind": "fire_point", "title": "东线滞留火点", "points": "500,300",
               "certainty": "", "note": ""}, cookie=cmd)
        st, _, _ = self._review_http("POST", f"{self.base}/review/generate", {}, cookie=owner)
        self.assertEqual(303, st)
        iid = next(iter(self.state.review_ledger.issues))
        token = owner.split('=', 1)[1]
        def basis(kind, target_id):
            meeting = review_meeting.record_note(self.state, token, dict(
                subject='匿名核查', provided_by='张三', feedback='对照模拟原始记录',
                evidence_refs='evt-001', occurred_at='', request_id=f'p17-meeting-{kind}'))
            return review_causality.record_assessment(self.state, token, dict(
                target_kind=kind, target_id=target_id, support_refs='audit#1',
                counter_refs='', counter_note='暂无反证，继续核查', alternatives='可能为模拟输入延迟',
                missing_info='', verify_targets='张三及原始记录', meeting_ref=meeting.id,
                meeting_finding='会议对照原始记录支持', causal_layer='direct' if kind == 'hypothesis' else 'undetermined',
                expected_object_version='0', request_id=f'p17-basis-{kind}'))
        issue_basis = basis('issue', iid)
        self._review_http("POST", f"{self.base}/review/issue-status",
              {"issue_id": iid, "target": "confirmed", "reason": "匿名核查通过",
               "assessment_ref": issue_basis.id}, cookie=owner)
        # 根因假设经逐条核实与负责人确认。
        self._review_http("POST", f"{self.base}/review/hyp-new",
              {"statement": "名册预配流程缺复核环节",
               "related_issues": iid, "to_verify": "核对原始记录"}, cookie=owner)
        hid = next(iter(self.state.review_ledger.hypotheses))
        self._review_http('POST', f'{self.base}/review/hyp-resolve', dict(
            hyp_id=hid, item='核对原始记录', answer='模拟资料支持'), cookie=owner)
        hyp_basis = basis('hypothesis', hid)
        self._review_http('POST', f'{self.base}/review/hyp-status', dict(
            hyp_id=hid, target='verified', reason='已核对其他解释',
            assessment_ref=hyp_basis.id), cookie=owner)
        # 整改草案须由负责人审核发布；报告发布不会提前关闭任务。
        rid = review_closure.create_rect_draft(self.state, token, iid, hid).rect_id
        review_closure.publish_rect(self.state, token, rid,
            measures='修订模拟名册预配流程', owner_unit='县防火办', owner_person='张三',
            responsible_phone='13912340001', deadline='2026-12-31',
            acceptance_standard='名册逐项复核', verifier_name='李四',
            verifier_phone='13912340002')
        self.assertEqual('in_progress', self.state.review_ledger.rects[rid].status)
        # 经验候选经来源、条件与限制补齐后由负责人确认。
        eid = review_closure.propose_experience(self.state, token,
            source_kind='issue', source_id=iid).exp_id
        review_closure.revise_experience(self.state, token, eid,
            content='匿名输入先核对来源', applicability='来源可追溯',
            limitations='来源不明时不适用', condition_tags='来源可追溯=是',
            evidence='audit#1', reason='补足条件')
        review_closure.decide_experience(self.state, token, eid,
            target='confirmed', reason='人工核对来源')
        self.assertEqual('13912340002', self.state.review_ledger.experiences[eid].confirmed_by)
        # 报告：系统草稿 → 负责人编辑定版发布
        from dataclasses import replace
        event = self.state.registry.events[self.state.active_event_id]
        self.state.registry.events[event.event_id] = replace(event, status='closed')
        self._review_http("POST", f"{self.base}/review/report-generate", {}, cookie=owner)
        rep = next(iter(self.state.review_ledger.reports))
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-edit",
                         {"report_id": rep,
                          "content_md": self.state.review_ledger.reports[rep].content_md
                          + "\n\n## 补充\n- 现场核对无遗漏。"}, cookie=owner)
        self.assertEqual(303, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-status",
                         {"report_id": rep, "target": "pending_review"}, cookie=owner)
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-status",
                         {"report_id": rep, "target": "published"}, cookie=owner)
        self.assertEqual("published",
                         self.state.review_ledger.reports[rep].status)
        self.assertEqual('in_progress', self.state.review_ledger.rects[rid].status)
        # 导出：章节齐备且不追加责任结论
        st, headers, md = _http("GET", f"{self.base}/export/review", cookie=cmd)
        self.assertEqual(200, st)
        self.assertIn("## 七、人工确认结论", md)
        self.assertIn("系统不自动定责", md)
        self.assertIn("负责人已核实直接原因", md)

    def test_step8_permissions_audit_and_reload(self):
        cmd = self._commander_login_and_event()
        self._seed_ledger_for_snapshot(cmd)
        personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"}, cookie=cmd)
        self._onboard_zhang()
        # 权限：匿名打回登录；成员可读五页但写被拒
        st, headers, _ = _http("GET", f"{self.base}/materials")
        self.assertEqual(303, st)
        _, m_headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD})
        member = _cookie_of(m_headers, "member_session")
        # 已授权值班人员可进入专用启动页，但不能进入指挥页面。
        for route in ("/", "/sand", "/materials", "/review"):
            st, _, _ = _http("GET", f"{self.base}{route}", cookie=member)
            self.assertEqual(200, st, route)
        st, _, duty_page = _http("GET", f"{self.base}/events", cookie=member)
        self.assertEqual(200, st)
        self.assertIn("火情事件", duty_page)
        self.assertNotIn('action="/events/create"', duty_page)
        st, _, _ = _http("POST", f"{self.base}/materials/item",
                         {"name": "越权物资"}, cookie=member)
        self.assertEqual(403, st)
        # 审计链完整（空 = 无断链）
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        # 刷新重载：五页连续两次全部可达
        for _round in (1, 2):
            for route in ("/", "/events", "/sand", "/materials", "/review"):
                st, _, _ = _http("GET", f"{self.base}{route}", cookie=cmd)
                self.assertEqual(200, st, f"round{_round}:{route}")


if __name__ == "__main__":
    unittest.main()
