"""P12-T3 scale correctness: the full chain must hold at thousands of events.

The P9 campaign drill peaked at v424. Nothing ever verified the workspace,
persistence, invariants, console, report, and panel rendering against a
workspace an order of magnitude larger. This suite builds one (>=5000 events),
round-trips it through SQLite, and asserts the structural guarantees survive.
Functional assertions only — no wall-clock bounds (sandbox timing is noisy).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.after_action_report import render_after_action_report
from panda_brain.console import build_console_text
from panda_brain.invariants import check_campaign_invariants
from panda_brain.models import Claim, ClaimStatus, Evidence
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace


def _timeline_count(ws: ScenarioWorkspace) -> int:
    return sum(1 for event in ws.events if event.kind in ScenarioWorkspace._TIMELINE_KINDS)


class P12ScaleTests(unittest.TestCase):
    N_PAIRS = 2500  # evidence+claim per pair -> >=5000 events

    def _build(self) -> ScenarioWorkspace:
        ws = ScenarioWorkspace("scale-drill")
        for i in range(self.N_PAIRS):
            ws.record_evidence(
                Evidence.create(
                    id=f"ev-scale-{i:05d}",
                    content=f"scale observation {i}: water level reading",
                    source="sensor-feed",
                )
            )
            ws.record_claim(
                Claim(
                    id=f"claim-scale-{i:05d}",
                    subject=f"water:site-{i:04d}",
                    value={"value": float(i) / 10, "unit": "m"},
                    status=ClaimStatus.OBSERVED,
                    source="sensor-feed",
                    confidence=0.9,
                    evidence_ids=(f"ev-scale-{i:05d}",),
                )
            )
        return ws

    def test_large_workspace_survives_roundtrip_with_all_guarantees(self) -> None:
        ws = self._build()
        self.assertGreaterEqual(len(ws.events), 2 * self.N_PAIRS)
        self.assertEqual(len(ws.claims), self.N_PAIRS)
        self.assertEqual(len(ws.evidence), self.N_PAIRS)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scale.db"
            save_workspace(ws, db_path)
            loaded = load_workspace(db_path)

        # Event chain survives byte-for-byte in order and content.
        self.assertEqual(len(loaded.events), len(ws.events))
        self.assertEqual(
            [(e.sequence, e.kind) for e in loaded.events],
            [(e.sequence, e.kind) for e in ws.events],
        )
        self.assertEqual(sorted(loaded.claims), sorted(ws.claims))
        self.assertEqual(sorted(loaded.evidence), sorted(ws.evidence))

        # Structural guarantees hold at scale. timeline() replays only the
        # decision/execution/verification kinds — assert against that count.
        self.assertEqual((), check_campaign_invariants(loaded))
        self.assertEqual(_timeline_count(loaded), len(loaded.timeline()))

    def test_console_and_report_render_at_scale_deterministically(self) -> None:
        ws = self._build()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scale.db"
            save_workspace(ws, db_path)
            loaded = load_workspace(db_path)

        text = build_console_text(loaded)
        self.assertIn("scale-drill", text)

        title = "规模演练战后报告"
        first = render_after_action_report(loaded, title=title)
        second = render_after_action_report(loaded, title=title)
        self.assertEqual(first, second)
        self.assertIn("## 五、收束态势", first)

    def test_panel_renders_overview_and_timeline_at_scale(self) -> None:
        from panda_brain.panel import PanelState, render_page

        ws = self._build()
        state = PanelState(ws, db_path=None)
        for route in ("/overview", "/timeline", "/brief", "/orders", "/questions", "/reviews"):
            status, body = render_page(state, route)
            self.assertEqual(200, status, route)
            self.assertIn("scale-drill", body)
        # Timeline page counts the whole chain and caps display at 80 rows.
        _, timeline_body = render_page(state, "/timeline")
        self.assertIn(f"共 {len(ws.events)} 条", timeline_body)
        self.assertIn("每页 80 条", timeline_body)
        self.assertIn("第 1 / ", timeline_body)


if __name__ == "__main__":
    unittest.main()
