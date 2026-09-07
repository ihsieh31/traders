"""Tests for the walk-forward backtesting engine and its metrics."""

import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tradingagents.backtest import (
    annualized_return,
    cumulative_return,
    load_recorded_signals,
    max_drawdown,
    normalize_action,
    normalize_price_frame,
    run_backtest,
    run_recorded_backtest,
    run_walk_forward,
    sharpe_ratio,
    summarize_performance,
    win_rate,
)


def make_prices(closes, start="2026-01-05", opens=None, freq="B"):
    """Build an OHLCV frame in AlpacaUtils.get_stock_data's output shape."""
    closes = [float(c) for c in closes]
    opens = [float(o) for o in opens] if opens is not None else list(closes)
    dates = pd.bdate_range(start=start, periods=len(closes)) if freq == "B" else (
        pd.date_range(start=start, periods=len(closes), freq=freq)
    )
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": opens,
            "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
            "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        }
    )


class MetricsTests(unittest.TestCase):
    def test_cumulative_return_hand_computed(self):
        self.assertAlmostEqual(cumulative_return([100.0, 125.0]), 0.25)
        self.assertAlmostEqual(cumulative_return([100.0, 80.0]), -0.20)

    def test_cumulative_return_degenerate_inputs(self):
        self.assertIsNone(cumulative_return([]))
        self.assertIsNone(cumulative_return([100.0]))
        self.assertIsNone(cumulative_return([0.0, 50.0]))

    def test_annualized_return_hand_computed(self):
        # +10% over 126 periods at 252 periods/year => (1.1)^2 - 1 = 21%
        curve = [100.0] + [100.0] * 125 + [110.0]
        self.assertAlmostEqual(
            annualized_return(curve, periods_per_year=252), 1.1**2 - 1.0, places=9
        )

    def test_sharpe_ratio_hand_computed(self):
        # Alternating +1%/-1% daily returns: mean 0 => Sharpe ~ 0 (slightly
        # negative because (1.01 * 0.99) < 1); must not be None.
        curve = [100.0]
        for i in range(20):
            curve.append(curve[-1] * (1.01 if i % 2 == 0 else 0.99))
        value = sharpe_ratio(curve)
        self.assertIsNotNone(value)
        self.assertLess(abs(value), 0.5)

    def test_sharpe_ratio_zero_volatility_is_none(self):
        self.assertIsNone(sharpe_ratio([100.0, 100.0, 100.0, 100.0]))
        self.assertIsNone(sharpe_ratio([100.0, 101.0]))  # too short

    def test_max_drawdown_hand_computed(self):
        # Peak 120 -> trough 90 = 25% drawdown; later recovery must not hide it.
        self.assertAlmostEqual(
            max_drawdown([100.0, 120.0, 90.0, 130.0]), 0.25
        )
        self.assertAlmostEqual(max_drawdown([100.0, 110.0, 121.0]), 0.0)

    def test_win_rate(self):
        self.assertAlmostEqual(win_rate([10.0, -5.0, 3.0, -1.0]), 0.5)
        self.assertIsNone(win_rate([]))
        self.assertAlmostEqual(win_rate([0.0, 2.0]), 0.5)  # breakeven not a win

    def test_summarize_performance_keys(self):
        summary = summarize_performance([100.0, 105.0, 103.0], [4.0])
        for key in (
            "cumulative_return",
            "annualized_return",
            "sharpe_ratio",
            "max_drawdown",
            "win_rate",
            "num_trades",
            "num_periods",
            "final_equity",
        ):
            self.assertIn(key, summary)
        self.assertEqual(summary["num_trades"], 1)
        self.assertEqual(summary["num_periods"], 2)
        self.assertAlmostEqual(summary["final_equity"], 103.0)


class SignalNormalizationTests(unittest.TestCase):
    def test_all_aliases(self):
        for raw, expected in {
            "BUY": "BUY",
            "long": "LONG",
            " Short ": "SHORT",
            "sell": "SELL",
            "HOLD": "HOLD",
            "Neutral": "NEUTRAL",
        }.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_action(raw), expected)

    def test_unknown_inputs_are_none(self):
        for raw in (None, "", "MAYBE", 42):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_action(raw))


def write_run_log(root, symbol, trade_date, final_signal, status="completed", started_at=None):
    runs_dir = Path(root) / symbol.replace("/", "_") / "TradingAgentsStrategy_logs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    started = started_at or f"{trade_date}T10:00:00+00:00"
    payload = {
        "run_id": f"{trade_date}_{started.replace(':', '')}",
        "symbol": symbol,
        "trade_date": trade_date,
        "started_at": started,
        "ended_at": started,
        "status": status,
        "summary": {"final_signal": final_signal, "llm_call_events": 1, "tool_events": 1} if final_signal else {},
    }
    path = runs_dir / f"{payload['run_id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class LoadRecordedSignalsTests(unittest.TestCase):
    def test_reads_completed_runs_and_normalizes(self):
        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "AAPL", "2026-01-05", "BUY")
            write_run_log(root, "AAPL", "2026-01-06", "long")
            write_run_log(root, "AAPL", "2026-01-07", "NEUTRAL")
            signals = load_recorded_signals("AAPL", eval_results_dir=root)
        self.assertEqual(
            signals,
            {"2026-01-05": "BUY", "2026-01-06": "LONG", "2026-01-07": "NEUTRAL"},
        )

    def test_ignores_aborted_missing_signal_and_bad_dates(self):
        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "AAPL", "2026-01-05", "BUY", status="aborted")
            write_run_log(root, "AAPL", "2026-01-06", None)
            write_run_log(root, "AAPL", "not-a-date", "BUY")
            self.assertEqual(load_recorded_signals("AAPL", eval_results_dir=root), {})

    def test_first_eligible_run_per_date_wins(self):
        with tempfile.TemporaryDirectory() as root:
            write_run_log(
                root, "AAPL", "2026-01-05", "BUY",
                started_at="2026-01-05T09:00:00+00:00",
            )
            write_run_log(
                root, "AAPL", "2026-01-05", "SELL",
                started_at="2026-01-05T15:00:00+00:00",
            )
            signals = load_recorded_signals("AAPL", eval_results_dir=root)
        self.assertEqual(signals, {"2026-01-05": "BUY"})

    def test_crypto_symbol_path_sanitization(self):
        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "BTC/USD", "2026-01-05", "BUY")
            signals = load_recorded_signals("BTC/USD", eval_results_dir=root)
        self.assertEqual(signals, {"2026-01-05": "BUY"})

    def test_missing_directory_returns_empty(self):
        self.assertEqual(
            load_recorded_signals("ZZZZ", eval_results_dir="definitely_missing_dir"),
            {},
        )


class NormalizePriceFrameTests(unittest.TestCase):
    def test_accepts_alpaca_layout(self):
        frame = normalize_price_frame(make_prices([100, 101, 102]))
        self.assertIsInstance(frame.index, pd.DatetimeIndex)
        self.assertEqual(
            list(frame.columns), ["open", "high", "low", "close", "volume"]
        )

    def test_strips_timezone_and_sorts(self):
        prices = make_prices([100, 101, 102])
        prices["timestamp"] = prices["timestamp"].dt.tz_localize("UTC")
        prices = prices.iloc[::-1]  # reversed order on purpose
        frame = normalize_price_frame(prices)
        self.assertIsNone(frame.index.tz)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_rejects_empty_and_missing_columns(self):
        with self.assertRaises(ValueError):
            normalize_price_frame(pd.DataFrame())
        with self.assertRaises(ValueError):
            normalize_price_frame(pd.DataFrame({"timestamp": ["2026-01-05"], "close": [1.0]}))


class RunBacktestTests(unittest.TestCase):
    def test_hold_only_keeps_cash_flat(self):
        prices = make_prices([100, 120, 80, 150])
        result = run_backtest(prices, {"2026-01-05": "HOLD"}, initial_cash=10_000)
        self.assertAlmostEqual(result.equity_curve.iloc[-1], 10_000.0)
        self.assertEqual(result.orders, [])
        self.assertEqual(result.metrics["num_trades"], 0)

    def test_buy_captures_uptrend(self):
        closes = [100 + i for i in range(30)]
        prices = make_prices(closes)
        result = run_backtest(
            prices, {"2026-01-05": "BUY"}, initial_cash=100_000, commission=0.0
        )
        self.assertGreater(result.equity_curve.iloc[-1], 100_000.0)
        self.assertEqual(result.orders[0]["side"], "buy")

    def test_no_lookahead_signal_fills_at_next_open(self):
        # The signal-day close is 100 but the next open gaps to 200. A leaky
        # engine filling at the signal-day close would buy 40 shares at 100
        # and end at 14,000; correct next-open execution buys at 200 and the
        # equity never captures the jump. (position_pct is small enough that
        # the gapped-up order stays affordable.)
        prices = make_prices(
            closes=[100, 200, 200, 200],
            opens=[100, 200, 200, 200],
        )
        result = run_backtest(
            prices,
            {"2026-01-05": "BUY"},
            initial_cash=10_000,
            commission=0.0,
            position_pct=0.4,
            slippage_bps=0.0,  # this test isolates lookahead, not costs
        )
        self.assertEqual(result.orders[0]["date"], "2026-01-06")
        self.assertAlmostEqual(result.orders[0]["price"], 200.0)
        self.assertAlmostEqual(result.equity_curve.iloc[-1], 10_000.0, delta=1.0)

    def test_unaffordable_gap_order_is_reported_not_silent(self):
        # Full-size order computed at close=100 becomes unaffordable at the
        # 200 open; the engine must record the rejection.
        prices = make_prices(closes=[100, 200, 200], opens=[100, 200, 200])
        result = run_backtest(
            prices, {"2026-01-05": "BUY"}, initial_cash=10_000, commission=0.0, position_pct=0.95
        )
        self.assertEqual(result.orders, [])
        self.assertEqual(len(result.rejected_orders), 1)
        self.assertEqual(result.rejected_orders[0]["status"], "margin")

    def test_sell_when_flat_without_shorts_does_nothing(self):
        prices = make_prices([100, 90, 80, 70])
        result = run_backtest(
            prices, {"2026-01-05": "SELL"}, initial_cash=10_000, allow_shorts=False
        )
        self.assertAlmostEqual(result.equity_curve.iloc[-1], 10_000.0)
        self.assertEqual(result.orders, [])

    def test_short_profits_in_downtrend_when_allowed(self):
        closes = [100 - 2 * i for i in range(20)]
        prices = make_prices(closes)
        result = run_backtest(
            prices,
            {"2026-01-05": "SHORT"},
            initial_cash=100_000,
            commission=0.0,
            allow_shorts=True,
        )
        self.assertGreater(result.equity_curve.iloc[-1], 100_000.0)
        self.assertEqual(result.orders[0]["side"], "sell")

    def test_round_trip_produces_closed_trade(self):
        closes = [100, 100, 110, 120, 120, 120]
        prices = make_prices(closes)
        result = run_backtest(
            prices,
            {"2026-01-05": "BUY", "2026-01-08": "SELL"},
            initial_cash=100_000,
            commission=0.0,
        )
        self.assertEqual(len(result.trade_pnls), 1)
        self.assertGreater(result.trade_pnls[0], 0.0)
        self.assertEqual(result.metrics["win_rate"], 1.0)

    def test_weekend_signal_applies_on_next_bar(self):
        # Business days 2026-01-05..09 and 12..13; 2026-01-10 is a Saturday,
        # so the buy applies on Monday the 12th and fills at Tuesday's open.
        prices = make_prices([100, 101, 102, 103, 104, 105, 106])
        result = run_backtest(
            prices, {"2026-01-10": "BUY"}, initial_cash=10_000, commission=0.0
        )
        self.assertEqual(len(result.orders), 1)
        self.assertEqual(result.orders[0]["date"], "2026-01-13")

    def test_commission_reduces_final_equity(self):
        closes = [100] * 10
        prices = make_prices(closes)
        signals = {"2026-01-05": "BUY", "2026-01-09": "SELL"}
        free = run_backtest(prices, signals, initial_cash=10_000, commission=0.0)
        costly = run_backtest(prices, signals, initial_cash=10_000, commission=0.01)
        self.assertLess(
            costly.equity_curve.iloc[-1], free.equity_curve.iloc[-1]
        )

    def test_result_to_dict_is_json_serializable(self):
        prices = make_prices([100, 101, 102])
        result = run_backtest(prices, {"2026-01-05": "BUY"})
        payload = json.dumps(result.to_dict())
        self.assertIn("cumulative_return", payload)


class SlippageTests(unittest.TestCase):
    """Execution-cost realism: fills must be able to model slippage.

    A zero-slippage assumption systematically inflates backtest results, so
    a fixed basis-point model is on by default and a volatility-scaled model
    is available for less liquid / more volatile symbols.
    """

    BUY = {"2026-01-05": "BUY"}

    def test_fixed_bps_slippage_raises_buy_fill_price(self):
        prices = make_prices([100] * 6)
        result = run_backtest(
            prices, self.BUY, initial_cash=10_000, commission=0.0, slippage_bps=50.0
        )
        # 50 bps over the 100.0 next-bar open.
        self.assertAlmostEqual(result.orders[0]["price"], 100.5)

    def test_zero_bps_fills_exactly_at_open(self):
        prices = make_prices([100] * 6)
        result = run_backtest(
            prices, self.BUY, initial_cash=10_000, commission=0.0, slippage_bps=0.0
        )
        self.assertAlmostEqual(result.orders[0]["price"], 100.0)

    def test_slippage_is_on_by_default(self):
        prices = make_prices([100] * 6)
        default = run_backtest(prices, self.BUY, initial_cash=10_000, commission=0.0)
        self.assertGreater(default.orders[0]["price"], 100.0)

    def test_sell_fill_price_slips_down(self):
        prices = make_prices([100] * 8)
        signals = {"2026-01-05": "BUY", "2026-01-08": "SELL"}
        result = run_backtest(
            prices, signals, initial_cash=10_000, commission=0.0, slippage_bps=50.0
        )
        sells = [o for o in result.orders if o["side"] == "sell"]
        self.assertAlmostEqual(sells[0]["price"], 99.5)

    def test_slippage_reduces_round_trip_equity(self):
        prices = make_prices([100] * 8)
        signals = {"2026-01-05": "BUY", "2026-01-08": "SELL"}
        free = run_backtest(
            prices, signals, initial_cash=10_000, commission=0.0, slippage_bps=0.0
        )
        slipped = run_backtest(
            prices, signals, initial_cash=10_000, commission=0.0, slippage_bps=50.0
        )
        self.assertLess(slipped.equity_curve.iloc[-1], free.equity_curve.iloc[-1])

    def test_volatility_model_scales_with_previous_bar_range(self):
        # make_prices gives every bar high=101/low=99/close=100, so the
        # previous-bar range fraction is 2%; with vol_fraction=0.1 the buy
        # slips by 20 bps over the 100.0 open.
        prices = make_prices([100] * 6)
        result = run_backtest(
            prices,
            self.BUY,
            initial_cash=10_000,
            commission=0.0,
            slippage_model="volatility",
            slippage_vol_fraction=0.1,
        )
        self.assertAlmostEqual(result.orders[0]["price"], 100.2)

    def test_volatility_model_respects_max_cap(self):
        prices = make_prices([100] * 6)
        result = run_backtest(
            prices,
            self.BUY,
            initial_cash=10_000,
            commission=0.0,
            slippage_model="volatility",
            slippage_vol_fraction=0.1,
            slippage_max_bps=5.0,
        )
        self.assertAlmostEqual(result.orders[0]["price"], 100.05)

    def test_none_model_disables_slippage(self):
        prices = make_prices([100] * 6)
        result = run_backtest(
            prices,
            self.BUY,
            initial_cash=10_000,
            commission=0.0,
            slippage_model="none",
            slippage_bps=50.0,
        )
        self.assertAlmostEqual(result.orders[0]["price"], 100.0)

    def test_unknown_model_raises(self):
        prices = make_prices([100] * 6)
        with self.assertRaises(ValueError):
            run_backtest(prices, self.BUY, slippage_model="quantum")

    def test_result_reports_slippage_config(self):
        prices = make_prices([100] * 6)
        result = run_backtest(prices, self.BUY, slippage_bps=7.5)
        self.assertEqual(result.to_dict()["slippage"]["model"], "fixed")
        self.assertAlmostEqual(result.to_dict()["slippage"]["bps"], 7.5)


class WalkForwardTests(unittest.TestCase):
    def test_windows_partition_all_bars(self):
        prices = make_prices([100 + i for i in range(50)])
        result = run_walk_forward(
            prices, {"2026-01-05": "BUY"}, window_bars=20, commission=0.0
        )
        self.assertEqual(sum(w["bars"] for w in result.windows), 50)
        # 20 + 20 + 10 -> the 10-bar remainder stands alone (>= min_window_bars)
        self.assertEqual([w["bars"] for w in result.windows], [20, 20, 10])

    def test_short_remainder_folds_into_last_window(self):
        prices = make_prices([100 + i for i in range(43)])
        result = run_walk_forward(
            prices, {"2026-01-05": "BUY"}, window_bars=20, min_window_bars=5
        )
        self.assertEqual([w["bars"] for w in result.windows], [20, 23])

    def test_each_window_has_metrics_and_full_period_present(self):
        prices = make_prices([100 + i for i in range(30)])
        result = run_walk_forward(prices, {"2026-01-05": "BUY"}, window_bars=10)
        for window in result.windows:
            self.assertIn("sharpe_ratio", window["metrics"])
            self.assertIn("max_drawdown", window["metrics"])
        self.assertIsNotNone(result.full_period)
        self.assertEqual(result.full_period.metrics["num_periods"], 29)

    def test_rejects_tiny_window(self):
        prices = make_prices([100, 101, 102])
        with self.assertRaises(ValueError):
            run_walk_forward(prices, {}, window_bars=1)


class RunRecordedBacktestTests(unittest.TestCase):
    def test_end_to_end_with_recorded_runs_and_injected_prices(self):
        closes = [100 + i for i in range(15)]
        prices = make_prices(closes, start="2026-01-05")

        def fake_loader(symbol, start, end):
            self.assertEqual(symbol, "AAPL")
            return prices

        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "AAPL", "2026-01-05", "BUY")
            write_run_log(root, "AAPL", "2026-01-12", "SELL")
            result = run_recorded_backtest(
                "AAPL",
                eval_results_dir=root,
                price_loader=fake_loader,
                commission=0.0,
            )
        self.assertEqual(result.signals_used, 2)
        self.assertEqual(len(result.trade_pnls), 1)
        self.assertGreater(result.metrics["cumulative_return"], 0.0)

    def test_raises_without_recorded_signals(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                run_recorded_backtest("AAPL", eval_results_dir=root)

    def test_signals_before_start_date_are_dropped(self):
        prices = make_prices([100 + i for i in range(10)], start="2026-02-02")

        def fake_loader(symbol, start, end):
            self.assertEqual(start, "2026-02-02")
            return prices

        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "AAPL", "2026-01-05", "SELL")
            write_run_log(root, "AAPL", "2026-02-03", "BUY")
            result = run_recorded_backtest(
                "AAPL",
                start_date="2026-02-02",
                eval_results_dir=root,
                price_loader=fake_loader,
            )
        self.assertEqual(result.signals_used, 1)

    def test_raises_when_all_signals_predate_start(self):
        with tempfile.TemporaryDirectory() as root:
            write_run_log(root, "AAPL", "2026-01-05", "BUY")
            with self.assertRaises(ValueError):
                run_recorded_backtest(
                    "AAPL", start_date="2026-06-01", eval_results_dir=root,
                    price_loader=lambda s, a, b: make_prices([1, 2]),
                )


if __name__ == "__main__":
    unittest.main()
