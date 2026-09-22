#!/usr/bin/env python3
"""Thin unattended launcher for the existing resumable A/B campaign.

It owns waiting and bounded same-session retries only.  Campaign state,
calendar freezing, pair execution, recovery, and finalization stay in their
existing owners.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from scripts.run_analysis_ab import _load_config
from scripts.run_analysis_ab_campaign import _campaign_state_path, _read_state, run_campaign
from tradingagents.long_run import effective_target_for_session


ET = ZoneInfo("America/New_York")
RUN_TIME_ET = "11:00"
RETRY_DELAY_SECONDS = 15 * 60
MAX_SAME_DAY_RETRIES = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_until_target(session: str, now: datetime, calendar_client: Any) -> float:
    info = effective_target_for_session(
        date.fromisoformat(session), RUN_TIME_ET, calendar_client=calendar_client
    )
    hour, minute = (int(part) for part in info["effective_target"].split(":"))
    target = datetime.combine(date.fromisoformat(session), datetime.min.time(), ET)
    return (target.replace(hour=hour, minute=minute) - now.astimezone(ET)).total_seconds()


def run_auto(
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    days: int = 30,
    results_root: str | Path | None = None,
    resume: str | Path | None = None,
    execute_paper: bool | None = None,
    paper_notional_usd: float | None = None,
    base_config: dict[str, Any] | None = None,
    calendar_client: Any = None,
    now_fn: Callable[[], datetime] = _now,
    sleep_fn: Callable[[float], None] = __import__("time").sleep,
    campaign_runner: Callable[..., dict[str, Any]] = run_campaign,
) -> dict[str, Any]:
    """Run the frozen 30-session campaign without duplicating its state machine."""
    retries = 0
    while True:
        result = campaign_runner(
            symbol=symbol, start_date=start_date, days=days, results_root=results_root,
            resume=resume, execute_paper=execute_paper,
            paper_notional_usd=paper_notional_usd, base_config=base_config,
            calendar_client=calendar_client,
        )
        root = Path(resume or results_root or "")
        state = result.get("state") or _read_state(_campaign_state_path(root))
        outcome = result["outcome"]
        if outcome == "completed":
            return result
        if outcome == "awaiting_final_settlement":
            # Bounded same-root re-checks only: no analysis, no new orders.
            # Each resume reruns recovery/reconciliation and the settlement
            # check; if the primary stays unsettled past the retry budget the
            # operator resumes manually.
            if retries >= MAX_SAME_DAY_RETRIES:
                return result
            retries += 1
            sleep_fn(RETRY_DELAY_SECONDS)
            resume = root
            symbol = start_date = None
            continue
        if outcome == "not_due":
            wait = _seconds_until_target(result["next_date"], now_fn(), calendar_client)
            sleep_fn(max(1.0, min(300.0, wait)))
            resume = root
            symbol = start_date = None
            continue
        if outcome in {"market_closed", "pair_unfinished"}:
            day = result.get("next_date") or (state or {}).get("current_date")
            if not day:
                return result
            now_et = now_fn().astimezone(ET)
            # effective target is always at least 30 minutes before close;
            # do not carry an unfinished session into tomorrow.
            remaining = _seconds_until_target(day, now_fn(), calendar_client)
            if now_et.date().isoformat() != day or remaining < -30 * 60 or retries >= MAX_SAME_DAY_RETRIES:
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
    args = parser.parse_args(argv)
    if bool(args.resume) == bool(args.symbol or args.start_date):
        parser.error("use --resume, or provide both --symbol and --start-date")
    result = run_auto(
        symbol=args.symbol, start_date=args.start_date, days=args.days,
        results_root=args.results_root, resume=args.resume,
        execute_paper=args.execute, paper_notional_usd=args.paper_notional_usd,
        base_config=_load_config(args.config_json),
    )
    print(f"[AB campaign auto] {result['outcome']}")
    return 0 if result["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
