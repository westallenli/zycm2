"""Audited, unverified task-report interpretations of simulated radio calls."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .offline_tasks import task_state
from .radio_devices import attribution_for_call
from .radio_reports import calls_for_event
from .record_contracts import explicit_time


RECORDED = 'SimulatedRadioTaskCandidateRecorded'
_ACTIONS = {'received', 'progress', 'difficulty', 'support_request', 'completed'}


def _required(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f'{label}须明确填写。')


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RadioTaskCandidate:
    event_id: str
    source_call_id: str
    task_id: str
    segment_id: str
    version: int
    task_version: int
    proposed_action: str
    quoted_text: str
    annotated_by: str
    basis: str
    source_ref: str
    request_id: str
    corrects_version: int
    recorded_at: str
    data_kind: str = 'simulated'
    review_status: str = 'pending_review'

    def __post_init__(self):
        for name, label in (
                ('event_id', '事件号'), ('source_call_id', '来源通话号'),
                ('task_id', '任务号'), ('segment_id', '通话片段号'),
                ('quoted_text', '转写原文片段'), ('annotated_by', '模拟标注人'),
                ('basis', '标注依据'), ('source_ref', '通话原始来源引用'),
                ('request_id', '请求号')):
            _required(getattr(self, name), label)
        if (type(self.version) is not int or self.version < 1
                or type(self.task_version) is not int or self.task_version < 1
                or type(self.corrects_version) is not int or self.corrects_version < 0
                or self.proposed_action not in _ACTIONS):
            raise ValueError('候选版本或报告类型不正确。')
        explicit_time(self.recorded_at)
        if self.data_kind != 'simulated' or self.review_status != 'pending_review':
            raise ValueError('模拟报告候选不能冒充已核实回执。')


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: RadioTaskCandidate
    issues: tuple[str, ...]
    status: str = 'pending_review'


def _key(candidate: RadioTaskCandidate) -> tuple[str, str, str]:
    return candidate.source_call_id, candidate.task_id, candidate.segment_id


def _same_source(a: RadioTaskCandidate, b: RadioTaskCandidate) -> bool:
    fields = ('event_id', 'source_call_id', 'task_id', 'segment_id',
              'task_version', 'proposed_action', 'quoted_text', 'annotated_by',
              'basis', 'source_ref', 'request_id', 'corrects_version')
    return all(getattr(a, field) == getattr(b, field) for field in fields)


def _validate_reference(candidate: RadioTaskCandidate, calls: dict) -> None:
    call = calls.get(candidate.source_call_id)
    if (call is None or call.event_id != candidate.event_id
            or candidate.task_id not in call.task_ids
            or candidate.source_ref != call.source_ref
            or not call.transcript
            or candidate.quoted_text not in call.transcript
            or explicit_time(call.times.occurred_at)
            > explicit_time(candidate.recorded_at)):
        raise ValueError('候选缺少同事件、同任务的原始通话转写依据。')


def _validate_revision(history: list[RadioTaskCandidate], candidate: RadioTaskCandidate) -> None:
    if candidate.version != len(history) + 1:
        raise ValueError('通话报告候选版本已变化。')
    if not history:
        if candidate.corrects_version:
            raise ValueError('首次标注不能引用被更正版本。')
        return
    previous = history[-1]
    if (candidate.corrects_version != previous.version
            or (candidate.proposed_action, candidate.quoted_text,
                candidate.task_version) ==
               (previous.proposed_action, previous.quoted_text,
                previous.task_version)):
        raise ValueError('更正须引用最新版本并改变报告内容或任务版本。')


def task_candidates(workspace, event_id: str) -> tuple[RadioTaskCandidate, ...]:
    """Replay candidate history and fail closed on broken provenance."""
    calls = {call.source_call_id: call for call in calls_for_event(workspace, event_id)}
    result = []
    histories: dict[tuple[str, str, str], list[RadioTaskCandidate]] = {}
    requests = set()
    for event in workspace.events:
        if event.kind != RECORDED:
            continue
        raw = event.payload.get('candidate')
        if not isinstance(raw, dict) or event.payload.get('event_id') != event_id:
            raise ValueError('通话报告候选审计格式或事件归属不正确。')
        try:
            candidate = RadioTaskCandidate(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('通话报告候选审计格式不正确。') from exc
        if (candidate.event_id != event_id or asdict(candidate) != raw
                or candidate.request_id in requests):
            raise ValueError('通话报告候选重复或跨事件。')
        _validate_reference(candidate, calls)
        history = histories.setdefault(_key(candidate), [])
        _validate_revision(history, candidate)
        history.append(candidate)
        requests.add(candidate.request_id)
        result.append(candidate)
    return tuple(result)


def record_task_candidate(workspace, *, event_id: str, source_call_id: str,
                          task_id: str, segment_id: str, task_version: int,
                          proposed_action: str, quoted_text: str,
                          annotated_by: str, basis: str, request_id: str,
                          expected_version: int = 0,
                          corrects_version: int = 0) -> RadioTaskCandidate:
    """Annotate a call segment; never creates an offline-task feedback."""
    existing = task_candidates(workspace, event_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('通话报告候选预期版本不正确。')
    calls = {call.source_call_id: call for call in calls_for_event(workspace, event_id)}
    call = calls.get(source_call_id)
    candidate = RadioTaskCandidate(
        event_id=event_id, source_call_id=source_call_id, task_id=task_id,
        segment_id=segment_id, version=expected_version + 1,
        task_version=task_version, proposed_action=proposed_action,
        quoted_text=quoted_text, annotated_by=annotated_by, basis=basis,
        source_ref=call.source_ref if call else '', request_id=request_id,
        corrects_version=corrects_version, recorded_at=_recorded_at())
    for previous in existing:
        if previous.request_id == request_id:
            if _same_source(previous, candidate) and previous.version == candidate.version:
                return previous
            raise ValueError('请求号已用于不同通话报告候选。')
    _validate_reference(candidate, calls)
    tasks = task_state(workspace, event_id)
    task = tasks.get(task_id)
    if task is None or task.version != task_version:
        raise ValueError('候选须引用当前版的本事件线下任务。')
    history = [item for item in existing if _key(item) == _key(candidate)]
    _validate_revision(history, candidate)
    workspace.append_event(RECORDED, event_id=event_id, candidate=asdict(candidate))
    return candidate


def assess_task_candidate(workspace, candidate: RadioTaskCandidate) -> CandidateAssessment:
    """Show contradictions and missing trust without promoting a business fact."""
    if not isinstance(candidate, RadioTaskCandidate) or candidate not in task_candidates(
            workspace, candidate.event_id):
        raise ValueError('候选不属于当前事件的已审计记录。')
    calls = {call.source_call_id: call for call in calls_for_event(workspace, candidate.event_id)}
    call = calls[candidate.source_call_id]
    task = task_state(workspace, candidate.event_id)[candidate.task_id]
    issues = []
    if candidate.task_version != task.version:
        issues.append('任务内容版本已变化，原候选须重新核对。')
    if task.state in {'paused', 'withdrawn', 'replaced', 'closed'}:
        issues.append('任务当前已暂停或终止，通话候选不能直接用作当前回执。')
    if len(call.task_ids) != 1:
        issues.append('通话关联多个任务，片段归属须人工核对。')
    if not task.group_id or call.reported_group_id != task.group_id:
        issues.append('通话自报队伍与任务执行组不一致或缺失。')
    if not task.recipient_id or call.reported_speaker_id != task.recipient_id:
        issues.append('通话自报发话人与任务接收人不一致或缺失。')
    attribution = attribution_for_call(workspace, call)
    issues.extend(attribution.issues)
    issues.append('设备归属和转写语义仍为模拟报告，尚无可信身份及内容核实。')
    return CandidateAssessment(candidate, tuple(issues))
