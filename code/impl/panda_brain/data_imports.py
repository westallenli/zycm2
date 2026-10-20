"""P16-T18 文件导入流水线（定版 04 号 §8）。

流程口径：上传 → 解析 → 字段映射 → 预览 → 查重/查错 → 生成草稿 → 授权发布 → 更新长期库；
失败态携带逐行错误清单，可重导。已发布批次保留回滚快照（受影响集合的发布前状态），
支持一键回滚与版本对比（发布前后条目数与哈希摘要）。

纪律：
- 运行时纯标准库；支持 TXT / CSV 粘贴导入。Word / Excel / PDF / 图片表格需要外部
  解析引擎（含 OCR），本模块在入口显式拒绝并标注「待接入」，不做假解析。
- 原始文件名、内容 SHA-256、上传人/时间、数据类型、错误清单、导入版本、发布人/时间
  全程留痕（§8.2）；未通过校验的批次绝不写长期库。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field

from .models import PersonRecord
from .org_model import normalize_phone

# ---------------------------------------------------------------------------
# 状态机（§8.1）：已上传 → 解析中 → 待映射 → 预览 → 已生成草稿 → 已发布；失败可重导。
# 「解析中/待映射」为上传请求内的瞬时步，落盘只保留可停留的三个稳态加终态。
# ---------------------------------------------------------------------------

STATUS_PREVIEW = "预览"          # 解析+自动映射完成，等待确认
STATUS_DRAFT = "已生成草稿"       # 查重/查错通过，等待授权发布
STATUS_PUBLISHED = "已发布"       # 已写入长期库
STATUS_FAILED = "失败"           # 有错误清单，可重导

TEXT_SUFFIXES = (".csv", ".txt")

def _norm_key(name: str) -> str:
    return (name or "").strip().lstrip("\ufeff").lower()


@dataclass
class DataTypeSpec:
    """一类导入对象的列名自动识别表与必填约束（§8.3）。"""

    key: str
    label: str
    aliases: dict[str, tuple[str, ...]]          # 目标字段 -> 可识别列名
    required: tuple[str, ...]                    # 每行必填字段
    dedupe_field: str = ""                       # 行间查重字段（如手机号）


DATA_TYPES: dict[str, DataTypeSpec] = {
    "personnel": DataTypeSpec(
        "personnel", "人员信息表",
        {"name": ("姓名", "名字"), "phone": ("手机号", "电话", "联系电话"),
         "unit": ("单位",), "home_position": ("岗位", "本职岗位", "工作组"),
         "specialty": ("专长", "专业"), "is_leader": ("是否队长", "队长")},
        required=("name", "phone"), dedupe_field="phone",
    ),
    "squad": DataTypeSpec(
        "squad", "中队编制",
        {"name": ("中队", "中队名称", "名称"),
         "member_phones": ("成员手机号", "手机号", "成员电话"),
         "headcount_planned": ("编制数", "人数")},
        required=("name",),
    ),
    "carry": DataTypeSpec(
        "carry", "中队携带物资",
        {"squad": ("中队", "中队名称"), "item_name": ("物资名称", "物资"),
         "quantity": ("数量",), "unit": ("单位",)},
        required=("squad", "item_name", "quantity"),
    ),
    "supply_point": DataTypeSpec(
        "supply_point", "物资点清单",
        {"name": ("物资点", "名称", "地点名称"), "location": ("位置", "地址"),
         "org": ("所属单位", "单位"), "contact_name": ("联系人",),
         "contact_phone": ("联系电话", "电话")},
        required=("name",), dedupe_field="name",
    ),
    "stock": DataTypeSpec(
        "stock", "物资点库存",
        {"point": ("物资点", "地点名称"), "item_name": ("物资名称", "物资"),
         "quantity": ("数量", "库存")},
        required=("point", "item_name", "quantity"),
    ),
}
for _spec in DATA_TYPES.values():
    assert _spec.key in DATA_TYPES, _spec.key


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 批次对象
# ---------------------------------------------------------------------------

@dataclass
class ImportBatch:
    batch_id: str
    data_type: str                 # DATA_TYPES 键
    filename: str
    file_kind: str                 # csv / txt / rejected:<原后缀>
    content_sha256: str
    uploaded_by: str
    uploaded_at: str
    status: str = STATUS_PREVIEW
    errors: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)     # 目标字段 -> 源列名
    rows: list[dict] = field(default_factory=list)            # 映射后的预览行
    version: int = 0                                          # 发布版本号
    published_by: str = ""
    published_at: str = ""
    undo_snapshot: dict | None = None      # 发布前受影响集合（回滚用）
    post_digest: str = ""                  # 发布后条目数摘要（版本对比用）
    rolled_back: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "ImportBatch":
        allowed = {f for f in ImportBatch.__dataclass_fields__}
        return ImportBatch(**{k: v for k, v in d.items() if k in allowed})


class ImportLedger:
    """导入批次台账（JSON 持久化，与 registry/materials 同构）。"""

    def __init__(self) -> None:
        self.batches: list[ImportBatch] = []
        self._seq = 0

    def new_batch(self, **kwargs) -> ImportBatch:
        self._seq += 1
        batch = ImportBatch(batch_id=f"imp-{self._seq:03d}", **kwargs)
        self.batches.append(batch)
        return batch

    def get(self, batch_id: str) -> ImportBatch | None:
        for b in self.batches:
            if b.batch_id == batch_id:
                return b
        return None

    def to_dict(self) -> dict:
        return {"version": "imports-v1", "seq": self._seq,
                "batches": [b.to_dict() for b in self.batches]}

    @classmethod
    def load(cls, path) -> "ImportLedger":
        from pathlib import Path
        ledger = cls()
        target = Path(path)
        if not target.exists():
            return ledger
        payload = json.loads(target.read_text(encoding="utf-8"))
        ledger._seq = int(payload.get("seq", 0))
        for item in payload.get("batches", []):
            ledger.batches.append(ImportBatch.from_dict(item))
        return ledger

    def save(self, path) -> None:
        from .durable_storage import before_write
        before_write(path)
        from pathlib import Path
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        import os
        os.replace(tmp, target)


# ---------------------------------------------------------------------------
# 解析 + 字段映射 + 查错（上传即完成，产出 预览 或 失败）
# ---------------------------------------------------------------------------

def parse_and_map(data_type: str, text: str) -> tuple[dict[str, str], list[dict], list[str]]:
    """返回 (mapping, mapped_rows, errors)。errors 非空即整批失败（不部分入库）。"""
    spec = DATA_TYPES[data_type]
    reader = csv.reader(io.StringIO(text or ""))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return {}, [], ["文件为空或未检测到数据行。"]
    header = [_norm_key(c) for c in rows[0]]
    mapping: dict[str, str] = {}
    for fld, aliases in spec.aliases.items():
        norm_aliases = {_norm_key(a) for a in aliases}
        for col, h in zip(rows[0], header):
            if h in norm_aliases and col not in mapping.values():
                mapping[fld] = col.strip()
                break
    missing = [f for f in spec.required if f not in mapping]
    if missing:
        labels = "、".join(missing)
        return mapping, [], [f"缺少必需列（{labels}）；已识别列：{', '.join(rows[0]) or '—'}"]
    idx = {fld: rows[0].index(name) for fld, name in mapping.items()}
    errors: list[str] = []
    seen: set[str] = set()
    out: list[dict] = []
    for lineno, raw in enumerate(rows[1:], start=2):
        def cell(fld: str) -> str:
            i = idx.get(fld, -1)
            return (raw[i].strip() if 0 <= i < len(raw) else "")
        row = {fld: cell(fld) for fld in idx}
        for fld in spec.required:
            if not row.get(fld):
                errors.append(f"第{lineno}行：{spec_label_field(spec, fld)}为空。")
        if data_type == "personnel" and row.get("phone"):
            try:
                phone = normalize_phone(row["phone"])
            except ValueError:
                errors.append(f"第{lineno}行：手机号格式错（{row['phone']}）。")
                out.append(row)
                continue
            if phone in seen:
                errors.append(f"第{lineno}行：重复手机号（{phone}）。")
            else:
                seen.add(phone)
            row["phone"] = phone
        if data_type == "supply_point" and row.get("contact_phone"):
            try:
                row["contact_phone"] = normalize_phone(row["contact_phone"])
            except ValueError:
                errors.append(f"第{lineno}行：联系电话格式错（{row['contact_phone']}）。")
        leader = row.get("is_leader", "")
        if "is_leader" in row:
            row["is_leader"] = "1" if leader in ("是", "y", "Y", "true", "1") else "0"
        qty = row.get("quantity", "")
        if "quantity" in idx:
            try:
                float(str(qty).replace(",", ""))
            except ValueError:
                errors.append(f"第{lineno}行：数量非数字（{qty}）。")
        if spec.dedupe_field == "name" and row.get("name"):
            if row["name"] in seen:
                errors.append(f"第{lineno}行：重复{name_label(spec)}（{row['name']}）。")
            seen.add(row.get("name", ""))
        out.append(row)
    if not out:
        errors.append("未解析到任何数据行。")
    return mapping, out, errors


def spec_label_field(spec: DataTypeSpec, fld: str) -> str:
    zh = {"name": "名称/姓名", "phone": "手机号", "squad": "中队", "item_name": "物资名称",
          "point": "物资点", "quantity": "数量"}
    return zh.get(fld, fld)


def name_label(spec: DataTypeSpec) -> str:
    return "物资点" if spec.key == "supply_point" else "名称"


# ---------------------------------------------------------------------------
# 草稿校验（对照当前长期库）→ 发布 → 回滚
# ---------------------------------------------------------------------------

def validate_draft(data_type: str, rows: list[dict], registry, ledger) -> list[str]:
    """生成草稿前的库级查重/查错（§8.3：中队无法匹配、重复物资等）。"""
    errors: list[str] = []
    if data_type == "squad":
        for i, r in enumerate(rows, 1):
            phones = [p for p in re.split(r"[;；,，\s]+", r.get("member_phones", "")) if p]
            unknown = [p for p in phones if normalize_phone(p) not in registry.library]
            if unknown:
                errors.append(f"第{i}行：成员不在长期人员库（{', '.join(unknown)}）。")
    elif data_type == "carry":
        squad_ids = {s.name: s.squad_id for s in ledger.squads.values()}
        item_ids = {it.name: it.item_id for it in ledger.items.values()}
        for i, r in enumerate(rows, 1):
            if r.get("squad") not in squad_ids:
                errors.append(f"第{i}行：中队无法匹配（{r.get('squad')}）。")
            if r.get("item_name") not in item_ids:
                errors.append(f"第{i}行：物资未建档（{r.get('item_name')}）。")
    elif data_type == "stock":
        points = {p.name: p.point_id for p in ledger.points.values()}
        items = {it.name: it.item_id for it in ledger.items.values()}
        for i, r in enumerate(rows, 1):
            if r.get("point") not in points:
                errors.append(f"第{i}行：物资点无法匹配（{r.get('point')}）。")
            if r.get("item_name") not in items:
                errors.append(f"第{i}行：物资未建档（{r.get('item_name')}）。")
    return errors


def _digest(counts: dict[str, int]) -> str:
    return sha256_text(json.dumps(counts, sort_keys=True, ensure_ascii=False))[:12]


def publish(batch: ImportBatch, registry, ledger) -> None:
    """授权发布：写入长期库并留存回滚快照。调用方负责保存两侧 JSON。"""
    if batch.status != STATUS_DRAFT:
        raise ValueError("只有「已生成草稿」的批次可以发布。")
    dt = batch.data_type
    rows = batch.rows
    if dt == "personnel":
        batch.undo_snapshot = {"library": {p: vars(r) for p, r in registry.library.items()}}
        records = [PersonRecord(name=r["name"], phone=r["phone"], unit=r.get("unit", ""),
                                home_position=r.get("home_position", ""),
                                specialty=r.get("specialty", ""),
                                is_leader=r.get("is_leader") == "1") for r in rows]
        registry.import_person_library(records)
    elif dt == "squad":
        batch.undo_snapshot = {"squads": {s.squad_id: vars(s) for s in ledger.squads.values()}}
        for r in rows:
            phones = tuple(normalize_phone(p) for p in
                            re.split(r"[;；,，\s]+", r.get("member_phones", "")) if p)
            hc = int(float(r.get("headcount_planned") or 0))
            existing = next((s for s in ledger.squads.values() if s.name == r["name"]), None)
            if existing is not None:
                existing.member_phones = phones
                existing.headcount_planned = hc or existing.headcount_planned
            else:
                ledger.add_squad(name=r["name"], member_phones=phones,
                                 headcount_planned=hc, library=registry.library)
    elif dt == "carry":
        batch.undo_snapshot = {"carry": {k: vars(v) for k, v in ledger.carry_lines.items()}}
        item_ids = {it.name: it.item_id for it in ledger.items.values()}
        squads = {s.name: s for s in ledger.squads.values()}
        for r in rows:
            sq = squads.get(r["squad"])
            ledger.set_carry_line(sq.squad_id, item_ids[r["item_name"]],
                                  quantity=float(r["quantity"]))
    elif dt == "supply_point":
        batch.undo_snapshot = {"points": {p.point_id: dict(vars(p))
                                          for p in ledger.points.values()}}
        for r in rows:
            existing = next((p for p in ledger.points.values() if p.name == r["name"]), None)
            if existing is not None:
                for k in ("location", "org", "contact_name", "contact_phone"):
                    if r.get(k):
                        setattr(existing, k, r[k])
            else:
                ledger.add_supply_point(name=r["name"], location=r.get("location", ""),
                                        org=r.get("org", ""),
                                        contact_name=r.get("contact_name", ""),
                                        contact_phone=r.get("contact_phone", ""))
    elif dt == "stock":
        batch.undo_snapshot = {"stocks": {p.point_id: dict(p.supplies)
                                          for p in ledger.points.values()}}
        points = {p.name: p for p in ledger.points.values()}
        item_ids = {it.name: it.item_id for it in ledger.items.values()}
        for r in rows:
            points[r["point"]].supplies[item_ids[r["item_name"]]] = float(r["quantity"])
    batch.version += 1
    batch.post_digest = _digest({
        "persons": len(registry.library), "squads": len(ledger.squads),
        "items": len(ledger.items), "points": len(ledger.points)})


def rollback(batch: ImportBatch, registry, ledger) -> None:
    """回滚：恢复发布前快照（整体替换受影响集合）。"""
    snap = batch.undo_snapshot
    if not snap:
        raise ValueError("该批次没有可用的回滚快照。")
    if "library" in snap:
        registry.library.clear()
        for phone, d in snap["library"].items():
            registry.library[phone] = PersonRecord(**d)
    if "squads" in snap:
        from .materials import Squad
        ledger.squads.clear()
        for sid, d in snap["squads"].items():
            ledger.squads[sid] = Squad(**d)
    if "carry" in snap:
        from .materials import CarryLine
        ledger.carry_lines.clear()
        for key, d in snap["carry"].items():
            ledger.carry_lines[key] = CarryLine(**d)
    if "points" in snap:
        from .materials import SupplyPoint
        ledger.points.clear()
        for pid, d in snap["points"].items():
            d.pop("stock_word", None)
            ledger.points[pid] = SupplyPoint(**d)
    if "stocks" in snap:
        for pid, supplies in snap["stocks"].items():
            if pid in ledger.points:
                ledger.points[pid].supplies = dict(supplies)
    batch.rolled_back = True
    batch.status = STATUS_DRAFT   # 回滚后回到草稿态，可修正后重新发布
