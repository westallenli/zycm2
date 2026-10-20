"""P10 simulation/refresh coordinator, driven by an explicit host clock.

No threads, sockets, wall-clock guesses, publication or verification actions.
The host serializes access and supplies aware timestamps. Restart restores the
cursor but pauses simulation; every scheduled/active batch uses the same driver.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta

from .ingestion import ObservationStore
from .scenario_driver import ScenarioDriver


REFRESH_MINUTES = (3, 5, 10, 30, 60)
SIMULATION_INTERVAL = timedelta(minutes=1)


def _aware(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('调度时间必须带明确时区。')
    return now


def _period(minutes: int) -> timedelta:
    if type(minutes) is not int or minutes not in REFRESH_MINUTES:
        raise ValueError('刷新周期只支持 3/5/10/30/60 分钟。')
    return timedelta(minutes=minutes)


class ScenarioRuntime:
    """One immutable simulated sequence, one event, independent refresh cadence.

    At most one simulation batch and one refresh per tick. Delayed ticks report
    their delay; they never skip batches or issue a burst of historic actions.
    """

    def __init__(self, driver: ScenarioDriver, batches: list[list[dict]], *,
                 scenario_id: str, source: str, now: datetime,
                 refresh_minutes: int = 3) -> None:
        now = _aware(now)
        period = _period(refresh_minutes)
        if (not isinstance(scenario_id, str) or not scenario_id.strip()
                or scenario_id != scenario_id.strip()
                or any(ord(char) < 32 for char in scenario_id)):
            raise ValueError('模拟场景编号必须是非空文本。')
        if not isinstance(batches, list) or any(not isinstance(batch, list) or not batch for batch in batches):
            raise ValueError('模拟序列必须由非空批次组成；无序列可用空列表。')
        if driver.store is not driver.bridge.store:
            raise ValueError('驱动与接入桥必须使用同一观测仓。')
        self.driver = driver
        self.event_id = driver.workspace.event_id
        self.scenario_id = scenario_id
        self.source = source
        self._batches = copy.deepcopy(batches)
        staging = ObservationStore()
        staging.import_source_json([], event_id=self.event_id, source=source, data_kind='simulated')
        fingerprints = []
        for index, batch in enumerate(self._batches):
            report = staging.import_source_json(batch, event_id=self.event_id,
                                                source=source, data_kind='simulated')
            if report.rejected:
                raise ValueError(f'模拟批次 {index} 未通过统一数据源契约：{report.rejected}')
            fingerprints.append([dict(record=asdict(record),
                original_collected_at=item['observed_at'], declared_received_at=item.get('received_at', ''))
                for record, item in zip(report.accepted, batch)])
        self._fingerprint = hashlib.sha256(json.dumps(dict(event_id=self.event_id,
            source=source, batches=fingerprints), ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        if driver.workspace.verify_audit_chain():
            raise ValueError('审计链异常，不能恢复或推进模拟。')
        self._cursor = 0
        self._playing = False
        self._next_simulation = None
        self._refresh_minutes = refresh_minutes
        self._next_refresh = now + period
        self._last_refresh = None
        self._last_error = ''
        self._last_now = now
        checkpoint = self._latest_checkpoint()
        self._checkpoint_id = checkpoint.id if checkpoint else None
        if checkpoint:
            state = checkpoint.payload
            if state.get('fingerprint') != self._fingerprint:
                raise ValueError('同场景编号的来源或序列已改变，不能复用原推进进度。')
            cursor = state['cursor']
            if type(cursor) is not int or not 0 <= cursor <= len(self._batches):
                raise ValueError('模拟检查点进度无效。')
            previous_now = _aware(datetime.fromisoformat(state['at']))
            if now < previous_now:
                raise ValueError('调度时间不得早于已记录检查点。')
            self._cursor = cursor
            _period(state['refresh_minutes'])
            self._refresh_minutes = state['refresh_minutes']
            self._next_refresh = _aware(datetime.fromisoformat(state['next_refresh_at']))
            self._last_refresh = (_aware(datetime.fromisoformat(state['last_refresh_at']))
                                  if state['last_refresh_at'] else None)
            self._last_error = state.get('last_error', '')
            # Restart is deliberately paused. Resuming schedules a fresh minute.

    def _latest_checkpoint(self):
        return next((event for event in reversed(self.driver.workspace.events)
                     if event.kind == 'ScenarioRuntimeCheckpoint'
                     and event.payload.get('event_id') == self.event_id
                     and event.payload.get('scenario_id') == self.scenario_id), None)

    def _guard(self, now: datetime) -> None:
        _aware(now)
        if now < self._last_now:
            raise ValueError('调度时间倒退，未执行。')
        if (self.driver.workspace.event_id != self.event_id
                or self.driver.store is not self.driver.bridge.store):
            raise ValueError('调度的事件或观测仓绑定已改变。')
        if self.driver.workspace.verify_audit_chain():
            raise ValueError('审计链异常，未执行模拟或刷新。')
        latest = self._latest_checkpoint()
        if (latest.id if latest else None) != self._checkpoint_id:
            raise ValueError('模拟进度已被另一实例更新，请重新加载。')

    def _save(self, now: datetime, action: str, **details) -> None:
        self._last_now = now
        event = self.driver.workspace.append_event('ScenarioRuntimeCheckpoint',
            event_id=self.event_id, scenario_id=self.scenario_id, source=self.source,
            fingerprint=self._fingerprint, cursor=self._cursor, playing=self._playing,
            refresh_minutes=self._refresh_minutes, next_refresh_at=self._next_refresh.isoformat(),
            next_simulation_at=self._next_simulation.isoformat() if self._next_simulation else '',
            last_refresh_at=self._last_refresh.isoformat() if self._last_refresh else '',
            last_error=self._last_error,
            at=now.isoformat(), action=action, **details)
        self._checkpoint_id = event.id

    def status(self) -> dict:
        received = [event.payload['received_at'] for event in self.driver.workspace.events
                    if event.kind == 'ObservationIntakeRecorded'
                    and event.payload.get('event_id') == self.event_id
                    and event.payload.get('received_at')]
        return dict(event_id=self.event_id, scenario_id=self.scenario_id,
            data_kind='simulated', playing=self._playing,
            completed=self._cursor == len(self._batches), cursor=self._cursor,
            batch_count=len(self._batches), refresh_minutes=self._refresh_minutes,
            next_simulation_at=self._next_simulation.isoformat() if self._next_simulation else '',
            next_refresh_at=self._next_refresh.isoformat(),
            last_refresh_at=self._last_refresh.isoformat() if self._last_refresh else '',
            last_error=self._last_error,
            latest_received_at=max(received, key=datetime.fromisoformat) if received else '')

    def play(self, *, now: datetime) -> dict:
        self._guard(now)
        if not self._playing and self._cursor < len(self._batches):
            self._playing = True
            self._next_simulation = now + SIMULATION_INTERVAL
            self._save(now, 'play')
        return self.status()

    def pause(self, *, now: datetime) -> dict:
        self._guard(now)
        if self._playing:
            self._playing = False
            self._next_simulation = None
            self._save(now, 'pause')
        return self.status()

    def set_refresh_minutes(self, minutes: int, *, now: datetime) -> dict:
        period = _period(minutes)
        self._guard(now)
        self._refresh_minutes = minutes
        self._next_refresh = now + period
        self._save(now, 'set_refresh_minutes')
        return self.status()

    def _advance(self, *, delay_seconds: float = 0) -> dict:
        index = self._cursor
        # Preflight the whole simulated batch against current source revisions.
        # Active receive retains per-row acceptance; a queued batch is one step.
        staging = copy.deepcopy(self.driver.store)
        report = staging.import_source_json(self._batches[index], event_id=self.event_id,
                                            source=self.source, data_kind='simulated')
        if report.rejected:
            self._playing = False
            self._next_simulation = None
            self._last_error = '模拟批次存在数据源冲突，整批未写入、进度未消费，请核对后重试。'
            return dict(batch_index=index, validation=report, result=None,
                        delay_seconds=delay_seconds, consumed=False)
        report = self.driver.load_source_sequence(self._batches[index],
                                                  source=self.source, data_kind='simulated')
        result = self.driver.step()
        self._last_error = ''
        self._cursor += 1
        if self._cursor == len(self._batches):
            self._playing = False
            self._next_simulation = None
        return dict(batch_index=index, validation=report, result=result,
                    delay_seconds=delay_seconds, consumed=True)

    def single_step(self, *, now: datetime) -> dict:
        self._guard(now)
        if self._playing:
            raise ValueError('请先暂停模拟，再单步推进。')
        result = None
        if self._cursor < len(self._batches):
            result = self._advance()
            self._save(now, 'single_step', consumed=result['consumed'])
        return dict(simulation=result, status=self.status())

    def refresh_now(self, *, now: datetime) -> dict:
        self._guard(now)
        result = self.driver.step()
        self._last_refresh = now
        self._save(now, 'refresh_now')
        return dict(refresh=result, status=self.status())

    def receive(self, items: list[dict], *, source: str, data_kind: str, now: datetime) -> dict:
        self._guard(now)
        report = self.driver.load_source_sequence(items, source=source, data_kind=data_kind)
        result = self.driver.step() if report.accepted else None
        self._save(now, 'receive', accepted=len(report.accepted), rejected=len(report.rejected))
        return dict(validation=report, result=result, status=self.status())

    def tick(self, *, now: datetime) -> dict:
        self._guard(now)
        simulation = refresh = None
        if self._playing and now >= self._next_simulation:
            simulation = self._advance(delay_seconds=(now - self._next_simulation).total_seconds())
            if self._playing:
                self._next_simulation = now + SIMULATION_INTERVAL
        if now >= self._next_refresh:
            refresh = self.driver.step()
            self._last_refresh = now
            self._next_refresh = now + _period(self._refresh_minutes)
        if simulation is not None or refresh is not None:
            self._save(now, 'tick', simulation_consumed=simulation['consumed'] if simulation else False,
                       refreshed=refresh is not None,
                       delay_seconds=simulation['delay_seconds'] if simulation else 0)
        else:
            self._last_now = now  # A poll has no business/audit write.
        return dict(simulation=simulation, refresh=refresh, status=self.status())
