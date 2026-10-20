"""Audited follow-up for instructions already given offline; never dispatches them."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .authorization import Action, Role
from .event_authorization import event_grants
from .record_contracts import explicit_time

CREATED = 'OfflineTaskInstructionRecorded'
AMENDED = 'OfflineTaskInstructionAmended'
FEEDBACK = 'OfflineTaskFeedbackRecorded'
CLOSED = 'OfflineTaskVerifiedClosed'
CONTROL = 'OfflineTaskControlRecorded'
_KINDS = {CREATED, AMENDED, FEEDBACK, CLOSED, CONTROL}
_FEEDBACK = {'received', 'started', 'progress', 'difficulty', 'anomaly',
             'resolved', 'completed'}


def _next_feedback(state, issue, action):
    if action == 'received' and state == 'reported_offline':
        return 'received', False
    if action == 'started' and state == 'received':
        return 'executing', False
    if action == 'progress' and state in {'executing', 'blocked'}:
        return state, issue
    if action in {'difficulty', 'anomaly'} and state == 'executing':
        return 'blocked', True
    if action == 'resolved' and state == 'blocked' and issue:
        return 'executing', False
    if action == 'completed' and state == 'executing' and not issue:
        return 'pending_verification', False
    raise ValueError('任务回执阶段不允许重复或跳跃。')


def _text(value, label, *, optional=False):
    if (not isinstance(value, str) or value != value.strip()
            or (not optional and not value)):
        raise ValueError(f'{label}须明确填写。')
    return value


def _time(value, label):
    try:
        return explicit_time(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label}须填写带时区的时间。') from exc


@dataclass(frozen=True)
class OfflineTask:
    id: str
    event_id: str
    version: int
    content: str
    group_id: str
    issued_by: str
    recipient_id: str
    verifier_id: str
    occurred_at: str
    recorded_at: str
    updated_at: str
    recorder_id: str
    evidence: str
    known_progress: str
    parent_id: str = ''
    state: str = 'reported_offline'
    current_feedback: tuple[dict, ...] = ()
    history: tuple[dict, ...] = ()
    issue_open: bool = False
    close_evidence: str = ''
    paused_from: str = ''
    successor_id: str = ''
    due_at: str = ''


def task_state(workspace, event_id: str) -> dict[str, OfflineTask]:
    """Replay only audited event facts; current-version feedback never carries over."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('线下任务与事件审计链不一致。')
    deputies = {grant.subject_id for grant in event_grants(workspace)
                if grant.role == Role.DEPUTY and grant.event_id == event_id}
    tasks: dict[str, OfflineTask] = {}
    requests = set()
    for event in workspace.events:
        if event.kind not in _KINDS:
            continue
        data = event.payload
        request = _text(data.get('request_id'), '请求号')
        if data.get('event_id') != event_id or request in requests:
            raise ValueError('线下任务事件归属或请求号不一致。')
        if data.get('data_kind') != 'reported':
            raise ValueError('线下任务只能保留为具名报告，不能补造核实来源。')
        requests.add(request)
        task_id = _text(data.get('task_id'), '任务号')
        recorded = _time(data.get('recorded_at'), '录入时间')
        if event.kind == CREATED:
            if task_id in tasks or data.get('actor') not in deputies or data.get('version') != 1:
                raise ValueError('线下任务初始记录不合法。')
            occurred = _time(data.get('occurred_at'), '实际下达时间')
            if occurred > recorded:
                raise ValueError('实际下达时间不能晚于补录时间。')
            parent_id = _text(data.get('parent_id', ''), '父任务号', optional=True)
            if parent_id and (parent_id not in tasks or tasks[parent_id].parent_id
                              or tasks[parent_id].state == 'closed'):
                raise ValueError('关联子任务须引用已登记的总任务。')
            for field, label in (('content', '指令内容'), ('issued_by', '下达人'),
                                 ('recipient_id', '接收人'), ('evidence', '线下依据')):
                _text(data.get(field), label)
            for field, label in (('group_id', '执行组'), ('verifier_id', '核验负责人'),
                                 ('known_progress', '已知执行情况')):
                _text(data.get(field, ''), label, optional=True)
            due_at = data.get('due_at', '')
            _text(due_at, '明确截止时间', optional=True)
            if due_at and _time(due_at, '明确截止时间') < occurred:
                raise ValueError('明确截止时间不能早于实际下达时间。')
            tasks[task_id] = OfflineTask(
                task_id, event_id, 1, data['content'], data['group_id'],
                data['issued_by'], data['recipient_id'], data['verifier_id'],
                data['occurred_at'], data['recorded_at'], data['recorded_at'],
                data['actor'], data['evidence'], data['known_progress'], parent_id,
                history=(dict(data),), due_at=due_at)
            continue
        previous = tasks.get(task_id)
        if previous is None or data.get('version') != previous.version:
            raise ValueError('线下任务版本缺失或已过期。')
        if recorded < _time(previous.updated_at, '上次录入时间'):
            raise ValueError('线下任务记录时间不能倒退。')
        if event.kind == AMENDED:
            if data.get('actor') not in deputies or previous.state in {
                    'closed', 'withdrawn', 'replaced', 'paused'}:
                raise ValueError('仅指定副总指挥可补录任务内容变更。')
            occurred = _time(data.get('occurred_at'), '实际变更时间')
            if (occurred > recorded or occurred < _time(previous.occurred_at, '上版实际时间')
                    or data.get('next_version') != previous.version + 1):
                raise ValueError('线下变更时间或内容版本不合法。')
            for field, label in (('content', '变更内容'), ('recipient_id', '接收人'),
                                 ('reason', '变更依据')):
                _text(data.get(field), label)
            for field, label in (('group_id', '执行组'), ('verifier_id', '核验负责人')):
                _text(data.get(field, ''), label, optional=True)
            due_at = data.get('due_at', '')
            _text(due_at, '明确截止时间', optional=True)
            if due_at and _time(due_at, '明确截止时间') < occurred:
                raise ValueError('明确截止时间不能早于实际变更时间。')
            tasks[task_id] = replace(
                previous, version=previous.version + 1, content=data['content'],
                group_id=data['group_id'], recipient_id=data['recipient_id'],
                verifier_id=data['verifier_id'], occurred_at=data['occurred_at'],
                updated_at=data['recorded_at'], state='reported_offline',
                current_feedback=(), issue_open=False, close_evidence='', paused_from='',
                due_at=due_at,
                history=previous.history + (dict(data),))
            continue
        if event.kind == FEEDBACK:
            action = data.get('action')
            actor = data.get('actor')
            _text(data.get('basis'), '回执依据')
            if action not in _FEEDBACK or actor != previous.recipient_id:
                raise ValueError('线下任务回执类型或接收人不一致。')
            state, issue = _next_feedback(previous.state, previous.issue_open, action)
            tasks[task_id] = replace(
                previous, state=state, issue_open=issue,
                current_feedback=previous.current_feedback + (dict(data),),
                history=previous.history + (dict(data),),
                updated_at=data['recorded_at'])
            continue
        if event.kind == CONTROL:
            action = data.get('action')
            if data.get('actor') not in deputies:
                raise ValueError('仅指定副总指挥可补录任务控制事实。')
            _text(data.get('reason'), '控制依据')
            occurred = _time(data.get('occurred_at'), '实际控制时间')
            if occurred < _time(previous.occurred_at, '任务实际时间') or occurred > recorded:
                raise ValueError('实际控制时间不在任务与补录时间之间。')
            successor_id = data.get('successor_id', '')
            _text(successor_id, '替代任务号', optional=True)
            if action == 'pause' and previous.state in {
                    'reported_offline', 'received', 'executing', 'blocked'}:
                state, paused_from = 'paused', previous.state
            elif action == 'resume' and previous.state == 'paused':
                state, paused_from = previous.paused_from, ''
            elif action == 'withdraw' and previous.state not in {
                    'closed', 'withdrawn', 'replaced'}:
                state, paused_from = 'withdrawn', ''
            elif action == 'replace' and previous.state not in {
                    'closed', 'withdrawn', 'replaced'} and successor_id in tasks and successor_id != task_id:
                state, paused_from = 'replaced', ''
            elif action in {'followup', 'escalate'} and previous.state not in {
                    'closed', 'withdrawn', 'replaced'}:
                state, paused_from = previous.state, previous.paused_from
            else:
                raise ValueError('当前任务状态不能执行该控制操作。')
            if (action == 'replace') != bool(successor_id):
                raise ValueError('仅替代操作能引用已登记的后续任务。')
            tasks[task_id] = replace(
                previous, state=state, paused_from=paused_from,
                successor_id=successor_id, updated_at=data['recorded_at'],
                history=previous.history + (dict(data),))
            continue
        if (data.get('actor') != previous.verifier_id or not previous.verifier_id
                or previous.state != 'pending_verification'):
            raise ValueError('仅本版指定核验负责人可核验关闭待核验任务。')
        _text(data.get('evidence'), '独立核验依据')
        if not previous.parent_id:
            _text(data.get('overall_goal_evidence'), '整体目标核验依据')
            if any(child.parent_id == task_id and child.state != 'closed'
                   for child in tasks.values()):
                raise ValueError('关联子任务尚未逐项核验关闭。')
        elif data.get('overall_goal_evidence'):
            raise ValueError('子任务不能代替总任务核验整体目标。')
        tasks[task_id] = replace(
            previous, state='closed', close_evidence=data['evidence'],
            updated_at=data['recorded_at'], history=previous.history + (dict(data),))
    return tasks


def _existing(workspace, event_id, request_id, kind, expected):
    for event in workspace.events:
        if event.kind in _KINDS and event.payload.get('request_id') == request_id:
            if event.kind == kind and event.payload.get('event_id') == event_id:
                actual = {key: event.payload.get(key) for key in expected}
                if actual == expected:
                    return event.payload
            raise ValueError('请求号已用于不同内容。')
    return None


def _record(workspace, kind, payload):
    result = workspace.append_event(kind, **payload)
    return result.payload


def record_offline(workspace, authorization, token, *, event_id, content,
                   group_id, issued_by, recipient_id, verifier_id,
                   occurred_at, evidence, known_progress='', parent_id='', request_id,
                   due_at=''):
    actor = authorization.require(token, Action.RECORD_OFFLINE_TASK, event_id=event_id)
    for value, label in ((content, '指令内容'), (issued_by, '下达人'),
                         (recipient_id, '接收人'), (evidence, '线下依据'),
                         (request_id, '请求号')):
        _text(value, label)
    for value, label in ((group_id, '执行组'), (verifier_id, '核验负责人'),
                         (known_progress, '已知执行情况'), (parent_id, '父任务号'),
                         (due_at, '明确截止时间')):
        _text(value, label, optional=True)
    occurred = _time(occurred_at, '实际下达时间')
    if due_at and _time(due_at, '明确截止时间') < occurred:
        raise ValueError('明确截止时间不能早于实际下达时间。')
    tasks = task_state(workspace, event_id)
    prior = _existing(workspace, event_id, request_id, CREATED, dict(
        actor=actor, content=content, group_id=group_id, issued_by=issued_by,
        recipient_id=recipient_id, verifier_id=verifier_id,
        occurred_at=occurred_at, evidence=evidence,
        known_progress=known_progress, parent_id=parent_id, due_at=due_at))
    if prior is not None:
        return prior
    if parent_id and (parent_id not in tasks or tasks[parent_id].parent_id
                      or tasks[parent_id].state == 'closed'):
        raise ValueError('关联子任务须引用已登记的总任务。')
    recorded_at = datetime.now(timezone.utc).isoformat()
    if occurred > _time(recorded_at, '补录时间'):
        raise ValueError('实际下达时间不能晚于补录时间。')
    task_id = f'offline-task-{workspace.version + 1}'
    return _record(workspace, CREATED, dict(
        event_id=event_id, task_id=task_id, version=1, actor=actor,
        content=content, group_id=group_id, issued_by=issued_by,
        recipient_id=recipient_id, verifier_id=verifier_id,
        occurred_at=occurred_at, recorded_at=recorded_at,
        evidence=evidence, known_progress=known_progress,
        parent_id=parent_id, due_at=due_at, request_id=request_id,
        data_kind='reported'))


def amend_offline(workspace, authorization, token, *, event_id, task_id,
                  expected_version, content, group_id, recipient_id,
                  verifier_id, occurred_at, reason, request_id, due_at=''):
    actor = authorization.require(token, Action.RECORD_OFFLINE_TASK, event_id=event_id)
    for value, label in ((content, '变更内容'), (recipient_id, '接收人'),
                         (reason, '变更依据'), (request_id, '请求号')):
        _text(value, label)
    for value, label in ((group_id, '执行组'), (verifier_id, '核验负责人'),
                         (due_at, '明确截止时间')):
        _text(value, label, optional=True)
    occurred = _time(occurred_at, '实际变更时间')
    if due_at and _time(due_at, '明确截止时间') < occurred:
        raise ValueError('明确截止时间不能早于实际变更时间。')
    tasks = task_state(workspace, event_id)
    prior = _existing(workspace, event_id, request_id, AMENDED, dict(
        actor=actor, task_id=task_id, version=expected_version,
        content=content, group_id=group_id, recipient_id=recipient_id,
        verifier_id=verifier_id, occurred_at=occurred_at, reason=reason,
        due_at=due_at))
    if prior is not None:
        return prior
    task = tasks.get(task_id)
    if task is None or type(expected_version) is not int or task.version != expected_version:
        raise ValueError('线下任务版本已变化。')
    if task.state in {'closed', 'withdrawn', 'replaced', 'paused'}:
        raise ValueError('终态任务不能修改内容。')
    recorded_at = datetime.now(timezone.utc).isoformat()
    if occurred > _time(recorded_at, '补录时间') or occurred < _time(task.occurred_at, '上版实际时间'):
        raise ValueError('实际变更时间不能晚于补录时间或早于上版实际时间。')
    return _record(workspace, AMENDED, dict(
        event_id=event_id, task_id=task_id, version=expected_version,
        next_version=expected_version + 1, actor=actor,
        content=content, group_id=group_id, recipient_id=recipient_id,
        verifier_id=verifier_id, occurred_at=occurred_at,
        recorded_at=recorded_at, reason=reason, due_at=due_at, request_id=request_id,
        data_kind='reported'))


def overdue_reminders(workspace, event_id: str, now: str) -> tuple[str, ...]:
    """Read-only reminders from explicit deadlines; no inferred refusal or blame."""
    instant = _time(now, '当前时间')
    tasks = task_state(workspace, event_id)
    return tuple(task.id for task in tasks.values()
                 if task.due_at and instant > _time(task.due_at, '明确截止时间')
                 and task.state not in {'closed', 'withdrawn', 'replaced'})


def record_control(workspace, authorization, token, *, event_id, task_id,
                   expected_version, action, occurred_at, reason,
                   successor_id='', request_id):
    actor = authorization.require(token, Action.RECORD_OFFLINE_TASK, event_id=event_id)
    _text(reason, '控制依据')
    _text(request_id, '请求号')
    _text(successor_id, '替代任务号', optional=True)
    occurred = _time(occurred_at, '实际控制时间')
    tasks = task_state(workspace, event_id)
    prior = _existing(workspace, event_id, request_id, CONTROL, dict(
        task_id=task_id, version=expected_version, action=action,
        actor=actor, occurred_at=occurred_at, reason=reason,
        successor_id=successor_id))
    if prior is not None:
        return prior
    task = tasks.get(task_id)
    if task is None or type(expected_version) is not int or expected_version != task.version:
        raise ValueError('任务内容版本已变化。')
    if action == 'pause' and task.state in {
            'reported_offline', 'received', 'executing', 'blocked'}:
        pass
    elif action == 'resume' and task.state == 'paused':
        pass
    elif action == 'withdraw' and task.state not in {'closed', 'withdrawn', 'replaced'}:
        pass
    elif action == 'replace' and task.state not in {'closed', 'withdrawn', 'replaced'} \
            and successor_id in tasks and successor_id != task_id:
        pass
    elif action in {'followup', 'escalate'} and task.state not in {
            'closed', 'withdrawn', 'replaced'}:
        pass
    else:
        raise ValueError('当前任务状态不能执行该控制操作。')
    if (action == 'replace') != bool(successor_id):
        raise ValueError('仅替代操作能引用已登记的后续任务。')
    recorded_at = datetime.now(timezone.utc).isoformat()
    if occurred < _time(task.occurred_at, '任务实际时间') or occurred > _time(recorded_at, '补录时间'):
        raise ValueError('实际控制时间不在任务与补录时间之间。')
    return _record(workspace, CONTROL, dict(
        event_id=event_id, task_id=task_id, version=expected_version,
        action=action, occurred_at=occurred_at, recorded_at=recorded_at,
        reason=reason, successor_id=successor_id, actor=actor,
        request_id=request_id, data_kind='reported'))


def record_feedback(workspace, resolve_subject, token, *, event_id, task_id,
                    expected_version, action, basis, request_id,
                    is_current_leader):
    actor = resolve_subject(token)
    _text(basis, '回执依据')
    _text(request_id, '请求号')
    tasks = task_state(workspace, event_id)
    prior = _existing(workspace, event_id, request_id, FEEDBACK, dict(
        task_id=task_id, version=expected_version, action=action,
        actor=actor, basis=basis))
    if prior is not None:
        return prior
    task = tasks.get(task_id)
    if task is None or type(expected_version) is not int or expected_version != task.version:
        raise ValueError('任务内容版本已变化，请重新接收。')
    if (not task.group_id or actor != task.recipient_id
            or not is_current_leader(task.group_id, actor)):
        raise PermissionError('仅当前版指定执行组的现任队长或组长本人可回执。')
    if action not in _FEEDBACK:
        raise ValueError('未知任务回执类型。')
    payload = dict(event_id=event_id, task_id=task_id,
                   version=expected_version, action=action, actor=actor,
                   basis=basis, recorded_at=datetime.now(timezone.utc).isoformat(),
                   request_id=request_id, data_kind='reported')
    _next_feedback(task.state, task.issue_open, action)
    return _record(workspace, FEEDBACK, payload)


def verify_close(workspace, resolve_subject, token, *, event_id, task_id,
                 expected_version, evidence, overall_goal_evidence,
                 request_id, subject_exists):
    actor = resolve_subject(token)
    _text(evidence, '独立核验依据')
    _text(request_id, '请求号')
    _text(overall_goal_evidence, '整体目标依据', optional=True)
    tasks = task_state(workspace, event_id)
    prior = _existing(workspace, event_id, request_id, CLOSED, dict(
        task_id=task_id, version=expected_version, actor=actor,
        evidence=evidence, overall_goal_evidence=overall_goal_evidence))
    if prior is not None:
        return prior
    task = tasks.get(task_id)
    if task is None or type(expected_version) is not int or expected_version != task.version:
        raise ValueError('任务内容版本已变化。')
    if not task.verifier_id or actor != task.verifier_id or not subject_exists(actor):
        raise PermissionError('仅当前版指定的可信核验负责人本人可关闭任务。')
    if task.state != 'pending_verification':
        raise ValueError('任务尚未由执行方报告完成并进入待核验。')
    if not task.parent_id:
        _text(overall_goal_evidence, '整体目标核验依据')
        if any(child.parent_id == task_id and child.state != 'closed'
               for child in tasks.values()):
            raise ValueError('关联子任务尚未逐项核验关闭。')
    elif overall_goal_evidence:
        raise ValueError('子任务不能替总任务核验整体目标。')
    return _record(workspace, CLOSED, dict(
        event_id=event_id, task_id=task_id, version=expected_version,
        actor=actor, evidence=evidence,
        overall_goal_evidence=overall_goal_evidence,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        request_id=request_id, data_kind='reported'))
