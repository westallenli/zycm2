"""Automatic source projection, continuous local refresh and original authority."""
import hashlib
import json
import re
import unittest
from unittest.mock import patch
from dataclasses import replace
from panda_brain import intelligence as intel
from panda_brain.map_annotations import MapAnnotation, AnnotationKind, AnnotationStatus
from panda_brain.panel import render_page
from tests import test_upgrade_business_modules as fixtures
from tests import test_upgrade_live_context as context_fixture
from tests.test_upgrade_source_channels import packet
from panda_brain.source_channels import receive_channel
Fixture=fixtures.BusinessModulesTests


class IntelligenceProjectionTests(context_fixture.LiveContextTests):
    # Reuse its source/audit tests as part of this same bounded projection work.
    def test_fire_position_projects_original_coordinate_without_writing_history(self):
        self.registry.events['event-a']=replace(self.registry.events['event-a'],status='active',lat=30.1234,lon=103.5678)
        before=self.ws.version
        report,marks=intel.collect(self.state,'event-a')
        self.assertEqual(1,len(marks));self.assertEqual(((30.1234,103.5678),),marks[0].geo_points)
        self.assertEqual(AnnotationStatus.PENDING_REVIEW,marks[0].status)
        self.assertTrue(marks[0].source_ref[0].startswith('event-record:event-a:'))
        self.assertEqual({},self.ws.annotations);self.assertEqual(before,self.ws.version)
        self.assertIn('火情来源',report['groups']['fire'][0]['text'])

    def test_unknown_position_stays_in_gap_list_never_uses_default_map_centre(self):
        report,marks=intel.collect(self.state,'event-a')
        self.assertFalse(marks);self.assertIn('fire-position',[g['id'] for g in report['gaps']])
        for lat,lon in ((True,110),(91,110),(30,float('nan'))):
            self.registry.events['event-a']=replace(self.registry.events['event-a'],status='active',lat=lat,lon=lon)
            self.assertIsNone(intel.fire_projection(self.state,'event-a'))

    def test_coordinate_change_has_new_source_reference_and_does_not_overwrite_objects(self):
        self.registry.events['event-a']=replace(self.registry.events['event-a'],status='active',lat=30,lon=103)
        a,first=intel.collect(self.state,'event-a')
        self.registry.events['event-a']=replace(self.registry.events['event-a'],lat=31)
        b,second=intel.collect(self.state,'event-a')
        self.assertNotEqual(a['fingerprint'],b['fingerprint']);self.assertNotEqual(first[0].source_ref,second[0].source_ref)
        self.assertEqual(((30,103),),first[0].geo_points);self.assertEqual({},self.ws.annotations)

    def test_incoming_channel_and_geographic_observation_are_collected_automatically(self):
        before,_=intel.collect(self.state,'event-a')
        received=receive_channel(self.ws,packet(summary='新增现场来源'),source='anonymous-camera',data_kind='simulated')
        self.ws.record_annotation(MapAnnotation(id='team-known',kind=AnnotationKind.TEAM_POSITION,points=(),geo_points=((31,110),),
            title='已报告队伍位置',event_id='event-a',source_ref=(received['evidence_id'],),status=AnnotationStatus.DRAFT))
        version=self.ws.version;after,marks=intel.collect(self.state,'event-a')
        self.assertNotEqual(before['fingerprint'],after['fingerprint']);self.assertEqual(['team-known'],[m.id for m in marks])
        self.assertIn('新增现场来源',intel.content(after));self.assertEqual(version,self.ws.version)
        self.assertEqual({},self.ws.orders);self.assertEqual({},self.ws.verifications)

    def test_no_position_map_object_remains_source_data_not_a_new_guessed_point(self):
        self.ws.record_annotation(MapAnnotation(id='unlocated',kind=AnnotationKind.WATER,points=((10,10),),
            title='口述水源待定位',event_id='event-a'))
        report,marks=intel.collect(self.state,'event-a')
        self.assertFalse(marks[0].geo_points);self.assertTrue(any(g['id']=='position-unlocated' for g in report['gaps']))
        self.assertIn('位置待核对',intel.content(report))

    def test_escaped_source_notes_and_no_external_acquisition_claim(self):
        self.registry.events['event-a']=replace(self.registry.events['event-a'],initial_report='<img src=x onerror=alert(1)>')
        report,_=intel.collect(self.state,'event-a');html=intel.content(report)
        self.assertIn('&lt;img',html);self.assertNotIn('<img',html);self.assertIn('尚未实测接通',html)

    def test_existing_freshness_rule_produces_readonly_review_advice(self):
        before=self.ws.version
        with patch.object(intel.sm,'scan_freshness',return_value=[dict(reason='队伍来源已过期，需要核查',fingerprint_parts=('freshness','team-a',1))]):
            report,_=intel.collect(self.state,'event-a')
        self.assertIn('核对来源时间与现状',intel.content(report));self.assertEqual(before,self.ws.version)
        self.assertEqual({},self.ws.situation_changes)


class IntelligenceHttpTests(unittest.TestCase):
    DUTY,DEPUTY,CAPTAIN=Fixture.DUTY,Fixture.DEPUTY,Fixture.CAPTAIN
    MEMBER_A,MEMBER_B,TEMP=Fixture.MEMBER_A,Fixture.MEMBER_B,Fixture.TEMP
    OTHER,OTHER_MEMBER=Fixture.OTHER,Fixture.OTHER_MEMBER
    _demo_event,bytes,server,request,foreign_event,page=Fixture._demo_event,Fixture.bytes,Fixture.server,Fixture.request,Fixture.foreign_event,Fixture.page
    def setUp(self):
        fixtures.BusinessModulesTests.setUp(self)
        self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],lat=30.1234,lon=103.5678)
        self.state._save_registry()

    def test_three_roles_share_automatic_fire_point_and_source_panel_without_write_authority_change(self):
        before=self.bytes();payloads=[]
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,html=self.page('/sand',phone);self.assertEqual(200,status)
            self.assertIn('智能参谋',html);self.assertIn('data-intel-content',html)
            self.assertIn('全部下图',html);self.assertIn('恢复显示',html)
            payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1]);payloads.append(payload)
            self.assertEqual([[30.1234,103.5678]],payload['marks'][-1]['pts'])
            self.assertTrue(payload['marks'][-1]['projection']);self.assertFalse(payload['marks'][-1]['published'])
        self.assertEqual(payloads[0],payloads[1]);self.assertEqual(payloads[1],payloads[2]);self.assertEqual(before,self.bytes())

    def test_feed_is_event_scoped_authenticated_and_all_gets_preserve_bytes(self):
        server=self.server();before=self.bytes();path='/sand/live-context?event_id='+self.event_id
        for phone in (self.DUTY,self.DEPUTY,self.CAPTAIN):
            status,_,body=self.request(server,path,phone=phone);self.assertEqual(200,status)
            data=json.loads(body);self.assertEqual(self.event_id,data['intelligence']['report']['event_id'])
            self.assertEqual('持续汇集',data['intelligence']['runtime']['mode'])
            self.assertTrue(data['marks'][-1]['projection'])
        self.assertEqual(403,self.request(server,path,cookie='invalid=')[0])
        self.assertEqual(404,self.request(server,'/sand/live-context?event_id=foreign',phone=self.CAPTAIN)[0])
        self.assertEqual(before,self.bytes())

    def test_background_tick_reads_bound_events_and_restores_original_context(self):
        server=self.server();before=self.bytes();prior=self.state.workspace,self.state.active_event_id,self.state.db_path
        runtime=self.state.intelligence_runtime;runtime.last_tick=None;runtime.tick()
        self.assertIn(self.event_id,runtime.events)
        self.assertNotIn('error',runtime.events[self.event_id])
        self.assertEqual(prior,(self.state.workspace,self.state.active_event_id,self.state.db_path));self.assertEqual(before,self.bytes())
        old=runtime.events[self.event_id]['checked_at'];runtime.tick()
        self.assertEqual(old,runtime.events[self.event_id]['checked_at'])

    def test_original_source_change_reaches_next_feed_and_does_not_replace_published_marks(self):
        server=self.server();path='/sand/live-context?event_id='+self.event_id
        first=json.loads(self.request(server,path,phone=self.DEPUTY)[2])
        self.state.handle_sand_observe(kind='hotspot',title='新到火点报告',geo_text='30.2,103.6',
            observed_at='2026-10-13T14:00:00+08:00',source_ref='test-source-001',reporter='模拟现场报告员',is_demo=True)
        second=json.loads(self.request(server,path,phone=self.DEPUTY)[2])
        self.assertNotEqual(first['fingerprint'],second['fingerprint'])
        self.assertTrue(any(m['title']=='新到火点报告' for m in second['marks']))
        self.assertFalse(any(m['published'] for m in second['marks']))

    def test_public_display_does_not_receive_projection_or_intelligence_panel(self):
        status,html=render_page(self.state,'/sand',member_token=self.tokens[self.CAPTAIN],query={'projector':['1']})
        self.assertEqual(200,status);self.assertNotIn('data-intel-content',html)
        payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1])
        self.assertFalse(any(m.get('projection') for m in payload['marks']))


del Fixture
