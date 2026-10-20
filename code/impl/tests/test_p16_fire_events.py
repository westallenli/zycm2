"""P16 T1 验收：火情事件域（多事件注册表 / 成员账号 / 二维码 / 刷码匹配 / FIRE_EVENT 接入）。

对齐口径：页面设计定版 01_登录页_V0.3；增强版PRD §13.3；对外数据接口规范 §2/§4/§5/§11。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from panda_brain.alarm_inbox import AlarmInbox
from panda_brain.fire_events import (
    DEFAULT_QR_VALID_HOURS,
    DEMO_INITIAL_PASSWORD,
    LOCK_THRESHOLD,
    OUTCOME_CONFIRM,
    OUTCOME_CROSS_CONFLICT,
    OUTCOME_EVENT_NOT_OPEN,
    OUTCOME_IDEMPOTENT,
    OUTCOME_NOT_IN_LIBRARY,
    OUTCOME_NOT_ON_ROSTER,
    OUTCOME_POSITION_UNRESOLVED,
    OUTCOME_QR_EXPIRED,
    OUTCOME_QR_REVOKED,
    OUTCOME_QR_UNKNOWN,
    EventRegistry,
    confirm_checkin,
    ensure_person_in_workspace,
    ingest_fire_event,
    resolve_checkin,
    resolve_home_slot,
)
from panda_brain.org_model import SLOT_INDEX


def _library_records():
    from panda_brain.models import PersonRecord

    return [
        PersonRecord(
            name="张三", phone="13912340001", unit="县防火办",
            home_position="扑救一组（一分区）", is_leader=True,
        ),
        PersonRecord(name="李四", phone="13912340002", unit="林场的护站", home_position=""),
        PersonRecord(name="王五", phone="13912340003", unit="县消防队", home_position="扑救督导"),
    ]


def _registry_with_event(**overrides) -> tuple[EventRegistry, str]:
    registry = EventRegistry()
    registry.import_person_library(_library_records())
    kwargs = dict(
        name="汉江上游山火",
        report_no="HC-2026-0823-01",
        discovered_at="2026-08-23T08:00:00+08:00",
        source="页面录入",
        location="汉江市青岗县西北侧山地",
    )
    kwargs.update(overrides)
    record = registry.create_event(**kwargs)
    return registry, record.event_id


def _activate(registry: EventRegistry, event_id: str) -> None:
    registry.transition(event_id, "start")


class TestEventLifecycle(unittest.TestCase):
    """01 号 §3.3 生命周期：登记中 → 进行中 → 已升级 → 已关闭（登记中可直关）。"""

    def test_create_defaults_registered(self):
        registry, event_id = _registry_with_event()
        record = registry.events[event_id]
        self.assertEqual(record.status, "registered")
        self.assertEqual(record.status_word(), "登记中")
        self.assertEqual(record.mission_space_id, f"ms-{event_id}")
        self.assertEqual(event_id, "fire-001")

    def test_happy_path_chain(self):
        registry, event_id = _registry_with_event()
        self.assertEqual(registry.transition(event_id, "start").status, "active")
        self.assertEqual(registry.transition(event_id, "upgrade").status, "upgraded")
        self.assertEqual(registry.transition(event_id, "close").status, "closed")

    def test_illegal_transitions_refused(self):
        registry, event_id = _registry_with_event()
        with self.assertRaisesRegex(ValueError, "只有「进行中」事件可标记升级（当前：登记中）"):
            registry.transition(event_id, "upgrade")
        # registered → close 合法（图上直连箭头）；关闭后为终态，任何动作均拒绝
        registry.transition(event_id, "close")
        for action in ("start", "upgrade", "close"):
            with self.assertRaises(ValueError):
                registry.transition(event_id, action)

    def test_unknown_action_and_event(self):
        registry, event_id = _registry_with_event()
        with self.assertRaisesRegex(ValueError, "未知生命周期动作"):
            registry.transition(event_id, "pause")
        with self.assertRaises(KeyError):
            registry.transition("fire-999", "start")

    def test_event_id_conflict(self):
        registry, event_id = _registry_with_event()
        with self.assertRaisesRegex(ValueError, "事件编号已存在"):
            registry.create_event(
                name="另一场火",
                report_no="RB-02",
                discovered_at="2026-08-23T09:00:00+08:00",
                source="页面录入",
                location="别处",
                event_id=event_id,
            )

    def test_required_fields_enforced(self):
        registry = EventRegistry()
        base = dict(
            name="X",
            report_no="R",
            discovered_at="2026-08-23T08:00:00+08:00",
            source="",
            location="L",
        )
        for blank_field in ("name", "report_no", "location"):
            kwargs = {**base, blank_field: ""}
            with self.assertRaises(ValueError):
                registry.create_event(**kwargs)

    def test_bad_time_rejected(self):
        registry = EventRegistry()
        with self.assertRaisesRegex(ValueError, "时间格式不合法"):
            registry.create_event(
                name="X", report_no="R", discovered_at="昨天下午",
                source="", location="L",
            )


class TestHomeSlotResolution(unittest.TestCase):
    """本职 → 编制槽位默认建议：精确命中优先、词重叠次之、未中显式 None。"""

    def test_exact_full_name(self):
        self.assertEqual(resolve_home_slot("扑救一组（一分区）"), "pjjz-1")

    def test_pos_code_passthrough(self):
        self.assertEqual(resolve_home_slot("supervisor"), "supervisor")

    def test_token_overlap(self):
        self.assertEqual(resolve_home_slot("扑救督导"), "supervisor")

    def test_weak_single_char_overlap_is_none(self):
        # 「扑救员」与任一槽位仅一词重叠 → 不猜，返回 None 转人工指派
        self.assertIsNone(resolve_home_slot("扑救员"))

    def test_empty_none(self):
        self.assertIsNone(resolve_home_slot(""))
        self.assertIsNone(resolve_home_slot(None))


class TestQRCodes(unittest.TestCase):
    """01 号 §5 二维码规则：仅进行中/已升级可签发；72h 现算过期；撤销留痕。"""

    def test_issue_requires_onboardable_status(self):
        registry, event_id = _registry_with_event()
        with self.assertRaisesRegex(ValueError, "只有「进行中／已升级」事件可签发"):
            registry.issue_qr(event_id, issued_by="admin")
        _activate(registry, event_id)
        qr = registry.issue_qr(event_id, issued_by="admin")
        self.assertEqual(qr.qr_id, "qr-001")

    def test_closed_event_cannot_issue(self):
        registry, event_id = _registry_with_event()
        _activate(registry, event_id)
        registry.transition(event_id, "close")
        with self.assertRaisesRegex(
            ValueError, "只有「进行中／已升级」事件可签发二维码（当前：已关闭）"
        ):
            registry.issue_qr(event_id, issued_by="admin")

    def test_default_validity_72h_and_expiry_computed(self):
        registry, event_id = _registry_with_event()
        _activate(registry, event_id)
        issued_at = "2026-08-23T08:00:00+08:00"
        qr = registry.issue_qr(event_id, issued_by="admin", issued_at=issued_at)
        self.assertEqual(qr.valid_hours, DEFAULT_QR_VALID_HOURS)
        just_before = "2026-08-26T07:59:59+08:00"
        at_expiry = "2026-08-26T08:00:00+08:00"
        self.assertEqual(registry.qr_status(qr, now=just_before), "有效")
        self.assertEqual(registry.qr_status(qr, now=at_expiry), "已过期")

    def test_custom_validity_window(self):
        registry, event_id = _registry_with_event()
        _activate(registry, event_id)
        issued_at = "2026-08-23T08:00:00+08:00"
        qr = registry.issue_qr(
            event_id, issued_by="admin", issued_at=issued_at, valid_hours=4
        )
        self.assertEqual(
            registry.qr_status(qr, now="2026-08-23T11:59:00+08:00"), "有效"
        )
        self.assertEqual(
            registry.qr_status(qr, now="2026-08-23T12:00:00+08:00"), "已过期"
        )
        with self.assertRaises(ValueError):
            registry.issue_qr(event_id, issued_by="admin", valid_hours=0)

    def test_revoke_audits_and_double_revoke_refused(self):
        registry, event_id = _registry_with_event()
        _activate(registry, event_id)
        qr = registry.issue_qr(event_id, issued_by="admin")
        revoked = registry.revoke_qr(qr.qr_id, revoked_by="admin")
        self.assertTrue(revoked.revoked_at)
        self.assertEqual(revoked.revoked_by, "admin")
        self.assertEqual(EventRegistry.qr_status(revoked, now=None), "已撤销")
        with self.assertRaisesRegex(ValueError, "不可重复撤销"):
            registry.revoke_qr(qr.qr_id, revoked_by="admin")

    def test_qr_ids_sequential_per_registry(self):
        registry, event_id = _registry_with_event()
        second = registry.create_event(
            name="第二场火", report_no="RB-2",
            discovered_at="2026-08-23T10:00:00+08:00", source="", location="L",
        )
        _activate(registry, event_id)
        _activate(registry, second.event_id)
        first = registry.issue_qr(event_id, issued_by="a")
        other = registry.issue_qr(second.event_id, issued_by="a")
        self.assertEqual((first.qr_id, other.qr_id), ("qr-001", "qr-002"))


class TestRosterConfig(unittest.TestCase):
    """01 号 §4.1 本事件名册：预配置授权清单，本职建议 + 人工改派。"""

    def test_add_with_home_suggestion(self):
        registry, event_id = _registry_with_event()
        entry = registry.add_roster_entry(event_id, "13912340001", added_by="admin")
        self.assertEqual(entry.pos_code, "pjjz-1")
        self.assertTrue(entry.resolved_from_home)

    def test_add_unresolved_home_stores_none(self):
        registry, event_id = _registry_with_event()
        entry = registry.add_roster_entry(event_id, "13912340002")
        self.assertIsNone(entry.pos_code)
        self.assertFalse(entry.resolved_from_home)
        # 指挥员人工指派后可解析
        updated = registry.assign_roster_position(event_id, "13912340002", "log-water")
        self.assertEqual(updated.pos_code, "log-water")
        self.assertFalse(updated.resolved_from_home)

    def test_manual_override_beats_suggestion(self):
        registry, event_id = _registry_with_event()
        entry = registry.add_roster_entry(
            event_id, "13912340003", pos_code="sec-order"
        )
        self.assertEqual(entry.pos_code, "sec-order")
        self.assertFalse(entry.resolved_from_home)

    def test_unknown_phone_or_slot_refused(self):
        registry, event_id = _registry_with_event()
        with self.assertRaisesRegex(KeyError, "不在长期人员库"):
            registry.add_roster_entry(event_id, "13900000000")
        with self.assertRaisesRegex(KeyError, "未知岗位槽位"):
            registry.add_roster_entry(
                event_id, "13912340001", pos_code="nope"
            )

    def test_version_bumps_and_remove(self):
        registry, event_id = _registry_with_event()
        roster = registry.rosters[event_id]
        v0 = roster.roster_version
        registry.add_roster_entry(event_id, "13912340001")
        v1 = registry.rosters[event_id].roster_version
        self.assertGreater(v1, v0)
        registry.remove_roster_entry(event_id, "13912340001")
        self.assertGreater(registry.rosters[event_id].roster_version, v1)
        with self.assertRaises(KeyError):
            registry.remove_roster_entry(event_id, "13912340001")

    def test_populate_all(self):
        registry, event_id = _registry_with_event()
        count = registry.populate_roster_all(event_id, added_by="admin")
        self.assertEqual(count, 3)
        entries = registry.rosters[event_id].entries
        self.assertIn("13912340003", entries)


class TestMemberCredentials(unittest.TestCase):
    """01 号 §2 账号体系：初始口令 123456、失败锁定、库外人转人工。"""

    def test_initial_password_login(self):
        registry, _ = _registry_with_event()
        phone = registry.verify_member_login("139 1234-0001", DEMO_INITIAL_PASSWORD)
        self.assertEqual(phone, "13912340001")

    def test_account_not_in_library_fail_closed(self):
        registry, _ = _registry_with_event()
        with self.assertRaisesRegex(PermissionError, "不在长期人员库"):
            registry.verify_member_login("13999999999", DEMO_INITIAL_PASSWORD)

    def test_lockout_after_threshold(self):
        registry, _ = _registry_with_event()
        for attempt in range(LOCK_THRESHOLD - 1):
            with self.assertRaisesRegex(PermissionError, "剩余"):
                registry.verify_member_login("13912340001", "wrong-pass")
        with self.assertRaisesRegex(PermissionError, "账号已锁定"):
            registry.verify_member_login("13912340001", "wrong-pass")
        # 锁定后正确口令也拒绝（fail-closed）
        with self.assertRaisesRegex(PermissionError, "已锁定"):
            registry.verify_member_login("13912340001", DEMO_INITIAL_PASSWORD)
        credential = registry.credentials["13912340001"]
        self.assertTrue(credential.locked)
        registry.unlock_account("13912340001", operator="admin")
        phone = registry.verify_member_login("13912340001", DEMO_INITIAL_PASSWORD)
        self.assertEqual(phone, "13912340001")

    def test_success_resets_failure_counter(self):
        registry, _ = _registry_with_event()
        with self.assertRaises(PermissionError):
            registry.verify_member_login("13912340001", "nope")
        registry.verify_member_login("13912340001", DEMO_INITIAL_PASSWORD)
        self.assertEqual(registry.credentials["13912340001"].failed_count, 0)


class TestResolveCheckin(unittest.TestCase):
    """01 号 §6 身份匹配结果表：全分支程序化验收。"""

    def setUp(self):
        self.registry, self.event_id = _registry_with_event()
        _activate(self.registry, self.event_id)
        self.qr = self.registry.issue_qr(
            self.event_id, issued_by="admin", issued_at="2026-08-23T09:00:00+08:00"
        )
        self.registry.add_roster_entry(self.event_id, "13912340001")
        self.registry.add_roster_entry(self.event_id, "13912340002")  # 岗位未解析

    def _resolve(self, **overrides):
        kwargs = dict(
            qr_input=self.qr.qr_id,
            phone="13912340001",
            session_bound_event_id="",
            now="2026-08-23T10:00:00+08:00",
        )
        kwargs.update(overrides)
        return resolve_checkin(self.registry, **kwargs)

    def test_success_pending_confirm_carries_group_position(self):
        outcome = self._resolve()
        self.assertEqual(outcome.kind, OUTCOME_CONFIRM)
        self.assertEqual(outcome.pos_code, "pjjz-1")
        self.assertIn("扑救一组（一分区）", outcome.position_name)
        self.assertIn("—", outcome.message)
        self.assertTrue(outcome.group_name)

    def test_qr_unknown(self):
        outcome = self._resolve(qr_input="qr-nope")
        self.assertEqual(outcome.kind, OUTCOME_QR_UNKNOWN)

    def test_qr_revoked_wins(self):
        self.registry.revoke_qr(self.qr.qr_id, revoked_by="admin")
        outcome = self._resolve()
        self.assertEqual(outcome.kind, OUTCOME_QR_REVOKED)

    def test_qr_expired(self):
        outcome = self._resolve(now="2026-08-30T10:00:00+08:00")
        self.assertEqual(outcome.kind, OUTCOME_QR_EXPIRED)

    def test_account_not_in_library(self):
        # 库外手机号先在面板会话层不可能出现，但域层仍 fail-closed 兜底
        self.registry.library.pop("13912340001")
        outcome = self._resolve()
        self.assertEqual(outcome.kind, OUTCOME_NOT_IN_LIBRARY)

    def test_registered_event_not_openable(self):
        other = self.registry.create_event(
            name="新火", report_no="R2",
            discovered_at="2026-08-23T11:00:00+08:00", source="", location="L",
        )
        # 登记中事件本身不可签发码；用历史遗留码改绑事件编号的方式构造场景
        from dataclasses import asdict

        from panda_brain.fire_events import QRCodeObject

        hacked = dict(asdict(self.qr))
        hacked["event_id"] = other.event_id
        self.registry.qrs[self.qr.qr_id] = QRCodeObject(**hacked)
        outcome = self._resolve()
        self.assertEqual(outcome.kind, OUTCOME_EVENT_NOT_OPEN)
        self.assertIn("登记中", outcome.message)

    def test_closed_event_not_openable(self):
        self.registry.transition(self.event_id, "close")
        outcome = self._resolve()
        self.assertEqual(outcome.kind, OUTCOME_EVENT_NOT_OPEN)
        self.assertIn("归档", outcome.message)

    def test_idempotent_already_onboarded(self):
        outcome = self._resolve(session_bound_event_id=self.event_id)
        self.assertEqual(outcome.kind, OUTCOME_IDEMPOTENT)
        self.assertIn("幂等", outcome.message)

    def test_cross_event_conflict(self):
        other = self.registry.create_event(
            name="另一场火", report_no="R3",
            discovered_at="2026-08-23T12:00:00+08:00", source="", location="L2",
        )
        _activate(self.registry, other.event_id)
        outcome = self._resolve(session_bound_event_id=other.event_id)
        self.assertEqual(outcome.kind, OUTCOME_CROSS_CONFLICT)
        self.assertIn("不允许自行改岗", outcome.message)

    def test_not_on_roster(self):
        outcome = self._resolve(phone="13912340003")
        self.assertEqual(outcome.kind, OUTCOME_NOT_ON_ROSTER)

    def test_position_unresolvable(self):
        outcome = self._resolve(phone="13912340002")
        self.assertEqual(outcome.kind, OUTCOME_POSITION_UNRESOLVED)

    def test_malformed_session_phone_raises(self):
        with self.assertRaises(PermissionError):
            self._resolve(phone="not-a-phone")


class TestConfirmCheckin(unittest.TestCase):
    """确认入岗：经注入回调走工作区唯一门，复核 fail-closed。"""

    def setUp(self):
        self.registry, self.event_id = _registry_with_event()
        _activate(self.registry, self.event_id)
        self.qr = self.registry.issue_qr(self.event_id, issued_by="admin")
        self.registry.add_roster_entry(self.event_id, "13912340001")
        self.outcome = resolve_checkin(
            self.registry,
            qr_input=self.qr.qr_id,
            phone="13912340001",
            session_bound_event_id="",
            now="2026-08-23T10:00:00+08:00",
        )
        self.calls = []

        def bind_fn(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(id="rbs-001")

        self.bind_fn = bind_fn

    def test_happy_path_calls_gate_once(self):
        result = confirm_checkin(
            self.registry, self.outcome, phone="13912340001", bind_fn=self.bind_fn
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["pos_code"], "pjjz-1")
        self.assertEqual(self.calls[0]["phone"], "13912340001")
        self.assertEqual(result["binding_id"], "rbs-001")
        self.assertEqual(result["event_id"], self.event_id)
        self.assertIn("roster_version", result)

    def test_non_confirm_outcome_refused(self):
        bad = resolve_checkin(
            self.registry, qr_input="qr-miss", phone="13912340001"
        )
        with self.assertRaisesRegex(PermissionError, "待确认"):
            confirm_checkin(self.registry, bad, phone="13912340001", bind_fn=self.bind_fn)

    def test_roster_changed_between_resolve_and_confirm(self):
        self.registry.assign_roster_position(self.event_id, "13912340001", "air-op")
        with self.assertRaisesRegex(PermissionError, "名册已变更"):
            confirm_checkin(
                self.registry, self.outcome, phone="13912340001", bind_fn=self.bind_fn
            )
        self.assertEqual(self.calls, [])

    def test_closed_after_resolve_refused(self):
        self.registry.transition(self.event_id, "close")
        with self.assertRaisesRegex(PermissionError, "不可入岗"):
            confirm_checkin(
                self.registry, self.outcome, phone="13912340001", bind_fn=self.bind_fn
            )

    def test_workspace_bridge_imports_missing_person(self):
        class FakeWorkspace:
            def __init__(self):
                self.person_library = {}
                self.imported = []

            def import_person_library(self, records):
                self.imported.extend(records)
                from panda_brain.models import PersonRecord

                for rec in records:
                    self.person_library[rec.phone] = rec
                return len(records)

        fake = FakeWorkspace()
        ensure_person_in_workspace(self.registry, fake, "13912340001")
        self.assertIn("13912340001", fake.person_library)
        # 第二次调用不再导入（幂等桥接）
        ensure_person_in_workspace(self.registry, fake, "13912340001")
        self.assertEqual(len(fake.imported), 1)


class TestIngestFireEvent(unittest.TestCase):
    """外部报警接入（P17-P1 口径）：一律先进待确认收件箱，绝不直通建档。

    2026-08-25 产品口径变更（DEVELOPMENT_PLAN_PAGES_V1 §4.1.B）：旧版
    「CREATE 直通建档 + UPDATE 直改状态」已废止；外部消息只能落收件箱，
    由指挥员人工核对后三选一。本类验证接入语义；处置语义在
    test_p17_alarm_inbox.py 展开。
    """

    def _message(self, **overrides):
        message = {
            "messageId": "msg-20260823-000001",
            "messageType": "FIRE_EVENT",
            "schemaVersion": "1.0",
            "sourceSystem": "EXT_DISPATCH",
            "sourceRecordId": "fe-ext-001",
            "sourceVersion": 1,
            "eventTime": "2026-08-23T09:30:00+08:00",
            "operation": "CREATE",
            "data": {
                "name": "北岭火情",
                "discoveredAt": "2026-08-23T09:20:00+08:00",
                "location": "北岭县东侧",
                "status": "registered",
            },
        }
        message.update(overrides)
        return message

    def _inbox(self):
        return AlarmInbox()

    def test_valid_create_lands_pending_not_event(self):
        inbox, registry = self._inbox(), EventRegistry()
        result = ingest_fire_event(inbox, registry, self._message())
        self.assertEqual(result["code"], "PENDING_CONFIRM")
        alarm = inbox.alarms[result["alarm_id"]]
        self.assertEqual(alarm.status, "pending")
        self.assertEqual(alarm.source_system, "EXT_DISPATCH")
        self.assertEqual(alarm.external_no, "fe-ext-001")
        self.assertEqual(alarm.reported_at, "2026-08-23T09:20:00+08:00")
        # 绝不直通建档：注册表保持为空，等人工确认。
        self.assertEqual(registry.events, {})
        self.assertEqual(len(alarm.raw_digest), 64)
        self.assertTrue(any(h["action"] == "received" for h in alarm.history))

    def test_external_eventid_stored_as_hint_only(self):
        inbox, registry = self._inbox(), EventRegistry()
        msg = self._message()
        msg["data"]["eventId"] = "FE-20260823-0007"
        result = ingest_fire_event(inbox, registry, msg)
        alarm = inbox.alarms[result["alarm_id"]]
        # 外部 eventId 仅作参考显示，内部编号始终由 fire-{n} 在建档时编列。
        self.assertEqual(alarm.suggested_event_id, "FE-20260823-0007")
        self.assertEqual(registry.events, {})

    def test_duplicate_returns_original_alarm(self):
        inbox, registry = self._inbox(), EventRegistry()
        first = ingest_fire_event(inbox, registry, self._message())
        replay = ingest_fire_event(inbox, registry, self._message())
        self.assertEqual(first["code"], "PENDING_CONFIRM")
        self.assertEqual(replay["code"], "DUPLICATE")
        self.assertEqual(replay["alarm_id"], first["alarm_id"])
        self.assertEqual(len(inbox.alarms), 1)
        self.assertEqual(registry.events, {})

    def test_envelope_field_errors_listed(self):
        inbox, registry = self._inbox(), EventRegistry()
        result = ingest_fire_event(inbox, registry, {"messageType": "OBSERVATION"})
        self.assertEqual(result["code"], "INVALID_ARGUMENT")
        fields_flagged = {err["field"] for err in result["errors"]}
        self.assertIn("messageId", fields_flagged)
        self.assertIn("sourceSystem", fields_flagged)
        self.assertIn("operation", fields_flagged)
        self.assertIn("data", fields_flagged)
        self.assertEqual(registry.events, {})
        self.assertEqual(inbox.alarms, {})

    def test_missing_data_minimums_listed(self):
        inbox, registry = self._inbox(), EventRegistry()
        msg = self._message()
        msg.pop("eventTime")  # 无 eventTime 兜底时 discoveredAt 必报缺失
        msg["data"] = {"name": ""}
        result = ingest_fire_event(inbox, registry, msg)
        self.assertEqual(result["code"], "INVALID_ARGUMENT")
        fields_flagged = {err["field"] for err in result["errors"]}
        self.assertIn("data.name", fields_flagged)
        self.assertIn("data.discoveredAt", fields_flagged)
        self.assertIn("data.location", fields_flagged)

    def test_unmapped_status_stored_verbatim_not_interpreted(self):
        # 口径变更：外部状态原文只存不解释（不再是本系统生命周期输入），
        # 枚举漂移不再拒绝真实报警，也绝不据此流转事件。
        inbox, registry = self._inbox(), EventRegistry()
        msg = self._message()
        msg["data"]["status"] = "PENDING_WEIRD_ENUM"
        result = ingest_fire_event(inbox, registry, msg)
        self.assertEqual(result["code"], "PENDING_CONFIRM")
        alarm = inbox.alarms[result["alarm_id"]]
        self.assertEqual(alarm.source_status, "PENDING_WEIRD_ENUM")

    def test_update_refreshes_pending_fields_and_version(self):
        inbox, registry = self._inbox(), EventRegistry()
        ingest_fire_event(inbox, registry, self._message())
        update = {
            "messageId": "msg-2",
            "messageType": "FIRE_EVENT",
            "sourceSystem": "EXT_DISPATCH",
            "sourceRecordId": "fe-ext-001",
            "sourceVersion": 2,
            "operation": "UPDATE",
            "data": {"name": "北岭火情（更新）", "level": "III级"},
        }
        result = ingest_fire_event(inbox, registry, update)
        self.assertEqual(result["code"], "PENDING_CONFIRM")
        alarm = next(iter(inbox.alarms.values()))
        self.assertEqual(alarm.status, "pending")
        self.assertEqual(alarm.level, "III级")
        self.assertTrue(any(h["action"] == "external_update" for h in alarm.history))

    def test_update_version_conflict(self):
        inbox, registry = self._inbox(), EventRegistry()
        ingest_fire_event(inbox, registry, self._message())
        update = {
            "messageId": "msg-2",
            "messageType": "FIRE_EVENT",
            "sourceSystem": "EXT_DISPATCH",
            "sourceRecordId": "fe-ext-001",
            "sourceVersion": 2,
            "operation": "UPDATE",
            "data": {"level": "III级"},
        }
        self.assertEqual(
            ingest_fire_event(inbox, registry, update)["code"], "PENDING_CONFIRM")
        conflict = ingest_fire_event(
            inbox, registry, {**update, "messageId": "msg-3"})
        self.assertEqual(conflict["code"], "VERSION_CONFLICT")

    def test_update_after_manual_decision_is_explicit_noop(self):
        inbox, registry = self._inbox(), EventRegistry()
        created = ingest_fire_event(inbox, registry, self._message())
        event = inbox.confirm(
            created["alarm_id"], registry=registry, report_no="BL-1")
        update = {
            "messageId": "msg-c",
            "messageType": "FIRE_EVENT",
            "sourceSystem": "EXT_DISPATCH",
            "sourceRecordId": "fe-ext-001",
            "sourceVersion": 9,
            "operation": "UPDATE",
            "data": {"status": "closed"},
        }
        result = ingest_fire_event(inbox, registry, update)
        self.assertEqual(result["code"], "MANUAL_DECIDED")
        self.assertEqual(result["event_id"], event.event_id)
        # 人工建档后外部更新不改写事件生命周期。
        self.assertEqual(registry.events[event.event_id].status, "registered")

    def test_update_unknown_target_rejected(self):
        inbox, registry = self._inbox(), EventRegistry()
        msg = self._message()
        msg["operation"] = "UPDATE"
        msg["data"] = {"status": "active"}
        result = ingest_fire_event(inbox, registry, msg)
        self.assertEqual(result["code"], "INVALID_ARGUMENT")
        self.assertTrue(
            any(err["field"] == "sourceRecordId" for err in result["errors"]))

    def test_same_version_update_conflicts_not_noop(self):
        # 幂等键语义保留：同版本重复受理 → VERSION_CONFLICT（§6.2 业务级幂等
        # 由 CREATE 的 DUPLICATE 承担）。
        inbox, registry = self._inbox(), EventRegistry()
        ingest_fire_event(inbox, registry, self._message())
        update = {
            "messageId": "msg-same",
            "messageType": "FIRE_EVENT",
            "sourceSystem": "EXT_DISPATCH",
            "sourceRecordId": "fe-ext-001",
            "sourceVersion": 5,
            "operation": "UPDATE",
            "data": {"status": "registered"},
        }
        self.assertEqual(
            ingest_fire_event(inbox, registry, update)["code"], "PENDING_CONFIRM")
        again = ingest_fire_event(
            inbox, registry, {**update, "messageId": "msg-same-2"})
        self.assertEqual(again["code"], "VERSION_CONFLICT")


class TestRegistryPersistence(unittest.TestCase):
    """注册表 JSON 往返：字段无损、缺省空表、原子写无残留。"""

    def _populated(self) -> EventRegistry:
        registry, event_id = _registry_with_event()
        _activate(registry, event_id)
        registry.set_snapshot_path(event_id, "snapshots/fire-001.db")
        registry.populate_roster_all(event_id, added_by="admin")
        registry.assign_roster_position(event_id, "13912340002", "log-water")
        registry.issue_qr(event_id, issued_by="admin")
        registry.verify_member_login("13912340001", DEMO_INITIAL_PASSWORD)
        return registry

    def test_round_trip_preserves_state(self):
        registry = self._populated()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reg.json"
            registry.save(path)
            loaded = EventRegistry.load(path)
            self.assertEqual(loaded.to_dict(), registry.to_dict())

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = EventRegistry.load(Path(tmp) / "absent.json")
            self.assertEqual(loaded.events, {})
            self.assertEqual(loaded.library, {})

    def test_save_atomic_no_tmp_residue(self):
        registry = self._populated()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reg.json"
            registry.save(path)
            residue = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(residue, [])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)

    def test_snapshot_path_recorded(self):
        registry = self._populated()
        record = registry.events["fire-001"]
        self.assertEqual(record.snapshot_path, "snapshots/fire-001.db")


if __name__ == "__main__":
    unittest.main()
