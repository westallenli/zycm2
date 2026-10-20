"""Reviewed-config loader for domain playbooks (doc 11 §7 P6-T4).

Discipline: configuration may define staffing and materials; it may never make
the system guess facts. Missing dependencies and malformed entries are rejected
explicitly at load time.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import AdvisorRole, StaffWorkType


class PlaybookConfigError(ValueError):
    """Raised when a playbook configuration is incomplete or inconsistent."""


_REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "name",
    "objective",
    "condition_subjects",
    "claim_refs",
    "research_summary",
    "analysis_summary",
    "red_team_summary",
    "analysis_rules",
    "red_team_rules",
    "dispatch",
    "logistics",
    "fire_action",
    "information",
    "disagreement",
    "staffing",
    "option_dependencies",
    "options",
    "feasibility",
)


class PlaybookConfig:
    """Validated view over one playbook configuration file."""

    def __init__(self, data: dict, source_path: Path | None = None) -> None:
        self.source_path = source_path
        self._validate(data)
        self._data = data

    # -- raw accessors ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def objective(self) -> str:
        return self._data["objective"]

    @property
    def condition_subjects(self) -> tuple[str, ...]:
        return tuple(self._data["condition_subjects"])

    @property
    def claim_refs(self) -> dict[str, str]:
        return dict(self._data["claim_refs"])

    def section(self, key: str) -> dict:
        return dict(self._data[key])

    def text(self, key: str) -> str:
        return str(self._data[key])

    @property
    def staffing(self) -> tuple[dict, ...]:
        return tuple(dict(entry) for entry in self._data["staffing"])

    @property
    def option_dependencies(self) -> tuple[str, ...]:
        return tuple(self._data["option_dependencies"])

    @property
    def options(self) -> tuple[dict, ...]:
        return tuple(dict(entry) for entry in self._data["options"])

    @property
    def feasibility_entries(self) -> tuple[dict, ...]:
        return tuple(dict(entry) for entry in self._data["feasibility"]["verdicts"])

    @property
    def feasibility_notes(self) -> str:
        return self._data["feasibility"]["notes"]

    def resolve_claim(self, ref: str) -> str:
        try:
            return self.claim_refs[ref]
        except KeyError:
            raise PlaybookConfigError(f"Unknown claim reference: {ref!r}") from None

    def resolve_claims(self, refs) -> tuple[str, ...]:
        return tuple(self.resolve_claim(ref) for ref in refs)

    # -- validation ------------------------------------------------------

    def _validate(self, data: dict) -> None:
        missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in data]
        if missing:
            raise PlaybookConfigError(f"Playbook config missing required keys: {missing}")
        unknown = sorted(set(data) - set(_REQUIRED_TOP_LEVEL_KEYS))
        if unknown:
            raise PlaybookConfigError(f"Playbook config has unknown top-level keys: {unknown}")

        agents = [entry.get("agent_id") for entry in data["staffing"]]
        if len(agents) != len(set(agents)):
            raise PlaybookConfigError(f"Duplicate agent_id in staffing: {agents}")

        agent_set = set(agents)
        for entry in data["staffing"]:
            for field in ("agent_id", "role", "work_type", "question_to_answer", "expected_deliverable"):
                if not entry.get(field):
                    raise PlaybookConfigError(f"Staffing entry missing {field}: {entry}")
            try:
                AdvisorRole(entry["role"])
                StaffWorkType(entry["work_type"])
            except ValueError as exc:
                raise PlaybookConfigError(
                    f"Invalid role/work_type for {entry['agent_id']}: {exc}"
                ) from exc
            dangling = [dep for dep in entry.get("dependencies", ()) if dep not in agent_set]
            if dangling:
                raise PlaybookConfigError(
                    f"{entry['agent_id']} depends on unknown specialists: {dangling}."
                    " Dependency closure is required; nothing is guessed."
                )

        dangling_option_deps = [dep for dep in data["option_dependencies"] if dep not in agent_set]
        if dangling_option_deps:
            raise PlaybookConfigError(f"option_dependencies reference unknown specialists: {dangling_option_deps}")

        for ref, subject in data["claim_refs"].items():
            if not isinstance(subject, str) or ":" not in subject:
                raise PlaybookConfigError(
                    f"Claim ref {ref!r} must map to a domain-tagged subject ('domain:tag'), got {subject!r}"
                )

        for option in data["options"]:
            if option.get("risk_source") not in {"analysis", "red_team"}:
                raise PlaybookConfigError(f"Option {option.get('id')} has invalid risk_source.")
            show_when = option.get("show_when")
            if show_when != "always" and not (
                isinstance(show_when, dict) and "all_claims_verified" in show_when
            ):
                raise PlaybookConfigError(f"Option {option['id']} has malformed show_when: {show_when!r}")

        verdict_statuses = {"feasible", "infeasible", "unknown"}
        for verdict in data["feasibility"]["verdicts"]:
            if verdict["status"] not in verdict_statuses:
                raise PlaybookConfigError(f"Verdict for {verdict['option_id']} has invalid status.")


def load_playbook_config(path: str | Path) -> PlaybookConfig:
    file_path = Path(path)
    if not file_path.exists():
        raise PlaybookConfigError(f"Playbook config file not found: {file_path}")
    with file_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise PlaybookConfigError(f"Playbook config must be a mapping: {file_path}")
    return PlaybookConfig(data, source_path=file_path)
