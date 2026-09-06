"""Phase D D1: the shared auto-trade helper preserves WebUI semantics.

The helper is the single home for non-UI sizing + execution preparation:
typed TradeIntent required, regime sizing then portfolio sizing for opening
long exposure only, then exactly one ExecutionService.execute call.
"""

import unittest
from unittest.mock import patch

from tradingagents.execution.auto_trade import execute_auto_trade


def _buy_intent():
    return {
        "symbol": "AAPL",
        "action": "BUY",
        "target_position": "LONG",
        "planned_actions": [
            {"action": "open_long", "order_type": "market",
             "side": "buy", "sizing_basis": "configured_notional"},
        ],
    }


def _hold_intent():
    return {
        "symbol": "AAPL",
        "action": "HOLD",
        "target_position": "NEUTRAL",
        "planned_actions": [],
    }


class FakeService:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "broker_attempted": True, "broker_calls": 1}


class AutoTradeSharedTest(unittest.TestCase):
    def test_missing_intent_is_fail_closed_with_zero_broker_calls(self):
        service = FakeService()
        result = execute_auto_trade(
            ticker="AAPL", trade_intent=None, base_trade_notional_usd=1000.0,
            allow_shorts=False, config={}, execution_service=service,
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["fail_closed"])
        self.assertFalse(result["broker_attempted"])
        self.assertEqual(result["broker_calls"], 0)
        self.assertEqual(service.calls, [])

    def test_hold_skips_sizing_and_calls_service_once(self):
        service = FakeService()
        with patch("tradingagents.regime.regime_risk_multiplier") as regime, \
             patch("tradingagents.portfolio.adjust_new_position_notional") as portfolio:
            result = execute_auto_trade(
                ticker="AAPL", trade_intent=_hold_intent(),
                base_trade_notional_usd=1000.0, allow_shorts=False,
                config={}, execution_service=service,
            )
        self.assertTrue(result["success"])
        regime.assert_not_called()
        portfolio.assert_not_called()
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0]["dollar_amount"], 1000.0)
        self.assertFalse(service.calls[0]["allow_shorts"])

    def test_buy_applies_regime_then_portfolio_in_order(self):
        service = FakeService()
        seen = []

        def fake_regime(symbol, config=None):
            seen.append(("regime", symbol))
            return 0.5

        def fake_portfolio(symbol, action, amount, gather_state=None, config=None):
            seen.append(("portfolio", symbol, action, amount))
            return amount * 0.8

        with patch("tradingagents.regime.regime_risk_multiplier",
                   side_effect=fake_regime), \
             patch("tradingagents.portfolio.adjust_new_position_notional",
                   side_effect=fake_portfolio):
            execute_auto_trade(
                ticker="AAPL", trade_intent=_buy_intent(),
                base_trade_notional_usd=1000.0, allow_shorts=False,
                config={}, execution_service=service,
            )
        self.assertEqual([step[0] for step in seen], ["regime", "portfolio"])
        # Regime saw the full amount; portfolio saw the regime-scaled amount.
        self.assertEqual(seen[1][3], 500.0)
        self.assertEqual(service.calls[0]["dollar_amount"], 400.0)

    def test_sizing_failure_keeps_requested_amount(self):
        service = FakeService()
        with patch("tradingagents.regime.regime_risk_multiplier",
                   side_effect=RuntimeError("broker down")), \
             patch("tradingagents.portfolio.adjust_new_position_notional",
                   side_effect=RuntimeError("broker down")):
            execute_auto_trade(
                ticker="AAPL", trade_intent=_buy_intent(),
                base_trade_notional_usd=1000.0, allow_shorts=False,
                config={}, execution_service=service,
            )
        self.assertEqual(service.calls[0]["dollar_amount"], 1000.0)

    def test_decision_identity_passthrough(self):
        service = FakeService()
        execute_auto_trade(
            ticker="AAPL", trade_intent=_hold_intent(),
            base_trade_notional_usd=1000.0, allow_shorts=False,
            config={}, execution_service=service,
            decision_id="did-1", run_id="run-1",
        )
        self.assertEqual(service.calls[0]["decision_id"], "did-1")
        self.assertEqual(service.calls[0]["run_id"], "run-1")


if __name__ == "__main__":
    unittest.main()
