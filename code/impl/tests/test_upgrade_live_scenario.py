"""P10: out-of-order observations never replace newer or verified conclusions."""

import tempfile
import unittest
from pathlib import Path

from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.models import Claim, ClaimStatus, Evidence, SourceTrustLevel
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.scenario_driver import ScenarioDriver, load_threshold_rules
from panda_brain.workspace import ScenarioWorkspace, WorkspaceConflictError


READING = dict(site_id='sx-hanjiang-001', metric='water_level_m', unit='m',
               value=3.4, observed_at='2026-10-07T10:00:00+08:00')
SUBJECT = 'sx-hanjiang-001:water_level_m'


class LiveObservationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ScenarioWorkspace('anonymous-p10')
        self.store = ObservationStore()
        self.bridge = IntakeBridge(self.store, authorized_sources=('synthetic-sensor',))

    def ingest(self, **fields):
        self.store.import_json([dict(READING, **fields)], source='synthetic-sensor')
        return self.bridge.bridge_pending(self.workspace)

    def test_late_reading_keeps_newer_claim_and_retains_evidence(self):
        self.ingest()
        current = self.workspace.claims[SUBJECT]
        report = self.ingest(value=1.2, observed_at='2026-10-07T09:00:00+08:00')
        self.assertEqual(current, self.workspace.claims[SUBJECT])
        self.assertEqual(2, len(self.workspace.evidence))
        self.assertEqual((), report.updated_subjects)
        self.assertTrue(any('迟到' in note for note in report.notes))
        self.assertEqual((), self.workspace.verify_audit_chain())

    def test_sensor_does_not_replace_verified_claim_even_if_newer(self):
        self.ingest()
        current = self.workspace.claims[SUBJECT]
        verified = self.workspace.update_claim(SUBJECT, value=current.value,
            status=ClaimStatus.VERIFIED, source='human-check', confidence=0.99,
            evidence_ids=current.evidence_ids)
        report = self.ingest(value=1.2, observed_at='2026-10-07T11:00:00+08:00')
        self.assertEqual(verified, self.workspace.claims[SUBJECT])
        self.assertEqual(2, len(self.workspace.evidence))
        self.assertTrue(any('人工复核' in note for note in report.notes))
        checkpoint = self.workspace.version
        self.bridge.bridge_pending(self.workspace)
        self.assertEqual(checkpoint, self.workspace.version)

    def test_legacy_claim_without_traceable_clock_is_not_assumed_older(self):
        evidence = Evidence.create(id='legacy', content='clock unknown', source='legacy',
                                   trust_level=SourceTrustLevel.PENDING_VERIFICATION)
        self.workspace.record_evidence(evidence)
        current = Claim(id='legacy-claim', subject=SUBJECT, value={'value': 9, 'unit': 'm'},
                        status=ClaimStatus.OBSERVED, source='legacy', confidence=0.5,
                        evidence_ids=(evidence.id,))
        self.workspace.record_claim(current)
        report = self.ingest()
        self.assertEqual(current, self.workspace.claims[SUBJECT])
        self.assertTrue(any('时间' in note for note in report.notes))

    def test_persisted_clock_protects_against_late_batch_in_a_fresh_store(self):
        self.ingest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.db'
            save_workspace(self.workspace, path, observation_store=self.store)
            restored = load_workspace(path)
        late_store = ObservationStore()
        late_store.import_json([dict(READING, value=1.2,
            observed_at='2026-10-07T09:00:00+08:00')], source='synthetic-sensor')
        report = IntakeBridge(late_store).bridge_pending(restored)
        self.assertEqual(self.workspace.claims[SUBJECT], restored.claims[SUBJECT])
        self.assertTrue(report.notes)
        self.assertEqual((), restored.verify_audit_chain())

    def test_rejected_late_reading_cannot_reverse_threshold_claim(self):
        rules = load_threshold_rules(Path(__file__).resolve().parents[1] /
                                     'config/forest_fire_thresholds.yaml')
        driver = ScenarioDriver(self.workspace, self.store, self.bridge, rules)
        self.store.import_json([READING], source='synthetic-sensor')
        driver.step()
        current = self.workspace.claims['water:W1']
        late = ObservationStore()
        late.import_json([dict(READING, value=1.2,
            observed_at='2026-10-07T09:00:00+08:00')], source='synthetic-sensor')
        result = ScenarioDriver(self.workspace, late, IntakeBridge(late), rules).step()
        self.assertEqual(current, self.workspace.claims['water:W1'])
        self.assertEqual((), result['claims_updated'])
        self.assertTrue(result['notes'])
        self.assertEqual({}, self.workspace.orders)
        self.assertEqual({}, self.workspace.verifications)

    def test_newer_reading_updates_and_records_its_original_collection_clock(self):
        self.ingest()
        report = self.ingest(value=4.1, observed_at='2026-10-07T03:00:00+00:00')
        self.assertEqual((SUBJECT,), report.updated_subjects)
        self.assertEqual(4.1, self.workspace.claims[SUBJECT].value['value'])
        intake = [e for e in self.workspace.events if e.kind == 'ObservationIntakeRecorded']
        self.assertEqual('2026-10-07T11:00:00+08:00', intake[-1].payload['collected_at'])
        self.assertEqual('', intake[-1].payload['received_at'])

    def test_matching_timestamp_conflict_is_explicit_and_evidence_is_retained(self):
        self.ingest()
        report = self.ingest(value=4.1)
        self.assertEqual(2, len(self.workspace.evidence))
        self.assertTrue(any('同采集时间' in note for note in report.notes))
        self.assertEqual(ClaimStatus.OBSERVED, self.workspace.claims[SUBJECT].status)

    def test_expected_version_is_checked_before_any_write(self):
        self.ingest()
        self.store.import_json([dict(READING, value=4.1,
            observed_at='2026-10-07T11:00:00+08:00')], source='synthetic-sensor')
        before = self.workspace.version
        evidence = dict(self.workspace.evidence)
        with self.assertRaises(WorkspaceConflictError):
            self.bridge.bridge_pending(self.workspace, expected_version=before - 1)
        self.assertEqual(before, self.workspace.version)
        self.assertEqual(evidence, self.workspace.evidence)
        self.bridge.bridge_pending(self.workspace, expected_version=before)
        self.assertEqual(4.1, self.workspace.claims[SUBJECT].value['value'])

    def test_tampered_collection_clock_is_rejected_before_any_write(self):
        self.ingest()
        intake = next(e for e in self.workspace.events if e.kind == 'ObservationIntakeRecorded')
        intake.payload['collected_at'] = '2026-10-07T08:00:00+08:00'
        self.store.import_json([dict(READING, value=1.2,
            observed_at='2026-10-07T09:00:00+08:00')], source='synthetic-sensor')
        before = self.workspace.version
        with self.assertRaises(ValueError):
            self.bridge.bridge_pending(self.workspace)
        self.assertEqual(before, self.workspace.version)


if __name__ == '__main__':
    unittest.main()
