"""P6-T1 acceptance tests: real authentication for the general entry (doc 11 §3.5 item 4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.auth import GeneralCredentials, GeneralEntryAuth
from panda_brain.models import DecisionActor, DecisionType, GENERAL_ENTRY_ID
from panda_brain.workspace import ScenarioWorkspace


class P6AuthenticationTests(unittest.TestCase):
    def _bound_workspace(self) -> tuple[ScenarioWorkspace, GeneralEntryAuth]:
        auth = GeneralEntryAuth(secret="unit-test-master-secret")
        return ScenarioWorkspace("auth-test", general_auth=auth), auth

    def _decision(self, workspace: ScenarioWorkspace):
        """Probe call: a minimal general act through record_decision."""
        return workspace.record_decision(
            DecisionType.DEFER,
            None,
            "probe",
            "test",
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
        )

    def test_bound_workspace_starts_locked(self) -> None:
        workspace, _ = self._bound_workspace()
        self.assertFalse(workspace._general_entry_unlocked)
        with self.assertRaises(PermissionError):
            self._decision(workspace)

    def test_wrong_secret_rejected_and_workspace_stays_locked(self) -> None:
        workspace, auth = self._bound_workspace()
        with self.assertRaises(PermissionError):
            auth.authenticate("wrong-secret")
        with self.assertRaises(PermissionError):
            self._decision(workspace)
        self.assertFalse(workspace._general_entry_unlocked)

    def test_correct_secret_unlocks_full_command_flow(self) -> None:
        workspace, auth = self._bound_workspace()
        credentials = auth.authenticate("unit-test-master-secret")
        workspace.unlock_general_entry(credentials)
        decision = self._decision(workspace)
        self.assertEqual(GENERAL_ENTRY_ID, decision.decided_by)

    def test_forged_credentials_rejected(self) -> None:
        workspace, _ = self._bound_workspace()
        forged = GeneralCredentials(entry_id=GENERAL_ENTRY_ID, secret_fingerprint="0" * 64)
        with self.assertRaises(PermissionError):
            workspace.unlock_general_entry(forged)
        with self.assertRaises(PermissionError):
            workspace.unlock_general_entry("local-general-entry")
        self.assertFalse(workspace._general_entry_unlocked)

    def test_unbound_workspace_opens_ephemeral_harness_entry(self) -> None:
        """First-version premise: single user, local run. Documented behavior."""
        workspace = ScenarioWorkspace("harness")
        self.assertTrue(workspace._general_entry_unlocked)
        self._decision(workspace)  # must not raise

    def test_file_backed_secret_persists_and_authenticates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "runtime" / "general_entry.secret"
            first = GeneralEntryAuth(secret_path)
            generated = first.reveal_secret()

            second = GeneralEntryAuth(secret_path)
            self.assertEqual(generated, second.reveal_secret())
            credentials = second.authenticate(generated)
            self.assertEqual(GENERAL_ENTRY_ID, credentials.entry_id)


if __name__ == "__main__":
    unittest.main()
