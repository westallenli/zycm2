"""Deterministic fire calculus (P15 T2) — the staff "算" module.

Implements the handbook's four estimations (《灭火组织指挥手册》实施阶段·算):
火线长度 / 蔓延速度 / 灭火任务量 / 灭火效率, plus material requirements and a
doctrine filter for reinforcement routes (六突六不突 + 接近火线九不 + PRD-002
safest-path slope rule).

Everything here is deterministic and auditable: the same inputs must always
produce the same advice. These outputs are STAFF SUGGESTIONS — they never
activate orders and never touch command authority. The handbook's own worked
example is locked by tests as the ground-truth baseline; when the LLM layer
later proposes numbers, drifting from this engine is grounds for rejection
(same discipline as llm_enhance).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# 兵力部署测算：合围时间与所需分队


@dataclass(frozen=True)
class SuppressionEstimate:
    """One crew-count scenario of the net-closing-rate calculus."""

    crews: int
    fireline_length_m: float
    spread_rate_m_h: float
    crew_rate_m_h: float
    net_closing_rate_m_h: float
    hours_required: float | None  # None = 无法扑灭
    verdict: str                  # suppressible | unwinnable
    explanation: str


def estimate_suppression(
    fireline_length_m: float,
    spread_rate_m_h: float,
    *,
    crews: int,
    crew_rate_m_h: float = 100.0,
) -> SuppressionEstimate:
    """手册口径：合围时间 = 火线长 ÷ (分队数×分队灭火速度 − 蔓延速度)。

    净速度 ≤0 即判"无法扑灭"——灭火速度追不上蔓延速度时，火不可能被扑灭。
    """
    if fireline_length_m <= 0:
        raise ValueError("火线长度必须为正。")
    if spread_rate_m_h < 0:
        raise ValueError("蔓延速度不能为负。")
    if crews <= 0:
        raise ValueError("分队数必须为正。")
    if crew_rate_m_h <= 0:
        raise ValueError("分队灭火速度必须为正。")
    net = crews * crew_rate_m_h - spread_rate_m_h
    if net <= 0:
        return SuppressionEstimate(
            crews=crews,
            fireline_length_m=fireline_length_m,
            spread_rate_m_h=spread_rate_m_h,
            crew_rate_m_h=crew_rate_m_h,
            net_closing_rate_m_h=net,
            hours_required=None,
            verdict="unwinnable",
            explanation=(
                f"{crews} 个分队的灭火速度追不上蔓延速度（净合围 {net:g} 米/小时），"
                "按手册口径该火线无法扑灭，必须增兵或等待火势减弱。"
            ),
        )
    hours = fireline_length_m / net
    return SuppressionEstimate(
        crews=crews,
        fireline_length_m=fireline_length_m,
        spread_rate_m_h=spread_rate_m_h,
        crew_rate_m_h=crew_rate_m_h,
        net_closing_rate_m_h=net,
        hours_required=hours,
        verdict="suppressible",
        explanation=(
            f"{crews} 个分队净合围速度 {net:g} 米/小时，"
            f"预计 {hours:.1f} 小时内合围 {fireline_length_m:g} 米火线。"
        ),
    )


def crews_needed(
    fireline_length_m: float,
    spread_rate_m_h: float,
    *,
    target_hours: float,
    crew_rate_m_h: float = 100.0,
) -> int:
    """在 target_hours 内合围所需的最少分队数（向上取整，至少 1 个）。"""
    if target_hours <= 0:
        raise ValueError("目标时限必须为正。")
    raw = (fireline_length_m / target_hours + spread_rate_m_h) / crew_rate_m_h
    return max(1, math.ceil(raw))


def estimate_from_roster_headcount(
    assault_headcount: int,
    fireline_length_m: float,
    spread_rate_m_h: float,
    *,
    crew_standard_size: int = 30,
    crew_rate_m_h: float = 100.0,
) -> SuppressionEstimate:
    """把花名册上一线实到人数折算为标准分队（默认 30 人/分队，手册原例口径）。"""
    if assault_headcount < 0:
        raise ValueError("一线人数不能为负。")
    crews = max(1, math.ceil(assault_headcount / crew_standard_size)) if assault_headcount else 0
    if crews == 0:
        return SuppressionEstimate(
            crews=0,
            fireline_length_m=fireline_length_m,
            spread_rate_m_h=spread_rate_m_h,
            crew_rate_m_h=crew_rate_m_h,
            net_closing_rate_m_h=-spread_rate_m_h,
            hours_required=None,
            verdict="unwinnable",
            explanation="尚无一线作战人员到岗，无法组织扑打。",
        )
    return estimate_suppression(
        fireline_length_m,
        spread_rate_m_h,
        crews=crews,
        crew_rate_m_h=crew_rate_m_h,
    )


# ---------------------------------------------------------------------------
# 物资需求评估（基线查表，确定性）


class MaterialBaselineError(ValueError):
    """Raised when a material baseline file is malformed."""


@dataclass(frozen=True)
class MaterialBaseline:
    name: str
    basis: str
    crew_standard_size: int
    profiles: dict[str, dict]

    def profile(self, tactic_code: str) -> dict:
        try:
            return self.profiles[tactic_code]
        except KeyError:
            raise KeyError(
                f"未知战法物资档案：{tactic_code}（可用：{sorted(self.profiles)}）"
            ) from None


def load_material_baseline(path: str | Path) -> MaterialBaseline:
    file_path = Path(path)
    if not file_path.exists():
        raise MaterialBaselineError(f"物资基线文件不存在：{file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MaterialBaselineError(f"物资基线必须是映射：{file_path}")
    required = {"schema_version", "name", "basis", "crew_standard_size", "profiles"}
    missing = required - set(data)
    if missing:
        raise MaterialBaselineError(f"物资基线缺少键：{sorted(missing)}")
    unknown = set(data) - required
    if unknown:
        raise MaterialBaselineError(f"物资基线存在未知键：{sorted(unknown)}")
    profiles = data["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise MaterialBaselineError("profiles 必须是非空映射。")
    seen_names: set[str] = set()
    for code, profile in profiles.items():
        for key in ("name", "per_crew"):
            if key not in profile:
                raise MaterialBaselineError(f"战法档案 {code} 缺少 {key}。")
        if profile["name"] in seen_names:
            raise MaterialBaselineError(f"战法名称重复：{profile['name']}")
        seen_names.add(profile["name"])
        for entry in profile["per_crew"]:
            for key in ("item", "unit", "qty_per_crew"):
                if key not in entry:
                    raise MaterialBaselineError(f"战法档案 {code} 装备项缺少 {key}: {entry}")
            if not isinstance(entry["qty_per_crew"], (int, float)) or entry["qty_per_crew"] <= 0:
                raise MaterialBaselineError(f"战法档案 {code} 装备项数量必须为正: {entry}")
        for entry in profile.get("consumables_per_hour", ()):
            if not isinstance(entry.get("qty_per_hour"), (int, float)) or entry["qty_per_hour"] <= 0:
                raise MaterialBaselineError(f"战法档案 {code} 消耗项数量必须为正: {entry}")
    return MaterialBaseline(
        name=str(data["name"]),
        basis=str(data["basis"]),
        crew_standard_size=int(data["crew_standard_size"]),
        profiles=dict(profiles),
    )


def material_requirements(
    baseline: MaterialBaseline,
    tactic_code: str,
    *,
    crews: int,
    hours: float = 0.0,
) -> dict[str, object]:
    """按'装备随人走、消耗随时长'的确定性查表。"""
    if crews <= 0:
        raise ValueError("分队数必须为正。")
    if hours < 0:
        raise ValueError("时长不能为负。")
    profile = baseline.profile(tactic_code)
    equipment = tuple(
        {
            "item": entry["item"],
            "unit": entry["unit"],
            "qty": math.ceil(entry["qty_per_crew"] * crews),
        }
        for entry in profile["per_crew"]
    )
    consumables = tuple(
        {
            "item": entry["item"],
            "unit": entry["unit"],
            "qty": math.ceil(entry["qty_per_hour"] * hours),
        }
        for entry in profile.get("consumables_per_hour", ())
        if hours > 0
    )
    return {
        "tactic_code": tactic_code,
        "profile_name": str(profile["name"]),
        "crews": crews,
        "hours": hours,
        "equipment": equipment,
        "consumables": consumables,
        "basis": baseline.basis,
    }


# ---------------------------------------------------------------------------
# 增援路线条令过滤（几何由平台引擎计算，这里只做条令裁决）

_CLAUSE_TEXT: dict[str, str] = {
    "downhill_from_ridge": "六突六不突：从山下向上突破，不从山上向下处突破",
    "narrow_gully": "六突六不突：从宽平之地突破，不从窄沟狭谷处突破",
    "dense_young_forest": "六突六不突：从成林边缘突破，不从灌幼密林处突破",
    "no_cover": "六突六不突：从依托之处突破，不无依托冒险突入",
    "headwind_at_firehead": "接近火线九不：不逆风迎火头接近火线",
    "slope_gt_25": "路径安全规则：避开坡度大于25°地段（最安全路径口径）",
}


@dataclass(frozen=True)
class RouteCandidate:
    route_id: str
    name: str
    downhill_from_ridge: bool = False
    narrow_gully: bool = False
    dense_young_forest: bool = False
    no_cover: bool = False
    headwind_at_firehead: bool = False
    slope_gt_25: bool = False
    notes: str = ""

    def violations(self) -> tuple[str, ...]:
        flags = (
            "downhill_from_ridge",
            "narrow_gully",
            "dense_young_forest",
            "no_cover",
            "headwind_at_firehead",
            "slope_gt_25",
        )
        return tuple(_CLAUSE_TEXT[flag] for flag in flags if getattr(self, flag))


def filter_routes(candidates: tuple[RouteCandidate, ...] | list[RouteCandidate]) -> dict[str, object]:
    """条令过滤器：通过的路线附零违规背书，被拒路线逐条引用违反条款。"""
    passed: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for candidate in candidates:
        clauses = candidate.violations()
        if clauses:
            rejected.append(
                {
                    "route_id": candidate.route_id,
                    "name": candidate.name,
                    "violated_clauses": clauses,
                    "notes": candidate.notes,
                }
            )
        else:
            passed.append(
                {
                    "route_id": candidate.route_id,
                    "name": candidate.name,
                    "notes": candidate.notes,
                }
            )
    return {
        "passed": tuple(passed),
        "rejected": tuple(rejected),
        "summary": f"候选 {len(candidates)} 条：可行 {len(passed)}，违令否决 {len(rejected)}。",
    }
