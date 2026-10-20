"""确定性规则推演（方案一，2026-08-26 对齐版）。

口径：
- 推演输入 = 已发布火线/火点（正式态势）＋方案推演几何（sim_shapes）；
- 预测火线 = 火线沿下风方位平移 速率×时长；火点按全向最大蔓延半径画圈；
- 对每个方案图形：火至时长 = 最近火点距离 ÷ 蔓延速率；开通耗时 = 线长 ÷ 单队效率；
  富余 = 火至 − 耗时 → 来得及 / 勉强 / 来不及；
- 目标圈（附加图例）：以火线/火点质心为圆心的固定风险半径，圈内重点目标标红。

所有常量是演示经验值，前台必须并列展示口径并标注「规则推演 · 非实测」；
本模块只读不改——推演层绝不写回正式态势（红线）。
"""

from __future__ import annotations

import math

RATE_M_PER_H = 120.0        # 下风侧蔓延速率（演示经验常量）
OPEN_M_PER_H = 60.0         # 隔离带/防线单队开通效率（演示经验常量）
STEPS_H = (2.0, 6.0, 12.0)  # 推演步长（小时）
SAFE_MARGIN_H = 1.0         # 富余 ≥1h 判「来得及」
RING_M = 1000.0             # 目标圈风险半径
BEARING_DEG = 90.0          # 风向：向东蔓延（演示常量）

FIRE_KINDS = ("fire_point", "fireline", "main_fireline", "hotspot", "fire_area")
TARGET_KINDS = ("water", "material", "headquarters", "protected_target",
                "water_source", "supply_point", "medical_point", "command_post")

CALIBER = (
    f"规则推演口径：下风侧 {RATE_M_PER_H:.0f} 米/时 · 开通 {OPEN_M_PER_H:.0f} 米/时·队 · "
    f"目标圈半径 {RING_M:.0f} 米 · 经验常量可调 · 非实测"
)


def haversine_m(a, b) -> float:
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(h))


def shift_pt(p, dist_m: float, bearing_deg: float = BEARING_DEG):
    lat, lon = p
    dra = dist_m * math.cos(math.radians(bearing_deg)) / 111320.0
    dlo = dist_m * math.sin(math.radians(bearing_deg)) / (
        111320.0 * math.cos(math.radians(lat)) or 1.0)
    return (round(lat + dra, 5), round(lon + dlo, 5))


def _path_len(pts, closed: bool) -> float:
    total = 0.0
    n = len(pts)
    if n < 2:
        return 0.0
    for i in range(n - 1):
        total += haversine_m(pts[i], pts[i + 1])
    if closed:
        total += haversine_m(pts[-1], pts[0])
    return total


def shape_verdict(pts, fronts, closed: bool = False) -> dict | None:
    """单图形：火至时长 vs 开通耗时 → 来得及/勉强/来不及。无火源返回 None。"""
    if not fronts or not pts:
        return None
    d = min(haversine_m(f, s) for f in fronts for s in pts)
    arrive_h = d / RATE_M_PER_H
    work_h = _path_len(pts, closed) / OPEN_M_PER_H
    margin = arrive_h - work_h
    if margin >= SAFE_MARGIN_H:
        word = "来得及"
    elif margin >= 0:
        word = "勉强"
    else:
        word = "来不及"
    return {
        "dist_m": round(d), "arrive_h": round(arrive_h, 1),
        "work_h": round(work_h, 1), "margin_h": round(margin, 1),
        "word": word,
    }


def _fire_marks(marks: list[dict]):
    fronts, lines, spots, centroids = [], [], [], []
    for m in marks:
        if not m.get("published") or m.get("kind") not in FIRE_KINDS:
            continue
        pts = [tuple(p) for p in m.get("pts") or ()]
        if not pts:
            continue
        fronts.extend(pts)
        centroids.append(pts)
        if m["kind"] in {"fireline", "main_fireline"}:
            lines.append(pts)
        else:
            spots.append(pts[0])
    return fronts, lines, spots, centroids


def project_steps(marks: list[dict]) -> list[dict]:
    """每档步长的预测火线（下风平移）与火点最大蔓延圈。"""
    _, lines, spots, _ = _fire_marks(marks)
    steps = []
    for t in STEPS_H:
        d = RATE_M_PER_H * t
        steps.append({
            "hours": t,
            "lines": [[list(shift_pt(p, d)) for p in ln] for ln in lines],
            "spots": [[float(spots[i][0]), float(spots[i][1])] for i in range(len(spots))],
            "r_m": round(d),
        })
    return steps


def rings_and_targets(marks: list[dict]) -> tuple[list[dict], list[dict]]:
    """目标圈（方案二作附加图例）：火源质心固定半径圈 + 圈内重点目标标红。"""
    _, _, _, centroids = _fire_marks(marks)
    rings = []
    for pts in centroids:
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        rings.append({"c": [round(lat, 5), round(lon, 5)], "r_m": RING_M})
    targets = []
    for m in marks:
        if not m.get("published") or m.get("kind") not in TARGET_KINDS:
            continue
        pts = [tuple(p) for p in m.get("pts") or ()]
        if not pts:
            continue
        c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        inside = any(haversine_m(c, r["c"]) <= r["r_m"] for r in rings)
        targets.append({
            "pt": [round(c[0], 5), round(c[1], 5)],
            "label": m.get("label", ""),
            "inside": inside,
        })
    return rings, targets


def enrich_sims(marks: list[dict], sims: list[dict]) -> dict:
    """给方案推演层补 verdict/一句话结论；返回推演上下文（含步长与目标圈）。

    sims 结构同 _map_payload：[{id, letter, title, shapes:[{geom,pts,label}]}]，
    就地写入 sh["verdict"] 与 sim["summary"]，返回 {"caliber", "steps", "rings", "targets"}。
    """
    fronts, *_ = _fire_marks(marks)
    for sim in sims:
        parts = []
        for sh in sim["shapes"]:
            v = shape_verdict(sh["pts"], fronts, closed=(sh.get("geom") == "polygon"))
            sh["verdict"] = v
            if v is None:
                continue
            label = sh.get("label") or "线段"
            parts.append(
                f"{label}：火至 {v['arrive_h']} 小时·作业 {v['work_h']} 小时→{v['word']}"
            )
        if not fronts:
            sim["summary"] = "推演暂不可用：无已发布火线/火点。"
        elif not parts:
            sim["summary"] = "推演层无定线对象，仅显示布防示意。"
        else:
            sim["summary"] = "；".join(parts)
    rings, targets = rings_and_targets(marks)
    return {
        "caliber": CALIBER,
        "steps": project_steps(marks),
        "rings": rings,
        "targets": targets,
    }
