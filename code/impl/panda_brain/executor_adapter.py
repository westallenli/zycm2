"""World-driven executor adapter (doc 11 §7 P7-T5).

A real executor derives its own outcome from its world; the simulation
protocol signature carries ``outcome`` because the simulated world is a fixed
script. This adapter proves the slot: wrap any ``world_act`` callable that
returns the world's own verdict, and let the existing executor perform every
boundary check, the recording, and idempotent replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .executor_protocol import Executor
from .models import AttemptStatus, ExecutionReport


@dataclass(frozen=True)
class WorldDecision:
    """The world's own verdict for one attempted action."""

    outcome: AttemptStatus
    observations: tuple[str, ...]


class WorldDrivenExecutorAdapter:
    """Adapts a world-decides-outcome callable to the Executor protocol."""

    name = "world-driven"

    def __init__(
        self,
        world_act: Callable[[str], WorldDecision],
        *,
        fallback_executor: Executor,
    ) -> None:
        self.world_act = world_act
        self._fallback = fallback_executor

    def execute(
        self,
        order_id: str,
        outcome: AttemptStatus | None = None,
        observations: tuple[str, ...] = (),
        *,
        idempotency_key: str | None = None,
    ) -> ExecutionReport:
        # Replay short-circuit: never re-consult the world for a known key.
        peek = getattr(self._fallback, "cached_report", None)
        if callable(peek) and peek(idempotency_key) is not None:
            cached = peek(idempotency_key)
            return self._fallback.execute(
                cached.order_id,
                cached.status,
                cached.observations,
                idempotency_key=idempotency_key,
            )

        del outcome, observations  # the world decides; caller-supplied values are ignored
        decision = self.world_act(order_id)
        return self._fallback.execute(
            order_id,
            decision.outcome,
            decision.observations,
            idempotency_key=idempotency_key,
        )
