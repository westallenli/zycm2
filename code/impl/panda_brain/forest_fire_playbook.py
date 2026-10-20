"""Forest-fire domain content for the generic staff council.

Domain staffing and materials come from the reviewed YAML configuration
(``config/forest_fire_playbook.yaml``); this module holds only the behavior
that turns configuration plus workspace state into assessments and options.
The council itself stays domain-neutral (doc 11 §3.3 layering).
"""

from __future__ import annotations

from pathlib import Path

from .advisor_core import DomainPlaybook, SpecialistSpec
from .forest_fire_roles import ForestFireCommandPostRole
from .models import (
    AdvisorRole,
    Assessment,
    ClaimStatus,
    Disagreement,
    FeasibilityStatus,
    FeasibilityVerdict,
    IntelItem,
    Option,
    Reversibility,
    StaffWorkType,
)
from .playbook_config import PlaybookConfig, PlaybookConfigError, load_playbook_config
from .workspace import ScenarioWorkspace

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "forest_fire_playbook.yaml"

#: agent_id → (builder factory, command-post role). Adding a specialist whose
#: behavior is not covered here requires code by design; everything else about
#: it lives in the reviewed YAML.
_COMMAND_POST_ROLES = {
    "ResearchAgent": ForestFireCommandPostRole.TECHNICAL_SUPPORT,
    "AnalysisAgent": ForestFireCommandPostRole.PROFESSIONAL_DEPUTY,
    "RedTeamAgent": ForestFireCommandPostRole.SECURITY_CONTROL,
    "DispatchChiefAgent": ForestFireCommandPostRole.DISPATCH_CHIEF,
    "LogisticsSupportAgent": ForestFireCommandPostRole.LOGISTICS_SUPPORT,
    "FireActionAgent": ForestFireCommandPostRole.FIRE_ACTION,
    "InformationReportingAgent": ForestFireCommandPostRole.INFORMATION_REPORTING,
}


def _open_condition_subjects(workspace: ScenarioWorkspace, cfg: PlaybookConfig) -> tuple[str, ...]:
    return tuple(
        subject
        for subject in cfg.condition_subjects
        if workspace.claims[subject].status != ClaimStatus.VERIFIED
    )


def _make_intel_builder(cfg: PlaybookConfig):
    def build_intel(workspace: ScenarioWorkspace) -> list[IntelItem]:
        return [
            IntelItem(
                id=f"intel-{claim.id}",
                subject=claim.subject,
                summary=f"Observed {claim.subject}={claim.value!r}.",
                source=claim.source,
                claim_id=claim.id,
                evidence_ids=claim.evidence_ids,
                source_version=workspace.version,
            )
            for claim in workspace.claims.values()
        ]

    return build_intel


def _make_research_builder(cfg: PlaybookConfig):
    def build(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        return Assessment(
            id=f"research-{workspace.version + 1:03d}",
            agent="ResearchAgent",
            summary=cfg.text("research_summary"),
            evidence_ids=tuple(claim.id for claim in workspace.claims.values()),
            unknowns=_open_condition_subjects(workspace, cfg),
            advisor_role=AdvisorRole.RESEARCH,
            domain_role=_COMMAND_POST_ROLES["ResearchAgent"].value,
        )

    return build


def _make_analysis_builder(cfg: PlaybookConfig):
    rules = cfg.section("analysis_rules")
    wind_subject = cfg.resolve_claim(rules["wind_ref"])
    fire_subject = cfg.resolve_claim(rules["fire_trend_ref"])
    challenged = {ClaimStatus(value) for value in rules["challenged_statuses"]}

    def build_analysis(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        claims = workspace.claims
        route_original = claims[cfg.resolve_claim("route_original")]
        risks: list[str] = list(feedback_risks)
        if route_original.status in challenged:
            risks.append(rules["challenged_basis_risk"])
        if claims[wind_subject].value == rules["wind_high_value"] and claims[fire_subject].value == rules["fire_expanding_value"]:
            risks.append(rules["weather_fire_risk"])
        evidence_refs = ("route_original", "route_alternative", "water", "wind", "fire_trend")
        return Assessment(
            id=f"analysis-{workspace.version + 1:03d}",
            agent="AnalysisAgent",
            summary=cfg.text("analysis_summary"),
            evidence_ids=tuple(claims[cfg.resolve_claim(ref)].id for ref in evidence_refs),
            risks=tuple(risks),
            unknowns=_open_condition_subjects(workspace, cfg),
            advisor_role=AdvisorRole.ANALYSIS,
            domain_role=_COMMAND_POST_ROLES["AnalysisAgent"].value,
        )

    return build_analysis


def _make_red_team_builder(cfg: PlaybookConfig, analysis_builder):
    unverified_rules = tuple(
        (rule["subject"], rule["risk"]) for rule in cfg.section("red_team_rules")["unverified_risks"]
    )

    def build_red_team(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        claims = workspace.claims
        risks = list(analysis_builder(workspace, feedback_risks).risks)
        for subject, risk_text in unverified_rules:
            if claims[subject].status != ClaimStatus.VERIFIED:
                risks.append(risk_text)
        return Assessment(
            id=f"redteam-{workspace.version + 1:03d}",
            agent="RedTeamAgent",
            summary=cfg.text("red_team_summary"),
            evidence_ids=tuple(claims[subject].id for subject, _ in unverified_rules),
            risks=tuple(dict.fromkeys(risks)),
            unknowns=_open_condition_subjects(workspace, cfg),
            advisor_role=AdvisorRole.RED_TEAM,
            domain_role=_COMMAND_POST_ROLES["RedTeamAgent"].value,
        )

    return build_red_team


def _make_dispatch_builder(cfg: PlaybookConfig):
    spec = cfg.section("dispatch")

    def build_dispatch(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        claims = workspace.claims
        unknowns = _open_condition_subjects(workspace, cfg)
        dispatch_risks = tuple(
            risk
            for risk in (
                spec["outstanding_risk"] if unknowns else spec["clear_risk"],
                spec["standing_risk"],
            )
        )
        return Assessment(
            id=f"dispatch-{workspace.version + 1:03d}",
            agent="DispatchChiefAgent",
            summary=spec["summary"],
            evidence_ids=tuple(claims[subject].id for subject in cfg.resolve_claims(spec["evidence_claim_refs"])),
            risks=dispatch_risks,
            unknowns=unknowns,
            advisor_role=AdvisorRole.CHIEF_ADVISOR,
            domain_role=_COMMAND_POST_ROLES["DispatchChiefAgent"].value,
        )

    return build_dispatch


def _make_logistics_builder(cfg: PlaybookConfig):
    spec = cfg.section("logistics")
    watch_subject = cfg.resolve_claim(spec["watch_ref"])

    def build_logistics(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        claims = workspace.claims
        watch_claim = claims[watch_subject]
        return Assessment(
            id=f"logistics-{workspace.version + 1:03d}",
            agent="LogisticsSupportAgent",
            summary=spec["summary"],
            evidence_ids=tuple(claims[subject].id for subject in cfg.resolve_claims(spec["evidence_claim_refs"])),
            risks=(spec["risk"],),
            unknowns=(watch_subject,) if watch_claim.status != ClaimStatus.VERIFIED else (),
            advisor_role=AdvisorRole.FEASIBILITY,
            domain_role=_COMMAND_POST_ROLES["LogisticsSupportAgent"].value,
        )

    return build_logistics


def _make_fire_action_builder(cfg: PlaybookConfig):
    spec = cfg.section("fire_action")

    def build_fire_action(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        claims = workspace.claims
        return Assessment(
            id=f"fire-action-{workspace.version + 1:03d}",
            agent="FireActionAgent",
            summary=spec["summary"],
            evidence_ids=tuple(claims[subject].id for subject in cfg.resolve_claims(spec["evidence_claim_refs"])),
            risks=(spec["risk"],),
            unknowns=_open_condition_subjects(workspace, cfg),
            advisor_role=AdvisorRole.FEASIBILITY,
            domain_role=_COMMAND_POST_ROLES["FireActionAgent"].value,
        )

    return build_fire_action


def _make_information_builder(cfg: PlaybookConfig):
    spec = cfg.section("information")

    def build_information(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> Assessment:
        return Assessment(
            id=f"information-{workspace.version + 1:03d}",
            agent="InformationReportingAgent",
            summary=spec["summary"],
            evidence_ids=tuple(claim.id for claim in workspace.claims.values()),
            risks=(spec["risk"],),
            unknowns=_open_condition_subjects(workspace, cfg),
            advisor_role=AdvisorRole.MEMORY,
            domain_role=_COMMAND_POST_ROLES["InformationReportingAgent"].value,
        )

    return build_information


def _make_disagreement_builder(cfg: PlaybookConfig):
    spec = cfg.section("disagreement")

    def build_disagreement(workspace: ScenarioWorkspace) -> Disagreement:
        claims = workspace.claims
        return Disagreement(
            id=f"disagreement-{workspace.version + 1:03d}",
            topic=spec["topic"],
            positions=dict(spec["positions"]),
            evidence_ids=tuple(claims[subject].id for subject in cfg.resolve_claims(spec["evidence_claim_refs"])),
        )

    return build_disagreement


def _option_risks(source: str, analysis_risks, red_team_risks) -> tuple[str, ...]:
    return tuple(dict.fromkeys(red_team_risks if source == "red_team" else analysis_risks))


def _show_when_allows(entry: dict, workspace: ScenarioWorkspace) -> bool:
    show_when = entry["show_when"]
    if show_when == "always":
        return True
    subjects = show_when.get("all_claims_verified", ())
    return all(
        subject in workspace.claims and workspace.claims[subject].status == ClaimStatus.VERIFIED
        for subject in subjects
    )


def _make_options_builder(cfg: PlaybookConfig, analysis_builder, red_team_builder):
    catalogue = cfg.options

    def build_options(workspace: ScenarioWorkspace, feedback_risks: tuple[str, ...]) -> list[Option]:
        claims = workspace.claims
        red_team_risks = list(red_team_builder(workspace, feedback_risks).risks)
        analysis_risks = list(analysis_builder(workspace, feedback_risks).risks)

        options: list[Option] = []
        for entry in catalogue:
            if not _show_when_allows(entry, workspace):
                continue
            options.append(
                Option(
                    id=entry["id"],
                    title=entry["title"],
                    summary=entry["summary"],
                    evidence_ids=tuple(claims[subject].id for subject in cfg.resolve_claims(entry["evidence_claim_refs"])),
                    claim_subjects=tuple(entry["claim_subjects"]),
                    risks=_option_risks(entry["risk_source"], analysis_risks, red_team_risks),
                    proposer_role=AdvisorRole.OPTION,
                    domain_role=entry["domain_role"],
                    preconditions=tuple(entry["preconditions"]),
                    cost_estimate=entry["cost_estimate"],
                    time_estimate=entry["time_estimate"],
                    reversibility=Reversibility(entry["reversibility"]),
                    no_go_conditions=tuple(entry["no_go_conditions"]),
                )
            )
        return options

    return build_options


def _make_feasibility_builder(cfg: PlaybookConfig):
    notes = cfg.feasibility_notes
    verdict_specs = []
    for entry in cfg.feasibility_entries:
        blocking = tuple(cfg.resolve_claims(entry.get("blocking_refs", ())))
        verdict_specs.append((entry["option_id"], FeasibilityStatus(entry["status"]), blocking))

    def build_feasibility_verdicts(workspace: ScenarioWorkspace) -> list[FeasibilityVerdict]:
        suffix = f"{workspace.version + 1:03d}"
        verdicts: list[FeasibilityVerdict] = []
        for option_id, base_status, blocking in verdict_specs:
            if option_id not in workspace.options:
                continue
            unmet = tuple(subject for subject in blocking if workspace.claims[subject].status != ClaimStatus.VERIFIED)
            status = FeasibilityStatus.UNKNOWN if unmet else base_status
            verdicts.append(
                FeasibilityVerdict(
                    id=f"verdict-{suffix}-{option_id}",
                    option_id=option_id,
                    status=status,
                    blocking_conditions=unmet,
                    notes=notes,
                    source_version=workspace.version,
                )
            )
        return verdicts

    return build_feasibility_verdicts


def build_playbook(cfg: PlaybookConfig) -> DomainPlaybook:
    """Assemble a DomainPlaybook from validated configuration."""
    builders = {
        "ResearchAgent": _make_research_builder(cfg),
        "AnalysisAgent": _make_analysis_builder(cfg),
    }
    builders["RedTeamAgent"] = _make_red_team_builder(cfg, builders["AnalysisAgent"])

    generic_factories = {
        "DispatchChiefAgent": _make_dispatch_builder,
        "LogisticsSupportAgent": _make_logistics_builder,
        "FireActionAgent": _make_fire_action_builder,
        "InformationReportingAgent": _make_information_builder,
    }
    for agent_id, factory in generic_factories.items():
        builders[agent_id] = factory(cfg)

    specialists = []
    for entry in cfg.staffing:
        agent_id = entry["agent_id"]
        builder = builders.get(agent_id)
        if builder is None:
            raise PlaybookConfigError(
                f"No behavior implementation registered for specialist {agent_id!r};"
                " add one in code before listing it in the reviewed config."
            )
        specialists.append(
            SpecialistSpec(
                agent_id=agent_id,
                role=AdvisorRole(entry["role"]),
                work_type=StaffWorkType(entry["work_type"]),
                question_to_answer=entry["question_to_answer"],
                expected_deliverable=entry["expected_deliverable"],
                dependencies=tuple(entry["dependencies"]),
                forbidden_actions=tuple(entry.get("forbidden_actions", ())),
                build=builder,
            )
        )

    return DomainPlaybook(
        name=cfg.name,
        objective=cfg.objective,
        specialists=tuple(specialists),
        option_dependencies=cfg.option_dependencies,
        build_intel=_make_intel_builder(cfg),
        build_options=_make_options_builder(cfg, builders["AnalysisAgent"], builders["RedTeamAgent"]),
        build_disagreement=_make_disagreement_builder(cfg),
        build_feasibility_verdicts=_make_feasibility_builder(cfg),
    )


def load_forest_fire_playbook(path: str | Path | None = None) -> DomainPlaybook:
    return build_playbook(load_playbook_config(path or DEFAULT_CONFIG_PATH))


FOREST_FIRE_PLAYBOOK = load_forest_fire_playbook()
