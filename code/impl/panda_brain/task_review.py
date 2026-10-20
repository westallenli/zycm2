"""Audited, reported task classification; never professional approval or release."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from .authorization import Action, Role
from .event_authorization import event_grants
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes

KIND = 'task_review_assessment'
CATEGORIES = {
    'undetermined': '待判定',
    'routine': '常规任务（待规则核对）',
    'professional': '需专业意见',
    'major': '重大会商',
}
SOURCE = 'named-deputy-classification-report'
_CONTENT_KEYS = {'decision_id', 'draft_version', 'task_id', 'category',
                 'reason', 'basis_ref'}


def _records(workspace):
    if workspace.verify_audit_chain():
        raise ValueError('当前事件审计链待核对，不能使用任务分类记录。')
    records = [record for record in workspace.object_revisions.values()
               if record.reference.object_kind == KIND]
    audited = [event.payload for event in workspace.events
               if event.kind == 'ObjectRevisionRecorded'
               and event.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND]
    if len(records) != len(audited):
        raise ValueError('任务分类记录与审计事件不一致。')
    deputy_ids = {grant.subject_id for grant in event_grants(workspace)
                  if grant.role == Role.DEPUTY and grant.event_id == workspace.event_id}
    for record in records:
        ref = record.reference
        content = json.loads(record.content_json)
        if (ref.event_id != workspace.event_id or record.actor_id not in deputy_ids
                or record.source != SOURCE or record.data_kind != 'reported'
                or set(content) != _CONTENT_KEYS
                or content['task_id'] != ref.object_id
                or not isinstance(content['category'], str)
                or content['category'] not in CATEGORIES
                or type(content['draft_version']) is not int
                or content['draft_version'] < 1
                or not isinstance(content['decision_id'], str)
                or not content['decision_id'].strip()
                or not all(isinstance(content[key], str) and content[key].strip()
                           for key in ('reason', 'basis_ref'))
                or audited.count({'revision_id': record.id, 'revision': asdict(record)}) != 1):
            raise ValueError('任务分类记录的归属、来源或审计依据不一致。')
    return records


def assessment_for(workspace, bundle, task_id):
    """Return the latest report and whether it still applies to this draft version."""
    if not any(task.id == task_id for task in bundle.tasks):
        raise ValueError('任务不属于本草案。')
    verified = workspace.task_drafts_for_decision(bundle.decision_id, event_id=bundle.event_id)
    if verified != bundle:
        raise ValueError('草案版本已变化，请重新核对。')
    matches = sorted((record for record in _records(workspace)
                      if record.reference.object_id == task_id),
                     key=lambda record: record.reference.version)
    if any(json.loads(record.content_json)['decision_id'] != bundle.decision_id
           for record in matches):
        raise ValueError('任务分类记录与采纳决断不一致。')
    if any(record.reference.version != index for index, record in enumerate(matches, 1)):
        raise ValueError('任务分类修订版本不连续。')
    latest = matches[-1] if matches else None
    return latest, bool(latest and json.loads(latest.content_json)['draft_version'] == bundle.version)


def record_assessment(workspace, authorization, token, *, event_id: str,
                      decision_id: str, task_id: str, expected_draft_version: int,
                      expected_review_version: int, category: str, reason: str,
                      basis_ref: str, request_id: str):
    actor = authorization.require(token, Action.ASSESS_TASK_REVIEW, event_id=event_id)
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError('任务分类不在允许范围内。')
    if any(not isinstance(value, str) or not value.strip() or value != value.strip()
           for value in (reason, basis_ref, request_id)):
        raise ValueError('分类理由、依据引用和请求号必须明确填写。')
    bundle = workspace.task_drafts_for_decision(decision_id, event_id=event_id)
    if bundle is None or bundle.status != 'draft':
        raise ValueError('当前事件没有可登记的方案任务草案。')
    if type(expected_draft_version) is not int or expected_draft_version != bundle.version:
        raise ValueError('草案版本已变化，请刷新后重试。')
    previous, _ = assessment_for(workspace, bundle, task_id)
    content = json.dumps({
        'decision_id': decision_id, 'draft_version': bundle.version,
        'task_id': task_id, 'category': category, 'reason': reason,
        'basis_ref': basis_ref,
    }, ensure_ascii=False, sort_keys=True)
    for record in workspace.object_revisions.values():
        if record.request_id == request_id:
            if (record.reference.object_kind == KIND and record.reference.event_id == event_id
                    and record.reference.object_id == task_id and record.actor_id == actor
                    and record.content_json == content):
                return record
            raise ValueError('请求号已用于不同内容。')
    current_version = previous.reference.version if previous else 0
    if type(expected_review_version) is not int or expected_review_version != current_version:
        raise ValueError('分类记录已变化，请刷新后重试。')
    record = ObjectRevision(
        reference=ObjectReference(event_id, KIND, task_id, current_version + 1),
        times=RecordTimes(recorded_at=datetime.now(timezone.utc).isoformat()),
        source=SOURCE, data_kind='reported', actor_id=actor,
        request_id=request_id, content_json=content)
    return workspace._record_object_revision(record, expected_version=current_version)
