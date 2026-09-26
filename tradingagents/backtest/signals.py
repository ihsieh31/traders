"""Recorded forward decisions for signal diagnostics, never proof of portfolio P&L.

Preserve all six action meanings. Historical reruns and fixture logs are not
forward observations. For daily diagnostics choose the first eligible run,
not a later configuration that happened to look better in hindsight.
"""
from __future__ import annotations
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Optional

from tradingagents.app_identity import default_results_dir, validate_app_path

ACTION_ALIASES = {action: action for action in ("BUY", "SELL", "HOLD", "LONG", "SHORT", "NEUTRAL")}


def normalize_action(raw) -> Optional[str]:
    return ACTION_ALIASES.get(str(raw).strip().upper()) if raw is not None else None


def recorded_action(run: dict) -> Optional[str]:
    """The replay signal for one recorded run: the typed intent's action.

    ``summary.final_signal`` is a parsed LABEL — for a trading-mode decision
    whose intent never materialized (or an investment-mode SHORT proposal
    the canonical plan reduced to a hold) it can disagree with the
    executable intent. Replaying the label would trade orders the system
    never placed, so the intent action is authoritative and the parsed
    label is only a fallback for runs without a final intent.
    """
    summary = run.get("summary") or {}
    state = (run.get("snapshots") or {}).get("final_state") or {}
    intent = state.get("final_trade_intent")
    if isinstance(intent, dict):
        action = normalize_action(intent.get("action"))
        if action:
            return action
    return normalize_action(summary.get("final_signal"))


def _sanitize_symbol_for_path(symbol: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", symbol.strip()) or "unknown"


def load_recorded_runs(
    symbol: str, eval_results_dir: str | Path | None = None
) -> dict[str, dict]:
    root = validate_app_path(
        eval_results_dir or default_results_dir(), field="results_dir"
    )
    runs_dir = root / _sanitize_symbol_for_path(symbol) / "TradingAgentsStrategy_logs" / "runs"
    chosen = {}
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") != "completed" or payload.get("symbol") != symbol:
                continue
            summary = payload.get("summary") or {}
            if not recorded_action(payload):
                continue
            if summary.get("llm_call_events", 0) < 1 or summary.get("tool_events", 0) < 1:
                continue
            if (payload.get("metadata") or {}).get("evaluation_mode") in {"test", "historical", "fixture"}:
                continue
            trade_date = date.fromisoformat(payload["trade_date"])
            start = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(payload["ended_at"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                continue
            zone = ZoneInfo("UTC") if "/" in symbol else ZoneInfo("America/New_York")
            if start.astimezone(zone).date() != trade_date or end.astimezone(zone).date() != trade_date:
                continue  # retrospective rerun or late decision: not a t-close signal
            key = trade_date.isoformat()
            if key not in chosen or start < chosen[key][0]:
                chosen[key] = (start, payload)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return {key: value[1] for key, value in sorted(chosen.items())}


def load_recorded_signals(
    symbol: str, eval_results_dir: str | Path | None = None
) -> Dict[str, str]:
    return {day: action
            for day, run in load_recorded_runs(symbol, eval_results_dir).items()
            if (action := recorded_action(run))}
