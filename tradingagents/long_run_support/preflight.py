"""Read-only preflight, post-authorization recovery, and account snapshots.

Public long_run wrappers supply named collaborators at call time, including
LongRunDeps and its default factories. Broker/LLM/calendar imports stay lazy
and probe secrets never cross the redacted boundary.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional


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
    """Default: the application ExecutionService (Phase A).

    The installed runtime config's execution_db_path is honored so an
    isolated analysis backend (Berkshire) never reads or writes the Traders
    execution DB. With no runtime override the app-level default/env
    resolution in resolve_execution_db_path() applies unchanged.
    """
    from tradingagents.dataflows.config import get_config
    from tradingagents.execution import ExecutionService

    config = get_config() or {}
    return ExecutionService(db_path=config.get("execution_db_path") or None)


def _default_broker_client() -> Any:
    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

    return get_alpaca_trading_client()


class SnapshotUnavailable(RuntimeError):
    """A required account/position fact is missing or non-finite (R16)."""


def _default_llm_probe(
    *,
    role: str,
    provider: str,
    model: str,
    backend_url: Optional[str],
    api_key: str,
    max_retries: int,
    REDACTED_API_KEY: str,
    LongRunStop: type[RuntimeError],
    sanitize_url: Callable[[Optional[str]], Optional[str]],
    time: Any,
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


def capture_account_snapshot(
    client: Any,
    *,
    utc_now_iso: Callable[[], str],
    hashlib: Any,
    SnapshotUnavailable: type[RuntimeError],
) -> Dict[str, Any]:
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


def run_preflight(
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    deps: Optional[LongRunDeps] = None,
    *,
    LongRunDeps: Any,
    LongRunStop: type[RuntimeError],
    _validate_unattended_safety: Callable[[Dict[str, Any]], None],
    validate_long_run_config: Callable[[Dict[str, Any], Optional[Dict[str, Any]]], List[str]],
    REDACTED_API_KEY: str,
    _default_llm_probe: Callable[..., Dict[str, Any]],
    _default_broker_client: Callable[[], Any],
    capture_account_snapshot: Callable[[Any], Dict[str, Any]],
    fetch_session_dates: Callable[..., List[date]],
    date: Any,
    timedelta: Any,
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
        capture_account_snapshot(client)
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


def run_post_authorization_recovery(
    deps: Optional['LongRunDeps'] = None,
    runtime: Optional[Dict[str, Any]] = None,
    *,
    can_submit: Optional[Callable[[], bool]] = None,
    LongRunDeps: Any,
    LongRunStop: type[RuntimeError],
    _apply_runtime_config: Callable[[Dict[str, Any]], None],
    _validate_long_run_execution_config: Callable[[Dict[str, Any]], None],
    _default_execution_service: Callable[[], Any],
    _default_broker_client: Callable[[], Any],
    capture_account_snapshot: Callable[[Any], Dict[str, Any]],
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
        recovery = (
            service.startup_recover(can_submit=can_submit)
            if can_submit is not None
            else service.startup_recover()
        )
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
