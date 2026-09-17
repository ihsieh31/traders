"""U08–U10 regression tests for webui.utils.market_hours.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4:
- U08: validate_market_hours accepts hour 9 but get_next_market_datetime
  builds 09:00, which the authoritative open gate (09:30) always rejects;
  the scheduler then waits for a slot that can never execute. Hour
  validation must only accept whole hours the open gate can execute
  (10-15; 16 kept but flagged as at-the-close) and get_next_market_datetime
  must raise instead of returning a guessed date when no valid slot can be
  proven.
- U09: get_next_market_datetime's exhausted-attempts fallback returns an
  unvalidated date (audit: all-unavailable returned +15 days) instead of
  failing closed.
- U10: advancing an aware pytz Eastern datetime by +1 day keeps the old
  UTC offset, producing a wrong wall time across the DST boundary
  (2026-03-09 11:00 -05 instead of EDT).
"""

import datetime
import importlib.util
import pathlib
import unittest
from types import SimpleNamespace

import pytz

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "webui" / "utils" / "market_hours.py"
SPEC = importlib.util.spec_from_file_location("market_hours_under_test_u08", MODULE_PATH)
market_hours = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(market_hours)


def _row(day, close_h=16, close_m=0):
    return SimpleNamespace(
        date=day,
        open=datetime.datetime(day.year, day.month, day.day, 9, 30),
        close=datetime.datetime(day.year, day.month, day.day, close_h, close_m),
    )


class ValidateMarketHoursTests(unittest.TestCase):
    def test_u08_hour_nine_is_rejected_because_open_gate_is_0930(self):
        is_valid, hours, error = market_hours.validate_market_hours("9,11")
        self.assertFalse(is_valid)
        self.assertIn("9:30", error)

    def test_u08_trading_hours_within_open_window_are_accepted(self):
        is_valid, hours, error = market_hours.validate_market_hours("10,15,16")
        self.assertTrue(is_valid, error)
        self.assertEqual(hours, [10, 15, 16])

    def test_u08_after_close_is_rejected(self):
        is_valid, _, error = market_hours.validate_market_hours("17")
        self.assertFalse(is_valid)


class NextMarketDatetimeTests(unittest.TestCase):
    def _week_rows(self, start_day, days=20):
        days_list = []
        day = start_day
        while len(days_list) < days:
            if day.weekday() < 5:
                days_list.append(day)
            day += datetime.timedelta(days=1)
        return [_row(d) for d in days_list]

    def test_u09_all_unavailable_calendar_fails_closed_instead_of_guessing(self):
        # Calendar rows prove nothing (empty) -> is_market_open raises
        # CalendarError internally -> fail closed, never return a date.
        start = pytz.timezone("US/Eastern").localize(datetime.datetime(2026, 9, 17, 11, 0))
        with self.assertRaises(market_hours.MarketScheduleError):
            market_hours.get_next_market_datetime(11, start, calendar_rows=[])

    def test_u08_hour_nine_never_schedules_an_unexecutable_slot(self):
        eastern = pytz.timezone("US/Eastern")
        rows = self._week_rows(datetime.date(2026, 9, 14))
        start = eastern.localize(datetime.datetime(2026, 9, 17, 11, 0))
        with self.assertRaises(market_hours.MarketScheduleError):
            market_hours.get_next_market_datetime(9, start, calendar_rows=rows)

    def test_u08_hour_sixteen_schedules_before_close(self):
        eastern = pytz.timezone("US/Eastern")
        rows = self._week_rows(datetime.date(2026, 9, 14))
        start = eastern.localize(datetime.datetime(2026, 9, 17, 11, 0))
        next_dt = market_hours.get_next_market_datetime(16, start, calendar_rows=rows)
        self.assertEqual(next_dt.strftime("%Y-%m-%d %H:%M"), "2026-09-17 16:00")

    def test_u09_exhausted_search_without_executable_slot_raises(self):
        # Rows are authoritative: 20 weekday sessions that all close early
        # (13:00, e.g. holiday weeks). An hour-16 slot can never execute on
        # them, so the bounded search must fail closed instead of returning
        # an unvalidated guessed date (audit: +15 days).
        eastern = pytz.timezone("US/Eastern")
        day = datetime.date(2026, 9, 14)
        rows = []
        while len(rows) < 20:
            if day.weekday() < 5:
                rows.append(_row(day, close_h=13))
            day += datetime.timedelta(days=1)
        start = eastern.localize(datetime.datetime(2026, 9, 17, 11, 0))
        with self.assertRaises(market_hours.MarketScheduleError):
            market_hours.get_next_market_datetime(16, start, calendar_rows=rows)

    def test_weekend_candidate_is_skipped_and_lands_on_monday(self):
        # Rows are authoritative Alpaca sessions (weekdays only). A Friday
        # 11:00 start must skip the weekend and land on Monday 11:00.
        eastern = pytz.timezone("US/Eastern")
        day = datetime.date(2026, 9, 14)
        rows = []
        while len(rows) < 8:
            if day.weekday() < 5:
                rows.append(_row(day))
            day += datetime.timedelta(days=1)
        start = eastern.localize(datetime.datetime(2026, 9, 18, 11, 0))  # Friday
        next_dt = market_hours.get_next_market_datetime(11, start, calendar_rows=rows)
        self.assertEqual(next_dt.strftime("%Y-%m-%d %H:%M %Z"), "2026-09-21 11:00 EDT")


class DstWallTimeTests(unittest.TestCase):
    def test_u10_dst_spring_forward_keeps_eastern_wall_time(self):
        # 2026-03-08 is the spring-forward transition. Friday 2026-03-06
        # 11:00 EST -> next weekday slot Monday 2026-03-09 must be 11:00 EDT
        # (-04:00), not a stale -05:00 offset rendering as 12:00 EDT.
        eastern = pytz.timezone("US/Eastern")
        rows = self._week_rows(datetime.date(2026, 3, 2))
        start = eastern.localize(datetime.datetime(2026, 3, 6, 11, 0))
        next_dt = market_hours.get_next_market_datetime(11, start, calendar_rows=rows)
        self.assertEqual(next_dt.strftime("%Y-%m-%d %H:%M %Z"), "2026-03-09 11:00 EDT")

    def _week_rows(self, start_day, days=10):
        days_list = []
        day = start_day
        while len(days_list) < days:
            if day.weekday() < 5:
                days_list.append(day)
            day += datetime.timedelta(days=1)
        return [_row(d) for d in days_list]


if __name__ == "__main__":
    unittest.main()
