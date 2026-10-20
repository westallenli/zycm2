"""Anonymous actual HTTP/UI contract for named custody verification/settlement."""
import json
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_custody_page as page_fixture
from . import test_upgrade_material_custody_settlements as domain_fixture


class CustodyDecisionPageTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'server'):
        locals()[name] = getattr(page_fixture.CustodyPageTests, name)
    for name in ('status', 'verify', 'settle', 'full'):
        locals()[name] = getattr(domain_fixture.CustodySettlementTests, name)

    def setUp(self):
        page_fixture.CustodyPageTests.setUp(self)

    def verify_form(self, batch, **changes):
        return dict(event_id=self.event_id, csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            loan_transfer_id='loan-1', receipt_id=batch.id, result='passed', conclusion='模拟逐批核对相符',
            evidence='模拟核验原始依据', request_id='http-verify-1', expected_version='0',
            expected_receipt_version='1', occurred_at='') | changes

    def settle_form(self, **changes):
        return dict(event_id=self.event_id, csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            loan_transfer_id='loan-1', evidence='模拟整笔结清确认', request_id='http-settle-1',
            expected_version='0', expected_receipt_version='1', expected_verification_version='1',
            handling_evidence='', handling_conclusion='', confirm='yes', occurred_at='') | changes

    def post_decision(self, base, route, fields, *, phone=None, raw=None, content_type=None):
        payload = raw if raw is not None else urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(base + '/material-custody/' + route, data=payload,
            headers={'Cookie': 'member_session=' + self.tokens[phone or self.LOGISTICS],
                     'Content-Type': content_type or 'application/x-www-form-urlencoded'})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def page(self):
        return render_page(self.state, '/material-custody', member_token=self.tokens[self.LOGISTICS])[1]

    def test_full_http_damage_explicit_confirm_sources_and_original_replay_zero_write(self):
        base = self.server()
        ledger = self.state.ledger.to_dict()
        materials = self.state.materials_path.read_bytes()
        batch = self.receipt(quantity=4, damaged_quantity=1)
        self.assertNotIn('action="/material-custody/settle"', self.page())
        code, verified = self.post_decision(base, 'verify', self.verify_form(batch))
        self.assertEqual(200, code)
        self.assertIn('最新核验：通过', verified['card_html'])
        self.assertIn('action="/material-custody/settle"', verified['card_html'])
        self.assertIn('name="handling_evidence" required', verified['card_html'])
        self.assertFalse(self.status().settled)
        before = self.files()
        for values in (dict(confirm='no'), dict(confirm=''), {},
                       dict(handling_evidence='已核实模拟处置记录')):
            self.assertEqual(409, self.post_decision(base, 'settle', self.settle_form(**values))[0])
            self.assertEqual(before, self.files())
        fields = self.settle_form(handling_evidence='已核实模拟处置记录', handling_conclusion='模拟隔离保管，不计可用')
        code, settled = self.post_decision(base, 'settle', fields)
        self.assertEqual(200, code)
        self.assertTrue(self.status().settled)
        self.assertIn('已具名确认整笔业务结清', settled['card_html'])
        self.assertIn('原流水未归还 4', settled['card_html'])
        self.assertIn('累计损坏 1', settled['card_html'])
        for operation in ('report', 'verify', 'settle'):
            self.assertNotIn('action="/material-custody/' + operation + '"', settled['card_html'])
        self.assertIn('action="/material-custody/correct"', settled['card_html'])
        self.assertIn(verified['record_id'], settled['card_html'])
        self.assertIn('整笔结清依据与冻结来源', settled['card_html'])
        self.assertNotIn('借用业务责任未结清', self.page())
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay must not save')):
            self.assertEqual(verified['record_id'], self.post_decision(base, 'verify', self.verify_form(batch))[1]['record_id'])
            self.assertEqual(settled['record_id'], self.post_decision(base, 'settle', fields)[1]['record_id'])
        self.assertEqual(before, self.files())
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(materials, self.state.materials_path.read_bytes())

    def test_partial_and_latest_failed_or_unknown_hide_settlement_no_damage_has_no_required_handling(self):
        base = self.server()
        batch = self.receipt(damaged_quantity=0)
        self.assertEqual(200, self.post_decision(base, 'verify', self.verify_form(batch))[0])
        self.assertNotIn('action="/material-custody/settle"', self.page())
        self.assertIn('仍待接收 2', self.page())
        before = self.files()
        self.assertEqual(409, self.post_decision(base, 'settle', self.settle_form())[0])
        self.assertEqual(before, self.files())
        second = self.receipt(quantity=2, damaged_quantity=0, expected_version=1, request_id='receipt-2')
        for version, result in enumerate(('unknown', 'failed', 'passed'), 1):
            code, body = self.post_decision(base, 'verify', self.verify_form(second, result=result,
                expected_version=str(version), expected_receipt_version='2', request_id='http-verify-' + str(version+1)))
            self.assertEqual(200, code)
            self.assertEqual(result == 'passed', 'action="/material-custody/settle"' in body['card_html'])
        self.assertNotIn('name="handling_evidence" required', self.page())
        self.assertEqual(200, self.post_decision(base, 'settle', self.settle_form(
            expected_receipt_version='2', expected_verification_version='4'))[0])

    def test_both_routes_enforce_identity_csrf_event_versions_fields_limit_and_terminal(self):
        base = self.server()
        batch, _ = self.full()
        for route, original in (('verify', self.verify_form(batch, expected_version='1', request_id='verify-new')),
                                ('settle', self.settle_form())):
            before = self.files()
            for actor in (self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
                self.assertEqual(403, self.post_decision(base, route, original, phone=actor)[0])
            for change, expected in ((dict(csrf='wrong'), 403), (dict(event_id='wrong'), 403),
                    (dict(expected_version='-1'), 409), (dict(expected_version='１'), 409),
                    (dict(expected_receipt_version='0'), 409), (dict(loan_transfer_id='wrong'), 409),
                    (dict(occurred_at='2026-10-09T12:00:00'), 409), (dict(evidence=''), 409)):
                with self.subTest(route=route, change=change):
                    self.assertEqual(expected, self.post_decision(base, route, original | change)[0])
            encoded = urllib.parse.urlencode(original).encode()
            self.assertEqual(409, self.post_decision(base, route, original, raw=encoded+b'&expected_version=0')[0])
            self.assertEqual(409, self.post_decision(base, route, original, raw=encoded+b'&role=admin')[0])
            missing = dict(original)
            missing.pop('evidence')
            self.assertEqual(409, self.post_decision(base, route, missing)[0])
            self.assertEqual(409, self.post_decision(base, route, original, content_type='application/json')[0])
            self.assertEqual(413, self.post_decision(base, route, original, raw=b'x'*8193)[0])
            self.assertEqual(before, self.files())
        self.assertEqual(200, self.post_decision(base, 'settle', self.settle_form())[0])
        before = self.files()
        self.assertEqual(409, self.post_decision(base, 'verify', self.verify_form(batch,
            expected_version='1', request_id='verify-terminal'))[0])
        self.assertEqual(409, self.post_decision(base, 'settle', self.settle_form(
            expected_version='1', request_id='settle-terminal'))[0])
        self.assertEqual(before, self.files())

    def test_http_postwrite_failures_keep_original_requests_retryable_for_both_actions(self):
        base = self.server()
        batch = self.receipt(quantity=4, damaged_quantity=0)
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous decision page post-write failure')
        for route, fields in (('verify', self.verify_form(batch)), ('settle', self.settle_form())):
            before = self.files()
            with patch('panda_brain.persistence.save_workspace', side_effect=fail):
                code, body = self.post_decision(base, route, fields)
            self.assertEqual(503, code)
            self.assertIn('保留原请求', body['message'])
            self.assertEqual(before, self.files())
            self.assertEqual(200, self.post_decision(base, route, fields)[0])

    def test_page_escapes_all_new_sources_historical_labels_and_fail_closed_audit(self):
        batch = self.receipt(quantity=4, damaged_quantity=1)
        verification = self.verify(batch, conclusion='<script>bad()</script>', evidence='<核验依据>')
        self.settle(evidence='<结清依据>', handling_evidence='<处理依据>', handling_conclusion='<处理结论>')
        before = self.files()
        page = self.page()
        for value in ('&lt;script&gt;bad()&lt;/script&gt;', '&lt;核验依据&gt;', '&lt;结清依据&gt;',
                      '&lt;处理依据&gt;', '&lt;处理结论&gt;', verification.id, '发生时间：未知'):
            self.assertIn(value, page)
        self.assertNotIn('<script>bad()', page)
        self.assertEqual(before, self.files())
        with patch('panda_brain.material_custody_settlements.summary', side_effect=ValueError('frozen source mismatch')):
            page = self.page()
        self.assertIn('来源或审计待核对', page)
        self.assertNotIn('已具名确认整笔业务结清', page)
        self.assertNotIn('<form', page.split('<main id="material-custody"',1)[1].split('</main>',1)[0])
        self.assertNotIn('原流水未归还 4', page)

    def test_replacement_revokes_forms_closed_event_keeps_authorized_material_replay(self):
        base = self.server()
        batch, _ = self.full()
        old_verify, old_settle = self.verify_form(batch), self.settle_form()
        self.appoint(self.REPLACEMENT)
        before = self.files()
        self.assertEqual(403, self.post_decision(base, 'verify', old_verify)[0])
        self.assertEqual(403, self.post_decision(base, 'settle', old_settle)[0])
        replacement_csrf = self.state.member_session(self.tokens[self.REPLACEMENT]).custody_csrf
        self.assertEqual(200, self.post_decision(base, 'settle', old_settle | dict(csrf=replacement_csrf),
                                               phone=self.REPLACEMENT)[0])
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        self.assertEqual(200, self.post_decision(base, 'settle', old_settle | dict(csrf=replacement_csrf),
                                               phone=self.REPLACEMENT)[0])
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
