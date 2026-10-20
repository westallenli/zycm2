"""Executor protocol boundary (doc 11 §7 P6-T5).

The execution slot behind ``SimulatorExecutor`` is now a typed protocol so a
real-world executor can be slotted in without touching command-side code.

Contract every executor must honor (enforced by convention here, by structure
at the workspace boundary):
1. Trust no caller: re-check order existence, expiry, ACTIVE status, action
   contract, and the L3+ approval gate at your own boundary;
2. Idempotency: replaying an idempotency key returns the cached report without
   duplicating side effects, leaving an IdempotentReplay audit event;
3. Report honestly: return an ExecutionReport through
   ``ScenarioWorkspace.record_execution`` so verification never inherits
   unrecorded effects.

The ``outcome`` parameter is supplied by the bound world. In simulation the
world is a fixed script; a real executor derives outcomes from its own world —
slot it in via this same protocol in P7 or adapt it with a thin wrapper.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import AttemptStatus, ExecutionReport


@runtime_checkable
class Executor(Protocol):
    """Structural type for anything allowed to act on an active order."""

    def execute(
        self,
        order_id: str,
        outcome: AttemptStatus,
        observations: tuple[str, ...],
        *,
        idempotency_key: str | None = None,
    ) -> ExecutionReport: ...
