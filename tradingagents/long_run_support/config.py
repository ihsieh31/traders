"""Phase-D non-secret configuration, validation, and runtime construction.

The public wrappers supply named collaborators at call time. Configuration
paths are not cached, and optional runtime dependencies remain lazy imports.
"""

from __future__ import annotations

import json
import math
import subprocess
import urllib.parse
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


from tradingagents.analysis_backends.resolver import VALID_ANALYSIS_BACKENDS


def default_long_run_config(
    *,
    LONG_RUN_SCHEMA_VERSION: int,
    DEFAULT_DURATION_CALENDAR_DAYS: int,
    DEFAULT_RUN_TIME_ET: str,
    VALID_ANALYSTS: Tuple[str, ...],
) -> Dict[str, Any]:
    return {
        "schema_version": LONG_RUN_SCHEMA_VERSION,
        "duration_calendar_days": DEFAULT_DURATION_CALENDAR_DAYS,
        # Continuous mode: never auto-finalize; duration_calendar_days becomes
        # the scheduling-window chunk size, not a total cap.
        "continuous": False,
        # Analysis topology for the single runner: native Traders graph or the
        # Berkshire Hathaway coordinator (frozen evidence, isolated artifacts).
        "analysis_backend": "traders",
        "run_time_et": DEFAULT_RUN_TIME_ET,
        "base_trade_notional_usd": None,
        "analysts": list(VALID_ANALYSTS),
        "research_depth": 3,
        "output_language": "English",
        "analysis_provider": None,
        "analysis_model": None,
        "analysis_backend_url": None,
        "decision_provider": None,
        "decision_model": None,
        "decision_backend_url": None,
        # Optional Analysis-only failover route (Phase B): all three keys
        # unset means failover stays disabled. Secrets never live here.
        "analysis_fallback_provider": None,
        "analysis_fallback_model": None,
        "analysis_fallback_backend_url": None,
        # Short exposure opt-in for the observation (paper-only build;
        # broker-side deterministic guards always apply).
        "allow_shorts": False,
        "screening_provider": None,
        "screening_model": None,
        "screening_backend_url": None,
    }


def load_long_run_config(
    *,
    default_long_run_config: Callable[[], Dict[str, Any]],
    read_json: Callable[[Path], Optional[Any]],
    config_path: Callable[[], Path],
) -> Dict[str, Any]:
    cfg = default_long_run_config()
    saved = read_json(config_path())
    if isinstance(saved, dict):
        for key in cfg:
            if saved.get(key) is not None:
                cfg[key] = saved[key]
    return cfg


def save_long_run_config(
    cfg: Dict[str, Any],
    *,
    default_long_run_config: Callable[[], Dict[str, Any]],
    LONG_RUN_SCHEMA_VERSION: int,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
) -> None:
    payload = {k: cfg.get(k) for k in default_long_run_config()}
    payload["schema_version"] = LONG_RUN_SCHEMA_VERSION
    text = json.dumps(payload, default=str)
    lowered = text.lower()
    if any(m.lower() in lowered for m in ("api_key", "secret_key", "sk-")):
        raise ValueError("refusing to persist possible secret material in config.json")
    atomic_write_json(config_path(), payload)


def _valid_backend_url(value: Any) -> bool:
    if value is None or str(value).strip() == "":
        return True
    try:
        parts = urllib.parse.urlsplit(str(value).strip())
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except Exception:
        return False


def missing_config_fields(
    cfg: Dict[str, Any],
    *,
    _looks_placeholder: Callable[[Any], bool],
    PROVIDERS_REQUIRING_URL: Tuple[str, ...],
) -> List[str]:
    """Fields the setup wizard must ask for (absent/invalid/placeholder)."""
    missing: List[str] = []
    if cfg.get("base_trade_notional_usd") is None:
        missing.append("base_trade_notional_usd")
    if not cfg.get("run_time_et"):
        missing.append("run_time_et")
    for key in (
        "analysis_provider", "analysis_model",
        "decision_provider", "decision_model",
        "screening_provider", "screening_model",
    ):
        value = cfg.get(key)
        if not value or _looks_placeholder(value):
            missing.append(key)
    for key in (
        "analysis_backend_url", "decision_backend_url", "screening_backend_url",
    ):
        provider = cfg.get(key.replace("_backend_url", "_provider")) or ""
        if str(provider).lower() in PROVIDERS_REQUIRING_URL and not cfg.get(key):
            if key not in missing:
                missing.append(key)
    if not cfg.get("analysts"):
        missing.append("analysts")
    return missing


def _unattended_safety_error(runtime: Dict[str, Any]) -> str:
    """P2-01: an unattended paper run cannot run with the safety layer off.

    Only ``runtime.get("safety_enabled", True) is True`` may proceed: the
    key may be absent (default on), but False/0/"false"/None (explicit)
    must refuse. The general SafetyGuard feature itself is untouched —
    this gates only the unattended long-run production path.
    """
    if runtime.get("safety_enabled", True) is True:
        return ""
    return (
        "unattended paper execution requires safety_enabled=True "
        f"(got {runtime.get('safety_enabled')!r})"
    )


def _validate_unattended_safety(
    runtime: Dict[str, Any],
    *,
    _unattended_safety_error: Callable[[Dict[str, Any]], str],
    LongRunStop: type[RuntimeError],
) -> None:
    error = _unattended_safety_error(runtime)
    if error:
        raise LongRunStop("SAFETY_DISABLED", error)
    _validate_unattended_limits(runtime, LongRunStop=LongRunStop)


def _validate_unattended_limits(config: Dict[str, Any], *, LongRunStop: type[RuntimeError]) -> None:
    from tradingagents.default_config import DEFAULT_CONFIG
    for key, upper in (
        ("max_trade_notional_usd", None),
        ("max_symbol_concentration_pct", 100),
        ("daily_loss_halt_pct", 100),
        ("max_drawdown_halt_pct", 100),
    ):
        raw = config.get(key, DEFAULT_CONFIG[key])
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            value = math.nan
        if isinstance(raw, bool) or not math.isfinite(value) or value <= 0 or (upper and value > upper):
            raise LongRunStop("SAFETY_LIMIT_INVALID", f"{key} must be finite and in (0, {upper or 'infinity'}]")
    raw = config.get("max_consecutive_rejections", DEFAULT_CONFIG["max_consecutive_rejections"])
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise LongRunStop("SAFETY_LIMIT_INVALID", "max_consecutive_rejections must be an integer >= 1")
    timeout = config.get("llm_request_timeout_seconds", DEFAULT_CONFIG["llm_request_timeout_seconds"])
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout_value = math.nan
    if isinstance(timeout, bool) or not math.isfinite(timeout_value) or timeout_value <= 0:
        raise LongRunStop("SAFETY_LIMIT_INVALID", "llm_request_timeout_seconds must be finite and > 0")


def validate_long_run_config(
    cfg: Dict[str, Any],
    runtime: Optional[Dict[str, Any]] = None,
    *,
    LONG_RUN_SCHEMA_VERSION: int,
    parse_run_time_et: Callable[[str], dtime],
    SESSION_OPEN_ET: dtime,
    SESSION_CLOSE_ET: dtime,
    VALID_ANALYSTS: Tuple[str, ...],
    VALID_RESEARCH_DEPTHS: Tuple[int, ...],
    _valid_backend_url: Callable[[Any], bool],
    PROVIDERS_REQUIRING_URL: Tuple[str, ...],
    _unattended_safety_error: Callable[[Dict[str, Any]], str],
) -> List[str]:
    """Secret-free validation; returns error strings (empty = valid)."""
    errors: List[str] = []
    if cfg.get("schema_version") != LONG_RUN_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {cfg.get('schema_version')!r}")
    duration = cfg.get("duration_calendar_days")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        errors.append("duration_calendar_days must be a positive integer")
    continuous = cfg.get("continuous", False)
    if not isinstance(continuous, bool):
        errors.append("continuous must be a boolean")
    backend = cfg.get("analysis_backend", "traders")
    if not isinstance(backend, str) or backend not in VALID_ANALYSIS_BACKENDS:
        allowed = ", ".join(sorted(VALID_ANALYSIS_BACKENDS))
        errors.append(f"analysis_backend must be one of: {allowed}")
    try:
        target = parse_run_time_et(str(cfg.get("run_time_et") or ""))
    except ValueError as exc:
        errors.append(str(exc))
        target = None
    if target is not None and not (SESSION_OPEN_ET <= target < SESSION_CLOSE_ET):
        errors.append(
            f"run_time_et {cfg.get('run_time_et')!r} can never be a regular-session "
            "time (09:30 <= run_time_et < 16:00 ET)"
        )
    notional = cfg.get("base_trade_notional_usd")
    if (
        not isinstance(notional, (int, float))
        or isinstance(notional, bool)
        or not math.isfinite(float(notional))
        or float(notional) <= 0
    ):
        errors.append("base_trade_notional_usd must be a positive finite number")
    analysts = cfg.get("analysts") or []
    if not analysts or any(a not in VALID_ANALYSTS for a in analysts):
        errors.append(f"analysts must be a non-empty subset of {list(VALID_ANALYSTS)}")
    if cfg.get("research_depth") not in VALID_RESEARCH_DEPTHS:
        errors.append(f"research_depth must be one of {list(VALID_RESEARCH_DEPTHS)}")
    if not (cfg.get("output_language") or "").strip():
        errors.append("output_language must be non-empty")
    for role in ("analysis", "decision", "screening"):
        provider = (cfg.get(f"{role}_provider") or "").strip()
        model = (cfg.get(f"{role}_model") or "").strip()
        if not provider or not model:
            errors.append(f"{role}_provider and {role}_model are required (no inheritance)")
        if not _valid_backend_url(cfg.get(f"{role}_backend_url")):
            errors.append(f"{role}_backend_url is not a valid http(s) URL")
        if provider.lower() in PROVIDERS_REQUIRING_URL and not (cfg.get(f"{role}_backend_url") or "").strip():
            errors.append(f"{role}_backend_url is required for provider {provider!r}")
    if runtime is not None:
        safety_error = _unattended_safety_error(runtime)
        if safety_error:
            errors.append(safety_error)
        try:
            from tradingagents.llm_clients.retry import validate_llm_max_retries

            validate_llm_max_retries(runtime.get("llm_max_retries", 3))
        except Exception as exc:
            errors.append(f"llm_max_retries invalid: {exc}")
        try:
            from tradingagents.llm_clients.roles import resolve_role_config

            resolved = resolve_role_config(runtime)
            if resolved.get("mode") != "roles":
                errors.append("role resolution did not enter roles mode")
        except Exception as exc:
            errors.append(f"role config invalid: {exc}")
        try:
            from tradingagents.screening.llm import resolve_screening_config

            resolved = resolve_screening_config(runtime)
            if not resolved.get("enabled"):
                errors.append("screening role did not resolve to enabled")
        except Exception as exc:
            errors.append(f"screening config invalid: {exc}")
        try:
            from tradingagents.dataflows.config import get_alpaca_use_paper
            from tradingagents.dataflows.alpaca_utils import alpaca_read_only_enabled

            flag = get_alpaca_use_paper()
            text = str(flag if flag is not None else "True").strip().lower()
            if text in ("false", "0", "no", "off", "live"):
                errors.append("ALPACA_USE_PAPER=False is not supported (paper-only)")
            if alpaca_read_only_enabled():
                errors.append(
                    "ALPACA_READ_ONLY must be False for an authorized trading observation"
                )
        except Exception as exc:
            errors.append(f"paper flag unreadable: {exc}")
    return errors


def parse_run_time_et(
    value: str,
    *,
    datetime: Any,
) -> dtime:
    text = (value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise ValueError(f"run_time_et {value!r} must be HH:MM (24h)")
    return parsed


def build_runtime_config(
    long_cfg: Dict[str, Any], base: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Full graph/execution config for a round: Phase D forces full-system mode."""
    if base is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        base = DEFAULT_CONFIG
    runtime = dict(base)
    depth = int(long_cfg.get("research_depth") or 3)
    runtime["max_debate_rounds"] = depth
    runtime["max_risk_discuss_rounds"] = depth
    runtime["output_language"] = long_cfg.get("output_language") or "English"
    # Base provider keys mirror the Analysis role so legacy construction paths
    # keep working; the explicit role keys switch resolution into roles mode.
    runtime["llm_provider"] = long_cfg.get("analysis_provider")
    runtime["backend_url"] = long_cfg.get("analysis_backend_url")
    runtime["deep_think_llm"] = long_cfg.get("analysis_model")
    runtime["quick_think_llm"] = long_cfg.get("analysis_model")
    for key in (
        "analysis_provider", "analysis_model", "analysis_backend_url",
        "analysis_fallback_provider", "analysis_fallback_model",
        "analysis_fallback_backend_url",
        "decision_provider", "decision_model", "decision_backend_url",
        "screening_provider", "screening_model", "screening_backend_url",
        "allow_shorts",
    ):
        runtime[key] = long_cfg.get(key)
    runtime["auto_screening_enabled"] = True
    # Short exposure is now an explicit per-observation opt-in (paper only;
    # the broker-side deterministic guards in execution.service still apply).
    runtime["allow_shorts"] = bool(long_cfg.get("allow_shorts", False))
    runtime["trading_mode"] = "trading" if runtime["allow_shorts"] else "investment"
    # Analysis topology: the graph resolver reads analysis_backend; the
    # profile key records that both backends run the Traders five-analyst
    # topology in this runner.
    backend = str(long_cfg.get("analysis_backend") or "traders").strip().lower()
    if backend not in VALID_ANALYSIS_BACKENDS:
        allowed = ", ".join(sorted(VALID_ANALYSIS_BACKENDS))
        raise ValueError(f"analysis_backend must be one of: {allowed}")
    runtime["analysis_backend"] = backend
    runtime["analysis_profile"] = "traders"
    return apply_single_backend_runtime_paths(runtime, backend)


def single_backend_root(backend: str) -> Path:
    """Isolated artifact root for a non-Traders single long-run backend."""
    from tradingagents.app_identity import app_home, validate_app_path

    return validate_app_path(app_home() / "single" / str(backend), field="results_dir")


def apply_single_backend_runtime_paths(
    runtime: Dict[str, Any], backend: str
) -> Dict[str, Any]:
    """Give a non-Traders single backend its own execution DB, results and
    cache paths. Traders keeps the existing shared defaults unchanged.

    Only runtime path keys this application actually consumes are remapped;
    the Phase-D lifecycle state directory (active.json, journals, manifests)
    is deliberately shared so one active observation stays one active
    observation regardless of backend.
    """
    if backend != "berkshire":
        return runtime
    from tradingagents.app_identity import validate_app_path

    root = single_backend_root(backend)
    runtime = dict(runtime)
    runtime["results_dir"] = str(
        validate_app_path(root / "results", field="results_dir")
    )
    runtime["data_cache_dir"] = str(
        validate_app_path(root / "data_cache", field="data_cache_dir")
    )
    runtime["execution_db_path"] = str(
        validate_app_path(root / "execution" / "execution.sqlite3", field="execution_db")
    )
    runtime["recovery_ledger_db_path"] = str(
        validate_app_path(
            root / "execution" / "recovery_ledger.sqlite3", field="execution_db"
        )
    )
    # Memory, safety and screening state are per-backend too: a Berkshire
    # observation must never read or write the Traders memory log, agent
    # memory store, safety state/kill switch, or screening selection cache.
    # No copy/symlink/fallback: each backend starts with its own empty state.
    runtime["memory_log_path"] = str(
        validate_app_path(root / "memory" / "trading_memory.md", field="memory_log_path")
    )
    runtime["agent_memory_dir"] = str(
        validate_app_path(root / "memory" / "agent_memory", field="agent_memory_dir")
    )
    runtime["safety_state_path"] = str(
        validate_app_path(root / "safety" / "state.json", field="safety_state_path")
    )
    runtime["safety_kill_switch_path"] = str(
        validate_app_path(
            root / "safety" / "KILL_SWITCH", field="safety_kill_switch_path"
        )
    )
    runtime["screening_selection_cache_path"] = str(
        validate_app_path(
            root / "screening_selection.json", field="screening_selection_cache_path"
        )
    )
    return runtime


def apply_ab_backend_runtime_paths(
    runtime: Dict[str, Any], backend: str, root: str | Path
) -> Dict[str, Any]:
    """Build one isolated A/B execution namespace around a shared selection.

    The daily selection cache and frozen evidence directory are deliberately
    shared. Everything that can learn, execute, recover, or hold a kill switch
    is stored under this arm's own directory.
    """
    from tradingagents.app_identity import validate_app_path

    backend = str(backend).strip().lower()
    if backend not in {"traders", "berkshire"}:
        raise ValueError("A/B backend must be traders or berkshire")
    root_path = validate_app_path(root, field="results_dir")
    arm = validate_app_path(root_path / "arms" / backend, field="results_dir")
    shared = validate_app_path(root_path / "shared", field="results_dir")
    runtime = dict(runtime)
    runtime.update({
        "analysis_backend": backend,
        "analysis_profile": "traders",
        "analysis_input_mode": "frozen_evidence",
        "shared_evidence_dir": str(
            validate_app_path(shared / "evidence", field="results_dir")
        ),
        "_alpaca_account_profile": "A" if backend == "traders" else "B",
        "auto_trade": True,
        "checkpoint_enabled": False,
        "memory_retrieval_enabled": True,
        "memory_maintenance_enabled": True,
        "results_dir": str(validate_app_path(arm / "results", field="results_dir")),
        "data_cache_dir": str(validate_app_path(arm / "data_cache", field="data_cache_dir")),
        "execution_db_path": str(
            validate_app_path(arm / "execution" / "execution.sqlite3", field="execution_db")
        ),
        "recovery_ledger_db_path": str(
            validate_app_path(
                arm / "execution" / "recovery_ledger.sqlite3", field="execution_db"
            )
        ),
        "memory_log_path": str(
            validate_app_path(arm / "memory" / "trading_memory.md", field="memory_log_path")
        ),
        "agent_memory_dir": str(
            validate_app_path(arm / "memory" / "agent_memory", field="agent_memory_dir")
        ),
        "safety_state_path": str(
            validate_app_path(arm / "safety" / "state.json", field="safety_state_path")
        ),
        "safety_kill_switch_path": str(
            validate_app_path(arm / "safety" / "KILL_SWITCH", field="safety_kill_switch_path")
        ),
        # Screening is one shared authoritative daily artifact. Keeping this
        # exact path in both runtime configs also preserves the existing
        # execution entry gate's safety semantics.
        "screening_selection_cache_path": str(
            validate_app_path(shared / "screening_selection_cache.json", field="screening_selection_cache_path")
        ),
    })
    return runtime


def git_baseline_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        commit = (out.stdout or "").strip()
        return commit if commit else "unknown"
    except Exception:
        return "unknown"


def _apply_runtime_config(
    runtime: Dict[str, Any],
    *,
    LongRunStop: type[RuntimeError],
) -> None:
    """R01: install this run's runtime as the global execution config BEFORE
    any path that could mutate the broker runs.

    The execution gate (screening Top20 entry check) reads the global config
    via get_config(), while long-run callers hold their own runtime dict.
    Until set_config() is applied, recovery may see runtime
    auto_screening_enabled=True against a global auto_screening_enabled=False
    and misclassify a gated PENDING opening as a manual-mode recovery. The
    merge keeps every key the run did not explicitly override.
    """
    try:
        from tradingagents.dataflows.config import get_config, set_config

        merged = dict(get_config() or {})
        merged.update(runtime or {})
        set_config(merged)
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED",
            f"could not apply runtime config before recovery: {exc}",
        )



def _validate_long_run_execution_config(
    runtime: Dict[str, Any],
    *,
    LongRunStop: type[RuntimeError],
    append_jsonl: Callable[[Path, Dict[str, Any]], None],
    base_dir: Callable[[], Path],
    utc_now_iso: Callable[[], str],
    _unattended_safety_error: Callable[[Dict[str, Any]], str],
) -> None:
    """Confirm the applied global config still proves unattended invariants.

    Runs after _apply_runtime_config (and after any later merge) and refuses
    to continue when the effective config turned off auto screening, paper
    mode or the safety layer for an unattended long run.
    """
    try:
        from tradingagents.dataflows.config import get_config
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED", f"global execution config unreadable: {exc}"
        )
    effective = get_config() or {}
    _validate_unattended_limits(effective, LongRunStop=LongRunStop)
    raw_budget = effective.get("daily_llm_token_budget", 0)
    if (
        isinstance(raw_budget, bool)
        or not isinstance(raw_budget, (int, float))
        or not math.isfinite(float(raw_budget))
    ):
        raise LongRunStop(
            "LLM_BUDGET_INVALID",
            "daily_llm_token_budget must be a finite numeric value",
        )
    if raw_budget < 0:
        raise LongRunStop(
            "LLM_BUDGET_INVALID",
            "daily_llm_token_budget must be >= 0",
        )
    if raw_budget > 20_000_000:
        raise LongRunStop(
            "LLM_BUDGET_TOO_HIGH",
            "unattended long-run daily_llm_token_budget must be <= 20000000",
        )
    if raw_budget == 0:
        # B-02: 0 stays "unlimited" in general mode, but an unattended
        # long run must never run uncapped. Normalize the effective budget
        # to the fixed 20M/day hard cap and persist it, so check_llm_budget()
        # and any lazily built SafetyGuard see the cap instead of 0.
        try:
            from tradingagents.dataflows.config import set_config
            from tradingagents.safety import reset_safety_guard

            merged = dict(get_config() or {})
            merged["daily_llm_token_budget"] = 20_000_000
            set_config(merged)
            reset_safety_guard()
            append_jsonl(
                base_dir() / "events.jsonl",
                {
                    "at": utc_now_iso(),
                    "type": "llm_budget_normalized",
                    "detail": {
                        "original": raw_budget,
                        "normalized": 20_000_000,
                        "scope": "unattended_long_run",
                    },
                },
            )
        except Exception as exc:
            raise LongRunStop(
                "CONFIG_APPLY_FAILED",
                f"could not persist normalized LLM token budget: {exc}",
            )
    if not effective.get("auto_screening_enabled"):
        raise LongRunStop(
            "SAFETY_DISABLED",
            "unattended long-run requires auto_screening_enabled=True after "
            "runtime config application",
        )
    error = _unattended_safety_error(effective)
    if error:
        raise LongRunStop("SAFETY_DISABLED", error)
    try:
        from tradingagents.dataflows.config import get_alpaca_use_paper
        from tradingagents.dataflows.alpaca_utils import alpaca_read_only_enabled

        flag = get_alpaca_use_paper()
        text = str(flag if flag is not None else "True").strip().lower()
        if text in ("false", "0", "no", "off", "live"):
            raise LongRunStop(
                "SAFETY_DISABLED",
                "unattended long-run requires paper mode "
                "(ALPACA_USE_PAPER=False is not supported)",
            )
        if alpaca_read_only_enabled():
            raise LongRunStop(
                "SAFETY_DISABLED",
                "authorized trading requires ALPACA_READ_ONLY=False; "
                "the Paper-only endpoint and paper=True enforcement remain active",
            )
    except LongRunStop:
        raise
    except Exception as exc:
        raise LongRunStop(
            "CONFIG_APPLY_FAILED", f"paper flag unreadable after apply: {exc}"
        )
