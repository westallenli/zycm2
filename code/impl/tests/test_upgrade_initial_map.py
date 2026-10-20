"""Initial map comes from scoped synthetic source records, not guessed field facts."""
import copy
import json
import re
import unittest
from dataclasses import replace
from unittest.mock import patch
from panda_brain import initial_map as im, intelligence, map_brief, sand_command
from panda_brain.durable_storage import action
from panda_brain.panel import render_page


class InitialMapTests(unittest.TestCase):
    def setUp(self):
        from tests.test_upgrade_map_brief import MapBriefTests
        self.f=MapBriefTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.s=self.f.s;self.eid=self.f.eid;self.token=self.f.token
        self.data=copy.deepcopy(im.bundle());self.data['event_id']=self.eid
        self.data['event_name']=self.s.registry.events[self.eid].name
        self.data['anchor']=[self.s.registry.events[self.eid].lat,self.s.registry.events[self.eid].lon]
        self.p=patch('panda_brain.initial_map.bundle',return_value=self.data);self.p.start();self.addCleanup(self.p.stop)

    def authorize(self,kind='simulated',source='DEMO_CONSOLE'):
        self.s.workspace.append_event('ExternalFireEventReceived',event_id=self.eid,data_kind=kind,source_system=source)
        self.s.persist_if_db();self.s._persist_active_task_snapshot()

    def brief(self):return sand_command.summary(self.s,self.token)['map_brief']

    def test_no_seed_for_unverified_source_or_real_event_even_with_same_name(self):
        self.assertFalse(self.brief()['initial_scene']['enabled'])
        self.authorize('reported');self.assertFalse(self.brief()['initial_scene']['enabled'])
        self.authorize(source='unverified-source');self.assertFalse(self.brief()['initial_scene']['enabled'])
        self.authorize();self.assertTrue(self.brief()['initial_scene']['enabled'])
        self.authorize('reported');self.assertFalse(self.brief()['initial_scene']['enabled'])

    def test_requires_exact_event_name_anchor_and_active_state(self):
        self.authorize();self.assertTrue(self.brief()['initial_scene']['enabled'])
        for key,value in (('event_id','foreign'),('event_name','foreign'),('anchor',[0,0])):
            old=self.data[key];self.data[key]=value
            self.assertFalse(im.collect(self.s,self.eid)['enabled']);self.data[key]=old
        saved=self.s.registry.events[self.eid];self.s.registry.events[self.eid]=replace(saved,status='registered')
        self.assertFalse(im.collect(self.s,self.eid)['enabled'])

    def test_twenty_two_source_projections_are_stable_readonly_and_unverified(self):
        self.authorize();before=self.f.f.f.files();a=im.collect(self.s,self.eid);b=im.collect(self.s,self.eid)
        self.assertEqual(22,len(a['marks']));self.assertEqual(22,len(a['sources']));self.assertEqual(a['fingerprint'],b['fingerprint'])
        self.assertTrue(all(m.is_demo and m.status.value=='pending_review' and m.source_ref for m in a['marks']))
        kinds={m.kind.value for m in a['marks']}
        self.assertTrue({'fire_area','main_fireline','team_position','water_source','command_post','protected_target'}<=kinds)
        self.assertEqual(before,self.f.f.f.files());self.assertFalse(self.s.workspace.annotations)
        self.assertFalse(self.s.workspace.orders)

    def test_entered_page_and_refresh_feed_have_same_source_geometry(self):
        self.authorize();before=self.f.f.f.files();code,html=render_page(self.s,'/sand',member_token=self.token)
        self.assertEqual(200,code)
        payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1])
        page={m['id']:m['pts'] for m in payload['marks'] if m['id'].startswith('initial-demo-')}
        report,marks=intelligence.collect(self.s,self.eid)
        self.assertEqual(page,{m.id:[list(p) for p in m.geo_points] for m in marks if m.id.startswith('initial-demo-')})
        self.assertEqual(22,len(page));self.assertIn('initial_demo',report['groups'])
        self.assertEqual(before,self.f.f.f.files())

    def test_demo_gap_alert_and_reply_are_located_but_do_not_close_real_gaps(self):
        self.authorize();brief=self.brief()
        messages=[i for i in brief['items'] if i['id'].startswith('initial-message:')]
        self.assertEqual({'pending','urgent','reply'},{m['category'] for m in messages})
        self.assertTrue(all(m['demo'] and m['points'] and '演示' in m['location'] for m in messages))
        self.assertTrue(any(i['id'].startswith('gap:') for i in brief['items']))
        self.assertFalse(self.s.workspace.orders)

    def test_initial_suggestion_edits_preserve_sources_and_survive_refresh(self):
        self.authorize();row=next(s for s in self.brief()['suggestions'] if s['id'].startswith('initial-demo-') and s['geometry']=='point')
        original=copy.deepcopy(self.data)
        v=dict(event_id=self.eid,id=row['id'],basis=row['basis'],action='edit',title='演示人工修改',points='[[30.15,103.59]]',reason='核对演示来源后调整',expected_version='0',request_id='initial-edit')
        with action(self.s):map_brief.write(self.s,self.token,v)
        updated=next(s for s in self.brief()['suggestions'] if s['id']==row['id'])
        self.assertEqual([[30.15,103.59]],updated['points']);self.assertEqual(row['original'],updated['original'])
        self.assertEqual(original,self.data)
        code=row['id'].rsplit('-',2)[-2:];source=next(r for r in self.data['records'] if r['id']=='-'.join(code))
        source['basis']='演示资料新增信息'
        refreshed=next(s for s in self.brief()['suggestions'] if s['id']==row['id'])
        self.assertTrue(refreshed['changed']);self.assertEqual(updated['points'],refreshed['points'])

    def test_invalid_source_coordinate_is_not_replaced_by_map_center(self):
        self.authorize();self.data['records'][0]['points'][0]=[999,0]
        with self.assertRaises(ValueError):im.collect(self.s,self.eid)

    def test_suggestions_share_semantic_rendering_and_located_firebreak_gap(self):
        from panda_brain.sandview import _mark_payload
        self.authorize();marks={m.id:m for m in im.collect(self.s,self.eid)['marks']}
        rows=[r for r in self.brief()['suggestions'] if r['id'] in marks]
        for row in rows:
            native=_mark_payload(marks[row['id']], 'commander', tip=False)
            self.assertEqual(row['render'],{k:native[k] for k in row['render']})
        gap=next(r for r in rows if r['id'].endswith('W-02'))
        self.assertEqual(2,gap['render']['gap_geometry'][0]['segment_index'])
        self.assertEqual(.5,gap['render']['gap_geometry'][0]['fraction'])
        self.assertTrue(all(r['render']['route_type']=='plan' for r in rows if r['kind']=='team_route'))

    def test_public_page_never_includes_private_initial_demo_bundle(self):
        self.authorize();code,html=render_page(self.s,'/sand',member_token=self.token,query={'projector':['1']})
        self.assertEqual(200,code)
        payload=json.loads(re.search(r'id="cmd-map-data">(.*?)</script>',html,re.S)[1])
        self.assertFalse(any(m['id'].startswith('initial-demo-') for m in payload['marks']))

    def test_demo_plan_ring_encloses_fire_and_actual_section_is_not_closed(self):
        self.authorize();marks={m.id.rsplit('-',2)[-2]+'-'+m.id.rsplit('-',1)[-1]:m for m in im.collect(self.s,self.eid)['marks']}
        ring=marks['W-01'].geo_points;actual=marks['W-02'].geo_points
        self.assertEqual(ring[0],ring[-1]);self.assertNotEqual(actual[0],actual[-1])
        def inside(p):
            odd=False
            for a,b in zip(ring,ring[1:]):
                if (a[0]>p[0])!=(b[0]>p[0]) and p[1]<(b[1]-a[1])*(p[0]-a[0])/(b[0]-a[0])+a[1]:odd=not odd
            return odd
        for m in marks.values():
            if m.kind.value == 'fire_area':
                self.assertTrue(all(inside(p) for p in m.geo_points),m.id)
        self.assertEqual('planned',marks['W-01'].detail('belt_status'))
