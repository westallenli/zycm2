"""P07 simulated radio captures; evidence only, never a task instruction or receipt.

The current system has no trusted radio device feed or holder registry. This
contract preserves source details and explicit task references while keeping
speaker identity and report content pending review. A real adapter must be a
separate, authenticated integration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .offline_tasks import task_state
from .record_contracts import RecordTimes


CAPTURED = 'SimulatedRadioCallCaptured'


def _text(value: str, label: str, *, optional: bool = False) -> None:
    if (not isinstance(value, str) or value != value.strip()
            or (not optional and not value)):
        raise ValueError(f'{label}格式不正确。')


@dataclass(frozen=True)
class RadioCall:
    event_id: str
    source_call_id: str
    source_ref: str
    channel_id: str
    device_id: str
    call_sign: str
    reported_group_id: str
    reported_holder_id: str
    reported_speaker_id: str
    transcript: str
    audio_ref: str
    times: RecordTimes
    task_ids: tuple[str, ...] = ()
    data_kind: str = 'simulated'
    identity_status: str = 'pending_review'
    content_status: str = 'reported_unverified'
    resolved_holder_id: str = ''
    location: str = ''

    def __post_init__(self):
        for name, label in (
                ('event_id', '事件号'), ('source_call_id', '来源通话号'),
                ('source_ref', '原始来源引用'), ('channel_id', '频道')):
            _text(getattr(self, name), label)
        for name, label in (
                ('device_id', '设备号'), ('call_sign', '呼号'),
                ('reported_group_id', '自报队伍'),
                ('reported_holder_id', '自报设备携带人'),
                ('reported_speaker_id', '自报发话人'),
                ('transcript', '转写原文'), ('audio_ref', '音频引用')):
            _text(getattr(self, name), label, optional=True)
        if not self.transcript and not self.audio_ref:
            raise ValueError('模拟通话须有转写原文或音频来源引用。')
        if (not isinstance(self.times, RecordTimes) or not self.times.occurred_at
                or not self.times.recorded_at):
            raise ValueError('通话发生时间与系统记录时间须明确。')
        if (not isinstance(self.task_ids, tuple)
                or len(set(self.task_ids)) != len(self.task_ids)):
            raise ValueError('任务关联须是不重复的明确任务号。')
        for task_id in self.task_ids:
            _text(task_id, '任务号')
        if (self.data_kind != 'simulated'
                or self.identity_status != 'pending_review'
                or self.content_status != 'reported_unverified'
                or self.resolved_holder_id or self.location):
            raise ValueError('模拟通话不能冒充核实身份、定位或真实采集。')


def _call_from_audit(raw) -> RadioCall:
    if (not isinstance(raw, dict) or not isinstance(raw.get('times'), dict)
            or not isinstance(raw.get('task_ids'), (list, tuple))):
        raise ValueError('对讲审计记录格式不正确。')
    try:
        values = dict(raw)
        values['times'] = RecordTimes(**values['times'])
        values['task_ids'] = tuple(values['task_ids'])
        call = RadioCall(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('对讲审计记录格式不正确。') from exc
    normalized = dict(raw)
    normalized['task_ids'] = tuple(raw['task_ids'])
    if asdict(call) != normalized:
        raise ValueError('对讲审计记录字段不一致。')
    return call


def calls_for_event(workspace, event_id: str) -> tuple[RadioCall, ...]:
    """Replay immutable simulated captures from the bound event's audit chain."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('对讲记录与当前事件审计链不一致。')
    calls = []
    seen = set()
    for event in workspace.events:
        if event.kind != CAPTURED:
            continue
        if event.payload.get('event_id') != event_id:
            raise ValueError('对讲审计记录跨事件。')
        call = _call_from_audit(event.payload.get('call'))
        if call.event_id != event_id or call.source_call_id in seen:
            raise ValueError('对讲来源号重复或跨事件。')
        seen.add(call.source_call_id)
        calls.append(call)
    known_tasks = task_state(workspace, event_id)
    if any(task_id not in known_tasks for call in calls for task_id in call.task_ids):
        raise ValueError('对讲记录引用了非本事件任务。')
    return tuple(calls)


def _source_fields(call: RadioCall) -> tuple:
    """Fields controlled by the simulation source, excluding system record time."""
    return (
        call.event_id, call.source_call_id, call.source_ref,
        call.channel_id, call.device_id, call.call_sign,
        call.reported_group_id, call.reported_holder_id,
        call.reported_speaker_id, call.transcript, call.audio_ref,
        call.times.occurred_at, call.times.collected_at,
        call.times.received_at, call.task_ids,
    )


def record_simulated_call(workspace, *, event_id: str, source_call_id: str,
                          source_ref: str, occurred_at: str,
                          channel_id: str, device_id: str = '',
                          call_sign: str = '', reported_group_id: str = '',
                          reported_holder_id: str = '',
                          reported_speaker_id: str = '',
                          transcript: str = '', audio_ref: str = '',
                          collected_at: str = '', received_at: str = '',
                          task_ids: tuple[str, ...] = ()) -> RadioCall:
    """Record one explicitly simulated call; never writes task or map state."""
    existing = calls_for_event(workspace, event_id)
    candidate = RadioCall(
        event_id=event_id, source_call_id=source_call_id,
        source_ref=source_ref, channel_id=channel_id, device_id=device_id,
        call_sign=call_sign, reported_group_id=reported_group_id,
        reported_holder_id=reported_holder_id,
        reported_speaker_id=reported_speaker_id,
        transcript=transcript, audio_ref=audio_ref,
        times=RecordTimes(
            occurred_at=occurred_at, collected_at=collected_at,
            received_at=received_at,
            recorded_at=datetime.now(timezone.utc).isoformat()),
        task_ids=task_ids,
    )
    for prior in existing:
        if prior.source_call_id == source_call_id:
            if _source_fields(prior) == _source_fields(candidate):
                return prior
            raise ValueError('来源通话号已用于不同内容。')
    known_tasks = task_state(workspace, event_id)
    if any(task_id not in known_tasks for task_id in candidate.task_ids):
        raise ValueError('模拟通话只能明确关联本事件已登记的线下任务。')
    workspace.append_event(CAPTURED, event_id=event_id, call=asdict(candidate))
    return candidate
