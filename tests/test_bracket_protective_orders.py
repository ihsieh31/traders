"""Protective stop-loss / take-profit orders must actually reach the broker.

These tests drive the single durable execution entry
(tradingagents.execution.ExecutionService) with a mocked broker: bracket/OTO
legs ride on the parent submit, and a protective-leg rejection falls back to
a plain market order without a second logical order.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tradingagents.agents.schemas import (
    ExecutableAction,
    RiskDecision,
    build_trade_intent_from_risk_decision,
    extract_protective_price,
)
from tradingagents.dataflows.alpaca_utils import AlpacaUtils


def _intent(symbol="AAPL", stop_loss=None, take_profit=None, action=ExecutableAction.BUY,
            trading_mode="investment", current_position="NEUTRAL", allow_shorts=False):
    return build_trade_intent_from_risk_decision(
        symbol=symbol,
        trading_mode=trading_mode,
        current_position=current_position,
        allow_shorts=allow_shorts,
        trade_date="2026-01-02",
        decision=RiskDecision(
            action=action,
            confidence="medium",
            risk_rationale="test",
            required_controls="test controls",
            stop_loss=stop_loss,
            take_profit=take_profit,
        ),
    )


class ExtractProtectivePriceTests(unittest.TestCase):
    def test_parses_plain_number(self):
        self.assertEqual(extract_protective_price("182.50"), 182.50)

    def test_parses_dollar_prefixed_number(self):
        self.assertEqual(extract_protective_price("Stop at $1,234.56 (ATR-based)"), 1234.56)

    def test_takes_first_price_when_multiple(self):
        self.assertEqual(extract_protective_price("195 then 202"), 195.0)

    def test_returns_none_for_no_number(self):
        self.assertIsNone(extract_protective_price("trail below support"))
        self.assertIsNone(extract_protective_price(None))

    def test_ignores_percentages(self):
        # "8% below entry" is relative guidance, not an absolute price level.
        self.assertIsNone(extract_protective_price("8% below entry"))


class TradeIntentNumericControlsTests(unittest.TestCase):
    def test_builder_populates_numeric_protective_prices(self):
        intent = _intent(stop_loss="182.50", take_profit="$195")
        self.assertEqual(intent.risk_controls.stop_loss_price, 182.50)
        self.assertEqual(intent.risk_controls.take_profit_price, 195.0)

    def test_builder_leaves_prices_none_for_qualitative_guidance(self):
        intent = _intent(stop_loss="below support", take_profit=None)
        self.assertIsNone(intent.risk_controls.stop_loss_price)
        self.assertIsNone(intent.risk_controls.take_profit_price)


class BracketExecutionTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        order = MagicMock()
        order.id = "order-1"
        order.symbol = "AAPL"
        order.side = "buy"
        order.qty = 5
        order.notional = None
        order.status = "accepted"
        order.order_class = "bracket"
        self.client.submit_order.return_value = order

        disabled_guard = MagicMock()
        disabled_guard.enabled = False

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.patches = [
            patch.object(
                AlpacaUtils,
                "get_latest_quote",
                return_value={"bid_price": 190.0, "ask_price": 190.1},
            ),
            # These tests isolate broker protective-order behavior. The safety
            # gate has its own integration tests and must not persist state in
            # the developer's real ~/.tradingagents directory during pytest.
            patch("tradingagents.safety.get_safety_guard", return_value=disabled_guard),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _execute(self, intent, symbol="AAPL", current_position="NEUTRAL", allow_shorts=False):
        from tradingagents.execution import ExecutionService

        svc = ExecutionService(
            db_path=str(Path(self._tmp.name) / "execution.db"),
            broker_factory=lambda: self.client,
        )
        return svc.execute(
            trade_intent=intent.model_dump(mode="json"),
            dollar_amount=1000,
            allow_shorts=allow_shorts,
            current_position=current_position,
        )

    def test_buy_with_stop_and_target_submits_bracket_order(self):
        result = self._execute(_intent(stop_loss="182.50", take_profit="195"))

        self.assertTrue(result["success"])
        self.assertEqual(result["protective_order_status"], "submitted_bracket")
        request = self.client.submit_order.call_args[0][0]
        self.assertEqual(str(request.order_class.value).lower(), "bracket")
        self.assertEqual(float(request.stop_loss.stop_price), 182.50)
        self.assertEqual(float(request.take_profit.limit_price), 195.0)
        self.assertEqual(str(request.time_in_force.value).lower(), "gtc")
        self.assertIsNotNone(request.qty)

    def test_buy_with_stop_only_submits_oto_order(self):
        result = self._execute(_intent(stop_loss="182.50"))

        self.assertEqual(result["protective_order_status"], "submitted_oto")
        request = self.client.submit_order.call_args[0][0]
        self.assertEqual(str(request.order_class.value).lower(), "oto")
        self.assertEqual(float(request.stop_loss.stop_price), 182.50)
        self.assertIsNone(request.take_profit)

    def test_crypto_buy_stays_advisory(self):
        result = self._execute(
            _intent(symbol="BTC/USD", stop_loss="60000", take_profit="70000"),
            symbol="BTC/USD",
        )

        self.assertEqual(result["protective_order_status"], "advisory_only")
        request = self.client.submit_order.call_args[0][0]
        self.assertIsNone(getattr(request, "stop_loss", None))
        self.assertTrue(any("crypto" in w.lower() for w in result["intent_warnings"]))

    def test_inverted_long_prices_fall_back_to_advisory(self):
        # For a long entry the stop must sit below the target.
        result = self._execute(_intent(stop_loss="200", take_profit="180"))

        self.assertEqual(result["protective_order_status"], "advisory_only")
        request = self.client.submit_order.call_args[0][0]
        self.assertIsNone(getattr(request, "stop_loss", None))

    def test_no_numeric_prices_stays_advisory(self):
        result = self._execute(_intent(stop_loss="below support"))

        self.assertEqual(result["protective_order_status"], "advisory_only")

    def test_config_flag_disables_bracket_submission(self):
        with patch(
            "tradingagents.dataflows.config.get_config",
            return_value={"protective_bracket_orders_enabled": False},
        ):
            result = self._execute(_intent(stop_loss="182.50", take_profit="195"))

        self.assertEqual(result["protective_order_status"], "advisory_only")
        request = self.client.submit_order.call_args[0][0]
        self.assertIsNone(getattr(request, "stop_loss", None))

    def test_bracket_rejection_falls_back_to_plain_market_order(self):
        plain_order = MagicMock()
        plain_order.id = "order-2"
        plain_order.symbol = "AAPL"
        plain_order.side = "buy"
        plain_order.qty = 5
        plain_order.notional = None
        plain_order.status = "accepted"
        self.client.submit_order.side_effect = [
            Exception("bracket orders not allowed"),
            plain_order,
        ]

        result = self._execute(_intent(stop_loss="182.50", take_profit="195"))

        self.assertTrue(result["success"])
        self.assertEqual(
            result["protective_order_status"], "bracket_rejected_fallback_plain"
        )
        self.assertEqual(self.client.submit_order.call_count, 2)

    def test_short_entry_bracket_prices_validated_inverted(self):
        # For a short entry the target sits below the stop.
        result = self._execute(
            _intent(
                stop_loss="210",
                take_profit="180",
                action=ExecutableAction.SHORT,
                trading_mode="trading",
                allow_shorts=True,
            ),
            allow_shorts=True,
        )

        self.assertEqual(result["protective_order_status"], "submitted_bracket")
        request = self.client.submit_order.call_args[0][0]
        self.assertEqual(float(request.stop_loss.stop_price), 210.0)
        self.assertEqual(float(request.take_profit.limit_price), 180.0)


if __name__ == "__main__":
    unittest.main()
