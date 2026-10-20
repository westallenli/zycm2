"""Deterministic situation scoring (doc 11 §6.6, P8-T2).

A pure function over claim values — no model, no defaults:

- Every component maps an observed categorical claim to 0..1 severity using
  reviewed YAML configuration; the reviewed mapping is authoritative (a
  claim valued ``unknown`` scores what the config says that means);
- An absent claim, or a value outside the reviewed mapping, makes that
  component explicitly ``unavailable`` with the reason attached;
- With ``allow_partial: false`` (default), any unavailable component makes the
  overall score unavailable and lists every missing component — an invented
  total is worse than no total.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ClaimStatus  # noqa: F401  (kept for typing clarity)


class ScoreConfigError(ValueError):
    """Raised for malformed or non-additive score configurations."""


_REQUIRED_KEYS = ("schema_version", "allow_partial", "weights", "components")


@dataclass(frozen=True)
class ComponentScore:
    """One scored component; ``unavailable`` carries the reason instead of a guess."""

    name: str
    state: str  # "scored" | "unavailable"
    value: float | None
    weight: float
    reason: str = ""


@dataclass(frozen=True)
class SituationScore:
    """Overall situation severity: available only when every component scored."""

    level: str  # "available" | "unavailable"
    total: float | None
    components: tuple[ComponentScore, ...]
    missing_components: tuple[str, ...]

    def render(self) -> str:
        if self.level == "unavailable":
            return (
                f"Situation score: unavailable (missing: {', '.join(self.missing_components)})"
            )
        parts = ", ".join(
            f"{c.name}={c.value:.2f}*{c.weight:.2f}" for c in self.components if c.state == "scored"
        )
        return f"Situation score: {self.total:.2f} ({parts})"


class SituationScorer:
    """Config-driven deterministic scorer over the workspace claim dictionary."""

    def __init__(self, config_path: str | Path) -> None:
        path = Path(config_path)
        if not path.exists():
            raise ScoreConfigError(f"Score config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._validate(raw)
        self._config_path = path
        self.weights: dict[str, float] = dict(raw["weights"])
        self.allow_partial: bool = bool(raw["allow_partial"])
        self.components: tuple[dict, ...] = tuple(dict(c) for c in raw["components"])

    def _validate(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            raise ScoreConfigError("Score config must be a mapping.")
        unknown = sorted(set(raw) - set(_REQUIRED_KEYS))
        if unknown:
            raise ScoreConfigError(f"Unknown top-level keys in score config: {unknown}")
        weights = raw.get("weights") or {}
        if not weights:
            raise ScoreConfigError("Score config requires at least one weight.")
        total_weight = sum(float(w) for w in weights.values())
        if abs(total_weight - 1.0) > 1e-6:
            raise ScoreConfigError(f"Weights must sum to 1.0; got {total_weight}.")
        components = raw.get("components") or []
        names = [c.get("name") for c in components]
        if sorted(names) != sorted(weights):
            raise ScoreConfigError(
                f"Components {sorted(names)} must match weights {sorted(weights)}."
            )
        for component in components:
            subject = str(component.get("subject", ""))
            if ":" not in subject:
                raise ScoreConfigError(
                    f"Component {component['name']} needs a domain-tagged subject ('domain:tag')."
                )
            values = component.get("values") or {}
            if not values or any(not isinstance(v, (int, float)) for v in values.values()):
                raise ScoreConfigError(
                    f"Component {component['name']} needs numeric severity values."
                )

    def score(self, claims: dict) -> SituationScore:
        """Score from a subject→Claim mapping (workspace.claims). Deterministic.

        The reviewed value mapping is authoritative: a claim whose observed
        value is ``unknown`` scores whatever the config says that means (for
        access conditions, worst case). Only an absent claim or a value
        outside the reviewed mapping is unavailable."""
        scored: list[ComponentScore] = []
        missing: list[str] = []
        total = 0.0
        for component in self.components:
            name = component["name"]
            weight = float(self.weights[name])
            subject = component["subject"]
            claim = claims.get(subject)
            if claim is None:
                scored.append(
                    ComponentScore(name=name, state="unavailable", value=None, weight=weight,
                                   reason=f"claim {subject!r} absent")
                )
                missing.append(name)
                continue
            mapped = component["values"].get(str(claim.value))
            if mapped is None:
                scored.append(
                    ComponentScore(name=name, state="unavailable", value=None, weight=weight,
                                   reason=f"unmapped value {str(claim.value)!r} for {subject!r}")
                )
                missing.append(name)
                continue
            value = round(float(mapped), 6)
            total += value * weight
            scored.append(ComponentScore(name=name, state="scored", value=value, weight=weight))

        total = round(total, 6)
        if missing and not self.allow_partial:
            return SituationScore(
                level="unavailable", total=None,
                components=tuple(scored), missing_components=tuple(missing),
            )
        return SituationScore(
            level="available", total=total,
            components=tuple(scored), missing_components=tuple(missing),
        )


DEFAULT_SCORE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "forest_fire_situation_score.yaml"


def default_scorer() -> SituationScorer:
    return SituationScorer(DEFAULT_SCORE_CONFIG)
