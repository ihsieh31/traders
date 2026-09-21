"""Authoritative session scheduling and missed-session bookkeeping.

Public long_run wrappers supply named collaborators at call time. Calendar
imports remain lazy, and scheduler retries retain the injected sleep owner.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def eastern_now(now: Any = None) -> datetime:
    from tradingagents.screening.sessions import eastern_now as _eastern_now

    return _eastern_now(now)


def effective_target_for_session(
    session_day: date,
    run_time_et: str,
    *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    parse_run_time_et: Callable[[str], dtime],
    datetime: Any,
) -> Dict[str, Any]:
    """Configured target vs effective target for one authoritative session."""
    from tradingagents.dataflows.market_calendar import session_close_et_auth

    target = parse_run_time_et(run_time_et)
    close = session_close_et_auth(
        session_day, client=calendar_client, calendar_rows=calendar_rows
    )
    configured = datetime.combine(session_day, target)
    if target >= close:
        close_dt = datetime.combine(session_day, close)
        effective = close_dt - timedelta(minutes=30)
        adjustment = "EARLY_CLOSE"
    else:
        effective = configured
        adjustment = "NONE"
    return {
        "session_date": session_day.isoformat(),
        "configured_target": configured.strftime("%H:%M"),
        "effective_target": effective.strftime("%H:%M"),
        "authoritative_close": close.strftime("%H:%M"),
        "schedule_adjustment": adjustment,
    }


def fetch_session_dates(
    start: date,
    end: date,
    client: Any = None,
    *,
    date: Any,
) -> List[date]:
    """Authoritative trading sessions in [start, end]; fail-closed on error."""
    from tradingagents.dataflows.market_calendar import (
        CalendarError, fetch_trading_calendar,
    )

    if start > end:
        return []
    rows = fetch_trading_calendar(start, end, client=client)
    days = set()
    for row in rows:
        value = row.get("date") if isinstance(row, dict) else getattr(row, "date", None)
        try:
            day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            raise CalendarError(f"calendar row has no usable date: {row!r}")
        days.add(day)
    return sorted(days)


def next_due_session(
    *,
    now: Any,
    run_time_et: str,
    started_at: datetime,
    ends_at: datetime,
    settled: List[str],
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    eastern_now: Callable[[Any], datetime],
    effective_target_for_session: Callable[..., Dict[str, Any]],
    datetime: Any,
) -> Optional[Dict[str, Any]]:
    """Next session to run: earliest unsettled trading session due now or future.

    ``settled`` lists session dates that are terminal (COMPLETED, MISSED, or
    STOPPED round journals); they are never scheduled again, so a missed
    session stays missed instead of being analyzed late. Returns None when
    nothing remains before ends_at. Raises CalendarError when the calendar
    cannot prove the answer (caller hard-stops).
    """
    from tradingagents.dataflows.market_calendar import is_us_trading_day_auth

    eastern = eastern_now(now)
    done = set(settled or [])
    start_day = started_at.astimezone(eastern.tzinfo).date()
    end_day = ends_at.astimezone(eastern.tzinfo).date()
    cursor = min(eastern.date(), end_day)
    # Walk from the observation start through the earlier of today/ends_at to
    # find overdue incomplete sessions first (resume-before-advance).
    day = start_day
    candidates: List[date] = []
    while day <= eastern.date() and day < end_day:
        trading = is_us_trading_day_auth(
            day, client=calendar_client, calendar_rows=calendar_rows
        )
        if trading and day.isoformat() not in done:
            candidates.append(day)
        day += timedelta(days=1)
    for session_day in candidates:
        info = effective_target_for_session(
            session_day, run_time_et,
            calendar_client=calendar_client, calendar_rows=calendar_rows,
        )
        naive = datetime.strptime(
            f"{info['session_date']} {info['effective_target']}", "%Y-%m-%d %H:%M"
        )
        effective = eastern.tzinfo.localize(naive)
        if session_day < eastern.date() or effective <= eastern:
            info["due"] = True
            info["effective_at"] = effective.isoformat()
            return info
    # Nothing overdue: find the next future session inside the window.
    day = max(eastern.date(), start_day)
    while day < end_day:
        if is_us_trading_day_auth(day, client=calendar_client, calendar_rows=calendar_rows):
            if day.isoformat() not in done:
                info = effective_target_for_session(
                    day, run_time_et,
                    calendar_client=calendar_client, calendar_rows=calendar_rows,
                )
                naive = datetime.strptime(
                    f"{info['session_date']} {info['effective_target']}", "%Y-%m-%d %H:%M"
                )
                effective = eastern.tzinfo.localize(naive)
                if effective > eastern:
                    info["due"] = False
                    info["effective_at"] = effective.isoformat()
                    return info
                info["due"] = True
                info["effective_at"] = effective.isoformat()
                return info
        day += timedelta(days=1)
    return None


def session_close_et(
    session_date: str,
    *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    date: Any,
) -> dtime:
    """Authoritative regular-session close (ET) for one session date."""
    from tradingagents.dataflows.market_calendar import session_close_et_auth

    return session_close_et_auth(
        date.fromisoformat(session_date),
        client=calendar_client, calendar_rows=calendar_rows,
    )


def mark_session_missed_after_close(
    *,
    run_id: str,
    session_date: str,
    now: datetime,
    run_time_et: str,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
    load_round_journal: Callable[[str, str], Optional[Dict[str, Any]]],
    round_path: Callable[[str, str], Path],
    LongRunStop: type[RuntimeError],
    eastern_now: Callable[[Any], datetime],
    session_close_et: Callable[..., dtime],
    TERMINAL_ROUND_STATUSES: Tuple[str, ...],
    SYMBOL_EXECUTING: str,
    new_round_journal: Callable[..., Dict[str, Any]],
    utc_now_iso: Callable[[], str],
    save_round_journal: Callable[[str, Dict[str, Any]], None],
    log_event: Callable[..., None],
    datetime: Any,
    date: Any,
) -> bool:
    """R13: settle a never-started session whose close has passed as MISSED.

    A session whose scheduled target is overdue, that has no round journal
    yet (never started), and whose authoritative regular-session close has
    already passed must never be executed as a "due round" — running it
    would analyze yesterday's data and leave a new entry for after-hours
    execution. It is settled with the existing MISSED journal/state so the
    normal settled-session rule keeps it from being replayed on later days.
    Returns True when the session was settled here.
    """
    journal = load_round_journal(run_id, session_date)
    # H-04: read_json() returns None for both a missing file and an
    # unreadable one. A journal that exists but cannot be parsed must stop
    # as STATE_CORRUPT — writing a fresh MISSED journal over it would
    # destroy the only evidence of a session that may have started.
    if journal is None and round_path(run_id, session_date).exists():
        raise LongRunStop(
            "STATE_CORRUPT",
            f"round journal for {session_date} exists but is unreadable",
        )
    eastern = eastern_now(now)
    # CalendarError propagates to the loop's bounded scheduler retry (F-03);
    # after the final attempt the observation fails closed with
    # CALENDAR_UNAVAILABLE rather than letting an unprovable close time
    # trade a stale session.
    close = session_close_et(
        session_date,
        calendar_client=calendar_client, calendar_rows=calendar_rows,
    )
    close_dt = eastern.tzinfo.localize(
        datetime.combine(date.fromisoformat(session_date), close)
    )
    if eastern < close_dt:
        return False  # still inside the regular session: keep it runnable
    if journal is not None:
        status = journal.get("status")
        if status in TERMINAL_ROUND_STATUSES:
            return False
        if status not in ("PENDING", "RUNNING"):
            raise LongRunStop(
                "STATE_CORRUPT",
                f"round journal for {session_date} has unknown status {status!r}",
            )
        if any(
            entry.get("status") == SYMBOL_EXECUTING
            for entry in (journal.get("symbols") or {}).values()
            if isinstance(entry, dict)
        ):
            return False  # a broker mutation may be unresolved; recovery owns it
        new_journal = journal
    else:
        new_journal = new_round_journal(session_date, [])
    new_journal["status"] = "MISSED"
    new_journal["stop_reason"] = "MISSED_SESSION_CLOSE"
    new_journal["finished_at"] = utc_now_iso()
    save_round_journal(run_id, new_journal)
    log_event(run_id, "round_missed",
              {"session": session_date, "reason": "MISSED_SESSION_CLOSE"})
    return True


def sweep_missed_sessions(
    *,
    run_id: str,
    expected: List[str],
    today: str,
    run_dir: Callable[[str], Path],
    read_json: Callable[[Path], Optional[Any]],
    TERMINAL_ROUND_STATUSES: Tuple[str, ...],
    utc_now_iso: Callable[[], str],
    atomic_write_json: Callable[[Path, Any], None],
    LongRunStop: type[RuntimeError],
    new_round_journal: Callable[..., Dict[str, Any]],
    log_event: Callable[..., None],
) -> List[str]:
    """Settle past expected sessions that never completed as MISSED.

    A past session counts as missed when it has no round file yet, or when
    its journal is still unfinished (PENDING/RUNNING — the process died
    mid-round and aged past its session date). Unfinished journals keep
    their partial per-symbol evidence: those analyses/executions really
    happened and the audit must show them. Corrupt round files stop the
    observation instead of being guessed through.
    """
    missed = []
    rounds_dir = run_dir(run_id) / "rounds"
    for session_day in sorted(expected or []):
        if session_day >= today:
            continue
        path = rounds_dir / f"{session_day}.json"
        data = read_json(path)
        if isinstance(data, dict):
            if data.get("status") in TERMINAL_ROUND_STATUSES:
                continue
            data["status"] = "MISSED"
            data["stop_reason"] = "MISSED_PROCESS_DOWN"
            data["finished_at"] = data.get("finished_at") or utc_now_iso()
            atomic_write_json(path, data)
        elif path.exists():
            raise LongRunStop(
                "STATE_CORRUPT",
                f"round journal {path.name} exists but is unreadable",
            )
        else:
            journal = new_round_journal(session_day, [])
            journal["status"] = "MISSED"
            journal["stop_reason"] = "MISSED_PROCESS_DOWN"
            journal["finished_at"] = utc_now_iso()
            atomic_write_json(path, journal)
        missed.append(session_day)
        log_event(run_id, "round_missed",
                  {"session": session_day, "reason": "MISSED_PROCESS_DOWN"})
    return missed


def _run_scheduler_operation_with_retry(
    operation: Callable[[], Any],
    *,
    deps: Any,
    run_id: str,
    label: str,
    SCHEDULER_RETRY_ATTEMPTS: int,
    SCHEDULER_RETRY_DELAY_SECONDS: float,
    LongRunStop: type[RuntimeError],
    log_event: Callable[..., None],
) -> Any:
    """Run one calendar-backed scheduler operation with a bounded retry.

    Only ordinary Exception is retried. LongRunStop is a deliberate
    fail-closed/terminal verdict (corrupt state, hard safety stop) and keeps
    its immediate-stop semantics. Persistent failure is re-raised as
    CALENDAR_UNAVAILABLE so the observation finalizes STOPPED with evidence
    instead of letting the exception kill the unattended process.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, SCHEDULER_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except LongRunStop:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= SCHEDULER_RETRY_ATTEMPTS:
                break
            log_event(run_id, "scheduler_retry", {
                "label": label,
                "attempt": attempt,
                "max_attempts": SCHEDULER_RETRY_ATTEMPTS,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            deps.sleep_fn(SCHEDULER_RETRY_DELAY_SECONDS)
    raise LongRunStop(
        "CALENDAR_UNAVAILABLE",
        f"{label} failed after {SCHEDULER_RETRY_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}",
    )
