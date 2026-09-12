"""Phase B corporate-action quarantine: persisted, fail-closed, no TTL.

A quarantine record blocks NEW exposure on a symbol after a corporate
action (split, ticker change, delisting, non-tradable status) until an
operator explicitly releases it with fresh authoritative facts and a CLEAN
reconciliation. Verified risk-reducing exits keep their Phase A path.

Honest scope: Phase B has NO automatic event feed. Records come from
explicit operator configuration (``corporate_action_events``) or runtime
calls; coverage is limited to reported events and never claims market-wide
automatic detection. Price moves never infer a split, a new ticker, or any
broker mutation.

Persistence is a JSON file (the same file-per-concern pattern as the rest
of the build — no new database). Restart preserves records; nothing ages
out automatically. A future-dated event (known split effective next week)
activates automatically once its effective time arrives; an unknown or
unparseable effective time quarantines immediately.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

VALID_REASONS = ("split", "ticker_change", "delisting", "non_tradable")


class QuarantineStateError(RuntimeError):
    """The persisted quarantine ledger exists but cannot be trusted.

    Fail-closed contract: only a MISSING file means an empty ledger. An
    existing-but-unreadable file must block new exposure until an operator
    resolves it — silently replacing corrupt durable state with an empty
    ledger would release a known quarantine exactly when its records are
    needed most.
    """

REASON_LABELS = {
    "split": "share split / reverse split (adjusted and unadjusted price data must not be mixed)",
    "ticker_change": "ticker symbol change (order routing under the old symbol must stop)",
    "delisting": "delisting (the asset may stop trading at any time)",
    "non_tradable": "asset marked non-tradable by the broker",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


class QuarantineStore:
    """Durable JSON quarantine ledger keyed by normalized symbol."""

    def __init__(self, path: str | Path, *, initial_events: Optional[list] = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records: dict[str, list[dict]] = {}
        self._load()
        # R08: a malformed configured event is operator input we cannot
        # interpret. Swallowing it would trade as if the quarantine never
        # existed — the exact moment its records are needed most — so the
        # store fails closed instead of guessing the operator's intent.
        if initial_events:
            for index, event in enumerate(initial_events):
                try:
                    self.quarantine(**event)
                except (TypeError, ValueError) as exc:
                    symbol = ""
                    if isinstance(event, dict):
                        symbol = str(event.get("symbol") or "")
                    raise QuarantineStateError(
                        f"configured corporate_action_events[{index}] is malformed"
                        f"{' for symbol ' + symbol if symbol else ''}: {exc}"
                    ) from exc

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        """Load the ledger; fail closed on any existing-but-untrusted state.

        FileNotFoundError is the ONLY path that yields an empty ledger.
        Permission/read errors, invalid JSON, a non-dict top level, or a
        malformed per-symbol shape raise QuarantineStateError so callers
        refuse new exposure instead of trading on missing quarantines.
        """
        if not self.path.exists():
            self._records = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise QuarantineStateError(
                f"quarantine state at {self.path} is unreadable: {exc}"
            ) from exc
        except ValueError as exc:
            raise QuarantineStateError(
                f"quarantine state at {self.path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise QuarantineStateError(
                f"quarantine state at {self.path} must be a dict of symbol -> "
                f"record list, got {type(raw).__name__}"
            )
        records: dict[str, list[dict]] = {}
        for key, value in raw.items():
            if not isinstance(value, list) or not all(
                isinstance(record, dict) for record in value
            ):
                raise QuarantineStateError(
                    f"quarantine state for {key!r} in {self.path} is malformed: "
                    "expected a list of record dicts"
                )
            records[str(key)] = list(value)
        self._records = records

    def reload(self) -> None:
        """Re-read the persisted ledger before judging active state.

        Another process — or another store instance in this process — may
        have quarantined or released a symbol since this instance loaded;
        the file is the source of truth, so gate reads must not run on a
        stale in-memory copy.
        """
        with self._lock, self._file_lock():
            self._load()

    @contextmanager
    def _file_lock(self):
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._records, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- operations --------------------------------------------------------

    def quarantine(
        self,
        *,
        symbol: str,
        reason: str,
        source: str = "operator",
        effective_at: Any = None,
        observed_at: Any = None,
        details: Optional[dict] = None,
    ) -> dict:
        """Add a quarantine record. Unknown/unparseable effective time means
        the record is quarantined immediately (never optimistically active-later)."""
        normalized = (symbol or "").upper().replace("/", "")
        if not normalized:
            raise ValueError("quarantine requires a symbol")
        reason_key = str(reason or "").strip().lower()
        if reason_key not in VALID_REASONS:
            raise ValueError(
                f"quarantine reason must be one of {VALID_REASONS}, got {reason!r}"
            )
        effective = _parse_timestamp(effective_at)
        observed = _parse_timestamp(observed_at) or utc_now()
        record = {
            "symbol": normalized,
            "reason": reason_key,
            "source": str(source or "operator"),
            # None effective_at quarantines immediately (uncertain time).
            "effective_at": effective.isoformat() if effective else None,
            "observed_at": observed.isoformat(),
            "status": "active",
            "details": dict(details or {}),
        }
        # Hold one cross-process lock across reload + mutation + replace.
        with self._lock, self._file_lock():
            self._load()
            if not any(
                r["reason"] == reason_key and r["status"] == "active"
                for r in self._records.get(normalized, [])
            ):
                self._records.setdefault(normalized, []).append(record)
                self._save()
        return record

    def active_for(self, symbol: str, *, now: Optional[datetime] = None) -> list[dict]:
        """Active quarantine records for a symbol (effective time applied)."""
        normalized = (symbol or "").upper().replace("/", "")
        current = (now or utc_now())
        self.reload()
        with self._lock:
            active = []
            for record in self._records.get(normalized, []):
                if record.get("status") != "active":
                    continue
                effective = _parse_timestamp(record.get("effective_at"))
                if effective is None or effective <= current:
                    # Future events only activate at their effective time;
                    # unknown times are active immediately (fail closed).
                    active.append(record)
            return [dict(r) for r in active]

    def is_quarantined(self, symbol: str, *, now: Optional[datetime] = None) -> bool:
        return bool(self.active_for(symbol, now=now))

    def release(
        self,
        symbol: str,
        *,
        operator: str,
        now: Optional[datetime] = None,
        reconciliation_is_clean: bool = False,
    ) -> dict:
        """Operator-confirmed release. Requires an explicit operator name and
        a current CLEAN reconciliation; there is no TTL or automatic release."""
        normalized = (symbol or "").upper().replace("/", "")
        if not operator or not str(operator).strip():
            raise ValueError("release requires an explicit operator confirmation")
        if not reconciliation_is_clean:
            raise ValueError(
                "release requires a current CLEAN reconciliation; re-run "
                "reconciliation after the authoritative data is updated"
            )
        current = (now or utc_now())
        released = 0
        with self._lock, self._file_lock():
            self._load()
            for record in self._records.get(normalized, []):
                if record.get("status") == "active":
                    record["status"] = "released"
                    record["released_at"] = current.isoformat()
                    record["released_by"] = str(operator)
                    released += 1
            if released:
                self._save()
        return {"symbol": normalized, "released": released}

    def all_active(self, *, now: Optional[datetime] = None) -> list[dict]:
        symbols = sorted(self._records.keys())
        active: list[dict] = []
        for symbol in symbols:
            active.extend(self.active_for(symbol, now=now))
        return active


class QuarantineGate:
    """Execution-facing view of the quarantine ledger.

    Loads configured events once per process; ``check`` is what the
    execution entry calls before any exposure-adding submit.
    """

    def __init__(self, store: QuarantineStore, *, alert: Optional[Callable[[str], None]] = None):
        self.store = store
        self._alert = alert

    def check(self, symbol: str) -> Optional[str]:
        """Return a rejection reason when the symbol is quarantined, else None."""
        records = self.store.active_for(symbol)
        if not records:
            return None
        reasons = []
        for record in records:
            label = REASON_LABELS.get(record.get("reason", ""), record.get("reason"))
            reasons.append(f"{record.get('reason')} ({label}; source: {record.get('source')})")
        return (
            f"{symbol} is quarantined by a corporate action: {'; '.join(reasons)}. "
            "New exposure is refused; verified risk-reducing exits keep the "
            "Phase A path. Release requires updated authoritative data, a CLEAN "
            "reconciliation and an explicit operator confirmation."
        )


def build_quarantine_gate(config: Optional[dict]) -> Optional[QuarantineGate]:
    """Build the gate from runtime config; returns None when unavailable so
    callers decide their own fail-closed behavior (the execution entry
    treats a missing gate as a configuration failure for opening orders)."""
    if not config:
        return None
    try:
        base_dir = Path(config.get("results_dir") or "eval_results")
        store = QuarantineStore(
            base_dir / "quarantine.json",
            initial_events=list(config.get("corporate_action_events") or []),
        )
        return QuarantineGate(store)
    except Exception:
        return None
