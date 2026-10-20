"""P16-T7 验收：复盘与经验页（doc 05——摘要/时间线/问题/整改/经验）。"""

from __future__ import annotations
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture

import datetime as _dt
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
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace
from panda_brain import review_causality, review_meeting

_SECRET = "p16-review-secret"


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


class ReviewPageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-review", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        self.state.registry.import_person_library(
            [PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
             PersonRecord(name="王五", phone="13912340003", unit="扑救一组")]
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

    def _member_session(self, phone: str = "13912340003") -> str:
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": phone, "password": DEMO_INITIAL_PASSWORD},
        )
        cookie = _cookie_of(headers, "member_session")
        if cookie:
            self.state.member_sessions[cookie.split('=',1)[1]].bound_event_id='fire-001'
        return cookie

    def _draft_annotation(self, title="草稿火点") -> None:
        _http("POST", f"{self.base}/sand/annotate",
              {"kind": "fire_point", "title": title, "points": "500,300",
               "certainty": "", "note": ""}, cookie=self.cmd)

    def _owner_session(self):
        from tests.review_support import owner_token
        return 'member_session='+owner_token(self.state)

    def _review_http(self, method, url, form=None, cookie=''):
        from tests.review_support import submit
        return submit(self.state,_http,method,url,form,cookie)

    def _assessment(self, kind, target_id, *, layer='undetermined'):
        token = self._owner_session().split('=', 1)[1]
        meeting = review_meeting.record_note(self.state, token, dict(
            subject='匿名核查', provided_by='王五', feedback='核对候选原始记录',
            evidence_refs='evt-001', occurred_at='',
            request_id=f'meeting-{kind}-{target_id}'))
        return review_causality.record_assessment(self.state, token, dict(
            target_kind=kind, target_id=target_id, support_refs='audit#1',
            counter_refs='', counter_note='暂无反证，继续核对',
            alternatives='也可能为模拟输入延迟', missing_info='',
            verify_targets='王五及原始记录', meeting_ref=meeting.id,
            meeting_finding='会议核对原始记录支持此候选', causal_layer=layer,
            expected_object_version='0', request_id=f'basis-{kind}-{target_id}'))

    # -- 门禁与页面骨架 -------------------------------------------------

    def test_anonymous_redirects_to_login(self):
        st, headers, _ = _http("GET", f"{self.base}/review")
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))

    def test_commander_sees_all_tabs_and_honest_digest(self):
        st, _, body = _http("GET", f"{self.base}/review", cookie=self.cmd)
        self.assertEqual(200, st)
        for needle in ("还原过程", "分析问题与建议", "落实整改",
                       "总结经验", "输出报告", "发生时间缺失"):
            self.assertIn(needle, body)
        st, _, body = _http("GET", f"{self.base}/review?tab=timeline", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("审计链", body)

    def test_generate_without_triggers_is_honest(self):
        # 空工作区无任何规则触发：不产出候选，不伪造问题
        st, _, _ = self._review_http("POST", f"{self.base}/review/generate", {}, cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual({}, self.state.review_ledger.issues)

    def test_issue_lifecycle_full_chain(self):
        # 触发 R4：一条停留草稿的标绘
        self._draft_annotation()
        st, _, body = self._review_http("POST", f"{self.base}/review/generate", {}, cookie=self._owner_session())
        self.assertEqual(303, st)
        issues = list(self.state.review_ledger.issues.values())
        self.assertEqual(1, len(issues))
        issue = issues[0]
        self.assertEqual("pending", issue.review_status)
        self.assertIn("review-rules-v1", issue.trigger_basis)
        iid = issue.issue_id

        # 非法跳转：待核验直接关闭 → 409
        st, _, body = self._review_http("POST", f"{self.base}/review/issue-status",
                            {"issue_id": iid, "target": "closed"}, cookie=self._owner_session())
        self.assertEqual(409, st)
        # 否决必须附原因
        st, _, body = self._review_http("POST", f"{self.base}/review/issue-status",
                            {"issue_id": iid, "target": "rejected", "reason": ""},
                            cookie=self._owner_session())
        self.assertEqual(409, st)
        basis = self._assessment('issue', iid)
        st, _, _ = self._review_http("POST", f"{self.base}/review/issue-status",
            {"issue_id": iid, "target": "confirmed", "reason": "匿名依据已核对",
             "assessment_ref": basis.id}, cookie=self._owner_session())
        self.assertEqual(303, st)
        # 事件绑定后不再通过问题卡直达关闭，须走整改验收。
        st, _, _ = self._review_http("POST", f"{self.base}/review/issue-status",
                         {"issue_id": iid, "target": "closed",
                          "reason": "整改完成归档"}, cookie=self._owner_session())
        self.assertEqual(409, st)
        self.assertEqual("confirmed",
                         self.state.review_ledger.issues[iid].review_status)
        # 没有新核查版本不能重开。
        st, _, _ = self._review_http("POST", f"{self.base}/review/issue-reopen",
                         {"issue_id": iid, "reason": "新影像证据出现"},
                         cookie=self._owner_session())
        self.assertEqual(409, st)
        self.assertEqual("confirmed",
                         self.state.review_ledger.issues[iid].review_status)

    # -- 整改闭环 -------------------------------------------------------

    def _confirmed_issue(self) -> str:
        self._draft_annotation()
        self._review_http("POST", f"{self.base}/review/generate", {}, cookie=self._owner_session())
        iid = next(iter(self.state.review_ledger.issues))
        basis = self._assessment('issue', iid)
        st, _, _ = self._review_http("POST", f"{self.base}/review/issue-status",
              {"issue_id": iid, "target": "confirmed", "reason": "匿名核查通过",
               "assessment_ref": basis.id}, cookie=self._owner_session())
        self.assertEqual(303, st)
        return iid

    def _confirmed_pair(self):
        iid = self._confirmed_issue()
        st, _, _ = self._review_http('POST', f'{self.base}/review/hyp-new', dict(
            statement='匿名记录复核环节待核', related_issues=iid,
            to_verify='核对原始记录'), cookie=self._owner_session())
        self.assertEqual(303, st)
        hid = next(iter(self.state.review_ledger.hypotheses))
        st, _, _ = self._review_http('POST', f'{self.base}/review/hyp-resolve', dict(
            hyp_id=hid, item='核对原始记录', answer='原始记录支持'), cookie=self._owner_session())
        self.assertEqual(303, st)
        basis = self._assessment('hypothesis', hid, layer='direct')
        st, _, _ = self._review_http('POST', f'{self.base}/review/hyp-status', dict(
            hyp_id=hid, target='verified', reason='已核对其他解释',
            assessment_ref=basis.id), cookie=self._owner_session())
        self.assertEqual(303, st)
        return iid, hid

    def test_rect_lifecycle_and_overdue(self):
        iid, hid = self._confirmed_pair()
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        st, _, _ = self._review_http(
            "POST", f"{self.base}/review/rect-draft",
            {"issue_id": iid, "hyp_id": hid},
            cookie=self._owner_session(),
        )
        self.assertEqual(303, st)
        task = next(iter(self.state.review_ledger.rects.values()))
        self.assertEqual("draft", task.status)
        rid = task.rect_id
        st, _, _ = self._review_http('POST', f'{self.base}/review/rect-publish', dict(
            rect_id=rid, measures='核对模拟名册原始记录', owner_unit='扑救一组',
            owner_person='王五', responsible_phone='13912340003',
            deadline=yesterday, acceptance_standard='逐项核对一致',
            verifier_name='李四', verifier_phone='13912340002'), cookie=self._owner_session())
        self.assertEqual(303, st)
        st, _, body = _http("GET", f"{self.base}/review?tab=rect", cookie=self.cmd)
        self.assertIn("超期", body)
        # 完成报告与指定验收分开，未通过退回整改。
        st, _, _ = self._review_http('POST', f'{self.base}/review/rect-feedback', dict(
            rect_id=rid, provided_by='王五', duty='责任人', content='已完成第一轮',
            evidence='audit#1', completed='1'), cookie=self._owner_session())
        self.assertEqual(303, st)
        st, _, _ = self._review_http('POST', f'{self.base}/review/rect-verify', dict(
            rect_id=rid, verifier='李四', outcome='failed', finding='一项未通过',
            evidence='audit#1'), cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual('in_progress', self.state.review_ledger.rects[rid].status)
        st, _, _ = self._review_http('POST', f'{self.base}/review/rect-status', dict(
            rect_id=rid, target='closed'), cookie=self._owner_session())
        self.assertEqual(409, st)
        self._review_http('POST', f'{self.base}/review/rect-feedback', dict(
            rect_id=rid, provided_by='王五', duty='责任人', content='已补核',
            evidence='audit#1', completed='1'), cookie=self._owner_session())
        self._review_http('POST', f'{self.base}/review/rect-verify', dict(
            rect_id=rid, verifier='李四', outcome='passed', finding='符合验收标准',
            evidence='audit#1'), cookie=self._owner_session())
        self.assertEqual('closed', self.state.review_ledger.rects[rid].status)
        history = self.state.review_ledger.rects[rid].history
        self.assertTrue(any("未通过" in h for h in history))

    def test_experience_three_states(self):
        iid = self._confirmed_issue()
        st, _, _ = self._review_http(
            "POST", f"{self.base}/review/exp-propose",
            {"source_kind": "issue", "source_id": iid}, cookie=self._owner_session(),
        )
        self.assertEqual(303, st)
        exp = next(iter(self.state.review_ledger.experiences.values()))
        self.assertEqual("candidate", exp.status)
        eid = exp.exp_id
        st, _, _ = self._review_http('POST', f'{self.base}/review/exp-revise', dict(
            exp_id=eid, content='匿名输入先做二次复核', applicability='来源需可追溯',
            limitations='来源不明时不适用', condition_tags='来源可追溯=是',
            evidence='audit#1', reason='补足条件'), cookie=self._owner_session())
        self.assertEqual(303, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/exp-decide",
                         {"exp_id": eid, "target": "confirmed", "reason": "人工核对来源"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("confirmed",
                         self.state.review_ledger.experiences[eid].status)
        # 已确认不能再确认（幂等保护语义：只有候选可确认）
        st, _, body = self._review_http("POST", f"{self.base}/review/exp-decide",
                            {"exp_id": eid, "target": "confirmed", "reason": "重复"},
                            cookie=self._owner_session())
        self.assertEqual(409, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/exp-decide",
                         {"exp_id": eid, "target": "expired", "reason": "匿名反例",
                          "evidence": "audit#1"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("expired",
                         self.state.review_ledger.experiences[eid].status)

    # -- 权限与持久化 ---------------------------------------------------

    def test_member_readonly_and_posts_denied(self):
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/review?tab=issues", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn("按规则生成候选", body)
        st, _, _ = self._review_http("POST", f"{self.base}/review/generate", {}, cookie=member)
        self.assertEqual(403, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/rect-new",
                         {"measures": "x"}, cookie=member)
        self.assertEqual(403, st)

    def test_ledger_persisted_to_disk(self):
        self._confirmed_issue()
        path = Path(self.state.reviews_path)
        self.assertTrue(path.exists())
        payload = path.read_text(encoding="utf-8")
        self.assertIn("ISSUE-", payload)

    # -- 报告导出（doc 05 §12） -----------------------------------------

    def test_export_review_markdown(self):
        # 匿名核查 → 问题/根因确认 → 整改草案发布 → 经验候选。
        iid, hid = self._confirmed_pair()
        self._review_http('POST', f'{self.base}/review/rect-draft', dict(
            issue_id=iid, hyp_id=hid), cookie=self._owner_session())
        rid = next(iter(self.state.review_ledger.rects))
        self._review_http('POST', f'{self.base}/review/rect-publish', dict(
            rect_id=rid, measures='修订名册预配流程', owner_unit='扑救一组',
            owner_person='王五', responsible_phone='13912340003',
            deadline='2026-12-31', acceptance_standard='名册逐项复核',
            verifier_name='李四', verifier_phone='13912340002'), cookie=self._owner_session())
        self._review_http('POST', f'{self.base}/review/exp-propose', dict(
            source_kind='issue', source_id=iid), cookie=self._owner_session())
        eid = next(iter(self.state.review_ledger.experiences))
        self._review_http('POST', f'{self.base}/review/exp-revise', dict(
            exp_id=eid, content='首侦前先确认模拟通信条件', applicability='来源可追溯',
            limitations='来源不明时不适用', condition_tags='来源可追溯=是',
            evidence='audit#1', reason='补足候选条件'), cookie=self._owner_session())
        st, headers, body = _http("GET", f"{self.base}/export/review",
                                  cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        for needle in (
            "系统草稿（缺证据处标未核实",
            "## 一、事件概况",
            "缺失（未补写）",
            "## 六、候选问题",
            "## 七、人工确认结论",
            "## 八、整改任务",
            "修订名册预配流程",
            "## 九、经验沉淀",
            "首侦前先确认模拟通信条件",
            "定责结论仅由复盘负责人手工形成",
        ):
            self.assertIn(needle, body)
        # 待核验候选与已确认结论分列，不混写
        self.assertIn("（无待核验候选）", body)  # 唯一问题已被人工确认

    def test_export_denied_for_member_and_anonymous(self):
        st, _, _ = _http("GET", f"{self.base}/export/review")
        self.assertEqual(404, st)
        member = self._member_session()
        st, _, _ = _http("GET", f"{self.base}/export/review", cookie=member)
        self.assertEqual(404, st)

    # -- 报告版本状态机（doc 05 §12） ----------------------------------

    def test_report_version_lifecycle(self):
        from dataclasses import replace
        self.state.registry.events['fire-001'] = replace(self.state.registry.events['fire-001'], status='closed')
        # 生成 v1 → 编辑 → 提交审核 → 发布 → 归档；旧版不可变
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-generate", {},
                         cookie=self._owner_session())
        self.assertEqual(303, st)
        r1 = next(iter(self.state.review_ledger.reports))
        self.assertEqual("ai_draft", self.state.review_ledger.reports[r1].status)
        self.assertIn("系统草稿", self.state.review_ledger.reports[r1].content_md)

        st, _, body = self._review_http(
            "POST", f"{self.base}/review/report-edit",
            {"report_id": r1,
             "content_md": self.state.review_ledger.reports[r1].content_md
             + "\n\n## 补充：指挥员人工补录结论\n- 现场核对无遗漏。"},
            cookie=self._owner_session(),
        )
        self.assertEqual(303, st)
        self.assertEqual("edited", self.state.review_ledger.reports[r1].status)

        for target in ("pending_review", "published"):
            st, _, _ = self._review_http("POST", f"{self.base}/review/report-status",
                             {"report_id": r1, "target": target}, cookie=self._owner_session())
            self.assertEqual(303, st)
        # 发布后编辑被拒（内容不可变）
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-edit",
                         {"report_id": r1, "content_md": "篡改"}, cookie=self._owner_session())
        self.assertEqual(409, st)
        # 非法跳转：published 直接 ai_draft 不存在此流转
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-status",
                         {"report_id": r1, "target": "edited"}, cookie=self._owner_session())
        self.assertEqual(409, st)

        st, _, _ = self._review_http("POST", f"{self.base}/review/report-status",
                         {"report_id": r1, "target": "archived"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("archived", self.state.review_ledger.reports[r1].status)

    def test_report_new_version_preserves_old(self):
        from dataclasses import replace
        self.state.registry.events['fire-001'] = replace(self.state.registry.events['fire-001'], status='closed')
        self._review_http("POST", f"{self.base}/review/report-generate", {}, cookie=self._owner_session())
        self._review_http("POST", f"{self.base}/review/report-generate", {}, cookie=self._owner_session())
        reports = sorted(self.state.review_ledger.reports.values(),
                         key=lambda r: r.version)
        self.assertEqual(2, len(reports))
        self.assertEqual([1, 2], [r.version for r in reports])
        self.assertNotEqual(reports[0].report_id, reports[1].report_id)
        # 两版内容独立快照，旧版不静默覆盖
        self.assertIn("v1", reports[0].report_id)
        self.assertIn("v2", reports[1].report_id)

    def test_report_tab_rendering_and_member_readonly(self):
        from dataclasses import replace
        self.state.registry.events['fire-001'] = replace(self.state.registry.events['fire-001'], status='closed')
        self._review_http("POST", f"{self.base}/review/report-generate", {}, cookie=self._owner_session())
        rid = next(iter(self.state.review_ledger.reports))
        st, _, body = _http(
            "GET", f"{self.base}/review?tab=report&rid={rid}", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn(rid, body)
        self.assertIn("系统草稿", body)
        # A-2：结论级动作仅负责人——指挥员无编辑/定版表单，有归属提示
        self.assertNotIn("保存人工编辑", body)
        self.assertNotIn("/review/report-edit", body)
        self.assertIn("报告范围", body)
        self.assertIn("下载 Word", body)
        owner = self._owner_session()
        st, _, body_own = _http(
            "GET", f"{self.base}/review?tab=report&rid={rid}", cookie=owner)
        self.assertEqual(200, st)
        self.assertIn("保存人工编辑", body_own)
        member = self._member_session("13912340003")   # 普通成员（非负责人）
        st, _, body = _http("GET", f"{self.base}/review?tab=report", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn("生成新版本", body)
        self.assertNotIn("提交审核", body)
        self.assertNotIn("保存人工编辑", body)
        st, _, _ = self._review_http("POST", f"{self.base}/review/report-generate", {},
                         cookie=member)
        self.assertEqual(403, st)

    # -- P16-T10 复盘问询（doc 05 §11：只读；七字段；命令意图不执行） ----

    def _seed_two_issues(self) -> None:
        """一条已确认 + 一条待核验（同事件）。"""
        self._confirmed_issue()
        # 规则按事件聚合幂等，第二条直接以领域构造补入（待核验态）
        from panda_brain.review_domain import ReviewIssue
        self.state.review_ledger.add_issue(ReviewIssue(
            issue_id="ISSUE-test-extra-fire-001", title="测试待核验问题",
            category="指挥", stage="处置", event_id="fire-001",
            trigger_basis="test", description="x",
        ))
        self.assertEqual(2, len(self.state.review_ledger.issues))

    def test_review_ask_command_intent_is_refused_and_readonly(self):
        path = Path(self.state.reviews_path)
        before = path.read_bytes() if path.exists() else b""
        token = self.cmd.split(';',1)[0].split('=',1)[1]
        st, _, _ = _http("POST", f"{self.base}/review/ask",
                         {"question": "给一中队下达拦截任务", "event_id":"fire-001",
                          "csrf":review_meeting.csrf(self.state,token)}, cookie=self.cmd)
        self.assertEqual(303, st)
        question, ans = self.state.review_qa_log[-1]
        self.assertEqual("command_refused", ans.topic)
        self.assertIn("不下达命令", ans.conclusion)
        after = path.read_bytes() if path.exists() else b""
        self.assertEqual(before, after)  # 只读不变式：台账字节不变

    def test_review_ask_analysis_verb_with_question_mark_routes_normally(self):
        # 含「下达」但带疑问词 → 分析性提问，不是命令意图
        _, ans = None, None
        state_ans = self.state.handle_review_ask("哪些命令是系统自动下达的？", "commander")
        self.assertNotEqual("command_refused", state_ans.topic)

    def test_review_ask_issues_topic_with_role_filter(self):
        self._seed_two_issues()
        ans = self.state.handle_review_ask("本次有哪些问题？", "commander")
        self.assertEqual("issues", ans.topic)
        self.assertEqual(2, len(ans.facts))
        self.assertIn('测试待核验问题', ans.conclusion)
        self.assertIn("issue:", " ".join(ans.evidence))
        member_ans = self.state.handle_review_ask("本次有哪些问题？", "member")
        self.assertEqual("issues", member_ans.topic)
        self.assertEqual(1, len(member_ans.facts))
        self.assertNotIn('测试待核验问题', member_ans.conclusion)
        self.assertIn("1 条", member_ans.excluded)  # 待核验未纳入成员可见范围

    def test_review_ask_common_single_event_sample_insufficient(self):
        self._seed_two_issues()
        ans = self.state.handle_review_ask("多次对比有什么共性规律", "commander")
        self.assertEqual("common", ans.topic)
        self.assertIn("只复盘本事件", ans.conclusion)

    def test_review_common_recurring_across_events_domain_level(self):
        from panda_brain.review_domain import (
            ReviewIssue, ReviewLedger, answer_review_inquiry,
        )
        ledger = ReviewLedger()
        base = dict(
            category="指挥", stage="处置", trigger_basis="test",
            description="x",
        )
        for eid in ("fire-001", "fire-002"):
            ledger.add_issue(ReviewIssue(
                issue_id=f"RVI-{eid}-v1", title="草稿标绘滞留",
                event_id=eid, **base,
            ))
        ans = answer_review_inquiry(
            "多次对比有什么共性规律", ledger=ledger,
            workspace=None, event_id="fire-002", role="commander",
        )
        self.assertEqual("common", ans.topic)
        self.assertIn("跨事件复现问题 1 类", ans.conclusion)
        self.assertIn("草稿标绘滞留", ans.conclusion)

    def test_review_ask_delegates_to_sandtable_same_data(self):
        ans = self.state.handle_review_ask("队伍在岗情况如何", "commander")
        self.assertEqual("personnel", ans.topic)
        self.assertIn("同源同权限", ans.scope)
        # 空工作区无活动绑定 → 沙盘引擎的诚实回答原样透传
        self.assertIn("尚无已入岗人员", ans.conclusion)

    def test_review_ask_unknown_topic_lists_supported(self):
        ans = self.state.handle_review_ask("今天天气怎么样", "commander")
        self.assertIn("无法从当前数据回答", ans.conclusion)
        for word in ("问题", "整改", "经验", "报告版本", "时间线"):
            self.assertIn(word, ans.conclusion)

    def test_review_ask_gate_anonymous_redirect_member_allowed(self):
        st, headers, _ = _http("POST", f"{self.base}/review/ask",
                               {"question": "本次有哪些问题"})
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))
        member = self._member_session()
        st, headers, _ = _http("POST", f"{self.base}/review/ask",
                               {"question": "整改进展如何", "event_id":"fire-001",
                                "csrf":review_meeting.csrf(self.state,member.split('=',1)[1])}, cookie=member)
        self.assertEqual(303, st)
        self.assertIn("tab=summary", headers.get("Location", ""))
        # 匿名被拦不留痕；成员成功提问留一条
        self.assertEqual(1, len(self.state.review_qa_log))

    def test_review_page_renders_ask_card_all_roles(self):
        st, _, body = _http("GET", f"{self.base}/review", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("还原过程", body)
        self.assertNotIn("/review/ask", body)
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/review", cookie=member)
        self.assertEqual(200, st)
        self.assertIn("复盘资料", body)
        self.assertNotIn("/review/ask", body)


if __name__ == "__main__":
    unittest.main()
