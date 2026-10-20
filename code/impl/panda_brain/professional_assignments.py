"""Event-scoped professional duties assigned by the named deputy.

An assignment is authority to submit an opinion in the specified scope, not a
professional conclusion, collective decision or permission to publish tasks.
"""
from copy import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from .authorization import Action, Role, RoleGrant
from .event_authorization import event_grants
from .fire_events import ONBOARDABLE_STATUSES
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time

KIND = 'professional_assignment'
SOURCE = 'named-deputy-professional-duty'
FIELDS = {'subject_id', 'professional_scope', 'active', 'evidence', 'reason'}


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('人员、专业范围、指定依据及理由须明确填写。')
    return value


def object_id(subject_id, professional_scope):
    value = json.dumps([_text(subject_id), _text(professional_scope)], ensure_ascii=False)
    return 'professional-duty-' + hashlib.sha256(value.encode()).hexdigest()


from .read_scope import validated_read


@validated_read
def _checked_history(workspace):
    if workspace.verify_audit_chain():
        raise ValueError('专业职责与事件审计不一致。')
    records = {r.id: r for r in workspace.object_revisions.values() if r.reference.object_kind == KIND}
    mirrors = [(i, e) for i, e in enumerate(workspace.events)
               if e.kind == 'ObjectRevisionRecorded'
               and e.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND]
    if not workspace.event_id and (records or mirrors):
        raise ValueError('专业职责缺少事件归属。')
    if len(records) != len(mirrors):
        raise ValueError('专业职责记录与审计镜像不一致。')
    versions = {}; clocks = {}; requests = set(); result = []
    for index, event in mirrors:
        record = records.get(event.payload.get('revision_id'))
        if record is None or event.payload != {'revision_id': record.id, 'revision': asdict(record)}:
            raise ValueError('专业职责审计镜像不一致。')
        value = json.loads(record.content_json); ref = record.reference
        if (set(value) != FIELDS or type(value['active']) is not bool
                or ref.event_id != workspace.event_id or record.source != SOURCE
                or record.data_kind != 'reported' or record.request_id in requests
                or ref.object_id != object_id(value['subject_id'], value['professional_scope'])
                or ref.version != versions.get(ref.object_id, 0) + 1):
            raise ValueError('专业职责来源、字段或连续版本不一致。')
        _text(value['evidence']); _text(value['reason'])
        if ref.version == 1 and not value['active']:
            raise ValueError('没有已指定职责可撤销。')
        if result and any(r.reference.object_id == ref.object_id for r in result):
            prior = next(r for r in reversed(result) if r.reference.object_id == ref.object_id)
            if json.loads(prior.content_json)['active'] == value['active']:
                raise ValueError('专业职责没有实际授予或撤销变化。')
        from .audited_records import prefix as before
        prefix = before(workspace,index)
        if not any(g.role == Role.DEPUTY and g.subject_id == record.actor_id
                   for g in event_grants(prefix)):
            raise ValueError('指定时没有本事件副指挥长职责。')
        recorded = explicit_time(record.times.recorded_at)
        if (record.times.occurred_at or record.times.collected_at or record.times.received_at
                or recorded > explicit_time(event.recorded_at)
                or ref.object_id in clocks and recorded < clocks[ref.object_id]):
            raise ValueError('专业职责记录时间不一致。')
        versions[ref.object_id] = ref.version; clocks[ref.object_id] = recorded
        requests.add(record.request_id); result.append(record)
    return tuple(result)


def history(workspace):
    try:
        return _checked_history(workspace)
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('专业职责来源格式不完整，不回退旧授权。') from error


def current(workspace):
    return {r.reference.object_id: r for r in history(workspace)}


def grants(workspace):
    subjects = {json.loads(r.content_json)['subject_id'] for r in current(workspace).values()
                if json.loads(r.content_json)['active']}
    return tuple(RoleGrant(subject, Role.PROFESSIONAL, workspace.event_id) for subject in sorted(subjects))


def require_scope(workspace, subject_id, professional_scope):
    _text(professional_scope)
    record = current(workspace).get(object_id(subject_id, professional_scope))
    if record is None or not json.loads(record.content_json)['active']:
        raise PermissionError('没有本事件该专业范围的当前指定职责。')
    return record


def record(workspace, authorization, registry, token, *, event_id, subject_id,
           professional_scope, active, expected_version, evidence, reason, request_id):
    actor = authorization.require(token, Action.ASSESS_TASK_REVIEW, event_id=event_id)
    event = registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('关闭或未启动事件不接受专业职责变更。')
    _text(subject_id)
    if subject_id not in registry.library:
        raise ValueError('须指定长期人员库中可核对的具名人员。')
    if type(active) is not bool or type(expected_version) is not int or expected_version < 0:
        raise ValueError('专业职责操作或版本不正确。')
    key = object_id(subject_id, professional_scope)
    for value in (evidence, reason, request_id): _text(value)
    records = history(workspace)
    content = json.dumps(dict(subject_id=subject_id, professional_scope=professional_scope,
                              active=active, evidence=evidence, reason=reason), ensure_ascii=False, sort_keys=True)
    for previous in workspace.object_revisions.values():
        if previous.request_id == request_id:
            if (previous.reference.object_kind == KIND and previous.reference.object_id == key
                    and previous.reference.event_id == event_id
                    and previous.actor_id == actor and previous.content_json == content
                    and previous.reference.version == expected_version + 1):
                return previous
            raise ValueError('请求号已用于不同内容。')
    matches = [r for r in records if r.reference.object_id == key]
    if len(matches) != expected_version:
        raise ValueError('专业职责版本已变化，请刷新。')
    if (not matches and not active) or (matches and json.loads(matches[-1].content_json)['active'] == active):
        raise ValueError('须有实际授予或撤销变化。')
    revision = ObjectRevision(ObjectReference(event_id, KIND, key, expected_version + 1),
                              RecordTimes(recorded_at=datetime.now(timezone.utc).isoformat()),
                              SOURCE, 'reported', actor, request_id, content)
    return workspace._record_object_revision(revision, expected_version=expected_version)
