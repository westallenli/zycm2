"""Approved model settings: anonymous loopback transport, no real event egress."""

import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from panda_brain import model_connections as mc
from panda_brain.panel import PanelActionError, make_server, render_page
from panda_brain.providers import MockProvider
from panda_brain.settings import SystemSettings
from tests import test_upgrade_grounded_inquiry as fixture


class ModelConnectionTests(unittest.TestCase):
    DUTY, DEPUTY, CAPTAIN, OUTSIDER = (fixture.GroundedInquiryTests.DUTY,
        fixture.GroundedInquiryTests.DEPUTY, fixture.GroundedInquiryTests.CAPTAIN,
        fixture.GroundedInquiryTests.OUTSIDER)
    setUp = fixture.GroundedInquiryTests.setUp
    ask = fixture.GroundedInquiryTests.ask

    def change(self, action, token=None, **fields):
        token = token or self.commander
        fields.update(action=action, csrf=self.state.model_session(token).csrf)
        self.state.handle_model_settings(fields, session_token=token)

    def cloud(self, vendor='deepseek', key='synthetic-secret', token=None):
        self.change('save-cloud', token=token, vendor=vendor, model=mc.CLOUDS[vendor][2],
                    base_url='', api_key=key, enabled='on', timeout='1')

    def server(self, mode='ok'):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, self.headers.get('Authorization'), body))
                if mode == 'redirect':
                    self.send_response(307)
                    self.send_header('Location', self.server.target)
                    self.end_headers()
                    return
                if mode == 'denied':
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'synthetic-secret echoed by endpoint')
                    return
                content = json.dumps({'answer': '仅核对模拟来源', 'source_ids': ['S1'], 'unknown': '待核'})
                if 'connectivity test' in body['messages'][0]['content']:
                    content = '{"ok":true}'
                data = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, captured, f'http://127.0.0.1:{server.server_port}/v1'

    def local(self, base, *, key='', remember=False):
        self.change('save-local', base_url=base, model='qwen3:8b', api_key=key,
                    enabled='on', timeout='1', remember='on' if remember else '')
        self.change('select-mode', mode='local')

    def test_cloud_registry_rejects_foreign_forged_and_wrong_region_urls(self):
        for vendor, url in (
            ('foreign', 'https://api.example.com/v1'),
            ('deepseek', 'https://api.deepseek.com.evil.test'),
            ('deepseek', 'https://api.deepseek.com:444'),
            ('qwen', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'),
            ('qwen', 'https://demo.cn-beijing.maas.aliyuncs.com.evil.test/compatible-mode/v1'),
            ('doubao', 'http://ark.cn-beijing.volces.com/api/v3')):
            with self.subTest(url=url), self.assertRaises(ValueError):
                mc.profile(vendor, url, 'test', 'synthetic-secret')
        self.assertIn('cn-beijing', mc.profile('qwen',
            'https://demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 'test').base_url)

    def test_local_address_is_literal_private_or_loopback_and_cloud_model_rejected(self):
        for url in ('http://169.254.169.254/v1', 'http://8.8.8.8/v1',
                    'http://internal.example/v1', 'http://127.0.0.1/v1?key=secret',
                    'http://user:secret@127.0.0.1/v1', 'http://127.0.0.1/%76%31'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                mc.profile('local', url, 'qwen3:8b')
        for url in ('http://192.168.10.2:11434/v1', 'http://[::1]:11434/v1', 'http://10.0.0.2/v1'):
            self.assertEqual(url, mc.profile('local', url, 'qwen3:8b').base_url)
        self.assertIn('127.0.0.1', mc.profile('local', 'http://localhost:11434/v1', 'qwen3:8b').base_url)
        with self.assertRaises(ValueError):
            mc.profile('local', 'http://127.0.0.1:11434/v1', 'qwen:cloud')

    def test_keys_are_session_only_and_local_file_contains_no_credentials_or_permission(self):
        self.cloud()
        _, _, base = self.server()
        self.local(base, key='local-synthetic-secret', remember=True)
        path = self.root / 'model-connections.json'
        self.assertEqual({'base_url', 'model', 'timeout'}, set(json.loads(path.read_text())))
        self.assertNotIn('secret', path.read_text())
        other = self.state.login('p09-grounded-demo')
        new = self.state.model_session(other)
        self.assertEqual('', new.cloud.api_key)
        self.assertEqual('', new.local.api_key)
        self.assertFalse(new.local.enabled)
        self.assertEqual('cloud', new.mode)
        self.assertNotIn('synthetic-secret', repr(self.state.model_session(self.commander)))
        self.state.logout(self.commander)
        self.assertNotIn(self.commander, self.state.model_connections)
        self.assertIn(other, self.state.model_connections)

    def test_switch_vendor_does_not_reuse_another_vendors_key_and_clear_stays_cleared(self):
        self.cloud()
        self.cloud('qwen', key='')
        self.assertEqual('', self.state.model_session(self.commander).cloud.api_key)
        self.cloud('qwen')
        self.cloud('qwen', key='')
        self.assertEqual('synthetic-secret', self.state.model_session(self.commander).cloud.api_key)
        self.change('clear-cloud')
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'must-not-return'}):
            value = self.state._model_profile(self.commander)
        self.assertEqual('', value.api_key)
        self.assertFalse(value.enabled)

    def test_changing_key_model_or_mode_invalidates_old_preview(self):
        self.cloud('qwen')
        self.ask('物资')
        record = self.state.inquiry_journal.records[-1]
        preview = self.state.prepare_deepseek_qa(record['id'], session_token=self.commander)
        self.assertEqual(mc.CLOUDS['qwen'][2], preview['request_body']['model'])
        self.assertEqual('阿里千问', preview['model_label'])
        self.cloud('qwen', key='replacement-synthetic-secret')
        mock = MockProvider(default_reply='{"answer":"模拟","source_ids":["S1"],"unknown":""}')
        with self.assertRaises(PanelActionError):
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander, provider=mock)
        self.assertEqual([], mock.calls)
        fresh = self.state.prepare_deepseek_qa(record['id'], session_token=self.commander)
        self.state.confirm_deepseek_qa(fresh['token'], session_token=self.commander, provider=mock)
        meta = self.state.inquiry_journal.records[-1]['model_meta']
        self.assertEqual('qwen', meta['provider'])
        self.assertEqual('cloud', meta['mode'])
        self.assertNotIn('secret', json.dumps(self.state.inquiry_journal.records))

    def test_local_inference_with_public_network_off_is_exact_and_ignores_proxy(self):
        _, captured, base = self.server()
        self.local(base, key='local-synthetic-secret')
        self.ask(f'物资 值班员 {self.DUTY} 北纬32.123456')
        with patch('panda_brain.settings.load_settings', return_value=SystemSettings()), patch.dict(
                'os.environ', {'HTTP_PROXY': 'http://8.8.8.8:9', 'HTTPS_PROXY': 'http://8.8.8.8:9', 'NO_PROXY': ''}):
            preview = self.state.prepare_deepseek_qa(self.state.inquiry_journal.records[-1]['id'], session_token=self.commander)
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander)
        self.assertEqual(1, len(captured))
        self.assertEqual(preview['request_body'], captured[0][2])
        self.assertEqual('Bearer local-synthetic-secret', captured[0][1])
        self.assertNotIn(self.DUTY, json.dumps(captured[0][2]))
        self.assertEqual('local', self.state.inquiry_journal.records[-1]['model_meta']['mode'])

    def test_local_redirect_cannot_forward_a_key_to_another_server(self):
        target, received, _ = self.server()
        origin, sent, base = self.server('redirect')
        origin.target = f'http://127.0.0.1:{target.server_port}/v1/chat/completions'
        provider = mc.SafeProvider(mc.profile('local', base, 'qwen3:8b', 'synthetic-secret', True, 1))
        with self.assertRaises(ValueError) as context:
            provider.complete(system='JSON', prompt='anonymous')
        self.assertIn('307', str(context.exception))
        self.assertEqual(1, len(sent))
        self.assertEqual([], received)
        self.assertNotIn('secret', str(context.exception))

    def test_replacement_key_is_used_by_the_next_confirmed_request(self):
        _, captured, base = self.server()
        self.local(base, key='first-synthetic-key')
        self.ask('物资')
        source = self.state.inquiry_journal.records[-1]
        for key in ('first-synthetic-key', 'second-synthetic-key'):
            self.local(base, key=key)
            preview = self.state.prepare_deepseek_qa(source['id'], session_token=self.commander)
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander)
        self.assertEqual(['Bearer first-synthetic-key', 'Bearer second-synthetic-key'], [x[1] for x in captured])

    def test_local_permission_does_not_grant_cloud_permission_or_automatic_fallback(self):
        _, captured, base = self.server()
        self.local(base)
        self.change('clear-cloud')
        self.change('select-mode', mode='cloud')
        self.ask('物资')
        preview = self.state.prepare_deepseek_qa(self.state.inquiry_journal.records[-1]['id'], session_token=self.commander)
        with self.assertRaises(PanelActionError):
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander)
        self.assertEqual([], captured)
        self.assertEqual('cloud', self.state.model_session(self.commander).mode)

    def test_connection_test_finishing_after_logout_cannot_restore_credentials(self):
        self.cloud()
        def complete(provider, **payload):
            self.state.logout(self.commander)
            return '{"ok":true}'
        with patch.object(mc.SafeProvider, 'complete', complete):
            self.change('test-cloud')
        self.assertNotIn(self.commander, self.state.model_connections)

    def test_failed_model_keeps_input_and_does_not_fallback_or_echo_secret(self):
        _, captured, base = self.server('denied')
        self.local(base, key='synthetic-secret')
        self.ask('物资')
        source = self.state.inquiry_journal.records[-1]
        preview = self.state.prepare_deepseek_qa(source['id'], session_token=self.commander)
        with self.assertRaises(PanelActionError) as context:
            self.state.confirm_deepseek_qa(preview['token'], session_token=self.commander)
        self.assertNotIn('secret', str(context.exception))
        self.assertEqual(1, len(captured))
        self.assertEqual('local', self.state.model_session(self.commander).mode)
        saved = self.state.inquiry_journal.records[-1]
        self.assertEqual(source['question'], saved['question'])
        self.assertEqual('failed', saved['model_meta']['status'])
        self.assertNotIn('secret', json.dumps(saved))

    def test_fixed_connection_test_has_no_event_information_and_permission_is_required(self):
        _, captured, base = self.server()
        self.local(base)
        self.change('test-local')
        self.assertEqual(1, len(captured))
        body = json.dumps(captured[0][2], ensure_ascii=False)
        for value in (self.event_id, self.DUTY, '问询测试事件'):
            self.assertNotIn(value, body)
        self.assertIn('连通测试通过', self.state.model_session(self.commander).local_status)
        self.change('clear-local')
        self.change('test-local')
        self.assertEqual(1, len(captured))
        self.assertIn('失败', self.state.model_session(self.commander).local_status)

    def test_expired_session_destroys_credentials_and_previews(self):
        self.cloud()
        self.ask('物资')
        self.state.prepare_deepseek_qa(self.state.inquiry_journal.records[-1]['id'], session_token=self.commander)
        self.state.session_issued_at[self.commander] = time.monotonic() - self.state.SESSION_TTL_SECONDS - 1
        self.assertFalse(self.state.session_valid(self.commander))
        self.assertNotIn(self.commander, self.state.model_connections)
        self.assertEqual({}, self.state.deepseek_previews)

    def test_http_settings_require_commander_and_csrf_and_never_return_key(self):
        server = make_server(self.state, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        def request(cookie, fields=None):
            req = urllib.request.Request(base + '/models',
                data=None if fields is None else urllib.parse.urlencode(fields).encode(),
                headers={'Cookie': cookie})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, exc.read().decode()
        commander = 'panel_session=' + self.commander
        member = 'member_session=' + self.tokens[self.DEPUTY]
        self.assertEqual(403, request(member)[0])
        self.assertEqual(403, request(member, {'action': 'clear-cloud'})[0])
        self.assertEqual(403, request(commander, {'action': 'clear-cloud', 'csrf': 'forged'})[0])
        status, page = request(commander)
        self.assertEqual(200, status)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        status, page = request(commander, {'action': 'save-cloud', 'csrf': csrf,
            'vendor': 'doubao', 'model': mc.CLOUDS['doubao'][2], 'api_key': 'synthetic-secret', 'enabled': 'on'})
        self.assertEqual(200, status)
        self.assertIn('已配置', page)
        self.assertNotIn('synthetic-secret', page)
        status, page = request(commander, {'action': 'save-cloud', 'csrf': csrf,
            'vendor': 'doubao', 'model': 'invalid model', 'api_key': 'must-not-echo'})
        self.assertEqual(422, status)
        self.assertNotIn('must-not-echo', page)
        self.assertEqual('synthetic-secret', self.state.model_session(self.commander).cloud.api_key)
        status, page = render_page(self.state, '/sand', session_token=self.commander)
        self.assertIn('/models', page)
        status, page = render_page(self.state, '/sand', member_token=self.tokens[self.DEPUTY])
        self.assertNotIn('href="/models"', page)

    def test_each_cloud_test_uses_its_own_model_and_key_without_event_context(self):
        for vendor in mc.CLOUDS:
            self.cloud(vendor, key='synthetic-' + vendor)
            captured = []
            def complete(provider, **payload):
                captured.append((provider.connection, payload))
                return '{"ok":true}'
            with patch.object(mc.SafeProvider, 'complete', complete):
                self.change('test-cloud')
            self.assertEqual('synthetic-' + vendor, captured[0][0].api_key)
            self.assertEqual(mc.CLOUDS[vendor][2], captured[0][0].model)
            self.assertNotIn(self.event_id, json.dumps(captured[0][1]))
            self.assertIn('连通测试通过', self.state.model_session(self.commander).cloud_status)

    def test_deleted_or_corrupt_saved_local_profile_does_not_restore_token(self):
        _, _, base = self.server()
        self.local(base, remember=True)
        self.change('forget-local')
        self.assertFalse((self.root / 'model-connections.json').exists())
        self.assertIsNone(self.state.model_session(self.commander).local)
        path = self.root / 'model-connections.json'
        path.write_text('{"base_url":"http://8.8.8.8/v1","api_key":"untrusted"}')
        another = self.state.login('p09-grounded-demo')
        self.assertIsNone(self.state.model_session(another).local)
        self.assertIn('无法读取', self.state.model_session(another).local_status)


if __name__ == '__main__':
    unittest.main()
