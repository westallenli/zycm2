import copy
import http.client
import json
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from panda_brain.simulation_panel import create_simulation_state, EVENT_ID, SimulationConflict
from panda_brain.source_channels import channel_history, channel_state
from panda_brain.live_context import event_context
from panda_brain.persistence import load_workspace
from panda_brain.panel import make_server, render_page
from tests import test_upgrade_simulation_panel as fixture
from tests.test_upgrade_source_channels import packet
from tests import test_upgrade_manual_material_reports as materials_fixture
from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.scenario_driver import ScenarioDriver
from panda_brain.map_annotations import MapAnnotation, AnnotationKind
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.offline_tasks import record_offline, record_feedback, verify_close
from panda_brain.radio_reports import record_simulated_call
from types import SimpleNamespace


class LiveIntegrationTests(unittest.TestCase):
    setUp = fixture.SimulationPanelTests.setUp
    fields = fixture.SimulationPanelTests.fields
    control = fixture.SimulationPanelTests.control

    def push_fields(self, **changes):
        fields = dict(csrf=self.state.model_session(self.token).csrf, event_id=EVENT_ID,
            expected_version=str(self.state.workspace.version), packet=json.dumps(packet(EVENT_ID)))
        fields.update(changes)
        return fields

    def test_push_is_immediate_while_paused_and_keeps_pending_raw_clocks(self):
        before = self.host.view()
        result = self.host.receive_channel_packet(self.push_fields(), session_token=self.token)
        after = self.host.view()
        self.assertFalse(after['playing'])
        self.assertEqual(before['cursor'],after['cursor'])
        self.assertEqual('simulated',result['report']['data_kind'])
        context,_ = event_context(self.state,EVENT_ID)
        self.assertIn('2026-10-07T10:00:00+08:00',context['groups']['channels'][0]['text'])
        self.assertEqual({},self.state.workspace.orders)
        self.assertEqual({},self.state.workspace.verifications)

    def test_push_auth_stale_binding_and_save_failure_are_zero_write(self):
        before = self.state.workspace
        for changes,token in (({},None),(dict(csrf='forged'),self.token),(dict(event_id='other'),self.token)):
            with self.assertRaises(PermissionError):
                self.host.receive_channel_packet(self.push_fields(**changes),session_token=token)
        with self.assertRaises(SimulationConflict):
            self.host.receive_channel_packet(self.push_fields(expected_version='-1'),session_token=self.token)
        with patch('panda_brain.simulation_panel.save_workspace',side_effect=OSError('anonymous')):
            with self.assertRaises(SimulationConflict):
                self.host.receive_channel_packet(self.push_fields(),session_token=self.token)
        self.assertIs(before,self.state.workspace)
        self.assertEqual([],channel_history(load_workspace(self.host.snapshot),EVENT_ID))

    def test_push_retry_is_idempotent_and_restart_retains_first_receipt(self):
        first = self.host.receive_channel_packet(self.push_fields(),session_token=self.token)
        self.host.receive_channel_packet(self.push_fields(),session_token=self.token)
        self.assertEqual(first['revision'],self.state.workspace.version)
        restored = create_simulation_state(self.root,secret='anonymous-test-only',clock=lambda:self.now,_isolated_test=True)
        self.assertEqual(first['report'],channel_state(restored.workspace,EVENT_ID)[0])

    def test_optin_channel_story_preserves_old_roots_and_restart_cursor(self):
        with self.assertRaises(ValueError):
            create_simulation_state(self.root,secret='unused',source_channels=True,_isolated_test=True)
        root = self.root.parent/'linked'
        self.state = create_simulation_state(root,secret='anonymous-test-only',clock=lambda:self.now,
                                             source_channels=True,_isolated_test=True)
        self.host = self.state.simulation_host
        self.token = self.state.login('anonymous-test-only')
        for i in range(3): self.control(request_id=f'linked-step-{i:04d}')
        self.assertEqual(6,len(channel_history(self.state.workspace,EVENT_ID)))
        camera = next(r for r in channel_state(self.state.workspace,EVENT_ID) if r['record']['channel_kind']=='camera')
        self.assertEqual('disconnected',camera['record']['state'])
        original = channel_history(self.state.workspace,EVENT_ID)
        restored = create_simulation_state(root,secret='anonymous-test-only',clock=lambda:self.now,_isolated_test=True)
        self.assertEqual(3,restored.simulation_host.view()['cursor'])
        self.assertEqual(original,channel_history(restored.workspace,EVENT_ID))
        self.assertFalse(restored.simulation_host.view()['playing'])
        self.assertEqual({},restored.workspace.annotations)

    def test_http_live_read_and_channel_push_enforce_role_event_size_and_duplicate_keys(self):
        server = make_server(self.state,port=0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(path,fields=None,authenticated=True):
            con = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            con.request('POST' if fields is not None else 'GET',path,
                body=urlencode(fields,doseq=True) if fields is not None else None,
                headers={'Cookie':'panel_session='+self.token if authenticated else '',
                         'Content-Type':'application/x-www-form-urlencoded'})
            response=con.getresponse()
            body=json.loads(response.read())
            con.close()
            return response.status,body
        path=f'/sand/live-context?event_id={EVENT_ID}'
        before=self.state.workspace.version
        self.assertEqual(403,request(path,authenticated=False)[0])
        self.assertEqual(404,request(path+'&event_id='+EVENT_ID)[0])
        code,data=request(path)
        self.assertEqual(200,code)
        self.assertEqual(6,len(data['group_html']))
        self.assertEqual(before,self.state.workspace.version)
        fields=self.push_fields()
        self.assertEqual(403,request('/sand/channel',fields,False)[0])
        self.assertEqual(413,request('/sand/channel',dict(big='x'*33000))[0])
        self.assertEqual(422,request('/sand/channel',dict(fields,event_id=[EVENT_ID,EVENT_ID]))[0])
        self.assertEqual(200,request('/sand/channel',fields)[0])
        self.assertNotEqual(data['fingerprint'],request(path)[1]['fingerprint'])
        self.assertEqual(0,self.host.view()['cursor'])

    def test_live_question_uses_local_citations_and_public_has_no_business_projection(self):
        self.host.receive_channel_packet(self.push_fields(),session_token=self.token)
        answer=self.state.handle_grounded_sand_ask('联动摄像头、任务和物资',role='commander',session_token=self.token)
        self.assertEqual('live_event',answer.topic)
        self.assertIn('未实测',answer.conclusion)
        self.assertTrue(answer.sources[0].startswith('live-state:'))
        _,public=render_page(self.state,'/sand',session_token=self.token,query={'projector':['1']})
        self.assertNotIn('data-live-kind',public)
        self.assertNotIn('anonymous-camera',public)

    def test_one_event_numeric_map_task_personnel_material_radio_and_manual_close(self):
        f=materials_fixture.ManualMaterialReportTests('test_named_report_correction_and_read_only_display')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.arrive()
        f.report()
        ws=f.event_space()
        state=SimpleNamespace(workspace=ws,registry=f.state.registry,ledger=f.state.ledger,
                              active_event_id=f.event_id)
        store=ObservationStore()
        driver=ScenarioDriver(ws,store,IntakeBridge(store),())
        row=dict(event_id=f.event_id,source_record_id='wind-one-event',source_version=1,
            site_id='anonymous',metric='wind_speed_m_s',value=8,unit='m/s',
            observed_at='2026-09-29T09:20:00+08:00',received_at='2026-09-29T09:20:05+08:00',
            quality_note='匿名模拟',coordinate_system='unknown')
        driver.load_source_sequence([row],source='anonymous',data_kind='simulated')
        driver.step()
        ws.record_annotation(MapAnnotation(id='anon-water',kind=AnnotationKind.WATER,title='匿名水源待核',
            event_id=f.event_id,points=((10.,20.),),source_ref=('wind-one-event',)))
        receive = __import__('panda_brain.source_channels',fromlist=['receive_channel']).receive_channel
        receive(ws,packet(f.event_id,targets=[dict(kind='map',id='anon-water')]),
                source='anonymous-camera',data_kind='simulated')
        auth=EventAuthorizationService(ws,resolve_subject=lambda token:token,subject_exists=lambda person:person in f.tokens)
        task=record_offline(ws,auth,f.DEPUTY,event_id=f.event_id,content='匿名线下已下达：巡查',
            group_id=f.squad_id,issued_by='匿名现场指挥员',recipient_id=f.CAPTAIN,verifier_id=f.DEPUTY,
            occurred_at='2026-09-29T09:00:00+08:00',evidence='匿名对讲记录',request_id='linked-task')
        record_simulated_call(ws,event_id=f.event_id,source_call_id='anonymous-call',source_ref='anonymous-record',
            occurred_at='2026-09-29T09:30:00+08:00',channel_id='anonymous-channel',
            transcript='匿名队长：进度待核',task_ids=(task['task_id'],))
        initial,_=event_context(state,f.event_id)
        self.assertTrue(all(initial['groups'][key] for key in initial['groups']))
        self.assertIn('来源证据',initial['groups']['maps'][0]['text'])
        material_text=json.dumps(initial['groups']['materials'],ensure_ascii=False)
        self.assertIn('模拟水带 3 盘',material_text)
        self.assertIn('接收核实尚无完整记录',material_text)
        for action in ('received','started','completed'):
            record_feedback(ws,lambda token:token,f.CAPTAIN,event_id=f.event_id,task_id=task['task_id'],
                expected_version=1,action=action,basis='匿名本人回报',request_id='linked-'+action,
                is_current_leader=lambda group,phone:group==f.squad_id and phone==f.CAPTAIN)
        pending,_=event_context(state,f.event_id)
        self.assertIn('待核验',pending['groups']['tasks'][0]['text'])
        verify_close(ws,lambda token:token,f.DEPUTY,event_id=f.event_id,task_id=task['task_id'],
            expected_version=1,evidence='匿名独立检查',overall_goal_evidence='匿名目标核查',
            request_id='linked-close',subject_exists=lambda person:person in f.tokens)
        closed,_=event_context(state,f.event_id)
        self.assertIn('指定人核验关闭',closed['groups']['tasks'][0]['text'])
        self.assertEqual(initial['groups']['materials'],closed['groups']['materials'])
        self.assertEqual({},ws.orders)
        self.assertEqual({},ws.verifications)
        self.assertEqual((),ws.verify_audit_chain())
