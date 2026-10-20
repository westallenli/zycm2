"""New drawing appearance round trips; original objects/authority and parallel work remain."""
import tempfile
import unittest
from pathlib import Path
from panda_brain import manual_shapes as ms, sop_guide
from panda_brain.map_annotations import MapAnnotation, AnnotationKind
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.sandview import _mark_payload
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.panel import PanelActionError


class ManualFlowTests(unittest.TestCase):
    def test_old_arrow_and_old_color_are_not_opted_in(self):
        old=MapAnnotation('old',AnnotationKind.ARROW,(),title='旧箭头',geo_points=((35,108),(35.1,108.1)))
        before=dict(old.details)
        self.assertEqual({},ms.presentation(old))
        self.assertEqual({},_mark_payload(old,'commander',tip=False)['manual'])
        self.assertEqual(before,old.details)

    def test_new_shape_style_roundtrips_without_upgrading_legacy_semantics(self):
        ws=ScenarioWorkspace('anonymous-shape-test')
        old=MapAnnotation('old',AnnotationKind.ARROW,(),title='旧对象',geo_points=((35,108),(35.1,108.1)))
        ws.record_annotation(old)
        mark=MapAnnotation('new',AnnotationKind.AREA,(),title='参考圆',geo_points=((35,108),(35.1,108),(35,108.1)),
             details={'manual_style':ms.VERSION,'manual_shape':'circle','manual_color':'purple'})
        ws.record_annotation(mark)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'anonymous.db';save_workspace(ws,path);restored=load_workspace(path)
        self.assertEqual('circle',restored.annotations['new'].detail('manual_shape'))
        payload=_mark_payload(restored.annotations['new'],'commander',tip=False)
        self.assertEqual('圆',payload['kind_word']);self.assertEqual('#8661FF',payload['manual']['fill'])
        self.assertEqual({},ms.presentation(restored.annotations['old']))
        self.assertEqual((),restored.verify_audit_chain())

    def test_profile_cannot_smuggle_type_or_arbitrary_css(self):
        for details in ({'manual_style':'foreign'},
                        {'manual_style':ms.VERSION,'manual_shape':'circle'},
                        {'manual_style':ms.VERSION,'manual_color':'url(javascript:bad)'}):
            with self.assertRaises(ValueError):ms.validate('text',details)

    def test_arrow_profile_uses_separate_fill_and_outline(self):
        m=MapAnnotation('a',AnnotationKind.ARROW,(),title='匿名箭头',geo_points=((35,108),(35.1,108.1)),
             details={'manual_style':ms.VERSION,'manual_shape':'arrow','manual_color':'yellow'})
        style=ms.presentation(m)
        self.assertEqual('#FFD83D',style['fill']);self.assertEqual('#E65A27',style['stroke'])

    def test_new_text_payload_uses_the_chosen_color(self):
        mark=MapAnnotation('label',AnnotationKind.TEXT,(),title='<b>匿名文字</b>',geo_points=((35,108),),
             details={'manual_style':ms.VERSION,'manual_shape':'text','manual_color':'purple'})
        payload=_mark_payload(mark,'commander',tip=False)
        self.assertEqual('#8661FF',payload['color'])
        self.assertEqual('文字',payload['kind_word'])
        self.assertEqual(mark.title,payload['title'])

    def test_all_nine_guides_have_order_actions_owner_and_parallel_access(self):
        fragment='<div data-sop-state="待人工复核"></div>'
        for step in range(9):
            html=sop_guide.guide(step,fragment)
            self.assertIn(f'第 {step+1} 项',html)
            self.assertIn('办理职责',html);self.assertIn('data-sop-work=',html)
            self.assertIn('可并行',html);self.assertIn('不代表完成',html)
            self.assertNotIn(' disabled',html)

    def test_emergency_navigation_has_priority_without_claiming_confirmed_risk(self):
        states=['待人工复核']*9;states[6]='有应急相关记录待核对'
        html=sop_guide.recommendation_html(states)
        self.assertIn('data-sop-start="6"',html)
        self.assertIn('应急救援',html);self.assertNotIn('已确认险情',html)

    def test_creation_rejects_mismatched_style_without_audit_write(self):
        from tests.test_upgrade_initial_map import InitialMapTests
        f=InitialMapTests();f.setUp();self.addCleanup(f.doCleanups)
        version=f.s.workspace.version
        with self.assertRaises(PanelActionError):
            f.s.handle_sand_annotate(kind='route',title='匿名错误样式',points='',geo_text='35,108;35.1,108.1',
                  details={'manual_style':ms.VERSION,'manual_shape':'circle','manual_color':'purple'})
        self.assertEqual(version,f.s.workspace.version)

    def test_style_does_not_satisfy_tactical_verification_fields(self):
        from tests.test_upgrade_initial_map import InitialMapTests
        f=InitialMapTests();f.setUp();self.addCleanup(f.doCleanups)
        version=f.s.workspace.version
        with self.assertRaises(PanelActionError):
            f.s.handle_sand_annotate(kind='main_fireline',title='匿名缺资料火线',points='',geo_text='35,108;35.1,108.1',action='verify',
                  details={'manual_style':ms.VERSION,'manual_color':'red'})
        self.assertEqual(version,f.s.workspace.version)
