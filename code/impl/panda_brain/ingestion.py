"""Offline-first observation ingestion channel (doc 11 §6.4 / P6-T3).

Disciplines absorbed from the flood-decision reference system:
- Field names carry their unit; anything outside the unit whitelist is refused,
  never silently converted;
- Timestamps must carry an explicit UTC offset and are normalized to
  Asia/Shanghai (fixed UTC+8; China has had no daylight saving since 1991);
- A ValidationReport lists every problem per item — nothing is silently dropped;
- Import is idempotent: re-importing the same payload creates zero duplicates;
- An empty store reports ``bootstrap_required`` explicitly. Sample data is never
  generated — there is deliberately no populate-sample-data entry point.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

#: The only units this system accepts, anywhere (doc: SI 归一).
UNIT_WHITELIST = frozenset({"m", "mm", "m3/s", "m/s", "C", "%", "dimensionless"})

#: Metrics this system understands, each bound to its required unit. Geography
#: and thresholds come from reviewed configuration elsewhere — ingestion never
#: invents either.
METRIC_UNITS: dict[str, str] = {
    "water_level_m": "m",
    "discharge_m3_s": "m3/s",
    "precipitation_mm": "mm",
    "wind_speed_m_s": "m/s",
    "temperature_c": "C",
    "humidity_pct": "%",
    "alert_level": "dimensionless",
}

SOURCE_DATA_KINDS = frozenset({'observed', 'simulated', 'unknown'})
_SOURCE_FIELDS = frozenset({'event_id', 'source_record_id', 'source_version',
    'site_id', 'metric', 'value', 'unit', 'observed_at', 'received_at',
    'quality_note', 'coordinate_system'})


def _plain_text(value: object, *, required: bool = False) -> bool:
    return (isinstance(value, str) and value == value.strip()
            and (bool(value) or not required)
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def normalize_to_shanghai(raw: str) -> str:
    """Normalize an ISO timestamp with explicit offset to Shanghai wall time.

    Naive timestamps are rejected: guessing a zone would violate the no-invention
    rule. Returns a naive ISO string meaning Asia/Shanghai local time.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"Timezone-naive timestamp rejected: {raw!r}. Supply an explicit UTC offset."
        )
    utc_naive = parsed.replace(tzinfo=None) - parsed.utcoffset()
    return (utc_naive + timedelta(hours=8)).isoformat()


@dataclass(frozen=True)
class ObservationRecord:
    """One normalized observation. The observation id is a content hash, which
    makes imports naturally idempotent."""

    observation_id: str
    site_id: str
    metric: str
    value: float
    unit: str
    observed_at: str  # Asia/Shanghai wall time, ISO format
    source: str
    # Empty event_id denotes legacy, unbound data. It cannot enter a bound event.
    event_id: str = ''
    data_kind: str = 'unknown'
    source_record_id: str = ''
    source_version: int = 1
    collected_at: str = ''  # Original offset-bearing source clock, never refresh time.
    received_at: str = ''   # Empty means unknown; no wall clock is invented.
    quality_note: str = ''  # Source declaration, not a measured accuracy guarantee.
    coordinate_system: str = ''

    @property
    def verification_status(self) -> str:
        return 'pending_verification'

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not isinstance(self.data_kind, str):
            raise ValueError('invalid event binding or data kind')
        if not self.event_id:
            if (self.data_kind != 'unknown' or self.source_record_id or self.collected_at
                    or self.received_at or self.quality_note or self.coordinate_system
                    or type(self.source_version) is not int or self.source_version != 1):
                raise ValueError('source metadata requires event binding')
            return
        if (not _plain_text(self.event_id, required=True)
                or not _plain_text(self.source, required=True)
                or not _plain_text(self.source_record_id, required=True)
                or self.data_kind not in SOURCE_DATA_KINDS
                or type(self.source_version) is not int or self.source_version < 1):
            raise ValueError('invalid source identity or data kind')
        for value in (self.collected_at, self.received_at, self.quality_note, self.coordinate_system):
            if not _plain_text(value):
                raise ValueError('source metadata must be plain text')
        if normalize_to_shanghai(self.collected_at) != self.observed_at:
            raise ValueError('collection clocks disagree')
        if self.received_at:
            normalize_to_shanghai(self.received_at)


@dataclass(frozen=True)
class RejectedItem:
    """A rejected raw item with every problem found in it."""

    index: int
    observation_key: str
    errors: tuple[str, ...]


@dataclass
class ValidationReport:
    """Per-item validation outcome. Rejections are listed, never swallowed."""

    accepted: list[ObservationRecord]
    rejected: list[RejectedItem]

    @property
    def all_passed(self) -> bool:
        return not self.rejected

    def summary(self) -> str:
        return f"{len(self.accepted)} accepted, {len(self.rejected)} rejected"


def validate_observations(items: list[dict], *, source: str) -> ValidationReport:
    """Validate raw items one by one; collect every error instead of failing fast."""
    accepted: list[ObservationRecord] = []
    rejected: list[RejectedItem] = []

    for index, item in enumerate(items):
        errors: list[str] = []
        site_id = str(item.get("site_id", "")).strip()
        metric = str(item.get("metric", "")).strip()
        unit = str(item.get("unit", "")).strip()
        observed_raw = str(item.get("observed_at", "")).strip()

        if not site_id:
            errors.append("missing site_id")
        if metric not in METRIC_UNITS:
            errors.append(f"unknown metric {metric!r}; known metrics: {sorted(METRIC_UNITS)}")
        elif unit != METRIC_UNITS[metric]:
            errors.append(
                f"metric {metric!r} requires unit {METRIC_UNITS[metric]!r}, got {unit!r}"
            )
        if unit not in UNIT_WHITELIST:
            errors.append(f"unit {unit!r} outside whitelist {sorted(UNIT_WHITELIST)}")

        value = item.get("value")
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors.append(f"value {value!r} is not a finite number")

        try:
            observed_at = normalize_to_shanghai(observed_raw)
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(str(exc))
            observed_at = None

        if errors:
            rejected.append(RejectedItem(index=index, observation_key=site_id or f"#{index}", errors=tuple(errors)))
            continue

        digest_basis = "|".join([site_id, metric, repr(numeric), unit, observed_at, source])
        observation_id = hashlib.sha256(digest_basis.encode("utf-8")).hexdigest()
        accepted.append(
            ObservationRecord(
                observation_id=observation_id,
                site_id=site_id,
                metric=metric,
                value=numeric,
                unit=unit,
                observed_at=observed_at,
                source=source,
            )
        )

    return ValidationReport(accepted=accepted, rejected=rejected)


class ObservationStore:
    """Idempotent observation collection with an explicit empty state."""

    def __init__(self) -> None:
        self._observations: dict[str, ObservationRecord] = {}
        self.last_duplicates: list[str] = []

    def status(self) -> dict:
        if not self._observations:
            return {"status": "bootstrap_required"}
        return {"status": "ready", "observation_count": len(self._observations)}

    def import_json(self, items: list[dict], *, source: str) -> ValidationReport:
        """Validate then insert. Duplicates (same content hash) are reported and
        skipped — re-importing identical data changes nothing."""
        report = validate_observations(items, source=source)
        self.last_duplicates: list[str] = []
        for record in report.accepted:
            if record.observation_id in self._observations:
                self.last_duplicates.append(record.observation_id)
                continue
            self._observations[record.observation_id] = record
        return report

    def import_source_json(self, items: list[dict], *, event_id: str,
                           source: str, data_kind: str) -> ValidationReport:
        """Event-bound adapter contract; declaring observed does not verify a device.

        Source identity/version is immutable. Retransmission keeps the first
        receipt clock; changed content at the same revision is explicitly rejected.
        Classification belongs to the adapter context, never to a payload override.
        """
        if (not _plain_text(event_id, required=True) or not _plain_text(source, required=True)
                or not isinstance(data_kind, str) or data_kind not in SOURCE_DATA_KINDS):
            raise ValueError('event_id, source and a supported data_kind are required')
        accepted, rejected = [], []
        self.last_duplicates = []
        revisions = {(r.event_id, r.source, r.source_record_id, r.source_version): r
                     for r in self._observations.values() if r.event_id}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                rejected.append(RejectedItem(index, f'#{index}', ('item must be an object',)))
                continue
            errors = []
            if set(item) - _SOURCE_FIELDS:
                errors.append('unsupported source fields')
            if item.get('event_id') != event_id:
                errors.append('event_id missing or does not match adapter context')
            if not _plain_text(item.get('source_record_id'), required=True):
                errors.append('source_record_id must be nonempty plain text')
            if type(item.get('source_version')) is not int or item['source_version'] < 1:
                errors.append('source_version must be a positive integer')
            for key in ('site_id', 'metric', 'unit', 'observed_at'):
                if not _plain_text(item.get(key), required=True):
                    errors.append(f'{key} must be nonempty plain text')
            for key in ('received_at', 'quality_note', 'coordinate_system'):
                if not _plain_text(item.get(key, '')):
                    errors.append(f'{key} must be plain text')
            if isinstance(item.get('value'), bool):
                errors.append('boolean is not a measurement')
            if isinstance(item.get('received_at'), str) and item['received_at']:
                try:
                    normalize_to_shanghai(item['received_at'])
                except (ValueError, TypeError, OverflowError) as exc:
                    errors.append(str(exc))
            basic = validate_observations([item], source=source)
            errors.extend(error for rejection in basic.rejected for error in rejection.errors)
            if errors:
                rejected.append(RejectedItem(index, str(item.get('site_id') or f'#{index}'), tuple(errors)))
                continue
            base = basic.accepted[0]
            canonical = dict(event_id=event_id, source=source, data_kind=data_kind,
                source_record_id=item['source_record_id'], source_version=item['source_version'],
                site_id=base.site_id, metric=base.metric, value=base.value, unit=base.unit,
                observed_at=base.observed_at, quality_note=item.get('quality_note', ''),
                coordinate_system=item.get('coordinate_system', ''))
            digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            identity = (event_id, source, item['source_record_id'], item['source_version'])
            previous = revisions.get(identity)
            if previous is not None:
                if previous.observation_id != digest:
                    rejected.append(RejectedItem(index, base.site_id,
                        ('source revision conflict: existing content retained',)))
                else:
                    self.last_duplicates.append(previous.observation_id)
                    accepted.append(previous)
                continue
            record = ObservationRecord(observation_id=digest, **canonical,
                collected_at=item['observed_at'], received_at=item.get('received_at', ''))
            self._observations[digest] = record
            revisions[identity] = record
            accepted.append(record)
        return ValidationReport(accepted, rejected)

    def observations(self) -> tuple[ObservationRecord, ...]:
        return tuple(sorted(self._observations.values(), key=lambda r: (r.site_id, r.metric, r.observed_at)))

    def get(self, observation_id: str) -> ObservationRecord | None:
        return self._observations.get(observation_id)
