"""Versioned supply attachments to existing task drafts, never dispatch or stock."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
import json

from . import material_supply_contacts as contacts
from .fire_events import ONBOARDABLE_STATUSES
from .materials import MaterialItem
from .models import GENERAL_ENTRY_ID
from .record_contracts import ObjectReference, ObjectRevision, RecordTimes, explicit_time

KIND = 'material_supply_draft'
SOURCE = '补给任务草案附件：'
_INPUTS = {'decision_id', 'task_id', 'item_id', 'base_bundle_version',
           'base_bundle_digest', 'item_digest', 'requested_quantity', 'quantity_basis',
           'needed_by', 'contact_id', 'internal_contact_ids', 'internal_priority_reason',
           'destination_hint'}
_CONTENT = _INPUTS | {'bundle_snapshot', 'bundle_digest', 'item_snapshot',
                      'contact_snapshots', 'automatic_gap'}


def _object_id(decision_id, task_id, item_id):
    for value in (decision_id, task_id, item_id):
        contacts._text(value)
    return 'supply-draft-' + contacts._digest([decision_id, task_id, item_id])


def _item(item, item_id):
    if (not isinstance(item, dict) or set(item) != {f.name for f in fields(MaterialItem)}
            or item['item_id'] != item_id
            or any(not isinstance(value, str) for key, value in item.items()
                   if key != 'default_carry_qty')
            or contacts._quantity(item['default_carry_qty']) is None):
        raise ValueError('补给物资冻结资料不一致。')
    contacts._text(item['name'])


def _inputs(*, decision_id, task_id, item_id, base_bundle_version,
            base_bundle_digest, item_digest, requested_quantity, quantity_basis,
            needed_by, contact_id, internal_contact_ids, internal_priority_reason,
            destination_hint):
    _object_id(decision_id, task_id, item_id)
    if type(base_bundle_version) is not int or base_bundle_version < 1:
        raise ValueError('任务草案版本须为正整数。')
    amount = contacts._quantity(requested_quantity)
    for value in (base_bundle_digest, item_digest):
        contacts._text(value)
    for value in (quantity_basis, needed_by, contact_id, internal_priority_reason, destination_hint):
        contacts._text(value, optional=True)
    if amount is not None and not quantity_basis:
        raise ValueError('人工明确数量须同时填写数量依据，不能冒充自动缺口。')
    if needed_by:
        explicit_time(needed_by)
    if (not isinstance(internal_contact_ids, tuple)
            or any(not isinstance(value, str) or not value or value != value.strip()
                   for value in internal_contact_ids)
            or len(set(internal_contact_ids)) != len(internal_contact_ids)
            or contact_id and contact_id in internal_contact_ids):
        raise ValueError('库内联系引用须唯一，不能重复所选供给联系。')
    return dict(decision_id=decision_id, task_id=task_id, item_id=item_id,
                base_bundle_version=base_bundle_version,
                base_bundle_digest=base_bundle_digest, item_digest=item_digest,
                requested_quantity=amount, quantity_basis=quantity_basis,
                needed_by=needed_by, contact_id=contact_id,
                internal_contact_ids=list(internal_contact_ids),
                internal_priority_reason=internal_priority_reason, destination_hint=destination_hint)


def _records(workspace, ledger, event_id):
    """Reconcile every attachment with historical task and contact audit positions."""
    contact_records = {r.id: r for r in contacts._records(workspace, ledger, event_id)}
    contact_positions = {e.payload.get('revision_id'): index
                         for index, e in enumerate(workspace.events)
                         if e.kind == 'ObjectRevisionRecorded'}
    records = {key: r for key, r in workspace.object_revisions.items()
               if r.reference.object_kind == KIND}
    audited = [(index, e) for index, e in enumerate(workspace.events)
               if e.kind == 'ObjectRevisionRecorded'
               and e.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND]
    if len(records) != len(audited):
        raise ValueError('补给附件与审计镜像数量不一致。')
    versions, requests, clocks = {}, set(), {}
    checked = []
    for index, event in audited:
        record = records.get(event.payload.get('revision_id'))
        if record is None or event.payload != {'revision_id': record.id, 'revision': asdict(record)}:
            raise ValueError('补给附件与审计镜像不一致。')
        content = json.loads(record.content_json)
        ref = record.reference
        if (not isinstance(content, dict) or set(content) != _CONTENT or ref.event_id != event_id
                or record.actor_id != GENERAL_ENTRY_ID or record.data_kind != 'reported'
                or not record.source.startswith(SOURCE) or record.request_id in requests
                or sum(r.request_id == record.request_id for r in workspace.object_revisions.values()) != 1
                or workspace.object_revisions.get(record.id) != record
                or content['automatic_gap'] is not None):
            raise ValueError('补给草案归属或数据性质不一致。')
        contacts._text(record.source[len(SOURCE):])
        values = {key: content[key] for key in _INPUTS}
        if not isinstance(values['internal_contact_ids'], list):
            raise ValueError('库内联系引用格式不正确。')
        values['internal_contact_ids'] = tuple(values['internal_contact_ids'])
        if _inputs(**values) != {key: content[key] for key in _INPUTS}:
            raise ValueError('补给附件输入与冻结内容不一致。')
        if workspace.task_drafts_for_decision(content['decision_id'], event_id=event_id) is None:
            raise ValueError('补给附件缺少可核对的采纳任务草案。')
        object_id = _object_id(content['decision_id'], content['task_id'], content['item_id'])
        if ref.object_id != object_id or ref.version != versions.get(object_id, 0) + 1:
            raise ValueError('补给附件对象或版本不连续。')
        item = content['item_snapshot']
        _item(item, content['item_id'])
        if contacts._digest(item) != content['item_digest']:
            raise ValueError('补给物资冻结指纹不一致。')
        bundle = content['bundle_snapshot']
        prior = [e.payload.get('bundle') for e in workspace.events[:index]
                 if e.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}
                 and e.payload.get('event_id') == event_id
                 and e.payload.get('decision_id') == content['decision_id']]
        if (not isinstance(bundle, dict) or len(prior) < 2
                or not isinstance(prior[-2], dict)
                or contacts._json(prior[-1]) != contacts._json(bundle)
                or contacts._digest(bundle) != content['bundle_digest']
                or contacts._digest(prior[-2]) != content['base_bundle_digest']
                or prior[-2].get('version') != content['base_bundle_version']
                or bundle.get('version') != content['base_bundle_version'] + 1
                or bundle.get('event_id') != event_id or bundle.get('status') != 'draft'
                or contacts._json(prior[-2] | {'version': bundle.get('version'),
                    'updated_at': bundle.get('updated_at')}) != contacts._json(bundle)
                or not any(t.get('id') == content['task_id'] for t in bundle.get('tasks', []))):
            raise ValueError('补给附件与当时任务草案版本不一致。')
        revision_event = workspace.events[index - 1]
        if (revision_event.kind != 'TaskDraftBundleRevised'
                or contacts._json(revision_event.payload) != contacts._json(dict(
                    event_id=event_id, decision_id=content['decision_id'], actor=GENERAL_ENTRY_ID,
                    bundle=bundle, supply_revision_id=record.id))):
            raise ValueError('补给附件未形成对应任务草案修订。')
        ids = ([content['contact_id']] if content['contact_id'] else []) + content['internal_contact_ids']
        snapshots = content['contact_snapshots']
        if not isinstance(snapshots, list) or len(ids) != len(snapshots):
            raise ValueError('补给联系冻结数量不一致。')
        for identifier, frozen in zip(ids, snapshots):
            source = contact_records.get(identifier)
            if (source is None or asdict(source) != frozen
                    or contact_positions[identifier] >= index - 1):
                raise ValueError('补给联系引用与当时来源不一致。')
            data = json.loads(source.content_json)
            previous_contacts = [r for r in contact_records.values()
                                 if r.reference.object_id == source.reference.object_id
                                 and contact_positions[r.id] < index - 1]
            if source != max(previous_contacts, key=lambda r: r.reference.version):
                raise ValueError('补给附件引用不是当时最新联系报告。')
            if data['item_id'] != content['item_id'] or (
                    identifier in content['internal_contact_ids'] and data['source_kind'] != 'point'):
                raise ValueError('补给联系物资或库内来源不一致。')
        recorded = explicit_time(record.times.recorded_at)
        if (record.times.occurred_at or record.times.collected_at or record.times.received_at
                or record.times.recorded_at != bundle.get('updated_at')
                or recorded > explicit_time(revision_event.recorded_at)
                or recorded > explicit_time(event.recorded_at)
                or object_id in clocks and recorded < clocks[object_id]):
            raise ValueError('补给草案记录时间不一致。')
        versions[object_id], clocks[object_id] = ref.version, recorded
        requests.add(record.request_id)
        checked.append(record)
    return tuple(checked)


@dataclass(frozen=True)
class SupplyDraftReview:
    records: tuple[ObjectRevision, ...]
    version: int
    current: bool
    changed_contact_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    bundle_version: int = 0
    continued_by: tuple[str, ...] = ()
    conclusion: str = '补给任务草案附件，未发布、未占用库存、未形成在途或实收'


def review(workspace, ledger, *, event_id, decision_id, task_id, item_id):
    object_id = _object_id(decision_id, task_id, item_id)
    all_records = _records(workspace, ledger, event_id)
    records = tuple(r for r in all_records
                    if r.reference.object_id == object_id)
    if not records:
        return SupplyDraftReview((), 0, False, (), ('尚无本任务物资的补给草案附件',))
    latest = json.loads(records[-1].content_json)
    bundle = workspace.task_drafts_for_decision(decision_id, event_id=event_id)
    current = bool(bundle and contacts._digest(asdict(bundle)) == latest['bundle_digest'])
    continued = ()
    if bundle and not current:
        position = next(index for index, event in enumerate(workspace.events)
                        if event.kind == 'ObjectRevisionRecorded'
                        and event.payload.get('revision_id') == records[-1].id)
        later = [event for event in workspace.events[position + 1:]
                 if event.kind in {'TaskDraftBundleCreated', 'TaskDraftBundleRevised'}
                 and event.payload.get('event_id') == event_id
                 and event.payload.get('decision_id') == decision_id]
        verified_ids = {record.id for record in all_records}
        # Only audited attachment-only revisions bridge versions. Real task edits
        # always invalidate the old attachment, even if later text is restored.
        if later and all(event.kind == 'TaskDraftBundleRevised'
                         and event.payload.get('supply_revision_id') in verified_ids
                         for event in later):
            continued = tuple(event.payload['supply_revision_id'] for event in later)
            current = True
    issues = [] if current else ['任务草案已变更或任务已移除，旧补给附件不能作为当前版依据']
    try:
        item = asdict(ledger.items[item_id])
        _item(item, item_id)
        item_current = contacts._digest(item) == latest['item_digest']
    except (KeyError, ValueError, TypeError):
        item_current = False
    if not item_current:
        issues.append('物资档案已变化或缺失，需重新核对冻结来源')
    all_contacts = contacts._records(workspace, ledger, event_id)
    changed = []
    for frozen in latest['contact_snapshots']:
        source = next(r for r in all_contacts if asdict(r) == frozen)
        data = json.loads(source.content_json)
        newest = max((r for r in all_contacts if r.reference.object_id == source.reference.object_id),
                     key=lambda r: r.reference.version)
        try:
            digest = contacts._digest(contacts._current_snapshot(ledger, item_id, data['source_kind'], data['source_id']))
        except ValueError:
            digest = None
        if source != newest or digest != data['source_digest']:
            changed.append(source.id)
    if changed:
        issues.append('所引用联系已更新或登记来源已变化，请重新核对')
    amount = latest['requested_quantity']
    if amount is None:
        issues.append('人工需求数量未知；自动缺口仍待专业用量依据，不以联系可供量代填')
    if not latest['needed_by']:
        issues.append('补给需求期限待明确')
    if not latest['destination_hint']:
        issues.append('接收对象或位置待明确，草案文字不授予接收职责')
    selected = next((json.loads(r['content_json']) for r in latest['contact_snapshots'][:1]), None) if latest['contact_id'] else None
    if selected is None:
        issues.append('供给联系来源待选择')
    else:
        available = selected['available_quantity']
        if available is None or available == 0 or amount is not None and available < amount:
            issues.append('联系报告可供量未知、无货或不足，不能确认为供应落实')
        if not selected['promised_delivery_at']:
            issues.append('联系报告送达承诺未知')
        elif latest['needed_by'] and explicit_time(selected['promised_delivery_at']) > explicit_time(latest['needed_by']):
            issues.append('报告送达承诺晚于本草案需求期限')
        if selected['source_kind'] == 'company' and (
                not latest['internal_contact_ids'] or not latest['internal_priority_reason']):
            issues.append('外部公司选择缺少库内无货、不足或时效不满足的可追溯依据')
    issues.append('人工数量及联系报告未做独立核验；正式发布专业、安全及具名门禁保持')
    return SupplyDraftReview(records, len(records), current and item_current and not changed,
                             tuple(changed), tuple(issues), bundle.version if bundle else 0, continued)


def record(workspace, ledger, registry, *, event_id, decision_id, task_id, item_id,
           expected_bundle_version, expected_bundle_digest, expected_item_digest,
           expected_version, requested_quantity, quantity_basis, needed_by, evidence,
           request_id, contact_id='', internal_contact_ids=(), internal_priority_reason='',
           destination_hint=''):
    workspace._require_general_entry()  # Existing task-drafting permission only.
    event = registry.events.get(event_id)
    if workspace.event_id != event_id or event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('当前事件不接受补给任务草案修订。')
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('补给附件版本须为非负整数。')
    contacts._text(evidence)
    contacts._text(request_id)
    inputs = _inputs(decision_id=decision_id, task_id=task_id, item_id=item_id,
        base_bundle_version=expected_bundle_version, base_bundle_digest=expected_bundle_digest,
        item_digest=expected_item_digest, requested_quantity=requested_quantity,
        quantity_basis=quantity_basis, needed_by=needed_by, contact_id=contact_id,
        internal_contact_ids=internal_contact_ids, internal_priority_reason=internal_priority_reason,
        destination_hint=destination_hint)
    history = _records(workspace, ledger, event_id)
    object_id = _object_id(decision_id, task_id, item_id)
    for previous in workspace.object_revisions.values():
        if previous.request_id != request_id:
            continue
        data = json.loads(previous.content_json)
        if (previous in history and previous.reference.object_id == object_id
                and previous.source == SOURCE + evidence
                and previous.reference.version == expected_version + 1
                and all(data[key] == value for key, value in inputs.items())):
            return previous
        raise ValueError('补给草案请求号已用于不同内容。')
    current_version = sum(r.reference.object_id == object_id for r in history)
    if current_version != expected_version:
        raise ValueError('补给草案附件版本已变化，请刷新核对。')
    bundle = workspace.task_drafts_for_decision(decision_id, event_id=event_id)
    if (bundle is None or bundle.status != 'draft'
            or bundle.version != expected_bundle_version
            or contacts._digest(asdict(bundle)) != expected_bundle_digest
            or not any(t.id == task_id for t in bundle.tasks)):
        raise ValueError('当前采纳任务草案版本或任务归属已变化。')
    item = ledger.items.get(item_id)
    if not isinstance(item, MaterialItem):
        raise ValueError('物资档案缺失。')
    item_snapshot = asdict(item)
    _item(item_snapshot, item_id)
    if contacts._digest(item_snapshot) != expected_item_digest:
        raise ValueError('物资档案已变化，请刷新核对。')
    all_contacts = contacts._records(workspace, ledger, event_id)
    ids = ([contact_id] if contact_id else []) + list(internal_contact_ids)
    frozen_contacts = []
    for identifier in ids:
        source = next((r for r in all_contacts if r.id == identifier), None)
        if source is None:
            raise ValueError('联系引用不在本事件审计来源中。')
        data = json.loads(source.content_json)
        if data['item_id'] != item_id or identifier in internal_contact_ids and data['source_kind'] != 'point':
            raise ValueError('联系物资或库内来源不一致。')
        newest = max((r for r in all_contacts if r.reference.object_id == source.reference.object_id),
                     key=lambda r: r.reference.version)
        current = contacts._current_snapshot(ledger, item_id, data['source_kind'], data['source_id'])
        if source != newest or contacts._digest(current) != data['source_digest']:
            raise ValueError('联系报告或登记来源已更新，请重新核对。')
        frozen_contacts.append(asdict(source))
    now = datetime.now(timezone.utc).isoformat()
    if any(r.reference.object_id == object_id and
           explicit_time(r.times.recorded_at) > explicit_time(now) for r in history):
        raise ValueError('补给附件系统时间不能早于已有版本。')
    updated = replace(bundle, version=bundle.version + 1, updated_at=now)
    content = inputs | dict(bundle_snapshot=asdict(updated), bundle_digest=contacts._digest(asdict(updated)),
        item_snapshot=item_snapshot, contact_snapshots=frozen_contacts, automatic_gap=None)
    revision = ObjectRevision(ObjectReference(event_id, KIND, object_id, current_version + 1),
        RecordTimes(recorded_at=now), SOURCE + evidence, 'reported', GENERAL_ENTRY_ID,
        request_id, contacts._json(content))
    # All validation completed before either the task or attachment mutates.
    workspace.task_drafts[updated.id] = updated
    workspace.append_event('TaskDraftBundleRevised', event_id=event_id, decision_id=decision_id,
        actor=GENERAL_ENTRY_ID, bundle=asdict(updated), supply_revision_id=revision.id)
    return workspace._record_object_revision(revision, expected_version=current_version)


def preflight(workspace, ledger, bundle):
    if not any(r.reference.object_kind == KIND for r in workspace.object_revisions.values()) and not any(
            e.kind == 'ObjectRevisionRecorded' and
            e.payload.get('revision', {}).get('reference', {}).get('object_kind') == KIND
            for e in workspace.events):
        return ()
    records = _records(workspace, ledger, bundle.event_id)
    objects = {}
    for record in records:
        content = json.loads(record.content_json)
        if content['decision_id'] == bundle.decision_id:
            objects[record.reference.object_id] = content
    issues = []
    for data in objects.values():
        view = review(workspace, ledger, event_id=bundle.event_id,
            decision_id=bundle.decision_id, task_id=data['task_id'], item_id=data['item_id'])
        amount = data['requested_quantity']
        issues.append(f'补给草案附件 {data["task_id"]} / {data["item_id"]} v{view.version}：'
                      + ('人工需求未知' if amount is None else f'人工申报数量 {amount:g}')
                      + f'；当前草案 v{view.bundle_version}，审计续接 {len(view.continued_by)} 次'
                      + '；自动缺口待核；未发布、未计入库存。')
        issues.extend(view.blockers)
    return tuple(issues)
