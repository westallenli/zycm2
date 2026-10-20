"""A connection check must not enter a queued completion request."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from panda_brain import deputy_models as dm
from tests import test_upgrade_deputy_model as fixture


class ModelProbeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.DeputyModelTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enable()
        self.requests = []
        self.mode = 'ok'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers.get('Authorization')))
                if owner.mode == 'timeout':
                    time.sleep(2)
                code = owner.mode if isinstance(owner.mode, int) else 200
                body = (b'not-json' if owner.mode == 'invalid' else
                        json.dumps({'object': 'list', 'data': [{'id': 'deepseek-flash'}]}).encode()
                        if code == 200 else b'anonymous-deputy-key must never be echoed')
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                owner.requests.append((self.command, self.path, self.headers.get('Authorization')))
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                # Simulate a completion queue longer than the configured timeout.
                time.sleep(2)
                self.send_response(503)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.key = (self.f.token, self.f.eid)
        current = self.f.s.sand_deputy_models[self.key]
        self.f.s.sand_deputy_models[self.key] = replace(current, cloud=replace(
            current.cloud, vendor='local', base_url=f'http://127.0.0.1:{server.server_port}/v1'))

    def check(self):
        self.f.s.sand_deputy_models[self.key] = replace(
            self.f.s.sand_deputy_models[self.key], cloud_status='尚未测试。')
        before = self.f.f.f.files()
        dm.change(self.f.s, self.f.token, self.f.values('test'))
        self.assertEqual(before, self.f.f.f.files())
        status = dm.session(self.f.s, self.f.token).cloud_status
        self.assertNotIn('anonymous-deputy-key', status)
        self.assertEqual([('GET', '/v1/models', 'Bearer anonymous-deputy-key')], self.requests)
        return status

    def test_deputy_probe_does_not_wait_for_a_completion(self):
        self.assertIn('连接测试通过', self.check())

    def test_auth_balance_rate_and_service_failures_are_distinguished(self):
        for code, hint in ((401, 'Key'), (402, '余额'), (429, '繁忙'), (503, '服务')):
            with self.subTest(code=code):
                self.mode = code
                self.requests.clear()
                text = self.check()
                self.assertIn(str(code), text)
                self.assertIn(hint, text)

    def test_no_response_is_a_timeout_not_a_key_verdict(self):
        self.mode = 'timeout'
        start = time.monotonic()
        text = self.check()
        self.assertLess(time.monotonic() - start, 1.8)
        self.assertIn('超时', text)
        self.assertIn('不能判定Key', text)

    def test_invalid_response_is_not_success(self):
        self.mode = 'invalid'
        text = self.check()
        self.assertIn('响应格式', text)
        self.assertNotIn('连接测试通过', text)
