"""Read-only preview for simulated external-company CSV; no publish authority."""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

from .record_contracts import explicit_time


_ALIASES = {
    'name': ('公司名称', '公司', '名称'),
    'services': ('服务/物资', '服务物资', '服务', '物资'),
    'region': ('服务区域', '区域'),
    'contact_name': ('联系人',),
    'contact_phone': ('联系电话', '电话'),
    'source': ('资料来源', '来源'),
    'source_updated_at': ('来源更新时间', '更新时间'),
}


def _key(value: str) -> str:
    return value.strip().lstrip('\ufeff').casefold()


@dataclass(frozen=True)
class CompanyImportPreview:
    mapping: dict[str, str]
    rows: tuple[dict, ...]
    errors: tuple[str, ...]
    unrecognized_columns: tuple[str, ...]
    content_sha256: str


def preview_external_companies(text: str, ledger) -> CompanyImportPreview:
    """Inspect CSV as unverified descriptions; never mutate ledger or infer stock."""
    content = text or ''
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()

    def result(mapping=None, rows=(), errors=(), unknown=()):
        return CompanyImportPreview(mapping or {}, tuple(rows), tuple(errors),
                                    tuple(unknown), digest)

    try:
        source_rows = list(csv.reader(io.StringIO(content, newline=''), strict=True))
    except csv.Error as exc:
        return result(errors=(f'CSV 格式错误：{exc}',))
    numbered = [(number, row) for number, row in enumerate(source_rows, start=1)
                if any(cell.strip() for cell in row)]
    if not numbered:
        return result(errors=('文件为空或未检测到数据行。',))
    header_number, header = numbered[0]
    names = [_key(name) for name in header]
    errors = []
    if any(not name for name in names):
        errors.append(f'第{header_number}行：列名不能为空。')
    if len(set(names)) != len(names):
        errors.append(f'第{header_number}行：列名重复。')
    mapping = {}
    field_indexes = {}
    for field, aliases in _ALIASES.items():
        candidates = [index for index, name in enumerate(names)
                      if name in {_key(alias) for alias in aliases}]
        if len(candidates) > 1:
            errors.append(f'第{header_number}行：{field} 对应多列，需明确保留一列。')
        elif candidates:
            index = candidates[0]
            field_indexes[field] = index
            mapping[field] = header[index].strip().lstrip('\ufeff')
    if 'name' not in field_indexes:
        errors.append('缺少必需列：公司名称。')
    extra_indexes = [index for index in range(len(header))
                     if index not in field_indexes.values()]
    unknown = tuple(header[index].strip().lstrip('\ufeff') for index in extra_indexes)
    if errors:
        return result(mapping=mapping, errors=errors, unknown=unknown)

    known = {company.name.casefold() for company in ledger.companies.values()}
    seen = set()
    rows = []
    for number, raw in numbered[1:]:
        if len(raw) != len(header):
            errors.append(f'第{number}行：列数与表头不一致。')
            continue
        row = {field: raw[index].strip() for field, index in field_indexes.items()}
        name = row['name']
        if not name:
            errors.append(f'第{number}行：公司名称为空。')
        elif name.casefold() in seen or name.casefold() in known:
            errors.append(f'第{number}行：公司名称重复或已在长期清单（{name}）。')
        else:
            seen.add(name.casefold())
        source_time = row.get('source_updated_at', '')
        if source_time:
            try:
                explicit_time(source_time)
            except ValueError:
                errors.append(f'第{number}行：来源更新时间须为含时区的 ISO 时间。')
        row['unverified_details'] = {
            unknown_name: raw[index].strip()
            for unknown_name, index in zip(unknown, extra_indexes)
        }
        row['verification_state'] = 'unverified'
        rows.append(row)
    if not rows and not errors:
        errors.append('未解析到任何公司数据行。')
    return result(mapping=mapping, rows=rows, errors=errors, unknown=unknown)
