"""D04 regression: regular-session bars must come from the session.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D04: the completion
clip to the reference session's close lets a bar that STARTS after that
close (e.g. a 16:30 extended-hours bar at a 17:00 reference) be accepted
as if it were the regular 16:00-close bar. Regular-session data must
exclude bars whose start is not inside a session's [open, close) window;
only the last bar OF the session may use the close-based early completion.
"""

import unittest
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from tradingagents.dataflows.technical_brief import _evaluate_frame_quality


def _rows():
    return [
        SimpleNamespace(date=date(2026, 9, 15),
                        open=datetime(2026, 9, 15, 9, 30),
                        close=datetime(2026, 9, 15, 16, 0)),
        SimpleNamespace(date=date(2026, 9, 16),
                        open=datetime(2026, 9, 16, 9, 30),
                        close=datetime(2026, 9, 16, 16, 0)),
    ]


class D04BarCompletionTests(unittest.TestCase):
    def test_d04_after_close_starting_bar_is_rejected(self):
        eastern_ref = pd.Timestamp("2026-09-16 17:00", tz="America/New_York").tz_convert("UTC")
        df = pd.DataFrame([
            {"timestamp": "2026-09-15 15:00-04:00", "open": 100, "high": 101,
             "low": 99, "close": 101, "volume": 10},
            {"timestamp": "2026-09-16 16:30-04:00", "open": 100, "high": 101,
             "low": 99, "close": 101, "volume": 10},
        ])
        cleaned, quality = _evaluate_frame_quality(
            symbol="AAPL", timeframe_key="1h", df=df,
            reference=eastern_ref, calendar_rows=_rows(),
        )
        # The 16:30 ET bar starts after the session close — it is not a
        # regular-session bar and must never be clipped into the session.
        if cleaned is not None:
            kept = [str(t) for t in cleaned["timestamp"].tolist()]
            self.assertNotIn("2026-09-16 20:30:00+00:00", kept)
        # Yesterday's 15:00 ET bar remains and is judged by the legitimate
        # freshness contract (belongs to the immediately previous session).
        self.assertEqual(quality.status, "fresh", quality.reason)

    def test_d04_in_session_bar_uses_close_truncation_only_for_last_bar(self):
        # 15:00 bar on the reference session completes at 16:00 close — that
        # is the legitimate close-based truncation and it must be accepted
        # once the reference is past the close.
        eastern_ref = pd.Timestamp("2026-09-16 17:00", tz="America/New_York").tz_convert("UTC")
        df = pd.DataFrame([
            {"timestamp": "2026-09-16 15:00-04:00", "open": 100, "high": 101,
             "low": 99, "close": 101, "volume": 10},
        ])
        cleaned, quality = _evaluate_frame_quality(
            symbol="AAPL", timeframe_key="1h", df=df,
            reference=eastern_ref, calendar_rows=_rows(),
        )
        self.assertEqual(quality.status, "fresh", quality.reason)

    def test_d04_mid_session_bar_still_requires_duration_completion(self):
        # 10:30 ET bar completes at 11:30 ET; at an 11:00 ET reference it is
        # NOT done and must not be treated as complete by close clipping.
        eastern_ref = pd.Timestamp("2026-09-16 11:00", tz="America/New_York").tz_convert("UTC")
        df = pd.DataFrame([
            {"timestamp": "2026-09-16 10:30-04:00", "open": 100, "high": 101,
             "low": 99, "close": 101, "volume": 10},
        ])
        cleaned, quality = _evaluate_frame_quality(
            symbol="AAPL", timeframe_key="1h", df=df,
            reference=eastern_ref, calendar_rows=_rows(),
        )
        self.assertEqual(quality.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
