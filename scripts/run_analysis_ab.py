#!/usr/bin/env python3
"""Run a controlled Traders versus Berkshire analysis pair.

The two runs share one frozen EvidencePacket and the complete downstream graph.
Only ``analysis_backend`` and explicitly isolated learning/run destinations
differ. Shadow decisions are the default; an explicit flag can route each arm
to its dedicated Alpaca Paper account. Runs are intentionally serial because
the application keeps some process-global configuration and audit context.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

# Make ``python scripts/run_analysis_ab.py`` work from a source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.analysis_backends import resolve_analysis_backend
from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    build_or_load_evidence_packet,
    validate_evidence_completeness,
)
from tradingagents.app_identity import default_results_dir, validate_app_path
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.long_run_support.state import atomic_write_json


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
FORMAL_ANALYSTS = ("market", "social", "news", "fundamentals", "macro")
BACKEND_ACCOUNT = {"traders": "A", "berkshire": "B"}
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
        "long_run_dir",
        "safety_state_path",
        "safety_kill_switch_path",
        "_alpaca_account_profile",
    }
)

# These identify one observation within a campaign.  They must be identical
# between arms, but must not make the campaign fingerprint change every day.
_CAMPAIGN_DYNAMIC_KEYS = frozenset(
    {"evidence_packet_path", "evidence_packet_sha256"}
)

_ISOLATED_PATH_KEYS = (
    "results_dir",
    "memory_log_path",
    "agent_memory_dir",
    "data_cache_dir",
    "screening_selection_cache_path",
    "execution_db_path",
    "long_run_dir",
    "safety_state_path",
    "safety_kill_switch_path",
)

_REQUIRED_MEMORY_FLAGS = (
    "memory_retrieval_enabled",
    "memory_maintenance_enabled",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def assert_ab_invariants(
    config_a: Mapping[str, Any],
    config_b: Mapping[str, Any],
    *,
    execute_paper: bool | None = None,
) -> None:
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
    expected_auto_trade = bool(execute_paper) if execute_paper is not None else False
    if (
        config_a.get("auto_trade") is not expected_auto_trade
        or config_b.get("auto_trade") is not expected_auto_trade
    ):
        raise RuntimeError(
            f"A/B invariant violation: auto_trade must be {expected_auto_trade}"
        )
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
    if expected_auto_trade:
        if config_a.get("_alpaca_account_profile") != "A":
            raise RuntimeError("A/B invariant violation: Traders must use Paper account A")
        if config_b.get("_alpaca_account_profile") != "B":
            raise RuntimeError("A/B invariant violation: Berkshire must use Paper account B")

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


@contextmanager
def _preserve_runtime_config():
    """Restore process-global graph and Safety configuration on every exit."""

    from tradingagents.dataflows.config import get_config, replace_config
    from tradingagents.safety import reset_safety_guard

    previous = get_config()
    try:
        yield
    finally:
        replace_config(previous)
        reset_safety_guard()


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
    execute_paper: bool = False,
    paper_notional_usd: float | None = None,
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
            "memory_maintenance_enabled": True,
            "checkpoint_enabled": False,
            "auto_trade": bool(execute_paper),
        }
    )
    if paper_notional_usd is not None:
        shared["paper_notional_usd"] = float(paper_notional_usd)

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
                "long_run_dir": str(profile_dir / "long_run"),
                "safety_state_path": str(profile_dir / "safety" / "state.json"),
                "safety_kill_switch_path": str(profile_dir / "safety" / "KILL_SWITCH"),
                "_alpaca_account_profile": BACKEND_ACCOUNT[backend],
            }
        )
        resolve_analysis_backend(config)
        configs[backend] = config

    assert_ab_invariants(
        configs["traders"], configs["berkshire"], execute_paper=execute_paper
    )
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
    config: Mapping[str, Any],
    selected_analysts: Sequence[str],
    implementation_fingerprint: str,
) -> str:
    shared = {
        key: value
        for key, value in config.items()
        if key not in AB_ALLOWED_DIFFERENCES and key not in _CAMPAIGN_DYNAMIC_KEYS
    }
    payload = {
        "shared_config": shared,
        "selected_analysts": list(selected_analysts),
        "implementation_fingerprint": implementation_fingerprint,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _implementation_fingerprint() -> str:
    """Hash effective experiment code and prompt bytes, including overrides."""

    digest = hashlib.sha256()
    roots_and_patterns = (
        (_REPO_ROOT / "tradingagents", "*.py"),
        (_REPO_ROOT / "tradingagents" / "prompts" / "templates", "*.md"),
        (_REPO_ROOT / "scripts", "*.py"),
    )
    files: dict[str, Path] = {}
    for root, pattern in roots_and_patterns:
        if root.exists():
            for path in root.rglob(pattern):
                files[f"repo:{path.relative_to(_REPO_ROOT).as_posix()}"] = path
    prompt_override = os.getenv("TRADINGBUFFETT_PROMPT_DIR")
    if prompt_override:
        override_root = validate_app_path(prompt_override, field="prompt_dir")
        if override_root.exists():
            for path in override_root.rglob("*.md"):
                files[f"override:{path.relative_to(override_root).as_posix()}"] = path
    for label in sorted(files):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[label].read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _resolved_llm_route_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the effective, non-secret LLM and embedding route configuration.

    The normal provider resolver is deliberately kept as the source of truth.
    This is only a manifest snapshot: credentials are never copied from its
    return value and endpoints use the resolver's display-safe form.
    """

    from tradingagents.dataflows.config import (
        get_config,
        get_embedding_client_config,
        get_openai_base_url,
        get_openai_embedding_model,
        is_local_openai_enabled,
        replace_config,
    )
    from tradingagents.llm_clients.roles import resolve_role_config

    previous = get_config()
    replace_config(dict(config))
    try:
        resolved = resolve_role_config(dict(config))
        embedding_client = get_embedding_client_config()
        embedding_endpoint = str(embedding_client.get("base_url") or "").strip()
        embedding = {
            "provider": (
                str(config.get("embedding_provider") or "").strip()
                or ("local_openai" if is_local_openai_enabled() else "openai")
            ),
            "model": get_openai_embedding_model(),
            # ``RoleSpec.display_endpoint`` is the repository's existing
            # endpoint sanitizer (query, fragment and userinfo removed).
            "endpoint": _display_safe_endpoint(embedding_endpoint),
        }
        if resolved.get("mode") == "roles":
            def route(spec: Any) -> dict[str, str]:
                return {
                    "provider": spec.provider,
                    "model": spec.model,
                    "endpoint": spec.display_endpoint() or "provider default",
                }

            snapshot: dict[str, Any] = {
                "mode": "roles",
                "analysis": route(resolved["analysis"]),
                "decision": route(resolved["decision"]),
                "embedding": embedding,
            }
            fallback = resolved.get("analysis_fallback")
            if fallback is not None:
                snapshot["analysis_fallback"] = route(fallback)
            return snapshot

        provider = str(config.get("llm_provider") or "openai").strip()
        if provider == "openai" and is_local_openai_enabled():
            provider = "local_openai"
        endpoint = get_openai_base_url() or str(config.get("backend_url") or "").strip()
        return {
            "mode": "legacy",
            "analysis": {
                "provider": provider,
                "quick_model": str(config.get("quick_think_llm") or ""),
                "deep_model": str(config.get("deep_think_llm") or ""),
                "endpoint": _display_safe_endpoint(endpoint),
            },
            "embedding": embedding,
        }
    finally:
        replace_config(previous)


def _display_safe_endpoint(endpoint: str) -> str:
    """Use RoleSpec's established secret-safe endpoint display behavior."""

    from tradingagents.llm_clients.roles import RoleSpec

    return RoleSpec(
        role="manifest",
        provider="openai",
        model="manifest",
        backend_url=endpoint or None,
        provider_explicit=False,
    ).display_endpoint() or "provider default"


def resolved_llm_route_snapshots(
    configs: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Snapshot each arm separately even when their routes are identical."""

    return {
        backend: _resolved_llm_route_snapshot(configs[backend])
        for backend in BACKEND_ORDER
    }


def _ensure_campaign_manifest(
    root: Path,
    config: Mapping[str, Any],
    selected_analysts: Sequence[str],
    *,
    account_preflight: Mapping[str, Mapping[str, Any]] | None = None,
    resolved_routes: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Pin experimental conditions across the full multi-day campaign."""

    implementation_fingerprint = _implementation_fingerprint()
    fingerprint = _campaign_fingerprint(
        config, selected_analysts, implementation_fingerprint
    )
    manifest_path = root / "AB_CAMPAIGN.json"
    routes = dict(resolved_routes or {"traders": _resolved_llm_route_snapshot(config), "berkshire": _resolved_llm_route_snapshot(config)})
    if set(routes) != set(BACKEND_ORDER):
        raise RuntimeError("A/B campaign resolved LLM routes must contain both arms")
    account_refs = (
        {
            backend: str((account_preflight or {}).get(backend, {}).get("account_ref") or "")
            for backend in BACKEND_ORDER
        }
        if config.get("auto_trade")
        else None
    )
    if account_refs is not None and any(not value for value in account_refs.values()):
        raise RuntimeError("A/B Paper account preflight did not return pinned identities")
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
            or existing.get("implementation_fingerprint")
            != implementation_fingerprint
            or existing.get("config_fingerprint") != fingerprint
            or existing.get("account_refs") != account_refs
            or existing.get("resolved_llm_routes") != routes
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
        "auto_trade": bool(config.get("auto_trade")),
        "max_arm_attempts": 3,
        "selected_analysts": list(selected_analysts),
        "implementation_fingerprint": implementation_fingerprint,
        "config_fingerprint": fingerprint,
        "resolved_llm_routes": routes,
        "memory_policy": "enabled and isolated per profile",
        "execution_policy": (
            "serial, order-counterbalanced, isolated Alpaca Paper accounts A/B"
            if config.get("auto_trade")
            else "serial, order-counterbalanced, shadow decisions only"
        ),
        "account_assignment": (
            dict(BACKEND_ACCOUNT) if config.get("auto_trade") else None
        ),
        "account_refs": account_refs,
        "berkshire_reference": {
            "repo": "xbtlin/ai-berkshire",
            "commit": "1cc1e362378cd3fea99a4f4c3b50676bce9aa4c6",
        },
        "traders_baseline": {
            "commit": "81123b6e7abe6526147bc79b469f68ee8a9c5e64",
        },
    }
    atomic_write_json(manifest_path, payload)
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


def _jsonable_intent(value: Any) -> dict[str, Any] | None:
    from tradingagents.execution.order_planning import validate_trade_intent

    validated, _ = validate_trade_intent(value)
    return validated


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
    # The safety guard is a process singleton.  Install the arm config before
    # the first guard lookup and clear it after the arm so A/B order cannot
    # decide which profile owns the runtime state.
    from tradingagents.dataflows.config import set_config
    from tradingagents.safety import get_safety_guard, reset_safety_guard

    set_config(dict(config))
    reset_safety_guard()
    try:
        try:
            budget = get_safety_guard().check_llm_budget()
        except Exception as exc:
            return {
                "status": "failed_terminal",
                "analysis_backend": backend,
                "signal": "",
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
                "error_type": type(exc).__name__,
                "error": f"LLM budget gate unavailable: {exc}",
                "decision_valid": False,
            }
        if not budget.allowed:
            return {
                "status": "failed_terminal",
                "analysis_backend": backend,
                "signal": "",
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
                "error_type": "LLMBudgetExhausted",
                "error": "; ".join(budget.reasons),
                "decision_valid": False,
            }
        if graph_cls is None:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph_cls = TradingAgentsGraph
        graph = graph_cls(
            selected_analysts=list(selected_analysts),
            debug=debug,
            config=dict(config),
        )
        state, signal = graph.propagate(symbol, trade_date)
        if isinstance(state, Mapping) and state.get("risk_invalid_reason"):
            reason = str(state["risk_invalid_reason"])
            return {
                "status": "failed_terminal",
                "analysis_backend": backend,
                "signal": _signal_text(signal),
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
                "final_state_keys": sorted(state),
                "trade_intent": None,
                "decision_valid": False,
                "risk_invalid_reason": reason,
                "error_type": "InvalidRiskDecision",
                "error": f"invalid risk decision: {reason}",
            }
        intent = _jsonable_intent(
            state.get("final_trade_intent") if isinstance(state, Mapping) else None
        )
        return {
            "status": "completed" if intent is not None else "failed_terminal",
            "analysis_backend": backend,
            "signal": _signal_text(signal),
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "run_log": _latest_run_log(config, symbol, exclude=existing_logs),
            "final_state_keys": sorted(state) if isinstance(state, Mapping) else [],
            "trade_intent": intent,
            "decision_valid": intent is not None,
            **({"error_type": "InvalidTradeIntent", "error": "missing or invalid final_trade_intent"}
               if intent is None else {}),
        }
    except Exception as exc:  # Pair state preserves partial evidence.
        retryable = bool(getattr(exc, "retryable", False)) or type(exc).__name__ in {
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
    finally:
        reset_safety_guard()


MAX_ARM_ATTEMPTS = 3


def _paper_account_preflight(
    *,
    trade_date: str,
    require_matched_flat_start: bool,
) -> dict[str, dict[str, Any]]:
    """Prove two distinct, mutation-enabled Alpaca Paper accounts are usable."""

    from tradingagents.dataflows.alpaca_utils import (
        alpaca_read_only_enabled,
        get_alpaca_trading_client,
    )
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    if alpaca_read_only_enabled():
        raise RuntimeError(
            "Paper execution requested but TRADINGBUFFETT_ALPACA_READ_ONLY is enabled"
        )
    snapshots: dict[str, dict[str, Any]] = {}
    for backend, account in BACKEND_ACCOUNT.items():
        client = get_alpaca_trading_client(account=account, read_only=False)
        raw_account = client.get_account()
        account_id = str(getattr(raw_account, "id", "") or "")
        equity = float(getattr(raw_account, "equity", 0) or 0)
        positions = list(client.get_all_positions() or [])
        orders = list(
            client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) or []
        )
        clock = client.get_clock()
        clock_timestamp = getattr(clock, "timestamp", None)
        if clock_timestamp is None or not hasattr(clock_timestamp, "date"):
            raise RuntimeError(f"Paper account {account} returned an invalid market clock")
        if getattr(clock_timestamp, "tzinfo", None) is None:
            raise RuntimeError(f"Paper account {account} returned a timezone-naive market clock")
        clock_date = clock_timestamp.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat()
        if clock_date != trade_date:
            raise RuntimeError(
                f"Paper execution date {trade_date} does not match Alpaca market date {clock_date}"
            )
        if getattr(clock, "is_open", None) is not True:
            raise RuntimeError("Alpaca Paper market is closed; refusing to start an execution pair")
        if not account_id or equity <= 0:
            raise RuntimeError(f"Paper account {account} has no usable identity/equity")
        if require_matched_flat_start and (positions or orders):
            raise RuntimeError(
                f"Paper account {account} must start flat with no open orders "
                f"(positions={len(positions)}, orders={len(orders)})"
            )
        snapshots[backend] = {
            "account": account,
            "account_ref": hashlib.sha256(account_id.encode()).hexdigest()[:16],
            "account_id": account_id,
            "equity": equity,
            "market_date": clock_date,
        }
    if snapshots["traders"]["account_id"] == snapshots["berkshire"]["account_id"]:
        raise RuntimeError("A/B Paper accounts A and B resolve to the same account")
    left = snapshots["traders"]["equity"]
    right = snapshots["berkshire"]["equity"]
    if require_matched_flat_start and abs(left - right) > max(1.0, min(left, right) * 0.001):
        raise RuntimeError(
            "A/B Paper starting equity mismatch exceeds 0.1%/$1 tolerance"
        )
    for snapshot in snapshots.values():
        snapshot.pop("account_id", None)
    return snapshots


def _execute_paper_arm_inner(
    backend: str,
    config: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: str,
    pair_id: str,
    paper_notional_usd: float,
    now_fn=None,
) -> dict[str, Any]:
    from tradingagents.dataflows.alpaca_utils import get_alpaca_execution_client
    from tradingagents.dataflows.config import set_config
    from tradingagents.execution import ExecutionService
    from tradingagents.execution.auto_trade import execute_auto_trade
    from tradingagents.safety import reset_safety_guard
    from tradingagents.long_run import effective_target_for_session
    from tradingagents.long_run_support.sessions import (
        AB_RUN_TIME_ET,
        make_session_submit_guard,
    )

    intent = result.get("trade_intent")
    if not isinstance(intent, Mapping):
        return {
            "success": False,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": "completed analysis produced no schema-valid TradeIntent",
        }
    account = BACKEND_ACCOUNT[backend]
    set_config(dict(config))
    reset_safety_guard()
    broker_factory = lambda: get_alpaca_execution_client(
        account=account, read_only=False
    )
    service = ExecutionService(
        db_path=config["execution_db_path"], broker_factory=broker_factory
    )
    try:
        schedule = effective_target_for_session(
            date.fromisoformat(trade_date), AB_RUN_TIME_ET
        )
        can_submit = make_session_submit_guard(
            session_date=trade_date,
            effective_target=str(schedule["effective_target"]),
            now_fn=now_fn or (lambda: datetime.now(timezone.utc)),
        )
    except Exception:
        # Calendar authority failure forbids new exposure; recovery still
        # runs so existing accepted/unknown orders can be reconciled.
        can_submit = lambda: False
    recovery = service.startup_recover(can_submit=can_submit)
    if not recovery.get("success"):
        return {
            "success": False,
            "paused": True,
            "broker_attempted": False,
            "broker_calls": int(recovery.get("broker_calls") or 0),
            "error": "Paper account recovery is not CLEAN: "
            + str(recovery.get("reconciliation_reasons") or recovery.get("error")),
        }
    execution = execute_auto_trade(
        ticker=symbol,
        trade_intent=dict(intent),
        base_trade_notional_usd=float(paper_notional_usd),
        allow_shorts=bool(config.get("allow_shorts", False)),
        config=dict(config),
        execution_service=service,
        decision_id=f"ab-{pair_id}-{backend}-{trade_date}-{symbol}",
        run_id=f"ab-{pair_id}-{backend}",
        can_submit=can_submit,
    )
    return {
        "success": bool(execution.get("success")),
        "broker_attempted": bool(execution.get("broker_attempted")),
        "broker_calls": int(execution.get("broker_calls") or 0),
        "hold": bool(execution.get("hold")),
        "deduped": bool(execution.get("deduped")),
        "paused": bool(execution.get("paused")),
        "has_unknown": bool(execution.get("has_unknown")),
        "error": str(execution.get("error") or "")[:500],
        "account": account,
        "orders": [
            {
                "client_order_id": order.get("client_order_id"),
                "status": order.get("status"),
            }
            for order in (execution.get("orders") or [])
            if isinstance(order, Mapping)
        ],
    }


def _execute_paper_arm(
    backend: str,
    config: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: str,
    pair_id: str,
    paper_notional_usd: float,
) -> dict[str, Any]:
    """Execute one Paper arm and always return a persistable terminal result."""

    try:
        return _execute_paper_arm_inner(
            backend,
            config,
            result,
            symbol=symbol,
            trade_date=trade_date,
            pair_id=pair_id,
            paper_notional_usd=paper_notional_usd,
        )
    except Exception as exc:
        return {
            "success": False,
            "paused": True,
            "broker_attempted": None,
            "broker_calls": 0,
            "has_unknown": True,
            "error": f"Paper execution raised {type(exc).__name__}",
        }


def _paper_execution_hard_stop(result: Mapping[str, Any]) -> str | None:
    """Classify execution states that cannot count as a completed arm."""
    if result.get("paused"):
        return "execution_paused"
    if result.get("has_unknown") or str(result.get("status") or "").upper() == "UNKNOWN":
        return "execution_unknown"
    return None


def _new_pair_state(
    pair_id: str,
    campaign_fingerprint: str,
    evidence_sha256: str,
    *,
    symbol: str,
    trade_date: str,
    evidence_packet_path: str,
    paper_notional_usd: float | None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "pair_id": pair_id,
        "campaign_fingerprint": campaign_fingerprint,
        "evidence_sha256": evidence_sha256,
        "symbol": symbol,
        "trade_date": trade_date,
        "evidence_packet_path": evidence_packet_path,
        "paper_notional_usd": paper_notional_usd,
        "status": "NEW",
        "arms": {
            backend: {"status": "pending", "attempts": 0, "attempt_history": []}
            for backend in BACKEND_ORDER
        },
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_json(path, payload)


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


def _assert_no_prior_unfinished_pairs(results_root: Path, requested_trade_date: str) -> None:
    """Reject a new trading date while any earlier campaign pair is unfinished."""

    requested = date.fromisoformat(requested_trade_date)
    legal_statuses = {"NEW", "IN_PROGRESS", "PARTIAL", "COMPLETED", "FAILED_TERMINAL"}
    for state_path in sorted(results_root.glob("*/*/pair_state.json")):
        state = _load_pair_state(state_path)
        if state is None:
            raise RuntimeError(f"pair_state.json disappeared during continuity check: {state_path}")

        state_trade_date = state.get("trade_date")
        if not isinstance(state_trade_date, str):
            raise RuntimeError(f"pair_state.json is missing trade_date: {state_path}")
        try:
            parsed_trade_date = date.fromisoformat(state_trade_date)
        except ValueError as exc:
            raise RuntimeError(f"pair_state.json has invalid trade_date: {state_path}") from exc

        status = state.get("status")
        if status not in legal_statuses:
            raise RuntimeError(f"pair_state.json has invalid status: {state_path}")
        if state_path.parent.parent.name != state_trade_date:
            raise RuntimeError(
                f"pair_state.json trade_date conflicts with its directory: {state_path}"
            )
        for field in ("pair_id", "campaign_fingerprint", "evidence_sha256", "symbol"):
            if not isinstance(state.get(field), str) or not state[field]:
                raise RuntimeError(f"pair_state.json is missing {field}: {state_path}")

        # Validate the complete persisted shape before trusting even a completed
        # state.  A malformed old state must never be treated as completed.
        _validate_pair_state(
            state,
            pair_id=state.get("pair_id"),
            fingerprint=state.get("campaign_fingerprint"),
            evidence_sha256=state.get("evidence_sha256"),
        )
        if parsed_trade_date < requested and status != "COMPLETED":
            raise RuntimeError(
                "A/B campaign continuity violation: prior pair is unfinished "
                f"({state_trade_date}, status={status})"
            )


def _recover_completed_analysis(
    config: Mapping[str, Any],
    *,
    backend: str,
    symbol: str,
    trade_date: str,
    evidence_sha256: str,
) -> dict[str, Any] | None:
    candidates = sorted(
        _run_logs(config, symbol), key=lambda path: path.stat().st_mtime_ns, reverse=True
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = payload.get("metadata") or {}
        if (
            payload.get("status") != "completed"
            or str(payload.get("trade_date")) != trade_date
            or metadata.get("analysis_backend") != backend
            or metadata.get("analysis_source") != f"ab_{backend}"
            or metadata.get("evidence_packet_sha256") != evidence_sha256
        ):
            continue
        final_state = (payload.get("snapshots") or {}).get("final_state")
        if not isinstance(final_state, Mapping):
            continue
        risk_invalid_reason = final_state.get("risk_invalid_reason")
        intent = _jsonable_intent(final_state.get("final_trade_intent"))
        invalid = bool(risk_invalid_reason) or intent is None
        result = {
            "status": "failed_terminal" if invalid else "completed",
            "analysis_backend": backend,
            "signal": _signal_text((payload.get("summary") or {}).get("final_signal")),
            "elapsed_seconds": 0.0,
            "run_log": str(path),
            "final_state_keys": sorted(final_state),
            "trade_intent": None if invalid else intent,
            "recovered_from_completed_run_log": True,
            "decision_valid": not invalid,
        }
        if risk_invalid_reason:
            result.update({
                "risk_invalid_reason": str(risk_invalid_reason),
                "error_type": "InvalidRiskDecision",
                "error": f"invalid risk decision: {risk_invalid_reason}",
            })
        elif invalid:
            result.update({
                "error_type": "InvalidTradeIntent",
                "error": "missing or invalid final_trade_intent",
            })
        return result
    return None


def _recover_interrupted_arms(
    state: dict[str, Any],
    *,
    configs: Mapping[str, Mapping[str, Any]],
    symbol: str,
    trade_date: str,
    evidence_sha256: str,
    execute_paper: bool,
) -> None:
    for backend, arm in state.get("arms", {}).items():
        if arm.get("status") == "in_progress":
            recovered = _recover_completed_analysis(
                configs[backend],
                backend=backend,
                symbol=symbol,
                trade_date=trade_date,
                evidence_sha256=evidence_sha256,
            )
            if recovered is not None:
                arm["result"] = recovered
                arm.setdefault("attempt_history", []).append(recovered)
                if recovered.get("status") == "failed_terminal":
                    arm["status"] = "failed_terminal"
                else:
                    arm["status"] = "analysis_completed" if execute_paper else "completed"
                continue
            arm["status"] = "failed_retryable"
            arm.setdefault("attempt_history", []).append(
                {
                    "status": "failed_retryable",
                    "error_type": "ProcessInterrupted",
                    "error": "previous process exited during arm",
                }
            )
    if any(arm.get("status") == "failed_terminal" for arm in state.get("arms", {}).values()):
        state["status"] = "FAILED_TERMINAL"
    elif any(
        arm.get("status") in {"failed_retryable", "analysis_completed"}
        for arm in state.get("arms", {}).values()
    ):
        state["status"] = "PARTIAL"


def _validate_pair_state(state: Mapping[str, Any], *, pair_id: str, fingerprint: str, evidence_sha256: str) -> None:
    legal_pair_states = {"NEW", "IN_PROGRESS", "PARTIAL", "COMPLETED", "FAILED_TERMINAL"}
    legal_arm_states = {
        "pending",
        "in_progress",
        "analysis_completed",
        "completed",
        "failed_retryable",
        "failed_terminal",
    }
    if state.get("pair_id") != pair_id:
        raise RuntimeError("pair state identity mismatch")
    if state.get("campaign_fingerprint") != fingerprint:
        raise RuntimeError("A/B campaign invariant violation: pair fingerprint changed")
    if state.get("evidence_sha256") != evidence_sha256:
        raise RuntimeError("A/B evidence invariant violation: evidence hash changed")
    if state.get("status") not in legal_pair_states:
        raise RuntimeError("pair state contains an invalid status")
    arms = state.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != set(BACKEND_ORDER):
        raise RuntimeError("pair state must contain exactly the two A/B arms")
    for backend in BACKEND_ORDER:
        arm = arms[backend]
        if not isinstance(arm, Mapping) or arm.get("status") not in legal_arm_states:
            raise RuntimeError(f"pair state contains an invalid {backend} arm")
        attempts = arm.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= MAX_ARM_ATTEMPTS:
            raise RuntimeError(f"pair state contains invalid {backend} attempts")
        history = arm.get("attempt_history")
        if not isinstance(history, list):
            raise RuntimeError(f"pair state contains invalid {backend} attempt history")
        arm_status = arm.get("status")
        if arm_status == "pending" and attempts != 0:
            raise RuntimeError(f"pair state pending {backend} arm has attempts")
        if arm_status != "pending" and attempts < 1:
            raise RuntimeError(f"pair state active {backend} arm has no attempt")
        if arm_status in {"analysis_completed", "completed"}:
            result = arm.get("result")
            if (
                not isinstance(result, Mapping)
                or result.get("status") != "completed"
                or result.get("analysis_backend") != backend
            ):
                raise RuntimeError(f"pair state contains invalid completed {backend} result")
    statuses = {arms[backend].get("status") for backend in BACKEND_ORDER}
    if state.get("status") == "COMPLETED" and statuses != {"completed"}:
        raise RuntimeError("completed pair state contains unfinished arms")
    if "failed_terminal" in statuses and state.get("status") != "FAILED_TERMINAL":
        raise RuntimeError("terminal arm is inconsistent with pair state")


def run_analysis_ab(
    *,
    symbol: str,
    trade_date: str,
    base_config: Mapping[str, Any] | None = None,
    results_root: str | Path | None = None,
    selected_analysts: Sequence[str] = ("market", "social", "news", "fundamentals", "macro"),
    debug: bool = False,
    graph_cls=None,
    execute_paper: bool = False,
    paper_notional_usd: float | None = None,
) -> dict[str, Any]:
    """Run or resume one crash-safe, frozen-evidence A/B pair."""

    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    symbol = symbol.strip().upper()
    try:
        date.fromisoformat(str(trade_date))
    except ValueError as exc:
        raise ValueError(f"trade_date must be ISO YYYY-MM-DD: {trade_date!r}") from exc

    analysts = tuple(str(item).strip().lower() for item in selected_analysts)
    if analysts != FORMAL_ANALYSTS:
        raise RuntimeError(
            "formal A/B requires exactly these analysts in this order: "
            + ",".join(FORMAL_ANALYSTS)
        )
    selected_analysts = analysts
    if execute_paper:
        try:
            notional = float(paper_notional_usd)
        except (TypeError, ValueError) as exc:
            raise ValueError("paper_notional_usd is required for Paper execution") from exc
        if notional <= 0 or notional != notional or notional == float("inf"):
            raise ValueError("paper_notional_usd must be a positive finite number")
        paper_notional_usd = notional
    elif paper_notional_usd is not None:
        raise ValueError("paper_notional_usd requires execute_paper=True")

    base = deepcopy(dict(DEFAULT_CONFIG if base_config is None else base_config))
    root = validate_app_path(results_root or (default_results_dir() / "ab"), field="results_dir")
    pair_id = hashlib.sha256(f"{symbol}|{trade_date}".encode("utf-8")).hexdigest()[:16]
    pair_dir = root / str(trade_date) / safe_ticker_component(symbol)
    execution_order = _execution_order(symbol, str(trade_date))
    summary_path = pair_dir / "pair_summary.json"
    state_path = pair_dir / "pair_state.json"
    with _experiment_lock(root), _preserve_runtime_config():
        if summary_path.exists():
            raise RuntimeError(
                f"A/B pair already completed; refusing to overwrite: {summary_path}"
            )

        _assert_no_prior_unfinished_pairs(root, str(trade_date))

        new_campaign = not (root / "AB_CAMPAIGN.json").exists()
        account_preflight = (
            _paper_account_preflight(
                trade_date=str(trade_date),
                require_matched_flat_start=new_campaign,
            )
            if execute_paper
            else {}
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
        try:
            validate_evidence_completeness(evidence)
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
            execute_paper=execute_paper,
            paper_notional_usd=paper_notional_usd,
        )
        assert_ab_invariants(
            configs["traders"], configs["berkshire"], execute_paper=execute_paper
        )
        campaign_fingerprint = _ensure_campaign_manifest(
            root,
            configs["traders"],
            selected_analysts,
            account_preflight=account_preflight,
            resolved_routes=resolved_llm_route_snapshots(configs),
        )
        pair_dir.mkdir(parents=True, exist_ok=True)
        # Evidence collection is a shared phase, never an A/B arm.
        from tradingagents.safety import reset_safety_guard
        reset_safety_guard()

        state = _load_pair_state(state_path)
        if state is None:
            state = _new_pair_state(
                pair_id,
                campaign_fingerprint,
                evidence["sha256"],
                symbol=symbol,
                trade_date=str(trade_date),
                evidence_packet_path=str(packet_path),
                paper_notional_usd=paper_notional_usd,
            )
        else:
            _validate_pair_state(
                state,
                pair_id=pair_id,
                fingerprint=campaign_fingerprint,
                evidence_sha256=evidence["sha256"],
            )
            if state.get("paper_notional_usd") != paper_notional_usd:
                raise RuntimeError(
                    "A/B pair invariant violation: paper_notional_usd changed"
                )
            if state.get("status") == "FAILED_TERMINAL":
                raise RuntimeError("A/B pair reached FAILED_TERMINAL; use a new results-root")
            _recover_interrupted_arms(
                state,
                configs=configs,
                symbol=symbol,
                trade_date=str(trade_date),
                evidence_sha256=evidence["sha256"],
                execute_paper=execute_paper,
            )
            if state.get("status") == "FAILED_TERMINAL":
                _write_json_atomic(state_path, state)
                return state
        state["status"] = "IN_PROGRESS"
        _write_json_atomic(state_path, state)

        for profile in execution_order:
            arm = state["arms"][profile]
            if arm.get("status") == "completed":
                continue
            if arm.get("status") == "analysis_completed":
                result = dict(arm.get("result") or {})
            else:
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
            if result["status"] == "completed" and execute_paper:
                # Persist the completed analysis before broker work. If the
                # process dies after a POST, resume starts from this exact
                # TradeIntent and the durable decision ID deduplicates it.
                arm["status"] = "analysis_completed"
                arm["result"] = result
                _write_json_atomic(state_path, state)
                execution = _execute_paper_arm(
                    profile,
                    configs[profile],
                    result,
                    symbol=symbol,
                    trade_date=str(trade_date),
                    pair_id=pair_id,
                    paper_notional_usd=float(paper_notional_usd),
                )
                result["paper_execution"] = execution
                hard_stop = _paper_execution_hard_stop(execution)
                if hard_stop or not execution.get("success"):
                    result["status"] = "failed_terminal"
                    result["error_type"] = "PaperExecutionFailed"
                    result["error"] = (
                        execution.get("error")
                        or hard_stop
                        or "Paper execution failed"
                    )
                    result["decision_valid"] = True
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
            "memory_retrieval_enabled": configs["traders"]["memory_retrieval_enabled"],
            "reflection_on_outcome_enabled": configs["traders"].get(
                "reflection_on_outcome_enabled", False
            ),
            "memory_maintenance_enabled": configs["traders"]["memory_maintenance_enabled"],
            "memory_isolation": "per-backend persistent stores",
            "checkpoint_enabled": False,
            "auto_trade": bool(execute_paper),
            "execution_mode": (
                "serial, order-counterbalanced, isolated Alpaca Paper accounts"
                if execute_paper
                else "serial, order-counterbalanced, shadow-decision-only"
            ),
            "paper_notional_usd": paper_notional_usd,
            "paper_account_preflight": account_preflight,
        }
        summary = {
            "schema_version": 2,
            "status": "COMPLETED",
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
        _write_json_atomic(summary_path, summary)
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
    parser.add_argument(
        "--execute-paper",
        action="store_true",
        help="Execute each arm on its isolated Alpaca Paper account (A/B)",
    )
    parser.add_argument(
        "--paper-notional-usd",
        type=float,
        help="Maximum notional per arm; required with --execute-paper",
    )
    args = parser.parse_args(argv)

    from tradingagents.long_run import GlobalRunnerLockBusy, global_runner_lock

    try:
        # Outermost lock: exactly one Paper runner process application-wide.
        with global_runner_lock():
            summary = run_analysis_ab(
                symbol=args.symbol,
                trade_date=args.trade_date,
                base_config=_load_config(args.config_json),
                results_root=args.results_root,
                selected_analysts=tuple(
                    analyst.strip() for analyst in args.analysts.split(",") if analyst.strip()
                ),
                debug=args.debug,
                execute_paper=args.execute_paper,
                paper_notional_usd=args.paper_notional_usd,
            )
    except GlobalRunnerLockBusy as exc:
        print(f"[AB] ERROR: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        print(f"[AB] ERROR: {exc}", file=sys.stderr)
        return 2

    if summary.get("status") in {"COMPLETED", "completed"}:
        return 0
    if not summary.get("arms") and all(
        (summary.get(backend) or {}).get("status") == "completed"
        for backend in BACKEND_ORDER
    ):
        return 0
    statuses = [
        (summary.get("arms", {}).get(backend) or {}).get("status")
        for backend in BACKEND_ORDER
    ]
    return 0 if all(status == "completed" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
