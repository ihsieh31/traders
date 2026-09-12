"""Deterministic production safety layer.

Every guard here is plain arithmetic over injected state — zero LLM calls,
zero imports from the agent stack — so it cannot be talked out of a halt by
a model and keeps working when providers misbehave. Three stages:

- pre-trade checks: per-order notional cap, per-symbol concentration cap
- circuit breakers: daily loss halt, drawdown-from-high-water-mark halt,
  consecutive-rejection halt (a data/connectivity glitch signal)
- kill switch: a flag file that stops all order flow no matter what the
  agents decide (ops can engage it by touching the file, no Python needed)

A daily LLM token budget rides along: run logs already count tokens, the
guard accumulates them per day and can refuse to start new analyses.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SAFETY_CONFIG: Dict[str, Any] = {
    "safety_enabled": True,
    # Pre-trade checks
    "max_trade_notional_usd": 25_000.0,  # 0 = uncapped
    "max_symbol_concentration_pct": 25.0,  # of account equity; 0 = uncapped
    # Circuit breakers
    "daily_loss_halt_pct": 10.0,  # halt when equity drops this % vs yesterday
    "max_drawdown_halt_pct": 15.0,  # halt when equity drops this % vs high-water mark
    "max_consecutive_rejections": 5,  # halt after this many broker rejections in a row
    # LLM cost cap
    "daily_llm_token_budget": 0,  # tokens/day across all runs; 0 = unlimited
}

_SAFETY_HOME = Path(os.path.expanduser("~")) / ".tradingagents" / "safety"


class SafetyStateError(RuntimeError):
    """The persisted safety state exists but cannot be trusted.

    A corrupt state file must stop startup instead of silently resetting
    the drawdown high-water mark, the rejection streak, or the token
    budget — resetting any of them is a fail-open, not a recovery.
    """


def _fresh_state() -> Dict[str, Any]:
    return {
        "high_water_mark": None,
        "consecutive_rejections": 0,
        "llm_tokens": {},
    }


def _validated_state(raw: Any, source: Path) -> Dict[str, Any]:
    """Minimal validation of a parsed state dict (missing keys default).

    Deliberate compatibility decision: a dict missing a known key is an
    older state file, not corruption — fill that key with its default. A
    key that is PRESENT but invalid is corruption and raises.
    """
    if not isinstance(raw, dict):
        raise SafetyStateError(
            f"safety state {source} is not a JSON object"
        )

    state = _fresh_state()
    state.update(raw)  # unknown keys are preserved, never rejected

    hwm = state.get("high_water_mark")
    if hwm is not None:
        if isinstance(hwm, bool) or not isinstance(hwm, (int, float)) \
                or not math.isfinite(float(hwm)) or float(hwm) <= 0:
            raise SafetyStateError(
                f"safety state {source} has invalid high_water_mark "
                f"(must be null or a positive finite number)"
            )

    rejects = state.get("consecutive_rejections")
    if isinstance(rejects, bool) or not isinstance(rejects, int) or rejects < 0:
        raise SafetyStateError(
            f"safety state {source} has invalid consecutive_rejections "
            f"(must be a non-negative integer)"
        )

    tokens = state.get("llm_tokens")
    if not isinstance(tokens, dict):
        raise SafetyStateError(
            f"safety state {source} has invalid llm_tokens (must be an object)"
        )
    for day, count in tokens.items():
        try:
            date.fromisoformat(str(day))
        except ValueError:
            raise SafetyStateError(
                f"safety state {source} has invalid llm_tokens key {day!r} "
                f"(must be a YYYY-MM-DD date string)"
            )
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SafetyStateError(
                f"safety state {source} has invalid llm_tokens count for "
                f"{day!r} (must be a non-negative integer)"
            )
    return state


@dataclass
class SafetyVerdict:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, dict] = field(default_factory=dict)
    # N08: stable machine-readable codes so callers (long-run hard stop)
    # can distinguish circuit breakers from single-order refusals without
    # parsing English reason text.
    reason_codes: List[str] = field(default_factory=list)


# N08: the fixed code vocabulary and which codes stop a whole unattended
# observation (vs. refusing only the current order).
KILL_SWITCH = "KILL_SWITCH"
MAX_TRADE_NOTIONAL = "MAX_TRADE_NOTIONAL"
MAX_SYMBOL_CONCENTRATION = "MAX_SYMBOL_CONCENTRATION"
DAILY_LOSS_HALT = "DAILY_LOSS_HALT"
MAX_DRAWDOWN_HALT = "MAX_DRAWDOWN_HALT"
CONSECUTIVE_REJECTIONS_HALT = "CONSECUTIVE_REJECTIONS_HALT"

OBSERVATION_HALT_CODES = frozenset({
    KILL_SWITCH,
    DAILY_LOSS_HALT,
    MAX_DRAWDOWN_HALT,
    CONSECUTIVE_REJECTIONS_HALT,
})


def _today(when: Optional[str] = None) -> str:
    return when or date.today().isoformat()


def _finite_float(value) -> Optional[float]:
    """Parse broker-supplied numbers defensively.

    Outage artifacts show up here as NaN/inf equity or literal HTML error
    pages in numeric fields. Anything non-finite or unparseable reads as
    'unavailable' (None): a NaN that reaches a comparison silently passes
    every breaker, and a NaN stored as the high-water mark disables the
    drawdown breaker permanently.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


class SafetyGuard:
    """Thread-safe guard with a small JSON state file.

    Persisted state: equity high-water mark, current rejection streak, and
    per-day LLM token counts. The kill switch is a separate flag file so a
    human (or cron job) can engage it with `touch`.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        state_path: Optional[Path] = None,
        kill_switch_path: Optional[Path] = None,
    ):
        merged = dict(DEFAULT_SAFETY_CONFIG)
        for key in merged:
            if config and config.get(key) is not None:
                merged[key] = config[key]
        self.config = merged
        self.state_path = Path(state_path or (_SAFETY_HOME / "state.json"))
        self.kill_switch_path = Path(
            kill_switch_path or (_SAFETY_HOME / "KILL_SWITCH")
        )
        self._lock = threading.RLock()
        self._state = self._load_state()

    # ----- cross-process critical section (R10) ------------------------------

    @contextmanager
    def _state_file_lock(self):
        """Serialize read-modify-write state cycles across processes (R10).

        A threading.RLock only protects one process; two processes can both
        read 0 and write back their own increment, losing the other's update
        (and a stale in-memory copy can hide another process's tokens or
        rejection streak entirely). The complete cycle is therefore:
        thread RLock -> flock LOCK_EX -> reload from disk -> mutate -> atomic
        save -> unlock. The OS releases the flock if a process crashes.
        """
        import contextlib

        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        with self._lock, contextlib.closing(handle):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def _reload_state_locked(self) -> Dict[str, Any]:
        """Refresh the in-memory state from disk while the flock is held.

        Must only be called inside ``_state_file_lock`` (or where the caller
        already holds the thread lock and needs the freshest disk copy).
        """
        self._state = self._load_state()
        return self._state

    # ----- state persistence -------------------------------------------------

    def _load_state(self) -> Dict[str, Any]:
        # Fail-closed: only a missing state file is a fresh install. A file
        # that exists but is unreadable/unparsable/invalid must raise —
        # silently starting from a zeroed state would reset the drawdown
        # high-water mark, the rejection streak, and the token budget.
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _fresh_state()
        except OSError as exc:
            raise SafetyStateError(
                f"safety state {self.state_path} exists but cannot be read: "
                f"{type(exc).__name__}"
            )
        except ValueError as exc:
            raise SafetyStateError(
                f"safety state {self.state_path} is not valid JSON: {exc}"
            )
        return _validated_state(raw, self.state_path)

    def _save_state(self) -> None:
        # Atomic durable write: temp file in the same directory, fsync, then
        # rename over the real state file so a crash can never truncate it.
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self.state_path.parent),
            prefix=self.state_path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ----- kill switch --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("safety_enabled", True))

    def kill_switch_active(self) -> bool:
        return self.kill_switch_path.exists()

    def kill_switch_reason(self) -> str:
        try:
            return self.kill_switch_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def engage_kill_switch(self, reason: str = "manual halt") -> None:
        self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        self.kill_switch_path.write_text(f"{reason} (engaged {stamp})", encoding="utf-8")
        # Ops alert; imported lazily and failure-isolated so the safety
        # layer keeps zero hard dependencies.
        try:
            from tradingagents.alerts import notify_kill_switch

            notify_kill_switch(reason)
        except Exception:
            pass

    def release_kill_switch(self) -> None:
        try:
            self.kill_switch_path.unlink()
        except FileNotFoundError:
            pass

    # ----- order/rejection tracking -------------------------------------------

    def record_order_result(self, success: bool) -> None:
        with self._state_file_lock():
            self._reload_state_locked()
            if success:
                self._state["consecutive_rejections"] = 0
            else:
                self._state["consecutive_rejections"] = (
                    int(self._state.get("consecutive_rejections", 0)) + 1
                )
            self._save_state()

    def consecutive_rejections(self) -> int:
        # R10: the streak may have been advanced by another process; read the
        # durable file, not this process's stale in-memory copy.
        with self._state_file_lock():
            self._reload_state_locked()
            return int(self._state.get("consecutive_rejections", 0))

    # ----- LLM token budget ----------------------------------------------------

    def record_llm_tokens(self, tokens: int, when: Optional[str] = None) -> None:
        if not tokens:
            return
        day = _today(when)
        with self._state_file_lock():
            self._reload_state_locked()
            counts = self._state.setdefault("llm_tokens", {})
            counts[day] = int(counts.get(day, 0)) + int(tokens)
            # Keep the map from growing unbounded.
            for old_day in sorted(counts)[:-31]:
                del counts[old_day]
            self._save_state()

    def llm_tokens_used(self, when: Optional[str] = None) -> int:
        day = _today(when)
        # R10: another process (long-run scheduler vs WebUI) may have spent
        # tokens since this instance loaded; the budget decision reads the
        # durable file under the same flock that guards the writes.
        with self._state_file_lock():
            self._reload_state_locked()
            return int(self._state.get("llm_tokens", {}).get(day, 0))

    def check_llm_budget(self, when: Optional[str] = None) -> SafetyVerdict:
        budget = float(self.config.get("daily_llm_token_budget", 0) or 0)
        # R10: read + decide under the state lock so a concurrent writer in
        # another process cannot slip a large spend between read and verdict.
        # The nested llm_tokens_used() is intentionally NOT called here — it
        # would flock a second time; read the refreshed state directly.
        with self._state_file_lock():
            self._reload_state_locked()
            day = _today(when)
            used = int(self._state.get("llm_tokens", {}).get(day, 0))
        if not self.enabled or budget <= 0 or used < budget:
            return SafetyVerdict(
                allowed=True,
                checks={"llm_budget": {"status": "pass", "used": used, "budget": budget}},
            )
        return SafetyVerdict(
            allowed=False,
            reasons=[
                f"Daily LLM token budget exhausted: {used:,.0f} of {budget:,.0f} tokens used."
            ],
            checks={"llm_budget": {"status": "fail", "used": used, "budget": budget}},
        )

    # ----- the main pre-order gate ----------------------------------------------

    def check_order(
        self,
        symbol: str,
        notional: float,
        account: Optional[Dict[str, float]] = None,
        position_value: Optional[float] = None,
        risk_reducing: bool = False,
    ) -> SafetyVerdict:
        """Deterministic gate every outbound order must pass.

        `account` carries {"equity", "last_equity"} when available. ``None``
        means unavailable and skips dependent checks; ``equity=0`` is a real
        value and fails closed for new exposure. ``last_equity=0`` cannot be
        a percentage-change baseline, so only that value is treated as unavailable.
        Risk-reducing exits bypass exposure and circuit-breaker checks so a
        loss halt cannot trap an existing position. The explicit kill switch
        still blocks all broker order flow.
        """
        if not self.enabled:
            return SafetyVerdict(
                allowed=True, checks={"safety": {"status": "skipped", "detail": "disabled"}}
            )

        reasons: List[str] = []
        codes: List[str] = []
        checks: Dict[str, dict] = {}
        equity = _finite_float(account.get("equity")) if account else None
        last_equity = _finite_float(account.get("last_equity")) if account else None
        if last_equity == 0.0:
            last_equity = None  # a 0 baseline can't produce a meaningful change %

        # Kill switch dominates everything.
        if self.kill_switch_active():
            reason = self.kill_switch_reason() or "engaged"
            reasons.append(f"Kill switch is engaged: {reason}")
            codes.append(KILL_SWITCH)
            checks["kill_switch"] = {"status": "fail", "detail": reason}
        else:
            checks["kill_switch"] = {"status": "pass"}

        if risk_reducing:
            for name in (
                "trade_notional",
                "concentration",
                "daily_loss",
                "drawdown",
                "rejection_streak",
            ):
                checks[name] = {
                    "status": "skipped",
                    "detail": "risk-reducing exit",
                }
            return SafetyVerdict(
                allowed=not reasons,
                reasons=reasons,
                checks=checks,
                reason_codes=codes,
            )

        # Pre-trade: per-order notional cap.
        notional_value = _finite_float(notional) or 0.0
        cap = float(self.config.get("max_trade_notional_usd", 0) or 0)
        if cap > 0 and notional_value > cap:
            reasons.append(
                f"Order notional ${notional_value:,.2f} exceeds max_trade_notional_usd ${cap:,.2f}."
            )
            codes.append(MAX_TRADE_NOTIONAL)
            checks["trade_notional"] = {"status": "fail", "notional": notional_value, "cap": cap}
        else:
            checks["trade_notional"] = {"status": "pass", "notional": notional_value, "cap": cap}

        # Pre-trade: per-symbol concentration cap.
        conc_pct = float(self.config.get("max_symbol_concentration_pct", 0) or 0)
        if conc_pct > 0:
            # M-04: equity==0.0 is a real finite value (fail-closed caps any
            # new exposure), not "data unavailable"; only None skips the check.
            if equity is not None:
                exposure = (_finite_float(position_value) or 0.0) + notional_value
                limit = equity * conc_pct / 100.0
                if exposure > limit:
                    reasons.append(
                        f"{symbol} exposure ${exposure:,.2f} would exceed "
                        f"{conc_pct:g}% of equity (${limit:,.2f})."
                    )
                    codes.append(MAX_SYMBOL_CONCENTRATION)
                    checks["concentration"] = {
                        "status": "fail",
                        "exposure": exposure,
                        "limit": limit,
                    }
                else:
                    checks["concentration"] = {
                        "status": "pass",
                        "exposure": exposure,
                        "limit": limit,
                    }
            else:
                checks["concentration"] = {
                    "status": "skipped",
                    "detail": "account equity unavailable",
                }
        else:
            checks["concentration"] = {"status": "pass", "detail": "uncapped"}

        # Circuit breaker: daily loss.
        halt_pct = float(self.config.get("daily_loss_halt_pct", 0) or 0)
        if halt_pct > 0 and equity is not None and last_equity is not None:
            change_pct = (equity - last_equity) / last_equity * 100.0
            if change_pct <= -halt_pct:
                reasons.append(
                    f"Daily loss circuit breaker: equity is {change_pct:+.2f}% vs "
                    f"yesterday (halt at -{halt_pct:g}%)."
                )
                codes.append(DAILY_LOSS_HALT)
                checks["daily_loss"] = {"status": "fail", "change_pct": change_pct}
            else:
                checks["daily_loss"] = {"status": "pass", "change_pct": change_pct}
        else:
            checks["daily_loss"] = {
                "status": "skipped" if halt_pct > 0 else "pass",
                "detail": "account equity unavailable" if halt_pct > 0 else "uncapped",
            }

        # Circuit breaker: drawdown from persisted high-water mark.
        dd_pct = float(self.config.get("max_drawdown_halt_pct", 0) or 0)
        if equity is not None:
            # R10: the high-water mark is read-modify-write state shared with
            # other processes; the whole cycle runs under the state flock.
            # Reload happens inside the lock, so this sees another process's
            # persisted mark instead of this process's stale copy.
            with self._state_file_lock():
                self._reload_state_locked()
                hwm = self._state.get("high_water_mark")
                # M-04: equity==0.0 is real data, but it is never a valid
                # high-water mark (state validation requires null or positive)
                # and cannot exceed an existing one — only a positive equity
                # may set or raise the persisted mark.
                if equity > 0 and (hwm is None or equity > float(hwm)):
                    self._state["high_water_mark"] = equity
                    self._save_state()
                    hwm = equity
            hwm = float(hwm) if hwm is not None else 0.0
            if dd_pct > 0 and hwm > 0:
                drawdown_pct = (hwm - equity) / hwm * 100.0
                if drawdown_pct >= dd_pct:
                    reasons.append(
                        f"Drawdown circuit breaker: equity is {drawdown_pct:.2f}% below the "
                        f"${hwm:,.2f} high-water mark (halt at {dd_pct:g}%)."
                    )
                    codes.append(MAX_DRAWDOWN_HALT)
                    checks["drawdown"] = {"status": "fail", "drawdown_pct": drawdown_pct}
                else:
                    checks["drawdown"] = {"status": "pass", "drawdown_pct": drawdown_pct}
            else:
                checks["drawdown"] = {"status": "pass", "detail": "uncapped"}
        else:
            checks["drawdown"] = {
                "status": "skipped" if dd_pct > 0 else "pass",
                "detail": "account equity unavailable" if dd_pct > 0 else "uncapped",
            }

        # Circuit breaker: consecutive broker rejections (data-glitch signal).
        max_rejects = int(self.config.get("max_consecutive_rejections", 0) or 0)
        # R10: read the streak under the state lock (not the nested helper,
        # which would flock again) from the freshest durable state.
        with self._state_file_lock():
            self._reload_state_locked()
            streak = int(self._state.get("consecutive_rejections", 0))
        if max_rejects > 0 and streak >= max_rejects:
            reasons.append(
                f"{streak} consecutive orders were rejected (halt at {max_rejects}); "
                "possible data or connectivity problem."
            )
            codes.append(CONSECUTIVE_REJECTIONS_HALT)
            checks["rejection_streak"] = {"status": "fail", "streak": streak}
        else:
            checks["rejection_streak"] = {"status": "pass", "streak": streak}

        return SafetyVerdict(allowed=not reasons, reasons=reasons, checks=checks,
                             reason_codes=codes)

    # ----- status for dashboards ---------------------------------------------

    def status(self, account: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Green/red snapshot of every guard for the WebUI panel."""
        order_view = self.check_order("_status_", 0.0, account=account)
        budget_view = self.check_llm_budget()
        guards: Dict[str, dict] = {}
        for name, check in {**order_view.checks, **budget_view.checks}.items():
            guards[name] = {
                "ok": check.get("status") != "fail",
                "status": check.get("status"),
                "detail": {k: v for k, v in check.items() if k != "status"},
            }
        return {
            "enabled": self.enabled,
            "kill_switch_active": self.kill_switch_active(),
            "kill_switch_reason": self.kill_switch_reason(),
            "reasons": list(order_view.reasons) + list(budget_view.reasons),
            "guards": guards,
            "config": dict(self.config),
        }


# ----- process-wide singleton ---------------------------------------------------

_GUARD: Optional[SafetyGuard] = None
_GUARD_LOCK = threading.Lock()


def get_safety_guard() -> SafetyGuard:
    """Process-wide guard configured from the runtime config (lazily)."""
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None:
            config: Dict[str, Any] = {}
            try:
                # Imported lazily: the safety layer must stay importable even
                # if the dataflows stack (and its dependencies) are not.
                from tradingagents.dataflows.config import get_config

                config = dict(get_config() or {})
            except Exception:
                config = {}
            _GUARD = SafetyGuard(config=config)
        return _GUARD


def reset_safety_guard() -> None:
    global _GUARD
    with _GUARD_LOCK:
        _GUARD = None
