"""Observation-driven scenario runner (doc 11 §7 P8-T3).

Replaces part of the fixed synthetic script with a live data path:

    JSON observations → ingestion → intake bridge → threshold rules
      → domain condition claims (OBSERVED) → the existing review chain.

Discipline:
- A threshold crossing moves a domain claim to OBSERVED, never VERIFIED —
  ingestion is not verification (P7-T1 carries over);
- Observations that cross no threshold stay in the store and in observation
  records; they never touch domain condition claims;
- An empty stream reports ``bootstrap_required`` and changes nothing;
- Everything is deterministic: the same sequence produces identical state.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path

import yaml

from .chief_advisor import ChiefAdvisor
from .intake_bridge import IntakeBridge
from .ingestion import ObservationStore, ValidationReport

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


class ThresholdConfigError(ValueError):
    """Raised for malformed threshold rule configurations."""


@dataclass(frozen=True)
class ThresholdRule:
    subject: str
    metric: str
    site: str | None
    op: str
    value: float
    met_value: str
    not_met_value: str


def load_threshold_rules(path: str | Path) -> tuple[ThresholdRule, ...]:
    file_path = Path(path)
    if not file_path.exists():
        raise ThresholdConfigError(f"Threshold config not found: {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "thresholds" not in raw:
        raise ThresholdConfigError("Threshold config must contain 'thresholds'.")
    unknown = sorted(set(raw) - {"schema_version", "thresholds"})
    if unknown:
        raise ThresholdConfigError(f"Unknown keys in threshold config: {unknown}")

    rules: list[ThresholdRule] = []
    for entry in raw["thresholds"]:
        for field in ("subject", "metric", "op", "value", "met_value", "not_met_value"):
            if field not in entry:
                raise ThresholdConfigError(f"Threshold entry missing {field}: {entry}")
        if entry["op"] not in _OPS:
            raise ThresholdConfigError(f"Unknown operator {entry['op']!r}.")
        if ":" not in str(entry["subject"]):
            raise ThresholdConfigError(f"Subject must be domain-tagged: {entry['subject']!r}")
        rules.append(
            ThresholdRule(
                subject=str(entry["subject"]),
                metric=str(entry["metric"]),
                site=str(entry["site"]) if entry.get("site") else None,
                op=str(entry["op"]),
                value=float(entry["value"]),
                met_value=str(entry["met_value"]),
                not_met_value=str(entry["not_met_value"]),
            )
        )
    return tuple(rules)


class ScenarioDriver:
    """Runs one workspace from an external observation sequence."""

    def __init__(
        self,
        workspace,
        store: ObservationStore,
        bridge: IntakeBridge,
        threshold_rules: tuple[ThresholdRule, ...],
        chief: ChiefAdvisor | None = None,
    ) -> None:
        self.workspace = workspace
        self.store = store
        self.bridge = bridge
        self.rules = threshold_rules
        self.chief = chief

    # -- data loading ------------------------------------------------------

    def load_sequence(self, items: list[dict], *, source: str = "sensor-feed") -> int:
        """Import one batch of raw observations; returns accepted count."""
        report = self.store.import_json(items, source=source)
        return len(report.accepted)

    def load_source_sequence(self, items: list[dict], *, source: str,
                             data_kind: str) -> ValidationReport:
        """Import through the bound adapter contract and retain per-item errors."""
        return self.store.import_source_json(items, event_id=self.workspace.event_id,
                                              source=source, data_kind=data_kind)

    # -- stepping ----------------------------------------------------------

    def step(self, *, orchestrate_question: str | None = None) -> dict:
        status = self.store.status()
        if status.get("status") == "bootstrap_required":
            return {"status": "bootstrap_required", "bridged": (), "claims_updated": ()}

        bridge_report = self.bridge.bridge_pending(self.workspace)
        notes = list(bridge_report.notes)
        updated = self._apply_thresholds(notes=notes)

        result = {
            "status": "ready",
            "bridged": bridge_report.bridged_subjects + bridge_report.updated_subjects,
            "claims_updated": updated,
            "notes": tuple(notes),
            "rejected_observation_ids": bridge_report.rejected_observation_ids,
        }
        if orchestrate_question and self.chief is not None:
            self.chief.orchestrate(self.workspace, orchestrate_question)
            result["orchestrated"] = True
        return result

    # -- internals ---------------------------------------------------------

    def _latest_by_site_metric(self) -> dict[tuple[str, str], object]:
        latest: dict[tuple[str, str], object] = {}
        for record in sorted(self.store.observations(), key=lambda r: r.observed_at):
            if not IntakeBridge.matches_event(record, self.workspace):
                continue
            current = self.workspace.claims.get(IntakeBridge.claim_subject_for(record))
            if current is None or IntakeBridge.evidence_id_for(record) not in current.evidence_ids:
                continue
            latest[(record.site_id, record.metric)] = record
        return latest

    def _apply_thresholds(self, *, notes: list[str]) -> tuple[str, ...]:
        from .models import Claim, ClaimStatus  # local: thin glue layer

        latest = self._latest_by_site_metric()
        updated: list[str] = []
        for rule in self.rules:
            candidates = [
                record
                for (site_id, metric), record in latest.items()
                if metric == rule.metric and (rule.site is None or site_id == rule.site)
                and (current := self.workspace.claims.get(IntakeBridge.claim_subject_for(record))) is not None
                and IntakeBridge.evidence_id_for(record) in current.evidence_ids
            ]
            if not candidates:
                continue
            newest = max(candidates, key=lambda r: r.observed_at)
            crossed = _OPS[rule.op](float(newest.value), rule.value)
            target_value = rule.met_value if crossed else rule.not_met_value
            evidence_id = IntakeBridge.evidence_id_for(newest)

            existing = self.workspace.claims.get(rule.subject)
            if existing is not None and existing.status == ClaimStatus.VERIFIED:
                continue  # sensors never touch VERIFIED claims — verification is
                # an authorized channel's judgement (P9: stale readings must not
                # clobber a later field verification)
            if (existing is not None and newest.event_id
                    and IntakeBridge.claim_data_kind(self.workspace, existing) != newest.data_kind):
                note = f'{rule.subject}：条件来源性质不同，保留原条件并待人工复核。'
                notes.append(note)
                if not any(event.kind == 'ConditionSourceKindConflict'
                           and event.payload.get('subject') == rule.subject
                           and event.payload.get('evidence_id') == evidence_id
                           and event.payload.get('previous_claim_id') == existing.id
                           for event in self.workspace.events):
                    self.workspace.append_event('ConditionSourceKindConflict',
                        event_id=newest.event_id, subject=rule.subject,
                        evidence_id=evidence_id, previous_claim_id=existing.id,
                        data_kind=newest.data_kind, note=note)
                continue  # A simulated condition cannot replace an observed one (or vice versa).
            if (existing is not None and existing.value == target_value
                    and (not newest.event_id or existing.evidence_ids == (evidence_id,))):
                continue  # no change, no event — determinism over chatter

            if evidence_id not in self.workspace.evidence:
                continue  # observation not bridged yet; nothing to rest on

            if rule.subject in self.workspace.claims:
                self.workspace.update_claim(
                    rule.subject,
                    value=target_value,
                    status=ClaimStatus.OBSERVED,
                    source=IntakeBridge.claim_source_for(newest),
                    confidence=0.9,
                    evidence_ids=(evidence_id,),
                )
            else:
                self.workspace.record_claim(
                    Claim(
                        id=f"claim-thr-{self.workspace.version + 1:03d}",
                        subject=rule.subject,
                        value=target_value,
                        status=ClaimStatus.OBSERVED,
                        source=IntakeBridge.claim_source_for(newest),
                        confidence=0.9,
                        evidence_ids=(evidence_id,),
                    )
                )
            updated.append(f"{rule.subject}={target_value}")
        return tuple(updated)
