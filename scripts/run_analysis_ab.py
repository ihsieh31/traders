#!/usr/bin/env python3
"""Run a controlled, decision-only Traders versus Berkshire analysis pair.

The two runs share one frozen EvidencePacket and the complete downstream graph.
Only ``analysis_backend`` and explicitly isolated learning/run destinations
differ. Runs are intentionally serial because the application keeps some
process-global configuration and audit context.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping, Sequence

# Make ``python scripts/run_analysis_ab.py`` work from a source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.analysis_backends import resolve_analysis_backend
from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    build_or_load_evidence_packet,
)
from tradingagents.app_identity import default_results_dir, validate_app_path
from tradingagents.default_config import DEFAULT_CONFIG


_SAFE_TICKER_RE = re.compile(r"^[A-Za-z0-9._\-/\^]+$")


def safe_ticker_component(value: str, *, max_len: int = 64) -> str:
    """Small dependency-free copy of the repository's path-safe ticker rule."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"ticker must be a non-empty trimmed string, got {value!r}")
    if len(value) > max_len or "\\" in value or "\x00" in value or any(ch.isspace() for ch in value):
        raise ValueError(f"ticker contains invalid path characters: {value!r}")
    if ".." in value or set(value) == {"."} or not _SAFE_TICKER_RE.fullmatch(value):
        raise ValueError(f"ticker contains invalid path characters: {value!r}")
    safe = value.replace("/", "_")
    if not safe or set(safe) <= {".", "_"}:
        raise ValueError(f"ticker cannot be used as a path component: {value!r}")
    return safe


BACKEND_ORDER = ("traders", "berkshire")
PROFILE_ORDER = BACKEND_ORDER  # Compatibility alias for summary consumers.
AB_ALLOWED_DIFFERENCES = frozenset(
    {
        "analysis_backend",
        "results_dir",
        "_analysis_source",
        "memory_log_path",
        "agent_memory_dir",
        "data_cache_dir",
        "screening_selection_cache_path",
        "execution_db_path",
        "execution_lock_dir",
        "long_run_dir",
    }
)

_ISOLATED_PATH_KEYS = (
    "results_dir",
    "memory_log_path",
    "agent_memory_dir",
    "data_cache_dir",
    "screening_selection_cache_path",
    "execution_db_path",
    "execution_lock_dir",
    "long_run_dir",
)

_REQUIRED_MEMORY_FLAGS = (
    "memory_retrieval_enabled",
    "reflection_on_outcome_enabled",
    "memory_maintenance_enabled",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def assert_ab_invariants(config_a: Mapping[str, Any], config_b: Mapping[str, Any]) -> None:
    """Fail closed if the pair differs outside the approved isolation keys."""

    differing = []
    for key in sorted(set(config_a) | set(config_b)):
        if key in AB_ALLOWED_DIFFERENCES:
            continue
        if _canonical(config_a.get(key)) != _canonical(config_b.get(key)):
            differing.append(key)
    if differing:
        raise RuntimeError(
            "A/B invariant violation: shared configuration differs for "
            + ", ".join(differing)
        )

    for key in _REQUIRED_MEMORY_FLAGS:
        if config_a.get(key) is not True or config_b.get(key) is not True:
            raise RuntimeError(f"A/B invariant violation: {key} must be enabled for both profiles")
    if config_a.get("auto_trade") is not False or config_b.get("auto_trade") is not False:
        raise RuntimeError("A/B invariant violation: auto_trade must be disabled")
    if config_a.get("analysis_profile") != config_b.get("analysis_profile"):
        raise RuntimeError("A/B invariant violation: analysis_profile must be identical")
    if config_a.get("analysis_input_mode") != "frozen_evidence" or config_b.get("analysis_input_mode") != "frozen_evidence":
        raise RuntimeError("A/B invariant violation: analysis_input_mode must be frozen_evidence")
    if not config_a.get("evidence_packet_path") or config_a.get("evidence_packet_path") != config_b.get("evidence_packet_path"):
        raise RuntimeError("A/B invariant violation: evidence_packet_path must be shared")
    if not config_a.get("evidence_packet_sha256") or config_a.get("evidence_packet_sha256") != config_b.get("evidence_packet_sha256"):
        raise RuntimeError("A/B invariant violation: evidence_packet_sha256 must be shared")
    if config_a.get("checkpoint_enabled") is not False or config_b.get("checkpoint_enabled") is not False:
        raise RuntimeError("A/B invariant violation: checkpoint_enabled must be false")

    for key in _ISOLATED_PATH_KEYS:
        left = Path(str(config_a.get(key, ""))).resolve(strict=False)
        right = Path(str(config_b.get(key, ""))).resolve(strict=False)
        if left == right:
            raise RuntimeError(f"A/B invariant violation: {key} is shared")


@contextmanager
def _experiment_lock(root: Path):
    """Serialize campaign runs so process-global config and state cannot overlap."""

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".ab.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    lock_api = None
    try:
        try:
            import fcntl as lock_api
        except ImportError as exc:  # pragma: no cover - production is Linux/macOS
            raise RuntimeError("A/B runner requires advisory file locking") from exc
        lock_api.flock(handle.fileno(), lock_api.LOCK_EX)
        yield
    finally:
        try:
            if lock_api is not None:
                lock_api.flock(handle.fileno(), lock_api.LOCK_UN)
        finally:
            handle.close()


def _execution_order(symbol: str, trade_date: str) -> list[str]:
    digest = hashlib.sha256(f"{symbol}|{trade_date}".encode("utf-8")).digest()
    if int.from_bytes(digest[:8], "big") % 2:
        return ["berkshire", "traders"]
    return ["traders", "berkshire"]


def build_ab_configs(
    base_config: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: str,
    pair_dir: str | Path,
    experiment_root: str | Path | None = None,
    evidence_packet_path: str | Path | None = None,
    evidence_packet_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build isolated profile configs while keeping all experiment inputs equal."""

    pair_path = validate_app_path(pair_dir, field="results_dir")
    campaign_path = validate_app_path(
        experiment_root or pair_path.parent.parent, field="results_dir"
    )
    shared = deepcopy(dict(base_config))
    # Each arm learns only from its own prior decisions.  Checkpoints stay off:
    # they are crash-resume state, not learning memory, and could silently
    # reuse a prior answer for the same symbol/date.
    shared.update(
        {
            "memory_retrieval_enabled": True,
            "reflection_on_outcome_enabled": True,
            "memory_maintenance_enabled": True,
            "checkpoint_enabled": False,
            "auto_trade": False,
        }
    )

    configs: dict[str, dict[str, Any]] = {}
    packet_path = Path(evidence_packet_path or (campaign_path / "evidence" / str(trade_date) / safe_ticker_component(symbol) / "evidence_packet.json"))
    for backend in BACKEND_ORDER:
        profile_dir = campaign_path / "_profiles" / backend
        config = deepcopy(shared)
        config.update(
            {
                # Compatibility profile is deliberately identical.  The
                # formal experiment variable is analysis_backend.
                "analysis_profile": "traders",
                "analysis_backend": backend,
                "analysis_input_mode": "frozen_evidence",
                "evidence_packet_path": str(packet_path),
                "evidence_packet_sha256": evidence_packet_sha256 or "pending",
                "results_dir": str(profile_dir / "results"),
                "_analysis_source": f"ab_{backend}",
                "memory_log_path": str(profile_dir / "memory.md"),
                "agent_memory_dir": str(profile_dir / "agent_memory"),
                "data_cache_dir": str(profile_dir / "data_cache"),
                "screening_selection_cache_path": str(
                    profile_dir / "screening_selection.json"
                ),
                "execution_db_path": str(profile_dir / "execution.sqlite3"),
                "execution_lock_dir": str(profile_dir / "execution-locks"),
                "long_run_dir": str(profile_dir / "long_run"),
            }
        )
        resolve_analysis_backend(config)
        configs[backend] = config

    assert_ab_invariants(configs["traders"], configs["berkshire"])
    return configs


def _run_logs(config: Mapping[str, Any], symbol: str) -> set[Path]:
    runs_dir = (
        validate_app_path(config["results_dir"], field="results_dir")
        / safe_ticker_component(symbol)
        / "TradingAgentsStrategy_logs"
        / "runs"
    )
    return set(runs_dir.glob("*.json"))


def _latest_run_log(
    config: Mapping[str, Any], symbol: str, *, exclude: set[Path] | None = None
) -> str:
    logs = sorted(
        _run_logs(config, symbol) - (exclude or set()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return str(logs[-1]) if logs else ""


def _campaign_fingerprint(
    config: Mapping[str, Any], selected_analysts: Sequence[str]
) -> str:
    shared = {
        key: value
        for key, value in config.items()
        if key not in AB_ALLOWED_DIFFERENCES
    }
    payload = {"shared_config": shared, "selected_analysts": list(selected_analysts)}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _ensure_campaign_manifest(
    root: Path,
    config: Mapping[str, Any],
    selected_analysts: Sequence[str],
) -> str:
    """Pin experimental conditions across the full multi-day campaign."""

    fingerprint = _campaign_fingerprint(config, selected_analysts)
    manifest_path = root / "AB_CAMPAIGN.json"
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("A/B campaign manifest is unreadable") from exc
        if (
            existing.get("schema_version") != 2
            or existing.get("experiment") != "traders-vs-ai-berkshire-analysis-backend"
            or existing.get("backends") != list(BACKEND_ORDER)
            or existing.get("analysis_input_mode") != "frozen_evidence"
            or existing.get("config_fingerprint") != fingerprint
        ):
            raise RuntimeError(
                "A/B campaign invariant violation: configuration or analyst set changed"
            )
        return fingerprint

    payload = {
        "schema_version": 2,
        "experiment": "traders-vs-ai-berkshire-analysis-backend",
        "backends": list(BACKEND_ORDER),
        "analysis_input_mode": "frozen_evidence",
        "downstream_shared": True,
        "auto_trade": False,
        "max_arm_attempts": 3,
        "selected_analysts": list(selected_analysts),
        "config_fingerprint": fingerprint,
        "memory_policy": "enabled and isolated per profile",
        "execution_policy": "serial, order-counterbalanced, shadow decisions only",
        "berkshire_reference": {
            "repo": "xbtlin/ai-berkshire",
            "commit": "1cc1e362378cd3fea99a4f4c3b50676bce9aa4c6",
        },
        "traders_baseline": {
            "commit": "81123b6e7abe6526147bc79b469f68ee8a9c5e64",
        },
    }
    temp_path = manifest_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temp_path, manifest_path)
    return fingerprint


def _signal_text(signal: Any) -> str:
    if signal is None:
        return ""
    if isinstance(signal, str):
        return signal.strip()
    if isinstance(signal, Mapping):
        action = signal.get("action") or signal.get("signal")
        return str(action or signal).strip()
    return str(signal).strip()


def _run_one(
    backend: str,
    config: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: str,
    selected_analysts: Sequence[str],
    debug: bool,
    graph_cls=None,
) -> dict[str, Any]:
    started = time.monotonic()
    existing_logs = _run_logs(config, symbol)
    try:
        if graph_cls is None:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph_cls = TradingAgentsGraph
        graph = graph_cls(
            selected_analysts=list(selected_analysts),
            debug=debug,
            config=dict(config),
        )
        state, signal = graph.propagate(symbol, trade_date)
        return {
            "status": "completed",
            "analysis_backend": backend,
            "signal": _signal_text(signal),
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
            "final_state_keys": sorted(state) if isinstance(state, Mapping) else [],
        }
    except Exception as exc:  # Pair state preserves partial evidence.
        retryable = type(exc).__name__ in {
            "ProviderFailure",
            "TimeoutError",
            "ConnectionError",
            "BrokenPipeError",
            "ConnectionResetError",
        }
        if isinstance(exc, OSError):
            retryable = True
        return {
            "status": "failed_retryable" if retryable else "failed_terminal",
            "analysis_backend": backend,
            "signal": "",
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


MAX_ARM_ATTEMPTS = 3


def _new_pair_state(pair_id: str, campaign_fingerprint: str, evidence_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "pair_id": pair_id,
        "campaign_fingerprint": campaign_fingerprint,
        "evidence_sha256": evidence_sha256,
        "status": "NEW",
        "arms": {
            backend: {"status": "pending", "attempts": 0, "attempt_history": []}
            for backend in BACKEND_ORDER
        },
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _load_pair_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"pair_state.json is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise RuntimeError("pair_state.json has an unsupported schema")
    return value


def _recover_interrupted_arms(state: dict[str, Any]) -> None:
    for arm in state.get("arms", {}).values():
        if arm.get("status") == "in_progress":
            arm["status"] = "failed_retryable"
            arm.setdefault("attempt_history", []).append(
                {
                    "status": "failed_retryable",
                    "error_type": "ProcessInterrupted",
                    "error": "previous process exited during arm",
                }
            )
    if any(arm.get("status") == "failed_retryable" for arm in state.get("arms", {}).values()):
        state["status"] = "PARTIAL"


def _validate_pair_state(state: Mapping[str, Any], *, pair_id: str, fingerprint: str, evidence_sha256: str) -> None:
    if state.get("pair_id") != pair_id:
        raise RuntimeError("pair state identity mismatch")
    if state.get("campaign_fingerprint") != fingerprint:
        raise RuntimeError("A/B campaign invariant violation: pair fingerprint changed")
    if state.get("evidence_sha256") != evidence_sha256:
        raise RuntimeError("A/B evidence invariant violation: evidence hash changed")


def run_analysis_ab(
    *,
    symbol: str,
    trade_date: str,
    base_config: Mapping[str, Any] | None = None,
    results_root: str | Path | None = None,
    selected_analysts: Sequence[str] = ("market", "social", "news", "fundamentals", "macro"),
    debug: bool = False,
    graph_cls=None,
) -> dict[str, Any]:
    """Run or resume one crash-safe, frozen-evidence A/B pair."""

    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    try:
        date.fromisoformat(str(trade_date))
    except ValueError as exc:
        raise ValueError(f"trade_date must be ISO YYYY-MM-DD: {trade_date!r}") from exc

    base = deepcopy(dict(DEFAULT_CONFIG if base_config is None else base_config))
    if base.get("auto_trade") is True:
        raise RuntimeError("A/B runner refuses auto_trade=True")
    root = validate_app_path(results_root or (default_results_dir() / "ab"), field="results_dir")
    pair_id = hashlib.sha256(f"{symbol}|{trade_date}".encode("utf-8")).hexdigest()[:16]
    pair_dir = root / str(trade_date) / safe_ticker_component(symbol)
    execution_order = _execution_order(symbol, str(trade_date))
    summary_path = pair_dir / "pair_summary.json"
    state_path = pair_dir / "pair_state.json"
    with _experiment_lock(root):
        if summary_path.exists():
            raise RuntimeError(
                f"A/B pair already completed; refusing to overwrite: {summary_path}"
            )

        packet_path = root / "evidence" / str(trade_date) / safe_ticker_component(symbol) / "evidence_packet.json"
        capture_config = deepcopy(base)
        capture_config.update({"analysis_input_mode": "live_tools", "evidence_packet_sha256": None})
        try:
            evidence = build_or_load_evidence_packet(
                packet_path,
                symbol=symbol,
                trade_date=str(trade_date),
                config=capture_config,
            )
        except EvidenceIntegrityError as exc:
            raise RuntimeError(f"frozen evidence unavailable: {exc}") from exc

        configs = build_ab_configs(
            base,
            symbol=symbol,
            trade_date=str(trade_date),
            pair_dir=pair_dir,
            experiment_root=root,
            evidence_packet_path=packet_path,
            evidence_packet_sha256=evidence["sha256"],
        )
        assert_ab_invariants(configs["traders"], configs["berkshire"])
        campaign_fingerprint = _ensure_campaign_manifest(
            root, configs["traders"], selected_analysts
        )
        pair_dir.mkdir(parents=True, exist_ok=True)

        state = _load_pair_state(state_path)
        if state is None:
            state = _new_pair_state(pair_id, campaign_fingerprint, evidence["sha256"])
        else:
            _validate_pair_state(
                state,
                pair_id=pair_id,
                fingerprint=campaign_fingerprint,
                evidence_sha256=evidence["sha256"],
            )
            if state.get("status") == "FAILED_TERMINAL":
                raise RuntimeError("A/B pair reached FAILED_TERMINAL; use a new results-root")
            _recover_interrupted_arms(state)
        state["status"] = "IN_PROGRESS"
        _write_json_atomic(state_path, state)

        for profile in execution_order:
            arm = state["arms"][profile]
            if arm.get("status") == "completed":
                continue
            if int(arm.get("attempts", 0) or 0) >= MAX_ARM_ATTEMPTS:
                arm["status"] = "failed_terminal"
                state["status"] = "FAILED_TERMINAL"
                _write_json_atomic(state_path, state)
                return state

            arm["attempts"] = int(arm.get("attempts", 0) or 0) + 1
            arm["status"] = "in_progress"
            _write_json_atomic(state_path, state)
            print(f"[AB] Running {profile} analysis backend for {symbol} as of {trade_date}")
            result = _run_one(
                profile,
                configs[profile],
                symbol=symbol,
                trade_date=str(trade_date),
                selected_analysts=selected_analysts,
                debug=debug,
                graph_cls=graph_cls,
            )
            arm["status"] = result["status"]
            arm["result"] = result
            arm.setdefault("attempt_history", []).append(result)
            if result["status"] != "completed":
                terminal = result["status"] == "failed_terminal" or arm["attempts"] >= MAX_ARM_ATTEMPTS
                if terminal:
                    arm["status"] = "failed_terminal"
                    state["status"] = "FAILED_TERMINAL"
                else:
                    state["status"] = "PARTIAL"
                _write_json_atomic(state_path, state)
                return state
            _write_json_atomic(state_path, state)

        if any(state["arms"][backend].get("status") != "completed" for backend in BACKEND_ORDER):
            state["status"] = "PARTIAL"
            _write_json_atomic(state_path, state)
            return state

        state["status"] = "COMPLETED"
        _write_json_atomic(state_path, state)
        results = {backend: state["arms"][backend]["result"] for backend in BACKEND_ORDER}
        traders_signal = results["traders"].get("signal", "").upper()
        berkshire_signal = results["berkshire"].get("signal", "").upper()
        shared = {
            "selected_analysts": list(selected_analysts),
            "analysis_input_mode": "frozen_evidence",
            "evidence_packet_path": str(packet_path),
            "evidence_packet_sha256": evidence["sha256"],
            "evidence_captured_at": evidence["captured_at"],
            "provider": base.get("analysis_provider") or base.get("llm_provider", ""),
            "quick_model": base.get("quick_think_llm", ""),
            "deep_model": base.get("deep_think_llm", ""),
            "research_depth": base.get("research_depth", ""),
            "max_tool_iterations": base.get("max_tool_iterations_per_agent", 0),
            "memory_retrieval_enabled": True,
            "reflection_on_outcome_enabled": True,
            "memory_maintenance_enabled": True,
            "memory_isolation": "per-backend persistent stores",
            "checkpoint_enabled": False,
            "auto_trade": False,
            "execution_mode": "serial, order-counterbalanced, shadow-decision-only",
        }
    summary = {
        "schema_version": 2,
        "pair_id": pair_id,
        "campaign_fingerprint": campaign_fingerprint,
        "symbol": symbol,
        "trade_date": str(trade_date),
        "evidence_sha256": evidence["sha256"],
        "execution_order": execution_order,
        "shared": shared,
        "traders": results["traders"],
        "berkshire": results["berkshire"],
        "signal_agreement": bool(
            traders_signal and berkshire_signal and traders_signal == berkshire_signal
        ),
    }
    temp_summary_path = summary_path.with_suffix(".tmp")
    temp_summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temp_summary_path, summary_path)
    print(f"[AB] Pair summary: {summary_path}")
    return summary


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return deepcopy(DEFAULT_CONFIG)
    config_path = Path(path)
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read config JSON {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Config JSON is invalid: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Config JSON must contain an object")
    config = deepcopy(DEFAULT_CONFIG)
    config.update(loaded)
    return config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", dest="trade_date", required=True)
    parser.add_argument("--config-json", help="Optional JSON object merged over DEFAULT_CONFIG")
    parser.add_argument("--results-root", default=str(default_results_dir() / "ab"))
    parser.add_argument("--analysts", default="market,social,news,fundamentals,macro")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = run_analysis_ab(
            symbol=args.symbol,
            trade_date=args.trade_date,
            base_config=_load_config(args.config_json),
            results_root=args.results_root,
            selected_analysts=tuple(
                analyst.strip() for analyst in args.analysts.split(",") if analyst.strip()
            ),
            debug=args.debug,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"[AB] ERROR: {exc}", file=sys.stderr)
        return 2

    if summary.get("status") == "COMPLETED":
        return 0
    statuses = [
        (summary.get("arms", {}).get(backend) or {}).get("status")
        for backend in BACKEND_ORDER
    ]
    return 0 if all(status == "completed" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
