"""P12-T4: panel after-action report export — read-only, renderer-parity."""

from __future__ import annotations

import threading
import unittest
import urllib.request

from panda_brain.after_action_report import render_after_action_report
from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import Claim, ClaimStatus, Evidence
from panda_brain.panel import (
    PanelState,
    export_report_markdown,
    make_server,
    render_page,
    report_title,
)
from panda_brain.workspace import ScenarioWorkspace


def _small_workspace(*, general_auth=None) -> ScenarioWorkspace:
    ws = ScenarioWorkspace("export-drill", general_auth=general_auth)
    ws.record_evidence(
        Evidence.create(
            id="ev-export-1",
            content="water level 2.8 m at site W1",
            source="sensor-feed",
        )
    )
    ws.record_claim(
        Claim(
            id="claim-export-1",
            subject="water:W1",
            value={"value": 2.8, "unit": "m"},
            status=ClaimStatus.OBSERVED,
            source="sensor-feed",
            confidence=0.9,
            evidence_ids=("ev-export-1",),
        )
    )
    return ws


class P12PanelExportTests(unittest.TestCase):
    def test_report_title_is_deterministic(self) -> None:
        ws = _small_workspace()
        self.assertEqual("export-drill 战后报告", report_title(ws))
        self.assertEqual(report_title(ws), report_title(ws))

    def test_export_matches_renderer_exactly(self) -> None:
        ws = _small_workspace()
        exported = export_report_markdown(ws)
        self.assertEqual(
            render_after_action_report(ws, title=report_title(ws)), exported
        )
        self.assertIn("## 五、收束态势", exported)

    def test_overview_links_export_route(self) -> None:
        state = PanelState(_small_workspace(), db_path=None)
        _, body = render_page(state, "/overview")
        self.assertIn('href="/export/report"', body)

    def test_export_http_roundtrip_readonly_with_attachment_header(self) -> None:
        auth = GeneralEntryAuth(secret='anonymous-export-probe')
        state = PanelState(_small_workspace(general_auth=auth), auth=auth, db_path=None)
        server = make_server(state, port=0)
        host, port = server.server_address[:2]
        try:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://{host}:{port}/export/report"
            token = state.login(state.auth.reveal_secret())
            request = urllib.request.Request(url, headers={'Cookie': 'panel_session='+token})
            with urllib.request.urlopen(request, timeout=5) as resp:
                status = resp.status
                raw = resp.read()
                headers = dict(resp.headers.items())
            body = raw.decode("utf-8")

            self.assertEqual(200, status)
            self.assertIn("text/markdown", headers.get("Content-Type", ""))
            disposition = headers.get("Content-Disposition", "")
            self.assertIn("attachment", disposition)
            self.assertIn("export-drill-report.md", disposition)

            # Renderer parity over HTTP.
            ws = state.workspace
            self.assertEqual(
                render_after_action_report(ws, title=report_title(ws)), body
            )

            # Export is read-only.
            version_after = state.workspace.version
            with urllib.request.urlopen(request, timeout=5) as resp:
                resp.read()
            self.assertEqual(version_after, state.workspace.version)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
