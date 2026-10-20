"""P9-T2 acceptance tests: campaign runner drives the whole chain."""

from __future__ import annotations

import unittest

from panda_brain.campaign_runner import run_campaign
from panda_brain.models import (
    AttemptStatus,
    ClaimStatus,
    DecisionType,
    OrderStatus,
    RiskLevel,
)


class P9CampaignRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_campaign()
        cls.workspace = cls.result.workspace
        assert cls.workspace is not None

    def test_campaign_completes_all_waves(self) -> None:
        self.assertGreaterEqual(self.result.waves_executed, 6)
        self.assertTrue(
            any(d.decision_type == DecisionType.ADOPT for d in self.workspace.decisions.values())
        )
        self.assertGreaterEqual(len(self.result.order_ids), 3)

    def test_l3_order_required_and_got_an_approval(self) -> None:
        l3_orders = [
            order.id
            for order in self.workspace.orders.values()
            if order.status == OrderStatus.ACTIVE or True
            if any(
                c.order_id == order.id and c.risk_level >= RiskLevel.L3
                for c in self.workspace.action_contracts.values()
            )
        ]
        self.assertTrue(l3_orders, "the approach action must run at L3")
        for order_id in l3_orders:
            approvals = [
                a for a in self.workspace.approvals.values() if a.order_id == order_id
            ]
            self.assertTrue(approvals, f"L3 order {order_id} must carry an approval")

    def test_anomaly_interlock_fired_and_recovered(self) -> None:
        statuses = [status for _, status in (a.split(":", 1) for a in self.result.anomalies)]
        self.assertIn(AttemptStatus.TIMEOUT.value, statuses)
        resumed = [
            e for e in self.workspace.events if e.kind == "OrderResumed"
        ]
        self.assertTrue(resumed, "timeout must be followed by an approved resume")
        final_reports = [
            r
            for r in self.workspace.reports.values()
            if r.status == AttemptStatus.SUCCEEDED and r.order_id.startswith("order-")
        ]
        self.assertTrue(final_reports)

    def test_sensors_never_verified_but_recon_did(self) -> None:
        water = self.workspace.claims["water:W1"]
        route = self.workspace.claims["route:R2"]
        fire = self.workspace.claims["fire:trend"]
        for claim in (water, route, fire):
            self.assertEqual(ClaimStatus.VERIFIED, claim.status)
            self.assertEqual("recon-alpha", claim.source)
        # No bridged sensor evidence ever backs a VERIFIED claim.
        from panda_brain.models import SourceTrustLevel

        verified_evidence_ids = {
            eid
            for claim in self.workspace.claims.values()
            if claim.status == ClaimStatus.VERIFIED
            for eid in claim.evidence_ids
        }
        for eid in verified_evidence_ids:
            evidence = self.workspace.evidence[eid]
            self.assertEqual(SourceTrustLevel.AUTHORIZED, evidence.trust_level)
            self.assertNotEqual("sensor-feed", evidence.source)

    def test_final_verification_passed_after_fire_controlled(self) -> None:
        self.assertIs(True, self.result.verification_passed)

    def test_memory_confirmed_and_injected_into_day2_briefs(self) -> None:
        self.assertGreaterEqual(self.result.memory_confirmed, 1)
        self.assertTrue(
            self.result.experience_injection_seen,
            "day-2 briefs must inject confirmed experiences",
        )

    def test_optional_enum_fields_survive_round_trip(self) -> None:
        """Regression: Optional[Enum] fields decoded as bare str (P9-T2 find)."""
        from panda_brain.models import AdvisorRole, Assessment, Evidence, SourceTrustLevel
        from panda_brain.persistence import decode_entity, encode_entity

        assessment = Assessment(
            id="a-1",
            agent="ResearchAgent",
            summary="s",
            evidence_ids=(),
            advisor_role=AdvisorRole.RESEARCH,
        )
        restored = decode_entity(encode_entity(assessment))
        self.assertIs(AdvisorRole.RESEARCH, restored.advisor_role)

        evidence = Evidence.create("e-1", "content", "src", SourceTrustLevel.AUTHORIZED)
        restored_evidence = decode_entity(encode_entity(evidence))
        self.assertIs(SourceTrustLevel.AUTHORIZED, restored_evidence.trust_level)

    def test_reload_checkpoint_kept_audit_chain_intact(self) -> None:
        self.assertIsNotNone(self.result.reload_checkpoint_version)
        self.assertIs(True, self.result.reload_audit_intact)
        self.assertFalse(self.workspace.verify_audit_chain())

    def test_quality_feedback_recorded(self) -> None:
        self.assertTrue(
            self.result.quality_feedback_kinds,
            "the drill should surface at least one orchestration-quality signal",
        )


if __name__ == "__main__":
    unittest.main()
