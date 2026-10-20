"""P13-T3: timeline pagination — pages, filters compose, out-of-range safe."""

from __future__ import annotations

import unittest

from panda_brain.models import Claim, ClaimStatus, Evidence
from panda_brain.panel import PanelState, render_page
from panda_brain.workspace import ScenarioWorkspace

PAGE_SIZE = 80  # keep in lockstep with panel._TIMELINE_PAGE_SIZE


def _bulk_workspace(pairs: int) -> PanelState:
    ws = ScenarioWorkspace("pager-drill")
    for i in range(pairs):
        ws.record_evidence(
            Evidence.create(
                id=f"ev-pg-{i:05d}",
                content=f"pager observation {i}",
                source="sensor-feed",
            )
        )
        ws.record_claim(
            Claim(
                id=f"claim-pg-{i:05d}",
                subject=f"water:pg-{i:04d}",
                value={"value": float(i), "unit": "m"},
                status=ClaimStatus.OBSERVED,
                source="sensor-feed",
                confidence=0.9,
                evidence_ids=(f"ev-pg-{i:05d}",),
            )
        )
    return PanelState(ws, db_path=None)


class P13TimelinePagerTests(unittest.TestCase):
    def test_default_page_shows_newest_with_pager_note(self) -> None:
        state = _bulk_workspace(250)  # 500 events + 1 bootstrap
        total = len(state.workspace.events)
        expected_pages = -(-total // PAGE_SIZE)
        status, body = render_page(state, "/timeline", session_token=None)
        self.assertEqual(200, status)
        self.assertIn(f"共 {total} 条", body)
        self.assertIn(f"第 1 / {expected_pages} 页 · 每页 {PAGE_SIZE} 条", body)
        self.assertIn("下一页", body)
        self.assertNotIn("上一页", body)
        # Newest sequence first on page one.
        self.assertIn(f"{total:04d}", body)
        self.assertNotIn(f"{total - PAGE_SIZE:04d}", body)

    def test_last_page_shows_oldest_without_next(self) -> None:
        state = _bulk_workspace(250)
        total = len(state.workspace.events)
        last_page = -(-total // PAGE_SIZE) - 1
        _, body = render_page(
            state, "/timeline", session_token=None, query={"page": [str(last_page)]}
        )
        self.assertIn(f"第 {last_page + 1} / {last_page + 1} 页", body)
        self.assertIn("上一页", body)
        self.assertNotIn("下一页", body)
        self.assertIn("0001", body)  # oldest event visible

    def test_out_of_range_page_renders_empty_gracefully(self) -> None:
        state = _bulk_workspace(250)
        status, body = render_page(
            state, "/timeline", session_token=None, query={"page": ["999"]}
        )
        self.assertEqual(200, status)
        self.assertIn("共 ", body)
        # No row markers beyond total.
        self.assertNotIn(f"{len(state.workspace.events) + 1:04d}", body)

    def test_kind_filter_composes_with_page_param(self) -> None:
        state = _bulk_workspace(250)
        evidence_events = sum(
            1 for e in state.workspace.events if e.kind == "EvidenceRecorded"
        )
        pages_for_kind = -(-evidence_events // PAGE_SIZE)
        _, body = render_page(
            state,
            "/timeline",
            session_token=None,
            query={"kind": ["EvidenceRecorded"], "page": ["1"]},
        )
        self.assertIn(f"过滤 EvidenceRecorded 后剩 {evidence_events} 条", body)
        self.assertIn(f"第 2 / {pages_for_kind} 页", body)
        # Nav links must carry both kind and page.
        self.assertIn("kind=EvidenceRecorded&amp;page=0", body)
        self.assertIn("kind=EvidenceRecorded&amp;page=2", body)
        # Only the filtered kind's rows appear.
        self.assertNotIn(">ClaimCreated</span>", body)

    def test_garbage_page_param_falls_back_to_first_page(self) -> None:
        state = _bulk_workspace(250)
        status, body = render_page(
            state, "/timeline", session_token=None, query={"page": ["abc"]}
        )
        self.assertEqual(200, status)
        self.assertIn("第 1 / ", body)


if __name__ == "__main__":
    unittest.main()
