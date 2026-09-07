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

ACTION_ALIASES = {action: action for action in ("BUY", "SELL", "HOLD", "LONG", "SHORT", "NEUTRAL")}


def normalize_action(raw) -> Optional[str]:
    return ACTION_ALIASES.get(str(raw).strip().upper()) if raw is not None else None


def _sanitize_symbol_for_path(symbol: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", symbol.strip()) or "unknown"


def load_recorded_runs(symbol: str, eval_results_dir: str = "eval_results") -> dict[str, dict]:
    runs_dir = Path(eval_results_dir) / _sanitize_symbol_for_path(symbol) / "TradingAgentsStrategy_logs" / "runs"
    chosen = {}
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") != "completed" or payload.get("symbol") != symbol:
                continue
            summary = payload.get("summary") or {}
            if not normalize_action(summary.get("final_signal")):
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


def load_recorded_signals(symbol: str, eval_results_dir: str = "eval_results") -> Dict[str, str]:
    return {day: normalize_action(run["summary"]["final_signal"])
            for day, run in load_recorded_runs(symbol, eval_results_dir).items()}
