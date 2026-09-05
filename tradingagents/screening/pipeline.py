"""Phase C screening pipeline: daily scan → Top40 → Screening Top20 →
deep-analysis set (Top20 ∪ fresh holdings).

This is the module the scheduler (WebUI auto mode, CLI auto mode) actually
calls once per round; it is not a standalone helper. Semantics:

- One full-market scan and one logical Screening invocation per US trading
  day. Later rounds the same day reuse the validated on-disk selection;
  holdings are re-fetched every round and never cached with the selection.
- Non-trading days run held-risk review only: no scan, no Screening call,
  no new entries (the execution gate blocks openings independently).
- Manual refresh is an explicit new scan: the existing cache is removed
  FIRST, so a failed refresh can never fall back to the old list; the
  round stops and resumption requires an explicitly successful retry.
- Any screening-stage failure (universe/bars/quarantine unavailable,
  INSUFFICIENT_CANDIDATES, INSUFFICIENT_SECTOR_CAPACITY, invalid LLM
  output, provider exhaustion) returns a stopped plan with zero
  downstream analysis — the caller must halt the round.
- All external dependencies are injectable so tests run offline; the
  defaults wire to the real Alpaca clients and the real Screening LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client
from tradingagents.llm_clients.retry import ProviderFailure
from tradingagents.screening.gate import check_entry_allowed  # noqa: F401  (re-export)
from tradingagents.screening.llm import (
    ScreeningConfigError,
    ScreeningStop,
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
    select_top_k,
)
from tradingagents.screening.prompt import build_sector_plan, run_screening_invocation
from tradingagents.screening.selection_store import (
    SCHEMA_VERSION,
    SelectionStore,
    default_selection_cache_path,
    eastern_timestamp,
)
from tradingagents.screening.sessions import eastern_now, is_us_trading_day
from tradingagents.screening.universe import fetch_us_equity_universe, normalize_symbol


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
    llm_factory: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None

    @classmethod
    def real(cls) -> "ScreeningDeps":
        return cls()


def _default_positions() -> List[dict]:
    client = get_alpaca_trading_client()
    positions = []
    for position in client.get_all_positions():
        qty = float(getattr(position, "qty", 0) or 0)
        if qty == 0:
            continue
        positions.append(
            {
                "symbol": normalize_symbol(getattr(position, "symbol", "")),
                "qty": qty,
                "asset_class": str(
                    getattr(getattr(position, "asset_class", ""), "value", getattr(position, "asset_class", ""))
                    or ""
                ).lower(),
            }
        )
    return positions


def _default_asset(symbol: str):
    client = get_alpaca_trading_client()
    return client.get_asset(symbol)


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


def _stopped(reason: str, detail: str = "") -> RoundPlan:
    return RoundPlan(status="stopped", reason=reason, detail=detail)


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
    """Full-market scan + one Screening invocation; saves the selection."""
    select_n = int(config.get("screening_select_n", 20))
    top_k = int(config.get("screening_top_k", 40))
    max_per_sector = int(config.get("screening_max_per_sector", 5))

    try:
        universe = (deps.universe_fn or (lambda _cfg: fetch_us_equity_universe()))(config)
    except Exception as exc:
        return _stopped("UNIVERSE_UNAVAILABLE", str(exc))

    try:
        checker = (deps.quarantine_fn or _default_quarantine)(config)
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)

    try:
        bars_by_symbol = (
            deps.bars_fn
            or fetch_daily_bars_batch
        )(
            [entry["symbol"] for entry in universe],
            as_of=as_of,
            adjustment=str(config.get("screening_bar_adjustment", "split")),
            batch_size=int(config.get("screening_bars_batch_size", 100)),
        )
    except Exception as exc:
        return _stopped("BARS_UNAVAILABLE", str(exc))

    sector_mapping = dict(config.get("sector_mapping") or {})
    scored, stats = scan_universe(
        universe,
        bars_by_symbol,
        as_of=as_of,
        thresholds=thresholds,
        sector_mapping=sector_mapping,
        quarantine_checker=checker,
    )
    if len(scored) < select_n:
        return _stopped(
            "INSUFFICIENT_CANDIDATES",
            f"only {len(scored)} eligible candidates; {select_n} required "
            "(unqualified symbols are never padded in)",
        )
    top40 = select_top_k(scored, top_k)
    sector_plan = build_sector_plan(top40, max_per_sector=max_per_sector, select_n=select_n)
    if sector_plan.get("insufficient_capacity"):
        return _stopped(
            "INSUFFICIENT_SECTOR_CAPACITY",
            "candidate sectors cannot fill the selection under the "
            f"{max_per_sector}-per-sector cap",
        )

    try:
        if deps.screening_invoke_fn is not None:
            candidates = deps.screening_invoke_fn(
                top40, sector_plan, select_n=select_n, max_per_sector=max_per_sector
            )
        else:
            invoke = _default_screening_invoke(config, resolved)
            candidates = invoke(
                top40, sector_plan, select_n=select_n, max_per_sector=max_per_sector
            )
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)
    except ProviderFailure as exc:
        # P2 run-stop semantics: a Screening provider exhaustion stops the
        # whole round; no repair request, no cross-provider fallback.
        return _stopped("PROVIDER_FAILURE", str(exc))
    except Exception as exc:
        return _stopped("SCREENING_STAGE_FAILED", f"{type(exc).__name__}: {exc}")

    spec = resolved.get("spec")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trading_date": trading_date,
        "as_of": as_of.isoformat(),
        "generated_at": eastern_timestamp(now),
        "role": {
            "provider": spec.provider,
            "model": spec.model,
            "endpoint": spec.display_endpoint(),
        },
        "config_fingerprint": store.config_fingerprint(config, spec),
        "adjustment_policy": str(config.get("screening_bar_adjustment", "split")),
        "sector_mode": {
            "applied": bool(sector_plan.get("applied")),
            "max_per_sector": max_per_sector,
            "missing_sectors": list(sector_plan.get("missing_sectors", [])),
        },
        "stats": {
            "universe_total": stats.universe_total,
            "eligible": stats.eligible,
            "excluded": dict(sorted(stats.excluded.items())),
        },
        "top40": [entry.to_cache_dict() for entry in top40],
        "top20": [
            {
                "rank": candidate.rank,
                "symbol": candidate.symbol,
                "screening_score": float(candidate.screening_score),
                "short_reason": candidate.short_reason,
            }
            for candidate in candidates
        ],
    }
    store.save(payload)
    return RoundPlan(status="ok", selection=payload)


def prepare_screening_round(
    config: Dict[str, Any],
    *,
    refresh: bool = False,
    deps: Optional[ScreeningDeps] = None,
    now=None,
) -> RoundPlan:
    """Prepare one auto-screening round (scan-or-cache + holdings union).

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

    eastern = eastern_now(now)
    trading_day = is_us_trading_day(eastern.date())

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
            selection = store.load_valid(config, spec=spec, now=now)
            cached = selection is not None
        if selection is None:
            with store.scan_lock():
                # Another runner may have finished the first scan while we
                # waited for the lock; reuse it instead of re-scanning.
                selection = store.load_valid(config, spec=spec, now=now)
                cached = selection is not None
                if selection is None:
                    scan_plan = _run_scan(
                        config,
                        resolved,
                        deps,
                        as_of=resolve_as_of(config, now),
                        thresholds=thresholds,
                        store=store,
                        trading_date=str(eastern.date()),
                        now=now,
                    )
                    if scan_plan.stopped:
                        plan.status = "stopped"
                        plan.reason = scan_plan.reason
                        plan.detail = scan_plan.detail
                        return plan
                    selection = scan_plan.selection
                    cached = False

    # ---- fresh holdings (every round; never cached with the selection) ----
    try:
        positions = (deps.positions_fn or _default_positions)()
    except Exception as exc:
        return _stopped(
            "HOLDINGS_UNAVAILABLE",
            f"broker positions could not be verified: {exc}",
        )

    us_holdings = [p for p in positions if "/" not in p["symbol"]]
    plan.other_asset_holdings = [
        {"symbol": p["symbol"], "qty": p["qty"]} for p in positions if "/" in p["symbol"]
    ]

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

    top20_symbols = [entry["symbol"] for entry in plan.top20]

    try:
        checker = (deps.quarantine_fn or _default_quarantine)(config)
    except ScreeningStop as exc:
        return _stopped(exc.reason, exc.detail)

    overlap: List[str] = []
    extras: List[str] = []
    blocked: List[dict] = []
    seen = set(top20_symbols)
    for holding in sorted(us_holdings, key=lambda p: p["symbol"]):
        symbol = holding["symbol"]
        if symbol in seen:
            overlap.append(symbol)
            continue
        seen.add(symbol)
        reason = checker(symbol)
        if reason:
            blocked.append({"symbol": symbol, "reason": f"quarantined: {reason}"})
            continue
        try:
            asset = (deps.asset_fn or _default_asset)(symbol)
            tradable = bool(getattr(asset, "tradable", False))
            status = str(getattr(asset, "status", "") or "").lower()
        except Exception as exc:
            blocked.append(
                {"symbol": symbol, "reason": f"asset status unavailable: {exc}"}
            )
            continue
        if not tradable or status != "active":
            blocked.append(
                {
                    "symbol": symbol,
                    "reason": f"not safely analyzable (tradable={tradable}, status={status})",
                }
            )
            continue
        extras.append(symbol)

    plan.overlap_holdings = sorted(overlap)
    plan.extra_holdings = extras
    plan.blocked_holdings = blocked
    plan.deep_analysis_set = [*top20_symbols, *extras]
    return plan
