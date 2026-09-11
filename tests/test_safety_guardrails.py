"""Tests for the deterministic production safety layer.

The safety layer is intentionally independent of agent logic: every check is
pure arithmetic over injected account/order state, so nothing here mocks an
LLM. Each guard is exercised on both sides of its threshold.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tradingagents.safety import (
    DEFAULT_SAFETY_CONFIG,
    SafetyGuard,
    SafetyStateError,
    SafetyVerdict,
    get_safety_guard,
    reset_safety_guard,
)


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


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


class SafetyStatePersistenceTests(unittest.TestCase):
    """P1-02: corrupt safety state must fail closed, never silently reset."""

    def _write_state(self, tmp, text):
        path = Path(tmp) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_state_file_initializes_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp)
            self.assertEqual(guard._state["consecutive_rejections"], 0)
            self.assertIsNone(guard._state["high_water_mark"])
            self.assertEqual(guard._state["llm_tokens"], {})

    def test_valid_full_state_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({
                "high_water_mark": 123456.0,
                "consecutive_rejections": 3,
                "llm_tokens": {"2026-09-01": 4321},
            }))
            guard = make_guard(tmp)
            self.assertEqual(guard._state["high_water_mark"], 123456.0)
            self.assertEqual(guard._state["consecutive_rejections"], 3)
            self.assertEqual(guard._state["llm_tokens"], {"2026-09-01": 4321})

    def test_legacy_state_missing_keys_get_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({
                "high_water_mark": 999.0,
                "future_unknown_key": "kept",
            }))
            guard = make_guard(tmp)
            self.assertEqual(guard._state["high_water_mark"], 999.0)
            self.assertEqual(guard._state["consecutive_rejections"], 0)
            self.assertEqual(guard._state["llm_tokens"], {})
            self.assertEqual(guard._state["future_unknown_key"], "kept")

    def test_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, '{"high_water_mark": ')
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_json_root_list_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, '[{"high_water_mark": 1}]')
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_non_numeric_high_water_mark_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({"high_water_mark": "abc"}))
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_nan_and_inf_high_water_mark_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("NaN", "Infinity", "-Infinity"):
                self._write_state(tmp, json.dumps(
                    {"high_water_mark": json.loads(bad.replace("Infinity", "1e999"))}
                    if bad.endswith("Infinity") and bad[0] != "-"
                    else {"high_water_mark": json.loads('1e999') * (-1 if bad[0] == "-" else 1)}
                ))
                with self.assertRaises(SafetyStateError):
                    make_guard(tmp)

    def test_negative_rejection_count_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({"consecutive_rejections": -1}))
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_bool_rejection_count_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({"consecutive_rejections": True}))
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_llm_tokens_list_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, json.dumps({"llm_tokens": []}))
            with self.assertRaises(SafetyStateError):
                make_guard(tmp)

    def test_negative_or_bool_token_count_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({"llm_tokens": {"2026-09-01": -5}},
                        {"llm_tokens": {"2026-09-01": True}},
                        {"llm_tokens": {"not-a-date": 5}}):
                self._write_state(tmp, json.dumps(bad))
                with self.assertRaises(SafetyStateError):
                    make_guard(tmp)

    def test_read_permission_error_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp, "{}")
            guard = make_guard(tmp)
            with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                with self.assertRaises(SafetyStateError):
                    guard._load_state()
            self.assertTrue(path.exists())

    def test_saved_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp)
            guard.record_llm_tokens(123, when="2026-09-09")
            guard.record_order_result(False)
            guard._state["high_water_mark"] = 4242.0
            guard._save_state()
            reloaded = make_guard(tmp)
            self.assertEqual(reloaded._state["high_water_mark"], 4242.0)
            self.assertEqual(reloaded._state["consecutive_rejections"], 1)
            self.assertEqual(reloaded._state["llm_tokens"], {"2026-09-09": 123})

    def test_save_failure_leaves_existing_state_untruncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp)
            original = {"high_water_mark": 555.0, "consecutive_rejections": 2,
                        "llm_tokens": {}}
            guard._state = dict(original)
            guard._save_state()
            before = (Path(tmp) / "state.json").read_text(encoding="utf-8")

            # A failure before the rename (e.g. fsync blowup) must never
            # have truncated state.json: the temp file absorbs it.
            with patch("os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    guard._save_state()
            self.assertEqual(
                (Path(tmp) / "state.json").read_text(encoding="utf-8"), before
            )
            leftovers = [p.name for p in Path(tmp).iterdir()
                         if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_corrupt_state_stops_execution_path_with_zero_mutations(self):
        # Startup fail-closed: a corrupt safety state surfaces as an
        # unusable guard; the execution path must then refuse every
        # mutation (no submit/close), whatever its exact refusal shape.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_state(tmp, '{"high_water_mark": ')
            with patch("tradingagents.safety.get_safety_guard",
                       side_effect=SafetyStateError("corrupt")):
                broker = MagicMock()
                quote_mod = __import__(
                    "tradingagents.execution.authority", fromlist=["BrokerQuote"]
                )
                from tradingagents.execution import ExecutionService

                service = ExecutionService(
                    db_path=str(Path(tmp) / "execution.db"),
                    broker_factory=lambda: broker,
                    quote_factory=lambda s: quote_mod.BrokerQuote(
                        s, 100.0, 100.1, datetime.now(timezone.utc)
                    ),
                )
                result = service.execute(
                    trade_intent=self._buy_intent_simple(),
                    dollar_amount=1000.0,
                    allow_shorts=False,
                )
            self.assertFalse(result["success"])
            self.assertFalse(result.get("broker_attempted"))
            broker.submit_order.assert_not_called()
            broker.close_position.assert_not_called()
            broker.cancel_order_by_id.assert_not_called()

    def _buy_intent_simple(self):
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
                risk_rationale="p1-02",
                required_controls="test",
                entry_policy=_ready_entry_policy(),
                stop_loss_price=95,
            ),
        ).model_dump(mode="json")


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
            quote_factory=lambda symbol: __import__(
                "tradingagents.execution.authority", fromlist=["BrokerQuote"]
            ).BrokerQuote(symbol, 100.0, 100.1, datetime.now(timezone.utc)),
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
                required_controls="test", entry_policy=_ready_entry_policy(), stop_loss_price=95,
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
                required_controls="None.", entry_policy=_ready_entry_policy(), stop_loss_price=105,
            ),
        ).model_dump(mode="json")

    def _mock_broker(self):
        broker = MagicMock()
        # R13: opening orders must prove the session open from the broker
        # clock before any exposure-adding POST; the fixture keeps it open.
        broker.get_clock.return_value = SimpleNamespace(is_open=True)
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
        broker.get_account.return_value = SimpleNamespace(
            id="paper-safety", equity="100000", last_equity="100000", cash="100000", buying_power="200000"
        )
        broker.get_all_positions.return_value = []
        broker.get_orders.return_value = []
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
            broker.get_all_positions.return_value = [
                SimpleNamespace(symbol="AAPL", qty="5", market_value="500")
            ]
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
        broker.submit_order.assert_called_once()
        broker.close_position.assert_not_called()

    def test_position_flip_closes_then_defers_open_for_fresh_analysis(self):
        # F05: the flip completes only its close phase in this call. The
        # oversized opposite open leg is deferred (never submitted), so the
        # per-trade cap is enforced on the NEXT round's fresh analysis
        # against fresh broker facts instead of pre-close sizing.
        with tempfile.TemporaryDirectory() as tmp:
            guard = make_guard(tmp, max_trade_notional_usd=100.0)
            broker = self._mock_broker()
            broker.get_all_positions.return_value = [
                SimpleNamespace(symbol="AAPL", qty="5", market_value="500")
            ]
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                result = self._service(tmp, lambda: broker).execute(
                    trade_intent=self._flip_intent(),
                    dollar_amount=1_000.0,
                    allow_shorts=True,
                    current_position="LONG",
                )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("reanalysis_required"))
        self.assertFalse(result.get("safety_blocked"))
        # Exactly one POST: the close leg. The open leg was deferred.
        broker.submit_order.assert_called_once()


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
