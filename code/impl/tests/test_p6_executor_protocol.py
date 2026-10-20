"""P6-T5 acceptance tests: executor protocol finalization (doc 11 §7 P6-T5)."""

from __future__ import annotations

import unittest

from panda_brain.executor_protocol import Executor
from panda_brain.models import AttemptStatus
from panda_brain.simulator import ForestFireScenario
from panda_brain.simulator_executor import SimulatorExecutor


class RecordingExecutor:
    """A second, structurally-conforming executor used to prove the slot."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        order_id: str,
        outcome: AttemptStatus,
        observations: tuple[str, ...],
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(order_id)
        return f"recorded:{order_id}"


class P6ExecutorProtocolTests(unittest.TestCase):
    def test_simulator_executor_conforms_to_protocol(self) -> None:
        scenario = ForestFireScenario()
        self.assertIsInstance(scenario.executor, Executor)
        self.assertIsInstance(SimulatorExecutor(scenario.workspace), Executor)

    def test_second_executor_conforms_structurally(self) -> None:
        self.assertIsInstance(RecordingExecutor(), Executor)

    def test_protocol_typed_reference_drives_real_execution_and_replay(self) -> None:
        from panda_brain.models import DecisionType

        scenario = ForestFireScenario()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        scenario.apply_e3_conditions_verified()
        decision = scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "Protocol conformance flow.",
            "protocol",
        )
        order = scenario.issue_order(decision.id)

        executor: Executor = scenario.executor  # typed through the protocol only
        report = executor.execute(
            order.id,
            AttemptStatus.SUCCEEDED,
            ("approach prepared",),
            idempotency_key="proto-key-1",
        )
        replay = executor.execute(
            order.id,
            AttemptStatus.SUCCEEDED,
            ("approach prepared",),
            idempotency_key="proto-key-1",
        )
        self.assertIs(report, replay)
        replays = [event for event in scenario.workspace.events if event.kind == "IdempotentReplay"]
        self.assertEqual(1, len(replays))

    def test_protocol_contract_is_documented(self) -> None:
        from panda_brain import executor_protocol

        doc = executor_protocol.__doc__ or ""
        for required in ("Trust no caller", "Idempotency", "Report honestly"):
            self.assertIn(required, doc)


if __name__ == "__main__":
    unittest.main()
