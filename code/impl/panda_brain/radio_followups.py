"""Read-only human follow-up prompts from audited task and radio evidence."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .authorization import Role
from .event_authorization import event_grants
from .offline_tasks import task_state
from .radio_positions import position_samples, review_device_position
from .radio_task_candidates import task_candidates
from .record_contracts import explicit_time


@dataclass(frozen=True)
class FollowupPrompt:
    subject_kind: str
    subject_id: str
    owner_id: str
    owner_sources: tuple[str, ...]
    coordinator_id: str
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    suggested_question: str
    status: str = 'suggested_unhandled'


@dataclass(frozen=True)
class FollowupBoard:
    prompts: tuple[FollowupPrompt, ...]
    issues: tuple[str, ...]
    status: str = 'read_only'


def _add(grouped: dict, subject_kind: str, subject_id: str, owner_id: str,
         reason_code: str, evidence_ref: str) -> None:
    key = subject_kind, subject_id, owner_id
    reasons, sources = grouped.setdefault(key, (set(), set()))
    reasons.add(reason_code)
    sources.add(evidence_ref)


def followup_board(workspace, event_id: str, *, now: str,
                   position_review_window_seconds: float | None = None) -> FollowupBoard:
    """Merge current review leads; nothing is sent, read, acknowledged, or closed."""
    moment = explicit_time(now)
    if position_review_window_seconds is not None and (
            isinstance(position_review_window_seconds, bool)
            or not isinstance(position_review_window_seconds, (int, float))
            or not math.isfinite(position_review_window_seconds)
            or position_review_window_seconds <= 0):
        raise ValueError('模拟位置核查窗口须为明确的正数秒。')
    tasks = task_state(workspace, event_id)
    deputies = {grant.subject_id for grant in event_grants(workspace)
                if grant.event_id == event_id and grant.role == Role.DEPUTY}
    if len(deputies) != 1:
        return FollowupBoard((), ('本事件没有可核对的唯一指定副总指挥，不能路由催办或追问。',))
    coordinator = next(iter(deputies))
    grouped = {}
    issues = []

    for task in tasks.values():
        if task.state in {'closed', 'withdrawn', 'replaced', 'paused'}:
            continue
        if task.state == 'pending_verification':
            if task.verifier_id:
                _add(grouped, 'task', task.id, task.verifier_id,
                     'completion_needs_verification',
                     f'{task.id}:v{task.version}:completed_report')
            else:
                issues.append(f'{task.id} 已报告完成，但本版无具名核验负责人。')
            continue
        if task.due_at and moment > explicit_time(task.due_at):
            _add(grouped, 'task', task.id, task.recipient_id,
                 'explicit_deadline_passed', f'{task.id}:v{task.version}:due:{task.due_at}')
        if task.state == 'blocked':
            problem = next((feedback for feedback in reversed(task.current_feedback)
                            if feedback.get('action') in {'difficulty', 'anomaly'}), None)
            if problem is not None:
                _add(grouped, 'task', task.id, task.recipient_id,
                     'reported_difficulty_needs_question',
                     f'{task.id}:v{task.version}:feedback:{problem["request_id"]}')

    latest_segments = {}
    for candidate in task_candidates(workspace, event_id):
        key = candidate.source_call_id, candidate.task_id, candidate.segment_id
        latest_segments[key] = candidate
    for candidate in latest_segments.values():
        task = tasks[candidate.task_id]
        if task.state in {'closed', 'withdrawn', 'replaced'}:
            continue
        _add(grouped, 'task', task.id, coordinator,
             'radio_candidate_needs_review',
             f'radio:{candidate.source_call_id}:{candidate.segment_id}:v{candidate.version}:{candidate.source_ref}')

    devices = {sample.device_id for sample in position_samples(workspace, event_id)}
    for device_id in sorted(devices):
        review = review_device_position(
            workspace, event_id, device_id, now=now,
            review_window_seconds=position_review_window_seconds)
        if review.sample is None:
            continue
        if review.freshness == 'over_review_window':
            _add(grouped, 'device', device_id, coordinator,
                 'position_over_review_window',
                 f'position:{review.sample.source_sample_id}:{review.sample.source_ref}')
        if any('不一致' in issue for issue in review.issues):
            _add(grouped, 'device', device_id, coordinator,
                 'position_holder_conflict',
                 f'position:{review.sample.source_sample_id}:{review.sample.source_ref}')
        if any('没有可用的模拟设备携带人记录' in issue for issue in review.issues):
            _add(grouped, 'device', device_id, coordinator,
                 'position_holder_missing',
                 f'position:{review.sample.source_sample_id}:{review.sample.source_ref}')

    wording = {
        'explicit_deadline_passed': '核对明确截止时间后的任务进展',
        'reported_difficulty_needs_question': '向执行负责人追问已报告困难或异常',
        'completion_needs_verification': '核对完成报告并决定是否独立核验',
        'radio_candidate_needs_review': '核对模拟通话的身份、内容与任务归属',
        'position_over_review_window': '核对超过本次模拟窗口的设备位置',
        'position_holder_conflict': '核对位置来源与设备携带记录的矛盾',
        'position_holder_missing': '核对设备位置缺少携带人记录的原因',
    }
    prompts = []
    for (subject_kind, subject_id, owner_id), (reasons, sources) in sorted(grouped.items()):
        codes = tuple(sorted(reasons))
        owner_sources = set()
        if subject_kind == 'device' or 'radio_candidate_needs_review' in reasons:
            owner_sources.add('event_appointment')
        if 'completion_needs_verification' in reasons:
            owner_sources.add('task_recorded_verifier')
        if reasons & {'explicit_deadline_passed', 'reported_difficulty_needs_question'}:
            owner_sources.add('task_recorded_recipient')
        prompts.append(FollowupPrompt(
            subject_kind=subject_kind, subject_id=subject_id,
            owner_id=owner_id, owner_sources=tuple(sorted(owner_sources)),
            coordinator_id=coordinator,
            reason_codes=codes, evidence_refs=tuple(sorted(sources)),
            suggested_question='；'.join(wording[code] for code in codes)))
    return FollowupBoard(tuple(prompts), tuple(issues))
