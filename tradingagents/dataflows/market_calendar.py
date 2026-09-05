"""US equity trading-day calendar (single shared source).

LEGACY STATIC TABLES (2024-2027) ARE NOT PRODUCTION AUTHORITY.
They are retained only for offline unit-test fixtures and legacy display.
Production Phase C screening, session resolution, 61-session validation,
selection-cache date checks, the execution entry gate, and WebUI scheduling
MUST use the Alpaca Trading Calendar API via ``fetch_trading_calendar`` and
the ``*_auth`` helpers below. Those helpers fail closed (raise
``CalendarError``) when the authoritative calendar cannot be obtained or
cannot prove the required range — they never fall back to the static tables.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz

EASTERN = pytz.timezone("US/Eastern")

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
    """LEGACY static check (Mon-Fri minus 2024-2027 table).

    NOT production authority. Retained for offline unit-test fixtures and
    legacy display only. Production MUST use :func:`is_us_trading_day_auth`.
    """
    if day.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return day.isoformat() not in ALL_US_MARKET_HOLIDAYS


def previous_trading_day(day: date) -> date:
    """LEGACY static walk. NOT production authority; see :func:`previous_trading_day_auth`."""
    candidate = day - timedelta(days=1)
    while not is_us_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def session_dates_ending_at(last_session: date, count: int) -> list:
    """LEGACY static window. NOT production authority; see :func:`session_dates_ending_at_auth`."""
    sessions = []
    cursor = last_session
    while len(sessions) < count:
        if is_us_trading_day(cursor):
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    sessions.reverse()
    return sessions


# ---------------------------------------------------------------------------
# Authoritative Alpaca calendar adapter (production authority).
# ---------------------------------------------------------------------------


class CalendarError(RuntimeError):
    """Authoritative calendar unavailable or cannot prove the required range."""


_CALENDAR_CACHE: Dict[Tuple[str, str], List[Any]] = {}
_CALENDAR_CACHE_ORDER: List[Tuple[str, str]] = []
MAX_CALENDAR_CACHE_ENTRIES = 8


def clear_calendar_cache() -> None:
    """Empty the bounded in-process calendar cache (tests only)."""
    _CALENDAR_CACHE.clear()
    _CALENDAR_CACHE_ORDER.clear()


def _resolve_calendar_client(client: Any = None) -> Any:
    if client is not None:
        return client
    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

    return get_alpaca_trading_client()


def fetch_trading_calendar(start: date, end: date, client: Any = None) -> List[Any]:
    """Fetch authoritative Alpaca calendar rows for [start, end] (inclusive).

    Uses ``TradingClient.get_calendar(GetCalendarRequest(...))``. Successful
    rows are cached in a small bounded in-process cache. Any failure raises
    :class:`CalendarError` — callers must fail closed and MUST NOT fall back
    to the static 2024-2027 tables.
    """
    if start > end:
        raise CalendarError(f"invalid calendar range {start} > {end}")
    key = (start.isoformat(), end.isoformat())
    if key in _CALENDAR_CACHE:
        return list(_CALENDAR_CACHE[key])
    try:
        from alpaca.trading.requests import GetCalendarRequest

        trading_client = _resolve_calendar_client(client)
        request = GetCalendarRequest(start=start, end=end)
        raw = trading_client.get_calendar(request)
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError(f"Alpaca trading calendar unavailable [{start}..{end}]: {exc}") from exc
    try:
        rows = list(raw) if raw is not None else []
    except TypeError as exc:
        raise CalendarError(f"Alpaca calendar returned non-iterable: {exc}") from exc
    # Cache successful fetches only (even empty ranges are cached: an empty
    # range that was successfully proven is a valid fact, e.g. a holiday week
    # with no sessions — callers validate sufficiency themselves).
    _CALENDAR_CACHE[key] = list(rows)
    _CALENDAR_CACHE_ORDER.append(key)
    while len(_CALENDAR_CACHE_ORDER) > MAX_CALENDAR_CACHE_ENTRIES:
        oldest = _CALENDAR_CACHE_ORDER.pop(0)
        _CALENDAR_CACHE.pop(oldest, None)
    return list(rows)


def _coerce_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _row_date(row: Any) -> Optional[date]:
    if isinstance(row, dict):
        return _coerce_date(row.get("date"))
    return _coerce_date(getattr(row, "date", None))


def _row_close_et(row: Any) -> Optional[time]:
    if isinstance(row, dict):
        raw = row.get("close")
    else:
        raw = getattr(row, "close", None)
    return _coerce_et_time(raw)


def _row_open_et(row: Any) -> Optional[time]:
    if isinstance(row, dict):
        raw = row.get("open")
    else:
        raw = getattr(row, "open", None)
    return _coerce_et_time(raw)


def _coerce_et_time(raw: Any) -> Optional[time]:
    if raw is None:
        return None
    if isinstance(raw, time):
        return raw.replace(tzinfo=None)
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = EASTERN.localize(dt)
        else:
            dt = dt.astimezone(EASTERN)
        return dt.timetz().replace(tzinfo=None)
    text = str(raw).strip()
    # Accept "HH:MM", "HH:MM:SS", or full datetimes containing a time part.
    try:
        # Plain HH:MM[:SS] possibly with trailing timezone like "-05:00".
        core = text.split("T")[-1].split(" ")[-1]
        # Strip trailing timezone offsets.
        for sep in ("+", "-"):
            if sep in core[5:]:
                core = core[: core.rfind(sep)] if core.rfind(sep) > 4 else core
                break
        core = core.rstrip("Z").strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                candidate = core[:8] if fmt == "%H:%M:%S" and len(core) >= 8 else core[:5]
                return datetime.strptime(candidate, fmt).time()
            except ValueError:
                continue
    except Exception:
        return None
    return None


def _calendar_date_set(rows: List[Any]) -> Set[date]:
    out: Set[date] = set()
    for row in rows:
        day = _row_date(row)
        if day is not None:
            out.add(day)
    return out


def _calendar_close_map(rows: List[Any]) -> Dict[date, time]:
    out: Dict[date, time] = {}
    for row in rows:
        day = _row_date(row)
        close = _row_close_et(row)
        if day is not None and close is not None:
            out[day] = close
    return out


def _eastern_now_auth(now: Any = None) -> datetime:
    if now is None:
        return datetime.now(EASTERN)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return EASTERN.localize(now)
        return now.astimezone(EASTERN)
    raise CalendarError(f"invalid instant for calendar resolution: {now!r}")


def is_us_trading_day_auth(
    day: date, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> bool:
    """Authoritative trading-day check from Alpaca calendar rows.

    When ``calendar_rows`` is given it is used directly (tests inject
    deterministic rows). Otherwise a covering window is fetched. Raises
    :class:`CalendarError` when the range cannot be proven — never falls back
    to the static table.
    """
    if calendar_rows is not None:
        proven = _calendar_date_set(calendar_rows)
        if not proven:
            raise CalendarError(f"calendar rows cannot prove {day}: empty set")
        return day in proven
    rows = fetch_trading_calendar(day - timedelta(days=14), day + timedelta(days=14), client=client)
    proven = _calendar_date_set(rows)
    if not proven:
        raise CalendarError(f"calendar cannot prove trading-day status of {day}: empty range")
    return day in proven


def previous_trading_day_auth(
    day: date, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> date:
    """Last authoritative session strictly before ``day``."""
    if calendar_rows is not None:
        proven = sorted(_calendar_date_set(calendar_rows))
        earlier = [d for d in proven if d < day]
        if not earlier:
            raise CalendarError(f"calendar rows cannot prove previous session before {day}")
        return earlier[-1]
    rows = fetch_trading_calendar(day - timedelta(days=45), day - timedelta(days=1), client=client)
    proven = sorted(d for d in _calendar_date_set(rows) if d < day)
    if not proven:
        raise CalendarError(f"calendar cannot prove previous session before {day}")
    return proven[-1]


def session_dates_ending_at_auth(
    last_session: date,
    count: int,
    client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> List[date]:
    """``count`` authoritative sessions ending at ``last_session`` (inclusive)."""
    if count <= 0:
        raise CalendarError(f"invalid session count: {count}")
    if calendar_rows is not None:
        proven = sorted(_calendar_date_set(calendar_rows))
        if last_session not in proven:
            raise CalendarError(f"calendar rows do not contain session {last_session}")
        idx = proven.index(last_session)
        window = proven[idx - count + 1 : idx + 1] if idx - count + 1 >= 0 else []
        if len(window) != count:
            raise CalendarError(
                f"calendar rows prove only {len(window)} sessions ending at {last_session}; {count} required"
            )
        return window
    span_days = int(count * 3 + 45)
    rows = fetch_trading_calendar(last_session - timedelta(days=span_days), last_session, client=client)
    proven = sorted(_calendar_date_set(rows))
    if last_session not in proven:
        raise CalendarError(f"calendar does not list {last_session} as a session")
    idx = proven.index(last_session)
    window = proven[idx - count + 1 : idx + 1] if idx - count + 1 >= 0 else []
    if len(window) != count:
        raise CalendarError(
            f"calendar proves only {len(window)} sessions ending at {last_session}; {count} required"
        )
    return window


def session_close_et_auth(
    day: date, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> time:
    """Actual ET close time for an authoritative session (early closes honored)."""
    if calendar_rows is not None:
        close_map = _calendar_close_map(calendar_rows)
        if day not in close_map:
            raise CalendarError(f"calendar rows have no close time for {day}")
        return close_map[day]
    rows = fetch_trading_calendar(day - timedelta(days=7), day + timedelta(days=1), client=client)
    close_map = _calendar_close_map(rows)
    if day not in close_map:
        raise CalendarError(f"calendar has no close time for {day}")
    return close_map[day]


def most_recent_completed_session_auth(
    now: Any = None, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> date:
    """Latest authoritative session whose actual ET close has passed.

    Normal sessions complete at their calendar close (usually 16:00 ET);
    early-close sessions (e.g. 13:00 ET) complete at 13:00 ET. Weekends and
    holidays walk back to the last authoritative session. Raises
    :class:`CalendarError` when the calendar cannot prove the answer.
    """
    eastern = _eastern_now_auth(now)
    today = eastern.date()
    now_t = eastern.timetz().replace(tzinfo=None)
    if calendar_rows is not None:
        proven = _calendar_date_set(calendar_rows)
        close_map = _calendar_close_map(calendar_rows)
        if not proven:
            raise CalendarError("calendar rows cannot prove completed session: empty set")
        if today in proven:
            close_t = close_map.get(today)
            if close_t is None:
                raise CalendarError(f"calendar rows have no close time for {today}")
            if now_t >= close_t:
                return today
        earlier = sorted(d for d in proven if d < today)
        if not earlier:
            # Today itself may be the only proven session but not yet closed:
            # that is an incomplete range, not a completed session.
            raise CalendarError(f"calendar rows cannot prove a completed session as of {eastern.isoformat()}")
        return earlier[-1]
    rows = fetch_trading_calendar(today - timedelta(days=14), today, client=client)
    proven = _calendar_date_set(rows)
    close_map = _calendar_close_map(rows)
    if not proven:
        raise CalendarError("calendar cannot prove completed session: empty range")
    if today in proven:
        close_t = close_map.get(today)
        if close_t is None:
            raise CalendarError(f"calendar has no close time for {today}")
        if now_t >= close_t:
            return today
    earlier = sorted(d for d in proven if d < today)
    if not earlier:
        raise CalendarError(f"calendar cannot prove a completed session as of {eastern.isoformat()}")
    return earlier[-1]


def current_trading_date_auth(
    now: Any = None, client: Any = None, calendar_rows: Optional[List[Any]] = None
) -> date:
    """Authoritative trading date: today when it is a session, else the last session."""
    eastern = _eastern_now_auth(now)
    today = eastern.date()
    if calendar_rows is not None:
        proven = _calendar_date_set(calendar_rows)
        if not proven:
            raise CalendarError("calendar rows cannot prove trading date: empty set")
        if today in proven:
            return today
        earlier = sorted(d for d in proven if d < today)
        if not earlier:
            raise CalendarError(f"calendar rows cannot prove trading date for {eastern.isoformat()}")
        return earlier[-1]
    rows = fetch_trading_calendar(today - timedelta(days=14), today, client=client)
    proven = _calendar_date_set(rows)
    if not proven:
        raise CalendarError("calendar cannot prove trading date: empty range")
    if today in proven:
        return today
    earlier = sorted(d for d in proven if d < today)
    if not earlier:
        raise CalendarError(f"calendar cannot prove trading date for {eastern.isoformat()}")
    return earlier[-1]
