"""Immutable recorded map states, separate from annotation/base-map versions."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time

KIND = 'map_annotation'


def _now():
    return datetime.now(timezone.utc).isoformat()


def capture_annotation(workspace, annotation, operation):
    if not workspace.event_id:
        return None  # Legacy workspace: no guessed event and no invented history.
    if annotation.event_id != workspace.event_id:
        raise ValueError('Map annotation belongs to another event or is unbound')
    from .persistence import encode_entity
    history = [r for r in workspace.object_revisions.values()
               if (r.reference.event_id, r.reference.object_kind, r.reference.object_id)
               == (workspace.event_id, KIND, annotation.id)]
    version = max((r.reference.version for r in history), default=0) + 1
    reference = ObjectReference(workspace.event_id, KIND, annotation.id, version)
    request_key = hashlib.sha256(json.dumps(asdict(reference), sort_keys=True).encode()).hexdigest()
    content = {'operation': operation, 'annotation': json.loads(encode_entity(annotation))}
    if operation == 'revision_draft':
        parent = latest_annotation_reference(workspace, event_id=workspace.event_id,
                                              annotation_id=annotation.supersedes_id)
        content['supersedes_reference'] = asdict(parent) if parent else None
    record = ObjectRevision(reference, RecordTimes(recorded_at=_now()),
        source='map-workspace', data_kind='simulated' if annotation.is_demo else (
            'inferred' if annotation.source.value == 'ai-suggestion' else 'reported'),
        actor_id='map-workspace', request_id='map-history-' + request_key,
        content_json=json.dumps(content, ensure_ascii=False))
    return workspace._record_object_revision(record, expected_version=version - 1)


def latest_annotation_reference(workspace, *, event_id, annotation_id):
    if not workspace.event_id or event_id != workspace.event_id:
        raise ValueError('Map reference belongs to another event or workspace is unbound')
    records = [r for r in workspace.object_revisions.values()
               if (r.reference.event_id, r.reference.object_kind, r.reference.object_id)
               == (event_id, KIND, annotation_id)]
    return max(records, key=lambda r: r.reference.version).reference if records else None


def resolve_annotation_reference(workspace, reference, *, known_at=None):
    if (not isinstance(reference, ObjectReference) or reference.object_kind != KIND
            or not workspace.event_id or reference.event_id != workspace.event_id):
        raise ValueError('An explicit map reference within the bound event is required')
    cutoff = explicit_time(known_at) if known_at is not None else None
    record = next((r for r in workspace.object_revisions.values() if r.reference == reference), None)
    if record is None or (cutoff is not None and explicit_time(record.times.recorded_at) > cutoff):
        return None
    if workspace.verify_audit_chain():
        raise ValueError('Map history audit chain is damaged')
    expected = {'revision_id': record.id, 'revision': asdict(record)}
    if sum(e.kind == 'ObjectRevisionRecorded' and e.payload == expected for e in workspace.events) != 1:
        raise ValueError('Map history record does not match its audit mirror')
    from .persistence import decode_entity
    from .map_annotations import MapAnnotation
    payload = json.loads(record.content_json)
    annotation = decode_entity(json.dumps(payload['annotation'], ensure_ascii=False))
    if not isinstance(annotation, MapAnnotation) or (annotation.event_id, annotation.id) != (reference.event_id, reference.object_id):
        raise ValueError('Map snapshot identity mismatch')
    return annotation  # Fresh object; changes to it cannot mutate stored history.


def annotation_at(workspace, *, event_id, annotation_id, known_at):
    record = workspace.object_revision_at(event_id, KIND, annotation_id, known_at)
    if record is None:
        return None
    return resolve_annotation_reference(workspace, record.reference, known_at=known_at)
