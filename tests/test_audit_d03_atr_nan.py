"""D03 regression: compute_atr must reject NaN high/low/close inputs.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D03: pandas
.concat(...).max(axis=1) skips NaN, so a frame whose high column is all
NaN yields a bogus ATR computed from low/prev_close only (audit repro:
ATR=1 vs the correct 2.0 reference; HEAD reproduces 0.9). Any non-finite
high/low/close in the evaluated window must return None.
"""

import math
import unittest

import pandas as pd

from tradingagents.risk.position_sizing import compute_atr


def _bars(high, low, close):
    return pd.DataFrame({"high": high, "low": low, "close": close})


class D03AtrValidationTests(unittest.TestCase):
    def test_d03_all_high_nan_returns_none(self):
        n = 16
        bars = _bars(
            [float("nan")] * n,
            [9.0 + i * 0.1 for i in range(n)],
            [10.0 + i * 0.1 for i in range(n)],
        )
        self.assertIsNone(compute_atr(bars))

    def test_d03_single_nan_in_window_returns_none(self):
        n = 16
        highs = [11.0] * n
        highs[5] = float("nan")
        bars = _bars(
            highs,
            [9.0] * n,
            [10.0 + i * 0.01 for i in range(n)],
        )
        self.assertIsNone(compute_atr(bars))

    def test_d03_inf_low_returns_none(self):
        n = 16
        bars = _bars(
            [11.0] * n,
            [float("inf")] * n,
            [10.0] * n,
        )
        self.assertIsNone(compute_atr(bars))

    def test_d03_clean_data_still_computes_normal_atr(self):
        n = 16
        bars = _bars(
            [11.0] * n,
            [9.0] * n,
            [10.0 + i * 0.01 for i in range(n)],
        )
        atr = compute_atr(bars)
        self.assertIsNotNone(atr)
        self.assertTrue(math.isfinite(atr))
        self.assertAlmostEqual(atr, 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
