"""P17-P4 验收：人员与物资页五页化收口（DEVELOPMENT_PLAN_PAGES_V1 §P4）。

- 长期人员库与编制概览入口统一在本页；
- 本次到场人员八态上页（只读展示，判定动作留在名册配置页）；
- 缺口测算为业务口径措辞，零后台来源名词；
- 成员只读 + 电话脱敏；无名册/无快照诚实空态。
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
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p17-matbiz-secret"


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


class MaterialsBizE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

        auth = GeneralEntryAuth(secret=_SECRET)
        state = PanelState(
            ScenarioWorkspace("p17-matbiz", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            alarm_inbox_path=tmp / "alarms.json",
            materials_path=tmp / "materials.json",
        )
        state.registry.import_person_library(
            [
                PersonRecord(
                    name="张三", phone="13912340001", unit="县防火办",
                    home_position="扑救一组（一分区）",
                ),
                PersonRecord(name="李四", phone="13912340002", unit="林场哨所"),
            ]
        )
        self.state = state
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

        status, headers, _ = _http("POST", f"{self.base}/login", {"secret": _SECRET})
        self.cmd = _cookie_of(headers, "panel_session")
        self.cmd += "; " + duty_cookie(self.state, phone="13912340001")
        received=receive_confirmed_fixture(self.state, {"name": "汉江山火", "report_no": "HC-9",
             "discovered_at": "2026-08-24T09:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST", f"{self.base}/events/action",
                {"event_id": "fire-001", "action": action},
                cookie=self.cmd,
            )
        for phone in ("13912340001", "13912340002"):
            personnel_submit(self.state, _http,
                "POST", f"{self.base}/events/fire-001/roster/add",
                {"phones": phone},
                cookie=self.cmd,
            )

    def _onboard_member(self, phone: str) -> str:
        _, headers, _ = _http(
            "POST", f"{self.base}/login/member",
            {"phone": phone, "password": DEMO_INITIAL_PASSWORD},
        )
        member = _cookie_of(headers, "member_session")
        _http("POST", f"{self.base}/checkin/scan", {"qr": "qr-001"}, cookie=member)
        _http("POST", f"{self.base}/checkin/confirm", {"qr": "qr-001"}, cookie=member)
        return member


class LibraryAndStaffingCardTest(MaterialsBizE2E):
    def test_library_card_counts_and_staffing_line(self):
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("<h2>长期人员库与编制</h2>", body)
        self.assertIn(f"在库人员 <b>{3}</b> 人", body)
        self.assertIn("涉及单位 <b>3</b> 个", body)
        # 编制岗位概览：32 组来自 org_model（数据驱动，不写死合计数）
        from panda_brain.org_model import POSITION_SLOTS

        self.assertIn(
            f"编制岗位：{len(POSITION_SLOTS)} 个一线组 · "
            f"编制上限合计 {sum(s.max_headcount for s in POSITION_SLOTS)} 人",
            body,
        )


class EightStateOnPageTest(MaterialsBizE2E):
    def test_bound_member_shows_arrived_with_masking_rules(self):
        member = self._onboard_member("13912340001")
        st, _, body_cmd = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("<h2>本次到场人员（八态）</h2>", body_cmd)
        self.assertIn("已到场", body_cmd)
        self.assertIn("扑救一组（一分区）", body_cmd)
        self.assertIn("13912340001", body_cmd)          # 指挥员全显
        self.assertIn("13912340002", body_cmd)          # 名册内未到场者也全显

        st, _, body_mem = _http("GET", f"{self.base}/materials", cookie=member)
        self.assertEqual(200, st)
        self.assertIn("本次到场人员（八态）", body_mem)
        self.assertIn("13912340001", body_mem)           # 本人完整号码
        self.assertNotIn("13912340002", body_mem)        # 他人打码
        self.assertIn("139****0002", body_mem)
        # 本人行高亮
        self.assertIn("background:#F0F7F4", body_mem)

    def test_checkout_reflects_departed_with_basis(self):
        self._onboard_member("13912340001")
        self.state.workspace.check_out("pjjz-1", phone="13912340001")
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn(">已撤离</span>", body)
        self.assertIn("依据：", body)

    def test_empty_roster_honest_empty_state(self):
        # 新事件未配名册 → 八态卡如实空态，不显示虚构名单
        received=receive_confirmed_fixture(self.state, {"name": "空名册事件", "report_no": "HC-X",
             "discovered_at": "2026-08-24T10:00:00+08:00",
             "location": "青岗县", "source": ""})
        self.assertEqual("CREATED",received["code"])
        for action in ("start", "issue-qr", "enter"):
            _http(
                "POST", f"{self.base}/events/action",
                {"event_id": "fire-002", "action": action},
                cookie=self.cmd,
            )
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("本事件尚无名册与到场人员", body)


class BusinessWordingTest(MaterialsBizE2E):
    """业务口径措辞扫描只看正文区（lz-wrap 内）。

    页壳顶栏含产品品牌「破局智援参谋·前指工作台」（当前产品名称），
    不属于自述词；禁词判定必须落在业务内容上，否则会被壳误报。
    """

    @staticmethod
    def _main(body: str) -> str:
        return body.split('<div class="lz-wrap">', 1)[-1]

    def test_gap_card_uses_business_wording_not_advisor_words(self):
        self._seed_min_ledger()
        self._onboard_member("13912340001")
        st, _, body = _http("GET", f"{self.base}/materials", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertIn("缺口与补给建议", body)
        self.assertIn("演练测算口径：火线", body)
        main = self._main(body)
        for word in ("参谋", "智脑", "AI", "LLM"):
            self.assertNotIn(word, main)

    def test_all_roles_no_backend_source_nouns(self):
        member = self._onboard_member("13912340001")
        for cookie, tag in ((self.cmd, "commander"), (member, "member")):
            with self.subTest(role=tag):
                st, _, body = _http("GET", f"{self.base}/materials", cookie=cookie)
                self.assertEqual(200, st)
                main = self._main(body)
                for word in ("参谋", "智脑", "LLM"):
                    self.assertNotIn(word, main)

    def _seed_min_ledger(self):
        # Domain fixtures do not impersonate a runtime grant.
        ledger = self.state.ledger
        ledger.upsert_item(name="高压水泵", category="装备", unit="台", default_carry_qty=4)
        ledger.upsert_item(name="水带 20m", unit="盘", default_carry_qty=20)
        ledger.add_squad(name="扑救一分队", member_phones=("13912340001",), headcount_planned=12)
        ledger.set_carry_line("sq-001", "mat-001")
        ledger.set_carry_line("sq-001", "mat-002")
        ledger.save(self.state.materials_path)


class MemberReadOnlyStillHoldsTest(MaterialsBizE2E):
    def test_member_sees_no_maintenance_forms(self):
        member = self._onboard_member("13912340001")
        st, _, body = _http("GET", f"{self.base}/materials", cookie=member)
        self.assertEqual(200, st)
        for form_action in (
            "/materials/item", "/materials/squad",
            "/materials/carry", "/materials/point", "/materials/check",
        ):
            self.assertNotIn(form_action, body)


if __name__ == "__main__":
    unittest.main()
