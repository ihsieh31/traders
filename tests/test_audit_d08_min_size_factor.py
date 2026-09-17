"""D08 regression: portfolio min_size_factor must stay within (0, 1].

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D08: the floor is taken
as-is from config, so min_size_factor=2 *multiplies* the requested size
($1000 -> $2000 in the audit repro). A floor above 1 is invalid — it can
only ever enlarge a trade. Invalid configs must be clamped to 1.0
(no silent enlargement), and non-finite/negative holdings must not inflate
the gross exposure cap calculation.
"""

import math
import unittest

import pandas as pd

from tradingagents.portfolio import (
    PortfolioLimitsConfig,
    assess_new_position,
)


def _history(symbol, days=70, base=100.0):
    import pandas as pd

    rows = []
    price = base
    for i in range(days):
        rows.append({"timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                     "close": price})
        price *= 1.001
    return {symbol: pd.DataFrame(rows)}


class D08MinSizeFactorTests(unittest.TestCase):
    def test_d08_min_size_factor_above_one_is_clamped(self):
        config = PortfolioLimitsConfig(min_size_factor=2.0)
        verdict = assess_new_position(
            "AAPL", 1000.0, 50_000.0, {}, _history("AAPL"), config
        )
        self.assertEqual(
            verdict.adjusted_notional, 1000.0,
            "D08: a floor >1 must not enlarge the requested notional",
        )

    def test_d08_min_size_factor_invalid_is_clamped(self):
        for bad in (-0.5, 0.0, float("nan")):
            config = PortfolioLimitsConfig(min_size_factor=bad)
            verdict = assess_new_position(
                "AAPL", 1000.0, 50_000.0, {}, _history("AAPL"), config
            )
            self.assertEqual(
                verdict.adjusted_notional, 1000.0,
                f"D08: invalid min_size_factor {bad} must not change sizing",
            )

    def test_d08_legitimate_floor_still_applies(self):
        # A correlated_size_factor of 0.1 with min_size_factor 0.25 keeps the
        # normal floor behavior: the verdict shrinks but never below 25%.
        # Both symbols get the same (noisy) history so the correlation is 1.0.
        import numpy as np

        config = PortfolioLimitsConfig(
            min_size_factor=0.25, correlated_size_factor=0.1, high_correlation=0.6
        )
        rng = np.random.default_rng(42)
        closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, 70))
        frame = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=70, freq="D"),
            "close": closes,
        })
        history = {"AAPL": frame, "MSFT": frame.copy()}
        verdict = assess_new_position(
            "AAPL", 1000.0, 50_000.0, {"MSFT": 5000.0}, history, config
        )
        self.assertGreaterEqual(verdict.adjusted_notional, 250.0)
        self.assertLess(verdict.adjusted_notional, 1000.0)


if __name__ == "__main__":
    unittest.main()
