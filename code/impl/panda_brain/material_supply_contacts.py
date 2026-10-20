"""Named manual supply contacts: reported evidence, never delivery or stock."""
from __future__ import annotations

from copy import copy
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math

from .fire_events import ONBOARDABLE_STATUSES
from .materials import MaterialItem, SupplyPoint, ExternalCompany
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time
from .temporary_group_readiness import leader_state

KIND = 'material_supply_contact'
GROUP = 'log-material'
SOURCE = '人工补给联系报告：'
_CONTENT = {'item_id', 'source_kind', 'source_id', 'source_snapshot',
            'source_digest', 'available_quantity', 'promised_delivery_at'}


def _text(value, *, optional=False):
    if (not isinstance(value, str) or value != value.strip()
            or not optional and not value):
        raise ValueError('物资、来源、依据和请求号须明确填写，不带首尾空白。')
    return value


def _quantity(value):
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError('报告可供量须为有限非负数；未知留空，不用零替代未知。')
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError('报告可供量超过支持范围。') from error
    if not math.isfinite(number) or number < 0:
        raise ValueError('报告可供量须为有限非负数；未知留空，不用零替代未知。')
    return number


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _object_id(item_id, source_kind, source_id):
    _text(item_id)
    _text(source_id)
    if source_kind not in ('point', 'company'):
        raise ValueError('联系来源须是已有物资点或外部公司。')
    return 'supply-contact-' + _digest([item_id, source_kind, source_id])


def require_leader(workspace, ledger, *, event_id, actor):
    if leader_state(workspace, event_id, ledger=ledger).get(GROUP) != actor:
        raise PermissionError('仅本事件物资供应组现任具名组长可登记或查看联系记录。')


def _validate_snapshot(snapshot, item_id, source_kind, source_id):
    if not isinstance(snapshot, dict) or set(snapshot) != {'item', 'source'}:
        raise ValueError('联系资料快照结构不正确。')
    item, source = snapshot['item'], snapshot['source']
    source_class = SupplyPoint if source_kind == 'point' else ExternalCompany
    for value, cls, numeric in ((item, MaterialItem, {'default_carry_qty'}),
                                (source, source_class, {'supplies'})):
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('联系资料字段与档案不一致。')
        if any(not isinstance(v, str) for key, v in value.items() if key not in numeric):
            raise ValueError('联系资料文字字段不正确。')
        _text(value['name'])
    if (item['item_id'] != item_id or
            source['point_id' if source_kind == 'point' else 'company_id'] != source_id):
        raise ValueError('联系资料归属不一致。')
    if _quantity(item['default_carry_qty']) is None:
        raise ValueError('物资默认携行量不正确。')
    if source_kind == 'point':
        stock = source['supplies']
        if not isinstance(stock, dict) or item_id not in stock:
            raise ValueError('所选物资点未登记该物资。')
        for key, value in stock.items():
            _text(key)
            _quantity(value)
        if source['source'] not in {'local', 'external', 'pending'}:
            raise ValueError('物资点来源标记不正确。')
    _json(snapshot)  # Reject nonfinite values, including unrelated stock entries.


def _current_snapshot(ledger, item_id, source_kind, source_id):
    item = ledger.items.get(item_id)
    source = (ledger.points if source_kind == 'point' else ledger.companies).get(source_id)
    cls = SupplyPoint if source_kind == 'point' else ExternalCompany
    if not isinstance(item, MaterialItem) or not isinstance(source, cls):
        raise ValueError('物资或联系来源已不存在，请核对当前资料。')
    snapshot = {'item': asdict(item), 'source': asdict(source)}
    _validate_snapshot(snapshot, item_id, source_kind, source_id)
    return snapshot


def _records(workspace, ledger, event_id):
    """Check mirrors and authority at each report's audit position, not today."""
    if workspace.event_id != event_id or workspace.verify_audit_chain():
        raise ValueError('联系记录与事件或审计链不一致。')
    records = {key: value for key, value in workspace.object_revisions.items()
               if value.reference.object_kind == KIND}
    audited = [(index, event) for index, event in enumerate(workspace.events)
               if event.kind == 'ObjectRevisionRecorded'
               and event.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND]
    if len(records) != len(audited):
        raise ValueError('联系记录与审计镜像数量不一致。')
    versions, clocks, requests = {}, {}, set()
    checked = []
    for index, event in audited:
        record = records.get(event.payload.get('revision_id'))
        if record is None or event.payload != {'revision_id': record.id, 'revision': asdict(record)}:
            raise ValueError('联系记录与审计镜像不一致。')
        ref = record.reference
        content = json.loads(record.content_json)
        if (set(content) != _CONTENT or record.data_kind != 'reported'
                or not record.source.startswith(SOURCE) or record.request_id in requests
                or ref.event_id != event_id or workspace.object_revisions.get(record.id) != record):
            raise ValueError('联系记录归属、来源或请求不一致。')
        _text(record.source[len(SOURCE):])
        object_id = _object_id(content['item_id'], content['source_kind'], content['source_id'])
        if ref.object_id != object_id or ref.version != versions.get(object_id, 0) + 1:
            raise ValueError('联系记录对象或版本不连续。')
        _validate_snapshot(content['source_snapshot'], content['item_id'],
                           content['source_kind'], content['source_id'])
        if content['source_digest'] != _digest(content['source_snapshot']):
            raise ValueError('联系资料冻结指纹不一致。')
        _quantity(content['available_quantity'])
        delivery = _text(content['promised_delivery_at'], optional=True)
        if delivery:
            explicit_time(delivery)
        recorded = explicit_time(record.times.recorded_at)
        if (record.times.collected_at or record.times.received_at
                or recorded > explicit_time(event.recorded_at)
                or object_id in clocks and recorded < clocks[object_id]
                or record.times.occurred_at and explicit_time(record.times.occurred_at) > recorded):
            raise ValueError('联系报告时间或来源时间不一致。')
        prefix = copy(workspace)
        prefix._events = list(workspace.events[:index])
        prior_ids = {e.payload.get('revision_id') for e in prefix.events
                     if e.kind == 'ObjectRevisionRecorded'}
        prefix.object_revisions = {key: r for key, r in workspace.object_revisions.items()
                                   if key in prior_ids}
        if leader_state(prefix, event_id, ledger=ledger).get(GROUP) != record.actor_id:
            raise ValueError('联系报告时没有相应具名组长职责。')
        versions[object_id] = ref.version
        clocks[object_id] = recorded
        requests.add(record.request_id)
        checked.append(record)
    return tuple(checked)


@dataclass(frozen=True)
class SupplyContactReview:
    event_id: str
    item_id: str
    source_kind: str
    source_id: str
    object_id: str
    version: int
    records: tuple[ObjectRevision, ...]
    current_source_json: str | None
    current_source_digest: str | None
    changed_source_refs: tuple[str, ...]
    blockers: tuple[str, ...]
    conclusion: str = '人工联系报告；供应落实及送达仍待核实，未发布、未占用或增加库存'


def review(workspace, ledger, *, event_id, item_id, source_kind, source_id):
    object_id = _object_id(item_id, source_kind, source_id)
    records = tuple(r for r in _records(workspace, ledger, event_id)
                    if r.reference.object_id == object_id)
    blockers = ['联系报告不是独立核验、正式需求或任务发布依据',
                '登记及报告数量不等于当前可用库存', '送达承诺不等于实际到货']
    try:
        current = _current_snapshot(ledger, item_id, source_kind, source_id)
        current_json, digest = _json(current), _digest(current)
    except ValueError:
        current_json = digest = None
        blockers.append('当前物资或来源资料缺失或无效，不能新增联系报告')
    changed = tuple(r.id for r in records
                    if json.loads(r.content_json)['source_digest'] != digest)
    if changed:
        blockers.append('部分联系记录的冻结来源与当前资料不同，须重新核对')
    if not records or json.loads(records[-1].content_json)['available_quantity'] is None:
        blockers.append('可供数量未知')
    elif json.loads(records[-1].content_json)['available_quantity'] == 0:
        blockers.append('最新联系报告明确无可供量')
    if not records or not json.loads(records[-1].content_json)['promised_delivery_at']:
        blockers.append('送达时间未知')
    return SupplyContactReview(event_id, item_id, source_kind, source_id, object_id,
                              len(records), records, current_json, digest, changed, tuple(blockers))


def record(workspace, ledger, registry, *, token, resolve_subject, event_id,
           item_id, source_kind, source_id, expected_source_digest,
           available_quantity, promised_delivery_at, evidence, request_id,
           expected_version, occurred_at=''):
    actor = resolve_subject(token)
    require_leader(workspace, ledger, event_id=event_id, actor=actor)
    event = registry.events.get(event_id)
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('当前事件不接受新的补给联系报告。')
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('联系记录版本须为非负整数。')
    _text(evidence)
    _text(request_id)
    _text(expected_source_digest)
    amount = _quantity(available_quantity)
    delivery = _text(promised_delivery_at, optional=True)
    if delivery:
        explicit_time(delivery)
    current = review(workspace, ledger, event_id=event_id, item_id=item_id,
                     source_kind=source_kind, source_id=source_id)
    times = RecordTimes(occurred_at=occurred_at,
                        recorded_at=datetime.now(timezone.utc).isoformat())
    if occurred_at and explicit_time(occurred_at) > explicit_time(times.recorded_at):
        raise ValueError('实际联系时间不能晚于记录时间。')
    for previous in workspace.object_revisions.values():
        if previous.request_id != request_id:
            continue
        content = json.loads(previous.content_json)
        if (previous in current.records and previous.actor_id == actor
                and previous.source == SOURCE + evidence
                and previous.times.occurred_at == occurred_at
                and previous.reference.version == expected_version + 1
                and content['source_digest'] == expected_source_digest
                and content['available_quantity'] == amount
                and content['promised_delivery_at'] == delivery):
            return previous
        raise ValueError('联系请求编号已用于不同内容。')
    if current.version != expected_version:
        raise ValueError('联系记录版本已变化，请刷新核对。')
    if current.current_source_digest is None or current.current_source_digest != expected_source_digest:
        raise ValueError('联系来源已变化或无效，请刷新核对，不沿用旧资料。')
    content = dict(item_id=item_id, source_kind=source_kind, source_id=source_id,
                   source_snapshot=json.loads(current.current_source_json),
                   source_digest=expected_source_digest,
                   available_quantity=amount, promised_delivery_at=delivery)
    revision = ObjectRevision(ObjectReference(event_id, KIND, current.object_id,
                                               current.version + 1), times,
                              SOURCE + evidence, 'reported', actor, request_id, _json(content))
    return workspace._record_object_revision(revision, expected_version=expected_version)
