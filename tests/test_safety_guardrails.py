"""Tests for the deterministic production safety layer.

The safety layer is intentionally independent of agent logic: every check is
pure arithmetic over injected account/order state, so nothing here mocks an
LLM. Each guard is exercised on both sides of its threshold.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tradingagents.safety import (
    DEFAULT_SAFETY_CONFIG,
    SafetyGuard,
    SafetyVerdict,
    get_safety_guard,
    reset_safety_guard,
)


def make_guard(tmp, **overrides):
    config = dict(DEFAULT_SAFETY_CONFIG)
    config.update(overrides)
    return SafetyGuard(
        config=config,
        state_path=Path(tmp) / "state.json",
        kill_switch_path=Path(tmp) / "KILL_SWITCH",
    )


ACCOUNT_OK = {"equity": 100_000.0, "last_equity": 100_000.0}


class KillSwitchTests(unittest.TestCase):
    def test_engage_blocks_all_orders_and_release_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp)
            self.assertTrue(guard.check_order("AAPL", 100.0, account=ACCOUNT_OK).allowed)

            guard.engage_kill_switch("manual test halt")
            verdict = guard.check_order("AAPL", 100.0, account=ACCOUNT_OK)
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("kill switch" in r.lower() for r in verdict.reasons))
            self.assertIn("manual test halt", guard.kill_switch_reason())

            guard.release_kill_switch()
            self.assertFalse(guard.kill_switch_active())
            self.assertTrue(guard.check_order("AAPL", 100.0, account=ACCOUNT_OK).allowed)

    def test_kill_switch_file_created_externally_is_honored(self):
        # Ops can halt trading by touching the file - no Python required.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "KILL_SWITCH").write_text("halted by ops", encoding="utf-8")
            guard = make_guard(tmp)
            self.assertTrue(guard.kill_switch_active())
            self.assertFalse(guard.check_order("AAPL", 100.0).allowed)


class PreTradeCheckTests(unittest.TestCase):
    def test_notional_above_cap_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_trade_notional_usd=5_000.0)
            self.assertTrue(guard.check_order("AAPL", 5_000.0, account=ACCOUNT_OK).allowed)
            verdict = guard.check_order("AAPL", 5_000.01, account=ACCOUNT_OK)
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("notional" in r.lower() for r in verdict.reasons))

    def test_concentration_limit_counts_existing_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_symbol_concentration_pct=25.0)
            # 20k existing + 10k new = 30% of 100k equity -> blocked
            verdict = guard.check_order(
                "AAPL", 10_000.0, account=ACCOUNT_OK, position_value=20_000.0
            )
            self.assertFalse(verdict.allowed)
            # 20k existing + 4k new = 24% -> allowed
            self.assertTrue(
                guard.check_order(
                    "AAPL", 4_000.0, account=ACCOUNT_OK, position_value=20_000.0
                ).allowed
            )

    def test_concentration_skipped_without_account_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_symbol_concentration_pct=25.0)
            verdict = guard.check_order("AAPL", 1_000.0, position_value=90_000.0)
            self.assertTrue(verdict.allowed)
            self.assertEqual(verdict.checks["concentration"]["status"], "skipped")


class CircuitBreakerTests(unittest.TestCase):
    def test_daily_loss_breaker_trips_beyond_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, daily_loss_halt_pct=10.0)
            ok = {"equity": 95_000.0, "last_equity": 100_000.0}  # -5%
            self.assertTrue(guard.check_order("AAPL", 100.0, account=ok).allowed)
            tripped = {"equity": 89_000.0, "last_equity": 100_000.0}  # -11%
            verdict = guard.check_order("AAPL", 100.0, account=tripped)
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("daily loss" in r.lower() for r in verdict.reasons))

    def test_drawdown_breaker_uses_persisted_high_water_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_drawdown_halt_pct=15.0)
            # Establish a 120k high-water mark.
            guard.check_order("AAPL", 100.0, account={"equity": 120_000.0, "last_equity": 120_000.0})

            # A fresh instance must read the same HWM from disk.
            guard2 = make_guard(tmp, max_drawdown_halt_pct=15.0)
            verdict = guard2.check_order(
                "AAPL", 100.0, account={"equity": 100_000.0, "last_equity": 100_000.0}
            )  # -16.7% from HWM
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("drawdown" in r.lower() for r in verdict.reasons))

    def test_consecutive_rejections_trip_and_success_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_consecutive_rejections=3)
            for _ in range(3):
                guard.record_order_result(False)
            verdict = guard.check_order("AAPL", 100.0, account=ACCOUNT_OK)
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("rejected" in r.lower() for r in verdict.reasons))

            guard.record_order_result(True)
            self.assertTrue(guard.check_order("AAPL", 100.0, account=ACCOUNT_OK).allowed)

    def test_risk_reducing_exit_bypasses_breakers_but_not_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_consecutive_rejections=1)
            guard.record_order_result(False)

            exit_verdict = guard.check_order(
                "AAPL",
                0.0,
                account={"equity": 80_000.0, "last_equity": 100_000.0},
                risk_reducing=True,
            )
            self.assertTrue(exit_verdict.allowed)
            self.assertEqual(
                exit_verdict.checks["daily_loss"]["status"], "skipped"
            )

            guard.engage_kill_switch("operator halt")
            self.assertFalse(
                guard.check_order(
                    "AAPL", 0.0, risk_reducing=True
                ).allowed
            )


class LLMBudgetTests(unittest.TestCase):
    def test_budget_exhaustion_blocks_new_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, daily_llm_token_budget=1_000_000)
            guard.record_llm_tokens(400_000, when="2026-07-11")
            self.assertTrue(guard.check_llm_budget(when="2026-07-11").allowed)
            guard.record_llm_tokens(700_000, when="2026-07-11")
            verdict = guard.check_llm_budget(when="2026-07-11")
            self.assertFalse(verdict.allowed)
            self.assertTrue(any("budget" in r.lower() for r in verdict.reasons))

    def test_budget_resets_each_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, daily_llm_token_budget=1_000_000)
            guard.record_llm_tokens(2_000_000, when="2026-07-10")
            self.assertTrue(guard.check_llm_budget(when="2026-07-11").allowed)

    def test_zero_budget_means_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, daily_llm_token_budget=0)
            guard.record_llm_tokens(10_000_000, when="2026-07-11")
            self.assertTrue(guard.check_llm_budget(when="2026-07-11").allowed)

    def test_token_usage_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_guard(tmp).record_llm_tokens(123, when="2026-07-11")
            self.assertEqual(
                make_guard(tmp).llm_tokens_used(when="2026-07-11"), 123
            )


class StatusAndTogglesTests(unittest.TestCase):
    def test_disabled_safety_allows_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, safety_enabled=False, max_trade_notional_usd=1.0)
            guard.engage_kill_switch("halt")
            verdict = guard.check_order("AAPL", 1_000_000.0, account=ACCOUNT_OK)
            self.assertTrue(verdict.allowed)

    def test_status_reports_every_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp)
            status = guard.status(account={"equity": 89_000.0, "last_equity": 100_000.0})
            for name in (
                "kill_switch",
                "trade_notional",
                "concentration",
                "daily_loss",
                "drawdown",
                "rejection_streak",
                "llm_budget",
            ):
                self.assertIn(name, status["guards"], name)
            self.assertFalse(status["guards"]["daily_loss"]["ok"])  # -11% today
            self.assertTrue(status["guards"]["kill_switch"]["ok"])

    def test_singleton_helper_returns_guard_and_resets(self):
        reset_safety_guard()
        guard = get_safety_guard()
        self.assertIsInstance(guard, SafetyGuard)
        self.assertIs(guard, get_safety_guard())
        reset_safety_guard()


class ExecutionIntegrationTests(unittest.TestCase):
    """ExecutionService must consult the safety layer before any broker POST."""

    @classmethod
    def setUpClass(cls):
        # Importing dataflows first trips a known circular import on main;
        # importing agents first is the production-safe order.
        import tradingagents.agents  # noqa: F401

    def _service(self, tmp, broker_factory):
        from tradingagents.execution import ExecutionService

        return ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=broker_factory,
        )

    def _buy_intent(self):
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

    def _sell_intent(self):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            RiskDecision,
            build_trade_intent_from_risk_decision,
        )

        return build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="LONG",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.SELL,
                confidence="medium",
                risk_rationale="exit",
                required_controls="None.",
            ),
        ).model_dump(mode="json")

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

    def _mock_broker(self):
        broker = MagicMock()
        order = MagicMock()
        order.id = "broker-1"
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
        return broker

    def _blocked_guard(self):
        guard = MagicMock(spec=SafetyGuard)
        guard.enabled = True
        guard.check_order.return_value = SafetyVerdict(
            allowed=False, reasons=["max trade notional exceeded"]
        )
        return guard

    def test_blocked_verdict_prevents_broker_calls(self):
        guard = self._blocked_guard()
        broker_factory = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        ):
            result = self._service(tmp, broker_factory).execute(
                trade_intent=self._buy_intent(),
                dollar_amount=1_000_000.0,
                allow_shorts=False,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result.get("safety_blocked"))
        self.assertIn("max trade notional exceeded", result["error"])
        broker_factory.assert_not_called()

    def test_order_results_feed_rejection_tracker(self):
        broker = self._mock_broker()
        guard = MagicMock(spec=SafetyGuard)
        guard.enabled = True
        guard.check_order.return_value = SafetyVerdict(allowed=True)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        ):
            self._service(tmp, lambda: broker).execute(
                trade_intent=self._buy_intent(),
                dollar_amount=1_000.0,
                allow_shorts=False,
            )

        guard.record_order_result.assert_called_with(True)

    def test_loss_breaker_does_not_trap_an_existing_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_consecutive_rejections=1)
            guard.record_order_result(False)
            broker = self._mock_broker()
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                result = self._service(tmp, lambda: broker).execute(
                    trade_intent=self._sell_intent(),
                    dollar_amount=10_000.0,
                    allow_shorts=False,
                    current_position="LONG",
                )

        self.assertTrue(result["success"])
        broker.close_position.assert_called_once_with("AAPL")

    def test_position_flip_closes_first_then_blocks_new_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_trade_notional_usd=100.0)
            broker = self._mock_broker()
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                result = self._service(tmp, lambda: broker).execute(
                    trade_intent=self._flip_intent(),
                    dollar_amount=1_000.0,
                    allow_shorts=True,
                    current_position="LONG",
                )

        self.assertFalse(result["success"])
        self.assertTrue(result["safety_blocked"])
        broker.close_position.assert_called_once_with("AAPL")
        broker.submit_order.assert_not_called()


class RunLoggerBudgetFeedTests(unittest.TestCase):
    def test_llm_call_events_feed_token_counter(self):
        import os

        from tradingagents.run_logger import RunAuditLogger

        guard = MagicMock(spec=SafetyGuard)
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)  # RunAuditLogger writes to ./eval_results
            try:
                with patch("tradingagents.safety.get_safety_guard", return_value=guard):
                    logger = RunAuditLogger()
                    run_id = logger.start_run(symbol="AAPL", trade_date="2026-07-11")
                    logger.log_event(
                        "llm_call",
                        symbol="AAPL",
                        run_id=run_id,
                        payload={"usage": {"total_tokens": 555}},
                    )
            finally:
                os.chdir(cwd)

        guard.record_llm_tokens.assert_called_with(555)


if __name__ == "__main__":
    unittest.main()
