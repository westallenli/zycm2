"""P18 第四批：前台全中文钉 + 本地离线瓦片包服务（docs/19 口径）。

- 页面文本不出现英文枚举原值/英文归属串/「T+」；底图默认高德在线源可用；
- tileserve：XYZ 目录与 MBTiles（TMS 翻转）读取、越界与穿越路径 fail-closed；
- /offlinemap 路由：命中回瓦片，缺包/非法一律 404；
- 决断/推荐模板为中文（新记录不再出现 Recommend/Adopted 句式）。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from panda_brain.auth import GeneralEntryAuth
from panda_brain.fire_events import EventRegistry
from panda_brain.map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
)
from panda_brain.panel import PanelState, make_server
from panda_brain.sandview import render_sand_page
from panda_brain import tileserve
from panda_brain.workspace import ScenarioWorkspace


class _FakeState:
    """render_sand_page 只需 workspace 与若干惰性属性（与 test_p18_cmd_page 同构）。"""

    def __init__(self, ws: ScenarioWorkspace):
        self.workspace = ws
        self.imagery = None
        self.ledger = None
        self.active_event_id = ""
        self.registry = EventRegistry()
        self.sand_qa_log = []
        self.intent_notice = ""
        self.voice_drafts = {}
        self.advisor_log = []


def _geo_mark(ws: ScenarioWorkspace, ann_id: str, title: str, status: AnnotationStatus,
              pts: tuple[tuple[float, float], ...]) -> None:
    ws.record_annotation(
        MapAnnotation(
            id=ann_id,
            kind=AnnotationKind.FIRE_POINT,
            points=(),
            geo_points=pts,
            title=title,
            status=status,
            source=AnnotationSource.MANUAL,
        )
    )


_SECRET = "p18-cn-secret"
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100" "05" "fe" "a2" "8c" "0000000049454e44ae426082"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _get(url: str, *, cookie=""):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        request = urllib.request.Request(url, headers={"Cookie":cookie} if cookie else {})
        with opener.open(request, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {}, exc.read()


class PageChineseTest(unittest.TestCase):
    def _html(self) -> str:
        ws = ScenarioWorkspace("p18-cn")
        _geo_mark(ws, "ann-001", "东线火点", AnnotationStatus.PUBLISHED, ((33.21, 107.55),))
        with mock.patch.object(tileserve, "OFFLINE_ROOT", Path(tempfile.mkdtemp())):
            html = render_sand_page(_FakeState(ws), role="commander")
        return html

    def test_no_english_leaks(self) -> str:
        html = self._html()
        for bad in (
            "OpenStreetMap contributors",
            "Recommend '", "Adopted '", "Conditional recommendation",
            "T+", "doc 0", "GeneralDecision",
        ):
            self.assertNotIn(bad, html)
        self.assertIn("开放街道地图贡献者", html)  # 归属保留但已中文
        self.assertIn("高德地图（在线）", html)
        self.assertIn("起火后", html)
        self.assertIn("规范 03·§4", html)          # 规范引用中文化

    def test_default_base_gaode_without_offline_pack(self):
        html = self._html()
        self.assertIn('"base": "gaode"', html)          # 无离线包默认高德
        self.assertNotIn('data-base-key="local"', html)  # 缺包时不渲染该选项

    def test_online_terrain_is_optional_and_discloses_source_limits(self):
        html = self._html()
        self.assertIn('data-base-key="opentopo"', html)
        self.assertIn('开源地形（在线，非影像）', html)
        self.assertIn('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', html)
        self.assertIn('https://creativecommons.org/licenses/by-sa/3.0/', html)
        self.assertIn('id="cmd-base-notice"', html)
        self.assertIn('id="cmd-base-credit"', html)
        self.assertIn('尚未经独立控制点核对', html)
        self.assertNotIn('data-base-key="opentopo" checked', html)

    def test_offline_option_appears_when_pack_exists(self):
        from .test_upgrade_p16_offline import P16Tests
        case=P16Tests();case.setUp();self.addCleanup(case.doCleanups)
        case.map()
        case.state.workspace=case.event_space();case.state.active_event_id=case.event_id
        html=render_sand_page(case.state,role='commander')
        self.assertIn('data-base-key="local"',html)
        self.assertIn('"base": "local"',html)
        # Merely placing an undeclared directory is no longer authorization.
        ws=ScenarioWorkspace('anonymous-unlicensed-directory')
        html=render_sand_page(_FakeState(ws),role='commander')
        self.assertNotIn('data-base-key="local"',html)


class TileserveUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._root = tileserve.OFFLINE_ROOT
        tileserve.OFFLINE_ROOT = self.tmp

    def tearDown(self):
        tileserve.OFFLINE_ROOT = self._root
        tileserve._conn, tileserve._conn_path = None, ""

    def test_xyz_dir_hit_and_miss(self):
        d = self.tmp / "tiles" / "1" / "0"
        d.mkdir(parents=True)
        (d / "1.png").write_bytes(_PNG_1PX)
        hit = tileserve.tile("1/0/1.png")
        self.assertIsNotNone(hit)
        self.assertEqual("image/png", hit[1])
        self.assertIsNone(tileserve.tile("1/0/0.png"))
        self.assertTrue(tileserve.available())

    def test_flat_dir_layout_also_supported(self):
        d = self.tmp / "3" / "4"
        d.mkdir(parents=True)
        (d / "5.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 20)
        hit = tileserve.tile("3/4/5.jpg")
        self.assertIsNotNone(hit)
        self.assertEqual("image/jpeg", hit[1])

    def test_mbtiles_tms_flip(self):
        db = sqlite3.connect(self.tmp / "pack.mbtiles")
        db.execute("CREATE TABLE tiles (zoom_level int, tile_column int,"
                   " tile_row int, tile_data blob)")
        z, x, y_xyz = 2, 1, 2
        tms_y = (1 << z) - 1 - y_xyz
        db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, tms_y, _PNG_1PX))
        db.commit()
        db.close()
        self.assertTrue(tileserve.available())
        hit = tileserve.tile(f"{z}/{x}/{y_xyz}.png")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], _PNG_1PX)
        self.assertEqual("image/png", hit[1])

    def test_traversal_and_bounds_fail_closed(self):
        self.assertIsNone(tileserve.tile("../../etc/passwd"))
        self.assertIsNone(tileserve.tile("1/9/9.png"))     # x 越界
        self.assertIsNone(tileserve.tile("99/0/0.png"))    # z 越界
        self.assertIsNone(tileserve.tile("1/0/0.txt"))


class OfflineRouteE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._root = tileserve.OFFLINE_ROOT
        tileserve.OFFLINE_ROOT = self.tmp
        self.addCleanup(setattr, tileserve, "OFFLINE_ROOT", self._root)
        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p18-cn-e2e", general_auth=auth),
            auth=auth,
            registry_path=self.tmp / "reg.json",
            materials_path=self.tmp / "m.json",
            reviews_path=self.tmp / "r.json",
            imagery_path=self.tmp / "i.json",
            upload_dir=self.tmp / "uploads",
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.shutdown)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_404_without_pack_and_with_bad_path(self):
        st, _, _ = _get(f"{self.base}/offlinemap/1/0/0.png")
        self.assertEqual(404, st)
        st, _, _ = _get(f"{self.base}/offlinemap/..%2f..%2fetc.png")
        self.assertEqual(404, st)

    def test_200_with_pack(self):
        from .test_upgrade_p16_offline import P16Tests,package
        case=P16Tests();case.setUp();self.addCleanup(case.doCleanups)
        case.map(archive_base64=package(files={'tiles/0/0/0.png':_PNG_1PX}))
        case.state.workspace=case.event_space();case.state.active_event_id=case.event_id
        server=make_server(case.state,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        url=f'http://127.0.0.1:{server.server_port}/offlinemap/0/0/0.png'
        self.assertEqual(404,_get(url)[0])
        st,headers,body=_get(url,cookie='member_session='+case.tokens[case.SOURCE])
        self.assertEqual(200,st);self.assertEqual('image/png',headers.get('Content-Type'))
        self.assertEqual(_PNG_1PX,body)


class CnTemplateTest(unittest.TestCase):
    def test_recommendation_templates_chinese(self):
        from panda_brain.chief_advisor import ChiefAdvisor

        row = types.SimpleNamespace(
            option_id="opt-a", reversibility="reversible",
            no_go_triggered=False, feasibility_status="feasible",
            preconditions_unmet=[], blocking_conditions=[],
        )
        rec = ChiefAdvisor._compose_recommendation([row])
        self.assertTrue(rec.startswith("推荐「opt-a」"))
        self.assertNotIn("Recommend", rec)
        row2 = types.SimpleNamespace(
            option_id="opt-b", reversibility="partially_reversible",
            no_go_triggered=False, feasibility_status="feasible",
            preconditions_unmet=["route:R2"], blocking_conditions=[],
        )
        rec2 = ChiefAdvisor._compose_recommendation([row2])
        self.assertTrue(rec2.startswith("有条件推荐「opt-b」"))
        self.assertIn("备选路线 R2", rec2)

    def test_policy_rule_texts_chinese(self):
        from panda_brain.workspace import POLICY_RULES

        for key, (text, _) in POLICY_RULES.items():
            self.assertNotRegex(text, r"[a-z]{3,} [a-z]{3,}", f"{key} 仍含英文句")


if __name__ == "__main__":
    unittest.main()
