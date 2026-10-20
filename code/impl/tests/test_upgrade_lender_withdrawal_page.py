"""Effective settlement projection and bounded HTTP departure on anonymous fixtures."""
from dataclasses import replace
import json
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from panda_brain import material_withdrawals as departures
from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_lender_material_withdrawals as fixture
from . import test_upgrade_material_custody_page as page_fixture


class LenderWithdrawalPageTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'full', 'verify', 'settle', 'status',
                 'report', 'review', 'third_team', 'loan_two'):
        locals()[name] = getattr(fixture.LenderWithdrawalTests, name)
    server = page_fixture.CustodyPageTests.server

    def setUp(self):
        fixture.LenderWithdrawalTests.setUp(self)

    def page(self):
        return render_page(self.state, '/captain-arrival', member_token=self.tokens[self.SOURCE])[1]

    def form(self, **changes):
        return dict(event_id=self.event_id, squad_id=self.source_squad,
            csrf=self.state.member_session(self.tokens[self.SOURCE]).custody_csrf,
            evidence='匿名HTTP撤离依据', request_id='http-lender', occurred_at='') | changes

    def post(self, base, fields=None, *, phone=None, raw=None, content_type=None):
        fields = self.form() if fields is None else fields
        request = urllib.request.Request(base + '/captain-arrival/material-withdrawal',
            data=raw if raw is not None else urllib.parse.urlencode(fields, doseq=True).encode(),
            headers={'Cookie': 'member_session=' + self.tokens[phone or self.SOURCE],
                     'Content-Type': content_type or 'application/x-www-form-urlencoded'})
        opener = urllib.request.build_opener(type('NoRedirect', (urllib.request.HTTPRedirectHandler,),
            {'redirect_request': lambda self, *args, **kwargs: None})())
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode('utf-8')

    def test_only_effective_explicit_settlement_opens_form_and_preserves_read_only_quantities(self):
        self.assertNotIn('action="/captain-arrival/material-withdrawal"', self.page())
        self.full()
        self.assertNotIn('action="/captain-arrival/material-withdrawal"', self.page())
        settlement = self.settle()
        before = self.files()
        page = self.page()
        self.assertIn('action="/captain-arrival/material-withdrawal"', page)
        self.assertIn(settlement.id, page)
        self.assertIn('当前可用 6', page)
        self.assertIn('后勤实收不计入本队撤出量或现场可用', page)
        self.assertNotIn('name="loan_transfer_id"', page)
        self.assertIn('name="csrf"', page)
        self.assertIn('/static/material-withdrawal.js', page)
        self.assertEqual(before, self.files())

    def test_mixed_borrowing_form_retains_only_unsettled_borrower_custody(self):
        self.full()
        self.settle()
        self.loan_two(incoming=True)
        page = self.page()
        self.assertIn('name="loan_transfer_id" value="loan-2"', page)
        self.assertNotIn('name="loan_transfer_id" value="loan-1"', page)
        self.assertIn('当前可用 8', page)

    def test_existing_frozen_source_conflict_hides_business_claim_and_quantities(self):
        self.full()
        self.settle()
        record = self.report()
        page = self.page()
        self.assertIn('已核对借出结清引用', page)
        self.assertIn('data-withdrawal-record=', page)
        self.assertNotIn('action="/captain-arrival/material-withdrawal"', page)
        content = json.loads(record.content_json)
        content['settled_lender_loans'][0]['settlement_id'] = 'wrong-source'
        self.state.ledger.withdrawal_records[record.id] = replace(record, content_json=json.dumps(content))
        page = self.page()
        self.assertIn('本队物资来源或数量待核对', page)
        self.assertNotIn('已核对借出结清引用', page)
        self.assertNotIn('随队撤出 6', page)
        self.assertNotIn('data-withdrawal-record=', page)

    def test_http_valid_departure_and_original_replay_are_zero_duplicate_writes(self):
        self.full()
        settlement = self.settle()
        base = self.server()
        self.assertEqual(303, self.post(base)[0])
        record = departures.withdrawal_record(self.state.ledger, event_id=self.event_id, squad_id=self.source_squad)
        self.assertEqual(settlement.id, json.loads(record.content_json)['settled_lender_loans'][0]['settlement_id'])
        before = self.files()
        with (patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('must not save')),
              patch('panda_brain.materials.MaterialsLedger.save', side_effect=AssertionError('must not save'))):
            self.assertEqual(303, self.post(base)[0])
        self.assertEqual(before, self.files())
        self.assertIn(record.id, self.page())

    def test_http_identity_csrf_fields_event_and_body_limit_reject_without_writes(self):
        self.full()
        self.settle()
        base = self.server()
        before = self.files()
        cases = [(403, dict(csrf='wrong')), (403, dict(squad_id=self.target_squad)),
                 (409, dict(event_id='other-event')), (409, dict(settled_lender_loans='forged')),
                 (409, dict(request_id=['one', 'two']))]
        for expected, change in cases:
            with self.subTest(change=change):
                self.assertEqual(expected, self.post(base, self.form(**change))[0])
            self.assertEqual(before, self.files())
        for phone in (self.DEPUTY, self.LOGISTICS, self.TARGET, self.OTHER):
            fields = self.form(csrf=self.state.member_session(self.tokens[phone]).custody_csrf)
            self.assertEqual(403, self.post(base, fields, phone=phone)[0])
        fields = self.form()
        fields.pop('csrf')
        self.assertEqual(409, self.post(base, fields)[0])
        self.assertEqual(413, self.post(base, raw=b'a' * 8193)[0])
        self.assertEqual(409, self.post(base, raw=b'\xff')[0])
        self.assertEqual(409, self.post(base, content_type='application/json')[0])
        self.assertEqual(before, self.files())

    def test_http_unsettled_and_closed_event_reject_and_save_failure_allows_original_retry(self):
        base = self.server()
        before = self.files()
        self.assertEqual(409, self.post(base)[0])
        self.assertEqual(before, self.files())
        self.full()
        self.settle()
        before = self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous HTTP post-write failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            code, message = self.post(base)
            self.assertEqual(503, code)
            self.assertIn('保留原输入和请求号', message)
        self.assertEqual(before, self.files())
        self.assertEqual(303, self.post(base)[0])
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        self.assertEqual(409, self.post(base)[0])
        self.assertEqual(before, self.files())
