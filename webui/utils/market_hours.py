"""
Market hours utilities for validating trading hours and checking if the market is open.

Production session authority is the Alpaca Trading Calendar API via
``tradingagents.dataflows.market_calendar`` (shared with Phase C screening).
The 2024-2027 static holiday lists below are LEGACY display data only and are
NOT used as production authority; ``is_market_open`` / ``get_next_market_datetime``
derive trading days and actual closes (including early closes) from the
authoritative calendar and fail closed when it cannot be proven.
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz

# LEGACY static tables — NOT production authority (see module docstring).
from tradingagents.dataflows.market_calendar import (  # noqa: F401
    ALL_US_MARKET_HOLIDAYS as _ALL_US_MARKET_HOLIDAYS,
    US_MARKET_HOLIDAYS_2024,
    US_MARKET_HOLIDAYS_2025,
    US_MARKET_HOLIDAYS_2026,
    US_MARKET_HOLIDAYS_2027,
)

# Market regular hours (EST/EDT)
MARKET_OPEN_HOUR = 9   # 9:30 AM (use 9 for conservative approach)
MARKET_CLOSE_HOUR = 16  # 4:00 PM

# Whole-hour scheduling slots must be strictly after the 09:30 open and at
# or before the 16:00 close, so the executable whole-hour window is 10-16
# (16 runs at the close instant on full sessions).
EXECUTABLE_FIRST_HOUR = 10
EXECUTABLE_LAST_HOUR = 16


class MarketScheduleError(RuntimeError):
    """Raised when no executable market slot can be proven (fail closed)."""


def _get_eastern_timezone():
    return pytz.timezone("US/Eastern")


def _current_eastern_time() -> datetime.datetime:
    return datetime.datetime.now(pytz.utc).astimezone(_get_eastern_timezone())


def _coerce_to_eastern(target_datetime: datetime.datetime = None) -> datetime.datetime:
    eastern = _get_eastern_timezone()
    if target_datetime is None:
        return _current_eastern_time()
    if target_datetime.tzinfo is None:
        # Naive datetimes passed into this module are treated as Eastern wall clock time.
        return eastern.localize(target_datetime)
    return target_datetime.astimezone(eastern)

def validate_market_hours(hours_str: str) -> Tuple[bool, List[int], str]:
    """
    Validate market hours input string.

    Args:
        hours_str: String like "11" or "11,13" representing hours

    Returns:
        Tuple of (is_valid, parsed_hours_list, error_message)
    """
    if not hours_str or not hours_str.strip():
        return False, [], "Please enter at least one trading hour"

    try:
        # Parse comma-separated hours
        hours_parts = [h.strip() for h in hours_str.split(',') if h.strip()]
        if not hours_parts:
            return False, [], "Please enter at least one trading hour"

        hours = []
        for hour_str in hours_parts:
            hour = int(hour_str)
            if hour < MARKET_OPEN_HOUR or hour > MARKET_CLOSE_HOUR:
                return False, [], f"Hour {hour} is outside market hours ({MARKET_OPEN_HOUR}AM-{MARKET_CLOSE_HOUR}PM EST/EDT)"
            # U08: hour 9 can never execute — the authoritative open gate is
            # 09:30 and the scheduler builds whole-hour slots. Reject it up
            # front instead of scheduling a wait that never fires.
            if hour < EXECUTABLE_FIRST_HOUR:
                return False, [], (
                    f"Hour {hour} can never execute: the market opens at 9:30 AM, "
                    f"so whole-hour slots start at {EXECUTABLE_FIRST_HOUR} AM EST/EDT"
                )
            hours.append(hour)

        # Remove duplicates and sort
        hours = sorted(list(set(hours)))
        return True, hours, ""

    except ValueError:
        return False, [], "Please enter valid hour numbers (e.g., 11,13)"

def is_market_open(
    target_datetime: datetime.datetime = None,
    calendar_client: Any = None,
    calendar_rows: Optional[list] = None,
) -> Tuple[bool, str]:
    """
    Check if the US stock market is open at the given datetime.

    Production authority is the Alpaca trading calendar (trading days and
    actual close times, including early closes). Calendar failures fail
    closed (reported as closed, never silently falling back to the static
    2024-2027 table).

    Args:
        target_datetime: Datetime to check (defaults to current time)
        calendar_client: injected Alpaca trading client (tests)
        calendar_rows: injected authoritative calendar rows (tests)

    Returns:
        Tuple of (is_open, reason_if_closed)
    """
    from tradingagents.dataflows.market_calendar import (
        CalendarError,
        is_us_trading_day_auth,
        session_close_et_auth,
    )

    target_datetime = _coerce_to_eastern(target_datetime)

    # Authoritative trading-day check (weekends/holidays/2028+/exceptional).
    try:
        trading_day = is_us_trading_day_auth(
            target_datetime.date(), client=calendar_client, calendar_rows=calendar_rows
        )
    except CalendarError as exc:
        return False, f"Market calendar unavailable; treating as closed ({exc})"
    if not trading_day:
        if target_datetime.weekday() >= 5:
            return False, "Market is closed on weekends"
        return False, f"Market is closed for holiday on {target_datetime.strftime('%Y-%m-%d')}"

    # Intraday window uses the authoritative close (early closes honored).
    try:
        close_t = session_close_et_auth(
            target_datetime.date(), client=calendar_client, calendar_rows=calendar_rows
        )
    except CalendarError as exc:
        return False, f"Market close unavailable; treating as closed ({exc})"
    market_open = target_datetime.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = target_datetime.replace(
        hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0
    )

    if target_datetime < market_open:
        return False, f"Market opens at 9:30 AM EST/EDT (currently {target_datetime.strftime('%I:%M %p %Z')})"
    elif target_datetime > market_close:
        return False, f"Market closed at {close_t.strftime('%I:%M %p')} EST/EDT (currently {target_datetime.strftime('%I:%M %p %Z')})"

    return True, "Market is open"

def get_next_market_datetime(
    target_hour: int,
    from_datetime: datetime.datetime = None,
    calendar_client: Any = None,
    calendar_rows: Optional[list] = None,
) -> datetime.datetime:
    """
    Get the next market datetime for the specified hour.

    Args:
        target_hour: Hour to target (e.g., 11 for 11 AM)
        from_datetime: Starting datetime (defaults to current time)
        calendar_client: injected Alpaca trading client (tests)
        calendar_rows: injected authoritative calendar rows (tests)

    Returns:
        Next datetime when market will be open at the target hour

    Raises:
        MarketScheduleError: when no executable slot can be proven (U09:
        all-unavailable calendars and permanently closed slots fail closed
        instead of returning a guessed date).
    """
    from tradingagents.dataflows.market_calendar import CalendarError

    if not EXECUTABLE_FIRST_HOUR <= target_hour <= EXECUTABLE_LAST_HOUR:
        raise MarketScheduleError(
            f"hour {target_hour} cannot execute: whole-hour slots must be "
            f"{EXECUTABLE_FIRST_HOUR}-{EXECUTABLE_LAST_HOUR} (market opens 09:30 ET)"
        )

    from_datetime = _coerce_to_eastern(from_datetime)

    # U10: rebuild the Eastern wall time on each candidate date. Adding a
    # timedelta to an aware pytz datetime keeps the old UTC offset across a
    # DST transition, which shifts the scheduled wall time by an hour.
    eastern = _get_eastern_timezone()

    max_attempts = 15  # Bounded search; exhausting it fails closed below.
    attempts = 0

    # Start with today at the target hour
    target_dt = from_datetime.replace(hour=target_hour, minute=0, second=0, microsecond=0)

    # If the target time today has already passed, start with tomorrow
    if target_dt <= from_datetime:
        target_dt += datetime.timedelta(days=1)

    while attempts < max_attempts:
        # Rebuild the wall clock for the candidate date so the UTC offset
        # matches that date's DST status.
        target_dt = eastern.localize(
            datetime.datetime(
                target_dt.year, target_dt.month, target_dt.day,
                target_hour, 0, 0,
            )
        )
        try:
            is_open, reason = is_market_open(
                target_dt, calendar_client=calendar_client, calendar_rows=calendar_rows
            )
        except CalendarError as exc:
            # U09: an unusable calendar proves nothing — fail closed instead
            # of returning an unvalidated date.
            raise MarketScheduleError(
                f"cannot prove a market slot for hour {target_hour}: {exc}"
            ) from exc
        if is_open:
            return target_dt

        # Move to next day
        target_dt += datetime.timedelta(days=1)
        attempts += 1

    # U09: no valid slot within the bounded window — fail closed, never
    # guess a date the calendar could not validate.
    raise MarketScheduleError(
        f"no executable market slot for hour {target_hour} within {max_attempts} days "
        "of calendar-verified search"
    )

def format_market_hours_info(hours: List[int]) -> Dict[str, Any]:
    """
    Format market hours information for display.

    Args:
        hours: List of hours (e.g., [11, 13])

    Returns:
        Dictionary with formatted information
    """
    if not hours:
        return {"error": "No hours provided"}

    # Format hours for display
    formatted_hours = []
    for hour in sorted(hours):
        if hour == 0:
            formatted_hours.append("12:00 AM")
        elif hour < 12:
            formatted_hours.append(f"{hour}:00 AM")
        elif hour == 12:
            formatted_hours.append("12:00 PM")
        else:
            formatted_hours.append(f"{hour-12}:00 PM")

    hours_str = " and ".join(formatted_hours)

    # Calculate next execution times
    next_executions = []
    for hour in hours:
        next_dt = get_next_market_datetime(hour)
        next_executions.append({
            "hour": hour,
            "formatted_hour": formatted_hours[hours.index(hour)],
            "next_datetime": next_dt,
            "next_formatted": next_dt.strftime("%A, %B %d at %I:%M %p %Z")
        })

    return {
        "hours": hours,
        "formatted_hours": hours_str,
        "next_executions": next_executions,
        "market_timezone": "US/Eastern"
    }
