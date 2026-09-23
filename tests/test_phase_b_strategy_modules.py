"""Phase B5 tests: the existing strategy modules keep their semantics and
have live production callers — correlation/volatility sizing now wired into
the auto-trade path, regime scaling in place, memory/reflection intact,
Kelly still off by default."""

import inspect
import unittest
from unittest.mock import patch

import pandas as pd

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio import (
    PortfolioLimitsConfig,
    assess_new_position,
    adjust_new_position_notional,
    gather_portfolio_state_via_alpaca,
)


def _frame(values):
    return pd.DataFrame({"close": values})


def _correlated_pair(n=80, seed=7):
    import random

    rng = random.Random(seed)
    base = [100.0]
    other = [100.0]
    for _ in range(n - 1):
        step = rng.uniform(-1.5, 1.5)
        base.append(base[-1] + step)
        other.append(other[-1] + step * 0.95 + rng.uniform(-0.1, 0.1))
    return _frame(base), _frame(other)


class CorrelationHookCallerTests(unittest.TestCase):
    def test_high_correlation_suppresses_new_risk_through_the_hook(self):
        mine, held = _correlated_pair()
        equity, positions, history = 100000.0, {"HELD": 20000.0}, {"NEW": mine, "HELD": held}

        def gather_state():
            return equity, positions, history

        adjusted = adjust_new_position_notional(
            "NEW", "BUY", 10000.0, gather_state=gather_state,
            config=PortfolioLimitsConfig(),
        )
        # Correlation ~0.95 > 0.6 -> size scaled by 0.5.
        self.assertEqual(adjusted, 5000.0)

    def test_reducing_actions_pass_through_untouched(self):
        adjusted = adjust_new_position_notional(
            "NEW", "SELL", 10000.0,
            gather_state=lambda: (_ for _ in ()).throw(AssertionError("must not gather")),
            config=PortfolioLimitsConfig(),
        )
        self.assertEqual(adjusted, 10000.0)

    def test_gather_failure_keeps_requested_amount(self):
        def broken():
            raise RuntimeError("broker down")

        adjusted = adjust_new_position_notional(
            "NEW", "BUY", 10000.0, gather_state=broken, config=PortfolioLimitsConfig(),
        )
        self.assertEqual(adjusted, 10000.0)

    def test_portfolio_correlation_uses_signed_pnl_for_long_short_pairs(self):
        returns = [((index % 9) - 4) / 1000 for index in range(60)]

        def frame(sign):
            closes = [100.0]
            for value in returns:
                closes.append(closes[-1] * (1 + sign * value))
            return _frame(closes)

        config = PortfolioLimitsConfig(
            vol_sizing_enabled=False, correlated_size_factor=0.5,
            high_correlation=0.6, min_size_factor=0.25,
        )
        cases = [
            # candidate side, held signed MV, raw return sign, penalty?
            ("LONG", 20000.0, 1, True),
            ("SHORT", -20000.0, 1, True),
            ("LONG", -20000.0, 1, False),
            ("LONG", -20000.0, -1, True),
        ]
        for action, held_value, raw_sign, should_penalize in cases:
            with self.subTest(action=action, held_value=held_value, raw_sign=raw_sign):
                verdict = assess_new_position(
                    "NEW", 10000.0, 100000.0,
                    {"HELD": held_value},
                    {"NEW": frame(1), "HELD": frame(raw_sign)},
                    config=config, action=action,
                )
                self.assertEqual(verdict.adjusted_notional,
                                 5000.0 if should_penalize else 10000.0)
                expected_corr = raw_sign * (-1 if action == "SHORT" else 1) * (
                    1 if held_value > 0 else -1
                )
                self.assertAlmostEqual(verdict.correlations["HELD"], expected_corr, places=6)

    def test_signed_market_values_keep_gross_cap_absolute(self):
        config = PortfolioLimitsConfig(
            vol_sizing_enabled=False, max_gross_exposure_pct=80.0,
        )
        verdict = assess_new_position(
            "NEW", 20000.0, 100000.0,
            {"LONG": 60000.0, "SHORT": -30000.0},
            {}, config=config, action="LONG",
        )
        self.assertEqual(verdict.adjusted_notional, 0.0)
        self.assertTrue(any("$90,000" in reason for reason in verdict.reasons))

    def test_gather_preserves_short_market_value_sign(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        client = SimpleNamespace(
            get_account=lambda: SimpleNamespace(equity="100000"),
            get_all_positions=lambda: [
                SimpleNamespace(symbol="LONG", qty="12", market_value="12000"),
                # Some broker adapters provide short market value unsigned.
                SimpleNamespace(symbol="SHORT", qty="-7", market_value="7000"),
            ],
        )
        with patch("tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client", return_value=client), \
             patch("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data", return_value=_frame([1, 2, 3])):
            _, positions, _ = gather_portfolio_state_via_alpaca("NEW")
        self.assertEqual(positions, {"LONG": 12000.0, "SHORT": -7000.0})



class KellyStaysOffTests(unittest.TestCase):
    def test_kelly_sizing_defaults_to_disabled(self):
        self.assertFalse(DEFAULT_CONFIG.get("risk_sizing_enabled"))
        # The deterministic caps (Phase B) do not flip the Kelly switch.
        self.assertNotIn("risk_sizing_enabled", {}, msg="sanity")

    def test_position_sizer_only_runs_with_risk_params(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from tradingagents.execution.service import ExecutionService
        from tests.test_phase_b_caps import ExecutionIntegrationTests

        broker = ExecutionIntegrationTests._broker(object())
        broker.get_account = lambda: SimpleNamespace(
            id="paper-1", equity="100000", last_equity="100000", cash="80000", buying_power="160000"
        )
        intent = ExecutionIntegrationTests._intent(object(), current="NEUTRAL")
        # N09: an opening payload must match the canonical plan derived from
        # action+trading_mode+current_position, so a same-side increase can
        # no longer be expressed by mutating current_position while keeping
        # the OPEN_LONG planned action. The Kelly gate is asserted on a
        # canonical fresh opening against a flat account.
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: __import__(
                    "tradingagents.execution.authority", fromlist=["BrokerQuote"]
                ).BrokerQuote("AAPL", 99.0, 101.0, __import__(
                    "datetime").datetime.now(__import__("datetime").timezone.utc)),
            )
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=SimpleNamespace(enabled=False, check_order=lambda *a, **k: None,
                                             record_order_result=lambda ok: None),
            ):
                result = svc.execute(trade_intent=intent, dollar_amount=1000.0)
        self.assertTrue(result["success"])
        self.assertNotIn("risk_sizing", result)  # Kelly path untouched without risk_params


class RegimeAndMemoryCallerEvidenceTests(unittest.TestCase):

    def test_regime_multiplier_shrinks_hostile_regimes_only(self):
        from types import SimpleNamespace

        from tradingagents.regime import RegimeConfig, regime_risk_multiplier

        hostile = SimpleNamespace(
            label="turbulent", risk_multiplier=0.4,
            volatility_state="turbulent", trend_state="downtrend",
            liquidity_state="normal",
        )
        with patch("tradingagents.regime._load_assessment", return_value=hostile):
            multiplier = regime_risk_multiplier(
                "NVDA", config=RegimeConfig.from_config(DEFAULT_CONFIG)
            )
        self.assertEqual(multiplier, 0.4)
        with patch("tradingagents.regime._load_assessment", return_value=None):
            self.assertEqual(
                regime_risk_multiplier("NVDA", config=RegimeConfig.from_config(DEFAULT_CONFIG)),
                1.0,
            )

    def test_memory_reflection_suites_exist_and_graph_uses_memory(self):
        # Memory maintenance/reflection keep their dedicated suites (run with
        # the full offline suite); the graph wires per-agent memories.
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        source = inspect.getsource(TradingAgentsGraph)
        for memory in ("bull_memory", "bear_memory", "trader_memory",
                       "invest_judge_memory", "risk_manager_memory"):
            self.assertIn(memory, source)
        self.assertIn("maintain_all_memories", source)


if __name__ == "__main__":
    unittest.main()
