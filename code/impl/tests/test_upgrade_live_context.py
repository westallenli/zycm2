import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from panda_brain.fire_events import EventRegistry
from panda_brain.materials import MaterialsLedger
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.map_annotations import MapAnnotation, AnnotationKind
from panda_brain.live_context import event_context, render_block, answer_question
from panda_brain.source_channels import receive_channel
from panda_brain.grounded_inquiry import validate_sources
from tests.test_upgrade_source_channels import packet
from tests import test_upgrade_offline_tasks as offline_fixture
from tests import test_upgrade_personnel_groups as personnel_fixture
from panda_brain.imagery import ImageryLedger
from panda_brain.persistence import load_workspace


class LiveContextTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('anonymous', event_id='event-a')
        self.registry = EventRegistry()
        self.registry.create_event(event_id='event-a', name='anonymous', report_no='ANON',
            discovered_at='2026-10-07T08:00:00+08:00', source='anonymous', location='unknown')
        self.state = SimpleNamespace(workspace=self.ws, registry=self.registry,
                                     active_event_id='event-a', ledger=MaterialsLedger())

    def context(self): return event_context(self.state, 'event-a')[0]

    def test_empty_projection_explains_unknowns_and_never_mutates(self):
        before = self.ws.version
        data = self.context()
        self.assertEqual(6, len(data['groups']))
        self.assertTrue(data['groups']['materials'])
        self.assertEqual(before, self.ws.version)
        self.assertEqual({}, self.ws.orders)

    def test_exact_source_link_no_geographic_guess_or_implicit_point(self):
        self.ws.record_annotation(MapAnnotation(id='map-a', kind=AnnotationKind.WATER,
            title='anonymous', points=((10.,20.),), event_id='event-a', source_ref=('frame-1',)))
        receive_channel(self.ws, packet(), source='anonymous-camera', data_kind='simulated')
        data, marks = event_context(self.state, 'event-a')
        row = data['groups']['maps'][0]
        self.assertIn('来源证据 channel-', row['text'])
        self.assertIn('不生成点位', row['text'])
        self.assertEqual((), marks[0].geo_points)
        self.assertEqual({}, self.ws.verifications)

    def test_explicit_target_link_and_unknown_target_do_not_create_objects(self):
        self.ws.record_annotation(MapAnnotation(id='map-a', kind=AnnotationKind.WATER,
            title='anonymous', points=((10.,20.),), event_id='event-a'))
        receive_channel(self.ws, packet(targets=[dict(kind='map', id='map-a'), dict(kind='task',id='missing')]),
                        source='anonymous-camera', data_kind='simulated')
        self.assertIn('来源证据 channel-', self.context()['groups']['maps'][0]['text'])
        self.assertEqual({}, self.ws.orders)

    def test_unaudited_map_not_exposed_and_other_event_rejected(self):
        self.ws.annotations['x'] = MapAnnotation(id='x', kind=AnnotationKind.WATER,
            title='secret', points=((10.,20.),), event_id='event-a')
        self.assertNotIn('secret', json.dumps(self.context(), ensure_ascii=False))
        self.state.active_event_id = 'other'
        with self.assertRaises(ValueError): self.context()

    def test_channel_change_updates_fingerprint_without_inferred_inventory(self):
        first = self.context()
        receive_channel(self.ws, packet(summary='<img src=x onerror=alert(1)>'),
                        source='anonymous-camera', data_kind='simulated')
        second = self.context()
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        self.assertEqual(first['groups']['materials'], second['groups']['materials'])
        html = render_block(second, 'channels')
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img', html)
        self.assertIn('未实测', html)

    def test_local_answer_has_checked_current_citation_and_stale_answer_is_rejected(self):
        answer = answer_question(self.state, 'event-a', '联动摄像头和物资')
        checked = validate_sources(answer,self.ws,self.state.ledger,'event-a','commander',registry=self.registry)
        self.assertTrue(checked)
        receive_channel(self.ws, packet(), source='anonymous-camera', data_kind='simulated')
        with self.assertRaises(ValueError):
            validate_sources(answer,self.ws,self.state.ledger,'event-a','commander',registry=self.registry)
        with self.assertRaises(ValueError):
            validate_sources(answer,self.ws,self.state.ledger,'event-a','member',registry=self.registry)

    def test_named_feedback_and_independent_closure_refresh_same_event_projection(self):
        fixture = offline_fixture.OfflineTaskTests('test_offline_record_keeps_two_times_and_never_publishes')
        fixture.setUp()
        self.state.workspace = fixture.ws
        task_id = fixture.create()['task_id']
        initial = self.context()
        for action in ('received','started','completed'): fixture.feedback(task_id,action)
        pending = self.context()
        self.assertIn('待核验', pending['groups']['tasks'][0]['text'])
        self.assertNotEqual(initial['fingerprint'],pending['fingerprint'])
        fixture.close(task_id)
        closed = self.context()
        self.assertNotEqual(pending['fingerprint'],closed['fingerprint'])
        self.assertEqual({},fixture.ws.orders)

    def test_audit_corruption_blocks_all_business_groups(self):
        receive_channel(self.ws, packet(), source='anonymous-camera', data_kind='simulated')
        self.ws.events[0].payload['changed'] = True
        with self.assertRaises(ValueError): self.context()

    def test_independent_imagery_change_updates_fingerprint_without_workspace_change(self):
        self.state.imagery = ImageryLedger()
        before = self.context()
        version = self.ws.version
        self.state.imagery.add_image(event_id='event-a', source_system='anonymous-camera',
            collected_by='anonymous', collected_at='2026-10-07T10:00:00+08:00',
            uploaded_at='2026-10-07T10:00:05+08:00', image_type='png', has_spatial_info=False)
        after = self.context()
        self.assertEqual(version, self.ws.version)
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertIn('仅元数据', after['groups']['channels'][0]['text'])
        self.assertEqual({}, self.ws.annotations)

    def test_named_personnel_arrival_changes_current_event_supply_only(self):
        f = personnel_fixture.PersonnelGroupTests('test_fixed_roster_group_is_frozen_and_arrival_is_separate')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.load_fixed()
        def read():
            ws = load_workspace(f.state.registry.events[f.event_id].snapshot_path, general_auth=f.auth)
            return event_context(SimpleNamespace(workspace=ws,registry=f.state.registry,
                ledger=f.state.ledger,active_event_id=f.event_id),f.event_id)[0]
        before = read()
        f.state.handle_captain_arrival(f.event_id,captain_token=f.tokens[f.CAPTAIN],
            squad_id=f.squad_id,mode='initial',phones=[f.FIXED],request_id='anonymous-live-arrival')
        after = read()
        self.assertNotEqual(before['fingerprint'],after['fingerprint'])
        self.assertTrue(any('报告到场 1' in row['text'] for row in after['groups']['personnel']), after['groups']['personnel'])
        self.assertTrue(any('需求人数待核定' in row['text'] for row in after['groups']['personnel']))
