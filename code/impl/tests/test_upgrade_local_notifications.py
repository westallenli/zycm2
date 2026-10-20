"""P03-08 duty-prepared, unsent local-person notices and QR linkage."""

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace


class LocalNotificationTests(unittest.TestCase):
    DUTY = "13999993001"
    OTHER = "13999993002"
    LOCAL_A = "13999993003"
    LOCAL_B = "13999993004"
    CAPTAIN = "13999993005"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        snapshot_patch = patch.object(wb, "SNAPSHOT_DIR", root / "snapshots")
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.auth = GeneralEntryAuth(secret="p03-local-notice-secret")
        self.state = PanelState(
            ScenarioWorkspace("p03-local-notice", general_auth=self.auth),
            auth=self.auth, registry_path=root / "registry.json",
            materials_path=root / "materials.json", reviews_path=root / "reviews.json",
            alarm_inbox_path=root / "alarms.json", duty_subjects=(self.DUTY,),
        )
        people = (
            PersonRecord(name="值班员", phone=self.DUTY, unit="值班室"),
            PersonRecord(name="无关人员", phone=self.OTHER, unit="其他"),
            PersonRecord(name="属地领导", phone=self.LOCAL_A, unit="属地政府"),
            PersonRecord(name="属地协助", phone=self.LOCAL_B, unit="属地林业"),
            PersonRecord(name="固定中队长", phone=self.CAPTAIN, unit="一中队", is_leader=True),
        )
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, "123456")
            self.assertEqual("", error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name="属地通知测试", report_no="P03-08",
            discovered_at="2026-09-28T08:00:00+08:00", source="测试建档",
            location="青岗县", created_by="fixture",
        )
        self.event_id = event.event_id
        self.state.ledger.add_squad(
            name="一中队", member_phones=(self.CAPTAIN,),
            leader_phone=self.CAPTAIN, library=self.state.registry.library,
        )
        self.state.handle_event_action(
            self.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.qr_id = next(q.qr_id for q in self.state.registry.qrs.values()
                          if q.event_id == self.event_id)

    def prepare(self, **changes):
        args = dict(
            duty_token=self.tokens[self.DUTY], phone=self.LOCAL_A,
            local_role="local_leader", channel="phone", request_id="notice-001",
        )
        return self.state.handle_prepare_local_notice(
            self.event_id, **(args | changes)
        )

    def test_duty_prepares_unsent_notice_without_registering_or_arriving(self):
        result = self.prepare()
        self.assertEqual("simulated", result["data_kind"])
        self.assertEqual("not_sent", result["transport_state"])
        self.assertEqual("not_delivered", result["delivery_state"])
        self.assertEqual(self.DUTY, result["prepared_by"])
        self.assertEqual({}, self.state.temporary_people(self.event_id))
        self.assertEqual({}, self.state.ledger.snapshots)
        self.assertEqual({}, self.state.ledger.flow_records)
        target = Path(self.state.registry.events[self.event_id].snapshot_path)
        restored = load_workspace(target, general_auth=self.auth)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual(1, sum(e.kind == "TemporaryLocalNoticePrepared"
                                for e in restored.events))
        status, page = render_page(self.state, "/events/local-notices",
                                   member_token=self.tokens[self.DUTY])
        self.assertEqual(200, status)
        self.assertIn("模拟已准备 · 未发送／未送达", page)
        self.assertIn("尚未扫码登记", page)

    def test_only_named_duty_can_prepare_and_fixed_members_are_excluded(self):
        for token in (self.tokens[self.OTHER], self.tokens[self.LOCAL_A], None):
            with self.subTest(token=token), self.assertRaises(PermissionError):
                self.prepare(duty_token=token)
        for changes in (
            {"phone": self.CAPTAIN}, {"phone": "13999993999"},
            {"channel": "sms"}, {"local_role": "chief"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(**changes)
        self.state.registry.add_roster_entry(
            self.event_id, self.LOCAL_B, pos_code="tech-info", added_by="fixture"
        )
        with self.assertRaises(ValueError):
            self.prepare(phone=self.LOCAL_B, local_role="assistant")
        self.assertEqual({}, self.state.local_notices(self.event_id))

    def test_request_retry_and_two_unsent_channels_do_not_create_delivery(self):
        first = self.prepare()
        self.assertEqual(first, self.prepare())
        with self.assertRaises(ValueError):
            self.prepare(phone=self.LOCAL_B)
        with self.assertRaises(ValueError):
            self.prepare(request_id="notice-002")
        second = self.prepare(channel="wechat", request_id="notice-002")
        self.assertEqual("not_sent", second["transport_state"])
        with self.assertRaises(ValueError):
            self.prepare(channel="wechat", local_role="assistant",
                         request_id="notice-003")
        self.assertEqual(2, len(self.state.local_notices(self.event_id)))

    def test_individual_scan_links_notice_but_remains_separate_from_arrival(self):
        self.prepare()
        status, before = render_page(self.state, "/temporary",
                                     member_token=self.tokens[self.LOCAL_A])
        self.assertEqual(200, status)
        self.assertIn("系统内模拟通知记录", before)
        self.assertIn("电话未拨打、微信未发送", before)
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[self.LOCAL_A], qr_id=self.qr_id,
        )
        self.assertEqual("", self.state.temporary_people(
            self.event_id)[self.LOCAL_A].arrived_at)
        status, after = render_page(self.state, "/events/local-notices",
                                    member_token=self.tokens[self.DUTY])
        self.assertEqual(200, status)
        self.assertIn("已本人扫码登记", after)
        self.assertIn("模拟已准备 · 未发送／未送达", after)
        self.assertEqual({}, self.state.ledger.snapshots)

    def test_existing_self_registration_does_not_require_prior_notice(self):
        self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[self.LOCAL_B], qr_id=self.qr_id,
        )
        self.assertIn(self.LOCAL_B, self.state.temporary_people(self.event_id))
        self.assertEqual({}, self.state.local_notices(self.event_id))
        self.prepare(phone=self.LOCAL_B, local_role="assistant",
                     request_id="notice-002")
        status, page = render_page(self.state, "/events/local-notices",
                                   member_token=self.tokens[self.DUTY])
        self.assertEqual(200, status)
        self.assertIn("已本人扫码登记", page)

    def test_notices_are_event_scoped_and_do_not_open_another_event(self):
        other = self.state.registry.create_event(
            name="另一场火情", report_no="P03-08-B",
            discovered_at="2026-09-28T10:00:00+08:00", source="测试建档",
            location="邻县", created_by="fixture",
        )
        self.state.handle_event_action(
            other.event_id, "start", duty_token=self.tokens[self.DUTY]
        )
        self.state.handle_prepare_local_notice(
            other.event_id, duty_token=self.tokens[self.DUTY],
            phone=self.LOCAL_A, local_role="local_leader", channel="wechat",
            request_id="other-event-notice",
        )
        self.assertEqual({}, self.state.local_notices(self.event_id))
        self.assertEqual(1, len(self.state.local_notices(other.event_id)))
        self.assertEqual([other.event_id], list(self.state.member_local_notices(
            self.tokens[self.LOCAL_A])))
        self.assertEqual({}, self.state.temporary_people(self.event_id))
        self.assertEqual({}, self.state.temporary_people(other.event_id))

    def test_http_scope_and_forged_delivered_record_are_rejected(self):
        self.assertEqual(404, render_page(self.state, "/events/local-notices",
                                           member_token=self.tokens[self.OTHER])[0])
        server = make_server(self.state, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def post(phone):
            data = urllib.parse.urlencode(dict(
                event_id=self.event_id, phone=self.LOCAL_A,
                local_role="local_leader", channel="phone",
                request_id="http-notice-001",
            )).encode()
            req = urllib.request.Request(
                base + "/events/local-notices/prepare", data=data, method="POST",
                headers={"Cookie": f"member_session={self.tokens[phone]}"},
            )
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(403, post(self.OTHER))
        self.assertEqual(303, post(self.DUTY))
        self.assertEqual(1, len(self.state.local_notices(self.event_id)))
        self.state.workspace.append_event(
            "TemporaryLocalNoticePrepared", event_id=self.event_id,
            phone=self.LOCAL_B, name="伪造", unit="属地", local_role="assistant",
            channel="wechat", prepared_by=self.DUTY, prepared_at="fake",
            request_id="forged-delivered", data_kind="simulated",
            transport_state="sent", delivery_state="delivered",
        )
        with self.assertRaises(ValueError):
            from panda_brain.local_notifications import notice_state
            notice_state(self.state.workspace, self.event_id)


if __name__ == "__main__":
    unittest.main()
