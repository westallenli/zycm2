"""P15 T4 — workbench views: map, org roster flow, drill snapshots, estimator
card, projector persistence, plain-words layer."""

from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace

SECRET = "p15-test-secret"


def _locked_state() -> PanelState:
    auth = GeneralEntryAuth(secret=SECRET)
    ws = ScenarioWorkspace("p15-wb", general_auth=auth)
    return PanelState(ws, auth=auth)


def _command_pair() -> tuple[PanelState, str]:
    state = _locked_state()
    token = state.login(SECRET)
    assert token, "login must succeed with the bound secret"
    return state, token


class MapViewTest(unittest.TestCase):
    def test_locked_map_readable_and_formless(self):
        state = _locked_state()
        status, body = render_page(state, "/map", session_token=None)
        self.assertEqual(200, status)
        self.assertIn("<svg", body)               # 战术地图主体
        self.assertIn("组织匹配", body)            # 流程条第一步可见
        self.assertNotIn("<form", body)           # 锁定态零表单
        self.assertIn("白话：", body)              # 白话层在锁定态同样可读

    def test_commander_map_renders_summary_and_todo(self):
        state, token = _command_pair()
        status, body = render_page(state, "/map", session_token=token)
        self.assertEqual(200, status)
        self.assertIn("态势研判", body)
        self.assertIn("待你决断", body)
        self.assertIn('body class=""', body)      # 非投屏态

    def test_locked_org_view_hides_forms(self):
        state = _locked_state()
        status, body = render_page(state, "/org", session_token=None)
        self.assertEqual(200, status)
        self.assertIn("编成架构树", body)
        self.assertNotIn("<form", body)


class OrgRosterFlowTest(unittest.TestCase):
    def setUp(self):
        self.state, self.token = _command_pair()

    def test_import_checkin_render_masking_checkout(self):
        result = self.state.handle_import_roster(
            "张三,13900000001,演练大队,扑救员,是\n李四,13900000002,演练大队,扑救员,\n"
        )
        self.assertEqual(2, result["count"])
        self.state.handle_checkin("pjjz-1", phone="13900000001")

        # 观察态：姓名可见、电话脱敏
        _, observer_body = render_page(self.state, "/org", session_token=None)
        self.assertIn("张三", observer_body)
        self.assertIn("139****0001", observer_body)
        self.assertNotIn("13900000001", observer_body)

        # 指挥态：全号可外呼
        _, commander_body = render_page(self.state, "/org", session_token=self.token)
        self.assertIn("13900000001", commander_body)

        # 一人一岗：同手机换槽位被拒（ValueError → HTTP 层 409）
        with self.assertRaises(ValueError) as ctx:
            self.state.handle_checkin("pjjz-2", phone="13900000001")
        self.assertIn("一人一岗", str(ctx.exception))

        # 离岗后回到灰位
        self.state.handle_checkout("pjjz-1|13900000001")
        _, after = render_page(self.state, "/org", session_token=None)
        self.assertIn("实到 0 人", after)

    def test_bad_csv_row_blocks_whole_batch(self):
        with self.assertRaises(Exception) as ctx:
            self.state.handle_import_roster("张三,not-a-phone")
        self.assertIn("逐行错误", str(ctx.exception))
        self.assertEqual(len(self.state.workspace.person_library), 0)

    def test_duplicate_phone_in_library_upserts(self):
        self.state.handle_import_roster("张三,13900000001,一队,,\n")
        self.state.handle_import_roster("张三,13900000001,二队,,\n")
        self.assertEqual(1, len(self.state.workspace.person_library))
        self.assertEqual("二队", self.state.workspace.person_library["13900000001"].unit)


class DrillAccessTest(unittest.TestCase):
    def test_drill_hidden_when_locked(self):
        state = _locked_state()
        status, _body = render_page(state, "/drill", session_token=None)
        self.assertEqual(404, status)

    def test_drill_visible_in_command_mode(self):
        state, token = _command_pair()
        status, body = render_page(state, "/drill", session_token=token)
        self.assertEqual(200, status)
        self.assertIn("新建演练", body)


class SnapshotLifecycleTest(unittest.TestCase):
    def test_save_list_load_preserves_roster(self):
        state, _token = _command_pair()
        state.handle_import_roster("张三,13900000001,演练大队,,\n李四,13900000002,演练大队,,\n")
        state.handle_checkin("pjjz-1", phone="13900000001")
        state.handle_checkin("log-medical", phone="13900000002")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wb, "SNAPSHOT_DIR", Path(tmp)):
                saved = state.handle_save_snapshot()
                filename = saved["filename"]
                names = {entry["name"] for entry in wb.list_snapshots(Path(tmp))}
                self.assertIn(filename, names)

                # 面板内装载：工作区被换、花名册保持、权限以活会话凭据重新开门
                result = state.handle_load_snapshot(filename)
                self.assertEqual(result["scenario_id"], state.workspace.scenario_id)
                self.assertEqual(2, state.workspace.org_chart()["total_members"])
                self.assertTrue(state.entry_unlocked)

                # 独立恢复路径：快照文件本身可被 load_workspace 重建
                restored = load_workspace(Path(tmp) / filename)
                self.assertEqual(2, restored.org_chart()["total_members"])

    def test_load_unknown_snapshot_refused(self):
        state, _token = _command_pair()
        with self.assertRaises(Exception):
            state.handle_load_snapshot("no-such-snapshot.db")


class EstimatorCardTest(unittest.TestCase):
    def _state_with_assault(self, per_slot: int) -> tuple[PanelState, str]:
        state, token = _command_pair()
        slots = ("pjjz-1", "pjjz-2", "pjjz-3", "pjjz-4")
        total = per_slot * 4
        rows = "\n".join(
            f"队员{index:03d},137{index:08d},演练大队,," for index in range(1, total + 1)
        )
        state.handle_import_roster(rows)
        for index in range(1, total + 1):
            state.handle_checkin(slots[(index - 1) // per_slot], phone=f"137{index:08d}")
        return state, token

    def test_forty_eight_assault_members_fold_into_two_crews(self):
        # 4 槽位 × 12 人（槽位上限），共 48 人 → ceil(48/30)=2 个标准分队
        state, token = self._state_with_assault(12)
        _, body = render_page(state, "/drill", session_token=token)
        self.assertIn("2 个分队", body)
        self.assertIn("预计 5.0 小时", body)

    def test_empty_roster_reports_unstaffed(self):
        state, token = _command_pair()
        _, body = render_page(state, "/drill", session_token=token)
        self.assertIn("尚无一线作战人员", body)


class ProjectorPersistenceTest(unittest.TestCase):
    def test_projector_flag_marks_body_and_rides_nav_links(self):
        state, token = _command_pair()
        _, plain_body = render_page(state, "/map", session_token=token)
        _, proj_body = render_page(
            state, "/map", session_token=token, query={"projector": ["1"]}
        )
        # CSS 注释里含字面量 ?projector=1，因此只看链接与 body 类标记
        self.assertNotIn('body class="projector"', plain_body)
        self.assertNotRegex(plain_body, r'href="[^"]*projector=1')
        self.assertIn('body class="projector"', proj_body)
        self.assertRegex(proj_body, r'href="[^"]*projector=1')


class PlainWordsLayerTest(unittest.TestCase):
    def test_reserve_term_renders_on_org_view(self):
        state, token = _command_pair()
        _, body = render_page(state, "/org", session_token=token)
        self.assertIn("留着机动补救的力量", body)

    def test_audit_chain_word_defined(self):
        self.assertIn("审计链", wb._PLAIN_WORDS)
        self.assertIn("哈希", wb._PLAIN_WORDS["审计链"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DrillHttpRoundtripTest(unittest.TestCase):
    def _post(self, base: str, route: str, fields: dict, cookie: str | None = None):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(base + route, data=data, method="POST")
        if cookie:
            request.add_header("Cookie", cookie)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(request, timeout=5)
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8")

    def test_login_import_checkin_duplicate_conflict_over_http(self):
        state = _locked_state()
        server = make_server(state, port=0)
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        try:
            threading.Thread(target=server.serve_forever, daemon=True).start()

            # 登录拿会话 Cookie（303 → /map）
            status, headers, _ = self._post(base, "/login", {"secret": SECRET})
            self.assertEqual(303, status)
            cookie = headers["Set-Cookie"].split(";", 1)[0]

            # 导入花名册 + 模拟扫码，各 303 落回
            status, _, _ = self._post(
                base,
                "/drill/import-roster",
                {"roster_csv": "张三,13900000001,演练大队,扑救员,是"},
                cookie=cookie,
            )
            self.assertEqual(303, status)
            status, _, _ = self._post(
                base,
                "/drill/checkin",
                {"pos_code": "pjjz-1", "phone": "13900000001"},
                cookie=cookie,
            )
            self.assertEqual(303, status)

            # 架构图出现姓名与全号（指挥模式）
            request = urllib.request.Request(base + "/org")
            request.add_header("Cookie", cookie)
            with urllib.request.urlopen(request, timeout=5) as resp:
                org_body = resp.read().decode("utf-8")
            self.assertIn("张三", org_body)
            self.assertIn("13900000001", org_body)

            # 同手机二次签到 → 409 且点名一人一岗
            status, _, conflict_body = self._post(
                base,
                "/drill/checkin",
                {"pos_code": "pjjz-2", "phone": "13900000001"},
                cookie=cookie,
            )
            self.assertEqual(409, status)
            self.assertIn("一人一岗", conflict_body)

            # 无会话 POST → 403（不泄露存在性）
            status, _, _ = self._post(
                base, "/drill/checkin", {"pos_code": "pjjz-2", "phone": "13900000001"}
            )
            self.assertEqual(403, status)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
