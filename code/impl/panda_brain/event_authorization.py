"""Audited initial appointments. No implicit commander grant.

Trusted application construction supplies the session resolver and long-term
grants. Deputy/review-owner replacements remain deferred pending U03 rules;
event professional duties have their separately audited deputy assignment.
"""
import json
from dataclasses import asdict
from datetime import datetime, timezone

from .authorization import Action, AuthorizationPolicy, Role, RoleGrant
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time

KIND = 'event_role_assignment'
_ROLES = {Role.DEPUTY, Role.REVIEW_OWNER}


def event_grants(workspace):
    """Reconcile security records with their audit entries before using authority.

    The legacy hash chain detects corruption, not a database administrator who
    rewrites the entire chain. Database integrity is not cryptographic identity.
    """
    records = sorted((r for r in workspace.object_revisions.values()
                      if r.reference.object_kind == KIND),
                     key=lambda r: r.reference.object_id)
    audited = [e.payload for e in workspace.events if e.kind == 'ObjectRevisionRecorded'
               and e.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND]
    if not records and not audited:
        return ()
    if workspace.verify_audit_chain():
        raise ValueError('Authorization audit chain is damaged')
    if len(records) != len(audited):
        raise ValueError('Authorization records and audit trail disagree')
    grants = []
    seen = set()
    for record in records:
        ref = record.reference
        if not workspace.event_id or ref.event_id != workspace.event_id or ref.version != 1:
            raise ValueError('Invalid appointment scope or unsupported replacement')
        explicit_time(record.times.recorded_at)
        content = json.loads(record.content_json)
        if set(content) != {'subject_id', 'role', 'evidence'} or not isinstance(content['evidence'], str) or not content['evidence'].strip():
            raise ValueError('Appointment evidence is required')
        role = Role(content['role'])
        if role not in _ROLES or ref.object_id != role.value or role in seen:
            raise ValueError('Invalid or duplicate appointment role')
        if record.source != 'manual-appointment' or record.data_kind != 'reported':
            raise ValueError('Invalid appointment provenance')
        expected = {'revision_id': record.id, 'revision': asdict(record)}
        if audited.count(expected) != 1:
            raise ValueError('Appointment does not match its audit record')
        seen.add(role)
        grants.append(RoleGrant(content['subject_id'], role, workspace.event_id))
    AuthorizationPolicy(tuple(grants))
    return tuple(grants)


class EventAuthorizationService:
    def __init__(self, workspace, *, resolve_subject, global_grants=(), subject_exists):
        self.workspace = workspace
        self.resolve_subject = resolve_subject
        self.subject_exists = subject_exists
        self.global_grants = tuple(global_grants)
        for grant in self.global_grants:
            if not isinstance(grant, RoleGrant) or grant.event_id or grant.team_id:
                raise ValueError('Only trusted long-term grants belong here')
        self._policy()

    def _policy(self):
        from .professional_assignments import grants
        return AuthorizationPolicy(self.global_grants + event_grants(self.workspace) + grants(self.workspace))

    def require(self, token, action, *, event_id, team_id='', professional_scope=''):
        if not self.workspace.event_id or event_id != self.workspace.event_id:
            raise PermissionError('Authorization workspace belongs to another event')
        subject = self.resolve_subject(token)
        self._policy().require(subject, action, event_id=event_id, team_id=team_id)
        if action == Action.SUBMIT_PROFESSIONAL_OPINION:
            from .professional_assignments import require_scope
            if not self.subject_exists(subject):
                raise PermissionError('专业人员已不在可信人员库。')
            require_scope(self.workspace, subject, professional_scope)
        elif professional_scope:
            raise ValueError('该操作不接受专业范围字段。')
        return subject

    def record_initial_appointment(self, token, *, event_id, role, subject_id,
                                   evidence, request_id, occurred_at=''):
        if not isinstance(role, Role) or role not in _ROLES:
            raise ValueError('Only initial deputy/review-owner appointments are supported')
        action = Action.RECORD_APPOINTMENT if role == Role.DEPUTY else Action.ASSIGN_REVIEW_OWNER
        actor = self.require(token, action, event_id=event_id)
        grant = RoleGrant(subject_id, role, event_id)
        if not self.subject_exists(grant.subject_id):
            raise ValueError('Appointee must exist in the trusted personnel registry')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('Appointment evidence/reference is required')
        content = json.dumps({'subject_id': subject_id, 'role': role.value, 'evidence': evidence},
                             ensure_ascii=False, sort_keys=True)
        # Idempotency precedes server timestamp generation; restarts preserve it.
        for previous in self.workspace.object_revisions.values():
            if previous.request_id == request_id:
                if (previous.reference == ObjectReference(event_id, KIND, role.value, 1)
                        and previous.actor_id == actor and previous.content_json == content
                        and previous.times.occurred_at == occurred_at):
                    return previous
                raise ValueError('Request ID already used for different content')
        if any(g.role == role for g in event_grants(self.workspace)):
            raise ValueError('Initial appointment already exists; replacement requires U03 policy')
        record = ObjectRevision(
            reference=ObjectReference(event_id, KIND, role.value, 1),
            times=RecordTimes(occurred_at=occurred_at, recorded_at=datetime.now(timezone.utc).isoformat()),
            source='manual-appointment', data_kind='reported', actor_id=actor,
            request_id=request_id, content_json=content)
        return self.workspace._record_object_revision(record, expected_version=0)
