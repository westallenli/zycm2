"""P16-T2 验收：登录页定版 HTTP e2e——双通道登录 / 多火情管理 / 刷码入岗闭环。

对齐口径：页面设计定版 01_登录页_V0.3 + PRD §13.3。全部走真实 HTTP（make_server），
验证 fail-closed 门、幂等语义与注册表 JSON 落盘。
"""

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
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD, EventRegistry
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p16-events-secret"


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _cookie_of(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith(f"{name}="):
            value = part.strip().split("=", 1)[1]
            if value:
                return f"{name}={value}"
    return ""


def _build_state(tmp: Path) -> PanelState:
    auth = GeneralEntryAuth(secret=_SECRET)
    ws = ScenarioWorkspace("p16-events", general_auth=auth)
    state = PanelState(
        ws,
        auth=auth,
        registry_path=tmp / "reg.json",
        alarm_inbox_path=tmp / "alarms.json",
        materials_path=tmp / "materials.json",
        reviews_path=tmp / "reviews.json",
    )
    state.registry.import_person_library(
        [
            PersonRecord(
                name="张三",
                phone="13912340001",
                unit="县防火办",
                home_position="扑救一组（一分区）",
                is_leader=True,
            ),
            PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
        ]
    )
    return state


class FireEventFlowE2E(unittest.TestCase):
    """指挥员建会→签码→名册→成员登录→刷码确认→入岗→幂等→冲突 全链路。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = _build_state(tmp)
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        # 指挥员登录
        status, headers, _ = _http(
            "POST", f"{self.base}/login", {"secret": _SECRET}
        )
        self.assertEqual(303, status)
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        self.assertTrue(self.cmd)

    def _post_event_action(self, event_id: str, action: str, qr_id: str = ""):
        return _http(
            "POST",
            f"{self.base}/events/action",
            {"event_id": event_id, "action": action, "qr_id": qr_id, "csrf": next(m.custody_csrf for m in self.state.member_sessions.values() if m.phone in {g.subject_id for g in self.state._duty_grants})},
            cookie=self.cmd,
        )

    def test_full_commander_member_checkin_loop(self):
        # ① 建事件（登记中）
        received = receive_confirmed_fixture(self.state, {
                "name": "汉江上游山火",
                "report_no": "HC-01",
                "discovered_at": "2026-08-23T20:00:00+08:00",
                "location": "青岗县",
                "source": "",
            })
        self.assertEqual("CREATED", received["code"])
        record = self.state.registry.events["fire-001"]
        self.assertEqual("registered", record.status)

        # 注册表 JSON 已落盘
        reg_path = self.state.registry_path
        self.assertTrue(Path(reg_path).exists())
        payload = json.loads(Path(reg_path).read_text(encoding="utf-8"))
        self.assertIn("fire-001", payload["events"])

        # ② 登记中不可签发二维码 → 409 且文案点名状态
        status, _, body = self._post_event_action("fire-001", "issue-qr")
        self.assertEqual(409, status)
        self.assertIn("进行中／已升级", body)

        # ③ 启动处置自动签发首张二维码；手动补发仍可用。
        status, headers, _ = self._post_event_action("fire-001", "start")
        self.assertEqual(303, status)
        self.assertIn("/events?started=fire-001", headers.get("Location", ""))
        self.assertFalse(self.state.registry.qrs)
        # Legacy admin QR compatibility; new duty handoff has its own full flow test.
        self.assertEqual(303,self._post_event_action("fire-001","issue-qr")[0])
        qr_id = next(iter(self.state.registry.qrs))
        status, headers, _ = self._post_event_action("fire-001", "issue-qr")
        self.assertEqual(303, status)
        self.assertIn("/events?issued=qr-002", headers.get("Location", ""))

        # ④ 配置本事件名册（HTTP 路径；phones 传字符串，parse_qs 端自然成列表）
        status, _, _ = personnel_submit(self.state, _http,
            "POST",
            f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"},
            cookie=self.cmd,
        )
        self.assertEqual(303, status)
        roster = self.state.registry.rosters["fire-001"]
        self.assertIn("13912340001", roster.entries)

        # ⑤ 指挥员进入事件（任务空间落地）
        status, _, _ = self._post_event_action("fire-001", "enter")
        self.assertEqual(303, status)
        self.assertEqual("fire-001", self.state.active_event_id)

        # ⑥ 成员登录（初始口令 123456）
        status, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        self.assertEqual(303, status)
        self.member = _cookie_of(headers, "member_session")
        self.assertTrue(self.member)
        # 成员凭证哈希落盘（不含明文）
        payload = json.loads(Path(reg_path).read_text(encoding="utf-8"))
        cred = payload["credentials"]["13912340001"]
        self.assertTrue(cred["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertNotIn(DEMO_INITIAL_PASSWORD, json.dumps(payload, ensure_ascii=False))

        # ⑦ 刷码 → 确认卡（工作组—岗位）
        status, _, body = _http(
            "POST",
            f"{self.base}/checkin/scan",
            {"qr": qr_id},
            cookie=self.member,
        )
        self.assertEqual(200, status)
        self.assertIn("即将进入", body)
        self.assertIn("扑救一组（一分区）", body)
        self.assertIn("/checkin/confirm", body)

        # ⑧ 确认入岗 → 花名册绑定 + 到场时间
        version_before = self.state.workspace.version
        status, headers, _ = _http(
            "POST",
            f"{self.base}/checkin/confirm",
            {"qr": qr_id},
            cookie=self.member,
        )
        self.assertEqual(303, status)
        self.assertEqual("/", headers.get("Location"))
        bindings = [
            b for b in self.state.workspace.roster_bindings.values() if b.status == "active"
        ]
        self.assertEqual(1, len(bindings))
        self.assertEqual("13912340001", bindings[0].person.phone)
        session = self.state.member_session(self.member.split("=", 1)[1])
        self.assertEqual("fire-001", session.bound_event_id)
        self.assertTrue(session.arrived_at)
        self.assertGreater(self.state.workspace.version, version_before)  # 审计入链

        # ⑨ 重复刷码：幂等回空间，不重复写记录
        count_before = len(self.state.workspace.roster_bindings)
        status, headers, _ = _http(
            "POST",
            f"{self.base}/checkin/scan",
            {"qr": qr_id},
            cookie=self.member,
        )
        self.assertEqual(303, status)
        self.assertIn("/login?back=space", headers.get("Location", ""))
        self.assertEqual(count_before, len(self.state.workspace.roster_bindings))

    def test_cross_event_conflict_branch(self):
        # 复用主链路步骤建立「已入岗 fire-001」的会话
        self._seed_onboarded()
        # 建第二场火并进入、签码
        received = receive_confirmed_fixture(self.state, {
                "name": "北岭火情",
                "report_no": "BL-02",
                "discovered_at": "2026-08-23T21:00:00+08:00",
                "location": "北岭县",
                "source": "",
            })
        self.assertEqual("CREATED", received["code"])
        self._post_event_action("fire-002", "start")
        self._post_event_action("fire-002", "enter")
        self._post_event_action("fire-002", "issue-qr")
        qr2 = sorted(self.state.registry.qrs)[-1]

        # 用 fire-002 的码刷：跨事件冲突转人工
        status, _, body = _http(
            "POST",
            f"{self.base}/checkin/scan",
            {"qr": qr2},
            cookie=self.member,
        )
        self.assertEqual(200, status)
        self.assertIn("不允许自行改岗", body)

    def _seed_onboarded(self):
        """直接复用 e2e 主链路的内部步骤（不重复断言）。"""
        received = receive_confirmed_fixture(self.state, {
                "name": "汉江上游山火",
                "report_no": "HC-01",
                "discovered_at": "2026-08-23T20:00:00+08:00",
                "location": "青岗县",
                "source": "",
            })
        self.assertEqual("CREATED", received["code"])
        self._post_event_action("fire-001", "start")
        self._post_event_action("fire-001", "issue-qr")
        self.assertEqual(303,self._post_event_action("fire-001","issue-qr")[0])
        self.qr1 = next(iter(self.state.registry.qrs))
        personnel_submit(self.state, _http,
            "POST",
            f"{self.base}/events/fire-001/roster/add",
            {"phones": "13912340001"},
            cookie=self.cmd,
        )
        self._post_event_action("fire-001", "enter")
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340001", "password": DEMO_INITIAL_PASSWORD},
        )
        self.member = _cookie_of(headers, "member_session")
        _http(
            "POST",
            f"{self.base}/checkin/scan",
            {"qr": self.qr1},
            cookie=self.member,
        )
        _http(
            "POST",
            f"{self.base}/checkin/confirm",
            {"qr": self.qr1},
            cookie=self.member,
        )


class FailClosedGateE2E(unittest.TestCase):
    """权限边界：成员不可见指挥页；无会话不可刷码；口令错误显式拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = _build_state(tmp)
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")

    def test_member_cannot_see_events_page(self):
        received = receive_confirmed_fixture(self.state, {"name": "X", "report_no": "R", "discovered_at": "2026-08-23T20:00:00+08:00",
             "location": "L", "source": ""})
        self.assertEqual("CREATED", received["code"])
        _, headers, _ = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340002", "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        self.assertTrue(member)
        status, _, _ = _http("GET", f"{self.base}/events", cookie=member)
        self.assertEqual(404, status)

    def test_scan_without_member_session_redirects_to_login(self):
        status, headers, _ = _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"})
        self.assertEqual(303, status)
        self.assertIn("/login", headers.get("Location", ""))

    def test_wrong_password_401_with_reason(self):
        status, _, body = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13912340002", "password": "wrong"},
        )
        self.assertEqual(401, status)
        self.assertIn("账号或口令不正确", body)

    def test_unknown_account_fail_closed_message(self):
        status, _, body = _http(
            "POST",
            f"{self.base}/login/member",
            {"phone": "13999999999", "password": DEMO_INITIAL_PASSWORD},
        )
        self.assertEqual(401, status)
        self.assertIn("不在长期人员库", body)


if __name__ == "__main__":
    unittest.main()
