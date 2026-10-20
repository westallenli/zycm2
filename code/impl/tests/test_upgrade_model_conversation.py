"""Ordinary chat must not inherit field-data disclaimers or invent provider facts."""
import json
import unittest
from unittest.mock import patch
from panda_brain import sand_command as sc
from panda_brain.sandtable import InquiryAnswer
from tests import test_upgrade_deputy_model as fixture


class ModelConversationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DeputyModelTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.enable()

    def preview(self,question='你好'):
        status,result=self.f.ask('model',question=question)
        self.assertEqual(200,status)
        return result['preview']

    def test_greeting_does_not_send_local_help_as_missing_field_data(self):
        body=self.preview()['body'];payload=json.loads(body['prompt'])
        self.assertEqual('你好',payload['question'])
        self.assertNotIn('local_answer',payload)
        self.assertIn('普通问候',body['system'])
        self.assertNotIn('可支持的问询主题',body['prompt'])

    def test_provider_identity_comes_from_the_selected_connection(self):
        body=self.preview('你是DeepSeek吗')['body']
        self.assertIn('DeepSeek',body['system']);self.assertIn('deepseek-flash',body['system'])
        self.assertNotIn('anonymous-deputy-key',json.dumps(body))

    def test_only_corrected_model_history_for_this_actor_and_event_is_sent(self):
        journal=self.f.s.inquiry_journal
        answer=InquiryAnswer('model_analysis','匿名历史回答','',(),'', '模型回答','')
        def add(q,actor=None,event=None,meta=None):
            journal.append(event_id=event or self.f.eid,actor_id=actor or self.f.f.f.DEPUTY,role='member',
                question=q,answer=answer,citations=(),model_meta=meta)
        add('旧现场资料缺项')
        add('旧模型模板',meta={'status':'succeeded'})
        meta={'status':'succeeded','conversation':'model-chat-v1'}
        add('本人之前的问题',meta=meta)
        add('他人的模型问题',actor=self.f.f.f.DUTY,meta=meta)
        add('其他事件模型问题',event='other-event',meta=meta)
        payload=json.loads(self.preview()['body']['prompt'])
        self.assertEqual([{'question':'本人之前的问题','answer':'匿名历史回答'}],payload['history'])
        self.assertEqual('question',list(payload)[-1])

    def test_greeting_reply_is_displayed_without_forced_analysis_or_unknown(self):
        p=self.preview()
        with patch('panda_brain.model_connections.SafeProvider.complete',return_value='{"answer":"你好！有什么可以帮你？","unknown":""}'):
            reply=sc.model_confirm(self.f.s,self.f.token,p['token'])
        self.assertEqual('你好！有什么可以帮你？',reply['conclusion'])
        self.assertEqual('',reply['unconfirmed']);self.assertEqual((),reply['sources'])
        self.assertFalse(self.f.s.workspace.orders)
