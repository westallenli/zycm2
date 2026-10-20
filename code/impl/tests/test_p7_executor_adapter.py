"""P7-T5 acceptance tests: world-driven executor adapter."""

from __future__ import annotations

import unittest

from panda_brain.executor_protocol import Executor
from panda_brain.executor_adapter import WorldDecision, WorldDrivenExecutorAdapter
from panda_brain.models import AttemptStatus, DecisionType, RiskLevel
from panda_brain.simulator import ForestFireScenario


class P7ExecutorAdapterTests(unittest.TestCase):
    def _scenario_with_active_order(self) -> tuple[ForestFireScenario, str]:
        scenario = ForestFireScenario()
        scenario.apply_e1_route_blocked()
        scenario.apply_e2_wind_and_fire_increase()
        scenario.apply_e3_conditions_verified()
        decision = scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "World-driven adapter flow.",
            "adapter",
        )
        order = scenario.issue_order(decision.id)
        return scenario, order.id

    def test_adapter_conforms_to_protocol(self) -> None:
        scenario, order_id = self._scenario_with_active_order()
        adapter = WorldDrivenExecutorAdapter(
            lambda oid: WorldDecision(AttemptStatus.SUCCEEDED, ("ok",)),
            fallback_executor=scenario.executor,
        )
        self.assertIsInstance(adapter, Executor)

    def test_world_decides_outcome_and_recording_stays_structured(self) -> None:
        scenario, order_id = self._scenario_with_active_order()

        def world_act(order: str) -> WorldDecision:
            # The world looks at itself and returns its own verdict.
            contract = scenario.workspace._latest_contract(order)
            if contract is not None and contract.risk_level >= RiskLevel.L0:
                return WorldDecision(
                    AttemptStatus.PARTIAL,
                    ("approach prepared; water source still unconfirmed",),
                )
            return WorldDecision(AttemptStatus.FAILED, ("no contract",))

        adapter = WorldDrivenExecutorAdapter(world_act, fallback_executor=scenario.executor)
        report = adapter.execute(order_id)

        self.assertEqual(AttemptStatus.PARTIAL, report.status)
        self.assertEqual(("approach prepared; water source still unconfirmed",), report.observations)
        self.assertEqual("active", scenario.workspace.orders[order_id].status.value)
        self.assertEqual((), scenario.workspace.verify_audit_chain())
        # The caller-supplied outcome is ignored by design.
        overridden = adapter.execute(order_id, AttemptStatus.SUCCEEDED)
        del overridden

    def test_idempotent_replay_flows_through_fallback(self) -> None:
        scenario, order_id = self._scenario_with_active_order()
        calls = {"n": 0}

        def world_act(order: str) -> WorldDecision:
            calls["n"] += 1
            return WorldDecision(AttemptStatus.SUCCEEDED, ("done once",))

        adapter = WorldDrivenExecutorAdapter(world_act, fallback_executor=scenario.executor)
        first = adapter.execute(order_id, idempotency_key="world-key-1")
        replay = adapter.execute(order_id, idempotency_key="world-key-1")

        self.assertIs(first, replay)
        self.assertEqual(1, calls["n"])  # the world was consulted only once


if __name__ == "__main__":
    unittest.main()
