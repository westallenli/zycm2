"""P10-02 source contracts use anonymous fixtures, with no device/network access."""

import json
import tempfile
import unittest
from pathlib import Path

from panda_brain.ingestion import ObservationRecord, ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.models import ClaimStatus, SourceTrustLevel
from panda_brain.persistence import decode_entity, load_workspace, save_workspace
from panda_brain.scenario_driver import ScenarioDriver, ThresholdRule
from panda_brain.workspace import ScenarioWorkspace


EVENT = 'anonymous-event-a'
ROW = dict(event_id=EVENT, source_record_id='reading-1', source_version=1,
           site_id='anonymous-site', metric='water_level_m', value=3.4, unit='m',
           observed_at='2026-10-07T02:00:00+00:00',
           received_at='2026-10-07T10:01:00+08:00', quality_note='模拟传感器，无实测精度结论',
           coordinate_system='unknown')
SUBJECT = 'anonymous-site:water_level_m'
RULE = ThresholdRule('water:anonymous-condition', 'water_level_m', None,
                     '>=', 3, 'met', 'not_met')


class SourceObservationTests(unittest.TestCase):
    def setUp(self):
        self.store = ObservationStore()
        self.workspace = ScenarioWorkspace('anonymous', event_id=EVENT)
        self.bridge = IntakeBridge(self.store, authorized_sources=('adapter',))
        self.driver = ScenarioDriver(self.workspace, self.store, self.bridge, (RULE,))

    def ingest(self, kind='simulated', **changes):
        return self.store.import_source_json([dict(ROW, **changes)], event_id=EVENT,
                                             source='adapter', data_kind=kind)

    def test_provenance_and_original_clocks_reach_evidence_audit_and_conditions(self):
        record = self.ingest().accepted[0]
        self.assertEqual('2026-10-07T10:00:00', record.observed_at)
        self.assertEqual(ROW['observed_at'], record.collected_at)
        self.assertEqual('pending_verification', record.verification_status)
        self.driver.step()
        for subject in (SUBJECT, RULE.subject):
            claim = self.workspace.claims[subject]
            self.assertEqual(ClaimStatus.OBSERVED, claim.status)
            self.assertIn('模拟', claim.source)
        evidence = self.workspace.evidence[self.bridge.evidence_id_for(record)]
        self.assertEqual(SourceTrustLevel.PENDING_VERIFICATION, evidence.trust_level)
        self.assertIn(ROW['quality_note'], evidence.content)
        intake = next(e.payload for e in self.workspace.events if e.kind == 'ObservationIntakeRecorded')
        for field in ('event_id', 'source_record_id', 'source_version', 'received_at',
                      'quality_note', 'coordinate_system'):
            self.assertEqual(ROW[field], intake[field])
        self.assertEqual(ROW['observed_at'], intake['original_collected_at'])
        self.assertEqual('simulated', intake['data_kind'])
        self.assertEqual({}, self.workspace.orders)
        self.assertEqual({}, self.workspace.verifications)
        self.assertEqual((), self.workspace.verify_audit_chain())

    def test_context_and_per_item_validation_reject_spoofing_without_dropping_good_row(self):
        bad = [dict(ROW, event_id='another-event'), dict(ROW, source_version=True),
               dict(ROW, observed_at='2026-10-07T10:00:00'),
               dict(ROW, received_at='2026-10-07T10:01:00'),
               dict(ROW, verification_status='verified'), dict(ROW, data_kind='observed'),
               dict(ROW, quality_note={}), dict(ROW, source_record_id=''),
               dict(ROW, source_version=0), dict(ROW, value=True), None]
        report = self.store.import_source_json(bad + [ROW], event_id=EVENT,
                                               source='adapter', data_kind='simulated')
        self.assertEqual(len(bad), len(report.rejected))
        self.assertEqual(list(range(len(bad))), [r.index for r in report.rejected])
        self.assertEqual(1, len(report.accepted))
        for context in (dict(event_id='', source='adapter', data_kind='simulated'),
                        dict(event_id=EVENT, source='', data_kind='observed'),
                        dict(event_id=EVENT, source='adapter', data_kind='verified')):
            with self.assertRaises(ValueError):
                self.store.import_source_json([ROW], **context)
        self.assertEqual(1, len(self.store.observations()))

    def test_same_revision_conflict_preserves_first_record_and_allows_next_version(self):
        original = self.ingest().accepted[0]
        report = self.ingest(value=4)
        self.assertFalse(report.accepted)
        self.assertIn('conflict', ' '.join(report.rejected[0].errors))
        self.assertEqual(original, self.store.get(original.observation_id))
        self.assertTrue(self.ingest(source_version=2, value=4).all_passed)
        self.assertEqual(2, len(self.store.observations()))

    def test_replay_keeps_first_receipt_and_equivalent_offset_has_same_identity(self):
        original = self.ingest().accepted[0]
        replay = self.ingest(observed_at='2026-10-07T10:00:00+08:00',
                             received_at='2026-10-07T11:00:00+08:00')
        self.assertEqual(original, replay.accepted[0])
        self.assertEqual([original.observation_id], self.store.last_duplicates)
        self.assertEqual(original, self.store.observations()[0])

    def test_different_event_has_distinct_identity_and_cannot_mask_current_event(self):
        local = self.ingest().accepted[0]
        foreign = self.store.import_source_json([dict(ROW, event_id='anonymous-event-b',
            observed_at='2026-10-07T11:00:00+08:00', value=0)], event_id='anonymous-event-b',
            source='adapter', data_kind='simulated').accepted[0]
        self.assertNotEqual(local.observation_id, foreign.observation_id)
        result = self.driver.step()
        self.assertEqual((foreign.observation_id,), result['rejected_observation_ids'])
        self.assertEqual('met', self.workspace.claims[RULE.subject].value)
        self.assertNotIn(self.bridge.evidence_id_for(foreign), self.workspace.evidence)

    def test_unbound_legacy_input_cannot_enter_bound_workspace(self):
        self.store.import_json([ROW], source='legacy')
        version = self.workspace.version
        report = self.bridge.bridge_pending(self.workspace)
        self.assertEqual(1, len(report.rejected_observation_ids))
        self.assertEqual(version, self.workspace.version)
        self.assertFalse(self.workspace.evidence)
        self.assertFalse(self.workspace.claims)

    def test_bound_input_cannot_fall_back_to_unbound_workspace(self):
        self.ingest()
        unbound = ScenarioWorkspace('legacy')
        self.assertTrue(self.bridge.bridge_pending(unbound).rejected_observation_ids)
        self.assertFalse(unbound.evidence)

    def test_declared_observed_source_is_still_pending_even_when_allowlisted(self):
        record = self.ingest(kind='observed').accepted[0]
        self.bridge.bridge_pending(self.workspace)
        self.assertEqual(SourceTrustLevel.PENDING_VERIFICATION,
                         self.workspace.evidence[self.bridge.evidence_id_for(record)].trust_level)
        self.assertIn('待核', self.workspace.claims[SUBJECT].source)

    def test_source_kind_change_keeps_current_claim_and_condition_but_retains_evidence(self):
        self.ingest(kind='observed')
        self.driver.step()
        current = self.workspace.claims[SUBJECT]
        condition = self.workspace.claims[RULE.subject]
        self.ingest(source_record_id='reading-2', value=0,
                    observed_at='2026-10-07T11:00:00+08:00')
        result = self.driver.step()
        self.assertEqual(current, self.workspace.claims[SUBJECT])
        self.assertEqual(condition, self.workspace.claims[RULE.subject])
        self.assertEqual(2, len(self.workspace.evidence))
        self.assertTrue(any('来源性质' in note for note in result['notes']))

    def test_cross_site_source_kind_conflict_is_explicit_and_repeat_is_audit_idempotent(self):
        self.ingest(kind='observed')
        self.driver.step()
        current = self.workspace.claims[RULE.subject]
        report = self.ingest(site_id='anonymous-site-b', source_record_id='reading-2', value=0,
                             observed_at='2026-10-07T11:00:00+08:00')
        self.assertTrue(report.all_passed)
        result = self.driver.step()
        self.assertEqual(current, self.workspace.claims[RULE.subject])
        self.assertTrue(any('条件来源性质' in note for note in result['notes']))
        self.assertTrue(any(e.kind == 'ConditionSourceKindConflict' for e in self.workspace.events))
        version = self.workspace.version
        self.driver.step()
        self.assertEqual(version, self.workspace.version)

    def test_same_condition_new_reading_tracks_new_evidence_without_faking_clocks(self):
        self.ingest()
        self.driver.step()
        previous = self.workspace.claims[RULE.subject]
        newer = self.ingest(source_record_id='reading-2', value=4,
                            observed_at='2026-10-07T11:00:00+08:00').accepted[0]
        self.driver.step()
        current = self.workspace.claims[RULE.subject]
        self.assertEqual(previous.value, current.value)
        self.assertNotEqual(previous.evidence_ids, current.evidence_ids)
        self.assertEqual((self.bridge.evidence_id_for(newer),), current.evidence_ids)
        version = self.workspace.version
        self.driver.step()
        self.assertEqual(version, self.workspace.version)

    def test_simulated_data_cannot_verify_an_observation_through_source_allowlist(self):
        self.ingest()
        self.driver.step()
        current = self.workspace.claims[SUBJECT]
        with self.assertRaises(ValueError):
            self.workspace.update_claim(SUBJECT, value=current.value,
                status=ClaimStatus.VERIFIED, source='test', confidence=1,
                evidence_ids=current.evidence_ids)
        self.assertEqual(current, self.workspace.claims[SUBJECT])

    def test_missing_optional_metadata_stays_unknown_without_fake_receipt_clock(self):
        raw = {key: value for key, value in ROW.items()
               if key not in ('received_at', 'quality_note', 'coordinate_system')}
        report = self.driver.load_source_sequence([raw], source='adapter', data_kind='unknown')
        record = report.accepted[0]
        self.assertEqual(('', '', ''), (record.received_at, record.quality_note, record.coordinate_system))
        self.driver.step()
        self.assertIn('未知', self.workspace.claims[SUBJECT].source)

    def test_metadata_and_revision_conflict_survive_sqlite_restart(self):
        original = self.ingest().accepted[0]
        self.driver.step()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.db'
            save_workspace(self.workspace, path, observation_store=self.store)
            restored_store = ObservationStore()
            restored = load_workspace(path, observation_store=restored_store)
        self.assertEqual((original,), restored_store.observations())
        self.assertFalse(restored_store.import_source_json([dict(ROW, value=0)],
            event_id=EVENT, source='adapter', data_kind='simulated').all_passed)
        version = restored.version
        IntakeBridge(restored_store).bridge_pending(restored)
        self.assertEqual(version, restored.version)
        self.assertEqual((), restored.verify_audit_chain())

    def test_old_serialized_observation_loads_with_explicit_unknown_defaults(self):
        original = self.store.import_json([ROW], source='legacy').accepted[0]
        old = {key: getattr(original, key) for key in
               ('observation_id', 'site_id', 'metric', 'value', 'unit', 'observed_at', 'source')}
        restored = decode_entity(json.dumps({'__class__': 'ObservationRecord', 'data': old}))
        self.assertIsInstance(restored, ObservationRecord)
        self.assertEqual('', restored.event_id)
        self.assertEqual('unknown', restored.data_kind)
        self.assertEqual('', restored.received_at)


if __name__ == '__main__':
    unittest.main()
