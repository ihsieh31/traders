"""Phase C session resolution for the full-market screen.

All session math is anchored to US/Eastern wall clock (pytz handles DST).
Two distinct notions exist and must never be conflated:

- ``as_of``: the most recent **completed** regular session. A session is
  completed only after 16:00 ET on one of its trading days. Today's
  partially-formed daily bar is never part of ``as_of``.
- ``trading_date``: the trading day the selection is valid for. On a
  trading day this is today (pre-market, intraday and post-close alike —
  one scan per trading day); on a weekend/holiday it falls back to the
  most recent completed session for cache-read purposes only. New scans
  and new entries are refused on non-trading days.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

import pytz

from tradingagents.dataflows.market_calendar import (
    is_us_trading_day,
    previous_trading_day,
)

EASTERN = pytz.timezone("US/Eastern")
# 16:00 ET regular close; early closes are still full daily-bar sessions.
SESSION_CLOSE_ET = time(16, 0)


def eastern_now(now: Optional[datetime] = None) -> datetime:
    """Current (or given) instant projected into US/Eastern wall clock."""
    if now is None:
        return datetime.now(EASTERN)
    if now.tzinfo is None:
        return EASTERN.localize(now)
    return now.astimezone(EASTERN)


def most_recent_completed_session(now: Optional[datetime] = None) -> date:
    """The latest regular session whose 16:00 ET close has passed.

    Intraday on a trading day this is the previous session; after the
    close it is today. Weekends and holidays walk back to the last
    trading day.
    """
    eastern = eastern_now(now)
    if is_us_trading_day(eastern.date()) and eastern.time() >= SESSION_CLOSE_ET:
        return eastern.date()
    return previous_trading_day(eastern.date())


def current_trading_date(now: Optional[datetime] = None) -> date:
    """The trading day a selection made right now belongs to.

    On a trading day this is today even pre-market (one scan per trading
    day). On a non-trading day it falls back to the last completed
    session — callers must independently refuse scans and new entries on
    non-trading days.
    """
    eastern = eastern_now(now)
    if is_us_trading_day(eastern.date()):
        return eastern.date()
    return previous_trading_day(eastern.date())


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
