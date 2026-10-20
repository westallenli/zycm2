# -*- coding: utf-8 -*-
"""组织架构原图像素锚点标定工具（开发期，PIL+numpy；运行时不依赖本文件）。

原理（确定性，无 AI）：
1. 灰度二值化（暗像素 = 边框/文字墨迹）；
2. 提取长水平/垂直墨迹段（框边线；文字笔画长度不足阈值自然排除）；
3. 成对水平段 + 左右垂直段闭合 → 候选矩形（即组织框）；
4. 去重后输出编号调试图 + 原始矩形 JSON，供人工定名后合成 org_anchors.json。

用法：
    python tools/calibrate_org_anchors.py detect   # 检测 → debug 图 + rects json
    python tools/calibrate_org_anchors.py build    # rects + 人工定名 → org_anchors.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
IMG_PATH = ROOT / "impl" / "static" / "org_chart.png"
DEBUG_PATH = ROOT / "impl" / "static" / "_anchors_debug.png"
RECTS_PATH = ROOT / "tools" / "_detected_rects.json"
NAMES_PATH = ROOT / "tools" / "_anchor_names.json"
OUT_PATH = ROOT / "impl" / "static" / "org_anchors.json"

MIN_HSEG = 30      # 水平边线最短像素
MIN_VSEG = 14      # 垂直边线最短像素
MIN_BOX_H = 16     # 框高下限
MAX_BOX_H = 140    # 框高上限
TOL = 4            # 对齐容差

# 角标空白检测参数：候选序 上方→下方→右侧→框内顶部→框内底部；
# 全被占用则不产角标（badge_rect=null，fail-closed）。
# 参数由 2026-08-24 对原图的确定性扫描定型：全宽大角标会撞邻框，
# 半宽居中 + 框内墨迹空白条可达 39/40 覆盖，剩余 1 框诚实无角标。
BADGE_H = 12       # 角标条高度（像素）
BADGE_GAP = 3      # 与框边缘的间距
BADGE_W_FACTOR = 0.5   # 角标宽度 = 框宽 × 系数
BADGE_W_CAP = 90   # 角标最大宽度（像素）
INK_RATIO_MAX = 0.01   # 区域暗像素占比超过 1% 视为被墨迹占用


def _runs(dark_line: np.ndarray, min_len: int):
    """一行/列上的暗像素连续段 [(start, end_exclusive), ...]。"""
    d = np.diff(dark_line.astype(np.int8))
    starts = np.where(d == 1)[0] + 1
    ends = np.where(d == -1)[0] + 1
    if dark_line[0]:
        starts = np.concatenate(([0], starts))
    if dark_line[-1]:
        ends = np.concatenate((ends, [len(dark_line)]))
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s >= min_len]


def detect_rects(arr: np.ndarray) -> list[list[int]]:
    dark = arr < 140
    h, w = dark.shape
    hsegs: list[tuple[int, int, int]] = []
    for y in range(h):
        for s, e in _runs(dark[y], MIN_HSEG):
            hsegs.append((y, s, e))
    vsegs: list[tuple[int, int, int]] = []
    for x in range(w):
        for s, e in _runs(dark[:, x], MIN_VSEG):
            vsegs.append((x, s, e))
    # 垂直段按 x 建桶，加速配对
    by_x: dict[int, list[tuple[int, int]]] = {}
    for x, y1, y2 in vsegs:
        by_x.setdefault(x, []).append((y1, y2))

    def vspan(x: int, ytop: int, ybot: int) -> bool:
        for dx in range(-TOL, TOL + 1):
            for y1, y2 in by_x.get(x + dx, ()):
                if y1 <= ytop + TOL and y2 >= ybot - TOL:
                    return True
        return False

    cands: list[tuple[int, int, int, int]] = []
    n = len(hsegs)
    for i in range(n):
        y1, s1, e1 = hsegs[i]
        for j in range(i + 1, n):
            y2, s2, e2 = hsegs[j]
            box_h = y2 - y1
            if box_h < MIN_BOX_H or box_h > MAX_BOX_H:
                continue
            if abs(s1 - s2) > TOL or abs(e1 - e2) > TOL:
                continue
            if vspan(s1, y1, y2) and vspan(e1, y1, y2):
                cands.append((s1, y1, e1, y2))

    # 去重：边线有厚度，同一框会被相邻行重复检出
    cands.sort()
    rects: list[list[int]] = []
    for x1, y1, x2, y2 in cands:
        dup = False
        for r in rects:
            if abs(r[0] - x1) <= TOL and abs(r[1] - y1) <= TOL \
                    and abs(r[2] - x2) <= TOL and abs(r[3] - y2) <= TOL:
                dup = True
                break
        if not dup:
            rects.append([x1, y1, x2, y2])
    rects.sort(key=lambda r: (r[1], r[0]))
    return rects


def _zone_blank(dark: np.ndarray, zone: tuple[int, int, int, int],
                rects: list[list[int]], own: int) -> bool:
    """候选角标区必须完全在图内、不与其他框相交、且几乎无墨迹。"""
    x1, y1, x2, y2 = zone
    h, w = dark.shape
    if x1 < 0 or y1 < 0 or x2 > w or y2 > h or x2 <= x1 or y2 <= y1:
        return False
    for j, (ox1, oy1, ox2, oy2) in enumerate(rects):
        if j == own:
            continue
        if not (x2 < ox1 - 1 or x1 > ox2 + 1 or y2 < oy1 - 1 or y1 > oy2 + 1):
            return False
    region = dark[y1:y2, x1:x2]
    if region.size == 0:
        return False
    return float(region.mean()) <= INK_RATIO_MAX


def badge_rects(arr: np.ndarray,
                rects: list[list[int]],
                ) -> list[tuple[int, int, int, int] | None]:
    """为每个框找确定性安全的角标位：上→下→右→框内顶→框内底。

    只有经墨迹检测确认的空白区才允许放角标（不遮挡原图文字）；
    全部占用时返回 None，运行时该框不渲染角标（点击区域不受影响）。
    """
    dark = arr < 140
    out: list[tuple[int, int, int, int] | None] = []
    for i, (x1, y1, x2, y2) in enumerate(rects):
        bw = min(int((x2 - x1) * BADGE_W_FACTOR), BADGE_W_CAP)
        cx = (x1 + x2) // 2
        ax1, ax2 = cx - bw // 2, cx + bw // 2
        cy = (y1 + y2) // 2
        candidates = [
            (ax1, y1 - BADGE_GAP - BADGE_H, ax2, y1 - BADGE_GAP),           # 上方居中
            (ax1, y2 + BADGE_GAP, ax2, y2 + BADGE_GAP + BADGE_H),          # 下方居中
            (x2 + BADGE_GAP, cy - BADGE_H // 2,
             x2 + BADGE_GAP + bw, cy + BADGE_H // 2),                        # 右侧
            (x1 + 2, y1 + 2, x1 + 2 + bw, y1 + 2 + BADGE_H),               # 框内顶部
            (x1 + 2, y2 - 2 - BADGE_H, x1 + 2 + bw, y2 - 2),               # 框内底部
        ]
        picked = None
        for zone in candidates:
            if _zone_blank(dark, zone, rects, i):
                picked = zone
                break
        out.append(picked)
    return out


def cmd_detect() -> None:
    img = Image.open(IMG_PATH)
    arr = np.asarray(img.convert("L"))
    rects = detect_rects(arr)
    debug = img.convert("RGB")
    draw = ImageDraw.Draw(debug)
    for idx, (x1, y1, x2, y2) in enumerate(rects):
        draw.rectangle([x1, y1, x2, y2], outline=(220, 0, 0), width=1)
        draw.rectangle([x1, y1 - 12, x1 + 18, y1], fill=(220, 0, 0))
        draw.text((x1 + 3, y1 - 11), str(idx), fill=(255, 255, 255))
    debug.save(DEBUG_PATH)
    payload = {
        "image": str(IMG_PATH),
        "size": [img.width, img.height],
        "count": len(rects),
        "rects": rects,
    }
    RECTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    print(f"detected {len(rects)} rects -> {DEBUG_PATH.name}, {RECTS_PATH.name}")


def cmd_build() -> None:
    rects_doc = json.loads(RECTS_PATH.read_text(encoding="utf-8"))
    names = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    rects = rects_doc["rects"]
    if len(names) != len(rects):
        raise SystemExit(f"定名数量 {len(names)} != 检测矩形数 {len(rects)}")
    w, h = rects_doc["size"]
    img = Image.open(IMG_PATH)
    if (img.width, img.height) != (w, h):
        raise SystemExit(
            f"原图尺寸变化：记录 {(w, h)} != 实际 {(img.width, img.height)}，"
            "请重跑 detect 后再 build"
        )
    badges = badge_rects(np.asarray(img.convert("L")), rects)
    entries = []
    for idx, rect in enumerate(rects):
        meta = names[str(idx)]
        # 归一化顺序固定为 [x1, y1, x2, y2]，与运行时约定一致；
        # 旧写法 rect[::2]+rect[1::2] 会产出 [x1, x2, y1, y2]，已废弃。
        norm_rect = [
            round(rect[0] / w, 5),
            round(rect[1] / h, 5),
            round(rect[2] / w, 5),
            round(rect[3] / h, 5),
        ]
        badge = badges[idx]
        entry = {
            "kind": meta["kind"],
            "code": meta["code"],
            "label": meta["label"],
            "rect": norm_rect,
            "badge_rect": (
                [
                    round(badge[0] / w, 5),
                    round(badge[1] / h, 5),
                    round(badge[2] / w, 5),
                    round(badge[3] / h, 5),
                ]
                if badge is not None else None
            ),
        }
        entries.append(entry)
    digest = hashlib.sha256(IMG_PATH.read_bytes()).hexdigest()
    doc = {
        "version": "org-anchors-v1",
        "image": "org_chart.png",
        "image_sha256": digest,
        "image_size": [w, h],
        "boxes": entries,
    }
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"built {len(entries)} anchors -> {OUT_PATH.name} (sha256 {digest[:12]}…)")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "detect":
        cmd_detect()
    elif cmd == "build":
        cmd_build()
    else:
        raise SystemExit("usage: calibrate_org_anchors.py detect|build")


if __name__ == "__main__":
    main()
