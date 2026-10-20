"""Named deputy configuration and deliberate scene/model inquiry routing."""
import json
import threading
import unittest
from http.client import HTTPConnection
from urllib.parse import urlencode
from unittest.mock import patch

from panda_brain import deputy_models as dm, sand_command as sc
from panda_brain.panel import make_server, render_page
from tests import test_upgrade_sand_command as fixture


class DeputyModelTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CommandTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.s, self.token, self.eid = self.f.s, self.f.token, self.f.eid

    def values(self, action, **extra):
        return dict(action=action, event_id=self.eid, expected_revision=str(dm.session(self.s,self.token).revision),
            csrf=self.s.member_session(self.token).custody_csrf) | extra

    def save(self):
        dm.change(self.s,self.token,self.values('save',api_key='anonymous-deputy-key',model='deepseek-flash',timeout='1'))

    def enable(self):
        self.save(); dm.change(self.s,self.token,self.values('enable'))

    def ask(self, mode='scene', question='解释一下这是什么，如何分析？', token=None):
        token = token or self.token
        return self.f.http(dict(op='ask', event_id=self.eid,csrf=self.s.member_session(token).custody_csrf,
            question=question,mode=mode),token=token)

    def test_fire_home_and_mode_choice_show_obvious_entry_for_admin_and_deputy(self):
        admin=self.s.login('p08-transfer-demo')
        for route,kwargs in (('/events',dict(session_token=admin)),('/events',dict(member_token=self.token)),
                ('/events',dict(member_token=self.token,query={'list':['1']}))):
            status,page=render_page(self.s,route,**kwargs)
            self.assertEqual(200,status);self.assertIn('data-model-config-entry',page)
            self.assertIn('大模型配置（Key）',page)
        for phone in (self.f.f.DUTY,self.f.f.SOURCE):
            status,page=render_page(self.s,'/events',member_token=self.f.f.tokens[phone])
            self.assertNotIn('data-model-config-entry',page)

    def test_settings_page_is_independent_of_admin_and_never_echoes_key(self):
        before=self.f.f.files();self.enable()
        admin=self.s.login('p08-transfer-demo');self.s.logout(admin)
        status,page=render_page(self.s,dm.ROUTE,member_token=self.token,query={'event_id':[self.eid]})
        self.assertEqual(200,status);self.assertIn('id="deputy-api-key"',page)
        self.assertIn('大模型已启用',page);self.assertNotIn('anonymous-deputy-key',page)
        self.assertEqual(before,self.f.f.files())
        provider,_,_=sc.configured_model(self.s,self.eid,self.token)
        self.assertEqual('deepseek',provider.connection.vendor)

    def test_settings_and_mutations_reject_other_roles_foreign_events_csrf_and_stale_forms(self):
        for phone in (self.f.f.DUTY,self.f.f.SOURCE):
            token=self.f.f.tokens[phone]
            self.assertEqual(404,render_page(self.s,dm.ROUTE,member_token=token)[0])
            with self.assertRaises(PermissionError):dm.change(self.s,token,self.values('enable'))
        self.assertEqual(404,render_page(self.s,dm.ROUTE,member_token=self.token,query={'event_id':['foreign']})[0])
        with self.assertRaises(PermissionError):dm.change(self.s,self.token,self.values('enable',csrf='wrong'))
        with self.assertRaises(ValueError):dm.change(self.s,self.token,self.values('enable',event_id='foreign'))
        stale=self.values('enable');self.save()
        with self.assertRaises(ValueError):dm.change(self.s,self.token,stale)

    def test_http_post_validates_duplicate_fields_and_does_not_touch_business_files(self):
        before=self.f.f.files();server=make_server(self.s,port=0)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            fields=self.values('save',api_key='anonymous-deputy-key',model='deepseek-flash',timeout='1')
            for body,expected in ((urlencode(fields),303),(urlencode(fields)+'&csrf=duplicate',422)):
                c=HTTPConnection('127.0.0.1',server.server_port,timeout=8)
                c.request('POST',dm.ROUTE,body,{'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+self.token})
                r=c.getresponse();data=r.read();c.close();self.assertEqual(expected,r.status)
                self.assertNotIn(b'anonymous-deputy-key',data)
        finally:server.shutdown();server.server_close();t.join()
        self.assertEqual(before,self.f.f.files())

    def test_scene_mode_keywords_and_legacy_requests_never_prepare_or_call_model(self):
        self.enable()
        with patch.object(sc,'model_preview') as preview,patch('panda_brain.model_connections.SafeProvider.complete') as call:
            status,result=self.ask('scene');self.assertEqual(200,status)
            self.assertEqual('scene',result['mode']);self.assertNotIn('preview',result)
            fields=dict(op='ask',event_id=self.eid,csrf=self.s.member_session(self.token).custody_csrf,question='介绍一下，为什么？')
            self.assertEqual(200,self.f.http(fields)[0]);preview.assert_not_called();call.assert_not_called()

    def test_model_mode_is_explicit_and_calls_only_after_request_confirmation(self):
        self.enable()
        with patch('panda_brain.model_connections.SafeProvider.complete',return_value='{"answer":"匿名知识解释","unknown":"待核"}') as call:
            status,result=self.ask('model');self.assertEqual(200,status);self.assertIn('preview',result)
            call.assert_not_called();self.assertNotIn('anonymous-deputy-key',json.dumps(result))
            sc.model_confirm(self.s,self.token,result['preview']['token']);call.assert_called_once()
        self.assertFalse(self.s.workspace.orders)

    def test_invalid_mode_other_roles_and_disabled_model_do_not_call_provider(self):
        with patch('panda_brain.model_connections.SafeProvider.complete') as call:
            self.assertEqual(409,self.ask('automatic')[0])
            self.assertEqual(403,self.ask('model',token=self.f.f.tokens[self.f.f.DUTY])[0])
            self.save();status,result=self.ask('model')
            self.assertEqual(200,status);self.assertNotIn('preview',result);self.assertIn('未启用',result['model_notice'])
            call.assert_not_called()

    def test_disable_clear_and_logout_invalidate_previews_and_remove_credentials(self):
        for action in ('disable','clear'):
            self.enable();p=sc.model_preview(self.s,self.token,'解释术语',self.f.local_answer())
            dm.change(self.s,self.token,self.values(action))
            self.assertNotIn(p['token'],self.s.deepseek_previews)
            with self.assertRaises(ValueError):sc.model_confirm(self.s,self.token,p['token'])
        self.enable();self.s.member_logout(self.token)
        self.assertFalse(self.s.sand_deputy_models)

    def test_own_disabled_profile_cannot_fall_back_to_admin_and_new_session_has_no_key(self):
        admin=self.f.admin_model();self.f.share(admin);self.save()
        with self.assertRaisesRegex(ValueError,'未启用'):sc.configured_model(self.s,self.eid,self.token)
        self.assertTrue(sc.configured_model(self.s,self.eid)[0].connection.enabled)
        other=self.s.member_login(self.f.f.DEPUTY,'123456')[0]
        self.assertFalse(dm.session(self.s,other).cloud.api_key)

    def test_connectivity_test_is_anonymous_and_not_executed_when_disabled(self):
        self.save()
        with patch('panda_brain.model_connections.SafeProvider.probe') as call:
            with self.assertRaises(ValueError):dm.change(self.s,self.token,self.values('test'))
            call.assert_not_called();dm.change(self.s,self.token,self.values('enable'))
            dm.change(self.s,self.token,self.values('test'));call.assert_called_once()
            self.assertEqual({},call.call_args.kwargs)
            self.assertIn('通过',dm.session(self.s,self.token).cloud_status)

    def test_sand_has_two_modes_but_no_key_form(self):
        status,page=render_page(self.s,'/sand',member_token=self.token)
        self.assertEqual(200,status);self.assertIn('data-chat-mode="scene" aria-selected="true"',page)
        self.assertIn('data-chat-mode="model" aria-selected="false"',page)
        self.assertNotIn('name="api_key"',page)

