"""本地离线瓦片包只读服务（docs/19 口径）。

约定：把瓦片放到 ``impl/data/offline_map/`` 下即可自动识别，两种形态——

1. XYZ 目录：``offline_map/tiles/{z}/{x}/{y}.png``（或 .jpg/.jpeg/.webp；
   也兼容省略 ``tiles/`` 层的 ``offline_map/{z}/{x}/{y}.png``）；
2. MBTiles：``offline_map/<任意名>.mbtiles``（SQLite，TMS 行序自动翻转）。

只读、零第三方依赖、fail-closed：路径不合法或瓦片不存在一律 None，
由调用方回 404；绝不做目录穿越。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

OFFLINE_ROOT = Path(__file__).resolve().parents[1] / "data" / "offline_map"

_TILE_SUFFIX = re.compile(r"\.(png|jpe?g|webp)$", re.IGNORECASE)
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp"}
_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_REL = re.compile(r"^(\d{1,2})/(\d{1,8})/(\d{1,8})\.(?:png|jpe?g|webp)$", re.IGNORECASE)

_conn: sqlite3.Connection | None = None
_conn_path: str = ""


def _mbtiles() -> Path | None:
    if not OFFLINE_ROOT.is_dir():
        return None
    for cand in sorted(OFFLINE_ROOT.glob("*.mbtiles")):
        if cand.is_file():
            return cand
    return None


def _tiles_dirs() -> tuple[Path, Path]:
    return (OFFLINE_ROOT / "tiles", OFFLINE_ROOT)


def available() -> bool:
    """离线包是否可用（渲染页面前探测一次即可）：tiles/ 目录或任一 .mbtiles。"""
    if not OFFLINE_ROOT.is_dir():
        return False
    return (OFFLINE_ROOT / "tiles").is_dir() or _mbtiles() is not None


def _from_dir(z: int, x: int, y: int) -> tuple[bytes, str] | None:
    a, b = _tiles_dirs()
    for base in (a, b):
        if not base.is_dir():
            continue
        for ext in _EXTS:
            f = base / str(z) / str(x) / f"{y}{ext}"
            if f.is_file():
                return f.read_bytes(), _MIME[ext]
    return None


def _db(path: Path) -> sqlite3.Connection | None:
    global _conn, _conn_path
    key = str(path)
    if _conn is not None and _conn_path == key:
        return _conn
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    _conn, _conn_path = conn, key
    return conn


def _sniff(blob: bytes) -> str:
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:2] == b"\xff\xd8":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _from_mbtiles(z: int, x: int, y: int) -> tuple[bytes, str] | None:
    path = _mbtiles()
    if path is None:
        return None
    conn = _db(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles"
            " WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, (1 << z) - 1 - y),  # XYZ → TMS 行序翻转
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    blob = bytes(row[0])
    return blob, _sniff(blob)


def tile(rel: str) -> tuple[bytes, str] | None:
    """按 ``{z}/{x}/{y}.png`` 相对路径取瓦片；不合法或不存在返回 None。"""
    rel = rel.replace("\\", "/").lstrip("/")
    m = _REL.match(rel)
    if not m:
        return None
    z, x, y = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if z > 22 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        return None
    hit = _from_dir(z, x, y)
    if hit is not None:
        return hit
    return _from_mbtiles(z, x, y)
