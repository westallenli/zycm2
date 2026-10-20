import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from panda_brain.workspace import ScenarioWorkspace, event_hash
from panda_brain.models import Event, PersonRecord
from panda_brain.persistence import save_workspace, load_workspace
from panda_brain.review_replay import replay, render_snapshot, plan_comparisons
from panda_brain.record_contracts import ObjectReference, ObjectRevision, RecordTimes
from panda_brain.map_annotations import MapAnnotation, AnnotationKind
from panda_brain.auth import GeneralEntryAuth
from panda_brain.panel import PanelState, make_server, render_page
from panda_brain.authorization import Role
from panda_brain import review_meeting as meeting
from panda_brain.fire_events import FireEventRecord
from panda_brain.review_domain import event_digest, ReviewIssue


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.ws=ScenarioWorkspace('anonymous',event_id='fire-a')

    def test_new_clock_is_hashed_and_persistent(self):
        self.assertTrue(self.ws.events[0].recorded_at.endswith('+00:00'))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db';save_workspace(self.ws,path)
            loaded=load_workspace(path)
            self.assertEqual([(e.hash,e.recorded_at) for e in self.ws.events],[(e.hash,e.recorded_at) for e in loaded.events])
        self.ws._events[0]=replace(self.ws.events[0],recorded_at='2000-01-01T00:00:00+00:00')
        self.assertEqual((1,),self.ws.verify_audit_chain())

    def test_legacy_database_keeps_no_clock_and_hash(self):
        payload={'legacy':True}
        self.ws._events=[Event('evt-001',1,'Legacy',payload,'0'*64,event_hash(1,'Legacy',payload,'0'*64))]
        from panda_brain.workspace import GENESIS_HASH
        e=self.ws.events[0]
        self.ws._events=[replace(e,prev_hash=GENESIS_HASH,hash=event_hash(1,e.kind,payload,GENESIS_HASH))]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'legacy.db';save_workspace(self.ws,path)
            with sqlite3.connect(path) as c:c.execute('ALTER TABLE events DROP COLUMN recorded_at')
            c.close()
            loaded=load_workspace(path)
            self.assertEqual('',loaded.events[0].recorded_at)
            self.assertEqual((),loaded.verify_audit_chain())
            save_workspace(loaded,path)
            self.assertEqual(self.ws.events[0].hash,load_workspace(path).events[0].hash)
            snap,_=replay(loaded,'fire-a',known_at='2026-01-01T00:00:00+00:00')
            self.assertEqual(1,len(snap['unknown']))

    def test_sequence_never_reads_later_live_values(self):
        self.ws.append_event('OfflineTaskInstructionRecorded',event_id='fire-a',task_id='task-a',content='初版',version=1)
        cut=self.ws.version
        self.ws.append_event('OfflineTaskInstructionAmended',event_id='fire-a',task_id='task-a',content='后来的新版',version=2)
        snap,_=replay(self.ws,'fire-a',sequence=cut)
        text=render_snapshot(snap)
        self.assertIn('初版',text);self.assertNotIn('后来的新版',text)
        self.assertEqual((),replay(self.ws,'fire-a',sequence=0)[1])

    def test_known_time_excludes_late_report_and_preserves_occurrence(self):
        self.ws.append_event('OfflineTaskInstructionRecorded',event_id='fire-a',task_id='late',
            occurred_at='2020-01-01T00:00:00+00:00',content='后来才得知')
        snap,_=replay(self.ws,'fire-a',known_at='2025-01-01T00:00:00+00:00')
        self.assertEqual([],snap['groups']['tasks'])
        self.assertEqual('2020-01-01T00:00:00+00:00',snap['later'][0]['occurred_at'])
        self.assertNotIn('后来才得知',render_snapshot(snap).split('后来录入的补充资料')[0])

    def test_map_uses_immutable_revision_not_current_index(self):
        self.ws.record_annotation(MapAnnotation('map-a',AnnotationKind.WATER,((1.,2.),),'原水源',
            event_id='fire-a',geo_points=((32.,110.),),is_demo=True))
        cut=self.ws.version
        self.ws.update_annotation('map-a',title='后来水源',base_version=1)
        snap,marks=replay(self.ws,'fire-a',sequence=cut)
        self.assertEqual('原水源',marks[0].title)
        self.assertNotIn('后来水源',render_snapshot(snap))
        self.ws.annotations.clear()
        self.assertEqual('原水源',replay(self.ws,'fire-a',sequence=cut)[1][0].title)

    def test_tampered_revision_or_chain_rejected(self):
        record=ObjectRevision(ObjectReference('fire-a','report','r',1),
            RecordTimes(recorded_at='2026-01-01T00:00:00+00:00'),'manual','reported','a','r','{}')
        self.ws.record_object_revision(record,expected_version=0)
        self.ws.object_revisions[record.id]=replace(record,content_json='{"forged":true}')
        with self.assertRaises(ValueError):replay(self.ws,'fire-a')

    def test_scope_cutoff_and_cross_event_rejected(self):
        for kwargs in ({'sequence':True},{'sequence':-1},{'sequence':999},{'known_at':'2026-10-07'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError): replay(self.ws,'fire-a',**kwargs)
        with self.assertRaises(ValueError):replay(self.ws,'fire-b')
        self.ws.append_event('ClaimCreated',event_id='fire-b',claim={'id':'wrong'})
        with self.assertRaises(ValueError):replay(self.ws,'fire-a')

    def test_first_batch_does_not_take_latest_and_bad_duration_unknown(self):
        self.ws.append_event('SquadArrivalReported',arrived_phones=['anonymous']);first=self.ws.version
        self.ws.append_event('SquadArrivalReported',arrived_phones=['anonymous-two'])
        self.ws.append_event('OrderConfirmed');order=self.ws.version
        self.ws.append_event('OrderConfirmed')
        rows={k:v for k,v,_ in event_digest(None,self.ws)}
        self.assertEqual(f'审计序列 #{first}',rows['首批到场报告'])
        self.assertEqual(f'审计序列 #{order}',rows['首批命令'])
        event=FireEventRecord('fire-a','匿名','anon','2026-01-01T00:00:00+08:00','模拟','未知',status='closed',updated_at='2026-01-01T00:00:00')
        self.assertEqual('—',dict((k,v) for k,v,_ in event_digest(event,self.ws))['总时长（发现→关闭）'])

    def test_display_escapes_source_and_keeps_unknown_fields(self):
        self.ws.append_event('EvidenceRecorded',event_id='fire-a',evidence={'id':'e','text':'<script>bad()</script>'})
        text=render_snapshot(replay(self.ws,'fire-a')[0])
        self.assertNotIn('<script>bad()',text);self.assertIn('&lt;script&gt;',text)
        self.assertIn('发生 未记录',text)

    def test_all_business_groups_keep_known_versions_and_original_basis(self):
        fixtures=(('ClaimUpdated','situation'),('SquadArrivalReported','personnel'),
            ('MaterialSupplyReceived','materials'),('OptionRecorded','plans'),
            ('OfflineTaskInstructionRecorded','tasks'),('RadioMessageReceived','evidence'))
        for i,(kind,group) in enumerate(fixtures):
            self.ws.append_event(kind,event_id='fire-a',task_id='anonymous-'+str(i),
                content='当时资料-'+str(i),audio_ref='opaque-anonymous-reference' if group=='evidence' else '')
        cut=self.ws.version
        self.ws.append_event('OfflineTaskInstructionAmended',event_id='fire-a',task_id='anonymous-4',content='后来的资料')
        snap,_=replay(self.ws,'fire-a',sequence=cut)
        for _,group in fixtures:self.assertTrue(snap['groups'][group])
        text=render_snapshot(snap)
        self.assertNotIn('后来的资料',text);self.assertIn('仅引用不证明已取得文件',text)
        latest=replay(self.ws,'fire-a')[0]['groups']['tasks'][0]
        self.assertEqual('当时资料-4',latest['basis'][0]['payload']['content'])

    def test_future_occurrence_is_not_mislabeled_as_later_supplement(self):
        self.ws.append_event('TaskReported',occurred_at='2099-01-01T00:00:00+00:00',content='未来发生')
        snap,_=replay(self.ws,'fire-a',sequence=1)
        self.assertEqual([],snap['later'])


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        auth=GeneralEntryAuth(secret='test-only')
        self.state=PanelState(ScenarioWorkspace('anonymous',event_id='fire-a',general_auth=auth),auth=auth,
            registry_path=root/'registry.json',materials_path=root/'materials.json',reviews_path=root/'reviews.json',db_path=root/'state.db')
        self.state.active_event_id='fire-a'
        self.state.registry.events['fire-a']=FireEventRecord('fire-a','匿名','anon','2026-01-01T00:00:00+00:00','模拟','未知',status='closed')
        self.phones=('13912340001','13912340002','13912340003','13912340004')
        self.state.registry.import_person_library([PersonRecord(name='匿名'+str(i),phone=p,unit='模拟') for i,p in enumerate(self.phones)])
        self.state.configure_duty_subjects((self.phones[0],))
        self.tokens=[self.state.member_login(p,'123456')[0] for p in self.phones]
        meeting.service(self.state).record_initial_appointment(self.tokens[0],event_id='fire-a',role=Role.DEPUTY,
            subject_id=self.phones[1],evidence='匿名依据',request_id='deputy')
        self.state.handle_review_owner_assign(self.phones[2],deputy_token=self.tokens[1],evidence='匿名会议指定',request_id='owner')

    def fields(self,**changes):
        args=dict(subject='匿名核实事项',provided_by='模拟队长（提供事实）',feedback='仅报告，未独立核实',
            evidence_refs='evt-001',occurred_at='2026-01-01T00:00:00+00:00',request_id='meeting-one')
        args.update(changes);return args

    def test_only_named_deputy_can_assign_including_closed_event(self):
        self.assertEqual(self.phones[2],meeting.owner(self.state))
        for token in (None,self.tokens[0],self.tokens[2],self.tokens[3]):
            with self.subTest(token=token),self.assertRaises(PermissionError):
                self.state.handle_review_owner_assign(self.phones[3],deputy_token=token,evidence='x',request_id='wrong')

    def test_old_global_owner_has_no_bound_authority_or_cross_event(self):
        self.state.review_ledger.owner_phone=self.phones[3]
        self.assertFalse(meeting.is_owner(self.state,self.tokens[3]))
        self.assertTrue(meeting.is_owner(self.state,self.tokens[2]))
        self.state.active_event_id='fire-b'
        self.assertFalse(meeting.is_owner(self.state,self.tokens[2]))

    def test_single_operator_note_has_provenance_and_retry(self):
        before=self.state.workspace.version
        note=meeting.record_note(self.state,self.tokens[2],self.fields())
        self.assertEqual(before+1,self.state.workspace.version)
        self.assertEqual(self.phones[2],note.actor_id)
        self.assertEqual('reported',note.data_kind)
        self.assertEqual(note,meeting.record_note(self.state,self.tokens[2],self.fields()))
        self.assertEqual(note,load_workspace(self.state.db_path).object_revisions[note.id])
        snap,_=replay(self.state.workspace,'fire-a',sequence=before)
        self.assertNotIn('仅报告，未独立核实',render_snapshot(snap).split('后来录入的补充资料')[0])
        with self.assertRaises(ValueError):meeting.record_note(self.state,self.tokens[2],self.fields(feedback='改写'))

    def test_explicit_plan_actual_difference_and_late_plan_warning(self):
        plan=meeting.record_note(self.state,self.tokens[2],self.fields(record_type='plan',
            planned_at='2026-01-01T01:00:00+00:00',request_id='plan-one'))
        before=self.state.workspace.version
        meeting.record_note(self.state,self.tokens[2],self.fields(record_type='actual',plan_reference=plan.id,
            occurred_at='2026-01-01T01:05:00+00:00',request_id='actual-one'))
        old=replay(self.state.workspace,'fire-a',sequence=before)[0]
        self.assertIn('实际缺失',plan_comparisons(old)[0])
        rows=plan_comparisons(replay(self.state.workspace,'fire-a')[0])
        self.assertIn('+5 分钟',rows[0]);self.assertIn('不当作当时已知',rows[0]);self.assertIn('不等同责任',rows[0])
        with self.assertRaises(ValueError):meeting.record_note(self.state,self.tokens[2],self.fields(record_type='actual',plan_reference='foreign',request_id='bad'))

    def test_plan_needs_original_evidence_and_actual_occurrence(self):
        before=self.state.workspace.events
        for changes in (dict(record_type='plan',planned_at='2026-01-01T01:00:00'),
                        dict(record_type='plan',planned_at='2026-01-01T01:00:00+00:00',evidence_refs=''),
                        dict(record_type='actual',occurred_at='',plan_reference='unknown')):
            with self.assertRaises(ValueError):meeting.record_note(self.state,self.tokens[2],self.fields(**changes))
        self.assertEqual(before,self.state.workspace.events)

    def test_note_permission_failed_save_and_invalid_refs_have_no_side_effect(self):
        before=self.state.workspace.events
        for token in (None,self.tokens[1],self.tokens[3]):
            with self.assertRaises(PermissionError):meeting.record_note(self.state,token,self.fields())
        with self.assertRaises(ValueError):meeting.record_note(self.state,self.tokens[2],self.fields(evidence_refs='not-here'))
        with patch('panda_brain.persistence.save_workspace',side_effect=OSError('模拟保存失败')):
            with self.assertRaises(OSError):meeting.record_note(self.state,self.tokens[2],self.fields())
        self.assertEqual(before,self.state.workspace.events)

    def test_scoped_ledger_never_shows_other_event_or_trends(self):
        self.state.review_ledger.add_issue(ReviewIssue('foreign','不得泄漏','x','x','fire-b','x','x'))
        page=render_page(self.state,'/review',member_token=self.tokens[2])[1]
        self.assertNotIn('不得泄漏',page);self.assertNotIn('href="/review?tab=trend"',page)
        self.assertNotIn('href="/review?tab=common"',page);self.assertIn('补充现场情况',page)

    def test_form_binds_session_event_and_version(self):
        fields={'csrf':[meeting.csrf(self.state,self.tokens[2])],'event_id':['fire-a'],'expected_version':[str(self.state.workspace.version)]}
        meeting.validate_form(self.state,self.tokens[2],fields)
        for changed in ({'event_id':['fire-b']},{'csrf':['forged']},{'expected_version':['0']},{'event_id':['fire-a','fire-b']}):
            with self.assertRaises((PermissionError,ValueError)):meeting.validate_form(self.state,self.tokens[2],{**fields,**changed})

    def test_logout_revokes_owner_and_direct_global_unlock_never_acts(self):
        self.state.login('test-only')
        with self.assertRaises(PermissionError):self.state.handle_review_generate()
        self.state.member_logout(self.tokens[2]);self.assertFalse(meeting.is_owner(self.state,self.tokens[2]))

    def test_replay_http_scope_and_no_mutation(self):
        server=make_server(self.state,port=0);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}/review/replay?'
        before=self.state.workspace.events
        request=Request(url+urlencode({'event_id':'fire-a','sequence':1}),headers={'Cookie':'member_session='+self.tokens[2]})
        with urlopen(request,timeout=5) as result:self.assertEqual(1,json.load(result)['sequence'])
        for event,token in (('fire-b',self.tokens[2]),('fire-a',self.tokens[3])):
            with self.assertRaises(HTTPError):urlopen(Request(url+urlencode({'event_id':event}),headers={'Cookie':'member_session='+token}),timeout=5)
        self.assertEqual(before,self.state.workspace.events)

    def test_owner_dual_cookie_http_form_scope_and_export(self):
        server=make_server(self.state,port=0);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        commander=self.state.login('test-only')
        cookie='member_session='+self.tokens[2]+'; panel_session='+commander
        def request(route,fields=None):
            req=Request(base+route,data=urlencode(fields).encode() if fields is not None else None,headers={'Cookie':cookie})
            try:
                with urlopen(req,timeout=5) as result:return result.status,result.read().decode()
            except HTTPError as exc:return exc.code,exc.read().decode()
        status,page=request('/review?tab=timeline')
        self.assertEqual(200,status);self.assertIn('补充现场情况',page)
        form={**self.fields(),'csrf':meeting.csrf(self.state,self.tokens[2]),'event_id':'fire-a',
              'expected_version':str(self.state.workspace.version)}
        before=self.state.workspace.events
        for changed in ({'csrf':'wrong'},{'event_id':'fire-b'},{'expected_version':'0'}):
            self.assertIn(request('/review/meeting',{**form,**changed})[0],(403,409))
        self.assertEqual(before,self.state.workspace.events)
        self.state.review_ledger.add_issue(ReviewIssue('foreign-related','他事件问题','x','x','fire-b','x','x'))
        self.assertEqual(409,request('/review/hyp-new',{**form,'related_issues':'foreign-related','statement':'匿名假设'})[0])
        self.assertEqual(before,self.state.workspace.events)
        self.assertEqual(200,request('/review/meeting',form)[0])
        self.assertEqual(200,request('/export/review')[0])
        self.assertEqual(404,request('/review?projector=1')[0])
        self.assertIsNone(self.state._pending_review_actor)

    def test_corrupt_audit_blocks_owner_read_and_export(self):
        self.state.workspace._events[0]=replace(self.state.workspace.events[0],recorded_at='2000-01-01T00:00:00+00:00')
        self.assertFalse(meeting.is_owner(self.state,self.tokens[2]))
        with self.assertRaises(ValueError):meeting.service(self.state)
        token=self.state.login('test-only')
        self.assertEqual(409,render_page(self.state,'/review',session_token=token)[0])

    def test_owner_appointment_survives_reload_without_global_owner(self):
        self.state.workspace=load_workspace(self.state.db_path)
        self.state.review_ledger.owner_phone=''
        self.assertTrue(meeting.is_owner(self.state,self.tokens[2]))
        self.assertEqual(self.phones[2],meeting.owner(self.state))

    def test_review_mirror_updates_separate_event_snapshot(self):
        target=Path(self.tmp.name)/'event-snapshot.db'
        save_workspace(self.state.workspace,target)
        self.state.registry.events['fire-a']=replace(self.state.registry.events['fire-a'],snapshot_path=str(target))
        self.state._mirror_review('ReviewIssueCreated',issue_id='anonymous-review',content='匿名候选待核')
        loaded=load_workspace(target)
        self.assertEqual([(e.hash,e.recorded_at) for e in self.state.workspace.events],
                         [(e.hash,e.recorded_at) for e in loaded.events])
        self.assertEqual((),loaded.verify_audit_chain())
        self.assertEqual('fire-a',loaded.events[-1].payload['event_id'])
