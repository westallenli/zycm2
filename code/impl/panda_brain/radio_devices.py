"""Event-scoped simulated radio holder reports and as-known-at attribution.

These entries are reported simulation evidence. They do not establish a
trusted equipment registry or unlock automatic task feedback and location.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .radio_reports import RadioCall, calls_for_event
from .record_contracts import explicit_time


REPORTED = 'SimulatedRadioDeviceHolderReported'
_CHANGES = {'initial', 'handover', 'correction'}


def _required(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f'{label}须明确填写。')


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DeviceHolderReport:
    event_id: str
    device_id: str
    version: int
    holder_id: str
    group_id: str
    call_sign: str
    effective_at: str
    recorded_at: str
    source_ref: str
    reported_by: str
    request_id: str
    change_kind: str
    corrects_version: int
    reason: str
    data_kind: str = 'simulated'
    trust_status: str = 'reported_unverified'

    def __post_init__(self):
        for name, label in (
                ('event_id', '事件号'), ('device_id', '设备号'),
                ('holder_id', '携带人'), ('group_id', '所属队伍'),
                ('call_sign', '呼号'), ('source_ref', '来源依据'),
                ('reported_by', '模拟登记人'),
                ('request_id', '请求号'), ('reason', '登记理由')):
            _required(getattr(self, name), label)
        if (type(self.version) is not int or self.version < 1
                or type(self.corrects_version) is not int or self.corrects_version < 0
                or self.change_kind not in _CHANGES):
            raise ValueError('设备记录版本或变更类型不正确。')
        effective = explicit_time(self.effective_at)
        recorded = explicit_time(self.recorded_at)
        if effective > recorded:
            raise ValueError('设备携带生效时间不能晚于系统记录时间。')
        if self.data_kind != 'simulated' or self.trust_status != 'reported_unverified':
            raise ValueError('模拟设备记录不能冒充真实或已核实来源。')


@dataclass(frozen=True)
class DeviceAttribution:
    report: DeviceHolderReport | None
    issues: tuple[str, ...]
    status: str = 'reported_unverified'


def _same_assignment(a: DeviceHolderReport, b: DeviceHolderReport) -> bool:
    return ((a.holder_id, a.group_id, a.call_sign)
            == (b.holder_id, b.group_id, b.call_sign))


def _validate_next(history: list[DeviceHolderReport], report: DeviceHolderReport) -> None:
    if report.version != len(history) + 1:
        raise ValueError('设备携带记录版本已变化。')
    if not history:
        if report.change_kind != 'initial' or report.corrects_version:
            raise ValueError('首次设备携带登记必须从初始版本开始。')
        return
    if report.change_kind == 'handover':
        latest_time = max(explicit_time(item.effective_at) for item in history)
        latest = max((item for item in history
                      if explicit_time(item.effective_at) == latest_time),
                     key=lambda item: item.version)
        if (report.corrects_version
                or explicit_time(report.effective_at) <= latest_time
                or _same_assignment(report, latest)):
            raise ValueError('设备交接须有更新的生效时间和实际归属变化。')
        return
    if report.change_kind == 'correction':
        target = next((item for item in history
                       if item.version == report.corrects_version), None)
        if target is None or explicit_time(report.effective_at) != explicit_time(target.effective_at):
            raise ValueError('更正须引用同一生效时点的已有版本。')
        latest_for_slot = max((item for item in history
                               if explicit_time(item.effective_at)
                               == explicit_time(target.effective_at)),
                              key=lambda item: item.version)
        if latest_for_slot.version != target.version or _same_assignment(report, target):
            raise ValueError('更正依据已过期或未改变归属。')
        return
    raise ValueError('已有设备记录只能交接或更正。')


def holder_reports(workspace, event_id: str) -> tuple[DeviceHolderReport, ...]:
    """Replay all simulated reports, rejecting gaps and stale corrections."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('设备携带记录与本事件审计链不一致。')
    result = []
    by_device: dict[str, list[DeviceHolderReport]] = {}
    request_ids = set()
    for event in workspace.events:
        if event.kind != REPORTED:
            continue
        raw = event.payload.get('report')
        if not isinstance(raw, dict) or event.payload.get('event_id') != event_id:
            raise ValueError('设备携带审计记录格式或事件归属不正确。')
        try:
            report = DeviceHolderReport(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('设备携带审计记录格式不正确。') from exc
        if (asdict(report) != raw or report.event_id != event_id
                or report.request_id in request_ids):
            raise ValueError('设备携带审计记录重复或跨事件。')
        history = by_device.setdefault(report.device_id, [])
        _validate_next(history, report)
        history.append(report)
        request_ids.add(report.request_id)
        result.append(report)
    return tuple(result)


def _source_fields(report: DeviceHolderReport) -> tuple:
    return (
        report.event_id, report.device_id, report.holder_id,
        report.group_id, report.call_sign, report.effective_at,
        report.source_ref, report.reported_by, report.request_id, report.change_kind,
        report.corrects_version, report.reason,
    )


def record_simulated_holder(workspace, *, event_id: str, device_id: str,
                            holder_id: str, group_id: str, call_sign: str,
                            effective_at: str, source_ref: str, reported_by: str,
                            request_id: str,
                            change_kind: str, expected_version: int,
                            corrects_version: int = 0, reason: str) -> DeviceHolderReport:
    """Append one simulated holder fact; same request is idempotent."""
    reports = holder_reports(workspace, event_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('设备记录预期版本不正确。')
    candidate = DeviceHolderReport(
        event_id=event_id, device_id=device_id,
        version=expected_version + 1, holder_id=holder_id,
        group_id=group_id, call_sign=call_sign,
        effective_at=effective_at, recorded_at=_recorded_at(),
        source_ref=source_ref, reported_by=reported_by, request_id=request_id,
        change_kind=change_kind, corrects_version=corrects_version,
        reason=reason)
    for prior in reports:
        if prior.request_id == request_id:
            if _source_fields(prior) == _source_fields(candidate) and prior.version == candidate.version:
                return prior
            raise ValueError('请求号已用于不同设备携带内容。')
    history = [report for report in reports if report.device_id == device_id]
    _validate_next(history, candidate)
    workspace.append_event(REPORTED, event_id=event_id, report=asdict(candidate))
    return candidate


def holder_at(workspace, event_id: str, device_id: str, occurred_at: str,
              *, known_at: str | None = None) -> DeviceHolderReport | None:
    """Return the simulated holder candidate effective then and known by cutoff."""
    _required(device_id, '设备号')
    moment = explicit_time(occurred_at)
    cutoff = explicit_time(known_at) if known_at is not None else None
    matching = [report for report in holder_reports(workspace, event_id)
                if report.device_id == device_id
                and explicit_time(report.effective_at) <= moment
                and (cutoff is None or explicit_time(report.recorded_at) <= cutoff)]
    return max(matching,
               key=lambda report: (explicit_time(report.effective_at), report.version),
               default=None)


def attribution_for_call(workspace, call: RadioCall,
                         *, known_at: str | None = None) -> DeviceAttribution:
    """Expose candidate and contradictions without modifying the radio call."""
    if not isinstance(call, RadioCall) or call not in calls_for_event(workspace, call.event_id):
        raise ValueError('通话不属于当前事件的已审计记录。')
    if not call.device_id:
        return DeviceAttribution(None, ('通话缺设备号，不能核对携带人。',))
    report = holder_at(workspace, call.event_id, call.device_id,
                       call.times.occurred_at, known_at=known_at)
    if report is None:
        return DeviceAttribution(None, ('通话发生时没有可用的模拟设备携带记录。',))
    issues = []
    if not call.call_sign:
        issues.append('通话缺呼号线索，需人工核对。')
    elif call.call_sign != report.call_sign:
        issues.append('通话自报呼号与设备记录不一致。')
    if not call.reported_group_id:
        issues.append('通话缺队伍线索，需人工核对。')
    elif call.reported_group_id != report.group_id:
        issues.append('通话自报队伍与设备记录不一致。')
    if not call.reported_holder_id:
        issues.append('通话缺设备携带人线索，需人工核对。')
    elif call.reported_holder_id != report.holder_id:
        issues.append('通话自报携带人与设备记录不一致。')
    if not call.reported_speaker_id:
        issues.append('通话缺发话人线索，需人工核对。')
    elif call.reported_speaker_id != report.holder_id:
        issues.append('自报发话人与设备携带人不同，需人工核对。')
    return DeviceAttribution(report, tuple(issues))
