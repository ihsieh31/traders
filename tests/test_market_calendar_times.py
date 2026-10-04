from datetime import date, datetime, time, timezone

import pytest

from tradingagents.dataflows.market_calendar import _coerce_et_time, session_close_et_auth


@pytest.mark.parametrize("raw, expected", [
    ("16:00", time(16, 0)),
    ("13:00:00", time(13, 0)),
    ("2026-09-04T16:00:00-04:00", time(16, 0)),
    # An instant in another zone is converted, not read as an ET wall time.
    ("2026-09-04T20:00:00Z", time(16, 0)),
    ("2026-12-24T18:00:00+00:00", time(13, 0)),
    ("2026-09-04 16:00:00", time(16, 0)),
    (datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc), time(16, 0)),
])
def test_calendar_times_are_eastern_wall_times(raw, expected):
    assert _coerce_et_time(raw) == expected


def test_utc_close_strings_yield_the_eastern_close():
    rows = [{"date": "2026-11-27", "open": "2026-11-27T14:30:00Z", "close": "2026-11-27T18:00:00Z"}]
    assert session_close_et_auth(date(2026, 11, 27), calendar_rows=rows) == time(13, 0)
