import unittest
from unittest.mock import patch

from tradingagents.agents.schemas import (
    AdvisoryRating,
    ExecutableAction,
    PositionTransition,
    ResearchPlan,
    RiskDecision,
    TargetPosition,
    TraderProposal,
    build_trade_intent_from_risk_decision,
    render_research_plan,
    render_risk_decision,
    render_trader_proposal,
    trade_intent_action,
)
from tradingagents.agents.utils.agent_trading_modes import extract_recommendation
from tradingagents.agents.utils.structured import invoke_structured_or_freetext
from tradingagents.dataflows.alpaca_utils import AlpacaUtils


class Message:
    def __init__(self, content):
        self.content = content


class PlainLLM:
    def invoke(self, _prompt):
        return Message("plain fallback\nFINAL TRANSACTION PROPOSAL: **HOLD**")


class BrokenStructuredLLM:
    def invoke(self, _prompt):
        raise RuntimeError("structured unavailable")


class StructuredDecisionTests(unittest.TestCase):
    def test_renderers_preserve_exact_executable_action_line(self):
        research = render_research_plan(
            ResearchPlan(
                recommendation=ExecutableAction.BUY,
                confidence="medium",
                advisory_rating=AdvisoryRating.OVERWEIGHT,
                rationale="Evidence supports upside.",
                strategic_actions="Enter on confirmation.",
            )
        )
        trader = render_trader_proposal(
            TraderProposal(
                action=ExecutableAction.LONG,
                confidence="high",
                reasoning="Trend and macro align.",
            )
        )
        risk = render_risk_decision(
            RiskDecision(
                action=ExecutableAction.SELL,
                confidence="low",
                risk_rationale="Downside exceeds reward.",
                required_controls="Do not re-enter without reversal.",
            )
        )

        self.assertIn("**Advisory Rating**: Overweight", research)
        self.assertIn("FINAL TRANSACTION PROPOSAL: **BUY**", research)
        self.assertIn("FINAL TRANSACTION PROPOSAL: **LONG**", trader)
        self.assertIn("FINAL TRANSACTION PROPOSAL: **SELL**", risk)
        self.assertEqual(extract_recommendation(research, "investment"), "BUY")

    def test_trade_intent_derives_execution_contract_from_risk_decision(self):
        intent = build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="NEUTRAL",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.BUY,
                confidence="medium",
                advisory_rating=AdvisoryRating.OVERWEIGHT,
                risk_rationale="Upside exceeds defined risk.",
                required_controls="Stop below support.",
                stop_loss="182.50",
                take_profit="195 then 202",
            ),
        )

        self.assertEqual(intent.target_position, TargetPosition.LONG)
        self.assertEqual(intent.position_transition, PositionTransition.OPEN_LONG)
        self.assertEqual(intent.order_intent.side, "buy")
        self.assertEqual(intent.risk_controls.mode, "advisory_only")
        # Numeric protective levels are extracted so execution can submit
        # real bracket orders instead of leaving controls advisory.
        self.assertEqual(intent.risk_controls.stop_loss_price, 182.50)
        self.assertEqual(intent.risk_controls.take_profit_price, 195.0)
        self.assertTrue(intent.execution_constraints.broker_protective_orders_enabled)
        self.assertEqual(trade_intent_action(intent.model_dump(mode="json")), "BUY")

    def test_alpaca_execution_prefers_validated_trade_intent(self):
        intent = build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="NEUTRAL",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.BUY,
                confidence="medium",
                risk_rationale="Buy setup.",
                required_controls="Stop below support.",
            ),
        ).model_dump(mode="json")

        with patch(
            "tradingagents.execution.service.ExecutionService.execute",
            return_value={"success": True, "symbol": "AAPL", "actions": []},
        ) as execute:
            result = AlpacaUtils.execute_trade_intent(
                symbol="AAPL",
                current_position="NEUTRAL",
                trade_intent=intent,
                dollar_amount=1000,
                allow_shorts=False,
            )

        self.assertTrue(result["success"])
        _, kwargs = execute.call_args
        self.assertEqual(kwargs["trade_intent"]["action"], "BUY")
        self.assertEqual(kwargs["dollar_amount"], 1000)
        self.assertEqual(kwargs["allow_shorts"], False)

    def test_structured_failure_falls_back_to_plain_text(self):
        content = invoke_structured_or_freetext(
            BrokenStructuredLLM(),
            PlainLLM(),
            "prompt",
            lambda value: value,
            "Unit Agent",
        )

        self.assertIn("FINAL TRANSACTION PROPOSAL: **HOLD**", content)


if __name__ == "__main__":
    unittest.main()
