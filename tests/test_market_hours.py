import datetime
import importlib.util
import pathlib
import unittest
from types import SimpleNamespace

import pytz

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "webui" / "utils" / "market_hours.py"
SPEC = importlib.util.spec_from_file_location("market_hours_under_test", MODULE_PATH)
market_hours = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(market_hours)

get_next_market_datetime = market_hours.get_next_market_datetime
is_market_open = market_hours.is_market_open


def _row(day, close_h=16, close_m=0):
    return SimpleNamespace(
        date=day,
        open=datetime.datetime(day.year, day.month, day.day, 9, 30),
        close=datetime.datetime(day.year, day.month, day.day, close_h, close_m),
    )


def _rows_for(dates):
    return [_row(d) for d in sorted(dates)]


class MarketHoursTests(unittest.TestCase):
    def test_naive_datetime_is_treated_as_eastern_wall_clock(self):
        rows = _rows_for([datetime.date(2025, 1, 6), datetime.date(2025, 1, 7)])
        is_open, reason = is_market_open(datetime.datetime(2025, 1, 6, 10, 0), calendar_rows=rows)
        self.assertTrue(is_open, reason)

    def test_aware_utc_datetime_is_converted_to_eastern(self):
        rows = _rows_for([datetime.date(2025, 1, 6), datetime.date(2025, 1, 7)])
        is_open, reason = is_market_open(datetime.datetime(2025, 1, 6, 15, 0, tzinfo=pytz.utc), calendar_rows=rows)
        self.assertTrue(is_open, reason)

    def test_2026_nyse_holiday_is_closed(self):
        eastern = pytz.timezone("US/Eastern")
        # 2026-07-03 is a holiday: rows contain 07-02 and 07-06, not 07-03.
        rows = _rows_for([datetime.date(2026, 7, 2), datetime.date(2026, 7, 6)])
        is_open, reason = is_market_open(eastern.localize(datetime.datetime(2026, 7, 3, 10, 0)), calendar_rows=rows)
        self.assertFalse(is_open)
        self.assertIn("holiday", reason.lower())

    def test_next_market_datetime_uses_eastern_schedule_from_aware_input(self):
        rows = _rows_for([datetime.date(2025, 1, 6), datetime.date(2025, 1, 7), datetime.date(2025, 1, 8)])
        start = datetime.datetime(2025, 1, 6, 17, 30, tzinfo=pytz.utc)  # Monday 12:30 PM ET
        next_dt = get_next_market_datetime(11, start, calendar_rows=rows)
        self.assertEqual(next_dt.strftime("%Y-%m-%d %H:%M %Z"), "2025-01-07 11:00 EST")

    def test_trading_day_without_close_fails_closed(self):
        day = datetime.date(2025, 1, 6)
        malformed = [SimpleNamespace(date=day, open="09:30", close=None)]

        is_open, reason = is_market_open(
            datetime.datetime(2025, 1, 6, 10, 0), calendar_rows=malformed
        )

        self.assertFalse(is_open)
        self.assertIn("close unavailable", reason.lower())


if __name__ == "__main__":
    unittest.main()
