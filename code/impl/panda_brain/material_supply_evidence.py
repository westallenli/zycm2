"""Read-only evidence gate for material supply leads (M04).

A registered stock figure or a company listing is a lead, not a verified
delivery promise. This module never creates a replenishment task or changes
event inventory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PointLead:
    point_id: str
    name: str
    stock: float | None
    source: str
    updated_at: str
    eta_load: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class CompanyLead:
    company_id: str
    name: str
    services: str
    source: str
    source_updated_at: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class SupplyEvidenceReview:
    item_id: str
    required_quantity: float | None
    points: tuple[PointLead, ...]
    companies: tuple[CompanyLead, ...]
    conclusion: str = "可补给性待核实"


def review_supply_leads(ledger, *, item_id: str,
                        required_quantity: float | None = None) -> SupplyEvidenceReview:
    """Inspect registered leads for one item without inferring availability.

    ``required_quantity`` must come from a separately verified formal shortage.
    Without it, registered stock cannot prove sufficiency. Even with it, an
    arrival time and a current stock confirmation are still required.
    """
    if item_id not in ledger.items:
        return SupplyEvidenceReview(item_id, None, (), ())
    if (required_quantity is not None
            and (type(required_quantity) not in {int, float}
                 or not math.isfinite(required_quantity) or required_quantity <= 0)):
        raise ValueError("正式缺口数量须为正数。")

    points = []
    for point in sorted(ledger.points.values(), key=lambda value: value.point_id):
        if item_id not in point.supplies:
            continue
        raw_stock = point.supplies[item_id]
        stock = (float(raw_stock) if type(raw_stock) in {int, float}
                 and math.isfinite(raw_stock) and raw_stock >= 0 else None)
        blockers = []
        if point.open_status != "开放":
            blockers.append("物资点未登记为开放")
        if stock is None:
            blockers.append("库存数量未知或无效")
        elif stock == 0:
            blockers.append("登记库存为零")
        elif required_quantity is not None and stock < required_quantity:
            blockers.append("登记数量不足")
        elif required_quantity is None:
            blockers.append("正式缺口数量待核实")
        blockers.extend(("库存来源及当前时效待核实", "送达时间待核实"))
        points.append(PointLead(point.point_id, point.name, stock, point.source_word(),
                                point.updated_at, point.eta_load, tuple(blockers)))

    companies = tuple(CompanyLead(
        company.company_id, company.name, company.services, company.source,
        company.source_updated_at,
        ("清单不证明该物资可供", "供应能力与数量待联系核实", "送达时间待核实"),
    ) for company in sorted(ledger.companies.values(), key=lambda value: value.company_id))
    return SupplyEvidenceReview(item_id, required_quantity, tuple(points), companies)
