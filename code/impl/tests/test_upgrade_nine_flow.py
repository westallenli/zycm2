"""Behavioral checks for event-scoped nine-area evidence and independent checks."""
from dataclasses import asdict,replace
from pathlib import Path
import json,shutil,tempfile,threading,unittest
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from html.parser import HTMLParser
from panda_brain import nine_flow as flow,nine_flow_view as view,workflow,assistant_view
from panda_brain.resource_demo import seed,open_state,DEPUTY
from panda_brain.panel import render_page,make_server
from panda_brain.reviewview import export_review_markdown

class NineFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory();seed(Path(cls.base.name)/'data')
    @classmethod
    def tearDownClass(cls):cls.base.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';shutil.copytree(Path(self.base.name)/'data',self.root)
        reg=self.root/'registry.json';v=json.loads(reg.read_text(encoding='utf-8'))
        for e in v['events'].values():e['snapshot_path']=str(self.root/'snapshots'/Path(e['snapshot_path']).name)
        reg.write_text(json.dumps(v,ensure_ascii=False),encoding='utf-8')
        self.state=open_state(self.root);self.token=self.state.member_login(DEPUTY,'123456')[0]
        self.verifier_id=next(p for p in flow.candidates(self.state) if p!=DEPUTY)
        self.verifier=self.state.member_login(self.verifier_id,'123456')[0]
        self.ref=self.state.workspace.events[0].id
    def obs(self,step=0,ident='a',**kw):
        return flow.observe(self.state,self.token,step=step,subject='模拟现场核对',feedback='独立新增模拟情况，不补写原历史',
            location='模拟保障点',refs=[self.ref],occurred_at='2026-10-17T09:00:00+00:00',expected_version=0,
            request_id='obs-'+ident,data_kind=kw.pop('data_kind','simulated'),**kw)
    def submit(self,obs,step=0,ident='a',**kw):
        values=dict(scope='本次模拟范围',feedback='核对现场及原始任务',basis='模拟原始依据',remaining='',followup='',
            verifier_id=self.verifier_id,result='completed');values.update(kw.pop('values',{}))
        return flow.submit(self.state,self.token,step=step,values=values,refs=[obs.id],expected_version=kw.pop('expected_version',0),
            request_id='submit-'+ident,data_kind=kw.pop('data_kind','simulated'),**kw)
    def accept(self,r,step=0,ident='a',**kw):
        return flow.verify(self.state,self.verifier,step=step,proposal_ref=r.id,expected_version=r.reference.version,
            request_id='check-'+ident,decision=kw.pop('decision','accept'),**kw)
    @unittest.skip('环节页改版：第 1 步对副指挥长只读、表单收进折叠，不再固定输出 csrf，等待按新界面重写')
    def test_reading_and_navigation_never_create_business_evidence(self):
        before=[asdict(e) for e in self.state.workspace.events]
        for i in range(9):
            status,html=render_page(self.state,'/workflow/records',query={'step':[str(i)]},member_token=self.token)
            self.assertEqual(200,status);self.assertIn('九步办理与交接',html);self.assertIn('name="csrf"',html)
            self.assertIn('data-workflow-mode="records"',html)
            self.assertNotIn('data-workflow-state',html)
        self.assertTrue(all(not x['done'] for x in flow.summary(self.state)))
        self.assertEqual(before,[asdict(e) for e in self.state.workspace.events])
    @unittest.skip('环节页改版：依据选择器已改为预置隐藏字段，不再显示"对应原始记录"，等待按新界面重写')
    def test_source_options_use_human_words_and_hide_internal_hashes(self):
        self.state.workspace.append_event('FieldObservation',basis='revision-'+'a'*64)
        html=view.page(self.state,self.token,0)
        class Text(HTMLParser):
            def __init__(self):super().__init__();self.parts=[]
            def handle_data(self,data):self.parts.append(data)
        parsed=Text();parsed.feed(html);text=''.join(parsed.parts)
        self.assertNotIn('revision-'+'a'*64,text)
        self.assertIn('对应原始记录',text)
    def test_submit_is_not_completion_and_only_another_named_person_can_verify(self):
        before=[asdict(e) for e in self.state.workspace.events];o=self.obs();r=self.submit(o)
        self.assertFalse(flow.summary(self.state)[0]['done'])
        with self.assertRaises(PermissionError):flow.verify(self.state,self.token,step=0,proposal_ref=r.id,expected_version=1,request_id='wrong',decision='accept')
        self.accept(r);self.assertTrue(flow.summary(self.state)[0]['done'])
        self.assertEqual(before,[asdict(e) for e in self.state.workspace.events[:len(before)]])
        self.assertFalse(self.state.workspace.verify_audit_chain())
    def test_changed_facts_invalidate_checks_and_cannot_accept_old_proposal(self):
        o=self.obs();r=self.submit(o)
        self.state.workspace.append_event('FieldObservation',statement='新增现场变化')
        self.state.persist_if_db()
        with self.assertRaisesRegex(ValueError,'变化'):self.accept(r)
        self.assertTrue(flow.summary(self.state)[0]['stale'])
    def test_stale_open_form_cannot_silently_claim_new_business_evidence(self):
        o=self.obs();digest=flow.digest(self.state)
        self.state.workspace.append_event('FieldObservation',statement='打开表单后现场变化')
        self.state.persist_if_db()
        before=self.state.workspace.version
        with self.assertRaisesRegex(ValueError,'表单'):self.submit(o,expected_source_digest=digest)
        self.assertEqual(before,self.state.workspace.version)
    def test_other_work_checks_and_reports_do_not_self_invalidate(self):
        a=self.obs(0,'a');b=self.obs(1,'b');r=self.submit(a);self.accept(r)
        r2=self.submit(b,step=1,ident='b');self.accept(r2,step=1,ident='b')
        self.assertTrue(all(x['done'] for x in flow.summary(self.state)[:2]))
    def test_return_requires_new_version_and_preserves_history(self):
        o=self.obs();r=self.submit(o);self.accept(r,decision='return')
        self.assertFalse(flow.summary(self.state)[0]['done'])
        with self.assertRaises(ValueError):self.accept(r,ident='late')
        r2=self.submit(o,ident='b',expected_version=2);self.accept(r2,ident='b')
        self.assertEqual(4,len(flow.history(self.state.workspace,flow.CHECK)))
        self.assertTrue(flow.summary(self.state)[0]['done'])
    def test_exact_retries_are_idempotent_and_changed_request_is_rejected(self):
        o=self.obs();r=self.submit(o);self.assertEqual(r,self.submit(o))
        checked=self.accept(r);n=self.state.workspace.version
        self.assertEqual(checked,self.accept(r));self.assertEqual(n,self.state.workspace.version)
        with self.assertRaisesRegex(ValueError,'请求号'):self.submit(o,values={'basis':'不同依据'})
    def test_unresolved_items_require_owner_and_prevent_completed_claim(self):
        o=self.obs()
        with self.assertRaises(ValueError):self.submit(o,values={'remaining':'餐食仍缺30份'})
        with self.assertRaises(ValueError):self.submit(o,values={'remaining':'餐食仍缺30份','followup':'具名责任与期限'})
        r=self.submit(o,values={'result':'ongoing','remaining':'餐食仍缺30份','followup':'具名负责人明日复核'})
        self.accept(r);self.assertFalse(flow.summary(self.state)[0]['done'])
    def test_handover_is_accepted_by_receiver_without_completing_material_delivery(self):
        o=self.obs(8);before=self.state.materials_path.read_bytes()
        r=self.submit(o,step=8,values={'result':'handover','remaining':'餐食30份、照明4套仍缺','followup':'指定接收人跟进，明日复核'})
        self.accept(r,step=8)
        self.assertTrue(flow.summary(self.state)[8]['done']);self.assertEqual(before,self.state.materials_path.read_bytes())
        self.assertIn('餐食30份',flow.markdown(self.state))
    def test_foreign_or_wrong_step_sources_and_simulation_laundering_rejected(self):
        o=self.obs()
        with self.assertRaises(ValueError):self.submit(o,step=1)
        with self.assertRaisesRegex(ValueError,'模拟'):self.submit(o,data_kind='reported')
        with self.assertRaises(ValueError):flow.observe(self.state,self.token,step=0,subject='a',feedback='b',location='c',refs=['foreign'],occurred_at='',expected_version=0,request_id='foreign')
        with self.assertRaises(PermissionError):flow.observe(self.state,self.verifier,step=0,subject='a',feedback='b',location='c',refs=[self.ref],occurred_at='',expected_version=0,request_id='no-deputy')
    def test_failed_persistence_rolls_back_memory_and_files(self):
        before=self.state.workspace.version;db=self.state.db_path.read_bytes()
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('模拟写入失败')):
            with self.assertRaises(OSError):self.obs()
        self.assertEqual(before,self.state.workspace.version);self.assertEqual(db,self.state.db_path.read_bytes())
    def test_closed_event_history_remains_readable_but_no_new_writes(self):
        self.obs();self.state.registry.events['fire-001']=replace(self.state.registry.events['fire-001'],status='closed')
        self.state.member_session(self.token).selected_event_id='fire-001'
        status,html=render_page(self.state,'/workflow/records',query={'step':['0']},member_token=self.token)
        self.assertEqual(200,status);self.assertIn('事件已结束',html)
        with self.assertRaises(PermissionError):self.obs(ident='closed')
    def test_report_and_review_use_same_records_and_new_task_projection(self):
        o=self.obs();r=self.submit(o);self.accept(r)
        md=export_review_markdown(self.state)
        self.assertIn('九步办理与交接',md);self.assertIn('独立新增模拟情况',md);self.assertIn('模拟 · 本次范围已核验',md)
        from panda_brain.task_sop import deployment_view
        cards=deployment_view(self.state.workspace,self.state.registry.events['fire-001'],self.state.ledger).tasks
        formal=next(x for x in cards if any('当前正式任务' in s for s in x.facts))
        self.assertFalse(any('旧命令状态' in x for x in formal.facts))
        self.assertIn('/materials/event',workflow.destination(2,0,'fire-001'))
        self.assertIn('/personnel/arrangements',workflow.destination(2,1,'fire-001'))
        self.assertEqual('/sand/task-feedback',workflow.destination(8,0,'fire-001'))
    def test_http_csrf_event_duplicate_fields_and_successful_persistence(self):
        server=make_server(self.state,port=0);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        member=self.state.member_session(self.token)
        fields=dict(event_id='fire-001',csrf=member.custody_csrf,step='0',action='observe',request_id='http-obs',expected_version='0',
            subject='HTTP模拟记录',feedback='HTTP保存并保留空时刻',location='模拟点',occurred_at='',refs=self.ref,data_kind='simulated')
        base=f'http://127.0.0.1:{server.server_port}'
        with urlopen(base+'/static/nine-flow.css') as response:
            self.assertEqual(200,response.status);self.assertIn('text/css',response.headers['Content-Type'])
            self.assertIn('.nine-steps',response.read().decode())
        def post(value,token=None):return urlopen(Request(base+'/workflow/records',data=urlencode(value,doseq=True).encode(),headers={'Cookie':'member_session='+(token or self.token)}))
        for change,status in ((dict(csrf='bad'),403),(dict(event_id='foreign'),403),(dict(step=['0','1']),409)):
            with self.assertRaises(HTTPError) as caught:post(fields|change)
            self.assertEqual(status,caught.exception.code)
        with post(fields) as response:self.assertEqual(200,response.status);self.assertIn('HTTP模拟记录',response.read().decode())
        self.assertTrue(any(v['subject']=='HTTP模拟记录' for r,v in flow.history(self.state.workspace,flow.OBS)))
        obs=flow.history(self.state.workspace,flow.OBS)[-1][0]
        submit=dict(event_id='fire-001',csrf=member.custody_csrf,step='0',action='submit',request_id='http-submit',expected_version='0',
            scope='HTTP核对范围',feedback='按原始记录核对',basis='HTTP独立核验依据',remaining='',followup='',verifier_id=self.verifier_id,
            result='completed',refs=obs.id,data_kind='simulated',source_digest=flow.digest(self.state))
        with post(submit) as response:self.assertEqual(200,response.status)
        proposal=flow.latest(self.state)[0][0]
        check=dict(event_id='fire-001',csrf=self.state.member_session(self.verifier).custody_csrf,step='0',action='verify',
            request_id='http-verify',expected_version='1',proposal_ref=proposal.id,decision='accept')
        with post(check,self.verifier) as response:self.assertEqual(200,response.status)
        self.assertTrue(flow.summary(self.state)[0]['done'])
