"""Strict append-only revision helpers shared by the V2 application services."""
from copy import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time
from .read_scope import validated_read


def text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('身份、范围、依据和理由须明确填写。')
    return value


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


@validated_read
def prefix(workspace, index):
    result = copy(workspace)
    result._events = list(workspace.events[:index])
    ids = {e.payload.get('revision_id') for e in result.events if e.kind == 'ObjectRevisionRecorded'}
    result.object_revisions = {k: r for k, r in workspace.object_revisions.items() if k in ids}
    return result


@validated_read
def records(workspace, kind):
    if not workspace.event_id or workspace.verify_audit_chain():
        raise ValueError('事件归属或审计链不一致。')
    objects = {r.id: r for r in workspace.object_revisions.values() if r.reference.object_kind == kind}
    mirrors = [(i, e) for i, e in enumerate(workspace.events) if e.kind == 'ObjectRevisionRecorded'
               and e.payload.get('revision', {}).get('reference', {}).get('object_kind') == kind]
    if len(objects) != len(mirrors):
        raise ValueError('记录和审计镜像不一致，不回退旧版。')
    versions = {}; clocks = {}; requests = set(); checked = []
    for index, event in mirrors:
        record = objects.get(event.payload.get('revision_id'))
        if record is None or event.payload != {'revision_id': record.id, 'revision': asdict(record)}:
            raise ValueError('审计镜像不一致。')
        ref = record.reference
        recorded = explicit_time(record.times.recorded_at)
        if (ref.event_id != workspace.event_id or ref.version != versions.get(ref.object_id, 0) + 1
                or record.data_kind != 'reported' or record.request_id in requests
                or recorded > explicit_time(event.recorded_at)
                or recorded < clocks.get(ref.object_id, recorded)
                or record.times.occurred_at or record.times.collected_at or record.times.received_at):
            raise ValueError('记录归属、版本、时间或来源不一致。')
        text(record.actor_id); text(record.request_id); text(record.source)
        value = json.loads(record.content_json)
        if not isinstance(value, dict):
            raise ValueError('记录内容须为结构化对象。')
        versions[ref.object_id] = ref.version; clocks[ref.object_id] = recorded
        requests.add(record.request_id); checked.append((index, record, value))
    return tuple(checked)


def append(workspace, *, kind, object_id, source, actor, values, expected_version, request_id):
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('版本须为明确非负整数。')
    text(actor); text(request_id)
    checked = records(workspace, kind)
    content = dump(values)
    for old in workspace.object_revisions.values():
        if old.request_id == request_id:
            if (old.reference == ObjectReference(workspace.event_id, kind, object_id, expected_version + 1)
                    and old.actor_id == actor and old.source == source and old.content_json == content):
                return old
            raise ValueError('请求号已用于不同内容。')
    count = sum(r.reference.object_id == object_id for _, r, _ in checked)
    if count != expected_version:
        raise ValueError('记录版本已变化，请刷新。')
    revision = ObjectRevision(ObjectReference(workspace.event_id, kind, object_id, count + 1),
        RecordTimes(recorded_at=datetime.now(timezone.utc).isoformat()), source, 'reported', actor, request_id, content)
    return workspace._record_object_revision(revision, expected_version=count)
