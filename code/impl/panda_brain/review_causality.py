"""P12-01: versioned, event-bound evidence review for issue and cause candidates."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from .authorization import Action
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes
from . import review_meeting

KIND = 'review_causal_assessment'


def assessments(state):
    """Return only this event's latest immutable assessment per candidate."""
    latest = {}
    for revision in state.workspace.object_revisions.values():
        ref = revision.reference
        if ref.event_id != state.active_event_id or ref.object_kind != KIND:
            continue
        previous = latest.get(ref.object_id)
        if previous is None or previous.reference.version < ref.version:
            latest[ref.object_id] = revision
    return latest


def decision_basis(state, kind, target_id, assessment_ref, reason, *, positive):
    """Require the latest reviewed evidence version before a human conclusion."""
    latest = assessments(state).get(f'{kind}:{target_id}')
    if latest is None or latest.id != assessment_ref:
        raise ValueError('须先核对本事件最新证据核查版本；新证据出现后需重新判断。')
    content = json.loads(latest.content_json)
    if not reason.strip() or not content.get('meeting_ref') or not content.get('meeting_finding'):
        raise ValueError('负责人结论须说明理由并关联已登记的会议核实记录。')
    if positive:
        if kind == 'hypothesis' and content.get('causal_layer') not in {'direct', 'deep'}:
            raise ValueError('确认根因前须区分直接原因或深层原因；无法区分则保留待验证。')
        if not content.get('support_refs'):
            raise ValueError('无支持证据不得确认问题或根因。')
        if content.get('missing_info') and content['missing_info'] not in {'无', '无缺失'}:
            raise ValueError('仍有缺失信息，应保留待验证。')
        if content.get('counter_refs') and not content.get('counter_note'):
            raise ValueError('已有反证须先登记核查说明。')
    return latest


def current_confirmation(state, kind, target_id):
    """A prior conclusion becomes stale when a newer assessment is recorded."""
    record = ((state.review_ledger.issues if kind == 'issue' else
               state.review_ledger.hypotheses).get(target_id))
    latest = assessments(state).get(f'{kind}:{target_id}')
    status = (record.review_status if kind == 'issue' else record.status) if record else ''
    expected = 'confirmed' if kind == 'issue' else 'verified'
    return bool(record and latest and record.event_id == state.active_event_id
                and status == expected and record.confirmed_assessment_ref == latest.id)


def evidence_refs(state, value):
    refs = tuple(part.strip() for part in value.replace('；', ';').replace(',', ';').split(';') if part.strip())
    if len(refs) != len(set(refs)) or len(refs) > 30:
        raise ValueError('证据引用重复或超过 30 项。')
    known = {ev.id for ev in state.workspace.events}
    known.update(state.workspace.evidence)
    known.update(state.workspace.object_revisions)
    known.update(f'audit#{ev.sequence}' for ev in state.workspace.events)
    if any(ref not in known for ref in refs):
        raise ValueError('证据须引用本事件现有审计、证据或不可变版本；规则提示不能当作已核实证据。')
    return refs


from .durable_storage import durable_action


@durable_action
def record_assessment(state, token, fields):
    """Owner's meeting assessment. Every correction appends a new object version."""
    if not state.workspace.event_id or state.workspace.event_id != state.active_event_id:
        raise ValueError('证据核查仅适用于已绑定的当前事件任务空间。')
    actor = review_meeting.service(state).require(
        token, Action.APPROVE_REPORT, event_id=state.active_event_id)
    required = {'target_kind', 'target_id', 'alternatives', 'verify_targets',
                'expected_object_version', 'request_id'}
    optional = {'support_refs', 'counter_refs', 'counter_note', 'missing_info',
                'meeting_ref', 'meeting_finding', 'causal_layer'}
    if (not required <= set(fields) or set(fields) - required - optional
            or any(not isinstance(value, str) or len(value) > 4000
                   for value in fields.values())):
        raise ValueError('核查记录字段不完整或长度无效。')
    fields = {**dict.fromkeys(optional, ''), **fields}
    layer = fields['causal_layer'].strip() or 'undetermined'
    if layer not in {'direct', 'deep', 'undetermined'}:
        raise ValueError('原因层级只能为直接、深层或待判。')
    target_kind = fields['target_kind'].strip()
    target_id = fields['target_id'].strip()
    mapping = {'issue': state.review_ledger.issues,
               'hypothesis': state.review_ledger.hypotheses}.get(target_kind)
    target = mapping.get(target_id) if mapping is not None else None
    if target is None or target.event_id != state.active_event_id:
        raise ValueError('问题或根因候选不属于当前事件。')
    object_id = f'{target_kind}:{target_id}'
    latest = assessments(state).get(object_id)
    current_version = latest.reference.version if latest else 0
    try:
        expected_version = int(fields['expected_object_version'])
    except ValueError as exc:
        raise ValueError('核查记录版本无效。') from exc
    support = evidence_refs(state, fields['support_refs'])
    counter = evidence_refs(state, fields['counter_refs'])
    meeting_ref = fields['meeting_ref'].strip()
    if meeting_ref:
        meeting = state.workspace.object_revisions.get(meeting_ref)
        if (meeting is None or meeting.reference.event_id != state.active_event_id
                or meeting.reference.object_kind != 'review_meeting_record'):
            raise ValueError('会议核实须引用本事件已有会议记录版本。')
    content = {
        'target_kind': target_kind, 'target_id': target_id,
        'support_refs': support, 'counter_refs': counter,
        'counter_note': fields['counter_note'].strip(),
        'alternatives': fields['alternatives'].strip(),
        'missing_info': fields['missing_info'].strip(),
        'verify_targets': fields['verify_targets'].strip(),
        'meeting_ref': meeting_ref,
        'meeting_finding': fields['meeting_finding'].strip(),
        'causal_layer': layer,
        'status': 'pending_verification',
    }
    if not content['alternatives'] or not content['verify_targets']:
        raise ValueError('须记录其他解释与待核实对象；未知时请明确写“待核”。')
    if content['meeting_finding'] and not meeting_ref:
        raise ValueError('会议核实结论须关联会议原始记录版本。')
    request_id = fields['request_id'].strip()
    if not request_id:
        raise ValueError('请求编号不能为空。')
    for previous in state.workspace.object_revisions.values():
        if previous.request_id == request_id:
            if (previous.reference.event_id == state.active_event_id
                    and previous.reference.object_kind == KIND
                    and previous.reference.object_id == object_id
                    and previous.actor_id == actor
                    and json.loads(previous.content_json) == json.loads(json.dumps(content))):
                return previous
            raise ValueError('请求编号已用于不同的核查记录。')
    if expected_version != current_version:
        raise ValueError('核查记录已更新，请重新载入后核对新版本。')
    revision = ObjectRevision(
        ObjectReference(state.active_event_id, KIND, object_id, current_version + 1),
        RecordTimes(recorded_at=datetime.now(timezone.utc).isoformat()),
        'review-owner-meeting', 'reported', actor, request_id,
        json.dumps(content, ensure_ascii=False),
    )
    candidate = copy.deepcopy(state.workspace)
    candidate._record_object_revision(revision, expected_version=current_version)
    from .persistence import save_workspace
    if state.db_path:
        save_workspace(candidate, state.db_path)
    event = state.registry.events[state.active_event_id]
    snapshot = event.snapshot_path
    if snapshot and (not state.db_path or str(snapshot) != str(state.db_path)):
        save_workspace(candidate, snapshot)
    state.workspace = candidate
    return revision
