"""Fixed-world offline executor (doc 11 §7 P4).

The executor accepts only an active order backed by an action contract, and
re-checks the L3+ approval gate at its own boundary (defence in depth).
Replaying an idempotency key returns the cached report without duplicating
side effects (doc 11 §6.4 recoverability).
"""

from __future__ import annotations

from .executor_protocol import Executor
from .models import AttemptStatus, ExecutionReport, RiskLevel
from .workspace import ScenarioWorkspace


class SimulatorExecutor(Executor):
    """Deterministic executor bound to one workspace's simulated world."""

    def __init__(self, workspace: ScenarioWorkspace) -> None:
        self.workspace = workspace
        self._reports_by_key: dict[str, ExecutionReport] = {}

    def cached_report(self, idempotency_key: str | None) -> ExecutionReport | None:
        """Read-only peek used by adapters so replays never re-consult the world."""
        if not idempotency_key:
            return None
        return self._reports_by_key.get(idempotency_key)

    def execute(
        self,
        order_id: str,
        outcome: AttemptStatus,
        observations: tuple[str, ...],
        *,
        idempotency_key: str | None = None,
    ) -> ExecutionReport:
        workspace = self.workspace
        key = idempotency_key
        if key is None:
            contract = workspace._latest_contract(order_id)
            key = contract.idempotency_key if contract is not None else ""

        if key and key in self._reports_by_key:
            cached = self._reports_by_key[key]
            workspace.append_event(
                "IdempotentReplay",
                order_id=order_id,
                idempotency_key=key,
                report_id=cached.id,
            )
            return cached

        # Boundary self-checks: the executor trusts no caller.
        order = workspace.orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown execution order: {order_id}")
        if workspace._refresh_expiry(order):
            raise PermissionError("Executor refused: the order expired.")
        if order.status.value != "active":
            raise PermissionError(
                f"Executor refused: order is {order.status.value}, not active."
            )
        contract = workspace._latest_contract(order_id)
        if contract is None:
            raise PermissionError(
                f"Executor refused: order {order_id} has no action contract."
            )
        if contract.risk_level >= RiskLevel.L3 and workspace._valid_approval(contract) is None:
            raise PermissionError(
                f"Executor refused: L3+ action '{contract.action_type}' is not approved."
            )

        report = workspace.record_execution(
            order_id,
            outcome,
            observations,
            action_contract_id=contract.id,
            idempotency_key=key,
        )
        if key:
            self._reports_by_key[key] = report
        return report
