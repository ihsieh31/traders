"""Chaos tests: inject broker/data failures at the system's boundaries and
verify the safety layer degrades gracefully instead of guessing or dying.

Each test states its hypothesis in the name. No network, no live keys — all
failures are injected via mocks at the Alpaca boundary, and every SafetyGuard
gets its own temp state so experiments never leak into each other.
"""

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Importing dataflows first trips a known circular import on main (fixed in
# the backtesting-engine PR); importing agents first is the production-safe
# order until that lands.
import tradingagents.agents  # noqa: F401
from tradingagents.dataflows.alpaca_utils import AlpacaUtils
from tradingagents.safety.guardrails import SafetyGuard


def _buy_intent():
    from tradingagents.agents.schemas import (
        ExecutableAction,
        RiskDecision,
        build_trade_intent_from_risk_decision,
    )

    return build_trade_intent_from_risk_decision(
        symbol="AAPL",
        trading_mode="investment",
        current_position="NEUTRAL",
        allow_shorts=False,
        trade_date="2026-01-02",
        decision=RiskDecision(
            action=ExecutableAction.BUY,
            confidence="medium",
            risk_rationale="test",
            required_controls="test",
        ),
    ).model_dump(mode="json")


def _guard(tmp, **config):
    merged = {"safety_enabled": True}
    merged.update(config)
    return SafetyGuard(
        config=merged,
        state_path=Path(tmp) / "state.json",
        kill_switch_path=Path(tmp) / "KILL_SWITCH",
    )


class NanAndGarbageEquityTests(unittest.TestCase):
    """Anomalous account data must read as 'unavailable', never as a number."""

    def test_nan_equity_does_not_poison_high_water_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            guard.check_order(
                "AAPL", 1000.0, account={"equity": float("nan"), "last_equity": 100000.0}
            )
            hwm = guard._state.get("high_water_mark")
            self.assertTrue(hwm is None or math.isfinite(float(hwm)))

    def test_drawdown_breaker_survives_a_nan_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, max_drawdown_halt_pct=15.0)
            # Healthy day establishes the mark, then a NaN day, then a crash day.
            guard.check_order("AAPL", 0.0, account={"equity": 100000.0})
            guard.check_order("AAPL", 0.0, account={"equity": float("nan")})
            verdict = guard.check_order("AAPL", 0.0, account={"equity": 80000.0})
            self.assertFalse(verdict.allowed)
            self.assertEqual(verdict.checks["drawdown"]["status"], "fail")

    def test_nan_equity_skips_account_breakers_instead_of_passing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, daily_loss_halt_pct=10.0, max_drawdown_halt_pct=15.0)
            verdict = guard.check_order(
                "AAPL",
                1000.0,
                account={"equity": float("nan"), "last_equity": float("nan")},
            )
            self.assertEqual(verdict.checks["daily_loss"]["status"], "skipped")
            self.assertEqual(verdict.checks["drawdown"]["status"], "skipped")

    def test_nginx_html_in_account_fields_does_not_crash_the_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            verdict = guard.check_order(
                "AAPL",
                1000.0,
                account={
                    "equity": "<html><body>401 Authorization Required</body></html>",
                    "last_equity": "<html>",
                },
            )
            # Cheap checks still run; account-based ones are skipped.
            self.assertTrue(verdict.allowed)
            self.assertEqual(verdict.checks["daily_loss"]["status"], "skipped")
            self.assertEqual(verdict.checks["drawdown"]["status"], "skipped")

    def test_infinite_equity_reads_as_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            verdict = guard.check_order(
                "AAPL", 1000.0, account={"equity": float("inf"), "last_equity": 1.0}
            )
            self.assertEqual(verdict.checks["drawdown"]["status"], "skipped")
            hwm = guard._state.get("high_water_mark")
            self.assertTrue(hwm is None or math.isfinite(float(hwm)))

    def test_nan_position_value_does_not_disable_concentration_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, max_symbol_concentration_pct=25.0)
            verdict = guard.check_order(
                "AAPL",
                30000.0,
                account={"equity": 100000.0},
                position_value=float("nan"),
            )
            # NaN exposure must not silently pass the cap: the $30k order alone
            # exceeds 25% of $100k equity.
            self.assertFalse(verdict.allowed)
            self.assertEqual(verdict.checks["concentration"]["status"], "fail")

    def test_zero_last_equity_skips_daily_loss_without_dividing(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, daily_loss_halt_pct=10.0)
            verdict = guard.check_order(
                "AAPL", 1000.0, account={"equity": 50000.0, "last_equity": 0.0}
            )
            self.assertEqual(verdict.checks["daily_loss"]["status"], "skipped")


class MidFlipOutageTests(unittest.TestCase):
    """API dies between closing one side and opening the other.

    Driven through the single durable entry: the close leg commits first,
    the open leg follows only when the close succeeded, and broker POST
    outcomes feed the rejection breaker.
    """

    def _flip_intent(self):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            RiskDecision,
            build_trade_intent_from_risk_decision,
        )

        return build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="trading",
            current_position="LONG",
            allow_shorts=True,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.SHORT,
                confidence="medium",
                risk_rationale="flip",
                required_controls="None.",
            ),
        ).model_dump(mode="json")

    def _run_flip(self, tmp, guard, broker):
        from tradingagents.execution import ExecutionService

        svc = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
        )
        with patch("tradingagents.safety.get_safety_guard", return_value=guard):
            return svc.execute(
                trade_intent=self._flip_intent(),
                dollar_amount=5000.0,
                allow_shorts=True,
                current_position="LONG",
            )

    def test_open_leg_failure_reports_failure_and_feeds_the_breaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, max_consecutive_rejections=5)
            broker = Mock()
            close_order = Mock()
            close_order.id = "close-1"
            close_order.status = "accepted"
            broker.close_position.return_value = close_order
            broker.submit_order.return_value = {
                "success": False,
                "error": "connection reset",
            }
            outcome = self._run_flip(tmp, guard, broker)
            self.assertFalse(outcome["success"])
            roles = [r.get("status") for r in outcome["results"]]
            self.assertEqual(len(roles), 2)
            # One success (close) then one rejection (open): streak is 1.
            self.assertEqual(guard.consecutive_rejections(), 1)

    def test_close_leg_failure_never_attempts_the_open_leg(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            broker = Mock()
            broker.close_position.return_value = {
                "success": False,
                "error": "504 gateway timeout",
            }
            open_order = Mock()
            broker.submit_order = open_order
            outcome = self._run_flip(tmp, guard, broker)
            self.assertFalse(outcome["success"])
            open_order.assert_not_called()

    def test_unexpected_exception_mid_flip_goes_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            broker = Mock()
            close_order = Mock()
            close_order.id = "close-1"
            close_order.status = "accepted"
            broker.close_position.return_value = close_order
            broker.submit_order.side_effect = ConnectionError(
                "socket closed mid-request"
            )
            outcome = self._run_flip(tmp, guard, broker)
            self.assertFalse(outcome["success"])
            self.assertTrue(outcome.get("has_unknown"))
            # Ambiguous POST: exactly one attempt, never an immediate retry.
            self.assertEqual(broker.submit_order.call_count, 1)


class KillSwitchAndBreakerFlowTests(unittest.TestCase):
    def test_kill_switch_blocks_before_any_broker_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            guard.engage_kill_switch("chaos drill")
            broker_factory = Mock()
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                from tradingagents.execution import ExecutionService

                outcome = ExecutionService(
                    db_path=str(Path(tmp) / "execution.db"),
                    broker_factory=broker_factory,
                ).execute(
                    trade_intent=_buy_intent(),
                    dollar_amount=1000.0,
                    allow_shorts=False,
                )
            self.assertFalse(outcome["success"])
            self.assertTrue(outcome.get("safety_blocked"))
            broker_factory.assert_not_called()

    def test_rejection_streak_halts_subsequent_order_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, max_consecutive_rejections=3)
            for _ in range(3):
                guard.record_order_result(False)
            verdict = guard.check_order("AAPL", 1000.0)
            self.assertFalse(verdict.allowed)
            self.assertEqual(verdict.checks["rejection_streak"]["status"], "fail")

    def test_unreachable_account_degrades_to_cheap_checks_only(self):
        """Broker down: orders are not wrongly blocked by absent breakers."""
        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp, daily_loss_halt_pct=10.0, max_drawdown_halt_pct=15.0)
            broker = Mock()
            order = Mock()
            order.id = "broker-1"
            order.status = "accepted"
            broker.submit_order.return_value = order
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                from tradingagents.execution import ExecutionService

                outcome = ExecutionService(
                    db_path=str(Path(tmp) / "execution.db"),
                    broker_factory=lambda: broker,
                ).execute(
                    trade_intent=_buy_intent(),
                    dollar_amount=1000.0,
                    allow_shorts=False,
                )
            self.assertTrue(outcome["success"])


class MalformedBrokerPayloadTests(unittest.TestCase):
    def test_garbage_position_payload_reads_as_neutral(self):
        bad_position = Mock()
        bad_position.symbol = "AAPL"
        bad_position.qty = "<html>502 Bad Gateway</html>"
        client = Mock()
        client.get_all_positions.return_value = [bad_position]
        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=client,
        ):
            state = AlpacaUtils.get_current_position_state("AAPL")
        self.assertEqual(state, "NEUTRAL")

    def test_strict_raises_on_malformed_target_position_qty(self):
        # A corrupted qty on the *matched* position is as unsafe as an outage:
        # a strict execution caller must not read it as NEUTRAL (which would
        # re-buy the real holding or skip a real exit). Non-strict prompt
        # callers keep the forgiving NEUTRAL behavior.
        bad_position = Mock()
        bad_position.symbol = "AAPL"
        bad_position.qty = "<html>502 Bad Gateway</html>"
        client = Mock()
        client.get_all_positions.return_value = [bad_position]
        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=client,
        ):
            with self.assertRaises((ValueError, AttributeError)):
                AlpacaUtils.get_current_position_state("AAPL", strict=True)
            self.assertEqual(
                AlpacaUtils.get_current_position_state("AAPL"), "NEUTRAL"
            )

    def test_strict_raises_on_non_finite_target_position_qty(self):
        # "nan"/"inf" parse through float() without raising but are not a real
        # position size; both LONG/SHORT comparisons are false so they would
        # fall through to NEUTRAL. A strict execution caller must reject them;
        # non-strict prompt callers keep NEUTRAL.
        for bad_qty in ("nan", "inf", "-inf"):
            bad_position = Mock()
            bad_position.symbol = "AAPL"
            bad_position.qty = bad_qty
            client = Mock()
            client.get_all_positions.return_value = [bad_position]
            with patch(
                "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
                return_value=client,
            ):
                with self.assertRaises(ValueError):
                    AlpacaUtils.get_current_position_state("AAPL", strict=True)
                self.assertEqual(
                    AlpacaUtils.get_current_position_state("AAPL"), "NEUTRAL"
                )

    def test_account_fetch_outage_returns_zeroed_info_not_exception(self):
        client = Mock()
        client.get_account.side_effect = ConnectionError("nginx: 502")
        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=client,
        ):
            info = AlpacaUtils.get_account_info()
        self.assertEqual(info["buying_power"], 0)
        self.assertEqual(info["cash"], 0)


class PositionFetchOutageTests(unittest.TestCase):
    """A broker outage during the pre-trade position check must abort the
    trade, not read as NEUTRAL.

    get_current_position_state defaults to NEUTRAL on any error so agent
    prompts keep working, but the trade executor uses the same value as
    ground truth. If the account is actually LONG and the position fetch
    fails, a BUY signal then re-buys the existing holding (pyramiding every
    loop iteration the outage persists), and a SELL signal reads as "no
    position to sell" and silently skips a risk exit.
    """

    def test_strict_position_check_raises_instead_of_guessing_neutral(self):
        client = Mock()
        client.get_all_positions.side_effect = ConnectionError("nginx: 502")
        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=client,
        ):
            with self.assertRaises(ConnectionError):
                AlpacaUtils.get_current_position_state("AAPL", strict=True)
            # Non-strict callers (agent prompt context) keep the old behavior.
            self.assertEqual(AlpacaUtils.get_current_position_state("AAPL"), "NEUTRAL")

    def test_trade_execution_skips_order_when_position_fetch_fails(self):
        from webui.components.analysis import execute_trade_after_analysis
        from webui.utils.state import app_state

        # Account is long AAPL, but the positions endpoint is down.
        client = Mock()
        client.get_all_positions.side_effect = ConnectionError("nginx: 502")
        client.get_account.side_effect = ConnectionError("nginx: 502")

        app_state.init_symbol_state("AAPL")
        state = app_state.get_state("AAPL")
        state["analysis_complete"] = True
        state["recommended_action"] = "BUY"

        with tempfile.TemporaryDirectory() as tmp:
            guard = _guard(tmp)
            with patch(
                "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
                return_value=client,
            ), patch(
                "webui.components.analysis.ExecutionService",
            ) as service_cls, patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ), patch(
                "tradingagents.portfolio.adjust_new_position_notional",
                side_effect=lambda **kwargs: kwargs["requested_notional"],
            ), patch(
                "tradingagents.regime.regime_risk_multiplier", return_value=1.0
            ):
                execute_trade_after_analysis("AAPL", allow_shorts=False, trade_amount=1000)

            # Strict position check aborts before the single entry is reached.
            service_cls.assert_not_called()
            self.assertIn("error", state.get("trading_results") or {})


if __name__ == "__main__":
    unittest.main()
