"""Command drafting transport: frozen evidence, strict forms and durable retry."""
from dataclasses import asdict, replace
import json
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from panda_brain import material_supply_draft_page as page
from panda_brain.panel import render_page
from panda_brain.persistence import save_workspace
from panda_brain.task_sop import render_deployment
from . import test_upgrade_material_supply_drafts as fixture
from . import test_upgrade_material_supply_page as transport


class SupplyDraftPageTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'event_space', 'appoint', 'report', 'view', 'files', 'bundle',
                 'attach', 'draft_view', 'next_inputs', 'edit', 'setUp'):
        locals()[name] = getattr(fixture.SupplyDraftTests, name)
    server = transport.SupplyContactPageTests.server

    def query(self, **changes):
        return dict(event_id=[self.event_id], decision_id=[self.decision_id],
                    task_id=[self.task_id], item_id=[self.item_id]) | changes

    def render(self, **changes):
        return render_page(self.state, '/supply-draft', session_token=self.token,
                           query=self.query(**changes))

    def form(self, **changes):
        fields = {k: '' if v is None else str(v) for k, v in self.inputs.items()
                  if k != 'session_token'}
        return fields | dict(event_id=self.event_id, csrf=page.csrf(self.state, self.token),
            contact_id='', internal_contact_ids=[''], internal_priority_reason='',
            destination_hint='') | changes

    def post(self, base, fields=None, *, token=None, raw=None, content_type=None):
        request = urllib.request.Request(base + '/supply-draft/save',
            data=raw if raw is not None else urllib.parse.urlencode(
                self.form() if fields is None else fields, doseq=True).encode(),
            headers={'Cookie': 'panel_session=' + (self.token if token is None else token),
                     'Content-Type': content_type or 'application/x-www-form-urlencoded'})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_read_only_command_entry_and_session_scoped_csrf(self):
        before = self.files()
        status, html = self.render()
        self.assertEqual(200, status)
        for text in ('人工需求量', '未知留空', '逐版附件与冻结依据', '/static/supply-draft.js',
                     '不占用或增加库存', '供给联系报告', '库内联系依据'):
            self.assertIn(text, html)
        self.assertEqual(before, self.files())
        for actor in self.tokens:
            self.assertEqual(404, render_page(self.state, '/supply-draft',
                member_token=self.tokens[actor], query=self.query())[0])
        self.assertEqual(404, render_page(self.state, '/supply-draft', query=self.query())[0])
        self.assertEqual(404, self.render(event_id=['other'])[0])
        self.assertEqual(409, self.render(item_id=[self.item_id, ''])[0])
        self.assertEqual(409, self.render(actor=['forged'])[0])
        entry = render_deployment(self.state, role='commander')
        self.assertIn('编制本任务补给附件', entry)
        self.assertIn(urllib.parse.urlencode(dict(event_id=self.event_id,
            decision_id=self.decision_id, task_id=self.task_id)).replace('&', '&amp;'), entry)
        another = self.state.login('p08-transfer-demo')
        self.assertNotEqual(page.csrf(self.state, another), page.csrf(self.state, self.token))

    def test_http_unknown_zero_company_freeze_and_response_identity_without_inventory(self):
        internal = self.report(available_quantity=0)
        company = self.report(source_kind='company', source_id=self.company_id,
            expected_source_digest=self.view(source_kind='company', source_id=self.company_id).current_source_digest,
            request_id='company-contact', available_quantity=8)
        before, ledger, original = self.files(), self.state.ledger.to_dict(), self.bundle
        base = self.server()
        code, result = self.post(base)
        self.assertEqual(200, code)
        for key in ('event_id', 'decision_id', 'task_id', 'item_id'):
            self.assertEqual(self.form()[key], result[key])
        self.assertEqual('supply-draft-1', result['request_id'])
        self.assertIn('人工需求 未知', result['main_html'])
        self.assertIn('data-base-bundle-digest="' + self.inputs['expected_bundle_digest'], result['main_html'])
        code, zero = self.post(base, self.form(**self.next_inputs(), requested_quantity='0', quantity_basis='人工零需求'))
        self.assertEqual(200, code)
        self.assertIn('人工需求 0', zero['main_html'])
        new = self.next_inputs() | {'request_id': 'supply-draft-3'}
        code, last = self.post(base, self.form(**new, requested_quantity='4', quantity_basis='人工申报',
            contact_id=company.id, internal_contact_ids=['', internal.id],
            internal_priority_reason='库内报告无货，尚未独立核实', destination_hint='接收位置待核'))
        self.assertEqual(200, code)
        frozen = json.loads(self.draft_view().records[-1].content_json)
        self.assertEqual([asdict(company), asdict(internal)], frozen['contact_snapshots'])
        self.assertIsNone(frozen['automatic_gap'])
        self.assertIn('未做独立核验', last['main_html'])
        self.assertEqual((4, 3), (self.bundle.version, self.draft_view().version))
        self.assertEqual(original.tasks, self.bundle.tasks)
        self.assertEqual(ledger, self.state.ledger.to_dict())
        self.assertEqual(before[self.state.materials_path], self.state.materials_path.read_bytes())
        for name in ('orders', 'command_flows', 'approvals'):
            self.assertFalse(getattr(self.state.workspace, name))

    def test_rejected_transport_and_business_conflicts_preserve_files(self):
        base, before = self.server(), self.files()
        for token in ('', self.tokens[self.LOGISTICS], self.tokens[self.DEPUTY]):
            self.assertEqual(403, self.post(base, token=token)[0])
        cases = [(403, dict(csrf='错误')), (403, dict(event_id='other')),
                 (409, dict(task_id='other')), (409, dict(decision_id='other')),
                 (409, dict(item_id='other')), (409, dict(expected_bundle_version='2')),
                 (409, dict(expected_version='1')), (409, dict(expected_bundle_version='-1')),
                 (409, dict(expected_version='０')), (409, dict(expected_item_digest='old')),
                 (409, dict(expected_bundle_digest='old')), (409, dict(requested_quantity='nan')),
                 (409, dict(requested_quantity='inf')), (409, dict(requested_quantity='-1')),
                 (409, dict(requested_quantity='0')), (409, dict(contact_id='missing')),
                 (409, dict(needed_by='2026-10-10T18:00:00')), (409, dict(evidence='')),
                 (409, dict(quantity_basis='x'*201)), (409, dict(internal_contact_ids=['', ''])),
                 (409, dict(internal_contact_ids=[]))]
        for expected, changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(expected, self.post(base, self.form(**changes))[0])
        for key in ('csrf', 'requested_quantity', 'internal_contact_ids', 'expected_version'):
            fields = self.form()
            fields.pop(key)
            self.assertEqual(409, self.post(base, fields)[0])
        encoded = urllib.parse.urlencode(self.form(), doseq=True).encode()
        for raw in (encoded+b'&requested_quantity=0', encoded+b'&actor_id=forged', b'\xff', encoded+b'&evidence=%FF'):
            self.assertEqual(409, self.post(base, raw=raw)[0])
        self.assertEqual(409, self.post(base, content_type='application/json')[0])
        self.assertEqual(413, self.post(base, raw=b'a'*32769)[0])
        another = self.state.login('p08-transfer-demo')
        self.assertEqual(403, self.post(base, token=another)[0])
        self.assertEqual(before, self.files())

    def test_stale_contact_disables_choice_and_rejects_new_attachment(self):
        first = self.report(available_quantity=2)
        self.report(expected_version=1, request_id='contact-2', available_quantity=3)
        before, base = self.files(), self.server()
        self.assertEqual(409, self.post(base, self.form(contact_id=first.id))[0])
        self.state.ledger.update_point_stock(self.point_id, self.item_id, value=5)
        self.state._save_ledger()
        html = self.render()[1]
        self.assertIn('来源已变，待核', html)
        self.assertIn(' disabled>', html)
        before = self.files()
        latest = self.view().records[-1]
        self.assertEqual(409, self.post(base, self.form(contact_id=latest.id))[0])
        self.assertEqual(before, self.files())

    def test_history_escaped_and_removed_item_remains_reachable_without_form(self):
        item = self.state.ledger.items[self.item_id]
        self.state.ledger.items[self.item_id] = replace(item, name='<物资>', unit='<单位>')
        self.state._save_ledger()
        self.inputs.update(expected_item_digest=fixture._digest(asdict(
            self.state.ledger.items[self.item_id])))
        record = self.attach(requested_quantity=3, quantity_basis='<img src=x onerror=alert(1)>',
                             evidence='<script>alert(1)</script>')
        html = self.render()[1]
        for text in ('&lt;物资&gt;', '&lt;单位&gt;', '&lt;script&gt;alert(1)&lt;/script&gt;'):
            self.assertIn(text, html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('<img src=x', html)
        self.state.ledger.items.pop(self.item_id)
        self.state._save_ledger()
        html = self.render()[1]
        self.assertIn('历史资料', html)
        self.assertIn(record.id, html)
        self.assertNotIn('class="supply-draft-form"', html)

    def test_inconsistent_mirror_hides_values_and_blocks_write(self):
        record = self.attach(requested_quantity=3, quantity_basis='人工申报')
        space = self.event_space()
        space.object_revisions.pop(record.id)
        save_workspace(space, self.state.registry.events[self.event_id].snapshot_path)
        before = self.files()
        status, html = self.render()
        self.assertEqual(200, status)
        self.assertIn('审计来源待核对', html)
        self.assertNotIn('人工需求 3', html)
        self.assertNotIn('class="supply-draft-form"', html)
        self.assertEqual(409, self.post(self.server())[0])
        self.assertEqual(before, self.files())

    def test_http_write_failure_rolls_back_then_original_retry_and_replay_after_edit(self):
        base, before, original = self.server(), self.files(), self.bundle
        def fail(space, target):
            save_workspace(space, target)
            raise OSError('anonymous attachment postwrite failure')
        with patch('panda_brain.persistence.save_workspace', side_effect=fail):
            code, error = self.post(base)
        self.assertEqual(503, code)
        self.assertIn('原输入和请求号', error['message'])
        self.assertEqual(before, self.files())
        self.assertEqual(original, self.bundle)
        code, first = self.post(base)
        self.assertEqual(200, code)
        self.edit()
        before = self.files()
        with patch('panda_brain.persistence.save_workspace', side_effect=AssertionError('replay saved')):
            code, replay = self.post(base)
        self.assertEqual(200, code)
        self.assertEqual(first['record_id'], replay['record_id'])
        self.assertEqual(before, self.files())
        self.assertIn('data-request="supply-draft-1"', replay['main_html'])
        self.assertFalse(self.draft_view().current)

    def test_closed_read_only_and_http_cache_headers_strict_queries(self):
        self.attach()
        base = self.server()
        self.state.registry.events[self.event_id] = replace(
            self.state.registry.events[self.event_id], status='closed')
        self.state._save_registry()
        before = self.files()
        query = urllib.parse.urlencode({k: v[0] for k, v in self.query().items()})
        request = urllib.request.Request(base+'/supply-draft?'+query,
                                         headers={'Cookie': 'panel_session='+self.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode()
            self.assertEqual(200, response.status)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual('no-referrer', response.headers['Referrer-Policy'])
        self.assertIn('附件第 1 版', html)
        self.assertNotIn('class="supply-draft-form"', html)
        self.assertEqual(403, self.post(base)[0])
        for suffix in ('&item_id=', '&actor=forged', '&item_id=%FF'):
            request.full_url = base+'/supply-draft?'+query+suffix
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(409, error.exception.code)
            error.exception.close()
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
