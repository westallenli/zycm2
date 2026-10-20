"""P9-T4 acceptance tests: integration invariants + mutation self-checks.

The green sweep proves a full campaign violates nothing. The mutation tests
then corrupt copies of that good workspace and require the matching checker
to catch exactly the injected break — an anti-always-green guarantee.
"""

from __future__ import annotations
import copy
import json
import unittest

from panda_brain.campaign_runner import run_campaign
from panda_brain.invariants import check_campaign_invariants, check_l3_approvals_before_activation
from panda_brain.invariants import (
    check_audit_chain,
    check_memory_purity,
    check_sensor_never_verifies,
)
from panda_brain.models import ClaimStatus


def _event_digest(workspace) -> str:
    return json.dumps(
        [[e.sequence, e.kind, e.hash] for e in workspace.events], sort_keys=True
    )


class P9InvariantSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_campaign()
        cls.workspace = cls.result.workspace
        assert cls.workspace is not None

    def test_good_campaign_violates_nothing(self) -> None:
        violations = check_campaign_invariants(self.workspace)
        self.assertEqual((), violations, f"violations: {violations}")

    def test_two_full_runs_are_event_identical(self) -> None:
        other = run_campaign()
        assert other.workspace is not None
        self.assertEqual(_event_digest(self.workspace), _event_digest(other.workspace))
        self.assertEqual(
            sorted((s, c.value, c.status.value) for s, c in self.workspace.claims.items()),
            sorted((s, c.value, c.status.value) for s, c in other.workspace.claims.items()),
        )


class P9MutationSelfCheckTests(unittest.TestCase):
    """Corrupt a good workspace; each checker must name its own break."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_campaign()
        cls.workspace = cls.result.workspace
        assert cls.workspace is not None

    def _copy_events(self):
        import pickle

        return pickle.loads(pickle.dumps(self.workspace._events))

    def test_tampered_event_row_is_located(self) -> None:
        tampered = ScenarioWorkspaceStub(self.workspace)
        events = self._copy_events()
        target = events[len(events) // 2]
        object.__setattr__(target, "payload", dict(target.payload))
        target.payload["tampered"] = True
        tampered._events = events
        violations = check_audit_chain(tampered)
        self.assertEqual(1, len(violations))
        self.assertIn(f"sequence {target.sequence}", violations[0].detail)

    def test_sensor_backed_verified_claim_is_caught_by_subject(self) -> None:
        probe = ScenarioWorkspaceStub(self.workspace)
        claims = dict(probe.claims)
        from panda_brain.models import Claim

        sensor_claim = Claim(
            id="claim-mutation",
            subject="water:W1",
            value="available",
            status=ClaimStatus.VERIFIED,
            source="sensor-feed",
            confidence=1.0,
            evidence_ids=("evidence-base-wind",),  # non-authorized baseline evidence
        )
        claims["water:W1"] = sensor_claim
        probe.claims = claims
        violations = check_sensor_never_verifies(probe)
        self.assertTrue(any("water:W1" in v.detail for v in violations))

    def test_missing_l3_approval_is_caught(self) -> None:
        probe = ScenarioWorkspaceStub(self.workspace)
        # Drop approval events so the L3 order looks unapproved.
        probe._events = tuple(
            e for e in probe.events if e.kind != "ApprovalRecorded"
        )
        violations = check_l3_approvals_before_activation(probe)
        self.assertTrue(violations)
        self.assertTrue(all(v.kind == "l3_approval" for v in violations))

    def test_mutation_inside_memory_window_is_caught(self) -> None:
        probe = ScenarioWorkspaceStub(self.workspace)
        proposed = sorted(
            e.sequence for e in probe.events if e.kind == "MemoryCandidateProposed"
        )
        self.assertGreaterEqual(len(proposed), 2)
        # Corrupt the second proposal in place: its slot now reads as a command
        # event, so the first propose->confirm window contains a mutable event.
        victim = proposed[1]
        mutated = tuple(
            _FakeEvent(e.sequence, "GeneralDecisionRecorded", payload={})
            if e.sequence == victim
            else e
            for e in probe.events
        )
        probe._events = mutated
        violations = check_memory_purity(probe)
        self.assertTrue(any(v.kind == "memory_purity" for v in violations))
        self.assertTrue(all(f"seq {victim}" in v.detail for v in violations))


# ---------------------------------------------------------------------------
# Lightweight workspace stand-ins for mutation testing: share real state via
# references without mutating the shared good workspace.

from panda_brain.workspace import ScenarioWorkspace  # noqa: E402


class ScenarioWorkspaceStub:
    def __init__(self, source: ScenarioWorkspace) -> None:
        self.scenario_id = source.scenario_id
        self.version = source.version
        self.claims = source.claims
        self.evidence = source.evidence
        self.action_contracts = source.action_contracts
        self.orders = source.orders
        self.approvals = source.approvals
        self._events = source._events

    @property
    def events(self):
        return tuple(self._events)

    def verify_audit_chain(self) -> tuple[int, ...]:
        return ScenarioWorkspace.verify_audit_chain(self)


class _FakeEvent:
    def __init__(self, sequence, kind, payload=None) -> None:
        self.sequence = sequence
        self.kind = kind
        self.payload = payload if payload is not None else {}


if __name__ == "__main__":
    unittest.main()
