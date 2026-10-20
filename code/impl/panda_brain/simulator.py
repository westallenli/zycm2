"""Synthetic E0-E4 forest-fire command-staff scenario."""

from __future__ import annotations

from .advisor_core import StaffCouncil
from .forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from .forest_fire_roles import FOREST_FIRE_ROLE_BINDINGS
from .models import (
    AdvisoryTopic,
    AttemptStatus,
    Claim,
    ClaimStatus,
    CommanderFeedback,
    DecisionActor,
    DecisionType,
    Evidence,
    FeedbackType,
    GENERAL_ENTRY_ID,
    InputClass,
    SourceTrustLevel,
)
from .simulator_executor import SimulatorExecutor
from .workspace import ScenarioWorkspace


class ForestFireScenario:
    """A deterministic offline scenario. It cannot contact external systems."""

    def __init__(self) -> None:
        self.workspace = ScenarioWorkspace("synthetic-southeast-fireline")
        self.council = StaffCouncil(FOREST_FIRE_PLAYBOOK)
        self.executor = SimulatorExecutor(self.workspace)
        self.role_bindings = FOREST_FIRE_ROLE_BINDINGS
        self.workspace.append_event(
            "ScenarioDomainMapped",
            domain="forest_fire",
            bindings={
                role.value: binding.advisor_role.value if binding.advisor_role else None
                for role, binding in self.role_bindings.items()
            },
        )
        self.workspace.append_event("ScenarioStarted", scenario_id=self.workspace.scenario_id)
        self._seed_initial_state()

    def apply_e0(self) -> None:
        self.workspace.append_event("IntelReceived", event_name="E0")
        self._review("E0", "Control the changing objective safely.", "What should the staff clarify before the next decision?")

    def apply_e1_route_blocked(self) -> None:
        self.apply_e1_facts()
        self.workspace.append_event("IntelReceived", event_name="E1")
        self._review("E1", "Control the changing objective safely.", "How does the new observation affect existing options?")

    def apply_e1_facts(self) -> None:
        """Inject the E1 road observation without convening the council (used by the P2 comparison)."""
        self.workspace.record_evidence(
            Evidence.create(
                "evidence-e1-road",
                "Synthetic road observation: the original access route is blocked.",
                "synthetic-road-observation",
            )
        )
        self.workspace.update_claim(
            "route:R1",
            value="blocked",
            status=ClaimStatus.CHALLENGED,
            source="synthetic-road-observation",
            confidence=0.9,
            evidence_ids=("evidence-e1-road",),
        )

    def apply_e2_wind_and_fire_increase(self) -> None:
        self.workspace.record_evidence(
            Evidence.create(
                "evidence-e2-wind",
                "Synthetic weather observation: wind has risen to high.",
                "synthetic-weather-observation",
            )
        )
        self.workspace.record_evidence(
            Evidence.create(
                "evidence-e2-fire",
                "Synthetic fire observation: the fire trend is expanding.",
                "synthetic-fire-observation",
            )
        )
        self.workspace.update_claim(
            "weather:wind",
            value="high",
            status=ClaimStatus.OBSERVED,
            source="synthetic-weather-observation",
            confidence=0.9,
            evidence_ids=("evidence-e2-wind",),
        )
        self.workspace.update_claim(
            "fire:trend",
            value="expanding",
            status=ClaimStatus.OBSERVED,
            source="synthetic-fire-observation",
            confidence=0.85,
            evidence_ids=("evidence-e2-fire",),
        )
        self.workspace.append_event("IntelReceived", event_name="E2")
        self._review("E2", "Control the changing objective safely.", "What is the next decision after the risk trend changed?")

    def apply_e3_conditions_verified(self) -> None:
        self.workspace.record_evidence(
            Evidence.create(
                "evidence-e3-r2",
                "Synthetic route check: the alternative route is passable.",
                "synthetic-route-check",
                SourceTrustLevel.AUTHORIZED,
            )
        )
        self.workspace.record_evidence(
            Evidence.create(
                "evidence-e3-w1",
                "Synthetic water check: the water source is available.",
                "synthetic-water-check",
                SourceTrustLevel.AUTHORIZED,
            )
        )
        self.workspace.update_claim(
            "route:R2",
            value="passable",
            status=ClaimStatus.VERIFIED,
            source="synthetic-route-check",
            confidence=0.95,
            evidence_ids=("evidence-e3-r2",),
        )
        self.workspace.update_claim(
            "water:W1",
            value="available",
            status=ClaimStatus.VERIFIED,
            source="synthetic-water-check",
            confidence=0.95,
            evidence_ids=("evidence-e3-w1",),
        )
        self.workspace.append_event("IntelReceived", event_name="E3")
        self._review("E3", "Control the changing objective safely.", "Which bounded options are now supported by verified conditions?")

    def commander_decision(self, decision_type: DecisionType, option_id: str | None, reason: str, scope: str, *, accepted_risks: tuple[str, ...] | None = None):
        proposal_id = next(
            (
                proposal.id
                for proposal in reversed(tuple(self.workspace.proposals.values()))
                if option_id is not None and option_id in proposal.option_ids
            ),
            None,
        )
        if accepted_risks is None and decision_type == DecisionType.ADOPT and option_id is not None:
            option = self.workspace.options.get(option_id)
            accepted_risks = tuple(option.risks) if option is not None else ()
        return self.workspace.record_decision(
            decision_type,
            option_id,
            reason,
            scope,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            accepted_risks=tuple(accepted_risks or ()),
            proposal_id=proposal_id,
        )

    def commander_feedback(
        self,
        feedback_type: FeedbackType,
        message: str,
        *,
        input_class: InputClass = InputClass.FORMAL_CORRECTION,
        topic_id: str | None = None,
        proposal_id: str | None = None,
        option_id: str | None = None,
        claim_updates: tuple[Claim, ...] = (),
    ) -> CommanderFeedback:
        topic_id = topic_id or next(reversed(self.workspace.topics), None)
        proposal_id = proposal_id or next(reversed(self.workspace.proposals), None)
        feedback = CommanderFeedback(
            id=f"feedback-{self.workspace.version + 1:03d}",
            feedback_type=feedback_type,
            message=message,
            source_version=self.workspace.version,
            actor_role=DecisionActor.COMMANDER,
            decided_by=GENERAL_ENTRY_ID,
            input_class=input_class,
            topic_id=topic_id,
            proposal_id=proposal_id,
            option_id=option_id,
            claim_updates=claim_updates,
        )
        return self.workspace.record_feedback(feedback)

    def reconsider_after_feedback(self, event_name: str = "CommanderFeedback") -> None:
        self._review(
            event_name,
            "Control the changing objective safely.",
            "How should the staff revise its advice after the commander's feedback?",
        )

    def issue_order(self, decision_id: str):
        return self.workspace.issue_order(decision_id)

    def simulate_e4(self, order_id: str, outcome: AttemptStatus = AttemptStatus.SUCCEEDED):
        if outcome == AttemptStatus.UNKNOWN_OUTCOME:
            observations = ("synthetic communications interruption; result requires reconciliation",)
        elif outcome == AttemptStatus.PARTIAL:
            observations = ("synthetic partial completion; fire trend remains unverified",)
        elif outcome == AttemptStatus.TIMEOUT:
            observations = ("synthetic action exceeded its time budget before any result",)
        elif outcome == AttemptStatus.PERMISSION_DENIED:
            observations = ("synthetic authority check denied the action in the field",)
        elif outcome == AttemptStatus.FAILED:
            observations = ("synthetic action failed; perimeter unchanged",)
        else:
            observations = ("synthetic team reached perimeter; fire trend has not declined",)
        report = self.executor.execute(order_id, outcome, observations)
        self.workspace.append_event("ObservationCaptured", report_id=report.id)
        return report

    def verify_goal(self):
        route = self.workspace.claims["route:R2"]
        water = self.workspace.claims["water:W1"]
        fire_trend = self.workspace.claims["fire:trend"]
        unmet = []
        if route.status != ClaimStatus.VERIFIED:
            unmet.append("alternative route is not verified")
        if water.status != ClaimStatus.VERIFIED:
            unmet.append("water source is not verified")
        if fire_trend.value != "controlled":
            unmet.append("fire trend is not independently verified as controlled")
        evidence = tuple(route.evidence_ids + water.evidence_ids + fire_trend.evidence_ids)
        return self.workspace.record_verification(not unmet, tuple(unmet), evidence)

    def replay(self):
        return self.workspace.replay()

    def _seed_initial_state(self) -> None:
        evidence = (
            Evidence.create("evidence-e0-r1", "Synthetic initial observation of the original route.", "synthetic-initial-observation"),
            Evidence.create("evidence-e0-wind", "Synthetic initial weather observation.", "synthetic-weather-observation"),
            Evidence.create("evidence-e0-fire", "Synthetic initial fire-trend field estimate.", "synthetic-field-estimate"),
            Evidence.create("evidence-e0-t1", "Synthetic team T1 status report.", "synthetic-team-status"),
            Evidence.create("evidence-e0-t2", "Synthetic team T2 status report.", "synthetic-team-status"),
        )
        for item in evidence:
            self.workspace.record_evidence(item)
        claims = (
            Claim("claim-route-r1", "route:R1", "conditionally_passable", ClaimStatus.OBSERVED, "synthetic-initial-observation", 0.6, ("evidence-e0-r1",)),
            Claim("claim-route-r2", "route:R2", "unknown", ClaimStatus.UNKNOWN, "synthetic-map", 0.0),
            Claim("claim-water-w1", "water:W1", "unknown", ClaimStatus.UNKNOWN, "synthetic-map", 0.0),
            Claim("claim-wind", "weather:wind", "medium", ClaimStatus.OBSERVED, "synthetic-weather-observation", 0.7, ("evidence-e0-wind",)),
            Claim("claim-fire", "fire:trend", "monitored", ClaimStatus.HYPOTHESIS, "synthetic-field-estimate", 0.5, ("evidence-e0-fire",)),
            Claim("claim-team-t1", "team:T1", "perimeter_ready", ClaimStatus.OBSERVED, "synthetic-team-status", 0.9, ("evidence-e0-t1",)),
            Claim("claim-team-t2", "team:T2", "standby", ClaimStatus.OBSERVED, "synthetic-team-status", 0.9, ("evidence-e0-t2",)),
        )
        for claim in claims:
            self.workspace.record_claim(claim)

    def _review(self, event_name: str, objective: str, question: str) -> None:
        topic = AdvisoryTopic(
            id=f"topic-{event_name.lower()}-{self.workspace.version + 1:03d}",
            objective=objective,
            question=question,
            constraints=("commander retains final decision authority", "offline simulation only"),
            source_version=self.workspace.version,
        )
        self.council.review(self.workspace, topic)
