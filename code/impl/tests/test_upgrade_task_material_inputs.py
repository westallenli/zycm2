"""Named task input reports never manufacture professional shortage evidence."""
from dataclasses import asdict, replace
from pathlib import Path
import unittest
import json
import threading
import urllib.request
import urllib.parse
import urllib.error
from unittest.mock import patch

from panda_brain.material_gap_evidence import assess_gap_inputs
from panda_brain.models import ExecutionOrder, OrderStatus
from panda_brain import tactical_map as tm
from panda_brain.persistence import save_workspace
from panda_brain.panel import render_page, PanelActionError, make_server
from . import test_upgrade_manual_material_transfers as fixture


class TaskMaterialInputTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space','setUp'):
        locals()[name]=getattr(fixture.NamedTransferTests,name)

    def task(self):
        space=self.event_space()
        space.orders['task-1']=ExecutionOrder('task-1','decision','option','匿名任务',
            status=OrderStatus.CONFIRMED,confirmed_by=self.DEPUTY)
        flow=tm.CommandFlow(order_id='task-1',event_id=self.event_id,content_version=1,target='匿名任务')
        space.command_flows['task-1']=flow
        space.append_event('CommandFlowOpened',order_id='task-1',event_id=self.event_id,flow=asdict(flow))
        save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)

    def view(self):
        return self.state.task_material_inputs(self.event_id,member_token=self.tokens[self.DEPUTY],
            order_id='task-1',item_id=self.item_id)

    def report(self,**changes):
        view=self.view()
        args=dict(member_token=self.tokens[self.DEPUTY],order_id='task-1',item_id=self.item_id,
            expected_version=view.version,expected_source_digest=view.source_digest,
            material_relation_source='匿名任务物资对应报告',force_count=2,force_source='匿名投入报告',
            duration_hours=3,duration_source='匿名预计作业时长报告',demand_quantity=4,
            projected_consumption=6,usage_source='匿名演练用量',usage_kind='simulated',
            evidence='匿名补录依据',occurred_at='',reason='再次核对' if view.version else '',request_id='inputs-1')
        return self.state.handle_task_material_inputs(self.event_id,**(args|changes))

    def files(self):
        return {p:p.read_bytes() for p in (self.state.materials_path,self.state.registry_path,
            Path(self.state.registry.events[self.event_id].snapshot_path))}

    def test_current_version_inputs_reach_gap_card_without_formal_number(self):
        self.task(); before=self.files(); record=self.report()
        space=self.event_space()
        result=assess_gap_inputs(space,self.state.ledger,event_id=self.event_id,
            order_id='task-1',item_id=self.item_id)
        self.assertEqual(record.id,result.input_ref)
        self.assertEqual((2,3),(result.force_count,result.duration_hours))
        for text in ('本版任务与该物资的需求对应关系','本版任务实际投入力量及来源','本版任务预计作业时长及来源'):
            self.assertNotIn(text,result.missing)
        self.assertIn('经专业核定的物资用量依据',result.missing)
        self.assertIsNone(result.current_gap); self.assertIsNone(result.projected_gap)
        self.assertEqual(before[self.state.materials_path],self.state.materials_path.read_bytes())
        self.assertEqual(before[self.state.registry_path],self.state.registry_path.read_bytes())
        self.assertEqual(('current',1),(self.view().state,self.view().version))

    def test_unknown_and_explicit_zero_survive_restart(self):
        self.task()
        self.report(force_count=None,force_source='',duration_hours=None,duration_source='',
            demand_quantity=None,projected_consumption=None,usage_source='',usage_kind='unreviewed')
        self.report(force_count=0,duration_hours=0,demand_quantity=0,projected_consumption=0,
            request_id='inputs-2')
        self.state=self.new_state(); self.tokens[self.DEPUTY],_=self.state.member_login(self.DEPUTY,'123456')
        import json
        values=[json.loads(r.content_json) for r in self.view().records]
        self.assertIsNone(values[0]['force_count']); self.assertEqual(0,values[1]['force_count'])
        self.assertEqual(2,self.view().version)

    def test_replay_has_no_write_and_request_collision_is_rejected(self):
        self.task(); self.report(); before=self.files()
        self.report(expected_version=0,reason='',request_id='inputs-1')
        self.assertEqual(before,self.files())
        with self.assertRaises(ValueError): self.report(expected_version=0,reason='',force_count=3)

    def test_task_revision_requires_new_input_report(self):
        self.task(); self.report(); space=self.event_space()
        flow=space.command_flows['task-1']; flow.content_version=2; flow.target='已改版任务'
        space.append_event('CommandFlowChanged',order_id='task-1',event_id=self.event_id,flow=asdict(flow))
        save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)
        self.assertEqual('needs_reassessment',self.view().state)
        result=assess_gap_inputs(space,self.state.ledger,event_id=self.event_id,order_id='task-1',item_id=self.item_id)
        self.assertIsNone(result.force_count)
        self.report(request_id='inputs-2')
        self.assertEqual('current',self.view().state)

    def test_inventory_change_invalidates_report_and_stale_form(self):
        self.task(); self.report(); old=self.view(); before=self.files()
        self.state.handle_manual_material_loss(self.event_id,captain_token=self.tokens[self.SOURCE],
            squad_id=self.source_squad,item_id=self.item_id,loss_id='loss-k02',kind='consumed',
            amount='1',expected_version='0',evidence='匿名耗损',occurred_at='',reason='',request_id='loss-k02')
        self.assertEqual('needs_reassessment',self.view().state)
        with self.assertRaises(ValueError): self.report(expected_source_digest=old.source_digest,request_id='inputs-2')
        self.assertNotEqual(before,self.files())
        self.report(request_id='inputs-2'); self.assertEqual('current',self.view().state)

    def test_invalid_numbers_sources_and_professional_claim_rejected(self):
        self.task(); before=self.files()
        for changes in ({'force_count':True},{'force_count':-1},{'force_count':1.5},
                {'duration_hours':float('nan')},{'demand_quantity':float('inf')},
                {'duration_hours':-1},{'material_relation_source':''},{'force_source':''},
                {'duration_source':''},{'usage_source':''},{'usage_kind':'professional_verified'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.report(**changes)
        self.assertEqual(before,self.files())

    def test_wrong_actor_and_closed_event_cannot_record(self):
        self.task(); before=self.files()
        for phone in (self.DUTY,self.SOURCE,self.OTHER):
            with self.assertRaises(PermissionError): self.report(member_token=self.tokens[phone])
        self.assertEqual(before,self.files())
        event=self.state.registry.events[self.event_id]
        self.state.registry.events[self.event_id]=replace(event,status='closed')
        with self.assertRaises((ValueError,PanelActionError)): self.report()

    def test_save_failure_keeps_original_snapshot_and_retry_succeeds(self):
        self.task(); before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('模拟写入失败')):
            with self.assertRaises(OSError): self.report()
        self.assertEqual(before,self.files()); self.report(); self.assertEqual(1,self.view().version)

    def test_orphan_or_changed_record_rejected_without_fallback(self):
        self.task(); record=self.report(); space=self.event_space()
        space.object_revisions.pop(record.id)
        save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)
        with self.assertRaises(ValueError): self.view()

    def test_deputy_page_shows_inputs_and_commander_has_no_write_route(self):
        self.task()
        status,html=render_page(self.state,'/task-materials',member_token=self.tokens[self.DEPUTY],
            query={'event_id':[self.event_id],'order_id':['task-1'],'item_id':[self.item_id]})
        self.assertEqual(200,status); self.assertIn('任务物资输入报告',html)
        self.assertIn('class="on" href="/task-materials?event_id='+self.event_id+'"',html)
        self.assertIn('material_relation_source',html)
        status,_=render_page(self.state,'/task-materials',session_token=self.state.login('p08-transfer-demo'))
        self.assertEqual(404,status)

    def test_corrupt_source_shape_hides_current_input_and_keeps_page_readable(self):
        from .test_upgrade_material_inventory_projection import rehash
        self.task(); record=self.report(); space=self.event_space()
        content=json.loads(record.content_json); content['source']['flow']=[]
        bad=replace(record,content_json=json.dumps(content))
        space.object_revisions[record.id]=bad
        for event in space.events:
            if event.payload.get('revision_id')==record.id:
                event.payload['revision']=asdict(bad)
        rehash(space); save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)
        with self.assertRaises(ValueError): self.view()
        result=assess_gap_inputs(space,self.state.ledger,event_id=self.event_id,order_id='task-1',item_id=self.item_id)
        self.assertIsNone(result.input_ref); self.assertIn('任务物资输入来源待核对',result.missing)

    def server(self):
        server=make_server(self.state,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(thread.join); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def form(self,**changes):
        return dict(event_id=self.event_id,order_id='task-1',item_id=self.item_id,
            expected_version='0',expected_source_digest=self.view().source_digest,
            csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf,
            material_relation_source='匿名物资对应',force_count='2',force_source='匿名实际投入报告',
            duration_hours='3',duration_source='匿名时长报告',demand_quantity='4',
            projected_consumption='6',usage_kind='simulated',usage_source='匿名模拟用量',
            evidence='匿名HTTP报告',occurred_at='',reason='',request_id='http-inputs-1')|changes

    def post(self,base,form,*,actor=None,raw=None,content_type='application/x-www-form-urlencoded'):
        request=urllib.request.Request(base+'/task-materials/report',
            data=urllib.parse.urlencode(form,doseq=True).encode() if raw is None else raw,
            headers={'Cookie':'member_session='+self.tokens[actor or self.DEPUTY],'Content-Type':content_type})
        try: response=urllib.request.urlopen(request,timeout=10)
        except urllib.error.HTTPError as exc: response=exc
        with response: return response.status,response.read().decode('utf-8')

    def test_http_submit_replay_and_correction_preserve_stock(self):
        self.task(); base=self.server(); form=self.form(); before=self.files()
        status,html=self.post(base,form)
        self.assertEqual(200,status); self.assertIn('报告版 1',html); self.assertIn('模拟用量',html)
        after=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('重放不应写入')):
            replay_status,replay_html=self.post(base,form)
        self.assertEqual(200,replay_status); self.assertIn('报告版 1',replay_html)
        self.assertEqual(1,self.view().version); self.assertEqual(after,self.files())
        self.assertEqual(200,self.post(base,self.form(expected_version='1',request_id='http-inputs-2',
            reason='匿名更正',force_count='0',duration_hours=''))[0])
        self.assertEqual(2,self.view().version)
        self.assertEqual(before[self.state.materials_path],self.state.materials_path.read_bytes())

    def test_http_forgery_ambiguity_bad_number_encoding_type_and_size_write_nothing(self):
        self.task(); base=self.server(); form=self.form(); before=self.files()
        for actor in (self.DUTY,self.SOURCE,self.OTHER):
            self.assertEqual(403,self.post(base,form,actor=actor)[0])
        for status,changes in ((403,{'csrf':'wrong'}),(409,{'item_id':[self.item_id,self.item_id]}),
                (409,{'role':'professional'}),(409,{'force_count':'2.5'}),(409,{'duration_hours':'nan'}),
                (409,{'expected_source_digest':'stale'}),(409,{'usage_kind':'professional_verified'})):
            with self.subTest(changes=changes): self.assertEqual(status,self.post(base,form|changes)[0])
        self.assertEqual(409,self.post(base,form,raw=b'\xff')[0])
        self.assertEqual(409,self.post(base,form,content_type='application/json')[0])
        self.assertEqual(413,self.post(base,form,raw=b'x'*8193)[0])
        self.assertEqual(before,self.files())

    def test_http_save_failure_returns_retryable_error_without_partial_report(self):
        self.task(); base=self.server(); form=self.form(); before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('模拟失败')):
            self.assertEqual(503,self.post(base,form)[0])
        self.assertEqual(before,self.files()); self.assertEqual(200,self.post(base,form)[0])

    def test_interrupted_save_blocks_current_writer_and_restart_recovers(self):
        class Interrupted(BaseException): pass
        self.task(); before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=Interrupted('模拟中断')):
            with self.assertRaises(Interrupted): self.report()
        with self.assertRaises(RuntimeError): self.report()
        self.state=self.new_state(); self.tokens[self.DEPUTY],_=self.state.member_login(self.DEPUTY,'123456')
        self.assertEqual(before,self.files()); self.report(); self.assertEqual(1,self.view().version)


if __name__=='__main__': unittest.main()
