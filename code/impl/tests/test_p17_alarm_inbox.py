"""历史收件箱兼容 + 2026-10-12 已确认推送直接建事件 HTTP 验收。

口径（DEVELOPMENT_PLAN_PAGES_V1 §4.1.B）：
- 外部报警不直接成为正式火情、不直接启动处置；
- 确认建档复用手动登记同一 ``registry.create_event``（同构事件，fire-{n} 编列）；
- 拒绝必填原因且不建档；重复报警关联已有事件，不重复建档；
- 关联与报文哈希入审计链（镜像进事件任务空间；建档前以收件箱 JSON 为事实源）；
- 真实外部端点待契约评审：演示入口带显式「演示数据」标注，不声称实时接入。
"""

from __future__ import annotations
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
from panda_brain.alarm_inbox import AlarmInbox
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import EventRegistry
from panda_brain.panel import PanelState, make_server
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p17-alarm-secret"

_DEMO_MESSAGE = {
    "messageId": "msg-alarm-001",
    "messageType": "FIRE_EVENT",
    "schemaVersion": "1.0",
    "sourceSystem": "EXT_DISPATCH",
    "sourceRecordId": "fe-ext-101",
    "sourceVersion": 1,
    "eventTime": "2026-08-25T10:00:00+08:00",
    "operation": "CREATE",
    "data": {
        "name": "北岭火情",
        "discoveredAt": "2026-08-25T09:50:00+08:00",
        "location": "北岭县东侧",
        "status": "registered",
        "level": "II级",
        "initialReport": "护林员报告林缘烟点。",
        "keyTargets": ["输电走廊"],
    },
}


class AlarmInboxDomainTests(unittest.TestCase):
    """域级：三选一处置语义、同构建档、fail-closed 与持久化。"""

    def setUp(self):
        self.inbox = AlarmInbox()
        self.registry = EventRegistry()

    def _alarm_id(self) -> str:
        result = self.inbox.ingest_message(dict(_DEMO_MESSAGE))
        self.assertEqual(result["code"], "PENDING_CONFIRM")
        return result["alarm_id"]

    def test_confirm_creates_isomorphic_event_via_same_factory(self):
        alarm_id = self._alarm_id()
        event = self.inbox.confirm(
            alarm_id, registry=self.registry, report_no="BL-2026-01")
        record = self.registry.events[event.event_id]
        # 与手动登记同一工厂：字段同构、fire-{n} 编列、初始态「登记中」。
        self.assertTrue(event.event_id.startswith("fire-"))
        self.assertEqual(record.status, "registered")
        self.assertEqual(record.name, "北岭火情")
        self.assertEqual(record.report_no, "BL-2026-01")
        self.assertEqual(record.discovered_at, "2026-08-25T09:50:00+08:00")
        self.assertEqual(record.source, "外部报警·EXT_DISPATCH")
        self.assertEqual(record.location, "北岭县东侧")
        self.assertEqual(record.level, "II级")
        self.assertEqual(record.initial_report, "护林员报告林缘烟点。")
        self.assertEqual(record.key_targets, ("输电走廊",))
        self.assertEqual(record.mission_space_id, f"ms-{event.event_id}")
        # 关联双写：报警记录 + 注册表幂等索引都指向正式事件。
        alarm = self.inbox.alarms[alarm_id]
        self.assertEqual(alarm.status, "confirmed")
        self.assertEqual(alarm.event_id, event.event_id)
        self.assertEqual(
            self.registry.external_index[alarm.index_key], event.event_id)

    def test_confirm_is_terminal_and_requires_report_no(self):
        alarm_id = self._alarm_id()
        with self.assertRaises(ValueError):
            self.inbox.confirm(alarm_id, registry=self.registry, report_no="  ")
        event = self.inbox.confirm(
            alarm_id, registry=self.registry, report_no="BL-1", name="修正名")
        self.assertEqual(event.name, "修正名")
        with self.assertRaises(ValueError):
            self.inbox.confirm(alarm_id, registry=self.registry, report_no="BL-2")

    def test_reject_requires_reason_and_creates_nothing(self):
        alarm_id = self._alarm_id()
        with self.assertRaises(ValueError):
            self.inbox.reject(alarm_id, reason="   ")
        self.inbox.reject(alarm_id, reason="重复误报，非火情")
        alarm = self.inbox.alarms[alarm_id]
        self.assertEqual(alarm.status, "rejected")
        self.assertEqual(alarm.reject_reason, "重复误报，非火情")
        self.assertEqual(self.registry.events, {})
        self.assertTrue(any(h["action"] == "rejected" for h in alarm.history))

    def test_duplicate_link_needs_existing_event(self):
        alarm_id = self._alarm_id()
        with self.assertRaises(ValueError):
            self.inbox.link_duplicate(
                alarm_id, registry=self.registry, event_id="fire-999")
        self.registry.create_event(
            name="已有事件", report_no="YB-1",
            discovered_at="2026-08-25T08:00:00+08:00", location="北岭县",
            source="页面录入")
        before = len(self.registry.events)
        self.inbox.link_duplicate(
            alarm_id, registry=self.registry, event_id="fire-001")
        alarm = self.inbox.alarms[alarm_id]
        self.assertEqual(alarm.status, "duplicate")
        self.assertEqual(alarm.event_id, "fire-001")
        self.assertEqual(len(self.registry.events), before)  # 不重复建档

    def test_json_round_trip_preserves_records(self):
        import json as _json

        alarm_id = self._alarm_id()
        self.inbox.confirm(alarm_id, registry=self.registry, report_no="BL-2")
        other = AlarmInbox()
        other.ingest_message(
            {**_DEMO_MESSAGE,
             "sourceRecordId": "fe-ext-102", "messageId": "msg-2"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            self.inbox.save(path)
            reloaded = AlarmInbox.load(path)
            self.assertEqual(
                _json.loads(path.read_text(encoding="utf-8"))["external_versions"],
                {"EXT_DISPATCH|fe-ext-101": 1})
        self.assertIn(alarm_id, reloaded.alarms)
        record = reloaded.alarms[alarm_id]
        self.assertEqual(record.status, "confirmed")
        self.assertEqual(record.raw_digest, self.inbox.alarms[alarm_id].raw_digest)


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


class AlarmInboxE2ETests(unittest.TestCase):
    """HTTP e2e：收件箱上页、门禁、三动作闭环、演示标注与审计镜像。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(
            wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p17-alarm", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            alarm_inbox_path=tmp / "alarm_inbox.json",
        )
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.assertEqual(303, status)
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state)
        # 预置一个进行中事件并进入其任务空间（供重复关联与审计镜像断言）。
        received = receive_confirmed_fixture(self.state, {"name": "已有火情", "report_no": "YB-9",
             "discovered_at": "2026-08-25T08:00:00+08:00",
             "location": "北岭县", "source": ""})
        self.assertEqual("CREATED", received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST", f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action},
                cookie=self.cmd,
            )

    def _demo_alarm(self) -> str:
        st, headers, _ = _http(
            "POST", f"{self.base}/events/alarm/demo", {}, cookie=self.cmd)
        self.assertIn(st, (302, 303))
        inbox = self.state.alarm_inbox
        self.assertTrue(inbox.alarms)
        return sorted(inbox.alarms)[-1]

    def test_demo_push_automatically_creates_without_start(self):
        aid = self._demo_alarm()
        alarm = self.state.alarm_inbox.alarms[aid]
        self.assertEqual("created", alarm.status)
        self.assertEqual("registered", self.state.registry.events[alarm.event_id].status)
        self.assertFalse(any(q.event_id == alarm.event_id for q in self.state.registry.qrs.values()))
        st, _, body = _http("GET", f"{self.base}/events", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("演示数据", body)
        self.assertNotIn("外部报警待确认", body)
        self.assertIn("真实推送接口：待对接", body)

    def test_demo_push_requires_command_authority(self):
        st, _, _ = _http("POST", f"{self.base}/events/alarm/demo", {}, cookie="member_session=x")
        self.assertEqual(403, st)

    def test_event_detail_replaces_confirmation_form(self):
        aid = self._demo_alarm(); eid = self.state.alarm_inbox.alarms[aid].event_id
        st, _, page = _http("GET", f"{self.base}/events/{eid}/details", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("DEMO_CONSOLE", page)
        self.assertNotIn('name="report_no"', page)
        st, _, _ = _http("GET", f"{self.base}/events/alarm/{aid}/confirm", cookie=self.cmd)
        self.assertEqual(404, st)

    def test_old_confirm_and_reject_posts_are_retired_without_mutation(self):
        aid = self._demo_alarm(); before = self.state.alarm_inbox.alarms[aid].to_dict()
        for action in ("confirm", "reject", "link"):
            st, _, _ = _http("POST", f"{self.base}/events/alarm/action",
                dict(alarm_action=action, alarm_id=aid, report_no="AGAIN", reason="old",event_id="fire-001"),cookie=self.cmd)
            self.assertEqual(410, st)
        self.assertEqual(before, self.state.alarm_inbox.alarms[aid].to_dict())
        self.assertEqual(2, len(self.state.registry.events))

    def test_manual_create_post_retired_without_new_event(self):
        st, _, _ = _http("POST", f"{self.base}/events/create",
            dict(name="old",report_no="old",location="old",discovered_at="2026-10-12T10:00:00+08:00"),cookie=self.cmd)
        self.assertEqual(410, st)
        self.assertEqual(1, len(self.state.registry.events))

    def test_new_event_does_not_write_to_existing_active_event(self):
        before = list(self.state.workspace.events)
        self._demo_alarm()
        self.assertEqual(before, list(self.state.workspace.events))
        self.assertEqual((), self.state.workspace.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
