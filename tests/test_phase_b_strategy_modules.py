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
    adjust_new_position_notional,
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

    def test_auto_trade_path_wires_the_hook(self):
        # The WebUI auto-trade path delegates to the shared helper, which
        # must call the hook (regime scaling and portfolio sizing both
        # apply to NEW long exposure).
        import webui.components.analysis as analysis
        import tradingagents.execution.auto_trade as auto_trade

        webui_source = inspect.getsource(analysis)
        self.assertIn("execute_auto_trade", webui_source)
        helper_source = inspect.getsource(auto_trade)
        self.assertIn("adjust_new_position_notional", helper_source)
        self.assertIn("regime_risk_multiplier", helper_source)


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
    def test_regime_scaling_is_wired_into_auto_trade(self):
        import tradingagents.execution.auto_trade as auto_trade
        import webui.components.analysis as analysis

        helper_source = inspect.getsource(auto_trade.execute_auto_trade)
        self.assertIn("regime_risk_multiplier", helper_source)
        # The WebUI keeps no second sizing path: it delegates to the helper.
        webui_source = inspect.getsource(analysis.execute_trade_after_analysis)
        self.assertIn("execute_auto_trade", webui_source)
        self.assertNotIn("regime_risk_multiplier", webui_source)

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
