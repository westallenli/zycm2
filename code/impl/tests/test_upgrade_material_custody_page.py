"""Anonymous receipt UI/HTTP integration; failed requests never settle or mutate stock."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from panda_brain.panel import make_server, render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_custody_receipts as fixture


class CustodyPageTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files'):
        locals()[name] = getattr(fixture.CustodyReceiptTests, name)

    def setUp(self):
        fixture.CustodyReceiptTests.setUp(self)
        self.commander = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)

    def form(self, **changes):
        member = self.state.member_session(self.tokens[self.LOGISTICS])
        return dict(event_id=self.event_id, csrf=member.custody_csrf, loan_transfer_id='loan-1',
                    expected_version='0', request_id='http-receipt-1', quantity='2',
                    damaged_quantity='1', location='模拟接收点', custodian='匿名保管人',
                    evidence='匿名接收清单', occurred_at='') | changes

    def server(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def post(self, base, fields=None, *, phone=None, raw=None):
        token = self.tokens[phone or self.LOGISTICS]
        payload = raw if raw is not None else urllib.parse.urlencode(fields or self.form()).encode()
        request = urllib.request.Request(base + '/material-custody/report', data=payload,
            headers={'Cookie': 'member_session=' + token,
                     'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_page_permission_read_only_sources_escape_and_existing_leader_entry(self):
        before = self.files()
        code, page = render_page(self.state, '/material-custody',
            member_token=self.tokens[self.LOGISTICS], query={'event_id': [self.event_id]})
        self.assertEqual(200, code)
        self.assertIn('仍待接收 4', page)
        self.assertIn('原借用与撤离依据', page)
        self.assertIn('登记本次实收', page)
        self.assertNotIn('确认结清', page)
        self.assertEqual(before, self.files())
        entry = render_page(self.state, '/temporary/leader',
                            member_token=self.tokens[self.LOGISTICS])[1]
        self.assertIn('/material-custody?event_id=' + self.event_id, entry)
        for actor in (None, self.DEPUTY, self.SOURCE, self.TARGET, self.REPLACEMENT):
            self.assertEqual(404, render_page(self.state, '/material-custody',
                member_token=self.tokens.get(actor), session_token=self.commander)[0])
        self.assertEqual(404, render_page(self.state, '/material-custody',
            member_token=self.tokens[self.LOGISTICS], query={'event_id': ['other']})[0])
        self.receipt(location='<script>bad()</script>', custodian='<保管人>', evidence='依据<script>')
        page = render_page(self.state, '/material-custody', member_token=self.tokens[self.LOGISTICS])[1]
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', page)
        self.assertNotIn('<script>bad()', page)

    def test_http_partial_full_receipt_and_replay_keep_materials_and_liability(self):
        base = self.server()
        ledger = self.state.ledger.to_dict()
        status, data = self.post(base)
        self.assertEqual(200, status)
        self.assertIn('累计实收 2', data['card_html'])
        self.assertIn('仍待接收 2', data['card_html'])
        first_id = data['record_id']
        self.assertEqual(200, self.post(base, self.form(expected_version='1', request_id='http-receipt-2',
                                                       quantity='2', damaged_quantity='0'))[0])
        before = self.files()
        status, data = self.post(base)
        self.assertEqual(200, status)
        self.assertEqual(first_id, data['record_id'])
        self.assertIn('仍待接收 0', data['card_html'])
        self.assertNotIn('action="/material-custody/report"', data['card_html'])
        self.assertIn('业务责任未结清', data['card_html'])
        self.assertEqual(before, self.files())
        self.assertEqual(ledger, self.state.ledger.to_dict())

    def test_http_wrong_identity_csrf_event_duplicate_unknown_and_limit_no_write(self):
        base = self.server()
        before = self.files()
        for actor in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            self.assertEqual(403, self.post(base, phone=actor)[0])
        for values, expected in ((dict(csrf='wrong'), 403), (dict(csrf='错误'), 403),
                (dict(event_id='another'), 403), (dict(expected_version='-1'), 409),
                (dict(expected_version='1'), 409), (dict(quantity='nan'), 409),
                (dict(damaged_quantity='3'), 409), (dict(loan_transfer_id='other-loan'), 409)):
            with self.subTest(values=values):
                self.assertEqual(expected, self.post(base, self.form(**values))[0])
        encoded = urllib.parse.urlencode(self.form()).encode()
        self.assertEqual(409, self.post(base, raw=encoded + b'&quantity=2')[0])
        self.assertEqual(409, self.post(base, raw=encoded + b'&role=logistics_admin')[0])
        self.assertEqual(413, self.post(base, raw=b'x' * 8193)[0])
        self.assertEqual(before, self.files())
        self.appoint(self.REPLACEMENT)
        self.assertEqual(403, self.post(base)[0])

    def test_http_save_failure_returns_retryable_error_and_original_request_succeeds(self):
        base = self.server()
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous receipt post-write failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            status, data = self.post(base)
        self.assertEqual(503, status)
        self.assertIn('原请求', data['message'])
        self.assertEqual(before, self.files())
        self.assertEqual(200, self.post(base)[0])

    def test_empty_and_inconsistent_sources_hide_form_and_quantities(self):
        with patch('panda_brain.material_custody_receipts.summary', side_effect=ValueError('missing mirror')):
            status, page = render_page(self.state, '/material-custody', member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200, status)
        self.assertIn('来源或审计待核对', page)
        self.assertNotIn('<form class="custody-form"', page)
        self.assertNotIn('仍待接收 4', page)
        self.state.ledger.withdrawal_records.clear()
        status, page = render_page(self.state, '/material-custody', member_token=self.tokens[self.LOGISTICS])
        self.assertEqual(200, status)
        self.assertIn('当前没有借入队撤离后的装备接收待办', page)


if __name__ == '__main__':
    unittest.main()
