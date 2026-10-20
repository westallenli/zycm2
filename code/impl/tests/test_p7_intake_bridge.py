"""P7-T1 acceptance tests: intake bridge (ingestion → workspace facts)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.models import (
    ClaimStatus,
    GeneralQuestion,
    InformationGap,
    InformationGapStatus,
    SourceTrustLevel,
)
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace

READING = {
    "site_id": "sx-hanjiang-001",
    "metric": "water_level_m",
    "unit": "m",
    "value": 412.5,
    "observed_at": "2026-08-22T02:00:00+00:00",
}


def _make_store(*items: dict) -> ObservationStore:
    store = ObservationStore()
    report = store.import_json(list(items), source="test")
    assert report.all_passed, [item.errors for item in report.rejected]
    return store


class P7IntakeBridgeTests(unittest.TestCase):
    def test_bridged_observations_are_observed_claims_backed_by_evidence(self) -> None:
        store = _make_store(READING, dict(READING, site_id="sx-2", value=413.1))
        workspace = ScenarioWorkspace("intake-test")

        report = IntakeBridge(store).bridge_pending(workspace)

        self.assertEqual(
            ("sx-2:water_level_m", "sx-hanjiang-001:water_level_m"),
            report.bridged_subjects,
        )
        for subject in report.bridged_subjects:
            claim = workspace.claims[subject]
            self.assertEqual(ClaimStatus.OBSERVED, claim.status)
            self.assertEqual({SourceTrustLevel.PENDING_VERIFICATION}, {
                workspace.evidence[evidence_id].trust_level for evidence_id in claim.evidence_ids
            })
        self.assertEqual({"value": 412.5, "unit": "m"}, workspace.claims["sx-hanjiang-001:water_level_m"].value)
        self.assertEqual((), workspace.verify_audit_chain())

    def test_verified_gate_reuses_authorized_evidence_check(self) -> None:
        store = _make_store(READING)
        workspace = ScenarioWorkspace("gate-test")
        bridge = IntakeBridge(store)
        bridge.bridge_pending(workspace)

        subject = "sx-hanjiang-001:water_level_m"
        claim = workspace.claims[subject]
        with self.assertRaises(ValueError) as ctx:
            workspace.update_claim(
                subject,
                value=claim.value,
                status=ClaimStatus.VERIFIED,
                source="test",
                confidence=0.99,
                evidence_ids=claim.evidence_ids,
            )
        self.assertIn("授权来源", str(ctx.exception))

        # Authorized source configuration changes the trust level at the bridge,
        # and then the SAME workspace gate lets verification through.
        authorized_store = _make_store(dict(READING, value=412.6))
        authorized_bridge = IntakeBridge(authorized_store, authorized_sources=("test",))
        authorized_bridge.bridge_pending(workspace)
        verified = workspace.update_claim(
            subject,
            value={"value": 412.6, "unit": "m"},
            status=ClaimStatus.VERIFIED,
            source="test",
            confidence=0.99,
            evidence_ids=(IntakeBridge.evidence_id_for(authorized_store.observations()[0]),),
        )
        self.assertEqual(ClaimStatus.VERIFIED, verified.status)
        self.assertEqual(
            SourceTrustLevel.AUTHORIZED,
            workspace.evidence[verified.evidence_ids[0]].trust_level,
        )

    def test_gap_resolution_only_after_authorized_verification(self) -> None:
        store = _make_store(READING)
        workspace = ScenarioWorkspace("gap-test")
        gap = InformationGap(
            id="gap-water-1",
            subject="sx-hanjiang-001:water_level_m",
            reason="Water availability unknown.",
            discovered_by="LogisticsSupportAgent",
        )
        workspace.record_information_gap(gap)

        bridge = IntakeBridge(store)
        bridge.bridge_pending(workspace)
        self.assertEqual(InformationGapStatus.OPEN, workspace.information_gaps["gap-water-1"].status)

        subject = "sx-hanjiang-001:water_level_m"
        claim = workspace.claims[subject]
        with self.assertRaises(ValueError):
            workspace.update_claim(
                subject,
                value=claim.value,
                status=ClaimStatus.VERIFIED,
                source="test",
                confidence=0.99,
                evidence_ids=claim.evidence_ids,
            )
        self.assertEqual(InformationGapStatus.OPEN, workspace.information_gaps["gap-water-1"].status)

        auth_bridge = IntakeBridge(_make_store(dict(READING, value=412.7)), authorized_sources=("test",))
        auth_bridge.bridge_pending(workspace)
        fresh_record = next(r for r in auth_bridge.store.observations() if r.value == 412.7)
        workspace.update_claim(
            subject,
            value={"value": 412.7, "unit": "m"},
            status=ClaimStatus.VERIFIED,
            source="test",
            confidence=0.99,
            evidence_ids=(IntakeBridge.evidence_id_for(fresh_record),),
        )
        resolved = workspace.information_gaps["gap-water-1"]
        self.assertEqual(InformationGapStatus.RESOLVED, resolved.status)

    def test_reimport_and_rebridge_are_fully_idempotent(self) -> None:
        store = _make_store(READING)
        workspace = ScenarioWorkspace("idem-test")
        bridge = IntakeBridge(store)

        first = bridge.bridge_pending(workspace)
        version_after_first = workspace.version
        claims_after_first = len(workspace.claims)

        second = bridge.bridge_pending(workspace)
        self.assertEqual((), second.bridged_subjects)
        self.assertEqual((IntakeBridge.evidence_id_for(store.observations()[0]),), second.skipped_evidence_ids)
        self.assertEqual(version_after_first, workspace.version)
        self.assertEqual(claims_after_first, len(workspace.claims))

    def test_newer_reading_routes_through_update_path(self) -> None:
        store = _make_store(READING, dict(READING, value=413.0))
        workspace = ScenarioWorkspace("update-path-test")

        report = IntakeBridge(store).bridge_pending(workspace)

        self.assertEqual(("sx-hanjiang-001:water_level_m",), report.bridged_subjects)
        self.assertEqual(("sx-hanjiang-001:water_level_m",), report.updated_subjects)
        kinds = [event.kind for event in workspace.events]
        self.assertIn("ClaimChallenged", kinds)
        self.assertEqual({"value": 413.0, "unit": "m"}, workspace.claims["sx-hanjiang-001:water_level_m"].value)

    def test_bridge_idempotency_survives_persistence_roundtrip(self) -> None:
        store = _make_store(READING)
        workspace = ScenarioWorkspace("roundtrip-intake")
        bridge = IntakeBridge(store)
        bridge.bridge_pending(workspace)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            save_workspace(workspace, db_path, observation_store=store)
            restored = load_workspace(db_path)

        third = bridge.bridge_pending(restored)
        self.assertEqual((), third.bridged_subjects)
        self.assertEqual(len(workspace.events), len(restored.events))


if __name__ == "__main__":
    unittest.main()
