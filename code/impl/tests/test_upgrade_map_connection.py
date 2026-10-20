"""Missing local detail, map-only CSP and scoped DeepSeek egress regressions."""
import json
import socket
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch


class MapConnectionTests(unittest.TestCase):
    def access(self):
        from panda_brain import runtime_access
        return runtime_access

    def test_only_sand_documents_allow_exact_existing_map_image_hosts(self):
        access = self.access()
        policy = access.content_security_policy('/sand?event_id=anonymous')
        self.assertIn('https://webrd01.is.autonavi.com', policy)
        self.assertIn('https://webst04.is.autonavi.com', policy)
        self.assertIn('https://tile.openstreetmap.org', policy)
        self.assertNotIn('https:', policy.replace('https://', ''))
        self.assertNotIn('*', policy)
        self.assertIn("connect-src 'self'", policy)
        for path in ('/models', '/login', '/offline', '/sand.evil', '/sand/command-data'):
            self.assertNotIn('autonavi', access.content_security_policy(path))

    def test_real_http_headers_use_map_policy_and_no_referrer(self):
        access = self.access()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
            def log_message(self, *_): pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        access.install_headers(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for path in ('/sand', '/models'):
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request('GET', path); response = conn.getresponse(); response.read()
                self.assertEqual('no-referrer', response.getheader('Referrer-Policy'))
                self.assertEqual(path == '/sand', 'autonavi' in response.getheader('Content-Security-Policy'))
                conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_scoped_deepseek_only_resolved_public_addresses_and_tls_port(self):
        a = self.access()
        from panda_brain.model_connections import profile
        p = profile('deepseek', '', 'deepseek-flash', 'synthetic-only', True, 1)
        self.assertFalse(a.permit_socket_event('socket.getaddrinfo', ('api.deepseek.com', 443)))
        with patch.object(a, '_runtime_enabled', True), patch.object(a.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.4.4', 443))]):
            with a.model_egress(p):
                self.assertTrue(a.permit_socket_event('socket.getaddrinfo', ('api.deepseek.com', 443)))
                self.assertFalse(a.permit_socket_event('socket.getaddrinfo', ('evil.test', 443)))
                self.assertTrue(a.permit_socket_event('socket.connect', (None, ('8.8.4.4', 443))))
                for address in (('8.8.4.4', 80), ('1.1.1.1', 443), ('127.0.0.1', 443)):
                    self.assertFalse(a.permit_socket_event('socket.connect', (None, address)))
                self.assertFalse(a.permit_socket_event('socket.bind', (None, ('8.8.4.4', 443))))
                results=[]
                t=threading.Thread(target=lambda:results.append(a.permit_socket_event('socket.connect',(None,('8.8.4.4',443)))))
                t.start();t.join();self.assertEqual([False],results)
            self.assertFalse(a.permit_socket_event('socket.connect', (None, ('8.8.4.4', 443))))

    def test_private_dns_answers_and_disabled_profiles_cannot_open_egress(self):
        a = self.access()
        from panda_brain.model_connections import profile
        p = profile('deepseek', '', 'deepseek-flash', 'synthetic-only', True, 1)
        with patch.object(a, '_runtime_enabled', True), patch.object(a.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(ValueError, '公网'):
                with a.model_egress(p): pass
        from dataclasses import replace
        with patch.object(a, '_runtime_enabled', True), patch.object(a.socket, 'getaddrinfo') as dns:
            for invalid in (replace(p, enabled=False), replace(p, api_key=''), replace(p, vendor='qwen')):
                with a.model_egress(invalid):
                    self.assertFalse(a.permit_socket_event('socket.connect', (None, ('8.8.4.4', 443))))
            dns.assert_not_called()

    def test_transport_failure_resets_scoped_permission(self):
        a = self.access()
        from panda_brain.model_connections import profile
        p = profile('deepseek', '', 'deepseek-flash', 'synthetic-only', True, 1)
        with patch.object(a, '_runtime_enabled', True), patch.object(a.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.4.4', 443))]):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with a.model_egress(p): raise RuntimeError('failure')
            self.assertFalse(a.permit_socket_event('socket.connect', (None, ('8.8.4.4', 443))))

    def test_default_p00_guard_still_denies_all_external_dns(self):
        import p00_baseline
        with self.assertRaises(PermissionError): socket.getaddrinfo('api.deepseek.com', 443)

    def test_missing_detail_asset_is_served_locally_and_loaded_before_map(self):
        source=Path('panda_brain/sandview.py').read_text('utf-8')
        self.assertIn('/static/offline-map.js',source)
        self.assertLess(source.index('/static/offline-map.js'),source.index('f"<script>{_CMD_MAP_JS}'))
        self.assertTrue(Path('static/offline-map.js').is_file())

    def test_model_summary_reports_state_without_credentials_or_provider_objects(self):
        a=self.access()
        from tests.test_upgrade_sand_command import CommandTests
        f=CommandTests();f.setUp()
        try:
            from panda_brain.sand_command import summary
            value=summary(f.s,f.token)
            self.assertFalse(value['model']['ready'])
            self.assertIn('副指挥',value['model']['notice'])
            text=json.dumps(value['model']);self.assertNotIn('api_key',text)
        finally:f.doCleanups()
