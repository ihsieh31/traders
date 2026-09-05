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

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

VALID_REASONS = ("split", "ticker_change", "delisting", "non_tradable")

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
        if initial_events:
            for event in initial_events:
                try:
                    self.quarantine(**event)
                except (TypeError, ValueError):
                    # Invalid configured events must not crash startup; the
                    # malformed entry is ignored and stays visible to the
                    # operator through the validation error at write time.
                    continue

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._records = {str(k): list(v) for k, v in raw.items()}
                return
        except (OSError, ValueError):
            pass
        self._records = {}

    def reload(self) -> None:
        """Re-read the persisted ledger before judging active state.

        Another process — or another store instance in this process — may
        have quarantined or released a symbol since this instance loaded;
        the file is the source of truth, so gate reads must not run on a
        stale in-memory copy.
        """
        with self._lock:
            self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._records, indent=2, sort_keys=True), encoding="utf-8"
        )
        import os

        os.replace(tmp, self.path)

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
        # Fold in concurrent writers before mutating so our save does not
        # clobber records another instance added after our last load.
        self.reload()
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
        with self._lock:
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
        self.reload()
        with self._lock:
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
