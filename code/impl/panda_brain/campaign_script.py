"""Campaign script loading (doc 11 §7 P9-T1).

A campaign script is pure scenario data: the world's evolution across days
and waves. Command policy deliberately does NOT live here — it is code
(``campaign_runner.GeneralSurrogate``) so that every command act stays on the
authenticated audit trail. The loader is strict: unknown keys, broken
structure, or non-increasing timelines are rejected explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import AttemptStatus


class CampaignConfigError(ValueError):
    """Raised for malformed campaign script configurations."""


_WAVE_TOP_KEYS = {"wave_id", "hours_elapsed", "label", "observations", "field_reports", "outcomes"}
_OBS_KEYS = {"site_id", "metric", "unit", "value", "observed_at"}
_REPORT_STATUS = {"verified", "observed"}


@dataclass(frozen=True)
class FieldReport:
    """An authorized field-recon fact: the only channel that may verify."""

    subject: str
    value: str
    status: str
    evidence_id: str
    summary: str


@dataclass(frozen=True)
class WorldOutcome:
    outcome: AttemptStatus
    observations: tuple[str, ...]


@dataclass(frozen=True)
class RetryPlan:
    answer: str
    world: WorldOutcome


@dataclass(frozen=True)
class Wave:
    wave_id: str
    day: int
    label: str
    hours_elapsed: int
    observations: tuple[dict, ...]
    field_reports: tuple[FieldReport, ...]
    outcomes: dict[str, WorldOutcome]
    retries: dict[str, RetryPlan]


@dataclass(frozen=True)
class CampaignDay:
    day: int
    label: str
    waves: tuple[Wave, ...]


@dataclass(frozen=True)
class CampaignScript:
    campaign_id: str
    title: str
    domain: str
    authorized_sources: tuple[str, ...]
    days: tuple[CampaignDay, ...]

    @property
    def waves(self) -> tuple[Wave, ...]:
        return tuple(wave for day in self.days for wave in day.waves)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CampaignConfigError(message)


def _parse_outcome(raw, where: str) -> WorldOutcome:
    _require(isinstance(raw, dict), f"{where}: outcome must be a mapping.")
    try:
        status = AttemptStatus(str(raw.get("outcome")))
    except ValueError as exc:
        raise CampaignConfigError(f"{where}: unknown attempt status {raw.get('outcome')!r}.") from exc
    notes = raw.get("observations", [])
    _require(isinstance(notes, list) and all(isinstance(n, str) for n in notes),
             f"{where}: observations must be a list of strings.")
    return WorldOutcome(status, tuple(notes))


def _parse_wave(raw: dict, day: int, seen_wave_ids: set[str], last_hour: int) -> Wave:
    _require(isinstance(raw, dict), f"Day {day}: each wave must be a mapping.")
    unknown = sorted(set(raw) - _WAVE_TOP_KEYS)
    _require(not unknown, f"Wave on day {day}: unknown keys {unknown}.")
    wave_id = raw.get("wave_id")
    _require(isinstance(wave_id, str) and wave_id, f"Wave on day {day}: wave_id required.")
    _require(wave_id not in seen_wave_ids, f"Duplicate wave_id {wave_id!r}.")
    seen_wave_ids.add(wave_id)

    hours = raw.get("hours_elapsed")
    _require(isinstance(hours, int) and not isinstance(hours, bool), f"{wave_id}: hours_elapsed must be an int.")
    _require(hours > last_hour, f"{wave_id}: hours_elapsed must strictly increase (got {hours}, after {last_hour}).")

    label = raw.get("label", wave_id)

    observations = raw.get("observations", []) or []
    _require(isinstance(observations, list), f"{wave_id}: observations must be a list.")
    for index, obs in enumerate(observations):
        _require(isinstance(obs, dict) and set(obs) == _OBS_KEYS,
                 f"{wave_id} observation #{index}: fields must be exactly {sorted(_OBS_KEYS)}.")
        _require(isinstance(obs["value"], (int, float)) and not isinstance(obs["value"], bool),
                 f"{wave_id} observation #{index}: value must be numeric.")

    reports: list[FieldReport] = []
    for index, rep in enumerate(raw.get("field_reports") or []):
        _require(isinstance(rep, dict), f"{wave_id} field_report #{index}: must be a mapping.")
        missing = [k for k in ("subject", "value", "status", "evidence_id", "summary") if k not in rep]
        _require(not missing, f"{wave_id} field_report #{index}: missing {missing}.")
        _require(":" in str(rep["subject"]), f"{wave_id} field_report #{index}: subject must be domain-tagged.")
        _require(rep["status"] in _REPORT_STATUS,
                 f"{wave_id} field_report #{index}: status must be one of {sorted(_REPORT_STATUS)}.")
        reports.append(
            FieldReport(
                subject=str(rep["subject"]),
                value=str(rep["value"]),
                status=str(rep["status"]),
                evidence_id=str(rep["evidence_id"]),
                summary=str(rep["summary"]),
            )
        )

    outcomes: dict[str, WorldOutcome] = {}
    retries: dict[str, RetryPlan] = {}
    raw_outcomes = raw.get("outcomes") or {}
    _require(isinstance(raw_outcomes, dict), f"{wave_id}: outcomes must be a mapping keyed by option id.")
    for option_id, spec in raw_outcomes.items():
        where = f"{wave_id}/{option_id}"
        first_raw, retry_raw = spec, None
        if isinstance(spec, dict) and "outcome" not in spec and "first" in spec:
            first_raw, retry_raw = spec["first"], spec.get("retry")
        outcomes[option_id] = _parse_outcome(first_raw, where)
        if isinstance(retry_raw, dict):
            answer = retry_raw.get("answer")
            _require(isinstance(answer, str) and answer, f"{where} retry: answer required.")
            retries[option_id] = RetryPlan(answer, _parse_outcome(retry_raw, f"{where} retry"))
    return Wave(
        wave_id=wave_id,
        day=day,
        label=label,
        hours_elapsed=hours,
        observations=tuple(observations),
        field_reports=tuple(reports),
        outcomes=outcomes,
        retries=retries,
    )


def load_campaign_script(path: str | Path, *, expected_domain: str | None = None) -> CampaignScript:
    file_path = Path(path)
    if not file_path.exists():
        raise CampaignConfigError(f"Campaign script not found: {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    _require(isinstance(raw, dict), "Campaign script must be a mapping.")
    top_keys = {"schema_version", "campaign_id", "title", "domain", "authorized_sources", "days"}
    unknown = sorted(set(raw) - top_keys)
    _require(not unknown, f"Unknown top-level keys in campaign script: {unknown}.")
    for key in ("campaign_id", "title", "domain"):
        _require(isinstance(raw.get(key), str) and raw[key], f"Campaign {key} required.")
    if expected_domain is not None:
        _require(raw["domain"] == expected_domain,
                 f"Campaign domain {raw['domain']!r} does not match playbook {expected_domain!r}.")
    sources = raw.get("authorized_sources")
    _require(isinstance(sources, list) and sources and all(isinstance(s, str) for s in sources),
             "authorized_sources must be a non-empty list of strings.")

    days_raw = raw.get("days")
    _require(isinstance(days_raw, list) and days_raw, "Campaign needs a non-empty 'days' list.")
    seen_wave_ids: set[str] = set()
    last_hour = -1
    previous_day = 0
    days: list[CampaignDay] = []
    for day_entry in days_raw:
        _require(isinstance(day_entry, dict), "Each day must be a mapping.")
        unknown_day = sorted(set(day_entry) - {"day", "label", "waves"})
        _require(not unknown_day, f"Unknown day keys: {unknown_day}.")
        day_no = day_entry.get("day")
        _require(isinstance(day_no, int) and not isinstance(day_no, bool) and day_no > previous_day,
                 f"Day numbers must be positive and strictly increasing (got {day_no!r}).")
        previous_day = day_no
        waves_raw = day_entry.get("waves")
        _require(isinstance(waves_raw, list) and waves_raw, f"Day {day_no}: needs a non-empty wave list.")
        waves = tuple(_parse_wave(w, day_no, seen_wave_ids, -1) for w in waves_raw)
        for wave in waves:
            _require(wave.hours_elapsed > last_hour,
                     f"{wave.wave_id}: hours_elapsed must strictly increase across the whole campaign.")
            last_hour = wave.hours_elapsed
        days.append(CampaignDay(day=day_no, label=str(day_entry.get("label", f"day-{day_no}")), waves=waves))

    return CampaignScript(
        campaign_id=str(raw["campaign_id"]),
        title=str(raw["title"]),
        domain=str(raw["domain"]),
        authorized_sources=tuple(sources),
        days=tuple(days),
    )
