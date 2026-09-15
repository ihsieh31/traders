"""Bounded retry for read-only Alpaca screening fetches.

Regression for the 2026-09-15 observation stop: a single 10s read timeout
on the FIRST SIP daily-bars batch of the market scan killed the whole
30-day window (BARS_UNAVAILABLE -> SCREENING_STOPPED -> hard stop), while
the broker GET layer already allowed three attempts for the same failure
shape. ``fetch_with_bounded_retry`` now guards the daily bulk-read paths:
bars batches, universe pagination, and holdings — transient transport
failures retry with short backoff; definitive failures (401/403/404,
unrecognized shapes) and the fail-closed contexts (no IEX fallback,
UniverseError, HOLDINGS_UNAVAILABLE) are unchanged.
"""
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from tradingagents.dataflows.alpaca_utils import (
    fetch_with_bounded_retry,
    is_transient_fetch_error,
)
from tradingagents.screening.metrics import fetch_daily_bars_batch
from tradingagents.screening.pipeline import _default_asset, _default_positions
from tradingagents.screening.universe import UniverseError, fetch_us_equity_universe


class StatusBearingError(Exception):
    """alpaca-py APIError shape: integer .code with a message."""

    def __init__(self, code):
        super().__init__(f"api error {code}")
        self.code = code


class ClassificationTests(unittest.TestCase):
    def test_transport_shapes_are_transient(self):
        for exc in (
            TimeoutError("Read timed out. (read timeout=10.0)"),
            ConnectionError("connection reset by peer"),
            RuntimeError("HTTPSConnectionPool: RemoteDisconnected"),
            StatusBearingError(500),
            StatusBearingError(503),
            StatusBearingError(429),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(is_transient_fetch_error(exc))

    def test_definitive_failures_are_permanent(self):
        for exc in (
            StatusBearingError(401),
            StatusBearingError(403),  # no SIP entitlement: never retry
            StatusBearingError(404),
            ValueError("unsupported screening_bar_adjustment"),
            RuntimeError("boom"),  # unknown shape fails fast
        ):
            with self.subTest(exc=exc):
                self.assertFalse(is_transient_fetch_error(exc))


class BoundedRetryHelperTests(unittest.TestCase):
    def test_transient_then_success_single_retry(self):
        state = {"calls": 0, "sleeps": []}

        def fn():
            state["calls"] += 1
            if state["calls"] == 1:
                raise TimeoutError("Read timed out")
            return "ok"

        self.assertEqual(
            fetch_with_bounded_retry(fn, sleep=state["sleeps"].append), "ok"
        )
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["sleeps"], [0.5])

    def test_exhaustion_reraises_original_after_three_attempts(self):
        state = {"calls": 0, "sleeps": []}

        def fn():
            state["calls"] += 1
            raise ConnectionError("connection reset")

        with self.assertRaises(ConnectionError):
            fetch_with_bounded_retry(fn, attempts=3, sleep=state["sleeps"].append)
        self.assertEqual(state["calls"], 3)
        self.assertEqual(state["sleeps"], [0.5, 1.0])

    def test_permanent_error_no_retry_no_sleep(self):
        state = {"calls": 0, "sleeps": []}

        def fn():
            state["calls"] += 1
            raise StatusBearingError(403)

        with self.assertRaises(StatusBearingError):
            fetch_with_bounded_retry(fn, sleep=state["sleeps"].append)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(state["sleeps"], [])


def _bars_response(symbols):
    rows = []
    for symbol in symbols:
        for i in range(3):
            rows.append({"symbol": symbol, "timestamp": f"2026-09-1{i}", "close": 10.0 + i})
    return SimpleNamespace(df=pd.DataFrame(rows))


class BarsBatchRetryTests(unittest.TestCase):
    def test_transient_timeout_retries_same_sip_request(self):
        state = {"calls": 0}
        sleeps = []

        class Client:
            def get_stock_bars(self, request):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise TimeoutError("Read timed out. (read timeout=10.0)")
                return _bars_response(["AAA", "BBB", "CCC"])

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_stock_client",
            return_value=Client(),
        ):
            frames = fetch_daily_bars_batch(
                ["AAA", "BBB", "CCC"], as_of=date(2026, 9, 15), batch_size=2,
                sleep_fn=sleeps.append,
            )
        self.assertEqual(set(frames), {"AAA", "BBB", "CCC"})
        # chunk1 first try + chunk1 retry + chunk2 = 3 requests, 1 backoff
        self.assertEqual(state["calls"], 3)
        self.assertEqual(sleeps, [0.5])

    def test_exhausted_retries_still_fail_closed_with_sip_context(self):
        state = {"calls": 0}
        sleeps = []

        class Client:
            def get_stock_bars(self, request):
                state["calls"] += 1
                raise TimeoutError("Read timed out. (read timeout=10.0)")

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_stock_client",
            return_value=Client(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_daily_bars_batch(
                    ["AAA"], as_of=date(2026, 9, 15), batch_size=100,
                    sleep_fn=sleeps.append,
                )
        self.assertIn("SIP consolidated bars unavailable (feed=sip)", str(ctx.exception))
        self.assertIn("Read timed out", str(ctx.exception))
        self.assertEqual(state["calls"], 3)  # exactly the bounded attempts, once
        self.assertEqual(len(sleeps), 2)

    def test_permanent_entitlement_error_never_retries(self):
        state = {"calls": 0}

        class Client:
            def get_stock_bars(self, request):
                state["calls"] += 1
                raise StatusBearingError(403)

        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_stock_client",
            return_value=Client(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_daily_bars_batch(
                    ["AAA"], as_of=date(2026, 9, 15), batch_size=100,
                    sleep_fn=lambda s: self.fail("permanent errors must not sleep"),
                )
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(state["calls"], 1)


class UniverseRetryTests(unittest.TestCase):
    def test_transient_page_failure_retries_then_universe_builds(self):
        asset = SimpleNamespace(
            symbol="AAA", name="Alpha", exchange="NYSE",
            status="active", asset_class="us_equity", tradable=True,
        )
        state = {"calls": 0}

        class Client:
            def get_all_assets(self, request):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise ConnectionError("connection reset by peer")
                return [asset]

        universe = fetch_us_equity_universe(broker=Client())
        # NOTE: no sleep injection hook in this path; the 0.5s backoff is
        # acceptable in one test to prove the retry resolves the scan.
        self.assertEqual([u["symbol"] for u in universe], ["AAA"])
        self.assertEqual(state["calls"], 2)

    def test_permanent_error_still_universe_error_once(self):
        state = {"calls": 0}

        class Client:
            def get_all_assets(self, request):
                state["calls"] += 1
                raise StatusBearingError(403)

        with self.assertRaises(UniverseError):
            fetch_us_equity_universe(broker=Client())
        self.assertEqual(state["calls"], 1)


class HoldingsRetryTests(unittest.TestCase):
    def test_asset_lookup_retries_transient_failure(self):
        state = {"calls": 0}

        class Client:
            def get_asset(self, symbol):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise TimeoutError("Read timed out")
                return SimpleNamespace(symbol=symbol)

        with patch(
            "tradingagents.screening.pipeline.get_alpaca_trading_client",
            return_value=Client(),
        ):
            asset = _default_asset("VLO")
        self.assertEqual(asset.symbol, "VLO")
        self.assertEqual(state["calls"], 2)

    def test_positions_transient_then_success(self):
        state = {"calls": 0}

        class Client:
            def get_all_positions(self):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise ConnectionError("connection reset")
                return [SimpleNamespace(qty=5.0, symbol="VLO", asset_class="us_equity")]

        with patch(
            "tradingagents.screening.pipeline.get_alpaca_trading_client",
            return_value=Client(),
        ):
            positions = _default_positions()
        self.assertEqual([p["symbol"] for p in positions], ["VLO"])
        self.assertEqual(state["calls"], 2)


if __name__ == "__main__":
    unittest.main()
