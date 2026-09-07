"""Store complete fixed-horizon hypothetical outcomes from eligible forward logs.

These are signal diagnostics, not realized portfolio P&L or causal lessons.
Incomplete horizons are skipped. Retrieval is opt-in and restricted to lessons
available before the analysis date. No causal LLM reflection is generated.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import pandas as pd

from .engine import normalize_price_frame
from .signals import load_recorded_signals, load_recorded_runs

_REPORT_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
)


def compute_decision_outcomes(
    prices: pd.DataFrame,
    signals: Dict[str, str],
    horizon_bars: int = 5,
    positions: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Measure a complete next-open, fixed-horizon hypothetical position.

    BUY/LONG are long, SHORT is short, SELL/NEUTRAL are flat. HOLD requires
    known starting exposure. Incomplete horizons are omitted. This excludes
    intervening orders, position sizes, stop execution and trading costs.
    """
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1.")

    frame = normalize_price_frame(prices)
    bar_dates = [ts.date().isoformat() for ts in frame.index]
    opens = frame["open"].tolist()

    outcomes: List[dict] = []
    for trade_date, action in sorted((signals or {}).items()):
        # First bar strictly after the decision date.
        entry_idx = next(
            (i for i, d in enumerate(bar_dates) if d > trade_date), None
        )
        if entry_idx is None:
            continue

        exit_idx = entry_idx + horizon_bars
        partial = exit_idx > len(frame) - 1
        if partial:
            continue  # Never teach a truncated horizon as a completed label.
        if exit_idx <= entry_idx:
            continue

        entry_price = float(opens[entry_idx])
        exit_price = float(opens[exit_idx])
        if entry_price <= 0:
            continue
        asset_return = exit_price / entry_price - 1.0

        position = (positions or {}).get(trade_date)
        if action in {"BUY", "LONG"}:
            direction = 1
        elif action == "SHORT":
            direction = -1
        elif action in {"SELL", "NEUTRAL"}:
            direction = 0
        elif action == "HOLD":
            direction = {"LONG": 1, "SHORT": -1, "NEUTRAL": 0, "FLAT": 0}.get(position)
        else:
            direction = None
        decision_return = asset_return * direction if direction is not None else None

        outcomes.append(
            {
                "trade_date": trade_date,
                "action": action,
                "entry_date": bar_dates[entry_idx],
                "entry_price": entry_price,
                "exit_date": bar_dates[exit_idx],
                "exit_price": exit_price,
                "horizon_used": exit_idx - entry_idx,
                "asset_return": asset_return,
                "decision_return": decision_return,
                "outcome_kind": "hypothetical_position_return",
                "current_position": position,
                "partial": partial,
            }
        )
    return outcomes


def _situation_from_state(state: dict) -> Optional[str]:
    parts = [str(state[k]) for k in _REPORT_KEYS if state.get(k)]
    return "\n\n".join(parts) if parts else None


def _deterministic_lesson(symbol: str, outcome: dict) -> str:
    action = outcome["action"]
    horizon = outcome["horizon_used"]
    asset_pct = f"{outcome['asset_return']:+.1%}"
    span = "" if not outcome["partial"] else " (horizon truncated by data end)"

    hypothetical = outcome["decision_return"]
    result = f"{hypothetical:+.1%}" if hypothetical is not None else "unknown (position was not recorded)"
    return (
        f"[Diagnostic observation v2] {symbol} on {outcome['trade_date']}: {action}. "
        f"Asset moved {asset_pct} over {horizon} bars; hypothetical post-decision position return: {result}. "
        f"Entry open {outcome['entry_date']}, exit open {outcome['exit_date']}. "
        "This is not broker realized P&L: fills, sizing, intervening decisions, stops and costs are not modeled. "
        "One outcome does not validate the reasoning or prove a tradable edge; HOLD may retain an existing position."
    )


def default_agent_memories(config: Optional[dict] = None) -> Dict[str, object]:
    """The five reflection memories, named exactly as TradingAgentsGraph names
    them so batch-taught lessons are what the live agents later retrieve.

    Imports are deferred: tradingagents.agents pulls dataflows at import
    time, and the backtest package must stay importable without it.
    """
    from tradingagents.agents.utils.memory import FinancialSituationMemory

    if config is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        config = DEFAULT_CONFIG
    return {
        name: FinancialSituationMemory(f"{name}_memory", config)
        for name in ("bull", "bear", "trader", "invest_judge", "risk_manager")
    }


def teach_memories_from_history(
    symbol: str,
    memories: Dict[str, object],
    price_loader: Optional[Callable[[str, str, Optional[str]], pd.DataFrame]] = None,
    reflector=None,
    horizon_bars: int = 5,
    eval_results_dir: str = "eval_results",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Store deterministic hypothetical outcomes with point-in-time metadata.

    The reflector parameter remains for API compatibility and is not invoked.
    Raises ValueError if no eligible forward decisions are available.
    """
    signals = load_recorded_signals(symbol, eval_results_dir=eval_results_dir)
    if start_date:
        signals = {d: a for d, a in signals.items() if d >= start_date}
    if end_date:
        signals = {d: a for d, a in signals.items() if d <= end_date}
    if not signals:
        raise ValueError(
            f"No recorded completed runs with final signals found for {symbol} "
            f"under {eval_results_dir}/."
        )

    if price_loader is None:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        price_loader = AlpacaUtils.get_stock_data

    prices = price_loader(symbol, min(signals), end_date)
    runs = load_recorded_runs(symbol, eval_results_dir)
    positions = {day: ((run.get("snapshots") or {}).get("final_state") or {}).get("current_position")
                 for day, run in runs.items()}
    outcomes = compute_decision_outcomes(prices, signals, horizon_bars=horizon_bars, positions=positions)

    from tradingagents.run_logger import load_final_state_snapshot

    summary = {
        "symbol": symbol,
        "signals_found": len(signals),
        "outcomes_computed": len(outcomes),
        "decisions_taught": 0,
        "decisions_skipped_duplicate": 0,
        "decisions_skipped_no_state": 0,
        "lessons_written": 0,
    }

    for outcome in outcomes:
        trade_date = outcome["trade_date"]
        teach_key = f"v2|{symbol}|{trade_date}|{horizon_bars}"

        targets = {
            name: memory
            for name, memory in memories.items()
            if memory is not None
            and not memory.has_metadata_value("teach_key", teach_key)
        }
        if not targets:
            summary["decisions_skipped_duplicate"] += 1
            continue

        state = (runs[trade_date].get("snapshots") or {}).get("final_state")
        situation = _situation_from_state(state) if state else None
        if not situation:
            summary["decisions_skipped_no_state"] += 1
            continue

        # Deterministic observations only: do not let an LLM manufacture a causal
        # lesson from a counterfactual return. The reflector argument is retained
        # for API compatibility but no longer invoked here.
        lesson = _deterministic_lesson(symbol, outcome)

        metadata = {
            "source": "backtest_teach",
            "teach_key": teach_key,
            "symbol": symbol,
            "trade_date": trade_date,
            "action": outcome["action"],
            "decision_return": float(outcome["decision_return"]) if outcome["decision_return"] is not None else "unknown",
            "outcome_kind": "hypothetical_position_return",
            "outcome_end": outcome["exit_date"],
            "horizon_bars": int(horizon_bars),
        }
        written = 0
        for memory in targets.values():
            before = memory.situation_collection.count()
            memory.add_situations([(situation, lesson)], extra_metadata=metadata)
            written += memory.situation_collection.count() - before

        # written == 0 means embeddings were unavailable and nothing stored;
        # such decisions are simply not counted as taught.
        if written:
            summary["decisions_taught"] += 1
            summary["lessons_written"] += written

    return summary
