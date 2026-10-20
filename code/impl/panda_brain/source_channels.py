"""Event-bound adapter reports. Connectivity declarations are not device proof."""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json

from .models import Evidence, SourceTrustLevel

KIND = 'SourceChannelReportRecorded'
FIELDS = {'event_id', 'channel_id', 'channel_kind', 'source_record_id', 'source_version',
          'state', 'collected_at', 'received_at', 'quality_note', 'coordinate_system',
          'media_kind', 'media_ref', 'summary', 'targets', 'evidence_refs'}
STATES = {'connected': '来源声明可用（未实测）', 'disconnected': '来源报告断连',
          'delayed': '来源报告延迟', 'unknown': '连接状态未知'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def clock(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('来源时间须含时区。')
    return parsed


def _text(value, *, required=False, limit=2000):
    if (not isinstance(value, str) or value != value.strip() or len(value) > limit
            or (required and not value) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('来源字段须为长度受限的明确文本。')
    return value


def channel_history(workspace, event_id):
    if not event_id or workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('来源通道事件或审计不一致。')
    recorded = {e.payload['evidence']['id']: e.payload['evidence'] for e in workspace.events
                if e.kind == 'EvidenceRecorded' and isinstance(e.payload.get('evidence'), dict)}
    rows = []
    for entry in workspace.events:
        if entry.kind != KIND or entry.payload.get('event_id') != event_id:
            continue
        row = entry.payload
        if (set(row) != {'event_id', 'source', 'data_kind', 'record', 'evidence_id', 'disposition'}
                or row.get('data_kind') not in {'simulated', 'observed', 'unknown'}
                or row.get('disposition') not in {'created', 'updated', 'late', 'clock_unknown',
                                                 'same_time_conflict', 'source_kind_conflict'}
                or not isinstance(row.get('record'), dict) or set(row['record']) != FIELDS
                or row['record'].get('event_id') != event_id):
            raise ValueError('来源通道记录格式不一致。')
        evidence = workspace.evidence.get(row.get('evidence_id'))
        if (evidence is None or canonical(asdict(evidence)) != canonical(recorded.get(evidence.id))
                or evidence.content != canonical(row.get('record')) or evidence.source != row['source']
                or evidence.trust_level != SourceTrustLevel.PENDING_VERIFICATION):
            raise ValueError('来源通道证据快照不一致。')
        rows.append(row)
    return rows


def channel_state(workspace, event_id):
    current = {}
    for row in channel_history(workspace, event_id):
        if row['disposition'] in {'created', 'updated'}:
            record = row['record']
            current[(row['source'], record['channel_id'])] = row
    return list(current.values())


def receive_channel(workspace, record, *, source, data_kind):
    """Caller supplies adapter identity. No HTTP, geometry, approval or inference."""
    event_id = workspace.event_id
    history = channel_history(workspace, event_id)
    _text(source, required=True, limit=200)
    if data_kind not in {'simulated', 'observed', 'unknown'}:
        raise ValueError('来源性质无效。')
    if not isinstance(record, dict) or set(record) - FIELDS:
        raise ValueError('来源通道字段无效。')
    row = dict(record)
    for field in ('event_id', 'channel_id', 'channel_kind', 'source_record_id', 'state'):
        _text(row.get(field), required=True, limit=200)
    if (row['event_id'] != event_id or row['channel_kind'] not in {'camera', 'weather'}
            or row['state'] not in STATES or type(row.get('source_version')) is not int
            or row['source_version'] < 1):
        raise ValueError('来源绑定、通道类型、状态或版本无效。')
    for field in ('collected_at', 'received_at', 'quality_note', 'coordinate_system',
                  'media_kind', 'media_ref', 'summary'):
        row[field] = _text(row.get(field, ''))
    clock(row['collected_at'])
    clock(row['received_at'])
    if row['media_kind'] not in {'', 'image', 'video'} or bool(row['media_kind']) != bool(row['media_ref']):
        raise ValueError('截图/视频须同时提供类型和原始引用；引用不等于已取得文件。')
    targets = row.setdefault('targets', [])
    refs = row.setdefault('evidence_refs', [])
    if (not isinstance(targets, list) or len(targets) > 30 or not isinstance(refs, list)
            or len(refs) > 30 or len(refs) != len(set(str(r) for r in refs))):
        raise ValueError('来源关联列表无效。')
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {'kind', 'id'}
                or target['kind'] not in {'map', 'task', 'personnel', 'material'}):
            raise ValueError('来源关联对象无效。')
        _text(target['id'], required=True, limit=200)
    bound_refs = {e.payload.get('evidence_id') for e in workspace.events
                  if e.payload.get('event_id') == event_id and e.kind in {'ObservationIntakeRecorded', KIND}}
    for ref in refs:
        _text(ref, required=True, limit=200)
        if ref not in bound_refs or ref not in workspace.evidence:
            raise ValueError('关联证据未绑定本事件。')
    identity = (source, row['source_record_id'], row['source_version'])
    comparable = dict(row)
    comparable.pop('received_at')  # A retry never rewrites the first receipt clock.
    for old in history:
        original = old['record']
        if (old['source'], original['source_record_id'], original['source_version']) != identity:
            continue
        previous = dict(original)
        previous.pop('received_at')
        if canonical(previous) != canonical(comparable) or old['data_kind'] != data_kind:
            raise ValueError('同来源版本内容冲突，请提供新版本。')
        return dict(old, duplicate=True)
    current = next((r for r in channel_state(workspace, event_id)
                    if r['source'] == source and r['record']['channel_id'] == row['channel_id']), None)
    disposition = 'created'
    if current:
        old_clock, new_clock = clock(current['record']['collected_at']), clock(row['collected_at'])
        if current['data_kind'] != data_kind:
            disposition = 'source_kind_conflict'
        elif old_clock is None or new_clock is None:
            disposition = 'clock_unknown'
        elif new_clock < old_clock:
            disposition = 'late'
        elif new_clock == old_clock:
            disposition = 'same_time_conflict'
        else:
            disposition = 'updated'
    digest = hashlib.sha256(canonical([event_id, source, data_kind, comparable]).encode('utf-8')).hexdigest()
    evidence = Evidence.create('channel-'+digest, canonical(row), source, SourceTrustLevel.PENDING_VERIFICATION)
    workspace.record_evidence(evidence)
    payload = dict(event_id=event_id, source=source, data_kind=data_kind, record=row,
                   evidence_id=evidence.id, disposition=disposition)
    workspace.append_event(KIND, **payload)
    return payload
