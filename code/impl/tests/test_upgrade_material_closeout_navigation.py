"""Real login and archive reachability without new authority or context writes."""
from copy import deepcopy
import http.client
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain.panel import PanelActionError, render_page
from . import test_upgrade_material_closed_closeout as fixture


class CloseoutNavigationTests(unittest.TestCase):
    for name in ('DUTY', 'DEPUTY', 'SOURCE', 'TARGET', 'OTHER', 'LOGISTICS', 'REPLACEMENT',
                 'new_state', 'approve', 'send', 'receive', 'event_space', 'make_loan_received',
                 'appoint', 'receipt', 'view', 'files', 'server'):
        locals()[name] = getattr(fixture.ClosedCustodyTests, name)

    def setUp(self):
        fixture.custody.CustodyDecisionPageTests.setUp(self)

    def close(self):
        self.state.handle_event_action(self.event_id, 'close')

    def page(self, route='/materials/closeout', phone=None):
        return render_page(self.state, route,
                           member_token=self.tokens[phone or self.LOGISTICS])

    def request(self, base, method, route, *, cookie='', fields=None):
        connection = http.client.HTTPConnection('127.0.0.1', int(base.rsplit(':', 1)[1]), timeout=10)
        headers = {'Cookie': cookie}
        if fields is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            connection.request(method, route, urlencode(fields) if fields else None, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode('utf-8')
        finally:
            connection.close()

    def test_real_login_lands_on_closeout_and_all_return_paths_are_read_only(self):
        self.close()
        before = self.files()
        context = deepcopy(self.state.workspace)
        base = self.server()
        code, headers, _ = self.request(base, 'POST', '/login/member',
                                       fields={'phone': self.LOGISTICS, 'password': '123456'})
        self.assertEqual(303, code)
        self.assertEqual('/materials/closeout', headers['Location'])
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        for route in ('/materials/closeout', '/temporary/leader',
                      '/material-custody?' + urlencode({'event_id': self.event_id}),
                      '/material-supply/receipts'):
            with self.subTest(route=route):
                code, headers, page = self.request(base, 'GET', route, cookie=cookie)
                self.assertEqual(200, code)
                self.assertIn('/materials/closeout', page)
                if route in ('/materials/closeout', '/temporary/leader'):
                    self.assertIn('装备接收、核验与结清', page)
                    self.assertNotIn('/temporary/leader/readiness', page)
                    self.assertNotIn('/materials/maintenance-authority', page)
        self.assertEqual(before, self.files())
        self.assertEqual(context.events, self.state.workspace.events)
        self.assertEqual(context.version, self.state.workspace.version)
        with self.assertRaises(PanelActionError):
            self.state.handle_temporary_group_readiness(self.event_id,
                leader_token=self.tokens[self.LOGISTICS], pos_code='log-material',
                phones=[self.LOGISTICS], status='standby', basis='模拟旧写口',
                order_id='', request_id='closed-readiness')
        self.assertEqual(before, self.files())

    def test_closed_index_uses_current_named_leader_not_other_existing_roles(self):
        self.close()
        before = self.files()
        for phone in (self.DUTY, self.DEPUTY, self.SOURCE, self.TARGET, self.OTHER, self.REPLACEMENT):
            with self.subTest(phone=phone):
                code, page = self.page(phone=phone)
                self.assertEqual(404, code)
                self.assertNotIn(self.state.registry.events[self.event_id].name, page)
        self.assertEqual(404, render_page(self.state, '/materials/closeout')[0])
        self.assertEqual(404, render_page(self.state, '/materials/closeout',
                                       session_token=self.commander)[0])
        self.assertEqual([], self.state.member_temporary_leadership(self.tokens[self.LOGISTICS]))
        self.assertEqual([self.event_id], self.state.member_material_closeout_events(self.tokens[self.LOGISTICS]))
        self.assertEqual(before, self.files())

    def test_latest_preclose_appointment_revokes_old_leader_and_survives_restart(self):
        self.appoint(self.REPLACEMENT)
        self.close()
        self.assertEqual(404, self.page()[0])
        self.assertEqual(200, self.page(phone=self.REPLACEMENT)[0])
        self.state = self.new_state()
        for phone in (self.LOGISTICS, self.REPLACEMENT):
            self.tokens[phone], error = self.state.member_login(phone, '123456')
            self.assertEqual('', error)
        before = self.files()
        self.assertEqual(404, self.page()[0])
        self.assertEqual(200, self.page(phone=self.REPLACEMENT)[0])
        self.assertEqual(before, self.files())

    def test_locked_removed_and_logged_out_accounts_immediately_lose_navigation(self):
        self.close()
        before = self.files()
        credential = self.state.registry.credentials[self.LOGISTICS]
        credential.locked = True
        self.assertEqual(404, self.page()[0])
        credential.locked = False
        person = self.state.registry.library.pop(self.LOGISTICS)
        self.assertEqual(404, self.page()[0])
        self.state.registry.library[self.LOGISTICS] = person
        self.state.member_logout(self.tokens[self.LOGISTICS])
        self.assertEqual(404, self.page()[0])
        self.assertEqual(before, self.files())

    def test_archive_selection_does_not_enter_or_reopen_event(self):
        self.close()
        other = self.state.registry.create_event(name='另一事件', report_no='K10-NAV',
            discovered_at='2026-10-11T08:00:00+08:00', source='人工模拟', location='模拟位置')
        self.state.handle_event_action(other.event_id, 'start', duty_token=self.tokens[self.DUTY])
        self.state.handle_enter_event(other.event_id)
        context = deepcopy(self.state.workspace)
        before = self.files()
        code, page = self.page()
        self.assertEqual(200, code)
        self.assertIn('/material-custody?' + urlencode({'event_id': self.event_id}), page)
        self.assertNotIn(other.name, page)
        self.assertEqual(200, render_page(self.state, '/material-custody',
            member_token=self.tokens[self.LOGISTICS], query={'event_id': [self.event_id]})[0])
        self.assertEqual(other.event_id, self.state.active_event_id)
        self.assertEqual(context.events, self.state.workspace.events)
        self.assertEqual(context.object_revisions, self.state.workspace.object_revisions)
        self.assertEqual(before, self.files())
        self.assertEqual('closed', self.state.registry.events[self.event_id].status)

    def test_corrupt_authority_source_stops_navigation_with_no_business_writes(self):
        self.close()
        before = self.files()
        with patch.object(self.state, 'event_temporary_group_leaders',
                          side_effect=ValueError('模拟授权来源冲突')):
            for route in ('/materials/closeout', '/temporary/leader'):
                code, page = self.page(route)
                self.assertEqual(409, code)
                self.assertNotIn('/material-custody?', page)
                self.assertNotIn('/temporary/leader/readiness', page)
        self.assertEqual(before, self.files())

    def test_event_name_is_escaped_and_live_leader_forms_remain_live_only(self):
        from dataclasses import replace
        from panda_brain.panel import render_temporary_leader_page

        code, page = self.page('/temporary/leader')
        self.assertEqual(200, code)
        self.assertIn('/temporary/leader/readiness', page)
        self.assertEqual(404, self.page()[0])
        # The live page's mixed-duty navigation keeps closed work in its own entry.
        page = render_temporary_leader_page(self.state,
            self.state.member_session(self.tokens[self.LOGISTICS]),
            [(self.event_id, 'log-material')], ['an-archived-event'])
        self.assertIn('/materials/closeout', page)
        self.assertIn('/temporary/leader/readiness', page)
        self.close()
        event = self.state.registry.events[self.event_id]
        self.state.registry.events[self.event_id] = replace(event, name='<script>模拟</script>')
        before = self.files()
        code, page = self.page()
        self.assertEqual(200, code)
        self.assertIn('&lt;script&gt;模拟&lt;/script&gt;', page)
        self.assertNotIn('<script>模拟</script>', page)
        self.assertNotIn('/temporary/leader/readiness', page)
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
