"""Named immutable catalog changes and imports with conflict-safe rollback."""
import base64
import csv
import hashlib
import io
import json
import math
from copy import deepcopy
from dataclasses import asdict
from . import audited_records as ar
from . import material_maintenance_authority as authority
from . import data_imports as imports
from .document_tables import extract

KIND = 'material_catalog_change'
SOURCE = 'named-long-term-material-catalog'
TYPES = ('item', 'carry', 'supply_point', 'stock', 'company')
TABLES = ('items', 'carry_lines', 'points', 'companies')


def snapshot(ledger):
    return {t: {k: asdict(v) for k, v in getattr(ledger, t).items()} for t in TABLES}


def inspect(ledger, spaces):
    grant_rows = authority.inspect(ledger, spaces)
    grants = {r.id: (r, v) for r, v in grant_rows}
    rows = []; mirrors = set()
    for eid, space in spaces.items():
        for index, record, value in ar.records(space, KIND):
            if ledger.catalog_records.get(record.id) != record or record.source != SOURCE:
                raise ValueError('长期资料修改镜像缺失或来源不一致。')
            mirrors.add(record.id); rows.append((record, value))
    if mirrors != set(ledger.catalog_records): raise ValueError('长期资料修改缺少具名事件来源。')
    rows.sort(key=lambda rv: rv[1]['sequence']); previous = ''; latest = {}; requests = set(); versions = {}
    import_sources = {}
    from .resource_maintenance import catalog_changes
    for seq, (record, v) in enumerate(rows, 1):
        catalog_changes(ledger, seq-1, latest)
        if set(v) != {'sequence', 'previous', 'grant_ref', 'version', 'basis', 'operation', 'details', 'changes'}:
            raise ValueError('资料审计字段异常。')
        g, gv = grants.get(v['grant_ref'], (None, None))
        oid = record.reference.object_id
        if (v['operation'] not in {'edit','upload','draft','publish','rollback','closeout'}
                or not oid.startswith('closeout-' if v['operation']=='closeout' else ('edit-' if v['operation']=='edit' else 'import-'))):
            raise ValueError('资料动作与修改对象/导入批次编号不一致。')
        if (v['sequence'] != seq or v['previous'] != previous or record.request_id in requests
                or v['version'] != versions.get(oid, 0) + 1 or not g or not gv['active']
                or gv['subject_id'] != record.actor_id or g.reference.event_id != record.reference.event_id
                or g.times.recorded_at > record.times.recorded_at):
            raise ValueError('资料修改授权、版本或流水断链。')
        if authority.at_revision(grant_rows, spaces, record).id != v['grant_ref']:
            raise ValueError('资料写入没有使用当时最新有效维护授权。')
        ar.text(v['basis'])
        touched = set()
        for change in v['changes']:
            if set(change) != {'table', 'key', 'before', 'after'} or change['table'] not in TABLES:
                raise ValueError('资料修改对象不受支持。')
            key = (change['table'], change['key'])
            if key in touched or key in latest and change['before'] != latest[key]: raise ValueError('资料修改前后版本不衔接。')
            latest[key] = change['after']; touched.add(key)
        if v['operation'] == 'closeout':
            from .material_closeout_accounts import inspect as inspect_closeout
            owner = spaces.get(v['details']['input']['event_id'])
            closeouts = inspect_closeout(owner, ledger) if owner else ()
            ref = next((r for _,r,q in closeouts if r.id==v['details']['input']['closeout_ref']), None)
            if not ref or ar.digest(asdict(ref))!=v['details']['closeout_hash'] or oid!=ref.reference.object_id:
                raise ValueError('长期底账调整的核实来源不一致。')
        if v['operation'] == 'upload':
            d = v['details']
            if d['input'] != {k:d[k] for k in ('data_type','filename','original_base64')}:
                raise ValueError('上传原文件与请求来源不一致。')
            raw = base64.b64decode(v['details']['original_base64'], validate=True)
            if hashlib.sha256(raw).hexdigest() != d['content_sha256']: raise ValueError('原文件哈希不一致。')
            mapping, mapped, errors = preview(d['data_type'], extract(d['filename'], raw), ledger, record.actor_id, validate_rows=False)
            if mapping != d['mapping'] or mapped != d['rows'] or not set(errors) <= set(d['errors']):
                raise ValueError('原文件、字段映射或逐行预览不一致。')
            import_sources[oid] = {k:d[k] for k in ('data_type','filename','original_base64','content_sha256','mapping','rows','errors')}
        elif v['operation'] in {'draft','publish','rollback'}:
            frozen = import_sources.get(oid)
            if frozen is None or any(v['details'].get(k)!=value for k,value in frozen.items()):
                raise ValueError('导入后续版本替换了原字节、映射或逐行来源。')
        previous = record.id; versions[oid] = v['version']; requests.add(record.request_id)
    catalog_changes(ledger, len(rows), latest)
    actual = snapshot(ledger)
    for (table, key), value in latest.items():
        if actual[table].get(key) != value: raise ValueError('长期底账与具名修改记录不一致。')
    return tuple(rows)


def _quantity(value, unknown=False):
    if unknown and value in (None, '', '未知'): return None
    if isinstance(value, bool): raise ValueError('数量须为非负有限数字。')
    try: qty = float(value)
    except (ValueError, TypeError): raise ValueError('数量须为非负有限数字。')
    if not math.isfinite(qty) or qty < 0: raise ValueError('数量须为非负有限数字。')
    return qty


def _match(values, name, label):
    matches = [v for v in values.values() if v.name == name]
    if len(matches) != 1: raise ValueError(label + '未建档或重名/多规格，须明确匹配。')
    return matches[0]


def _apply(ledger, data_type, row, actor):
    allowed = {
        'item': {'name', 'category', 'spec', 'unit', 'quantity'},
        'carry': {'squad', 'item_name', 'quantity', 'unit'},
        'supply_point': {'name', 'location', 'org', 'contact_name', 'contact_phone'},
        'stock': {'point', 'item_name', 'quantity'},
        'company': {'name', 'services', 'region', 'contact_name', 'contact_phone', 'source', 'source_updated_at'},
    }
    if data_type not in allowed or set(row) - allowed[data_type]: raise ValueError('资料类型或字段越出物资维护范围。')
    if any(not isinstance(v, (str, int, float, type(None))) or isinstance(v, bool) for v in row.values()): raise ValueError('资料字段类型异常。')
    if any(not isinstance(v, str) for k, v in row.items() if k != 'quantity'):
        raise ValueError('文字资料字段须为文本。')
    if data_type == 'item':
        name = ar.text(row.get('name')); spec = row.get('spec', '').strip()
        matches = [v for v in ledger.items.values() if v.name == name and v.spec == spec]
        if len(matches) > 1: raise ValueError('物资档案重名/同规格，须先核对来源。')
        existing = matches[0] if matches else None
        preserved = dict(status=existing.status, valid_until=existing.valid_until, owner=existing.owner) if existing else {}
        ledger.upsert_item(name=name, category=row.get('category', existing.category if existing else ''), spec=spec,
            unit=row.get('unit', existing.unit if existing else ''),
            default_carry_qty=_quantity(row.get('quantity', existing.default_carry_qty if existing else 0)),
            item_id=existing.item_id if existing else None, **preserved)
    elif data_type == 'carry':
        squad = _match(ledger.squads, row.get('squad'), '队伍'); item = _match(ledger.items, row.get('item_name'), '物资')
        if row.get('unit') and row['unit'] != item.unit: raise ValueError('数量单位与物资档案不一致。')
        ledger.set_carry_line(squad.squad_id, item.item_id, quantity=_quantity(row.get('quantity')))
    elif data_type == 'supply_point':
        name = ar.text(row.get('name')); existing = next((v for v in ledger.points.values() if v.name == name), None)
        fields = {k: str(row.get(k, '')).strip() for k in ('location', 'org', 'contact_name', 'contact_phone')}
        if fields['contact_phone']:
            from .org_model import normalize_phone
            fields['contact_phone'] = normalize_phone(fields['contact_phone'])
        if existing:
            for k, v in fields.items(): setattr(existing, k, v)
        else: ledger.add_supply_point(name=name, **fields)
    elif data_type == 'stock':
        point = _match(ledger.points, row.get('point'), '物资点'); item = _match(ledger.items, row.get('item_name'), '物资')
        point.supplies[item.item_id] = _quantity(row.get('quantity'), unknown=True)
    elif data_type == 'company':
        from .materials import ExternalCompany
        from .record_contracts import explicit_time
        name = ar.text(row.get('name')); ar.text(row.get('source'))
        fields = {k: str(row.get(k, '')).strip() for k in allowed['company'] - {'name'}}
        if fields['source_updated_at']: explicit_time(fields['source_updated_at'])
        if fields['contact_phone']:
            from .org_model import normalize_phone
            fields['contact_phone'] = normalize_phone(fields['contact_phone'])
        existing = next((v for v in ledger.companies.values() if v.name == name), None)
        if existing:
            ledger.companies[existing.company_id] = ExternalCompany(**(asdict(existing) | fields | {'recorded_by': actor}))
        else: ledger.add_external_company(name=name, recorded_by=actor, **fields)


def preview(data_type, text, ledger, actor, *, validate_rows=True):
    specs = dict(imports.DATA_TYPES)
    specs['item'] = imports.DataTypeSpec('item', '物资档案', {'name': ('物资名称','名称'), 'category': ('分类',),
        'spec': ('规格',), 'unit': ('单位',), 'quantity': ('默认携带数量','数量')}, ('name','unit'))
    from .company_import_preview import _ALIASES
    specs['company'] = imports.DataTypeSpec('company', '支援公司', _ALIASES, ('name','source'))
    if data_type not in TYPES: raise ValueError('该入口不包含人员、编制或岗位授予。')
    reader = list(csv.reader(io.StringIO(text), strict=True)); spec = specs[data_type]; errors = []; mapping = {}
    if not reader or len(reader) > 2001: return {}, [], ['空表或超过2000行。']
    header = [c.strip().lstrip('\ufeff') for c in reader[0]]
    if any(not c for c in header) or len(set(header)) != len(header): return {}, [], ['表头为空或重复。']
    for field, aliases in spec.aliases.items():
        candidates = [i for i, c in enumerate(header) if c.casefold() in {a.casefold() for a in aliases} | {field}]
        if len(candidates) > 1: errors.append('字段对应多列：'+field)
        elif candidates: mapping[field] = header[candidates[0]]
    if set(header) - set(mapping.values()): errors.append('存在未映射列：'+ '、'.join(c for c in header if c not in mapping.values()))
    if set(spec.required) - set(mapping): errors.append('缺少必需列：'+ '、'.join(sorted(set(spec.required)-set(mapping))))
    rows = []; seen = set(); candidate = deepcopy(ledger)
    for number, raw in enumerate(reader[1:], 2):
        if not any(raw): continue
        if len(raw) != len(header): errors.append(f'第{number}行列数不一致。'); continue
        row = {f: raw[header.index(c)].strip() for f,c in mapping.items()}; rows.append(row)
        identity = tuple(row.get(k,'') for k in ('name','spec','squad','point','item_name'))
        if identity in seen: errors.append(f'第{number}行重复资料/物资。')
        seen.add(identity)
        try:
            if validate_rows: _apply(candidate, data_type, row, actor)
        except (ValueError, KeyError, TypeError) as error: errors.append(f'第{number}行：{error}')
    if not rows: errors.append('未解析到数据行。')
    return mapping, rows, errors


def _restore(ledger, table, key, value):
    from .materials import MaterialItem, CarryLine, SupplyPoint, ExternalCompany
    classes = dict(items=MaterialItem, carry_lines=CarryLine, points=SupplyPoint, companies=ExternalCompany)
    target = getattr(ledger, table)
    if value is None: target.pop(key, None)
    else: target[key] = classes[table](**value)


def record(space, ledger, spaces, registry, actor, *, operation, object_id, expected_version, request_id, basis, details):
    grant = authority.require(ledger, spaces, registry, actor); rows = inspect(ledger, spaces)
    if grant.reference.event_id != space.event_id: raise ValueError('资料审计须绑定当前有效授权的来源事件。')
    ar.text(basis); ar.text(request_id); ar.text(object_id)
    if operation not in {'edit','upload','draft','publish','rollback','closeout'}: raise ValueError('资料动作不支持。')
    if not object_id.startswith('closeout-' if operation=='closeout' else ('edit-' if operation=='edit' else 'import-')):
        raise ValueError('资料动作与修改对象/导入批次编号不一致。')
    own = [(r,v) for r,v in rows if r.reference.object_id == object_id]
    old = next(((r,v) for r,v in rows if r.request_id == request_id), None)
    if old:
        r,v = old
        # The exact input survives later imports, revocation and edits; current authorization is still required.
        if (r.actor_id != actor or r.reference.object_id != object_id or v['operation'] != operation
                or v['basis'] != basis or v['version'] != expected_version+1 or v['details'].get('input') != details):
            raise ValueError('资料请求号已用于不同内容。')
        return r
    if type(expected_version) is not int or expected_version != len(own): raise ValueError('资料版本已变化。')
    before = snapshot(ledger); data = {'input': details}; latest = own[-1][1] if own else None
    if operation == 'closeout':
        from .material_closeout_accounts import apply
        closeout = apply(ledger, spaces, details)
        if object_id != closeout.reference.object_id:
            raise ValueError('底账调整对象与核实版不一致。')
        if any(v['operation']=='closeout' and v['details']['input']==details for r,v in rows):
            raise ValueError('该核实版已经调整底账，请追加核实后更正。')
        data['closeout_hash'] = ar.digest(asdict(closeout))
    elif operation == 'edit':
        if set(details) != {'data_type','row'}: raise ValueError('修改字段不正确。')
        _apply(ledger, details['data_type'], details['row'], actor)
    elif operation == 'upload':
        if own or set(details) != {'data_type','filename','original_base64'}: raise ValueError('导入批次已存在或字段不正确。')
        raw = base64.b64decode(details['original_base64'], validate=True)
        text = extract(details['filename'], raw)
        mapping, mapped, errors = preview(details['data_type'], text, ledger, actor)
        data.update(filename=details['filename'], data_type=details['data_type'], original_base64=details['original_base64'],
            content_sha256=hashlib.sha256(raw).hexdigest(), mapping=mapping, rows=mapped, errors=errors, state='失败' if errors else '预览')
    else:
        if not latest or details: raise ValueError('导入批次不存在或字段不正确。')
        prior = latest['details']; data.update({k:v for k,v in prior.items() if k != 'input'})
        if operation == 'draft':
            if prior.get('state') != '预览' or prior.get('errors'): raise ValueError('仅无错误预览可生成草稿。')
            candidate = deepcopy(ledger)
            for row in prior['rows']: _apply(candidate, prior['data_type'], row, actor)
            data.update(state='已生成草稿', catalog_digest=ar.digest(snapshot(ledger)))
        elif operation == 'publish':
            if prior.get('state') != '已生成草稿' or prior['catalog_digest'] != ar.digest(snapshot(ledger)):
                raise ValueError('草稿缺失或库已变化，请重新导入预览。')
            for row in prior['rows']: _apply(ledger, prior['data_type'], row, actor)
            data['state'] = '已发布'
        else:
            if prior.get('state') != '已发布': raise ValueError('仅已发布批次可回滚。')
            for change in latest['changes']:
                table,key = change['table'],change['key']
                if before[table].get(key) != change['after']: raise ValueError('同一资料已有后续变更，拒绝覆盖。')
                if change['before'] is None:
                    if table == 'items' and (any(l.item_id == key for l in ledger.carry_lines.values())
                            or any(key in p.supplies for p in ledger.points.values())
                            or any(key in s.lines for snap in ledger.snapshots.values() for s in snap.squads.values())):
                        raise ValueError('新增物资已被清单、库存或到场引用，不能删除。')
                    if any(key in r.content_json for e in spaces.values() for r in e.object_revisions.values() if r.reference.object_kind not in {KIND,authority.KIND}):
                        raise ValueError('新增资料已有业务引用，不能删除。')
                _restore(ledger, table, key, change['before'])
            data['state'] = '已回滚'
    after = snapshot(ledger); changes = []
    for table in TABLES:
        for key in sorted(set(before[table]) | set(after[table])):
            if before[table].get(key) != after[table].get(key): changes.append(dict(table=table,key=key,before=before[table].get(key),after=after[table].get(key)))
    if operation == 'edit' and not changes: raise ValueError('资料内容没有变化。')
    value = dict(sequence=len(rows)+1,previous=rows[-1][0].id if rows else '',grant_ref=grant.id,version=len(own)+1,
        basis=basis,operation=operation,details=data,changes=changes)
    local = sum(r.reference.object_id==object_id for _,r,v in ar.records(space,KIND))
    revision = ar.append(space,kind=KIND,object_id=object_id,source=SOURCE,actor=actor,values=value,expected_version=local,request_id=request_id)
    ledger.catalog_records[revision.id] = revision; inspect(ledger, spaces | {space.event_id:space})
    return revision
