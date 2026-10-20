"""P16-T14 验收：台账关键状态流转镜像入工作区哈希链。

背景（v3 P16-T2/T7 记录的已知限制）：QR 签发/撤销与复盘台账只在独立 JSON
留痕，无防篡改能力。本任务把关键流转以审计事件镜像进工作区哈希链——
走 append_event 唯一写入口；台账仍是业务事实源，链上事件是审计副本。

覆盖：
- 事件域：start（含名册基线数）/ upgrade / close / issue-qr / revoke-qr
  镜像进该事件自己的快照链；start 前的名册登记跳过镜像（无空间，
  以注册表 JSON 为准，不为审计凭空造空间）；
- 复盘域：generate/issue/rect/exp/hyp/report 全部关键流转镜像进当前
  工作区链；db 绑定时随动作落盘、重载后仍在；
- 全程 verify_audit_chain 完整；复盘时间线渲染中文标签。
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

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace
from panda_brain import review_causality, review_meeting, review_closure

_SECRET = "p16-mirror-secret"


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


class AuditMirrorE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tmp = tmp
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-mirror", general_auth=self.auth),
            auth=self.auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            db_path=tmp / "default-ws.db",
        )
        self.state.registry.import_person_library([
            PersonRecord(name="张三", phone="13912340001",
                         unit="县防火办", home_position="扑救一组（一分区）"),
            PersonRecord(name="李四", phone="13912340002",
                         unit="林场哨所", home_position="供水组"),
        ])
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state, phone="13912340001")
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])

    def _event_kinds_from_snapshot(self) -> tuple[list[str], object]:
        dbs = list((self.tmp / "snapshots").glob("*.db"))
        self.assertTrue(dbs, "事件任务空间快照应已落地")
        space = load_workspace(dbs[0], general_auth=self.auth)
        kinds = [e.kind for e in space.events]
        return kinds, space

    def _act(self, action: str, **form):
        form = {"event_id": "fire-001", "action": action, **form}
        st, _, body = _http("POST", f"{self.base}/events/action",
                            form, cookie=self.cmd)
        self.assertEqual(303, st, body)

    def _owner_call(self, method, *args, **kwargs):
        from tests.review_support import owner_token
        token=owner_token(self.state)
        self.state._pending_review_actor=('owner',self.state.member_sessions[token].name,self.state.active_event_id,token)
        try:
            return method(*args,**kwargs)
        finally:
            self.state._pending_review_actor=None

    def _review_http(self, method, url, form=None, cookie=''):
        from tests.review_support import submit
        return submit(self.state,_http,method,url,form,cookie)

    def _owner_session(self):
        from tests.review_support import owner_token
        return 'member_session='+owner_token(self.state)

    def _basis(self, kind, target_id):
        from tests.review_support import owner_token
        token = owner_token(self.state)
        meeting = review_meeting.record_note(self.state, token, dict(
            subject='匿名复盘核查', provided_by='张三', feedback='核对模拟原始记录',
            evidence_refs='evt-001', occurred_at='', request_id=f'mirror-meeting-{kind}-{target_id}'))
        return review_causality.record_assessment(self.state, token, dict(
            target_kind=kind, target_id=target_id, support_refs='audit#1',
            counter_refs='', counter_note='暂无反证，继续核查', alternatives='也可能是模拟输入延迟',
            missing_info='', verify_targets='张三及原始记录', meeting_ref=meeting.id,
            meeting_finding='会议对照原始记录', causal_layer='direct' if kind == 'hypothesis' else 'undetermined',
            expected_object_version='0', request_id=f'mirror-basis-{kind}-{target_id}'))

    # -- 事件域镜像 -------------------------------------------------------

    def test_start_mirrors_with_roster_baseline(self):
        # start 前：名册登记不镜像（尚无任务空间），注册表 JSON 为准
        st, _, _ = _http(
            "POST", f"{self.base}/events/fire-001/roster/all-add", {},
            cookie=self.cmd)
        self.assertEqual(403, st)  # ordinary login cannot determine personnel
        self.state.registry.populate_roster_all("fire-001", added_by="anonymous-preexisting-fixture")
        self.state._save_registry()
        self._act("start")
        kinds, space = self._event_kinds_from_snapshot()
        self.assertIn("FireEventStarted", kinds)
        started = [e for e in space.events if e.kind == "FireEventStarted"][0]
        self.assertEqual(2, started.payload["roster_count"])
        self.assertNotIn("RosterEntriesAdded", kinds)   # start 前登记未镜像
        self.assertEqual((), space.verify_audit_chain())

    def test_qr_and_lifecycle_mirror_into_event_chain(self):
        self._act("start")
        self._act("issue-qr")
        kinds, space = self._event_kinds_from_snapshot()
        self.assertIn("EventQRIssued", kinds)
        issued = [e for e in space.events if e.kind == "EventQRIssued"][0]
        qr_id = issued.payload["qr_id"]
        self.assertTrue(qr_id)
        self._act("revoke-qr", qr_id=qr_id)
        self._act("upgrade")
        self._act("close")
        kinds, space = self._event_kinds_from_snapshot()
        for kind in ("EventQRRevoked", "FireEventUpgraded", "FireEventClosed"):
            self.assertIn(kind, kinds)
        self.assertEqual((), space.verify_audit_chain())

    def test_post_start_roster_ops_mirror(self):
        self._act("start")
        st, _, _ = personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/all-add", {},
            cookie=self.cmd)
        self.assertEqual(303, st)
        st, _, _ = personnel_submit(self.state, _http,
            "POST", f"{self.base}/events/fire-001/roster/assign",
            {"phone": "13912340001", "pos_code": "pjjz-1"}, cookie=self.cmd)
        self.assertEqual(303, st)
        kinds, space = self._event_kinds_from_snapshot()
        from panda_brain.personnel_management import inspect
        rows=inspect(space)
        self.assertEqual(3,len(rows))
        self.assertEqual("assign_roster", rows[-1][2]["operation"])
        self.assertEqual("pjjz-1", rows[-1][2]["details"]["pos_code"])
        self.assertEqual((), space.verify_audit_chain())

    # -- 复盘域镜像 -------------------------------------------------------

    def _enter_event(self):
        self._act("start")
        self._act("enter")

    def _draft_annotation(self):
        _http("POST", f"{self.base}/sand/annotate",
              {"kind": "fire_point", "title": "草稿火点", "points": "500,300",
               "certainty": "", "note": ""}, cookie=self.cmd)

    def test_review_full_flow_mirrors_all_transitions(self):
        self._enter_event()
        self._draft_annotation()
        st, _, _ = self._review_http("POST", f"{self.base}/review/generate", {},
                         cookie=self._owner_session())
        self.assertEqual(303, st)
        iid = next(iter(self.state.review_ledger.issues))
        basis = self._basis('issue', iid)
        self._owner_call(self.state.handle_review_issue_status, iid, 'confirmed',
                         reason='匿名核查通过', assessment_ref=basis.id)
        hid = self._owner_call(self.state.handle_review_hyp_new,
            statement="标绘停滞与岗位未解析同期出现",
            related_issues=iid, to_verify="核对作者岗位\r\n查当日交接记录")
        self._owner_call(self.state.handle_review_hyp_resolve, hid, "核对作者岗位", "同组")
        self._owner_call(self.state.handle_review_hyp_resolve, hid, "查当日交接记录", "未覆盖")
        hyp_basis = self._basis('hypothesis', hid)
        self._owner_call(self.state.handle_review_hyp_status, hid, "verified",
                         reason='时序与其他解释已核', assessment_ref=hyp_basis.id)
        token = self._owner_session().split('=',1)[1]
        rid = review_closure.create_rect_draft(self.state, token, iid, hid).rect_id
        review_closure.publish_rect(self.state, token, rid,
            measures='核对模拟预配原始记录', owner_unit='县防火办', owner_person='张三',
            responsible_phone='13912340001', deadline='2026-12-31',
            acceptance_standard='逐项核对一致', verifier_name='李四',
            verifier_phone='13912340002')
        review_closure.record_rect_feedback(self.state, token, rid,
            provided_by='张三', duty='责任人', content='已完成', evidence='audit#1', completed=True)
        review_closure.record_rect_verification(self.state, token, rid,
            verifier='李四', outcome='passed', finding='符合标准', evidence='audit#1')
        eid = review_closure.propose_experience(self.state, token, source_kind='rect', source_id=rid).exp_id
        review_closure.revise_experience(self.state, token, eid,
            content='先核对模拟来源', applicability='来源可追溯', limitations='来源不明时不适用',
            condition_tags='来源可追溯=是', evidence='audit#1', reason='补足条件')
        review_closure.decide_experience(self.state, token, eid, target='confirmed', reason='人工核对')
        from dataclasses import replace
        event = self.state.registry.events[self.state.active_event_id]
        self.state.registry.events[event.event_id] = replace(event, status='closed')
        rpt = self._owner_call(self.state.handle_report_generate, )
        self._owner_call(self.state.handle_report_edit, rpt, "# 人工修订\n内容更新")
        self._owner_call(self.state.handle_report_status, rpt, "pending_review")

        kinds = [e.kind for e in self.state.workspace.events]
        for kind in (
            "ReviewIssuesGenerated", "ReviewIssueStatusChanged",
            "ReviewRectDrafted", "ReviewRectPublished", "ReviewRectFeedbackRecorded",
            "ReviewRectVerificationRecorded", "ReviewExperienceProposed", "ReviewExperienceRevised",
            "ReviewExperienceStatusChanged", "ReviewHypothesisRecorded",
            "ReviewHypothesisVerifiedItem", "ReviewHypothesisStatusChanged",
            "ReviewReportVersioned", "ReviewReportEdited",
            "ReviewReportStatusChanged",
        ):
            self.assertIn(kind, kinds, kind)
        self.assertEqual((), self.state.workspace.verify_audit_chain())

        # 状态与原因进入载荷
        confirmed = [e for e in self.state.workspace.events
                     if e.kind == "ReviewIssueStatusChanged"][0]
        self.assertEqual("confirmed", confirmed.payload["to"])

    def test_review_mirror_persists_with_db_binding(self):
        self._enter_event()
        self._draft_annotation()
        self._review_http('POST', f'{self.base}/review/generate', {}, cookie=self._owner_session())
        iid = next(iter(self.state.review_ledger.issues))
        basis = self._basis('issue', iid)
        self._owner_call(self.state.handle_review_issue_status, iid, 'confirmed',
                         reason='匿名核查通过', assessment_ref=basis.id)
        token = self._owner_session().split('=',1)[1]
        eid = review_closure.propose_experience(self.state, token, source_kind='issue', source_id=iid).exp_id
        review_closure.revise_experience(self.state, token, eid,
            content='匿名输入须复核', applicability='来源可追溯', limitations='来源不明时不适用',
            condition_tags='来源可追溯=是', evidence='audit#1', reason='补足条件')
        review_closure.decide_experience(self.state, token, eid,
            target='confirmed', reason='人工确认')
        review_closure.decide_experience(self.state, token, eid,
            target='expired', reason='匿名反例', evidence='audit#1')
        # db_path 已绑定：重载同一快照，镜像事件仍在且链完整
        space = load_workspace(self.tmp / "default-ws.db",
                               general_auth=self.auth)
        kinds = [e.kind for e in space.events]
        self.assertIn("ReviewExperienceProposed", kinds)
        self.assertIn("ReviewExperienceStatusChanged", kinds)
        changed = [e for e in space.events
                   if e.kind == "ReviewExperienceStatusChanged"][-1]
        self.assertEqual("expired", changed.payload["to"])
        self.assertEqual((), space.verify_audit_chain())

    def test_timeline_labels_render_for_mirrors(self):
        self._enter_event()
        self._draft_annotation()
        self._review_http("POST", f"{self.base}/review/generate", {}, cookie=self._owner_session())
        iid = next(iter(self.state.review_ledger.issues))
        basis = self._basis('issue', iid)
        self._owner_call(self.state.handle_review_issue_status, iid, 'rejected',
                         reason='模拟误报', assessment_ref=basis.id)
        st, _, body = _http("GET", f"{self.base}/review?tab=timeline",
                            cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("规则扫描产出候选问题", body)
        self.assertIn("问题状态流转（镜像）", body)


if __name__ == "__main__":
    unittest.main()
