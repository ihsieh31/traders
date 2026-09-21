#!/usr/bin/env python3
"""Aggregate analysis A/B pair summaries and run-log telemetry."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any, Iterable

# Make ``python scripts/summarize_analysis_ab.py`` work from a source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.app_identity import default_results_dir, validate_app_path


def _load_pairs(root: Path) -> list[dict[str, Any]]:
    pairs = []
    for path in sorted(root.glob("*/**/pair_summary.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_path"] = str(path)
            pairs.append(payload)
    return pairs


def _run_log_payload(result: dict[str, Any]) -> dict[str, Any]:
    path = result.get("run_log")
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_log_stats(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("summary") if isinstance(payload.get("summary"), dict) else {}


def _empty_profile_stats() -> dict[str, Any]:
    return {
        "runs": 0,
        "completed": 0,
        "failed": 0,
        "signals": Counter(),
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tool_calls": 0,
        "latency_seconds": 0.0,
        "error_events": 0,
        "report_context_runs": 0,
        "reports_with_content": 0,
        "contradictions": 0,
        "missing_evidence": 0,
    }


def summarize(root: str | Path) -> dict[str, Any]:
    """Return a JSON-safe aggregate without interpreting heuristic scores."""

    pairs = _load_pairs(Path(root))
    by_profile = defaultdict(_empty_profile_stats)
    agreement = 0
    disagreement = 0
    for pair in pairs:
        if pair.get("signal_agreement"):
            agreement += 1
        else:
            disagreement += 1
        for profile in ("traders", "berkshire"):
            result = pair.get(profile) or {}
            stats = by_profile[profile]
            stats["runs"] += 1
            status = result.get("status")
            if status == "completed":
                stats["completed"] += 1
            else:
                stats["failed"] += 1
            signal = str(result.get("signal") or "UNKNOWN").upper()
            stats["signals"][signal] += 1
            run_payload = _run_log_payload(result)
            run_stats = _run_log_stats(run_payload)
            stats["llm_calls"] += int(run_stats.get("llm_call_events", 0) or 0)
            stats["input_tokens"] += int(run_stats.get("total_llm_input_tokens", 0) or 0)
            stats["output_tokens"] += int(run_stats.get("total_llm_output_tokens", 0) or 0)
            stats["total_tokens"] += int(run_stats.get("total_llm_tokens", 0) or 0)
            stats["tool_calls"] += int(run_stats.get("tool_events", 0) or 0)
            stats["latency_seconds"] += float(run_stats.get("total_llm_time_seconds", 0.0) or 0.0)
            stats["error_events"] += int(run_stats.get("error_events", 0) or 0)
            if result.get("status") == "completed":
                stats["report_context_runs"] += int(
                    "report_context" in (result.get("final_state_keys") or [])
                )
            report_types = set()
            for event in run_payload.get("events", []):
                if not isinstance(event, dict) or event.get("type") != "agent_output":
                    continue
                payload = event.get("payload") or {}
                output_type = payload.get("output_type")
                content = payload.get("content")
                if output_type in {
                    "market_report",
                    "sentiment_report",
                    "news_report",
                    "fundamentals_report",
                    "macro_report",
                } and content:
                    report_types.add(output_type)
                    content_text = str(content).lower()
                    stats["contradictions"] += content_text.count("contradict")
                    stats["missing_evidence"] += content_text.count(
                        "missing or conflicting evidence"
                    ) + content_text.count("insufficient evidence")
            stats["reports_with_content"] += len(report_types)

    profile_json = {}
    for profile, stats in by_profile.items():
        profile_json[profile] = {
            **{key: value for key, value in stats.items() if key != "signals"},
            "signals": dict(stats["signals"]),
        }
        if stats["runs"]:
            profile_json[profile]["completed_rate"] = stats["completed"] / stats["runs"]
    return {
        "schema_version": 1,
        "root": str(Path(root)),
        "pair_count": len(pairs),
        "signal_agreement_pairs": agreement,
        "signal_disagreement_pairs": disagreement,
        # ``profiles`` remains a read-compatibility alias for old dashboards;
        # formal experiments are keyed by analysis backend.
        "backends": profile_json,
        "profiles": profile_json,
        "notes": [
            "Telemetry is read from RunAuditLogger JSON files.",
            "Evidence scoreboards and report-context heuristics are not treated as confidence or accuracy.",
            "Forward-return and backtest performance require a separate point-in-time evaluator.",
        ],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Analysis A/B Summary",
        "",
        f"- Pair count: {summary['pair_count']}",
        f"- Signal agreement: {summary['signal_agreement_pairs']}",
        f"- Signal disagreement: {summary['signal_disagreement_pairs']}",
        "",
        "## Profile telemetry",
        "",
        "| Profile | Completed | Failed | Reports | Total tokens | Tool calls | Error events | Signals |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for profile in ("traders", "berkshire"):
        stats = summary["profiles"].get(profile, {})
        signals = ", ".join(
            f"{key}={value}" for key, value in sorted((stats.get("signals") or {}).items())
        )
        lines.append(
            f"| {profile} | {stats.get('completed', 0)} | {stats.get('failed', 0)} | "
            f"{stats.get('reports_with_content', 0)} | {stats.get('total_tokens', 0)} | "
            f"{stats.get('tool_calls', 0)} | {stats.get('error_events', 0)} | {signals or 'none'} |"
        )
    lines.extend(
        [
            "",
            "## Analysis evidence telemetry",
            "",
            "| Profile | Report-context runs | Reports with content | Contradiction mentions | Missing-evidence mentions |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for profile in ("traders", "berkshire"):
        stats = summary["profiles"].get(profile, {})
        lines.append(
            f"| {profile} | {stats.get('report_context_runs', 0)} | "
            f"{stats.get('reports_with_content', 0)} | {stats.get('contradictions', 0)} | "
            f"{stats.get('missing_evidence', 0)} |"
        )
    lines.extend(["", "## Interpretation guardrails", ""])
    lines.extend(f"- {note}" for note in summary["notes"])
    lines.append("")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(default_results_dir() / "ab"))
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = validate_app_path(args.root, field="results_dir")
    summary = summarize(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "AB_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (root / "AB_SUMMARY.md").write_text(render_markdown(summary), encoding="utf-8")
    print(f"[AB] Summary written under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
