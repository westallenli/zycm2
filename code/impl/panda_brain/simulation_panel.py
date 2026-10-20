"""Explicit anonymous simulation host. Never attach to an existing business root."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth import GeneralEntryAuth
from .fire_events import EventRegistry
from .ingestion import ObservationStore
from .intake_bridge import IntakeBridge
from .persistence import load_workspace, save_workspace
from .scenario_driver import ScenarioDriver, ThresholdRule
from .scenario_runtime import ScenarioRuntime
from .workspace import ScenarioWorkspace


EVENT_ID = 'anonymous-simulation-001'
POLICY = 'anonymous-p10-simulation-v1'


class SimulationConflict(ValueError):
    pass


class ChannelScenarioDriver(ScenarioDriver):
    """Opt-in, prerecorded anonymous camera/weather reports, never recognition."""
    def step(self, **kwargs):
        result = super().step(**kwargs)
        from .source_channels import receive_channel
        from .intake_bridge import IntakeBridge
        states = ('connected', 'delayed', 'disconnected', 'connected', 'unknown', 'connected')
        for observation in self.store.observations():
            if observation.source != 'anonymous-simulator' or not re.fullmatch(r'wind-[0-5]', observation.source_record_id):
                continue
            index = int(observation.source_record_id[-1])
            for kind in ('camera', 'weather'):
                record = dict(event_id=self.workspace.event_id, channel_id=f'anonymous-{kind}-01',
                    channel_kind=kind, source_record_id=f'{kind}-{observation.source_record_id}',
                    source_version=index+1, state=states[index] if kind == 'camera' else 'connected',
                    collected_at=observation.collected_at, received_at=observation.received_at,
                    quality_note='匿名预制报文，未接设备，精度未知', coordinate_system='unknown',
                    media_kind='image' if kind == 'camera' else '',
                    media_ref=f'anonymous-frame-{index}' if kind == 'camera' else '',
                    summary='匿名预制来源状态；截图仅引用，未取得文件、未作真实图像识别',
                    targets=[], evidence_refs=[IntakeBridge.evidence_id_for(observation)])
                receive_channel(self.workspace, record, source='anonymous-channel-simulator', data_kind='simulated')
        return result


class SimulationHost:
    def __init__(self, state, batches, snapshot, *, clock=None, source_channels=False):
        self.state, self.batches, self.snapshot = state, batches, Path(snapshot)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = state._simulation_lock
        self.error = ''
        self.source_channels = source_channels
        store = ObservationStore()
        load_workspace(self.snapshot, observation_store=store)
        self.runtime = self._runtime(state.workspace, store)

    def _runtime(self, workspace, store):
        rules = (ThresholdRule('weather:anonymous-wind', 'wind_speed_m_s', None,
                               '>=', 8, '偏大（模拟待核）', '未达演示阈值（模拟待核）'),)
        driver_type = ChannelScenarioDriver if self.source_channels else ScenarioDriver
        driver = driver_type(workspace, store, IntakeBridge(store), rules)
        return ScenarioRuntime(driver, self.batches, scenario_id=POLICY+('-channels-v1' if self.source_channels else ''),
                               source='anonymous-simulator', now=self.clock())

    def _bound(self):
        if (self.state.active_event_id != EVENT_ID or self.state.workspace.event_id != EVENT_ID
                or self.state.db_path != self.snapshot):
            return False
        record = self.state.registry.events.get(EVENT_ID)
        if record is None or Path(record.snapshot_path).resolve() != self.snapshot.resolve():
            return False
        if self.runtime.driver.workspace is not self.state.workspace:
            store = ObservationStore()
            load_workspace(self.snapshot, observation_store=store)
            self.runtime = self._runtime(self.state.workspace, store)
        return True

    def view(self):
        with self.lock:
            if not self._bound():
                return None
            status = self.runtime.status()
            status.update(revision=self.state.workspace.version, host_error=self.error,
                observations=[dict(metric=r.metric, value=r.value, unit=r.unit,
                    collected_at=r.collected_at, received_at=r.received_at,
                    data_kind=r.data_kind, verification_status=r.verification_status)
                    for r in self.runtime.driver.store.observations()[-4:]])
            return status

    def _commit(self, candidate):
        save_workspace(candidate.driver.workspace, self.snapshot,
                       observation_store=candidate.driver.store)
        self.runtime = candidate
        self.state.workspace = candidate.driver.workspace

    def tick(self):
        with self.lock:
            if self.error or not self._bound():
                return
            try:
                candidate = copy.deepcopy(self.runtime)
                before = candidate.driver.workspace.version
                candidate.tick(now=self.clock())
                if candidate.driver.workspace.version != before:
                    self._commit(candidate)
            except Exception:
                # Freeze retries after a failed write/clock/audit gate. Never
                # show an in-memory advancement as successfully persisted.
                self.error = '模拟宿主已停止推进：保存或调度校验失败，请核对后重启。'

    def control(self, fields, *, session_token):
        with self.lock:
            session = self.state.model_session(session_token)
            import secrets
            if not secrets.compare_digest(fields.get('csrf', ''), session.csrf):
                raise PermissionError('模拟控制表单已失效。')
            if not self._bound() or fields.get('event_id') != EVENT_ID:
                raise PermissionError('模拟控制与当前事件不匹配。')
            allowed = {'csrf', 'event_id', 'action', 'expected_version', 'request_id', 'minutes'}
            if set(fields) - allowed or not re.fullmatch(r'[a-zA-Z0-9-]{8,80}', fields.get('request_id', '')):
                raise ValueError('模拟控制请求格式无效。')
            action = fields.get('action', '')
            if action not in {'play', 'pause', 'single_step', 'refresh_now', 'set_refresh_minutes'}:
                raise ValueError('不支持的模拟动作。')
            if self.error:
                raise SimulationConflict(self.error)
            actor = hashlib.sha256(session_token.encode()).hexdigest()[:16]
            identity = dict(event_id=EVENT_ID, request_id=fields['request_id'], actor=actor,
                            action=action, minutes=fields.get('minutes', ''))
            for event in self.state.workspace.events:
                if event.kind == 'SimulationControlApplied' and event.payload.get('request_id') == fields['request_id']:
                    if event.payload != identity:
                        raise SimulationConflict('同请求编号的内容或操作者不同。')
                    return self.view()
            if fields.get('expected_version') != str(self.state.workspace.version):
                raise SimulationConflict('态势已变化，请查看最新状态后重新操作。')
            candidate = copy.deepcopy(self.runtime)
            now = self.clock()
            if action == 'set_refresh_minutes':
                if not re.fullmatch(r'\d{1,2}', fields.get('minutes', '')):
                    raise ValueError('刷新周期无效。')
                candidate.set_refresh_minutes(int(fields['minutes']), now=now)
            else:
                getattr(candidate, action)(now=now)
            candidate.driver.workspace.append_event('SimulationControlApplied', **identity)
            try:
                self._commit(candidate)
            except (OSError, sqlite3.Error):
                raise SimulationConflict('保存失败，模拟进度未提交。') from None
            return self.view()


    def receive_channel_packet(self, fields, *, session_token):
        """Explicit anonymous push. Atomic save, active-event and CSRF gates."""
        with self.lock:
            import secrets
            session = self.state.model_session(session_token)
            if not secrets.compare_digest(fields.get('csrf', ''), session.csrf):
                raise PermissionError('来源报文会话已失效。')
            if not self._bound() or fields.get('event_id') != EVENT_ID:
                raise PermissionError('来源报文未绑定匿名模拟事件。')
            if set(fields) != {'csrf', 'event_id', 'expected_version', 'packet'}:
                raise ValueError('来源报文请求字段无效。')
            if self.error or fields['expected_version'] != str(self.state.workspace.version):
                raise SimulationConflict(self.error or '资料已变化，请重读后重试。')
            from .source_channels import receive_channel
            try:
                packet = json.loads(fields['packet'])
            except (TypeError, ValueError):
                raise ValueError('来源报文不是有效 JSON。') from None
            candidate = copy.deepcopy(self.runtime)
            result = receive_channel(candidate.driver.workspace, packet,
                                     source='anonymous-explicit-push', data_kind='simulated')
            if not result.get('duplicate'):
                try:
                    self._commit(candidate)
                except (OSError, sqlite3.Error):
                    raise SimulationConflict('保存失败，来源报文未提交。') from None
            return dict(event_id=EVENT_ID, revision=self.state.workspace.version, report=result)


def create_simulation_state(data_root, *, secret, clock=None, _isolated_test=False, source_channels=False):
    """Create/resume only our marked, isolated root; never import a business DB."""
    from .panel import PanelState
    from . import workbench as wb
    root = Path(data_root).resolve()
    code = Path(__file__).resolve().parents[1]
    if (root == root.parent or root == code or root == code.parent
            or root.is_relative_to(code / 'data') or root.is_relative_to(code / 'snapshots')
            or ('.p00-runs' in root.parts and not _isolated_test) or '熊猫智脑cola v4' in root.parts):
        raise ValueError('请指定新的独立模拟数据根，不能使用旧数据、快照或测试运行目录。')
    for name in ('simulation.json', 'registry.json', 'simulation.db', 'snapshots', 'uploads',
                 'materials.json', 'reviews.json', 'imagery.json', 'alarms.json',
                 'imports.json', 'inquiries.json', 'model-connections.json'):
        if not (root / name).resolve().is_relative_to(root):
            raise ValueError('模拟路径越出数据根。')
    manifest = root / 'simulation.json'
    auth = GeneralEntryAuth(secret=secret)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.utcoffset() is None:
        raise ValueError('模拟宿主时钟必须带时区。')
    if root.exists() and any(root.iterdir()):
        if not manifest.is_file():
            raise ValueError('数据根非空且无模拟标识，拒绝覆盖。')
        meta = json.loads(manifest.read_text(encoding='utf-8'))
        if meta.get('policy') != POLICY or meta.get('event_id') != EVENT_ID:
            raise ValueError('模拟数据根标识不匹配。')
        if source_channels and not meta.get('source_channels', False):
            raise ValueError('已有模拟队列未启用来源剧情，请使用新独立数据根。')
        if type(meta.get('source_channels', False)) is not bool:
            raise ValueError('模拟来源剧情标识不合法。')
    else:
        root.mkdir(parents=True, exist_ok=True)
        meta = dict(policy=POLICY, event_id=EVENT_ID, anchor=now.isoformat(), source_channels=source_channels)
    snapshot = root / 'simulation.db'
    if manifest.exists():
        # Validate the explicit anonymous root first, then recover the entire
        # configured bundle before parsing potentially interrupted JSON/SQLite.
        from .durable_storage import recover
        recover(root / '.state-transactions', files=tuple(root / name for name in (
            'registry.json', 'simulation.db', 'materials.json', 'reviews.json',
            'imagery.json', 'alarms.json', 'imports.json')),
            directories=(root / 'snapshots',))
        if not snapshot.is_file() or not (root / 'registry.json').is_file():
            raise ValueError('模拟数据根不完整，拒绝重建覆盖。')
        registry = EventRegistry.load(root / 'registry.json')
        if set(registry.events) != {EVENT_ID}:
            raise ValueError('模拟根含其他事件，拒绝加载。')
        record = registry.events[EVENT_ID]
        if Path(record.snapshot_path).resolve() != snapshot:
            raise ValueError('模拟快照路径不匹配。')
        workspace = load_workspace(snapshot, general_auth=auth)
        if workspace.event_id != EVENT_ID:
            raise ValueError('模拟库事件不匹配。')
    else:
        registry = EventRegistry()
        registry.create_event(event_id=EVENT_ID, name='匿名山火模拟', report_no='ANONYMOUS-SIM',
            discovered_at=meta['anchor'], source='匿名模拟 · 非现场数据', location='模拟林区，位置未核',
            initial_report='仅演示数值观测，不代表现场态势或战术结论。')
        workspace = ScenarioWorkspace('匿名山火模拟', event_id=EVENT_ID, general_auth=auth)
        save_workspace(workspace, snapshot)
        registry.set_snapshot_path(EVENT_ID, str(snapshot))
        registry.save(root / 'registry.json')
        manifest.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    wb.SNAPSHOT_DIR = root / 'snapshots'
    state = PanelState(workspace, db_path=snapshot, auth=auth, registry=registry,
        **{key:root / name for key, name in (
            ('registry_path', 'registry.json'), ('materials_path', 'materials.json'),
            ('reviews_path', 'reviews.json'), ('imagery_path', 'imagery.json'),
            ('alarm_inbox_path', 'alarms.json'), ('imports_path', 'imports.json'))},
        upload_dir=root / 'uploads')
    state.active_event_id = EVENT_ID
    anchor = datetime.fromisoformat(meta['anchor']) - timedelta(minutes=10)
    batches = [[dict(event_id=EVENT_ID, source_record_id=f'wind-{i}', source_version=1,
        site_id='anonymous-forest-01', metric='wind_speed_m_s', unit='m/s', value=value,
        observed_at=(anchor + timedelta(minutes=i)).isoformat(),
        received_at=(anchor + timedelta(minutes=i, seconds=5)).isoformat(),
        quality_note='匿名模拟，无实测精度结论', coordinate_system='unknown')]
        for i, value in enumerate((3.0, 5.0, 8.5, 10.0, 6.0, 4.0))]
    state.simulation_host = SimulationHost(state, batches, snapshot, clock=clock,
                                           source_channels=meta.get('source_channels', False))
    return state
