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
from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    load_evidence_packet,
)


def _load_pairs(root: Path) -> list[dict[str, Any]]:
    summary_paths = sorted(root.glob("*/**/pair_summary.json"))
    state_paths = sorted(root.glob("*/**/pair_state.json"))
    pair_dirs = {path.parent for path in summary_paths} | {path.parent for path in state_paths}
    if not pair_dirs:
        return []
    manifest_path = root / "AB_CAMPAIGN.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("A/B summary requires a readable campaign manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise RuntimeError("A/B campaign manifest has an unsupported schema")
    fingerprint = manifest.get("config_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise RuntimeError("A/B campaign manifest fingerprint is invalid")

    summary_by_dir = {path.parent: path for path in summary_paths}
    state_by_dir = {path.parent: path for path in state_paths}
    pairs = []
    pair_ids: set[str] = set()
    for pair_dir in sorted(pair_dirs):
        summary_path = summary_by_dir.get(pair_dir)
        state_path = state_by_dir.get(pair_dir)
        state: dict[str, Any] | None = None
        if state_path is not None:
            try:
                loaded_state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"A/B pair state is unreadable: {state_path}") from exc
            if not isinstance(loaded_state, dict) or loaded_state.get("schema_version") != 2:
                raise RuntimeError(f"A/B pair state has an unsupported schema: {state_path}")
            state = loaded_state

        if summary_path is not None:
            path = summary_path
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"A/B pair summary is unreadable: {path}") from exc
            if not isinstance(payload, dict) or payload.get("schema_version") != 2:
                raise RuntimeError(f"A/B pair summary has an unsupported schema: {path}")
        elif state is not None:
            path = state_path
            arms = state.get("arms") or {}

            def arm_result(backend: str) -> dict[str, Any]:
                arm = arms.get(backend) if isinstance(arms, dict) else None
                result = dict(arm.get("result") or {}) if isinstance(arm, dict) else {}
                result.setdefault("status", (arm or {}).get("status", "not_run"))
                result.setdefault("analysis_backend", backend)
                return result

            traders = arm_result("traders")
            berkshire = arm_result("berkshire")
            both_completed = (
                state.get("status") == "COMPLETED"
                and traders.get("status") == "completed"
                and berkshire.get("status") == "completed"
            )
            payload = {
                "schema_version": 2,
                "pair_id": state.get("pair_id"),
                "campaign_fingerprint": state.get("campaign_fingerprint"),
                "symbol": state.get("symbol", ""),
                "trade_date": state.get("trade_date", ""),
                "evidence_sha256": state.get("evidence_sha256"),
                "shared": {
                    "evidence_packet_path": state.get("evidence_packet_path", ""),
                    "evidence_packet_sha256": state.get("evidence_sha256"),
                },
                "signal_agreement": (
                    bool(traders.get("signal"))
                    and bool(berkshire.get("signal"))
                    and str(traders.get("signal")).upper() == str(berkshire.get("signal")).upper()
                    if both_completed else None
                ),
                "traders": traders,
                "berkshire": berkshire,
            }
        else:  # pragma: no cover - pair_dirs is built from these maps
            continue
        pair_id = payload.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id or pair_id in pair_ids:
            raise RuntimeError(f"A/B pair summary has a missing/duplicate pair_id: {path}")
        pair_ids.add(pair_id)
        if payload.get("campaign_fingerprint") != fingerprint:
            raise RuntimeError(f"A/B pair summary fingerprint mismatch: {path}")
        for backend in ("traders", "berkshire"):
            result = payload.get(backend)
            if not isinstance(result, dict):
                raise RuntimeError(f"A/B pair observation is malformed for {backend}: {path}")
        shared = payload.get("shared")
        evidence_hash = payload.get("evidence_sha256")
        if not isinstance(shared, dict) or shared.get("evidence_packet_sha256") != evidence_hash:
            raise RuntimeError(f"A/B pair observation evidence metadata is invalid: {path}")
        evidence_path = shared.get("evidence_packet_path")
        if evidence_path:
            try:
                packet = load_evidence_packet(
                    evidence_path,
                    symbol=str(payload.get("symbol") or ""),
                    trade_date=str(payload.get("trade_date") or ""),
                    expected_sha256=evidence_hash,
                )
            except (EvidenceIntegrityError, OSError) as exc:
                if summary_path is not None or payload.get("status") == "COMPLETED":
                    raise RuntimeError(f"A/B pair evidence failed integrity validation: {path}") from exc
                payload["evidence_validation"] = "unknown"
            else:
                if (
                    packet.get("symbol") != payload.get("symbol")
                    or str(packet.get("trade_date")) != str(payload.get("trade_date"))
                ):
                    raise RuntimeError(f"A/B pair evidence identity mismatch: {path}")
        else:
            payload["evidence_validation"] = "unknown"
        payload["_path"] = str(path)
        payload["_state"] = state
        payload["_arm_states"] = {
            backend: ((state.get("arms", {}).get(backend) or {}).get("status") if state else None)
            for backend in ("traders", "berkshire")
        }
        payload["_attempt_histories"] = {
            backend: list(((state.get("arms", {}).get(backend) or {}).get("attempt_history") or []))
            if state else [payload.get(backend) or {}]
            for backend in ("traders", "berkshire")
        }
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
        "attempts": 0,
        "completed": 0,
        "valid_completed": 0,
        "failed": 0,
        "invalid": 0,
        "unfinished": 0,
        "unknown_telemetry": 0,
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
        "paper_executions": 0,
        "paper_execution_successes": 0,
        "broker_attempted": 0,
        "holds": 0,
        "deduped": 0,
        "orders": 0,
    }


def summarize(root: str | Path) -> dict[str, Any]:
    """Return a JSON-safe aggregate without interpreting heuristic scores."""

    pairs = _load_pairs(Path(root))
    by_profile = defaultdict(_empty_profile_stats)
    agreement = 0
    disagreement = 0
    for pair in pairs:
        if pair.get("signal_agreement") is True:
            agreement += 1
        elif pair.get("signal_agreement") is False:
            disagreement += 1
        for profile in ("traders", "berkshire"):
            result = pair.get(profile) or {}
            stats = by_profile[profile]
            stats["runs"] += 1
            arm_state = (pair.get("_arm_states") or {}).get(profile)
            status = result.get("status")
            fully_completed = status == "completed" and arm_state in {None, "completed"}
            if fully_completed:
                stats["completed"] += 1
                stats["valid_completed"] += int(result.get("decision_valid", True) is not False)
            else:
                stats["failed"] += 1
                stats["invalid"] += int(result.get("decision_valid") is False)
                stats["unfinished"] += int(
                    arm_state in {"pending", "in_progress", "analysis_completed", "failed_retryable"}
                    or status in {"pending", "in_progress", "failed_retryable"}
                )
            signal = str(result.get("signal") or "UNKNOWN").upper()
            stats["signals"][signal] += 1
            attempts = (pair.get("_attempt_histories") or {}).get(profile) or [result]
            stats["attempts"] += len(attempts)
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    stats["unknown_telemetry"] += 1
                    continue
                paper = attempt.get("paper_execution")
                if isinstance(paper, dict):
                    stats["paper_executions"] += 1
                    stats["paper_execution_successes"] += int(bool(paper.get("success")))
                    stats["broker_attempted"] += int(bool(paper.get("broker_attempted")))
                    stats["holds"] += int(bool(paper.get("hold")))
                    stats["deduped"] += int(bool(paper.get("deduped")))
                    stats["orders"] += len(paper.get("orders") or [])
                run_payload = _run_log_payload(attempt)
                if not run_payload and not attempt.get("run_log"):
                    stats["unknown_telemetry"] += 1
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
