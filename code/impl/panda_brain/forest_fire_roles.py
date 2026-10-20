"""Forest-fire domain role bindings kept outside the generic advisor core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import AdvisorRole


class ForestFireCommandPostRole(StrEnum):
    COMMANDER = "commander"
    PROFESSIONAL_DEPUTY = "professional_deputy"
    DISPATCH_CHIEF = "dispatch_chief"
    TECHNICAL_SUPPORT = "technical_support"
    FIRE_ACTION = "fire_action"
    SECURITY_CONTROL = "security_control"
    LOGISTICS_SUPPORT = "logistics_support"
    INFORMATION_REPORTING = "information_reporting"


@dataclass(frozen=True)
class ForestFireRoleBinding:
    domain_role: ForestFireCommandPostRole
    advisor_role: AdvisorRole | None
    usage: str


FOREST_FIRE_ROLE_BINDINGS: dict[ForestFireCommandPostRole, ForestFireRoleBinding] = {
    ForestFireCommandPostRole.COMMANDER: ForestFireRoleBinding(
        ForestFireCommandPostRole.COMMANDER,
        None,
        "The commander is the final business decision maker, not an advisor agent.",
    ),
    ForestFireCommandPostRole.PROFESSIONAL_DEPUTY: ForestFireRoleBinding(
        ForestFireCommandPostRole.PROFESSIONAL_DEPUTY,
        AdvisorRole.ANALYSIS,
        "Provides professional fire behavior and operation analysis.",
    ),
    ForestFireCommandPostRole.DISPATCH_CHIEF: ForestFireRoleBinding(
        ForestFireCommandPostRole.DISPATCH_CHIEF,
        AdvisorRole.CHIEF_ADVISOR,
        "Coordinates the advisor council and prepares decision material.",
    ),
    ForestFireCommandPostRole.TECHNICAL_SUPPORT: ForestFireRoleBinding(
        ForestFireCommandPostRole.TECHNICAL_SUPPORT,
        AdvisorRole.RESEARCH,
        "Collects and validates fire-ground information.",
    ),
    ForestFireCommandPostRole.FIRE_ACTION: ForestFireRoleBinding(
        ForestFireCommandPostRole.FIRE_ACTION,
        AdvisorRole.FEASIBILITY,
        "Reports simulated action feasibility and observations.",
    ),
    ForestFireCommandPostRole.SECURITY_CONTROL: ForestFireRoleBinding(
        ForestFireCommandPostRole.SECURITY_CONTROL,
        AdvisorRole.RED_TEAM,
        "Challenges unsafe routes, airspace, traffic, and public-order assumptions.",
    ),
    ForestFireCommandPostRole.LOGISTICS_SUPPORT: ForestFireRoleBinding(
        ForestFireCommandPostRole.LOGISTICS_SUPPORT,
        AdvisorRole.FEASIBILITY,
        "Checks reserves, water, medical, communications, and supplies.",
    ),
    ForestFireCommandPostRole.INFORMATION_REPORTING: ForestFireRoleBinding(
        ForestFireCommandPostRole.INFORMATION_REPORTING,
        AdvisorRole.MEMORY,
        "Prepares reviewed situation-report and release drafts.",
    ),
}
