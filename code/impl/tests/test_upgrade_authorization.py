import unittest

from panda_brain.authorization import Action as A, AuthorizationPolicy, Role as R, RoleGrant


class AuthorizationTests(unittest.TestCase):
    def test_duty_can_start_before_deputy_exists(self):
        policy = AuthorizationPolicy((RoleGrant('duty', R.DUTY),))
        policy.require('duty', A.REGISTER_EVENT)
        policy.require('duty', A.START_EVENT, event_id='fire-1')
        policy.require('duty', A.RECORD_APPOINTMENT, event_id='fire-1')
        with self.assertRaises(PermissionError):
            policy.require('duty', A.PUBLISH_TASK, event_id='fire-1')

    def test_deputy_cannot_cross_events(self):
        policy = AuthorizationPolicy((RoleGrant('deputy', R.DEPUTY, 'fire-1'),))
        for action in (A.PUBLISH_TASK, A.PUBLISH_MAP, A.PUBLISH_DEPLOYMENT,
                       A.ASSIGN_PERSONNEL, A.ASSIGN_REVIEW_OWNER):
            policy.require('deputy', action, event_id='fire-1')
            with self.assertRaises(PermissionError):
                policy.require('deputy', action, event_id='fire-2')

    def test_review_requires_separate_appointment_even_for_deputy(self):
        policy = AuthorizationPolicy((RoleGrant('d', R.DEPUTY, 'e'),
                                      RoleGrant('owner', R.REVIEW_OWNER, 'e')))
        for action in (A.CONFIRM_CAUSE, A.PUBLISH_RECTIFICATION, A.CONFIRM_EXPERIENCE,
                       A.APPROVE_REPORT, A.PUBLISH_REPORT):
            policy.require('owner', action, event_id='e')
            for subject, event in (('d', 'e'), ('owner', 'other')):
                with self.assertRaises(PermissionError):
                    policy.require(subject, action, event_id=event)

    def test_same_person_may_hold_deputy_and_review_role(self):
        policy = AuthorizationPolicy((RoleGrant('d', R.DEPUTY, 'e'),
                                      RoleGrant('d', R.REVIEW_OWNER, 'e')))
        policy.require('d', A.PUBLISH_TASK, event_id='e')
        policy.require('d', A.PUBLISH_REPORT, event_id='e')

    def test_maintenance_is_global_and_separated(self):
        policy = AuthorizationPolicy((RoleGrant('p', R.PERSONNEL_ADMIN),
                                      RoleGrant('m', R.LOGISTICS_ADMIN)))
        policy.require('p', A.MAINTAIN_PERSONNEL)
        policy.require('m', A.MAINTAIN_MATERIALS)
        for subject, action in (('p', A.MAINTAIN_MATERIALS), ('m', A.MAINTAIN_PERSONNEL)):
            with self.assertRaises(PermissionError):
                policy.require(subject, action)
        for subject in ('p', 'm'):
            with self.assertRaises(PermissionError):
                policy.require(subject, A.PUBLISH_DEPLOYMENT, event_id='e')

    def test_leader_must_match_event_and_team(self):
        policy = AuthorizationPolicy((RoleGrant('l', R.LEADER, 'e', 't'),
                                      RoleGrant('member', R.MEMBER, 'e')))
        for action in (A.RECEIVE_TASK, A.REPORT_TEAM):
            policy.require('l', action, event_id='e', team_id='t')
            for subject, event, team in (('l', 'e', 'other'), ('l', 'other', 't'),
                                         ('member', 'e', 't')):
                with self.assertRaises(PermissionError):
                    policy.require(subject, action, event_id=event, team_id=team)

    def test_chief_and_professional_are_not_publishers(self):
        policy = AuthorizationPolicy((RoleGrant('c', R.CHIEF, 'e'),
                                      RoleGrant('p', R.PROFESSIONAL, 'e')))
        policy.require('p', A.SUBMIT_PROFESSIONAL_OPINION, event_id='e')
        for subject in ('c', 'p'):
            with self.assertRaises(PermissionError):
                policy.require(subject, A.PUBLISH_TASK, event_id='e')

    def test_external_news_has_no_implicit_grant(self):
        grants = tuple(RoleGrant('all', r, '' if r in (R.DUTY, R.PERSONNEL_ADMIN,
                           R.LOGISTICS_ADMIN) else 'e', 't' if r == R.LEADER else '') for r in R)
        with self.assertRaises(PermissionError):
            AuthorizationPolicy(grants).require('all', A.PUBLISH_EXTERNAL_NEWS, event_id='e')

    def test_missing_unknown_and_malformed_inputs_fail_closed(self):
        policy = AuthorizationPolicy((RoleGrant('d', R.DEPUTY, 'e'),))
        for subject, action, event in (('', A.PUBLISH_TASK, 'e'), ('unknown', A.PUBLISH_TASK, 'e'),
                                       ('d', 'publish_task', 'e'), ('d', A.PUBLISH_TASK, ''),
                                       ('d', A.PUBLISH_TASK, ' e')):
            with self.assertRaises(PermissionError):
                policy.require(subject, action, event_id=event)
        with self.assertRaises(PermissionError):
            AuthorizationPolicy().require('local-general-entry', A.PUBLISH_TASK, event_id='e')

    def test_invalid_grant_scopes_rejected(self):
        for args in (('', R.DEPUTY, 'e'), ('d', 'designated_deputy', 'e'),
                     ('d', R.DEPUTY), ('p', R.PERSONNEL_ADMIN, 'e'),
                     ('l', R.LEADER, 'e'), ('d', R.DEPUTY, 'e', 't')):
            with self.assertRaises(ValueError):
                RoleGrant(*args)

    def test_unique_event_publisher_and_owner(self):
        for role in (R.DEPUTY, R.REVIEW_OWNER):
            with self.assertRaises(ValueError):
                AuthorizationPolicy((RoleGrant('a', role, 'e'), RoleGrant('b', role, 'e')))
            AuthorizationPolicy((RoleGrant('a', role, 'e'), RoleGrant('b', role, 'other')))

    def test_snapshot_does_not_follow_mutable_input(self):
        grants = [RoleGrant('d', R.DEPUTY, 'e')]
        policy = AuthorizationPolicy(grants)
        grants.append(RoleGrant('attacker', R.DEPUTY, 'other'))
        with self.assertRaises(PermissionError):
            policy.require('attacker', A.PUBLISH_TASK, event_id='other')


if __name__ == '__main__':
    unittest.main()
