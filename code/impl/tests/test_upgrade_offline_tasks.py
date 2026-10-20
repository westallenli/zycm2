"""P06 offline instructions: reporting cannot fabricate release or review."""
import http.client
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.authorization import Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService
from panda_brain.models import PersonRecord
from panda_brain.offline_tasks import (amend_offline, record_control, record_feedback,
                                       record_offline, overdue_reminders,
                                       task_state, verify_close)
from panda_brain.panel import PanelState, make_server
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.task_recipients import recipient_route
from panda_brain.workspace import ScenarioWorkspace


class OfflineTaskTests(unittest.TestCase):
    def setUp(self):
        self.ws = ScenarioWorkspace('p06-offline', event_id='event-a')
        self.people = {'duty', 'deputy', 'leader', 'other', 'verifier'}
        self.auth = EventAuthorizationService(
            self.ws, resolve_subject=lambda token: token,
            global_grants=(RoleGrant('duty', Role.DUTY),),
            subject_exists=lambda person: person in self.people)
        self.auth.record_initial_appointment(
            'duty', event_id='event-a', role=Role.DEPUTY,
            subject_id='deputy', evidence='模拟线下任命', request_id='appoint-1')
        self.seq = 0

    def create(self, **changes):
        self.seq += 1
        args = dict(event_id='event-a', content='线下已下达：观察北段',
                    group_id='squad-a', issued_by='现场指挥员',
                    recipient_id='leader', verifier_id='verifier',
                    occurred_at='2026-09-29T09:30:00+08:00',
                    evidence='对讲记录编号-001', known_progress='已口头回复',
                    request_id=f'offline-{self.seq}')
        args.update(changes)
        return record_offline(self.ws, self.auth, 'deputy', **args)

    def feedback(self, task_id, action, *, version=1, actor='leader', **changes):
        self.seq += 1
        args = dict(event_id='event-a', task_id=task_id, expected_version=version,
                    action=action, basis='本人阶段回报', request_id=f'feedback-{self.seq}',
                    is_current_leader=lambda group, phone: group == 'squad-a' and phone == 'leader')
        args.update(changes)
        return record_feedback(self.ws, lambda token: token, actor, **args)

    def close(self, task_id, *, version=1, actor='verifier', overall='整体目标已现场核查', **changes):
        self.seq += 1
        args = dict(event_id='event-a', task_id=task_id, expected_version=version,
                    evidence='独立现场核验-001', overall_goal_evidence=overall,
                    request_id=f'close-{self.seq}', subject_exists=lambda person: person in self.people)
        args.update(changes)
        return verify_close(self.ws, lambda token: token, actor, **args)

    def test_offline_record_keeps_two_times_and_never_publishes(self):
        first = self.create()
        task = task_state(self.ws, 'event-a')[first['task_id']]
        self.assertEqual('reported_offline', task.state)
        self.assertEqual('2026-09-29T09:30:00+08:00', task.occurred_at)
        self.assertNotEqual(task.occurred_at, task.recorded_at)
        self.assertEqual('已口头回复', task.known_progress)
        self.assertEqual((), task.current_feedback)
        self.assertEqual({}, self.ws.orders)
        self.assertEqual({}, self.ws.approvals)
        self.assertEqual((), self.ws.verify_audit_chain())
        count = len(self.ws.events)
        same = record_offline(self.ws, self.auth, 'deputy',
                              event_id='event-a', content='线下已下达：观察北段',
                              group_id='squad-a', issued_by='现场指挥员',
                              recipient_id='leader', verifier_id='verifier',
                              occurred_at='2026-09-29T09:30:00+08:00',
                              evidence='对讲记录编号-001', known_progress='已口头回复',
                              request_id='offline-1')
        self.assertEqual(first, same)
        self.assertEqual(count, len(self.ws.events))
        with self.assertRaises(PermissionError):
            record_offline(self.ws, self.auth, 'other', event_id='event-a',
                           content='内容', group_id='', issued_by='甲',
                           recipient_id='乙', verifier_id='',
                           occurred_at='2026-09-29T09:00:00+08:00',
                           evidence='依据', request_id='unauthorized')
        with self.assertRaises(PermissionError):
            self.create(event_id='another-event')
        with self.assertRaises(ValueError):
            self.create(occurred_at='2099-01-01T00:00:00+08:00')
        self.assertEqual(count, len(self.ws.events))

    def test_missing_dispatch_details_do_not_block_honest_offline_record(self):
        task_id = self.create(group_id='', verifier_id='')['task_id']
        task = task_state(self.ws, 'event-a')[task_id]
        self.assertEqual('reported_offline', task.state)
        self.assertEqual('', task.group_id)
        with self.assertRaises(PermissionError):
            self.feedback(task_id, 'received')
        with self.assertRaises(PermissionError):
            self.close(task_id)
        self.assertEqual('reported_offline', task_state(self.ws, 'event-a')[task_id].state)

    def test_feedback_is_leader_only_and_close_requires_independent_verifier(self):
        task_id = self.create()['task_id']
        with self.assertRaises(ValueError):
            self.feedback(task_id, 'started')
        with self.assertRaises(PermissionError):
            self.feedback(task_id, 'received', actor='other')
        with self.assertRaises(PermissionError):
            self.feedback(task_id, 'received', is_current_leader=lambda *_: False)
        self.feedback(task_id, 'received')
        with self.assertRaises(ValueError):
            self.feedback(task_id, 'received')
        self.feedback(task_id, 'started')
        self.feedback(task_id, 'difficulty')
        self.feedback(task_id, 'progress')
        with self.assertRaises(ValueError):
            self.feedback(task_id, 'completed')
        self.feedback(task_id, 'resolved')
        self.feedback(task_id, 'completed')
        self.assertEqual('pending_verification', task_state(self.ws, 'event-a')[task_id].state)
        with self.assertRaises(PermissionError):
            self.close(task_id, actor='leader')
        with self.assertRaises(ValueError):
            self.close(task_id, overall='')
        self.close(task_id)
        self.assertEqual('closed', task_state(self.ws, 'event-a')[task_id].state)
        with self.assertRaises(ValueError):
            self.close(task_id)

    def test_amendment_requires_new_receipt_and_child_blocks_main_close(self):
        main = self.create()['task_id']
        child = self.create(parent_id=main, content='线下协作：检查东侧')['task_id']
        self.feedback(main, 'received')
        self.feedback(main, 'started')
        self.feedback(main, 'completed')
        with self.assertRaises(ValueError):
            self.close(main)
        amend_offline(self.ws, self.auth, 'deputy', event_id='event-a',
                      task_id=main, expected_version=1,
                      content='线下变更：观察北段和西侧', group_id='squad-a',
                      recipient_id='leader', verifier_id='verifier',
                      occurred_at='2026-09-29T09:45:00+08:00',
                      reason='现场调整记录-002', request_id='amend-1')
        current = task_state(self.ws, 'event-a')[main]
        self.assertEqual(2, current.version)
        self.assertEqual('reported_offline', current.state)
        self.assertEqual((), current.current_feedback)
        self.assertEqual('2026-09-29T09:30:00+08:00', current.history[0]['occurred_at'])
        with self.assertRaises(ValueError):
            self.feedback(main, 'completed', version=1)
        self.feedback(main, 'received', version=2)
        self.feedback(main, 'started', version=2)
        self.feedback(main, 'completed', version=2)
        with self.assertRaises(ValueError):
            self.close(main, version=2)
        self.feedback(child, 'received')
        self.feedback(child, 'started')
        self.feedback(child, 'completed')
        self.close(child, overall='')
        self.close(main, version=2)
        self.assertEqual('closed', task_state(self.ws, 'event-a')[main].state)
        with self.assertRaises(ValueError):
            amend_offline(self.ws, self.auth, 'deputy', event_id='event-a',
                          task_id=main, expected_version=2, content='变更',
                          group_id='squad-a', recipient_id='leader',
                          verifier_id='verifier', occurred_at='2026-09-29T10:00:00+08:00',
                          reason='依据', request_id='amend-closed')

    def test_snapshot_round_trip_and_audit_tamper_refusal(self):
        task_id = self.create()['task_id']
        self.feedback(task_id, 'received')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'active.db'
            save_workspace(self.ws, path)
            restored = load_workspace(path)
            self.assertEqual(task_state(self.ws, 'event-a'), task_state(restored, 'event-a'))
        self.ws._events[-1] = replace(self.ws._events[-1], payload={'altered': True})
        with self.assertRaises(ValueError):
            task_state(self.ws, 'event-a')

    def test_pause_resume_withdraw_replace_are_reported_without_resend(self):
        task_id = self.create()['task_id']
        later = self.create(content='另行线下下达的替代任务')['task_id']
        def control(action, request_id, *, successor_id=''):
            return record_control(
                self.ws, self.auth, 'deputy', event_id='event-a',
                task_id=task_id, expected_version=1, action=action,
                occurred_at='2026-09-29T10:00:00+08:00',
                reason='现场控制事实记录', successor_id=successor_id,
                request_id=request_id)

        control('pause', 'pause-1')
        self.assertEqual('paused', task_state(self.ws, 'event-a')[task_id].state)
        with self.assertRaises(ValueError):
            self.feedback(task_id, 'received')
        with self.assertRaises(ValueError):
            amend_offline(self.ws, self.auth, 'deputy', event_id='event-a',
                          task_id=task_id, expected_version=1,
                          content='暂停时修改', group_id='squad-a',
                          recipient_id='leader', verifier_id='verifier',
                          occurred_at='2026-09-29T10:00:00+08:00',
                          reason='依据', request_id='amend-paused')
        control('resume', 'resume-1')
        self.feedback(task_id, 'received')
        followup = control('followup', 'followup-1')
        count = len(self.ws.events)
        self.assertEqual(followup, control('followup', 'followup-1'))
        self.assertEqual(count, len(self.ws.events))
        control('escalate', 'escalate-1')
        self.assertEqual('received', task_state(self.ws, 'event-a')[task_id].state)
        count = len(self.ws.events)
        with self.assertRaises(ValueError):
            control('replace', 'bad-replacement', successor_id='missing')
        self.assertEqual(count, len(self.ws.events))
        control('replace', 'replace-1', successor_id=later)
        self.assertEqual('replaced', task_state(self.ws, 'event-a')[task_id].state)
        self.assertEqual('reported_offline', task_state(self.ws, 'event-a')[later].state)
        with self.assertRaises(ValueError):
            self.feedback(task_id, 'started')
        third = self.create()['task_id']
        record_control(self.ws, self.auth, 'deputy', event_id='event-a',
                       task_id=third, expected_version=1, action='withdraw',
                       occurred_at='2026-09-29T10:00:00+08:00',
                       reason='线下撤回事实', request_id='withdraw-1')
        self.assertEqual('withdrawn', task_state(self.ws, 'event-a')[third].state)
        self.assertEqual({}, self.ws.orders)

    def test_only_explicit_deadline_produces_read_only_reminder(self):
        unknown = self.create()['task_id']
        due = self.create(due_at='2026-09-29T10:00:00+08:00')['task_id']
        later = self.create(due_at='2026-09-29T14:00:00+08:00')['task_id']
        before = len(self.ws.events)
        self.assertEqual((due,), overdue_reminders(
            self.ws, 'event-a', '2026-09-29T11:00:00+08:00'))
        self.assertEqual(before, len(self.ws.events))
        self.assertEqual('reported_offline', task_state(self.ws, 'event-a')[due].state)
        self.assertNotIn(unknown, overdue_reminders(
            self.ws, 'event-a', '2026-09-29T20:00:00+08:00'))
        self.assertIn(later, overdue_reminders(
            self.ws, 'event-a', '2026-09-29T20:00:00+08:00'))
        with self.assertRaises(ValueError):
            self.create(due_at='2026-09-29T08:00:00+08:00')


class OfflineTaskHttpTests(unittest.TestCase):
    DUTY = '13977770001'
    DEPUTY = '13977770002'
    LEADER = '13977770003'
    VERIFIER = '13977770004'
    OTHER = '13977770005'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        setting = patch.object(wb, 'SNAPSHOT_DIR', root / 'snapshots')
        setting.start()
        self.addCleanup(setting.stop)
        auth = GeneralEntryAuth(secret='p06-offline-http')
        self.state = PanelState(
            ScenarioWorkspace('p06-offline-http', general_auth=auth), auth=auth,
            db_path=root / 'active.db', registry_path=root / 'registry.json',
            materials_path=root / 'materials.json', reviews_path=root / 'reviews.json',
            alarm_inbox_path=root / 'alarms.json', duty_subjects=(self.DUTY,))
        people = [
            PersonRecord(name='值班员', phone=self.DUTY, unit='值班室'),
            PersonRecord(name='副总指挥', phone=self.DEPUTY, unit='前指'),
            PersonRecord(name='队长', phone=self.LEADER, unit='一中队', is_leader=True),
            PersonRecord(name='核验人', phone=self.VERIFIER, unit='前指'),
            PersonRecord(name='其他成员', phone=self.OTHER, unit='其他'),
        ]
        self.state.registry.import_person_library(people)
        self.tokens = {}
        for person in people:
            token, error = self.state.member_login(person.phone, '123456')
            self.assertEqual('', error)
            self.tokens[person.phone] = token
        event = self.state.registry.create_event(
            name='模拟线下任务事件', report_no='P06-offline',
            discovered_at='2026-09-29T08:00:00+08:00',
            source='人工模拟登记', location='模拟北段')
        self.event_id = event.event_id
        squad = self.state.ledger.add_squad(
            name='一中队', member_phones=(self.LEADER,),
            position_slots=('pjjz-1',), leader_phone=self.LEADER,
            library=self.state.registry.library)
        self.squad_id = squad.squad_id
        self.state.handle_event_action(
            self.event_id, 'start', duty_token=self.tokens[self.DUTY])
        self.state.handle_deputy_appointment(
            self.event_id, duty_token=self.tokens[self.DUTY],
            subject_id=self.DEPUTY, evidence='模拟任命依据', request_id='appoint-http')
        self.state.handle_select_squads(
            self.event_id, deputy_token=self.tokens[self.DEPUTY],
            squad_ids=[self.squad_id], channel='phone', request_id='select-http')
        server = make_server(self.state, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def request(self, method, phone, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        body = urlencode(data) if data is not None else None
        headers = {'Cookie': f'member_session={self.tokens[phone]}'} if phone else {}
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        conn.request(method, '/tasks/offline', body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, response.read().decode('utf-8')
        conn.close()
        return result

    def test_named_http_record_receipt_amend_and_verify(self):
        form = dict(action='record', event_id=self.event_id, content='已口头下达观察北段',
                    group_id=self.squad_id, issued_by='现场指挥员',
                    recipient_id=self.LEADER, verifier_id=self.VERIFIER,
                    occurred_at='2026-09-29T09:30:00+08:00',
                    evidence='模拟对讲记录-01', known_progress='已口头确认',
                    parent_id='', request_id='record-http')
        self.assertEqual(404, self.request('GET', None)[0])
        self.assertEqual(403, self.request('POST', self.OTHER, form)[0])
        self.assertEqual(303, self.request('POST', self.DEPUTY, form)[0])
        space = load_workspace(Path(self.state.registry.events[self.event_id].snapshot_path))
        task_id = next(iter(task_state(space, self.event_id)))
        status, page = self.request('GET', self.LEADER)
        self.assertEqual(200, status)
        self.assertIn('已口头下达观察北段', page)
        self.assertIn('139****0004', page)
        self.assertNotIn(self.VERIFIER, page)
        self.assertNotIn('补录线下事实</button>', page)
        feedback = dict(action='feedback', event_id=self.event_id,
                        task_id=task_id, expected_version='1',
                        feedback_action='received', basis='队长本人已收',
                        request_id='receive-http')
        self.assertEqual(403, self.request('POST', self.OTHER, feedback)[0])
        self.assertEqual(303, self.request('POST', self.LEADER, feedback)[0])
        self.assertEqual(303, self.request('POST', self.LEADER, feedback)[0])
        change = dict(action='amend', event_id=self.event_id, task_id=task_id,
                      expected_version='1', content='调整为观察北段和西侧',
                      group_id=self.squad_id, recipient_id=self.LEADER,
                      verifier_id=self.VERIFIER,
                      occurred_at='2026-09-29T09:45:00+08:00',
                      reason='模拟变更依据', request_id='amend-http')
        self.assertEqual(303, self.request('POST', self.DEPUTY, change)[0])
        self.assertEqual(409, self.request('POST', self.LEADER,
                                           feedback | {'request_id': 'stale-http'})[0])
        for version_action in ('received', 'started', 'completed'):
            payload = feedback | {'expected_version': '2',
                                  'feedback_action': version_action,
                                  'request_id': f'new-{version_action}'}
            self.assertEqual(303, self.request('POST', self.LEADER, payload)[0])
        close = dict(action='close', event_id=self.event_id, task_id=task_id,
                     expected_version='2', evidence='现场独立核查记录',
                     overall_goal_evidence='整体目标已核对', request_id='close-http')
        self.assertEqual(403, self.request('POST', self.LEADER, close)[0])
        self.assertEqual(303, self.request('POST', self.VERIFIER, close)[0])
        stored = load_workspace(Path(self.state.registry.events[self.event_id].snapshot_path))
        self.assertEqual('closed', task_state(stored, self.event_id)[task_id].state)
        self.assertEqual({}, stored.orders)
        self.assertEqual({}, stored.approvals)

    def test_route_requires_event_selection_and_current_captain(self):
        space = load_workspace(Path(self.state.registry.events[self.event_id].snapshot_path))
        route = recipient_route(
            space, self.event_id, self.squad_id,
            registry=self.state.registry, ledger=self.state.ledger)
        self.assertEqual(self.LEADER, route.leader_id)
        self.assertEqual('fixed_squad', route.kind)
        unknown = recipient_route(
            space, self.event_id, 'not-selected',
            registry=self.state.registry, ledger=self.state.ledger)
        self.assertEqual('', unknown.leader_id)
        self.assertTrue(unknown.issue)
        squad = self.state.ledger.squads[self.squad_id]
        self.state.ledger.squads[self.squad_id] = replace(squad, leader_phone=self.OTHER)
        changed = recipient_route(
            space, self.event_id, self.squad_id,
            registry=self.state.registry, ledger=self.state.ledger)
        self.assertEqual('', changed.leader_id)
        self.assertIn('不一致', changed.issue)


if __name__ == '__main__':
    unittest.main()
