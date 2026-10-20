"""V2 authorization policy; inputs come only from trusted server-side state.

This module does not authenticate users or grant roles. Never construct grants
from request fields. Legacy panel routes are not yet wired to this policy.
"""

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    DUTY = 'duty'
    DEPUTY = 'designated_deputy'
    CHIEF = 'chief'
    PROFESSIONAL = 'professional'
    LEADER = 'team_or_group_leader'
    MEMBER = 'member'
    PERSONNEL_ADMIN = 'personnel_admin'
    LOGISTICS_ADMIN = 'logistics_admin'
    REVIEW_OWNER = 'review_owner'


class Action(str, Enum):
    REGISTER_EVENT = 'register_event'
    START_EVENT = 'start_event'
    PREPARE_LOCAL_NOTIFICATION = 'prepare_local_temporary_notification'
    RECORD_APPOINTMENT = 'record_offline_appointment'
    PUBLISH_TASK = 'publish_task'
    ASSESS_TASK_REVIEW = 'assess_task_review'
    RECORD_OFFLINE_TASK = 'record_offline_task'
    PUBLISH_MAP = 'publish_map'
    PUBLISH_DEPLOYMENT = 'publish_deployment'
    ASSIGN_PERSONNEL = 'summon_assign_reassign_personnel'
    REVIEW_FORCE_NEEDS = 'review_force_needs'
    ASSIGN_REVIEW_OWNER = 'assign_review_owner'
    SUBMIT_PROFESSIONAL_OPINION = 'submit_professional_opinion'
    RECEIVE_TASK = 'receive_task'
    RECEIVE_CALLOUT = 'receive_callout'
    REPORT_TEAM = 'report_team'
    MAINTAIN_PERSONNEL = 'maintain_personnel'
    MAINTAIN_MATERIALS = 'maintain_materials'
    CONFIRM_MATERIAL_TRANSFER = 'confirm_material_transfer'
    CONFIRM_CAUSE = 'confirm_cause'
    PUBLISH_RECTIFICATION = 'publish_rectification'
    CONFIRM_EXPERIENCE = 'confirm_experience'
    APPROVE_REPORT = 'approve_report'
    PUBLISH_REPORT = 'publish_report'
    PUBLISH_EXTERNAL_NEWS = 'publish_external_news'


_GLOBAL = frozenset({Role.DUTY, Role.PERSONNEL_ADMIN, Role.LOGISTICS_ADMIN})
_TEAM_ACTIONS = frozenset({Action.RECEIVE_TASK, Action.RECEIVE_CALLOUT, Action.REPORT_TEAM})
_NO_EVENT = frozenset({Action.REGISTER_EVENT, Action.MAINTAIN_PERSONNEL,
                       Action.MAINTAIN_MATERIALS})
_ROLE_ACTIONS = {
    Role.DUTY: frozenset({Action.REGISTER_EVENT, Action.START_EVENT,
                          Action.PREPARE_LOCAL_NOTIFICATION,
                          Action.RECORD_APPOINTMENT}),
    Role.DEPUTY: frozenset({Action.PUBLISH_TASK, Action.ASSESS_TASK_REVIEW,
                            Action.RECORD_OFFLINE_TASK, Action.PUBLISH_MAP,
                            Action.PUBLISH_DEPLOYMENT, Action.ASSIGN_PERSONNEL,
                            Action.REVIEW_FORCE_NEEDS,
                            Action.ASSIGN_REVIEW_OWNER,
                            Action.CONFIRM_MATERIAL_TRANSFER}),
    Role.PROFESSIONAL: frozenset({Action.SUBMIT_PROFESSIONAL_OPINION}),
    Role.LEADER: _TEAM_ACTIONS,
    Role.PERSONNEL_ADMIN: frozenset({Action.MAINTAIN_PERSONNEL}),
    Role.LOGISTICS_ADMIN: frozenset({Action.MAINTAIN_MATERIALS}),
    Role.REVIEW_OWNER: frozenset({Action.CONFIRM_CAUSE,
                                  Action.PUBLISH_RECTIFICATION,
                                  Action.CONFIRM_EXPERIENCE,
                                  Action.APPROVE_REPORT, Action.PUBLISH_REPORT}),
}


def _valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


@dataclass(frozen=True)
class RoleGrant:
    subject_id: str
    role: Role
    event_id: str = ''
    team_id: str = ''

    def __post_init__(self) -> None:
        if not _valid_id(self.subject_id) or not isinstance(self.role, Role):
            raise ValueError('A stable subject and known role are required')
        if self.role in _GLOBAL:
            if self.event_id != '' or self.team_id != '':
                raise ValueError('Long-term roles cannot carry event/team scope')
        elif not _valid_id(self.event_id):
            raise ValueError('Event roles require an explicit event')
        if self.role == Role.LEADER:
            if not _valid_id(self.team_id):
                raise ValueError('A leader must be assigned to a specific team/group')
        elif self.team_id != '':
            raise ValueError('Only a leader grant carries team scope')


class AuthorizationPolicy:
    """Immutable snapshot of validated grants, rebuilt after assignment changes.

    Authenticated subject and resource scope must be resolved by the caller,
    not accepted from HTTP parameters. This is a role gate only: professional
    review, resource existence and workflow prerequisites remain separate gates.
    """

    def __init__(self, grants: tuple[RoleGrant, ...] = ()) -> None:
        self._grants = tuple(grants)
        unique_holders: dict[tuple[str, Role], str] = {}
        for grant in self._grants:
            if not isinstance(grant, RoleGrant):
                raise ValueError('Only validated role grants are accepted')
            if grant.role in {Role.DEPUTY, Role.REVIEW_OWNER}:
                key = (grant.event_id, grant.role)
                previous = unique_holders.setdefault(key, grant.subject_id)
                if previous != grant.subject_id:
                    raise ValueError('An event may have only one holder of this role')

    def require(self, subject_id: str, action: Action, *,
                event_id: str = '', team_id: str = '') -> None:
        if not _valid_id(subject_id) or not isinstance(action, Action):
            raise PermissionError('Authenticated subject and known action required')
        if action in _NO_EVENT:
            valid_scope = event_id == '' and team_id == ''
        else:
            valid_scope = _valid_id(event_id) and (
                _valid_id(team_id) if action in _TEAM_ACTIONS else team_id == '')
        if not valid_scope:
            raise PermissionError('Explicit resource scope required')
        for grant in self._grants:
            if grant.subject_id != subject_id:
                continue
            if action not in _ROLE_ACTIONS.get(grant.role, frozenset()):
                continue
            if grant.role not in _GLOBAL and grant.event_id != event_id:
                continue
            if action in _TEAM_ACTIONS and grant.team_id != team_id:
                continue
            return
        raise PermissionError('Action is not authorized in this scope')
