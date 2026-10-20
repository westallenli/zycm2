"""Safety-doctrine policy rules (P15 T3).

Encodes the hard safety constraints of 《森林草原火灾灭火组织指挥手册》 and
《前线指挥部工作指南》 as order-activation gates. These join the kernel's
POLICY_RULES registry, so an order whose action contract violates doctrine is
blocked at activation and the failure names the violated clauses.

Scope discipline:
- Rules evaluate ONLY contracts that declare the relevant semantic parameters
  (engagement / time_window / wind_force ...). Where a required declaration is
  missing for a hazardous engagement type, the check FAILS — silence is never
  taken as compliance ("configuration may never make the system guess facts").
- Geometric route vetting stays in fire_calculus.filter_routes; this module
  governs engagement commitments at activation time.
- The reserve-commitment gate reads live roster bindings (P15 T1), tying force
  posture to what may be activated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .org_model import force_posture

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import ActionContract
    from .workspace import ScenarioWorkspace


def _param(contract: "ActionContract", key: str) -> str | None:
    for param_key, value in contract.parameters:
        if param_key == key:
            return value
    return None


def _int_param(contract: "ActionContract", key: str) -> int | None:
    raw = _param(contract, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def rule_no_night_direct_assault(workspace: "ScenarioWorkspace", contract: "ActionContract") -> bool:
    """特殊打火规则：夜间原则上不得部署人员开展直接对抗灭火作业。

    直接对抗（engagement=direct-attack）必须申报 time_window；夜间一律拦截。
    非直接手段（机械隔离带、灭火炮、植保无人机等）不受本条约束。
    """
    engagement = _param(contract, "engagement")
    if engagement != "direct-attack":
        return True
    window = _param(contract, "time_window")
    if window is None:
        return False  # 未申报作业时间窗，无法核验夜间禁令
    return window != "night"


def rule_burnout_weather_limits(workspace: "ScenarioWorkspace", contract: "ActionContract") -> bool:
    """点烧五种情况：风力4级以上不点逆风火/侧风火，风向不定禁止点烧。"""
    if _param(contract, "engagement") != "burn-out":
        return True
    wind_force = _int_param(contract, "wind_force")
    stability = _param(contract, "wind_stability")
    orientation = _param(contract, "burn_orientation")
    if wind_force is None or stability is None or orientation is None:
        return False  # 点烧作业必须申报风力、风向稳定性与点烧方位
    if stability == "shifting":
        return False
    if wind_force >= 4 and orientation in {"upwind", "crosswind"}:
        return False
    return True


def rule_air_operation_weather_limits(
    workspace: "ScenarioWorkspace", contract: "ActionContract"
) -> bool:
    """特殊打火规则：直升机无日照或6级以上大风天气不作业。"""
    if _param(contract, "engagement") != "air-operation":
        return True
    daylight = _param(contract, "daylight")
    wind_force = _int_param(contract, "wind_force")
    if daylight is None or wind_force is None:
        return False  # 空中作业必须申报日照与风力条件
    if daylight != "true":
        return False
    return wind_force < 6


def rule_reserve_before_commitment(
    workspace: "ScenarioWorkspace", contract: "ActionContract"
) -> bool:
    """火场管控·动态补充预备队规则：投入一线直抗前，预备队按≥30%配置到位。

    读取实时花名册（P15 T1 编成模型）核验力量格局。
    """
    if _param(contract, "engagement") != "direct-attack":
        return True
    active = tuple(b for b in workspace.roster_bindings.values() if b.status == "active")
    report = force_posture(active)
    committed = report["committed"]
    if not committed:
        return False  # 一线无作战人员到岗，不得激活直抗命令
    return bool(report["reserve_ratio_ok"])


#: rule_id -> (activation-gate description, human-readable clause citations)
SAFETY_DOCTRINE_RULES: dict[str, tuple[str, Any]] = {
    "no-night-direct-assault": (
        "特殊打火规则：夜间禁止直接对抗灭火作业；直抗命令必须申报作业时间窗。",
        rule_no_night_direct_assault,
    ),
    "burnout-weather-limits": (
        "点烧五种情况：风力4级以上不点逆风火侧风火，风向不定禁止点烧。",
        rule_burnout_weather_limits,
    ),
    "air-op-weather-limits": (
        "飞行管制：直升机无日照或6级以上大风不作业。",
        rule_air_operation_weather_limits,
    ),
    "reserve-before-commitment": (
        "动态补充预备队规则：一线直抗投入前预备队须按不低于30%配置到位。",
        rule_reserve_before_commitment,
    ),
}

CLAUSE_INDEX: dict[str, tuple[str, ...]] = {
    "no-night-direct-assault": (
        "《手册》特殊打火规则：可视条件差时，夜间（特别是凌晨12时至次日4时）"
        "不得部署人员开展直接对抗灭火作业",
        "《指南》火场管控七规则：灭火炮、植保无人机、投弹无人机等非直接手段夜间可稳妥实施",
    ),
    "burnout-weather-limits": (
        "《手册》点烧五种情况：风力4级以上，不点逆风火，不点侧风火",
        "《手册》点烧五种情况：风向不定禁止点烧",
    ),
    "air-op-weather-limits": (
        "《手册》特殊打火规则：直升机在无日照情况或6级以上大风天气下不作业",
    ),
    "reserve-before-commitment": (
        "《指南》动态补充预备队规则：每在火场投入100人，预备队人员应准备不低于30人待命",
    ),
}


def clause_texts_for(rule_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Human-readable violated-clause citations for a failed-rules tuple."""
    texts: list[str] = []
    for rule_id in rule_ids:
        texts.extend(CLAUSE_INDEX.get(rule_id, ()))
    return tuple(texts)
