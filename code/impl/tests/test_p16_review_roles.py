"""P16-T20 验收：复盘权限细化（05 号 §9）+ 登录小项（01 号 §7/§8）。

覆盖：复盘负责人指派（留痕+镜像+持久化）；负责人本人会话执行结论级动作
（署名「复盘负责人·姓名」）；普通成员结论级动作 403；授权维护人员授予/撤销；
公共屏免登录只读入口（/public → /review?projector=1）；指挥员登录落点 /events
（01 号 §7）；会话 TTL 过期吊销；锁定账号解锁入口。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p20-secret"


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
            return resp.status, resp.headers.get("Location", ""), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode("utf-8")


class ReviewRolesE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = PanelState(
            ScenarioWorkspace("p20", general_auth=GeneralEntryAuth(secret=_SECRET)),
            auth=GeneralEntryAuth(secret=_SECRET),
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        from panda_brain.models import PersonRecord
        self.state.registry.import_person_library([
            PersonRecord(name="王负责人", phone="13912340001", unit="县防火办"),
            PersonRecord(name="赵维护", phone="13912340002", unit="林场哨所"),
            PersonRecord(name="孙队员", phone="13912340003", unit="扑救一组"),
        ])
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        self.state.registry.save(self.state.registry_path)

    def _cmd_post(self, route, form):
        return _http("POST", f"{self.base}{route}", form, cookie=self._get_cmd_cookie())

    def _get_cmd_cookie(self):
        if getattr(self, "_cmd_cache", None):
            return self._cmd_cache
        req = urllib.request.Request(
            f"{self.base}/login",
            data=urllib.parse.urlencode({"username": "admin", "secret": _SECRET}).encode(),
            method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
        self._cmd_cache = raw.split(";")[0]
        return self._cmd_cache

    def _member_cookie(self, phone, password="123456"):
        req = urllib.request.Request(
            f"{self.base}/login/member",
            data=urllib.parse.urlencode({"phone": phone, "password": password}).encode(),
            method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303):
                raw = exc.headers.get("Set-Cookie", "")
                exc.read()
                return raw.split(";")[0], None   # 登录成功即重定向
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
            return "", exc.code
        return raw.split(";")[0], None

    # -- 复盘负责人 -----------------------------------------------------------

    def test_owner_assign_persists_mirrors_and_labels(self):
        st, _, _ = self._cmd_post("/review/owner", {"phone": "13912340001"})
        self.assertEqual(303, st)
        self.assertEqual("13912340001", self.state.review_ledger.owner_phone)
        # 持久化
        reloaded = type(self.state.review_ledger).load(self.state.reviews_path)
        self.assertEqual("13912340001", reloaded.owner_phone)
        # 时间线标签注册
        from panda_brain.review_domain import _TIMELINE_LABELS
        self.assertEqual("复盘负责人指派", _TIMELINE_LABELS.get("ReviewOwnerAssigned"))

    def test_owner_member_session_executes_and_signs(self):
        self._cmd_post("/review/exp-new",
                       {"content": "无人机首侦前先确认通信盲区"})
        self._cmd_post("/review/owner", {"phone": "13912340001"})
        exp_id = next(iter(self.state.review_ledger.experiences))
        cookie, err = self._member_cookie("13912340001")
        self.assertIsNone(err)
        st, _, _ = _http("POST", f"{self.base}/review/exp-status",
                         {"exp_id": exp_id, "target": "confirmed"}, cookie=cookie)
        self.assertEqual(303, st)
        exp = self.state.review_ledger.experiences[exp_id]
        self.assertEqual("confirmed", exp.status)
        self.assertEqual("复盘负责人·王负责人", exp.confirmed_by)

    def test_non_owner_member_gets_403_on_conclusion_actions(self):
        self._cmd_post("/review/exp-new", {"content": "候选经验X"})
        exp_id = next(iter(self.state.review_ledger.experiences))
        cookie, err = self._member_cookie("13912340003")   # 未指派为负责人的成员
        self.assertIsNone(err)
        st, _, body = _http("POST", f"{self.base}/review/exp-status",
                            {"exp_id": exp_id, "target": "confirmed"}, cookie=cookie)
        self.assertEqual(403, st)
        self.assertIn("复盘负责人", body)

    def test_a2_commander_blocked_on_all_six_conclusion_routes(self):
        """P17-P5：六类结论级动作指挥员会话一律 403（A-2 严格落实）。"""
        # 造数据：假设/问题/整改/经验/报告各一
        self._cmd_post("/review/hyp-new",
                       {"statement": "标绘停滞与岗位未解析同期出现"})
        from panda_brain.review_domain import ReviewIssue
        if not self.state.review_ledger.issues:
            self.state.review_ledger.add_issue(ReviewIssue(
                issue_id="ISSUE-a2-fire-001", title="测试问题",
                category="指挥", stage="处置", event_id="fire-001",
                trigger_basis="test", description="x"))
            self._cmd_post("/review/issue-status",
                           {"issue_id": "ISSUE-a2-fire-001", "target": "confirmed"})
        _iid = next(iter(self.state.review_ledger.issues))
        self._cmd_post("/review/rect-new", {"measures": "修订流程", "related_id": _iid})
        self._cmd_post("/review/exp-new", {"content": "经验候选A2"})
        self._cmd_post("/review/report-generate", {})
        hyp_id = next(iter(self.state.review_ledger.hypotheses))
        rect_id = next(iter(self.state.review_ledger.rects))
        exp_id = next(iter(self.state.review_ledger.experiences))
        report_id = next(iter(self.state.review_ledger.reports))
        cases = [
            ("/review/hyp-status", {"hyp_id": hyp_id, "target": "rejected",
                                    "reason": "无关联"}),
            ("/review/hyp-resolve", {"hyp_id": hyp_id, "item": "i", "answer": "a"}),
            ("/review/rect-status", {"rect_id": rect_id, "target": "in_progress"}),
            ("/review/exp-status", {"exp_id": exp_id, "target": "confirmed"}),
            ("/review/report-edit", {"report_id": report_id,
                                     "content_md": "x"}),
            ("/review/report-status", {"report_id": report_id,
                                       "target": "pending_review"}),
        ]
        for route, form in cases:
            with self.subTest(route=route):
                st, _, body = self._cmd_post(route, form)
                self.assertEqual(403, st)
                self.assertIn("仅复盘负责人本人可执行", body)

    # -- 授权维护人员 -----------------------------------------------------------

    def test_maintenance_grant_revoke_persist(self):
        st, _, _ = self._cmd_post("/review/maintainer", {"phone": "13912340002"})
        self.assertEqual(303, st)
        self.assertIn("13912340002", self.state.registry.maintenance_phones)
        st, _, _ = self._cmd_post("/review/maintainer",
                                  {"phone": "13912340002", "revoke": "1"})
        self.assertEqual(303, st)
        self.assertNotIn("13912340002", self.state.registry.maintenance_phones)
        # 持久化往返
        self.state.registry.save(self.state.registry_path)
        reloaded = type(self.state.registry).load(self.state.registry_path)
        self.assertNotIn("13912340002", reloaded.maintenance_phones)

    # -- 公共屏免登录只读入口 ---------------------------------------------------

    def test_public_screen_anonymous_readonly(self):
        st, loc, _ = _http("GET", f"{self.base}/public")
        self.assertIn(st, (302, 303))
        self.assertIn("/review?projector=1", loc)
        st, _, body = _http("GET", f"{self.base}/review?projector=1")
        self.assertEqual(200, st)
        self.assertIn("公共屏", body)
        self.assertIn("自动分析硬约束", body)
        self.assertIn("✗ 自动定责", body)

    # -- 登录落点 / 会话过期 / 账号解锁 -----------------------------------------

    def test_commander_lands_on_events(self):
        req = urllib.request.Request(
            f"{self.base}/login",
            data=urllib.parse.urlencode({"username": "admin", "secret": _SECRET}).encode(),
            method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            loc = resp.headers.get("Location", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location", "")
            exc.read()
        self.assertTrue(loc.startswith("/events"), loc)   # doc 01 §7

    def test_session_ttl_expiry_revokes(self):
        token_str = self._get_cmd_cookie()
        token = token_str.split("=")[-1]
        self.assertIn(token, self.state.sessions)
        self.state.session_issued_at[token] = (
            time.monotonic() - self.state.SESSION_TTL_SECONDS - 1)
        self.assertFalse(self.state.session_valid(token))
        self.assertNotIn(token, self.state.sessions)      # 过期即吊销

    def test_unlock_account_flow(self):
        # 连续失败锁号
        for _ in range(10):
            self._member_cookie("13912340003", password="wrong-pass")
        cookie, err = self._member_cookie("13912340003")
        self.assertEqual(401, err)   # 锁定后拒绝
        cred = self.state.registry.credentials["13912340003"]
        self.assertTrue(cred.locked)
        # 指挥员解锁
        st, _, _ = self._cmd_post("/account/unlock", {"phone": "13912340003"})
        self.assertEqual(303, st)
        self.assertFalse(self.state.registry.credentials["13912340003"].locked)
        cookie2, err2 = self._member_cookie("13912340003")
        self.assertIsNone(err2)


if __name__ == "__main__":
    unittest.main()
