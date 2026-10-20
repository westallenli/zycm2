"""P17 演示增强回归钉：图上手动标绘工具 + 演示场景数据完备性。

- 标绘作业必须提供「图上取点」交互（开关/坐标回填/清空），且仅指挥员可见；
- data/demo/ 演示场景（tools/build_demo_scenario.py 产物）必须让五页一打开
  就有内容：外部报警待确认、标绘核验链、方案/命令、物资缺口、复盘台账；
  data/demo 是只读种子（bat 复制成 data/work 运行），用户折腾不脏它。
"""

from __future__ import annotations

import shutil
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
from panda_brain.campaign_runner import DRILL_SECRET
from panda_brain.panel import PanelState, load_state, make_server
from panda_brain.persistence import load_workspace
from panda_brain.sandview import _marks_payload

_DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"


def _http(method: str, url: str, form: dict | None = None, cookie: str = ""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)

    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NR)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers.items()), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read().decode("utf-8")


def _cookie_of(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        if part.strip().startswith(f"{name}="):
            return part.strip()
    return ""


class DrawToolTest(unittest.TestCase):
    """图上直接绘制工具在指挥员沙盘中在位（P18 重排后接替旧取点表单）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        auth = GeneralEntryAuth(secret="pick-secret")
        from panda_brain.workspace import ScenarioWorkspace

        state = PanelState(
            ScenarioWorkspace("p17-pick", general_auth=auth),
            auth=auth,
            registry_path=self.tmp / "reg.json",
            materials_path=self.tmp / "mat.json",
        )
        self.server = make_server(state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.shutdown)
        _, headers, _ = _http("POST", f"{self.base}/login", {"secret": "pick-secret"})
        self.cmd = _cookie_of(headers, "panel_session")

    def test_commander_tools_have_draw_controls(self):
        st, _, html = _http("GET", f"{self.base}/sand", cookie=self.cmd)
        self.assertEqual(200, st)
        for marker in (
            'id="cmd-draw-point"',    # 图上落点工具
            "完成绘制",                # 线/面多顶点结束钮
            "cmd-undo",               # 撤销上一顶点
            "cmd-map-data",           # 地图载荷 JSON（含已录坐标标绘）
            "L.map(",                 # 真实底图初始化
            'data-cmd-layer="published"',  # 图层开关
        ):
            self.assertIn(marker, html)

    def test_public_screen_sees_no_draw_tool(self):
        """公共屏只显已发布内容——绘制作业与助手面板不得出现。"""
        st, _, html = _http("GET", f"{self.base}/sand?projector=1", cookie=self.cmd)
        self.assertEqual(200, st)
        self.assertNotIn('data-draw-kind="fire_point"', html)
        self.assertNotIn('action="/sand/annotate"', html)
        self.assertNotIn('id="cmd-advisor"', html)


@unittest.skipUnless(_DEMO.exists() and (_DEMO / "demo_hanjiang.db").exists(),
                     "演示场景未构建（运行 tools/build_demo_scenario.py）")
class DemoScenarioTest(unittest.TestCase):
    """data/demo 种子场景：五页一打开就有可演示的业务内容。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        # 复制到工作副本再挂服务器：种子目录保持只读
        for f in _DEMO.iterdir():
            shutil.copy2(f, cls.tmp / f.name)
        auth = GeneralEntryAuth(secret=DRILL_SECRET)
        cls.state = load_state(db_path=cls.tmp / "demo_hanjiang.db", auth=auth)
        # 种子注册表里烧的是构建机上的绝对快照路径（impl/snapshots/fire-001.db）；
        # 不中和的话，后续任何镜像动作都会写回共享文件（历次“测试污染”同源）。
        from dataclasses import replace as _dc_replace
        for event_id, record in cls.state.registry.events.items():
            path = getattr(record, "snapshot_path", "") or ""
            if path and not Path(path).is_relative_to(cls.tmp):
                cls.state.registry.events[event_id] = _dc_replace(
                    record, snapshot_path=str(cls.tmp / Path(path).name))
        open_events = [
            e for e in sorted(cls.state.registry.events)
            if cls.state.registry.events[e].status != "closed"
        ]
        if open_events:
            cls.state.active_event_id = open_events[-1]
        cls.server = make_server(cls.state, port=0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        _, headers, _ = _http("POST", f"{cls.base}/login", {"secret": DRILL_SECRET})
        cls.cmd = _cookie_of(headers, "panel_session")

        def fin():
            cls.server.shutdown()
            shutil.rmtree(cls.tmp, ignore_errors=True)
        cls.addClassCleanup(fin)

    def _get(self, path: str) -> str:
        st, _, html = _http("GET", f"{self.base}{path}", cookie=self.cmd)
        self.assertEqual(200, st, path)
        return html

    def test_registry_and_sidecars_loaded(self):
        """load_state(db) 的侧车同目录挂载：注册表/收件箱/物资/复盘全部就位。"""
        self.assertEqual(len(self.state.registry.events), 1)
        self.assertGreaterEqual(len(self.state.alarm_inbox.alarms), 2)
        self.assertTrue(self.state.ledger.items)
        self.assertEqual(self.state.review_ledger.owner_phone, "13912340002")

    def test_events_page_has_pending_alarms(self):
        html = self._get("/events")
        self.assertIn("卫星热点", html)
        self.assertIn("待确认", html)

    def test_sand_has_annotations_and_command_shell(self):
        """P19：演示首屏为战术态势对象（火场/火线/隔火带/保障点）。"""
        html = self._get("/sand")
        for marker in ("东线重点火线", "河谷模拟取水点", "前线指挥部", "北侧已开设隔火带",
                      "初始研判火场范围", "东侧模拟保护目标", "扑救一分队回报轨迹"):
            self.assertIn(marker, html)
        # P18 指挥工作台壳与参谋链可见：方案区、候选、命令反馈进页面
        self.assertIn("应急预案 · 方案比较与推演", html)
        self.assertIn("待核验候选", html)
        self.assertIn("命令与执行反馈", html)
        # P19 图层与符号：6 组业务图层 + 战术工具分组常驻
        for layer in ("fire", "control", "force", "support", "risk", "evidence"):
            self.assertIn(f'data-layer-group="{layer}"', html)
        self.assertIn('data-draw-kind="firebreak"', html)

    def test_map_object_links_command_and_evidence_without_public_leak(self):
        marks = _marks_payload(self.state, "commander")
        fireline = next(m for m in marks if m["title"] == "东线重点火线")
        route = next(m for m in marks if m["title"] == "扑救一分队确认路线")
        self.assertIn(["证据编号", "ev-demo-source-01"], fireline["linked_evidence"])
        self.assertTrue(any(row[0] == "来源记录" for row in fireline["linked_evidence"]))
        self.assertTrue(any(row[0] == "命令编号" for row in route["linked_command"]))
        public = _marks_payload(self.state, "public")
        public_fireline = next(m for m in public if m["title"] == "东线重点火线")
        self.assertNotIn("linked_evidence", public_fireline)
        self.assertNotIn("linked_command", public_fireline)

    def test_materials_gap_numbers(self):
        html = self._get("/materials")
        self.assertIn("高压水泵", html)
        self.assertIn("缺口", html)
        self.assertIn("已撤离", html)   # 王五八态演示位
        self.assertIn("请先进入事件任务空间", html)  # 旧战役库不能充作事件任务证据

    def test_review_tabs_content(self):
        for path, marker in (
            ("/review", "李四"),
            ("/review?tab=rect", "复核隔离带选线"),
            ("/review?tab=exp", "核对下风侧风险"),
            ("/review?tab=root", "隔离带开口位置偏东"),
        ):
            with self.subTest(path=path):
                self.assertIn(marker, self._get(path))

    def test_audit_chain_intact(self):
        self.assertEqual(tuple(self.state.workspace.verify_audit_chain()), ())


if __name__ == "__main__":
    unittest.main()
