"""Phase-D paths, durable local state, journals, and single-runner locking.

Named collaborators are supplied by the public long_run wrappers; this module
never imports the orchestration module or caches environment-derived paths.

Authoritative JSON (active state, round journals and final reports) is
synchronously durable: file contents and directory metadata are fsynced before
success is returned. Active-state deletion is durable too; I/O failures propagate.
JSONL telemetry is best effort: close flushes it, but events are not fsynced and
must never be used as resume or execution authority.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tradingagents.app_identity import app_home, get_env, validate_app_path


TERMINAL_ROUND_STATUSES = ("COMPLETED", "MISSED", "STOPPED")


def base_dir() -> Path:
    override = get_env("LONG_RUN_DIR")
    if override:
        return validate_app_path(override, field="long_run_dir")
    return app_home() / "long_run"


def config_path(*, base_dir: Callable[[], Path]) -> Path:
    return base_dir() / "config.json"


def active_path(*, base_dir: Callable[[], Path]) -> Path:
    return base_dir() / "active.json"


def lock_path(*, base_dir: Callable[[], Path]) -> Path:
    return base_dir() / "runner.lock"


def run_dir(run_id: str, *, base_dir: Callable[[], Path]) -> Path:
    return base_dir() / "runs" / run_id


def round_path(run_id: str, session_date: str, *, run_dir: Callable[[str], Path]) -> Path:
    return run_dir(run_id) / "rounds" / f"{session_date}.json"


def _looks_placeholder(value: Any, *, PLACEHOLDER_MARKERS: Tuple[str, ...]) -> bool:
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


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(path: Path) -> None:
    # Persist newly created ancestor entries as well as the final file rename.
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_directory(directory.parent)


def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    _mkdir_durable(path.parent)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
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


def append_jsonl(path: Path, record: Dict[str, Any], *, sanitize_for_log: Callable[[Any], Any]) -> None:
    """Append diagnostic evidence without a per-record durability barrier."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_for_log(record), ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def utc_now_iso(*, datetime: Any) -> str:
    return datetime.now(timezone.utc).isoformat()


class RunnerLockBusy(RuntimeError):
    pass


class GlobalRunnerLockBusy(RuntimeError):
    """Raised when another application-level runner already holds the global lock."""


def global_runner_lock_path() -> Path:
    return app_home() / "runner.global.lock"


def global_runner_lock(*, utc_now_iso: Callable[[], str]):
    """Application-wide runner lock: exactly one long-run / A/B Paper runner
    per machine/user, independent of mode.

    Acquired (outermost) by the unified single long-run entry and by every
    A/B Paper runner entry; the existing mode-specific locks (Phase-D
    account lock, A/B campaign/experiment locks) stay innermost. The lock
    file is never deleted; the flock alone is the authority.
    """
    path = global_runner_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise GlobalRunnerLockBusy(
                "another long-run or A/B Paper runner is already active "
                f"on this machine ({path})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "at": utc_now_iso()}))
        handle.flush()
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()


def runner_lock(*, lock_path: Callable[[], Path], utc_now_iso: Callable[[], str], RunnerLockBusy: type[RuntimeError]):
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


def new_observation_state(long_cfg: Dict[str, Any], *, expected_sessions: List[str], now: Optional[datetime]=None, git_baseline_commit: Callable[[], str], sanitize_for_log: Callable[[Any], Any], LONG_RUN_SCHEMA_VERSION: int, datetime: Any) -> Dict[str, Any]:
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
        # Continuous mode: the observation never auto-finalizes; the window
        # is extended chunk-by-chunk under the same run_id.
        "continuous": bool(long_cfg.get("continuous", False)),
        "baseline_commit": git_baseline_commit(),
        "config": sanitize_for_log(long_cfg),
        "expected_sessions": list(expected_sessions or []),
        "restart_count": 0,
        "stop": None,
    }


def load_active_state(*, active_path: Callable[[], Path], LongRunStop: type[RuntimeError]) -> Optional[Dict[str, Any]]:
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


def save_active_state(state: Dict[str, Any], *, active_path: Callable[[], Path], atomic_write_json: Callable[[Path, Any], None]) -> None:
    atomic_write_json(active_path(), state)


def clear_active_state(*, active_path: Callable[[], Path]) -> None:
    path = Path(active_path())
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def log_event(run_id: str, event_type: str, detail: Any=None, *, append_jsonl: Callable[[Path, Dict[str, Any]], None], run_dir: Callable[[str], Path], utc_now_iso: Callable[[], str]) -> None:
    append_jsonl(
        run_dir(run_id) / "events.jsonl",
        {"at": utc_now_iso(), "type": event_type, "detail": detail},
    )


def completed_sessions(run_id: str, *, run_dir: Callable[[str], Path], read_json: Callable[[Path], Optional[Any]]) -> List[str]:
    rounds_dir = run_dir(run_id) / "rounds"
    done = []
    if not rounds_dir.is_dir():
        return done
    for path in sorted(rounds_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("status") == "COMPLETED":
            done.append(path.stem)
    return done


def settled_sessions(run_id: str, *, run_dir: Callable[[str], Path], read_json: Callable[[Path], Optional[Any]], TERMINAL_ROUND_STATUSES: Tuple[str, ...]) -> List[str]:
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


def _empty_maintenance_summary() -> Dict[str, Any]:
    """N15: the zero-mutation maintenance summary shape (recovery/deadline)."""
    return {
        "broker_calls": 0, "submit_calls": 0, "cancel_calls": 0,
        "submitted_symbols": [], "has_unknown": False, "paused": False,
        "error": "",
    }


def new_round_journal(session_date: str, symbols: List[str], *, scheduled_at: str='', schedule_adjustment: str='NONE', LONG_RUN_SCHEMA_VERSION: int, SYMBOL_PENDING: str) -> Dict[str, Any]:
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


def save_round_journal(run_id: str, journal: Dict[str, Any], *, atomic_write_json: Callable[[Path, Any], None], round_path: Callable[[str, str], Path]) -> None:
    atomic_write_json(round_path(run_id, journal["session_date"]), journal)


def load_round_journal(run_id: str, session_date: str, *, read_json: Callable[[Path], Optional[Any]], round_path: Callable[[str, str], Path]) -> Optional[Dict[str, Any]]:
    data = read_json(round_path(run_id, session_date))
    return data if isinstance(data, dict) else None


def validate_resumable_symbols(journal: Dict[str, Any], *, LongRunStop) -> None:
    """Reject corrupt resume entries before recovery or journal mutation.

    Empty symbol maps are valid: a fresh journal is saved before screening.
    Terminal journals are handled by the coordinator and are not resumed.
    """
    symbols = journal.get("symbols")
    if not isinstance(symbols, dict):
        raise LongRunStop("STATE_CORRUPT", "resumable round journal has no symbol map")
    valid = {"PENDING", "ANALYZING", "ANALYZED", "EXECUTING", "DONE", "FAILED"}
    for symbol, entry in symbols.items():
        if (not isinstance(symbol, str) or not symbol.strip()
                or not isinstance(entry, dict)
                or not isinstance(entry.get("status"), str)
                or entry["status"] not in valid):
            raise LongRunStop("STATE_CORRUPT", f"invalid resume state for symbol {symbol!r}")
