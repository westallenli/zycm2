"""Intake bridge: ingested observations become first-class workspace facts.

Discipline (doc 11 §7 P7-T1): ingestion is NOT verification. A bridged
observation lands as an OBSERVED claim backed by an immutable evidence
snapshot. Reaching VERIFIED still requires the AUTHORIZED-evidence gate that
lives in the workspace itself — the bridge never bypasses it.

Bridging is derived from workspace state, so it is restartable and naturally
idempotent: an observation whose evidence snapshot already exists is skipped,
which propagates the ingestion channel's content-hash idempotency up into the
claim layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .ingestion import ObservationRecord, ObservationStore
from .models import (
    Claim,
    ClaimStatus,
    Evidence,
    SourceTrustLevel,
)
from .workspace import ScenarioWorkspace

#: Confidence assigned to a raw instrument reading reported as-is.
_INTAKE_CONFIDENCE = 0.9


@dataclass(frozen=True)
class BridgeReport:
    """What one bridge pass did; every outcome is explicit."""

    bridged_subjects: tuple[str, ...]
    updated_subjects: tuple[str, ...]
    skipped_evidence_ids: tuple[str, ...]
    notes: tuple[str, ...] = field(default=())
    rejected_observation_ids: tuple[str, ...] = field(default=())


class IntakeBridge:
    """Bridges an ObservationStore into a ScenarioWorkspace."""

    def __init__(self, store: ObservationStore, *, authorized_sources: Iterable[str] = ()) -> None:
        self.store = store
        self.authorized_sources = frozenset(authorized_sources)

    # -- deterministic naming --------------------------------------------

    @staticmethod
    def evidence_id_for(record: ObservationRecord) -> str:
        return f"obs-{record.observation_id[:32]}"

    @staticmethod
    def claim_subject_for(record: ObservationRecord) -> str:
        """Domain-tagged subject: '<site>:<metric>' keeps retrieval matching."""
        return f"{record.site_id}:{record.metric}"

    def trust_level_for(self, record: ObservationRecord) -> SourceTrustLevel:
        if record.event_id:
            return SourceTrustLevel.PENDING_VERIFICATION
        return SourceTrustLevel.AUTHORIZED if record.source in self.authorized_sources else SourceTrustLevel.PENDING_VERIFICATION

    @staticmethod
    def matches_event(record: ObservationRecord, workspace: ScenarioWorkspace) -> bool:
        return record.event_id == workspace.event_id

    @staticmethod
    def claim_source_for(record: ObservationRecord) -> str:
        if not record.event_id:
            return record.source
        prefix = {'simulated': '模拟', 'observed': '观测待核', 'unknown': '来源性质未知'}
        return f'{prefix[record.data_kind]}：{record.source}'

    @staticmethod
    def claim_data_kind(workspace: ScenarioWorkspace, claim: Claim) -> str:
        for event in reversed(workspace.events):
            if (event.kind == 'ObservationIntakeRecorded'
                    and event.payload.get('evidence_id') in claim.evidence_ids):
                return event.payload.get('data_kind', 'unknown')
        return 'unknown'

    # -- bridging ----------------------------------------------------------

    @staticmethod
    def _collection_time(record: ObservationRecord) -> datetime:
        # Ingestion has already normalized this legacy field to Shanghai wall time.
        return datetime.fromisoformat(record.observed_at).replace(
            tzinfo=timezone(timedelta(hours=8)))

    @staticmethod
    def _claim_time(workspace: ScenarioWorkspace, claim: Claim) -> datetime | None:
        for event in reversed(workspace.events):
            p = event.payload
            if (event.kind == 'ObservationIntakeRecorded'
                    and p.get('subject') == claim.subject
                    and p.get('evidence_id') in claim.evidence_ids):
                try:
                    clock = datetime.fromisoformat(p['collected_at'])
                    return clock if clock.utcoffset() is not None else None
                except (ValueError, TypeError, KeyError):
                    return None
        return None  # Never infer a collection clock for a legacy/manual claim.

    def bridge_pending(self, workspace: ScenarioWorkspace, *, expected_version: int | None = None) -> BridgeReport:
        """Bridge every not-yet-bridged observation in the store."""
        workspace._check_revision(expected_version)
        if workspace.verify_audit_chain():
            raise ValueError('观测接入前审计链校验失败，不能依据失真的时间记录更新态势。')
        self.store.status()  # explicit empty state surfaces bootstrap_required upstream
        bridged: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        notes: list[str] = []
        rejected: list[str] = []

        for record in self.store.observations():
            if not self.matches_event(record, workspace):
                rejected.append(record.observation_id)
                notes.append(f'{record.observation_id}：事件绑定缺失或不匹配，未写入当前事件。')
                continue
            evidence_id = self.evidence_id_for(record)
            subject = self.claim_subject_for(record)

            if evidence_id in workspace.evidence:
                skipped.append(evidence_id)
                continue

            metadata = dict(event_id=record.event_id, data_kind=record.data_kind,
                site_id=record.site_id, metric=record.metric, value=record.value, unit=record.unit,
                source_record_id=record.source_record_id, source_version=record.source_version,
                original_collected_at=record.collected_at, received_at=record.received_at,
                quality_note=record.quality_note, coordinate_system=record.coordinate_system,
                verification_status=record.verification_status)
            evidence = Evidence.create(
                id=evidence_id,
                content=(
                    f"observation {record.site_id} {record.metric}={record.value:g} {record.unit}"
                    f" at {record.observed_at} (source: {record.source})"
                    + (f' source_contract={json.dumps(metadata, ensure_ascii=False, sort_keys=True)}'
                       if record.event_id else '')
                ),
                source=record.source,
                trust_level=self.trust_level_for(record),
            )
            current = workspace.claims.get(subject)
            collected = self._collection_time(record)
            disposition, note = ('created' if current is None else 'updated'), ''
            if current is not None:
                previous_time = self._claim_time(workspace, current)
                if current.status == ClaimStatus.VERIFIED:
                    disposition = 'review_required'
                    note = f'{subject}：已有人工核实结论；新观测仅留证，需人工复核。'
                elif record.event_id and self.claim_data_kind(workspace, current) != record.data_kind:
                    disposition = 'source_kind_conflict'
                    note = f'{subject}：来源性质不同，新观测仅留证并待人工复核。'
                elif previous_time is None:
                    disposition = 'clock_unknown'
                    note = f'{subject}：当前结论的采集时间不可核，新观测仅留证并待人工复核。'
                elif collected < previous_time:
                    disposition = 'late'
                    note = f'{subject}：迟到观测已留证，不替换较新结论。'
                elif collected == previous_time and current.value != {'value': record.value, 'unit': record.unit}:
                    disposition = 'same_time_conflict'
                    note = f'{subject}：同采集时间存在冲突，保留双方证据，当前观测仍待人工复核。'

            workspace.record_evidence(evidence)
            workspace.append_event('ObservationIntakeRecorded',
                observation_id=record.observation_id, evidence_id=evidence.id,
                subject=subject, source=record.source, collected_at=collected.isoformat(),
                disposition=disposition, note=note, **metadata)
            if note:
                notes.append(note)
            if disposition in {'review_required', 'clock_unknown', 'late', 'source_kind_conflict'}:
                continue
            claim = Claim(
                id=f"claim-{workspace.version + 1:03d}",
                subject=subject,
                value={"value": record.value, "unit": record.unit},
                status=ClaimStatus.OBSERVED,
                source=self.claim_source_for(record),
                confidence=_INTAKE_CONFIDENCE,
                evidence_ids=(evidence.id,),
            )

            if subject in workspace.claims:
                workspace.update_claim(
                    subject,
                    value=claim.value,
                    status=ClaimStatus.OBSERVED,
                    source=claim.source,
                    confidence=_INTAKE_CONFIDENCE,
                    evidence_ids=(evidence.id,),
                )
                updated.append(subject)
            else:
                workspace.record_claim(claim)
                bridged.append(subject)

        return BridgeReport(
            bridged_subjects=tuple(bridged),
            updated_subjects=tuple(updated),
            skipped_evidence_ids=tuple(skipped),
            notes=tuple(notes),
            rejected_observation_ids=tuple(rejected),
        )
