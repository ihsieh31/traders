"""Round configuration, intent recovery, screening audit, and execution helpers.

Orchestration and stop state stay in long_run. Explicit collaborators keep its
patchable boundaries authoritative without importing the orchestration module.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional






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
    symbol: str,
    session_date: str,
    *,
    observation_id: str,
    results_dir: str,
    _normalize_intent: Callable[[Any], Optional[Dict[str, Any]]],
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


def _screening_with_audit_scope(
    screening_fn: Callable[..., Any],
    runtime: Dict[str, Any],
    run_id: str,
    session_date: str,
    *,
    SCREENING_AUDIT_SYMBOL: str,
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


def _record_execution(
    journal,
    run_id,
    symbol,
    result,
    *,
    summarize_execution_result: Callable[[Dict[str, Any]], Dict[str, Any]],
    SYMBOL_EXECUTING: str,
    SYMBOL_DONE: str,
    save_round_journal: Callable[[str, Dict[str, Any]], None],
    log_event: Callable[..., None],
) -> None:
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


def _check_execution_hard_stop(
    symbol: str,
    result: Dict[str, Any],
    *,
    LongRunStop: type[RuntimeError],
) -> None:
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
