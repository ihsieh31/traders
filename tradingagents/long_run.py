"""Phase D — unattended Alpaca Paper observation (single CLI command).

This module coordinates daily rounds, scheduling, crash/resume and
finalization. Bounded responsibilities live in long_run_support; explicit
compatibility functions preserve existing call-time replacement points. No orchestration framework, no
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
from tradingagents.long_run_support import reporting as _reporting
from tradingagents.long_run_support import state as _state
from tradingagents.long_run_support import config as _long_run_config
from tradingagents.long_run_support import sessions as _sessions
from tradingagents.long_run_support import preflight as _preflight
from tradingagents.long_run_support import symbols as _symbols
from tradingagents.long_run_support import round_support as _round_support
from tradingagents.long_run_support.sessions import make_session_submit_guard

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
    return _state.base_dir()


def config_path() -> Path:
    return _state.config_path(base_dir=base_dir)


def active_path() -> Path:
    return _state.active_path(base_dir=base_dir)


def lock_path() -> Path:
    return _state.lock_path(base_dir=base_dir)


def run_dir(run_id: str) -> Path:
    return _state.run_dir(run_id, base_dir=base_dir)


def round_path(run_id: str, session_date: str) -> Path:
    return _state.round_path(run_id, session_date, run_dir=run_dir)


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def _looks_placeholder(value: Any) -> bool:
    return _state._looks_placeholder(value, PLACEHOLDER_MARKERS=PLACEHOLDER_MARKERS)


def scan_files_for_secrets(paths: List[Path], markers: List[str]) -> List[str]:
    """Return file paths containing any marker (fake-secret leak test)."""
    return _state.scan_files_for_secrets(paths, markers)


# ---------------------------------------------------------------------------
# Atomic JSON state + events + single-runner lock
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: Any) -> None:
    return _state.atomic_write_json(path, payload)


def read_json(path: Path) -> Optional[Any]:
    return _state.read_json(path)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    return _state.append_jsonl(path, record, sanitize_for_log=sanitize_for_log)


def utc_now_iso() -> str:
    return _state.utc_now_iso(datetime=datetime)


RunnerLockBusy = _state.RunnerLockBusy


@contextmanager
def runner_lock():
    """Single-runner guard: only one Phase-D process per machine/user."""
    yield from _state.runner_lock(
        lock_path=lock_path,
        utc_now_iso=utc_now_iso,
        RunnerLockBusy=RunnerLockBusy,
    )


GlobalRunnerLockBusy = _state.GlobalRunnerLockBusy


def global_runner_lock_path() -> Path:
    return _state.global_runner_lock_path()


@contextmanager
def global_runner_lock():
    """Application-wide runner lock: exactly one long-run or A/B Paper runner
    per machine/user. Entrypoints acquire this BEFORE the mode-specific lock
    (lock ordering: global runner lock -> runner_lock / campaign locks)."""
    yield from _state.global_runner_lock(utc_now_iso=utc_now_iso)


# ---------------------------------------------------------------------------
# Non-secret Phase-D configuration
# ---------------------------------------------------------------------------

def default_long_run_config() -> Dict[str, Any]:
    return _long_run_config.default_long_run_config(
        LONG_RUN_SCHEMA_VERSION=LONG_RUN_SCHEMA_VERSION,
        DEFAULT_DURATION_CALENDAR_DAYS=DEFAULT_DURATION_CALENDAR_DAYS,
        DEFAULT_RUN_TIME_ET=DEFAULT_RUN_TIME_ET,
        VALID_ANALYSTS=VALID_ANALYSTS,
    )


def load_long_run_config() -> Dict[str, Any]:
    return _long_run_config.load_long_run_config(
        default_long_run_config=default_long_run_config,
        read_json=read_json,
        config_path=config_path,
    )


def save_long_run_config(cfg: Dict[str, Any]) -> None:
    return _long_run_config.save_long_run_config(
        cfg,
        default_long_run_config=default_long_run_config,
        LONG_RUN_SCHEMA_VERSION=LONG_RUN_SCHEMA_VERSION,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
    )


def _valid_backend_url(value: Any) -> bool:
    return _long_run_config._valid_backend_url(value)


def missing_config_fields(cfg: Dict[str, Any]) -> List[str]:
    """Fields the setup wizard must ask for (absent/invalid/placeholder)."""
    return _long_run_config.missing_config_fields(
        cfg,
        _looks_placeholder=_looks_placeholder,
        PROVIDERS_REQUIRING_URL=PROVIDERS_REQUIRING_URL,
    )


def _unattended_safety_error(runtime: Dict[str, Any]) -> str:
    """P2-01: an unattended paper run cannot run with the safety layer off.

    Only ``runtime.get("safety_enabled", True) is True`` may proceed: the
    key may be absent (default on), but False/0/"false"/None (explicit)
    must refuse. The general SafetyGuard feature itself is untouched —
    this gates only the unattended long-run production path.
    """
    return _long_run_config._unattended_safety_error(runtime)


def _validate_unattended_safety(runtime: Dict[str, Any]) -> None:
    return _long_run_config._validate_unattended_safety(
        runtime,
        _unattended_safety_error=_unattended_safety_error,
        LongRunStop=LongRunStop,
    )


def validate_long_run_config(
    cfg: Dict[str, Any], runtime: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Secret-free validation; returns error strings (empty = valid)."""
    return _long_run_config.validate_long_run_config(
        cfg,
        runtime,
        LONG_RUN_SCHEMA_VERSION=LONG_RUN_SCHEMA_VERSION,
        parse_run_time_et=parse_run_time_et,
        SESSION_OPEN_ET=SESSION_OPEN_ET,
        SESSION_CLOSE_ET=SESSION_CLOSE_ET,
        VALID_ANALYSTS=VALID_ANALYSTS,
        VALID_RESEARCH_DEPTHS=VALID_RESEARCH_DEPTHS,
        _valid_backend_url=_valid_backend_url,
        PROVIDERS_REQUIRING_URL=PROVIDERS_REQUIRING_URL,
        _unattended_safety_error=_unattended_safety_error,
    )


def parse_run_time_et(value: str) -> dtime:
    return _long_run_config.parse_run_time_et(value, datetime=datetime)


def build_runtime_config(
    long_cfg: Dict[str, Any], base: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Full graph/execution config for a round: Phase D forces full-system mode."""
    return _long_run_config.build_runtime_config(long_cfg, base)


def apply_single_backend_runtime_paths(
    runtime: Dict[str, Any], backend: str
) -> Dict[str, Any]:
    """Isolated results/cache/execution-DB paths for a non-Traders backend."""
    return _long_run_config.apply_single_backend_runtime_paths(runtime, backend)


def apply_ab_backend_runtime_paths(
    runtime: Dict[str, Any], backend: str, root: str | Path
) -> Dict[str, Any]:
    return _long_run_config.apply_ab_backend_runtime_paths(runtime, backend, root)


def git_baseline_commit() -> str:
    return _long_run_config.git_baseline_commit()


# One chunk boundary can legitimately contain no trading day (a short chunk
# landing on a weekend, for example). That must delay the next growth attempt,
# never cancel it: the observation would otherwise stop extending its window
# and silently trade nothing for the rest of an unbounded run.
EMPTY_EXTENSION_RETRY_SECONDS = 300.0


def _extension_retry_due(state: Dict[str, Any], now: datetime) -> bool:
    """Whether a previously empty continuous growth attempt may be retried."""
    raw = state.get("extension_retry_after")
    if not raw:
        return True
    try:
        retry_after = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if retry_after.tzinfo is None:
        retry_after = retry_after.replace(tzinfo=now.tzinfo)
    return now >= retry_after


def experiment_fingerprint(long_cfg: Dict[str, Any], runtime: Dict[str, Any]) -> str:
    """Hash non-secret inputs that can change an unattended A/B decision."""
    from tradingagents.default_config import DEFAULT_CONFIG
    import hashlib

    excluded = {
        "project_dir", "results_dir", "data_dir", "data_cache_dir",
        "memory_log_path", "agent_memory_dir", "evidence_packet_path",
        "evidence_packet_sha256", "alerts_enabled", "alert_cooldown_seconds",
    }
    def behavioral(key: str) -> bool:
        lowered = key.lower()
        return (key not in excluded and not lowered.endswith(("_path", "_dir"))
                and not any(marker in lowered for marker in (
                    "api_key", "secret", "password", "webhook", "telegram", "chat_id",
                    "access_token", "auth_token", "private_key")))

    def without_secrets(value: Any, key: Any = None) -> Any:
        """Drop non-behavioral/secret material and survive the state round trip.

        The active state is persisted through ``sanitize_for_log``, which
        rewrites every ``*_url`` key with ``sanitize_url`` — and that maps an
        unset ``None`` to ``""``. The resume path rebuilds its config from that
        sanitized copy, so hashing the raw ``None`` would make every resume of
        a live observation a permanent CONFIG_DRIFT.
        """
        if isinstance(value, dict):
            return {child: without_secrets(item, child) for child, item in value.items()
                    if behavioral(str(child))}
        if isinstance(value, (list, tuple)):
            return [without_secrets(item) for item in value]
        if value is None and (str(key).lower().endswith("_url")
                              or str(key).lower() in {"backend_url", "endpoint"}):
            return ""
        return value

    def canonical(key: str, value: Any) -> Any:
        """Survive the sanitize_for_log round trip the active state goes through.

        ``sanitize_for_log`` rewrites every ``*_url`` key through
        ``sanitize_url``, which maps an unset ``None`` to ``""``. The resume
        path rebuilds its config from that sanitized copy, so hashing the raw
        ``None`` would make every resume a permanent CONFIG_DRIFT.
        """
        if value is None and (str(key).lower().endswith("_url")
                              or str(key).lower() in {"backend_url", "endpoint"}):
            return ""
        return value

    effective = {key: canonical(key, without_secrets(runtime.get(key, value), key))
                 for key, value in DEFAULT_CONFIG.items() if behavioral(key)}
    parameters = {key: canonical(key, without_secrets(value, key)) for key, value in long_cfg.items()
                  if behavioral(key) and key not in {"ab_results_root"}}
    prompt_root = Path(__file__).resolve().parent / "prompts" / "templates"
    prompts = {str(path.relative_to(prompt_root)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(prompt_root.rglob("*.md"))}
    project_root = Path(__file__).resolve().parent.parent
    sources = {
        str(path.relative_to(project_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in (project_root / "tradingagents", project_root / "cli")
        for path in sorted(folder.rglob("*.py"))
    }
    payload = {"commit": git_baseline_commit(), "runtime": effective,
               "long_run": parameters, "prompts": prompts, "sources": sources}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_ab_startup_snapshots(snapshots: Dict[str, Dict[str, Any]]) -> None:
    """Require flat, distinct, comparably funded Paper accounts at startup."""
    import math
    if set(snapshots) != set(AB_BACKENDS):
        raise LongRunStop("AB_BASELINE_INVALID", "both A/B account snapshots are required")
    refs = [str(snapshots[backend].get("account_ref") or "") for backend in AB_BACKENDS]
    if not all(refs) or len(set(refs)) != 2:
        raise LongRunStop("AB_BASELINE_INVALID", "A/B Paper account identities are not distinct")
    equities = []
    for backend in AB_BACKENDS:
        snapshot = snapshots[backend]
        if snapshot.get("positions"):
            raise LongRunStop("AB_BASELINE_INVALID", f"{backend} account is not flat")
        try:
            equity = float(snapshot.get("equity"))
        except (TypeError, ValueError):
            equity = math.nan
        if not math.isfinite(equity) or equity <= 0:
            raise LongRunStop("AB_BASELINE_INVALID", f"{backend} account equity is invalid")
        equities.append(equity)
    if abs(equities[0] - equities[1]) / max(equities) > 0.01:
        raise LongRunStop("AB_BASELINE_INVALID", "A/B starting equities differ by more than 1%")


# ---------------------------------------------------------------------------
# Scheduling (authoritative Alpaca calendar only)
# ---------------------------------------------------------------------------

def eastern_now(now: Any = None) -> datetime:
    return _sessions.eastern_now(now)


def effective_target_for_session(
    session_day: date,
    run_time_et: str,
    *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Configured target vs effective target for one authoritative session."""
    return _sessions.effective_target_for_session(
        session_day,
        run_time_et,
        calendar_client=calendar_client,
        calendar_rows=calendar_rows,
        parse_run_time_et=parse_run_time_et,
        datetime=datetime,
    )


def fetch_session_dates(
    start: date, end: date, client: Any = None
) -> List[date]:
    """Authoritative trading sessions in [start, end]; fail-closed on error."""
    return _sessions.fetch_session_dates(start, end, client, date=date)


def next_due_session(
    *,
    now: Any,
    run_time_et: str,
    started_at: datetime,
    ends_at: datetime,
    settled: List[str],
    expected_sessions: Optional[List[str]] = None,
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
    return _sessions.next_due_session(
        now=now,
        run_time_et=run_time_et,
        started_at=started_at,
        ends_at=ends_at,
        settled=settled,
        expected_sessions=expected_sessions,
        calendar_client=calendar_client,
        calendar_rows=calendar_rows,
        eastern_now=eastern_now,
        effective_target_for_session=effective_target_for_session,
        datetime=datetime,
    )


# ---------------------------------------------------------------------------
# Observation state
# ---------------------------------------------------------------------------

def session_close_et(
    session_date: str, *,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> dtime:
    """Authoritative regular-session close (ET) for one session date."""
    return _sessions.session_close_et(
        session_date,
        calendar_client=calendar_client,
        calendar_rows=calendar_rows,
        date=date,
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
    return _sessions.mark_session_missed_after_close(
        run_id=run_id,
        session_date=session_date,
        now=now,
        run_time_et=run_time_et,
        calendar_client=calendar_client,
        calendar_rows=calendar_rows,
        load_round_journal=load_round_journal,
        round_path=round_path,
        LongRunStop=LongRunStop,
        eastern_now=eastern_now,
        session_close_et=session_close_et,
        TERMINAL_ROUND_STATUSES=TERMINAL_ROUND_STATUSES,
        SYMBOL_EXECUTING=SYMBOL_EXECUTING,
        new_round_journal=new_round_journal,
        utc_now_iso=utc_now_iso,
        save_round_journal=save_round_journal,
        log_event=log_event,
        datetime=datetime,
        date=date,
    )

def new_observation_state(
    long_cfg: Dict[str, Any], *, expected_sessions: List[str], now: Optional[datetime] = None
) -> Dict[str, Any]:
    return _state.new_observation_state(
        long_cfg,
        expected_sessions=expected_sessions,
        now=now,
        git_baseline_commit=git_baseline_commit,
        sanitize_for_log=sanitize_for_log,
        LONG_RUN_SCHEMA_VERSION=LONG_RUN_SCHEMA_VERSION,
        datetime=datetime,
    )


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
    return _state.load_active_state(active_path=active_path, LongRunStop=LongRunStop)


def save_active_state(state: Dict[str, Any]) -> None:
    return _state.save_active_state(
        state,
        active_path=active_path,
        atomic_write_json=atomic_write_json,
    )


def clear_active_state() -> None:
    return _state.clear_active_state(active_path=active_path)


def log_event(run_id: str, event_type: str, detail: Any = None) -> None:
    return _state.log_event(
        run_id,
        event_type,
        detail,
        append_jsonl=append_jsonl,
        run_dir=run_dir,
        utc_now_iso=utc_now_iso,
    )


def completed_sessions(run_id: str) -> List[str]:
    return _state.completed_sessions(run_id, run_dir=run_dir, read_json=read_json)


# A round journal in one of these states is settled: its session was decided
# once (traded, missed, or hard-stopped) and must never be re-picked or
# re-run. Re-running a MISSED session would analyze stale data days late and
# submit a stale order (A18), and overwriting one would erase the audit
# record that the session ever slipped (B02).
TERMINAL_ROUND_STATUSES = _state.TERMINAL_ROUND_STATUSES


def settled_sessions(run_id: str) -> List[str]:
    """Session dates with a terminal round journal (never scheduled again)."""
    return _state.settled_sessions(
        run_id,
        run_dir=run_dir,
        read_json=read_json,
        TERMINAL_ROUND_STATUSES=TERMINAL_ROUND_STATUSES,
    )


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
    return _preflight._default_screening(config, refresh)


def _default_graph_factory(config: Dict[str, Any]) -> Any:
    return _preflight._default_graph_factory(config)


def _default_execution_service() -> Any:
    return _preflight._default_execution_service()


def _default_broker_client() -> Any:
    return _preflight._default_broker_client()


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
    return _preflight._default_llm_probe(
        role=role,
        provider=provider,
        model=model,
        backend_url=backend_url,
        api_key=api_key,
        max_retries=max_retries,
        REDACTED_API_KEY=REDACTED_API_KEY,
        LongRunStop=LongRunStop,
        sanitize_url=sanitize_url,
        time=time,
    )


# ---------------------------------------------------------------------------
# Account snapshots (sanitized, no credentials)
# ---------------------------------------------------------------------------

SnapshotUnavailable = _preflight.SnapshotUnavailable


def capture_account_snapshot(client: Any) -> Dict[str, Any]:
    """Sanitized account/positions snapshot; raise on unprovable facts (R16).

    A NaN equity, missing cash, or ``positions`` of None/invalid value means
    the broker could not prove the account state. Coercing any of them to a
    number would let a report claim -100% returns or a fully flat account;
    instead the capture raises so callers fail closed (preflight aborts,
    finalize records ``final_snapshot_unavailable``).
    """
    return _preflight.capture_account_snapshot(
        client,
        utc_now_iso=utc_now_iso,
        hashlib=hashlib,
        SnapshotUnavailable=SnapshotUnavailable,
    )


# ---------------------------------------------------------------------------
# Preflight (read-only except local config/state files)
# ---------------------------------------------------------------------------

def run_preflight(
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    deps: Optional[LongRunDeps] = None,
) -> Dict[str, Any]:
    """Validate config, probe LLM transports, verify Alpaca read-only."""
    return _preflight.run_preflight(
        long_cfg,
        runtime,
        deps,
        LongRunDeps=LongRunDeps,
        LongRunStop=LongRunStop,
        _validate_unattended_safety=_validate_unattended_safety,
        validate_long_run_config=validate_long_run_config,
        REDACTED_API_KEY=REDACTED_API_KEY,
        _default_llm_probe=_default_llm_probe,
        _default_broker_client=_default_broker_client,
        capture_account_snapshot=capture_account_snapshot,
        fetch_session_dates=fetch_session_dates,
        date=date,
        timedelta=timedelta,
    )


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
    _long_run_config._apply_runtime_config(runtime, LongRunStop=LongRunStop)
    # Runtime switching is intentional inside the single A/B coordinator.
    # Rebuild the cached guard so each arm uses its own safety paths.
    try:
        from tradingagents.safety import reset_safety_guard

        reset_safety_guard()
    except Exception:
        pass


def _validate_long_run_execution_config(runtime: Dict[str, Any]) -> None:
    """Confirm the applied global config still proves unattended invariants.

    Runs after _apply_runtime_config (and after any later merge) and refuses
    to continue when the effective config turned off auto screening, paper
    mode or the safety layer for an unattended long run.
    """
    return _long_run_config._validate_long_run_execution_config(
        runtime,
        LongRunStop=LongRunStop,
        append_jsonl=append_jsonl,
        base_dir=base_dir,
        utc_now_iso=utc_now_iso,
        _unattended_safety_error=_unattended_safety_error,
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
    return _preflight.run_post_authorization_recovery(
        deps,
        runtime,
        can_submit=getattr(deps, "_startup_recovery_can_submit", None),
        LongRunDeps=LongRunDeps,
        LongRunStop=LongRunStop,
        _apply_runtime_config=_apply_runtime_config,
        _validate_long_run_execution_config=_validate_long_run_execution_config,
        _default_execution_service=_default_execution_service,
        _default_broker_client=_default_broker_client,
        capture_account_snapshot=capture_account_snapshot,
    )


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
    return _state._empty_maintenance_summary()


def new_round_journal(
    session_date: str, symbols: List[str], *, scheduled_at: str = "",
    schedule_adjustment: str = "NONE",
) -> Dict[str, Any]:
    return _state.new_round_journal(
        session_date,
        symbols,
        scheduled_at=scheduled_at,
        schedule_adjustment=schedule_adjustment,
        LONG_RUN_SCHEMA_VERSION=LONG_RUN_SCHEMA_VERSION,
        SYMBOL_PENDING=SYMBOL_PENDING,
    )


def save_round_journal(run_id: str, journal: Dict[str, Any]) -> None:
    return _state.save_round_journal(
        run_id,
        journal,
        atomic_write_json=atomic_write_json,
        round_path=round_path,
    )


def load_round_journal(run_id: str, session_date: str) -> Optional[Dict[str, Any]]:
    journal = _state.load_round_journal(
        run_id,
        session_date,
        read_json=read_json,
        round_path=round_path,
    )
    if journal is not None and journal.get("session_date") != session_date:
        raise LongRunStop("STATE_CORRUPT", f"round journal date mismatch for {session_date}")
    return journal


def _normalize_intent(intent: Any) -> Optional[Dict[str, Any]]:
    return _round_support._normalize_intent(intent)


def _recover_intent_from_run_log(
    symbol: str, session_date: str, *, observation_id: str, results_dir: str,
) -> Optional[Dict[str, Any]]:
    """Recover a crashed analysis intent, bound to THIS observation only.

    The run log must be a completed ``long_run`` analysis whose
    ``long_run_observation_id`` metadata matches exactly. Without a match the
    caller re-runs the analysis; a manual/WebUI/other-observation result is
    never borrowed (F12).
    """
    return _round_support._recover_intent_from_run_log(
        symbol,
        session_date,
        observation_id=observation_id,
        results_dir=results_dir,
        _normalize_intent=_normalize_intent,
    )


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
    return _round_support.summarize_execution_result(result)


def _build_graph_config(
    runtime: Dict[str, Any], long_cfg: Dict[str, Any], run_id: Optional[str] = None
) -> Dict[str, Any]:
    return _round_support._build_graph_config(runtime, long_cfg, run_id)


@dataclass
class _PreparedDailyRound:
    """One arm's safety-checked daily context, ready for symbol work."""

    run_id: str
    session_date: str
    long_cfg: Dict[str, Any]
    runtime: Dict[str, Any]
    schedule_info: Optional[Dict[str, Any]]
    deps: LongRunDeps
    ends_at: Optional[datetime]
    journal: Dict[str, Any]
    service: Any
    graph_factory: Callable[..., Any]
    broker_factory: Callable[..., Any]
    recovery_can_submit: Callable[[], bool]
    session_submit_allowed: Callable[[], bool]


def _run_prepared_round_symbols(
    prepared: _PreparedDailyRound,
    *,
    phase: str = "all",
    symbols: Optional[List[str]] = None,
    reapply_runtime: bool = False,
) -> Optional[str]:
    """Run existing per-symbol journal work for one arm or one symbol."""
    from tradingagents.agents.schemas import trade_intent_action
    from tradingagents.llm_clients.retry import ProviderFailure

    if reapply_runtime:
        # A/B interleaves account contexts in one process. Never rely on the
        # config left installed by the previous arm operation.
        _apply_runtime_config(prepared.runtime)
        _validate_long_run_execution_config(prepared.runtime)
    return _symbols.run_symbol_work(
        journal=prepared.journal,
        run_id=prepared.run_id,
        session_date=prepared.session_date,
        long_cfg=prepared.long_cfg,
        runtime=prepared.runtime,
        deps=prepared.deps,
        service=prepared.service,
        ends_at=prepared.ends_at,
        graph_factory=prepared.graph_factory,
        _recovery_can_submit=prepared.recovery_can_submit,
        _session_submit_allowed=prepared.session_submit_allowed,
        trade_intent_action=trade_intent_action,
        ProviderFailure=ProviderFailure,
        LongRunStop=LongRunStop,
        _build_graph_config=_build_graph_config,
        _control_stop_reason=_control_stop_reason,
        STOP_REASON_WINDOW_ENDED=STOP_REASON_WINDOW_ENDED,
        STOP_REASON_STOP_REQUESTED=STOP_REASON_STOP_REQUESTED,
        SYMBOL_PENDING=SYMBOL_PENDING,
        SYMBOL_ANALYZING=SYMBOL_ANALYZING,
        SYMBOL_ANALYZED=SYMBOL_ANALYZED,
        SYMBOL_EXECUTING=SYMBOL_EXECUTING,
        SYMBOL_DONE=SYMBOL_DONE,
        SYMBOL_FAILED=SYMBOL_FAILED,
        save_round_journal=save_round_journal,
        utc_now_iso=utc_now_iso,
        _execute_intent=_execute_intent,
        _record_execution=_record_execution,
        _check_execution_hard_stop=_check_execution_hard_stop,
        _recover_intent_from_run_log=_recover_intent_from_run_log,
        log_event=log_event,
        _normalize_intent=_normalize_intent,
        phase=phase,
        symbols=symbols,
    )


def _finish_prepared_daily_round(
    prepared: _PreparedDailyRound,
    stop_reason: Optional[str],
    *,
    reapply_runtime: bool = False,
) -> Dict[str, Any]:
    """Persist the ordinary post-round snapshot, report and terminal journal."""
    journal = prepared.journal
    run_id = prepared.run_id
    session_date = prepared.session_date
    if stop_reason == STOP_REASON_STOP_REQUESTED:
        journal["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        log_event(run_id, "round_interrupted", {
            "session": session_date, "reason": "STOP_REQUESTED",
        })
        return journal
    if stop_reason == STOP_REASON_WINDOW_ENDED:
        journal["status"] = "RUNNING"
        journal["stop_reason"] = journal.get("stop_reason") or "WINDOW_ENDED_DURING_ROUND"
        save_round_journal(run_id, journal)
        log_event(run_id, "round_window_ended", {"session": session_date})
        return journal

    if reapply_runtime:
        _apply_runtime_config(prepared.runtime)
        _validate_long_run_execution_config(prepared.runtime)
    try:
        post_snapshot = capture_account_snapshot(prepared.broker_factory())
    except Exception as exc:
        raise LongRunStop("SNAPSHOT_UNAVAILABLE", f"post-round snapshot failed: {exc}")
    append_jsonl(
        run_dir(run_id) / "account_snapshots.jsonl",
        {"phase": "post_round", "session": session_date, **post_snapshot},
    )

    try:
        from tradingagents.daily_report import write_daily_report

        md_path, _html_path = write_daily_report(
            day=session_date,
            output_dir=str(run_dir(run_id) / "daily_reports"),
            config=prepared.runtime,
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

    journal["status"] = "COMPLETED"
    journal["finished_at"] = utc_now_iso()
    save_round_journal(run_id, journal)
    log_event(run_id, "round_completed", {"session": session_date})
    return journal


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

    # Keep the long-standing public function signature stable. The unified A/B
    # coordinator passes its immutable per-arm plan through reserved private
    # keys, which are removed before the config reaches any downstream code.
    long_cfg = dict(long_cfg)
    screening_plan = long_cfg.pop("_ab_screening_plan", None)
    ab_mode = bool(long_cfg.pop("_ab_mode", False))
    ab_prepare_only = bool(long_cfg.pop("_ab_prepare_only", False))
    deps = deps or LongRunDeps()
    service_factory = deps.execution_service_factory or _default_execution_service
    screening_fn = deps.screening_fn or _default_screening
    graph_factory = deps.graph_factory or _default_graph_factory
    broker_factory = deps.broker_client_factory or _default_broker_client
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
        _state.validate_resumable_symbols(journal, LongRunStop=LongRunStop)
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
    # The ExecutionService is constructed only AFTER the runtime became the
    # installed global config, so the default factory resolves this backend's
    # execution DB (Berkshire isolation) — never a stale or foreign one.
    service = service_factory()

    # Step 1 — recover first; unsafe state stops the whole observation.
    # The scheduled target plus its fixed grace fences opening POSTs at the
    # broker boundary. Existing reconciliation remains available after it.
    effective_target = (schedule_info or {}).get("effective_target")
    if not effective_target and (schedule_info or {}).get("effective_at"):
        try:
            effective_target = datetime.fromisoformat(
                str(schedule_info["effective_at"])
            ).strftime("%H:%M")
        except (TypeError, ValueError):
            effective_target = None
    if schedule_info is None:
        # Direct/internal round callers still need the same calendar authority
        # as the scheduler. If it cannot prove the session target, fail closed.
        try:
            target_info = effective_target_for_session(
                date.fromisoformat(session_date),
                str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                calendar_client=deps.calendar_client or runtime.get("calendar_client"),
                calendar_rows=deps.calendar_rows or runtime.get("calendar_rows"),
            )
            effective_target = str(target_info["effective_target"])
        except Exception:
            effective_target = None
    if effective_target:
        _session_submit_allowed = make_session_submit_guard(
            session_date=session_date,
            effective_target=str(effective_target),
            now_fn=deps.now_fn,
        )
    else:
        _session_submit_allowed = lambda: False

    # R02 Layer 2: stop, observation window, and this session's exposure
    # window are re-checked inside recovery after its broker GET lookups.
    def _recovery_can_submit() -> bool:
        return (
            _control_stop_reason(deps, ends_at) is None
            and _session_submit_allowed()
        )

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

    if resume_without_new_llm:
        plan = None
    elif screening_plan is not None:
        plan = screening_plan
    elif _session_submit_allowed():
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
            scan_stats = getattr(plan, "scan_stats", None)
            if isinstance(scan_stats, dict):
                screening_evidence = journal.setdefault("screening", {})
                screening_evidence["scan_stats"] = scan_stats
                screening_evidence["stop_reason"] = getattr(plan, "reason", None)
                save_round_journal(run_id, journal)
            raise LongRunStop(
                "SCREENING_STOPPED",
                f"screening stopped the round: {plan.stop_reason_text()}",
            )
    else:
        plan = None
        if screening_plan is None and not resume_without_new_llm:
            journal.setdefault("screening", {})["skipped_reason"] = (
                "SESSION_SUBMISSION_DEADLINE"
            )
            save_round_journal(run_id, journal)

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
        screening_summary = {
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
        if ab_mode:
            screening_summary.update({
                "selection_hash": getattr(plan, "selection_hash", None),
                "config_fingerprint": (getattr(plan, "selection", None) or {}).get("config_fingerprint"),
                "data_feed": (getattr(plan, "selection", None) or {}).get("data_feed"),
            })
        journal["screening"] = screening_summary
        for symbol in journal["screening"]["deep_analysis_set"]:
            journal["symbols"].setdefault(symbol, {
                "status": SYMBOL_PENDING, "analysis_run_ref": None, "signal": None,
                "trade_intent": None, "execution_result_summary": None,
            })
        save_round_journal(run_id, journal)
        log_event(run_id, "round_screening",
                  {"session": session_date, "cached": journal["screening"]["cached"],
                   "universe": len(journal["screening"]["deep_analysis_set"])})

    prepared = _PreparedDailyRound(
        run_id=run_id,
        session_date=session_date,
        long_cfg=long_cfg,
        runtime=runtime,
        schedule_info=schedule_info,
        deps=deps,
        ends_at=ends_at,
        journal=journal,
        service=service,
        graph_factory=graph_factory,
        broker_factory=broker_factory,
        recovery_can_submit=_recovery_can_submit,
        session_submit_allowed=_session_submit_allowed,
    )
    if ab_prepare_only:
        return prepared

    # Single-backend mode preserves its existing analyze-and-execute loop.
    stop_reason = _run_prepared_round_symbols(prepared)
    return _finish_prepared_daily_round(prepared, stop_reason)


AB_BACKENDS = ("traders", "berkshire")


def _ab_arm_run_id(run_id: str, backend: str) -> str:
    return f"{run_id}-{backend}"


def _ab_selection_hash(selection: Dict[str, Any]) -> str:
    canonical = json.dumps(selection, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ab_selection_artifact_path(run_id: str, root: str | Path, session_date: str) -> Path:
    from tradingagents.dataflows.utils import safe_ticker_component

    return (
        Path(root) / "shared" / safe_ticker_component(session_date)
        / "screening_selection.json"
    )


def _read_ab_selection_artifact(
    path: Path, *, session_date: str, expected_config_fingerprint: Optional[str] = None,
    expected_top20_count: int = 20,
) -> Optional[Dict[str, Any]]:
    payload = read_json(path)
    if payload is None:
        if path.exists():
            raise LongRunStop("STATE_CORRUPT", f"shared A/B selection artifact is unreadable: {path}")
        return None
    if not isinstance(payload, dict) or payload.get("status") != "SCREENING_COMPLETE":
        raise LongRunStop("STATE_CORRUPT", f"shared A/B selection artifact is invalid: {path}")
    selection = payload.get("selection")
    if not isinstance(selection, dict):
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection artifact has no selection object")
    digest = _ab_selection_hash(selection)
    if payload.get("selection_hash") != digest:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection hash mismatch")
    if payload.get("session_date") != session_date:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection session mismatch")
    if expected_config_fingerprint and selection.get("config_fingerprint") != expected_config_fingerprint:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection config fingerprint changed")
    try:
        from datetime import date as _date

        selected_day = _date.fromisoformat(str(selection.get("trading_date") or ""))
        as_of = _date.fromisoformat(str(selection.get("as_of") or ""))
    except ValueError as exc:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection has invalid dates") from exc
    if selected_day.isoformat() != session_date or as_of > selected_day:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection contains a future or mismatched date")
    if payload.get("as_of") != selection.get("as_of"):
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection cutoff metadata mismatch")
    from tradingagents.screening.selection_store import SCREENING_DATA_FEED

    if selection.get("data_feed") != SCREENING_DATA_FEED:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection data feed changed")
    top40 = selection.get("top40")
    top20 = selection.get("top20")
    if not isinstance(top40, list) or not isinstance(top20, list) or len(top20) != expected_top20_count:
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection Top40/Top20 is invalid")
    top40_symbols = {
        row.get("symbol") for row in top40
        if isinstance(row, dict) and isinstance(row.get("symbol"), str)
    }
    top20_symbols = [
        row.get("symbol") for row in top20 if isinstance(row, dict)
    ]
    if (
        len(top20_symbols) != len(top20)
        or any(not isinstance(symbol, str) or symbol not in top40_symbols for symbol in top20_symbols)
        or len(set(top20_symbols)) != len(top20_symbols)
    ):
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection Top20 membership is invalid")
    from tradingagents.screening.selection_store import _integrity_digest

    if selection.get("integrity") != _integrity_digest(selection):
        raise LongRunStop("STATE_CORRUPT", "shared A/B selection seal mismatch")
    return payload


def run_ab_daily_round(
    *,
    run_id: str,
    session_date: str,
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    schedule_info: Optional[Dict[str, Any]] = None,
    deps: Optional[LongRunDeps] = None,
    ends_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run one shared-screening A/B round with deterministic symbol pairing."""
    deps = deps or LongRunDeps()
    from tradingagents.screening.pipeline import (
        RoundPlan,
        prepare_screening_round_from_selection,
    )

    root = long_cfg.get("ab_results_root")
    if not root:
        raise LongRunStop("CONFIG_INVALID", "A/B observation has no results root")
    root = str(root)
    arm_runtimes = {
        backend: apply_ab_backend_runtime_paths(runtime, backend, root)
        for backend in AB_BACKENDS
    }
    journal = load_round_journal(run_id, session_date)
    if journal is None:
        journal_path = round_path(run_id, session_date)
        if journal_path.exists():
            raise LongRunStop("STATE_CORRUPT", f"A/B round journal is unreadable: {journal_path}")
        journal = new_round_journal(
            session_date, [],
            scheduled_at=(schedule_info or {}).get("effective_at", ""),
            schedule_adjustment=(schedule_info or {}).get("schedule_adjustment", "NONE"),
        )
        journal["mode"] = "ab"
        journal["shared_screening"] = {"status": "SCREENING_PENDING"}
        journal["arms"] = {
            backend: {"run_id": _ab_arm_run_id(run_id, backend), "status": "PENDING"}
            for backend in AB_BACKENDS
        }
        save_round_journal(run_id, journal)
    elif journal.get("mode") != "ab":
        raise LongRunStop("STATE_CORRUPT", "A/B round journal mode mismatch")
    elif journal.get("status") in ("COMPLETED", "MISSED", "STOPPED"):
        if journal.get("status") == "COMPLETED":
            return journal
        raise LongRunStop("SESSION_SETTLED", f"{session_date} is already {journal.get('status')}")

    artifact_path = _ab_selection_artifact_path(run_id, root, session_date)
    from tradingagents.screening.llm import resolve_screening_config
    from tradingagents.screening.selection_store import SelectionStore, default_selection_cache_path

    screening_store = SelectionStore(default_selection_cache_path(arm_runtimes["traders"]))
    resolved_screening = resolve_screening_config(arm_runtimes["traders"])
    expected_fingerprint = screening_store.config_fingerprint(
        arm_runtimes["traders"], resolved_screening.get("spec")
    )
    selection_record = _read_ab_selection_artifact(
        artifact_path, session_date=session_date,
        expected_config_fingerprint=expected_fingerprint,
        expected_top20_count=int(arm_runtimes["traders"].get("screening_select_n", 20)),
    )
    shared_plan = None
    if selection_record is None:
        if (journal.get("shared_screening") or {}).get("status") == "SCREENING_COMPLETE":
            raise LongRunStop("STATE_CORRUPT", "completed shared selection artifact is missing")
        if _control_stop_reason(deps, ends_at):
            return journal
        effective_target = (schedule_info or {}).get("effective_target")
        if not effective_target:
            try:
                target_info = effective_target_for_session(
                    date.fromisoformat(session_date),
                    str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                    calendar_client=deps.calendar_client or runtime.get("calendar_client"),
                    calendar_rows=deps.calendar_rows or runtime.get("calendar_rows"),
                )
                effective_target = target_info["effective_target"]
            except Exception:
                effective_target = None
        if not effective_target or not make_session_submit_guard(
            session_date=session_date, effective_target=str(effective_target),
            now_fn=deps.now_fn,
        )():
            journal["shared_screening"] = {
                "status": "SCREENING_SKIPPED", "reason": "SESSION_SUBMISSION_DEADLINE"
            }
            save_round_journal(run_id, journal)
            return journal
        _apply_runtime_config(arm_runtimes["traders"])
        _validate_long_run_execution_config(arm_runtimes["traders"])
        try:
            from tradingagents.safety import get_safety_guard

            budget = get_safety_guard().check_llm_budget()
            if not budget.allowed:
                raise LongRunStop("LLM_BUDGET_EXHAUSTED", f"before shared screening: {'; '.join(budget.reasons)}")
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop("LLM_BUDGET_EXHAUSTED", f"shared screening budget check unavailable: {exc}")

        screening_fn = deps.screening_fn or _default_screening
        shared_plan = _screening_with_audit_scope(
            screening_fn, arm_runtimes["traders"], run_id, session_date
        )
        if getattr(shared_plan, "stopped", False):
            raise LongRunStop(
                "SCREENING_STOPPED",
                f"shared screening stopped: {shared_plan.stop_reason_text()}",
            )
        selection = getattr(shared_plan, "selection", None)
        if not isinstance(selection, dict):
            raise LongRunStop("SCREENING_STOPPED", "shared screening returned no frozen selection")
        if str(selection.get("trading_date") or "") != session_date:
            raise LongRunStop(
                "SCREENING_STOPPED",
                f"shared selection date {selection.get('trading_date')!r} does not match {session_date}",
            )
        selection_record = {
            "status": "SCREENING_COMPLETE",
            "session_date": session_date,
            "as_of": selection.get("as_of"),
            "cached": bool(getattr(shared_plan, "cached", False)),
            "selection_hash": _ab_selection_hash(selection),
            "selection": selection,
        }
        # Artifact first, state second. If the process dies between writes,
        # resume trusts this sealed/hash-checked selection without a new scan.
        atomic_write_json(artifact_path, selection_record)
        journal["shared_screening"] = {
            "status": "SCREENING_COMPLETE",
            "selection_path": str(artifact_path),
            "selection_hash": selection_record["selection_hash"],
            "selection_date": selection.get("trading_date"),
            "as_of": selection.get("as_of"),
            "cached": selection_record.get("cached", False),
            "config_fingerprint": selection.get("config_fingerprint"),
            "data_feed": selection.get("data_feed"),
            "top40": [row.get("symbol") for row in selection.get("top40", []) if isinstance(row, dict)],
            "top20": [row.get("symbol") for row in selection.get("top20", []) if isinstance(row, dict)],
        }
        journal["status"] = "RUNNING"
        if not journal.get("started_at"):
            journal["started_at"] = utc_now_iso()
        save_round_journal(run_id, journal)
    else:
        selection = selection_record["selection"]
        journal["shared_screening"] = {
            "status": "SCREENING_COMPLETE",
            "selection_path": str(artifact_path),
            "selection_hash": selection_record["selection_hash"],
            "selection_date": selection.get("trading_date"),
            "as_of": selection.get("as_of"),
            "cached": selection_record.get("cached", False),
            "config_fingerprint": selection.get("config_fingerprint"),
            "data_feed": selection.get("data_feed"),
            "top40": [row.get("symbol") for row in selection.get("top40", []) if isinstance(row, dict)],
            "top20": [row.get("symbol") for row in selection.get("top20", []) if isinstance(row, dict)],
        }
        journal["status"] = "RUNNING"
        if not journal.get("started_at"):
            journal["started_at"] = utc_now_iso()
        save_round_journal(run_id, journal)

    # Re-establish the exact selection cache consumed by the existing
    # execution entry gate if a crash removed it. Never replace a different
    # or unreadable file with a new selection.
    selection_store = screening_store
    cached_selection = selection_store.load_raw()
    if cached_selection is None:
        if selection_store.path.exists():
            raise LongRunStop("STATE_CORRUPT", "shared screening cache is unreadable")
        selection_store.save(selection)
    elif _ab_selection_hash(cached_selection) != selection_record["selection_hash"]:
        raise LongRunStop("STATE_CORRUPT", "shared screening cache differs from frozen A/B selection")

    prepared_arms: Dict[str, _PreparedDailyRound] = {}
    arm_plans: Dict[str, Any] = {}

    for backend in AB_BACKENDS:
        arm_id = _ab_arm_run_id(run_id, backend)
        prior = load_round_journal(arm_id, session_date)
        arm_path = round_path(arm_id, session_date)
        if prior is None and arm_path.exists():
            raise LongRunStop("STATE_CORRUPT", f"A/B arm journal is unreadable: {arm_path}")
        arm_entry = journal["arms"].setdefault(backend, {"run_id": arm_id})
        arm_entry["run_id"] = arm_id
        if prior is not None and prior.get("status") == "COMPLETED":
            arm_entry["status"] = "COMPLETED"
            prior_hash = (prior.get("screening") or {}).get("selection_hash")
            if prior_hash and prior_hash != selection_record["selection_hash"]:
                raise LongRunStop("STATE_CORRUPT", f"{backend} completed arm selection hash changed")
            arm_entry["selection_hash"] = prior_hash or selection_record["selection_hash"]
            save_round_journal(run_id, journal)
            continue
        if arm_entry.get("status") == "COMPLETED":
            raise LongRunStop(
                "STATE_CORRUPT",
                f"coordinator marks {backend} complete but its arm journal does not",
            )

        frozen_symbols = arm_entry.get("candidate_symbols")
        if isinstance(frozen_symbols, list):
            top20_symbols = [str(row.get("symbol")) for row in selection.get("top20", [])]
            if frozen_symbols[:len(top20_symbols)] != top20_symbols:
                raise LongRunStop("STATE_CORRUPT", f"{backend} frozen candidate pool changed")
            if arm_entry.get("selection_hash") != selection_record["selection_hash"]:
                raise LongRunStop("STATE_CORRUPT", f"{backend} frozen selection hash changed")
            arm_plan = RoundPlan(
                selection=selection,
                selection_date=str(selection.get("trading_date")),
                as_of=str(selection.get("as_of")),
                cached=bool(selection_record.get("cached", False)),
                entry_allowed=True,
                top20=list(selection.get("top20") or []),
                overlap_holdings=list(arm_entry.get("overlap_holdings") or []),
                extra_holdings=list(arm_entry.get("extra_holdings") or []),
                blocked_holdings=list(arm_entry.get("blocked_holdings") or []),
                deep_analysis_set=list(frozen_symbols),
                scan_stats=selection.get("stats"),
                screening_description="shared A/B screening selection",
                selection_hash=selection_record["selection_hash"],
            )
        elif backend == "traders" and shared_plan is not None:
            arm_plan = shared_plan
        else:
            _apply_runtime_config(arm_runtimes[backend])
            arm_plan = prepare_screening_round_from_selection(
                arm_runtimes[backend], selection, session_date=session_date
            )
        if getattr(arm_plan, "stopped", False):
            raise LongRunStop("SCREENING_STOPPED", f"{backend} held-review plan stopped: {arm_plan.stop_reason_text()}")
        arm_plan.cached = bool(selection_record.get("cached", False))
        if arm_plan.selection_hash != selection_record["selection_hash"]:
            raise LongRunStop("STATE_CORRUPT", f"{backend} arm plan does not match shared selection")
        arm_entry["candidate_symbols"] = list(arm_plan.deep_analysis_set or [])
        arm_entry["overlap_holdings"] = list(arm_plan.overlap_holdings or [])
        arm_entry["extra_holdings"] = list(arm_plan.extra_holdings or [])
        arm_entry["blocked_holdings"] = list(arm_plan.blocked_holdings or [])
        arm_entry["selection_hash"] = selection_record["selection_hash"]
        arm_entry["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        prepared = run_daily_round(
            run_id=arm_id,
            session_date=session_date,
            long_cfg={
                **long_cfg,
                "_ab_mode": True,
                "_ab_prepare_only": True,
                "_ab_screening_plan": arm_plan,
            },
            runtime=arm_runtimes[backend],
            schedule_info=schedule_info,
            deps=deps,
            ends_at=ends_at,
        )
        if isinstance(prepared, _PreparedDailyRound):
            prepared_arms[backend] = prepared
            arm_plans[backend] = arm_plan
        elif isinstance(prepared, dict) and prepared.get("status") == "COMPLETED":
            arm_entry["status"] = "COMPLETED"
            arm_entry["selection_hash"] = selection_record["selection_hash"]
        else:
            arm_entry["status"] = "RUNNING"
        save_round_journal(run_id, journal)

    if any(
        journal["arms"].get(backend, {}).get("status") != "COMPLETED"
        and backend not in prepared_arms
        for backend in AB_BACKENDS
    ):
        journal["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        return journal

    def _yield_ab_round() -> Dict[str, Any]:
        journal["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        return journal

    top20_symbols = [str(row["symbol"]) for row in selection.get("top20", [])]
    def _arm_symbol_entry(backend: str, symbol: str) -> Dict[str, Any]:
        arm_journal = (prepared_arms[backend].journal if backend in prepared_arms
                       else load_round_journal(_ab_arm_run_id(run_id, backend), session_date) or {})
        return (arm_journal.get("symbols") or {}).get(symbol) or {}

    def _pair_evidence_sha(symbol: str) -> Optional[str]:
        pair = journal.setdefault("pair_evidence", {}).setdefault(symbol, {})
        hashes = {pair["sha256"]} if pair.get("sha256") else set()
        for backend in AB_BACKENDS:
            sha = _arm_symbol_entry(backend, symbol).get("evidence_packet_sha256")
            if sha:
                hashes.add(sha)
        if len(hashes) > 1:
            raise LongRunStop("STATE_CORRUPT", f"A/B evidence hash differs for {symbol}")
        if not hashes:
            return None
        sha = next(iter(hashes))
        pair["sha256"] = sha
        save_round_journal(run_id, journal)
        return sha

    # Pair by symbol: both analysis intents are durable before either account
    # executes. Every switch reapplies its arm config and safety guard.
    for symbol in top20_symbols:
        import hashlib
        first_arm = int.from_bytes(hashlib.sha256(f"{session_date}:{symbol}".encode()).digest()[:8], "big") % 2
        pair_order = AB_BACKENDS if first_arm == 0 else tuple(reversed(AB_BACKENDS))
        if all(journal["arms"].get(backend, {}).get("status") == "COMPLETED"
               for backend in AB_BACKENDS):
            break
        for backend in pair_order:
            prepared = prepared_arms.get(backend)
            if prepared is None:
                continue
            sha = _pair_evidence_sha(symbol)
            entry = prepared.journal["symbols"].get(symbol) or {}
            if sha and not entry.get("evidence_packet_sha256"):
                entry["evidence_packet_sha256"] = sha
                save_round_journal(prepared.run_id, prepared.journal)
            stop_reason = _run_prepared_round_symbols(
                prepared, phase="analysis", symbols=[symbol], reapply_runtime=True,
            )
            if stop_reason:
                return _yield_ab_round()

        sha = _pair_evidence_sha(symbol)
        if sha is None:
            statuses = [_arm_symbol_entry(backend, symbol).get("status") for backend in AB_BACKENDS]
            if all(status in (None, SYMBOL_PENDING, SYMBOL_DONE, SYMBOL_FAILED) for status in statuses):
                continue
            raise LongRunStop("STATE_CORRUPT", f"A/B evidence was not pinned for {symbol}")
        entries = [_arm_symbol_entry(backend, symbol) for backend in AB_BACKENDS]
        routes = {backend: list(_arm_symbol_entry(backend, symbol).get("analysis_routes") or [])
                  for backend in AB_BACKENDS}
        pair = journal["pair_evidence"][symbol]
        pair["analysis_routes"] = routes
        pair["route_contaminated"] = any(
            route.get("route") == "fallback" for arm_routes in routes.values()
            for route in arm_routes
        )
        pair["route_status"] = (
            "fallback" if pair["route_contaminated"] else
            "primary" if all(routes.values()) else "unknown"
        )
        save_round_journal(run_id, journal)
        if any(entry.get("evidence_packet_sha256") != sha for entry in entries):
            raise LongRunStop("STATE_CORRUPT", f"A/B arm evidence is missing or changed for {symbol}")
        if any(entry.get("status") == SYMBOL_FAILED for entry in entries):
            journal["pair_evidence"][symbol]["status"] = "FAILED"
            save_round_journal(run_id, journal)
            continue

        for backend in pair_order:
            prepared = prepared_arms.get(backend)
            if prepared is None:
                continue
            entry = prepared.journal["symbols"].get(symbol) or {}
            if entry.get("status") not in (SYMBOL_ANALYZED, SYMBOL_EXECUTING):
                continue
            stop_reason = _run_prepared_round_symbols(
                prepared, phase="execution", symbols=[symbol], reapply_runtime=True,
            )
            if stop_reason:
                return _yield_ab_round()

    # Positions outside the shared Top20 have no counterpart. Keep their
    # existing-account management after the paired entry universe.
    for backend in AB_BACKENDS:
        prepared = prepared_arms.get(backend)
        if prepared is None:
            continue
        for symbol in arm_plans[backend].extra_holdings:
            stop_reason = _run_prepared_round_symbols(
                prepared, phase="analysis", symbols=[symbol], reapply_runtime=True,
            )
            if stop_reason:
                return _yield_ab_round()
            entry = prepared.journal["symbols"].get(symbol) or {}
            if entry.get("status") in (SYMBOL_ANALYZED, SYMBOL_EXECUTING):
                stop_reason = _run_prepared_round_symbols(
                    prepared, phase="execution", symbols=[symbol], reapply_runtime=True,
                )
                if stop_reason:
                    return _yield_ab_round()

    for backend, prepared in prepared_arms.items():
        arm_journal = _finish_prepared_daily_round(
            prepared, None, reapply_runtime=True,
        )
        arm_entry = journal["arms"][backend]
        arm_entry["status"] = (
            "COMPLETED" if arm_journal.get("status") == "COMPLETED" else "RUNNING"
        )
        arm_entry["selection_hash"] = selection_record["selection_hash"]
        save_round_journal(run_id, journal)

    if any(journal["arms"].get(backend, {}).get("status") != "COMPLETED" for backend in AB_BACKENDS):
        journal["status"] = "RUNNING"
        save_round_journal(run_id, journal)
        return journal
    journal["status"] = "COMPLETED"
    journal["screening"] = {
        "selection_date": selection.get("trading_date"),
        "as_of": selection.get("as_of"),
        "cached": bool(selection_record.get("cached", False)),
        "top20": [
            {"symbol": row.get("symbol"), "rank": row.get("rank"),
             "score": row.get("screening_score"), "reason": row.get("short_reason")}
            for row in selection.get("top20", []) if isinstance(row, dict)
        ],
        "selection_hash": selection_record["selection_hash"],
        "shared_selection_path": str(artifact_path),
    }
    journal["finished_at"] = utc_now_iso()
    save_round_journal(run_id, journal)
    log_event(run_id, "ab_round_completed", {"session": session_date,
                                               "selection_hash": selection_record["selection_hash"]})
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
    return _round_support._screening_with_audit_scope(
        screening_fn,
        runtime,
        run_id,
        session_date,
        SCREENING_AUDIT_SYMBOL=SCREENING_AUDIT_SYMBOL,
    )


def _execute_intent(deps, service, symbol, intent, notional, *, run_id, session_date,
                    allow_shorts=False, can_submit: Optional[Callable[[], bool]] = None):
    return _round_support._execute_intent(
        deps,
        service,
        symbol,
        intent,
        notional,
        run_id=run_id,
        session_date=session_date,
        allow_shorts=allow_shorts,
        can_submit=can_submit,
    )


def _record_execution(journal, run_id, symbol, result) -> None:
    return _round_support._record_execution(
        journal,
        run_id,
        symbol,
        result,
        summarize_execution_result=summarize_execution_result,
        SYMBOL_EXECUTING=SYMBOL_EXECUTING,
        SYMBOL_DONE=SYMBOL_DONE,
        save_round_journal=save_round_journal,
        log_event=log_event,
    )


def _check_execution_hard_stop(symbol: str, result: Dict[str, Any]) -> None:
    """Unknown/ambiguous broker state or a paused account stops everything.

    N08: safety circuit breakers (daily loss, drawdown, consecutive
    rejections) are distinguished by their stable reason codes — the
    observation stops with SAFETY_CIRCUIT_BREAKER instead of continuing to
    analyze/queue. Single-order refusals (notional cap, concentration) do
    NOT stop the observation.
    """
    return _round_support._check_execution_hard_stop(
        symbol,
        result,
        LongRunStop=LongRunStop,
    )


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
    return _sessions.sweep_missed_sessions(
        run_id=run_id,
        expected=expected,
        today=today,
        run_dir=run_dir,
        read_json=read_json,
        TERMINAL_ROUND_STATUSES=TERMINAL_ROUND_STATUSES,
        utc_now_iso=utc_now_iso,
        atomic_write_json=atomic_write_json,
        LongRunStop=LongRunStop,
        new_round_journal=new_round_journal,
        log_event=log_event,
    )


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
    return _sessions._run_scheduler_operation_with_retry(
        operation,
        deps=deps,
        run_id=run_id,
        label=label,
        SCHEDULER_RETRY_ATTEMPTS=SCHEDULER_RETRY_ATTEMPTS,
        SCHEDULER_RETRY_DELAY_SECONDS=SCHEDULER_RETRY_DELAY_SECONDS,
        LongRunStop=LongRunStop,
        log_event=log_event,
    )


def should_extend_continuous_window(
    *,
    state: Dict[str, Any],
    ends_at: datetime,
    now: datetime,
    run_time_et: str,
    calendar_client: Any = None,
    calendar_rows: Optional[List[Any]] = None,
) -> bool:
    """Continuous mode: prove the frozen chunk's scheduling horizon is spent.

    The chunk boundary must be crossed BEFORE the boundary date's own target
    when that date is a trading session: growing the window only once
    ``now >= ends_at`` freezes the boundary session after its close, and the
    scheduler then settles it MISSED without ever running it.

    Only two situations authorize early growth:

    * the chunk holds no session date at all (nothing schedulable inside it), or
    * the chunk's LAST session date has already passed its own effective
      target, so no opportunity inside this chunk can still open.

    A weekend or holiday therefore cannot grow the window: growth waits for
    the chunk's last session target, and an extension that appends no session
    falls back to the normal bounded wait (see ``run_observation_loop``)
    instead of spinning. An unprovable target (calendar authority down) never
    grows the window; it returns False and the caller keeps waiting.
    """
    if not state.get("continuous", False):
        return False
    eastern = eastern_now(now)
    tz = eastern.tzinfo
    end_day = ends_at.astimezone(tz).date().isoformat()
    chunk_sessions = [
        s for s in (state.get("expected_sessions") or []) if s < end_day
    ]
    if not chunk_sessions:
        return True
    try:
        info = effective_target_for_session(
            date.fromisoformat(chunk_sessions[-1]),
            run_time_et,
            calendar_client=calendar_client,
            calendar_rows=calendar_rows,
        )
        naive = datetime.strptime(
            f"{info['session_date']} {info['effective_target']}", "%Y-%m-%d %H:%M"
        )
        effective = tz.localize(naive)
    except Exception:
        # Cannot prove the chunk is finished: never grow early on a guess.
        return False
    return effective <= eastern


def extend_continuous_window(
    state: Dict[str, Any], long_cfg: Dict[str, Any], deps: "LongRunDeps"
) -> Dict[str, Any]:
    """Continuous mode: grow the SAME run's scheduling window by one chunk.

    The run continues under the same run_id — journal, round history and
    account history are preserved; only ends_at and expected_sessions grow.
    A restart re-enters here and extends again, so a crash never forces a new
    run and no fake "999999 days" horizon is ever persisted. Fail-closed: a
    calendar-authority failure raises (the caller converts it to a hard stop)
    rather than silently ending the observation.
    """
    run_id = state["run_id"]
    eastern_tz = eastern_now(deps.now_fn()).tzinfo
    old_ends = datetime.fromisoformat(state["ends_at"])
    if old_ends.tzinfo is None:
        old_ends = old_ends.replace(tzinfo=timezone.utc)
    chunk = int(long_cfg.get("duration_calendar_days") or DEFAULT_DURATION_CALENDAR_DAYS)
    expected = list(state.get("expected_sessions") or [])
    old_end_date = old_ends.astimezone(eastern_tz).date()
    last_session = (
        date.fromisoformat(expected[-1])
        if expected
        else old_end_date - timedelta(days=1)
    )
    new_ends = old_ends + timedelta(days=chunk)
    # Keep the initial window's half-open [start, end) semantics: the
    # calendar query is inclusive, but a session ON the new ends_at date is
    # not executable in this chunk (the scheduler only runs sessions with
    # date < ends_at). Including it would settle it MISSED after the next
    # extension without ever scheduling it.
    new_end_date = new_ends.astimezone(eastern_tz).date()
    new_sessions = [
        d for d in fetch_session_dates(
            last_session + timedelta(days=1),
            new_end_date,
            client=deps.calendar_client,
        )
        if d < new_end_date
    ]
    for day in new_sessions:
        iso = day.isoformat()
        # Append only sessions strictly after the current last expected one.
        if not expected or iso > expected[-1]:
            expected.append(iso)
    state["expected_sessions"] = expected
    state["ends_at"] = new_ends.isoformat()
    save_active_state(state)
    # Keep the run manifest the durable mirror of the extended window.
    manifest_path = run_dir(run_id) / "manifest.json"
    manifest = read_json(manifest_path) or {}
    manifest["ends_at"] = state["ends_at"]
    manifest["expected_sessions"] = list(expected)
    manifest["continuous"] = True
    atomic_write_json(manifest_path, manifest)
    log_event(run_id, "continuous_window_extended", {
        "old_ends_at": old_ends.isoformat(),
        "new_ends_at": state["ends_at"],
        "sessions_appended": len(new_sessions),
    })
    return state


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
                + ("  python -m cli.main long-run --mode ab"
                   if state.get("mode") == "ab" else "  python -m cli.main long-run")
            )
            return {"outcome": "interrupted", "run_id": run_id}

        now = deps.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now >= ends_at:
            if state.get("continuous", False):
                # Continuous mode: never auto-finalize. Grow the same run's
                # window by duration chunks (authoritative calendar) until now
                # is inside it again, then keep scheduling normally.
                try:
                    while ends_at <= now:
                        extend_continuous_window(state, long_cfg, deps)
                        ends_at = datetime.fromisoformat(state["ends_at"])
                        if ends_at.tzinfo is None:
                            ends_at = ends_at.replace(tzinfo=timezone.utc)
                except Exception as exc:
                    stop = LongRunStop(
                        "CALENDAR_UNAVAILABLE",
                        f"continuous window extension failed: {exc}",
                    )
                    return finalize_observation(
                        state, long_cfg, runtime, deps,
                        final_status="STOPPED",
                        stop_code=stop.code, stop_detail=stop.detail,
                    )
                continue
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
                expected_sessions=state.get("expected_sessions") or None,
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
            # Continuous mode: the next chunk's sessions — including a
            # trading day exactly ON the boundary — must be frozen BEFORE
            # that day's target, otherwise it is added post-close and settled
            # MISSED. Grow the window as soon as the current chunk is
            # provably spent (never merely because nothing is due now).
            if (_extension_retry_due(state, now)
                and should_extend_continuous_window(
                state=state, ends_at=ends_at, now=now,
                run_time_et=str(long_cfg.get("run_time_et") or DEFAULT_RUN_TIME_ET),
                calendar_client=deps.calendar_client,
                calendar_rows=deps.calendar_rows,
            )):
                before = len(state.get("expected_sessions") or [])
                try:
                    extend_continuous_window(state, long_cfg, deps)
                except Exception as exc:
                    stop = LongRunStop(
                        "CALENDAR_UNAVAILABLE",
                        f"continuous window extension failed: {exc}",
                    )
                    return finalize_observation(
                        state, long_cfg, runtime, deps,
                        final_status="STOPPED",
                        stop_code=stop.code, stop_detail=stop.detail,
                    )
                next_ends = datetime.fromisoformat(state["ends_at"])
                if next_ends.tzinfo is None:
                    next_ends = next_ends.replace(tzinfo=timezone.utc)
                ends_at = next_ends
                if len(state.get("expected_sessions") or []) > before:
                    state.pop("extension_retry_after", None)
                    save_active_state(state)
                    # A new session is schedulable: re-enter the scheduler
                    # instead of sleeping, so the boundary date is frozen
                    # well before its target.
                    continue
                # Nothing appended (a range with no session at all): never
                # spin. Back off for one bounded wait, then let a later pass
                # grow the following chunk — a chunk boundary that lands on a
                # weekend must not disable continuous growth for the rest of
                # the run.
                state["extension_retry_after"] = (
                    now + timedelta(seconds=EMPTY_EXTENSION_RETRY_SECONDS)
                ).isoformat()
                save_active_state(state)
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
            round_runner = run_ab_daily_round if state.get("mode") == "ab" else run_daily_round
            round_runner(
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
    return _reporting._load_snapshots(run_id, run_dir=run_dir)


def _load_rounds(run_id: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Readable round journals, plus session dates whose journal exists but
    could not be parsed (preserved as-is, never guessed through)."""
    return _reporting._load_rounds(run_id, run_dir=run_dir, read_json=read_json)


def _sum_unrealized(positions: Any) -> Optional[float]:
    return _reporting._sum_unrealized(positions)


def compute_drawdown(equities: List[float]) -> Dict[str, Any]:
    return _reporting.compute_drawdown(equities)


def aggregate_final_report(
    state: Dict[str, Any],
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministic Markdown+JSON inputs from persisted evidence only."""
    return _reporting.aggregate_final_report(
        state, long_cfg, runtime,
        _load_snapshots=_load_snapshots, _load_rounds=_load_rounds,
        compute_drawdown=compute_drawdown, _sum_unrealized=_sum_unrealized,
        run_dir=run_dir, SYMBOL_DONE=SYMBOL_DONE, datetime=datetime,
    )


def aggregate_ab_final_report(
    state: Dict[str, Any], long_cfg: Dict[str, Any], runtime: Dict[str, Any]
) -> Dict[str, Any]:
    """Combine existing per-arm long-run reports and persisted shared decisions."""
    root = str(long_cfg.get("ab_results_root") or "")
    if not root:
        raise LongRunStop("CONFIG_INVALID", "A/B report has no results root")
    arm_reports: Dict[str, Dict[str, Any]] = {}
    for backend in AB_BACKENDS:
        arm_id = _ab_arm_run_id(state["run_id"], backend)
        arm_runtime = apply_ab_backend_runtime_paths(runtime, backend, root)
        arm_state = {
            "run_id": arm_id, "status": state.get("status"),
            "stop": state.get("stop"), "started_at": state.get("started_at"),
            "ends_at": state.get("ends_at"),
            "expected_sessions": state.get("expected_sessions") or [],
            "restart_count": state.get("restart_count", 0),
            "baseline_commit": state.get("baseline_commit", "unknown"),
            "account_ref": ((state.get("arms") or {}).get(backend) or {}).get("account_ref"),
        }
        arm_reports[backend] = aggregate_final_report(arm_state, long_cfg, arm_runtime)

    top20_by_day: Dict[str, List[str]] = {}
    selection_hashes: Dict[str, str] = {}
    screening_cutoffs: Dict[str, Dict[str, Any]] = {}
    pair_routes: Dict[str, Dict[str, Any]] = {}
    signals: Dict[str, Dict[tuple[str, str], str]] = {b: {} for b in AB_BACKENDS}
    for session in state.get("expected_sessions") or []:
        coordinator = load_round_journal(state["run_id"], session)
        if not isinstance(coordinator, dict):
            continue
        screening = coordinator.get("screening") or {}
        shared = coordinator.get("shared_screening") or {}
        top20_by_day[session] = [
            str(row["symbol"]) for row in (screening.get("top20") or shared.get("top20") or [])
            if isinstance(row, dict) and row.get("symbol")
        ]
        if shared.get("selection_hash"):
            selection_hashes[session] = str(shared["selection_hash"])
        screening_cutoffs[session] = {
            "as_of": shared.get("as_of"),
            "data_feed": shared.get("data_feed"),
            "config_fingerprint": shared.get("config_fingerprint"),
            "top40": shared.get("top40") or [],
        }
        pair_routes[session] = {
            symbol: {"analysis_routes": pair.get("analysis_routes") or {},
                     "route_contaminated": bool(pair.get("route_contaminated")),
                     "route_status": pair.get("route_status", "unknown"),
                     "status": pair.get("status")}
            for symbol, pair in (coordinator.get("pair_evidence") or {}).items()
            if isinstance(pair, dict)
        }
        for backend in AB_BACKENDS:
            arm_round = load_round_journal(_ab_arm_run_id(state["run_id"], backend), session) or {}
            for symbol, entry in (arm_round.get("symbols") or {}).items():
                if entry.get("signal"):
                    signals[backend][(session, symbol)] = str(entry["signal"]).upper()

    turnover = []
    previous: Optional[set[str]] = None
    for session, symbols in sorted(top20_by_day.items()):
        current = set(symbols)
        if previous is not None:
            turnover.append({"session": session, "entered": sorted(current - previous),
                             "exited": sorted(previous - current)})
        previous = current
    shared_signals = set(signals["traders"]) & set(signals["berkshire"])
    disagreements = [
        {"session": session, "symbol": symbol,
         "traders": signals["traders"][(session, symbol)],
         "berkshire": signals["berkshire"][(session, symbol)]}
        for session, symbol in sorted(shared_signals)
        if signals["traders"][(session, symbol)] != signals["berkshire"][(session, symbol)]
    ]

    timing_gaps = []
    for session, symbols in sorted(top20_by_day.items()):
        arm_rounds = {
            backend: load_round_journal(_ab_arm_run_id(state["run_id"], backend), session) or {}
            for backend in AB_BACKENDS
        }
        for symbol in symbols:
            starts = {
                backend: ((arm_rounds[backend].get("symbols") or {}).get(symbol) or {}).get(
                    "execution_started_at"
                )
                for backend in AB_BACKENDS
            }
            gap_seconds = None
            if all(starts.values()):
                try:
                    parsed = {}
                    for backend, value in starts.items():
                        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                        parsed[backend] = timestamp
                    gap_seconds = round(abs((parsed["traders"] - parsed["berkshire"]).total_seconds()), 3)
                except (TypeError, ValueError, OverflowError):
                    gap_seconds = None
            timing_gaps.append({
                "session": session,
                "symbol": symbol,
                "traders_execution_started_at": starts["traders"],
                "berkshire_execution_started_at": starts["berkshire"],
                "execution_time_gap_seconds": gap_seconds,
            })
    available_gaps = [
        row["execution_time_gap_seconds"]
        for row in timing_gaps
        if row["execution_time_gap_seconds"] is not None
    ]
    timing_fairness = {
        "shared_top20_symbol_sessions": len(timing_gaps),
        "paired_execution_count": len(available_gaps),
        "mean_execution_time_gap_seconds": (
            round(sum(available_gaps) / len(available_gaps), 3) if available_gaps else None
        ),
        "max_execution_time_gap_seconds": max(available_gaps) if available_gaps else None,
        "by_symbol_session": timing_gaps,
    }

    comparison: Dict[str, Any] = {}
    for backend, report in arm_reports.items():
        account = report.get("account") or {}
        execution_db = report.get("execution_db") or {"available": False}
        order_statuses = execution_db.get("by_status") or {}
        rejected_orders = sum(
            int(count or 0) for status, count in order_statuses.items()
            if str(status).upper().startswith("REJECT")
        )
        failed_rows = [
            row for row in (report.get("decisions") or {}).get("per_symbol", [])
            if row.get("error") or row.get("status") == SYMBOL_FAILED
        ]
        positions = account.get("ending_positions")
        equity = account.get("ending_equity")
        exposure_pct = None
        if isinstance(positions, list) and equity not in (None, 0):
            values = []
            for position in positions:
                try:
                    value = float(position.get("market_value"))
                except (AttributeError, TypeError, ValueError):
                    values = []
                    break
                if not math.isfinite(value):
                    values = []
                    break
                values.append(abs(value))
            if values or not positions:
                exposure_pct = sum(values) / float(equity)
        comparison[backend] = {
            "account": "A" if backend == "traders" else "B",
            "account_ref": ((state.get("arms") or {}).get(backend) or {}).get("account_ref"),
            "starting_equity": account.get("starting_equity"),
            "ending_equity": account.get("ending_equity"),
            "equity_change": account.get("absolute_pl"),
            "equity_return": account.get("total_return"),
            "max_drawdown": account.get("max_drawdown"),
            "daily_equity": account.get("daily_equity_series") or [],
            "realized_pl": {"available": False, "reason": "not reliably exposed by broker snapshots"},
            "unrealized_pl": account.get("ending_unrealized_pl"),
            "positions": account.get("ending_positions"),
            "exposure_pct_of_equity": exposure_pct,
            "signals": (report.get("decisions") or {}).get("signal_counts") or {},
            "execution": report.get("execution") or {},
            "execution_db": execution_db,
            "rejected_order_rows": rejected_orders if execution_db.get("available") else None,
            "execution_failures": len(failed_rows),
            "llm_operations": report.get("llm_operations") or {"available": False},
            "safety": report.get("safety") or {},
            "trades": {"available": False, "reason": "no reliable round-trip trade attribution"},
            "fills": {"available": False, "reason": "distinct fill count is not exposed by the report contract"},
            "turnover": {"available": False, "reason": "order notional is not reliably available from persisted report data"},
            "wins_losses": {"available": False, "reason": "closed-trade outcomes are not reliably attributed"},
        }
    return {
        "run_id": state["run_id"], "mode": "ab",
        "final_status": state.get("status"), "stop": state.get("stop"),
        "started_at": state.get("started_at"), "ends_at": state.get("ends_at"),
        "duration_calendar_days": long_cfg.get("duration_calendar_days"),
        "restart_count": int(state.get("restart_count") or 0),
        "shared_screening": {"selection_hashes": selection_hashes,
                             "top20_by_day": top20_by_day,
                             "cutoffs_by_day": screening_cutoffs, "turnover": turnover},
        "arms": arm_reports, "comparison": comparison,
        "signal_disagreement": {"shared_symbol_session_count": len(shared_signals),
                                 "disagreements": disagreements},
        "pair_routes": pair_routes,
        "timing_fairness": timing_fairness,
        "metrics_unavailable": {
            "realized_pl": "broker snapshots do not provide reliable realized P/L attribution",
            "trades": "no reliable round-trip trade attribution is available",
            "fills": "the report contract does not expose distinct fill counts",
            "turnover": "order notional is not reliably available from persisted report data",
            "wins_losses": "closed-trade outcomes are not reliably attributed",
        },
    }


def write_ab_final_report(report: Dict[str, Any]) -> Tuple[str, str]:
    directory = run_dir(str(report["run_id"]))
    json_path = directory / "ab_final_report.json"
    md_path = directory / "ab_final_report.md"
    atomic_write_json(json_path, sanitize_for_log(report))
    lines = [f"# Long-run A/B Paper report — {report['run_id']}", "",
             f"- Status: {report.get('final_status')}",
             f"- Window: {report.get('started_at')} → {report.get('ends_at')}", "",
             "## Shared screening"]
    for session, symbols in sorted((report.get("shared_screening", {}).get("top20_by_day") or {}).items()):
        lines.append(f"- {session} Top20: {', '.join(symbols)}")
    lines += ["", "## Account comparison", "",
              "| Arm | Account | Start equity | End equity | Change | Return | Max drawdown |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for backend in AB_BACKENDS:
        row = report["comparison"][backend]
        lines.append(f"| {backend} | {row['account']} | {row['starting_equity']} | "
                     f"{row['ending_equity']} | {row['equity_change']} | "
                     f"{row['equity_return']} | {row['max_drawdown']} |")
    lines += ["", "## Signal disagreement", "",
              json.dumps(report.get("signal_disagreement"), ensure_ascii=False, sort_keys=True),
              "", "## Analysis routes by pair", "",
              json.dumps(report.get("pair_routes"), ensure_ascii=False, sort_keys=True),
              "", "## Timing fairness", "",
              json.dumps(report.get("timing_fairness"), ensure_ascii=False, sort_keys=True),
              "", "## Metrics marked unavailable"]
    for key, reason in report.get("metrics_unavailable", {}).items():
        lines.append(f"- {key}: {reason}")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(md_path), str(json_path)


def _fmt_pct(value: Any) -> str:
    return _reporting._fmt_pct(value)


def _fmt_usd(value: Any) -> str:
    return _reporting._fmt_usd(value)


def render_final_markdown(report: Dict[str, Any]) -> str:
    """Human-readable 30-day observation report (deterministic)."""
    return _reporting.render_final_markdown(
        report, _fmt_pct=_fmt_pct, _fmt_usd=_fmt_usd,
    )


def write_final_report(
    report: Dict[str, Any],
) -> Tuple[str, str]:
    """Persist final_report.json + final_report.md; return their paths."""
    return _reporting.write_final_report(
        report, run_dir=run_dir, atomic_write_json=atomic_write_json,
        sanitize_for_log=sanitize_for_log, render_final_markdown=render_final_markdown,
    )


def send_long_run_alert(
    subject: str, body: str, runtime: Optional[Dict[str, Any]],
    deps: Optional[LongRunDeps] = None,
) -> None:
    """Existing alert channel, failure-isolated (never affects trading)."""
    return _reporting.send_long_run_alert(subject, body, runtime, deps)


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
    if final_status == "COMPLETED":
        if state.get("mode") == "ab":
            root = str(long_cfg.get("ab_results_root") or "")
            if not root:
                raise LongRunStop("SETTLEMENT_UNRESOLVED", "A/B results root is unavailable")
            settlement_runtimes = [(backend, apply_ab_backend_runtime_paths(runtime, backend, root))
                                   for backend in AB_BACKENDS]
        else:
            settlement_runtimes = [(str(runtime.get("analysis_backend") or "traders"), runtime)]
        for backend, arm_runtime in settlement_runtimes:
            try:
                _apply_runtime_config(arm_runtime)
                service = (deps.execution_service_factory or _default_execution_service)()
                recovered = service.startup_recover(can_submit=lambda: False)
                if not recovered.get("success") or recovered.get("account_execution_state") != "CLEAN":
                    raise ValueError(f"{backend} reconciliation is not CLEAN: {recovered.get('reconciliation_reasons')}")
                unresolved = service.store.list_recoverable_orders()
                if unresolved:
                    raise ValueError(f"{backend} has {len(unresolved)} nonterminal execution orders")
            except Exception as exc:
                raise LongRunStop("SETTLEMENT_UNRESOLVED", str(exc)) from exc
    # Sessions in the window that never completed were missed: record them so
    # they stay in the denominator instead of silently disappearing. An
    # unfinished journal keeps its partial per-symbol evidence — those
    # analyses/executions really happened and the audit must show them.
    for session_day in sorted(state.get("expected_sessions") or []):
        journal = load_round_journal(run_id, session_day)
        if journal is not None and journal.get("status") in TERMINAL_ROUND_STATUSES:
            if state.get("mode") == "ab":
                for backend in AB_BACKENDS:
                    arm_id = _ab_arm_run_id(run_id, backend)
                    arm_journal = load_round_journal(arm_id, session_day)
                    arm_file = round_path(arm_id, session_day)
                    if arm_journal is None and arm_file.exists():
                        log_event(run_id, "ab_arm_round_unreadable", {
                            "session": session_day, "backend": backend,
                        })
                        continue
                    if arm_journal is not None and arm_journal.get("status") not in TERMINAL_ROUND_STATUSES:
                        arm_journal["status"] = "STOPPED"
                        arm_journal["stop_reason"] = "PARENT_TERMINAL_ARM_UNFINISHED"
                        arm_journal["finished_at"] = arm_journal.get("finished_at") or utc_now_iso()
                        save_round_journal(arm_id, arm_journal)
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
        if state.get("mode") == "ab":
            for backend in AB_BACKENDS:
                arm_id = _ab_arm_run_id(run_id, backend)
                arm_journal = load_round_journal(arm_id, session_day)
                arm_file = round_path(arm_id, session_day)
                if arm_journal is None and arm_file.exists():
                    log_event(run_id, "ab_arm_round_unreadable", {
                        "session": session_day, "backend": backend,
                    })
                    continue
                if arm_journal is None:
                    arm_journal = new_round_journal(session_day, [])
                if arm_journal.get("status") in TERMINAL_ROUND_STATUSES:
                    continue
                arm_journal["status"] = journal["status"]
                arm_journal["stop_reason"] = journal.get("stop_reason")
                arm_journal["finished_at"] = arm_journal.get("finished_at") or utc_now_iso()
                save_round_journal(arm_id, arm_journal)
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
    if state.get("mode") == "ab":
        root = str(long_cfg.get("ab_results_root") or "")
        for backend in AB_BACKENDS:
            arm_id = _ab_arm_run_id(run_id, backend)
            arm_runtime = apply_ab_backend_runtime_paths(runtime, backend, root)
            try:
                _apply_runtime_config(arm_runtime)
                broker_factory = deps.broker_client_factory or _default_broker_client
                final_snapshot = capture_account_snapshot(broker_factory())
                expected_ref = ((state.get("arms") or {}).get(backend) or {}).get("account_ref")
                if expected_ref and final_snapshot.get("account_ref") != expected_ref:
                    raise SnapshotUnavailable(f"{backend} final Paper account identity changed")
                append_jsonl(run_dir(arm_id) / "account_snapshots.jsonl",
                             {"phase": "final", **final_snapshot})
            except Exception as exc:
                log_event(run_id, "ab_final_snapshot_unavailable", {
                    "backend": backend,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                })
                print(f"[Phase-D A/B {backend}] Final snapshot unavailable: {exc}")
        report = aggregate_ab_final_report(state, long_cfg, runtime)
        md_path, json_path = write_ab_final_report(report)
    else:
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

    E07: the lock probe and the loop now share ONE runner lock. The
    previous probe-then-release-then-rewrite-then-relock sequence let a
    concurrent resume read the state between probe and rewrite, losing a
    restart_count increment (read-modify-write outside the lock); lock
    contention anywhere in the path now surfaces as ALREADY_RUNNING.
    """
    deps = deps or LongRunDeps()
    cfg = long_cfg or dict(default_long_run_config())
    try:
        with runner_lock():
            state = load_active_state()
            if state is None:
                raise LongRunStop(
                    "NO_ACTIVE_OBSERVATION", "no unfinished observation to resume"
                )
            state["restart_count"] = int(state.get("restart_count") or 0) + 1
            save_active_state(state)
            long_cfg_effective = long_cfg or dict(cfg, **(state.get("config") or {}))
            run_config = runtime or build_runtime_config(long_cfg_effective)
            return run_observation_loop(state, long_cfg_effective, run_config, deps)
    except RunnerLockBusy as exc:
        raise LongRunStop(
            "ALREADY_RUNNING", f"runner active while resuming observation"
        ) from exc
