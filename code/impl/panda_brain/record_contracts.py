"""P01 shared record contract; unknown clocks stay empty, never inferred."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime


def explicit_time(value: str) -> datetime:
    """Parse a supplied instant without assuming a timezone."""
    if not isinstance(value, str) or not value:
        raise ValueError('A nonempty timestamp with UTC offset is required')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Timestamp requires an explicit UTC offset')
    return parsed


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('Identifiers must be nonempty and have no surrounding whitespace')


@dataclass(frozen=True)
class ObjectReference:
    event_id: str
    object_kind: str
    object_id: str
    version: int

    def __post_init__(self):
        for value in (self.event_id, self.object_kind, self.object_id):
            _identifier(value)
        if type(self.version) is not int or self.version < 1:
            raise ValueError('Object version must be a positive integer')


@dataclass(frozen=True)
class RecordTimes:
    occurred_at: str = ''
    collected_at: str = ''
    received_at: str = ''
    recorded_at: str = ''

    def __post_init__(self):
        for value in (self.occurred_at, self.collected_at, self.received_at, self.recorded_at):
            if value != '':
                explicit_time(value)


@dataclass(frozen=True)
class ObjectRevision:
    reference: ObjectReference
    times: RecordTimes
    source: str
    data_kind: str
    actor_id: str
    request_id: str
    # Immutable canonical JSON snapshot, not a pointer to a mutable live object.
    content_json: str

    def __post_init__(self):
        if not isinstance(self.reference, ObjectReference) or not isinstance(self.times, RecordTimes):
            raise ValueError('Typed reference and times are required')
        for value in (self.source, self.actor_id, self.request_id):
            _identifier(value)
        if self.data_kind not in {'reported', 'observed', 'inferred', 'simulated', 'unknown'}:
            raise ValueError('Unknown data kind; verification is a separate business act')
        if not isinstance(self.content_json, str):
            raise ValueError('Content must be a JSON string')
        content = json.loads(self.content_json)
        if not isinstance(content, dict):
            raise ValueError('Content must be a JSON object')
        canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False)
        object.__setattr__(self, 'content_json', canonical)

    @property
    def id(self) -> str:
        ref = self.reference
        value = json.dumps([ref.event_id, ref.object_kind, ref.object_id, ref.version], ensure_ascii=False)
        return 'revision-' + hashlib.sha256(value.encode('utf-8')).hexdigest()
