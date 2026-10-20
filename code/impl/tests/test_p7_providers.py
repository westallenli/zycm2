"""P7-T2 acceptance tests: LLM provider factory (offline-first)."""

from __future__ import annotations

import unittest

from panda_brain.providers import (
    AdvisorLLMProvider,
    LocalNimProvider,
    MockProvider,
    NvidiaNimProvider,
    NullProvider,
    ProviderUnavailableError,
    create_provider,
)
from panda_brain.simulator import ForestFireScenario


class P7ProviderFactoryTests(unittest.TestCase):
    def test_default_factory_returns_null_provider(self) -> None:
        provider = create_provider()
        self.assertEqual("null", provider.name)
        with self.assertRaises(ProviderUnavailableError):
            provider.complete(system="s", prompt="p")

    def test_null_provider_leaves_whole_system_running(self) -> None:
        """The rule layer IS the system: no key configured, everything works."""
        provider = create_provider()  # default: null
        scenario = ForestFireScenario()
        scenario.apply_e1_route_blocked()
        from panda_brain.chief_advisor import ChiefAdvisor

        chief = ChiefAdvisor(scenario.council)
        goal = chief.open_goal_frame(scenario.workspace, "Control safely.")
        topic = chief.orchestrate(scenario.workspace, "Next?")
        brief = chief.compose_decision_brief(scenario.workspace, topic, goal)
        self.assertTrue(brief.recommendation)
        del provider

    def test_mock_provider_records_calls_and_routes_script(self) -> None:
        provider = create_provider(
            {
                "provider": "mock",
                "script": {"water_level": "[mock] polished water summary"},
                "default_reply": "[mock] generic polish",
            }
        )
        self.assertIsInstance(provider, MockProvider)
        reply = provider.complete(system="staff enhancement", prompt="summarize water_level reading")
        self.assertEqual("[mock] polished water summary", reply)
        self.assertEqual(1, len(provider.calls))

        other = provider.complete(system="s", prompt="nothing matching")
        self.assertEqual("[mock] generic polish", other)
        self.assertEqual(2, len(provider.calls))
        self.assertIsInstance(provider, AdvisorLLMProvider)

    def test_unknown_provider_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_provider({"provider": "sky-net"})

    def test_network_slots_inert_without_credentials(self) -> None:
        """P16-T22 后新契约：无门上下文时返回惰性实例，缺凭证调用即报错不触网。"""
        provider = create_provider({"provider": "nvidia"})
        self.assertIsInstance(provider, NvidiaNimProvider)
        with self.assertRaises(ProviderUnavailableError) as ctx:
            provider.complete(system="s", prompt="p")
        self.assertIn("needs credentials", str(ctx.exception))
        local = create_provider({"provider": "local_nim"})
        with self.assertRaises(ProviderUnavailableError) as ctx2:
            local.complete(system="s", prompt="p")
        self.assertIn("base_url", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
