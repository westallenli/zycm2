"""无人机影像导入与配准域层（P16-T8，03 号定版 §9）。

纯逻辑，无 HTTP 无 HTML。

状态机（doc 03 §9.1）::

    已上传 → 待配准（无空间信息）/ 已定位（有空间信息）
    待配准 → 已定位（人工配准：登记空间范围）
    已定位 → 已叠加 → 智脑分析中 → 已生成候选 → 已确认/已驳回

硬纪律：
- 未配准图像只能是「未配准证据」——不得进入智脑分析，
  更不得作为正式火线/路线判断依据；
- 智脑分析产出的候选标注走既有标绘核验链（pending_review 入库），
  本模块不直接产生正式态势对象；
- 驳回保留原图与分析记录（不物理删除）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path


class ImageryError(ValueError):
    """业务错误；消息可直接展示。"""


#: 允许的图像类型（doc 03 §9：JPG/PNG/带范围影像/截图；GeoTIFF 等预留）。
ALLOWED_TYPES = ("jpg", "jpeg", "png")
#: 上传体积上限（字节）：8MB 足够现场照片，防止把上传当存储用。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

STATUS_WORDS = {
    "uploaded": "已上传",
    "awaiting_registration": "待配准",
    "located": "已定位",
    "overlaid": "已叠加",
    "analyzing": "处理中",
    "candidate_generated": "已生成候选",
    "confirmed": "已确认",
    "rejected": "已驳回",
}

_TRANSITIONS: dict[str, set[str]] = {
    "uploaded": {"awaiting_registration", "located"},
    "awaiting_registration": {"located"},
    "located": {"overlaid"},
    "overlaid": {"analyzing"},
    "analyzing": {"candidate_generated"},
    "candidate_generated": {"confirmed", "rejected"},
    "confirmed": set(),
    "rejected": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, set())


@dataclass
class DroneImage:
    """图像记录（doc 03 §9 字段表）。"""

    image_id: str
    event_id: str
    source_system: str          # 来源系统：无人机/其他系统名称
    collected_by: str           # 采集单位
    collected_at: str           # 采集时间 ISO
    uploaded_at: str            # 上传时间 ISO
    image_type: str             # jpg/png/screenshot…
    spatial_range: str = ""     # 空间范围描述（配准后必填）
    geo_precision: str = ""     # 定位精度说明
    status: str = "uploaded"
    analyzed: bool = False      # 是否已经过智脑分析
    note: str = ""
    file_path: str = ""         # 相对静态目录路径；空＝仅登记元数据
    related_annotation_id: str = ""  # 分析产出的候选标注 id

    def status_word(self) -> str:
        return STATUS_WORDS.get(self.status, self.status)

    @property
    def registered(self) -> bool:
        """是否已完成空间定位（待配准及之前一律视为未配准证据）。"""
        return self.status in {
            "located", "overlaid", "analyzing",
            "candidate_generated", "confirmed",
        }


class ImageryLedger:
    """无人机影像底账；独立 JSON 分库（同物资/复盘底账模式）。"""

    def __init__(self) -> None:
        self.images: dict[str, DroneImage] = {}
        self._seq: int = 0

    # -- 写入 ------------------------------------------------------------

    def add_image(
        self,
        *,
        event_id: str,
        source_system: str,
        collected_by: str,
        collected_at: str,
        uploaded_at: str,
        image_type: str,
        has_spatial_info: bool,
        spatial_range: str = "",
        geo_precision: str = "",
        file_path: str = "",
        note: str = "",
    ) -> DroneImage:
        if not (source_system or "").strip():
            raise ImageryError("来源系统不能为空（如：XX 型无人机 / 护林通）")
        if not (collected_at or "").strip():
            raise ImageryError("采集时间不能为空")
        self._seq += 1
        image_id = f"IMG-{event_id or 'none'}-{self._seq:03d}"
        status = "located" if has_spatial_info else "awaiting_registration"
        if has_spatial_info and not (spatial_range or "").strip():
            # 有空间信息却没给范围＝数据不完整，按未配准处理并明示原因
            status = "awaiting_registration"
        image = DroneImage(
            image_id=image_id,
            event_id=event_id,
            source_system=source_system.strip(),
            collected_by=(collected_by or "").strip(),
            collected_at=collected_at.strip(),
            uploaded_at=uploaded_at.strip(),
            image_type=image_type.strip().lower(),
            spatial_range=spatial_range.strip(),
            geo_precision=geo_precision.strip(),
            status=status,
            file_path=file_path,
            note=note.strip(),
        )
        self.images[image_id] = image
        return image

    def transition(self, image_id: str, target: str, *,
                   operator: str = "", spatial_range: str = "") -> DroneImage:
        image = self._get(image_id)
        if target == image.status:
            return image
        if not can_transition(image.status, target):
            raise ImageryError(
                f"图像状态不允许从 {image.status_word()} 变为 "
                f"{STATUS_WORDS.get(target, target)}"
            )
        if target == "located":
            merged = (spatial_range or "").strip() or image.spatial_range
            if not merged:
                raise ImageryError("人工配准必须提供空间范围（如四至坐标或覆盖描述）")
            image = replace(image, spatial_range=merged)
        updated = replace(image, status=target)
        self.images[image_id] = updated
        return updated

    def mark_analyzed(self, image_id: str) -> DroneImage:
        image = self._get(image_id)
        if not image.registered:
            raise ImageryError(
                f"{image.image_id} 尚未完成空间配准——未配准证据不得进入分析"
            )
        updated = replace(image, analyzed=True)
        self.images[image_id] = updated
        return updated

    def link_annotation(self, image_id: str, annotation_id: str) -> DroneImage:
        image = self._get(image_id)
        updated = replace(image, related_annotation_id=annotation_id)
        self.images[image_id] = updated
        return updated

    def get(self, image_id: str) -> DroneImage:
        return self._get(image_id)

    def for_event(self, event_id: str) -> list[DroneImage]:
        return sorted(
            (i for i in self.images.values() if i.event_id == event_id),
            key=lambda i: i.image_id,
        )

    # -- io ---------------------------------------------------------------

    @staticmethod
    def _get_image(images: dict, key: str) -> DroneImage:
        obj = images.get(key)
        if obj is None:
            raise ImageryError(f"图像不存在：{key}")
        return obj

    def _get(self, image_id: str) -> DroneImage:
        return self._get_image(self.images, image_id)

    def save(self, path: str | Path) -> None:
        from .durable_storage import before_write
        before_write(path)
        data = {
            "seq": self._seq,
            "images": {k: asdict(v) for k, v in self.images.items()},
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)

    @classmethod
    def load(cls, path: str | Path) -> "ImageryLedger":
        ledger = cls()
        target = Path(path)
        if not target.exists():
            return ledger
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ledger  # 损坏文件按空底账起步，不伪造内容
        ledger._seq = int(data.get("seq") or 0)
        for key, raw in (data.get("images") or {}).items():
            ledger.images[key] = DroneImage(**raw)
        return ledger


# ---------------------------------------------------------------------------
# 文件名与类型安全


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_upload_name(filename: str) -> str:
    """清洗上传文件名：保留字母数字点横线下划线，防路径穿越。"""
    base = _SAFE_NAME_RE.sub("-", Path(filename or "").name)
    base = base.lstrip(".") or "upload"
    suffix = Path(base).suffix.lower().lstrip(".")
    if suffix and suffix not in ALLOWED_TYPES:
        raise ImageryError(
            f"暂不支持 .{suffix} 类型（当前支持 {'/'.join(ALLOWED_TYPES)}；"
            "GeoTIFF/正射/热成像/视频流为后续预留）"
        )
    return base


def sniff_image_type(payload: bytes) -> str | None:
    """魔数嗅探：只认 jpg/png；认不出返回 None 由调用方显式报错。"""
    if payload[:3] == b"\xff\xd8\xff":
        return "jpg"
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None
