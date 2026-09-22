#!/usr/bin/env python3
"""Thin unattended launcher for the existing resumable A/B campaign.

It owns waiting (across sessions and within a session) and bounded
same-session retries only.  Campaign state, calendar freezing, pair
execution, recovery/reconciliation, settlement refresh, and finalization
stay in their existing owners: every iteration is a plain --resume of the
same campaign root, so a completed session is never rerun and no second
campaign state machine exists here.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from scripts.run_analysis_ab import _load_config
from scripts.run_analysis_ab_campaign import (
    _calendar_client as _campaign_calendar_client,
)
from scripts.run_analysis_ab_campaign import _campaign_state_path, _read_state, run_campaign
from tradingagents.app_identity import default_results_dir, validate_app_path
from tradingagents.long_run import effective_target_for_session


ET = ZoneInfo("America/New_York")
RUN_TIME_ET = "11:00"
RETRY_DELAY_SECONDS = 15 * 60
MAX_SAME_DAY_RETRIES = 3
# Bounded cross-day polling: at most this many seconds per sleep while
# waiting for a future session target (acceptable 5-minute polling).
POLL_INTERVAL_SECONDS = 300.0
# market_closed is a schedule wait, not a transient failure.  After the
# session's effective execution target passes, keep polling this long
# before handing the session back to the operator.
EXECUTION_WINDOW_GRACE_SECONDS = 30 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _campaign_root(*, resume: str | Path | None, results_root: str | Path | None) -> Path:
    """Resolve the campaign root exactly like the campaign coordinator does.

    Create mode must not fall back to the process CWD: the coordinator
    defaults to ``default_results_dir() / "ab"``, so a create pass started
    without ``--results-root`` has to resume that same directory afterwards.
    Reuses the coordinator's own resolvers instead of a second scheme.
    """
    if resume is not None:
        return validate_app_path(resume, field="campaign_path")
    return validate_app_path(
        results_root or (default_results_dir() / "ab"), field="results_dir"
    )


def _seconds_until_target(session: str, now: datetime, calendar_client: Any) -> float:
    info = effective_target_for_session(
        date.fromisoformat(session), RUN_TIME_ET, calendar_client=calendar_client
    )
    hour, minute = (int(part) for part in info["effective_target"].split(":"))
    target = datetime.combine(date.fromisoformat(session), datetime.min.time(), ET)
    return (target.replace(hour=hour, minute=minute) - now.astimezone(ET)).total_seconds()


def _wait_for_session_target(
    session: str, *, now_fn: Callable[[], datetime], sleep_fn: Callable[[float], None],
    calendar_client: Any,
) -> None:
    """Bounded-polling wait until the frozen session's effective target."""
    while True:
        remaining = _seconds_until_target(session, now_fn(), calendar_client)
        if remaining <= 0:
            return
        sleep_fn(max(1.0, min(POLL_INTERVAL_SECONDS, remaining)))


def _wait_for_execution_window(
    day: str, *, now_fn: Callable[[], datetime], sleep_fn: Callable[[float], None],
    calendar_client: Any,
) -> bool:
    """Schedule wait for today's session (a closed market is not a failure).

    Returns True when the caller should resume the campaign; False when the
    calendar day rolled over or the effective execution window (target plus
    grace) has closed for today.
    """
    while True:
        now = now_fn()
        if now.astimezone(ET).date().isoformat() != str(day):
            return False
        remaining = _seconds_until_target(str(day), now, calendar_client)
        if remaining <= -EXECUTION_WINDOW_GRACE_SECONDS:
            return False
        if remaining > 0:
            sleep_fn(max(1.0, min(POLL_INTERVAL_SECONDS, remaining)))
        else:
            # Past target but inside the grace window: short poll, then let
            # the next campaign pass re-decide.
            sleep_fn(min(60.0, EXECUTION_WINDOW_GRACE_SECONDS))
            return True


def run_auto(
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    days: int = 30,
    results_root: str | Path | None = None,
    resume: str | Path | None = None,
    execute_paper: bool | None = None,
    paper_notional_usd: float | None = None,
    continuous: bool = False,

    base_config: dict[str, Any] | None = None,
    calendar_client: Any = None,
    now_fn: Callable[[], datetime] = _now,
    sleep_fn: Callable[[float], None] = time.sleep,
    campaign_runner: Callable[..., dict[str, Any]] = run_campaign,
) -> dict[str, Any]:
    """Run the frozen multi-session campaign unattended without duplicating
    its state machine: every pass resumes the same campaign root."""
    retries = 0
    resolved_calendar = calendar_client
    # Same resolved root the coordinator uses for this pass, so our own state
    # reads and every later --resume provably point at one directory.
    root = _campaign_root(resume=resume, results_root=results_root)
    state_path = _campaign_state_path(root)
    if resume is not None:
        resume = root

    def _calendar(state: dict[str, Any]) -> Any:
        nonlocal resolved_calendar
        if resolved_calendar is None:
            # Reuse the campaign's calendar authority: a Paper campaign reads
            # the calendar with Account A read-only credentials (via
            # _calendar_client); an analysis-only campaign keeps the allowed
            # default read-only source.  Never the default trading credentials.
            resolved_calendar = _campaign_calendar_client(
                execute_paper=bool((state or {}).get("execute_paper")),
                supplied=None,
            )
        return resolved_calendar

    while True:
        result = campaign_runner(
            symbol=symbol, start_date=start_date, days=days,
            results_root=None if resume is not None else root,
            resume=resume, execute_paper=execute_paper,
            paper_notional_usd=paper_notional_usd, base_config=base_config,
            continuous=continuous,
            calendar_client=calendar_client,
        )
        state = result.get("state") or _read_state(state_path) or {}
        outcome = result["outcome"]

        if outcome == "completed":
            return result

        if outcome == "session_completed":
            # Keep going: wait for the next frozen session's effective target
            # and resume the same root.  state["sessions"] is the campaign's
            # frozen authority; this launcher never invents trading days.  If
            # every session is done, the next pass runs campaign finalization.
            sessions = list(state.get("sessions") or [])
            done = list(state.get("completed_dates") or [])
            retries = 0
            resume = root
            symbol = start_date = None
            if len(done) < len(sessions):
                _wait_for_session_target(
                    sessions[len(done)], now_fn=now_fn, sleep_fn=sleep_fn,
                    calendar_client=_calendar(state),
                )
            continue

        if outcome == "not_due":
            retries = 0
            _wait_for_session_target(
                str(result["next_date"]), now_fn=now_fn, sleep_fn=sleep_fn,
                calendar_client=_calendar(state),
            )
            resume = root
            symbol = start_date = None
            continue

        if outcome == "market_closed":
            # Schedule wait, not a transient retry: an early start simply
            # waits for the session's effective execution target (early
            # closes are honored by the same authority) and never consumes
            # the same-day retry budget.
            day = result.get("next_date") or state.get("current_date")
            retries = 0
            if day and _wait_for_execution_window(
                str(day), now_fn=now_fn, sleep_fn=sleep_fn,
                calendar_client=_calendar(state),
            ):
                resume = root
                symbol = start_date = None
                continue
            return result

        if outcome == "awaiting_final_settlement":
            # Bounded same-root re-checks only.  Each resume reruns the
            # campaign's recovery/reconciliation settlement refresh before
            # rechecking the durable DBs; no analysis, no new orders.
            if retries >= MAX_SAME_DAY_RETRIES:
                return result
            retries += 1
            sleep_fn(RETRY_DELAY_SECONDS)
            resume = root
            symbol = start_date = None
            continue

        if outcome == "pair_unfinished":
            day = result.get("next_date") or state.get("current_date")
            if not day:
                return result
            now_et = now_fn().astimezone(ET)
            remaining = _seconds_until_target(str(day), now_fn(), _calendar(state))
            # effective target is always at least 30 minutes before close;
            # do not carry an unfinished session into tomorrow.
            if (now_et.date().isoformat() != str(day)
                    or remaining < -EXECUTION_WINDOW_GRACE_SECONDS
                    or retries >= MAX_SAME_DAY_RETRIES):
                return result
            retries += 1
            sleep_fn(RETRY_DELAY_SECONDS)
            resume = root
            symbol = start_date = None
            continue

        # previous_session_unfinished and fail-closed campaign outcomes need
        # operator attention; never reinterpret yesterday as today's run.
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--start-date")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--results-root")
    parser.add_argument("--resume")
    parser.add_argument("--config-json")
    parser.add_argument("--execute", action="store_true", default=None)
    parser.add_argument("--paper-notional-usd", type=float)
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Never finalize: extend the same campaign by frozen chunks of --days sessions",
    )
    args = parser.parse_args(argv)
    if args.resume:
        if args.symbol or args.start_date:
            parser.error("--resume cannot be combined with --symbol/--start-date")
    elif not (args.symbol and args.start_date):
        parser.error(
            "creating a campaign requires both --symbol and --start-date"
            " (or use --resume to continue an existing one)"
        )
    from tradingagents.long_run import GlobalRunnerLockBusy, global_runner_lock

    try:
        # Outermost lock: exactly one Paper runner process application-wide;
        # held for the whole unattended campaign so no single long-run or
        # second A/B runner can start concurrently.
        with global_runner_lock():
            result = run_auto(
                symbol=args.symbol, start_date=args.start_date, days=args.days,
                results_root=args.results_root, resume=args.resume,
                execute_paper=args.execute, paper_notional_usd=args.paper_notional_usd,
                continuous=args.continuous,
                base_config=_load_config(args.config_json),
            )
    except GlobalRunnerLockBusy as exc:
        print(f"[AB campaign auto] ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"[AB campaign auto] {result['outcome']}")
    return 0 if result["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
