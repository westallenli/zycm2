"""Model errors must reach the visible chat without exposing private details."""
import json
import unittest
from unittest.mock import patch
from panda_brain.model_connections import ConnectionUnavailable
from tests import test_upgrade_deputy_model as fixture


class DollFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DeputyModelTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.enable()

    def confirm(self, nonce):
        return self.f.f.http(dict(op='model-confirm',event_id=self.f.eid,
            csrf=self.f.s.member_session(self.f.token).custody_csrf,preview_token=nonce))

    def test_sanitized_transport_error_is_visible_and_failed_request_cannot_replay(self):
        status,result=self.f.ask('model');self.assertEqual(200,status)
        before=self.f.f.f.files()
        with patch('panda_brain.model_connections.SafeProvider.complete',
                   side_effect=ConnectionUnavailable('模型请求失败（HTTP 401）；鉴权失败，请核对Key。')) as call:
            status,error=self.confirm(result['preview']['token'])
            self.assertEqual(409,status);self.assertIn('HTTP 401',error['error'])
            self.assertIn('原问题已保留',error['error']);self.assertNotIn('anonymous-deputy-key',json.dumps(error))
            self.assertEqual(409,self.confirm(result['preview']['token'])[0]);call.assert_called_once()
        self.assertEqual(before,self.f.f.f.files())

    def test_unexpected_exception_details_never_reach_the_chat(self):
        _,result=self.f.ask('model')
        with patch('panda_brain.model_connections.SafeProvider.complete',side_effect=RuntimeError('anonymous-deputy-key')):
            status,error=self.confirm(result['preview']['token'])
        self.assertEqual(409,status);self.assertIn('模型分析失败',error['error'])
        self.assertNotIn('anonymous-deputy-key',json.dumps(error))
