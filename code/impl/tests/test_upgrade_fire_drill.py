"""Acceptance of the actually built rehearsal, not fixture-only assertions."""
import json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace


class FireDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=os.environ.get('PZ_FIRE_DRILL_ROOT')
        if not root:raise unittest.SkipTest('build the explicit Qinglan rehearsal first')
        cls.root=Path(root)
        from panda_brain.resource_demo import open_state
        cls.state=open_state(cls.root/'data')

    def test_morning_geometry_and_current_roster_are_linked(self):
        from panda_brain.initial_map import bundle,collect
        result=collect(self.state,self.state.active_event_id)
        self.assertTrue(result['enabled']);self.assertEqual(len(result['marks']),22)
        self.assertEqual([self.state.registry.events['fire-001'].lat,self.state.registry.events['fire-001'].lon],bundle()['anchor'])
        first=next(m for m in result['marks'] if m.id.endswith('-T-01'))
        self.assertEqual(first.detail('current_squad_id'),self.state.resource_demo['called_squads'][0])
        self.assertEqual(first.responsible_unit,self.state.ledger.squads[first.detail('current_squad_id')].name)

    def test_two_published_versions_and_old_order_superseded(self):
        from panda_brain.task_release import releases
        from panda_brain.formal_task_feedback import verified
        rows=[(r,v) for r,v in releases(self.state.workspace) if any(t['spec']['title'].startswith('北山林火') for t in v['tasks'])]
        self.assertEqual([v['draft_version'] for r,v in rows],[1,2])
        for task in rows[0][1]['tasks']:
            self.assertEqual(self.state.workspace.orders[task['order_id']].status.value,'superseded')
        for task in rows[1][1]['tasks']:
            self.assertTrue(verified(self.state.workspace,self.state.ledger,task['order_id']))

    def test_water_received_once_and_fire_stays_active(self):
        from panda_brain.material_supply_receipts import history
        rows=history(self.state.workspace,self.state.ledger)
        self.assertEqual(len(rows),1)
        self.assertIn('北山',json.dumps(rows[0][1],ensure_ascii=False))
        self.assertEqual(self.state.registry.events['fire-001'].status,'active')
        self.assertEqual(len(self.state.registry.library),120)

    def test_replaced_orders_are_history_not_current_command_work(self):
        from panda_brain.formal_task_reads import cards
        from panda_brain.formal_task_receipts import published
        from panda_brain.command_sop import queue
        from panda_brain.map_brief import collect
        from panda_brain.intelligence import collect as intelligence_collect
        old={oid for oid in published(self.state.workspace) if self.state.workspace.orders[oid].status.value=='superseded'}
        self.assertEqual(len(old),3)
        self.assertFalse(old & cards(self.state.workspace,self.state.ledger).keys())
        report,marks=intelligence_collect(self.state,self.state.active_event_id)
        from panda_brain.resource_demo import DEPUTY
        token,error=self.state.member_login(DEPUTY,'123456')
        self.assertFalse(error)
        brief=collect(self.state,token,report,marks,[])
        self.assertFalse(old & {item['source'] for item in brief['items']})
        self.assertFalse(old & {item['source'] for item in queue(self.state,brief,[])['rows']})
        from panda_brain.review_flow_view import task_chains
        chains=task_chains(self.state)
        self.assertIn('已被后续版本替代',chains)
        self.assertIn('第2版；当前有效版本',chains)
        from panda_brain.review_sources import records
        self.assertTrue(any(row['kind']=='formal_task_release' and '北山' in row['summary'] for row in records(self.state)))

    def test_nine_steps_and_review_share_the_fire_sources(self):
        from panda_brain.nine_flow import summary
        from panda_brain.review_sources import records
        steps=summary(self.state)
        self.assertEqual(len(steps),9);self.assertTrue(all(x['value'] for x in steps))
        self.assertEqual(steps[2]['value']['result'],'ongoing')
        self.assertEqual(steps[5]['value']['result'],'ongoing')
        self.assertEqual(steps[8]['value']['result'],'handover')
        self.assertTrue(all(not x['stale'] for x in steps))
        rows=records(self.state)
        self.assertTrue(any(r['kind']=='formal_task_release' and '北山' in r['summary'] for r in rows))
        self.assertTrue(any(r['kind']=='workflow_checkpoint' for r in rows))

    def test_review_is_a_fire_report_with_unfinished_items(self):
        reports=list(self.state.review_ledger.reports.values())
        self.assertEqual(len(reports),1)
        report=reports[0]
        self.assertTrue(report.stage_report);self.assertEqual(report.status,'edited')
        for text in ('青岚县北山','通道','第2版','照明','九步'):
            self.assertIn(text,report.content_md)
        self.assertTrue(report.source_snapshot_json)

    def test_scene_loader_rejects_geometry_substitution(self):
        from panda_brain.fire_drill import load_scene
        data=json.loads((self.root/'data/fire-drill-scene.json').read_text(encoding='utf-8'))
        data['records'][0]['points'][0][0]+=.1
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'fire-drill-scene.json').write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaises(ValueError):load_scene(SimpleNamespace(registry_path=path/'registry.json',registry=self.state.registry))

    def test_fireground_opens_at_scene_scale_with_separate_view_preference(self):
        from unittest.mock import patch
        from panda_brain.sandview import _map_payload
        with patch('panda_brain.sandview._map_sources',return_value=({'resource':None,'error':''},{'zoom':8})):
            payload=json.loads(_map_payload(self.state,'commander'))
        self.assertEqual(payload['initialZoom'],13)
        self.assertEqual(payload['viewContext'],'DEMO-QINGLAN-CONTINUOUS')
        self.assertEqual(payload['base'],'satellite')

    def test_all_sources_belong_to_this_isolated_root(self):
        self.assertFalse(self.state.workspace.verify_audit_chain())
        for event in self.state.registry.events.values():
            self.assertTrue(Path(event.snapshot_path).resolve().is_relative_to((self.root/'data').resolve()))
