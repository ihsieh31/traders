"""Persisted-evidence reporting for Phase-D observations.

The public long_run wrappers supply named collaborators at call time so
reporting remains independent of orchestration and its patchable boundaries.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tradingagents.app_identity import (
    DEFAULT_RESULTS_DIR,
    default_results_dir,
    validate_app_path,
)


def _load_snapshots(
    run_id: str, *, run_dir: Callable[[str], Path],
) -> List[Dict[str, Any]]:
    path = run_dir(run_id) / "account_snapshots.jsonl"
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def _load_rounds(
    run_id: str, *, run_dir: Callable[[str], Path],
    read_json: Callable[[Path], Optional[Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Readable round journals, plus session dates whose journal exists but
    could not be parsed (preserved as-is, never guessed through)."""
    rounds_dir = run_dir(run_id) / "rounds"
    rounds = []
    unreadable = []
    if not rounds_dir.is_dir():
        return rounds, unreadable
    for path in sorted(rounds_dir.glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict):
            rounds.append(data)
        else:
            unreadable.append(path.stem)
    return rounds, unreadable


def _sum_unrealized(positions: Any) -> Optional[float]:
    if not positions:
        return None
    total = 0.0
    seen = False
    for row in positions:
        value = (row or {}).get("unrealized_pl")
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def compute_drawdown(equities: List[float]) -> Dict[str, Any]:
    if not equities:
        return {"peak": None, "trough": None, "max_drawdown": None}
    peak = equities[0]
    max_dd = 0.0
    trough = equities[0]
    for value in equities:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (value - peak) / peak
            if drawdown < max_dd:
                max_dd = drawdown
                trough = value
    return {"peak": peak, "trough": trough, "max_drawdown": max_dd}


def aggregate_llm_operations(run_id: str, runtime: Dict[str, Any]) -> Dict[str, Any]:
    """Read costs for exactly one observation, including shared A/B screening."""
    try:
        from tradingagents.llm_cost import aggregate_costs, scan_run_costs

        records = scan_run_costs(
            eval_results_dir=validate_app_path(
                runtime.get("results_dir") or default_results_dir(),
                field="results_dir",
            ),
            overrides=runtime.get("llm_pricing_per_million"),
            metadata_match={"long_run_observation_id": run_id},
        )
        totals = aggregate_costs(records)
        return {"available": True, "totals": totals.get("totals", {}),
                "per_day": totals.get("per_day", {}),
                "unpriced_tokens": totals.get("totals", {}).get("unpriced_tokens", 0)}
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def aggregate_final_report(
    state: Dict[str, Any],
    long_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
    *,
    _load_snapshots: Callable[[str], List[Dict[str, Any]]],
    _load_rounds: Callable[[str], Tuple[List[Dict[str, Any]], List[str]]],
    compute_drawdown: Callable[[List[float]], Dict[str, Any]],
    _sum_unrealized: Callable[[Any], Optional[float]],
    run_dir: Callable[[str], Path],
    SYMBOL_DONE: str,
    datetime: Any,
) -> Dict[str, Any]:
    """Deterministic Markdown+JSON inputs from persisted evidence only."""
    run_id = state["run_id"]
    snapshots = _load_snapshots(run_id)
    rounds, unreadable_journals = _load_rounds(run_id)
    expected = list(state.get("expected_sessions") or [])
    completed = [r["session_date"] for r in rounds if r.get("status") == "COMPLETED"]
    missed = [r["session_date"] for r in rounds if r.get("status") == "MISSED"]
    stopped = [r["session_date"] for r in rounds if r.get("status") == "STOPPED"]

    equities = [float(s.get("equity") or 0) for s in snapshots]
    start_equity = equities[0] if equities else None
    # F14: ending values come ONLY from a fresh phase="final" snapshot taken
    # at observation end. The last post_round snapshot can be days stale; a
    # missing/failed final capture is reported as unknown, never labeled
    # with the stale round value.
    final_snapshot = next(
        (s for s in reversed(snapshots) if s.get("phase") == "final"), None,
    )
    last_observed_equity = next(
        (float(s.get("equity")) for s in reversed(snapshots)
         if s.get("phase") == "post_round" and s.get("equity") is not None),
        None,
    )
    end_equity = final_snapshot.get("equity") if final_snapshot else None
    total_return = (
        (end_equity - start_equity) / start_equity
        if start_equity not in (None, 0) and end_equity is not None else None
    )
    drawdown = compute_drawdown(equities)
    starting_positions = next(
        (s.get("positions") for s in snapshots if s.get("phase") == "pre_round"), None,
    )
    ending_positions = final_snapshot.get("positions") if final_snapshot else None
    ending_snapshot_available = final_snapshot is not None

    # Decisions from round journals (authoritative for the observation) plus
    # the normal run logs for token/error detail.
    symbol_rows: List[Dict[str, Any]] = []
    signal_counts: Dict[str, int] = {}
    provider_failures = 0
    for journal in rounds:
        for symbol, entry in (journal.get("symbols") or {}).items():
            signal = entry.get("signal")
            if signal:
                signal_counts[str(signal)] = signal_counts.get(str(signal), 0) + 1
            summary = entry.get("execution_result_summary") or {}
            if summary.get("error", "").startswith("LLM ") or "provider" in summary.get("error", "").lower():
                provider_failures += 1
            symbol_rows.append({
                "session": journal.get("session_date"), "symbol": symbol,
                "status": entry.get("status"), "signal": signal,
                "broker_calls": (summary or {}).get("broker_calls", 0),
                "error": (summary or {}).get("error", ""),
            })
    symbol_rows.sort(key=lambda r: (r["session"] or "", r["symbol"] or ""))

    # Screening: fresh vs cached scans, daily Top20, turnover.
    fresh_scans = sum(1 for r in rounds
                      if r.get("status") == "COMPLETED" and not (r.get("screening") or {}).get("cached"))
    cache_reuses = sum(1 for r in rounds
                       if r.get("status") == "COMPLETED" and (r.get("screening") or {}).get("cached"))
    top20_by_day = {r["session_date"]: [e.get("symbol") for e in (r.get("screening") or {}).get("top20", [])]
                    for r in rounds if r.get("status") == "COMPLETED"}
    turnover: List[Dict[str, Any]] = []
    previous: set = set()
    for day in sorted(top20_by_day):
        current = set(top20_by_day[day])
        if previous:
            turnover.append({"session": day,
                             "entered": sorted(current - previous),
                             "exited": sorted(previous - current)})
        previous = current

    # Broker execution tallies from journals (execution.db stays authoritative).
    # N15: round maintenance (recovery/deadline) mutations are part of the
    # same tallies — broker_calls sums per-symbol execution AND maintenance;
    # submitted_symbols counts distinct symbols with actual submit events,
    # never ledger-row counts (a bracket POST may create parent + 2 child rows).
    exec_tally = {"submitted_symbols": 0, "broker_calls": 0, "holds": 0,
                  "safety_blocks": 0, "entry_gate_blocks": 0,
                  "quarantined": 0, "unknown": 0, "deduped": 0,
                  "maintenance_broker_calls": 0}
    submitted_symbol_names: set = set()
    round_durations: List[Dict[str, Any]] = []
    for journal in rounds:
        maintenance = journal.get("maintenance_execution") or {}
        for section in ("recovery", "deadline"):
            summary = maintenance.get(section) or {}
            maintenance_calls = int(summary.get("broker_calls") or 0)
            exec_tally["broker_calls"] += maintenance_calls
            exec_tally["maintenance_broker_calls"] += maintenance_calls
            if int(summary.get("submit_calls") or 0) > 0:
                for name in (summary.get("submitted_symbols") or []):
                    submitted_symbol_names.add(str(name).upper())
            if summary.get("has_unknown"):
                exec_tally["unknown"] += 1
        for symbol, entry in (journal.get("symbols") or {}).items():
            summary = entry.get("execution_result_summary") or {}
            if entry.get("status") != SYMBOL_DONE and not summary:
                continue
            if summary.get("no_trade"):
                exec_tally["holds"] += 1
                continue
            if summary.get("broker_calls"):
                submitted_symbol_names.add(symbol)
                exec_tally["broker_calls"] += int(summary["broker_calls"])
            elif (entry.get("signal") or "") == "HOLD":
                exec_tally["holds"] += 1
            if summary.get("safety_blocked"):
                exec_tally["safety_blocks"] += 1
            if summary.get("entry_gate_blocked"):
                exec_tally["entry_gate_blocks"] += 1
            if summary.get("quarantined"):
                exec_tally["quarantined"] += 1
            if summary.get("has_unknown"):
                exec_tally["unknown"] += 1
            if summary.get("deduped"):
                exec_tally["deduped"] += 1
        if journal.get("started_at") and journal.get("finished_at"):
            try:
                start = datetime.fromisoformat(journal["started_at"])
                end = datetime.fromisoformat(journal["finished_at"])
                round_durations.append({
                    "session": journal.get("session_date"),
                    "seconds": round((end - start).total_seconds(), 1),
                })
            except ValueError:
                pass
    exec_tally["submitted_symbols"] = len(submitted_symbol_names)

    # LLM operations from existing cost aggregation (best-effort, no estimates).
    # F13: scoped to THIS observation's exact run-log metadata so manual,
    # old, or concurrent-observation runs can never enter these totals.
    llm_ops = aggregate_llm_operations(run_id, runtime)

    # Execution DB tallies (best-effort reference; journals stay primary).
    # F13: same resolver as ExecutionService, so the report and execution
    # can never point at different files (env read at call time).
    exec_db: Dict[str, Any] = {"available": False}
    try:
        from tradingagents.execution import resolve_execution_db_path
        from tradingagents.execution.store import ACCOUNT_BINDING_DECISION_ID

        db_path = Path(resolve_execution_db_path(runtime.get("execution_db_path")))
        if not db_path.exists():
            exec_db = {"available": False, "missing": True}
            raise FileNotFoundError(f"execution DB missing: {db_path}")
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY rowid")]
            binding_row = conn.execute(
                "SELECT payload_json FROM execution_intents WHERE decision_id=?",
                (ACCOUNT_BINDING_DECISION_ID,),
            ).fetchone()
        expected_account = state.get("account_ref")
        binding = (json.loads(binding_row["payload_json"]).get("account_id")
                   if binding_row else None)
        if expected_account and (
            not binding or hashlib.sha256(binding.encode()).hexdigest()[:16] != expected_account
        ):
            raise ValueError("execution DB account binding does not match report arm")
        by_status: Dict[str, int] = {}
        for order in orders:
            status = str(order.get("status") or "UNKNOWN").upper()
            by_status[status] = by_status.get(status, 0) + 1
        exec_db = {"available": True, "orders_total": len(orders), "by_status": by_status}
    except Exception as exc:
        exec_db = {**exec_db, "available": False,
                   "error": f"{type(exc).__name__}: {exc}"[:200]}

    # Safety: final guard status (best-effort) + deterministic gate tallies.
    safety: Dict[str, Any] = {
        "kill_switch_active": None,
        "kill_switch_reason": "",
        "safety_gate_refusals": exec_tally["safety_blocks"],
        "entry_gate_refusals": exec_tally["entry_gate_blocks"],
        "quarantine_blocks": exec_tally["quarantined"],
    }
    try:
        from tradingagents.safety import get_safety_guard
        kill_path = runtime.get("safety_kill_switch_path")
        if kill_path:
            path = Path(kill_path)
            status = {"kill_switch_active": path.exists(),
                      "kill_switch_reason": path.read_text(encoding="utf-8").strip() if path.exists() else ""}
        else:
            status = get_safety_guard().status()
        if isinstance(status, dict):
            safety["kill_switch_active"] = bool(status.get("kill_switch_active"))
            safety["kill_switch_reason"] = str(
                status.get("kill_switch_reason") or "")[:200]
    except Exception:
        pass

    started_at = datetime.fromisoformat(state["started_at"])
    wall_seconds = 0.0
    try:
        wall_seconds = max(
            0.0, (datetime.now(timezone.utc) - started_at).total_seconds()
        )
    except Exception:
        pass

    return {
        "run_id": run_id,
        "baseline_commit": state.get("baseline_commit", "unknown"),
        "started_at": state.get("started_at"),
        "ends_at": state.get("ends_at"),
        "duration_calendar_days": long_cfg.get("duration_calendar_days"),
        "timezone": "US/Eastern",
        "final_status": state.get("status"),
        "stop": state.get("stop"),
        "coverage": {
            "expected_trading_sessions": len(expected),
            "completed_sessions": len(completed),
            "missed_sessions": len(missed),
            "stopped_sessions": len(stopped),
            "unreadable_journals": list(unreadable_journals),
            "completion_rate": (len(completed) / len(expected)) if expected else None,
            "restart_count": int(state.get("restart_count") or 0),
            "wall_clock_seconds": round(wall_seconds, 1),
        },
        "account": {
            "starting_equity": start_equity, "ending_equity": end_equity,
            "absolute_pl": (end_equity - start_equity)
            if start_equity is not None and end_equity is not None else None,
            "total_return": total_return,
            "return_kind": "unadjusted_account_equity_change",
            "return_limitations": [
                "Not adjusted for deposits or withdrawals",
                "May include positions that existed before the observation",
                "Not pure strategy attribution",
                "Not net profitability unless all research and trading costs are included",
            ],
            "peak_equity": drawdown["peak"], "trough_equity": drawdown["trough"],
            "max_drawdown": drawdown["max_drawdown"],
            "starting_cash": snapshots[0].get("cash") if snapshots else None,
            # F14: final cash comes from the phase="final" snapshot too; the
            # last observed round value is kept separately for diagnostics.
            "ending_cash": final_snapshot.get("cash") if final_snapshot else None,
            "last_observed_equity": last_observed_equity,
            "ending_snapshot_available": ending_snapshot_available,
            "starting_positions": starting_positions,
            "ending_positions": ending_positions,
            "ending_unrealized_pl": _sum_unrealized(ending_positions),
            "daily_equity_series": [
                {"at": s.get("at"), "phase": s.get("phase"),
                 "session": s.get("session"), "equity": s.get("equity"),
                 "cash": s.get("cash")}
                for s in snapshots
            ],
        },
        "decisions": {
            "symbol_analyses": len(symbol_rows),
            "signal_counts": signal_counts,
            "provider_failures": provider_failures,
            "per_symbol": symbol_rows,
        },
        "screening": {
            "fresh_scans": fresh_scans, "cache_reuses": cache_reuses,
            "top20_by_day": top20_by_day, "turnover": turnover,
            "stopped_sessions": stopped,
            "held_only_reviews": sorted(
                r["session_date"] for r in rounds
                if (r.get("screening") or {}).get("mode") == "held_review"
            ),
        },
        "safety": safety,
        "execution": exec_tally,
        "execution_db": exec_db,
        "llm_operations": llm_ops,
        "reliability": {
            "round_durations": round_durations,
            "missed_rounds": missed,
            "stopped_rounds": stopped,
            "schedule_adjustments": [
                {"session": r.get("session_date"),
                 "adjustment": r.get("schedule_adjustment", "NONE")}
                for r in rounds if r.get("schedule_adjustment", "NONE") != "NONE"
            ],
        },
        "evidence": {
            "manifest": str(run_dir(run_id) / "manifest.json"),
            "events": str(run_dir(run_id) / "events.jsonl"),
            "snapshots": str(run_dir(run_id) / "account_snapshots.jsonl"),
            "rounds": sorted(str(run_dir(run_id) / "rounds" / f"{r.get('session_date')}.json")
                             for r in rounds),
            "unreadable_round_journals": [
                str(run_dir(run_id) / "rounds" / f"{name}.json")
                for name in unreadable_journals],
            "daily_reports": sorted(str(run_dir(run_id) / "daily_reports" / f"{r.get('session_date')}.md")
                                    for r in rounds if r.get("status") == "COMPLETED"),
        },
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_usd(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def render_final_markdown(
    report: Dict[str, Any], *,
    _fmt_pct: Callable[[Any], str], _fmt_usd: Callable[[Any], str],
) -> str:
    """Human-readable 30-day observation report (deterministic)."""
    lines = [
        f"# Phase-D 30-Day Paper Observation — {report['run_id']}",
        "",
        f"- status: **{report['final_status']}**",
        f"- window: {report['started_at']} → {report['ends_at']} "
        f"({report['duration_calendar_days']} calendar days, US/Eastern)",
        f"- baseline commit: `{report['baseline_commit']}`",
    ]
    if report.get("stop"):
        lines.append(
            f"- stop: `{report['stop'].get('code')}` — "
            f"{report['stop'].get('detail', '')}"
        )
    cov = report["coverage"]
    rate = cov['completion_rate']
    lines += [
        "",
        "## Observation coverage",
        f"- expected US trading sessions: {cov['expected_trading_sessions']}",
        f"- completed: {cov['completed_sessions']}",
        f"- missed (process down): {cov['missed_sessions']}",
        f"- stopped: {cov['stopped_sessions']}",
        f"- completion: {_fmt_pct(rate)}",
    ]
    if cov.get("unreadable_journals"):
        lines.append(
            "- unreadable round journals (preserved as-is, operator review "
            f"required): {', '.join(cov['unreadable_journals'])}")
    lines += [
        f"- process restarts: {cov['restart_count']}",
        f"- wall-clock: {cov['wall_clock_seconds']:.0f}s",
        "",
        "## Portfolio / account result (broker snapshots)",
    ]
    acct = report["account"]
    lines += [
        f"- starting equity: {_fmt_usd(acct['starting_equity'])}",
        f"- ending equity: {_fmt_usd(acct['ending_equity'])}",
        f"- absolute P/L: {_fmt_usd(acct['absolute_pl'])}",
        f"- total return: {_fmt_pct(acct['total_return'])}",
    ]
    if not acct.get("ending_snapshot_available", False):
        lines.append(
            "- ending snapshot: unavailable (final broker snapshot failed; "
            f"last post-round equity {_fmt_usd(acct.get('last_observed_equity'))} "
            "is diagnostic only, not final)"
        )
    if acct.get("return_kind"):
        lines.append(f"- return kind: {acct['return_kind']}")
    for limitation in acct.get("return_limitations") or []:
        lines.append(f"- return limitation: {limitation}")
    lines += [
        f"- peak equity: {_fmt_usd(acct['peak_equity'])}",
        f"- trough equity: {_fmt_usd(acct['trough_equity'])}",
        f"- maximum drawdown: {_fmt_pct(acct['max_drawdown'])}",
        f"- starting cash: {_fmt_usd(acct['starting_cash'])}",
        f"- ending cash: {_fmt_usd(acct['ending_cash'])}",
        "",
        "Daily equity series (phase boundaries):",
        "| At | Phase | Session | Equity | Cash |",
        "|---|---|---|---:|---:|",
    ]
    for point in acct["daily_equity_series"]:
        lines.append(
            f"| {point.get('at', '')} | {point.get('phase', '')} "
            f"| {point.get('session', '')} | {_fmt_usd(point.get('equity'))} "
            f"| {_fmt_usd(point.get('cash'))} |"
        )
    lines += ["", "## Decision / analysis result", ""]
    dec = report["decisions"]
    lines += [
        f"- symbol analyses: {dec['symbol_analyses']}",
        f"- signals: {json.dumps(dec['signal_counts'], sort_keys=True)}",
        f"- provider failures: {dec['provider_failures']}",
        "",
        "| Session | Symbol | Status | Signal | Broker calls | Error |",
        "|---|---|---|---|---:|---|",
    ]
    for row in dec["per_symbol"]:
        lines.append(
            f"| {row['session']} | {row['symbol']} | {row['status']} "
            f"| {row['signal'] or '—'} | {row['broker_calls']} "
            f"| {(row['error'] or '')[:80]} |"
        )
    lines += ["", "## Screening result", ""]
    scr = report["screening"]
    lines += [
        f"- fresh full-market scans: {scr['fresh_scans']}",
        f"- same-day cache reuses: {scr['cache_reuses']}",
    ]
    for day in sorted(scr["top20_by_day"]):
        lines.append(f"- {day} Top20: {', '.join(scr['top20_by_day'][day]) or '—'}")
    for change in scr["turnover"]:
        lines.append(
            f"- {change['session']} turnover: +{', '.join(change['entered']) or '—'} "
            f"/ −{', '.join(change['exited']) or '—'}"
        )
    lines += ["", "## Broker execution result", ""]
    exe = report["execution"]
    lines += [
        f"- symbols with broker submissions: {exe['submitted_symbols']}",
        f"- broker POST calls: {exe['broker_calls']}",
        f"- round maintenance broker calls (recovery/deadline): "
        f"{exe.get('maintenance_broker_calls', 0)}",
        f"- HOLD / no-order outcomes: {exe['holds']}",
        f"- safety-gate refusals: {exe['safety_blocks']}",
        f"- entry-gate refusals: {exe['entry_gate_blocks']}",
        f"- quarantined: {exe['quarantined']}",
        f"- unknown/ambiguous: {exe['unknown']}",
        f"- idempotent dedupes: {exe['deduped']}",
    ]
    db = report["execution_db"]
    if db.get("available"):
        lines.append(
            f"- execution.db: {db['orders_total']} orders {db['by_status']}"
        )
    else:
        lines.append(f"- execution.db: unavailable ({db.get('error', '')})")
    lines += ["", "## Safety result", ""]
    saf = report.get("safety", {})
    lines += [
        f"- kill switch active: {saf.get('kill_switch_active')}",
        f"- kill switch reason: {saf.get('kill_switch_reason') or '—'}",
        f"- safety-gate refusals: {saf.get('safety_gate_refusals', 0)}",
        f"- entry-gate refusals: {saf.get('entry_gate_refusals', 0)}",
        f"- quarantine blocks: {saf.get('quarantine_blocks', 0)}",
    ]
    lines += ["", "## LLM operational result", ""]
    ops = report["llm_operations"]
    if ops.get("available"):
        totals = ops.get("totals", {})
        lines += [
            f"- runs: {totals.get('runs', 0)}",
            f"- total tokens: {totals.get('total_tokens', 0):,}",
            f"- estimated cost: ≈ ${totals.get('cost_usd', 0.0):,.2f}",
            f"- unpriced tokens: {totals.get('unpriced_tokens', 0):,}",
        ]
    else:
        lines.append(f"- token/cost data unavailable ({ops.get('error', '')})")
    lines += ["", "## Runtime reliability", ""]
    rel = report["reliability"]
    lines.append(f"- missed rounds: {rel['missed_rounds'] or 'none'}")
    lines.append(f"- stopped rounds: {rel['stopped_rounds'] or 'none'}")
    for adj in rel["schedule_adjustments"]:
        lines.append(f"- {adj['session']}: schedule_adjustment={adj['adjustment']}")
    for duration in rel["round_durations"]:
        lines.append(f"- {duration['session']}: round {duration['seconds']:.0f}s")
    lines += ["", "## Raw evidence index", ""]
    ev = report["evidence"]
    lines += [
        f"- manifest: `{ev['manifest']}`",
        f"- events: `{ev['events']}`",
        f"- snapshots: `{ev['snapshots']}`",
    ]
    for path in ev["rounds"]:
        lines.append(f"- round: `{path}`")
    for path in ev.get("unreadable_round_journals", []):
        lines.append(f"- round journal (unreadable, preserved as-is): `{path}`")
    for path in ev["daily_reports"]:
        lines.append(f"- daily report: `{path}`")
    lines += [
        "- execution DB: `eval_results/execution.db` (authoritative fills ledger)",
        "- run logs: `eval_results/<SYMBOL>/TradingAgentsStrategy_logs/runs/*.json`",
    ]
    lines += [
        "",
        "> Paper observation only. Decision-log returns are not broker-realized "
        "P/L. A fake-clock simulation never replaces the real 30-day run.",
        "",
    ]
    return "\n".join(lines)


def write_final_report(
    report: Dict[str, Any],
    *,
    run_dir: Callable[[str], Path],
    atomic_write_json: Callable[[Path, Any], None],
    sanitize_for_log: Callable[[Any], Any],
    render_final_markdown: Callable[[Dict[str, Any]], str],
) -> Tuple[str, str]:
    """Persist final_report.json + final_report.md; return their paths."""
    directory = run_dir(report["run_id"])
    json_path = directory / "final_report.json"
    md_path = directory / "final_report.md"
    atomic_write_json(json_path, sanitize_for_log(report))
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_final_markdown(report), encoding="utf-8")
    return str(md_path), str(json_path)



def send_long_run_alert(
    subject: str, body: str, runtime: Optional[Dict[str, Any]],
    deps: Optional[LongRunDeps] = None,
) -> None:
    """Existing alert channel, failure-isolated (never affects trading)."""
    try:
        if deps is not None and deps.alert_fn is not None:
            deps.alert_fn(subject, body, runtime)
            return
        from tradingagents.alerts import AlertConfig, send_alert

        send_alert(subject, body, config=AlertConfig.from_config(runtime or {}))
    except Exception:
        pass
