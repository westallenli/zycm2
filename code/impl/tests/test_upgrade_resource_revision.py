"""Approved daily/event split, independent duty writes and proactive public supplies."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from dataclasses import asdict, replace

from panda_brain import resource_maintenance as daily, resource_advisor as advisor
from panda_brain import material_catalog as catalog
from panda_brain import business_modules as business
from panda_brain.models import PersonRecord
from panda_brain.panel import render_page
from . import test_upgrade_task_material_inputs as task_fixture
from . import test_upgrade_material_catalog as catalog_fixture
from . import test_upgrade_manual_material_transfers as transfer_fixture
from . import test_upgrade_material_supply_receipts as receipt_fixture
from . import test_upgrade_material_closeout_accounts as closeout_fixture


class ResourceRevisionTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space','setUp'):
        locals()[name]=getattr(transfer_fixture.NamedTransferTests,name)
    task=task_fixture.TaskMaterialInputTests.task
    view=task_fixture.TaskMaterialInputTests.view
    report=task_fixture.TaskMaterialInputTests.report

    def change(self,operation='catalog',details=None,token=None,**changes):
        values=dict(operation=operation,details=details or dict(data_type='item',row=dict(name='公共饮水',unit='瓶',quantity='0')),
            basis='明确标记的模拟日常来源',expected_version=len(self.state.ledger.daily_resource_records),
            request_id='daily-'+str(len(self.state.ledger.daily_resource_records)))
        return self.state.handle_daily_resources(member_token=token or self.tokens[self.DUTY],**(values|changes))

    def metadata(self,kind,identifier,**values):
        return self.change('metadata',dict(kind=kind,identifier=identifier,values=values|{'basis':'模拟明确参数来源'}))

    def files(self):
        return {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.state-transactions' not in p.parts}

    def selected(self,token=None):
        from panda_brain.panel import _deputy_event_context
        return _deputy_event_context(self.state,'/materials/event',member_token=token or self.tokens[self.DEPUTY])

    def test_two_entry_buttons_and_no_blue_child_row(self):
        for who in (self.DUTY,self.DEPUTY,self.SOURCE):
            before=self.files(); code,html=render_page(self.state,'/materials',member_token=self.tokens[who])
            self.assertEqual(200,code); self.assertIn('日常数据维护',html); self.assertIn('本火情人员与物资',html)
            self.assertNotIn('unified-children',html); self.assertEqual(before,self.files())

    def test_duty_can_maintain_without_any_fire_and_restart(self):
        self.state=self.new_state()
        self.state.registry.events.clear(); self.state.registry.rosters.clear(); self.state._save_registry()
        token,_=self.state.member_login(self.DUTY,'123456'); self.tokens[self.DUTY]=token
        self.assertEqual(200,render_page(self.state,'/materials/daily',member_token=token)[0])
        result=self.change(); self.assertEqual('duty',result['authority'])
        self.assertFalse(self.state.registry.events)
        self.state=self.new_state(); daily.journal(self.state.ledger)
        self.assertTrue(any(i.name=='公共饮水' for i in self.state.ledger.items.values()))
        self.assertFalse(self.state.registry.events)

    def test_missing_retired_snapshot_keeps_daily_and_passive_advice_running(self):
        self.prepare_extra()
        retired=replace(self.state.registry.events[self.event_id],event_id='retired-missing',snapshot_path=str(self.root/'absent-retired.db'))
        self.state.registry.archived_test_events[retired.event_id]=retired
        closed=replace(retired,event_id='closed-missing',status='closed')
        self.state.registry.events[closed.event_id]=closed
        before=self.files()
        code,html=render_page(self.state,'/materials/daily',member_token=self.tokens[self.DUTY])
        self.assertEqual(200,code);self.assertIn('日常数据维护</h1>',html);self.assertIn('历史事件空间缺失',html)
        code,html=render_page(self.state,'/materials/daily',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code);self.assertIn('日常数据维护</h1>',html)
        self.assertNotIn('新增或更新外协公司',html)
        with self.selected():
            data=advisor.assess(self.state,self.event_id)
            self.assertTrue(data['issues']);self.assertFalse(data['rows'][0]['points'])
            from panda_brain.live_context import event_context
            event_context(self.state,self.event_id)
            from panda_brain.intelligence import IntelligenceRuntime
            IntelligenceRuntime(self.state).tick()
        self.assertEqual(before,self.files())
        self.change()
        self.assertEqual(2,len(self.state.ledger.daily_resource_records))

    def test_missing_live_or_referenced_retired_source_still_fails_closed(self):
        from panda_brain.panel import PanelActionError
        with patch.object(self.state,'_appointment_space',side_effect=PanelActionError('missing')):
            with self.assertRaisesRegex(ValueError,'来源事件空间待核对'):daily.event_spaces(self.state)
        self.state.ledger.catalog_records['missing-mirror']=object()
        with self.assertRaises(ValueError):catalog.inspect(self.state.ledger,{})

    def test_global_catalog_permission_replay_collision_version_and_nan(self):
        before=self.files()
        for who in (self.DEPUTY,self.SOURCE,self.OTHER):
            with self.assertRaises(PermissionError): self.change(token=self.tokens[who])
        self.assertEqual(before,self.files()); original=self.change(); after=self.files()
        again=self.change(expected_version=0,request_id=original['request_id'])
        self.assertEqual(original,again); self.assertEqual(after,self.files())
        with self.assertRaises(ValueError): self.change(expected_version=0,request_id='stale')
        with self.assertRaises(ValueError): self.change(details=dict(data_type='item',row=dict(name='其他',unit='瓶',quantity='nan')))
        with self.assertRaises(ValueError): self.change(basis='different',expected_version=0,request_id=original['request_id'])
        self.assertEqual(after,self.files())

    def test_people_import_does_not_add_arrival_or_roster(self):
        before=copy.deepcopy(self.state.ledger.snapshots)
        text='姓名,手机号,单位,本职岗位,是否队长\n模拟新人员,13977773999,模拟保障单位,联络,否'
        self.change('people',dict(csv_text=text))
        self.assertEqual('联络',self.state.registry.library['13977773999'].home_position)
        self.assertEqual(before,self.state.ledger.snapshots)
        self.assertNotIn('13977773999',self.state.registry.rosters[self.event_id].entries)

    def test_global_people_and_squad_reject_active_identity_replacement(self):
        before=self.files()
        with self.assertRaises(ValueError): self.change('people',dict(csv_text='替换队长,'+self.SOURCE+',其他单位,,否'))
        sq=self.state.ledger.squads[self.source_squad]
        with self.assertRaises(ValueError):
            self.change('squad',dict(squad_id=sq.squad_id,name=sq.name,member_phones=list(sq.member_phones),leader_phone=sq.leader_phone,headcount_planned=2,contact=''))
        self.assertEqual(before,self.files())

    def test_failed_cross_file_save_restores_memory_and_files(self):
        before=self.files(); records=len(self.state.ledger.daily_resource_records)
        with patch.object(type(self.state.registry),'save',side_effect=OSError('simulated save failure')):
            with self.assertRaises(OSError): self.change('people',dict(csv_text='模拟新人员,13977773999,模拟保障单位,,否'))
        self.assertEqual(before,self.files()); self.assertEqual(records,len(self.state.ledger.daily_resource_records))
        self.assertNotIn('13977773999',self.state.registry.library)

    def test_metadata_bad_coordinates_and_tampering_fail_closed(self):
        point=self.state.ledger.add_supply_point(name='模拟坐标库',supplies={self.item_id:2})
        self.state._save_ledger()
        before=self.files()
        with self.assertRaises(ValueError): self.metadata('points',point.point_id,latitude=91,longitude=110,lead_hours=1)
        self.assertEqual(before,self.files())
        self.metadata('points',point.point_id,latitude=30,longitude=110,lead_hours=1)
        self.state.ledger.resource_metadata['points'][point.point_id]['latitude']=31
        with self.assertRaises(ValueError): daily.journal(self.state.ledger)

    def test_self_kit_loss_rejected_and_no_standard_replenishment(self):
        before=self.files()
        with self.assertRaisesRegex(ValueError,'自带标配'):
            self.state.handle_manual_material_loss(self.event_id,captain_token=self.tokens[self.SOURCE],squad_id=self.source_squad,item_id=self.item_id,
                loss_id='new-kit-loss',kind='consumed',amount='1',expected_version='0',evidence='模拟耗损原文',occurred_at='',reason='',request_id='kit-loss')
        self.assertEqual(before,self.files()); self.task(); self.report()
        with self.selected(): self.assertFalse(advisor.assess(self.state,self.event_id)['rows'])

    def prepare_extra(self):
        self.metadata('items',self.item_id,scope='extra',rate_per_person_hour=1)
        self.task(); self.report()

    def test_extra_demand_excludes_kit_baseline_and_warns_automatically(self):
        self.prepare_extra(); before=self.files()
        with self.selected():
            data=advisor.assess(self.state,self.event_id); row=data['rows'][0]
        self.assertEqual((0,4,6,4,6,'当前短缺'),(row['available'],row['current_need'],row['forecast_need'],row['current_gap'],row['forecast_gap'],row['status']))
        self.assertTrue(row['sources']); self.assertEqual(before,self.files())

    def test_nearby_ranking_excludes_closed_zero_and_unmatched_company(self):
        self.prepare_extra()
        for name,lat,stock,opened in [('较远库',31,8,'开放'),('较近库',30.01,8,'开放'),('空库',30,0,'开放'),('关闭库',30,100,'关闭')]:
            p=self.state.ledger.add_supply_point(name='模拟'+name,supplies={self.item_id:stock})
            p.open_status=opened
            self.state._save_ledger(); self.metadata('points',p.point_id,latitude=lat,longitude=110,lead_hours=1)
        self.state.ledger.add_external_company(name='模拟匹配公司',services='可供应模拟水带',source='模拟名录',recorded_by=self.DUTY)
        self.state.ledger.add_external_company(name='模拟不匹配公司',services='餐饮',source='模拟名录',recorded_by=self.DUTY)
        self.state._save_ledger()
        with self.selected():
            self.state.handle_resource_planning(self.event_id,member_token=self.tokens[self.DEPUTY],expected_version=0,request_id='context-1',latitude=30,longitude=110,planning_hours=3,buffer_hours=1,basis='模拟保障位置')
            row=advisor.assess(self.state,self.event_id)['rows'][0]
        self.assertEqual(['模拟较近库','模拟较远库'],[p['name'] for p in row['points']])
        self.assertLess(row['points'][0]['distance_km'],row['points'][1]['distance_km'])
        self.assertEqual(['模拟匹配公司'],[c['name'] for c in row['companies']]); self.assertIsNone(row['companies'][0]['quantity'])

    def test_task_revision_removes_old_forecast_and_recovery_does_not_reuse_it(self):
        self.prepare_extra(); space=self.event_space()
        flow=space.command_flows['task-1']; flow.content_version=2
        space.append_event('CommandFlowChanged',order_id='task-1',event_id=self.event_id,flow=asdict(flow))
        from panda_brain.persistence import save_workspace
        save_workspace(space,self.state.registry.events[self.event_id].snapshot_path)
        with self.selected():
            data=advisor.assess(self.state,self.event_id)
            self.assertIsNone(data['rows'][0]['forecast_gap']); self.assertIn('已改版','；'.join(data['issues']))

    def test_unknown_is_not_zero_and_no_fake_near_distance(self):
        self.prepare_extra(); self.state.ledger.snapshots.clear(); self.state._save_ledger()
        with self.selected(): row=advisor.assess(self.state,self.event_id)['rows'][0]
        self.assertIsNone(row['available']); self.assertIsNone(row['current_gap'])
        self.assertIn('保障接收点坐标','；'.join(row['missing']))

    def test_event_page_company_visibility_scoped_feed_and_readonly(self):
        self.prepare_extra()
        self.state.ledger.add_external_company(name='模拟直达公司',services='模拟水带',source='模拟维护',recorded_by=self.DUTY)
        self.state._save_ledger(); before=self.files()
        code,html=render_page(self.state,'/materials/event',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code); self.assertIn('模拟直达公司',html); self.assertIn('resource-advice',html); self.assertNotIn('unified-children',html)
        self.assertIn('转出队长',html); self.assertIn('接收队长',html)
        self.assertEqual(404,render_page(self.state,'/materials/adviser-feed',member_token=self.tokens[self.DEPUTY],query={'event_id':['foreign']})[0])
        self.assertEqual(404,render_page(self.state,'/materials/adviser-feed',member_token=self.tokens[self.SOURCE],query={'event_id':[self.event_id]})[0])
        self.assertEqual(before,self.files())

    def test_old_kit_loss_history_preserved_but_new_revision_blocked(self):
        args=dict(captain_token=self.tokens[self.SOURCE],squad_id=self.source_squad,item_id=self.item_id,
            loss_id='legacy-kit-loss',kind='consumed',amount='1',expected_version='0',evidence='模拟旧版本耗损',occurred_at='',reason='',request_id='legacy-loss')
        with patch('panda_brain.resource_maintenance.standard_line',return_value=False):
            record=self.state.handle_manual_material_loss(self.event_id,**args)
        before=self.files()
        self.assertEqual(record,self.state.handle_manual_material_loss(self.event_id,**args))
        with self.assertRaises(ValueError): self.state.handle_manual_material_loss(self.event_id,**(args|dict(amount='2',expected_version='1',request_id='new-loss',reason='新报告')))
        self.assertEqual(before,self.files()); self.assertIn(record.id,self.state.ledger.flow_records)

    def test_event_page_does_not_fallback_to_unlinked_arrival_numbers(self):
        arrival=self.state.ledger.snapshots[self.event_id].squads[self.source_squad].arrival_id
        self.state.ledger.flow_records.pop(arrival); self.state._save_ledger(); before=self.files()
        code,html=render_page(self.state,'/materials/event',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code); self.assertIn('登记到场物资来源待核',html)
        self.assertNotIn('模拟水带 × 10',html); self.assertEqual(before,self.files())

    def test_real_http_daily_preview_confirmation_csrf_duplicate_and_stale_source(self):
        import http.client,threading
        from urllib.parse import urlencode
        from panda_brain.panel import make_server
        server=make_server(self.state,port=0); threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        token=self.tokens[self.DUTY]
        def send(route,fields,use=token):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            c.request('POST',route,urlencode(fields,doseq=True).encode(),{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+use})
            r=c.getresponse(); code=r.status; body=r.read().decode(); c.close(); return code,body
        fields=dict(csrf=self.state.member_session(token).custody_csrf,expected_version='0',request_id='http-people',
            expected_source_digest=daily.source_digest(self.state),operation='people',basis='模拟人员导入',csv_text='模拟新增,13977773997,模拟单位,,否')
        before=self.files(); code,body=send('/materials/daily/preview',fields)
        self.assertEqual(200,code); self.assertIn('确认导入',body); self.assertEqual(before,self.files())
        self.assertEqual(403,send('/materials/daily/save',fields|dict(csrf='wrong'))[0])
        duplicate=dict(fields); duplicate['basis']=['a','b']
        self.assertEqual(409,send('/materials/daily/save',duplicate)[0])
        confirm={k:v for k,v in fields.items() if k!='csv_text'}|dict(details=json.dumps({'csv_text':fields['csv_text']}))
        self.assertEqual(303,send('/materials/daily/confirm',confirm)[0]); self.assertIn('13977773997',self.state.registry.library)
        after=self.files(); self.assertEqual(303,send('/materials/daily/confirm',confirm)[0]); self.assertEqual(after,self.files())
        self.assertEqual(409,send('/materials/daily/save',fields|dict(expected_version='1',request_id='stale-source'))[0])
        other=self.tokens[self.SOURCE]
        self.assertEqual(403,send('/materials/daily/save',fields|dict(csrf=self.state.member_session(other).custody_csrf),other)[0])

    def test_http_global_material_import_preview_and_confirm_recheck_source(self):
        # A stale preview must not overwrite a change made through the retained editor.
        expected=daily.source_digest(self.state)
        self.state.ledger.items[self.item_id]=__import__('dataclasses').replace(self.state.ledger.items[self.item_id],category='later-change')
        self.state._save_ledger(); before=self.files()
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.change(expected_source_digest=expected)
        self.assertEqual(before,self.files())


class ResourceSupplyPredictionTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','files','space','status','plan','attach','review','verify','publish','act','receive'):
        locals()[name]=getattr(receipt_fixture.SupplyReceiptTests,name)
    task=task_fixture.TaskMaterialInputTests.task
    input_view=task_fixture.TaskMaterialInputTests.view
    contact_report=receipt_fixture.SupplyReceiptTests.report

    def report(self,**changes):
        return self.contact_report(**changes)

    def setUp(self):
        receipt_fixture.SupplyReceiptTests.setUp(self)
        self.state.handle_daily_resources(member_token=self.tokens[self.DUTY],operation='metadata',
            details=dict(kind='items',identifier=self.item_id,values=dict(scope='extra',rate_per_person_hour=1,basis='模拟公共额外需求参数')),
            basis='模拟用途分类',expected_version=0,request_id='extra-classification')
        self.task()
        self.state.workspace=self.event_space()
        # Reuse the existing named task input service, not an injected forecast result.
        values=self.input_view()
        self.state.handle_task_material_inputs(self.event_id,member_token=self.tokens[self.DEPUTY],order_id='task-1',item_id=self.item_id,
            expected_version=values.version,expected_source_digest=values.source_digest,material_relation_source='模拟额外保障任务',force_count=2,force_source='模拟任务投入',
            duration_hours=3,duration_source='模拟持续作业',demand_quantity=2,projected_consumption=6,usage_source='模拟用量',usage_kind='simulated',evidence='模拟额外需求报告',occurred_at='',reason='',request_id='public-input')

    def result(self):
        from panda_brain.panel import _deputy_event_context
        with _deputy_event_context(self.state,'/materials/event',member_token=self.tokens[self.DEPUTY]):
            return advisor.assess(self.state,self.event_id)['rows'][0]

    def test_dispatch_not_available_then_actual_receipt_recomputes_predicted_shortage(self):
        before=self.result(); self.assertEqual((0,4,2),(before['available'],before['in_transit'],before['current_gap']))
        self.receive(quantity=3)
        after=self.result()
        self.assertEqual((3,1,0,3,'预计短缺'),(after['available'],after['in_transit'],after['current_gap'],after['forecast_gap'],after['status']))
        self.assertEqual(1.5,after['coverage_hours'])
        self.receive(quantity=1,expected_version=1,request_id='receive-rest')
        self.assertEqual((4,2),(self.result()['available'],self.result()['forecast_gap']))

    def test_supply_plan_and_real_receipt_do_not_change_fixed_baseline(self):
        baseline=copy.deepcopy(self.state.ledger.to_dict()['snapshots'][self.event_id]['squads'])
        self.receive(quantity=3); self.result()
        after=self.state.ledger.to_dict()['snapshots'][self.event_id]['squads']
        self.assertEqual(baseline,after)

    def test_recommendations_use_verified_stock_less_dispatch_and_holds(self):
        row=self.result()
        self.assertEqual(6,next(p['quantity'] for p in row['points'] if p['id']==self.point_id))
        self.assertIn('扣除已交出',next(p['quantity_basis'] for p in row['points'] if p['id']==self.point_id))

    def test_extra_receipt_loss_cannot_debit_kit_and_forecast_recomputes(self):
        self.receive(quantity=3)
        values=dict(captain_token=self.tokens[self.TARGET],squad_id=self.target_squad,item_id=self.item_id,
            loss_id='extra-loss',kind='consumed',amount='4',expected_version='0',evidence='模拟额外实收物资使用',occurred_at='',reason='',request_id='extra-loss')
        before=self.files()
        with self.assertRaisesRegex(ValueError,'不得扣减'):
            self.state.handle_manual_material_loss(self.event_id,**values)
        self.assertEqual(before,self.files())
        record=self.state.handle_manual_material_loss(self.event_id,**(values|dict(amount='1')))
        self.assertEqual('public_extra',json.loads(record.content_json)['inventory_scope'])
        self.assertEqual((2,4),(self.result()['available'],self.result()['forecast_gap']))
        self.assertEqual(record,self.state.handle_manual_material_loss(self.event_id,**(values|dict(amount='1'))))


class ResourceCloseoutBoundaryTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','grant','change','facts','verify','adjust'):
        locals()[name]=getattr(closeout_fixture.CloseoutAccountsTests,name)

    def setUp(self):
        # Construct an authentic pre-change loss/withdrawal history; new writes use the real gate.
        with patch('panda_brain.resource_maintenance.standard_line',return_value=False):
            closeout_fixture.CloseoutAccountsTests.setUp(self)

    def test_historical_closeout_cannot_reduce_next_kit_and_new_kit_account_is_refused(self):
        with patch('panda_brain.resource_maintenance.standard_line',return_value=False):
            historical=self.verify()
        before=self.files()
        with self.assertRaisesRegex(ValueError,'下次标配'): self.adjust(historical)
        self.assertEqual(10,self.state.ledger.carry_list(self.source_squad)[0].quantity)
        self.assertEqual(before,self.files())
        self.assertEqual(historical,self.verify())
        with self.assertRaisesRegex(ValueError,'自带标配'):
            self.verify(expected_version=1,request_id='new-kit-closeout')
        self.assertEqual(before,self.files())


class ResourceLegacyCompatibilityTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','LOGISTICS','REPLACEMENT','new_state','event_space','appoint','view','report','files','grant','setUp','change'):
        locals()[name]=getattr(catalog_fixture.CatalogTests,name)

    def test_daily_edits_interleave_with_original_catalog_and_keep_granted_right(self):
        self.change()
        self.state.handle_daily_resources(member_token=self.tokens[self.DUTY],operation='catalog',
            details=dict(data_type='item',row=dict(name='匿名长期物资',unit='盘',quantity='8')),
            basis='模拟值班员更正',expected_version=0,request_id='daily-interleave')
        self.change(version=1,request='legacy-again',details=dict(data_type='item',row=dict(name='匿名长期物资',unit='盘',quantity='9')))
        self.assertEqual(2,len(catalog.inspect(self.state.ledger,self.state.supply_event_spaces())))
        self.state=self.new_state(); self.assertEqual(2,len(catalog.inspect(self.state.ledger,self.state.supply_event_spaces())))
        self.assertEqual(9,next(i.default_carry_qty for i in self.state.ledger.items.values() if i.name=='匿名长期物资'))

    def test_existing_material_maintainer_global_write_but_not_people(self):
        token=self.tokens[self.OTHER]
        self.state.handle_daily_resources(member_token=token,operation='catalog',details=dict(data_type='company',row=dict(name='模拟值守公司',source='模拟表',services='保障饮水')),
            basis='模拟授权人员维护',expected_version=0,request_id='retained-authority')
        self.assertEqual('material-maintainer',self.state.ledger.daily_resource_records[-1]['authority'])
        with self.assertRaises(PermissionError):
            self.state.handle_daily_resources(member_token=token,operation='people',details=dict(csv_text='越权,13977773998'),basis='模拟',expected_version=1,request_id='bad-people')


if __name__=='__main__': unittest.main()
