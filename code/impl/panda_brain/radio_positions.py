"""Event-scoped simulated device positions; never map marks or task facts."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .radio_devices import holder_at
from .record_contracts import explicit_time


CAPTURED = 'SimulatedRadioDevicePositionCaptured'


def _required(value: str, label: str, *, optional: bool = False) -> None:
    if (not isinstance(value, str) or value != value.strip()
            or (not optional and not value)):
        raise ValueError(f'{label}格式不正确。')


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SimulatedPosition:
    event_id: str
    source_sample_id: str
    source_ref: str
    device_id: str
    latitude: float
    longitude: float
    accuracy_m: float
    captured_at: str
    received_at: str
    recorded_at: str
    reported_group_id: str = ''
    reported_holder_id: str = ''
    coordinate_system: str = 'WGS84'
    data_kind: str = 'simulated'
    trust_status: str = 'reported_unverified'

    def __post_init__(self):
        for field, label in (
                ('event_id', '事件号'), ('source_sample_id', '来源样本号'),
                ('source_ref', '原始来源引用'), ('device_id', '设备号')):
            _required(getattr(self, field), label)
        for field, label in (
                ('reported_group_id', '来源自报队伍'),
                ('reported_holder_id', '来源自报携带人')):
            _required(getattr(self, field), label, optional=True)
        for value, label, bound in (
                (self.latitude, '纬度', 90),
                (self.longitude, '经度', 180)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > bound):
                raise ValueError(f'{label}不在有效范围。')
        if (isinstance(self.accuracy_m, bool)
                or not isinstance(self.accuracy_m, (int, float))
                or not math.isfinite(self.accuracy_m)
                or self.accuracy_m <= 0):
            raise ValueError('定位精度须为明确的正数米。')
        captured = explicit_time(self.captured_at)
        recorded = explicit_time(self.recorded_at)
        if captured > recorded:
            raise ValueError('采集时间不能晚于系统记录时间。')
        if self.received_at:
            received = explicit_time(self.received_at)
            if not captured <= received <= recorded:
                raise ValueError('接收时间须位于采集与系统记录之间。')
        if (self.coordinate_system != 'WGS84' or self.data_kind != 'simulated'
                or self.trust_status != 'reported_unverified'):
            raise ValueError('模拟位置不能冒充未知坐标系或已核实定位。')


@dataclass(frozen=True)
class PositionReview:
    sample: SimulatedPosition | None
    age_seconds: float | None
    freshness: str
    issues: tuple[str, ...]
    status: str = 'simulated_unverified'


def _source_fields(sample: SimulatedPosition) -> tuple:
    return (
        sample.event_id, sample.source_sample_id, sample.source_ref,
        sample.device_id, sample.latitude, sample.longitude, sample.accuracy_m,
        sample.captured_at, sample.received_at, sample.reported_group_id,
        sample.reported_holder_id, sample.coordinate_system,
    )


def position_samples(workspace, event_id: str) -> tuple[SimulatedPosition, ...]:
    """Replay this event's immutable simulated location evidence."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('设备位置与当前事件审计链不一致。')
    result = []
    seen = set()
    for event in workspace.events:
        if event.kind != CAPTURED:
            continue
        raw = event.payload.get('position')
        if not isinstance(raw, dict) or event.payload.get('event_id') != event_id:
            raise ValueError('设备位置审计格式或事件归属不正确。')
        try:
            sample = SimulatedPosition(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('设备位置审计格式不正确。') from exc
        if (asdict(sample) != raw or sample.event_id != event_id
                or sample.source_sample_id in seen):
            raise ValueError('设备位置样本重复或跨事件。')
        seen.add(sample.source_sample_id)
        result.append(sample)
    return tuple(result)


def record_simulated_position(workspace, *, event_id: str,
                              source_sample_id: str, source_ref: str,
                              device_id: str, latitude: float, longitude: float,
                              accuracy_m: float, captured_at: str,
                              received_at: str = '', reported_group_id: str = '',
                              reported_holder_id: str = '') -> SimulatedPosition:
    """Append a simulated device coordinate, with source-id idempotency."""
    previous = position_samples(workspace, event_id)
    candidate = SimulatedPosition(
        event_id=event_id, source_sample_id=source_sample_id,
        source_ref=source_ref, device_id=device_id,
        latitude=latitude, longitude=longitude, accuracy_m=accuracy_m,
        captured_at=captured_at, received_at=received_at,
        recorded_at=_recorded_at(), reported_group_id=reported_group_id,
        reported_holder_id=reported_holder_id)
    for sample in previous:
        if sample.source_sample_id == source_sample_id:
            if _source_fields(sample) == _source_fields(candidate):
                return sample
            raise ValueError('来源样本号已用于不同设备位置。')
    workspace.append_event(CAPTURED, event_id=event_id, position=asdict(candidate))
    return candidate


def latest_device_position(workspace, event_id: str, device_id: str,
                           *, as_of: str | None = None,
                           known_at: str | None = None) -> SimulatedPosition | None:
    """Latest captured sample, optionally bounded by event and knowledge time."""
    _required(device_id, '设备号')
    moment = explicit_time(as_of) if as_of is not None else None
    cutoff = explicit_time(known_at) if known_at is not None else None
    samples = [sample for sample in position_samples(workspace, event_id)
               if sample.device_id == device_id
               and (moment is None or explicit_time(sample.captured_at) <= moment)
               and (cutoff is None or explicit_time(sample.recorded_at) <= cutoff)]
    return max(samples, key=lambda sample: (
        explicit_time(sample.captured_at), explicit_time(sample.recorded_at)),
        default=None)


def review_device_position(workspace, event_id: str, device_id: str,
                           *, now: str, review_window_seconds: float | None = None,
                           known_at: str | None = None) -> PositionReview:
    """Read-only age and holder contradictions, never an operational verdict."""
    moment = explicit_time(now)
    if review_window_seconds is not None and (
            isinstance(review_window_seconds, bool)
            or not isinstance(review_window_seconds, (int, float))
            or not math.isfinite(review_window_seconds)
            or review_window_seconds <= 0):
        raise ValueError('模拟核查窗口须为明确的正数秒。')
    sample = latest_device_position(
        workspace, event_id, device_id, as_of=now, known_at=known_at)
    if sample is None:
        return PositionReview(None, None, 'unknown',
                              ('没有该设备在查询时点可用的模拟定位样本。',))
    age = (moment - explicit_time(sample.captured_at)).total_seconds()
    freshness = 'unknown'
    issues = []
    if review_window_seconds is not None:
        freshness = ('over_review_window' if age > review_window_seconds
                     else 'within_review_window')
        if freshness == 'over_review_window':
            issues.append('位置样本超过本次显式模拟核查窗口，需核对。')
    holder = holder_at(workspace, event_id, device_id, sample.captured_at,
                       known_at=known_at)
    if holder is None:
        issues.append('采集时没有可用的模拟设备携带人记录。')
    else:
        if sample.reported_group_id and sample.reported_group_id != holder.group_id:
            issues.append('位置来源自报队伍与设备携带记录不一致。')
        if sample.reported_holder_id and sample.reported_holder_id != holder.holder_id:
            issues.append('位置来源自报携带人与设备携带记录不一致。')
    issues.append('设备位置与归属仅为模拟来源，不能推定任务地点或人员安全状态。')
    return PositionReview(sample, age, freshness, tuple(issues))
