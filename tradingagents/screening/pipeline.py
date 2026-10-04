"""Daily production screening, validated caching and account-specific analysis plans.

Long-only auto mode uses the report's deterministic gates and survivor ranking;
legacy mode retains its price lanes and Screening LLM (also used by auto shorts).
All modes reserve holdings within the 20-stock analysis limit, including symbols
seen by the legacy Screening LLM. Non-trading days only review holdings.
Refresh invalidates the old cache first, and failures stop downstream work.
External dependencies are injectable for offline tests.
"""

from __future__ import annotations

from tradingagents.redaction import sanitize_for_log

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import math
from typing import Any, Callable, Dict, List, Optional

from tradingagents.dataflows.alpaca_utils import (
    fetch_with_bounded_retry,
    get_alpaca_trading_client,
)
from tradingagents.llm_clients.retry import ProviderFailure
from tradingagents.screening.gate import check_entry_allowed  # noqa: F401  (re-export)
from tradingagents.screening.llm import (
    ScreeningConfigError,
    ScreeningStop,
    ScreenedCandidate,
    build_screening_llm,
    describe_screening_role,
    resolve_screening_config,
)
from tradingagents.screening.metrics import (
    EligibilityThresholds,
    ScanStats,
    fetch_daily_bars_batch,
    resolve_as_of,
    scan_universe,
    select_research_candidates,
    score_exclusion_candidates,
    select_exclusion_candidates,
    FORMULA_VERSION,
)
from .policy import ExclusionThresholds, screening_method
from .quality import identity_issues, build_quality_report
from tradingagents.screening.prompt import build_sector_plan, run_screening_invocation
from tradingagents.screening.selection_store import (
    SCHEMA_VERSION,
    SCREENING_DATA_FEED,
    SelectionStore,
    _integrity_digest,
    default_selection_cache_path,
    eastern_timestamp,
    selection_rows_valid,
)
from tradingagents.screening.sessions import (
    current_trading_date_production,
    eastern_now,
    is_us_trading_day_production,
)
from tradingagents.screening.universe import enum_value, fetch_us_equity_universe, normalize_symbol


@dataclass
class RoundPlan:
    """What one scheduler round may do; ``status != "ok"`` halts everything."""

    status: str = "ok"  # "ok" | "stopped"
    reason: Optional[str] = None
    detail: str = ""
    mode: str = "auto"  # "auto" (Top20 ∪ holdings) | "held_review" (no entries)
    selection: Optional[dict] = None
    selection_date: Optional[str] = None
    as_of: Optional[str] = None
    cached: bool = False
    entry_allowed: bool = False
    deep_analysis_set: List[str] = field(default_factory=list)
    top20: List[dict] = field(default_factory=list)
    overlap_holdings: List[str] = field(default_factory=list)
    extra_holdings: List[str] = field(default_factory=list)
    blocked_holdings: List[dict] = field(default_factory=list)
    other_asset_holdings: List[dict] = field(default_factory=list)
    scan_stats: Optional[dict] = None
    screening_description: str = ""
    selection_hash: Optional[str] = None
    deferred_candidates: List[str] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return self.status != "ok"

    def stop_reason_text(self) -> str:
        if not self.stopped:
            return ""
        return f"{self.reason}: {self.detail}" if self.detail else str(self.reason)


@dataclass
class ScreeningDeps:
    """Injectable external boundaries (defaults = real Alpaca + real LLM)."""

    universe_fn: Optional[Callable[[Any], List[dict]]] = None
    bars_fn: Optional[Callable[..., Dict[str, Any]]] = None
    positions_fn: Optional[Callable[[], List[dict]]] = None
    asset_fn: Optional[Callable[[str], Any]] = None
    quarantine_fn: Optional[Callable[[Dict[str, Any]], Callable[[str], Optional[str]]]] = None
    screening_invoke_fn: Optional[Callable[..., List[Any]]] = None
    # Authoritative calendar injection (tests/fakes). When None, the real
    # Alpaca Trading Calendar API is used via config keys below.
    calendar_client: Optional[Any] = None
    calendar_rows: Optional[List[Any]] = None

    @classmethod
    def real(cls) -> "ScreeningDeps":
        return cls()


def _default_positions() -> List[dict]:
    client = get_alpaca_trading_client()
    positions = []
    for position in fetch_with_bounded_retry(client.get_all_positions):
        qty = float(getattr(position, "qty", 0) or 0)
        if qty == 0:
            continue
        positions.append(
            {
                "symbol": normalize_symbol(getattr(position, "symbol", "")),
                "qty": qty,
                "asset_class": enum_value(getattr(position, "asset_class", "")),
            }
        )
    return positions


def _default_asset(symbol: str):
    client = get_alpaca_trading_client()
    return fetch_with_bounded_retry(lambda: client.get_asset(symbol))


def _default_quarantine(config: Dict[str, Any]) -> Callable[[str], Optional[str]]:
    from tradingagents.risk.corporate_actions import build_quarantine_gate

    gate = build_quarantine_gate(config)
    if gate is None:
        raise ScreeningStop(
            "QUARANTINE_STATE_UNAVAILABLE",
            "corporate-action quarantine state could not be built; "
            "eligibility cannot be proven",
        )

    def checker(symbol: str) -> Optional[str]:
        return gate.check(symbol)

    return checker


def _default_screening_invoke(config: Dict[str, Any], resolved: Dict[str, Any]):
    llm = build_screening_llm(resolved, config)

    def invoke(candidates, sector_plan, *, select_n, max_per_sector):
        return run_screening_invocation(
            llm,
            candidates,
            sector_plan,
            select_n=select_n,
            max_per_sector=max_per_sector,
        )

    return invoke


def _stopped(
    reason: str, detail: str = "", *, scan_stats: Optional[dict] = None
) -> RoundPlan:
    return RoundPlan(status="stopped", reason=reason, detail=detail, scan_stats=scan_stats)


def _resolve_calendar(
    config: Dict[str, Any], deps: ScreeningDeps
) -> tuple[Any, Optional[List[Any]]]:
    """Authoritative calendar injection: explicit deps win, else config keys."""
    client = deps.calendar_client
    if client is None and config.get("calendar_client") is not None:
        client = config.get("calendar_client")
    rows = deps.calendar_rows
    if rows is None and isinstance(config.get("calendar_rows"), list):
        rows = config.get("calendar_rows")
    return client, rows


def _run_scan(
    config: Dict[str, Any],
    resolved: Dict[str, Any],
    deps: ScreeningDeps,
    *,
    as_of: date,
    thresholds: EligibilityThresholds,
    store: SelectionStore,
    trading_date: str,
    now=None,
) -> RoundPlan:
    """Full-market scan + configured selection method; saves the sealed selection."""
    from tradingagents.dataflows.market_calendar import CalendarError

    select_n = int(config.get("screening_select_n", 20))
    top_k = int(config.get("screening_top_k", 40))
    max_per_sector = int(config.get("screening_max_per_sector", 5))
    calendar_client, calendar_rows = _resolve_calendar(config, deps)

    # Reserve risk-review names before any Screening LLM sees a factor table.
    held = _attach_holdings(RoundPlan(), config, deps)
    if held.stopped:
        return held
    held_count = len(held.deep_analysis_set)
    limit = config.get("screening_analysis_limit", 20)

    try:
        universe = (deps.universe_fn or (lambda _cfg: fetch_us_equity_universe()))(config)
    except Exception as exc:
        return _stopped("UNIVERSE_UNAVAILABLE", str(exc))
    if not isinstance(universe, list) or not universe:
        return _stopped("UNIVERSE_UNAVAILABLE", "the instrument snapshot is empty or malformed")

    try:
        checker = (deps.quarantine_fn or _default_quarantine)(config)
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)

    try:
        bar_symbols = [entry["symbol"] for entry, reason in zip(universe, identity_issues(universe)) if reason is None]
        bars_by_symbol = (
            deps.bars_fn
            or fetch_daily_bars_batch
        )(
            bar_symbols,
            as_of=as_of,
            adjustment=str(config.get("screening_bar_adjustment", "split")),
            batch_size=int(config.get("screening_bars_batch_size", 100)),
        ) if bar_symbols else {}
    except Exception as exc:
        return _stopped("BARS_UNAVAILABLE", str(exc))

    sector_mapping = dict(config.get("sector_mapping") or {})
    try:
        scored, stats = scan_universe(
            universe,
            bars_by_symbol,
            as_of=as_of,
            thresholds=thresholds,
            sector_mapping=sector_mapping,
            quarantine_checker=checker,
            calendar_client=calendar_client,
            calendar_rows=calendar_rows,
            adjustment_policy=str(config.get("screening_bar_adjustment", "split")),
        )
    except CalendarError as exc:
        return _stopped("CALENDAR_UNAVAILABLE", str(exc))
    scan_stats = {
        "universe_total": stats.universe_total,
        "eligible": stats.eligible,
        "excluded": dict(sorted(stats.excluded.items())),
    }
    allow_shorts = bool(config.get("allow_shorts", False))
    deterministic = resolved["method"] == "exclusion"
    if deterministic:
        top40 = score_exclusion_candidates(scored, ExclusionThresholds.from_config(config), stats)
        scan_stats.update({"survivors": len(top40), "excluded": dict(sorted(stats.excluded.items()))})
    else:
        top40 = select_research_candidates(scored, top_k=min(top_k, limit-held_count), allow_shorts=allow_shorts)
    effective_n = min(select_n, len(top40))
    sector_plan = build_sector_plan(top40, max_per_sector=max_per_sector, select_n=effective_n)
    if not deterministic and sector_plan.get("insufficient_capacity"):
        effective_n = min(effective_n, sum(min(count, max_per_sector) for count in sector_plan["sector_counts"].values()))
        sector_plan = build_sector_plan(top40, max_per_sector=max_per_sector, select_n=effective_n)

    try:
        if not effective_n:
            candidates = []
        elif deterministic:
            selected = select_exclusion_candidates(top40, select_n=select_n, sector_plan=sector_plan)
            candidates = [ScreenedCandidate(
                rank=i, symbol=item.symbol, screening_score=item.exclusion_score,
                short_reason="Passed vol20/trend/r60 gates; survivor-percentile formula priority",
            ) for i, item in enumerate(selected, 1)]
        elif deps.screening_invoke_fn is not None:
            candidates = deps.screening_invoke_fn(
                top40, sector_plan, select_n=effective_n, max_per_sector=max_per_sector
            )
        else:
            invoke = _default_screening_invoke(config, resolved)
            candidates = invoke(
                top40, sector_plan, select_n=effective_n, max_per_sector=max_per_sector
            )
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)
    except ProviderFailure as exc:
        # P2 run-stop semantics: a Screening provider exhaustion stops the
        # whole round; no repair request, no cross-provider fallback.
        return _stopped("PROVIDER_FAILURE", str(exc))
    except Exception as exc:
        return _stopped("SCREENING_STAGE_FAILED", f"{type(exc).__name__}: {sanitize_for_log(str(exc))}")

    if not isinstance(candidates, list) or (not deterministic and len(candidates) != effective_n):
        return _stopped("SCREENING_INVALID_OUTPUT", "Screening response does not match the bounded selection count", scan_stats=scan_stats)
    pool_rows = {item.symbol: item for item in top40}
    if any(getattr(candidate, "symbol", None) not in pool_rows for candidate in candidates):
        return _stopped("SCREENING_INVALID_OUTPUT", "Screening selected a symbol outside the presented pool", scan_stats=scan_stats)
    scan_stats["selected"] = len(candidates)
    scan_stats["selection_shortfall"] = select_n - len(candidates)
    scan_stats["screening_llm_symbols"] = [] if deterministic else [item.symbol for item in top40]

    spec = resolved.get("spec")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trading_date": trading_date,
        "as_of": as_of.isoformat(),
        "generated_at": eastern_timestamp(now),
        # R4: bind consolidated-feed semantics so IEX-era caches never validate.
        "data_feed": SCREENING_DATA_FEED,
        "method": resolved["method"],
        "role": {
            "provider": spec.provider if spec else "deterministic",
            "model": spec.model if spec else FORMULA_VERSION,
            "endpoint": spec.display_endpoint() if spec else None,
        },
        "config_fingerprint": store.config_fingerprint(config, spec),
        "adjustment_policy": str(config.get("screening_bar_adjustment", "split")),
        "sector_mode": {
            "applied": bool(sector_plan.get("applied")),
            "max_per_sector": max_per_sector,
            "missing_sectors": list(sector_plan.get("missing_sectors", [])),
        },
        "stats": scan_stats,
        "top40": [entry.to_cache_dict() for entry in top40],
        "top20": [
            {
                "rank": candidate.rank,
                "symbol": candidate.symbol,
                "screening_score": float(candidate.screening_score),
                "short_reason": candidate.short_reason,
                **pool_rows[candidate.symbol].factor_row(),
            }
            for candidate in candidates
        ],
    }
    payload["data_quality"] = build_quality_report(stats, as_of=as_of,
        observed_at=payload["generated_at"], adjustment=payload["adjustment_policy"])
    selected_symbols = {r["symbol"] for r in payload["top20"]}
    for record in stats.records:
        if record.get("bars") and record["symbol"] not in selected_symbols:
            record["bars"].pop("window", None)
    if not selection_rows_valid(payload, config):
        return _stopped("SCREENING_INPUT_QUALITY_INVALID", "selection input evidence did not validate", scan_stats=scan_stats)
    try:
        store.save(payload)
    except OSError as exc:
        return _stopped("SCREENING_SNAPSHOT_UNAVAILABLE", sanitize_for_log(str(exc)), scan_stats=scan_stats)
    # Carry the exact sealed bytes forward so the long-run A/B coordinator
    # can persist and revalidate this same authoritative selection.
    payload = store.load_raw() or payload
    return RoundPlan(
        status="ok", selection=payload,
        selection_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest(),
    )


def _attach_holdings(
    plan: RoundPlan,
    config: Dict[str, Any],
    deps: ScreeningDeps,
) -> RoundPlan:
    """Add only this account's safely reviewable holdings to a frozen Top20."""
    try:
        positions = (deps.positions_fn or _default_positions)()
        normalized = []
        for position in positions:
            qty = position["qty"]
            if isinstance(qty, bool) or not math.isfinite(float(qty)):
                raise ValueError("position quantity is unavailable or nonfinite")
            symbol = normalize_symbol(position["symbol"])
            if not symbol:
                raise ValueError("position symbol is unavailable")
            if float(qty) != 0:
                normalized.append({**position, "symbol": symbol, "qty": float(qty)})
        positions = normalized
    except Exception as exc:
        return _stopped(
            "HOLDINGS_UNAVAILABLE",
            f"broker positions could not be verified: {sanitize_for_log(str(exc))}",
        )

    us_holdings = [p for p in positions if "/" not in p["symbol"]]
    plan.other_asset_holdings = [
        {"symbol": p["symbol"], "qty": p["qty"]}
        for p in positions if "/" in p["symbol"]
    ]
    top20_symbols = [entry["symbol"] for entry in plan.top20]

    try:
        checker = (deps.quarantine_fn or _default_quarantine)(config)
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)

    overlap: List[str] = []
    extras: List[str] = []
    blocked: List[dict] = []
    seen = set()
    for holding in sorted(us_holdings, key=lambda p: p["symbol"]):
        symbol = holding["symbol"]
        if symbol in seen:
            continue
        seen.add(symbol)
        reason = checker(symbol)
        if reason:
            blocked.append({"symbol": symbol, "reason": f"quarantined: {reason}"})
            continue
        try:
            asset = (deps.asset_fn or _default_asset)(symbol)
            from tradingagents.dataflows.alpaca_utils import asset_field
            tradable = asset_field(asset, "tradable", False) is True
            status = enum_value(asset_field(asset, "status", ""))
        except Exception as exc:
            blocked.append({"symbol": symbol, "reason": f"asset status unavailable: {sanitize_for_log(str(exc))}"})
            continue
        if not tradable or status != "active":
            blocked.append({
                "symbol": symbol,
                "reason": f"not safely analyzable (tradable={tradable}, status={status})",
            })
            continue
        if symbol in top20_symbols:
            overlap.append(symbol)
        else:
            extras.append(symbol)

    plan.overlap_holdings = sorted(overlap)
    plan.extra_holdings = extras
    plan.blocked_holdings = blocked
    # Held risk review gets slots first. Keep the shared Top20 selection intact
    # for A/B and entry validation, but do not invoke analysis on deferred names.
    safe_top = [s for s in top20_symbols if s not in {b["symbol"] for b in blocked}]
    limit = config.get("screening_analysis_limit", 20)
    if len(overlap) + len(extras) > limit:
        plan.status, plan.reason = "stopped", "HELD_REVIEW_BUDGET_EXCEEDED"
        plan.detail = f"{len(overlap) + len(extras)} reviewable US holdings exceed the {limit}-stock analysis limit"
        plan.entry_allowed = False
        return plan
    new_slots = limit - len(overlap) - len(extras)
    if (screening_method(config) == "legacy" and plan.selection is not None and not plan.cached
            and len({r["symbol"] for r in plan.selection.get("top40", [])} | set(overlap + extras)) > limit):
        plan.status, plan.reason = "stopped", "SCREENING_ANALYSIS_BUDGET_EXCEEDED"
        plan.detail = "holdings changed after screening; the combined LLM stock budget would be exceeded"
        plan.entry_allowed = False
        return plan
    new_names = [s for s in safe_top if s not in overlap][:new_slots]
    allowed = set(overlap + new_names)
    plan.deep_analysis_set = [s for s in safe_top if s in allowed] + extras
    plan.deferred_candidates = [s for s in safe_top if s not in allowed]
    return plan


def prepare_screening_round_from_selection(
    config: Dict[str, Any],
    selection: Dict[str, Any],
    *,
    deps: Optional[ScreeningDeps] = None,
    session_date: Optional[str] = None,
) -> RoundPlan:
    """Build an account-specific held-review plan without scanning or invoking an LLM.

    Used by the long-run A/B coordinator after it has durably frozen the
    authoritative shared selection. The seal, session identity and
    selection-affecting config fingerprint are checked before account
    holdings are added.
    """
    deps = deps or ScreeningDeps()
    resolved = resolve_screening_config(config)
    if not resolved.get("enabled"):
        raise ScreeningConfigError(
            "prepare_screening_round_from_selection requires auto_screening_enabled"
        )
    store = SelectionStore(default_selection_cache_path(config))
    if not isinstance(selection, dict):
        return _stopped("FROZEN_SELECTION_INVALID", "selection is not an object")
    seal = selection.get("integrity")
    if not isinstance(seal, str) or seal != _integrity_digest(selection):
        return _stopped("FROZEN_SELECTION_INVALID", "selection integrity seal mismatch")
    if selection.get("schema_version") != SCHEMA_VERSION:
        return _stopped("FROZEN_SELECTION_INVALID", "selection schema is unsupported")
    spec = resolved.get("spec")
    if selection.get("config_fingerprint") != store.config_fingerprint(config, spec):
        return _stopped("FROZEN_SELECTION_INVALID", "selection config fingerprint mismatch")
    selected_date = str(selection.get("trading_date") or "")
    if session_date and selected_date != str(session_date):
        return _stopped("FROZEN_SELECTION_INVALID", "selection session date mismatch")
    try:
        as_of = date.fromisoformat(str(selection.get("as_of")))
        selected_day = date.fromisoformat(selected_date)
    except ValueError:
        return _stopped("FROZEN_SELECTION_INVALID", "selection has an invalid date")
    if as_of > selected_day:
        return _stopped("FROZEN_SELECTION_INVALID", "selection uses future market data")
    top20 = selection.get("top20")
    top40 = selection.get("top40")
    if not isinstance(top20, list) or not isinstance(top40, list):
        return _stopped("FROZEN_SELECTION_INVALID", "selection is missing Top40 or Top20")
    if not selection_rows_valid(selection, config):
        return _stopped("FROZEN_SELECTION_INVALID", "selection formula, gates or candidate rows are invalid")
    top40_symbols = {
        row.get("symbol") for row in top40 if isinstance(row, dict)
    }
    top20_symbols = [row.get("symbol") for row in top20 if isinstance(row, dict)]
    if (
        len(top20_symbols) != len(top20)
        or any(not isinstance(symbol, str) or symbol not in top40_symbols for symbol in top20_symbols)
        or len(set(top20_symbols)) != len(top20_symbols)
    ):
        return _stopped("FROZEN_SELECTION_INVALID", "selection Top20 membership is invalid")

    digest = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    plan = RoundPlan(
        selection=selection,
        selection_date=selected_date,
        as_of=str(selection.get("as_of")),
        cached=True,
        entry_allowed=True,
        top20=list(top20),
        scan_stats=selection.get("stats"),
        screening_description=describe_screening_role(resolved),
        selection_hash=digest,
    )
    return _attach_holdings(plan, config, deps)


def prepare_screening_round(
    config: Dict[str, Any],
    *,
    refresh: bool = False,
    deps: Optional[ScreeningDeps] = None,
    now=None,
) -> RoundPlan:
    """Prepare one auto-screening round (scan-or-cache + bounded held review).

    The public scheduler entry. Never raises for expected screening
    failures — it returns a stopped plan; configuration errors raise
    :class:`ScreeningConfigError` because they mean the run may not start.
    """
    deps = deps or ScreeningDeps()
    resolved = resolve_screening_config(config)  # strict; raises when broken
    if not resolved.get("enabled"):
        raise ScreeningConfigError(
            "prepare_screening_round requires auto_screening_enabled"
        )

    plan = RoundPlan()
    plan.screening_description = describe_screening_role(resolved)
    spec = resolved.get("spec")
    store = SelectionStore(default_selection_cache_path(config))
    thresholds = EligibilityThresholds.from_config(config)
    calendar_client, calendar_rows = _resolve_calendar(config, deps)

    eastern = eastern_now(now)
    try:
        trading_day = is_us_trading_day_production(
            eastern.date(), client=calendar_client, calendar_rows=calendar_rows
        )
    except Exception as exc:
        return _stopped("CALENDAR_UNAVAILABLE", str(exc))

    if refresh and not trading_day:
        return _stopped(
            "SCAN_REFUSED_NON_TRADING_DAY",
            "manual refresh is a new scan and scans do not run on non-trading days",
        )
    if refresh:
        # Explicit new scan: the old list becomes unusable BEFORE the scan
        # starts, so a failed refresh cannot fall back to it.
        store.invalidate()

    selection: Optional[dict] = None
    cached = False

    if trading_day:
        if not refresh:
            selection = store.load_valid(
                config, spec=spec, now=now, calendar_client=calendar_client, calendar_rows=calendar_rows
            )
            cached = selection is not None
        if selection is None:
            with store.scan_lock():
                # Another runner may have finished the first scan while we
                # waited for the lock; reuse it instead of re-scanning.
                selection = store.load_valid(
                    config, spec=spec, now=now, calendar_client=calendar_client, calendar_rows=calendar_rows
                )
                cached = selection is not None
                if selection is None:
                    try:
                        as_of = resolve_as_of(
                            config, now, calendar_client=calendar_client, calendar_rows=calendar_rows
                        )
                    except Exception as exc:
                        return _stopped("CALENDAR_UNAVAILABLE", str(exc))
                    try:
                        trading_date = str(
                            current_trading_date_production(
                                now, client=calendar_client, calendar_rows=calendar_rows
                            )
                        )
                    except Exception as exc:
                        return _stopped("CALENDAR_UNAVAILABLE", str(exc))
                    scan_plan = _run_scan(
                        config,
                        resolved,
                        deps,
                        as_of=as_of,
                        thresholds=thresholds,
                        store=store,
                        trading_date=trading_date,
                        now=now,
                    )
                    if scan_plan.stopped:
                        plan.status = "stopped"
                        plan.reason = scan_plan.reason
                        plan.detail = scan_plan.detail
                        plan.scan_stats = scan_plan.scan_stats
                        return plan
                    selection = scan_plan.selection
                    cached = False

    if not trading_day:
        # Held-risk review only: no scan ran, no Top20, no new entries.
        plan.mode = "held_review"
        plan.entry_allowed = False
        plan.detail = (
            "non-trading day: no scan and no new entries; held-risk review only"
        )
    else:
        assert selection is not None
        plan.selection = selection
        plan.selection_date = selection.get("trading_date")
        plan.as_of = selection.get("as_of")
        plan.cached = cached
        plan.scan_stats = selection.get("stats")
        plan.entry_allowed = True
        plan.top20 = list(selection.get("top20", []))

    if plan.selection is not None:
        plan.selection_hash = hashlib.sha256(
            json.dumps(plan.selection, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
    return _attach_holdings(plan, config, deps)
