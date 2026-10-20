"""Anonymous member page/HTTP: contacts remain reports, never stock or tasks."""
from dataclasses import replace
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from panda_brain.panel import make_server, render_page
from panda_brain.persistence import save_workspace
from . import test_upgrade_material_supply_contacts as fixture


class SupplyContactPageTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'event_space', 'appoint', 'report', 'view', 'files'):
        locals()[name] = getattr(fixture.SupplyContactTests, name)

    def setUp(self):
        fixture.SupplyContactTests.setUp(self)
        self.commander = self.state.login('p08-transfer-demo')
        self.state.handle_enter_event(self.event_id)

    def page(self, **changes):
        query = {'event_id': [self.event_id], 'item_id': [self.item_id],
                 'source': ['point:' + self.point_id]}
        return render_page(self.state, '/material-supply', member_token=self.tokens[self.LOGISTICS],
                           query=query | changes)

    def form(self, **changes):
        return dict(event_id=self.event_id,
            csrf=self.state.member_session(self.tokens[self.LOGISTICS]).custody_csrf,
            item_id=self.item_id, source_kind='point', source_id=self.point_id,
            expected_source_digest=self.digest, available_quantity='', promised_delivery_at='',
            evidence='匿名HTTP联系依据', request_id='http-contact-1', expected_version='0',
            occurred_at='') | changes

    def server(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def post(self, base, fields=None, *, actor=None, raw=None, content_type=None):
        request = urllib.request.Request(base + '/material-supply/report',
            data=raw if raw is not None else urllib.parse.urlencode(self.form() if fields is None else fields, doseq=True).encode(),
            headers={'Cookie': 'member_session=' + self.tokens[actor or self.LOGISTICS],
                     'Content-Type': content_type or 'application/x-www-form-urlencoded'})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_page_only_current_leader_read_only_and_existing_entry_includes_link(self):
        before = self.files()
        status, page = self.page()
        self.assertEqual(200, status)
        for text in ('人工补给联系', '库内物资点', '外部公司资料', '登记库存 未知',
                     '登记下一次人工联系', '未知留空；无货填0', '/static/material-supply.js'):
            self.assertIn(text, page)
        self.assertIn('name="expected_source_digest" value="' + self.digest + '"', page)
        self.assertEqual(before, self.files())
        entry = render_page(self.state, '/temporary/leader', member_token=self.tokens[self.LOGISTICS])[1]
        self.assertIn('/material-supply?event_id=' + self.event_id, entry)
        for actor in (None, self.DUTY, self.DEPUTY, self.SOURCE, self.OTHER, self.REPLACEMENT):
            self.assertEqual(404, render_page(self.state, '/material-supply',
                member_token=self.tokens.get(actor), session_token=self.commander)[0])
        self.assertEqual(404, self.page(event_id=['another'])[0])
        self.assertEqual(409, self.page(item_id=[self.item_id, self.item_id])[0])
        self.assertEqual(409, self.page(role=['logistics-admin'])[0])

    def test_http_unknown_zero_company_and_original_replay_keep_stock_and_tasks(self):
        base, before = self.server(), self.files()
        ledger, space = self.state.ledger.to_dict(), self.event_space()
        status, first = self.post(base)
        self.assertEqual(200, status)
        self.assertIn('可供量 未知', first['card_html'])
        self.assertEqual(200, self.post(base, self.form(expected_version='1', request_id='http-contact-2', available_quantity='0'))[0])
        company_digest = self.view(source_kind='company', source_id=self.company_id).current_source_digest
        status, company = self.post(base, self.form(source_kind='company', source_id=self.company_id,
            expected_source_digest=company_digest, request_id='company-contact', available_quantity='3',
            promised_delivery_at='2026-10-10T18:00:00+08:00'))
        self.assertEqual(200, status)
        self.assertIn('可供量 3', company['card_html'])
        files = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            status, replay = self.post(base)
        self.assertEqual(200, status)
        self.assertEqual(first['record_id'], replay['record_id'])
        self.assertIn('最新联系报告明确无可供量', replay['card_html'])
        self.assertEqual(files, self.files())
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        for name in ('orders', 'command_flows', 'task_drafts'):
            self.assertEqual(getattr(space, name), getattr(self.event_space(), name))

    def test_http_identity_csrf_event_fields_encoding_type_and_limit_write_nothing(self):
        base, before = self.server(), self.files()
        for actor in (self.DUTY, self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            self.assertEqual(403, self.post(base, actor=actor)[0])
        cases = [(403, dict(csrf='wrong')), (403, dict(csrf='错误')), (403, dict(event_id='other')),
                 (409, dict(available_quantity='nan')), (409, dict(available_quantity='-1')),
                 (409, dict(expected_version='1')), (409, dict(expected_source_digest='old')),
                 (409, dict(promised_delivery_at='2026-10-10T18:00:00')),
                 (409, dict(source_id='unknown')), (409, dict(evidence=''))]
        for expected, changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(expected, self.post(base, self.form(**changes))[0])
        for key in ('csrf', 'source_kind', 'available_quantity'):
            fields = self.form()
            fields.pop(key)
            self.assertEqual(409, self.post(base, fields)[0])
        encoded = urllib.parse.urlencode(self.form()).encode()
        self.assertEqual(409, self.post(base, raw=encoded+b'&available_quantity=0')[0])
        self.assertEqual(409, self.post(base, raw=encoded+b'&actor_id=forged')[0])
        self.assertEqual(409, self.post(base, raw=b'\xff')[0])
        self.assertEqual(409, self.post(base, raw=encoded+b'&evidence=%FF')[0])
        self.assertEqual(409, self.post(base, content_type='application/json')[0])
        self.assertEqual(413, self.post(base, raw=b'a'*8193)[0])
        self.assertEqual(before, self.files())
        self.appoint(self.REPLACEMENT)
        self.assertEqual(403, self.post(base)[0])

    def test_source_change_rejects_new_but_validates_original_replay_and_current_card(self):
        base = self.server()
        first = self.post(base)[1]
        self.state.ledger.update_point_stock(self.point_id, self.item_id, value=5)
        self.state._save_ledger()
        before = self.files()
        self.assertEqual(409, self.post(base, self.form(expected_version='1', request_id='http-contact-2'))[0])
        code, result = self.post(base)
        self.assertEqual(200, code)
        self.assertEqual(first['record_id'], result['record_id'])
        self.assertIn('冻结来源已与当前资料不同', result['card_html'])
        self.assertEqual(before, self.files())
        self.state.ledger.points.pop(self.point_id)
        self.state._save_ledger()
        status, page = self.page()
        self.assertEqual(200, status)
        self.assertIn('当前资料已移除', page)
        self.assertIn(first['record_id'], page)
        self.assertNotIn('class="supply-contact-form"', page)

    def test_html_escapes_contact_and_catalog_text_and_removed_item_history_stays_reachable(self):
        item = self.state.ledger.items[self.item_id]
        self.state.ledger.items[self.item_id] = replace(item, name='<物资>', unit='<单位>')
        self.state.ledger.points[self.point_id].name = '<img src=x onerror=alert(1)>'
        self.state._save_ledger()
        digest = self.view().current_source_digest
        self.report(expected_source_digest=digest, available_quantity=3,
                    evidence='<script>alert(1)</script>')
        page = self.page()[1]
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<img src=x', page)
        self.assertIn('&lt;单位&gt;', page)
        self.state.ledger.items.pop(self.item_id)
        self.state._save_ledger()
        page = render_page(self.state, '/material-supply', member_token=self.tokens[self.LOGISTICS])[1]
        self.assertIn('历史资料', page)
        self.assertIn('value="' + self.item_id + '"', page)
        self.assertIn('第 1 次联系报告', self.page()[1])
        self.assertNotIn('class="supply-contact-form"', self.page()[1])

    def test_inconsistent_mirror_hides_reported_quantity_and_form(self):
        record = self.report(available_quantity=3)
        space = self.event_space()
        space.object_revisions.pop(record.id)
        target = self.state.registry.events[self.event_id].snapshot_path
        save_workspace(space, target)
        code, page = self.page()
        self.assertEqual(200, code)
        self.assertIn('联系来源或审计待核对', page)
        self.assertNotIn('可供量 3', page)
        self.assertNotIn('class="supply-contact-form"', page)

    def test_http_postwrite_failure_restores_files_and_original_request_retries(self):
        base, before = self.server(), self.files()
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous contact HTTP save failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            code, payload = self.post(base)
        self.assertEqual(503, code)
        self.assertIn('原输入和请求号', payload['message'])
        self.assertEqual(before, self.files())
        self.assertEqual(200, self.post(base)[0])

    def test_http_cache_headers_and_closed_read_only_history_reject_post(self):
        base = self.server()
        self.post(base)
        request = urllib.request.Request(base+'/material-supply',
            headers={'Cookie': 'member_session='+self.tokens[self.LOGISTICS]})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual('no-store', response.headers.get('Cache-Control'))
            self.assertEqual('no-referrer', response.headers.get('Referrer-Policy'))
        self.state.registry.transition(self.event_id, 'close')
        self.state.registry.save(self.state.registry_path)
        before = self.files()
        code, page = self.page()
        self.assertEqual(200, code)
        self.assertIn('第 1 次联系报告', page)
        self.assertNotIn('class="supply-contact-form"', page)
        self.assertEqual(403, self.post(base)[0])
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
