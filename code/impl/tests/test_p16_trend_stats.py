"""P16-T15 验收：复盘趋势与统计口径（doc 05 §7）。

定版纪律：指标公开口径（名称/计算方法/样本数量/缺失处理/时间边界）、
可下钻到事实、不做黑盒综合评分；无数据源的指标如实标注，绝不编造。

覆盖：
- 到场时长聚合：双时间戳齐备才纳入，缺任一即排除并留痕；
- 异常任务占比：链上 OrderActivated/OrderAnomalyPaused 计数；
- 物资缺口频次：台账到场快照 missing>0 行占比；
- 六项结构性无数据源指标如实交代理由；指标总数 9 = 可算 3 + 无源 6；
- 只读纯函数（两次计算逐字节同结果）；签到事件增量 at 时间戳入链；
- 复盘页 trend 标签指挥员可见、成员隐藏且回退 summary。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import Event, PersonRecord
from panda_brain.panel import PanelState, make_server
from panda_brain.reviewview import render_review_page
from panda_brain.trend_stats import compute_trend_metrics
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p15-trend-secret"
_T0 = datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone(+timedelta(hours=8)))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _evt(seq: int, kind: str, **payload) -> Event:
    return Event(id=f"evt-{seq:03d}", sequence=seq, kind=kind,
                 payload=payload, prev_hash="0" * 8, hash="h")


class _FakeRegistry:
    def __init__(self, ids):
        self.events = {eid: SimpleNamespace(event_id=eid) for eid in ids}


class TrendStatsUnitTest(unittest.TestCase):
    def _spaces(self):
        """三个事件：ev-a 双戳齐备(30min)；ev-b 缺签到戳；ev-c 快照不存在。"""
        ev_a = SimpleNamespace(events=[
            _evt(1, "FireEventStarted", event_id="ev-a", at=_iso(_T0)),
            _evt(2, "PersonCheckedIn", binding_id="rbs-001",
                 at=_iso(_T0 + timedelta(minutes=30))),
            _evt(3, "PersonCheckedIn", binding_id="rbs-002",
                 at=_iso(_T0 + timedelta(minutes=45))),
        ])
        ev_b = SimpleNamespace(events=[
            _evt(1, "FireEventStarted", event_id="ev-b", at=_iso(_T0)),
            # T15 前旧格式：无 at 键 → 排除
            _evt(2, "PersonCheckedIn", binding_id="rbs-003"),
        ])
        spaces = {"ev-a": ev_a, "ev-b": ev_b, "ev-c": None}
        registry = _FakeRegistry(["ev-a", "ev-b", "ev-c"])

        def loader(event_id):
            if event_id == "ev-c":
                raise RuntimeError("boom")
            return spaces[event_id]

        return registry, loader

    def test_arrival_avg_includes_only_double_stamped(self):
        registry, loader = self._spaces()
        metrics = {m.key: m for m in compute_trend_metrics(registry, None, loader)}
        m = metrics["avg_arrival_duration"]
        self.assertTrue(m.available)
        self.assertEqual("平均 30.0 分钟（n=1）", m.value)
        self.assertEqual(1, m.sample_count)
        self.assertEqual(2, m.excluded_count)
        details = " | ".join(s["detail"] for s in m.samples)
        self.assertIn("首名队员于启动后 30.0 分钟签到", details)
        self.assertIn("排除——快照加载失败：boom", details)
        self.assertIn("签到事件无时间戳", details)

    def test_anomaly_ratio_counts_across_chains(self):
        registry, loader = self._spaces()
        # 注入订单事实到 ev-a
        registry.events["ev-a"]  # noqa: B018 - 存在性自明
        spaces = {
            "ev-a": SimpleNamespace(events=[
                _evt(1, "OrderActivated"), _evt(2, "OrderActivated"),
                _evt(3, "OrderActivated"), _evt(4, "OrderAnomalyPaused"),
            ]),
            "ev-b": SimpleNamespace(events=[_evt(1, "OrderCancelled")]),
            "ev-c": None,
        }
        metrics = {m.key: m for m in compute_trend_metrics(
            registry, None, lambda eid: spaces[eid])}
        m = metrics["anomaly_order_ratio"]
        self.assertEqual("1/3（33.3%）", m.value)

    def test_shortfall_freq_over_ledger_snapshots(self):
        from panda_brain.materials import (EventMaterialsSnapshot, MaterialsLedger,
                                          SnapshotLine, SquadSnapshot)
        # 空台账：结构可用但无样本，不算「无数据源」
        empty = {m.key: m for m in compute_trend_metrics(
            _FakeRegistry([]), MaterialsLedger(), None)}
        self.assertTrue(empty["material_shortfall_freq"].available)
        self.assertIsNone(empty["material_shortfall_freq"].value)

        ledger = MaterialsLedger()
        ledger.snapshots = {
            "ev-a": EventMaterialsSnapshot(
                event_id="ev-a",
                squads=[SquadSnapshot(
                    squad_id="sq-1", arrived_phones=[],
                    lines=[SnapshotLine(item_id="mat-1", missing=2.0,
                                        reason="泵损")])],
                updated_at=_iso(_T0)),
            "ev-b": EventMaterialsSnapshot(
                event_id="ev-b",
                squads=[SquadSnapshot(
                    squad_id="sq-2", arrived_phones=[],
                    lines=[SnapshotLine(item_id="mat-2")])],
                updated_at=_iso(_T0)),
        }
        metrics = {m.key: m for m in compute_trend_metrics(
            _FakeRegistry([]), ledger, None)}
        m = metrics["material_shortfall_freq"]
        self.assertEqual("1/2（50.0%）", m.value)
        self.assertEqual(2, m.sample_count)
        self.assertIn("存在缺口", m.samples[0]["detail"])

    def test_unavailable_metrics_honest_and_total_nine(self):
        from panda_brain.materials import MaterialsLedger

        metrics = compute_trend_metrics(
            _FakeRegistry([]), MaterialsLedger(), None)
        self.assertEqual(9, len(metrics))
        unavailable = [m for m in metrics if not m.available]
        self.assertEqual(6, len(unavailable))
        for m in unavailable:
            self.assertIsNone(m.value)
            self.assertTrue(m.unavailable_reason, m.key)
            self.assertIn("暂无", m.missing_policy + m.method + m.time_bound)
        keys = {m.key for m in metrics}
        self.assertLess({"first_situation_duration", "command_ack_rate",
                         "ontime_task_rate", "feedback_missing_rate",
                         "comm_outage_duration",
                         "reignite_handling_duration"}, keys)

    def test_pure_readonly_deterministic(self):
        registry, loader = self._spaces()

        class _Probe:
            calls = 0

        def counting_loader(eid):
            _Probe.calls += 1
            return loader(eid)

        first = compute_trend_metrics(registry, None, counting_loader)
        n_after_first = _Probe.calls
        second = compute_trend_metrics(registry, None, counting_loader)
        self.assertEqual(n_after_first * 2, _Probe.calls)  # 每轮各扫一遍，无缓存副作用
        self.assertEqual(
            [(m.key, m.value) for m in first],
            [(m.key, m.value) for m in second])


class CheckinTimestampTest(unittest.TestCase):
    def test_scan_check_in_carries_at_key(self):
        ws = ScenarioWorkspace("t15-checkin")
        persons = [PersonRecord(name="张三", phone="13912340001",
                                unit="县防火办", home_position="供水组")]
        ws.import_person_library(persons)
        binding = ws.scan_check_in("log-water", phone="13912340001")
        self.assertEqual("active", binding.status)
        evt = [e for e in ws.events if e.kind == "PersonCheckedIn"][-1]
        self.assertIn("at", evt.payload)
        parsed = datetime.fromisoformat(evt.payload["at"])
        self.assertIsNotNone(parsed)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class ReviewTrendTabTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p16-trend", general_auth=self.auth),
            auth=self.auth,
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
            db_path=tmp / "ws.db",
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)
        form = urllib.parse.urlencode({"username": "admin",
                                       "secret": _SECRET}).encode()
        req = urllib.request.Request(f"{self.base}/login", data=form,
                                     method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            headers = dict(resp.headers.items())
            resp.read()
        except urllib.error.HTTPError as exc:
            headers = dict(exc.headers.items())
            exc.read()
        raw = ""
        for key, value in headers.items():
            if key.lower() == "set-cookie":
                raw = value
        self.cmd = raw.split(";")[0] if raw else ""
        self.assertTrue(self.cmd, "登录应发会话 Cookie")

    def _get(self, path: str):
        req = urllib.request.Request(f"{self.base}{path}")
        req.add_header("Cookie", self.cmd)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_commander_sees_trend_tab_with_open_metrics(self):
        before = len(self.state.workspace.events)
        st, body = self._get("/review?tab=trend")
        self.assertEqual(200, st)
        self.assertIn("趋势与统计口径", body)
        self.assertIn("异常任务占比", body)
        self.assertIn("队伍平均到场时长", body)
        self.assertIn("暂无数据源", body)          # 六项结构性无源如实交代
        self.assertIn("不做黑盒综合评分", body)
        self.assertIn("计算方法", body)
        self.assertIn("时间边界", body)
        self.assertEqual(before, len(self.state.workspace.events))  # 零写入

    def test_member_never_gets_trend_tab(self):
        html = render_review_page(self.state, role="member", viewer_word="成员",
                                  tab="trend")
        self.assertNotIn("趋势与统计口径", html)   # 强制回退，不渲染
        self.assertNotIn('href="/review?tab=trend"', html)  # 导航也不出现
        # 指挥员导航含该页签
        cmd_html = render_review_page(self.state, role="commander",
                                      viewer_word="指挥员", tab="summary")
        self.assertIn('href="/review?tab=trend"', cmd_html)


if __name__ == "__main__":
    unittest.main()
