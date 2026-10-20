"""Deputy unified modules keep event scope, arrival facts and existing authority."""
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from urllib.parse import urlencode

from panda_brain.panel import render_page, make_server
from panda_brain.persistence import load_workspace
from panda_brain import review_meeting as meeting
from tests import test_upgrade_personnel_home as home


class DeputyModulesTests(unittest.TestCase):
    DUTY, DEPUTY, CAPTAIN = home.PersonnelHomeTests.DUTY, home.PersonnelHomeTests.DEPUTY, home.PersonnelHomeTests.CAPTAIN
    MEMBER_A, MEMBER_B, TEMP = home.PersonnelHomeTests.MEMBER_A, home.PersonnelHomeTests.MEMBER_B, home.PersonnelHomeTests.TEMP
    _demo_event = home.PersonnelHomeTests._demo_event
    bytes = home.PersonnelHomeTests.bytes
    setUp = home.PersonnelHomeTests.setUp
    NAV = (('/events', '火情事件'), ('/', '人员架构'), ('/sand', '沙盘指挥'),
           ('/materials', '人员与物资'), ('/review', '复盘'))

    def foreign_event(self, appointed=False):
        event_id = self.state.registry.create_event(name='另一模拟事件', report_no='MODULES-FOREIGN',
            discovered_at='2026-10-12T10:00:00+08:00', source='模拟测试', location='模拟地点').event_id
        self.state.handle_event_action(event_id, 'start', duty_token=self.tokens[self.DUTY])
        if appointed:
            self.state.handle_deputy_appointment(event_id, duty_token=self.tokens[self.DUTY],
                subject_id=self.DEPUTY, evidence='第二事件模拟线下任命', request_id='modules-second')
        return event_id

    def server(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=5))
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, *, phone=None, form=None, cookie=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=8)
        headers = {'Cookie': cookie or 'member_session=' + self.tokens[phone or self.DEPUTY]}
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            conn.request('GET' if form is None else 'POST', path,
                         body=None if form is None else urlencode(form), headers=headers)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read().decode('utf-8')
            # Response delivery may precede the request context's finally block.
            # Direct assertions observe state after that serialized request exits.
            with self.state._event_authority_lock:
                return result
        finally:
            conn.close()

    def test_five_pages_follow_deputy_event_while_admin_has_foreign_context(self):
        foreign = self.foreign_event()
        self.state.handle_event_action(foreign, 'enter')
        original = self.state.workspace
        before = self.bytes()
        for route, _ in self.NAV:
            with self.subTest(route=route):
                status, html = render_page(self.state, route, member_token=self.tokens[self.DEPUTY])
                self.assertEqual(200, status)
                for target, label in self.NAV:
                    self.assertIn('href="' + target + '"', html)
                    self.assertIn(label, html)
                self.assertNotIn('另一模拟事件', html)
                self.assertIs(original, self.state.workspace)
                self.assertEqual(foreign, self.state.active_event_id)
        self.assertEqual(before, self.bytes())
        member = self.state.member_session(self.tokens[self.DEPUTY])
        self.assertEqual('', member.bound_event_id)
        self.assertEqual('', member.arrived_at)

    def test_fire_list_details_and_actions_do_not_leak_foreign_event(self):
        foreign = self.foreign_event()
        status, html = render_page(self.state, '/events', member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200, status)
        self.assertIn(self.event_id, html)
        self.assertNotIn(foreign, html)
        for action in ('/events/appointment', '/events/action', '/events/alarm/demo', '/account/unlock'):
            self.assertNotIn('action="' + action + '"', html)
        self.assertEqual(200, render_page(self.state, '/events/' + self.event_id + '/details',
                                        member_token=self.tokens[self.DEPUTY])[0])
        for phone in (self.DEPUTY, self.MEMBER_A):
            self.assertEqual(404, render_page(self.state, '/events/' + foreign + '/details',
                                            member_token=self.tokens[phone])[0])
        self.assertEqual(404, render_page(self.state, '/events', member_token=self.tokens[self.MEMBER_A])[0])

    def test_unified_login_enters_events_without_commander_authority(self):
        status, headers, _ = self.request(self.server(), '/login/unified',
            form={'account': self.DEPUTY, 'password': '123456'}, cookie='panel_session=' + self.commander)
        self.assertEqual(303, status)
        self.assertEqual('/events', headers['Location'])
        self.assertFalse(self.state.command_mode(self.commander))
        deputy = next(s for s in self.state.member_sessions.values() if s.selected_event_id == self.event_id)
        self.assertEqual('', deputy.bound_event_id)
        self.assertEqual('', deputy.arrived_at)

    def test_select_is_session_only_and_requires_csrf_and_own_appointment(self):
        second = self.foreign_event(appointed=True)
        foreign = self.foreign_event()
        server = self.server()
        member = self.state.member_session(self.tokens[self.DEPUTY])
        before, original = self.bytes(), self.state.workspace
        for event_id, csrf, expected in ((second, 'wrong', 403), (foreign, member.custody_csrf, 403),
                                          (second, member.custody_csrf, 303)):
            status, _, _ = self.request(server, '/events/select', form={'event_id': event_id, 'csrf': csrf})
            self.assertEqual(expected, status)
        self.assertEqual(second, member.selected_event_id)
        self.assertEqual('', member.bound_event_id)
        self.assertEqual(before, self.bytes())
        self.assertIs(original, self.state.workspace)
        self.assertEqual(self.event_id, self.state.active_event_id)
        status, _, html = self.request(server, '/materials')
        self.assertEqual(200, status)
        self.assertIn('当前事件：另一模拟事件', html)

    def test_existing_review_deputy_write_targets_selected_event_and_restores_admin(self):
        foreign = self.foreign_event()
        self.state.handle_event_action(foreign, 'enter')
        original, original_version = self.state.workspace, self.state.workspace.version
        member_token = self.tokens[self.DEPUTY]
        selected = load_workspace(self.state.registry.events[self.event_id].snapshot_path, general_auth=self.auth)
        status, _, _ = self.request(self.server(), '/review/owner', form={
            'event_id': self.event_id, 'csrf': meeting.csrf(self.state, member_token),
            'expected_version': str(selected.version), 'phone': self.MEMBER_A,
            'evidence': '模拟复盘负责人任命依据', 'request_id': 'modules-review-owner'})
        self.assertEqual(303, status)
        selected = load_workspace(self.state.registry.events[self.event_id].snapshot_path, general_auth=self.auth)
        self.assertTrue(any(r.reference.object_id == 'review_owner' for r in selected.object_revisions.values()))
        self.assertIs(original, self.state.workspace)
        self.assertEqual(foreign, self.state.active_event_id)
        self.assertEqual(original_version, original.version)
        self.assertFalse(original.verify_audit_chain())
        self.assertFalse(selected.verify_audit_chain())

    def test_deputy_navigation_does_not_enable_admin_writes(self):
        server = self.server()
        before = self.bytes()
        for route, form in (('/events/action', {'event_id': self.event_id, 'action': 'close'}),
                            ('/events/appointment', {'event_id': self.event_id, 'subject_id': self.MEMBER_A}),
                            ('/sand/goal', {'statement': 'unauthorized'}),
                            ('/materials/item', {'name': 'unauthorized', 'unit': '个'})):
            status, _, _ = self.request(server, route, form=form)
            self.assertIn(status, (303, 403, 404))
        self.assertEqual(before, self.bytes())
        self.assertEqual('active', self.state.registry.events[self.event_id].status)

    def test_http_feed_and_pages_are_readonly_and_restore_foreign_workspace(self):
        foreign = self.foreign_event()
        self.state.handle_event_action(foreign, 'enter')
        original, before = self.state.workspace, self.bytes()
        server = self.server()
        for route in [r for r, _ in self.NAV] + ['/sand/situation-feed']:
            self.assertEqual(200, self.request(server, route)[0])
            # The client can receive the final byte before the server finishes
            # its finally block; observe state only at the request lock boundary.
            with self.state._event_authority_lock:
                self.assertIs(original, self.state.workspace)
                self.assertEqual(foreign, self.state.active_event_id)
        self.assertEqual(before, self.bytes())

    def test_selected_closed_event_cannot_fall_back_to_admin_context(self):
        member = self.state.member_session(self.tokens[self.DEPUTY])
        member.selected_event_id = self.event_id
        self.state.registry.events[self.event_id] = replace(self.state.registry.events[self.event_id], status='closed')
        for route, _ in self.NAV:
            self.assertEqual(404, render_page(self.state, route, member_token=self.tokens[self.DEPUTY])[0])


if __name__ == '__main__':
    unittest.main()
