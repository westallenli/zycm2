"""Shared stock conservation, frozen supply publication and named dispatch."""
from dataclasses import asdict,replace
import http.client,json,threading,unittest
from unittest.mock import patch
from urllib.parse import urlencode
from panda_brain import material_supply_execution as supply
from panda_brain.material_supply_contacts import _digest
from panda_brain.materials import MaterialsLedger
from panda_brain.models import Option,PersonRecord
from panda_brain.panel import render_page,make_server
from panda_brain.persistence import save_workspace
from panda_brain.task_drafts import PlanTaskBlueprint,TaskDraftSpec,TaskCommonContent
from panda_brain.task_release import releases
from . import test_upgrade_material_supply_contacts as fixture


class Interrupted(BaseException):pass


class SupplyExecutionTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files'):
        locals()[name]=getattr(fixture.SupplyContactTests,name)

    def setUp(self):
        fixture.SupplyContactTests.setUp(self)
        self.token=self.state.login('p08-transfer-demo');self.state.handle_enter_event(self.event_id)
        self.scope='模拟补给专业核对'
        self.state.handle_professional_duty(self.event_id,deputy_token=self.tokens[self.DEPUTY],subject_id=self.OTHER,
            professional_scope=self.scope,active=True,expected_version=0,evidence='模拟指定',reason='核对模拟供给',request_id='assign-supply-reviewer')
        self.contact=self.report(available_quantity=10,promised_delivery_at='2026-10-10T12:00:00+08:00')
        self.plan(4)

    def space(self):return self.state._appointment_space(self.event_id)[0]

    def status(self):return supply.inspect(self.state.ledger,self.state.supply_event_spaces())

    def plan(self,amount,*,name='plan-1',contact=None,item=None,**extra):
        iid=item or self.item_id
        self.state.workspace.add_option(Option(name,'模拟补给任务','模拟供给目标',(),(),task_blueprint=PlanTaskBlueprint(main=TaskDraftSpec(
            title='模拟物资补给',objective='供给明确数量',completion_standard='清单核对良好实收',responsible_group_id=self.source_squad,
            verifier_id=self.TARGET,content=TaskCommonContent(time_requirement='模拟明日08时前',feedback_requirement='分批具名报告',safety_measures='模拟安全条件')))))
        self.decision_id=self.state.handle_plan_decision(name,'adopt')
        bundle=self.state.workspace.task_drafts_for_decision(self.decision_id,event_id=self.event_id)
        self.task_id=bundle.tasks[0].id
        self.attach(amount,contact=contact or self.contact,item=iid,**extra)
        self.review()

    def attach(self,amount,*,contact,item=None,**extra):
        iid=item or self.item_id;bundle=self.state.workspace.task_drafts_for_decision(self.decision_id,event_id=self.event_id)
        from panda_brain.material_supply_drafts import review
        version=review(self.space(),self.state.ledger,event_id=self.event_id,decision_id=self.decision_id,task_id=self.task_id,item_id=iid).version
        return self.state.handle_material_supply_draft(self.event_id,session_token=self.token,decision_id=self.decision_id,task_id=self.task_id,item_id=iid,
            expected_bundle_version=bundle.version,expected_bundle_digest=_digest(asdict(bundle)),expected_item_digest=_digest(asdict(self.state.ledger.items[iid])),
            expected_version=version,requested_quantity=amount,quantity_basis='模拟清单数量',needed_by='2026-10-10T18:00:00+08:00',
            contact_id=contact.id,destination_hint='模拟接收点',evidence='模拟编制依据',request_id=f'attach-{self.task_id}-{iid}-{version}',**extra)

    def review(self):
        self.draft=self.state.workspace.task_drafts_for_decision(self.decision_id,event_id=self.event_id)
        from panda_brain.task_release import history
        # Count immutable versions for this task rather than reuse another plan's request.
        reviews=[r for r in self.space().object_revisions.values() if r.reference.object_kind=='task_review_assessment' and json.loads(r.content_json).get('task_id')==self.task_id]
        self.state.handle_task_review_assessment(self.event_id,deputy_token=self.tokens[self.DEPUTY],decision_id=self.decision_id,task_id=self.task_id,
            expected_draft_version=self.draft.version,expected_review_version=len(reviews),category='routine',reason='模拟分类',basis_ref='模拟分类依据',request_id=f'assess-{self.task_id}-{self.draft.version}-{len(reviews)}')
        for purpose,payload in [('rules',dict(category='routine',required_scopes=[],safety_conditions=['模拟运输条件已核对'])),('safety',{'模拟运输条件已核对':True})]:
            count=sum(v['task_id']==self.task_id and v['purpose']==purpose for _,v in history(self.space()))
            self.state.handle_task_release_action(self.event_id,member_token=self.tokens[self.OTHER],action='evidence',decision_id=self.decision_id,
                task_id=self.task_id,draft_version=self.draft.version,purpose=purpose,scope=self.scope,payload=payload,evidence='模拟专业依据',reason='模拟通过',expected_version=count,
                request_id=f'{purpose}-{self.task_id}-{self.draft.version}-{count}')

    def verify(self,**changes):
        values=dict(member_token=self.tokens[self.LOGISTICS],action='verify_stock',point_id=self.point_id,item_id=self.item_id,quantity=10,
            basis='模拟逐件核实',expected_stock_version=0,expected_source_digest=self.digest,request_id='stock-1')|changes
        return self.state.handle_material_supply_execution(self.event_id,**values)

    def publish(self,**changes):
        return self.state.handle_task_release_action(self.event_id,member_token=self.tokens[self.DEPUTY],action='publish',decision_id=self.decision_id,
            draft_version=self.draft.version,reason='模拟审定补给',request_id='pub-'+self.task_id,**changes)

    def act(self,action='dispatch',**changes):
        oid=next(iter(self.status()[2]))
        values=dict(member_token=self.tokens[self.LOGISTICS],action=action,order_id=oid,quantity=2 if action=='dispatch' else 0,basis='模拟交出或取消依据',expected_version=1,request_id='dispatch-1' if action=='dispatch' else 'cancel-1')|changes
        return self.state.handle_material_supply_execution(self.event_id,**values)

    def test_unknown_is_not_stock_then_reserve_dispatch_cancel_conserves_without_field_receipt(self):
        before=self.files()
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(before,self.files())
        self.verify();release=self.publish();self.assertEqual(self.DEPUTY,next(r.actor_id for r,v in self.status()[0] if v.get('action')=='reserve'))
        self.assertEqual(10,self.status()[1][(self.point_id,self.item_id)]['quantity'])
        self.act();self.assertEqual(8,self.status()[1][(self.point_id,self.item_id)]['quantity'])
        self.act('cancel',expected_version=2)
        row=next(iter(self.status()[2].values()));self.assertEqual(2,row['dispatched']);self.assertTrue(row['cancelled'])
        self.assertIsNone(self.state.ledger.points[self.point_id].supplies[self.item_id])
        from panda_brain.material_inventory_projection import project
        projection=project(self.space(),self.state.ledger,event_id=self.event_id,item_id=self.item_id)
        self.assertEqual(12,projection.book_available)
        self.assertEqual('confirmed_unack',self.space().command_flows[json.loads(release.content_json)['tasks'][0]['order_id']].stage)

    def test_two_distinct_publications_share_stock_and_failed_publish_rolls_back_all_files(self):
        self.verify(quantity=5);self.publish();self.plan(2,name='plan-2');before=self.files()
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(before,self.files());self.assertEqual(1,len(releases(self.space())))
        self.act('cancel');self.publish();self.assertEqual(2,len(self.status()[2]))

    def test_stock_correction_cannot_erase_reservations_and_explicit_zero_survives_reload(self):
        self.verify();self.publish();before=self.files()
        with self.assertRaises(ValueError):self.verify(quantity=3,expected_stock_version=1,request_id='stock-low')
        self.assertEqual(before,self.files())
        self.act('cancel');self.verify(quantity=0,expected_stock_version=1,request_id='stock-zero')
        self.state=self.new_state();self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456')
        self.assertEqual(0,self.status()[1][(self.point_id,self.item_id)]['quantity'])
        self.assertEqual(self.state.ledger.to_dict(),MaterialsLedger.load(self.state.materials_path).to_dict())

    def test_company_promises_separate_and_sent_amount_not_released_by_partial_cancel(self):
        internal=self.report(available_quantity=0,expected_version=1,request_id='internal-empty')
        digest=self.view(source_kind='company',source_id=self.company_id).current_source_digest
        company=self.report(source_kind='company',source_id=self.company_id,expected_source_digest=digest,
            available_quantity=5,promised_delivery_at='2026-10-10T12:00:00+08:00',request_id='company-promise')
        self.plan(4,name='company-plan',contact=company,internal_contact_ids=(internal.id,),internal_priority_reason='模拟库内无货依据')
        self.publish();self.assertFalse(self.status()[1]);self.act();self.act('cancel',expected_version=2)
        self.plan(4,name='company-second',contact=company,internal_contact_ids=(internal.id,),internal_priority_reason='模拟库内无货依据')
        before=self.files()
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(before,self.files());self.assertFalse(self.status()[1])

    def test_other_event_uses_same_global_stock_version_and_hold_never_disappears(self):
        self.verify();self.publish();first=self.event_id
        event=self.state.registry.create_event(name='模拟另一事件',report_no='P08-SHARED',discovered_at='2026-10-10T08:00:00+08:00',source='匿名模拟',location='模拟南段')
        self.event_id=event.event_id
        self.state.handle_event_action(self.event_id,'start',duty_token=self.tokens[self.DUTY])
        self.state.handle_deputy_appointment(self.event_id,duty_token=self.tokens[self.DUTY],subject_id=self.DEPUTY,evidence='模拟第二事件任命',request_id='deputy-second')
        self.LOGISTICS='13977773108'
        self.state.registry.import_person_library((PersonRecord(name='匿名第二事件后勤',phone=self.LOGISTICS,unit='模拟物资供应组'),))
        self.tokens[self.LOGISTICS],error=self.state.member_login(self.LOGISTICS,'123456');self.assertEqual('',error)
        qr=next(q.qr_id for q in self.state.registry.qrs.values() if q.event_id==self.event_id)
        self.state.handle_temporary_scan(self.event_id,member_token=self.tokens[self.LOGISTICS],qr_id=qr)
        self.state.handle_temporary_assign(self.event_id,deputy_token=self.tokens[self.DEPUTY],phones=[self.LOGISTICS],pos_code='log-material',request_id='log-second')
        self.state.handle_temporary_arrival(self.event_id,member_token=self.tokens[self.LOGISTICS]);self.appoint(self.LOGISTICS)
        with self.assertRaises(ValueError):self.verify(request_id='stock-second-stale')
        with self.assertRaises(ValueError):self.verify(quantity=3,expected_stock_version=1,request_id='stock-second-low')
        self.verify(quantity=8,expected_stock_version=1,request_id='stock-second-valid')
        rows,stocks,orders=self.status();self.assertEqual(2,stocks[(self.point_id,self.item_id)]['stock_version']);self.assertEqual(first,next(iter(orders.values()))['event_id'])
        self.assertEqual(4,next(iter(orders.values()))['quantity']);self.assertEqual(3,len(rows))

    def test_multiple_items_in_same_task_each_reserve_and_no_second_item_silent_loss(self):
        iid=self.state.ledger.upsert_item(name='模拟连接头',unit='个').item_id
        pid=self.state.ledger.add_supply_point(name='模拟第二点',source='pending',supplies={iid:None}).point_id;self.state._save_ledger()
        digest=self.view(item_id=iid,source_id=pid).current_source_digest
        contact=self.report(item_id=iid,source_id=pid,expected_source_digest=digest,available_quantity=2,promised_delivery_at='2026-10-10T12:00:00+08:00',request_id='contact-second')
        self.attach(2,contact=contact,item=iid);self.review();self.verify()
        before=self.files()
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(before,self.files())
        self.verify(point_id=pid,item_id=iid,expected_source_digest=digest,quantity=2,request_id='stock-second');self.publish()
        self.assertEqual({self.item_id,iid},{v['item_id'] for v in self.status()[2].values()})

    def test_only_current_logistics_authority_and_generic_entry_cannot_forge_special_records(self):
        before=self.files()
        for person in (self.DUTY,self.DEPUTY,self.SOURCE,self.TARGET,self.OTHER,self.REPLACEMENT):
            with self.assertRaises(PermissionError):self.verify(member_token=self.tokens[person])
        self.assertEqual(before,self.files());record=self.verify()
        for kind in ('professional_assignment','task_release_evidence','formal_task_release','formal_task_receipt','formal_task_relations','formal_task_control',supply.STOCK,supply.ORDER):
            forged=replace(record,reference=replace(record.reference,object_kind=kind,object_id='fake'))
            with self.assertRaises(PermissionError):self.state.workspace.record_object_revision(forged,expected_version=0)
        self.appoint(self.REPLACEMENT)
        with self.assertRaises(PermissionError):self.verify()
        self.verify(member_token=self.tokens[self.REPLACEMENT],expected_stock_version=1,request_id='stock-new-leader')

    def test_exact_replay_zero_save_conflict_oversend_stale_and_nonfinite_rejected(self):
        stock=self.verify();self.publish();sent=self.act();before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay saved')):
            self.assertEqual(stock,self.verify());self.assertEqual(sent,self.act())
        for changes in (dict(quantity=3),dict(quantity=3,expected_version=2,request_id='oversend'),dict(expected_version=0,request_id='stale'),dict(quantity=float('nan'),expected_version=2,request_id='nan')):
            with self.assertRaises(ValueError):self.act(**changes)
        self.assertEqual(before,self.files())

    def test_new_review_or_stale_draft_blocks_dispatch_but_explicit_cancel_releases(self):
        self.verify();self.publish();self.review();before=self.files()
        with self.assertRaises(ValueError):self.act()
        self.assertEqual(before,self.files());self.act('cancel')

    def test_missing_shared_or_event_mirror_has_readable_stop_and_no_old_balance_fallback(self):
        stock=self.verify();self.state.ledger.supply_records.pop(stock.id)
        with self.assertRaises(ValueError):self.status()
        code,page=render_page(self.state,route='/material-supply/execution',member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(409,code);self.assertIn('共享补给来源待核对',page);self.assertNotIn('<form',page)

    def test_postwrite_failure_restores_both_journals_and_same_request_retries(self):
        self.verify();before=self.files()
        def fail(space,target):save_workspace(space,target);raise OSError('anonymous postwrite failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertEqual(before,self.files());self.assertFalse(self.status()[2]);self.publish()

    def test_interruption_blocks_then_restart_restores_shared_and_event_and_retries_once(self):
        self.verify();self.publish();before=self.files();oid=next(iter(self.status()[2]))
        def stop(space,target):save_workspace(space,target);raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=stop),self.assertRaises(Interrupted):self.act()
        with self.assertRaises(RuntimeError):self.state.handle_material_supply_execution(self.event_id,member_token=self.tokens[self.LOGISTICS],action='dispatch',
            order_id=oid,quantity=2,basis='模拟交出或取消依据',expected_version=1,request_id='dispatch-1')
        self.state=self.new_state();self.assertEqual(before,self.files())
        self.tokens[self.LOGISTICS],_=self.state.member_login(self.LOGISTICS,'123456');self.act()
        self.assertEqual(2,next(iter(self.status()[2].values()))['dispatched'])

    def test_actual_http_get_readonly_csrf_duplicates_unknown_numeric_and_named_write(self):
        before=self.files();code,page=render_page(self.state,route='/material-supply/execution',member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200,code);self.assertIn('核实存量：未知',page);self.assertEqual(before,self.files())
        self.assertEqual(404,render_page(self.state,route='/material-supply/execution',member_token=self.tokens[self.SOURCE])[0])
        server=make_server(self.state,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        fields=dict(csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,event_id=self.event_id,action='verify_stock',point_id=self.point_id,
            item_id=self.item_id,quantity='10',basis='模拟HTTP核实',request_id='http-stock',expected_stock_version='0',expected_source_digest=self.digest)
        def send(values,token=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request('POST','/material-supply/execution',urlencode(values,doseq=True),
                {'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+(token or self.tokens[self.LOGISTICS])})
            r=c.getresponse();r.read();c.close();return r.status
        self.assertEqual(403,send(fields|dict(csrf='wrong')))
        for values in (fields|dict(quantity=['1','2']),fields|dict(actor=self.DEPUTY),fields|dict(quantity='NaN'),fields|dict(expected_stock_version='０')):
            self.assertEqual(409,send(values))
        wrong=fields|dict(csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf)
        self.assertEqual(403,send(wrong,self.tokens[self.DEPUTY]));self.assertEqual(before,self.files());self.assertEqual(303,send(fields))
