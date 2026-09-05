"""
Market hours utilities for validating trading hours and checking if the market is open.
"""

import datetime
import pytz
from typing import List, Tuple, Dict, Any

# US market holidays moved to the shared calendar module so tradingagents
# (Phase C screening) and the WebUI stay on one source of truth.
from tradingagents.dataflows.market_calendar import (  # noqa: F401
    US_MARKET_HOLIDAYS_2024,
    US_MARKET_HOLIDAYS_2025,
    US_MARKET_HOLIDAYS_2026,
    US_MARKET_HOLIDAYS_2027,
    ALL_US_MARKET_HOLIDAYS as _ALL_US_MARKET_HOLIDAYS,
)

# Market regular hours (EST/EDT)
MARKET_OPEN_HOUR = 9   # 9:30 AM (use 9 for conservative approach)
MARKET_CLOSE_HOUR = 16  # 4:00 PM


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
            hours.append(hour)

        # Remove duplicates and sort
        hours = sorted(list(set(hours)))
        return True, hours, ""

    except ValueError:
        return False, [], "Please enter valid hour numbers (e.g., 11,13)"

def is_market_open(target_datetime: datetime.datetime = None) -> Tuple[bool, str]:
    """
    Check if the US stock market is open at the given datetime.

    Args:
        target_datetime: Datetime to check (defaults to current time)

    Returns:
        Tuple of (is_open, reason_if_closed)
    """
    target_datetime = _coerce_to_eastern(target_datetime)

    # Check if it's a weekend
    if target_datetime.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False, "Market is closed on weekends"

    # Check if it's a holiday
    date_str = target_datetime.strftime("%Y-%m-%d")
    if date_str in _ALL_US_MARKET_HOLIDAYS:
        return False, f"Market is closed for holiday on {date_str}"

    # Check if it's within market hours (9:30 AM - 4:00 PM EST/EDT)
    market_open = target_datetime.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = target_datetime.replace(hour=16, minute=0, second=0, microsecond=0)

    if target_datetime < market_open:
        return False, f"Market opens at 9:30 AM EST/EDT (currently {target_datetime.strftime('%I:%M %p %Z')})"
    elif target_datetime > market_close:
        return False, f"Market closed at 4:00 PM EST/EDT (currently {target_datetime.strftime('%I:%M %p %Z')})"

    return True, "Market is open"

def get_next_market_datetime(target_hour: int, from_datetime: datetime.datetime = None) -> datetime.datetime:
    """
    Get the next market datetime for the specified hour.

    Args:
        target_hour: Hour to target (e.g., 11 for 11 AM)
        from_datetime: Starting datetime (defaults to current time)

    Returns:
        Next datetime when market will be open at the target hour
    """
    from_datetime = _coerce_to_eastern(from_datetime)

    # Start with today at the target hour
    target_dt = from_datetime.replace(hour=target_hour, minute=0, second=0, microsecond=0)

    # If the target time today has already passed, start with tomorrow
    if target_dt <= from_datetime:
        target_dt += datetime.timedelta(days=1)

    # Keep advancing until we find a valid market day
    max_attempts = 10  # Prevent infinite loops
    attempts = 0

    while attempts < max_attempts:
        is_open, reason = is_market_open(target_dt)
        if is_open:
            return target_dt

        # Move to next day
        target_dt += datetime.timedelta(days=1)
        attempts += 1

    # Fallback - return the target datetime even if we couldn't validate
    return target_dt

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
