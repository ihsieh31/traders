"""Daily operations report assembled from data the system already persists.

One function call produces a Markdown (and simple HTML) digest of a
trading day: the decisions made (run logs), the safety layer's guard
status, estimated LLM cost for the day (model-attributed), and cumulative
realized performance per symbol from the decision log. Scheduling is left
to the operator (cron / Task Scheduler) — the report itself is pure local
file reading, no network, no LLM calls.
"""

from __future__ import annotations

import html as html_lib
import json
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple


def _day_runs(eval_results_dir: str, day: str) -> List[dict]:
    root = Path(eval_results_dir)
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.glob("*/TradingAgentsStrategy_logs/runs/*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        started_day = str(payload.get("started_at") or "")[:10]
        if started_day != day and str(payload.get("trade_date")) != day:
            continue
        summary = payload.get("summary") or {}
        runs.append(
            {
                "symbol": str(payload.get("symbol") or "?"),
                "status": str(payload.get("status") or "?"),
                "signal": str(summary.get("final_signal") or "—"),
                "tokens": int(summary.get("total_llm_tokens", 0) or 0),
                "errors": int(summary.get("error_events", 0) or 0),
            }
        )
    return runs


def _decisions_section(runs: List[dict]) -> List[str]:
    lines = ["## Decisions"]
    if not runs:
        lines.append("No completed analyses recorded for this day.")
        return lines
    lines.append("| Symbol | Signal | Status | LLM tokens | Errors |")
    lines.append("|---|---|---|---:|---:|")
    for run in runs:
        lines.append(
            f"| {run['symbol']} | {run['signal']} | {run['status']} "
            f"| {run['tokens']:,} | {run['errors']} |"
        )
    return lines


def _safety_section(guard) -> List[str]:
    lines = ["## Safety status"]
    if guard is None:
        lines.append("Safety guard unavailable.")
        return lines
    try:
        status = guard.status()
    except Exception as exc:
        lines.append(f"Safety status unavailable: {exc}")
        return lines
    if status.get("kill_switch_active"):
        lines.append(
            f"- 🔴 **KILL SWITCH ENGAGED**: {status.get('kill_switch_reason') or 'no reason recorded'}"
        )
    for name, view in (status.get("guards") or {}).items():
        icon = "✅" if view.get("ok") else "🛑"
        lines.append(f"- {icon} {name}: {view.get('status')}")
    for reason in status.get("reasons") or []:
        lines.append(f"- ⚠️ {reason}")
    return lines


def _cost_section(day: str, config: dict) -> List[str]:
    lines = ["## LLM cost (estimated)"]
    try:
        from tradingagents.llm_cost import aggregate_costs, scan_run_costs

        records = scan_run_costs(
            eval_results_dir=config.get("results_dir", "eval_results"),
            overrides=config.get("llm_pricing_per_million"),
        )
        day_bucket = aggregate_costs(records)["per_day"].get(day)
    except Exception as exc:
        lines.append(f"Cost data unavailable: {exc}")
        return lines
    if not day_bucket:
        lines.append("No token usage recorded for this day.")
        return lines
    lines.append(
        f"- {day_bucket['runs']} analysis run(s), {day_bucket['total_tokens']:,} tokens, "
        f"≈ ${day_bucket['cost_usd']:,.2f}"
    )
    if day_bucket.get("unpriced_tokens"):
        lines.append(
            f"- {day_bucket['unpriced_tokens']:,} tokens from unpriced models are "
            "excluded from the dollar figure."
        )
    lines.append(
        "- Estimates are an upper bound (cache discounts are not recorded)."
    )
    return lines


def _performance_section(config: dict) -> List[str]:
    lines = ["## Realized performance (cumulative)"]
    try:
        from tradingagents.llm_cost import realized_returns_by_symbol

        returns = realized_returns_by_symbol(config)
    except Exception as exc:
        lines.append(f"Decision-log data unavailable: {exc}")
        return lines
    if not returns:
        lines.append("No resolved decisions in the log yet.")
        return lines
    lines.append("| Symbol | Resolved decisions | Avg realized return |")
    lines.append("|---|---:|---:|")
    for symbol in sorted(returns):
        bucket = returns[symbol]
        lines.append(
            f"| {symbol} | {bucket['resolved']} | {bucket['avg_return']:+.2%} |"
        )
    return lines


def generate_daily_report(
    day: Optional[str] = None,
    config: Optional[dict] = None,
    guard=None,
    extra_header: Optional[List[str]] = None,
) -> str:
    """Markdown digest for one trading day, from persisted data only."""
    day = day or date.today().isoformat()
    config = dict(config or {})
    if guard is None:
        try:
            from tradingagents.safety import get_safety_guard

            guard = get_safety_guard()
        except Exception:
            guard = None

    runs = _day_runs(config.get("results_dir", "eval_results"), day)
    parts: List[str] = [f"# Daily Trading Report — {day}", ""]
    for line in extra_header or []:
        parts.append(f"> {line}")
    if extra_header:
        parts.append("")
    for section in (
        _decisions_section(runs),
        _safety_section(guard),
        _cost_section(day, config),
        _performance_section(config),
    ):
        parts.extend(section)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_daily_report(
    day: Optional[str] = None,
    output_dir: str = "reports",
    config: Optional[dict] = None,
    guard=None,
    extra_header: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Write the day's report as Markdown and a simple HTML page.

    Returns (markdown_path, html_path).
    """
    day = day or date.today().isoformat()
    markdown = generate_daily_report(
        day=day, config=config, guard=guard, extra_header=extra_header
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{day}.md"
    md_path.write_text(markdown, encoding="utf-8")

    html_body = html_lib.escape(markdown)
    html_path = out / f"{day}.html"
    html_path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Daily Trading Report — {day}</title></head>"
        "<body style='background:#111;color:#eee;font-family:monospace'>"
        f"<pre style='white-space:pre-wrap;max-width:960px;margin:2rem auto'>{html_body}</pre>"
        "</body></html>",
        encoding="utf-8",
    )
    return str(md_path), str(html_path)
