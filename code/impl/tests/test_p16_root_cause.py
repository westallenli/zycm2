"""P16-R1 验收：复盘根因分析域（doc 05 §5——相关·非因果·责任认定不入 AI 卡）。

域级：
- 假设校验与编号唯一；
- 「已验证」因果硬门禁：无待验证事实清单拒绝、未全部核实拒绝；
- 判定不成立必须附依据；反证推翻留痕；新证据重开留痕；
- 规则候选只在两条独立信号同期出现时产出弱相关假设；
- 持久化往返 + 旧格式（无 hypotheses 键）向后兼容。

HTTP e2e：
- /review?tab=root 三档角色；成员只读；
- 登记假设→逐条记核实→被门禁拦→补足后判已验证 全链路；
- 导出报告第十章条件出现；复盘问询路由 root 主题且按角色过滤。
"""

from __future__ import annotations
from tests.duty_support import duty_cookie
from tests.confirmed_fire_support import receive_confirmed_fixture
from tests.review_support import owner_token, submit

import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

from panda_brain import review_domain as rvd
from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace
from panda_brain import review_causality, review_meeting

_SECRET = "p16-root-secret"


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


def _hyp(statement="命令异常与信息缺口同期出现", **kw) -> rvd.RootCauseHypothesis:
    return rvd.RootCauseHypothesis(hyp_id=kw.pop("hyp_id", "HYP-T-01"),
                                   statement=statement, **kw)


class RootCauseDomain(unittest.TestCase):
    """doc 05 §5 状态机与因果纪律硬门禁。"""

    def setUp(self):
        self.ledger = rvd.ReviewLedger()

    def test_validation_rejects_bad_fields(self):
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.add_hypothesis(_hyp(statement="  "))
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.add_hypothesis(_hyp(correlation="必然"))
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.add_hypothesis(_hyp(to_verify=(" ",)))
        self.ledger.add_hypothesis(_hyp())
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.add_hypothesis(_hyp())  # 编号唯一

    def test_verify_gate_blocks_empty_list(self):
        self.ledger.add_hypothesis(_hyp())     # 无待验证事实清单
        with self.assertRaises(rvd.ReviewDomainError) as cm:
            self.ledger.transition_hypothesis(
                "HYP-T-01", "verified", operator="测试", stamp="st")
        self.assertIn("待验证事实", str(cm.exception))

    def test_verify_gate_blocks_until_all_resolved(self):
        self.ledger.add_hypothesis(_hyp(to_verify=("核对态势版本", "查交接记录")))
        with self.assertRaises(rvd.ReviewDomainError) as cm:
            self.ledger.transition_hypothesis(
                "HYP-T-01", "verified", operator="测试", stamp="st")
        self.assertIn("2 条", str(cm.exception))
        self.ledger.resolve_verification(
            "HYP-T-01", "核对态势版本", "当时缺口清单确有两条未关闭",
            operator="测试", stamp="st1")
        with self.assertRaises(rvd.ReviewDomainError) as cm:
            self.ledger.transition_hypothesis(
                "HYP-T-01", "verified", operator="测试", stamp="st2")
        self.assertIn("1 条", str(cm.exception))
        # 重复核实拒绝
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.resolve_verification(
                "HYP-T-01", "核对态势版本", "重复", operator="测试", stamp="st3")
        self.ledger.resolve_verification(
            "HYP-T-01", "查交接记录", "交接记录未覆盖该岗位",
            operator="测试", stamp="st4")
        updated = self.ledger.transition_hypothesis(
            "HYP-T-01", "verified", operator="测试", stamp="st5")
        self.assertEqual("verified", updated.status)
        self.assertIn("待验证→已验证", updated.history[-1])
        self.assertEqual(2, len(updated.verification_log))

    def test_rejected_requires_reason_and_reopen_trail(self):
        self.ledger.add_hypothesis(_hyp())
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.transition_hypothesis(
                "HYP-T-01", "rejected", operator="测试", stamp="s1")
        updated = self.ledger.transition_hypothesis(
            "HYP-T-01", "rejected", operator="测试", stamp="s2",
            reason="回放显示缺口当时已闭合")
        self.assertEqual("回放显示缺口当时已闭合", updated.note)
        # 不成立 → 待验证（重开，需原因）；再判不成立仍需原因
        updated = self.ledger.transition_hypothesis(
            "HYP-T-01", "pending", operator="测试", stamp="s3",
            reason="新影像证据出现")
        self.assertEqual("pending", updated.status)
        self.assertIn("新影像证据出现", updated.history[-1])
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.transition_hypothesis(
                "HYP-T-01", "verified", operator="测试", stamp="s4")  # 未核实直接验证
        illegal = rvd.hyp_can_transition("rejected", "verified")
        self.assertFalse(illegal)

    def test_verified_can_be_overturned_by_counter_evidence(self):
        self.ledger.add_hypothesis(_hyp(to_verify=("核a",)))
        self.ledger.resolve_verification(
            "HYP-T-01", "核a", "结论x", operator="测试", stamp="s0")
        self.ledger.transition_hypothesis(
            "HYP-T-01", "verified", operator="测试", stamp="s1")
        with self.assertRaises(rvd.ReviewDomainError):
            self.ledger.resolve_verification(
                "HYP-T-01", "核a", "事后补", operator="测试", stamp="s2")
        updated = self.ledger.transition_hypothesis(
            "HYP-T-01", "rejected", operator="测试", stamp="s3",
            reason="后续事件构成反例")
        self.assertEqual("rejected", updated.status)

    def test_candidates_require_two_independent_signals(self):
        event = NS(event_id="fire-9")
        # 单信号：仅草稿标绘 → 无假设
        ws = NS(events=[], information_gaps={},
                annotations={"a": NS(status=NS(value="draft"))},
                roster_bindings={})
        self.assertEqual([], rvd.generate_hypothesis_candidates(ws, event))
        # 双信号：草稿标绘 × 岗位未解析 → R7 弱相关候选
        ws.roster_bindings = {"b1": NS(pos_code="")}
        got = rvd.generate_hypothesis_candidates(ws, event)
        self.assertEqual(1, len(got))
        hyp = got[0]
        self.assertEqual("HYP-R7-corr-draft-slot-fire-9", hyp.hyp_id)
        self.assertEqual("弱", hyp.correlation)
        self.assertTrue(hyp.to_verify)
        self.assertIn("ISSUE-R4-draft-annotations-fire-9", hyp.related_issues)
        # 双信号：异常命令 × 未关闭缺口 → R6
        ws2 = NS(events=[NS(kind="OrderAnomalyPaused", sequence=7)],
                 information_gaps={"g": NS(id="gap-1", status=NS(value="open"))},
                 annotations={}, roster_bindings={})
        got2 = rvd.generate_hypothesis_candidates(ws2, event)
        self.assertEqual(1, len(got2))
        self.assertEqual("HYP-R6-corr-gap-order-fire-9", got2[0].hyp_id)
        self.assertIn("audit#7", got2[0].evidence_ids)

    def test_persistence_roundtrip_and_backward_compat(self):
        self.ledger.add_issue(rvd.ReviewIssue(
            issue_id="ISSUE-X-1", title="旧问题", category="指挥",
            stage="命令", event_id="e1",
            trigger_basis="规则 R4", description="草稿标绘未提交"))
        self.ledger.add_hypothesis(_hyp(
            to_verify=("核a",), evidence_ids=("audit#3",),
            related_issues=("ISSUE-X-1",)))
        self.ledger.resolve_verification(
            "HYP-T-01", "核a", "结论x", operator="测试", stamp="s1")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reviews.json"
            self.ledger.save(path)
            loaded = rvd.ReviewLedger.load(path)
            hyp = loaded.hypotheses["HYP-T-01"]
            self.assertEqual(("核a",), hyp.resolved_items)
            self.assertEqual(("ISSUE-X-1",), hyp.related_issues)
            self.assertEqual(1, len(hyp.verification_log))
            self.assertEqual("旧问题", loaded.issues["ISSUE-X-1"].title)
            # 旧格式文件（无 hypotheses 键）加载不报错、不丢既有分册
            old = {
                "issues": {"ISSUE-Y-1": {
                    "issue_id": "ISSUE-Y-1", "title": "老数据",
                    "category": "作战", "stage": "态势", "event_id": "e2",
                    "trigger_basis": "规则 R4", "description": "旧版记录",
                }},
            }
            path_old = Path(td) / "old.json"
            path_old.write_text(json.dumps(old, ensure_ascii=False),
                                encoding="utf-8")
            legacy = rvd.ReviewLedger.load(path_old)
            self.assertEqual({}, legacy.hypotheses)
            self.assertEqual("老数据", legacy.issues["ISSUE-Y-1"].title)


class RootCausePageE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-root", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        self.state.registry.import_person_library(
            [PersonRecord(name="李四", phone="13912340002", unit="林场哨所")]
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
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": "13912340002", "password": DEMO_INITIAL_PASSWORD},
        )
        cookie = _cookie_of(headers, "member_session")
        self.state.member_sessions[cookie.split('=',1)[1]].bound_event_id = self.state.active_event_id
        return cookie

    def _owner_session(self) -> str:
        """A-2：指派李四(13912340002)为复盘负责人，返回其本人会话 cookie。"""
        return 'member_session=' + owner_token(self.state)

    def _review_http(self, method, url, form=None, cookie=''):
        return submit(self.state,_http,method,url,form,cookie=cookie)

    def _new_hyp(self, **over) -> str:
        form = {
            "statement": "标绘停滞与岗位未解析同期出现",
            "correlation": "弱",
            "evidence": "audit#3；workspace:roster_bindings",
            "counter_examples": "",
            "to_verify": "核对作者岗位\r\n查当日交接记录",
            "related_issues": "",
        }
        form.update(over)
        st, headers, _ = self._review_http("POST", f"{self.base}/review/hyp-new",
                               form, cookie=self._owner_session())
        self.assertEqual(303, st)
        location = headers.get("Location", "")
        self.assertIn("tab=root", location)
        return list(self.state.review_ledger.hypotheses)[0]

    def _basis(self, hyp_id, version=0):
        token = self._owner_session().split('=', 1)[1]
        meeting = review_meeting.record_note(self.state, token, dict(
            subject='匿名根因核查', provided_by='李四', feedback='核对模拟原始记录',
            evidence_refs='evt-001', occurred_at='', request_id=f'root-meeting-{hyp_id}-{version}'))
        return review_causality.record_assessment(self.state, token, dict(
            target_kind='hypothesis', target_id=hyp_id, support_refs='audit#1',
            counter_refs='', counter_note='暂无反证，持续核对',
            alternatives='也可能是记录延迟', missing_info='',
            verify_targets='李四及原始记录', meeting_ref=meeting.id,
            meeting_finding='匿名核查支持此候选', causal_layer='direct',
            expected_object_version=str(version), request_id=f'root-basis-{hyp_id}-{version}'))

    # -- 页面与门禁 -------------------------------------------------------

    def test_tab_visibility_and_role_gates(self):
        st, headers, _ = _http("GET", f"{self.base}/review?tab=root")
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))
        st, _, body = _http("GET", f"{self.base}/review?tab=root", cookie=self.cmd)
        self.assertEqual(200, st)
        for needle in ("根因分析（非定责）", "三类结论边界", "因果纪律",
                       "根因假设"):
            self.assertIn(needle, body)
        self.assertNotIn('action="/review/hyp-new"',body)
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/review?tab=root", cookie=member)
        self.assertEqual(200, st)
        self.assertNotIn("/review/hyp-new", body)  # 成员无表单
        # 成员写操作一律 403
        for route, form in (
            ("/review/hyp-new", {"statement": "x"}),
            ("/review/hyp-status", {"hyp_id": "HYP-x", "target": "verified"}),
            ("/review/hyp-resolve", {"hyp_id": "HYP-x", "item": "i",
                                     "answer": "a"}),
        ):
            st, _, _ = _http("POST", f"{self.base}{route}", form, cookie=member)
            self.assertEqual(403, st)

    def test_full_chain_register_resolve_gate_verify(self):
        hyp_id = self._new_hyp()
        basis = self._basis(hyp_id)
        ledger = self.state.review_ledger
        hyp = ledger.hypotheses[hyp_id]
        self.assertEqual("pending", hyp.status)
        self.assertEqual(("核对作者岗位", "查当日交接记录"), hyp.to_verify)
        self.assertEqual(("audit#3", "workspace:roster_bindings"),
                         hyp.evidence_ids)
        self.assertEqual("manual", hyp.origin)

        # 未核实直接判「已验证」→ 因果门禁拦截 409
        st, _, body = self._review_http("POST", f"{self.base}/review/hyp-status",
                            {"hyp_id": hyp_id, "target": "verified", "reason": "核查",
                             "assessment_ref": basis.id},
                            cookie=self._owner_session())
        self.assertEqual(409, st)
        self.assertIn("待验证事实", body)

        # 逐条记核实；第二条空结论拒收
        st, _, _ = self._review_http("POST", f"{self.base}/review/hyp-resolve",
                         {"hyp_id": hyp_id, "item": "核对作者岗位",
                          "answer": "作者与未解析人员同组"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        st, _, body = self._review_http("POST", f"{self.base}/review/hyp-resolve",
                            {"hyp_id": hyp_id, "item": "查当日交接记录",
                             "answer": " "}, cookie=self._owner_session())
        self.assertEqual(409, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/hyp-resolve",
                         {"hyp_id": hyp_id, "item": "查当日交接记录",
                          "answer": "交接记录未覆盖该岗位"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        st, _, _ = self._review_http("POST", f"{self.base}/review/hyp-status",
                         {"hyp_id": hyp_id, "target": "verified", "reason": "时序与其他解释已核",
                          "assessment_ref": basis.id},
                         cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("verified",
                         self.state.review_ledger.hypotheses[hyp_id].status)
        self.assertEqual(2,
                         len(self.state.review_ledger.hypotheses[hyp_id].verification_log))

        # 落盘校验
        raw = (Path(self.state.reviews_path)).read_text(encoding="utf-8")
        self.assertIn(hyp_id, raw)
        self.assertIn("已核实【核对作者岗位】", raw)

        # 页面渲染：已验证卡不再带「相关·非因果」角标，核实项打勾
        st, _, body = _http("GET", f"{self.base}/review?tab=root", cookie=self.cmd)
        self.assertIn("☑ 核对作者岗位", body)
        self.assertNotIn("相关·非因果", body.split(hyp_id, 1)[1]
                         .split("</article>", 1)[0])

    def test_reject_flow_requires_reason_via_http(self):
        hyp_id = self._new_hyp(statement="临时假设")
        basis = self._basis(hyp_id)
        st, _, body = self._review_http("POST", f"{self.base}/review/hyp-status",
                            {"hyp_id": hyp_id, "target": "rejected",
                             "reason": "", "assessment_ref": basis.id}, cookie=self._owner_session())
        self.assertEqual(409, st)
        self.assertIn("理由", body)
        st, _, _ = self._review_http("POST", f"{self.base}/review/hyp-status",
                         {"hyp_id": hyp_id, "target": "rejected",
                          "reason": "核查后发现两信号无关联", "assessment_ref": basis.id},
                         cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("rejected",
                         self.state.review_ledger.hypotheses[hyp_id].status)
        # 重开
        self._basis(hyp_id, version=1)
        st, _, _ = self._review_http("POST", f"{self.base}/review/hyp-status",
                         {"hyp_id": hyp_id, "target": "pending",
                          "reason": "新证据"}, cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual("pending",
                         self.state.review_ledger.hypotheses[hyp_id].status)

    def test_generate_single_signal_produces_no_hypothesis(self):
        # 仅一条停留草稿（单信号）：问题候选产出，但根因假设不编造
        _http("POST", f"{self.base}/sand/annotate",
              {"kind": "fire_point", "title": "草稿火点", "points": "500,300",
               "certainty": "", "note": ""}, cookie=self.cmd)
        st, _, _ = self._review_http("POST", f"{self.base}/review/generate", {},
                         cookie=self._owner_session())
        self.assertEqual(303, st)
        self.assertEqual(1, len(self.state.review_ledger.issues))   # R4 命中
        self.assertEqual({}, self.state.review_ledger.hypotheses)   # 单信号不产

    def test_export_chapter_conditional_and_deterministic(self):
        st, _, body = _http("GET", f"{self.base}/export/review", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertNotIn("## 十、根因核查", body)   # 无假设时不输出章节
        self._new_hyp()
        st, _, body1 = _http("GET", f"{self.base}/export/review",
                             cookie=self.cmd)
        self.assertIn("## 十、根因核查与待验证假设", body1)
        self.assertIn("相关性记录·弱", body1)
        self.assertIn("待验证，相关线索·非因果结论", body1)
        self.assertIn("系统不自动定责", body1)
        st, _, body2 = _http("GET", f"{self.base}/export/review",
                             cookie=self.cmd)
        self.assertEqual(body1, body2)              # 同态逐字节一致

    def test_inquiry_routes_root_topic_with_role_filter(self):
        self._new_hyp()  # pending only
        ans = rvd.answer_review_inquiry(
            "这次火情的根因方面有什么发现？",
            ledger=self.state.review_ledger,
            workspace=self.state.workspace,
            event_id="fire-001", role="commander",
        )
        self.assertEqual("root", ans.topic)
        self.assertIn("均为相关性表述", ans.conclusion)
        self.assertIn("hyp:", "".join(ans.evidence))
        # 成员只见已验证：当前全是待验证 → 如实说无可查看并计入未纳入
        ans_m = rvd.answer_review_inquiry(
            "根因分析结果如何",
            ledger=self.state.review_ledger,
            workspace=self.state.workspace,
            event_id="fire-001", role="member",
        )
        self.assertIn("尚无可查看的根因假设", ans_m.conclusion)
        self.assertIn("未纳入", ans_m.excluded)
        # 命令意图依旧拒绝
        ans_c = rvd.answer_review_inquiry(
            "根据根因下达封山命令",
            ledger=self.state.review_ledger,
            workspace=self.state.workspace,
            event_id="fire-001", role="commander",
        )
        self.assertEqual("command_refused", ans_c.topic)


if __name__ == "__main__":
    unittest.main()
