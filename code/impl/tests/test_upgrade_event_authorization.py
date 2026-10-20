import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from panda_brain.auth import GeneralEntryAuth
from panda_brain.authorization import Action, Role, RoleGrant
from panda_brain.event_authorization import EventAuthorizationService, event_grants
from panda_brain.models import PersonRecord
from panda_brain.panel import PanelState
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.workspace import ScenarioWorkspace


class EventAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.duty, self.deputy, self.owner = ('13912340001', '13912340002', '13912340003')
        self.auth = GeneralEntryAuth(secret='isolated-test-secret')
        self.ws = ScenarioWorkspace('test', general_auth=self.auth, event_id='fire-a')
        self.state = self.panel(self.ws)
        self.state.registry.import_person_library([
            PersonRecord(name=name, phone=phone, unit='模拟单位')
            for name, phone in [('值班', self.duty), ('副总', self.deputy), ('复盘', self.owner)]])
        self.tokens = {}
        for phone in (self.duty, self.deputy, self.owner):
            token, error = self.state.member_login(phone, '123456')
            self.assertEqual('', error)
            self.tokens[phone] = token
        self.service = self.service_for(self.ws, self.state)

    def panel(self, ws):
        return PanelState(ws, auth=self.auth, registry_path=self.root/'registry.json',
                          materials_path=self.root/'materials.json', reviews_path=self.root/'reviews.json')

    def service_for(self, ws, state):
        return EventAuthorizationService(ws, resolve_subject=state.authorization_subject,
            global_grants=(RoleGrant(self.duty, Role.DUTY),),
            subject_exists=lambda subject: subject in state.registry.library)

    def appoint(self, role=Role.DEPUTY, **changes):
        args = dict(event_id='fire-a', role=role,
                    subject_id=self.deputy if role == Role.DEPUTY else self.owner,
                    evidence='模拟线下任命记录-001', request_id=role.value,
                    occurred_at='2026-09-24T08:00:00+08:00')
        args.update(changes)
        token = self.tokens[self.duty if role == Role.DEPUTY else self.deputy]
        return self.service.record_initial_appointment(token, **args)

    def test_named_duty_records_deputy_without_unlocking_general(self):
        record = self.appoint()
        self.assertEqual(self.duty, record.actor_id)
        self.assertEqual('2026-09-24T08:00:00+08:00', record.times.occurred_at)
        self.assertTrue(record.times.recorded_at.endswith('+00:00'))
        self.assertEqual((), self.ws.verify_audit_chain())
        self.service.require(self.tokens[self.deputy], Action.PUBLISH_TASK, event_id='fire-a')

    def test_review_assignment_requires_deputy_and_separate_role(self):
        self.appoint()
        with self.assertRaises(PermissionError):
            self.service.require(self.tokens[self.deputy], Action.PUBLISH_REPORT, event_id='fire-a')
        self.appoint(Role.REVIEW_OWNER)
        self.service.require(self.tokens[self.owner], Action.PUBLISH_REPORT, event_id='fire-a')
        with self.assertRaises(PermissionError):
            self.service.require(self.tokens[self.owner], Action.PUBLISH_TASK, event_id='fire-a')

    def test_denied_write_has_no_effect(self):
        before = self.ws.events
        for token, event in ((self.tokens[self.owner], 'fire-a'),
                             (self.tokens[self.duty], 'fire-b'), ('forged', 'fire-a')):
            with self.assertRaises(PermissionError):
                self.service.record_initial_appointment(token, event_id=event, role=Role.DEPUTY,
                    subject_id=self.deputy, evidence='ref', request_id='r')
        self.assertEqual(before, self.ws.events)
        self.assertEqual({}, self.ws.object_revisions)

    def test_restart_restores_grants_but_not_sessions(self):
        self.appoint()
        self.appoint(Role.REVIEW_OWNER)
        db = self.root/'state.db'
        save_workspace(self.ws, db)
        restored = load_workspace(db, general_auth=self.auth)
        panel = self.panel(restored)
        service = self.service_for(restored, panel)
        with self.assertRaises(PermissionError):
            service.require(self.tokens[self.deputy], Action.PUBLISH_TASK, event_id='fire-a')
        token, error = panel.member_login(self.deputy, '123456')
        self.assertEqual('', error)
        service.require(token, Action.PUBLISH_TASK, event_id='fire-a')
        with self.assertRaises(PermissionError):
            service.require(token, Action.PUBLISH_TASK, event_id='fire-b')
        self.assertEqual(event_grants(self.ws), event_grants(restored))

    def test_retry_survives_restart_and_conflict_is_rejected(self):
        record = self.appoint()
        db = self.root/'state.db'
        save_workspace(self.ws, db)
        self.ws = load_workspace(db, general_auth=self.auth)
        self.service = self.service_for(self.ws, self.state)
        before = self.ws.events
        self.assertEqual(record, self.appoint())
        with self.assertRaises(ValueError):
            self.appoint(evidence='different evidence')
        self.assertEqual(before, self.ws.events)

    def test_replacement_and_missing_evidence_not_silently_accepted(self):
        before = self.ws.events
        for changes in ({'evidence': ''}, {'subject_id': 'missing'}, {'request_id': ''},
                        {'occurred_at': '2026-09-24T08:00:00'}):
            with self.assertRaises(ValueError):
                self.appoint(**changes)
        self.assertEqual(before, self.ws.events)
        self.appoint()
        with self.assertRaises(ValueError):
            self.appoint(subject_id=self.owner, request_id='replace')

    def test_old_general_token_never_becomes_named_subject(self):
        token = self.state.login('isolated-test-secret')
        self.assertTrue(token)
        with self.assertRaises(PermissionError):
            self.state.authorization_subject(token)
        with self.assertRaises(ValueError):
            EventAuthorizationService(self.ws, resolve_subject=self.state.authorization_subject,
                global_grants=(RoleGrant(self.deputy, Role.DEPUTY, 'fire-a'),), subject_exists=lambda _: True)

    def test_logout_lock_and_deleted_person_revoke_session_use(self):
        token = self.tokens[self.duty]
        self.state.registry.credentials[self.duty].locked = True
        with self.assertRaises(PermissionError):
            self.appoint()
        self.state.registry.credentials[self.duty].locked = False
        self.state.member_logout(token)
        with self.assertRaises(PermissionError):
            self.appoint()
        del self.state.registry.library[self.deputy]
        with self.assertRaises(PermissionError):
            self.state.authorization_subject(self.tokens[self.deputy])

    def test_generic_record_api_cannot_mint_roles(self):
        record = self.appoint()
        other = ScenarioWorkspace('test', event_id='fire-a')
        with self.assertRaises(PermissionError):
            other.record_object_revision(record, expected_version=0)
        self.assertEqual({}, other.object_revisions)

    def test_modified_entity_and_missing_entity_rejected_on_load(self):
        record = self.appoint()
        content = json.loads(record.content_json)
        content['subject_id'] = self.owner
        self.ws.object_revisions[record.id] = replace(record, content_json=json.dumps(content))
        db = self.root/'tampered.db'
        save_workspace(self.ws, db)
        with self.assertRaises(ValueError):
            load_workspace(db)
        self.ws.object_revisions.clear()
        save_workspace(self.ws, db)
        with self.assertRaises(ValueError):
            load_workspace(db)

    def test_damaged_audit_and_wrong_event_rejected(self):
        self.appoint()
        self.ws.event_id = 'fire-b'
        with self.assertRaises(ValueError):
            event_grants(self.ws)
        self.ws.event_id = 'fire-a'
        self.ws._events[-1].payload['revision']['actor_id'] = self.owner
        with self.assertRaises(ValueError):
            event_grants(self.ws)

    def test_legacy_state_does_not_gain_roles(self):
        legacy = ScenarioWorkspace('legacy')
        db = self.root/'legacy.db'
        save_workspace(legacy, db)
        self.assertEqual((), event_grants(load_workspace(db)))


if __name__ == '__main__':
    unittest.main()
