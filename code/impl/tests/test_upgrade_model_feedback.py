"""Real form/HTTP fallback results and the local connection-feedback asset."""
from http.client import HTTPConnection
import threading
from urllib.parse import urlencode
from unittest.mock import patch
import unittest

from panda_brain import deputy_models as dm
from panda_brain.panel import make_server, render_page
from tests import test_upgrade_deputy_model as fixture


class ModelFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DeputyModelTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.s,self.token=self.f.s,self.f.token
        server=make_server(self.s,port=0);self.server=server
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)

    def request(self,path,fields=None):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=8)
        c.request('POST' if fields is not None else 'GET',path,urlencode(fields) if fields is not None else None,
            {'Content-Type':'application/x-www-form-urlencoded','Cookie':'member_session='+self.token})
        r=c.getresponse();status=r.status;headers=dict(r.getheaders());body=r.read().decode('utf-8');c.close()
        return status,headers,body

    def test_native_precondition_failure_is_visible_beside_test_button(self):
        status,_,page=self.request(dm.ROUTE,self.f.values('test'))
        self.assertEqual(422,status)
        self.assertIn('id="model-test-result"',page)
        self.assertIn('请先保存Key并启用大模型',page[page.index('id="model-test-result"'):])

    def test_success_and_transport_failure_have_inline_result_and_native_anchor(self):
        self.f.enable()
        from panda_brain.model_connections import ConnectionUnavailable
        for error,text in ((None,'连接测试通过'),(ConnectionUnavailable('模型请求失败（HTTP 401）；鉴权失败'),'HTTP 401')):
            with patch('panda_brain.model_connections.SafeProvider.probe',side_effect=error):
                status,headers,_=self.request(dm.ROUTE,self.f.values('test'))
            self.assertEqual(303,status);self.assertIn('#model-test-result',headers['Location'])
            status,_,page=self.request(headers['Location'].split('#')[0])
            self.assertEqual(200,status);self.assertIn(text,page[page.index('id="model-test-result"'):])
            self.assertNotIn('anonymous-deputy-key',page)

    def test_local_feedback_script_is_served_and_does_not_mutate_business(self):
        before=self.f.f.f.files()
        status,headers,script=self.request('/static/model-test.js')
        self.assertEqual(200,status);self.assertIn('javascript',headers['Content-Type'])
        self.assertEqual(before,self.f.f.f.files())
        status,page=render_page(self.s,dm.ROUTE,member_token=self.token)
        self.assertEqual(200,status);self.assertIn('/static/model-test.js',page)
