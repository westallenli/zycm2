"""Campaign runner (doc 11 §7 P9-T2): one entry point that drives the whole
chain across a multi-day scripted campaign.

    script (pure data) → ingestion → intake bridge → threshold rules
      → staff council → decision brief → general decision → order chain
      → policy/approval gates → world-driven execution → anomaly interlock
      → verification → review → controlled memory → persist/reload → next day

The general is played by ``GeneralSurrogate``: a deterministic, documented
command policy that walks through the SAME authenticated entry points as a
human commander (offline drill premise; doc 11 §3.5 identity discipline is
never bypassed — every command act lands on the audit trail as the general
entry). It prefers acting on fully verified premises and defers otherwise;
it never invents facts and never overrides policy gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .advisor_core import StaffCouncil
from .auth import GeneralEntryAuth
from .campaign_script import (
    CampaignScript,
    Wave,
    WorldOutcome,
    load_campaign_script,
)
from .chief_advisor import ChiefAdvisor
from .executor_adapter import WorldDrivenExecutorAdapter
from .forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from .ingestion import ObservationStore
from .intake_bridge import IntakeBridge
from .models import (
    AdvisorRole,
    AttemptStatus,
    Claim,
    ClaimStatus,
    DecisionActor,
    DecisionType,
    Evidence,
    FeasibilityStatus,
    GENERAL_ENTRY_ID,
    GeneralQuestion,
    RiskLevel,
    SourceTrustLevel,
)
from .persistence import load_workspace, save_workspace
from .scenario_driver import ScenarioDriver, load_threshold_rules
from .simulator_executor import SimulatorExecutor
from .situation_score import default_scorer
from .workspace import ScenarioWorkspace

DEFAULT_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scenarios" / "campaign_hanjiang_72h.yaml"
)
DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "forest_fire_thresholds.yaml"
)

#: Drill premise: the surrogate carries the general entry secret locally so
#: command acts pass the real authentication gate during rehearsals.
DRILL_SECRET = "campaign-drill-secret"

#: Which simulated actions carry which risk tier (reviewed policy stays in
#: code on purpose: command-relevant parameters never hide in scenario data).
ACTION_RISK: dict[str, RiskLevel] = {"prepare-approach-r2": RiskLevel.L3}

_ANOMALY_STATUSES = {
    AttemptStatus.FAILED,
    AttemptStatus.TIMEOUT,
    AttemptStatus.PERMISSION_DENIED,
    AttemptStatus.UNKNOWN_OUTCOME,
}

_REPORT_STATUS = {
    "verified": ClaimStatus.VERIFIED,
    "observed": ClaimStatus.OBSERVED,
}

_ROUTABLE_KINDS = {"insufficient_evidence", "value_tradeoff", "key_disagreement"}


@dataclass
class CampaignRunResult:
    script: CampaignScript
    workspace: ScenarioWorkspace | None = None
    waves_executed: int = 0
    decision_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    approval_ids: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()
    memory_confirmed: int = 0
    verification_passed: bool | None = None
    reload_checkpoint_version: int | None = None
    reload_audit_intact: bool | None = None
    experience_injection_seen: bool = False
    quality_feedback_kinds: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


def seed_baseline_state(workspace: ScenarioWorkspace) -> None:
    """Campaign-start baseline: the same initial picture as the synthetic
    scenario (map estimates and team status), so scoring components start
    populated instead of unavailable."""
    evidence = (
        Evidence.create("evidence-base-r1", "底图判读：原路线有条件通行。", "campaign-baseline-map"),
        Evidence.create("evidence-base-wind", "基础天气摘要：风力中等。", "campaign-baseline-weather"),
        Evidence.create("evidence-base-fire", "现场判读：火势趋势持续监视。", "campaign-baseline-field"),
        Evidence.create("evidence-base-t1", "一分队状态：具备围护能力。", "campaign-baseline-team"),
        Evidence.create("evidence-base-t2", "预备队状态：待命。", "campaign-baseline-team"),
    )
    for item in evidence:
        workspace.record_evidence(item)
    claims = (
        Claim("claim-route-r1", "route:R1", "conditionally_passable", ClaimStatus.OBSERVED, "campaign-baseline-map", 0.6, ("evidence-base-r1",)),
        Claim("claim-route-r2", "route:R2", "unknown", ClaimStatus.UNKNOWN, "campaign-baseline-map", 0.0),
        Claim("claim-water-w1", "water:W1", "unknown", ClaimStatus.UNKNOWN, "campaign-baseline-map", 0.0),
        Claim("claim-wind", "weather:wind", "medium", ClaimStatus.OBSERVED, "campaign-baseline-weather", 0.7, ("evidence-base-wind",)),
        Claim("claim-fire", "fire:trend", "monitored", ClaimStatus.HYPOTHESIS, "campaign-baseline-field", 0.5, ("evidence-base-fire",)),
        Claim("claim-team-t1", "team:T1", "perimeter_ready", ClaimStatus.OBSERVED, "campaign-baseline-team", 0.9, ("evidence-base-t1",)),
        Claim("claim-team-t2", "team:T2", "standby", ClaimStatus.OBSERVED, "campaign-baseline-team", 0.9, ("evidence-base-t2",)),
    )
    for claim in claims:
        workspace.record_claim(claim)


class GeneralSurrogate:
    """Deterministic stand-in for the general (offline drills only).

    Command policy, deliberately code rather than scenario data:
    1. Prefer the first fully-clean option in PREFERENCE order (act on
       verified premises);
    2. otherwise DEFER explicitly — never adopt unverified premises;
    3. answer open questions honestly and resume after re-checks;
    4. grant approvals bound to one specific action contract, never in bulk.
    """

    PREFERENCE = ("prepare-approach-r2", "maintain-readiness", "verify-conditions")

    def decide(self, workspace: ScenarioWorkspace, *, scope: str) -> object:
        latest_brief = max(
            workspace.decision_briefs.values(), key=lambda b: b.source_version
        )
        clean = {
            row.option_id
            for row in latest_brief.option_matrix
            if row.feasibility_status == FeasibilityStatus.FEASIBLE.value
            and not row.preconditions_unmet
            and not row.no_go_triggered
        }
        chosen = next((opt for opt in self.PREFERENCE if opt in clean), None)
        if chosen is not None:
            option = workspace.options[chosen]
            return workspace.record_decision(
                DecisionType.ADOPT,
                chosen,
                reason=f"采纳「{chosen}」：前提已核验，参谋推荐。",
                scope=scope,
                actor_role=DecisionActor.COMMANDER,
                decided_by=GENERAL_ENTRY_ID,
                accepted_risks=tuple(option.risks),
            )
        return workspace.record_decision(
            DecisionType.DEFER,
            None,
            reason="各方案前提均未完全核验——暂缓，继续收集条件。",
            scope=scope,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
        )

    def approve(self, workspace: ScenarioWorkspace, contract_id: str):
        return workspace.record_approval(
            contract_id, actor_role=DecisionActor.COMMANDER, approver=GENERAL_ENTRY_ID
        )

    def answer(self, workspace: ScenarioWorkspace, question_id: str, answer: str):
        return workspace.answer_general_question(
            question_id, answer, actor_role=DecisionActor.COMMANDER, answered_by=GENERAL_ENTRY_ID
        )

    def confirm_pending_memory(self, workspace: ScenarioWorkspace) -> int:
        confirmed = 0
        for candidate in sorted(workspace.memory_candidates.values(), key=lambda c: c.id):
            if candidate.status.value == "proposed":
                workspace.confirm_memory_candidate(
                    candidate.id,
                    actor_role=DecisionActor.COMMANDER,
                    confirmed_by=GENERAL_ENTRY_ID,
                )
                confirmed += 1
        return confirmed


class CampaignRunner:
    """Runs one campaign script end-to-end and returns an auditable result."""

    def __init__(
        self,
        script: CampaignScript,
        *,
        thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
        secret: str | None = None,
    ) -> None:
        self.script = script
        self.thresholds_path = thresholds_path
        self.rules = load_threshold_rules(thresholds_path)
        # 演练态原本固定用 DRILL_SECRET；显式传入口令时以传入为准，否则启动解锁
        # 会因工作区指纹与登录口令不一致而直接抛 PermissionError。
        self.secret = secret or DRILL_SECRET
        self.result = CampaignRunResult(script=script)
        self._attempt_counts: dict[str, int] = {}
        self._current_wave: Wave | None = None

    # -- public --------------------------------------------------------------

    def run(self) -> CampaignRunResult:
        auth = GeneralEntryAuth(secret=self.secret)
        workspace = ScenarioWorkspace(self.script.campaign_id, general_auth=auth)
        workspace.unlock_general_entry(auth.authenticate(self.secret))
        seed_baseline_state(workspace)
        self.result.workspace = workspace

        chief = ChiefAdvisor(
            StaffCouncil(FOREST_FIRE_PLAYBOOK), situation_scorer=default_scorer()
        )
        goal_frame = chief.open_goal_frame(workspace, self.script.title, priority="high")
        chief.confirm_goal_frame(workspace, goal_frame.id)

        store = ObservationStore()
        bridge = IntakeBridge(store, authorized_sources=self.script.authorized_sources)
        driver = ScenarioDriver(workspace, store, bridge, self.rules, chief)
        adapter = WorldDrivenExecutorAdapter(
            self._world_act, fallback_executor=SimulatorExecutor(workspace)
        )
        surrogate = GeneralSurrogate()

        db_path: Path | None = None
        for day_index, day in enumerate(self.script.days):
            if day_index > 0 and db_path is not None:
                workspace, store, bridge, driver = self._reload(db_path)
                adapter = WorldDrivenExecutorAdapter(
                    self._world_act, fallback_executor=SimulatorExecutor(workspace)
                )

            for wave in day.waves:
                self._current_wave = wave
                self._run_wave(wave, driver, chief, surrogate, adapter, goal_frame)
                self.result.waves_executed += 1

            passed = self._verify_goal(workspace)
            if day == self.script.days[-1]:
                self.result.verification_passed = passed
            review = chief.conduct_review(workspace, scope=f"day-{day.day}")
            self.result.memory_confirmed += surrogate.confirm_pending_memory(workspace)
            feedbacks = chief.review_orchestration_quality(workspace)
            for fb in feedbacks:
                if fb.kind not in self.result.quality_feedback_kinds:
                    self.result.quality_feedback_kinds += (fb.kind,)
            self.result.notes.append(
                f"day-{day.day}: review={review.id}, findings={len(review.findings)}"
            )

            if day_index < len(self.script.days) - 1:
                db_path = self._save_checkpoint(workspace)

        self.result.workspace = workspace
        return self.result

    # -- wave internals --------------------------------------------------------

    def _run_wave(self, wave: Wave, driver: ScenarioDriver, chief: ChiefAdvisor,
                  surrogate: GeneralSurrogate, adapter, goal_frame) -> None:
        workspace = driver.workspace
        if wave.observations:
            driver.load_sequence(list(wave.observations), source="sensor-feed")
            driver.step()
        self._apply_field_reports(workspace, wave)

        # Answer anything still open from earlier waves (anomaly interlocks).
        for question in sorted(workspace.general_questions.values(), key=lambda q: q.id):
            if question.status != "open" or question.kind != "execution_anomaly":
                continue
            surrogate.answer(workspace, question.id, "复核完成，批准恢复推进。")
            workspace.resume_order(question.subject, actor_role=DecisionActor.COMMANDER)
            self._execute_with_retries(workspace, question.subject, chief, adapter, surrogate)

        topic = chief.orchestrate(
            workspace, f"[{wave.wave_id}] {wave.label}：下一步要完成的有限目标是什么？"
        )
        brief = chief.compose_decision_brief(workspace, topic, goal_frame)
        if brief.relevant_experiences:
            self.result.experience_injection_seen = True

        # Raise this brief's escalation items as real questions, then route the
        # answers through classified handling (P7-T4) inside the campaign.
        for item in brief.question_items:
            if item.kind not in {"insufficient_evidence", "value_tradeoff"}:
                continue
            question_id = f"gq-{item.kind}-{wave.wave_id}"
            workspace.raise_general_question(
                GeneralQuestion(
                    id=question_id,
                    kind=item.kind,
                    subject=item.subject or wave.wave_id,
                    question=item.text,
                    context=(),
                    source_version=workspace.version,
                )
            )
            chief.handle_question_answer(
                workspace,
                question_id,
                "指挥员指示：条件核验通过后再推进。",
                actor_role=DecisionActor.COMMANDER,
                answered_by=GENERAL_ENTRY_ID,
            )

        decision = surrogate.decide(workspace, scope=wave.wave_id)
        self.result.decision_ids += (decision.id,)
        if decision.decision_type != DecisionType.ADOPT:
            return
        order = self._issue_order(workspace, decision.id, surrogate)
        self.result.order_ids += (order.id,)
        self._execute_with_retries(workspace, order.id, chief, adapter, surrogate)

    def _apply_field_reports(self, workspace: ScenarioWorkspace, wave: Wave) -> None:
        for index, report in enumerate(wave.field_reports, start=1):
            workspace.record_evidence(
                Evidence.create(
                    report.evidence_id,
                    report.summary,
                    "recon-alpha",
                    SourceTrustLevel.AUTHORIZED,
                )
            )
            status = _REPORT_STATUS[report.status]
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

    def _issue_order(self, workspace: ScenarioWorkspace, decision_id: str, surrogate) -> object:
        decision = workspace.decisions[decision_id]
        risk_level = ACTION_RISK.get(decision.option_id or "", RiskLevel.L2)
        order = workspace.draft_order(
            decision_id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
        )
        contract = workspace.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=risk_level,
            capability_requirements=("simulated-world",),
            parameters=(("option_id", order.option_id), ("decision_id", decision_id)),
            idempotency_key=f"{order.id}:{order.option_id}",
        )
        workspace.submit_order_for_confirmation(order.id)
        workspace.confirm_order(
            order.id, actor_role=DecisionActor.COMMANDER, confirmed_by=GENERAL_ENTRY_ID
        )
        if risk_level >= RiskLevel.L3:
            approval = surrogate.approve(workspace, contract.id)
            self.result.approval_ids += (approval.id,)
        return workspace.activate_order(order.id)

    def _execute_with_retries(self, workspace: ScenarioWorkspace, order_id: str,
                              chief: ChiefAdvisor, adapter, surrogate: GeneralSurrogate) -> None:
        while True:
            attempt_no = self._attempt_counts.get(order_id, 0) + 1
            self._attempt_counts[order_id] = attempt_no
            report = adapter.execute(order_id, idempotency_key=f"{order_id}:a{attempt_no}")
            if report.status not in _ANOMALY_STATUSES:
                return
            self.result.anomalies += (f"{order_id}:{report.status.value}",)
            outcome = chief.handle_execution_report(workspace, order_id, report)
            if not outcome.get("paused"):
                return
            option_id = workspace.orders[order_id].option_id
            retry_plan = (
                self._current_wave.retries.get(option_id) if self._current_wave else None
            )
            if retry_plan is None:
                return  # stays paused; the open question waits for the general
            surrogate.answer(workspace, outcome["question_id"], retry_plan.answer)
            workspace.resume_order(order_id, actor_role=DecisionActor.COMMANDER)

    def _world_act(self, order_id: str) -> WorldOutcome:
        """The world decides outcomes from the script — never from the caller."""
        wave = self._current_wave
        if wave is None:
            return WorldOutcome(AttemptStatus.SUCCEEDED, ("default scripted success",))
        option_id = self._workspace.orders[order_id].option_id
        attempt_no = self._attempt_counts.get(order_id, 0)
        if attempt_no <= 1:
            spec = wave.outcomes.get(option_id)
            if spec is not None:
                return spec
            return WorldOutcome(AttemptStatus.SUCCEEDED, ("default scripted success",))
        retry = wave.retries.get(option_id)
        if retry is not None:
            return retry.world
        return WorldOutcome(AttemptStatus.SUCCEEDED, ("retry defaulted to success",))

    @property
    def _workspace(self) -> ScenarioWorkspace:
        assert self.result.workspace is not None, "run() must start before execution"
        return self.result.workspace

    # -- day boundaries ----------------------------------------------------------

    def _save_checkpoint(self, workspace: ScenarioWorkspace) -> Path:
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="panda-campaign-")
        db_path = Path(tmp_dir) / "checkpoint.db"
        save_workspace(workspace, db_path)
        self.result.reload_checkpoint_version = workspace.version
        return db_path

    def _reload(self, db_path: Path):
        reload_auth = GeneralEntryAuth(secret=self.secret)
        store = ObservationStore()
        restored = load_workspace(
            db_path, general_auth=reload_auth, observation_store=store
        )
        restored.unlock_general_entry(reload_auth.authenticate(self.secret))
        intact = not restored.verify_audit_chain()
        self.result.reload_audit_intact = (
            intact if self.result.reload_audit_intact is None else
            (self.result.reload_audit_intact and intact)
        )
        bridge = IntakeBridge(store, authorized_sources=self.script.authorized_sources)
        rules = load_threshold_rules(self.thresholds_path)
        chief = ChiefAdvisor(
            StaffCouncil(FOREST_FIRE_PLAYBOOK), situation_scorer=default_scorer()
        )
        self.result.workspace = restored
        driver = ScenarioDriver(restored, store, bridge, rules, chief)
        return restored, store, bridge, driver

    def _verify_goal(self, workspace: ScenarioWorkspace) -> bool:
        route = workspace.claims.get("route:R2")
        water = workspace.claims.get("water:W1")
        fire = workspace.claims.get("fire:trend")
        unmet: list[str] = []
        if route is None or route.status != ClaimStatus.VERIFIED:
            unmet.append("alternative route is not verified")
        if water is None or water.status != ClaimStatus.VERIFIED:
            unmet.append("water source is not verified")
        if fire is None or fire.value != "controlled":
            unmet.append("fire trend is not independently verified as controlled")
        evidence = tuple(
            eid
            for claim in (route, water, fire)
            if claim is not None
            for eid in claim.evidence_ids
        )
        result = workspace.record_verification(not unmet, tuple(unmet), evidence)
        return result.passed


def run_campaign(
    script_path: str | Path | None = None,
    *,
    expected_domain: str = FOREST_FIRE_PLAYBOOK.name,
    thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
    secret: str | None = None,
) -> CampaignRunResult:
    path = Path(script_path) if script_path is not None else DEFAULT_SCRIPT_PATH
    script = load_campaign_script(path, expected_domain=expected_domain)
    return CampaignRunner(
        script, thresholds_path=thresholds_path, secret=secret).run()
