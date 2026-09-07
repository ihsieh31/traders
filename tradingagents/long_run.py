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

SECRET_KEY_NAMES = (
    "api_key", "secret_key", "token", "secret", "password",
    "alpaca_api_key", "alpaca_secret_key",
)
SECRET_ENV_MARKERS = ("_API_KEY", "_SECRET", "_TOKEN", "AZURE_OPENAI")
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


def sanitize_url(url: Any) -> str:
    """Strip query/userinfo (possible credentials) before display/logging."""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(str(url))
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, parts.path, "", "")
        )
    except Exception:
        return str(url).split("?", 1)[0].split("#", 1)[0]


def sanitize_for_log(obj: Any) -> Any:
    """Recursively redact secret values and URL credentials."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(name in lowered for name in SECRET_KEY_NAMES) or any(
                str(key).upper().endswith(m) or m in str(key).upper()
                for m in SECRET_ENV_MARKERS
            ):
                out[key] = "***"
            elif lowered.endswith("_url") or lowered == "backend_url" or lowered == "endpoint":
                out[key] = sanitize_url(value)
            else:
                out[key] = sanitize_for_log(value)
        return out
    if isinstance(obj, list):
        return [sanitize_for_log(v) for v in obj]
    return obj


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
    if target is not None and not (SESSION_OPEN_ET <= target <= SESSION_CLOSE_ET):
        errors.append(
            f"run_time_et {cfg.get('run_time_et')!r} can never be a regular-session "
            "time (09:30-16:00 ET)"
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
    ):
        runtime[key] = long_cfg.get(key)
    runtime["auto_screening_enabled"] = True
    runtime["allow_shorts"] = False
    runtime["trading_mode"] = "investment"
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
    data = read_json(active_path())
    if not isinstance(data, dict) or not data.get("run_id"):
        return None
    if data.get("status") not in ("RUNNING", "INTERRUPTED"):
        return None
    return data


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

def capture_account_snapshot(client: Any) -> Dict[str, Any]:
    account = client.get_account()
    positions = client.get_all_positions() or []

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
    equity = _num(_get(account, "equity")) or 0.0
    cash = _num(_get(account, "cash")) or 0.0
    buying_power = _num(_get(account, "buying_power"))
    rows = []
    long_mv = 0.0
    short_mv = 0.0
    for raw in positions:
        symbol = str(_get(raw, "symbol") or "").upper()
        qty = _num(_get(raw, "qty")) or 0.0
        if not symbol or qty == 0:
            continue
        market_value = _num(_get(raw, "market_value")) or 0.0
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

    # Alpaca read-only preflight: paper client, account, positions, calendar,
    # then execution recovery. No test order is ever placed.
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
    try:
        service_factory = deps.execution_service_factory or _default_execution_service
        recovery = service_factory().startup_recover()
        ok = bool(recovery.get("success"))
        _check("execution_recovery", ok, str(recovery.get("reconciliation_reasons")))
    except LongRunStop:
        raise
    except Exception as exc:
        raise LongRunStop("PREFLIGHT_FAILED", f"execution_recovery: {exc}")

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


# ---------------------------------------------------------------------------
# Daily round journal + resume state machine
# ---------------------------------------------------------------------------

SYMBOL_PENDING = "PENDING"
SYMBOL_ANALYZING = "ANALYZING"
SYMBOL_ANALYZED = "ANALYZED"
SYMBOL_EXECUTING = "EXECUTING"
SYMBOL_DONE = "DONE"
SYMBOL_FAILED = "FAILED"


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


def _recover_intent_from_run_log(symbol: str, session_date: str) -> Optional[Dict[str, Any]]:
    try:
        from tradingagents.run_logger import load_final_state_snapshot

        final_state = load_final_state_snapshot(symbol, session_date)
    except Exception:
        return None
    if not isinstance(final_state, dict):
        return None
    return _normalize_intent(final_state.get("final_trade_intent"))


def summarize_execution_result(result: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "success": bool(result.get("success")),
        "broker_attempted": bool(result.get("broker_attempted")),
        "broker_calls": int(result.get("broker_calls") or 0),
        "hold": bool(result.get("hold")),
        "deduped": bool(result.get("deduped")),
        "safety_blocked": bool(result.get("safety_blocked")),
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


def _build_graph_config(runtime: Dict[str, Any], long_cfg: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(runtime)
    config["_long_run_analysts"] = list(long_cfg.get("analysts") or [])
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
) -> Dict[str, Any]:
    """One trading-day round: recover → snapshot → screen → analyze → trade."""
    from tradingagents.agents.schemas import trade_intent_action
    from tradingagents.llm_clients.retry import ProviderFailure

    deps = deps or LongRunDeps()
    service_factory = deps.execution_service_factory or _default_execution_service
    screening_fn = deps.screening_fn or _default_screening
    graph_factory = deps.graph_factory or _default_graph_factory
    broker_factory = deps.broker_client_factory or _default_broker_client
    service = service_factory()
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

    # Step 1 — recover first; unsafe state stops the whole observation.
    try:
        recovery = service.startup_recover()
    except Exception as exc:
        raise LongRunStop("RECOVERY_FAILED", f"startup_recover raised: {exc}")
    if not recovery.get("success"):
        raise LongRunStop(
            "RECOVERY_UNSAFE",
            f"execution recovery forbids unattended continuation: "
            f"{recovery.get('reconciliation_reasons')}",
        )

    # Step 2 — pre-round account snapshot.
    try:
        pre_snapshot = capture_account_snapshot(broker_factory())
    except Exception as exc:
        raise LongRunStop("SNAPSHOT_UNAVAILABLE", f"pre-round snapshot failed: {exc}")
    append_jsonl(run_dir(run_id) / "account_snapshots.jsonl",
                 {"phase": "pre_round", "session": session_date, **pre_snapshot})

    # Step 3 — Phase C screening (fail-closed; a stop ends the observation).
    plan = screening_fn(runtime, False)
    if getattr(plan, "stopped", False):
        raise LongRunStop(
            "SCREENING_STOPPED",
            f"screening stopped the round: {plan.stop_reason_text()}",
        )

    if journal is None:
        journal = new_round_journal(
            session_date, list(getattr(plan, "deep_analysis_set", []) or []),
            scheduled_at=(schedule_info or {}).get("effective_at", ""),
            schedule_adjustment=(schedule_info or {}).get("schedule_adjustment", "NONE"),
        )
    journal["status"] = "RUNNING"
    if not journal.get("started_at"):
        journal["started_at"] = utc_now_iso()

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
    graph_config = _build_graph_config(runtime, long_cfg)
    notional = float(long_cfg.get("base_trade_notional_usd") or 0)
    graph = None

    def _execute_symbol(symbol: str, intent: Dict[str, Any]) -> None:
        journal["symbols"][symbol]["status"] = SYMBOL_EXECUTING
        save_round_journal(run_id, journal)
        try:
            result = _execute_intent(
                deps, service, symbol, intent, notional,
                run_id=run_id, session_date=session_date,
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

        # Case D (resume): re-enter execution with the identical decision
        # identity; the durable outbox dedupes without a second broker POST.
        if status == SYMBOL_EXECUTING:
            intent = entry.get("trade_intent")
            if not intent:
                entry["status"] = SYMBOL_FAILED
                entry["execution_result_summary"] = {"error": "EXECUTING without intent"}
                save_round_journal(run_id, journal)
                continue
            try:
                recovery = service.startup_recover()
                if not recovery.get("success"):
                    raise LongRunStop("RECOVERY_UNSAFE",
                                      f"re-entry recovery unsafe: {recovery.get('reconciliation_reasons')}")
                result = _execute_intent(
                    deps, service, symbol, intent, notional,
                    run_id=run_id, session_date=session_date,
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
                _execute_symbol(symbol, intent)
                continue

        # Case B: ANALYZING means the previous process died before writing
        # ANALYZED. Broker execution is forbidden before ANALYZED, so reuse a
        # provably completed run or safely re-run the analysis.
        if status == SYMBOL_ANALYZING:
            recovered = _recover_intent_from_run_log(symbol, session_date)
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
        entry["status"] = SYMBOL_ANALYZING
        save_round_journal(run_id, journal)
        symbol_started = utc_now_iso()
        try:
            if graph is None:
                graph = graph_factory(graph_config)
            final_state, _signal = graph.propagate(symbol, session_date)
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


def _execute_intent(deps, service, symbol, intent, notional, *, run_id, session_date):
    from tradingagents.execution.auto_trade import execute_auto_trade

    return execute_auto_trade(
        ticker=symbol,
        trade_intent=intent,
        base_trade_notional_usd=notional,
        allow_shorts=False,
        config=None,
        execution_service=service,
        decision_id=f"{run_id}-{session_date}-{symbol}",
        run_id=f"{run_id}-{session_date}-{symbol}",
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
    """Unknown/ambiguous broker state or a paused account stops everything."""
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
        try:
            sweep_missed_sessions(
                run_id=run_id,
                expected=[s for s in (state.get("expected_sessions") or [])
                          if s < ends_at.astimezone(eastern.tzinfo).date().isoformat()],
                today=eastern.date().isoformat(),
            )
            target = next_due_session(
                now=now,
                run_time_et=str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                started_at=datetime.fromisoformat(state["started_at"]),
                ends_at=ends_at,
                settled=settled_sessions(run_id),
                calendar_client=deps.calendar_client,
                calendar_rows=deps.calendar_rows,
            )
        except LongRunStop as exc:
            # A hard stop at the scheduling stage (calendar authority
            # unavailable, corrupt round journal) must end the observation
            # exactly like a mid-round stop: STOPPED state, partial final
            # report, active window closed. Raising past the loop would leave
            # the window RUNNING forever with no report to inspect.
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
        try:
            run_daily_round(
                run_id=run_id,
                session_date=target["session_date"],
                long_cfg=long_cfg,
                runtime=runtime,
                schedule_info=target,
                deps=deps,
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
    end_equity = equities[-1] if equities else None
    total_return = (
        (end_equity - start_equity) / start_equity
        if start_equity not in (None, 0) and end_equity is not None else None
    )
    drawdown = compute_drawdown(equities)
    starting_positions = next(
        (s.get("positions") for s in snapshots if s.get("phase") == "pre_round"), None,
    )
    ending_positions = next(
        (s.get("positions") for s in reversed(snapshots)
         if s.get("phase") == "post_round"), None,
    )

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
    exec_tally = {"submitted_symbols": 0, "broker_calls": 0, "holds": 0,
                  "safety_blocks": 0, "entry_gate_blocks": 0,
                  "quarantined": 0, "unknown": 0, "deduped": 0}
    round_durations: List[Dict[str, Any]] = []
    for journal in rounds:
        for symbol, entry in (journal.get("symbols") or {}).items():
            summary = entry.get("execution_result_summary") or {}
            if entry.get("status") != SYMBOL_DONE and not summary:
                continue
            if summary.get("no_trade"):
                exec_tally["holds"] += 1
                continue
            if summary.get("broker_calls"):
                exec_tally["submitted_symbols"] += 1
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

    # LLM operations from existing cost aggregation (best-effort, no estimates).
    llm_ops: Dict[str, Any] = {"available": False}
    try:
        from tradingagents.llm_cost import aggregate_costs, scan_run_costs

        records = scan_run_costs(
            eval_results_dir=runtime.get("results_dir", "eval_results"),
            overrides=runtime.get("llm_pricing_per_million"),
        )
        totals = aggregate_costs(records)
        llm_ops = {"available": True, "totals": totals.get("totals", {}),
                   "per_day": totals.get("per_day", {}),
                   "unpriced_tokens": totals.get("totals", {}).get("unpriced_tokens", 0)}
    except Exception as exc:
        llm_ops = {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    # Execution DB tallies (best-effort reference; journals stay primary).
    exec_db: Dict[str, Any] = {"available": False}
    try:
        from tradingagents.execution import ExecutionStore

        store = ExecutionStore(runtime.get("execution_db_path", "eval_results/execution.db"))
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
            "peak_equity": drawdown["peak"], "trough_equity": drawdown["trough"],
            "max_drawdown": drawdown["max_drawdown"],
            "starting_cash": snapshots[0].get("cash") if snapshots else None,
            "ending_cash": snapshots[-1].get("cash") if snapshots else None,
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