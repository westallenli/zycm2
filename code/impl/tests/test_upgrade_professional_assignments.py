"""Anonymous event duties, never real qualification or professional approval."""
from dataclasses import replace
from pathlib import Path
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain.authorization import Action, Role
from panda_brain.event_authorization import EventAuthorizationService, event_grants
from panda_brain.panel import render_page, PanelActionError
from panda_brain.persistence import save_workspace
from panda_brain.professional_assignments import history, current, KIND
from . import test_upgrade_manual_material_transfers as fixture


class ProfessionalDutyTests(unittest.TestCase):
    for name in ('DUTY','DEPUTY','SOURCE','TARGET','OTHER','new_state','event_space','setUp'):
        locals()[name] = getattr(fixture.NamedTransferTests, name)

    def assign(self, **changes):
        values = dict(deputy_token=self.tokens[self.DEPUTY], subject_id=self.OTHER,
                      professional_scope='模拟林火专业', active=True, expected_version=0,
                      evidence='匿名线下指定依据', reason='本事件专业复核', request_id='professional-one') | changes
        event_id = values.pop('event_id', self.event_id)
        return self.state.handle_professional_duty(event_id, **values)

    def files(self):
        return {p: p.read_bytes() for p in (self.state.materials_path,self.state.registry_path,
                                           Path(self.state.registry.events[self.event_id].snapshot_path))}

    def service(self, space=None):
        return EventAuthorizationService(space or self.event_space(), resolve_subject=self.state.authorization_subject,
            global_grants=self.state._duty_grants, subject_exists=lambda subject: subject in self.state.registry.library)

    def opinion(self, **changes):
        return self.service().require(self.tokens[self.OTHER], Action.SUBMIT_PROFESSIONAL_OPINION,
                                  **(dict(event_id=self.event_id,professional_scope='模拟林火专业') | changes))

    def test_explicit_named_event_scope_survives_reload_without_approval(self):
        before = self.files(); record = self.assign()
        self.assertEqual(KIND, record.reference.object_kind)
        self.assertEqual(self.OTHER, self.opinion())
        self.assertEqual((record,), history(self.event_space()))
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        self.assertEqual(before[self.state.registry_path], self.state.registry_path.read_bytes())
        self.assertFalse(self.event_space().orders)
        self.assertFalse(self.event_space().approvals)
        self.assertEqual({Role.DEPUTY}, {g.role for g in event_grants(self.event_space())})

    def test_no_implicit_professional_right_for_any_person_or_generic_login(self):
        for phone in (self.DUTY,self.DEPUTY,self.SOURCE,self.TARGET,self.OTHER):
            with self.assertRaises(PermissionError):
                self.service().require(self.tokens[phone],Action.SUBMIT_PROFESSIONAL_OPINION,
                                    event_id=self.event_id,professional_scope='模拟林火专业')
        for changes in ({'deputy_token':None},{'deputy_token':self.tokens[self.DUTY]},
                        {'deputy_token':self.tokens[self.OTHER]}, {'event_id':'other'}):
            before=self.files()
            with self.assertRaises((PermissionError,PanelActionError)): self.assign(**changes)
            self.assertEqual(before,self.files())

    def test_scope_event_and_publish_authority_stay_separate(self):
        self.assign()
        for changes in ({'professional_scope':''},{'professional_scope':'另一专业'},{'event_id':'other'}):
            with self.assertRaises((ValueError,PermissionError)): self.opinion(**changes)
        with self.assertRaises(PermissionError):
            self.service().require(self.tokens[self.OTHER],Action.PUBLISH_TASK,event_id=self.event_id)
        with self.assertRaises(ValueError):
            self.service().require(self.tokens[self.DEPUTY],Action.PUBLISH_TASK,event_id=self.event_id,professional_scope='模拟林火专业')

    def test_revoke_immediately_denies_and_regrant_preserves_all_versions(self):
        self.assign()
        self.assign(active=False,expected_version=1,reason='撤销本事件职责',request_id='revoke-one')
        with self.assertRaises(PermissionError): self.opinion()
        self.assign(expected_version=2,reason='重新指定',request_id='regrant-one')
        self.assertEqual(self.OTHER,self.opinion())
        self.assertEqual([True,False,True],[json.loads(r.content_json)['active'] for r in history(self.event_space())])

    def test_multiple_scopes_are_independent(self):
        self.assign(); self.assign(professional_scope='模拟气象专业',request_id='weather-one')
        self.assign(active=False,expected_version=1,request_id='revoke-one')
        with self.assertRaises(PermissionError): self.opinion()
        self.assertEqual(self.OTHER,self.opinion(professional_scope='模拟气象专业'))

    def test_removed_person_cannot_use_retained_historical_duty(self):
        self.assign(); self.state.registry.library.pop(self.OTHER)
        with self.assertRaises(PermissionError): self.opinion()
        self.assertEqual(1,len(history(self.event_space())))

    def test_invalid_stale_noop_and_unregistered_assignments_save_nothing(self):
        for changes in ({'subject_id':'missing'},{'professional_scope':''},{'professional_scope':' x '},
                        {'active':1},{'expected_version':True},{'reason':''},{'evidence':''},{'request_id':''},
                        {'active':False},{'expected_version':9}):
            before=self.files()
            with self.assertRaises(ValueError): self.assign(**changes)
            self.assertEqual(before,self.files())
        self.assign(); before=self.files()
        for changes in ({'request_id':'noop','expected_version':1}, {'active':False,'request_id':'stale'}):
            with self.assertRaises(ValueError): self.assign(**changes)
            self.assertEqual(before,self.files())

    def test_original_replay_after_revoke_saves_nothing_and_does_not_regrant(self):
        first=self.assign(); self.assign(active=False,expected_version=1,request_id='revoke')
        before=self.files()
        with patch('panda_brain.persistence.save_workspace',side_effect=AssertionError('replay saved')):
            self.assertEqual(first,self.assign())
        self.assertEqual(before,self.files())
        with self.assertRaises(PermissionError): self.opinion()
        with self.assertRaises(ValueError): self.assign(evidence='different')

    def test_closed_event_rejects_new_assignment_and_replay(self):
        self.assign(); self.state.registry.events[self.event_id]=replace(self.state.registry.events[self.event_id],status='closed')
        before=self.files()
        with self.assertRaises((ValueError,PanelActionError)): self.assign()
        self.assertEqual(before,self.files())

    def test_duty_assignment_never_clears_classification_or_safety_release_gate(self):
        from panda_brain.models import Option
        from panda_brain.task_sop import checked_release_preflight
        self.state.login('p08-transfer-demo'); self.state.handle_enter_event(self.event_id)
        self.state.workspace.add_option(Option('professional-option','模拟方案','模拟目标',(),()))
        decision=self.state.handle_plan_decision('professional-option','adopt')
        bundle=self.state.workspace.task_drafts_for_decision(decision,event_id=self.event_id)
        before=checked_release_preflight(self.state.workspace,bundle,registry=self.state.registry,ledger=self.state.ledger)
        self.assign()
        self.assertEqual(before,checked_release_preflight(self.state.workspace,bundle,registry=self.state.registry,ledger=self.state.ledger))
        self.assertFalse(self.state.workspace.orders); self.assertFalse(self.state.workspace.approvals)

    def test_save_failure_rolls_back_and_original_retry_succeeds(self):
        before=self.files(); original=save_workspace
        def broken(space,target): original(space,target); raise OSError('anonymous after-save failure')
        with patch('panda_brain.persistence.save_workspace',side_effect=broken):
            with self.assertRaises(OSError): self.assign()
        self.assertEqual(before,self.files())
        with self.assertRaises(PermissionError): self.opinion()
        self.assign(); self.assertEqual(self.OTHER,self.opinion())

    def test_interruption_blocks_writer_and_restart_recovers_one_batch(self):
        class Interrupted(BaseException): pass
        original=save_workspace
        def broken(space,target): original(space,target); raise Interrupted()
        with patch('panda_brain.persistence.save_workspace',side_effect=broken):
            with self.assertRaises(Interrupted): self.assign()
        with self.assertRaises(RuntimeError): self.assign()
        self.state=self.new_state()
        self.assertEqual(0,len(history(self.event_space())))
        self.tokens[self.DEPUTY],error=self.state.member_login(self.DEPUTY,'123456'); self.assertFalse(error)
        self.assign(); self.assertEqual(1,len(history(self.event_space())))
        service=self.service()
        # Authenticate a new session after restart; tokens are not persisted.
        token,error=self.state.member_login(self.OTHER,'123456'); self.assertFalse(error)
        self.assertEqual(self.OTHER,service.require(token,Action.SUBMIT_PROFESSIONAL_OPINION,
                          event_id=self.event_id,professional_scope='模拟林火专业'))

    def test_missing_mutated_or_orphan_mirror_refuses_authority(self):
        record=self.assign(); space=self.event_space()
        for mutate in ('missing','orphan','changed'):
            from copy import deepcopy
            bad=deepcopy(space)
            if mutate=='missing': bad.object_revisions.pop(record.id)
            elif mutate=='changed': bad.object_revisions[record.id]=replace(record,data_kind='simulated')
            else:
                bad._events=[e for e in bad.events if e.payload.get('revision_id')!=record.id]
                # Rehashing does not restore the absent mirror.
                from .test_upgrade_material_inventory_projection import rehash
                rehash(bad)
            with self.assertRaises(ValueError): history(bad)
            with self.assertRaises(ValueError): self.service(bad)

    def test_named_page_is_read_only_and_other_roles_cannot_see_it(self):
        self.assign(); before=self.files()
        code,page=render_page(self.state,'/dispatch/professionals',member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,code); self.assertIn('模拟林火专业',page); self.assertIn('撤销本范围职责',page)
        self.assertNotIn('发布任务</button>',page); self.assertEqual(before,self.files())
        self.assertEqual(404,render_page(self.state,'/dispatch/professionals',member_token=self.tokens[self.OTHER])[0])

    def test_http_limits_csrf_unique_fields_and_actual_grant_revoke(self):
        server=self._server()
        fields=dict(event_id=self.event_id,subject_id=self.OTHER,professional_scope='模拟林火专业',
            action='grant',expected_version='0',evidence='匿名HTTP指定依据',reason='专业复核',request_id='http-one',
            csrf=self.state.member_session(self.tokens[self.DEPUTY]).custody_csrf)
        def send(data,token=None,raw=None):
            con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            con.request('POST','/dispatch/professionals',body=raw if raw is not None else urlencode(data,doseq=True),
                        headers={'Cookie':'member_session='+ (token or self.tokens[self.DEPUTY]),'Content-Type':'application/x-www-form-urlencoded'})
            response=con.getresponse(); body=response.read(); con.close(); return response.status
        before=self.files()
        self.assertEqual(403,send(fields,self.tokens[self.OTHER]))
        for bad in (dict(fields,csrf='bad'),dict(fields,event_id='other')): self.assertEqual(403,send(bad))
        for bad in (dict(fields,scope='fake'),dict(fields,subject_id=[self.OTHER,self.OTHER]),dict(fields,expected_version='-1')):
            self.assertEqual(409,send(bad))
        self.assertEqual(409,send(fields,raw=b'%FF=bad')); self.assertEqual(413,send(fields,raw=b'x'*8200))
        self.assertEqual(before,self.files()); self.assertEqual(303,send(fields)); self.assertEqual(self.OTHER,self.opinion())
        self.assertEqual(303,send(dict(fields,action='revoke',expected_version='1',request_id='http-revoke',reason='撤销')))
        with self.assertRaises(PermissionError): self.opinion()

    def _server(self):
        from panda_brain.panel import make_server
        server=make_server(self.state,port=0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(thread.join); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return server
