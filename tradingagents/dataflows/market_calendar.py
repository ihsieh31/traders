"""US equity trading-day calendar (single shared source).

The NYSE/Nasdaq regular-session holiday tables previously lived only in
``webui/utils/market_hours.py``; Phase C screening needs the same calendar
inside ``tradingagents`` (session selection and bar-window integrity), so
the tables moved here and the WebUI module re-imports them. The lists are
plain UTC-date strings and stay synchronized with Alpaca's published
market holidays. Half days (early close) are still full sessions for
daily-bar purposes.
"""

from __future__ import annotations

from datetime import date, timedelta

US_MARKET_HOLIDAYS_2024 = [
    "2024-01-01",  # New Year's Day
    "2024-01-15",  # Martin Luther King Jr. Day
    "2024-02-19",  # Presidents' Day
    "2024-03-29",  # Good Friday
    "2024-05-27",  # Memorial Day
    "2024-06-19",  # Juneteenth
    "2024-07-04",  # Independence Day
    "2024-09-02",  # Labor Day
    "2024-11-28",  # Thanksgiving Day
    "2024-12-25",  # Christmas Day
]

US_MARKET_HOLIDAYS_2025 = [
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # Martin Luther King Jr. Day
    "2025-02-17",  # Presidents' Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving Day
    "2025-12-25",  # Christmas Day
]

US_MARKET_HOLIDAYS_2026 = [
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day observed
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
]

US_MARKET_HOLIDAYS_2027 = [
    "2027-01-01",  # New Year's Day
    "2027-01-18",  # Martin Luther King Jr. Day
    "2027-02-15",  # Presidents' Day
    "2027-03-26",  # Good Friday
    "2027-05-31",  # Memorial Day
    "2027-06-18",  # Juneteenth observed
    "2027-07-05",  # Independence Day observed
    "2027-09-06",  # Labor Day
    "2027-11-25",  # Thanksgiving Day
    "2027-12-24",  # Christmas Day observed
]

ALL_US_MARKET_HOLIDAYS = frozenset(
    US_MARKET_HOLIDAYS_2024
    + US_MARKET_HOLIDAYS_2025
    + US_MARKET_HOLIDAYS_2026
    + US_MARKET_HOLIDAYS_2027
)


def is_us_trading_day(day: date) -> bool:
    """True for a regular US equity session (Mon-Fri, not a listed holiday)."""
    if day.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return day.isoformat() not in ALL_US_MARKET_HOLIDAYS


def previous_trading_day(day: date) -> date:
    """The last regular session strictly before ``day``."""
    candidate = day - timedelta(days=1)
    while not is_us_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def session_dates_ending_at(last_session: date, count: int) -> list:
    """``count`` consecutive session dates ending at (and including)
    ``last_session``, walking backwards over weekends and holidays."""
    sessions = []
    cursor = last_session
    while len(sessions) < count:
        if is_us_trading_day(cursor):
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    sessions.reverse()
    return sessions
