"""T20: one anonymous event; each stage consumes the preceding outputs."""
from copy import deepcopy
from pathlib import Path
import json
import http.client
import threading
import unittest

from panda_brain import review_meeting as meeting, review_causality as causal, review_closure as closure
from panda_brain import review_reports, review_exports, radio_reports, radio_task_candidates
from panda_brain import durable_storage, offline_tasks
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState, PanelActionError, make_server
from panda_brain.persistence import load_workspace
from panda_brain.workspace import ScenarioWorkspace
from panda_brain.material_transfers import transfer_in_transit, transfer_balance
from . import test_upgrade_checkin_flow as catalog


class FullEventTests(unittest.TestCase):
    DUTY=catalog.CheckinFlowTests.DUTY
    DEPUTY=catalog.CheckinFlowTests.DEPUTY
    CAPTAIN=catalog.CheckinFlowTests.CAPTAIN
    MEMBER_A=catalog.CheckinFlowTests.MEMBER_A
    MEMBER_B=catalog.CheckinFlowTests.MEMBER_B
    TEMP=catalog.CheckinFlowTests.TEMP
    SECOND='13999995007'
    OWNER='13999995008'

    def setUp(self):
        # Only identity/catalogue setup is reused. There are no event fixtures.
        catalog.CheckinFlowTests.setUp(self)
        self.root=self.state.registry_path.parent
        self.state.db_path=self.root/'active.db'
        self.state.imagery_path=self.root/'imagery.json'
        self.state.imports_path=self.root/'imports.json'
        for phone,name in ((self.SECOND,'模拟二队长'),(self.OWNER,'模拟复盘负责人')):
            self.state.registry.import_person_library([PersonRecord(name=name,phone=phone,unit='模拟',is_leader=(phone==self.SECOND))])
            self.tokens[phone]=self.state.member_login(phone,'123456')[0]
        second=self.state.ledger.add_squad(name='模拟二中队',member_phones=(self.SECOND,),
            leader_phone=self.SECOND,library=self.state.registry.library)
        self.second=second.squad_id
        self.state.ledger.set_carry_line(self.second,self.item_id)
        self.state._save_ledger();self.state._save_registry()
        self.trace=[]

    def mark(self,stage,**outputs):
        self.assertEqual((),self.state.workspace.verify_audit_chain())
        self.trace.append(dict(stage=stage,event_id=self.state.active_event_id,
            workspace_version=self.state.workspace.version,audit_count=len(self.state.workspace.events),**outputs))

    def start_and_arrive(self):
        s=self.state
        self.assertFalse(s.registry.events);self.assertFalse(s.workspace.claims)
        alarm=s.handle_alarm_demo_ingest()
        event=s.alarm_inbox.alarms[alarm].event_id
        self.event_id=event
        with self.assertRaises(PermissionError):
            s.handle_event_action(event,'start',duty_token=self.tokens[self.CAPTAIN])
        qr=s.handle_event_action(event,'start',duty_token=self.tokens[self.DUTY])['qr_id']
        s.handle_enter_event(event)
        self.assertFalse(s.workspace.claims);self.assertFalse(s.workspace.evidence)
        self.assertFalse(s.workspace.annotations);self.assertFalse(s.workspace.orders)
        self.assertFalse(any(e.kind=='ScenarioStarted' for e in s.workspace.events))
        s.handle_deputy_appointment(event,duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY,evidence='匿名演练人工指定',request_id='t20-deputy')
        s.handle_select_squads(event,deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad.squad_id,self.second],channel='phone',request_id='t20-select')
        for squad,phone in ((self.squad.squad_id,self.CAPTAIN),(self.second,self.SECOND)):
            s.handle_captain_reply(event,captain_token=self.tokens[phone],squad_id=squad,
                can_depart='yes',expected_people='3' if phone==self.CAPTAIN else '1',
                eta='2026-10-01T09:00:00+08:00',reason='',request_id='reply-'+phone)
            result=s.handle_captain_scan(event,captain_token=self.tokens[phone],squad_id=squad,qr_id=qr)
            self.assertEqual(result,s.handle_captain_scan(event,captain_token=self.tokens[phone],squad_id=squad,qr_id=qr))
            s.handle_captain_arrival(event,captain_token=self.tokens[phone],squad_id=squad,
                mode='initial',phones=[self.MEMBER_B] if phone==self.CAPTAIN else [],request_id='arrive-'+phone)
        first=s.ledger.snapshots[event].squads[self.squad.squad_id]
        self.assertEqual(2,len(first.arrived_phones))
        s.handle_captain_arrival(event,captain_token=self.tokens[self.CAPTAIN],
            squad_id=self.squad.squad_id,mode='supplement',phones=[self.MEMBER_B],request_id='t20-supplement')
        self.assertEqual(3,len(s.ledger.snapshots[event].squads[self.squad.squad_id].arrived_phones))
        self.assertEqual(6,s.ledger.snapshots[event].squads[self.squad.squad_id].lines[self.item_id].qty_registered)
        s.handle_temporary_scan(event,member_token=self.tokens[self.TEMP],qr_id=qr)
        s.handle_temporary_assign(event,deputy_token=self.tokens[self.DEPUTY],phones=[self.TEMP],
            pos_code='tech-info',request_id='t20-temp-group')
        s.handle_temporary_arrival(event,member_token=self.tokens[self.TEMP])
        self.mark('01-alarm-start-arrival',alarm_id=alarm,qr_id=qr,squad_id=self.squad.squad_id)

    def field_chain(self):
        s=self.state;e=self.event_id;captain=self.tokens[self.CAPTAIN];deputy=self.tokens[self.DEPUTY]
        # These coordinates are explicit synthetic input, never inferred from a name.
        annotation=s.handle_sand_annotate(kind='fire_point',title='匿名模拟火点草稿',points='',
            geo_text='110.100,30.100',note='明确模拟坐标，现场尚未核实',is_demo=True,event_id=e)
        self.annotation=annotation
        task=s.handle_offline_task_record(e,deputy_token=deputy,content='线下已下达：观察模拟北段',
            group_id=self.squad.squad_id,issued_by='模拟现场指挥员',recipient_id=self.CAPTAIN,
            verifier_id=self.DEPUTY,occurred_at='2026-10-01T09:05:00+08:00',
            evidence='匿名演练下达记录',known_progress='待队长具名报告',parent_id='',request_id='t20-task')['task_id']
        self.task_id=task
        s.handle_offline_task_feedback(e,member_token=captain,task_id=task,expected_version=1,
            action='received',basis='匿名队长明确接收记录',request_id='t20-receipt-v1')
        s.handle_offline_task_amend(e,deputy_token=deputy,task_id=task,expected_version=1,
            content='线下改版：观察模拟北段与西侧',group_id=self.squad.squad_id,
            recipient_id=self.CAPTAIN,verifier_id=self.DEPUTY,
            occurred_at='2026-10-01T09:10:00+08:00',reason='匿名演练现场变更记录',request_id='t20-amend')
        with self.assertRaises(ValueError):
            s.handle_offline_task_feedback(e,member_token=captain,task_id=task,expected_version=1,
                action='completed',basis='旧版回执',request_id='t20-stale')
        call_args=dict(event_id=e,source_call_id='t20-call',source_ref='anonymous-simulated-radio',
            occurred_at='2026-10-01T09:12:00+08:00',channel_id='sim-1',transcript='可能完成了，具体任务待确认',
            task_ids=(task,),reported_group_id=self.squad.squad_id)
        with durable_storage.action(s):
            call=radio_reports.record_simulated_call(s.workspace,**call_args)
            self.assertEqual(call,radio_reports.record_simulated_call(s.workspace,**call_args))
            candidate=radio_task_candidates.record_task_candidate(s.workspace,event_id=e,source_call_id=call.source_call_id,
                task_id=task,segment_id='segment-1',task_version=2,proposed_action='completed',
                quoted_text=call.transcript,annotated_by='匿名演练记录员',basis='语义与发话人不明确',request_id='t20-radio-candidate')
            self.assertTrue(radio_task_candidates.assess_task_candidate(s.workspace,candidate).issues)
            s.persist_if_db()
        self.assertEqual('reported_offline',offline_tasks.task_state(s.workspace,e)[task].state)
        for action in ('received','started','anomaly','resolved','completed'):
            s.handle_offline_task_feedback(e,member_token=captain,task_id=task,expected_version=2,
                action=action,basis='匿名演练具名报告：'+action,request_id='t20-feedback-'+action)
        with self.assertRaises(PermissionError):
            s.handle_offline_task_close(e,member_token=captain,task_id=task,expected_version=2,
                evidence='执行人不得自行验收',overall_goal_evidence='模拟',request_id='t20-self-close')
        s.handle_offline_task_close(e,member_token=deputy,task_id=task,expected_version=2,
            evidence='匿名演练独立核查记录',overall_goal_evidence='仅模拟观察目标已核对',request_id='t20-close-task')
        self.assertEqual('closed',offline_tasks.task_state(s.workspace,e)[task].state)
        self.assertFalse(s.workspace.orders)
        s.handle_manual_material_loss(e,captain_token=captain,squad_id=self.squad.squad_id,
            item_id=self.item_id,loss_id='t20-loss',kind='consumed',amount='1',expected_version='0',
            evidence='匿名演练耗用报告',occurred_at='2026-10-01T09:15:00+08:00',reason='',request_id='t20-loss')
        approval=s.handle_manual_material_transfer(e,member_token=deputy,stage='approved',transfer_id='t20-transfer',
            request_id='t20-transfer-approve',evidence='匿名线下调拨记录',item_id=self.item_id,
            from_squad=self.squad.squad_id,to_squad=self.second,quantity='2')
        s.handle_manual_material_transfer(e,member_token=captain,stage='sent',transfer_id='t20-transfer',
            request_id='t20-transfer-send',evidence='匿名实交单',expected_version='1')
        self.assertEqual(2,transfer_in_transit(s.ledger,event_id=e,item_id=self.item_id))
        receive=dict(member_token=self.tokens[self.SECOND],stage='received',transfer_id='t20-transfer',
            request_id='t20-transfer-receive',evidence='匿名部分实收单',expected_version='2',quantity='1')
        s.handle_manual_material_transfer(e,**receive);s.handle_manual_material_transfer(e,**receive)
        self.assertEqual(7,transfer_balance(s.ledger,event_id=e,squad_id=self.second,item_id=self.item_id).available)
        self.mark('02-task-radio-anomaly-materials',task_id=task,annotation_id=annotation,
            transfer_id='t20-transfer',remaining_in_transit=transfer_in_transit(s.ledger,event_id=e,item_id=self.item_id))
        target=Path(s.registry.events[e].snapshot_path)
        self.assertEqual(len(s.workspace.events),len(load_workspace(target).events))
        s.handle_event_action(e,'close')

    def review_chain(self):
        s=self.state;e=self.event_id;token=self.tokens[self.OWNER]
        s.handle_review_owner_assign(self.OWNER,deputy_token=self.tokens[self.DEPUTY],
            evidence='匿名演练集中复盘指定',request_id='t20-owner')
        s._pending_review_actor=('owner','模拟复盘负责人',e,token)
        count=s.handle_review_generate();self.assertGreater(count,0)
        self.assertEqual(0,s.handle_review_generate())
        issue=next(i for i in s.review_ledger.issues.values() if i.event_id==e)
        self.assertEqual('pending',issue.review_status)
        ref=next(ev.id for ev in s.workspace.events if self.annotation in json.dumps(ev.payload))
        note=meeting.record_note(s,token,dict(subject='匿名模拟偏差核查',provided_by='队长',
            feedback='只核对本次模拟记录，草稿未核实；不认定个人责任',evidence_refs=ref,
            occurred_at='2026-10-01T09:16:00+08:00',request_id='t20-meeting'))
        planned=meeting.record_note(s,token,dict(subject='模拟任务时间关联',provided_by='副总指挥',
            feedback='事后登记有原始依据的模拟计划，非实时历史事实',evidence_refs=ref,
            occurred_at='',record_type='plan',planned_at='2026-10-01T09:20:00+08:00',request_id='t20-plan'))
        meeting.record_note(s,token,dict(subject='模拟任务时间关联',provided_by='队长',feedback='明确关联计划版本的模拟实际报告',
            evidence_refs=ref,occurred_at='2026-10-01T09:25:00+08:00',record_type='actual',plan_reference=planned.id,request_id='t20-actual'))
        hyp=s.handle_review_hyp_new('匿名模拟假设：交接说明不足可能与草稿未核实相关',
            evidence=ref,to_verify='核对本次模拟交接记录',related_issues=issue.issue_id)
        self.assertEqual('pending',s.review_ledger.hypotheses[hyp].status)
        s.handle_review_hyp_resolve(hyp,'核对本次模拟交接记录','匿名演练人工对照本事件记录，结论仅适用模拟',operator='模拟复盘负责人')
        for kind,target in (('issue',issue.issue_id),('hypothesis',hyp)):
            basis=causal.record_assessment(s,token,dict(target_kind=kind,target_id=target,support_refs=ref,
                counter_refs='',counter_note='模拟资料未发现反证，不能外推现场',alternatives='也可能是模拟录入顺序造成',
                missing_info='',verify_targets='匿名交接记录',meeting_ref=note.id,meeting_finding='人工核对本次模拟草稿交接',
                causal_layer='direct' if kind=='hypothesis' else 'undetermined',expected_object_version='0',request_id='t20-basis-'+kind))
            if kind=='issue':s.handle_review_issue_status(target,'confirmed',reason='人工仅确认模拟资料中的偏差',assessment_ref=basis.id)
            else:s.handle_review_hyp_status(target,'verified',reason='人工仅确认模拟关联，不归责',assessment_ref=basis.id,operator='模拟复盘负责人')
        rect=closure.create_rect_draft(s,token,issue.issue_id,hyp)
        rect=closure.publish_rect(s,token,rect.rect_id,measures='完善模拟交接记录并逐项复核',owner_unit='值班室',
            owner_person='值班员',responsible_phone=self.DUTY,deadline='2026-12-31',
            acceptance_standard='本次模拟记录逐项一致',verifier_name='副总指挥',verifier_phone=self.DEPUTY)
        for outcome in ('failed','passed'):
            closure.record_rect_feedback(s,token,rect.rect_id,provided_by='值班员',duty='模拟责任人',
                content='匿名模拟交接补齐报告',evidence=ref,completed=True)
            closure.record_rect_verification(s,token,rect.rect_id,verifier='副总指挥',outcome=outcome,
                finding='匿名演练复核：'+outcome,evidence=ref)
        self.assertEqual('closed',s.review_ledger.rects[rect.rect_id].status)
        exp=closure.propose_experience(s,token,source_kind='rect',source_id=rect.rect_id)
        closure.revise_experience(s,token,exp.exp_id,content='模拟交接应逐项记录核对',applicability='仅本次匿名模拟',
            limitations='不代表现场专业结论',condition_tags='场景=匿名模拟',evidence=ref,reason='明确模拟边界')
        closure.decide_experience(s,token,exp.exp_id,target='confirmed',reason='人工模拟核对',evidence=ref)
        closure.decide_experience(s,token,exp.exp_id,target='expired',reason='模拟条件已结束',evidence=ref)
        report=s.handle_report_generate();original=deepcopy(s.review_ledger.reports[report])
        s.handle_report_edit(report,original.content_md+'\n匿名演练人工补充。')
        s.handle_report_status(report,'pending_review')
        meeting.record_note(s,token,dict(subject='事后补录模拟证据',provided_by='模拟二队长',feedback='补录部分实收说明，余量仍待结清',
            evidence_refs=ref,occurred_at='2026-10-01T09:17:00+08:00',request_id='t20-late-evidence'))
        with self.assertRaises(PanelActionError):s.handle_report_status(report,'published')
        second=s.handle_report_generate()
        self.assertEqual(original.source_snapshot_json,s.review_ledger.reports[report].source_snapshot_json)
        self.assertNotEqual(report,second)
        current=s.review_ledger.reports[second]
        s.handle_report_edit(second,current.content_md+'\n人工审阅匿名模拟资料。')
        s.handle_report_status(second,'pending_review');s.handle_report_status(second,'published')
        self.report_id=second
        before=s.workspace.version
        for format in ('docx','pdf'):
            raw=review_exports.export_version(s.review_ledger.reports[second],format)
            self.assertTrue(raw.startswith(b'PK' if format=='docx' else b'%PDF'))
        self.assertEqual(before,s.workspace.version)
        self.mark('03-review-rect-experience-report',issue_id=issue.issue_id,hyp_id=hyp,
            rect_id=rect.rect_id,experience_id=exp.exp_id,old_report_id=report,report_id=second)
        s._pending_review_actor=None

    def run_flow(self):
        self.start_and_arrive();self.field_chain();self.review_chain()

    def test_legacy_export_checks_live_identity_and_event_before_read(self):
        self.start_and_arrive();self.field_chain()
        s=self.state
        s.handle_review_owner_assign(self.OWNER,deputy_token=self.tokens[self.DEPUTY],
            evidence='匿名演练复盘指定',request_id='export-owner')
        commander=s.login('p03-flow-secret')
        server=make_server(s,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=s.workspace.version
        def request(cookie=''):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            try:
                connection.request('GET','/export/report',headers={'Cookie':cookie})
                response=connection.getresponse();return response.status,response.read()
            finally:connection.close()
        try:
            self.assertEqual(404,request()[0])
            self.assertEqual(404,request('member_session='+self.tokens[self.CAPTAIN])[0])
            self.assertEqual(200,request('panel_session='+commander)[0])
            self.assertEqual(200,request('member_session='+self.tokens[self.OWNER])[0])
            s.active_event_id='wrong-event'
            self.assertEqual(409,request('panel_session='+commander)[0])
            s.active_event_id=self.event_id
            s.member_logout(self.tokens[self.OWNER])
            self.assertEqual(404,request('member_session='+self.tokens[self.OWNER])[0])
            self.assertEqual(before,s.workspace.version)
        finally:server.shutdown();server.server_close();thread.join()

    def test_continuous_anonymous_event_and_adverse_branches(self):
        self.run_flow()
        s=self.state;e=self.event_id
        before=deepcopy(s.workspace.events)
        restarted=PanelState(load_workspace(s.db_path,general_auth=self.auth),auth=self.auth,db_path=s.db_path,
            registry_path=s.registry_path,materials_path=s.materials_path,reviews_path=s.reviews_path,
            alarm_inbox_path=s.alarm_inbox_path,duty_subjects=(self.DUTY,))
        self.assertFalse(restarted.sessions);self.assertFalse(restarted.member_sessions)
        self.assertFalse(restarted.entry_unlocked)
        restarted.login('p03-flow-secret');restarted.handle_enter_event(e)
        self.assertEqual(len(before),len(restarted.workspace.events))
        self.assertEqual('published',restarted.review_ledger.reports[self.report_id].status)
        other=restarted.registry.create_event(name='独立第二匿名事件',report_no='T20-OTHER',
            discovered_at='2026-10-01T10:00:00+08:00',source='模拟人工',location='未知')
        restarted._save_registry()
        duty=restarted.member_login(self.DUTY,'123456')[0]
        restarted.handle_event_action(other.event_id,'start',duty_token=duty)
        restarted.handle_enter_event(other.event_id)
        self.assertFalse(restarted.workspace.annotations)
        self.assertFalse(offline_tasks.task_state(restarted.workspace,other.event_id))
        with self.assertRaises(PermissionError):
            restarted.handle_review_owner_assign(self.OWNER,deputy_token=duty,evidence='不继承授权',request_id='wrong-event')
        restarted.handle_enter_event(e)
        self.assertEqual(len(before),len(restarted.workspace.events))
        self.assertEqual('closed',offline_tasks.task_state(restarted.workspace,e)[self.task_id].state)
