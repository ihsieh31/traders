import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from tradingagents.agents.schemas import (
    EntryPolicy,
    ExecutableAction,
    RiskDecision,
    build_trade_intent_from_risk_decision,
)
from tradingagents.dataflows.alpaca_utils import AlpacaUtils
from tradingagents.execution.authority import BrokerAuthorityError
from tradingagents.risk.position_sizing import (
    PositionSizer,
    RiskParameters,
    SizingDecision,
    compute_atr,
    kelly_position_fraction,
)


def _fresh_quote(symbol):
    from tradingagents.execution.authority import BrokerQuote

    return BrokerQuote(symbol.replace("/", ""), 100.0, 100.0, datetime.now(timezone.utc))


def _bars(highs, lows, closes):
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _constant_bars(rows=20, high=101.0, low=99.0, close=100.0):
    return _bars([high] * rows, [low] * rows, [close] * rows)


class ComputeAtrTests(unittest.TestCase):
    def test_matches_wilder_smoothing_reference_values(self):
        bars = _bars(
            highs=[101, 103, 102, 104, 103.5, 105],
            lows=[99, 101, 100, 102, 101.5, 103],
            closes=[100, 102, 101, 103, 102.5, 104],
        )
        # TRs from the 2nd row: 3, 2, 3, 2, 2.5
        # ATR(3) seed = mean(3, 2, 3) = 8/3
        # then (8/3*2 + 2)/3 = 22/9, then (22/9*2 + 2.5)/3 = 66.5/27
        atr = compute_atr(bars, period=3)
        self.assertAlmostEqual(atr, 66.5 / 27, places=6)

    def test_constant_range_bars_give_constant_atr(self):
        atr = compute_atr(_constant_bars(), period=14)
        self.assertAlmostEqual(atr, 2.0, places=9)

    def test_insufficient_rows_returns_none(self):
        self.assertIsNone(compute_atr(_constant_bars(rows=3), period=14))

    def test_empty_or_malformed_data_returns_none(self):
        self.assertIsNone(compute_atr(pd.DataFrame(), period=14))
        self.assertIsNone(compute_atr(None, period=14))
        missing_cols = pd.DataFrame({"close": [1.0] * 30})
        self.assertIsNone(compute_atr(missing_cols, period=14))

    def test_non_positive_result_returns_none(self):
        flat = _bars([100.0] * 20, [100.0] * 20, [100.0] * 20)
        self.assertIsNone(compute_atr(flat, period=14))


class KellyFractionTests(unittest.TestCase):
    def test_positive_edge_scaled_by_fraction(self):
        # full Kelly = 0.55 - 0.45/1.5 = 0.25 ; half Kelly = 0.125
        self.assertAlmostEqual(
            kelly_position_fraction(0.55, 1.5, kelly_fraction=0.5), 0.125, places=9
        )

    def test_negative_edge_clamps_to_zero(self):
        self.assertEqual(kelly_position_fraction(0.40, 1.0, kelly_fraction=0.5), 0.0)

    def test_invalid_inputs_clamp_to_zero(self):
        self.assertEqual(kelly_position_fraction(0.55, 0.0, kelly_fraction=0.5), 0.0)
        self.assertEqual(kelly_position_fraction(0.55, -2.0, kelly_fraction=0.5), 0.0)
        self.assertEqual(kelly_position_fraction(0.0, 1.5, kelly_fraction=0.5), 0.0)
        self.assertEqual(kelly_position_fraction(1.2, 1.5, kelly_fraction=0.5), 0.0)


class PositionSizerTests(unittest.TestCase):
    def setUp(self):
        self.sizer = PositionSizer(RiskParameters())

    def test_kelly_cap_binds_when_smallest(self):
        self.sizer = PositionSizer(RiskParameters(kelly_enabled=True, confidence_edge={"high": (0.55, 1.5)}))
        # equity=100k, price=100, atr=2, stop=4 -> risk notional 25k
        # kelly(high)=0.125 -> 12.5k ; max position 20% -> 20k ; requested 50k
        decision = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="high",
            requested_notional=50_000.0,
        )
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.notional, 12_500.0, places=2)
        self.assertIn("kelly", decision.caps_applied)
        self.assertAlmostEqual(decision.stop_loss_price, 96.0, places=6)
        self.assertAlmostEqual(decision.risk_amount, 500.0, places=2)

    def test_requested_notional_is_a_hard_ceiling(self):
        decision = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="high",
            requested_notional=1_000.0,
        )
        self.assertTrue(decision.approved)
        self.assertLessEqual(decision.notional, 1_000.0)
        self.assertIn("requested_notional", decision.caps_applied)

    def test_total_exposure_limit_blocks_new_position(self):
        decision = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="high",
            requested_notional=10_000.0,
            current_gross_exposure=79_995.0,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.notional, 0.0)
        self.assertIn("exposure", decision.reason.lower())

    def test_missing_atr_uses_conservative_default_stop(self):
        decision = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=None,
            confidence="high",
            requested_notional=50_000.0,
        )
        self.assertTrue(decision.approved)
        self.assertIn("atr_unavailable_default_stop", decision.caps_applied)
        # default stop distance = 5% of price
        self.assertAlmostEqual(decision.stop_loss_price, 95.0, places=6)

    def test_sell_side_places_stop_above_price(self):
        decision = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="high",
            requested_notional=10_000.0,
            side="sell",
        )
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.stop_loss_price, 104.0, places=6)

    def test_unknown_confidence_falls_back_to_most_conservative_edge(self):
        low = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="low",
            requested_notional=50_000.0,
        )
        unknown = self.sizer.size_position(
            equity=100_000.0,
            price=100.0,
            atr=2.0,
            confidence="galactic",
            requested_notional=50_000.0,
        )
        self.assertAlmostEqual(unknown.notional, low.notional, places=6)

    def test_invalid_inputs_are_rejected(self):
        for kwargs in (
            {"equity": 0.0, "price": 100.0},
            {"equity": -5.0, "price": 100.0},
            {"equity": 100_000.0, "price": 0.0},
            {"equity": 100_000.0, "price": -1.0},
        ):
            with self.subTest(kwargs=kwargs):
                decision = self.sizer.size_position(
                    atr=2.0,
                    confidence="high",
                    requested_notional=1_000.0,
                    **kwargs,
                )
                self.assertFalse(decision.approved)
                self.assertEqual(decision.notional, 0.0)

    def test_result_below_minimum_notional_is_rejected(self):
        decision = self.sizer.size_position(
            equity=40.0,
            price=100.0,
            atr=2.0,
            confidence="high",
            requested_notional=1_000.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("minimum", decision.reason.lower())


class AccountRiskSnapshotTests(unittest.TestCase):
    def test_snapshot_aggregates_equity_and_gross_exposure(self):
        class FakePosition:
            def __init__(self, market_value):
                self.market_value = market_value

        class FakeAccount:
            equity = "100000"

        class FakeClient:
            def get_clock(self):
                return SimpleNamespace(is_open=True)

            def get_account(self):
                return FakeAccount()

            def get_all_positions(self):
                return [FakePosition("2500.5"), FakePosition("-1500.25")]

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=FakeClient(),
        ):
            snapshot = AlpacaUtils.get_account_risk_snapshot()

        self.assertAlmostEqual(snapshot["equity"], 100_000.0, places=2)
        self.assertAlmostEqual(snapshot["gross_exposure"], 4_000.75, places=2)

    def test_snapshot_failure_raises(self):
        class BrokenClient:
            def get_clock(self):
                return SimpleNamespace(is_open=True)

            def get_account(self):
                raise RuntimeError("alpaca down")

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=BrokenClient(),
        ):
            with self.assertRaises(Exception):
                AlpacaUtils.get_account_risk_snapshot()


def _ready_policy():
    now = datetime.now(timezone.utc)
    return EntryPolicy(status="READY", minimum_price=100, maximum_price=100,
                       expires_at=(now + timedelta(hours=1)).isoformat(),
                       exit_by=(now + timedelta(days=5)).isoformat(), confirmation="observed setup")


def _buy_intent(confidence="high"):
    return build_trade_intent_from_risk_decision(
        symbol="AAPL",
        trading_mode="investment",
        current_position="NEUTRAL",
        allow_shorts=False,
        trade_date="2026-01-02",
        decision=RiskDecision(
            action=ExecutableAction.BUY,
            confidence=confidence,
            risk_rationale="Buy setup.",
            required_controls="Stop below support.",
            stop_loss_price=96.0, entry_policy=_ready_policy(),
        ),
    ).model_dump(mode="json")


class ExecuteTradeIntentRiskSizingTests(unittest.TestCase):
    """Deterministic sizing runs inside the single durable entry.

    These tests drive ExecutionService with a mocked broker and assert on
    the submitted broker request (notional / protective legs) plus the
    risk_sizing report. No real Alpaca calls.
    """

    def _service(self, tmp, broker):
        from tradingagents.execution import ExecutionService

        return ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=_fresh_quote,
        )

    def _broker(self, order_id="broker-1"):
        broker = MagicMock()
        order = MagicMock()
        order.id = order_id
        order.symbol = "AAPL"
        order.side = "buy"
        order.qty = 5
        order.notional = None
        order.status = "accepted"
        broker.submit_order.return_value = order
        close_order = MagicMock()
        close_order.id = "close-1"
        close_order.symbol = "AAPL"
        close_order.side = "sell"
        close_order.qty = 5
        close_order.status = "accepted"
        broker.close_position.return_value = close_order
        broker.get_clock.return_value = SimpleNamespace(is_open=True)
        broker.get_account.return_value = SimpleNamespace(
            id="paper-risk", equity="100000", last_equity="100000", cash="100000", buying_power="200000"
        )
        broker.get_all_positions.return_value = []
        broker.get_orders.return_value = []
        return broker

    def _disabled_guard(self):
        guard = MagicMock()
        guard.enabled = False
        return guard

    def test_risk_sizing_shrinks_dollar_amount_before_execution(self):
        import tempfile

        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils,
            "get_account_risk_snapshot",
            return_value={"equity": 100_000.0, "gross_exposure": 0.0},
        ), patch.object(
            AlpacaUtils, "get_stock_data_window", return_value=_constant_bars()
        ), patch.object(
            AlpacaUtils,
            "get_latest_quote",
            return_value={"bid_price": 100.0, "ask_price": 100.0},
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=50_000,
                allow_shorts=False,
                risk_params={"kelly_enabled": True, "confidence_edge": {"high": (0.55, 1.5)}},
            )

        self.assertTrue(result["success"])
        sizing = result["risk_sizing"]
        self.assertTrue(sizing["applied"])
        self.assertAlmostEqual(sizing["notional"], 12_500.0, places=2)
        # The sized stop rides the same parent submit as an OTO leg with
        # whole-share qty resolved from the quote (12500 / 100 = 125).
        request = broker.submit_order.call_args[0][0]
        self.assertEqual(float(request.stop_loss.stop_price), 96.0)
        self.assertEqual(float(request.qty), 125.0)
        self.assertEqual(result["protective_order_status"], "submitted_oto")

    def test_risk_sizing_rejection_blocks_order_submission(self):
        import tempfile

        broker = self._broker()
        broker.get_all_positions.return_value = [
            SimpleNamespace(symbol="MSFT", qty="100", market_value="79995")
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils,
            "get_account_risk_snapshot",
            return_value={"equity": 100_000.0, "gross_exposure": 79_995.0},
        ), patch.object(
            AlpacaUtils, "get_stock_data_window", return_value=_constant_bars()
        ), patch.object(
            AlpacaUtils,
            "get_latest_quote",
            return_value={"bid_price": 100.0, "ask_price": 100.0},
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=10_000,
                allow_shorts=False,
                risk_params={},
            )

        self.assertFalse(result["success"])
        self.assertIn("risk", result["error"].lower())
        broker.submit_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_intent_stop_is_preserved_if_sizer_returns_a_different_stop(self):
        import tempfile

        sizing = SizingDecision(
            approved=True,
            notional=1_000.0,
            stop_loss_price=95.0,
            risk_amount=50.0,
            caps_applied=["requested_notional"],
            reason="test",
        )
        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils, "compute_risk_sized_amount", return_value=sizing
        ), patch.object(
            AlpacaUtils,
            "get_latest_quote",
            return_value={"bid_price": 100.0, "ask_price": 100.0},
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=1_000.0,
                allow_shorts=False,
                risk_params={},
            )

        self.assertTrue(result["success"])
        request = broker.submit_order.call_args[0][0]
        self.assertEqual(float(request.stop_loss.stop_price), 96.0)

    def test_risk_engine_data_failure_fails_closed(self):
        import tempfile

        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils,
            "get_stock_data_window",
            side_effect=RuntimeError("market data down"),
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=50_000,
                allow_shorts=False,
                risk_params={},
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["fail_closed"])
        self.assertIn("Risk sizing unavailable", result["error"])
        broker.submit_order.assert_not_called()

    def test_risk_sizing_skipped_for_closing_actions(self):
        import tempfile

        intent = build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="LONG",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.SELL,
                confidence="high",
                risk_rationale="Exit.",
                required_controls="None.",
            ),
        ).model_dump(mode="json")

        broker = self._broker()
        broker.get_all_positions.return_value = [
            SimpleNamespace(symbol="AAPL", qty="5", market_value="500")
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils, "get_account_risk_snapshot"
        ) as snapshot, patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=intent,
                dollar_amount=10_000,
                allow_shorts=False,
                risk_params={},
                current_position="LONG",
            )

        self.assertTrue(result["success"])
        snapshot.assert_not_called()
        broker.submit_order.assert_called_once()
        broker.close_position.assert_not_called()

    def test_risk_sizing_also_checks_same_side_increases(self):
        import tempfile

        broker = self._broker()
        # N09: an opening payload must match the canonical plan derived from
        # action+trading_mode+current_position, so a same-side increase can
        # no longer be expressed by mutating current_position while keeping
        # the OPEN_LONG planned action. The sizing gate is asserted for new
        # risk opened while the account already carries gross exposure (a
        # foreign-symbol holding), which the sizing engine must account for.
        broker.get_all_positions.return_value = [
            SimpleNamespace(symbol="MSFT", qty="5", market_value="500")
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils, "compute_risk_sized_amount", return_value=SizingDecision(approved=True, notional=1000, stop_loss_price=96, risk_amount=40, caps_applied=[], reason="test")
        ) as snapshot, patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=10_000,
                allow_shorts=False,
                risk_params={},
            )

        self.assertTrue(result["success"])
        snapshot.assert_called_once()
        # Opening new risk against existing gross exposure must be sized.
        self.assertFalse(result.get("hold", False))
        self.assertEqual(broker.submit_order.call_count, 1)
        self.assertTrue(result["risk_sizing"]["applied"])

    def test_default_call_still_requires_a_protective_stop(self):
        import tempfile

        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            result = self._service(tmp, broker).execute(
                trade_intent=_buy_intent(),
                dollar_amount=1_000,
                allow_shorts=False,
            )

        self.assertTrue(result["success"])
        self.assertNotIn("risk_sizing", result)
        request = broker.submit_order.call_args[0][0]
        self.assertEqual(float(request.qty), 10)
        self.assertEqual(float(request.stop_loss.stop_price), 96)


class CalcQtyPriceFailureTests(unittest.TestCase):
    """Size honesty inside the single durable entry.

    Plain notional orders never need a price guess. Broker-side protective
    legs need whole-share qty: without a trustworthy quote the service
    submits a plain market order (advisory) instead of guessing shares.
    """

    def _service(self, tmp, broker):
        from tradingagents.execution import ExecutionService

        return ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=_fresh_quote,
        )

    def _broker(self):
        broker = MagicMock()
        order = MagicMock()
        order.id = "broker-1"
        order.symbol = "AAPL"
        order.side = "buy"
        order.qty = 5
        order.notional = None
        order.status = "accepted"
        broker.submit_order.return_value = order
        broker.get_clock.return_value = SimpleNamespace(is_open=True)
        broker.get_account.return_value = SimpleNamespace(
            id="paper-price", equity="100000", last_equity="100000", cash="100000", buying_power="200000"
        )
        broker.get_all_positions.return_value = []
        broker.get_orders.return_value = []
        return broker

    def _disabled_guard(self):
        guard = MagicMock()
        guard.enabled = False
        return guard

    def test_plain_notional_still_requires_fresh_execution_quote(self):
        import tempfile

        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils, "get_latest_quote", return_value={}
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            svc = self._service(tmp, broker)
            svc._quote_factory = lambda symbol: (_ for _ in ()).throw(
                BrokerAuthorityError("quote unavailable")
            )
            result = svc.execute(
                trade_intent=_buy_intent(),
                dollar_amount=1_000,
                allow_shorts=False,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["paused"])
        broker.submit_order.assert_not_called()

    def test_protective_legs_without_quote_fail_closed(self):
        import tempfile

        from tradingagents.agents.schemas import build_trade_intent_from_risk_decision as build

        intent = build(
            symbol="AAPL",
            trading_mode="investment",
            current_position="NEUTRAL",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.BUY,
                confidence="high",
                risk_rationale="Buy setup.",
                required_controls="Stop at 95, target 110.",
                stop_loss="95", entry_policy=_ready_policy(),
                take_profit="110",
            ),
        ).model_dump(mode="json")
        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            AlpacaUtils, "get_latest_quote", return_value={}
        ), patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._disabled_guard(),
        ):
            svc = self._service(tmp, broker)
            svc._quote_factory = lambda symbol: (_ for _ in ()).throw(
                BrokerAuthorityError("quote unavailable")
            )
            result = svc.execute(
                trade_intent=intent,
                dollar_amount=1_000,
                allow_shorts=False,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["paused"])
        broker.submit_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
