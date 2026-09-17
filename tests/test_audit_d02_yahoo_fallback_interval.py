"""D02 regression: the Yahoo fallback must not silently downsample 4h bars.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D02: _yfinance_fallback_
data maps any "hour" timeframe to Yahoo's 1h interval, so a 4Hour Alpaca
request would be answered with 1h bars — different bars under the caller's
expected granularity. Unsupported intervals must return no data (fail
closed); only intervals Yahoo actually supports are passed through.
"""

import unittest
from unittest.mock import patch

import pandas as pd


class _FakeTimeFrame4Hour:
    def __str__(self):
        return "4Hour"


class _FakeTimeFrame1Hour:
    def __str__(self):
        return "1Hour"


class D02FallbackIntervalTests(unittest.TestCase):
    def _fallback(self, timeframe):
        from tradingagents.dataflows import alpaca_utils

        with patch.object(alpaca_utils, "get_config",
                          lambda: {"data_fallback_enabled": True}):
            return alpaca_utils._yfinance_fallback_data(
                "AAPL", pd.Timestamp("2026-09-01"), None, timeframe
            )

    def test_d02_4hour_timeframe_is_unavailable_not_downsampled(self):
        import pandas as pd

        df = self._fallback(_FakeTimeFrame4Hour())
        self.assertTrue(df.empty,
                        "4h must not be answered with silently different 1h bars")

    def test_d02_1hour_timeframe_still_uses_1h(self):
        import pandas as pd
        from tradingagents.dataflows import alpaca_utils

        captured = {}

        def fake_download(symbol, start, end=None, interval=None, **kwargs):
            captured["interval"] = interval
            return pd.DataFrame()

        import yfinance

        with patch.object(alpaca_utils, "get_config",
                          lambda: {"data_fallback_enabled": True}), \
             patch.object(yfinance, "download", fake_download):
            alpaca_utils._yfinance_fallback_data(
                "AAPL", pd.Timestamp("2026-09-01"), None, "1Hour"
            )
        self.assertEqual(captured.get("interval"), "1h")

    def test_d02_minute_timeframe_stays_unavailable(self):
        import pandas as pd

        df = self._fallback("1Min")
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main()
