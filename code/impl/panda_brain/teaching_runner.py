"""Interactive teaching-scenario runner (P16-A, docs 07/08 alignment).

Unlike :mod:`campaign_runner` — which drives the whole drill with a
deterministic general surrogate — this runner advances the WORLD wave by wave
and leaves every command act to the human general through the panel:

    advance_wave()   -> inject observations / field reports, re-run staff work
    adopt_option()   -> the general adopts an option; the full power chain
                        (decision -> order -> contract -> confirmation ->
                        policy/approval -> activation -> execution) runs with
                        the SAME gates as any other path, then the scripted
                        world answers per doc 08's divergent futures.

The runner holds no authority of its own: it is called from panel actions that
already sit behind the authenticated command gate, and everything it does is
recorded on the workspace audit chain by the workspace methods themselves.
"""

from __future__ import annotations

from pathlib import Path

from .advisor_core import StaffCouncil
from .campaign_runner import (
    ACTION_RISK,
    DEFAULT_THRESHOLDS_PATH,
    _ANOMALY_STATUSES,
    seed_baseline_state,
)
from .campaign_script import Wave, load_campaign_script
from .chief_advisor import ChiefAdvisor
from .executor_adapter import WorldDecision, WorldDrivenExecutorAdapter
from .forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from .ingestion import ObservationStore
from .intake_bridge import IntakeBridge
from .models import (
    AttemptStatus,
    AdvisorRole,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    RiskLevel,
)
from .scenario_driver import ScenarioDriver, load_threshold_rules
from .simulator_executor import SimulatorExecutor
from .situation_score import default_scorer
from .workspace import ScenarioWorkspace

TEACHING_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scenarios" / "campaign_donghe_teaching.yaml"
)

#: Condition subjects the ledger watches for the completeness gate, split by
#: what "known" means. Sensors can NEVER verify a claim (ingestion is not
#: verification), so situation conditions (fire trend, wind) count as known
#: once OBSERVED by an authorized report or threshold; ENTRY conditions
#: (route/water) must be field-VERIFIED before any approach is decidable.
ENTRY_CONDITIONS: tuple[str, ...] = ("route:R2", "water:W1")
SITUATION_CONDITIONS: tuple[str, ...] = ("fire:trend", "weather:wind", "route:R1")
GATE_SUBJECTS: tuple[str, ...] = SITUATION_CONDITIONS + ENTRY_CONDITIONS

_GATE_LABELS = {
    "fire:trend": "火线情况",
    "weather:wind": "气象影响",
    "route:R1": "原进入路线",
    "route:R2": "替代进入路线",
    "water:W1": "备用水源",
}


class TeachingActionError(ValueError):
    """Refused teaching action; message is safe to show the commander."""


class TeachingRunner:
    """Wave-by-wave world driver with a human general (no surrogate)."""

    def __init__(
        self,
        workspace: ScenarioWorkspace,
        *,
        script_path: str | Path = TEACHING_SCRIPT_PATH,
    ) -> None:
        self.workspace = workspace
        self.script = load_campaign_script(script_path, expected_domain="forest_fire")
        self.chief = ChiefAdvisor(
            StaffCouncil(FOREST_FIRE_PLAYBOOK), situation_scorer=default_scorer()
        )
        self.rules = load_threshold_rules(DEFAULT_THRESHOLDS_PATH)
        self.store = ObservationStore()
        self.bridge = IntakeBridge(
            self.store, authorized_sources=self.script.authorized_sources
        )
        self.driver = ScenarioDriver(workspace, self.store, self.bridge, self.rules, self.chief)
        self.adapter = WorldDrivenExecutorAdapter(
            self._world_act, fallback_executor=SimulatorExecutor(workspace)
        )
        self.waves: tuple[Wave, ...] = tuple(
            wave for day in self.script.days for wave in day.waves
        )
        self.wave_index = 0
        self.goal_frame_id: str | None = None
        self._current_wave: Wave | None = None
        self._attempt_counts: dict[str, int] = {}
        self._executed_options: set[str] = set()

    # -- construction ------------------------------------------------------

    @classmethod
    def fresh(cls, *, general_auth, credentials) -> "TeachingRunner":
        """Build an auth-bound workspace plus its runner.

        The caller passes the SAME ``general_auth`` the panel authenticated
        against and the live proof-of-possession ``credentials``, so command
        authority originates exclusively from a real general-entry login —
        same re-arm pattern as the drill console reset.
        """
        from .auth import GeneralEntryAuth

        if not isinstance(general_auth, GeneralEntryAuth):
            raise TypeError("general_auth must be a GeneralEntryAuth")
        workspace = ScenarioWorkspace("donghe-teaching", general_auth=general_auth)
        seed_baseline_state(workspace)
        workspace.unlock_general_entry(credentials)
        return cls(workspace)

    # -- world answers -----------------------------------------------------

    def _world_act(self, order_id: str) -> WorldDecision:
        """The scripted world decides — never the caller (P9 discipline)."""
        wave = self._current_wave
        if wave is None:
            return WorldDecision(AttemptStatus.SUCCEEDED, ("default scripted success",))
        option_id = self.workspace.orders[order_id].option_id
        spec = wave.outcomes.get(option_id)
        if spec is None:
            return WorldDecision(
                AttemptStatus.SUCCEEDED, ("该方案在本波次无预设世界反馈，默认顺利执行",)
            )
        return WorldDecision(spec.outcome, spec.observations)

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        ws = self.workspace
        done = self.wave_index >= len(self.waves)
        current = None if done else self.waves[self.wave_index]
        return {
            "campaign": self.script.campaign_id,
            "title": self.script.title,
            "wave_count": len(self.waves),
            "wave_index": self.wave_index,
            "done": done,
            "next_wave": None
            if current is None
            else {"wave_id": current.wave_id, "label": current.label},
            "gate": self.completeness_gate(),
            "adopted": sorted(self._executed_options),
        }

    # -- advancing the world ----------------------------------------------

    def advance_wave(self) -> dict:
        """Inject the next wave's data and re-run the staff work."""
        if self.wave_index >= len(self.waves):
            raise TeachingActionError("剧本波次已全部推进完毕")
        wave = self.waves[self.wave_index]
        self._current_wave = wave
        ws = self.workspace

        if self.goal_frame_id is None:
            frame = self.chief.open_goal_frame(ws, self.script.title, priority="high")
            confirmed = self.chief.confirm_goal_frame(ws, frame.id)
            self.goal_frame_id = confirmed.id

        bridged: list[str] = []
        if wave.observations:
            self.driver.load_sequence(list(wave.observations), source="sensor-feed")
            step = self.driver.step()
            bridged = list(step.get("bridged", ()))

        reports_applied = apply_field_reports(ws, wave)

        topic = self.chief.orchestrate(
            ws, f"[{wave.wave_id}] {wave.label}: 下一步有界行动是什么？"
        )
        brief = self.chief.compose_decision_brief(ws, topic, self._goal_frame())
        self.wave_index += 1
        return {
            "status": "advanced",
            "wave_id": wave.wave_id,
            "label": wave.label,
            "observations_bridged": bridged,
            "field_reports_applied": reports_applied,
            "brief_id": brief.id,
        }

    def _goal_frame(self):
        if self.goal_frame_id is None or self.goal_frame_id not in self.workspace.goal_frames:
            raise TeachingActionError("目标框尚未建立，无法参谋")
        return self.workspace.goal_frames[self.goal_frame_id]

    # -- the human decision --------------------------------------------------

    def adopt_option(self, option_id: str, *, reason: str) -> dict:
        """The general adopts an option; run the full power chain, let the
        world answer, and report honestly."""
        ws = self.workspace
        if self._current_wave is None:
            raise TeachingActionError("当前没有待决断的波次")
        option = ws.options.get(option_id)
        if option is None or option.status != "active":
            raise TeachingActionError(f"选项 {option_id} 不存在或未激活")

        decision = ws.record_decision(
            DecisionType.ADOPT,
            option_id,
            reason=reason or f"指挥员批准「{option.title}」",
            scope=self._current_wave.wave_id,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            accepted_risks=tuple(option.risks),
        )

        risk_level = ACTION_RISK.get(option_id, RiskLevel.L2)
        order = ws.draft_order(
            decision.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
        )
        contract = ws.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=risk_level,
            capability_requirements=("simulated-world",),
            parameters=(("option_id", order.option_id), ("decision_id", decision.id)),
            idempotency_key=f"{order.id}:{order.option_id}",
        )
        ws.submit_order_for_confirmation(order.id)
        ws.confirm_order(
            order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID
        )
        approval_id = None
        if risk_level >= RiskLevel.L3:
            approval = ws.record_approval(
                contract.id, actor_role=DecisionActor.COMMANDER, approver=GENERAL_ENTRY_ID
            )
            approval_id = approval.id
        activated = ws.activate_order(order.id)

        attempt_no = self._attempt_counts.get(order.id, 0) + 1
        self._attempt_counts[order.id] = attempt_no
        report = self.adapter.execute(order.id, idempotency_key=f"{order.id}:a{attempt_no}")
        self._executed_options.add(option_id)

        paused = False
        if report.status in _ANOMALY_STATUSES:
            outcome = self.chief.handle_execution_report(ws, order.id, report)
            paused = bool(outcome.get("paused"))
        return {
            "status": "adopted",
            "option_id": option_id,
            "decision_id": decision.id,
            "order_id": activated.id,
            "order_status": activated.status.value,
            "approval_id": approval_id,
            "execution": {
                "outcome": report.status.value,
                "observations": tuple(report.observations),
            },
            "paused_for_anomaly": paused,
        }

    # -- completeness gate ---------------------------------------------------

    def completeness_gate(self) -> dict:
        """Information-completeness view for the right-hand staff column.

        Mirrors doc 07 §3.5: entry conditions (route/water) must be
        field-VERIFIED before the staff will push an approach decision;
        situation conditions count as known once OBSERVED (sensors can never
        verify — that is a system red line, not a UI choice).
        """
        ws = self.workspace
        from .models import ClaimStatus

        rows = []
        entry_blocking: list[str] = []
        situation_open: list[str] = []
        for subject in GATE_SUBJECTS:
            claim = ws.claims.get(subject)
            is_entry = subject in ENTRY_CONDITIONS
            if claim is None:
                state, label = "missing", "未知"
            elif claim.status == ClaimStatus.VERIFIED:
                state, label = "verified", "已核实"
            elif claim.status == ClaimStatus.OBSERVED:
                state, label = "observed", "待核实"
            else:  # UNKNOWN / HYPOTHESIS / CHALLENGED
                state, label = claim.status.value, "缺口"
            if state == "verified":
                pass
            elif state == "observed":
                if is_entry:
                    entry_blocking.append(subject)
                else:
                    situation_open.append(subject)
            else:
                entry_blocking.append(subject) if is_entry else situation_open.append(subject)
            value = "" if claim is None else str(claim.value)
            rows.append(
                {
                    "subject": subject,
                    "label": _GATE_LABELS.get(subject, subject),
                    "state": state,
                    "state_label": label,
                    "value": value,
                    "is_entry": is_entry,
                }
            )
        ready = not entry_blocking
        return {
            "ready": ready,
            "rows": rows,
            "blocking": entry_blocking,
            "situation_open": situation_open,
            "headline": (
                "进入条件已核实，可以形成处置决断"
                if ready
                else "进入条件未核实——先补齐关键条件，再形成处置决断"
            ),
        }


def apply_field_reports(workspace: ScenarioWorkspace, wave: Wave) -> list[str]:
    """Authorized recon reports are the only channel allowed to verify.

    Same semantics as CampaignRunner._apply_field_reports (kept local so the
    teaching runner stays independent of the surrogate-driven campaign).
    """
    applied: list[str] = []
    from .models import Claim, ClaimStatus, Evidence, SourceTrustLevel

    statuses = {"verified": ClaimStatus.VERIFIED, "observed": ClaimStatus.OBSERVED}
    for index, report in enumerate(wave.field_reports, start=1):
        workspace.record_evidence(
            Evidence.create(
                report.evidence_id, report.summary, "recon-alpha", SourceTrustLevel.AUTHORIZED
            )
        )
        status = statuses[report.status]
        if report.subject in workspace.claims:
            workspace.update_claim(
                report.subject,
                value=report.value,
                status=status,
                source="recon-alpha",
                confidence=0.95,
                evidence_ids=(report.evidence_id,),
            )
        else:
            workspace.record_claim(
                Claim(
                    id=f"claim-fr-{workspace.version + 1:03d}-{index:02d}",
                    subject=report.subject,
                    value=report.value,
                    status=status,
                    source="recon-alpha",
                    confidence=0.95,
                    evidence_ids=(report.evidence_id,),
                )
            )
        applied.append(f"{report.subject}={report.value}")
    return applied
