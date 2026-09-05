"""Phase C session resolution for the full-market screen.

All session math is anchored to US/Eastern wall clock (pytz handles DST).
Two distinct notions exist and must never be conflated:

- ``as_of``: the most recent **completed** authoritative session. A session is
  completed only after its actual Alpaca calendar close (usually 16:00 ET,
  early closes e.g. 13:00 ET honored). Today's partially-formed daily bar is
  never part of ``as_of``.
- ``trading_date``: the trading day the selection is valid for. On a
  trading day this is today (pre-market, intraday and post-close alike —
  one scan per trading day); on a weekend/holiday it falls back to the
  most recent completed session for cache-read purposes only. New scans
  and new entries are refused on non-trading days.

LEGACY static helpers (``most_recent_completed_session``,
``current_trading_date`` with fixed 16:00 ET) remain for offline unit-test
fixtures only. Production MUST use the ``*_auth`` variants below, which
derive sessions from the Alpaca Trading Calendar API and fail closed when
the calendar cannot be proven.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, List, Optional

import pytz

from tradingagents.dataflows.market_calendar import (
    CalendarError,
    current_trading_date_auth,
    is_us_trading_day,
    is_us_trading_day_auth,
    most_recent_completed_session_auth,
    previous_trading_day,
    previous_trading_day_auth,
)

EASTERN = pytz.timezone("US/Eastern")
# LEGACY fixed close — NOT production authority (early closes honored via
# the Alpaca calendar in the *_auth helpers). Retained for offline fixtures.
SESSION_CLOSE_ET = time(16, 0)


def eastern_now(now: Optional[datetime] = None) -> datetime:
    """Current (or given) instant projected into US/Eastern wall clock."""
    if now is None:
        return datetime.now(EASTERN)
    if now.tzinfo is None:
        return EASTERN.localize(now)
    return now.astimezone(EASTERN)


def most_recent_completed_session(now: Optional[datetime] = None) -> date:
    """LEGACY fixed-16:00 resolution. NOT production authority; see ``*_auth``."""
    eastern = eastern_now(now)
    if is_us_trading_day(eastern.date()) and eastern.time().replace(tzinfo=None) >= SESSION_CLOSE_ET:
        return eastern.date()
    return previous_trading_day(eastern.date())


def current_trading_date(now: Optional[datetime] = None) -> date:
    """LEGACY static trading date. NOT production authority; see ``*_auth``."""
    eastern = eastern_now(now)
    if is_us_trading_day(eastern.date()):
        return eastern.date()
    return previous_trading_day(eastern.date())


def most_recent_completed_session_production(
    now: Optional[datetime] = None,
    client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> date:
    """Authoritative completed session from the Alpaca calendar (production)."""
    return most_recent_completed_session_auth(now, client=client, calendar_rows=calendar_rows)


def current_trading_date_production(
    now: Optional[datetime] = None,
    client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> date:
    """Authoritative trading date from the Alpaca calendar (production)."""
    return current_trading_date_auth(now, client=client, calendar_rows=calendar_rows)


def is_us_trading_day_production(
    day: date, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> bool:
    """Authoritative trading-day check (production, fail-closed)."""
    return is_us_trading_day_auth(day, client=client, calendar_rows=calendar_rows)


def is_fresh_utc_timestamp(text: str, *, now: Optional[datetime] = None) -> bool:
    """False for missing/unparseable/future timestamps (cache integrity)."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return parsed <= reference
