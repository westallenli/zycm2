"""Personnel discovery must preserve event scope, authority and read-only GETs."""
import hashlib
import unittest
import threading
from http.client import HTTPConnection
from dataclasses import replace
from pathlib import Path

from panda_brain.panel import home_gate, render_page, make_server
from tests import test_upgrade_checkin_flow as flow


class PersonnelHomeTests(unittest.TestCase):
    DUTY = flow.CheckinFlowTests.DUTY
    DEPUTY = flow.CheckinFlowTests.DEPUTY
    CAPTAIN = flow.CheckinFlowTests.CAPTAIN
    MEMBER_A = flow.CheckinFlowTests.MEMBER_A
    MEMBER_B = flow.CheckinFlowTests.MEMBER_B
    TEMP = flow.CheckinFlowTests.TEMP
    _demo_event = flow.CheckinFlowTests._demo_event

    def setUp(self):
        flow.CheckinFlowTests.setUp(self)
        _, self.event_id = self._demo_event()
        self.qr_id = self.state.handle_event_action(
            self.event_id, 'start', duty_token=self.tokens[self.DUTY])['qr_id']
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='模拟线下任命依据',
            request_id='home-appointment', occurred_at='2026-10-12T09:00:00+08:00')
        self.commander = self.state.login('p03-flow-secret')
        self.state.handle_event_action(self.event_id, 'enter')

    def register(self):
        return self.state.handle_temporary_scan(
            self.event_id, member_token=self.tokens[self.TEMP], qr_id=self.qr_id)

    def home(self, phone=None):
        return render_page(self.state, '/', member_token=self.tokens[phone] if phone else None,
                           session_token=None if phone else self.commander)

    def bytes(self):
        root = Path(self.state.registry_path).parent
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def test_named_deputy_without_personal_checkin_can_find_pending_people(self):
        self.register()
        self.assertEqual('', self.state.member_session(self.tokens[self.DEPUTY]).bound_event_id)
        status, html = self.home(self.DEPUTY)
        self.assertEqual(200, status)
        self.assertIn('待安排 <b>1</b> 人', html)
        self.assertIn('属地协助', html)
        self.assertIn('/personnel/temporary?event_id=' + self.event_id, html)
        self.assertIn('调组安排', html)

    def test_commander_summary_is_readonly_and_masks_contact(self):
        self.register()
        status, html = self.home()
        self.assertEqual(200, status)
        self.assertIn('待安排 <b>1</b> 人', html)
        self.assertIn('此处只读', html)
        self.assertNotIn('href="/temporary/assign', html)
        self.assertNotIn(self.TEMP, html)
        status,paused = render_page(self.state, '/personnel/temporary', session_token=self.commander)
        self.assertEqual(200,status); self.assertIn('独立工作台已暂停',paused)
        with self.assertRaises(PermissionError):
            self.state.handle_temporary_assign(self.event_id, deputy_token=self.commander,
                phones=[self.TEMP], pos_code='tech-info', request_id='admin-cannot-arrange')

    def test_home_get_does_not_write_business_or_arrival(self):
        self.register()
        before, version = self.bytes(), self.state.workspace.version
        for phone in (None, self.DEPUTY):
            self.assertEqual(200, self.home(phone)[0])
        self.assertEqual(before, self.bytes())
        self.assertEqual(version, self.state.workspace.version)
        self.assertEqual('', self.state.temporary_people(self.event_id)[self.TEMP].arrived_at)
        self.assertEqual('', self.state.member_session(self.tokens[self.DEPUTY]).bound_event_id)

    def test_arrangement_and_transfer_update_summary_without_adding_arrival(self):
        self.register()
        self.state.handle_temporary_assign(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phones=[self.TEMP], pos_code='tech-info', request_id='home-assign')
        self.state.handle_person_group_transfer(self.event_id, deputy_token=self.tokens[self.DEPUTY],
            phone=self.TEMP, to_pos_code='log-comm', request_id='home-transfer')
        status, html = self.home(self.DEPUTY)
        self.assertEqual(200, status)
        self.assertIn('待安排 <b>0</b> 人 · 已编组 <b>1</b> 人', html)
        status, page = render_page(self.state, '/temporary/assign',
            member_token=self.tokens[self.DEPUTY], query={'event_id': [self.event_id]})
        self.assertEqual(200, status)
        self.assertIn('通讯保障组', page)
        self.assertIn('按人调组', page)
        self.assertEqual('', self.state.temporary_people(self.event_id)[self.TEMP].arrived_at)

    def test_no_appointment_or_foreign_event_does_not_gain_access(self):
        self.register()
        self.assertEqual('login', home_gate(self.state))
        self.assertEqual('login', home_gate(self.state, member_token=self.tokens[self.MEMBER_A]))
        self.assertEqual(200, render_page(self.state, '/personnel/temporary',
            member_token=self.tokens[self.CAPTAIN])[0])
        foreign = self.state.registry.create_event(
            name='另一模拟事件', report_no='HOME-FOREIGN',
            discovered_at='2026-10-12T10:00:00+08:00', source='模拟测试', location='模拟地点').event_id
        self.state.handle_event_action(foreign, 'start', duty_token=self.tokens[self.DUTY])
        self.assertEqual(404, render_page(self.state, '/temporary/assign',
            member_token=self.tokens[self.DEPUTY], query={'event_id': [foreign]})[0])
        status, html = self.home(self.DEPUTY)
        self.assertEqual(200, status)
        self.assertIn('/personnel/temporary?event_id=' + self.event_id, html)
        self.assertNotIn('/personnel/temporary?event_id=' + foreign, html)

    def test_closed_event_does_not_grant_home_or_arrangement_access(self):
        self.state.registry.events[self.event_id] = replace(
            self.state.registry.events[self.event_id], status='closed')
        self.assertEqual('login', home_gate(self.state, member_token=self.tokens[self.DEPUTY]))
        status,paused = render_page(self.state, '/personnel/temporary', member_token=self.tokens[self.DEPUTY])
        self.assertEqual(200,status); self.assertIn('独立工作台已暂停',paused)

    def test_pending_names_are_escaped_and_empty_is_truthful(self):
        self.assertIn('暂无待安排临时人员', self.home()[1])
        self.state.registry.library[self.TEMP] = replace(
            self.state.registry.library[self.TEMP], name='<script>attack</script>', unit='<b>单位</b>')
        self.register()
        html = self.home(self.DEPUTY)[1]
        self.assertIn('&lt;script&gt;attack&lt;/script&gt;', html)
        self.assertNotIn('<script>attack</script>', html)
        self.assertIn('&lt;b&gt;单位&lt;/b&gt;', html)

    def test_home_logout_form_really_invalidates_admin_and_member_sessions(self):
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for phone, cookie_name, action in ((None, 'panel_session', '/logout'),
                                              (self.DEPUTY, 'member_session', '/logout/member')):
                with self.subTest(role='admin' if phone is None else 'deputy'):
                    token = self.commander if phone is None else self.tokens[phone]
                    cookie = cookie_name + '=' + token
                    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    try:
                        conn.request('GET', '/', headers={'Cookie': cookie})
                        response = conn.getresponse()
                        html = response.read().decode('utf-8')
                        self.assertEqual(200, response.status)
                        self.assertIn('action="' + action + '"', html)
                        self.assertIn('登出</button>', html)
                        conn.request('POST', action, body='', headers={'Cookie': cookie})
                        response = conn.getresponse(); response.read()
                        self.assertEqual(303, response.status)
                        self.assertIn(cookie_name + '=', response.getheader('Set-Cookie'))
                        self.assertIn('Max-Age=0', response.getheader('Set-Cookie'))
                        conn.request('GET', '/', headers={'Cookie': cookie})
                        response = conn.getresponse(); response.read()
                        self.assertEqual(303, response.status)
                        self.assertEqual('/login', response.getheader('Location'))
                    finally:
                        conn.close()
        finally:
            server.shutdown(); thread.join(timeout=5); server.server_close()
