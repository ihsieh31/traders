"""Phase D — 30-day unattended Alpaca Paper observation (single CLI command).

One module owns the Phase-D lifecycle: non-secret config, atomic state,
single-runner lock, preflight, scheduling, daily rounds, crash/resume,
snapshots, hard-stop, and final reporting. No orchestration framework, no
new order-state database, no live-trading path — every broker mutation goes
through :class:`ExecutionService`, every universe decision through Phase C
``prepare_screening_round``, every analysis through ``TradingAgentsGraph``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tradingagents.redaction import sanitize_for_log, sanitize_url

LONG_RUN_SCHEMA_VERSION = 1
DEFAULT_DURATION_CALENDAR_DAYS = 30
DEFAULT_RUN_TIME_ET = "11:00"
VALID_ANALYSTS = ("market", "social", "news", "fundamentals", "macro")
VALID_RESEARCH_DEPTHS = (1, 3, 5)
# Regular-session window (ET) a configured daily time must fall in; a time
# that can never be a regular-session time is rejected at setup.
SESSION_OPEN_ET = dtime(9, 30)
SESSION_CLOSE_ET = dtime(16, 0)
# Providers with no usable provider-default endpoint: a backend URL is required.
PROVIDERS_REQUIRING_URL = ("local_openai", "ollama")

PLACEHOLDER_MARKERS = ("your_", "here", "changeme", "xxx")


class LongRunStop(RuntimeError):
    """Hard stop: safe unattended continuation cannot be proven."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail or code
        super().__init__(f"{code}: {self.detail}" if detail else code)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def base_dir() -> Path:
    override = os.getenv("TRADINGAGENTS_LONG_RUN_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~")) / ".tradingagents" / "long_run"


def config_path() -> Path:
    return base_dir() / "config.json"


def active_path() -> Path:
    return base_dir() / "active.json"


def lock_path() -> Path:
    return base_dir() / "runner.lock"


def run_dir(run_id: str) -> Path:
    return base_dir() / "runs" / run_id


def round_path(run_id: str, session_date: str) -> Path:
    return run_dir(run_id) / "rounds" / f"{session_date}.json"


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def _looks_placeholder(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(m in text for m in PLACEHOLDER_MARKERS)


def scan_files_for_secrets(paths: List[Path], markers: List[str]) -> List[str]:
    """Return file paths containing any marker (fake-secret leak test)."""
    hits = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(m and m in text for m in markers):
            hits.append(str(path))
    return hits


# ---------------------------------------------------------------------------
# Atomic JSON state + events + single-runner lock
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_for_log(record), ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunnerLockBusy(RuntimeError):
    pass


@contextmanager
def runner_lock():
    """Single-runner guard: only one Phase-D process per machine/user."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RunnerLockBusy(
                "another Phase-D runner holds the lock"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "at": utc_now_iso()}))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()


# ---------------------------------------------------------------------------
# Non-secret Phase-D configuration
# ---------------------------------------------------------------------------

def default_long_run_config() -> Dict[str, Any]:
    return {
        "schema_version": LONG_RUN_SCHEMA_VERSION,
        "duration_calendar_days": DEFAULT_DURATION_CALENDAR_DAYS,
        "run_time_et": DEFAULT_RUN_TIME_ET,
        "base_trade_notional_usd": None,
        "analysts": list(VALID_ANALYSTS),
        "research_depth": 3,
        "output_language": "English",
        "analysis_provider": None,
        "analysis_model": None,
        "analysis_backend_url": None,
        "decision_provider": None,
        "decision_model": None,
        "decision_backend_url": None,
        # Optional Analysis-only failover route (Phase B): all three keys
        # unset means failover stays disabled. Secrets never live here.
        "analysis_fallback_provider": None,
        "analysis_fallback_model": None,
        "analysis_fallback_backend_url": None,
        # Short exposure opt-in for the observation (paper-only build;
        # broker-side deterministic guards always apply).
        "allow_shorts": False,
        "screening_provider": None,
        "screening_model": None,
        "screening_backend_url": None,
    }


def load_long_run_config() -> Dict[str, Any]:
    cfg = default_long_run_config()
    saved = read_json(config_path())
    if isinstance(saved, dict):
        for key in cfg:
            if saved.get(key) is not None:
                cfg[key] = saved[key]
    return cfg


def save_long_run_config(cfg: Dict[str, Any]) -> None:
    payload = {k: cfg.get(k) for k in default_long_run_config()}
    payload["schema_version"] = LONG_RUN_SCHEMA_VERSION
    text = json.dumps(payload, default=str)
    lowered = text.lower()
    if any(m.lower() in lowered for m in ("api_key", "secret_key", "sk-")):
        raise ValueError("refusing to persist possible secret material in config.json")
    atomic_write_json(config_path(), payload)


def _valid_backend_url(value: Any) -> bool:
    if value is None or str(value).strip() == "":
        return True
    try:
        parts = urllib.parse.urlsplit(str(value).strip())
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except Exception:
        return False


def missing_config_fields(cfg: Dict[str, Any]) -> List[str]:
    """Fields the setup wizard must ask for (absent/invalid/placeholder)."""
    missing: List[str] = []
    if cfg.get("base_trade_notional_usd") is None:
        missing.append("base_trade_notional_usd")
    if not cfg.get("run_time_et"):
        missing.append("run_time_et")
    for key in (
        "analysis_provider", "analysis_model",
        "decision_provider", "decision_model",
        "screening_provider", "screening_model",
    ):
        value = cfg.get(key)
        if not value or _looks_placeholder(value):
            missing.append(key)
    for key in (
        "analysis_backend_url", "decision_backend_url", "screening_backend_url",
    ):
        provider = cfg.get(key.replace("_backend_url", "_provider")) or ""
        if str(provider).lower() in PROVIDERS_REQUIRING_URL and not cfg.get(key):
            if key not in missing:
                missing.append(key)
    if not cfg.get("analysts"):
        missing.append("analysts")
    return missing


def _unattended_safety_error(runtime: Dict[str, Any]) -> str:
    """P2-01: an unattended paper run cannot run with the safety layer off.

    Only ``runtime.get("safety_enabled", True) is True`` may proceed: the
    key may be absent (default on), but False/0/"false"/None (explicit)
    must refuse. The general SafetyGuard feature itself is untouched —
    this gates only the unattended long-run production path.
    """
    if runtime.get("safety_enabled", True) is True:
        return ""
    return (
        "unattended paper execution requires safety_enabled=True "
        f"(got {runtime.get('safety_enabled')!r})"
    )


def _validate_unattended_safety(runtime: Dict[str, Any]) -> None:
    error = _unattended_safety_error(runtime)
    if error:
        raise LongRunStop("SAFETY_DISABLED", error)


def validate_long_run_config(
    cfg: Dict[str, Any], runtime: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Secret-free validation; returns error strings (empty = valid)."""
    errors: List[str] = []
    if cfg.get("schema_version") != LONG_RUN_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {cfg.get('schema_version')!r}")
    duration = cfg.get("duration_calendar_days")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        errors.append("duration_calendar_days must be a positive integer")
    try:
        target = parse_run_time_et(str(cfg.get("run_time_et") or ""))
    except ValueError as exc:
        errors.append(str(exc))
        target = None
    if target is not None and not (SESSION_OPEN_ET <= target < SESSION_CLOSE_ET):
        errors.append(
            f"run_time_et {cfg.get('run_time_et')!r} can never be a regular-session "
            "time (09:30 <= run_time_et < 16:00 ET)"
        )
    notional = cfg.get("base_trade_notional_usd")
    if (
        not isinstance(notional, (int, float))
        or isinstance(notional, bool)
        or not math.isfinite(float(notional))
        or float(notional) <= 0
    ):
        errors.append("base_trade_notional_usd must be a positive finite number")
    analysts = cfg.get("analysts") or []
    if not analysts or any(a not in VALID_ANALYSTS for a in analysts):
        errors.append(f"analysts must be a non-empty subset of {list(VALID_ANALYSTS)}")
    if cfg.get("research_depth") not in VALID_RESEARCH_DEPTHS:
        errors.append(f"research_depth must be one of {list(VALID_RESEARCH_DEPTHS)}")
    if not (cfg.get("output_language") or "").strip():
        errors.append("output_language must be non-empty")
    for role in ("analysis", "decision", "screening"):
        provider = (cfg.get(f"{role}_provider") or "").strip()
        model = (cfg.get(f"{role}_model") or "").strip()
        if not provider or not model:
            errors.append(f"{role}_provider and {role}_model are required (no inheritance)")
        if not _valid_backend_url(cfg.get(f"{role}_backend_url")):
            errors.append(f"{role}_backend_url is not a valid http(s) URL")
        if provider.lower() in PROVIDERS_REQUIRING_URL and not (cfg.get(f"{role}_backend_url") or "").strip():
            errors.append(f"{role}_backend_url is required for provider {provider!r}")
    if runtime is not None:
        safety_error = _unattended_safety_error(runtime)
        if safety_error:
            errors.append(safety_error)
        try:
            from tradingagents.llm_clients.retry import validate_llm_max_retries

            validate_llm_max_retries(runtime.get("llm_max_retries", 3))
        except Exception as exc:
            errors.append(f"llm_max_retries invalid: {exc}")
        try:
            from tradingagents.llm_clients.roles import resolve_role_config

            resolved = resolve_role_config(runtime)
            if resolved.get("mode") != "roles":
                errors.append("role resolution did not enter roles mode")
        except Exception as exc:
            errors.append(f"role config invalid: {exc}")
        try:
            from tradingagents.screening.llm import resolve_screening_config

            resolved = resolve_screening_config(runtime)
            if not resolved.get("enabled"):
                errors.append("screening role did not resolve to enabled")
        except Exception as exc:
            errors.append(f"screening config invalid: {exc}")
        try:
            from tradingagents.dataflows.config import get_alpaca_use_paper

            flag = get_alpaca_use_paper()
            text = str(flag if flag is not None else "True").strip().lower()
            if text in ("false", "0", "no", "off", "live"):
                errors.append("ALPACA_USE_PAPER=False is not supported (paper-only)")
        except Exception as exc:
            errors.append(f"paper flag unreadable: {exc}")
    return errors


def parse_run_time_et(value: str) -> dtime:
    text = (value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise ValueError(f"run_time_et {value!r} must be HH:MM (24h)")
    return parsed


def build_runtime_config(
    long_cfg: Dict[str, Any], base: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Full graph/execution config for a round: Phase D forces full-system mode."""
    if base is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        base = DEFAULT_CONFIG
    runtime = dict(base)
    depth = int(long_cfg.get("research_depth") or 3)
    runtime["max_debate_rounds"] = depth
    runtime["max_risk_discuss_rounds"] = depth
    runtime["output_language"] = long_cfg.get("output_language") or "English"
    # Base provider keys mirror the Analysis role so legacy construction paths
    # keep working; the explicit role keys switch resolution into roles mode.
    runtime["llm_provider"] = long_cfg.get("analysis_provider")
    runtime["backend_url"] = long_cfg.get("analysis_backend_url")
    runtime["deep_think_llm"] = long_cfg.get("analysis_model")
    runtime["quick_think_llm"] = long_cfg.get("analysis_model")
    for key in (
        "analysis_provider", "analysis_model", "analysis_backend_url",
        "analysis_fallback_provider", "analysis_fallback_model",
        "analysis_fallback_backend_url",
        "decision_provider", "decision_model", "decision_backend_url",
        "screening_provider", "screening_model", "screening_backend_url",
        "allow_shorts",
    ):
        runtime[key] = long_cfg.get(key)
    runtime["auto_screening_enabled"] = True
    # Short exposure is now an explicit per-observation opt-in (paper only;
    # the broker-side deterministic guards in execution.service still apply).
    runtime["allow_shorts"] = bool(long_cfg.get("allow_shorts", False))
    runtime["trading_mode"] = "trading" if runtime["allow_shorts"] else "investment"
    return runtime


def git_baseline_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        commit = (out.stdout or "").strip()
        return commit if commit else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Scheduling (authoritative Alpaca calendar only)
# ---------------------------------------------------------------------------

def eastern_now(now: Any = None) -> datetime:
    from tradingagents.screening.sessions import eastern_now as _eastern_now

    return _eastern_now(now)


def effective_target_for_session(
    session_day: date,
    run_time_et: str,
    *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Configured target vs effective target for one authoritative session."""
    from tradingagents.dataflows.market_calendar import session_close_et_auth

    target = parse_run_time_et(run_time_et)
    close = session_close_et_auth(
        session_day, client=calendar_client, calendar_rows=calendar_rows
    )
    configured = datetime.combine(session_day, target)
    if target > close:
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
    start: date, end: date, client: Any = None
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
    use_rows = calendar_rows is not None or calendar_client is not None
    while day <= min(eastern.date(), end_day):
        if use_rows or True:
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
    while day <= end_day:
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


# ---------------------------------------------------------------------------
# Observation state
# ---------------------------------------------------------------------------

def session_close_et(
    session_date: str, *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
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
    if journal is not None:
        return False  # started earlier (even if unfinished): recovery owns it
    # H-04: read_json() returns None for both a missing file and an
    # unreadable one. A journal that exists but cannot be parsed must stop
    # as STATE_CORRUPT — writing a fresh MISSED journal over it would
    # destroy the only evidence of a session that may have started.
    if round_path(run_id, session_date).exists():
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
    new_journal = new_round_journal(session_date, [])
    new_journal["status"] = "MISSED"
    new_journal["stop_reason"] = "MISSED_SESSION_CLOSE"
    new_journal["finished_at"] = utc_now_iso()
    save_round_journal(run_id, new_journal)
    log_event(run_id, "round_missed",
              {"session": session_date, "reason": "MISSED_SESSION_CLOSE"})
    return True

def new_observation_state(
    long_cfg: Dict[str, Any], *, expected_sessions: List[str], now: Optional[datetime] = None
) -> Dict[str, Any]:
    started = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    ends = started + timedelta(days=int(long_cfg.get("duration_calendar_days") or 30))
    run_id = f"lr-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    return {
        "schema_version": LONG_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "RUNNING",
        "started_at": started.isoformat(),
        "ends_at": ends.isoformat(),
        "timezone": "US/Eastern",
        "baseline_commit": git_baseline_commit(),
        "config": sanitize_for_log(long_cfg),
        "expected_sessions": list(expected_sessions or []),
        "restart_count": 0,
        "stop": None,
    }


def load_active_state() -> Optional[Dict[str, Any]]:
    """Resume state, or None only when no active state file exists.

    Only a missing file may mean "no active run". A file that exists but
    cannot be trusted (unreadable, truncated, non-dict, missing/blank
    run_id, unexpected/non-resumable status) is a hard stop: creating a
    fresh run would mint a new run_id, and execution decision identity
    includes run_id, so the old run's idempotency could no longer protect
    this restart. Terminal statuses (e.g. COMPLETED) are included because
    finalize_observation() persists them before its final reports and
    clear_active_state() — a crash in that window must not look like a
    fresh install.
    """
    try:
        with open(active_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise LongRunStop(
            "ACTIVE_STATE_CORRUPT",
            f"active state {active_path()} is not valid JSON "
            f"({exc.msg} at line {exc.lineno} column {exc.colno})",
        )
    except OSError as exc:
        raise LongRunStop(
            "ACTIVE_STATE_CORRUPT",
            f"active state {active_path()} exists but cannot be read: "
            f"{type(exc).__name__}",
        )
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not run_id.strip():
        raise LongRunStop(
            "ACTIVE_STATE_CORRUPT",
            f"active state {active_path()} has no usable run_id",
        )
    status = data.get("status")
    if status in ("RUNNING", "INTERRUPTED"):
        return data
    raise LongRunStop(
        "ACTIVE_STATE_CORRUPT",
        f"active state {active_path()} exists with unexpected/non-resumable "
        f"status {status!r}",
    )


def save_active_state(state: Dict[str, Any]) -> None:
    atomic_write_json(active_path(), state)


def clear_active_state() -> None:
    try:
        os.unlink(active_path())
    except OSError:
        pass


def log_event(run_id: str, event_type: str, detail: Any = None) -> None:
    append_jsonl(
        run_dir(run_id) / "events.jsonl",
        {"at": utc_now_iso(), "type": event_type, "detail": detail},
    )


def completed_sessions(run_id: str) -> List[str]:
    rounds_dir = run_dir(run_id) / "rounds"
    done = []
    if not rounds_dir.is_dir():
        return done
    for path in sorted(rounds_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("status") == "COMPLETED":
            done.append(path.stem)
    return done


# A round journal in one of these states is settled: its session was decided
# once (traded, missed, or hard-stopped) and must never be re-picked or
# re-run. Re-running a MISSED session would analyze stale data days late and
# submit a stale order (A18), and overwriting one would erase the audit
# record that the session ever slipped (B02).
TERMINAL_ROUND_STATUSES = ("COMPLETED", "MISSED", "STOPPED")


def settled_sessions(run_id: str) -> List[str]:
    """Session dates with a terminal round journal (never scheduled again)."""
    rounds_dir = run_dir(run_id) / "rounds"
    settled = []
    if not rounds_dir.is_dir():
        return settled
    for path in sorted(rounds_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("status") in TERMINAL_ROUND_STATUSES:
            settled.append(path.stem)
    return settled


# ---------------------------------------------------------------------------
# Injectable runtime boundaries (tests drive fakes through these)
# ---------------------------------------------------------------------------

@dataclass
class LongRunDeps:
    screening_fn: Optional[Callable[..., Any]] = None
    graph_factory: Optional[Callable[[Dict[str, Any]], Any]] = None
    execution_service_factory: Optional[Callable[[], Any]] = None
    broker_client_factory: Optional[Callable[[], Any]] = None
    llm_probe_fn: Optional[Callable[..., Dict[str, Any]]] = None
    calendar_client: Any = None
    calendar_rows: Optional[List[Any]] = None
    now_fn: Callable[[], datetime] = field(
        default_factory=lambda: (lambda: datetime.now(timezone.utc))
    )
    sleep_fn: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    alert_fn: Optional[Callable[[str, str, Optional[dict]], dict]] = None


def _default_screening(config: Dict[str, Any], refresh: bool = False) -> Any:
    from tradingagents.screening.pipeline import prepare_screening_round

    return prepare_screening_round(config, refresh=refresh)


def _default_graph_factory(config: Dict[str, Any]) -> Any:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    analysts = config.get("_long_run_analysts") or [
        "market", "social", "news", "fundamentals", "macro",
    ]
    return TradingAgentsGraph(list(analysts), config=config, debug=False)


def _default_execution_service() -> Any:
    from tradingagents.execution import ExecutionService

    return ExecutionService()


def _default_broker_client() -> Any:
    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

    return get_alpaca_trading_client()


# Redacted marker passed to injectable probes; the real probe re-resolves
# the live credential itself so no live key crosses the probe boundary.
REDACTED_API_KEY = "***"


def _default_llm_probe(
    *, role: str, provider: str, model: str, backend_url: Optional[str],
    api_key: str, max_retries: int,
) -> Dict[str, Any]:
    """One minimal bounded transport probe (tiny prompt, existing retry owner).

    Secrets boundary: run_preflight (and any injected probe double) only ever
    receives the redacted marker. This real probe re-resolves the live key
    for the exact role itself and never stores or echoes it.
    """
    from tradingagents.llm_clients.roles import _resolve_provider_key

    if api_key == REDACTED_API_KEY:
        api_key = _resolve_provider_key(provider, role)
    if not api_key:
        raise LongRunStop(
            "PREFLIGHT_FAILED",
            f"no API key for probe role={role} provider={provider}",
        )
    started = time.monotonic()
    try:
        from tradingagents.llm_clients.factory import create_llm_client
        from tradingagents.llm_clients.retry import RetryingLLM

        client = create_llm_client(
            provider, model, base_url=backend_url, api_key=api_key,
            model_role="quick",
        )
        llm = RetryingLLM(
            client.get_llm(), role=f"preflight-{role}",
            provider=provider, model=model, max_retries=int(max_retries),
        )
        llm.invoke("Reply with the single word: ok")
    except Exception as exc:
        from tradingagents.llm_clients.retry import ProviderFailure

        if isinstance(exc, ProviderFailure):
            raise
        raise LongRunStop(
            "LLM_PROBE_FAILED",
            f"preflight probe failed for role={role} provider={provider} "
            f"model={model}: {type(exc).__name__}: {exc}",
        )
    return {
        "role": role, "provider": provider, "model": model,
        "endpoint": sanitize_url(backend_url) or "provider default",
        "latency_seconds": round(time.monotonic() - started, 3),
        "result": "ok",
    }


# ---------------------------------------------------------------------------
# Account snapshots (sanitized, no credentials)
# ---------------------------------------------------------------------------

class SnapshotUnavailable(RuntimeError):
    """A required account/position fact is missing or non-finite (R16)."""


def capture_account_snapshot(client: Any) -> Dict[str, Any]:
    """Sanitized account/positions snapshot; raise on unprovable facts (R16).

    A NaN equity, missing cash, or ``positions`` of None/invalid value means
    the broker could not prove the account state. Coercing any of them to a
    number would let a report claim -100% returns or a fully flat account;
    instead the capture raises so callers fail closed (preflight aborts,
    finalize records ``final_snapshot_unavailable``).
    """
    account = client.get_account()

    def _num(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _get(obj: Any, *names: str) -> Any:
        for name in names:
            value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if value is not None:
                return getattr(value, "value", value)
        return None

    account_id = str(_get(account, "id", "account_id") or "")
    if not account_id:
        raise SnapshotUnavailable("account snapshot has no usable account id")
    equity = _num(_get(account, "equity"))
    if equity is None:
        raise SnapshotUnavailable("account equity is unavailable (missing/NaN/non-finite)")
    cash = _num(_get(account, "cash"))
    if cash is None:
        raise SnapshotUnavailable("account cash is unavailable (missing/NaN/non-finite)")
    raw_buying_power = _get(account, "buying_power")
    buying_power = _num(raw_buying_power) if raw_buying_power is not None else None
    if raw_buying_power is not None and buying_power is None:
        raise SnapshotUnavailable(
            "account buying_power is present but not a finite number"
        )
    # None means the broker could not enumerate positions at all — an empty
    # list (explicitly no holdings) is a legal, different fact.
    positions = client.get_all_positions()
    if positions is None:
        raise SnapshotUnavailable("position list unavailable (broker returned None)")
    rows = []
    long_mv = 0.0
    short_mv = 0.0
    for raw in positions:
        symbol = str(_get(raw, "symbol") or "").upper()
        if not symbol:
            raise SnapshotUnavailable("position row has no symbol")
        qty = _num(_get(raw, "qty"))
        if qty is None:
            raise SnapshotUnavailable(f"position {symbol} qty is unavailable")
        if qty == 0:
            continue
        market_value = _num(_get(raw, "market_value"))
        if market_value is None:
            raise SnapshotUnavailable(
                f"position {symbol} market_value is unavailable (missing/NaN)"
            )
        if market_value >= 0:
            long_mv += market_value
        else:
            short_mv += market_value
        rows.append({
            "symbol": symbol,
            "qty": qty,
            "side": "long" if qty > 0 else "short",
            "market_value": market_value,
            "avg_entry_price": _num(_get(raw, "avg_entry_price")),
            "unrealized_pl": _num(_get(raw, "unrealized_pl")),
        })
    rows.sort(key=lambda r: r["symbol"])
    return {
        "at": utc_now_iso(),
        "account_ref": hashlib.sha256(account_id.encode()).hexdigest()[:16] if account_id else "unknown",
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "long_market_value": long_mv,
        "short_market_value": short_mv,
        "positions": rows,
    }


# ---------------------------------------------------------------------------
# Preflight (read-only except local config/state files)
# ---------------------------------------------------------------------------

def run_preflight(
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    deps: Optional[LongRunDeps] = None,
) -> Dict[str, Any]:
    """Validate config, probe LLM transports, verify Alpaca read-only."""
    deps = deps or LongRunDeps()
    checks: List[Dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise LongRunStop("PREFLIGHT_FAILED", f"{name}: {detail}")

    # Defense layer 1: this gate runs before any validation/probe/broker
    # call, so a disabled safety layer can never reach preflight at all
    # (not just when a caller ran validate_long_run_config first).
    _validate_unattended_safety(runtime)
    errors = validate_long_run_config(long_cfg, runtime)
    _check("config_schema", not errors, "; ".join(errors))

    from tradingagents.llm_clients.roles import (
        _resolve_provider_key,
        resolve_role_config,
    )
    from tradingagents.screening.llm import SCREENING_ROLE, resolve_screening_config

    roles = resolve_role_config(runtime)
    screening = resolve_screening_config(runtime)
    max_retries = int(runtime.get("llm_max_retries", 3))

    # One transport probe per role route — never deduped across roles. Two
    # roles sharing provider/model/endpoint may still resolve different
    # role-specific credentials (ANALYSIS_*_API_KEY vs DECISION_*_API_KEY vs
    # ANALYSIS_FALLBACK_*), so every route is verified independently. At
    # most four tiny probes: far cheaper than a dead role credential being
    # discovered mid-way through 30 unattended days.
    probe_routes = [
        ("analysis", roles["analysis"]),
        ("decision", roles["decision"]),
        (SCREENING_ROLE, screening["spec"]),
    ]
    if roles.get("analysis_fallback") is not None:
        probe_routes.append(("analysis_fallback", roles["analysis_fallback"]))
    probe_fn = deps.llm_probe_fn or _default_llm_probe
    for role_name, spec in probe_routes:
        try:
            # Resolve only to prove the credential exists for THIS role; the
            # probe itself still receives just the redacted marker.
            if not _resolve_provider_key(spec.provider, role_name):
                raise LongRunStop(
                    "PREFLIGHT_FAILED",
                    f"no API key for probe role={role_name} provider={spec.provider}",
                )
            result = probe_fn(
                role=role_name, provider=spec.provider, model=spec.model,
                backend_url=spec.backend_url or None, api_key=REDACTED_API_KEY,
                max_retries=max_retries,
            )
            checks.append({"name": f"llm_probe:{role_name}", "ok": True,
                           "detail": str(result)})
        except LongRunStop:
            raise
        except Exception as exc:
            from tradingagents.llm_clients.retry import ProviderFailure

            if isinstance(exc, ProviderFailure):
                raise LongRunStop("PREFLIGHT_FAILED", f"llm_probe:{role_name}: {exc}")
            raise LongRunStop(
                "PREFLIGHT_FAILED", f"llm_probe:{role_name}: {type(exc).__name__}: {exc}"
            )

    # Alpaca read-only preflight: paper client, account, positions, calendar.
    # No test order is ever placed and NO recovery mutation happens here
    # (F06): preflight runs BEFORE the explicit 30-day authorization
    # question, so startup_recover() — which may resubmit a missing
    # PENDING/UNKNOWN order — must not run until the user has authorized.
    try:
        broker_factory = deps.broker_client_factory or _default_broker_client
        client = broker_factory()
        account = client.get_account()
        _ = float(getattr(account, "equity", 0) or 0)
        positions = client.get_all_positions()
        _ = list(positions or [])
        checks.append({"name": "alpaca_account", "ok": True, "detail": "read-only ok"})
    except Exception as exc:
        raise LongRunStop("PREFLIGHT_FAILED", f"alpaca_account: {exc}")
    try:
        start = date.today() - timedelta(days=7)
        fetch_session_dates(start, date.today() + timedelta(days=7),
                            client=deps.calendar_client)
        checks.append({"name": "alpaca_calendar", "ok": True, "detail": "authoritative ok"})
    except Exception as exc:
        raise LongRunStop("PREFLIGHT_FAILED", f"alpaca_calendar: {exc}")

    # Optional data sources are degraded-source status, never hard dependencies.
    try:
        from tradingagents.dataflows.config import (
            get_api_key as _get_key,
        )

        optional = {
            "finnhub": bool(_get_key("finnhub_api_key", "FINNHUB_API_KEY")),
            "fred": bool(_get_key("fred_api_key", "FRED_API_KEY")),
            "coindesk": bool(_get_key("coindesk_api_key", "COINDESK_API_KEY")),
        }
    except Exception:
        optional = {}
    checks.append({"name": "optional_sources", "ok": True, "detail": str(optional)})

    snapshot = capture_account_snapshot(client)
    return {"ok": True, "checks": checks, "snapshot": snapshot,
            "optional_sources": optional}


def _apply_runtime_config(runtime: Dict[str, Any]) -> None:
    """R01: install this run's runtime as the global execution config BEFORE
    any path that could mutate the broker runs.

    The execution gate (screening Top20 entry check) reads the global config
    via get_config(), while long-run callers hold their own runtime dict.
    Until set_config() is applied, recovery may see runtime
    auto_screening_enabled=True against a global auto_screening_enabled=False
    and misclassify a gated PENDING opening as a manual-mode recovery. The
    merge keeps every key the run did not explicitly override.
    """
    try:
        from tradingagents.dataflows.config import get_config, set_config

        merged = dict(get_config() or {})
        merged.update(runtime or {})
        set_config(merged)
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED",
            f"could not apply runtime config before recovery: {exc}",
        )


def _validate_long_run_execution_config(runtime: Dict[str, Any]) -> None:
    """Confirm the applied global config still proves unattended invariants.

    Runs after _apply_runtime_config (and after any later merge) and refuses
    to continue when the effective config turned off auto screening, paper
    mode or the safety layer for an unattended long run.
    """
    try:
        from tradingagents.dataflows.config import get_config
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED", f"global execution config unreadable: {exc}"
        )
    effective = get_config() or {}
    raw_budget = effective.get("daily_llm_token_budget", 0)
    if (
        isinstance(raw_budget, bool)
        or not isinstance(raw_budget, (int, float))
        or not math.isfinite(float(raw_budget))
    ):
        raise LongRunStop(
            "LLM_BUDGET_INVALID",
            "daily_llm_token_budget must be a finite numeric value",
        )
    if raw_budget < 0:
        raise LongRunStop(
            "LLM_BUDGET_INVALID",
            "daily_llm_token_budget must be >= 0",
        )
    if raw_budget > 20_000_000:
        raise LongRunStop(
            "LLM_BUDGET_TOO_HIGH",
            "unattended long-run daily_llm_token_budget must be <= 20000000",
        )
    if raw_budget == 0:
        # B-02: 0 stays "unlimited" in general mode, but an unattended
        # long run must never run uncapped. Normalize the effective budget
        # to the fixed 20M/day hard cap and persist it, so check_llm_budget()
        # and any lazily built SafetyGuard see the cap instead of 0.
        try:
            from tradingagents.dataflows.config import set_config
            from tradingagents.safety import reset_safety_guard

            merged = dict(get_config() or {})
            merged["daily_llm_token_budget"] = 20_000_000
            set_config(merged)
            reset_safety_guard()
            append_jsonl(
                base_dir() / "events.jsonl",
                {
                    "at": utc_now_iso(),
                    "type": "llm_budget_normalized",
                    "detail": {
                        "original": raw_budget,
                        "normalized": 20_000_000,
                        "scope": "unattended_long_run",
                    },
                },
            )
        except Exception as exc:
            raise LongRunStop(
                "CONFIG_APPLY_FAILED",
                f"could not persist normalized LLM token budget: {exc}",
            )
    if not effective.get("auto_screening_enabled"):
        raise LongRunStop(
            "SAFETY_DISABLED",
            "unattended long-run requires auto_screening_enabled=True after "
            "runtime config application",
        )
    error = _unattended_safety_error(effective)
    if error:
        raise LongRunStop("SAFETY_DISABLED", error)
    try:
        from tradingagents.dataflows.config import get_alpaca_use_paper

        flag = get_alpaca_use_paper()
        text = str(flag if flag is not None else "True").strip().lower()
        if text in ("false", "0", "no", "off", "live"):
            raise LongRunStop(
                "SAFETY_DISABLED",
                "unattended long-run requires paper mode "
                "(ALPACA_USE_PAPER=False is not supported)",
            )
    except LongRunStop:
        raise
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED", f"paper flag unreadable after apply: {exc}"
        )


def run_post_authorization_recovery(
    deps: Optional["LongRunDeps"] = None,
    runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mandatory recovery gate between authorization and observation creation.

    F06: run_preflight is genuinely read-only (no startup_recover, no broker
    submit/cancel). After the user authorizes — but BEFORE any observation
    state/manifest/RUNNING exists — this gate recovers durable nonterminal
    orders and requires a CLEAN authority state. On failure nothing is
    created and the process exits.

    R01: ``runtime`` (the run's own config) is applied to the global
    execution config BEFORE startup_recover() runs, so recovery and every
    later execution-path gate see the same screening/entry-policy authority
    as the round itself. Callers without a runtime keep the legacy behavior
    of recovering under whatever global config is already installed.
    """
    deps = deps or LongRunDeps()
    if runtime is not None:
        _apply_runtime_config(runtime)
        _validate_long_run_execution_config(runtime)
    service_factory = deps.execution_service_factory or _default_execution_service
    try:
        service = service_factory()
        recovery = service.startup_recover()
    except LongRunStop:
        raise
    except Exception as exc:
        raise LongRunStop(
            "PREFLIGHT_FAILED", f"post-authorization execution_recovery: {exc}"
        )
    ok = bool(recovery.get("success"))
    if not ok:
        raise LongRunStop(
            "PREFLIGHT_FAILED",
            "execution_recovery: "
            + str(recovery.get("reconciliation_reasons") or recovery.get("error")),
        )
    try:
        broker_factory = deps.broker_client_factory or _default_broker_client
        fresh_snapshot = capture_account_snapshot(broker_factory())
    except Exception as exc:
        raise LongRunStop(
            "PREFLIGHT_FAILED", f"post-recovery account snapshot: {exc}"
        )
    return {"recovery": recovery, "snapshot": fresh_snapshot}


# ---------------------------------------------------------------------------
# Daily round journal + resume state machine
# ---------------------------------------------------------------------------

SYMBOL_PENDING = "PENDING"
SYMBOL_ANALYZING = "ANALYZING"
SYMBOL_ANALYZED = "ANALYZED"
SYMBOL_EXECUTING = "EXECUTING"
SYMBOL_DONE = "DONE"
SYMBOL_FAILED = "FAILED"


def _empty_maintenance_summary() -> Dict[str, Any]:
    """N15: the zero-mutation maintenance summary shape (recovery/deadline)."""
    return {
        "broker_calls": 0, "submit_calls": 0, "cancel_calls": 0,
        "submitted_symbols": [], "has_unknown": False, "paused": False,
        "error": "",
    }


def new_round_journal(
    session_date: str, symbols: List[str], *, scheduled_at: str = "",
    schedule_adjustment: str = "NONE",
) -> Dict[str, Any]:
    return {
        "schema_version": LONG_RUN_SCHEMA_VERSION,
        "session_date": session_date,
        "status": "PENDING",
        "scheduled_at": scheduled_at,
        "started_at": None,
        "finished_at": None,
        "schedule_adjustment": schedule_adjustment,
        "screening": {},
        "symbols": {
            symbol: {
                "status": SYMBOL_PENDING,
                "analysis_run_ref": None,
                "signal": None,
                "trade_intent": None,
                "execution_result_summary": None,
            }
            for symbol in symbols
        },
        "stop_reason": None,
    }


def save_round_journal(run_id: str, journal: Dict[str, Any]) -> None:
    atomic_write_json(round_path(run_id, journal["session_date"]), journal)


def load_round_journal(run_id: str, session_date: str) -> Optional[Dict[str, Any]]:
    data = read_json(round_path(run_id, session_date))
    return data if isinstance(data, dict) else None


def _normalize_intent(intent: Any) -> Optional[Dict[str, Any]]:
    if intent is None:
        return None
    try:
        dump = getattr(intent, "model_dump", None)
        if callable(dump):
            data = dump(mode="json")
        elif isinstance(intent, dict):
            data = dict(intent)
        else:
            return None
    except Exception:
        return None
    return json.loads(json.dumps(data, default=str))


def _recover_intent_from_run_log(
    symbol: str, session_date: str, *, observation_id: str, results_dir: str,
) -> Optional[Dict[str, Any]]:
    """Recover a crashed analysis intent, bound to THIS observation only.

    The run log must be a completed ``long_run`` analysis whose
    ``long_run_observation_id`` metadata matches exactly. Without a match the
    caller re-runs the analysis; a manual/WebUI/other-observation result is
    never borrowed (F12).
    """
    try:
        from tradingagents.run_logger import load_final_state_snapshot

        final_state = load_final_state_snapshot(
            symbol,
            session_date,
            eval_results_dir=results_dir,
            metadata_match={
                # H-09: propagate() writes analysis_source/long_run_observation_id;
                # a "source" key never exists in run-log metadata, so the exact
                # match must use the key that is actually written.
                "analysis_source": "long_run",
                "long_run_observation_id": observation_id,
            },
        )
    except Exception:
        return None
    if not isinstance(final_state, dict):
        return None
    return _normalize_intent(final_state.get("final_trade_intent"))


# -- F10 cooperative stop/window checkpoints ------------------------------
# A stop (SIGTERM) or window end cannot interrupt an in-flight SDK call;
# finite timeouts (F07) bound those. These checkpoints guarantee that once
# control returns, no NEW analysis or broker order begins after the stop/
# cutoff is observed.

STOP_REASON_NONE = None
STOP_REASON_STOP_REQUESTED = "STOP_REQUESTED"
STOP_REASON_WINDOW_ENDED = "WINDOW_ENDED"


def _control_stop_reason(
    deps: LongRunDeps,
    ends_at: Optional[datetime],
) -> Optional[str]:
    """Return why the round must stop now, or None when trading may proceed."""
    if _stop_requested:
        return STOP_REASON_STOP_REQUESTED
    if ends_at is not None:
        now = deps.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = ends_at if ends_at.tzinfo is not None else ends_at.replace(tzinfo=timezone.utc)
        if now >= cutoff:
            return STOP_REASON_WINDOW_ENDED
    return None


def summarize_execution_result(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "success": bool(result.get("success")),
        "broker_attempted": bool(result.get("broker_attempted")),
        "broker_calls": int(result.get("broker_calls") or 0),
        "hold": bool(result.get("hold")),
        "deduped": bool(result.get("deduped")),
        "safety_blocked": bool(result.get("safety_blocked")),
        "safety_reason_codes": [
            str(code) for code in (result.get("safety_reason_codes") or [])
        ],
        "entry_gate_blocked": bool(result.get("entry_gate_blocked")),
        "quarantined": bool(result.get("quarantined")),
        "paused": bool(result.get("paused")),
        "has_unknown": bool(result.get("has_unknown")),
        "error": str(result.get("error") or "")[:300],
    }
    orders = result.get("orders") or []
    summary["orders"] = [
        {"client_order_id": o.get("client_order_id"), "status": o.get("status")}
        for o in orders if isinstance(o, dict)
    ]
    return summary


def _build_graph_config(
    runtime: Dict[str, Any], long_cfg: Dict[str, Any], run_id: Optional[str] = None
) -> Dict[str, Any]:
    config = dict(runtime)
    config["_long_run_analysts"] = list(long_cfg.get("analysts") or [])
    if run_id:
        # F12: bind every long-run analysis log to this exact observation so
        # crash recovery can never borrow another run's decision.
        config["_long_run_observation_id"] = run_id
        config["_analysis_source"] = "long_run"
    try:
        from tradingagents.dataflows.config import set_config

        set_config(config)
    except Exception:
        pass
    return config


def run_daily_round(
    *,
    run_id: str,
    session_date: str,
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    schedule_info: Optional[Dict[str, Any]] = None,
    deps: Optional[LongRunDeps] = None,
    ends_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One trading-day round: recover → snapshot → screen → analyze → trade.

    ``ends_at`` (timezone-aware) enables the F10 cooperative window check:
    at every broker-mutation boundary a round whose window already ended
    stops before starting new analysis or submitting a new order. Existing
    direct unit callers may omit it.
    """
    from tradingagents.agents.schemas import trade_intent_action
    from tradingagents.llm_clients.retry import ProviderFailure

    deps = deps or LongRunDeps()
    service_factory = deps.execution_service_factory or _default_execution_service
    screening_fn = deps.screening_fn or _default_screening
    graph_factory = deps.graph_factory or _default_graph_factory
    broker_factory = deps.broker_client_factory or _default_broker_client
    service = service_factory()
    if ends_at is not None and ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    # R02 Layer 1 — round precheck: a stop already requested or an already
    # ended observation window forbids starting this round at all. Recovery
    # (below) re-POSTs unresolved PENDING/UNKNOWN orders; that is a new
    # broker mutation and must not begin once control has said stop. Runs
    # after the journal gate so a refused round still yields its journal
    # (resume evidence) to the caller.
    # Journal gate (fail-closed, before anything touches the broker): a
    # settled session is never re-run — re-running a MISSED round would
    # submit a stale order, and an unreadable journal must not be silently
    # replaced with a fresh one.
    journal_path = round_path(run_id, session_date)
    journal = load_round_journal(run_id, session_date)
    if journal is not None:
        if journal.get("schema_version") != LONG_RUN_SCHEMA_VERSION:
            raise LongRunStop(
                "STATE_CORRUPT",
                f"round journal schema_version {journal.get('schema_version')!r} "
                f"unsupported for {session_date}",
            )
        status = journal.get("status")
        if status == "COMPLETED":
            return journal  # idempotent: one session runs at most once
        if status in ("MISSED", "STOPPED"):
            raise LongRunStop(
                "SESSION_SETTLED",
                f"{session_date} is already settled as {status}; refusing to re-run",
            )
        if status not in ("PENDING", "RUNNING"):
            raise LongRunStop(
                "STATE_CORRUPT",
                f"round journal for {session_date} has unknown status {status!r}",
            )
    elif journal_path.exists():
        raise LongRunStop(
            "STATE_CORRUPT",
            f"round journal for {session_date} exists but is unreadable",
        )

    control = _control_stop_reason(deps, ends_at)
    if control:
        # Nothing was mutated; yield the journal (resume evidence) so the
        # outer loop records INTERRUPTED/finalizes the window normally.
        return journal

    # R01: this round's runtime must be the installed global execution
    # config BEFORE any path below can mutate the broker. Until now the
    # set_config() call only happened in _build_graph_config (after
    # recovery), so startup_recover() ran against whatever global config
    # was left from a previous process — a stale manual-mode config let it
    # resubmit a gated opening without the Phase C entry gate.
    _apply_runtime_config(runtime)
    _validate_long_run_execution_config(runtime)

    # Step 1 — recover first; unsafe state stops the whole observation.
    # R02 Layer 2: the stop/window authority is re-checked inside recovery
    # itself, immediately before any resubmit POST — the outer precheck
    # above cannot see a stop that arrives while recovery's GET lookups run.
    def _recovery_can_submit() -> bool:
        return _control_stop_reason(deps, ends_at) is None

    # N15: a fresh round has no journal yet. Create and save the empty
    # PENDING journal BEFORE recovery, so recovery/deadline mutation
    # evidence survives even when screening later fails before symbols are
    # known. An empty symbols map is never read as a completed round.
    if journal is None:
        journal = new_round_journal(
            session_date, [],
            scheduled_at=(schedule_info or {}).get("effective_at", ""),
            schedule_adjustment=(schedule_info or {}).get("schedule_adjustment", "NONE"),
        )
        save_round_journal(run_id, journal)

    def _record_maintenance(section: str, summary: Dict[str, Any]) -> None:
        # N15: persist the maintenance summary immediately after the call
        # returns and BEFORE any raise or the next step, so the evidence is
        # on disk even when the round hard-stops right after.
        journal.setdefault("maintenance_execution", {})[section] = summary
        save_round_journal(run_id, journal)

    try:
        recovery = service.startup_recover(can_submit=_recovery_can_submit)
    except Exception as exc:
        _record_maintenance("recovery", {
            "broker_calls": 0, "submit_calls": 0, "cancel_calls": 0,
            "submitted_symbols": [], "has_unknown": False, "paused": True,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        })
        raise LongRunStop("RECOVERY_FAILED", f"startup_recover raised: {exc}")
    _record_maintenance(
        "recovery", recovery.get("recovery_maintenance") or _empty_maintenance_summary()
    )
    if not recovery.get("success"):
        raise LongRunStop(
            "RECOVERY_UNSAFE",
            f"execution recovery forbids unattended continuation: "
            f"{recovery.get('reconciliation_reasons')}",
        )

    deadlines = service.enforce_exit_deadlines(can_submit=_recovery_can_submit)
    _record_maintenance(
        "deadline", deadlines.get("deadline_maintenance") or _empty_maintenance_summary()
    )
    if not deadlines.get("success"):
        raise LongRunStop("DEADLINE_EXIT_UNSAFE", deadlines.get("error", "Deadline exit blocked"))

    # Step 2 — pre-round account snapshot.
    try:
        pre_snapshot = capture_account_snapshot(broker_factory())
    except Exception as exc:
        raise LongRunStop("SNAPSHOT_UNAVAILABLE", f"pre-round snapshot failed: {exc}")
    append_jsonl(run_dir(run_id) / "account_snapshots.jsonl",
                 {"phase": "pre_round", "session": session_date, **pre_snapshot})

    # Step 3 — Phase C screening (fail-closed; a stop ends the observation).
    # F10 checkpoint 1: before screening/model work, re-check stop/window.
    control = _control_stop_reason(deps, ends_at)
    if control:
        # Signal-stop: yield to the outer loop (it records INTERRUPTED).
        # Window-end: the outer loop finalizes the observation normally.
        return journal  # may be None; nothing was mutated beyond recovery

    # F19: the daily LLM budget gates NEW LLM work, not already-authorized
    # broker execution. A fresh round (or one still holding PENDING/ANALYZING
    # symbols) must pass the gate before any screening invocation; a resume
    # whose remaining work is only ANALYZED/EXECUTING execution consumes no
    # tokens and proceeds under the execution/recovery safety rules.
    needs_new_llm_work = journal is None or any(
        (entry or {}).get("status") in (SYMBOL_PENDING, SYMBOL_ANALYZING)
        for entry in (journal.get("symbols") or {}).values()
    )

    # F19: an execution-only resume whose journal already proves the round's
    # selection (a persisted screening summary) and holds no PENDING/ANALYZING
    # symbol must not re-enter the screening pipeline — a lost, expired or
    # unreadable selection cache would otherwise issue fresh Screening LLM
    # requests even under an exhausted budget. The journal's own
    # screening/symbols/trade_intent state is the resume authority; the
    # execution below still passes the unchanged Phase C entry gate.
    # A journal whose screening summary is missing is never trusted: it
    # re-enters screening behind the same budget gate below.
    resume_without_new_llm = (
        journal is not None
        and bool(journal.get("screening"))
        and not needs_new_llm_work
    )

    if not resume_without_new_llm:
        try:
            from tradingagents.safety import get_safety_guard

            verdict = get_safety_guard().check_llm_budget()
            if not verdict.allowed:
                raise LongRunStop(
                    "LLM_BUDGET_EXHAUSTED",
                    f"before screening: {'; '.join(verdict.reasons)}",
                )
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop(
                "LLM_BUDGET_EXHAUSTED", f"budget check unavailable: {exc}"
            )
        plan = _screening_with_audit_scope(
            screening_fn, runtime, run_id, session_date
        )
        if getattr(plan, "stopped", False):
            raise LongRunStop(
                "SCREENING_STOPPED",
                f"screening stopped the round: {plan.stop_reason_text()}",
            )
    else:
        plan = None

    # N15: the round journal already exists (created before recovery for
    # fresh rounds, or loaded for a resume); its symbols are filled in from
    # the screening result below via setdefault.
    journal["status"] = "RUNNING"
    if not journal.get("started_at"):
        journal["started_at"] = utc_now_iso()

    if plan is None:
        # Execution-only resume: the persisted screening summary stays the
        # authority; never rewrite it from a re-run selection.
        save_round_journal(run_id, journal)
    else:
        # Step 4 — persist screening summary (metadata only, no second cache).
        top20 = getattr(plan, "top20", []) or []
        journal["screening"] = {
            "selection_date": getattr(plan, "selection_date", None),
            "as_of": getattr(plan, "as_of", None),
            "cached": bool(getattr(plan, "cached", False)),
            "top20": [
                {"symbol": e.get("symbol"), "rank": e.get("rank"),
                 "score": e.get("screening_score"), "reason": e.get("short_reason")}
                for e in top20 if isinstance(e, dict)
            ],
            "overlap_holdings": list(getattr(plan, "overlap_holdings", []) or []),
            "extra_holdings": list(getattr(plan, "extra_holdings", []) or []),
            "blocked_holdings": list(getattr(plan, "blocked_holdings", []) or []),
            "deep_analysis_set": list(getattr(plan, "deep_analysis_set", []) or []),
            "description": sanitize_for_log(getattr(plan, "screening_description", "")),
        }
        for symbol in journal["screening"]["deep_analysis_set"]:
            journal["symbols"].setdefault(symbol, {
                "status": SYMBOL_PENDING, "analysis_run_ref": None, "signal": None,
                "trade_intent": None, "execution_result_summary": None,
            })
        save_round_journal(run_id, journal)
        log_event(run_id, "round_screening",
                  {"session": session_date, "cached": journal["screening"]["cached"],
                   "universe": len(journal["screening"]["deep_analysis_set"])})

    # Steps 5-6 — serial per-symbol analysis + shared auto-trade execution.
    graph_config = _build_graph_config(runtime, long_cfg, run_id=run_id)
    notional = float(long_cfg.get("base_trade_notional_usd") or 0)
    graph = None
    stop_reason: Optional[str] = None

    def _execute_symbol(symbol: str, intent: Dict[str, Any], *, _reentry: bool = False) -> None:
        # F10 checkpoint 6: immediately before broker execution. A stop that
        # became observable while the analysis ran must prevent the order.
        control = _control_stop_reason(deps, ends_at)
        if control:
            nonlocal stop_reason
            if control == STOP_REASON_WINDOW_ENDED and not _reentry:
                journal["symbols"][symbol]["execution_result_summary"] = {
                    "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                }
                journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                journal["symbols"][symbol]["status"] = SYMBOL_DONE  # analyzed, not traded
                save_round_journal(run_id, journal)
            stop_reason = control
            return
        journal["symbols"][symbol]["status"] = SYMBOL_EXECUTING
        save_round_journal(run_id, journal)
        try:
            result = _execute_intent(
                deps, service, symbol, intent, notional,
                run_id=run_id, session_date=session_date,
                allow_shorts=bool(runtime.get("allow_shorts", False)),
                can_submit=_recovery_can_submit,
            )
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop("EXECUTION_AMBIGUOUS",
                              f"{symbol}: execution failed: {exc}")
        _record_execution(journal, run_id, symbol, result)
        _check_execution_hard_stop(symbol, result)

    for symbol in list(journal["symbols"]):
        entry = journal["symbols"][symbol]
        status = entry.get("status")

        if status == SYMBOL_DONE:
            continue  # Case E: never analyze or execute twice for one session.
        if status == SYMBOL_FAILED:
            continue

        # F10 checkpoint 2: before starting each symbol's work.
        control = _control_stop_reason(deps, ends_at)
        if control:
            stop_reason = control
            break

        # Case D (resume): re-enter execution with the identical decision
        # identity; the durable outbox dedupes without a second broker POST.
        if status == SYMBOL_EXECUTING:
            # F10 checkpoint 5: before re-entering EXECUTING recovery.
            control = _control_stop_reason(deps, ends_at)
            if control:
                stop_reason = control
                break
            intent = entry.get("trade_intent")
            if not intent:
                entry["status"] = SYMBOL_FAILED
                entry["execution_result_summary"] = {"error": "EXECUTING without intent"}
                save_round_journal(run_id, journal)
                continue
            try:
                recovery = service.startup_recover(can_submit=_recovery_can_submit)
                if not recovery.get("success"):
                    raise LongRunStop("RECOVERY_UNSAFE",
                                      f"re-entry recovery unsafe: {recovery.get('reconciliation_reasons')}")
                result = _execute_intent(
                    deps, service, symbol, intent, notional,
                    run_id=run_id, session_date=session_date,
                    allow_shorts=bool(runtime.get("allow_shorts", False)),
                    can_submit=_recovery_can_submit,
                )
            except LongRunStop:
                raise
            except Exception as exc:
                raise LongRunStop("EXECUTION_AMBIGUOUS",
                                  f"{symbol}: re-entry failed: {exc}")
            _record_execution(journal, run_id, symbol, result)
            _check_execution_hard_stop(symbol, result)
            continue

        # Case C: reuse the exact persisted intent, never re-ask the LLM.
        if status == SYMBOL_ANALYZED:
            intent = entry.get("trade_intent")
            if not intent:
                entry["status"] = SYMBOL_PENDING
            else:
                # F10 checkpoint 4: a persisted ANALYZED intent must be
                # re-checked before executing on resume.
                control = _control_stop_reason(deps, ends_at)
                if control:
                    if control == STOP_REASON_WINDOW_ENDED:
                        entry["execution_result_summary"] = {
                            "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                        }
                        entry["status"] = SYMBOL_DONE
                        journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                        save_round_journal(run_id, journal)
                        stop_reason = STOP_REASON_WINDOW_ENDED
                        break
                    stop_reason = control
                    break
                _execute_symbol(symbol, intent)
                continue

        # Case B: ANALYZING means the previous process died before writing
        # ANALYZED. Broker execution is forbidden before ANALYZED, so reuse a
        # provably completed run — of THIS observation only (F12) — or safely
        # re-run the analysis.
        if status == SYMBOL_ANALYZING:
            recovered = _recover_intent_from_run_log(
                symbol, session_date,
                observation_id=run_id,
                results_dir=str(runtime.get("results_dir") or "eval_results"),
            )
            if recovered:
                entry["trade_intent"] = recovered
                entry["signal"] = trade_intent_action(recovered)
                entry["status"] = SYMBOL_ANALYZED
                save_round_journal(run_id, journal)
                log_event(run_id, "symbol_recovered",
                          {"symbol": symbol, "signal": entry["signal"]})
                _execute_symbol(symbol, recovered)
                continue
            entry["status"] = SYMBOL_PENDING

        # Case A: analyze normally (PENDING, or ANALYZING without proof).
        if entry.get("status") != SYMBOL_PENDING:
            continue
        # F19 checkpoint: earlier symbols may have consumed the rest of the
        # day's budget, so every fresh analysis re-checks before new LLM work.
        try:
            from tradingagents.safety import get_safety_guard

            verdict = get_safety_guard().check_llm_budget()
            if not verdict.allowed:
                raise LongRunStop(
                    "LLM_BUDGET_EXHAUSTED",
                    f"{symbol}: {'; '.join(verdict.reasons)}",
                )
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop(
                "LLM_BUDGET_EXHAUSTED", f"budget check unavailable: {exc}"
            )
        entry["status"] = SYMBOL_ANALYZING
        symbol_started = utc_now_iso()
        # Persist the analysis-start marker BEFORE propagate so a crash leaves
        # an explicit start identity/time. It alone never claims completion.
        entry["analysis_run_ref"] = symbol_started
        save_round_journal(run_id, journal)
        try:
            if graph is None:
                graph = graph_factory(graph_config)
            final_state, _signal = graph.propagate(symbol, session_date)
            # F10 checkpoint 3: immediately after analysis returns, BEFORE
            # persisting a tradeable intent or starting broker execution.
            control = _control_stop_reason(deps, ends_at)
            if control:
                if control == STOP_REASON_WINDOW_ENDED:
                    # Keep the analysis result for audit, but never trade it:
                    # the authorized window ended while this symbol ran.
                    entry["trade_intent"] = _normalize_intent(
                        (final_state or {}).get("final_trade_intent")
                    )
                    entry["signal"] = None
                    entry["execution_result_summary"] = {
                        "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                    }
                    journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                    save_round_journal(run_id, journal)
                    log_event(run_id, "symbol_window_ended", {"symbol": symbol})
                    stop_reason = STOP_REASON_WINDOW_ENDED
                    break
                # STOP_REQUESTED: persist resume-safe journal state and yield
                # to the outer loop without marking remaining symbols FAILED.
                save_round_journal(run_id, journal)
                stop_reason = STOP_REASON_STOP_REQUESTED
                break
            intent = _normalize_intent((final_state or {}).get("final_trade_intent"))
            if not intent:
                from tradingagents.execution.service import validate_trade_intent

                _, err = validate_trade_intent(None)
                entry["status"] = SYMBOL_DONE  # fail closed: completed, no trade.
                entry["signal"] = None
                entry["execution_result_summary"] = {
                    "no_trade": True, "error": f"no schema-valid TradeIntent ({err})",
                }
                save_round_journal(run_id, journal)
                log_event(run_id, "symbol_no_intent", {"symbol": symbol})
                continue
            entry["trade_intent"] = intent
            entry["signal"] = trade_intent_action(intent)
            entry["status"] = SYMBOL_ANALYZED
            entry["analysis_run_ref"] = symbol_started
            save_round_journal(run_id, journal)
            log_event(run_id, "symbol_analyzed",
                      {"symbol": symbol, "signal": entry["signal"]})
        except ProviderFailure as exc:
            entry["status"] = SYMBOL_FAILED
            save_round_journal(run_id, journal)
            raise LongRunStop("PROVIDER_FAILURE", f"{symbol}: {exc}")
        except LongRunStop:
            raise
        except Exception as exc:
            entry["status"] = SYMBOL_FAILED
            entry["execution_result_summary"] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]
            }
            save_round_journal(run_id, journal)
            log_event(run_id, "symbol_failed",
                      {"symbol": symbol, "error": str(exc)[:200]})
            continue

        # Fresh ANALYZED → execute immediately (same pass, no re-loop needed).
        _execute_symbol(symbol, entry["trade_intent"])
        if stop_reason:
            break

    # F10: a stop observed at a checkpoint yields to the outer loop without
    # finalizing this round. The journal keeps its partial evidence and the
    # remaining symbols stay PENDING (never FAILED), so a later resume
    # continues exactly where this process stopped. A window end is recorded
    # on the journal for the final report; the outer loop may still finalize
    # the observation COMPLETED because its window ended normally.
    if stop_reason == STOP_REASON_STOP_REQUESTED:
        journal["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        log_event(run_id, "round_interrupted", {"session": session_date,
                                                "reason": "STOP_REQUESTED"})
        return journal
    if stop_reason == STOP_REASON_WINDOW_ENDED:
        journal["status"] = "RUNNING"
        journal["stop_reason"] = journal.get("stop_reason") or "WINDOW_ENDED_DURING_ROUND"
        save_round_journal(run_id, journal)
        log_event(run_id, "round_window_ended", {"session": session_date})
        return journal

    # Step 8 — post-round account snapshot.
    try:
        post_snapshot = capture_account_snapshot(broker_factory())
    except Exception as exc:
        raise LongRunStop("SNAPSHOT_UNAVAILABLE", f"post-round snapshot failed: {exc}")
    append_jsonl(run_dir(run_id) / "account_snapshots.jsonl",
                 {"phase": "post_round", "session": session_date, **post_snapshot})

    # Step 9 — daily operations report (existing renderer + round metadata).
    try:
        from tradingagents.daily_report import write_daily_report

        md_path, _html_path = write_daily_report(
            day=session_date,
            output_dir=str(run_dir(run_id) / "daily_reports"),
            config=runtime,
            extra_header=[
                f"Phase-D round {session_date} "
                f"(schedule_adjustment={journal.get('schedule_adjustment', 'NONE')}, "
                f"screening={'cached' if journal['screening'].get('cached') else 'fresh'})",
            ],
        )
        journal["daily_report"] = md_path
    except LongRunStop:
        raise
    except Exception as exc:
        journal["daily_report_error"] = f"{type(exc).__name__}: {exc}"[:200]

    # Step 10 — complete the session exactly once.
    journal["status"] = "COMPLETED"
    journal["finished_at"] = utc_now_iso()
    save_round_journal(run_id, journal)
    log_event(run_id, "round_completed", {"session": session_date})
    return journal


SCREENING_AUDIT_SYMBOL = "__SCREENING__"


def _screening_with_audit_scope(
    screening_fn: Callable[..., Any],
    runtime: Dict[str, Any],
    run_id: str,
    session_date: str,
) -> Any:
    """F15: run one screening round inside a normal audit-run scope.

    Screening happens before any per-symbol run exists, so its token usage
    would otherwise be lost from both the daily budget attribution and the
    final cost report. The scope is a regular RunAuditLogger run tagged
    ``source="long_run_screening"`` with the exact observation id, so F13's
    metadata filter can attribute it; no new persistence format.
    """
    from tradingagents.run_logger import get_run_audit_logger

    audit = get_run_audit_logger()
    scope_started = False
    try:
        audit.start_run(
            symbol=SCREENING_AUDIT_SYMBOL,
            trade_date=session_date,
            config=runtime,
            metadata={
                "source": "long_run_screening",
                "long_run_observation_id": run_id,
            },
        )
        scope_started = True
    except Exception as exc:
        # The audit scope must never block the actual screening work.
        print(f"[RUN_LOG] Screening audit scope unavailable: {exc}")

    try:
        plan = screening_fn(runtime, False)
    except Exception as exc:
        if scope_started:
            try:
                audit.finish_run(
                    symbol=SCREENING_AUDIT_SYMBOL,
                    status="failed",
                    error_message=str(exc)[:300],
                )
            except Exception:
                pass
        raise

    if scope_started:
        status = "stopped" if getattr(plan, "stopped", False) else "completed"
        error_message = plan.stop_reason_text() if getattr(plan, "stopped", False) else None
        try:
            audit.finish_run(
                symbol=SCREENING_AUDIT_SYMBOL,
                status=status,
                error_message=error_message,
            )
        except Exception:
            pass
    return plan


def _execute_intent(deps, service, symbol, intent, notional, *, run_id, session_date,
                    allow_shorts=False, can_submit: Optional[Callable[[], bool]] = None):
    from tradingagents.execution.auto_trade import execute_auto_trade

    return execute_auto_trade(
        ticker=symbol,
        trade_intent=intent,
        base_trade_notional_usd=notional,
        allow_shorts=bool(allow_shorts),
        config=None,
        execution_service=service,
        decision_id=f"{run_id}-{session_date}-{symbol}",
        run_id=f"{run_id}-{session_date}-{symbol}",
        # N03: the stop/window authority rides down to the final opening-POST
        # boundary, where it is re-checked after every blocking broker GET.
        can_submit=can_submit,
    )


def _record_execution(journal, run_id, symbol, result) -> None:
    entry = journal["symbols"][symbol]
    entry["execution_result_summary"] = summarize_execution_result(result)
    entry["signal"] = entry.get("signal")
    if result.get("has_unknown") or result.get("paused"):
        entry["status"] = SYMBOL_EXECUTING  # still ambiguous: resume re-enters Case D.
        save_round_journal(run_id, journal)
        return
    entry["status"] = SYMBOL_DONE
    save_round_journal(run_id, journal)
    log_event(run_id, "symbol_executed",
              {"symbol": symbol, **entry["execution_result_summary"]})


def _check_execution_hard_stop(symbol: str, result: Dict[str, Any]) -> None:
    """Unknown/ambiguous broker state or a paused account stops everything.

    N08: safety circuit breakers (daily loss, drawdown, consecutive
    rejections) are distinguished by their stable reason codes — the
    observation stops with SAFETY_CIRCUIT_BREAKER instead of continuing to
    analyze/queue. Single-order refusals (notional cap, concentration) do
    NOT stop the observation.
    """
    if result.get("has_unknown"):
        raise LongRunStop(
            "EXECUTION_AMBIGUOUS",
            f"{symbol}: ambiguous broker outcome requires operator review",
        )
    if result.get("paused"):
        raise LongRunStop(
            "ACCOUNT_PAUSED",
            f"{symbol}: account paused: {result.get('error') or result.get('reconciliation_reasons')}",
        )
    breaker_codes: List[str] = []
    try:
        from tradingagents.safety.guardrails import OBSERVATION_HALT_CODES

        breaker_codes = [
            str(code)
            for code in (result.get("safety_reason_codes") or [])
            if str(code) in OBSERVATION_HALT_CODES and str(code) != "KILL_SWITCH"
        ]
    except Exception:
        breaker_codes = []
    if breaker_codes:
        raise LongRunStop(
            "SAFETY_CIRCUIT_BREAKER",
            f"{symbol}: safety circuit breaker engaged "
            f"({', '.join(breaker_codes)}): {result.get('error') or 'no detail'}",
        )
    try:
        from tradingagents.safety import get_safety_guard

        guard = get_safety_guard()
        status = guard.status() if hasattr(guard, "status") else {}
        if isinstance(status, dict) and status.get("kill_switch_active"):
            raise LongRunStop(
                "KILL_SWITCH",
                f"kill switch engaged: {status.get('kill_switch_reason') or 'no reason'}",
            )
    except LongRunStop:
        raise
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Observation loop (30-calendar-day scheduler + resume + hard-stop)
# ---------------------------------------------------------------------------

_stop_requested = False


def _install_signal_handlers() -> None:
    global _stop_requested

    def _handler(signum, _frame):
        global _stop_requested
        _stop_requested = True

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass
    # KeyboardInterrupt is handled by the caller; SIGINT default is kept.


def request_stop() -> bool:
    return _stop_requested


def sweep_missed_sessions(
    *, run_id: str, expected: List[str], today: str,
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


# F-03: a transient calendar/scheduling failure must not scrap a 30-day
# unattended observation. Fixed bounded retry only — no exponential backoff,
# no failure windows, no circuit breaker. After the final attempt the failure
# still fails closed (CALENDAR_UNAVAILABLE → STOPPED).
SCHEDULER_RETRY_ATTEMPTS = 3
SCHEDULER_RETRY_DELAY_SECONDS = 5.0


def _run_scheduler_operation_with_retry(
    operation: Callable[[], Any],
    *,
    deps: LongRunDeps,
    run_id: str,
    label: str,
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


def run_observation_loop(
    state: Dict[str, Any],
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    deps: Optional[LongRunDeps] = None,
) -> Dict[str, Any]:
    """Block until the window ends, a hard stop fires, or a signal arrives."""
    deps = deps or LongRunDeps()
    run_id = state["run_id"]
    ends_at = datetime.fromisoformat(state["ends_at"])
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    _install_signal_handlers()

    while True:
        if _stop_requested:
            state["status"] = "INTERRUPTED"
            save_active_state(state)
            log_event(run_id, "interrupted", {})
            print(
                "\n[Phase-D] Interrupted. Resume with the same command:\n"
                "  python -m cli.main long-run"
            )
            return {"outcome": "interrupted", "run_id": run_id}

        now = deps.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now >= ends_at:
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="COMPLETED")

        eastern = eastern_now(now)

        # F-03: sweep + due-session selection form one scheduler operation.
        # sweep_missed_sessions() is idempotent over the persisted journal,
        # so re-running the pair after a transient calendar error is safe.
        def _scheduling_operation():
            sweep_missed_sessions(
                run_id=run_id,
                expected=[s for s in (state.get("expected_sessions") or [])
                          if s < ends_at.astimezone(eastern.tzinfo).date().isoformat()],
                today=eastern.date().isoformat(),
            )
            return next_due_session(
                now=now,
                run_time_et=str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                started_at=datetime.fromisoformat(state["started_at"]),
                ends_at=ends_at,
                settled=settled_sessions(run_id),
                calendar_client=deps.calendar_client,
                calendar_rows=deps.calendar_rows,
            )

        try:
            target = _run_scheduler_operation_with_retry(
                _scheduling_operation,
                deps=deps, run_id=run_id,
                label="scheduling/session proof",
            )
        except LongRunStop as exc:
            # A hard stop at the scheduling stage (calendar authority
            # unavailable after bounded retry, corrupt round journal) must
            # end the observation exactly like a mid-round stop: STOPPED
            # state, partial final report, active window closed. Raising
            # past the loop would leave the window RUNNING forever with no
            # report to inspect.
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="STOPPED",
                                        stop_code=exc.code, stop_detail=exc.detail)
        except Exception as exc:
            stop = LongRunStop("CALENDAR_UNAVAILABLE",
                               f"scheduling/session proof failed: {exc}")
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="STOPPED",
                                        stop_code=stop.code, stop_detail=stop.detail)

        if target is None:
            # No session left before ends_at: wait out the window in chunks.
            remaining = (ends_at - now).total_seconds()
            deps.sleep_fn(min(60.0, max(1.0, remaining)))
            continue
        if not target.get("due"):
            effective = datetime.fromisoformat(target["effective_at"])
            wait = (effective - now).total_seconds()
            if wait > 0:
                deps.sleep_fn(min(60.0, wait))
                continue
        # Due now: run the session exactly once (resume-safe journal).
        existing = load_round_journal(run_id, target["session_date"])
        if existing is not None and existing.get("status") in TERMINAL_ROUND_STATUSES:
            continue
        # R13 scheduler layer: a never-started session whose authoritative
        # close has already passed is settled MISSED here — it must not run
        # as an overdue round (stale data, after-hours entry) and, being
        # settled, can never be replayed on a later day.
        # F-03: the calendar-backed close proof gets the same bounded retry;
        # persistent failure fails closed instead of crashing the loop.
        try:
            settled_here = _run_scheduler_operation_with_retry(
                lambda: mark_session_missed_after_close(
                    run_id=run_id,
                    session_date=target["session_date"],
                    now=now,
                    run_time_et=str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                    calendar_client=deps.calendar_client,
                    calendar_rows=deps.calendar_rows,
                ),
                deps=deps, run_id=run_id,
                label="mark_session_missed_after_close",
            )
        except LongRunStop as exc:
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="STOPPED",
                                        stop_code=exc.code, stop_detail=exc.detail)
        if settled_here:
            continue
        try:
            run_daily_round(
                run_id=run_id,
                session_date=target["session_date"],
                long_cfg=long_cfg,
                runtime=runtime,
                schedule_info=target,
                deps=deps,
                ends_at=ends_at,
            )
        except LongRunStop as exc:
            journal = load_round_journal(run_id, target["session_date"])
            if journal is not None and journal.get("status") != "COMPLETED":
                journal["status"] = "STOPPED"
                journal["stop_reason"] = f"{exc.code}: {exc.detail}"[:300]
                journal["finished_at"] = utc_now_iso()
                save_round_journal(run_id, journal)
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="STOPPED",
                                        stop_code=exc.code, stop_detail=exc.detail)
        except Exception as exc:
            # F-04 fence: an ordinary bug/SDK failure inside a round is still
            # observation evidence — it must finalize STOPPED (journal kept,
            # report written) instead of escaping and leaving the window
            # RUNNING with no report. BaseException (KeyboardInterrupt,
            # SystemExit) deliberately propagates.
            detail = f"{type(exc).__name__}: {exc}"[:300]
            journal = load_round_journal(run_id, target["session_date"])
            if journal is not None and journal.get("status") != "COMPLETED":
                journal["status"] = "STOPPED"
                journal["stop_reason"] = f"UNEXPECTED_ROUND_ERROR: {detail}"[:300]
                journal["finished_at"] = utc_now_iso()
                save_round_journal(run_id, journal)
            return finalize_observation(state, long_cfg, runtime, deps,
                                        final_status="STOPPED",
                                        stop_code="UNEXPECTED_ROUND_ERROR",
                                        stop_detail=detail)


# ---------------------------------------------------------------------------
# Final 30-day report
# ---------------------------------------------------------------------------

def _load_snapshots(run_id: str) -> List[Dict[str, Any]]:
    path = run_dir(run_id) / "account_snapshots.jsonl"
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def _load_rounds(run_id: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Readable round journals, plus session dates whose journal exists but
    could not be parsed (preserved as-is, never guessed through)."""
    rounds_dir = run_dir(run_id) / "rounds"
    rounds = []
    unreadable = []
    if not rounds_dir.is_dir():
        return rounds, unreadable
    for path in sorted(rounds_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict):
            rounds.append(data)
        else:
            unreadable.append(path.stem)
    return rounds, unreadable


def _sum_unrealized(positions: Any) -> Optional[float]:
    if not positions:
        return None
    total = 0.0
    seen = False
    for row in positions:
        value = (row or {}).get("unrealized_pl")
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def compute_drawdown(equities: List[float]) -> Dict[str, Any]:
    if not equities:
        return {"peak": None, "trough": None, "max_drawdown": None}
    peak = equities[0]
    max_dd = 0.0
    trough = equities[0]
    for value in equities:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (value - peak) / peak
            if drawdown < max_dd:
                max_dd = drawdown
                trough = value
    return {"peak": peak, "trough": trough, "max_drawdown": max_dd}


def aggregate_final_report(
    state: Dict[str, Any],
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministic Markdown+JSON inputs from persisted evidence only."""
    run_id = state["run_id"]
    snapshots = _load_snapshots(run_id)
    rounds, unreadable_journals = _load_rounds(run_id)
    expected = list(state.get("expected_sessions") or [])
    completed = [r["session_date"] for r in rounds if r.get("status") == "COMPLETED"]
    missed = [r["session_date"] for r in rounds if r.get("status") == "MISSED"]
    stopped = [r["session_date"] for r in rounds if r.get("status") == "STOPPED"]

    equities = [float(s.get("equity") or 0) for s in snapshots]
    start_equity = equities[0] if equities else None
    # F14: ending values come ONLY from a fresh phase="final" snapshot taken
    # at observation end. The last post_round snapshot can be days stale; a
    # missing/failed final capture is reported as unknown, never labeled
    # with the stale round value.
    final_snapshot = next(
        (s for s in reversed(snapshots) if s.get("phase") == "final"), None,
    )
    last_observed_equity = next(
        (float(s.get("equity")) for s in reversed(snapshots)
         if s.get("phase") == "post_round" and s.get("equity") is not None),
        None,
    )
    end_equity = final_snapshot.get("equity") if final_snapshot else None
    total_return = (
        (end_equity - start_equity) / start_equity
        if start_equity not in (None, 0) and end_equity is not None else None
    )
    drawdown = compute_drawdown(equities)
    starting_positions = next(
        (s.get("positions") for s in snapshots if s.get("phase") == "pre_round"), None,
    )
    ending_positions = final_snapshot.get("positions") if final_snapshot else None
    ending_snapshot_available = final_snapshot is not None

    # Decisions from round journals (authoritative for the observation) plus
    # the normal run logs for token/error detail.
    symbol_rows: List[Dict[str, Any]] = []
    signal_counts: Dict[str, int] = {}
    provider_failures = 0
    for journal in rounds:
        for symbol, entry in (journal.get("symbols") or {}).items():
            signal = entry.get("signal")
            if signal:
                signal_counts[str(signal)] = signal_counts.get(str(signal), 0) + 1
            summary = entry.get("execution_result_summary") or {}
            if summary.get("error", "").startswith("LLM ") or "provider" in summary.get("error", "").lower():
                provider_failures += 1
            symbol_rows.append({
                "session": journal.get("session_date"), "symbol": symbol,
                "status": entry.get("status"), "signal": signal,
                "broker_calls": (summary or {}).get("broker_calls", 0),
                "error": (summary or {}).get("error", ""),
            })
    symbol_rows.sort(key=lambda r: (r["session"] or "", r["symbol"] or ""))

    # Screening: fresh vs cached scans, daily Top20, turnover.
    fresh_scans = sum(1 for r in rounds
                      if r.get("status") == "COMPLETED" and not (r.get("screening") or {}).get("cached"))
    cache_reuses = sum(1 for r in rounds
                       if r.get("status") == "COMPLETED" and (r.get("screening") or {}).get("cached"))
    top20_by_day = {r["session_date"]: [e.get("symbol") for e in (r.get("screening") or {}).get("top20", [])]
                    for r in rounds if r.get("status") == "COMPLETED"}
    turnover: List[Dict[str, Any]] = []
    previous: set = set()
    for day in sorted(top20_by_day):
        current = set(top20_by_day[day])
        if previous:
            turnover.append({"session": day,
                             "entered": sorted(current - previous),
                             "exited": sorted(previous - current)})
        previous = current

    # Broker execution tallies from journals (execution.db stays authoritative).
    # N15: round maintenance (recovery/deadline) mutations are part of the
    # same tallies — broker_calls sums per-symbol execution AND maintenance;
    # submitted_symbols counts distinct symbols with actual submit events,
    # never ledger-row counts (a bracket POST may create parent + 2 child rows).
    exec_tally = {"submitted_symbols": 0, "broker_calls": 0, "holds": 0,
                  "safety_blocks": 0, "entry_gate_blocks": 0,
                  "quarantined": 0, "unknown": 0, "deduped": 0,
                  "maintenance_broker_calls": 0}
    submitted_symbol_names: set = set()
    round_durations: List[Dict[str, Any]] = []
    for journal in rounds:
        maintenance = journal.get("maintenance_execution") or {}
        for section in ("recovery", "deadline"):
            summary = maintenance.get(section) or {}
            maintenance_calls = int(summary.get("broker_calls") or 0)
            exec_tally["broker_calls"] += maintenance_calls
            exec_tally["maintenance_broker_calls"] += maintenance_calls
            if int(summary.get("submit_calls") or 0) > 0:
                for name in (summary.get("submitted_symbols") or []):
                    submitted_symbol_names.add(str(name).upper())
            if summary.get("has_unknown"):
                exec_tally["unknown"] += 1
        for symbol, entry in (journal.get("symbols") or {}).items():
            summary = entry.get("execution_result_summary") or {}
            if entry.get("status") != SYMBOL_DONE and not summary:
                continue
            if summary.get("no_trade"):
                exec_tally["holds"] += 1
                continue
            if summary.get("broker_calls"):
                submitted_symbol_names.add(symbol)
                exec_tally["broker_calls"] += int(summary["broker_calls"])
            elif (entry.get("signal") or "") == "HOLD":
                exec_tally["holds"] += 1
            if summary.get("safety_blocked"):
                exec_tally["safety_blocks"] += 1
            if summary.get("entry_gate_blocked"):
                exec_tally["entry_gate_blocks"] += 1
            if summary.get("quarantined"):
                exec_tally["quarantined"] += 1
            if summary.get("has_unknown"):
                exec_tally["unknown"] += 1
            if summary.get("deduped"):
                exec_tally["deduped"] += 1
        if journal.get("started_at") and journal.get("finished_at"):
            try:
                start = datetime.fromisoformat(journal["started_at"])
                end = datetime.fromisoformat(journal["finished_at"])
                round_durations.append({
                    "session": journal.get("session_date"),
                    "seconds": round((end - start).total_seconds(), 1),
                })
            except ValueError:
                pass
    exec_tally["submitted_symbols"] = len(submitted_symbol_names)

    # LLM operations from existing cost aggregation (best-effort, no estimates).
    # F13: scoped to THIS observation's exact run-log metadata so manual,
    # old, or concurrent-observation runs can never enter these totals.
    llm_ops: Dict[str, Any] = {"available": False}
    try:
        from tradingagents.llm_cost import aggregate_costs, scan_run_costs

        records = scan_run_costs(
            eval_results_dir=runtime.get("results_dir", "eval_results"),
            overrides=runtime.get("llm_pricing_per_million"),
            metadata_match={"long_run_observation_id": run_id},
        )
        totals = aggregate_costs(records)
        llm_ops = {"available": True, "totals": totals.get("totals", {}),
                   "per_day": totals.get("per_day", {}),
                   "unpriced_tokens": totals.get("totals", {}).get("unpriced_tokens", 0)}
    except Exception as exc:
        llm_ops = {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    # Execution DB tallies (best-effort reference; journals stay primary).
    # F13: same resolver as ExecutionService, so the report and execution
    # can never point at different files (env read at call time).
    exec_db: Dict[str, Any] = {"available": False}
    try:
        from tradingagents.execution import ExecutionStore, resolve_execution_db_path

        store = ExecutionStore(
            resolve_execution_db_path(runtime.get("execution_db_path"))
        )
        orders = store.list_all_orders()
        by_status: Dict[str, int] = {}
        for order in orders:
            status = str(order.get("status") or "UNKNOWN").upper()
            by_status[status] = by_status.get(status, 0) + 1
        exec_db = {"available": True, "orders_total": len(orders), "by_status": by_status}
    except Exception as exc:
        exec_db = {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    # Safety: final guard status (best-effort) + deterministic gate tallies.
    safety: Dict[str, Any] = {
        "kill_switch_active": None,
        "kill_switch_reason": "",
        "safety_gate_refusals": exec_tally["safety_blocks"],
        "entry_gate_refusals": exec_tally["entry_gate_blocks"],
        "quarantine_blocks": exec_tally["quarantined"],
    }
    try:
        from tradingagents.safety import get_safety_guard

        status = get_safety_guard().status()
        if isinstance(status, dict):
            safety["kill_switch_active"] = bool(status.get("kill_switch_active"))
            safety["kill_switch_reason"] = str(
                status.get("kill_switch_reason") or "")[:200]
    except Exception:
        pass

    started_at = datetime.fromisoformat(state["started_at"])
    wall_seconds = 0.0
    try:
        wall_seconds = max(
            0.0, (datetime.now(timezone.utc) - started_at).total_seconds()
        )
    except Exception:
        pass

    return {
        "run_id": run_id,
        "baseline_commit": state.get("baseline_commit", "unknown"),
        "started_at": state.get("started_at"),
        "ends_at": state.get("ends_at"),
        "duration_calendar_days": long_cfg.get("duration_calendar_days"),
        "timezone": "US/Eastern",
        "final_status": state.get("status"),
        "stop": state.get("stop"),
        "coverage": {
            "expected_trading_sessions": len(expected),
            "completed_sessions": len(completed),
            "missed_sessions": len(missed),
            "stopped_sessions": len(stopped),
            "unreadable_journals": list(unreadable_journals),
            "completion_rate": (len(completed) / len(expected)) if expected else None,
            "restart_count": int(state.get("restart_count") or 0),
            "wall_clock_seconds": round(wall_seconds, 1),
        },
        "account": {
            "starting_equity": start_equity, "ending_equity": end_equity,
            "absolute_pl": (end_equity - start_equity)
            if start_equity is not None and end_equity is not None else None,
            "total_return": total_return,
            "return_kind": "unadjusted_account_equity_change",
            "return_limitations": [
                "Not adjusted for deposits or withdrawals",
                "May include positions that existed before the observation",
                "Not pure strategy attribution",
                "Not net profitability unless all research and trading costs are included",
            ],
            "peak_equity": drawdown["peak"], "trough_equity": drawdown["trough"],
            "max_drawdown": drawdown["max_drawdown"],
            "starting_cash": snapshots[0].get("cash") if snapshots else None,
            # F14: final cash comes from the phase="final" snapshot too; the
            # last observed round value is kept separately for diagnostics.
            "ending_cash": final_snapshot.get("cash") if final_snapshot else None,
            "last_observed_equity": last_observed_equity,
            "ending_snapshot_available": ending_snapshot_available,
            "starting_positions": starting_positions,
            "ending_positions": ending_positions,
            "ending_unrealized_pl": _sum_unrealized(ending_positions),
            "daily_equity_series": [
                {"at": s.get("at"), "phase": s.get("phase"),
                 "session": s.get("session"), "equity": s.get("equity"),
                 "cash": s.get("cash")}
                for s in snapshots
            ],
        },
        "decisions": {
            "symbol_analyses": len(symbol_rows),
            "signal_counts": signal_counts,
            "provider_failures": provider_failures,
            "per_symbol": symbol_rows,
        },
        "screening": {
            "fresh_scans": fresh_scans, "cache_reuses": cache_reuses,
            "top20_by_day": top20_by_day, "turnover": turnover,
            "stopped_sessions": stopped,
            "held_only_reviews": sorted(
                r["session_date"] for r in rounds
                if (r.get("screening") or {}).get("mode") == "held_review"
            ),
        },
        "safety": safety,
        "execution": exec_tally,
        "execution_db": exec_db,
        "llm_operations": llm_ops,
        "reliability": {
            "round_durations": round_durations,
            "missed_rounds": missed,
            "stopped_rounds": stopped,
            "schedule_adjustments": [
                {"session": r.get("session_date"),
                 "adjustment": r.get("schedule_adjustment", "NONE")}
                for r in rounds if r.get("schedule_adjustment", "NONE") != "NONE"
            ],
        },
        "evidence": {
            "manifest": str(run_dir(run_id) / "manifest.json"),
            "events": str(run_dir(run_id) / "events.jsonl"),
            "snapshots": str(run_dir(run_id) / "account_snapshots.jsonl"),
            "rounds": sorted(str(run_dir(run_id) / "rounds" / f"{r.get('session_date')}.json")
                             for r in rounds),
            "unreadable_round_journals": [
                str(run_dir(run_id) / "rounds" / f"{name}.json")
                for name in unreadable_journals],
            "daily_reports": sorted(str(run_dir(run_id) / "daily_reports" / f"{r.get('session_date')}.md")
                                    for r in rounds if r.get("status") == "COMPLETED"),
        },
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_usd(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def render_final_markdown(report: Dict[str, Any]) -> str:
    """Human-readable 30-day observation report (deterministic)."""
    lines = [
        f"# Phase-D 30-Day Paper Observation — {report['run_id']}",
        "",
        f"- status: **{report['final_status']}**",
        f"- window: {report['started_at']} → {report['ends_at']} "
        f"({report['duration_calendar_days']} calendar days, US/Eastern)",
        f"- baseline commit: `{report['baseline_commit']}`",
    ]
    if report.get("stop"):
        lines.append(
            f"- stop: `{report['stop'].get('code')}` — "
            f"{report['stop'].get('detail', '')}"
        )
    cov = report["coverage"]
    rate = cov['completion_rate']
    lines += [
        "",
        "## Observation coverage",
        f"- expected US trading sessions: {cov['expected_trading_sessions']}",
        f"- completed: {cov['completed_sessions']}",
        f"- missed (process down): {cov['missed_sessions']}",
        f"- stopped: {cov['stopped_sessions']}",
        f"- completion: {_fmt_pct(rate)}",
    ]
    if cov.get("unreadable_journals"):
        lines.append(
            "- unreadable round journals (preserved as-is, operator review "
            f"required): {', '.join(cov['unreadable_journals'])}")
    lines += [
        f"- process restarts: {cov['restart_count']}",
        f"- wall-clock: {cov['wall_clock_seconds']:.0f}s",
        "",
        "## Portfolio / account result (broker snapshots)",
    ]
    acct = report["account"]
    lines += [
        f"- starting equity: {_fmt_usd(acct['starting_equity'])}",
        f"- ending equity: {_fmt_usd(acct['ending_equity'])}",
        f"- absolute P/L: {_fmt_usd(acct['absolute_pl'])}",
        f"- total return: {_fmt_pct(acct['total_return'])}",
    ]
    if not acct.get("ending_snapshot_available", False):
        lines.append(
            "- ending snapshot: unavailable (final broker snapshot failed; "
            f"last post-round equity {_fmt_usd(acct.get('last_observed_equity'))} "
            "is diagnostic only, not final)"
        )
    if acct.get("return_kind"):
        lines.append(f"- return kind: {acct['return_kind']}")
    for limitation in acct.get("return_limitations") or []:
        lines.append(f"- return limitation: {limitation}")
    lines += [
        f"- peak equity: {_fmt_usd(acct['peak_equity'])}",
        f"- trough equity: {_fmt_usd(acct['trough_equity'])}",
        f"- maximum drawdown: {_fmt_pct(acct['max_drawdown'])}",
        f"- starting cash: {_fmt_usd(acct['starting_cash'])}",
        f"- ending cash: {_fmt_usd(acct['ending_cash'])}",
        "",
        "Daily equity series (phase boundaries):",
        "| At | Phase | Session | Equity | Cash |",
        "|---|---|---|---:|---:|",
    ]
    for point in acct["daily_equity_series"]:
        lines.append(
            f"| {point.get('at', '')} | {point.get('phase', '')} "
            f"| {point.get('session', '')} | {_fmt_usd(point.get('equity'))} "
            f"| {_fmt_usd(point.get('cash'))} |"
        )
    lines += ["", "## Decision / analysis result", ""]
    dec = report["decisions"]
    lines += [
        f"- symbol analyses: {dec['symbol_analyses']}",
        f"- signals: {json.dumps(dec['signal_counts'], sort_keys=True)}",
        f"- provider failures: {dec['provider_failures']}",
        "",
        "| Session | Symbol | Status | Signal | Broker calls | Error |",
        "|---|---|---|---|---:|---|",
    ]
    for row in dec["per_symbol"]:
        lines.append(
            f"| {row['session']} | {row['symbol']} | {row['status']} "
            f"| {row['signal'] or '—'} | {row['broker_calls']} "
            f"| {(row['error'] or '')[:80]} |"
        )
    lines += ["", "## Screening result", ""]
    scr = report["screening"]
    lines += [
        f"- fresh full-market scans: {scr['fresh_scans']}",
        f"- same-day cache reuses: {scr['cache_reuses']}",
    ]
    for day in sorted(scr["top20_by_day"]):
        lines.append(f"- {day} Top20: {', '.join(scr['top20_by_day'][day]) or '—'}")
    for change in scr["turnover"]:
        lines.append(
            f"- {change['session']} turnover: +{', '.join(change['entered']) or '—'} "
            f"/ −{', '.join(change['exited']) or '—'}"
        )
    lines += ["", "## Broker execution result", ""]
    exe = report["execution"]
    lines += [
        f"- symbols with broker submissions: {exe['submitted_symbols']}",
        f"- broker POST calls: {exe['broker_calls']}",
        f"- round maintenance broker calls (recovery/deadline): "
        f"{exe.get('maintenance_broker_calls', 0)}",
        f"- HOLD / no-order outcomes: {exe['holds']}",
        f"- safety-gate refusals: {exe['safety_blocks']}",
        f"- entry-gate refusals: {exe['entry_gate_blocks']}",
        f"- quarantined: {exe['quarantined']}",
        f"- unknown/ambiguous: {exe['unknown']}",
        f"- idempotent dedupes: {exe['deduped']}",
    ]
    db = report["execution_db"]
    if db.get("available"):
        lines.append(
            f"- execution.db: {db['orders_total']} orders {db['by_status']}"
        )
    else:
        lines.append(f"- execution.db: unavailable ({db.get('error', '')})")
    lines += ["", "## Safety result", ""]
    saf = report.get("safety", {})
    lines += [
        f"- kill switch active: {saf.get('kill_switch_active')}",
        f"- kill switch reason: {saf.get('kill_switch_reason') or '—'}",
        f"- safety-gate refusals: {saf.get('safety_gate_refusals', 0)}",
        f"- entry-gate refusals: {saf.get('entry_gate_refusals', 0)}",
        f"- quarantine blocks: {saf.get('quarantine_blocks', 0)}",
    ]
    lines += ["", "## LLM operational result", ""]
    ops = report["llm_operations"]
    if ops.get("available"):
        totals = ops.get("totals", {})
        lines += [
            f"- runs: {totals.get('runs', 0)}",
            f"- total tokens: {totals.get('total_tokens', 0):,}",
            f"- estimated cost: ≈ ${totals.get('cost_usd', 0.0):,.2f}",
            f"- unpriced tokens: {totals.get('unpriced_tokens', 0):,}",
        ]
    else:
        lines.append(f"- token/cost data unavailable ({ops.get('error', '')})")
    lines += ["", "## Runtime reliability", ""]
    rel = report["reliability"]
    lines.append(f"- missed rounds: {rel['missed_rounds'] or 'none'}")
    lines.append(f"- stopped rounds: {rel['stopped_rounds'] or 'none'}")
    for adj in rel["schedule_adjustments"]:
        lines.append(f"- {adj['session']}: schedule_adjustment={adj['adjustment']}")
    for duration in rel["round_durations"]:
        lines.append(f"- {duration['session']}: round {duration['seconds']:.0f}s")
    lines += ["", "## Raw evidence index", ""]
    ev = report["evidence"]
    lines += [
        f"- manifest: `{ev['manifest']}`",
        f"- events: `{ev['events']}`",
        f"- snapshots: `{ev['snapshots']}`",
    ]
    for path in ev["rounds"]:
        lines.append(f"- round: `{path}`")
    for path in ev.get("unreadable_round_journals", []):
        lines.append(f"- round journal (unreadable, preserved as-is): `{path}`")
    for path in ev["daily_reports"]:
        lines.append(f"- daily report: `{path}`")
    lines += [
        "- execution DB: `eval_results/execution.db` (authoritative fills ledger)",
        "- run logs: `eval_results/<SYMBOL>/TradingAgentsStrategy_logs/runs/*.json`",
    ]
    lines += [
        "",
        "> Paper observation only. Decision-log returns are not broker-realized "
        "P/L. A fake-clock simulation never replaces the real 30-day run.",
        "",
    ]
    return "\n".join(lines)


def write_final_report(
    report: Dict[str, Any],
) -> Tuple[str, str]:
    """Persist final_report.json + final_report.md; return their paths."""
    directory = run_dir(report["run_id"])
    json_path = directory / "final_report.json"
    md_path = directory / "final_report.md"
    atomic_write_json(json_path, sanitize_for_log(report))
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_final_markdown(report), encoding="utf-8")
    return str(md_path), str(json_path)


def send_long_run_alert(
    subject: str, body: str, runtime: Optional[Dict[str, Any]],
    deps: Optional[LongRunDeps] = None,
) -> None:
    """Existing alert channel, failure-isolated (never affects trading)."""
    try:
        if deps is not None and deps.alert_fn is not None:
            deps.alert_fn(subject, body, runtime)
            return
        from tradingagents.alerts import AlertConfig, send_alert

        send_alert(subject, body, config=AlertConfig.from_config(runtime or {}))
    except Exception:
        pass


def finalize_observation(
    state: Dict[str, Any],
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    deps: Optional[LongRunDeps] = None,
    *,
    final_status: str,
    stop_code: str = "",
    stop_detail: str = "",
) -> Dict[str, Any]:
    """Finalize the window: persist status, reports, alerts; clear active."""
    deps = deps or LongRunDeps()
    run_id = state["run_id"]
    # Sessions in the window that never completed were missed: record them so
    # they stay in the denominator instead of silently disappearing. An
    # unfinished journal keeps its partial per-symbol evidence — those
    # analyses/executions really happened and the audit must show them.
    for session_day in sorted(state.get("expected_sessions") or []):
        journal = load_round_journal(run_id, session_day)
        if journal is not None and journal.get("status") in TERMINAL_ROUND_STATUSES:
            continue
        journal_file = round_path(run_id, session_day)
        if journal is None and journal_file.exists():
            # Unreadable journal — quite possibly the very reason this
            # observation is stopping. Never overwrite the original bytes
            # with a synthesized classification: that would erase the
            # evidence and guess through the corruption A30 forbids.
            log_event(run_id, "round_journal_unreadable", {"session": session_day})
            continue
        if journal is None:
            journal = new_round_journal(session_day, [])
        if final_status == "COMPLETED":
            journal["status"] = "MISSED"
            journal["stop_reason"] = "MISSED_PROCESS_DOWN"
            log_event(run_id, "round_missed",
                       {"session": session_day, "reason": "MISSED_PROCESS_DOWN"})
        else:
            # A stopped observation did not lose sessions to downtime: every
            # unrun session stopped together with the observation, under the
            # same stop reason (B02 classification vocabulary).
            journal["status"] = "STOPPED"
            journal["stop_reason"] = f"{stop_code or 'STOPPED'}: {stop_detail}"[:300]
            log_event(run_id, "round_stopped",
                       {"session": session_day, "reason": journal["stop_reason"]})
        journal["finished_at"] = journal.get("finished_at") or utc_now_iso()
        atomic_write_json(journal_file, journal)
    state["status"] = final_status
    if final_status == "STOPPED":        state["stop"] = {
            "code": stop_code or "STOPPED",
            "detail": (stop_detail or stop_code)[:500],
            "at": utc_now_iso(),
        }
    else:
        state["stop"] = None
    save_active_state(state)
    atomic_write_json(run_dir(run_id) / "manifest.json", sanitize_for_log({
        "run_id": run_id, "status": final_status, "stop": state["stop"],
        "started_at": state.get("started_at"), "ends_at": state.get("ends_at"),
        "expected_sessions": state.get("expected_sessions"),
        "baseline_commit": state.get("baseline_commit"),
        "config": state.get("config"),
    }))
    # F14: one read-only fresh account snapshot at observation end. The last
    # post-round snapshot can be days old; without this, ending equity and
    # positions would silently misrepresent the final state. If the capture
    # fails, ending values are reported as unavailable instead of being
    # labeled with stale data; no broker mutation happens either way.
    try:
        broker_factory = deps.broker_client_factory or _default_broker_client
        final_snapshot = capture_account_snapshot(broker_factory())
        append_jsonl(
            run_dir(run_id) / "account_snapshots.jsonl",
            {"phase": "final", **final_snapshot},
        )
    except Exception as exc:
        log_event(
            run_id,
            "final_snapshot_unavailable",
            {"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        print(f"[Phase-D] Final snapshot unavailable: {exc}")

    report = aggregate_final_report(state, long_cfg, runtime)
    md_path, json_path = write_final_report(report)
    log_event(run_id, "observation_finalized",
              {"status": final_status, "md": md_path, "json": json_path})
    if final_status == "STOPPED":
        send_long_run_alert(
            f"Phase-D observation {final_status}: {run_id}",
            f"Stop {state['stop'].get('code')}: {state['stop'].get('detail')}\n"
            f"Markdown: {md_path}\nJSON: {json_path}",
            runtime, deps,
        )
    else:
        send_long_run_alert(
            f"Phase-D observation COMPLETED: {run_id}",
            f"Markdown: {md_path}\nJSON: {json_path}",
            runtime, deps,
        )
    clear_active_state()
    print(f"\n[Phase-D] Observation {final_status}: {run_id}")
    print(f"[Phase-D] Markdown: {md_path}")
    print(f"[Phase-D] JSON: {json_path}")
    if final_status == "STOPPED":
        print(f"[Phase-D] Stop: {state['stop'].get('code')}: "
              f"{state['stop'].get('detail')}")
    return {"outcome": "completed" if final_status == "COMPLETED" else "stopped",
            "run_id": run_id, "md_path": md_path, "json_path": json_path}


def setup_or_resume(
    *,
    long_cfg: Optional[Dict[str, Any]] = None,
    runtime: Optional[Dict[str, Any]] = None,
    deps: Optional[LongRunDeps] = None,
) -> Dict[str, Any]:
    """Programmatic entry (tests/ops): resume active state or raise if none.

    Interactive setup lives in the CLI command; this owns the resume path:
    lock → recover → continue the original window. Never starts a second run.
    """
    deps = deps or LongRunDeps()
    state = load_active_state()
    if state is None:
        raise LongRunStop("NO_ACTIVE_OBSERVATION", "no unfinished observation to resume")
    try:
        with runner_lock():
            pass
    except RunnerLockBusy as exc:
        raise LongRunStop("ALREADY_RUNNING",
                          f"runner active for {state['run_id']}") from exc
    state["restart_count"] = int(state.get("restart_count") or 0) + 1
    save_active_state(state)
    cfg = long_cfg or dict(default_long_run_config(), **(state.get("config") or {}))
    run_config = runtime or build_runtime_config(cfg)
    with runner_lock():
        return run_observation_loop(state, cfg, run_config, deps)
