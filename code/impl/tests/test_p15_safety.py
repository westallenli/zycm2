"""P15 T3 — safety-doctrine policy gates: night assault ban, burnout/air weather,
reserve commitment; end-to-end activation block with clause citations."""

from __future__ import annotations

import unittest

from panda_brain.models import (
    ActionContract,
    AdvisorRole,
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    PersonRecord,
    RiskLevel,
)
from panda_brain.safety_doctrine import (
    CLAUSE_INDEX,
    clause_texts_for,
    rule_air_operation_weather_limits,
    rule_burnout_weather_limits,
    rule_no_night_direct_assault,
    rule_reserve_before_commitment,
)
from panda_brain.workspace import POLICY_RULES, ScenarioWorkspace


def _contract(**parameters: str) -> ActionContract:
    return ActionContract(
        id="contract-test",
        order_id="order-test",
        action_type="simulated:test",
        risk_level=RiskLevel.L1,
        parameters=tuple(parameters.items()),
    )


def _person(name: str, phone: str) -> PersonRecord:
    return PersonRecord(name=name, phone=phone, unit="演练大队")


class DoctrineRuleUnitTest(unittest.TestCase):
    def test_night_direct_assault_blocked_day_allowed_undeclared_fails(self):
        ws = ScenarioWorkspace("p15-safety")
        self.assertTrue(
            rule_no_night_direct_assault(ws, _contract(engagement="direct-attack", time_window="day"))
        )
        self.assertFalse(
            rule_no_night_direct_assault(ws, _contract(engagement="direct-attack", time_window="night"))
        )
        # 未申报时间窗 → 视为不合规（沉默不是合规）
        self.assertFalse(rule_no_night_direct_assault(ws, _contract(engagement="direct-attack")))
        # 非直抗作业不受本条约束
        self.assertTrue(rule_no_night_direct_assault(ws, _contract(engagement="mop-up")))

    def test_burnout_weather_limits(self):
        ws = ScenarioWorkspace("p15-safety")

        def check(**params):
            base = {"engagement": "burn-out"}
            base.update(params)
            return rule_burnout_weather_limits(ws, _contract(**base))

        # 三级风、风向稳定，可以点烧
        self.assertTrue(check(wind_force="3", wind_stability="stable", burn_orientation="upwind"))
        # 四级风点逆风火/侧风火 → 拦截
        self.assertFalse(check(wind_force="4", wind_stability="stable", burn_orientation="upwind"))
        self.assertFalse(check(wind_force="5", wind_stability="stable", burn_orientation="crosswind"))
        # 顺风点烧四级风仍可（条令只禁逆风/侧风）
        self.assertTrue(check(wind_force="4", wind_stability="stable", burn_orientation="downwind"))
        # 风向不定禁止点烧
        self.assertFalse(check(wind_force="2", wind_stability="shifting", burn_orientation="downwind"))
        # 缺申报 → 不合规
        self.assertFalse(check(wind_force="3", wind_stability="stable"))
        # 非点烧作业不受约束
        self.assertTrue(rule_burnout_weather_limits(ws, _contract(engagement="mop-up")))

    def test_air_operation_weather_limits(self):
        ws = ScenarioWorkspace("p15-safety")

        def check(**params):
            base = {"engagement": "air-operation"}
            base.update(params)
            return rule_air_operation_weather_limits(ws, _contract(**base))

        self.assertTrue(check(daylight="true", wind_force="5"))
        self.assertFalse(check(daylight="false", wind_force="3"))   # 无日照
        self.assertFalse(check(daylight="true", wind_force="6"))    # 六级大风
        self.assertFalse(check(daylight="true"))                    # 缺风力申报
        self.assertTrue(rule_air_operation_weather_limits(ws, _contract(engagement="mop-up")))

    def test_reserve_commitment_reads_live_roster(self):
        ws = ScenarioWorkspace("p15-safety")
        contract = _contract(engagement="direct-attack")
        # 无一人到岗：不得激活直抗
        self.assertFalse(rule_reserve_before_commitment(ws, contract))
        ws.import_person_library([_person(f"队员{index}", f"1360000000{index}") for index in range(1, 6)])
        # 一线 2 人到岗但无预备 → 拦截
        ws.scan_check_in("pjjz-1", phone="13600000001")
        ws.scan_check_in("pjjz-2", phone="13600000002")
        self.assertFalse(rule_reserve_before_commitment(ws, contract))
        # 机动支援 1 人到位（≥ceil(0.6)=1）→ 放行
        ws.scan_check_in("mobile-support", phone="13600000003")
        self.assertTrue(rule_reserve_before_commitment(ws, contract))
        # 非直抗不查预备队
        self.assertTrue(rule_reserve_before_commitment(ws, _contract(engagement="mop-up")))


class RegistryIntegrationTest(unittest.TestCase):
    def test_doctrine_rules_join_activation_gate(self):
        for rule_id in (
            "no-night-direct-assault",
            "burnout-weather-limits",
            "air-op-weather-limits",
            "reserve-before-commitment",
        ):
            self.assertIn(rule_id, POLICY_RULES)
            self.assertIn(rule_id, CLAUSE_INDEX)
        # 原有内核规则未被挤掉
        for kernel_rule in ("no-challenged-basis", "declared-boundary", "verified-basis-for-l3"):
            self.assertIn(kernel_rule, POLICY_RULES)

    def test_clause_citations_retrievable_for_failed_rules(self):
        texts = clause_texts_for(("no-night-direct-assault", "burnout-weather-limits"))
        self.assertTrue(any("凌晨12时至次日4时" in text for text in texts))
        self.assertTrue(any("风力4级以上" in text for text in texts))


class ActivationGateEndToEndTest(unittest.TestCase):
    """夜间直抗命令在激活闸门被拦截，且失败名单点名违反条款。"""

    def setUp(self):
        from panda_brain.chief_advisor import ChiefAdvisor
        from panda_brain.simulator import ForestFireScenario

        self.scenario = ForestFireScenario()
        self.chief = ChiefAdvisor(self.scenario.council)
        self.ws = self.scenario.workspace
        self._reach_e3()
        # 预先配置花名册：一线 2 人 + 机动支援 1 人，满足预备队 30% 参考线，
        # 使本组用例只考验时间窗条令本身。
        self.ws.import_person_library(
            [
                PersonRecord(name="攻坚甲", phone="13600000001", unit="演练大队"),
                PersonRecord(name="攻坚乙", phone="13600000002", unit="演练大队"),
                PersonRecord(name="机动丙", phone="13600000003", unit="演练大队"),
            ]
        )
        self.ws.scan_check_in("pjjz-1", phone="13600000001")
        self.ws.scan_check_in("pjjz-2", phone="13600000002")
        self.ws.scan_check_in("mobile-support", phone="13600000003")
        decision = self.scenario.commander_decision(
            DecisionType.ADOPT,
            "prepare-approach-r2",
            "夜间直抗条令拦截演练。",
            "simulated assault order",
        )
        order = self.ws.draft_order(
            decision.id, drafted_by="DispatchChiefAgent", drafter_role=AdvisorRole.CHIEF_ADVISOR
        )
        self.ws.draft_action_contract(
            order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{order.option_id}",
            risk_level=RiskLevel.L2,
            parameters=(
                ("option_id", order.option_id),
                ("engagement", "direct-attack"),
                ("time_window", "night"),
            ),
            idempotency_key=f"{order.id}:night-assault",
        )
        self.ws.submit_order_for_confirmation(order.id)
        self.ws.confirm_order(
            order.id,
            actor_role=DecisionActor.COMMANDER,
            confirmed_by=GENERAL_ENTRY_ID,
        )
        self.order = order

    def _reach_e3(self):
        self.scenario.apply_e1_route_blocked()
        self.scenario.apply_e2_wind_and_fire_increase()
        self.scenario.apply_e3_conditions_verified()

    def test_night_assault_order_cannot_activate(self):
        with self.assertRaises(ValueError) as ctx:
            self.ws.activate_order(self.order.id)
        message = str(ctx.exception)
        self.assertIn("no-night-direct-assault", message)
        citations = clause_texts_for(("no-night-direct-assault",))
        self.assertTrue(any("夜间" in text for text in citations))

    def test_same_order_passes_in_daytime_after_recontract(self):
        self.ws.draft_action_contract(
            self.order.id,
            drafted_by="DispatchChiefAgent",
            drafter_role=AdvisorRole.CHIEF_ADVISOR,
            action_type=f"simulated:{self.order.option_id}",
            risk_level=RiskLevel.L2,
            parameters=(
                ("option_id", self.order.option_id),
                ("engagement", "direct-attack"),
                ("time_window", "day"),
            ),
            idempotency_key=f"{self.order.id}:day-assault",
        )
        activated = self.ws.activate_order(self.order.id)
        self.assertEqual(activated.status.value, "active")


if __name__ == "__main__":
    unittest.main()
