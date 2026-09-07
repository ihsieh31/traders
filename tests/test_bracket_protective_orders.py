"""Protective stop-loss / take-profit orders must actually reach the broker.

These tests drive the single durable execution entry
(tradingagents.execution.ExecutionService) with a mocked broker: bracket/OTO
legs ride on the parent submit, and a protective-leg rejection falls back to
a plain market order without a second logical order.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tradingagents.agents.schemas import (
    ExecutableAction,
    RiskDecision,
    build_trade_intent_from_risk_decision,
    extract_protective_price,
)
from tradingagents.dataflows.alpaca_utils import AlpacaUtils


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 189, "maximum_price": 191,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


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
            entry_policy=_ready_entry_policy(),
            stop_loss=stop_loss,
            take_profit=take_profit,
        ),
    )


class ExtractProtectivePriceTests(unittest.TestCase):
    def test_parses_plain_number(self):
        self.assertEqual(extract_protective_price("182.50"), 182.50)

    def test_parses_dollar_prefixed_number(self):
        self.assertEqual(extract_protective_price("$1,234.56"), 1234.56)

    def test_rejects_ambiguous_multiple_prices(self):
        self.assertIsNone(extract_protective_price("195 then 202"))

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
        self.client.get_account.return_value = SimpleNamespace(
            id="paper-bracket", equity="100000", last_equity="100000", cash="100000", buying_power="200000"
        )
        self.client.get_all_positions.return_value = []
        self.client.get_orders.return_value = []

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
            quote_factory=lambda requested: __import__(
                "tradingagents.execution.authority", fromlist=["BrokerQuote"]
            ).BrokerQuote(
                requested.replace("/", ""), 190.0, 190.1, datetime.now(timezone.utc)
            ),
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

    def test_crypto_buy_without_supported_protection_is_blocked(self):
        result = self._execute(
            _intent(symbol="BTC/USD", stop_loss="60000", take_profit="70000"),
            symbol="BTC/USD",
        )

        self.assertFalse(result["success"])
        self.client.submit_order.assert_not_called()

    def test_inverted_long_prices_are_blocked(self):
        # For a long entry the stop must sit below the target.
        result = self._execute(_intent(stop_loss="200", take_profit="180"))

        self.assertFalse(result["success"])
        self.client.submit_order.assert_not_called()

    def test_no_numeric_stop_is_blocked(self):
        result = self._execute(_intent(stop_loss="below support"))

        self.assertFalse(result["success"])
        self.client.submit_order.assert_not_called()

    def test_config_flag_disables_bracket_submission(self):
        with patch(
            "tradingagents.dataflows.config.get_config",
            return_value={"protective_bracket_orders_enabled": False},
        ):
            result = self._execute(_intent(stop_loss="182.50", take_profit="195"))

        self.assertFalse(result["success"])
        self.client.submit_order.assert_not_called()

    def test_bracket_rejection_is_terminal_without_plain_retry(self):
        self.client.submit_order.side_effect = Exception("bracket orders not allowed")

        result = self._execute(_intent(stop_loss="182.50", take_profit="195"))

        self.assertFalse(result["success"])
        self.assertEqual(self.client.submit_order.call_count, 1)
        self.assertEqual(result["orders"][0]["status"], "REJECTED")

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
