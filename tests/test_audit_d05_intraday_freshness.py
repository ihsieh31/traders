"""Intraday freshness follows completed session bars, not date membership."""

import pandas as pd
import pytest

from tradingagents.dataflows.technical_brief import _evaluate_frame_quality


@pytest.mark.parametrize("bar,reference,timeframe,close,expected", [
    ("2026-09-15 09:30", "2026-09-16 08:00", "1h", "16:00", "stale"),
    ("2026-09-15 15:30", "2026-09-16 08:00", "1h", "16:00", "fresh"),
    ("2026-09-15 15:30", "2026-09-16 14:00", "1h", "16:00", "stale"),
    ("2026-09-16 09:30", "2026-09-16 14:00", "1h", "16:00", "stale"),
    ("2026-09-16 12:30", "2026-09-16 14:00", "1h", "16:00", "fresh"),
    ("2026-09-15 13:30", "2026-09-16 12:00", "4h", "16:00", "fresh"),
    ("2026-09-16 12:30", "2026-09-16 17:00", "1h", "13:00", "fresh"),
    ("2026-09-18 15:30", "2026-09-20 12:00", "1h", "16:00", "fresh"),
    ("2026-09-18 09:30", "2026-09-20 12:00", "1h", "16:00", "stale"),
])
def test_intraday_completed_bar_gap(bar, reference, timeframe, close, expected):
    rows = [{"date": day, "open": "09:30", "close": close}
            for day in ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")]
    frame = pd.DataFrame([dict(timestamp=pd.Timestamp(bar, tz="America/New_York"),
                               open=100, high=101, low=99, close=100, volume=10)])
    _, quality = _evaluate_frame_quality(
        symbol="AAPL", timeframe_key=timeframe, df=frame,
        reference=pd.Timestamp(reference, tz="America/New_York").tz_convert("UTC"),
        calendar_rows=rows,
    )
    assert quality.status == expected, quality.reason
