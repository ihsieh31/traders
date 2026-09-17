"""D01 regression: _clean_frame must reject numerically invalid OHLCV rows.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D01: _clean_frame
validates timestamps and column presence but not values, so NaN prices,
negative prices, inverted OHLC relationships (high < low), and negative
volumes reach the indicator pipeline. volume=0 is legitimate (no-trade
bars) and must be accepted.
"""

import unittest

import pandas as pd


def _frame(rows):
    return pd.DataFrame(rows)


class D01CleanFrameValidationTests(unittest.TestCase):
    def test_d01_nan_close_rows_are_rejected(self):
        from tradingagents.dataflows.technical_brief import _clean_frame

        reference = pd.Timestamp("2026-09-15 15:00", tz="UTC")
        df = _frame([
            {"timestamp": "2026-09-15 14:00", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 1000},
            {"timestamp": "2026-09-15 14:30", "open": 100.5, "high": 101.5,
             "low": 100.0, "close": float("nan"), "volume": 1000},
        ])
        cleaned = _clean_frame(df, reference)
        self.assertIsNotNone(cleaned)
        # The NaN-close bar must be dropped, the good bar kept.
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(float(cleaned["close"].iloc[0]), 100.5)

    def test_d01_nonfinite_or_nonpositive_prices_rejected(self):
        from tradingagents.dataflows.technical_brief import _clean_frame

        reference = pd.Timestamp("2026-09-15 15:00", tz="UTC")
        df = _frame([
            {"timestamp": "2026-09-15 14:00", "open": -5.0, "high": 101.0,
             "low": -6.0, "close": 100.0, "volume": 1000},
            {"timestamp": "2026-09-15 14:30", "open": float("inf"),
             "high": 101.0, "low": 100.0, "close": 100.5, "volume": 1000},
        ])
        cleaned = _clean_frame(df, reference)
        self.assertTrue(cleaned is None or cleaned.empty)

    def test_d01_inverted_ohlc_rejected(self):
        from tradingagents.dataflows.technical_brief import _clean_frame

        reference = pd.Timestamp("2026-09-15 15:00", tz="UTC")
        df = _frame([
            # high < low is impossible
            {"timestamp": "2026-09-15 14:00", "open": 100.0, "high": 99.0,
             "low": 101.0, "close": 100.0, "volume": 1000},
        ])
        cleaned = _clean_frame(df, reference)
        self.assertTrue(cleaned is None or cleaned.empty)

    def test_d01_negative_volume_rejected_zero_volume_allowed(self):
        from tradingagents.dataflows.technical_brief import _clean_frame

        reference = pd.Timestamp("2026-09-15 15:00", tz="UTC")
        df = _frame([
            {"timestamp": "2026-09-15 14:00", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0, "volume": -10},
            {"timestamp": "2026-09-15 14:30", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 0},
        ])
        cleaned = _clean_frame(df, reference)
        self.assertIsNotNone(cleaned)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned["volume"].iloc[0], 0)

    def test_d01_duplicate_timestamps_deduplicated(self):
        from tradingagents.dataflows.technical_brief import _clean_frame

        reference = pd.Timestamp("2026-09-15 15:00", tz="UTC")
        row = {"timestamp": "2026-09-15 14:00", "open": 100.0, "high": 101.0,
               "low": 99.0, "close": 100.0, "volume": 1000}
        df = _frame([row, dict(row, close=100.7)])
        cleaned = _clean_frame(df, reference)
        self.assertIsNotNone(cleaned)
        self.assertEqual(len(cleaned), 1)


if __name__ == "__main__":
    unittest.main()
