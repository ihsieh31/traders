#!/usr/bin/env python3
"""Run one due session of a resumable Traders versus Berkshire A/B campaign.

This module is intentionally only a thin campaign coordinator.  It freezes an
authoritative NYSE session list, calls the existing single-pair runner once per
invocation, and writes a broker-authoritative final equity comparison after all
sessions complete.  It is not a daemon: invoke it from the existing daily
scheduler/cron while the market is open, then use ``--resume`` on later days.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_analysis_ab import (
    BACKEND_ACCOUNT,
    BACKEND_ORDER,
    FORMAL_ANALYSTS,
    _campaign_fingerprint,
    _implementation_fingerprint,
    _load_config,
    _paper_account_preflight,
    build_ab_configs,
    resolved_llm_route_snapshots,
    run_analysis_ab as run_single_pair,
)
from scripts.summarize_analysis_ab import summarize
from tradingagents.app_identity import default_results_dir, validate_app_path
from tradingagents.long_run_support.sessions import fetch_session_dates
from tradingagents.long_run_support.state import atomic_write_json


CAMPAIGN_SCHEMA_VERSION = 1
CAMPAIGN_STATUSES = frozenset({"NEW", "RUNNING", "STOPPED", "COMPLETED"})
_EASTERN = ZoneInfo("America/New_York")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _eastern_date(now: datetime | None = None) -> date:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("campaign now must be timezone-aware")
    return instant.astimezone(_EASTERN).date()


def _campaign_state_path(root: Path) -> Path:
    return root / "campaign_state.json"


@contextmanager
def _campaign_lock(root: Path):
    """Serialize the complete campaign state read/run/write lifecycle."""

    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".campaign.lock").open("a+", encoding="utf-8")
    lock_api = None
    try:
        try:
            import fcntl as lock_api
        except ImportError as exc:  # pragma: no cover - production is Linux/macOS
            raise RuntimeError("A/B campaign requires advisory file locking") from exc
        lock_api.flock(handle.fileno(), lock_api.LOCK_EX)
        yield
    finally:
        try:
            if lock_api is not None:
                lock_api.flock(handle.fileno(), lock_api.LOCK_UN)
        finally:
            handle.close()


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"campaign state is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("campaign state must be a JSON object")
    _validate_state(value)
    return value


def _validate_state(state: Mapping[str, Any]) -> None:
    if state.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
        raise RuntimeError("campaign state has an unsupported schema")
    if not isinstance(state.get("campaign_id"), str) or not state["campaign_id"]:
        raise RuntimeError("campaign state has no campaign_id")
    if state.get("status") not in CAMPAIGN_STATUSES:
        raise RuntimeError("campaign state has an invalid status")
    if not isinstance(state.get("symbol"), str) or not state["symbol"]:
        raise RuntimeError("campaign state has no symbol")
    try:
        date.fromisoformat(str(state.get("start_date")))
    except ValueError as exc:
        raise RuntimeError("campaign state has an invalid start_date") from exc
    target_days = state.get("target_days")
    if isinstance(target_days, bool) or not isinstance(target_days, int) or target_days <= 0:
        raise RuntimeError("campaign state has an invalid target_days")
    sessions = state.get("sessions")
    completed = state.get("completed_dates")
    if not isinstance(sessions, list) or len(sessions) != target_days:
        raise RuntimeError("campaign state has an invalid frozen session list")
    if not isinstance(completed, list) or len(set(completed)) != len(completed):
        raise RuntimeError("campaign state has invalid completed_dates")
    if any(not isinstance(day, str) for day in sessions + completed):
        raise RuntimeError("campaign state has non-string session dates")
    try:
        [date.fromisoformat(day) for day in sessions + completed]
    except ValueError as exc:
        raise RuntimeError("campaign state has invalid session date format") from exc
    if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
        raise RuntimeError("campaign state sessions are not ordered and unique")
    # A campaign can only advance a contiguous prefix.  This prevents a
    # hand-edited state from silently skipping an unfinished previous pair.
    if completed != sessions[: len(completed)]:
        raise RuntimeError("campaign state skips an unfinished prior session")
    current = state.get("current_date")
    if current is not None and current not in sessions:
        raise RuntimeError("campaign state current_date is not a campaign session")
    fingerprint = state.get("config_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise RuntimeError("campaign state has an invalid config fingerprint")
    routes = state.get("resolved_llm_routes")
    if not isinstance(routes, dict) or set(routes) != set(BACKEND_ORDER):
        raise RuntimeError("campaign state has invalid resolved LLM routes")
    if bool(state.get("execute_paper")):
        starting = state.get("starting_equity")
        ending = state.get("ending_equity")
        if starting is not None:
            _validate_equity_snapshot(starting, label="starting")
        if ending is not None:
            _validate_equity_snapshot(ending, label="ending")
        if completed and starting is None:
            raise RuntimeError("Paper campaign state completed sessions without starting equity")
        if state.get("status") == "COMPLETED":
            if starting is None or ending is None:
                raise RuntimeError("completed Paper campaign state has no ending equity")
            _assert_matching_account_refs(starting, ending)


def _validate_equity_snapshot(snapshot: Any, *, label: str) -> None:
    if not isinstance(snapshot, Mapping) or set(snapshot) != set(BACKEND_ORDER):
        raise RuntimeError(f"{label} equity snapshot must contain both A/B arms")
    refs = set()
    for backend in BACKEND_ORDER:
        item = snapshot[backend]
        if not isinstance(item, Mapping):
            raise RuntimeError(f"{label} equity snapshot is invalid for {backend}")
        if item.get("account") != BACKEND_ACCOUNT[backend]:
            raise RuntimeError(f"{label} equity snapshot account mapping is invalid for {backend}")
        reference = item.get("account_ref")
        if not isinstance(reference, str) or not reference:
            raise RuntimeError(f"{label} equity snapshot has no account_ref for {backend}")
        try:
            equity = float(item.get("equity"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label} equity snapshot has invalid equity for {backend}") from exc
        if not math.isfinite(equity) or equity <= 0:
            raise RuntimeError(f"{label} equity snapshot has unusable equity for {backend}")
        if not isinstance(item.get("captured_at"), str) or not item["captured_at"]:
            raise RuntimeError(f"{label} equity snapshot has no captured_at for {backend}")
        refs.add(reference)
    if len(refs) != len(BACKEND_ORDER):
        raise RuntimeError(f"{label} equity snapshot does not isolate A/B accounts")


def _assert_matching_account_refs(
    starting: Mapping[str, Mapping[str, Any]], ending: Mapping[str, Mapping[str, Any]]
) -> None:
    for backend in BACKEND_ORDER:
        if starting[backend]["account_ref"] != ending[backend]["account_ref"]:
            raise RuntimeError(f"{backend} Paper account identity changed during campaign")


def _calendar_client(*, execute_paper: bool, supplied: Any = None) -> Any:
    if supplied is not None:
        return supplied
    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

    # Account A is enough for an authenticated calendar read.  The actual
    # pair runner still preflights both dedicated Paper accounts before trade.
    return get_alpaca_trading_client(
        account="A" if execute_paper else None, read_only=True
    )


def _freeze_sessions(
    start_date: date,
    target_days: int,
    *,
    calendar_client: Any,
    session_fetcher: Callable[..., list[date]],
) -> list[str]:
    # The shared authoritative calendar adapter supplies the holiday and
    # early-close aware NYSE sessions.  The deliberately generous range is
    # only to obtain N future rows; no static holiday table is consulted.
    end_date = start_date + timedelta(days=max(90, target_days * 4 + 14))
    sessions = session_fetcher(start_date, end_date, calendar_client, date=date)
    selected = [day for day in sessions if day >= start_date][:target_days]
    if len(selected) != target_days:
        raise RuntimeError(
            f"authoritative calendar proved only {len(selected)} sessions; "
            f"{target_days} required"
        )
    return [day.isoformat() for day in selected]


def _campaign_conditions(
    *,
    base_config: Mapping[str, Any],
    root: Path,
    symbol: str,
    probe_date: str,
    execute_paper: bool,
    paper_notional_usd: float | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    configs = build_ab_configs(
        base_config,
        symbol=symbol,
        trade_date=probe_date,
        pair_dir=root / probe_date / symbol,
        experiment_root=root,
        execute_paper=execute_paper,
        paper_notional_usd=paper_notional_usd,
    )
    fingerprint = _campaign_fingerprint(
        configs["traders"], FORMAL_ANALYSTS, _implementation_fingerprint()
    )
    return fingerprint, resolved_llm_route_snapshots(configs)


def _broker_equity_snapshot() -> dict[str, dict[str, Any]]:
    """Read the two Paper accounts; broker equity is the sole P&L authority."""

    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

    snapshots: dict[str, dict[str, Any]] = {}
    account_ids: set[str] = set()
    for backend in BACKEND_ORDER:
        account = BACKEND_ACCOUNT[backend]
        raw = get_alpaca_trading_client(account=account, read_only=True).get_account()
        account_id = str(getattr(raw, "id", "") or "")
        try:
            equity = float(getattr(raw, "equity", None))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Paper account {account} returned invalid equity") from exc
        if not account_id or not math.isfinite(equity) or equity <= 0:
            raise RuntimeError(f"Paper account {account} has no usable identity/equity")
        if account_id in account_ids:
            raise RuntimeError("Traders and Berkshire resolve to the same Paper account")
        account_ids.add(account_id)
        snapshots[backend] = {
            "account": account,
            "account_ref": hashlib.sha256(account_id.encode()).hexdigest()[:16],
            "equity": equity,
            "captured_at": _utc_now(),
        }
    return snapshots


def _capture_initial_equity_from_preflight(trade_date: str) -> dict[str, dict[str, Any]]:
    """Pin the baseline only after the existing first-session Paper gate passes."""

    preflight = _paper_account_preflight(
        trade_date=trade_date, require_matched_flat_start=True
    )
    captured_at = _utc_now()
    return {
        backend: {
            "account": preflight[backend]["account"],
            "account_ref": preflight[backend]["account_ref"],
            "equity": float(preflight[backend]["equity"]),
            "captured_at": captured_at,
        }
        for backend in BACKEND_ORDER
    }


def _new_state(
    *,
    symbol: str,
    start_date: date,
    target_days: int,
    sessions: list[str],
    execute_paper: bool,
    paper_notional_usd: float | None,
    config_fingerprint: str,
    resolved_routes: Mapping[str, Mapping[str, Any]],
    starting_equity: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": f"ab-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
        "status": "NEW",
        "symbol": symbol,
        "start_date": start_date.isoformat(),
        "target_days": target_days,
        "sessions": sessions,
        "completed_dates": [],
        "current_date": None,
        "execute_paper": bool(execute_paper),
        "paper_notional_usd": paper_notional_usd,
        "config_fingerprint": config_fingerprint,
        "resolved_llm_routes": dict(resolved_routes),
        "starting_equity": dict(starting_equity) if starting_equity is not None else None,
        "ending_equity": None,
        "created_at": now,
        "updated_at": now,
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    atomic_write_json(path, state)


def _pair_completed(result: Mapping[str, Any]) -> bool:
    if result.get("status") == "COMPLETED":
        return True
    return all(
        isinstance(result.get(backend), Mapping)
        and result[backend].get("status") == "completed"
        for backend in BACKEND_ORDER
    )


def _performance(
    state: Mapping[str, Any], ending: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    starting = state["starting_equity"]
    _validate_equity_snapshot(starting, label="starting")
    _validate_equity_snapshot(ending, label="ending")
    _assert_matching_account_refs(starting, ending)
    for backend in BACKEND_ORDER:
        start = starting[backend]
        end = ending[backend]
        initial = float(start["equity"])
        final = float(end["equity"])
        pnl = final - initial
        output[backend] = {
            "account_ref": start["account_ref"],
            "starting_equity": initial,
            "ending_equity": final,
            "pnl": pnl,
            "return_pct": round((final / initial - 1.0) * 100.0, 8),
            "starting_captured_at": start["captured_at"],
            "ending_captured_at": end["captured_at"],
        }
    return output


def _render_summary(summary: Mapping[str, Any]) -> str:
    campaign = summary["campaign"]
    lines = [
        "# 30-Day Analysis A/B Result",
        "",
        f"- Campaign: {campaign['campaign_id']}",
        f"- Symbol: {campaign['symbol']}",
        f"- Start: {campaign['start_date']}",
        f"- End: {campaign['sessions'][-1]}",
        f"- Trading sessions: {len(campaign['sessions'])}",
        "",
    ]
    if summary.get("performance"):
        for backend, title in (("traders", "Traders"), ("berkshire", "Berkshire")):
            item = summary["performance"][backend]
            lines.extend([
                f"## {title}",
                "",
                f"- Starting equity: {item['starting_equity']:.2f}",
                f"- Ending equity: {item['ending_equity']:.2f}",
                f"- P&L: {item['pnl']:.2f}",
                f"- Return: {item['return_pct']:.2f}%",
                "",
            ])
    analysis = summary["analysis_statistics"]
    lines.extend([
        "## Analysis statistics",
        "",
        "| Arm | Completed | Failed | Invalid | LLM calls | Tokens | Latency (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for backend in BACKEND_ORDER:
        item = (analysis.get("backends") or {}).get(backend, {})
        lines.append(
            f"| {backend} | {item.get('completed', 0)} | {item.get('failed', 0)} | "
            f"{item.get('invalid', 0)} | {item.get('llm_calls', 0)} | "
            f"{item.get('total_tokens', 0)} | {item.get('latency_seconds', 0.0):.2f} |"
        )
    lines.extend([
        "",
        "## Execution statistics",
        "",
        "| Arm | Orders | Deduped | Holds | Failures |",
        "|---|---:|---:|---:|---:|",
    ])
    for backend in BACKEND_ORDER:
        item = (analysis.get("backends") or {}).get(backend, {})
        lines.append(
            f"| {backend} | {item.get('orders', 0)} | {item.get('deduped', 0)} | "
            f"{item.get('holds', 0)} | {item.get('failed', 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _unsettled_primary_orders(root: Path, symbol: str) -> list[dict[str, Any]]:
    """Read-only settlement check across both arms' durable execution DBs.

    A campaign can only be finalized when every campaign-owned primary order
    has reached a broker terminal state.  Protective children (stop-loss /
    take-profit / bracket legs) are broker-managed coverage and are allowed
    to stay live; they never block finalization.
    """

    from tradingagents.execution.store import ExecutionStore

    settled = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
    found: list[dict[str, Any]] = []
    for backend in BACKEND_ORDER:
        db_path = root / "_profiles" / backend / "execution.sqlite3"
        if not db_path.exists():
            continue
        store = ExecutionStore(db_path)
        for order in store.list_all_orders():
            if store.protective_parent(order["order_id"]) is not None:
                continue
            if str(order.get("symbol") or "").upper() != str(symbol).upper():
                continue
            if (order.get("status") or "").upper() not in settled:
                found.append(
                    {
                        "backend": backend,
                        "order_id": order["order_id"],
                        "client_order_id": order["client_order_id"],
                        "status": order["status"],
                    }
                )
    return found


def _refresh_broker_settlement(root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh durable order state from the broker before the settlement check.

    Recovery/lookup/adopt only: it reuses the existing
    ``ExecutionService.startup_recover`` entry point per backend, with that
    backend's dedicated Paper account credentials and its own execution DB
    (traders -> Account A, berkshire -> Account B; never a shared service, a
    default account, or crossed DBs).  It never re-analyzes, never regenerates
    a signal, never creates a new decision_id, and never opens new exposure
    beyond the recovery rules that already own durable PENDING orders.
    Idempotent: a crash mid-refresh is handled by simply running it again on
    the next resume; lookup/adopt cannot duplicate POSTs.
    """

    from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client
    from tradingagents.execution.service import ExecutionService

    results: dict[str, Any] = {}
    for backend in BACKEND_ORDER:
        db_path = root / "_profiles" / backend / "execution.sqlite3"
        if not db_path.exists():
            continue
        service = ExecutionService(
            db_path=db_path,
            broker_factory=(
                lambda account=BACKEND_ACCOUNT[backend]: get_alpaca_trading_client(
                    account=account, read_only=False
                )
            ),
        )
        results[backend] = service.startup_recover()
    return results


def _expected_pair_dirs(root: Path, state: Mapping[str, Any]) -> list[Path]:
    """The frozen campaign sessions+symbol fully determine its pair paths."""

    from tradingagents.dataflows.utils import safe_ticker_component

    pair_symbol = safe_ticker_component(str(state["symbol"]))
    return [root / session / pair_symbol for session in state["sessions"]]


def _assert_pair_integrity(
    root: Path, state: Mapping[str, Any], expected_dirs: Sequence[Path]
) -> None:
    """Fail closed unless every frozen session has its own COMPLETED pair."""

    symbol = str(state["symbol"]).upper()
    for pair_dir in expected_dirs:
        state_path = pair_dir / "pair_state.json"
        if not state_path.exists():
            raise RuntimeError(
                f"campaign finalization requires a completed pair state: {state_path}"
            )
        try:
            pair = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"pair_state.json is unreadable: {state_path}") from exc
        if not isinstance(pair, dict) or pair.get("status") != "COMPLETED":
            raise RuntimeError(
                f"campaign finalization requires pair status COMPLETED: {state_path}"
            )
        if str(pair.get("symbol") or "").upper() != symbol:
            raise RuntimeError(
                f"campaign finalization pair symbol mismatch: {state_path}"
            )
        if str(pair.get("trade_date") or "") != state_path.parent.parent.name:
            raise RuntimeError(
                f"campaign finalization pair date mismatch: {state_path}"
            )


def finalize_campaign(
    root: Path,
    state: Mapping[str, Any],
    *,
    summarize_fn: Callable[[str | Path], dict[str, Any]] = summarize,
) -> dict[str, Any]:
    """Idempotently produce final reports; never runs another pair."""

    expected_dirs = _expected_pair_dirs(root, state)
    _assert_pair_integrity(root, state, expected_dirs)
    # Only the campaign's own frozen session+symbol pairs may contribute to
    # the final report; unrelated pairs under the root are never counted.
    if summarize_fn is summarize:
        analysis = summarize_fn(root, expected_pair_dirs=expected_dirs)
    else:
        analysis = summarize_fn(root)
    pair_count = analysis.get("pair_count")
    if pair_count is not None and int(pair_count) != len(state["sessions"]):
        raise RuntimeError(
            "campaign final report requires exactly "
            f"{len(state['sessions'])} pairs; summarizer observed {pair_count}"
        )
    performance = (
        _performance(state, state["ending_equity"]) if state.get("execute_paper") else None
    )
    summary = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "campaign": {
            key: state[key]
            for key in ("campaign_id", "symbol", "start_date", "target_days", "sessions", "completed_dates")
        },
        "performance": performance,
        "analysis_statistics": analysis,
    }
    atomic_write_json(root / "campaign_summary.json", summary)
    # Markdown is derived output, not resume authority.  A crash here is safe:
    # a later invocation sees COMPLETED and regenerates it from the durable
    # state and broker account snapshots without rerunning a pair.
    (root / "campaign_summary.md").write_text(_render_summary(summary), encoding="utf-8")
    return summary


def _has_existing_ab_artifacts(root: Path) -> bool:
    return (
        (root / "AB_CAMPAIGN.json").exists()
        or any(root.glob("*/*/pair_state.json"))
        or any(root.glob("*/*/pair_summary.json"))
    )


def _complete_campaign(
    root: Path,
    state_path: Path,
    state: dict[str, Any],
    *,
    broker_snapshotter: Callable[[], dict[str, dict[str, Any]]],
    summarize_fn: Callable[[str | Path], dict[str, Any]],
    settlement_refresh_fn: Callable[[Path, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Durably pin ending equity before rendering derived report files."""

    if state.get("status") != "COMPLETED":
        if state.get("execute_paper"):
            # The local DB may lag the broker (e.g. local ACCEPTED while the
            # broker already FILLED).  Run the existing recovery/reconciliation
            # for both arms first so the settlement check below reads fresh
            # durable state; only a still-unsettled check blocks completion.
            # _refresh_broker_settlement is the production default, so a plain
            # --resume (CLI or auto launcher) really refreshes; tests inject a
            # stub.  It only runs when something looks unsettled.
            refresh = settlement_refresh_fn or _refresh_broker_settlement
            if _unsettled_primary_orders(root, str(state["symbol"])):
                refresh(root, state)
        unsettled = _unsettled_primary_orders(root, str(state["symbol"]))
        if unsettled:
            state["status"] = "RUNNING"
            state["current_date"] = None
            _save_state(state_path, state)
            return {
                "outcome": "awaiting_final_settlement",
                "state": state,
                "unsettled_orders": unsettled,
            }
    if state.get("execute_paper"):
        if state.get("starting_equity") is None:
            raise RuntimeError("Paper campaign cannot complete without starting equity")
        if state.get("ending_equity") is None:
            ending = broker_snapshotter()
            _validate_equity_snapshot(ending, label="ending")
            _assert_matching_account_refs(state["starting_equity"], ending)
            state["ending_equity"] = ending
        _assert_matching_account_refs(state["starting_equity"], state["ending_equity"])
        if state.get("status") != "COMPLETED":
            state["status"] = "COMPLETED"
            state["current_date"] = None
        # Campaign state is the authority.  Later resume reads this snapshot
        # and never refreshes it from a potentially changed broker account.
        _save_state(state_path, state)
    elif state.get("status") != "COMPLETED":
        state["status"] = "COMPLETED"
        state["current_date"] = None
        _save_state(state_path, state)
    summary = finalize_campaign(root, state, summarize_fn=summarize_fn)
    return {"outcome": "completed", "state": state, "summary": summary}


def _run_campaign_locked(
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    days: int = 30,
    results_root: str | Path | None = None,
    resume: str | Path | None = None,
    base_config: Mapping[str, Any] | None = None,
    execute_paper: bool | None = None,
    paper_notional_usd: float | None = None,
    debug: bool = False,
    now: datetime | None = None,
    calendar_client: Any = None,
    session_fetcher: Callable[..., list[date]] = fetch_session_dates,
    pair_runner: Callable[..., dict[str, Any]] = run_single_pair,
    broker_snapshotter: Callable[[], dict[str, dict[str, Any]]] = _broker_equity_snapshot,
    summarize_fn: Callable[[str | Path], dict[str, Any]] = summarize,
    settlement_refresh_fn: Callable[[Path, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Create/resume a campaign and run at most its earliest due pair."""

    if resume is not None:
        root = validate_app_path(resume, field="campaign_path")
    else:
        root = validate_app_path(results_root or (default_results_dir() / "ab"), field="results_dir")
    state_path = _campaign_state_path(root)
    state = _read_state(state_path)
    if resume is None:
        if state is not None:
            raise RuntimeError(
                "campaign already exists at this results root; use --resume explicitly"
            )
        if _has_existing_ab_artifacts(root):
            raise RuntimeError(
                "results root contains existing A/B artifacts; use a dedicated new root"
            )
    elif state is None:
        raise RuntimeError("no campaign exists at --resume root")
    config = deepcopy(dict(base_config or _load_config(None)))

    if state is None:
        if not symbol or not start_date:
            raise ValueError("--symbol and --start-date are required to create a campaign")
        try:
            requested_start = date.fromisoformat(start_date)
        except ValueError as exc:
            raise ValueError("start_date must be ISO YYYY-MM-DD") from exc
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days must be a positive integer")
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        create_execute_paper = bool(execute_paper)
        if create_execute_paper:
            try:
                paper_notional_usd = float(paper_notional_usd)
            except (TypeError, ValueError) as exc:
                raise ValueError("paper_notional_usd is required for Paper execution") from exc
            if not math.isfinite(paper_notional_usd) or paper_notional_usd <= 0:
                raise ValueError("paper_notional_usd must be positive and finite")
        elif paper_notional_usd is not None:
            raise ValueError("paper_notional_usd requires execute_paper=True")
        client = _calendar_client(execute_paper=create_execute_paper, supplied=calendar_client)
        sessions = _freeze_sessions(
            requested_start, days, calendar_client=client, session_fetcher=session_fetcher
        )
        fingerprint, routes = _campaign_conditions(
            base_config=config,
            root=root,
            symbol=symbol,
            probe_date=sessions[0],
            execute_paper=create_execute_paper,
            paper_notional_usd=paper_notional_usd,
        )
        state = _new_state(
            symbol=symbol,
            start_date=requested_start,
            target_days=days,
            sessions=sessions,
            execute_paper=create_execute_paper,
            paper_notional_usd=paper_notional_usd,
            config_fingerprint=fingerprint,
            resolved_routes=routes,
            starting_equity=None,
        )
        _save_state(state_path, state)
    else:
        # Resume is deliberately strict: external configuration and all
        # non-secret resolved routes must still match the first invocation.
        fingerprint, routes = _campaign_conditions(
            base_config=config,
            root=root,
            symbol=state["symbol"],
            probe_date=state["sessions"][0],
            execute_paper=bool(state["execute_paper"]),
            paper_notional_usd=state.get("paper_notional_usd"),
        )
        if fingerprint != state["config_fingerprint"] or routes != state["resolved_llm_routes"]:
            raise RuntimeError("campaign configuration or resolved LLM route changed; refusing resume")
        if execute_paper is not None and bool(execute_paper) != bool(state["execute_paper"]):
            raise RuntimeError("campaign execute/analysis mode changed; refusing resume")
        if paper_notional_usd is not None and paper_notional_usd != state.get("paper_notional_usd"):
            raise RuntimeError("campaign paper_notional_usd changed; refusing resume")

    if len(state["completed_dates"]) == state["target_days"]:
        return _complete_campaign(
            root, state_path, state,
            broker_snapshotter=broker_snapshotter, summarize_fn=summarize_fn,
            settlement_refresh_fn=settlement_refresh_fn,
        )

    next_date = state["sessions"][len(state["completed_dates"])]
    today = _eastern_date(now)
    if date.fromisoformat(next_date) > today:
        state["status"] = "RUNNING"
        state["current_date"] = None
        _save_state(state_path, state)
        return {"outcome": "not_due", "state": state, "next_date": next_date}
    if date.fromisoformat(next_date) < today:
        state["status"] = "STOPPED"
        state["current_date"] = next_date
        _save_state(state_path, state)
        return {"outcome": "previous_session_unfinished", "state": state, "next_date": next_date}

    state["status"] = "RUNNING"
    state["current_date"] = next_date
    _save_state(state_path, state)
    if state.get("execute_paper") and state.get("starting_equity") is None:
        # The existing pair runner's initial preflight proves open market,
        # distinct/flat A/B accounts and matched starting equity.  Persist its
        # result before a pair can submit an order; retry only after failure.
        try:
            starting = _capture_initial_equity_from_preflight(next_date)
        except RuntimeError as exc:
            if "market is closed" in str(exc).lower():
                return {"outcome": "market_closed", "state": state, "next_date": next_date}
            raise
        _validate_equity_snapshot(starting, label="starting")
        state["starting_equity"] = starting
        _save_state(state_path, state)
    try:
        result = pair_runner(
            symbol=state["symbol"],
            trade_date=next_date,
            base_config=config,
            results_root=root,
            selected_analysts=FORMAL_ANALYSTS,
            debug=debug,
            execute_paper=bool(state["execute_paper"]),
            paper_notional_usd=state.get("paper_notional_usd"),
        )
    except RuntimeError as exc:
        # The underlying Paper runner remains the actual market gate.  A
        # closed market is not a completed day and is safe to retry later.
        if "market is closed" in str(exc).lower():
            return {"outcome": "market_closed", "state": state, "next_date": next_date}
        state["status"] = "STOPPED"
        _save_state(state_path, state)
        raise

    if not _pair_completed(result):
        state["status"] = "STOPPED"
        _save_state(state_path, state)
        return {"outcome": "pair_unfinished", "state": state, "pair": result}

    state["completed_dates"].append(next_date)
    state["current_date"] = None
    _save_state(state_path, state)
    if len(state["completed_dates"]) != state["target_days"]:
        return {"outcome": "session_completed", "state": state, "pair": result}

    return _complete_campaign(
        root, state_path, state,
        broker_snapshotter=broker_snapshotter, summarize_fn=summarize_fn,
        settlement_refresh_fn=settlement_refresh_fn,
    )


def run_campaign(
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    days: int = 30,
    results_root: str | Path | None = None,
    resume: str | Path | None = None,
    base_config: Mapping[str, Any] | None = None,
    execute_paper: bool | None = None,
    paper_notional_usd: float | None = None,
    debug: bool = False,
    now: datetime | None = None,
    calendar_client: Any = None,
    session_fetcher: Callable[..., list[date]] = fetch_session_dates,
    pair_runner: Callable[..., dict[str, Any]] = run_single_pair,
    broker_snapshotter: Callable[[], dict[str, dict[str, Any]]] = _broker_equity_snapshot,
    summarize_fn: Callable[[str | Path], dict[str, Any]] = summarize,
    settlement_refresh_fn: Callable[[Path, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Serialize the entire campaign lifecycle before touching its state."""

    root = validate_app_path(
        resume if resume is not None else results_root or (default_results_dir() / "ab"),
        field="campaign_path" if resume is not None else "results_dir",
    )
    # Lock ordering is always campaign lock -> the pair runner's .ab.lock.
    with _campaign_lock(root):
        return _run_campaign_locked(
            symbol=symbol,
            start_date=start_date,
            days=days,
            results_root=results_root,
            resume=resume,
            base_config=base_config,
            execute_paper=execute_paper,
            paper_notional_usd=paper_notional_usd,
            debug=debug,
            now=now,
            calendar_client=calendar_client,
            session_fetcher=session_fetcher,
            pair_runner=pair_runner,
            broker_snapshotter=broker_snapshotter,
            summarize_fn=summarize_fn,
            settlement_refresh_fn=settlement_refresh_fn,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--start-date")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--results-root", default=str(default_results_dir() / "ab"))
    parser.add_argument("--resume", help="Campaign root containing campaign_state.json")
    parser.add_argument("--config-json", help="Optional JSON object merged over DEFAULT_CONFIG")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--execute", "--execute-paper", dest="execute_paper", action="store_true", default=None
    )
    parser.add_argument("--paper-notional-usd", type=float)
    args = parser.parse_args(argv)
    try:
        result = run_campaign(
            symbol=args.symbol,
            start_date=args.start_date,
            days=args.days,
            results_root=args.results_root,
            resume=args.resume,
            base_config=_load_config(args.config_json),
            execute_paper=args.execute_paper,
            paper_notional_usd=args.paper_notional_usd,
            debug=args.debug,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"[AB campaign] ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"[AB campaign] {result['outcome']}")
    return 1 if result["outcome"] in {"pair_unfinished", "previous_session_unfinished"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
