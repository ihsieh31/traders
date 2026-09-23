"""Phase B shared broker position context for the Trader and Risk Manager.

One formatter over the strict account-bound `capture_broker_snapshot` data —
not a second snapshot implementation. Both decision nodes render the same
block from the same source, freshly captured at the start of each node:

- Trader: one snapshot captured before the node starts.
- Risk Manager: a new snapshot captured before it starts (so the Decision
  role sees holdings that may have changed during the debate).

Fail-closed contract:
- Any capture failure (timeout, missing account, NaN/Inf, equity<=0, wrong
  account, stale/future timestamp) raises BrokerAuthorityError, which stops
  the run. A failed positions read must never be presented as "flat" and
  must never fall back to memory or stale state.
- Only a complete, successful positions response that does not contain the
  symbol may be rendered as flat with qty=0.
- Optional fields the snapshot does not carry render as "unavailable",
  never as fabricated zeros. Shorts keep the broker's negative market value
  and sign semantics.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tradingagents.execution.authority import (
    BrokerAuthorityError,
    BrokerSnapshot,
    capture_broker_snapshot,
    validate_freshness,
)


@dataclass(frozen=True)
class PositionContext:
    """Typed, broker-sourced position context for one symbol."""

    symbol: str
    observed_at_iso: str
    account_id: str
    equity: float
    cash: float
    buying_power: float
    gross_exposure: float
    gross_exposure_pct: float
    qty: float
    side: str  # LONG / SHORT / FLAT
    market_value: Optional[float]
    avg_entry_price: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]
    position_weight_pct: Optional[float]
    current_price: Optional[float]


@dataclass(frozen=True)
class ActiveTradePlanContext:
    """Original plan proven by the active filled lot in the execution ledger."""

    symbol: str
    availability: str
    decision_id: Optional[str] = None
    trade_date: Optional[str] = None
    original_thesis: Optional[str] = None
    invalidation: Optional[str] = None
    original_stop_loss_price: Optional[float] = None
    original_take_profit_price: Optional[float] = None
    entry_minimum_price: Optional[float] = None
    entry_maximum_price: Optional[float] = None
    entry_expires_at: Optional[str] = None
    exit_by: Optional[str] = None
    risk_fraction: Optional[float] = None
    time_horizon: Optional[str] = None
    active_lot_qty: Optional[float] = None
    source: str = "execution ledger"
    reason: Optional[str] = None


def _unavailable_trade_plan(symbol: str, reason: str, *, availability: str = "unavailable") -> ActiveTradePlanContext:
    return ActiveTradePlanContext(
        symbol=(symbol or "").upper().replace("/", ""),
        availability=availability,
        reason=reason,
    )


def load_active_trade_plan_context(
    position: PositionContext,
    config: Optional[dict] = None,
) -> ActiveTradePlanContext:
    """Load a single matching active lot's original plan without DB writes.

    Missing, unbound, mismatched, malformed, or ambiguous ledger evidence is
    rendered unavailable; current analysis prose is never used as a fallback.
    """
    symbol = (position.symbol or "").upper().replace("/", "")
    if position.side == "FLAT" or abs(position.qty) <= 1e-9:
        return _unavailable_trade_plan(symbol, "broker position is flat")
    try:
        from tradingagents.execution.service import resolve_execution_db_path
        from tradingagents.execution.store import ExecutionStore
        from tradingagents.execution.lifecycle import remaining_lots

        db_path = resolve_execution_db_path((config or {}).get("execution_db_path"))
        if not Path(db_path).is_file():
            return _unavailable_trade_plan(symbol, "configured execution ledger does not exist")
        store = ExecutionStore(db_path, read_only=True)
        if store.account_binding_owner() != position.account_id:
            return _unavailable_trade_plan(symbol, "execution ledger account binding does not match broker snapshot")
        all_lots = remaining_lots(store)
        lots = [lot for lot in all_lots.get(symbol, []) if abs(float(lot.get("qty") or 0)) > 1e-9]
        if not lots:
            return _unavailable_trade_plan(symbol, "no matching active filled lot in execution ledger")
        signed_qty = sum(float(lot["qty"]) for lot in lots)
        if any((float(lot["qty"]) > 0) != (position.qty > 0) for lot in lots):
            return _unavailable_trade_plan(symbol, "ledger lot direction conflicts with broker position")
        if not math.isclose(signed_qty, position.qty, rel_tol=0, abs_tol=1e-6):
            return _unavailable_trade_plan(symbol, "ledger active quantity does not match broker position")

        plans = []
        for lot in lots:
            intent = lot.get("intent") or {}
            if str(intent.get("symbol") or "").upper().replace("/", "") != symbol:
                return _unavailable_trade_plan(symbol, "durable intent symbol does not match active lot")
            payload = json.loads(intent.get("payload_json") or "")
            controls = payload.get("risk_controls") or {}
            policy = payload.get("entry_policy") or {}
            plans.append((intent, payload, controls, policy))
        decision_ids = {str(row[0].get("decision_id") or "") for row in plans}
        if "" in decision_ids:
            return _unavailable_trade_plan(symbol, "durable decision identity is missing")
        if len(decision_ids) != 1:
            return _unavailable_trade_plan(
                symbol, "multiple active trade plans cannot be represented as one original thesis",
                availability="ambiguous",
            )

        intent, payload, controls, policy = plans[0]
        thesis = payload.get("rationale_summary")
        stop = _positive_number(controls.get("stop_loss_price"))
        target = _positive_number(controls.get("take_profit_price"))
        minimum = _positive_number(policy.get("minimum_price"))
        maximum = _positive_number(policy.get("maximum_price"))
        risk_fraction = _positive_number(policy.get("risk_fraction"))
        trade_date = payload.get("trade_date")
        expiry = policy.get("expires_at")
        exit_by = policy.get("exit_by")
        horizon = payload.get("time_horizon")
        required = (thesis, stop, target, minimum, maximum, risk_fraction,
                    trade_date, expiry, exit_by, horizon)
        if not all(value is not None and str(value).strip() for value in required):
            return _unavailable_trade_plan(symbol, "durable original plan is incomplete")
        if minimum > maximum:
            return _unavailable_trade_plan(symbol, "durable entry range is invalid")
        return ActiveTradePlanContext(
            symbol=symbol,
            availability="available",
            decision_id=next(iter(decision_ids)),
            trade_date=str(trade_date),
            original_thesis=str(thesis).strip(),
            invalidation=(str(controls.get("invalidation")).strip()
                          if controls.get("invalidation") is not None else None),
            original_stop_loss_price=stop,
            original_take_profit_price=target,
            entry_minimum_price=minimum,
            entry_maximum_price=maximum,
            entry_expires_at=str(expiry),
            exit_by=str(exit_by),
            risk_fraction=risk_fraction,
            time_horizon=str(horizon).strip(),
            active_lot_qty=signed_qty,
        )
    except Exception as exc:
        return _unavailable_trade_plan(symbol, f"execution ledger evidence unavailable ({type(exc).__name__})")


def _positive_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def render_trade_plan_context(context: ActiveTradePlanContext) -> str:
    if context.availability != "available":
        detail = f" ({context.reason})" if context.reason else ""
        return f"Original trade plan: {context.availability}{detail}."
    def price(value):
        return f"${value:,.2f}" if value is not None else "unavailable"
    return "\n".join((
        f"Original trade plan: available (source: {context.source})",
        f"- Symbol / decision: {context.symbol} / {context.decision_id}",
        f"- Trade date: {context.trade_date}",
        f"- Original thesis: {context.original_thesis}",
        f"- Original invalidation: {context.invalidation or 'unavailable'}",
        f"- Original stop / target: {price(context.original_stop_loss_price)} / {price(context.original_take_profit_price)}",
        f"- Authorized entry range: {price(context.entry_minimum_price)}–{price(context.entry_maximum_price)}",
        f"- Entry expiry / exit_by: {context.entry_expires_at} / {context.exit_by}",
        f"- Original risk fraction / horizon: {context.risk_fraction:.4f} / {context.time_horizon}",
        f"- Active lot quantity: {context.active_lot_qty:g}",
    ))


def render_unavailable_trade_plan(reason: str) -> str:
    return f"Original trade plan: unavailable ({reason})."


def capture_position_context(
    symbol: str,
    broker: Any = None,
    *,
    broker_factory: Any = None,
    expected_account_id: Optional[str] = None,
) -> PositionContext:
    """Capture a fresh authoritative snapshot and derive position context.

    Raises BrokerAuthorityError on any missing/malformed/stale broker fact,
    on account mismatch, or on equity <= 0. The same broker GET retry and
    freshness policy as the execution boundary apply here.
    """
    if broker is None:
        if broker_factory is None:
            from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

            broker_factory = get_alpaca_trading_client
        broker = broker_factory()

    snapshot: BrokerSnapshot = capture_broker_snapshot(
        broker, expected_account_id=expected_account_id
    )
    return build_position_context(symbol, snapshot)


def build_position_context(symbol: str, snapshot: BrokerSnapshot) -> PositionContext:
    """Derive the typed context from an existing validated snapshot.

    Defense in depth: the snapshot's own freshness is re-validated here so a
    stale pre-built snapshot can never be rendered as current context.
    """
    from tradingagents.app_identity import get_env
    from tradingagents.execution.authority import SNAPSHOT_TTL_SECONDS

    validate_freshness(
        snapshot.observed_at,
        ttl_seconds=float(
            get_env("SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)
        ),
        label="position context snapshot",
    )
    if not snapshot.equity or snapshot.equity <= 0:
        raise BrokerAuthorityError(
            f"account equity must be positive for position context, got {snapshot.equity}"
        )

    normalized = (symbol or "").upper().replace("/", "")
    position = snapshot.position(normalized)
    qty = position.qty if position is not None else 0.0
    side = "FLAT"
    if qty > 0:
        side = "LONG"
    elif qty < 0:
        side = "SHORT"

    market_value: Optional[float] = None
    avg_entry: Optional[float] = None
    unrealized_pl: Optional[float] = None
    current_price: Optional[float] = None
    if position is not None:
        market_value = position.market_value
        avg_entry = position.avg_entry_price
        unrealized_pl = position.unrealized_pl
        current_price = position.current_price

    unrealized_pl_pct: Optional[float] = None
    if unrealized_pl is not None and avg_entry and qty:
        cost_basis = abs(avg_entry * qty)
        if cost_basis > 0:
            unrealized_pl_pct = unrealized_pl / cost_basis * 100.0

    position_weight_pct: Optional[float] = None
    if market_value is not None:
        # Broker side/sign convention preserved: abs() keeps a short's
        # weight comparable to a long's without flipping its direction.
        position_weight_pct = abs(market_value) / snapshot.equity * 100.0

    return PositionContext(
        symbol=normalized,
        observed_at_iso=snapshot.observed_at.isoformat(),
        account_id=snapshot.account_id,
        equity=snapshot.equity,
        cash=snapshot.cash,
        buying_power=snapshot.buying_power,
        gross_exposure=snapshot.gross_exposure,
        gross_exposure_pct=snapshot.gross_exposure / snapshot.equity * 100.0,
        qty=qty,
        side=side,
        market_value=market_value,
        avg_entry_price=avg_entry,
        unrealized_pl=unrealized_pl,
        unrealized_pl_pct=unrealized_pl_pct,
        position_weight_pct=position_weight_pct,
        current_price=current_price,
    )


def _money(value: Optional[float]) -> str:
    if value is None:
        return "unavailable"
    return f"${value:,.2f}"


def _percent(value: Optional[float]) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.2f}%"


def _qty(value: Optional[float]) -> str:
    if value is None:
        return "unavailable"
    if abs(value) <= 1e-9:
        return "0"
    sign = "" if value > 0 else "-"
    magnitude = abs(value)
    if float(magnitude).is_integer():
        return f"{sign}{int(magnitude)} shares"
    return f"{sign}{magnitude:.6f} shares (fractional)"


def render_account_context(context: PositionContext) -> str:
    """Account-level lines from the same typed context (no second capture)."""
    return (
        f"Equity: {_money(context.equity)} | Cash: {_money(context.cash)} | "
        f"Buying power: {_money(context.buying_power)} | "
        f"Gross exposure: {_money(context.gross_exposure)} "
        f"({_percent(context.gross_exposure_pct)} of equity) | "
        f"observed {context.observed_at_iso} UTC"
    )


def render_position_context(context: PositionContext) -> str:
    """Deterministic prompt block. Unknown facts say 'unavailable' — never 0."""
    if context.side == "FLAT":
        symbol_line = (
            f"Broker position for {context.symbol}: FLAT (qty=0). The broker "
            "returned a complete, successful positions list and it contains no "
            "open position for this symbol."
        )
    else:
        symbol_line = (
            f"Broker position for {context.symbol}: {context.side}, "
            f"qty={_qty(context.qty)}, market value={_money(context.market_value)}"
        )

    lines = [
        "Broker position context (authoritative snapshot, account "
        f"{context.account_id}, observed at {context.observed_at_iso} UTC):",
        symbol_line,
    ]
    if context.side != "FLAT":
        lines.extend(
            [
                f"- Average entry price: {_money(context.avg_entry_price)}",
                f"- Unrealized P/L: {_money(context.unrealized_pl)} "
                f"({_percent(context.unrealized_pl_pct)})",
                f"- Current price: {_money(context.current_price)}",
                f"- Position weight: {_percent(context.position_weight_pct)} of equity",
            ]
        )
    lines.extend(
        [
            f"- Account equity: {_money(context.equity)}",
            f"- Cash: {_money(context.cash)}",
            f"- Buying power: {_money(context.buying_power)}",
            f"- Gross exposure (sum of absolute position values): "
            f"{_money(context.gross_exposure)} ({_percent(context.gross_exposure_pct)} of equity)",
            "These are the broker's own facts at the timestamp above; they may "
            "differ from anything stated in research reports or memory.",
        ]
    )
    return "\n".join(lines)
