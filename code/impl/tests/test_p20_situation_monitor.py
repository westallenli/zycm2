"""P20 态势自动发现与手动复核回归钉（docs/20_沙盘态势自动发现与手动复核开发设计_V1.0）。

覆盖定稿 §11/§12 的可量化项：
- 自动发现三类规则：时效、冲突、方案影响；幂等与等级升级；
- 统一数据入口：现场观测 fail-closed（无坐标/无来源/无时间/错字段一律拒收）；
- 产出边界：候选只能是草稿/待核验，绝不静默发布、不改命令、不动正式对象；
- 复核批次：固定数据截止点、同一状态重复点击幂等、无实质变化如实输出；
- 变化处理：已阅/按现有资料继续（必须写原因）；核验链流转后旧提示让位；
- 持久化与审计：SQLite 往返一致、哈希链完整；
- 页面与权限：指挥员/现场人员/公共屏三档隔离，前台全中文零枚举泄漏。
"""

from __future__ import annotations
from tests.personnel_support import personnel_submit
from tests.duty_support import duty_token

import json
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from panda_brain import situation_monitor as sm
from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import DEMO_INITIAL_PASSWORD
from panda_brain.map_annotations import (
    AnnotationSource,
    AnnotationStatus,
)
from panda_brain.models import Option, PersonRecord
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.panel import PanelState, make_server
from panda_brain.sandview import render_sand_page
from panda_brain.workspace import ScenarioWorkspace

SECRET = "p20-secret"


def _iso(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).astimezone().isoformat(
        timespec="seconds")


def _ago(minutes_ago: int) -> str:
    """与工作区时钟同口径：系统把时间统一折算成 UTC naive 后比较
    （tactical_map._naive 会减去时区偏移），所以夹具必须用 UTC 钟面。"""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M")


def _publish_staging(state, *, title="前沿集结点") -> str:
    """夹具：经完整核验链发布一个带时效规则的集结点（120 分钟复核周期）。"""
    aid = state.handle_sand_annotate(
        kind="staging_point", title=title, points="",
        geo_text="33.22100,107.55600",
        details={"purpose": "前沿集结", "unit": "扑救二组",
                 "open_status": "open", "safety_status": "safe"},
        responsible_unit="扑救二组",
    )
    for step in ("pending_review", "verified", "published"):
        state.handle_sand_annotation_status(aid, step)
    return aid


def _stale(ws, annotation_id: str, *, minutes: int = 500) -> None:
    """模拟时间流逝：把对象的复核依据拨回过去（仅测试夹具；扫描只读）。"""
    mark = ws.annotations[annotation_id]
    details = dict(mark.details)
    details["checked_at"] = _ago(minutes)
    ws.annotations[annotation_id] = replace(
        mark, details=details, updated_at="", freshness_status="",
    )


_HOTSPOT_FIELDS = {
    "found_at": _ago(40), "hot_status": "confirmed",
    "source_ref": "无人机影像-3", "unit": "扑救一分队",
}


def _state(tmp: Path, scenario: str = "p20-domain", secret: str = SECRET,
           test: unittest.TestCase | None = None) -> PanelState:
    auth = GeneralEntryAuth(secret=secret)
    if test is not None:
        # 事件 start 会把任务空间落到 wb.SNAPSHOT_DIR；重定向到临时目录，
        # 不污染共享的 impl/snapshots/fire-001.db。
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        test.addCleanup(patcher.stop)
    state = PanelState(
        ScenarioWorkspace(scenario, general_auth=auth),
        auth=auth,
        registry_path=tmp / "reg.json",
        materials_path=tmp / "mat.json",
        imagery_path=tmp / "img.json",
        reviews_path=tmp / "rev.json",
    )
    state.login(secret)
    state.handle_create_event(
        name="P20 测试火情", report_no="测试-P20-01",
        discovered_at=_iso(180), source="测试", location="测试南坡",
        lat="33.21", lon="107.55",
    )
    event_id = sorted(state.registry.events)[-1]
    state.active_event_id = event_id
    state.handle_event_action(event_id, "start", duty_token=duty_token(state))
    return state


class ScanRuleTest(unittest.TestCase):
    """§6.3 自动发现规则：时效/冲突/方案影响 + 幂等与升级。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, test=self)
        self.ws = self.state.workspace
        self.aid = _publish_staging(self.state)

    def test_freshness_change_keeps_formal_object_untouched(self):
        before = self.ws.annotations[self.aid]
        _stale(self.ws, self.aid)
        added = self.ws.situation_scan()
        self.assertEqual(
            (sm.ChangeKind.FRESHNESS_EXPIRED.value,),
            tuple(c.kind for c in added),
        )
        change = next(iter(self.ws.situation_changes.values()))
        self.assertEqual((self.aid,), change.affected_annotation_ids)
        self.assertEqual(sm.ChangeSeverity.REVIEW.value, change.severity)
        after = self.ws.annotations[self.aid]
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.geo_points, after.geo_points)
        self.assertEqual(before.version, after.version)

    def test_rescan_is_idempotent(self):
        _stale(self.ws, self.aid)
        self.ws.situation_scan()
        again = self.ws.situation_scan()
        self.assertEqual((), tuple(c.kind for c in again))
        self.assertEqual(1, len(self.ws.situation_changes))

    def test_escalation_only_on_severity_increase(self):
        _stale(self.ws, self.aid, minutes=200)  # 超过 120 分钟但未到 2 倍：提示级
        added = self.ws.situation_scan()
        change = added[0]
        self.assertEqual(sm.ChangeSeverity.NOTICE.value, change.severity)
        version_before = self.ws.version
        self.ws.situation_scan()  # 同级重复：不写事件、不改状态
        self.assertEqual(version_before, self.ws.version)
        _stale(self.ws, self.aid, minutes=500)  # 超过 2 倍：升级为需复核
        self.ws.situation_scan()
        escalated = self.ws.situation_changes[change.change_id]
        self.assertEqual(sm.ChangeSeverity.REVIEW.value, escalated.severity)
        self.assertEqual(sm.ChangeStatus.NEW.value, escalated.status)
        self.assertEqual(version_before + 1, self.ws.version)
        audit = self.ws.events[-1]
        self.assertEqual('SituationChangeEscalated', audit.kind)
        self.assertEqual(escalated.event_id, audit.payload['event_id'])
        self.assertEqual(escalated.reason, audit.payload['reason'])
        self.assertEqual(escalated.severity, audit.payload['severity'])
        self.assertEqual(escalated.affected_annotation_ids,
                         audit.payload['affected_annotation_ids'])
        self.assertEqual((), self.ws.verify_audit_chain())

    def test_option_impact_via_sim_shapes_and_option_untouched(self):
        _stale(self.ws, self.aid)
        self.ws.add_option(Option(
            id="opt-1", title="方案甲", summary="北线阻隔",
            evidence_ids=(), claim_subjects=(),
            sim_shapes=({"geom": "line",
                         "pts": [[33.2212, 107.5562], [33.2216, 107.5566]],
                         "label": "北线阻隔"},),
        ))
        added = self.ws.situation_scan()
        self.assertIn(sm.ChangeKind.OPTION_AFFECTED.value,
                      tuple(c.kind for c in added))
        impact = next(c for c in self.ws.situation_changes.values()
                      if c.kind == sm.ChangeKind.OPTION_AFFECTED.value)
        self.assertEqual(("opt-1",), impact.affected_option_ids)
        self.assertIn(self.aid, impact.affected_annotation_ids)
        self.assertEqual("active", self.ws.options["opt-1"].status)

    def test_scan_never_touches_annotations(self):
        _stale(self.ws, self.aid)
        before = {k: (v.status.value, v.version) for k, v in self.ws.annotations.items()}
        self.ws.situation_scan()
        after = {k: (v.status.value, v.version) for k, v in self.ws.annotations.items()}
        self.assertEqual(before, after)
        self.assertEqual((), self.ws.verify_audit_chain())


class ObservationTest(unittest.TestCase):
    """§9.2 统一数据入口：fail-closed 与候选边界。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, scenario="p20-obs", test=self)
        self.ws = self.state.workspace
        _publish_staging(self.state)

    def _register(self, **kw):
        params = dict(
            kind="hotspot", title="东南坡新增烟点",
            geo_points=((33.2235, 107.5688),),
            observed_at=_ago(9), source_ref="无人机影像-9",
            positioning_status="rough", event_id="fire-001",
        )
        params.update(kw)
        return self.ws.register_observation(**params)

    def test_incomplete_observation_lands_as_draft_with_gap(self):
        result = self._register()
        self.assertEqual("draft", result["status"])
        self.assertTrue(result["missing_fields"])
        self.assertEqual(AnnotationSource.AI_SUGGESTION,
                         self.ws.annotations[result["annotation_id"]].source)
        kinds = {self.ws.situation_changes[c].kind for c in result["change_ids"]}
        self.assertIn(sm.ChangeKind.CANDIDATE_CREATED.value, kinds)
        self.assertIn(sm.ChangeKind.INFORMATION_GAP.value, kinds)

    def test_complete_located_observation_lands_as_pending_review(self):
        result = self._register(
            title="东南坡新烟点", positioning_status="located",
            details=dict(_HOTSPOT_FIELDS),
        )
        self.assertEqual("pending_review", result["status"])
        self.assertEqual((), result["missing_fields"])
        kinds = {self.ws.situation_changes[c].kind for c in result["change_ids"]}
        self.assertNotIn(sm.ChangeKind.INFORMATION_GAP.value, kinds)

    def test_observation_near_published_object_flags_conflict(self):
        result = self._register(
            title="火场边缘新烟点", geo_points=((33.22105, 107.55610),),
            positioning_status="located", details=dict(_HOTSPOT_FIELDS),
        )
        kinds = {self.ws.situation_changes[c].kind for c in result["change_ids"]}
        self.assertIn(sm.ChangeKind.OBSERVATION_CONFLICT.value, kinds)

    def test_fail_closed_without_geo_source_or_time(self):
        with self.assertRaises(ValueError):
            self._register(geo_points=())
        with self.assertRaises(ValueError):
            self._register(source_ref="  ")
        with self.assertRaises(ValueError):
            self._register(observed_at="  ")
        with self.assertRaises(ValueError):
            self._register(details={"not_a_field": "x"})
        with self.assertRaises(ValueError):
            self._register(kind="route")
        self.assertEqual({}, self.ws.situation_changes)

    def test_observation_never_publishes(self):
        result = self._register(
            title="东南坡新烟点", positioning_status="located",
            details=dict(_HOTSPOT_FIELDS),
        )
        mark = self.ws.annotations[result["annotation_id"]]
        self.assertIn(mark.status, {AnnotationStatus.DRAFT,
                                    AnnotationStatus.PENDING_REVIEW})
        self.assertNotEqual(AnnotationStatus.PUBLISHED, mark.status)


class ChangeActionAndReviewTest(unittest.TestCase):
    """§7 手动复核与变化处理：幂等、原因必填、让位与审计。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, scenario="p20-review", test=self)
        self.ws = self.state.workspace
        self.aid = _publish_staging(self.state)
        _stale(self.ws, self.aid)
        self.ws.situation_scan()
        self.change = next(iter(self.ws.situation_changes.values()))

    def test_ack_and_continue_require_reason(self):
        updated = self.ws.handle_situation_change(
            self.change.change_id, action="ack", by="local-general-entry")
        self.assertEqual(sm.ChangeStatus.ACKNOWLEDGED.value, updated.status)
        with self.assertRaises(ValueError):
            self.ws.handle_situation_change(
                self.change.change_id, action="continue", by="local-general-entry")
        updated = self.ws.handle_situation_change(
            self.change.change_id, action="continue",
            by="local-general-entry", reason="按现场回报维持现状")
        self.assertEqual(sm.ChangeStatus.DISMISSED.value, updated.status)

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            self.ws.handle_situation_change(
                self.change.change_id, action="publish", by="local-general-entry")

    def test_review_batch_is_idempotent_without_new_data(self):
        batch1, created1 = self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        self.assertTrue(created1)
        self.assertEqual((self.change.change_id,), batch1.source_change_ids)
        batch2, created2 = self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        self.assertFalse(created2)
        self.assertEqual(batch1.review_batch_id, batch2.review_batch_id)
        self.assertEqual(1, len(self.ws.situation_reviews))

    def test_review_moves_changes_to_reviewed(self):
        self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        stored = self.ws.situation_changes[self.change.change_id]
        self.assertEqual(sm.ChangeStatus.REVIEWED.value, stored.status)
        self.assertEqual("srev-001", stored.review_batch_id)

    def test_review_without_changes_reports_no_material_change(self):
        # 已阅仍算待复核（需人工核验）；只有明确“按现有资料继续”才退出复核清单
        self.ws.handle_situation_change(
            self.change.change_id, action="continue",
            by="local-general-entry", reason="按现场回报维持现状")
        batch, _ = self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        self.assertEqual(sm.ReviewOutcome.NO_MATERIAL_CHANGE.value, batch.outcome)
        self.assertIn("未覆盖的现场信息仍需人工核对", batch.summary)
        self.assertNotIn("维持当前态势与命令", batch.recommendation)

    def test_new_data_after_review_allows_new_batch(self):
        self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        self.ws.register_observation(
            kind="hotspot", title="北坡新烟点",
            geo_points=((33.2300, 107.5700),),
            observed_at=_ago(5), source_ref="无人机影像-10",
            positioning_status="located", details=dict(_HOTSPOT_FIELDS),
            event_id="fire-001",
        )
        batch2, created2 = self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        self.assertTrue(created2)
        self.assertEqual("srev-002", batch2.review_batch_id)
        self.assertEqual("srev-001", batch2.prior_review_batch_id)

    def test_withdraw_supersedes_related_changes(self):
        # 已发布对象被撤回：旧时效提示自动让位（只改台账，不改对象历史）
        self.state.handle_sand_annotation_status(self.aid, "withdrawn")
        stored = self.ws.situation_changes[self.change.change_id]
        self.assertEqual(sm.ChangeStatus.SUPERSEDED.value, stored.status)

    def test_all_writes_on_hash_chain(self):
        self.ws.handle_situation_change(
            self.change.change_id, action="ack", by="local-general-entry")
        self.ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        kinds = {e.kind for e in self.ws.events}
        for expected in ("SituationChangeRecorded", "SituationChangeHandled",
                         "SituationReviewCompleted"):
            self.assertIn(expected, kinds)
        self.assertEqual((), self.ws.verify_audit_chain())


class PersistenceTest(unittest.TestCase):
    """§11.1 快照往返：变化台账与复核批次一致、链完整。"""

    def test_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        state = _state(tmp, scenario="p20-persist", test=self)
        ws = state.workspace
        aid = _publish_staging(state)
        _stale(ws, aid)
        ws.situation_scan()
        ws.review_current_situation(
            requested_by="local-general-entry", event_id="fire-001")
        db = tmp / "snap.db"
        save_workspace(ws, db)
        loaded = load_workspace(db)
        self.assertEqual(sorted(ws.situation_changes), sorted(loaded.situation_changes))
        self.assertEqual(sorted(ws.situation_reviews), sorted(loaded.situation_reviews))
        self.assertEqual((), loaded.verify_audit_chain())
        self.assertIsInstance(loaded.situation_changes["sit-001"], sm.SituationChange)
        self.assertIsInstance(loaded.situation_reviews["srev-001"],
                              sm.SituationReviewBatch)
        again = loaded.situation_scan()
        self.assertEqual((), tuple(c.kind for c in again))


class PageTest(unittest.TestCase):
    """§8 前台：复核区可见性、公共屏隔离、零枚举泄漏。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = _state(self.tmp, scenario="p20-page", test=self)
        self.ws = self.state.workspace
        self.aid = _publish_staging(self.state)
        _stale(self.ws, self.aid)
        self.ws.situation_scan()

    def test_commander_sees_situation_block_and_actions(self):
        html = render_sand_page(self.state, role="commander")
        self.assertIn("态势复核", html)
        self.assertIn("复核当前态势", html)
        self.assertIn("/sand/review-current", html)
        self.assertIn("上报现场观测", html)
        self.assertIn("待复核变化 1 项", html)
        self.assertIn("正式对象需要复核", html)
        self.assertIn('data-feed="/sand/situation-feed"', html)
        self.assertIn("__cmdFocus", html)
        self.assertNotIn("source_freshness_expired", html)
        self.assertNotIn("needs_review", html)

    def test_member_sees_block_without_command_actions(self):
        html = render_sand_page(self.state, role="member")
        self.assertIn("态势复核", html)
        self.assertIn("上报现场观测", html)
        self.assertNotIn("/sand/review-current", html)
        self.assertNotIn("/sand/change-action", html)

    def test_public_screen_has_no_situation_block(self):
        html = render_sand_page(self.state, role="public")
        self.assertNotIn("态势复核", html)
        self.assertNotIn("/sand/situation-feed", html)
        self.assertNotIn("复核当前态势", html)


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
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8", "replace")
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


class HttpE2E(unittest.TestCase):
    """§12.2 HTTP 与权限：馈送只读、复核仅指挥员、成员可上报观测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        auth = GeneralEntryAuth(secret=SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p20-http", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            imagery_path=tmp / "imagery.json",
            upload_dir=tmp / "uploads",
        )
        self.state.login(SECRET)
        self.state.registry.import_person_library([
            PersonRecord(
                name="王五", phone="13912340002", unit="扑救二组",
                home_position="扑救二组（二分区）",
            ),
        ])
        self.state.handle_create_event(
            name="P20 HTTP 火情", report_no="测试-P20-02",
            discovered_at=_iso(120), source="测试", location="测试东坡",
            lat="33.21", lon="107.55",
        )
        duty = duty_token(self.state)
        for action in ("start", "issue-qr", "enter"):
            self.state.handle_event_action("fire-001", action,
                                           duty_token=duty if action == "start" else None)
        self._aid = _publish_staging(self.state, title="东坡集结点")
        _stale(self.state.workspace, self._aid)
        self.state.workspace.situation_scan()
        handler = make_server(self.state, port=0)
        self._thread = threading.Thread(target=handler.serve_forever, daemon=True)
        self._thread.start()
        # LIFO：先 shutdown 停事件循环，再 join，再关 socket
        self.addCleanup(handler.server_close)
        self.addCleanup(self._thread.join)
        self.addCleanup(handler.shutdown)
        self.base = f"http://127.0.0.1:{handler.server_address[1]}"
        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": SECRET})
        self.cmd = _cookie_of(headers, "panel_session")

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

    def test_feed_requires_login_and_is_read_only(self):
        st, headers, _ = _http("GET", f"{self.base}/sand/situation-feed")
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))
        version_before = self.state.workspace.version
        st, _, body = _http("GET", f"{self.base}/sand/situation-feed", cookie=self.cmd)
        self.assertEqual(200, st)
        data = json.loads(body)
        self.assertEqual(1, data["pending_count"])
        self.assertIn("force_review", data)
        self.assertIn(data["force_review"]["state"],
                      {"initial_pending", "current", "needs_reassessment", "unavailable"})
        self.assertEqual(version_before, self.state.workspace.version)
        member = self._member_session()
        st, _, body = _http("GET", f"{self.base}/sand/situation-feed", cookie=member)
        self.assertEqual(200, st)
        member_data = json.loads(body)
        self.assertEqual(1, member_data["pending_count"])
        self.assertNotIn("force_review", member_data)

    def test_feed_polls_audited_advice_without_writing(self):
        change = self.state.workspace.record_situation_change(dict(
            event_id=self.state.active_event_id,
            kind=sm.ChangeKind.INFORMATION_GAP.value,
            severity=sm.ChangeSeverity.REVIEW.value,
            fingerprint='f' * 64, reason='坡顶缺风向观测'))
        version = self.state.workspace.version
        page = render_sand_page(self.state, role='commander')
        self.assertIn('data-advice-version=', page)
        self.assertIn('建议（待人工核查）', page)
        st, _, body = _http('GET', f'{self.base}/sand/situation-feed', cookie=self.cmd)
        self.assertEqual(200, st)
        advice = json.loads(body)['advice']
        self.assertEqual(1, advice['total'])
        self.assertEqual(change.change_id, advice['items'][0]['id'])
        self.assertIn('补齐记录指出的缺项', advice['items'][0]['advice'])
        self.assertIn(f'data-advice-version="{advice["version"]}"', page)
        self.assertEqual(version, self.state.workspace.version)

    def test_feed_rejects_logged_in_member_outside_current_event(self):
        self.state.registry.import_person_library([
            PersonRecord(name="未入本事件人员", phone="13912340003", unit="其他队伍"),
        ])
        token, error = self.state.member_login("13912340003", DEMO_INITIAL_PASSWORD)
        self.assertEqual("", error)
        cookie = f"member_session={token}"
        version = self.state.workspace.version
        st, _, body = _http("GET", f"{self.base}/sand/situation-feed", cookie=cookie)
        self.assertEqual(404, st)
        self.assertNotIn("pending_count", body)
        self.state.member_sessions[token].bound_event_id = "other-event"
        st, _, body = _http("GET", f"{self.base}/sand/situation-feed", cookie=cookie)
        self.assertEqual(404, st)
        self.assertNotIn("pending_count", body)
        self.state.workspace.event_id = "other-event"
        st, _, body = _http("GET", f"{self.base}/sand/situation-feed", cookie=self.cmd)
        self.assertEqual(404, st)
        self.assertNotIn("pending_count", body)
        self.assertEqual(version, self.state.workspace.version)

    def test_review_current_is_commander_only_and_idempotent(self):
        member = self._member_session()
        before = len(self.state.workspace.situation_reviews)
        st, _, _ = _http("POST", f"{self.base}/sand/review-current", {}, cookie=self.cmd)
        self.assertEqual(303, st)
        self.assertEqual(before + 1, len(self.state.workspace.situation_reviews))
        st, _, _ = _http("POST", f"{self.base}/sand/review-current", {}, cookie=member)
        self.assertEqual(403, st)
        _http("POST", f"{self.base}/sand/review-current", {}, cookie=self.cmd)
        self.assertEqual(before + 1, len(self.state.workspace.situation_reviews))

    def test_member_can_submit_observation_but_not_change_actions(self):
        member = self._member_session()
        st, _, _ = _http(
            "POST", f"{self.base}/sand/observe",
            {"kind": "hotspot", "title": "东坡新烟点",
             "geo": "33.22350,107.56880", "observed_at": _ago(6),
             "source_ref": "现场回报-7", "positioning_status": "rough",
             "note": "", "is_demo": ""},
            cookie=member,
        )
        self.assertEqual(303, st)
        marks = [m for m in self.state.workspace.annotations.values()
                 if m.title == "东坡新烟点"]
        self.assertEqual(1, len(marks))
        self.assertIn(marks[0].status,
                      {AnnotationStatus.DRAFT, AnnotationStatus.PENDING_REVIEW})
        change_id = sorted(self.state.workspace.situation_changes)[-1]
        st, _, _ = _http(
            "POST", f"{self.base}/sand/change-action",
            {"change_id": change_id, "action": "ack", "reason": ""},
            cookie=member,
        )
        self.assertEqual(403, st)
        st, _, _ = _http(
            "POST", f"{self.base}/sand/change-action",
            {"change_id": change_id, "action": "ack", "reason": ""},
            cookie=self.cmd,
        )
        self.assertEqual(303, st)
        self.assertEqual(
            sm.ChangeStatus.ACKNOWLEDGED.value,
            self.state.workspace.situation_changes[change_id].status,
        )

    def test_anonymous_observe_redirects_to_login(self):
        st, headers, _ = _http(
            "POST", f"{self.base}/sand/observe",
            {"kind": "hotspot", "title": "x", "geo": "33.22,107.55",
             "observed_at": _ago(6), "source_ref": "s"},
        )
        self.assertEqual(303, st)
        self.assertIn("/login", headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
