"""P10-T4 acceptance tests: audit timeline view, invariant health badge,
and the full HTTP journey."""

from __future__ import annotations

import pickle
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain.panel import PanelState, load_state, make_server, render_page
from panda_brain.persistence import load_workspace, save_workspace


def _db_state(tmp: str, secret: str = "panel-secret-1") -> PanelState:
    demo = load_state()
    db_path = Path(tmp) / "panel.db"
    save_workspace(demo.workspace, db_path)
    auth = __import__("panda_brain.auth", fromlist=["GeneralEntryAuth"]).GeneralEntryAuth(secret=secret)
    workspace = load_workspace(db_path, general_auth=auth)
    return PanelState(workspace, db_path=db_path, auth=auth)


class P10TimelineViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = load_state()
        assert cls.state.workspace is not None

    def test_timeline_renders_latest_first_with_chain_status(self) -> None:
        status, page = render_page(self.state, "/timeline")
        self.assertEqual(200, status)
        ws = self.state.workspace
        last_seq = max(e.sequence for e in ws.events)
        self.assertIn(f"{last_seq:04d}", page)
        self.assertIn("按时间倒序", page)
        self.assertIn(f"共 {len(ws.events)} 条", page)
        # First event row shown must be the highest sequence (reversed order).
        head = page[page.index("事件链"):]
        self.assertIn("GeneralQuestionAnswered", head)

    def test_kind_filter_narrows_rows(self) -> None:
        _, full_page = render_page(self.state, "/timeline")
        _, filtered = render_page(
            self.state, "/timeline", query={"kind": ["GeneralDecisionRecorded"]}
        )
        self.assertIn("过滤 GeneralDecisionRecorded", filtered)
        self.assertIn("GeneralDecisionRecorded</span>", filtered)
        # The filter link list mentions every kind, but no event ROW may show
        # a non-matching kind.
        self.assertNotIn('>MemoryCandidateProposed</span>', filtered)
        self.assertIn('>MemoryCandidateProposed</span>', full_page)

    def test_timeline_is_read_only_and_in_nav(self) -> None:
        version_before = self.state.workspace.version
        _, page = render_page(self.state, "/timeline")
        self.assertEqual(version_before, self.state.workspace.version)
        self.assertIn("时间线审计", page)

    def test_overview_shows_invariant_health_badge(self) -> None:
        _, page = render_page(self.state, "/overview")
        self.assertIn("集成不变量", page)
        self.assertIn("全部通过", page)

    def test_broken_chain_is_highlighted(self) -> None:
        # Deep-copy the events, tamper one payload, and render through a stub.
        events = pickle.loads(pickle.dumps(self.state.workspace._events))
        victim = events[len(events) // 2]
        object.__setattr__(victim, "payload", dict(victim.payload))
        victim.payload["tampered"] = True

        stub = PanelState(self.state.workspace, auth=self.state.auth)
        stub.workspace = type(
            "W",
            (),
            {
                "scenario_id": self.state.workspace.scenario_id,
                "version": self.state.workspace.version,
                "claims": self.state.workspace.claims,
                "decision_briefs": self.state.workspace.decision_briefs,
                "orders": self.state.workspace.orders,
                "action_contracts": self.state.workspace.action_contracts,
                "approvals": self.state.workspace.approvals,
                "general_questions": self.state.workspace.general_questions,
                "reviews": self.state.workspace.reviews,
                "memory_candidates": self.state.workspace.memory_candidates,
                "events": tuple(events),
                "_events": events,
                "_general_entry_unlocked": True,
                "verify_audit_chain": lambda self_: tuple(
                    e.sequence for e in events if e.sequence == victim.sequence
                ),
            },
        )()
        _, page = render_page(stub, "/timeline")
        self.assertIn(f"断于 {victim.sequence}", page)
        self.assertIn("color:#f85149", page)


class P10FullJourneyTests(unittest.TestCase):
    """The acceptance journey: browse → authenticate → act → audit visible."""

    def test_browse_login_act_audit_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)
            version_before = state.workspace.version

            from panda_brain.models import GeneralQuestion

            state.workspace.unlock_general_entry(
                state.auth.authenticate("panel-secret-1")
            )
            question = state.workspace.raise_general_question(
                GeneralQuestion(
                    id="gq-journey",
                    kind="insufficient_evidence",
                    subject="journey-option",
                    question="全流程验收：下一步如何处置未核实条件？",
                    context=(),
                    source_version=state.workspace.version,
                )
            )
            # Lock it again so the journey must authenticate through HTTP.
            state.workspace._general_entry_unlocked = False
            state.sessions.clear()

            server = make_server(state, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            try:
                base = f"http://127.0.0.1:{port}"

                # 1. Browsing works while locked, no forms.
                with urllib.request.urlopen(f"{base}/questions", timeout=5) as resp:
                    locked_page = resp.read().decode("utf-8")
                self.assertNotIn("<form", locked_page)

                # 2. Authenticate.
                data = urllib.parse.urlencode({"secret": "panel-secret-1"}).encode()
                req = urllib.request.Request(f"{base}/login", data=data, method="POST")
                try:
                    opener.open(req, timeout=5)
                    self.fail("expected redirect")
                except urllib.error.HTTPError as err:
                    self.assertEqual(303, err.code)
                    cookie = err.headers.get("Set-Cookie", "").split(";", 1)[0]

                # 3. Act: answer the open question.
                data = urllib.parse.urlencode(
                    {"question_id": question.id, "answer": "先核实，后投入。"}
                ).encode()
                req = urllib.request.Request(
                    f"{base}/answer",
                    data=data,
                    method="POST",
                    headers={"Cookie": cookie},
                )
                try:
                    opener.open(req, timeout=5)
                    self.fail("expected redirect")
                except urllib.error.HTTPError as err:
                    self.assertEqual(303, err.code)

                # 4. Audit trail shows the act; version advanced; snapshot persisted.
                req = urllib.request.Request(
                    f"{base}/timeline", headers={"Cookie": cookie}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    timeline = resp.read().decode("utf-8")
                self.assertIn("GeneralQuestionAnswered", timeline)
                self.assertIn("QuestionAnswerApplied", timeline)
                self.assertGreater(state.workspace.version, version_before)

                from panda_brain.persistence import load_workspace

                reloaded = load_workspace(
                    state.db_path,
                    general_auth=__import__(
                        "panda_brain.auth", fromlist=["GeneralEntryAuth"]
                    ).GeneralEntryAuth(secret="panel-secret-1"),
                )
                self.assertEqual(
                    "answered", reloaded.general_questions[question.id].status
                )
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
