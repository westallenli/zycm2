"""P14-01: real loopback rejection responses and bounded connection teardown."""
import http.client
import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

from panda_brain import http_rejection, review_word
from panda_brain.panel import make_server
from tests import test_upgrade_review_reports as fixture


class HTTPRejectionTests(unittest.TestCase):
    def setUp(self):
        fixture.ReportSnapshotTests.setUp(self)
        self.server = make_server(self.state, port=0)
        self.finished = queue.Queue()
        original = self.server.RequestHandlerClass
        finished = self.finished
        class Observed(original):
            def finish(self):
                try:
                    super().finish()
                finally:
                    finished.put(True)
        self.server.RequestHandlerClass = Observed
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.before = self.snapshot()

    def snapshot(self):
        return (self.state.workspace.version, len(self.state.workspace.events),
                self.state.db_path.read_bytes(),
                self.state.reviews_path.read_bytes() if self.state.reviews_path.exists() else None)

    def connection(self, route, headers, *, owner=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        conn.putrequest('POST', route)
        if owner:
            conn.putheader('Cookie', 'member_session='+self.tokens[1])
        for key, value in headers:
            conn.putheader(key, str(value))
        conn.endheaders()
        return conn

    def response(self, conn, status):
        response = conn.getresponse()
        self.assertEqual(status, response.status)
        self.assertEqual('close', response.getheader('Connection'))
        raw = response.read()
        self.assertEqual(len(raw), int(response.getheader('Content-Length')))
        data = json.loads(raw)
        self.assertTrue(data.get('error') or data.get('message'))

    def test_full_and_fragmented_oversized_simulation_bodies_keep_complete_413(self):
        for route, size in (('/sand/runtime', 5006), ('/sand/channel', 33004)):
            for fragmented in (False, True):
                for attempt in range(10):
                    with self.subTest(route=route, fragmented=fragmented, attempt=attempt):
                        conn = self.connection(route, [('Content-Length', size)])
                        try:
                            raw = b'x'*size
                            if fragmented:
                                conn.send(raw[:8]); time.sleep(.03); conn.send(raw[8:])
                            else:
                                conn.send(raw)
                            self.response(conn, 413)
                        finally:
                            conn.close()
        self.assertEqual(self.before, self.snapshot())

    def test_word_size_rejections_cover_full_large_and_fragmented_bodies(self):
        for route, size in (('/review/word-preview', review_word.MAX_REQUEST_BYTES+1),
                            ('/review/word-import', 5000)):
            conn = self.connection(route, [('Content-Length', size)])
            try:
                conn.send(b'x'*8); time.sleep(.03); conn.send(b'x'*(size-8))
                self.response(conn, 413)
            finally:
                conn.close()
        self.assertEqual(self.before, self.snapshot())

    def test_unauthorized_word_headers_get_403_before_any_upload(self):
        for route in ('/review/word-preview', '/review/word-import'):
            conn = self.connection(route, [('Content-Length', review_word.MAX_REQUEST_BYTES)], owner=False)
            try:
                self.response(conn, 403)
            finally:
                conn.close()
        self.assertEqual(self.before, self.snapshot())

    def test_invalid_or_ambiguous_framing_is_rejected_without_reading_or_dispatch(self):
        headers = [[], [('Content-Length', -1)], [('Content-Length', '+1')],
                   [('Content-Length', 'abc')], [('Content-Length', 0)],
                   [('Content-Length', 1), ('Content-Length', 1)],
                   [('Content-Length', 1), ('Transfer-Encoding', 'chunked')],
                   [('Transfer-Encoding', 'chunked')]]
        for route in ('/sand/runtime', '/sand/channel', '/review/word-preview', '/review/word-import'):
            for fields in headers:
                with self.subTest(route=route, fields=fields):
                    conn = self.connection(route, fields)
                    try:
                        self.response(conn, 413)
                    finally:
                        conn.close()
        self.assertEqual(self.before, self.snapshot())

    def test_nonclosing_client_is_released_at_deadline_and_next_request_works(self):
        conn = self.connection('/sand/runtime', [('Content-Length', 5000)])
        # HTTPConnection closes its socket automatically on a Connection: close
        # response. Retain the raw socket to model a peer that does not close.
        sock = conn.sock
        conn.sock = None
        try:
            # Keep the sending side open without sending the promised body.
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(413, response.status)
            self.assertTrue(json.loads(response.read())['error'])
            self.finished.get(timeout=2)
            self.assertEqual(b'', sock.recv(1))
        finally:
            sock.close()
            conn.close()
        healthy = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            healthy.request('GET', '/login')
            response = healthy.getresponse(); self.assertEqual(200, response.status); response.read()
        finally:
            healthy.close()
        self.assertEqual(self.before, self.snapshot())

    def test_discard_has_byte_bound_and_does_not_parse_pipelined_bytes(self):
        class Connection:
            def settimeout(self, value): pass
            def shutdown(self, value): pass
        class Stream:
            total = 0
            def read1(self, size):
                self.total += size
                return b'x'*size
            def flush(self): pass
        class Handler:
            connection = Connection()
            rfile = Stream()
            wfile = Stream()
            def _respond_json(self, payload, *, status, close):
                self.sent = (payload, status, close)
        handler = Handler()
        # Freeze time so the byte cap, independently of the deadline, must stop it.
        with patch.object(http_rejection.time, 'monotonic', return_value=1):
            http_rejection.reject_json(handler, {'error':'denied'}, status=413)
        self.assertEqual(http_rejection.MAX_DRAIN_BYTES, handler.rfile.total)
        self.assertTrue(handler.close_connection)
        self.assertEqual(({'error':'denied'}, 413, True), handler.sent)


if __name__ == '__main__':
    unittest.main()
