"""Relocation must preserve scene scale and strict source/audit boundaries."""
import copy, json, math, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

class QinlingTemplateTests(unittest.TestCase):
    def test_original_template_is_unchanged(self):
        from panda_brain.initial_map import bundle
        from panda_brain.demo_templates import template
        self.assertEqual(template(), bundle())

    def test_relocation_preserves_metre_scale_and_simulation_sources(self):
        from panda_brain.demo_templates import template
        old,new=template(),template('qinling-north-v1')
        self.assertEqual(new['anchor'],[33.945638,108.778381])
        self.assertEqual(len(new['records']),22)
        self.assertIn('秦岭',new['event_name']);self.assertEqual(new['data_kind'],'simulated')
        for a,b in zip(old['records'],new['records']):
            self.assertEqual(a['id'],b['id']);self.assertEqual(a['kind'],b['kind'])
            for p,q in zip(a['points'],b['points']):
                self.assertAlmostEqual(p[0]-old['anchor'][0],q[0]-new['anchor'][0],places=5)
                self.assertAlmostEqual((p[1]-old['anchor'][1])*math.cos(math.radians(old['anchor'][0])),
                    (q[1]-new['anchor'][1])*math.cos(math.radians(new['anchor'][0])),places=5)
        changed=template('qinling-north-v1');changed['records'][0]['points'][0][0]=0
        self.assertNotEqual(template('qinling-north-v1')['records'][0]['points'][0][0],0)

    def test_unknown_template_and_tampered_scene_are_rejected(self):
        from panda_brain.demo_templates import template
        from panda_brain.fire_drill import load_scene,SCHEMA
        from panda_brain import audited_records as ar
        with self.assertRaises(ValueError):template('../../anything')
        original=template('qinling-north-v1');scene=copy.deepcopy(original)
        scene.update(template_id='qinling-north-v1',rehearsal_schema=SCHEMA,origin_digest=ar.digest(original),event_id='fire-001')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);state=SimpleNamespace(registry_path=path/'registry.json',registry=SimpleNamespace(events={'fire-001':object()}))
            (path/'fire-drill-scene.json').write_text(json.dumps(scene),encoding='utf-8')
            load_scene(state);self.assertEqual(state.fire_drill_scene['anchor'],original['anchor'])
            scene['records'][0]['points'][0][0]+=.001
            (path/'fire-drill-scene.json').write_text(json.dumps(scene),encoding='utf-8')
            with self.assertRaises(ValueError):load_scene(state)

class QinlingBuiltTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=os.environ.get('PZ_QINLING_ROOT')
        if not root:raise unittest.SkipTest('requires independent Qinling build')
        from panda_brain.resource_demo import open_state
        cls.root=Path(root);cls.state=open_state(cls.root/'data')

    def test_current_scene_and_existing_workflows_are_coherent(self):
        from panda_brain.initial_map import collect
        from panda_brain.nine_flow import summary
        from panda_brain.material_supply_receipts import history
        state=self.state;event=state.registry.events[state.active_event_id]
        self.assertEqual([event.lat,event.lon],[33.945638,108.778381]);self.assertIn('秦岭',event.name)
        self.assertFalse(state.workspace.verify_audit_chain());self.assertEqual(len(state.registry.library),120)
        scene=collect(state,event.event_id);self.assertTrue(scene['enabled']);self.assertEqual(len(scene['marks']),22)
        self.assertEqual(len(state.workspace.options),4) # original supply plan plus three fire plans
        self.assertEqual(len(summary(state)),9);self.assertEqual(len(history(state.workspace,state.ledger)),1)
        self.assertEqual(event.status,'active')
        for point in state.ledger.resource_metadata['points'].values():self.assertGreater(point['latitude'],33)
        for company in state.ledger.resource_metadata['companies'].values():self.assertGreater(company['latitude'],33)
        for report in state.review_ledger.reports.values():
            self.assertIn('秦岭',report.content_md);self.assertNotIn('青岚县北山',report.content_md)

    def test_full_sixteen_level_scene_bbox_is_available(self):
        from panda_brain.local_satellite import load_installed
        source=load_installed(self.root);scene=self.state.fire_drill_scene
        points=[p for r in scene['records'] for p in r['points']];z=16;n=2**z
        def xy(la,lo):return int((lo+180)/360*n),int((1-math.asinh(math.tan(math.radians(la)))/math.pi)/2*n)
        x0,y1=xy(min(p[0] for p in points),min(p[1] for p in points))
        x1,y0=xy(max(p[0] for p in points),max(p[1] for p in points))
        for x in range(x0,x1+1):
            for y in range(y0,y1+1):self.assertIsNotNone(source.tile(z,x,y),(z,x,y))
