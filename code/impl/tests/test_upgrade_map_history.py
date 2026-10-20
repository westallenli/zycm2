import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from panda_brain.auth import GeneralEntryAuth
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain.map_history import annotation_at, latest_annotation_reference, resolve_annotation_reference
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.record_contracts import ObjectReference
from panda_brain.workspace import ScenarioWorkspace, WorkspaceConflictError


class MapHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('map', event_id='e1')
        self.clock = patch('panda_brain.map_history._now', return_value='2026-09-25T01:00:00+00:00')
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def mark(self, **changes):
        args = dict(id='ann-001', kind=AnnotationKind.WATER, title='模拟水源',
                    points=((10.0, 20.0),), event_id='e1', map_version='base-A')
        args.update(changes)
        return MapAnnotation(**args)

    def reference(self, annotation_id='ann-001'):
        return latest_annotation_reference(self.ws, event_id='e1', annotation_id=annotation_id)

    def test_edit_retains_old_state_and_does_not_use_current_data(self):
        self.ws.record_annotation(self.mark())
        first = self.reference()
        self.now.return_value = '2026-09-25T02:00:00+00:00'
        self.ws.update_annotation('ann-001', title='修正水源', base_version=1)
        self.assertEqual('模拟水源', resolve_annotation_reference(self.ws, first).title)
        self.assertEqual('修正水源', resolve_annotation_reference(self.ws, self.reference()).title)
        self.assertEqual('base-A', resolve_annotation_reference(self.ws, first).map_version)
        self.assertEqual('模拟水源', annotation_at(self.ws, event_id='e1', annotation_id='ann-001',
            known_at='2026-09-25T09:30:00+08:00').title)

    def test_status_has_separate_history_sequence(self):
        self.ws.record_annotation(self.mark())
        first = self.reference()
        self.ws.set_annotation_status('ann-001', AnnotationStatus.PENDING_REVIEW)
        self.assertEqual(2, self.reference().version)
        self.assertEqual(1, self.ws.annotations['ann-001'].version)
        self.assertEqual(AnnotationStatus.DRAFT, resolve_annotation_reference(self.ws, first).status)
        self.assertEqual(AnnotationStatus.PENDING_REVIEW,
                         resolve_annotation_reference(self.ws, self.reference()).status)

    def test_geometry_history_and_duplicate_geometry_no_write(self):
        self.ws.record_annotation(self.mark())
        first = self.reference()
        self.ws.set_annotation_geo('ann-001', ((30.0, 100.0),))
        self.assertEqual((), resolve_annotation_reference(self.ws, first).geo_points)
        self.assertEqual(((30.0, 100.0),), resolve_annotation_reference(self.ws, self.reference()).geo_points)
        before = self.ws.events
        self.ws.set_annotation_geo('ann-001', ((30.0, 100.0),))
        self.assertEqual(before, self.ws.events)

    def test_published_revision_references_exact_parent_and_keeps_original(self):
        self.ws.record_annotation(self.mark(status=AnnotationStatus.PUBLISHED))
        parent = self.reference()
        with self.assertRaises(ValueError):
            self.ws.set_annotation_geo('ann-001', ((30.0, 100.0),))
        child = self.ws.update_annotation('ann-001', title='新草稿', base_version=1, as_revision=True)
        ref = self.reference(child.id)
        self.assertEqual(1, ref.version)
        self.assertEqual(2, child.version)
        record = next(r for r in self.ws.object_revisions.values() if r.reference == ref)
        self.assertEqual(parent.object_id, json.loads(record.content_json)['supersedes_reference']['object_id'])
        self.assertEqual(parent.version, json.loads(record.content_json)['supersedes_reference']['version'])
        self.assertEqual(AnnotationStatus.PUBLISHED, self.ws.annotations['ann-001'].status)
        self.assertEqual(AnnotationStatus.DRAFT, resolve_annotation_reference(self.ws, ref).status)

    def test_cross_event_and_wrong_kind_references_rejected(self):
        self.ws.record_annotation(self.mark())
        for ref in (ObjectReference('e2', 'map_annotation', 'ann-001', 1),
                    ObjectReference('e1', 'task', 'ann-001', 1)):
            with self.assertRaises(ValueError):
                resolve_annotation_reference(self.ws, ref)
        before = self.ws.events
        with self.assertRaises(ValueError):
            self.ws.record_annotation(self.mark(id='ann-002', event_id='e2'))
        self.assertEqual(before, self.ws.events)
        self.assertNotIn('ann-002', self.ws.annotations)

    def test_missing_history_has_no_current_map_fallback(self):
        self.ws.annotations['ann-001'] = self.mark()  # legacy/import without history
        self.assertIsNone(self.reference())
        self.assertIsNone(annotation_at(self.ws, event_id='e1', annotation_id='ann-001',
                                       known_at='2026-09-26T00:00:00+00:00'))

    def test_late_record_does_not_appear_before_recording(self):
        self.ws.record_annotation(self.mark(created_at='2026-09-24T01:00:00+00:00'))
        self.assertIsNone(resolve_annotation_reference(self.ws, self.reference(),
                            known_at='2026-09-25T00:59:59+00:00'))
        record = next(iter(self.ws.object_revisions.values()))
        self.assertEqual('', record.times.occurred_at)

    def test_history_results_do_not_mutate_persisted_snapshots(self):
        self.ws.record_annotation(self.mark(details={'source_ref': '模拟来源'}))
        ref = self.reference()
        result = resolve_annotation_reference(self.ws, ref)
        result.title = 'mutated'
        result.details['source_ref'] = 'modified'
        self.assertEqual('模拟水源', resolve_annotation_reference(self.ws, ref).title)
        self.assertEqual('模拟来源', resolve_annotation_reference(self.ws, ref).details['source_ref'])

    def test_restart_preserves_exact_versions_and_audit(self):
        self.ws.record_annotation(self.mark())
        first = self.reference()
        self.ws.set_annotation_geo('ann-001', ((30.0, 100.0),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.db'
            save_workspace(self.ws, path)
            restored = load_workspace(path)
        self.assertEqual((), restored.verify_audit_chain())
        self.assertEqual((), resolve_annotation_reference(restored, first).geo_points)
        self.assertEqual(((30.0, 100.0),), resolve_annotation_reference(restored, self.reference()).geo_points)

    def test_entity_tamper_and_audit_tamper_rejected(self):
        self.ws.record_annotation(self.mark())
        ref = self.reference()
        original = next(iter(self.ws.object_revisions.values()))
        content = json.loads(original.content_json)
        content['annotation']['data']['title'] = 'tampered'
        self.ws.object_revisions[original.id] = replace(original, content_json=json.dumps(content))
        with self.assertRaises(ValueError):
            resolve_annotation_reference(self.ws, ref)
        self.ws.object_revisions[original.id] = original
        self.ws._events[-1].payload['title'] = 'tampered'
        with self.assertRaises(ValueError):
            resolve_annotation_reference(self.ws, ref)

    def test_rejected_status_or_stale_edit_never_appends_history(self):
        auth = GeneralEntryAuth(secret='simulation-secret')
        self.ws = ScenarioWorkspace('map', event_id='e1', general_auth=auth)
        self.ws.record_annotation(self.mark())
        self.ws.set_annotation_status('ann-001', AnnotationStatus.PENDING_REVIEW)
        before = self.ws.events
        with self.assertRaises(PermissionError):
            self.ws.set_annotation_status('ann-001', AnnotationStatus.VERIFIED)
        with self.assertRaises(WorkspaceConflictError):
            self.ws.update_annotation('ann-001', title='stale', base_version=99)
        self.assertEqual(before, self.ws.events)

    def test_legacy_workspace_gets_no_guessed_event_history(self):
        old = ScenarioWorkspace('legacy')
        old.record_annotation(self.mark(event_id=''))
        self.assertEqual({}, old.object_revisions)
        with self.assertRaises(ValueError):
            resolve_annotation_reference(old, ObjectReference('e1', 'map_annotation', 'ann-001', 1))

    def test_duplicate_status_no_write_and_generic_history_injection_rejected(self):
        self.ws.record_annotation(self.mark())
        before = self.ws.events
        self.ws.set_annotation_status('ann-001', AnnotationStatus.DRAFT)
        self.assertEqual(before, self.ws.events)
        record = next(iter(self.ws.object_revisions.values()))
        with self.assertRaises(PermissionError):
            self.ws.record_object_revision(record, expected_version=0)
        self.assertEqual(before, self.ws.events)


if __name__ == '__main__':
    unittest.main()
