"""P10-T3 acceptance tests: command acts — answer routing, memory handling,
snapshot write-back, and the HTTP end-to-end flow."""

from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.models import (
    AdvisorRole,
    AttemptStatus,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    MemoryCandidate,
    RiskLevel,
)
from panda_brain.panel import PanelState, load_state, make_server
from panda_brain.persistence import load_workspace, save_workspace


def _db_state(tmp: str) -> PanelState:
    demo = load_state()
    db_path = Path(tmp) / "panel.db"
    save_workspace(demo.workspace, db_path)
    auth = GeneralEntryAuth(secret="panel-secret-1")
    workspace = load_workspace(db_path, general_auth=auth)
    return PanelState(workspace, db_path=db_path, auth=auth)


def _login(state: PanelState) -> str:
    token = state.login("panel-secret-1")
    assert token is not None
    return token


def _raise_probe_question(state: PanelState) -> str:
    question = state.workspace.raise_general_question(
        GeneralQuestion(
            id="gq-panel-answer-me",
            kind="insufficient_evidence",
            subject="probe-option",
            question="面板动作验收探针：条件未核实怎么办？",
            context=(),
            source_version=state.workspace.version,
        )
    )
    return question.id


class P10CommandActTests(unittest.TestCase):
    def test_answer_routes_targeted_restaffing_and_writes_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)
            _login(state)
            question_id = _raise_probe_question(state)
            version_before = state.workspace.version

            result = state.handle_answer(question_id, "先核实条件再谈投入。")

            self.assertEqual(question_id, result["question_id"])
            self.assertIsNotNone(result.get("plan_id"))
            self.assertIsNotNone(result.get("assessment_id"))
            self.assertGreater(state.workspace.version, version_before)
            self.assertEqual(
                "answered", state.workspace.general_questions[question_id].status
            )
            kinds = [e.kind for e in state.workspace.events]
            self.assertIn("GeneralQuestionAnswered", kinds)
            self.assertIn("QuestionAnswerApplied", kinds)

            # Snapshot write-back: a fresh load sees the answered question.
            reloaded = load_workspace(
                state.db_path, general_auth=GeneralEntryAuth(secret="panel-secret-1")
            )
            self.assertEqual(
                "answered", reloaded.general_questions[question_id].status
            )

    def test_answer_anomaly_resumes_paused_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)
            _login(state)
            ws = state.workspace
            option = ws.options["verify-conditions"]
            decision = ws.record_decision(
                DecisionType.ADOPT,
                option.id,
                reason="panel anomaly drill",
                scope="panel-test",
                actor_role=DecisionActor.COMMANDER,
                decided_by=GENERAL_ENTRY_ID,
                accepted_risks=tuple(option.risks),
            )
            order = ws.draft_order(
                decision.id,
                drafted_by="DispatchChiefAgent",
                drafter_role=AdvisorRole.CHIEF_ADVISOR,
            )
            ws.draft_action_contract(
                order.id,
                drafted_by="DispatchChiefAgent",
                drafter_role=AdvisorRole.CHIEF_ADVISOR,
                action_type=f"simulated:{order.option_id}",
                risk_level=RiskLevel.L2,
                idempotency_key=f"{order.id}:panel",
            )
            ws.submit_order_for_confirmation(order.id)
            ws.confirm_order(
                order.id,
                actor_role=DecisionActor.COMMANDER,
                confirmed_by=GENERAL_ENTRY_ID,
            )
            ws.activate_order(order.id)
            report = ws.record_execution(
                order.id,
                AttemptStatus.TIMEOUT,
                ("comms lost",),
                idempotency_key=f"{order.id}:panel:a1",
            )
            outcome = ws_chief_handle_anomaly(state, order.id, report)
            question_id = outcome["question_id"]
            self.assertEqual("paused", ws.orders[order.id].status.value)

            state.handle_answer(question_id, "复核完毕，批准恢复。")

            self.assertEqual("active", ws.orders[order.id].status.value)
            self.assertEqual("answered", ws.general_questions[question_id].status)

    def test_memory_confirm_and_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)
            _login(state)
            review = state.chief.conduct_review(state.workspace, scope="panel-probe")
            proposed_ids = sorted(
                c.id
                for c in state.workspace.memory_candidates.values()
                if c.source_review_id == review.id
            )
            self.assertGreaterEqual(len(proposed_ids), 2)
            confirmed = state.handle_memory(proposed_ids[0], "confirm")
            rejected = state.handle_memory(proposed_ids[1], "reject")
            self.assertEqual("confirmed", confirmed["status"])
            self.assertEqual("rejected", rejected["status"])
            kinds = [e.kind for e in state.workspace.events]
            self.assertIn("MemoryCandidateConfirmed", kinds)

    def test_actions_refused_when_locked_or_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)  # locked, no login
            with self.assertRaises(PermissionError):
                state.handle_answer("gq-x", "text")
            _login(state)
            question_id = _raise_probe_question(state)
            with self.assertRaises(PanelActionErrorShim):
                state.handle_answer(question_id, "   ")
            with self.assertRaises(PanelActionErrorShim):
                state.handle_memory("mem-nonexistent", "confirm")

    def test_http_answer_flow_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _db_state(tmp)
            token = _login(state)
            question_id = _raise_probe_question(state)
            server = make_server(state, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{port}"
                data = urllib.parse.urlencode(
                    {"question_id": question_id, "answer": "x"}
                ).encode()

                # Without a session cookie the action is refused.
                req = urllib.request.Request(f"{base}/answer", data=data, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=5)
                    self.fail("expected 403")
                except urllib.error.HTTPError as err:
                    self.assertEqual(403, err.code)

                # With the session cookie the action lands on the audit trail.
                req = urllib.request.Request(
                    f"{base}/answer",
                    data=data,
                    method="POST",
                    headers={"Cookie": f"panel_session={token}"},
                )

                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None

                opener = urllib.request.build_opener(NoRedirect)
                try:
                    opener.open(req, timeout=5)
                    self.fail("expected redirect")
                except urllib.error.HTTPError as err:
                    self.assertEqual(303, err.code)
                    self.assertTrue(err.headers.get("Location", "").endswith("/questions"))
                self.assertEqual(
                    "answered", state.workspace.general_questions[question_id].status
                )
            finally:
                server.shutdown()
                server.server_close()


def ws_chief_handle_anomaly(state: PanelState, order_id: str, report):
    return state.chief.handle_execution_report(state.workspace, order_id, report)


# Import alias so the refusal assertions read naturally without polluting the
# module namespace used by other suites.
from panda_brain.panel import PanelActionError as PanelActionErrorShim  # noqa: E402


if __name__ == "__main__":
    unittest.main()
