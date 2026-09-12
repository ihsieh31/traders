"""Single Phase A execution entry. All production order/close paths go here.

Trust boundary: validate TradeIntent -> durable outbox COMMIT -> safety ->
broker submit. DB commit failure => zero broker calls. Missing/invalid
intent => fail-closed with zero broker calls.

Ponytail ceiling note: one service + one SQLite store + one authority module.
No facade/adapter/repository, scheduler, retry, or lease framework.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from tradingagents.execution.store import (
    ExecutionStore,
    canonical_decision_id,
    client_order_id_for,
    is_valid_order_transition,  # re-export for tests/callers
)
from tradingagents.execution.authority import (
    AccountExecutionLock,
    AccountLockBusy,
    BrokerAuthorityError,
    BrokerSnapshot,
    Reconciler,
    ReconciliationResult,
    SNAPSHOT_TTL_SECONDS,
    broker_status_to_local,
    capture_broker_snapshot,
    capture_quote,
    utc_now,
    validate_freshness,
    validate_quote,
)

__all__ = [
    "ExecutionService",
    "execute_trade_intent",
    "liquidate_position",
    "is_valid_order_transition",
]

_DEFAULT_DB = "eval_results/execution.db"

_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connectionerror",
    "socket closed",
    "temporarily unavailable",
)


def _exception_http_status(exc: BaseException) -> Optional[int]:
    """Extract an HTTP status from an exception chain without text guessing (R07).

    Reads structured attributes only — the exception's own ``response`` /
    ``status_code`` and the same attributes on wrapped causes (requests
    ``HTTPError``, alpaca ``APIError``). Message text never proves an HTTP
    status, so it is not pattern-matched here.
    """
    current: Optional[BaseException] = exc
    for _ in range(6):
        if current is None:
            return None
        for attr in ("response", "status_code"):
            try:
                value = getattr(current, attr, None)
            except Exception:
                value = None
            status = getattr(value, "status_code", None)
            if isinstance(status, int) and 100 <= status <= 599:
                return status
            if isinstance(value, int) and 100 <= value <= 599:
                return value
        current = current.__cause__ or next(
            (a for a in current.args if isinstance(a, BaseException)), None
        )
    return None


def _is_ambiguous_error(exc: BaseException) -> bool:
    """True when a submit outcome cannot be proven either way (R07).

    HTTP 408 and any 5xx (including Cloudflare 520-527/530) mean the broker
    may have accepted the POST before failing to answer; transport timeouts
    and connection resets are equally unprovable. Only a provable outcome
    (a definitive 4xx response, or no POST at all) may be treated as
    terminal.
    """
    status = _exception_http_status(exc)
    if status is not None and (status == 408 or 500 <= status <= 599):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _TIMEOUT_MARKERS)


def _definitive_rejection(exc: BaseException) -> bool:
    """N11: True only for a provable broker rejection after a POST was sent.

    Only a structured HTTP 4xx found in the exception chain (never 408, and
    never guessed from message text) proves the broker read and refused the
    request. HTTP 200 responses that fail JSON/schema decoding, missing
    broker order identity, timeouts, resets, 408 and 5xx all leave the
    outcome unprovable: the submit may have been accepted, so the row must
    stay UNKNOWN and reconcile by its client order id.
    """
    status = _exception_http_status(exc)
    return status is not None and 400 <= status <= 499 and status != 408


class StaleRecoveryPositionError(BrokerAuthorityError):
    """N02: a recovery resubmit was blocked before any POST because the
    persisted position assumption would trade through the fresh broker
    position. Carries the flag so callers can surface
    ``stale_position_transition`` instead of a generic authority failure."""

    stale_position_transition = True


def _new_maintenance_collector() -> dict[str, Any]:
    """N15: mutation collector for one round-maintenance pass.

    Counts every POST/DELETE attempt exactly once at the broker boundary;
    adopting an existing broker order is never a mutation.
    """
    return {"submit_calls": 0, "cancel_calls": 0, "submitted_symbols": set(),
            "has_unknown": False}


def _maintenance_summary(
    collector: dict[str, Any], *, paused: bool, error: str = "",
) -> dict[str, Any]:
    submits = int(collector.get("submit_calls") or 0)
    cancels = int(collector.get("cancel_calls") or 0)
    return {
        "broker_calls": submits + cancels,
        "submit_calls": submits,
        "cancel_calls": cancels,
        "submitted_symbols": sorted(
            str(s).upper() for s in (collector.get("submitted_symbols") or set())
        ),
        "has_unknown": bool(collector.get("has_unknown")),
        "paused": bool(paused),
        "error": str(error or "")[:300],
    }


def resolve_execution_db_path(explicit: Optional[str | Path] = None) -> str:
    """Single execution-DB path resolver (F13): service and reports share it.

    Precedence: 1) explicit caller/runtime path, 2) the current
    ``TRADINGAGENTS_EXECUTION_DB`` environment value read at call time
    (never frozen at import), 3) the literal default. No default DB is
    created or opened here — callers decide when to open the store.
    """
    if explicit:
        return str(explicit)
    env_value = os.getenv("TRADINGAGENTS_EXECUTION_DB", "").strip()
    if env_value:
        return env_value
    return "eval_results/execution.db"


def _default_db_path() -> str:
    return resolve_execution_db_path()


def validate_trade_intent(trade_intent: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return (validated_dict, error). Invalid => (None, reason), zero broker calls.

    N09: a pre-built ``TradeIntent`` model is re-validated from its dump so the
    canonical-consistency model validator can never be bypassed by callers that
    already constructed the model.
    """
    if trade_intent is None:
        return None, "missing trade_intent: strict boundary requires schema-valid TradeIntent"
    try:
        from tradingagents.agents.schemas import TradeIntent as _TI
    except Exception as exc:  # pragma: no cover - import should exist
        return None, f"TradeIntent schema unavailable: {exc}"
    try:
        if isinstance(trade_intent, _TI):
            validated = _TI.model_validate(trade_intent.model_dump(mode="json"))
            return validated.model_dump(mode="json"), None
        validated = _TI.model_validate(trade_intent)
        return validated.model_dump(mode="json"), None
    except Exception as exc:
        return None, f"Invalid trade intent: {exc}"


def _planned_order_specs(intent: dict[str, Any], dollar_amount: Optional[float]):
    """Map TradeIntent.planned_actions to logical order specs.

    Returns list of {role, seq, side, order_type, quantity, notional}.
    HOLD/STAY (order_type none) => [] (no broker calls).
    """
    planned = intent.get("planned_actions") or []
    actionable = [a for a in planned if isinstance(a, dict) and a.get("order_type") != "none"]
    if not actionable:
        # Fallback to order_intent when planned_actions is empty but an
        # explicit market/close intent exists (defensive; canonical path
        # always populates planned_actions).
        oi = intent.get("order_intent") or {}
        if oi.get("order_type") in ("market", "close_position") and oi.get("side"):
            actionable = [
                {
                    "action": "open" if oi["order_type"] == "market" else "close",
                    "order_type": oi["order_type"],
                    "side": oi["side"],
                    "sizing_basis": oi.get("sizing_basis", "configured_notional"),
                }
            ]
        else:
            return []
    specs = []
    multi = len(actionable) > 1
    for i, a in enumerate(actionable):
        order_type = str(a.get("order_type") or "market")
        side = str(a.get("side") or "").lower()
        action_name = str(a.get("action") or "")
        if multi:
            # close-then-open flip: first leg is the close.
            role = "close" if i == 0 else "open"
        else:
            role = "close" if order_type == "close_position" else "open"
        notional = None
        quantity = None
        oi = intent.get("order_intent") or {}
        if dollar_amount and dollar_amount > 0 and role == "open":
            notional = float(dollar_amount)
        elif oi.get("notional_usd") and role == "open":
            try:
                notional = float(oi["notional_usd"])
            except (TypeError, ValueError):
                notional = None
        if oi.get("quantity") and role == "open":
            try:
                quantity = float(oi["quantity"])
            except (TypeError, ValueError):
                quantity = None
        specs.append(
            {
                "role": role,
                "seq": i,
                "side": side or ("sell" if "close" in action_name and False else "buy"),
                "order_type": order_type,
                "action_name": action_name,
                "quantity": quantity,
                "notional": notional,
            }
        )
    # Fix side when planner omitted it: close LONG => sell, close SHORT => buy.
    for s in specs:
        if not s["side"]:
            s["side"] = "sell" if s["role"] == "close" else "buy"
    return specs


def _resolve_qty(symbol: str, amount: float) -> Optional[int]:
    """Return integer share qty from the latest quote, or None.

    Never guesses a price: missing/zero quotes mean no whole-share qty.
    Plain notional orders do not need a qty; broker-side bracket/OTO legs do.
    """
    try:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        quote = AlpacaUtils.get_latest_quote(symbol)
        price = quote.get("bid_price") or quote.get("ask_price")
        price = float(price) if price else 0.0
    except Exception:
        return None
    if price <= 0:
        return None
    try:
        qty = int(float(amount) / price)
    except (TypeError, ValueError):
        return None
    return qty if qty >= 1 else None


def _resolve_protective_prices(
    intent_dict: dict[str, Any], signal: str, is_crypto: bool, warnings: list
) -> Optional[dict[str, float]]:
    """Decide which protective price levels can be submitted to the broker.

    Returns a dict for bracket/OTO submission, or None when the intent must
    stay advisory (no numeric levels, crypto asset, disabled by config,
    non-opening action, or inconsistent levels).
    """
    try:
        from tradingagents.agents.schemas import extract_protective_price
        from tradingagents.dataflows.config import get_config
    except Exception:
        return None
    controls = intent_dict.get("risk_controls") or {}
    if isinstance(controls, dict):
        stop_price = controls.get("stop_loss_price") or extract_protective_price(
            controls.get("stop_loss")
        )
        target_price = controls.get("take_profit_price") or extract_protective_price(
            controls.get("take_profit")
        )
    else:
        stop_price = getattr(controls, "stop_loss_price", None)
        target_price = getattr(controls, "take_profit_price", None)
    if not stop_price and not target_price:
        return None

    opening_long = signal in {"BUY", "LONG"}
    opening_short = signal == "SHORT"
    if not opening_long and not opening_short:
        return None  # closes/holds carry nothing to protect

    if is_crypto:
        warnings.append(
            "Protective bracket/OTO orders are not supported for crypto assets; controls remain advisory."
        )
        return None

    try:
        enabled = get_config().get("protective_bracket_orders_enabled", True)
    except Exception:
        enabled = True
    if not enabled:
        warnings.append(
            "Protective bracket orders are disabled by configuration; controls remain advisory."
        )
        return None

    if stop_price and target_price:
        inverted = (opening_long and stop_price >= target_price) or (
            opening_short and target_price >= stop_price
        )
        if inverted:
            warnings.append(
                f"Protective prices are inconsistent for a {'long' if opening_long else 'short'} entry "
                f"(stop={stop_price}, target={target_price}); controls remain advisory."
            )
            return None

    out: dict[str, float] = {}
    if stop_price:
        out["stop_loss_price"] = float(stop_price)
    if target_price:
        out["take_profit_price"] = float(target_price)
    return out or None


def _build_protective_request(
    symbol: str,
    side: str,
    qty: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    client_order_id: str,
):
    """Build a broker-side bracket/OTO market order. Equities only, GTC."""
    if not stop_loss_price and not take_profit_price:
        return None
    if "/" in (symbol or "").upper():
        return None
    try:
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        stop_loss = (
            StopLossRequest(stop_price=round(float(stop_loss_price), 2))
            if stop_loss_price
            else None
        )
        take_profit = (
            TakeProfitRequest(limit_price=round(float(take_profit_price), 2))
            if take_profit_price
            else None
        )
        order_class = (
            OrderClass.BRACKET if (stop_loss and take_profit) else OrderClass.OTO
        )
        return MarketOrderRequest(
            symbol=(symbol or "").upper().replace("/", ""),
            side=order_side,
            time_in_force=TimeInForce.GTC,
            qty=int(qty),
            order_class=order_class,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_order_id=client_order_id,
        )
    except Exception:
        return None


def _build_market_request(symbol: str, side: str, notional, quantity, client_order_id: str):
    is_crypto = "/" in (symbol or "").upper()
    tif_value = "gtc" if is_crypto else "day"
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        kwargs: dict[str, Any] = {
            "symbol": (symbol or "").upper().replace("/", ""),
            "side": order_side,
            "time_in_force": tif,
            "client_order_id": client_order_id,
        }
        if notional and float(notional) > 0:
            kwargs["notional"] = float(notional)
        elif quantity and float(quantity) > 0:
            kwargs["qty"] = float(quantity)
        else:
            return None
        return MarketOrderRequest(**kwargs)
    except Exception:
        # Alpaca SDK unavailable (offline unit tests use mock brokers that
        # accept any request object). Return a minimal dict-like payload.
        if notional and float(notional) > 0:
            payload = {"notional": float(notional)}
        elif quantity and float(quantity) > 0:
            payload = {"qty": float(quantity)}
        else:
            return None
        payload.update(
            {
                "symbol": (symbol or "").upper().replace("/", ""),
                "side": side,
                "time_in_force": tif_value,
                "client_order_id": client_order_id,
            }
        )
        return payload


def _get_execution_config() -> dict:
    try:
        from tradingagents.dataflows.config import get_config

        return get_config() or {}
    except Exception:
        return {}


def _position_unchanged(after_qty: float, before_qty: float) -> bool:
    """Same side and same absolute quantity (NEW-R1).

    ``before_qty`` may be negative (SHORT); comparing the signed quantity
    against ``abs(before)`` falsely reported every unchanged SHORT as
    changed. Only a real quantity change or a side flip counts as changed.
    """
    before_qty = float(before_qty)
    after_qty = float(after_qty)
    same_side = (after_qty > 0) == (before_qty > 0)
    same_qty = abs(abs(after_qty) - abs(before_qty)) <= 1e-8
    return same_side and same_qty


def _evaluate_opening_caps(
    *,
    symbol: str,
    specs: list[dict[str, Any]],
    snapshot: Any,
    quote: Any,
    intent_dict: dict[str, Any],
    quote_factory: Optional[Callable[[str], Any]] = None,
):
    """Run the Phase B deterministic exposure evaluator for opening legs.

    A verified close leg in the same intent (close-then-open flip) credits
    its freed market value so only the increasing part is clipped.
    Quantity-based legs of the candidate are valued with the execution
    quote; every OTHER symbol that has a quantity-only outstanding order is
    valued with its own validated quote. A missing/invalid quote for any
    such symbol fails the evaluation closed (zero broker calls) instead of
    guessing a price from the candidate.
    """
    from tradingagents.risk.exposure import (
        evaluate_opening_exposure,
        outstanding_increasing_notional,
    )

    config = _get_execution_config()
    quote_price = float(quote.price) if quote is not None else None
    position = snapshot.position(symbol)
    planned_close_reduction = 0.0
    proposed_notional = 0.0
    for spec in specs:
        if spec.get("role") != "open":
            continue
        if spec.get("notional") is not None:
            proposed_notional += float(spec["notional"])
        elif spec.get("quantity") is not None and quote_price:
            proposed_notional += float(spec["quantity"]) * quote_price
    for spec in specs:
        if spec.get("role") == "close" and position is not None:
            quantity = spec.get("quantity")
            if quantity is not None and abs(float(quantity)) >= abs(position.qty) - 1e-9:
                planned_close_reduction += abs(float(position.market_value))

    # F03: determine which distinct symbols need their own price because a
    # live increasing order carries a quantity but no notional. The
    # candidate's validated quote seeds the map; every other required symbol
    # gets its own validated quote or the whole evaluation fails closed.
    # N05: terminal historical orders (FILLED/CANCELED/REJECTED/EXPIRED per
    # broker_status_to_local) never demand a quote — an old canceled ticket
    # must not block unrelated new exposure.
    reference_prices: dict[str, float] = {}
    if quote_price is not None:
        reference_prices[(symbol or "").upper().replace("/", "")] = quote_price
    needs_price = {
        order.symbol
        for order in snapshot.orders
        if order.notional is None
        and broker_status_to_local(order.status)
        not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
        and float(order.qty or 0) - float(order.filled_qty or 0) > 0
    }
    for required in sorted(needs_price):
        if required in reference_prices:
            continue
        if quote_factory is None:
            total, fully_estimated = outstanding_increasing_notional(snapshot)
            if not fully_estimated:
                raise BrokerAuthorityError(
                    f"cannot obtain a validated quote for outstanding-order "
                    f"symbol {required}; refusing to add exposure on a "
                    "guessed price"
                )
            continue
        try:
            required_quote = validate_quote(quote_factory(required), required)
            reference_prices[required] = float(required_quote.price)
        except Exception as exc:
            raise BrokerAuthorityError(
                f"cannot obtain a validated quote for outstanding-order "
                f"symbol {required}: {exc}; refusing to add exposure"
            ) from exc

    return evaluate_opening_exposure(
        symbol=symbol,
        proposed_notional=proposed_notional,
        snapshot=snapshot,
        quote_price=quote_price,
        reference_prices=reference_prices,
        symbol_cap_pct=float(config.get("max_symbol_concentration_pct", 25.0) or 0),
        sector_cap_pct=config.get("max_sector_exposure_pct", 30.0),
        gross_cap_pct=config.get("portfolio_max_gross_exposure_pct", 100.0),
        sector_mapping=dict(config.get("sector_mapping") or {}),
        planned_close_reduction=planned_close_reduction,
    )


class _DeadlineGapOutcome(Exception):
    """Carries a protection-gap result out of the deadline loop (F01)."""

    def __init__(self, gap: dict[str, Any]):
        super().__init__(str(gap.get("error") or "protection gap"))
        self.gap = gap


class ExecutionService:
    """Unique production execution entry (paper-only)."""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        broker_factory: Optional[Callable[[], Any]] = None,
        quote_factory: Optional[Callable[[str], Any]] = None,
    ):
        self.db_path = str(db_path or _default_db_path())
        self._store = ExecutionStore(self.db_path)
        self._broker_factory = broker_factory or self._default_broker_factory
        self._quote_factory = quote_factory or capture_quote
        self._quarantine_gate: Any = None

    @property
    def store(self) -> ExecutionStore:
        return self._store

    def quarantine_gate(self):
        """Lazily built Phase B corporate-action gate (persisted ledger)."""
        if self._quarantine_gate is None:
            from tradingagents.risk.corporate_actions import build_quarantine_gate

            self._quarantine_gate = build_quarantine_gate(_get_execution_config())
        return self._quarantine_gate

    def _quarantine_rejection(self, symbol: str) -> Optional[dict]:
        """Fail-closed corporate-action gate for exposure-adding orders.

        A missing/unbuildable gate is itself a failure for opening orders:
        the quarantine state would be unprovable, so no new risk is taken.
        """
        normalized = (symbol or "").upper()
        try:
            gate = self.quarantine_gate()
        except Exception as exc:
            return {
                "error": f"corporate-action quarantine state unavailable ({exc}); "
                "refusing to add exposure",
            }
        if gate is None:
            return {
                "error": "corporate-action quarantine state unavailable; "
                "refusing to add exposure",
            }
        reason = gate.check(normalized)
        if reason:
            return {"error": reason}
        return None

    @staticmethod
    def _default_broker_factory():
        from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

        return get_alpaca_trading_client()

    def _verify_owned_close_protections(self, broker, snapshot, symbol):
        """Validate that a symbol's live orders are proven owned protections.

        Raises without touching the broker when ownership cannot be proven.
        This is the pre-cancellation check (no mutation); the canceling
        variant reuses it before issuing cancel calls.
        """
        position = snapshot.position(symbol)
        if position is None:
            return
        orders = [o for o in snapshot.orders if o.symbol == position.symbol and broker_status_to_local(o.status) not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}]
        if not orders:
            return
        side = "sell" if position.qty > 0 else "buy"
        for order in orders:
            local = self._store.get_order_by_client(order.client_order_id)
            if (not local or local.get("broker_order_id") != order.broker_order_id
                    or not self._store.protective_parent(local["order_id"]) or order.side != side):
                raise BrokerAuthorityError("Exit conflicts with an order not proven to be its protection")

    def _abandon_prepared_rows(self, prepared: dict[str, Any]) -> None:
        """Mark a durably-committed close intent CANCELED without a broker call.

        Used when the position proved closed/changed during the protection
        cancellation race, so the committed rows cannot be replayed later as
        a new submit against facts that no longer hold.
        """
        order_rows = prepared.get("order_rows") or []
        intent_row = prepared.get("intent_row") or {}
        for row in order_rows:
            status = str((row.get("status") or "")).upper()
            if status == "PENDING":
                try:
                    self._store.transition_order(row["order_id"], "CANCELED")
                except Exception:
                    pass
        try:
            self._store.update_intent_state(intent_row["intent_id"], "CANCELED")
        except Exception:
            pass

    @staticmethod
    def _broker_order_live(snapshot: BrokerSnapshot, broker_order_id: str) -> bool:
        """R03: is this broker order still live according to fresh facts?"""
        for order in snapshot.orders:
            if order.broker_order_id == broker_order_id:
                return broker_status_to_local(order.status) not in {
                    "FILLED", "CANCELED", "REJECTED", "EXPIRED",
                }
        return False

    def _cancel_protection_with_race_check(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        order: Any,
        *,
        account_id: str,
    ) -> tuple[BrokerSnapshot, int]:
        """One proven-protection DELETE hardened against bracket cascade (R03).

        Canceling one Alpaca bracket child often cascade-cancels its sibling,
        so a blind second DELETE raises "already canceled" and previously
        aborted the close mid-flow, leaving a live position with no
        protection. Rules here:

        - a fresh snapshot before every DELETE skips children the broker has
          already taken to a terminal state (cascade done, no DELETE sent);
        - the outcome of a failing DELETE is settled by FRESH FACTS, never by
          the error text: a proven-terminal child was a harmless cascade
          race; a still-live child keeps its protection and the caller's
          verified-exit check refuses the close (fail closed);
        - every DELETE actually sent counts, even when it raised;
        - every step reconciles against a fresh snapshot.

        Returns (freshest snapshot, 1 when a DELETE was actually sent else 0).
        """
        fresh = capture_broker_snapshot(broker, expected_account_id=account_id)
        self._reconcile_snapshot(broker, fresh)
        if not self._broker_order_live(fresh, order.broker_order_id):
            return fresh, 0  # broker cascade already completed this child
        # N04: the kill switch may have engaged while the fresh snapshot GET
        # ran. Re-check it AFTER the GET and BEFORE the DELETE: an engaged
        # kill switch means DELETE=0 and the protection order stays live
        # (the caller's verified-exit check then refuses the close, fail
        # closed). This closes only the pre-check race window; a DELETE that
        # already entered the network cannot be recalled.
        try:
            from tradingagents.safety import get_safety_guard

            _guard = get_safety_guard()
        except Exception:
            _guard = None
        if (
            _guard is not None
            and getattr(_guard, "enabled", True)
            and callable(getattr(_guard, "kill_switch_active", None))
            and _guard.kill_switch_active() is True
        ):
            return fresh, 0
        try:
            broker.cancel_order_by_id(order.broker_order_id)
        except Exception:
            after = capture_broker_snapshot(broker, expected_account_id=account_id)
            self._reconcile_snapshot(broker, after)
            # Fresh facts decide: still-live means the cancel genuinely failed
            # and the position is still protected — the verified-exit gate
            # below will refuse the close instead of racing a bare position.
            return after, 1
        after = capture_broker_snapshot(broker, expected_account_id=account_id)
        self._reconcile_snapshot(broker, after)
        return after, 1

    def _cancel_owned_close_protections(self, broker, snapshot, symbol):
        """Cancel proven protections before an explicit full-position exit.

        Manual or ambiguous orders are never canceled. A pending cancellation
        is still rejected by the subsequent verified-exit check. The caller
        must already hold a durably-committed close (F04 sequencing) before
        invoking this mutation. R03: bracket cascade and cancel races are
        absorbed per-child instead of aborting the close flow.
        """
        position = snapshot.position(symbol)
        if position is None:
            return snapshot, 0
        orders = [o for o in snapshot.orders if o.symbol == position.symbol and broker_status_to_local(o.status) not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}]
        if not orders:
            return snapshot, 0
        side = "sell" if position.qty > 0 else "buy"
        for order in orders:
            local = self._store.get_order_by_client(order.client_order_id)
            if (not local or local.get("broker_order_id") != order.broker_order_id
                    or not self._store.protective_parent(local["order_id"]) or order.side != side):
                raise BrokerAuthorityError("Exit conflicts with an order not proven to be its protection")
        from tradingagents.safety import get_safety_guard
        guard = get_safety_guard()
        verdict = (
            guard.check_order(
                symbol,
                abs(position.market_value),
                account={
                    "equity": snapshot.equity,
                    "last_equity": snapshot.last_equity,
                },
                position_value=abs(position.market_value),
                risk_reducing=True,
            )
            if getattr(guard, "enabled", True)
            else None
        )
        if verdict is not None and not verdict.allowed:
            raise BrokerAuthorityError("Protection cancellation blocked by safety policy")
        canceled_calls = 0
        for order in orders:
            snapshot, calls = self._cancel_protection_with_race_check(
                broker, snapshot, order, account_id=snapshot.account_id,
            )
            canceled_calls += calls
        return snapshot, canceled_calls

    # -- protection-gap invariant (F04) ------------------------------------

    def _program_owned_live_reducing_qty(
        self, snapshot: BrokerSnapshot, symbol: str, reducing_side: str
    ) -> float:
        """Remaining qty of live program-owned reducing orders for a symbol.

        Program-owned means the durable ledger knows the row (submitted
        orders and registered protective children both qualify). Manual or
        unknown broker orders never prove protection.
        """
        covered = 0.0
        for order in snapshot.orders:
            if order.symbol != symbol or order.side != reducing_side:
                continue
            if broker_status_to_local(order.status) in {
                "FILLED", "CANCELED", "REJECTED", "EXPIRED",
            }:
                continue
            local = self._store.get_order_by_client(order.client_order_id)
            if not local or local.get("broker_order_id") != order.broker_order_id:
                continue
            covered += max(0.0, float(order.qty or 0) - float(order.filled_qty or 0))
        return covered

    def _evaluate_protection_gap(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        symbol: str,
        *,
        canceled_protections: int,
    ) -> Optional[dict[str, Any]]:
        """Fail-closed PAUSED when our close attempt left a live position bare.

        After this program canceled proven protections, one of these must be
        provable from broker facts before the account may stay CLEAN:

        - the broker position is gone; or
        - a program-owned, broker-visible, live exposure-reducing close for
          the remaining quantity exists; or
        - broker-visible program-owned protection still exists.

        Otherwise the durable account state becomes PAUSED with a stable
        ``PROTECTION_GAP:`` reason, and every subsequent opening exposure is
        blocked until recovery/operator action proves safety.
        """
        if canceled_protections <= 0:
            return None
        position = snapshot.position(symbol)
        if position is None or abs(position.qty) <= 1e-9:
            return None  # position closed: gap impossible
        reducing_side = "sell" if position.qty > 0 else "buy"
        covered = self._program_owned_live_reducing_qty(
            snapshot, position.symbol, reducing_side
        )
        if covered >= abs(position.qty) - 1e-8:
            return None  # a live close/protection still covers the position
        reason = (
            f"PROTECTION_GAP: {symbol} remains exposed after a close attempt "
            f"canceled its protections: no proven live close or protection "
            f"covers the {abs(position.qty):g}-share position; operator "
            "review required"
        )
        self._store.save_account_state(
            account_id=snapshot.account_id,
            state="PAUSED",
            reasons=(reason,),
            snapshot_version=snapshot.version,
            baseline_positions={p.symbol: p.qty for p in snapshot.positions},
        )
        return {
            "paused": True,
            "fail_closed": True,
            "protection_gap": True,
            "account_execution_state": "PAUSED",
            "reconciliation_reasons": [reason],
            "error": reason,
        }

    def _recover_persisted_protection_gaps(
        self, snapshot: BrokerSnapshot, result: Any, prior_reasons: list[str],
    ) -> Any:
        """Keep a persisted PROTECTION_GAP pause until broker facts prove safety.

        On every reconciliation: if durable account state carried a
        PROTECTION_GAP reason (captured BEFORE reconcile overwrote it) and
        the broker still holds that position with neither a proven
        protection nor a proven live close, remain PAUSED. When later facts
        prove the position closed or protected/closing, normal
        reconciliation may clear the pause.
        """
        gap_reasons = [
            r for r in prior_reasons
            if isinstance(r, str) and r.startswith("PROTECTION_GAP:")
        ]
        if not gap_reasons:
            return result
        still_gapped: list[str] = []
        for reason in gap_reasons:
            parts = reason.split(":", 1)[1].strip().split(" ", 1)
            symbol = (parts[0] or "").upper()
            if not symbol:
                still_gapped.append(reason)
                continue
            position = snapshot.position(symbol)
            if position is None or abs(position.qty) <= 1e-9:
                continue  # broker proves the position closed: may clear
            reducing_side = "sell" if position.qty > 0 else "buy"
            covered = self._program_owned_live_reducing_qty(
                snapshot, position.symbol, reducing_side
            )
            if covered >= abs(position.qty) - 1e-8:
                continue  # protected/closing: may clear
            still_gapped.append(reason)
        if not still_gapped:
            return result
        if result.clean:
            # Re-persist PAUSED: the gap outlives this reconcile pass.
            self._store.save_account_state(
                account_id=snapshot.account_id,
                state="PAUSED",
                reasons=tuple(dict.fromkeys(still_gapped)),
                snapshot_version=snapshot.version,
                baseline_positions={p.symbol: p.qty for p in snapshot.positions},
            )
        return ReconciliationResult(
            state="PAUSED",
            reasons=tuple(dict.fromkeys(list(result.reasons) + still_gapped)),
            snapshot_version=result.snapshot_version,
        )

    # -- general protection coverage invariant (R09) ------------------------

    def _protection_coverage_gaps(self, snapshot: BrokerSnapshot) -> list[str]:
        """R09/N07: symbols whose program-built protection is unprovable while
        the position is still live.

        Only positions the durable ledger can prove came from THIS program's
        protected entry are judged — manual holdings are never forced under
        this rule. Protection obligation is proven EITHER by a registered
        protective child relation (a child was actually observed on the
        broker) OR — N07 — by a durable, FILLED program-owned opening parent
        whose intent payload required a broker stop (``risk_controls.
        stop_loss_price`` plus the protective entry contract: the payload's
        target side matches the parent's side). A parent that filled without
        its protective child ever appearing anywhere must therefore still be
        judged: missing proof is a gap, not a manual position.

        For each such live position the provable coverage is:

        - live program-owned protective children, grouped by their opening
          parent: a sibling stop/target pair contributes its LARGEST single
          remaining qty (never the sum — one lot is covered once);
        - plus any other program-owned live exposure-reducing order
          (a proven close in progress covers the exit).

        Terminal children cover nothing, and a held child of a not-yet-filled
        parent is not yet active protection. Insufficient coverage means the
        account must be PAUSED with a stable ``PROTECTION_GAP:`` reason until
        fresh broker facts prove the position closed or safely covered again.
        """
        gaps: list[str] = []
        rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        try:
            for row in self._store.list_all_orders():
                rows_by_symbol.setdefault(str(row.get("symbol") or "").upper(), []).append(row)
        except Exception as exc:
            # Ledger unavailable: coverage cannot be proven either way.
            return [
                f"PROTECTION_GAP: protection coverage could not be evaluated "
                f"because the durable ledger is unreadable: {exc}"
            ]
        for position in snapshot.positions:
            if abs(position.qty) <= 1e-9:
                continue
            symbol = position.symbol
            rows = rows_by_symbol.get(symbol, [])
            has_protected_entry = False
            for row in rows:
                try:
                    if self._store.protective_parent(row["order_id"]):
                        has_protected_entry = True
                        break
                except Exception:
                    continue
            if not has_protected_entry:
                # N07: no child relation was ever registered — prove the
                # protection obligation from the durable opening intent
                # itself. A FILLED program-owned opening parent whose payload
                # required a broker stop owes protection even if no child
                # was ever seen on any snapshot.
                opening_side = "buy" if position.qty > 0 else "sell"
                expected_target = "LONG" if position.qty > 0 else "SHORT"
                for row in rows:
                    if str(row.get("status") or "").upper() != "FILLED":
                        continue
                    if str(row.get("side") or "").lower() != opening_side:
                        continue
                    try:
                        intent = self._store.get_intent_for_order(row["order_id"]) or {}
                        payload = json.loads(intent.get("payload_json") or "{}")
                    except Exception:
                        continue
                    if payload.get("kind") == "liquidation":
                        continue  # exits never create a protection obligation
                    if str(payload.get("target_position") or "").upper() != expected_target:
                        continue  # not the opening leg that formed this position
                    controls = payload.get("risk_controls")
                    stop_price = (
                        controls.get("stop_loss_price")
                        if isinstance(controls, dict)
                        else getattr(controls, "stop_loss_price", None)
                    )
                    if not stop_price:
                        continue
                    has_protected_entry = True
                    break
            if not has_protected_entry:
                continue  # never a program-protected entry: manual position
            reducing_side = "sell" if position.qty > 0 else "buy"
            group_best: dict[str, float] = {}
            close_covered = 0.0
            for order in snapshot.orders:
                if order.symbol != symbol or order.side != reducing_side:
                    continue
                if broker_status_to_local(order.status) in {
                    "FILLED", "CANCELED", "REJECTED", "EXPIRED",
                }:
                    continue  # terminal children/orders cover nothing
                local = self._store.get_order_by_client(order.client_order_id)
                if not local or local.get("broker_order_id") != order.broker_order_id:
                    continue  # manual/unknown orders never prove protection
                remaining = max(0.0, float(order.qty or 0) - float(order.filled_qty or 0))
                if remaining <= 1e-9:
                    continue
                try:
                    parent_id = self._store.protective_parent(local["order_id"])
                except Exception:
                    parent_id = None
                if parent_id:
                    if str(order.status).lower() == "held":
                        parent_row = self._store.get_order(parent_id)
                        if not parent_row or str(parent_row.get("status") or "").upper() != "FILLED":
                            # A held child of a not-fully-filled parent is not
                            # yet active protection.
                            continue
                    group_best[parent_id] = max(group_best.get(parent_id, 0.0), remaining)
                else:
                    close_covered += remaining  # program-owned live reducing close
            covered = close_covered + sum(group_best.values())
            if covered >= abs(position.qty) - 1e-8:
                continue
            gaps.append(
                f"PROTECTION_GAP: {symbol} remains exposed with no proven live "
                f"protection or program-owned close covering the "
                f"{abs(position.qty):g}-share position; operator review required"
            )
        return gaps

    def _apply_protection_coverage(
        self, snapshot: BrokerSnapshot, result: Any,
    ) -> Any:
        """Recovery wrap-up (R09): PAUSED when proven protection is missing.

        Fresh broker facts decide every time: a closed position or restored
        sufficient coverage lets normal reconciliation report CLEAN, so a
        prior pause is never sticky beyond what the broker still shows.
        """
        gaps = self._protection_coverage_gaps(snapshot)
        if not gaps:
            return result
        merged = tuple(dict.fromkeys(list(result.reasons) + gaps))
        self._store.save_account_state(
            account_id=snapshot.account_id,
            state="PAUSED",
            reasons=merged,
            snapshot_version=snapshot.version,
            baseline_positions={p.symbol: p.qty for p in snapshot.positions},
        )
        return ReconciliationResult(
            state="PAUSED",
            reasons=merged,
            snapshot_version=snapshot.version,
        )


    def enforce_exit_deadlines(self) -> dict[str, Any]:
        """Exit due, fill-proven positions on scheduled checks under the account lock.

        Offline time, market closure and ambiguous broker state can delay exits.
        Any ownership/quantity conflict pauses instead of closing an unrelated lot.
        """
        from .lifecycle import due_positions
        results = []
        cancellation_calls = 0
        # N15: deadline maintenance facts for the round journal — counted at
        # the mutation boundaries, never inferred from ledger rows.
        deadline_submit_calls = 0
        deadline_submit_symbols: set[str] = set()
        deadline_has_unknown = False

        def _deadline_maintenance(*, paused: bool = False, error: str = "") -> dict[str, Any]:
            return _maintenance_summary(
                {
                    "submit_calls": deadline_submit_calls,
                    "cancel_calls": cancellation_calls,
                    "submitted_symbols": deadline_submit_symbols,
                    "has_unknown": deadline_has_unknown,
                },
                paused=paused, error=error,
            )

        try:
            if not due_positions(self._store, utc_now()):
                return {"success": True, "deadline_exits": [], "broker_calls": 0,
                        "deadline_maintenance": _deadline_maintenance()}
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(broker, expected_account_id=identity.account_id)
                snapshot, reconciliation = self._recover_locked(broker, snapshot)
                for symbol, due in due_positions(self._store, utc_now()).items():
                    position = snapshot.position(symbol)
                    if position is None:
                        continue
                    if abs(position.qty - due["qty"]) > 1e-8:
                        raise BrokerAuthorityError("Deadline position does not match durable filled lots")
                    from tradingagents.safety import get_safety_guard
                    guard = get_safety_guard()
                    verdict = (
                        guard.check_order(
                            symbol,
                            abs(position.market_value),
                            account={
                                "equity": snapshot.equity,
                                "last_equity": snapshot.last_equity,
                            },
                            position_value=abs(position.market_value),
                            risk_reducing=True,
                        )
                        if getattr(guard, "enabled", True)
                        else None
                    )
                    if verdict is not None and not verdict.allowed:
                        raise BrokerAuthorityError("Deadline exit blocked by safety policy: " +
                                                   "; ".join(getattr(verdict, "reasons", ())))
                    # Only cancel child protections proven to belong to our filled parents.
                    from alpaca.trading.requests import GetOrderByIdRequest
                    owned_children = set()
                    for lot in due["lots"]:
                        parent_id = lot["order"].get("broker_order_id")
                        if not parent_id:
                            raise BrokerAuthorityError("Deadline lot has no broker parent identity")
                        parent = broker.get_order_by_id(parent_id, filter=GetOrderByIdRequest(nested=True))
                        if str(getattr(parent, "id", "")) != parent_id:
                            raise BrokerAuthorityError("Protective parent identity mismatch")
                        owned_children.update(str(child.id) for child in (getattr(parent, "legs", None) or []))
                    closing_side = "sell" if position.qty > 0 else "buy"
                    conflicting = [o for o in snapshot.orders if o.symbol == position.symbol
                                   and broker_status_to_local(o.status) not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}]
                    if any(o.broker_order_id not in owned_children or o.side != closing_side for o in conflicting):
                        raise BrokerAuthorityError("Deadline exit conflicts with an order not proven to be its protection")
                    # F04 sequencing: durably commit the deadline close BEFORE
                    # canceling any protection, so a crash after cancellation
                    # leaves a recorded intent to complete the exit.
                    prepared_outbox = self._prepare_liquidation_outbox(
                        symbol,
                        decision_id=due["decision_id"],
                        quantity=abs(position.qty),
                        side=closing_side,
                    )
                    if not prepared_outbox.get("ok"):
                        raise BrokerAuthorityError(
                            f"deadline close durable commit failed: "
                            f"{prepared_outbox.get('error')}"
                        )
                    # F01: cancellation count is per symbol; the protection-gap
                    # evaluation below must never see another symbol's tally.
                    # R03: the same cascade/race-hardened cancel helper as the
                    # close flow — a sibling canceled by broker cascade must
                    # never abort the deadline close and leave a bare position.
                    canceled_here = 0
                    for order in conflicting:
                        snapshot, calls = self._cancel_protection_with_race_check(
                            broker, snapshot, order,
                            account_id=identity.account_id,
                        )
                        canceled_here += calls
                    cancellation_calls += canceled_here
                    current = snapshot.position(symbol)
                    # A protective child may fill during cancellation. Recompute lots.
                    self._reconcile_snapshot(broker, snapshot)
                    refreshed = due_positions(self._store, utc_now()).get(symbol)
                    if current is None:
                        # Position closed during cancellation: nothing to close.
                        self._abandon_prepared_rows(prepared_outbox)
                        continue

                    def _fail_with_gap_check(message: str) -> None:
                        # F01: after this symbol's protections were canceled, a
                        # verification failure must first consult the existing
                        # protection-gap invariant before any further action.
                        gap = self._evaluate_protection_gap(
                            broker, snapshot, symbol,
                            canceled_protections=canceled_here,
                        )
                        if gap is not None:
                            raise _DeadlineGapOutcome(gap)
                        raise BrokerAuthorityError(message)

                    if not refreshed or abs(current.qty - refreshed["qty"]) > 1e-8:
                        _fail_with_gap_check("Deadline position changed during protection cancellation")
                    expected_close_side = "sell" if current.qty > 0 else "buy"
                    prepared_qty = float(prepared_outbox["order_rows"][0]["quantity"] or 0)
                    if str(prepared_outbox["order_rows"][0]["side"] or "").lower() != expected_close_side:
                        _fail_with_gap_check("Deadline close side no longer matches the position")
                    if abs(abs(current.qty) - abs(prepared_qty)) > 1e-8:
                        _fail_with_gap_check("Deadline close quantity no longer matches the position")
                    spec = [{"role": "close", "side": closing_side, "quantity": abs(current.qty)}]
                    if not self._verified_reducing_exit(snapshot, symbol, spec):
                        _fail_with_gap_check("Deadline close not yet safe; protection cancellation may be pending")
                    result = self._liquidate_core(symbol, decision_id=due["decision_id"], _broker=broker,
                                                   _quantity=abs(current.qty), _side=closing_side,
                                                   _outbox=prepared_outbox)
                    results.append(result)
                    if int(result.get("broker_calls") or 0) > 0:
                        deadline_submit_calls += 1
                        deadline_submit_symbols.add(str(symbol).upper())
                    if str(result.get("status") or "").upper() == "UNKNOWN":
                        deadline_has_unknown = True
                    snapshot = capture_broker_snapshot(broker, expected_account_id=identity.account_id)
                    post = self._reconcile_snapshot(broker, snapshot)
                    if not result.get("success"):
                        # F01: a failed close on a now-bare position is exactly
                        # the protection-gap condition; decide it from fresh facts.
                        if result.get("status") != "UNKNOWN":
                            gap = self._evaluate_protection_gap(
                                broker, snapshot, symbol,
                                canceled_protections=canceled_here,
                            )
                            if gap is not None:
                                raise _DeadlineGapOutcome(gap)
                    if not result.get("success") or not post.clean:
                        return {"success": False, "paused": True, "deadline_exits": results,
                                "broker_calls": cancellation_calls + sum(r.get("broker_calls", 0) for r in results),
                                "error": "Deadline exit requires reconciliation before further trading",
                                "deadline_maintenance": _deadline_maintenance(
                                    paused=True,
                                    error="Deadline exit requires reconciliation before further trading")}
                return {"success": True, "deadline_exits": results,
                        "broker_calls": cancellation_calls + sum(r.get("broker_calls", 0) for r in results),
                        "deadline_maintenance": _deadline_maintenance()}
        except _DeadlineGapOutcome as gap_exc:
            out = {"success": False,
                   "deadline_exits": results,
                   "broker_calls": cancellation_calls + sum(r.get("broker_calls", 0) for r in results),
                   "deadline_maintenance": _deadline_maintenance(
                       paused=True, error=str(gap_exc.gap.get("error") or "protection gap"))}
            out.update(gap_exc.gap)
            return out
        except Exception as exc:
            return {"success": False, "paused": True, "fail_closed": True,
                    "deadline_exits": results,
                    "broker_calls": cancellation_calls + sum(r.get("broker_calls", 0) for r in results),
                    "error": f"Deadline enforcement paused: {exc}",
                    "deadline_maintenance": _deadline_maintenance(
                        paused=True, error=f"Deadline enforcement paused: {exc}")}

    # -- main entry ------------------------------------------------------

    def execute(
        self,
        *,
        trade_intent: Any,
        decision_id: Optional[str] = None,
        run_id: Optional[str] = None,
        dollar_amount: Optional[float] = None,
        allow_shorts: bool = False,
        risk_params: Optional[dict] = None,
        current_position: Optional[str] = None,
        can_submit: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        """Execute under one verified account lock and one authority snapshot.

        N03: ``can_submit`` (long-run callers pass their stop/window
        authority) is re-checked inside each exposure-opening submit after
        every blocking broker GET, immediately before the POST. ``None``
        keeps the legacy behavior for manual/WebUI callers.
        """
        intent_dict, err = validate_trade_intent(trade_intent)
        if err or intent_dict is None:
            return self._execute_core(
                trade_intent=trade_intent,
                decision_id=decision_id,
                run_id=run_id,
                dollar_amount=dollar_amount,
                allow_shorts=allow_shorts,
                risk_params=risk_params,
                current_position=current_position,
                can_submit=can_submit,
            )
        deadlines = self.enforce_exit_deadlines()
        if not deadlines.get("success"):
            return deadlines
        if deadlines.get("deadline_exits"):
            # The analysis position snapshot predates these exits. Require a fresh decision.
            return {"success": True, "hold": True, "deadline_exits": deadlines["deadline_exits"],
                    "broker_calls": deadlines.get("broker_calls", 0),
                    "reason": "Deadline exits processed; reanalyze before any new entry"}
        specs = _planned_order_specs(intent_dict, dollar_amount)
        # R04: a reversal intent (close the existing side + open the opposite
        # side) is executed as a CLOSE-ONLY phase in this call. Classifying it
        # as opening let the opening-only gates and the own-protection
        # conflict check block even the risk-reducing close leg.
        is_reversal = any(spec.get("role") == "close" for spec in specs) and any(
            spec.get("role") == "open" for spec in specs
        )
        opening_this_call = any(spec.get("role") == "open" for spec in specs) and not is_reversal
        if not specs:  # HOLD has no execution facts to gate.
            return self._execute_core(
                trade_intent=trade_intent,
                decision_id=decision_id,
                run_id=run_id,
                dollar_amount=dollar_amount,
                allow_shorts=allow_shorts,
                risk_params=risk_params,
                current_position=current_position,
                can_submit=can_submit,
            )
        if opening_this_call:
            from .policy import entry_check
            _, policy_error = entry_check(intent_dict)
            if policy_error:
                return {"success": False, "entry_policy_blocked": True, "fail_closed": True,
                        "broker_attempted": False, "broker_calls": 0, "error": policy_error}
        # Phase B corporate-action quarantine: a quarantined symbol takes no
        # new exposure (zero broker calls). Verified reducing exits keep the
        # Phase A path and are checked below under the account lock.
        if opening_this_call:
            quarantine = self._quarantine_rejection(intent_dict["symbol"])
            if quarantine:
                return {
                    "success": False,
                    "fail_closed": True,
                    "quarantined": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    **quarantine,
                }
        # Cheap deterministic blocks (kill switch / per-order cap) need no
        # broker identity call. Account-dependent checks run again below with
        # the authoritative snapshot.
        if specs and all(spec.get("role") == "open" for spec in specs):
            try:
                from tradingagents.safety import get_safety_guard

                guard = get_safety_guard()
                amount = float(dollar_amount or 0)
                verdict = guard.check_order(intent_dict["symbol"], amount)
            except Exception:
                verdict = None
            if verdict is not None and not verdict.allowed:
                reasons = list(getattr(verdict, "reasons", []) or ["blocked"])
                return {
                    "success": False,
                    "safety_blocked": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "error": " ".join(reasons),
                    "safety_reason_codes": [
                        str(code) for code in dict.fromkeys(
                            getattr(verdict, "reason_codes", []) or []
                        )
                    ],
                }
        try:
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                snapshot, reconciliation = self._recover_locked(broker, snapshot)
                opening_this_call = any(
                    spec.get("role") == "open" for spec in specs
                ) and not (
                    any(spec.get("role") == "close" for spec in specs)
                    and any(spec.get("role") == "open" for spec in specs)
                )
                closing_specs = [spec for spec in specs if spec.get("role") == "close"]
                if not reconciliation.clean and opening_this_call:
                    return self._paused_result(snapshot, reconciliation.reasons)
                canceled_protections = 0
                prepared_outbox: Optional[dict[str, Any]] = None
                if closing_specs and not opening_this_call:
                    symbol_for_close = intent_dict.get("symbol", "")
                    close_position = snapshot.position(symbol_for_close)
                    if close_position is not None:
                        # F04 sequencing: verify ownership, durably commit the
                        # close, and only then cancel proven protections. A
                        # durable commit failure leaves protections untouched.
                        self._verify_owned_close_protections(broker, snapshot, symbol_for_close)
                        close_side = "sell" if close_position.qty > 0 else "buy"
                        prepared_outbox = self._prepare_liquidation_outbox(
                            symbol_for_close,
                            # Must equal _execute_core's identity so the
                            # durable commit dedupes instead of duplicating.
                            decision_id=decision_id or canonical_decision_id(intent_dict),
                            run_id=run_id,
                            quantity=abs(close_position.qty),
                            side=close_side,
                        )
                        if not prepared_outbox.get("ok"):
                            return {
                                "success": False,
                                "fail_closed": True,
                                "broker_attempted": False,
                                "broker_calls": 0,
                                "error": prepared_outbox.get(
                                    "error", "durable commit failed"
                                ),
                            }
                    snapshot, canceled_protections = self._cancel_owned_close_protections(
                        broker, snapshot, symbol_for_close
                    )
                    if prepared_outbox is not None:
                        after_cancel = snapshot.position(symbol_for_close)
                        if after_cancel is None:
                            # Position closed during the cancellation race:
                            # no new close is needed and this is not a gap.
                            self._abandon_prepared_rows(prepared_outbox)
                            return {
                                "success": True,
                                "hold": True,
                                "no_close_needed": True,
                                "broker_attempted": False,
                                "broker_calls": canceled_protections,
                                "note": "position closed during protection cancellation",
                                "decision_id": prepared_outbox["decision_id"],
                            }
                        if not _position_unchanged(
                            after_cancel.qty, close_position.qty
                        ):
                            self._abandon_prepared_rows(prepared_outbox)
                            gap = self._evaluate_protection_gap(
                                broker, snapshot, symbol_for_close,
                                canceled_protections=canceled_protections,
                            )
                            if gap is not None:
                                return gap
                            paused = self._paused_result(
                                snapshot,
                                ["close position changed during protection cancellation"],
                            )
                            paused["broker_calls"] = canceled_protections
                            return paused
                if closing_specs and not self._verified_reducing_exit(
                    snapshot, intent_dict.get("symbol", ""), closing_specs
                ):
                    paused = self._paused_result(
                        snapshot,
                        list(reconciliation.reasons)
                        + ["close requires a fresh matching broker position and no conflicting close order"],
                    )
                    paused["broker_calls"] = canceled_protections
                    return paused
                # Phase C entry gate: in auto-screening mode only today's
                # validated Top20 may open exposure. Program-derived and
                # re-verified inside the single execution entry, so direct
                # callers and checkpoint resumes cannot bypass it. HOLD and
                # verified reducing exits are untouched; recovery of already
                # authorized orders has already happened above.
                if opening_this_call:
                    from tradingagents.screening.gate import check_entry_allowed

                    entry_block = check_entry_allowed(
                        intent_dict.get("symbol", ""),
                        config=_get_execution_config(),
                    )
                    if entry_block:
                        return {
                            "success": False,
                            "fail_closed": True,
                            "entry_gate_blocked": True,
                            "broker_attempted": False,
                            "broker_calls": 0,
                            "error": entry_block,
                            "trade_intent": intent_dict,
                        }
                position = snapshot.position(intent_dict["symbol"])
                target = str(intent_dict.get("target_position") or "").upper()
                # Phase B note: the Phase A "already holds the target
                # position" shortcut was removed — an opening order on a held
                # symbol is an exposure increase governed by the deterministic
                # caps (clipped to headroom) instead of an implicit HOLD.
                quote = (
                    validate_quote(self._quote_factory(intent_dict["symbol"]), intent_dict["symbol"])
                    if opening_this_call else None
                )
                verified_position = (
                    "LONG" if position and position.qty > 0 else
                    "SHORT" if position and position.qty < 0 else "NEUTRAL"
                )
                result = self._execute_core(
                    trade_intent=trade_intent,
                    decision_id=decision_id,
                    run_id=run_id,
                    dollar_amount=dollar_amount,
                    allow_shorts=allow_shorts,
                    risk_params=risk_params,
                    current_position=verified_position,
                    can_submit=can_submit,
                    _broker=broker,
                    _snapshot=snapshot,
                    _quote=quote,
                    # R04: reversal runs its close phase only this call; the
                    # opposite open stays deferred to a later fresh analysis.
                    _reversal_close_only=is_reversal,
                )
                result["broker_calls"] = result.get("broker_calls", 0) + canceled_protections
                result["preflight_snapshot_version"] = snapshot.version
                try:
                    after = capture_broker_snapshot(
                        broker, expected_account_id=snapshot.account_id
                    )
                    post = self._reconcile_snapshot(broker, after)
                    # N07: a filled protected opening without provable live
                    # coverage must PAUSE the account on the execution path
                    # too, not only during startup recovery.
                    post = self._apply_protection_coverage(after, post)
                    result["snapshot_version"] = after.version
                    result["account_execution_state"] = post.state
                    result["reconciliation_reasons"] = list(post.reasons)
                    if not post.clean:
                        result["paused"] = True
                except BrokerAuthorityError as exc:
                    self._store.save_account_state(
                        account_id=snapshot.account_id,
                        state="PAUSED",
                        reasons=(f"post-order reconciliation failed: {exc}",),
                        snapshot_version=snapshot.version,
                        baseline_positions={p.symbol: p.qty for p in snapshot.positions},
                    )
                    result["account_execution_state"] = "PAUSED"
                    result["reconciliation_reasons"] = [str(exc)]
                    result["paused"] = True
                # F04: enforce the protection-gap invariant on fresh facts
                # when this call canceled protections.
                if canceled_protections > 0 and result.get("status") != "UNKNOWN":
                    try:
                        gap_snapshot = capture_broker_snapshot(
                            broker, expected_account_id=snapshot.account_id
                        )
                        self._reconcile_snapshot(broker, gap_snapshot)
                    except BrokerAuthorityError:
                        gap_snapshot = snapshot
                    gap = self._evaluate_protection_gap(
                        broker, gap_snapshot, intent_dict.get("symbol", ""),
                        canceled_protections=canceled_protections,
                    )
                    if gap is not None:
                        result.update(gap)
                return result
        except AccountLockBusy as exc:
            return {
                "success": False, "paused": True, "busy": True,
                "broker_attempted": False, "broker_calls": 0, "error": str(exc),
            }
        except BrokerAuthorityError as exc:
            return {
                "success": False, "paused": True, "fail_closed": True,
                "broker_attempted": False, "broker_calls": 0, "error": str(exc),
            }
        except Exception as exc:
            return {
                "success": False, "paused": True, "fail_closed": True,
                "broker_attempted": False, "broker_calls": 0,
                "error": f"broker authority unavailable: {exc}",
            }

    def _execute_core(
        self,
        *,
        trade_intent: Any,
        decision_id: Optional[str] = None,
        run_id: Optional[str] = None,
        dollar_amount: Optional[float] = None,
        allow_shorts: bool = False,
        risk_params: Optional[dict] = None,
        current_position: Optional[str] = None,
        can_submit: Optional[Callable[[], bool]] = None,
        _broker: Any = None,
        _snapshot: Optional[BrokerSnapshot] = None,
        _quote: Any = None,
        _reversal_close_only: bool = False,
    ) -> dict[str, Any]:
        intent_dict, err = validate_trade_intent(trade_intent)
        if err or intent_dict is None:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": err or "invalid trade intent",
            }
        symbol = intent_dict.get("symbol", "")
        action = intent_dict.get("action", "")
        target_position = intent_dict.get("target_position", "")
        if not symbol or not action:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": "trade intent missing symbol/action",
                "trade_intent": intent_dict,
            }

        # Deterministic short guards stay fail-closed (no broker calls).
        is_crypto = "/" in str(symbol).upper()
        if str(action).upper() == "SHORT" and (is_crypto or not allow_shorts):
            reason = (
                "Crypto short exposure is not supported by Alpaca spot trading"
                if is_crypto
                else "Short exposure is disabled for this session"
            )
            # Still persist the durable intent for audit, then mark canceled.
            did = decision_id or canonical_decision_id(intent_dict)
            specs = _planned_order_specs(intent_dict, dollar_amount)
            order_payloads = [
                {
                    "client_order_id": client_order_id_for(
                        did, symbol, s["side"], role=s["role"], seq=s["seq"]
                    ),
                    "symbol": symbol,
                    "side": s["side"],
                    "quantity": s["quantity"],
                    "notional": s["notional"],
                }
                for s in specs
            ]
            try:
                intent_row, order_rows, _ = self._store.create_outbox(
                    decision_id=did,
                    run_id=run_id,
                    symbol=symbol,
                    action=str(action),
                    target_position=str(target_position),
                    payload_json=json.dumps(intent_dict, sort_keys=True, default=str),
                    orders=order_payloads,
                )
            except Exception as exc:
                return {
                    "success": False,
                    "fail_closed": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "error": f"durable commit failed: {exc}",
                    "trade_intent": intent_dict,
                }
            for o in order_rows:
                self._store.transition_order(o["order_id"], "CANCELED")
            self._store.update_intent_state(intent_row["intent_id"], "CANCELED")
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": reason,
                "trade_intent": intent_dict,
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
            }

        did = decision_id or canonical_decision_id(intent_dict)
        signal = str(action or "").upper()
        warnings: list[str] = []
        risk_sizing_info: Optional[dict] = None
        risk_stop_price: Optional[float] = None
        effective_amount = dollar_amount

        # Deterministic risk sizing (moved from the legacy AlpacaUtils path so
        # the single entry keeps it): the LLM decided direction; position size
        # is recomputed mathematically for position-opening actions when the
        # caller opts in with risk_params.
        live_position = current_position or intent_dict.get("current_position")
        target_position_open = {
            "BUY": "LONG",
            "LONG": "LONG",
            "SHORT": "SHORT",
        }.get(signal)
        # R04: in a reversal close-phase call the specs still carry the
        # opposite open leg (durable outbox identity is preserved), but this
        # call never adds exposure — every opening-only path below stays off.
        opens_new_exposure = (
            any(s.get("role") == "open" for s in _planned_order_specs(intent_dict, dollar_amount))
            and not _reversal_close_only
        )
        if risk_params is not None and opens_new_exposure:
            order_side = "sell" if signal == "SHORT" else "buy"
            try:
                from tradingagents.dataflows.alpaca_utils import AlpacaUtils

                sizing = AlpacaUtils.compute_risk_sized_amount(
                    symbol=symbol,
                    confidence=str(intent_dict.get("confidence") or "unknown"),
                    requested_notional=dollar_amount,
                    risk_params=risk_params,
                    side=order_side,
                    authoritative_snapshot=_snapshot,
                    authoritative_quote=_quote,
                    stop_loss_price=(intent_dict.get("risk_controls") or {}).get("stop_loss_price"),
                )
            except Exception as exc:
                return {"success": False, "fail_closed": True, "broker_attempted": False,
                        "broker_calls": 0, "error": f"Risk sizing unavailable: {exc}"}
            else:
                if not sizing.approved:
                    return {
                        "success": False,
                        "broker_attempted": False,
                        "broker_calls": 0,
                        "error": f"Trade blocked by deterministic risk engine: {sizing.reason}",
                        "trade_intent": intent_dict,
                        "intent_warnings": warnings,
                        "risk_sizing": {"applied": True, **sizing.to_dict()},
                    }
                effective_amount = sizing.notional
                risk_stop_price = sizing.stop_loss_price
                risk_sizing_info = {"applied": True, **sizing.to_dict()}

        # Broker-side protective legs (bracket/OTO) for opening orders only.
        protective_prices = (
            _resolve_protective_prices(intent_dict, signal, is_crypto, warnings)
            if opens_new_exposure
            else None
        )
        if risk_stop_price and not is_crypto:
            try:
                from tradingagents.dataflows.config import get_config

                bracket_enabled = get_config().get(
                    "protective_bracket_orders_enabled", True
                )
            except Exception:
                bracket_enabled = True
            controls = intent_dict.get("risk_controls") or {}
            target_price = (
                (protective_prices or {}).get("take_profit_price")
                or (controls.get("take_profit_price") if isinstance(controls, dict) else None)
            )
            stop_is_consistent = not target_price or (
                signal in {"BUY", "LONG"} and risk_stop_price < target_price
            ) or (signal == "SHORT" and target_price < risk_stop_price)
            if bracket_enabled and stop_is_consistent:
                protective_prices = dict(protective_prices or {})
                if not protective_prices.get("stop_loss_price"):
                    protective_prices["stop_loss_price"] = float(risk_stop_price)

        if opens_new_exposure:
            from .policy import entry_check
            if _quote is None or _snapshot is None:
                return {"success": False, "fail_closed": True, "broker_calls": 0,
                        "broker_attempted": False, "error": "Execution policy requires authoritative quote and account"}
            execution_price = _quote.ask_price if signal in {"BUY", "LONG"} else _quote.bid_price
            proposed_specs = _planned_order_specs(intent_dict, effective_amount)
            requested = sum(float(s.get("notional") or float(s.get("quantity") or 0) * _quote.price)
                            for s in proposed_specs if s.get("role") == "open")
            effective_amount, policy_error = entry_check(intent_dict, price=execution_price or float("nan"),
                                                        equity=_snapshot.equity, requested=requested)
            if policy_error or not protective_prices or not protective_prices.get("stop_loss_price"):
                return {"success": False, "fail_closed": True, "entry_policy_blocked": True,
                        "broker_attempted": False, "broker_calls": 0,
                        "error": policy_error or "Broker-side stop-loss is required for opening exposure"}

        specs = _planned_order_specs(intent_dict, effective_amount)
        if opens_new_exposure:
            for spec in specs:
                if spec.get("role") == "open":
                    spec["quantity"] = None  # clipped notional is authoritative
        if _snapshot is not None:
            verified = _snapshot.position(symbol)
            for spec in specs:
                if spec.get("role") == "close" and verified is not None:
                    spec["quantity"] = abs(verified.qty)
                    spec["notional"] = None
                    spec["side"] = "sell" if verified.qty > 0 else "buy"
        # F05: a close-then-open reversal completes ONLY its close phase in
        # this call (see the submit loop below). Computed once here because
        # the R14 precondition below must never block a reversal's
        # risk-reducing close phase.
        is_reversal_flip = any(
            spec.get("role") == "close" for spec in specs
        ) and any(spec.get("role") == "open" for spec in specs)
        # R14: position-transition precondition. The decision was made
        # against the intent's current_position facts; if the fresh broker
        # snapshot now shows a different side, the decision is stale. It is
        # never replayed as-is: no automatic flip reinterpretation, no
        # quantity rewrite to "just flip it" — fail closed with zero broker
        # calls and require a fresh analysis. Verified reducing exits
        # (closes, reversal close phases) are not gated by this rule.
        if (
            not is_reversal_flip
            and any(spec.get("role") == "open" for spec in specs)
            and _snapshot is not None
        ):
            live_position = _snapshot.position(symbol)
            actual_side = (
                "LONG" if live_position is not None and live_position.qty > 0
                else "SHORT" if live_position is not None and live_position.qty < 0
                else "NEUTRAL"
            )
            intent_side = str(intent_dict.get("current_position") or "").upper()
            if intent_side != actual_side:
                return {
                    "success": False,
                    "fail_closed": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "stale_position_transition": True,
                    "error": (
                        f"stale position transition: decision assumed "
                        f"current_position={intent_side or 'unknown'} but the "
                        f"broker now holds {actual_side}; fresh analysis is "
                        "required before any opening order"
                    ),
                    "trade_intent": intent_dict,
                }

        # Phase B deterministic exposure caps: clip the increasing leg of
        # every exposure-adding order (fresh opens AND increases of an
        # existing position) to the single canonical symbol cap, the sector
        # cap, the gross cap and cash — with outstanding open orders counted
        # against headroom. Verified reducing exits never pass through here.
        exposure_check_info: Optional[dict] = None
        adds_exposure = target_position_open is not None and not _reversal_close_only
        if adds_exposure and _snapshot is not None and any(
            spec.get("role") == "open" for spec in specs
        ):
            cap_result = _evaluate_opening_caps(
                symbol=symbol,
                specs=specs,
                snapshot=_snapshot,
                quote=_quote,
                intent_dict=intent_dict,
                quote_factory=self._quote_factory,
            )
            if not cap_result.approved:
                return {
                    "success": False,
                    "fail_closed": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "error": f"Exposure cap rejected the order: {cap_result.reason}",
                    "trade_intent": intent_dict,
                    "exposure_check": cap_result.to_dict(),
                    "intent_warnings": warnings,
                }
            approved_total = cap_result.notional
            opening_specs = [s for s in specs if s.get("role") == "open"]
            # Normalize quantity-based opening legs to a notional value with
            # the execution quote so the approved total applies uniformly.
            quote_price = float(_quote.price) if _quote is not None else None
            for open_spec in opening_specs:
                if open_spec.get("notional") is None and open_spec.get("quantity") and quote_price:
                    open_spec["notional"] = float(open_spec["quantity"]) * quote_price
            leg_totals = [
                float(s.get("notional") or 0.0) for s in opening_specs
            ]
            total_proposed = sum(leg_totals)
            if total_proposed > 0 and approved_total < total_proposed:
                scale = approved_total / total_proposed
                for open_spec in opening_specs:
                    if open_spec.get("notional") is None:
                        continue
                    original = float(open_spec["notional"])
                    clipped_leg = round(original * scale, 2)
                    if clipped_leg < original:
                        warnings.append(
                            f"Opening notional clipped from ${original:,.2f} to "
                            f"${clipped_leg:,.2f} by deterministic exposure caps."
                        )
                        open_spec["notional"] = clipped_leg
                        if open_spec.get("quantity") and quote_price:
                            open_spec["quantity"] = None  # notional governs now
            exposure_check_info = cap_result.to_dict()

        # Attach protective legs to the opening leg only (closes carry nothing).
        if protective_prices:
            for s in specs:
                if s.get("role") == "open":
                    s["stop_loss_price"] = protective_prices.get("stop_loss_price")
                    s["take_profit_price"] = protective_prices.get("take_profit_price")
        # Bracket/OTO legs need whole-share qty: resolve from the latest quote.
        for s in specs:
            if (
                s.get("role") == "open"
                and (s.get("stop_loss_price") or s.get("take_profit_price"))
                and not is_crypto
            ):
                if _quote is not None:
                    qty_int = int(float(s.get("notional") or effective_amount or 0.0) / float(intent_dict["entry_policy"]["maximum_price"]))
                    qty_int = qty_int if qty_int >= 1 else None
                else:
                    qty_int = _resolve_qty(symbol, float(s.get("notional") or effective_amount or 0.0))
                if qty_int:
                    s["quantity"] = float(qty_int)
                    s["notional"] = None
        order_payloads = [
            {
                "client_order_id": client_order_id_for(
                    did, symbol, s["side"], role=s["role"], seq=s["seq"]
                ),
                "symbol": symbol,
                "side": s["side"],
                "quantity": s["quantity"],
                "notional": s["notional"],
            }
            for s in specs
        ]
        # Durable outbox COMMIT happens before any broker call.
        try:
            intent_row, order_rows, created = self._store.create_outbox(
                decision_id=did,
                run_id=run_id,
                symbol=symbol,
                action=str(action),
                target_position=str(target_position),
                payload_json=json.dumps(intent_dict, sort_keys=True, default=str),
                orders=order_payloads,
            )
        except Exception as exc:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": f"durable commit failed: {exc}",
                "trade_intent": intent_dict,
            }

        # Idempotent replay: same decision_id returns the existing intent
        # without new broker calls when its orders are already non-PENDING.
        if not created:
            existing = self._store.list_orders_for_intent(intent_row["intent_id"])
            if existing and all(
                (o.get("status") or "").upper() != "PENDING" for o in existing
            ):
                return {
                    "success": True,
                    "deduped": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                    "trade_intent": intent_dict,
                    "orders": existing,
                }
            order_rows = existing

        if not order_rows:
            self._store.update_intent_state(intent_row["intent_id"], "COMPLETED")
            return {
                "success": True,
                "hold": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
                "trade_intent": intent_dict,
                "orders": [],
            }

        # Existing deterministic safety gate (same guard as legacy path).
        try:
            from tradingagents.safety import get_safety_guard

            guard = get_safety_guard()
            guard_error = None
        except Exception as exc:
            guard = None
            guard_error = str(exc)

        broker_calls = 0
        results: list[dict[str, Any]] = []
        close_leg_failed = False
        # F05: a close-then-open reversal completes ONLY its close phase in
        # this call. The opposite open leg is never submitted here — sizing,
        # caps and quotes were computed from pre-close facts, and an accepted
        # close is not a proven flat position. A later fresh analysis decides
        # the new direction from a fresh broker snapshot. (is_reversal_flip
        # was computed above, before the outbox commit, for the R14 gate.)
        for spec, orow in zip(specs, order_rows):
            if (orow.get("status") or "").upper() != "PENDING":
                results.append(
                    {"client_order_id": orow["client_order_id"], "deduped": True}
                )
                continue
            if is_reversal_flip and spec["role"] == "open":
                self._store.transition_order(orow["order_id"], "CANCELED")
                results.append(
                    {
                        "client_order_id": orow["client_order_id"],
                        "skipped": True,
                        "reversal_open_deferred": True,
                        "error": (
                            "skipped: reversal close was submitted; a fresh "
                            "analysis on fresh broker facts is required "
                            "before the opposite open"
                        ),
                    }
                )
                continue
            if close_leg_failed and spec["role"] == "open":
                # A close-then-open flip whose close leg failed must not open
                # new exposure on top of the unclosed position.
                self._store.transition_order(orow["order_id"], "CANCELED")
                results.append(
                    {
                        "client_order_id": orow["client_order_id"],
                        "skipped": True,
                        "error": "skipped: close leg failed, refusing to open new exposure",
                    }
                )
                continue
            risk_reducing = spec["role"] == "close"
            amount = float(
                spec.get("notional") or effective_amount or dollar_amount or 0.0
            )
            if guard_error is not None:
                self._store.transition_order(orow["order_id"], "CANCELED")
                results.append(
                    {
                        "client_order_id": orow["client_order_id"],
                        "safety_blocked": True,
                        "error": f"safety policy unavailable: {guard_error}",
                    }
                )
                continue
            if guard is not None and getattr(guard, "enabled", True):
                try:
                    verdict = guard.check_order(
                        symbol,
                        amount,
                        account=(
                            {
                                "equity": _snapshot.equity,
                                "last_equity": _snapshot.last_equity,
                            }
                            if _snapshot
                            else None
                        ),
                        position_value=(
                            abs(_snapshot.position(symbol).market_value)
                            if _snapshot and _snapshot.position(symbol) else 0.0
                        ),
                        risk_reducing=risk_reducing,
                    )
                except Exception as exc:
                    verdict = None
                    safety_error = str(exc)
                else:
                    safety_error = None
                if safety_error is not None or verdict is None or not verdict.allowed:
                    self._store.transition_order(orow["order_id"], "CANCELED")
                    results.append(
                        {
                            "client_order_id": orow["client_order_id"],
                            "safety_blocked": True,
                            "error": (
                                f"safety policy failed: {safety_error}"
                                if safety_error is not None
                                else " ".join(getattr(verdict, "reasons", ["blocked"]))
                            ),
                            "safety_reason_codes": [
                                str(code) for code in dict.fromkeys(
                                    getattr(verdict, "reason_codes", []) or []
                                )
                            ] if safety_error is None else [],
                        }
                    )
                    continue
            # PENDING -> SUBMITTING inside the durable ledger.
            ok, orow = self._store.transition_order(orow["order_id"], "SUBMITTING")
            if not ok:
                results.append(
                    {"client_order_id": orow["client_order_id"], "error": "state conflict"}
                )
                continue
            self._store.update_intent_state(intent_row["intent_id"], "SUBMITTING")
            submit_outcome = self._submit_one(
                order_row=orow, spec=spec, symbol=symbol, intent_dict=intent_dict,
                broker=_broker, _snapshot=_snapshot, _quote=_quote,
                can_submit=can_submit,
            )
            broker_calls += int(submit_outcome.get("broker_calls", 0))
            if (
                guard is not None
                and getattr(guard, "enabled", True)
                and int(submit_outcome.get("broker_calls", 0)) > 0
            ):
                # Feed the consecutive-rejection circuit breaker with real
                # broker POST outcomes (safety-blocked rows never reach here).
                try:
                    guard.record_order_result(bool(submit_outcome.get("ok")))
                except Exception:
                    pass
            if spec["role"] == "close" and not submit_outcome.get("ok"):
                close_leg_failed = True
            results.append(submit_outcome)

        final_orders = self._store.list_orders_for_intent(intent_row["intent_id"])
        statuses = {(o.get("status") or "").upper() for o in final_orders}
        if statuses and statuses <= {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            self._store.update_intent_state(intent_row["intent_id"], "COMPLETED")
        # F05: deferred reversal open legs are a planned outcome, not a
        # failure — judge success on the close phase only.
        deferral_results = [
            r for r in results if r.get("reversal_open_deferred")
        ]
        judged_results = [
            r for r in results if not r.get("reversal_open_deferred")
        ]
        success = all(
            r.get("ok") or r.get("deduped") for r in judged_results
        ) and not any(r.get("safety_blocked") for r in judged_results)
        # Safety blocks fail the overall call but never touched the broker.
        broker_attempted = broker_calls > 0
        out: dict[str, Any] = {
            "success": bool(success),
            "broker_attempted": broker_attempted,
            "broker_calls": broker_calls,
            "intent_id": intent_row["intent_id"],
            "decision_id": did,
            "trade_intent": intent_dict,
            "orders": final_orders,
            "results": results,
        }
        if any(r.get("safety_blocked") for r in results):
            out["safety_blocked"] = True
            first_block = next(
                (r for r in results if r.get("safety_blocked")), {}
            )
            if first_block.get("error"):
                out["error"] = first_block["error"]
        # N08: propagate stable safety reason codes (deduped, order kept) so
        # the long-run hard-stop can distinguish circuit breakers from
        # single-order refusals without parsing English reasons.
        safety_codes: list[str] = []
        for r in results:
            for code in (r.get("safety_reason_codes") or []):
                if code not in safety_codes:
                    safety_codes.append(str(code))
        if safety_codes:
            out["safety_reason_codes"] = safety_codes
        if is_reversal_flip and deferral_results and success:
            # F05: the close phase completed; the opposite open must come
            # from a later fresh analysis against fresh broker facts. This
            # never claims the target position was achieved.
            out["hold"] = True
            out["reanalysis_required"] = True
            out["reason"] = (
                "reversal close submitted; fresh analysis required before "
                "opposite open"
            )
        if exposure_check_info is not None:
            out["exposure_check"] = exposure_check_info
        if any((r.get("status") or "") == "UNKNOWN" for r in results):
            out["has_unknown"] = True
        if warnings:
            out["intent_warnings"] = warnings
        if risk_sizing_info is not None:
            out["risk_sizing"] = risk_sizing_info
        # Broker protective-order status for callers/tests (advisory_only until
        # a leg actually rides a bracket/OTO submit).
        protective_status = "advisory_only"
        for r in results:
            if r.get("order_class") == "bracket":
                protective_status = "submitted_bracket"
            elif r.get("order_class") == "oto":
                protective_status = "submitted_oto"
            elif r.get("protective_fallback"):
                protective_status = "bracket_rejected_fallback_plain"
                warnings.append(
                    "Protective order submission was rejected by the broker; "
                    f"entered with a plain market order instead ({r.get('protective_error')})."
                )
        if protective_status == "advisory_only" and opens_new_exposure:
            controls = intent_dict.get("risk_controls") or {}
            if isinstance(controls, dict):
                needs_controls = bool(
                    controls.get("required_controls")
                    or controls.get("stop_loss")
                    or controls.get("take_profit")
                )
            else:
                needs_controls = False
            if needs_controls:
                warnings.append(
                    "Broker stop-loss/take-profit orders were not submitted; controls remain advisory."
                )
        out["protective_order_status"] = protective_status
        if warnings and "intent_warnings" not in out:
            out["intent_warnings"] = warnings
        return out

    @staticmethod
    def _paused_result(snapshot: BrokerSnapshot, reasons: Any) -> dict[str, Any]:
        reason_list = list(reasons) or ["account reconciliation is not CLEAN"]
        return {
            "success": False,
            "paused": True,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "account_execution_state": "PAUSED",
            "snapshot_version": snapshot.version,
            "reconciliation_reasons": reason_list,
            "error": "; ".join(reason_list),
        }

    @staticmethod
    def _verified_reducing_exit(
        snapshot: BrokerSnapshot, symbol: str, specs: list[dict[str, Any]]
    ) -> bool:
        position = snapshot.position(symbol)
        if position is None or abs(position.qty) <= 1e-9 or not specs:
            return False
        expected_side = "sell" if position.qty > 0 else "buy"
        for spec in specs:
            if spec.get("role") != "close" or str(spec.get("side")).lower() != expected_side:
                return False
            quantity = spec.get("quantity")
            if quantity is not None and float(quantity) > abs(position.qty) + 1e-9:
                return False
        return not any(
            order.symbol == position.symbol
            and order.side == expected_side
            and broker_status_to_local(order.status) not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
            for order in snapshot.orders
        )

    def _lookup_for_recovery(self, broker: Any, client_order_id: str) -> Any:
        getters = [
            getattr(broker, name, None)
            for name in ("get_order_by_client_order_id", "get_order_by_client_id")
        ]
        getters = [getter for getter in getters if callable(getter)]
        if not getters:
            raise BrokerAuthorityError("broker has no client_order_id lookup API")
        getter = getters[0]
        last: Optional[BaseException] = None
        explicit_not_found = 0
        for attempt in range(3):
            try:
                found = getter(client_order_id)
                if found is not None:
                    return found
                explicit_not_found += 1
            except Exception as exc:
                last = exc
                text = f"{type(exc).__name__} {exc}".lower()
                status_code = getattr(exc, "status_code", None)
                if status_code == 404 or "404" in text or "not found" in text or "does not exist" in text:
                    explicit_not_found += 1
                elif attempt == 2:
                    raise BrokerAuthorityError(
                        f"order lookup remains uncertain: {exc}"
                    ) from exc
            if attempt < 2:
                import time

                time.sleep(0.05 * (2**attempt))
        if explicit_not_found == 3:
            return None
        raise BrokerAuthorityError(f"order lookup remains uncertain: {last}")

    def _adopt_recovery_order(self, local: dict[str, Any], found: Any) -> None:
        broker_id = getattr(found, "id", None) or (
            found.get("id") if isinstance(found, dict) else None
        )
        status = getattr(found, "status", None) or (
            found.get("status") if isinstance(found, dict) else None
        )
        filled = getattr(found, "filled_qty", None)
        if isinstance(found, dict):
            filled = found.get("filled_qty", filled)
        if not broker_id or not status:
            raise BrokerAuthorityError("recovered broker order identity is incomplete")
        self._store.sync_order_from_broker(
            local["order_id"],
            broker_status_to_local(status),
            broker_order_id=str(broker_id),
            filled_qty=float(local.get("filled_qty") or 0),
        )

    @staticmethod
    def _recovery_crossing_block(
        snapshot: BrokerSnapshot, symbol: str, opening_side: str,
        authorized_close: bool,
    ) -> Optional[str]:
        """N02: fixed fail-closed reason when a recovery resubmit would cross
        or reverse the FRESH broker position, else None.

        Deliberately narrower than execute()'s R14 rule: recovery only blocks
        openings that would trade THROUGH the live position (buy against a
        fresh SHORT, sell against a fresh LONG). Same-direction increases
        (buy onto LONG, sell onto SHORT) and canonical authorized closes
        stay on the existing recovery path, where the current allow_shorts
        policy, caps, entry policy and protection gates still bind them.
        """
        if authorized_close:
            return None
        position = snapshot.position(symbol)
        if position is None or abs(position.qty) <= 1e-9:
            return None  # fresh NEUTRAL: the opening proceeds through the other gates
        fresh_side = "LONG" if position.qty > 0 else "SHORT"
        if opening_side == "buy" and fresh_side == "SHORT":
            return (
                f"stale position transition: recovery resubmit buy for {symbol} "
                f"would cross/reverse the fresh broker SHORT position "
                f"({position.qty:g} shares); fresh analysis is required before "
                "any opening order"
            )
        if opening_side == "sell" and fresh_side == "LONG":
            return (
                f"stale position transition: recovery resubmit sell for {symbol} "
                f"would cross/reverse the fresh broker LONG position "
                f"({position.qty:g} shares); fresh analysis is required before "
                "any opening order"
            )
        return None

    def _resubmit_recovered(
        self, broker: Any, local: dict[str, Any], snapshot: BrokerSnapshot,
        can_submit: Optional[Callable[[], bool]] = None,
        maintenance: Optional[dict[str, Any]] = None,
    ) -> None:
        if local.get("quantity") is None and local.get("notional") is None:
            raise BrokerAuthorityError(
                f"non-idempotent close cannot be auto-resubmitted: {local['client_order_id']}"
            )
        request = _build_market_request(
            local["symbol"], local["side"], local.get("notional"),
            local.get("quantity"), local["client_order_id"],
        )
        if request is None:
            raise BrokerAuthorityError(f"recovery order has no valid size: {local['client_order_id']}")
        quote = validate_quote(self._quote_factory(local["symbol"]), local["symbol"])
        position = snapshot.position(local["symbol"])
        side = str(local["side"]).lower()
        intent = self._store.get_intent_for_order(local["order_id"]) or {}
        payload = json.loads(intent.get("payload_json") or "{}")
        closing_ids = {
            client_order_id_for(intent.get("decision_id", ""), local["symbol"], spec["side"],
                                role="close", seq=spec["seq"])
            for spec in _planned_order_specs(payload, None) if spec.get("role") == "close"
        }
        authorized_close = payload.get("kind") == "liquidation" or local["client_order_id"] in closing_ids
        risk_reducing = bool(
            authorized_close and position
            and local.get("quantity") is not None
            and 0 < float(local["quantity"]) <= abs(position.qty) + 1e-8
            and ((position.qty > 0 and side == "sell") or (position.qty < 0 and side == "buy"))
        )
        if risk_reducing and not self._verified_reducing_exit(snapshot, local["symbol"],
                [{"role": "close", "side": side, "quantity": local["quantity"]}]):
            raise BrokerAuthorityError("Recovery close conflicts with a live order")
        # N02: a stale opening assumption must never trade THROUGH the fresh
        # broker position. Fail closed BEFORE any mutation or protective-leg
        # work: the row is provably POST-free, so it becomes CANCELED.
        crossing = self._recovery_crossing_block(
            snapshot, local["symbol"], side, authorized_close
        )
        if crossing:
            self._store.transition_order(local["order_id"], "CANCELED")
            raise StaleRecoveryPositionError(
                f"recovery resubmit blocked for {local['symbol']}: {crossing}"
            )
        # N01: re-derive the CURRENT short-exposure policy from the live
        # config — never from the persisted payload's old
        # execution_constraints. Only a genuinely about-to-POST opening sell
        # is gated here; canonical close buys (covering an existing short)
        # and broker-existing adoptions never reach this block.
        if not authorized_close and side == "sell":
            is_crypto_symbol = "/" in str(local.get("symbol") or "").upper()
            current_allow_shorts = bool(_get_execution_config().get("allow_shorts", False))
            if is_crypto_symbol or not current_allow_shorts:
                reason = (
                    "Crypto short exposure is not supported by Alpaca spot trading"
                    if is_crypto_symbol
                    else "Short exposure is disabled for this session"
                )
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(
                    f"recovery resubmit blocked by the current short-exposure "
                    f"policy for {local['symbol']}: {reason}"
                )
        # Phase C: exposure-increasing US-equity recovery resubmits must
        # re-run today's program-derived entry gate (trading day + validated
        # current Top20) before any broker POST. Broker-existing orders are
        # adopted upstream without reaching here; verified risk-reducing
        # exits and crypto keep the Phase A path. Blocked rows become
        # CANCELED (durable, non-retryable) with an operator-visible reason.
        effective_notional = local.get("notional")
        effective_quantity = local.get("quantity")
        if not risk_reducing and "/" not in str(local.get("symbol") or "").upper():
            from tradingagents.screening.gate import check_entry_allowed

            entry_block = check_entry_allowed(
                str(local.get("symbol") or ""),
                config=_get_execution_config(),
            )
            if entry_block:
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(
                    f"recovery resubmit blocked by Phase C entry gate for {local['symbol']}: {entry_block}"
                )
        # Phase B: recovery resubmits recompute every gate from the fresh
        # snapshot — quarantine and exposure caps included. Headroom is never
        # reused from the original submit; when the caps allow less than the
        # original size, the resubmit is clipped to the allowed notional.
        if not risk_reducing:
            quarantine = self._quarantine_rejection(local["symbol"])
            if quarantine:
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(
                    f"recovery resubmit blocked for {local['symbol']}: {quarantine['error']}"
                )
            try:
                cap_result = _evaluate_opening_caps(
                    symbol=local["symbol"],
                    specs=[
                        {
                            "role": "open",
                            "notional": local.get("notional"),
                            "quantity": local.get("quantity"),
                        }
                    ],
                    snapshot=snapshot,
                    quote=quote,
                    intent_dict={"symbol": local["symbol"]},
                    quote_factory=self._quote_factory,
                )
            except BrokerAuthorityError:
                raise
            except Exception as exc:
                raise BrokerAuthorityError(
                    f"recovery exposure cap evaluation failed: {exc}"
                ) from exc
            if not cap_result.approved:
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(
                    f"recovery resubmit rejected by exposure caps: {cap_result.reason}"
                )
            if effective_quantity is not None and effective_notional is None and quote.price:
                effective_notional = float(effective_quantity) * quote.price
                effective_quantity = None
            if (
                effective_notional is not None
                and float(effective_notional) > cap_result.notional
            ):
                effective_notional = cap_result.notional
        intent = self._store.get_intent_for_order(local["order_id"])
        # Phase B note: the Phase A "recovery target already exists" block is
        # superseded — an increase onto an existing same-side position is
        # allowed and clipped by the recomputed exposure caps above.
        recovery_controls = None
        if not risk_reducing:
            from .policy import entry_check
            payload = json.loads((intent or {}).get("payload_json") or "{}")
            amount = float(effective_notional or float(effective_quantity or 0) * quote.price)
            amount, error = entry_check(payload, price=(quote.ask_price if side == "buy" else quote.bid_price) or float("nan"),
                                        equity=snapshot.equity, requested=amount)
            recovery_controls = _resolve_protective_prices(payload, payload.get("action", ""), False, [])
            if error or not recovery_controls or not recovery_controls.get("stop_loss_price"):
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(error or "Recovery requires the original protective stop")
            effective_notional, effective_quantity = amount, None
        try:
            from tradingagents.safety import get_safety_guard

            guard = get_safety_guard()
            amount = float(
                effective_notional
                if effective_notional is not None
                else (float(effective_quantity or 0) * quote.price)
            )
            verdict = guard.check_order(
                local["symbol"],
                amount,
                account={
                    "equity": snapshot.equity,
                    "last_equity": snapshot.last_equity,
                },
                position_value=abs(position.market_value) if position else 0.0,
                risk_reducing=risk_reducing,
            )
        except Exception as exc:
            raise BrokerAuthorityError(f"recovery safety policy unavailable: {exc}") from exc
        if verdict is not None and not verdict.allowed:
            self._store.transition_order(local["order_id"], "CANCELED")
            return
        # R05: recovery resubmits share the same final-POST boundary proof as
        # initial submits — one helper, one semantics. Every potentially
        # blocking broker GET (including the market-clock GET inside
        # dispatch revalidation) runs BEFORE the stop/window and kill-switch
        # final checks below, so control cannot revoke authority while a
        # GET is in flight and still let the POST through (N03/N04). The row
        # is still pre-transition and provably POST-free here, so CANCELED
        # is the safe terminal state for gate failures.
        if not risk_reducing:
            blocked = self._validate_opening_dispatch(
                intent_dict=payload,
                symbol=local["symbol"],
                spec={"notional": effective_notional, "quantity": effective_quantity},
                quote=quote,
                snapshot=snapshot,
                broker=broker,
            )
            if blocked:
                self._store.transition_order(local["order_id"], "CANCELED")
                raise BrokerAuthorityError(block)
        # R02 Layer 2 / N03: the submit-boundary authority check, run AFTER
        # every blocking GET and immediately before the PENDING/UNKNOWN ->
        # SUBMITTING transition. Refusal is NOT a rejection and NOT a CLEAN
        # outcome — and it provably made no POST, so the row keeps its exact
        # pre-transition status (PENDING stays PENDING, UNKNOWN stays
        # UNKNOWN): a later authorized resume re-enters the normal
        # adopt-or-resubmit path, and this round's reconciliation reports it
        # unresolved.
        if can_submit is not None and not can_submit():
            raise BrokerAuthorityError(
                f"recovery resubmit deferred by stop/window authority: "
                f"{local['client_order_id']} (no broker POST was made; the "
                "order stays in its current durable state for a later "
                "authorized resume)"
            )
        # N04: kill-switch final check, after the clock GET, before the POST.
        # A kill switch that engaged while recovery's GETs ran must not let
        # the resubmit through. The row keeps its pre-transition status and
        # the account stays unresolved (PAUSED via the failed recovery).
        _guard_now = None
        try:
            from tradingagents.safety import get_safety_guard as _gsg

            _guard_now = _gsg()
        except Exception:
            _guard_now = None
        if (
            _guard_now is not None
            and getattr(_guard_now, "enabled", True)
            and callable(getattr(_guard_now, "kill_switch_active", None))
            and _guard_now.kill_switch_active() is True
        ):
            raise BrokerAuthorityError(
                f"recovery resubmit blocked by kill switch: "
                f"{local['client_order_id']} (no broker POST was made; the "
                "order stays in its current durable state for a later "
                "authorized resume)"
            )
        ok, current = self._store.transition_order(local["order_id"], "SUBMITTING")
        if not ok:
            raise BrokerAuthorityError(
                f"recovery state conflict: {local['client_order_id']}"
            )
        # Rebuild the request with the effective (cap-clipped) size before
        # the POST; the durable client_order_id is unchanged.
        request = _build_market_request(
            local["symbol"], local["side"], effective_notional,
            effective_quantity, local["client_order_id"],
        )
        if recovery_controls:
            quantity = int(float(effective_notional) / float(payload["entry_policy"]["maximum_price"]))
            request = _build_protective_request(local["symbol"], side, quantity,
                                                recovery_controls.get("stop_loss_price"),
                                                recovery_controls.get("take_profit_price"), local["client_order_id"]) if quantity >= 1 else None
        if request is None:
            self._store.transition_order(current["order_id"], "REJECTED")
            raise BrokerAuthorityError(
                f"recovery order has no valid size: {local['client_order_id']}"
            )
        # N15: count the mutation exactly once at the broker POST boundary.
        # Adopting an existing broker order (upstream) is never a mutation.
        if maintenance is not None:
            maintenance["submit_calls"] = int(maintenance.get("submit_calls") or 0) + 1
            maintenance["submitted_symbols"] = set(
                maintenance.get("submitted_symbols") or set()
            )
            maintenance["submitted_symbols"].add(str(local["symbol"]).upper())
        try:
            response = broker.submit_order(request)
        except Exception as exc:
            # N11: only a structured 4xx (never 408) proves the broker read
            # and refused the POST. Any other failure after the request left
            # — including an HTTP 200 whose body cannot be decoded/validated
            # — leaves the outcome unprovable and must stay UNKNOWN so the
            # original client order id keeps reconciling.
            terminal = _definitive_rejection(exc)
            if maintenance is not None:
                if not terminal:
                    maintenance["has_unknown"] = True
            self._store.transition_order(
                current["order_id"], "REJECTED" if terminal else "UNKNOWN"
            )
            raise BrokerAuthorityError(
                f"recovery submit {'rejected' if terminal else 'outcome is uncertain'}: "
                f"{local['client_order_id']}: {exc}"
            ) from exc
        self._adopt_recovery_order(current, response)

    def _reconcile_snapshot(self, broker: Any, snapshot: BrokerSnapshot):
        """Recognize only protective children proven by a nested broker parent.

        Without these rows, a filled stop looks like an unexplained position
        change and the lot book retains an already-closed position.
        """
        from alpaca.trading.requests import GetOrderByIdRequest
        orders = self._store.list_all_orders()
        known_ids = {o.get("broker_order_id") for o in orders}
        unknown = {o.broker_order_id: o for o in snapshot.orders if o.broker_order_id not in known_ids}
        if unknown:
            for local in orders:
                if not local.get("broker_order_id") or self._store.protective_parent(local["order_id"]):
                    continue
                intent = self._store.get_intent_for_order(local["order_id"]) or {}
                payload = json.loads(intent.get("payload_json") or "{}")
                expected_side = "buy" if payload.get("target_position") == "LONG" else "sell"
                if not (payload.get("risk_controls") or {}).get("stop_loss_price") or local["side"] != expected_side:
                    continue
                if not any(o.symbol == local["symbol"] and o.side != local["side"] for o in unknown.values()):
                    continue
                parent = broker.get_order_by_id(local["broker_order_id"], filter=GetOrderByIdRequest(nested=True))
                if str(getattr(parent, "id", "")) != local["broker_order_id"]:
                    raise BrokerAuthorityError("Protective parent identity mismatch")
                for leg in (getattr(parent, "legs", None) or []):
                    child = unknown.get(str(leg.id))
                    if child is None:
                        continue
                    if (child.symbol != local["symbol"] or child.side == local["side"]
                            or not 0 < child.qty <= float(local.get("quantity") or 0) + 1e-8
                            or str(getattr(leg, "client_order_id", "")) != child.client_order_id):
                        raise BrokerAuthorityError("Protective child does not match its parent's exposure")
                    self._store.register_protective_child(local, child)
                    unknown.pop(child.broker_order_id)
        # F04: capture the durable gap reasons BEFORE reconcile overwrites
        # the account state, then re-check them against fresh broker facts.
        try:
            prior_state = self._store.get_account_state(snapshot.account_id)
            prior_reasons = (
                json.loads(prior_state["reasons_json"]) if prior_state else []
            )
        except Exception:
            prior_reasons = []
        result = Reconciler(self._store).reconcile(snapshot)
        return self._recover_persisted_protection_gaps(
            snapshot, result, prior_reasons
        )

    def _recover_locked(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        can_submit: Optional[Callable[[], bool]] = None,
        maintenance: Optional[dict[str, Any]] = None,
    ) -> tuple[BrokerSnapshot, Any]:
        """Resolve durable nonterminal rows before permitting new exposure.

        After every recovery action that changed broker or ledger state, the
        authority snapshot is refreshed and re-reconciled so the NEXT
        recoverable order evaluates caps against live facts — a prior
        resubmit/adoption must never be invisible to the following cap check
        (F02). A failed refresh stops recovery fail-closed on the old facts.

        F06: verify (or first-establish) the DB's single broker-account
        binding BEFORE any recovery or execution mutation. Every mutating
        entry (execute / startup_recover / enforce_exit_deadlines /
        liquidate) passes through here under the account lock.

        R02: ``can_submit`` is the caller's stop/window authority, checked
        one final time inside each resubmit immediately before its broker
        POST. ``None`` keeps the legacy behavior for non-long-run callers.
        N15: ``maintenance`` (when supplied by the round-level
        ``startup_recover`` call) collects the real broker mutation counts
        for the round journal; nested internal recovery calls pass None so
        the same mutation is never counted twice.
        """
        try:
            self._store.ensure_account_binding(snapshot.account_id)
        except Exception as exc:
            raise BrokerAuthorityError(
                f"execution DB account binding check failed: {exc}"
            ) from exc
        initial = self._reconcile_snapshot(broker, snapshot)
        recoverable_reasons = (
            "unresolved PENDING order:",
            "unresolved UNKNOWN order:",
            # F07: a crash mid-submit leaves a SUBMITTING row the broker may
            # or may not have accepted. It may enter the read-only
            # client-order-id lookup (adopt if found); it is never
            # auto-resubmitted — without a broker fact it stays unresolved
            # and the account stays paused.
            "unresolved SUBMITTING order:",
        )
        if any(not reason.startswith(recoverable_reasons) for reason in initial.reasons):
            return snapshot, self._apply_protection_coverage(snapshot, initial)

        def _broker_clients(current: BrokerSnapshot) -> set[str]:
            return {order.client_order_id for order in current.orders}

        changed = False
        queued = {row["client_order_id"] for row in self._store.list_recoverable_orders()}
        processed: set[str] = set()

        def _blocking_anomalies(result: Any) -> list[str]:
            # F04: reasons that describe real broker-side anomalies (a
            # partial fill, a position mismatch, an unknown live order, a
            # stale snapshot...) stop the recovery round immediately.
            # "unresolved PENDING/UNKNOWN/SUBMITTING order" rows that are
            # still QUEUED for this same loop are the loop's own remaining
            # work items, not anomalies — the final reconcile below still
            # reports any that survive the round.
            blocking = []
            for reason in result.reasons:
                match = re.match(
                    r"unresolved (PENDING|UNKNOWN|SUBMITTING) order: (\S+)$",
                    reason,
                )
                if match and match.group(2) in (queued - processed):
                    continue
                blocking.append(reason)
            return blocking

        for local in list(self._store.list_recoverable_orders()):
            if local["client_order_id"] in _broker_clients(snapshot):
                continue
            if self._store.protective_parent(local["order_id"]):
                raise BrokerAuthorityError("Missing protective child must be reconciled; never resubmit it as a market order")
            status = str(local["status"]).upper()
            found = self._lookup_for_recovery(broker, local["client_order_id"])
            if found is not None:
                self._adopt_recovery_order(local, found)
                changed = True
            elif status in {"PENDING", "UNKNOWN"}:
                self._resubmit_recovered(broker, local, snapshot, can_submit=can_submit,
                                         maintenance=maintenance)
                changed = True
            else:
                # SUBMITTING/PARTIAL without a broker fact is not safe to replay.
                # F04: this row's turn came and the bounded lookup still found
                # no broker fact, so its submit outcome stays ambiguous. It is
                # no longer exempted queued work: stop the round immediately —
                # no resubmit of this row, no recovery mutation for any later
                # queue item. The unresolved-SUBMITTING reason keeps the
                # account PAUSED.
                break
            processed.add(local["client_order_id"])
            # Refresh authority immediately after the adoption/resubmit above
            # so the next iteration's cap evaluation sees it (F02).
            try:
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=snapshot.account_id
                )
                step_result = self._reconcile_snapshot(broker, snapshot)
            except Exception as exc:
                raise BrokerAuthorityError(
                    f"broker snapshot refresh failed during recovery; "
                    f"stopping before further mutations: {exc}"
                ) from exc
            # F04: once fresh broker facts are not CLEAN after a recovery
            # mutation, no further recovery mutation may run this round —
            # the next recoverable order must wait for proven safety.
            if step_result.clean:
                continue
            if _blocking_anomalies(step_result):
                return snapshot, step_result
        if not changed:
            return snapshot, self._apply_protection_coverage(snapshot, initial)
        return snapshot, self._apply_protection_coverage(
            snapshot, self._reconcile_snapshot(broker, snapshot)
        )

    def startup_recover(
        self, can_submit: Optional[Callable[[], bool]] = None
    ) -> dict[str, Any]:
        """Scheduler/startup gate: recover first; only CLEAN may auto-trade.

        R02: ``can_submit`` (long-run callers pass their stop/window
        authority) is consulted inside recovery immediately before each
        resubmit POST, closing the TOCTOU window between the caller's outer
        precheck and the broker mutation. A refusal leaves the row durably
        unresolved (no fake REJECTED, no CLEAN claim).

        N15: the returned ``recovery_maintenance`` carries the real broker
        mutation facts of THIS recovery pass (submit attempts counted at the
        POST boundary, adoption excluded) for the round journal.
        """
        maintenance = _new_maintenance_collector()
        try:
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                snapshot, result = self._recover_locked(
                    broker, snapshot, can_submit=can_submit, maintenance=maintenance
                )
                return {
                    "success": result.clean,
                    "account_execution_state": result.state,
                    "reconciliation_reasons": list(result.reasons),
                    "snapshot_version": snapshot.version,
                    "account_id": snapshot.account_id,
                    "recovery_maintenance": _maintenance_summary(
                        maintenance, paused=not result.clean
                    ),
                }
        except (BrokerAuthorityError, AccountLockBusy) as exc:
            out: dict[str, Any] = {
                "success": False,
                "account_execution_state": "PAUSED",
                "reconciliation_reasons": [str(exc)],
                "error": str(exc),
                "recovery_maintenance": _maintenance_summary(
                    maintenance, paused=True, error=str(exc)
                ),
            }
            if getattr(exc, "stale_position_transition", False):
                out["stale_position_transition"] = True
            return out

    def account_status(self) -> dict[str, Any]:
        """Return the last durable CLEAN/PAUSED status for operator displays."""
        result: dict[str, Any]
        try:
            broker = self._broker_factory()
            snapshot = capture_broker_snapshot(broker)
        except Exception as exc:
            result = {"state": "PAUSED", "reasons": [str(exc)]}
            self._attach_quarantine_status(result)
            return result
        # F06: never surface another account's durable state from this DB.
        bound = None
        try:
            bound = self._store.account_binding_owner()
        except Exception:
            bound = None
        if bound is not None and bound != snapshot.account_id:
            result = {
                "state": "PAUSED",
                "reasons": [
                    f"execution DB is bound to broker account {bound!r}, "
                    f"not the current account {snapshot.account_id!r}"
                ],
            }
            self._attach_quarantine_status(result)
            return result
        try:
            state = self._store.get_account_state(snapshot.account_id)
        except Exception as exc:
            result = {"state": "PAUSED", "reasons": [str(exc)]}
            self._attach_quarantine_status(result)
            return result
        if state is None:
            # Surface quarantines even before the first reconciliation ran:
            # the operator must see why a symbol will refuse new exposure.
            result = {"state": "PAUSED", "reasons": ["startup reconciliation has not run"]}
            self._attach_quarantine_status(result)
            return result
        result = {
            "state": state["state"],
            "reasons": json.loads(state["reasons_json"]),
            "snapshot_version": state["snapshot_version"],
            "updated_at": state["updated_at"],
        }
        self._attach_quarantine_status(result)
        return result

    def _attach_quarantine_status(self, result: dict[str, Any]) -> None:
        """Surface active corporate-action quarantines on the operator path."""
        try:
            gate = self.quarantine_gate()
            if gate is not None:
                active = gate.store.all_active()
                if active:
                    result["quarantined_symbols"] = {
                        record["symbol"]: record["reason"] for record in active
                    }
        except Exception:
            pass

    def _market_clock_closed(self, broker: Any) -> Optional[str]:
        """R13: None when the broker clock proves the session open, else a
        fail-closed reason (closed market, unavailable or malformed clock).

        A clock that cannot be proven open is treated as closed: an entry
        POST left to a later session would trade on stale analysis. This gate
        applies only to exposure-adding opening dispatch; verified closes and
        risk-reducing orders never consult it.
        """
        if broker is None:
            return "broker market clock unavailable: no broker to prove the session open"
        getter = getattr(broker, "get_clock", None)
        if not callable(getter):
            return "broker exposes no market clock; cannot prove the session open"
        try:
            clock = getter()
        except Exception as exc:
            return f"broker market clock unavailable ({exc}); refusing to open exposure"
        is_open = getattr(clock, "is_open", None)
        if is_open is None and isinstance(clock, dict):
            is_open = clock.get("is_open")
        if is_open is not True:
            return "market/session is closed per the broker clock; no new entry may be posted"
        return None

    def _validate_opening_dispatch(
        self,
        *,
        intent_dict: dict[str, Any],
        symbol: str,
        spec: dict[str, Any],
        quote: Any,
        snapshot: Optional[BrokerSnapshot],
        broker: Any = None,
    ) -> Optional[str]:
        """R05: re-prove entry authorization at the final broker POST boundary.

        Facts captured before sizing/caps/outbox-commit can go stale while the
        process is suspended or the durable commit is slow. Immediately before
        any exposure-adding POST, the SAME one helper (initial submit and
        recovery) re-checks the broker market clock, snapshot freshness, quote
        freshness and the entry policy at the current time — in that order, so
        every potentially blocking network GET happens BEFORE the final
        freshness proof and nothing can stale the facts between the check and
        the POST. On any failure the caller must NOT refresh facts, resize, or
        rewrite the durable row: broker POST = 0, the row becomes CANCELED
        (provable: no POST was made) and the caller must re-acquire fresh
        facts and re-analyze. Returns None when dispatch may proceed, else a
        fail-closed reason.

        R13: opening orders additionally require the broker's own clock to
        prove the regular session is open (clock.is_open). A closed market,
        an unavailable or malformed clock response — all fail closed with
        zero POSTs. Close/risk-reducing orders never route through this
        helper and are never blocked by the opening gate.
        """
        if snapshot is None:
            return "dispatch revalidation failed: no authoritative broker snapshot"
        if quote is None:
            return "dispatch revalidation failed: no authoritative quote"
        # R05: the broker clock GET is a potentially blocking network call.
        # It must run BEFORE the final freshness proof — a slow clock response
        # must never widen the gap between the freshness check and the POST.
        # R13: an entry additionally requires the broker's own clock to
        # prove the regular session is open (clock.is_open). A closed market,
        # an unavailable or malformed clock response — all fail closed with
        # zero POSTs. Close/risk-reducing orders never route through this
        # helper and are never blocked by the opening gate.
        market_closed = self._market_clock_closed(broker)
        if market_closed is not None:
            return f"dispatch revalidation failed: {market_closed}"
        # Final time-sensitive proof, after every blocking GET above.
        try:
            validate_freshness(
                snapshot.observed_at,
                ttl_seconds=float(
                    os.getenv("TRADINGAGENTS_SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)
                ),
                label="broker snapshot",
            )
            validate_quote(quote, symbol)
        except BrokerAuthorityError as exc:
            return f"dispatch revalidation failed: {exc}"
        if spec.get("notional") is not None:
            amount = float(spec["notional"])
        elif spec.get("quantity") is not None:
            amount = float(spec["quantity"]) * float(quote.price)
        else:
            return "dispatch revalidation failed: opening order has no provable size"
        from .policy import entry_check

        signal = str(intent_dict.get("action") or "").upper()
        execution_price = (
            quote.ask_price if signal in {"BUY", "LONG"} else quote.bid_price
        )
        capped, policy_error = entry_check(
            intent_dict,
            price=execution_price if execution_price else float("nan"),
            equity=float(snapshot.equity),
            requested=amount,
        )
        if policy_error:
            return f"dispatch revalidation failed: {policy_error}"
        if capped is None:
            return "dispatch revalidation failed: entry policy returned no amount"
        # entry_check floors to cents; tolerate only floor-level dust. A real
        # shortfall (expired authorization, moved quote, changed equity) is
        # far larger and fails closed instead of silently resizing here.
        if capped + 0.02 < amount:
            return (
                f"dispatch revalidation failed: entry policy now allows at most "
                f"${capped:,.2f}, below the durable order amount ${amount:,.2f}; "
                "the order must not be sent at the old size"
            )
        return None

    def _submit_one(
        self,
        *,
        order_row: dict[str, Any],
        spec: dict[str, Any],
        symbol: str,
        intent_dict: dict[str, Any],
        broker: Any = None,
        _snapshot: Optional[BrokerSnapshot] = None,
        _quote: Any = None,
        can_submit: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        client_oid = order_row["client_order_id"]
        order_id = order_row["order_id"]
        side = spec.get("side", "")
        order_type = spec.get("order_type", "market")
        # POST counter must exist before any exception handler references it.
        broker_calls = 0
        try:
            broker = broker or self._broker_factory()
        except Exception as exc:
            self._store.transition_order(order_id, "UNKNOWN")
            return {
                "ok": False,
                "status": "UNKNOWN",
                "client_order_id": client_oid,
                "broker_calls": 0,
                "error": f"broker factory failed (ambiguous): {exc}",
            }
        # NOTE: broker instantiation itself is not a POST; count POSTs only.
        try:
            if order_type == "close_position":
                if not spec.get("quantity"):
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False, "status": "REJECTED",
                        "client_order_id": client_oid, "broker_calls": 0,
                        "error": "verified close quantity is unavailable",
                    }
                # Use an explicit market order so the verified quantity and
                # deterministic client_order_id cross the broker boundary.
                order_type = "market"
            request = _build_market_request(
                symbol, side, spec.get("notional"), spec.get("quantity"), client_oid
            )
            if request is None:
                self._store.transition_order(order_id, "REJECTED")
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "client_order_id": client_oid,
                    "broker_calls": 0,
                    "error": "no quantity/notional: refusing to guess size",
                }
            # Broker-side protective legs ride on the same parent submit (no
            # separate child POSTs). A rejection never falls back to a bare market order.
            protective_request = None
            if (
                spec.get("role") == "open"
                and (spec.get("stop_loss_price") or spec.get("take_profit_price"))
                and spec.get("quantity")
                and float(spec.get("quantity") or 0) > 0
            ):
                protective_request = _build_protective_request(
                    symbol,
                    side,
                    spec.get("quantity"),
                    spec.get("stop_loss_price"),
                    spec.get("take_profit_price"),
                    client_oid,
                )
            if spec.get("role") == "open" and protective_request is None:
                self._store.transition_order(order_id, "REJECTED")
                return {"ok": False, "status": "REJECTED", "broker_calls": 0,
                        "error": "Required protective order could not be constructed"}
            # R05: last safety point before ANY exposure-adding POST. A stale
            # snapshot/quote or an expired/reduced authorization must not slip
            # through on the pre-commit approval; the durable row is provably
            # POST-free here, so CANCELED is legal.
            if spec.get("role") == "open":
                blocked = self._validate_opening_dispatch(
                    intent_dict=intent_dict, symbol=symbol, spec=spec,
                    quote=_quote, snapshot=_snapshot, broker=broker,
                )
                if blocked:
                    _, blocked_row = self._store.transition_order(order_id, "CANCELED")
                    return {
                        "ok": False,
                        "status": str(blocked_row.get("status") or "CANCELED"),
                        "pre_submit_blocked": True,
                        "fail_closed": True,
                        "client_order_id": client_oid,
                        "broker_calls": 0,
                        "error": blocked,
                    }
            # N03: final stop/window authority check — every potentially
            # blocking broker GET (including the market-clock GET inside
            # dispatch revalidation) has completed by now, and the caller's
            # authority may have flipped while they ran. The row is already
            # SUBMITTING and provably POST-free, so a legal transition to
            # CANCELED records the refusal; SUBMITTING is never left behind
            # pretending the order might still be in flight.
            if spec.get("role") == "open" and can_submit is not None and not can_submit():
                self._store.transition_order(order_id, "CANCELED")
                return {
                    "ok": False,
                    "status": "CANCELED",
                    "pre_submit_blocked": True,
                    "fail_closed": True,
                    "client_order_id": client_oid,
                    "broker_calls": 0,
                    "error": (
                        "execution deferred by stop/window authority at the "
                        "final submit boundary (no broker POST was made)"
                    ),
                }
            # N04: kill-switch final check — the switch may have engaged while
            # the dispatch GETs ran. Re-read AFTER those GETs and BEFORE the
            # POST; POST=0 and the row becomes CANCELED (proven).
            if spec.get("role") == "open":
                try:
                    from tradingagents.safety import get_safety_guard as _get_guard

                    _guard = _get_guard()
                except Exception:
                    _guard = None
                if (
                    _guard is not None
                    and getattr(_guard, "enabled", True)
                    and callable(getattr(_guard, "kill_switch_active", None))
                    and _guard.kill_switch_active() is True
                ):
                    self._store.transition_order(order_id, "CANCELED")
                    return {
                        "ok": False,
                        "status": "CANCELED",
                        "pre_submit_blocked": True,
                        "fail_closed": True,
                        "client_order_id": client_oid,
                        "broker_calls": 0,
                        "error": (
                            "kill switch engaged before the final submit "
                            "(no broker POST was made)"
                        ),
                    }
            resp = None
            broker_calls = 0
            order_class: Optional[str] = None
            if protective_request is not None:
                try:
                    resp = broker.submit_order(protective_request)
                    broker_calls = 1
                except Exception as exc:
                    if not _definitive_rejection(exc):
                        # N11: an unprovable bracket outcome (ambiguous
                        # transport failure, or an HTTP 200 whose body cannot
                        # be decoded/validated) — the parent may have been
                        # accepted, so never fall through to a second POST.
                        self._store.transition_order(order_id, "UNKNOWN")
                        return {
                            "ok": False,
                            "status": "UNKNOWN",
                            "client_order_id": client_oid,
                            "broker_calls": 1,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    # An explicit structured 4xx validation/rejection is
                    # terminal. Do not turn it into a second,
                    # less-protected POST.
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": 1,
                        "error": str(exc),
                    }
            if protective_request is not None and resp is None:
                self._store.transition_order(order_id, "UNKNOWN")
                return {"ok": False, "status": "UNKNOWN", "broker_calls": 1,
                        "client_order_id": client_oid, "error": "Empty protective submit response; reconcile before retry"}
            if resp is None:
                try:
                    resp = broker.submit_order(request)
                    broker_calls = 1
                except Exception as exc:
                    if not _definitive_rejection(exc):
                        # N11: ambiguous POST outcome (transport failure, or
                        # an HTTP 200 whose body cannot be decoded/validated):
                        # mark UNKNOWN, never retry here. Lookup by
                        # client_order_id (A2 orchestration) resolves later.
                        self._store.transition_order(order_id, "UNKNOWN")
                        return {
                            "ok": False,
                            "status": "UNKNOWN",
                            "client_order_id": client_oid,
                            "broker_calls": 1,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": 1,
                        "error": str(exc),
                    }
            if protective_request is not None:
                order_class = (
                    "bracket"
                    if spec.get("stop_loss_price") and spec.get("take_profit_price")
                    else "oto"
                )
            broker_oid = getattr(resp, "id", None) or (
                resp.get("order_id") if isinstance(resp, dict) else None
            )
            status_raw = getattr(resp, "status", None) or (
                resp.get("status") if isinstance(resp, dict) else "accepted"
            )
            if isinstance(resp, dict) and resp.get("success") is False:
                self._store.transition_order(order_id, "REJECTED")
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "client_order_id": client_oid,
                    "broker_calls": broker_calls,
                    "error": str(resp.get("error", "broker rejected order")),
                }
            if not broker_oid:
                self._store.transition_order(order_id, "UNKNOWN")
                return {"ok": False, "status": "UNKNOWN", "broker_calls": broker_calls,
                        "client_order_id": client_oid, "error": "Submit response has no broker order identity"}
            local = broker_status_to_local(status_raw)
            self._store.transition_order(
                order_id, local, broker_order_id=str(broker_oid) if broker_oid else None
            )
            # Child roles (protect-stop/protect-target) remain available via
            # client_order_id_for() when A2 needs standalone children.
            submitted: dict[str, Any] = {
                "ok": True,
                "status": local,
                "client_order_id": client_oid,
                "broker_order_id": broker_oid,
                "broker_calls": broker_calls,
            }
            if order_class is not None:
                submitted["order_class"] = order_class
                if protective_request is not None:
                    submitted["stop_loss_price"] = spec.get("stop_loss_price")
                    submitted["take_profit_price"] = spec.get("take_profit_price")
            return submitted
        except Exception as exc:
            # N11: only a structured HTTP 4xx (never 408) proves the broker
            # read and refused the POST. Any other failure — including one
            # raised after a 200 arrived but before the row could be updated
            # — leaves the outcome unprovable and must stay UNKNOWN with the
            # real POST count so the original client order id keeps
            # reconciling.
            if _definitive_rejection(exc):
                self._store.transition_order(order_id, "REJECTED")
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "client_order_id": client_oid,
                    "broker_calls": broker_calls,
                    "error": str(exc),
                }
            self._store.transition_order(order_id, "UNKNOWN")
            return {
                "ok": False,
                "status": "UNKNOWN",
                "client_order_id": client_oid,
                "broker_calls": broker_calls,
                "error": f"ambiguous submit outcome: {exc}",
            }

    # -- liquidation (same trust boundary) ---------------------------------

    def liquidate(
        self, symbol: str, *, decision_id: Optional[str] = None, run_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Execute a broker-verified, exposure-reducing close under the account lock.

        F04 sequencing: verify ownership → durable close outbox commit →
        cancel proven protections → refresh/re-verify → submit the already
        durable close → reconcile → enforce the protection-gap invariant.
        A durable commit failure returns with the original protections
        untouched.
        """
        sym = (symbol or "").upper()
        if not sym:
            return {
                "success": False, "fail_closed": True, "broker_attempted": False,
                "broker_calls": 0, "error": "missing symbol for liquidation",
            }
        try:
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                snapshot, reconciliation = self._recover_locked(broker, snapshot)
                position = snapshot.position(sym)
                side = "sell" if position and position.qty > 0 else "buy"
                before_cancel_qty = float(position.qty) if position else None
                committed_quantity = abs(position.qty) if position else None
                prepared: Optional[dict[str, Any]] = None
                if position is not None:
                    # Step 1: verify ownership/conflicts WITHOUT canceling.
                    # (The protection itself is a live same-side order, so the
                    # full verified-exit check can only pass AFTER cancel.)
                    self._verify_owned_close_protections(broker, snapshot, sym)
                    try:
                        from tradingagents.safety import get_safety_guard

                        guard = get_safety_guard()
                        verdict = (
                            guard.check_order(
                                sym,
                                abs(position.market_value),
                                account={
                                    "equity": snapshot.equity,
                                    "last_equity": snapshot.last_equity,
                                },
                                position_value=abs(position.market_value),
                                risk_reducing=True,
                            )
                            if getattr(guard, "enabled", True)
                            else None
                        )
                    except Exception as exc:
                        return self._paused_result(
                            snapshot, [f"liquidation safety policy unavailable: {exc}"]
                        )
                    if verdict is not None and not verdict.allowed:
                        result = self._paused_result(
                            snapshot,
                            list(getattr(verdict, "reasons", ())) or ["liquidation blocked by safety policy"],
                        )
                        result["safety_blocked"] = True
                        return result
                    # Step 2: durable close outbox BEFORE canceling protection.
                    prepared = self._prepare_liquidation_outbox(
                        sym, decision_id=decision_id, run_id=run_id,
                        quantity=committed_quantity, side=side,
                    )
                    if not prepared.get("ok"):
                        # Durable commit failed: protections stay untouched.
                        return {
                            "success": False,
                            "fail_closed": True,
                            "broker_attempted": False,
                            "broker_calls": 0,
                            "error": prepared.get("error", "durable commit failed"),
                        }
                # Step 3: cancel only the proven owned protections.
                snapshot, canceled_protections = self._cancel_owned_close_protections(broker, snapshot, sym)
                # Step 4: refresh already done; re-verify the position.
                position = snapshot.position(sym)
                if prepared is not None and position is None:
                    # The position closed during the cancellation race: no
                    # new close is needed and this is not a gap.
                    self._abandon_prepared_rows(prepared)
                    result = {
                        "success": True,
                        "status": "FILLED",
                        "broker_attempted": False,
                        "broker_calls": canceled_protections,
                        "no_close_needed": True,
                        "note": "position closed during protection cancellation",
                    }
                    return result
                if prepared is not None and (
                    position is None
                    or not _position_unchanged(position.qty, before_cancel_qty)
                ):
                    # The position changed during cancellation: the durable
                    # close's fixed quantity is no longer provably safe.
                    self._abandon_prepared_rows(prepared)
                    gap = self._evaluate_protection_gap(
                        broker, snapshot, sym, canceled_protections=canceled_protections,
                    )
                    if gap is not None:
                        gap["broker_calls"] = canceled_protections
                        return gap
                    paused = self._paused_result(
                        snapshot,
                        ["liquidation position changed during protection cancellation"],
                    )
                    paused["broker_calls"] = canceled_protections
                    return paused
                if prepared is not None and not self._verified_reducing_exit(
                    snapshot, sym,
                    [{"role": "close", "side": side, "quantity": abs(position.qty)}],
                ):
                    self._abandon_prepared_rows(prepared)
                    paused = self._paused_result(
                        snapshot,
                        ["liquidation requires a fresh broker position and no conflicting close order"],
                    )
                    paused["broker_calls"] = canceled_protections
                    return paused
                result = self._liquidate_core(
                    sym,
                    decision_id=decision_id,
                    run_id=run_id,
                    _broker=broker,
                    _quantity=abs(position.qty) if position else None,
                    _side=side,
                    _outbox=prepared,
                )
                result["broker_calls"] = result.get("broker_calls", 0) + canceled_protections
                try:
                    after = capture_broker_snapshot(
                        broker, expected_account_id=snapshot.account_id
                    )
                    post = self._reconcile_snapshot(broker, after)
                    result["account_execution_state"] = post.state
                    result["reconciliation_reasons"] = list(post.reasons)
                    result["snapshot_version"] = after.version
                    if not post.clean:
                        result["paused"] = True
                except BrokerAuthorityError as exc:
                    result["account_execution_state"] = "PAUSED"
                    result["reconciliation_reasons"] = [str(exc)]
                    result["paused"] = True
                # Step 7: enforce the protection-gap invariant on fresh facts.
                if result.get("status") != "UNKNOWN":
                    try:
                        gap_snapshot = capture_broker_snapshot(
                            broker, expected_account_id=snapshot.account_id
                        )
                        self._reconcile_snapshot(broker, gap_snapshot)
                    except BrokerAuthorityError:
                        gap_snapshot = snapshot
                    gap = self._evaluate_protection_gap(
                        broker, gap_snapshot, sym, canceled_protections=canceled_protections,
                    )
                    if gap is not None:
                        result.update(gap)
                return result
        except AccountLockBusy as exc:
            return {
                "success": False, "paused": True, "busy": True,
                "broker_attempted": False, "broker_calls": 0, "error": str(exc),
            }
        except Exception as exc:
            return {
                "success": False, "paused": True, "fail_closed": True,
                "broker_attempted": False, "broker_calls": 0,
                "error": f"broker authority unavailable: {exc}",
            }

    def _prepare_liquidation_outbox(
        self,
        symbol: str,
        *,
        decision_id: Optional[str] = None,
        run_id: Optional[str] = None,
        quantity: Optional[float] = None,
        side: str = "sell",
    ) -> dict[str, Any]:
        """Durably commit the close intent BEFORE any protection is canceled.

        F04 sequencing: if this durable commit fails, the caller must return
        with the original protections untouched — a close that only exists
        in memory must never be allowed to strip a position's stop first.
        """
        sym = (symbol or "").upper()
        # Each liquidation is its own operator decision: without an explicit
        # decision_id the default is per-call, so a later legitimate exit of
        # the same symbol is never silently deduped against an older one.
        # Repeat/concurrent safety comes from _verified_reducing_exit (fresh
        # broker position + no conflicting live close order), not from ID reuse.
        did = decision_id or f"liq-{sym}-{utc_now().strftime('%Y%m%dT%H%M%S%f')}"
        client_oid = client_order_id_for(did, sym, side, role="close", seq=0)
        try:
            intent_row, order_rows, created = self._store.create_outbox(
                decision_id=did,
                run_id=run_id,
                symbol=sym,
                action="SELL" if side == "sell" else "BUY",
                target_position="NEUTRAL",
                payload_json=json.dumps(
                    {
                        "symbol": sym,
                        "action": "SELL" if side == "sell" else "BUY",
                        "kind": "liquidation",
                    },
                    sort_keys=True,
                ),
                orders=[
                    {
                        "client_order_id": client_oid,
                        "symbol": sym,
                        "side": side,
                        "quantity": quantity,
                        "notional": None,
                    }
                ],
            )
        except Exception as exc:
            return {
                "ok": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": f"durable commit failed: {exc}",
            }
        return {
            "ok": True,
            "decision_id": did,
            "client_order_id": client_oid,
            "intent_row": intent_row,
            "order_rows": order_rows,
            "created": created,
        }

    def _liquidate_core(
        self,
        symbol: str,
        *,
        decision_id: Optional[str] = None,
        run_id: Optional[str] = None,
        _broker: Any = None,
        _quantity: Optional[float] = None,
        _side: str = "sell",
        _outbox: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Risk-reducing exit through the same durable boundary.

        When ``_outbox`` is supplied (the F04 pre-cancel durable commit), the
        already-committed intent is submitted instead of creating another.
        """
        sym = (symbol or "").upper()
        if not sym:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": "missing symbol for liquidation",
            }
        prepared = _outbox or self._prepare_liquidation_outbox(
            sym, decision_id=decision_id, run_id=run_id,
            quantity=_quantity, side=_side,
        )
        if not prepared.get("ok"):
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": prepared.get("error", "durable commit failed"),
            }
        did = prepared["decision_id"]
        client_oid = prepared["client_order_id"]
        intent_row = prepared["intent_row"]
        order_rows = prepared["order_rows"]
        created = prepared["created"]
        if not created:
            existing = self._store.list_orders_for_intent(intent_row["intent_id"])
            if existing and all(
                (o.get("status") or "").upper() != "PENDING" for o in existing
            ):
                return {
                    "success": True,
                    "deduped": True,
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                    "orders": existing,
                }
            order_rows = existing
        orow = order_rows[0]
        if (orow.get("status") or "").upper() != "PENDING":
            return {
                "success": True,
                "deduped": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
                "orders": order_rows,
            }
        self._store.transition_order(orow["order_id"], "SUBMITTING")
        try:
            broker = _broker or self._broker_factory()
        except Exception as exc:
            self._store.transition_order(orow["order_id"], "UNKNOWN")
            return {
                "success": False,
                "status": "UNKNOWN",
                "broker_attempted": False,
                "broker_calls": 0,
                "error": f"broker factory failed (ambiguous): {exc}",
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
            }
        # N04: the kill switch may have engaged while the earlier steps of the
        # close flow (protection cancellation, fresh GETs) ran. The final
        # close POST must re-prove that order flow is still permitted — the
        # standing policy is that an engaged kill switch also blocks exits.
        # The row is SUBMITTING and provably POST-free, so CANCELED is legal.
        try:
            from tradingagents.safety import get_safety_guard as _get_guard

            _guard = _get_guard()
        except Exception:
            _guard = None
        if (
            _guard is not None
            and getattr(_guard, "enabled", True)
            and callable(getattr(_guard, "kill_switch_active", None))
            and _guard.kill_switch_active() is True
        ):
            self._store.transition_order(orow["order_id"], "CANCELED")
            return {
                "success": False,
                "status": "CANCELED",
                "broker_attempted": False,
                "broker_calls": 0,
                "error": (
                    "kill switch engaged before the close submit "
                    "(no broker POST was made)"
                ),
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
            }
        posted = False
        try:
            request = _build_market_request(
                sym, _side, None, _quantity, client_oid
            )
            if request is None:
                raise ValueError("verified close quantity is unavailable")
            posted = True
            resp = broker.submit_order(request)
            broker_oid = getattr(resp, "id", None) or (
                resp.get("order_id") if isinstance(resp, dict) else None
            )
            if isinstance(resp, dict) and resp.get("success") is False:
                self._store.transition_order(orow["order_id"], "REJECTED")
                return {
                    "success": False,
                    "status": "REJECTED",
                    "broker_attempted": True,
                    "broker_calls": 1,
                    "error": str(resp.get("error", "broker rejected close")),
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                }
            status_raw = getattr(resp, "status", None) or (
                resp.get("status") if isinstance(resp, dict) else "accepted"
            )
            if not broker_oid:
                self._store.transition_order(orow["order_id"], "UNKNOWN")
                return {"success": False, "status": "UNKNOWN", "broker_attempted": True,
                        "broker_calls": 1, "error": "Close response has no broker order identity"}
            local = broker_status_to_local(status_raw)
            self._store.transition_order(
                orow["order_id"], local,
                broker_order_id=str(broker_oid) if broker_oid else None,
            )
            return {
                "success": True,
                "status": local,
                "broker_attempted": True,
                "broker_calls": 1,
                "broker_order_id": broker_oid,
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
                "orders": self._store.list_orders_for_intent(intent_row["intent_id"]),
            }
        except Exception as exc:
            # N11: only a structured HTTP 4xx (never 408) proves the broker
            # read and refused the POST. A local pre-POST failure (request
            # construction) is a terminal REJECTED with zero broker calls;
            # anything after the request left stays UNKNOWN.
            if not posted and not _definitive_rejection(exc):
                self._store.transition_order(orow["order_id"], "REJECTED")
                return {
                    "success": False,
                    "status": "REJECTED",
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "error": str(exc),
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                }
            if posted and not _definitive_rejection(exc):
                self._store.transition_order(orow["order_id"], "UNKNOWN")
                return {
                    "success": False,
                    "status": "UNKNOWN",
                    "broker_attempted": True,
                    "broker_calls": 1,
                    "error": f"ambiguous close outcome: {exc}",
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                }
            self._store.transition_order(orow["order_id"], "REJECTED")
            return {
                "success": False,
                "status": "REJECTED",
                "broker_attempted": posted,
                "broker_calls": 1 if posted else 0,
                "error": str(exc),
                "intent_id": intent_row["intent_id"],
                "decision_id": did,
            }

    # -- bounded UNKNOWN lookup/adopt ---------------------------------------

    def lookup_unknown(self, client_order_id: str) -> dict[str, Any]:
        """Query the broker by client_order_id and adopt the observed state.

        No second POST happens here. Returns adopted status or NOT_FOUND.
        Uses the same bounded lookup policy as startup recovery.
        """
        existing = self._store.get_order_by_client(client_order_id)
        if existing is None:
            return {"found": False, "error": "unknown client_order_id"}
        if (existing.get("status") or "").upper() != "UNKNOWN":
            return {"found": True, "adopted": False, "order": existing}
        try:
            broker = self._broker_factory()
        except Exception as exc:
            return {"found": False, "error": f"broker unavailable: {exc}"}
        try:
            broker_order = self._lookup_for_recovery(broker, client_order_id)
        except BrokerAuthorityError as exc:
            return {"found": False, "uncertain": True, "order": existing, "error": str(exc)}
        if broker_order is None:
            return {
                "found": False,
                "not_found": True,
                "order": existing,
                "detail": "broker explicitly reported no such order in bounded lookup",
            }
        self._adopt_recovery_order(existing, broker_order)
        row = self._store.get_order(existing["order_id"])
        return {"found": True, "adopted": True, "order": row, "status": row["status"]}


def _service(db_path=None, broker_factory=None, quote_factory=None) -> ExecutionService:
    return ExecutionService(
        db_path=db_path, broker_factory=broker_factory, quote_factory=quote_factory
    )


def execute_trade_intent(
    symbol: str,
    current_position: str,
    trade_intent: Any,
    dollar_amount: float,
    allow_shorts: bool = False,
    risk_params: Optional[dict] = None,
    decision_id: Optional[str] = None,
    run_id: Optional[str] = None,
    db_path: Optional[str] = None,
    broker_factory: Optional[Callable[[], Any]] = None,
    quote_factory: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Compatibility wrapper keeping the legacy call shape.

    Production callers should prefer ExecutionService.execute(); this wrapper
    is the single durable trust boundary for intent-based execution, including
    deterministic sizing (risk_params) and broker-side protective legs.
    """
    svc = _service(db_path, broker_factory, quote_factory)
    return svc.execute(
        trade_intent=trade_intent,
        decision_id=decision_id,
        run_id=run_id,
        dollar_amount=dollar_amount,
        allow_shorts=allow_shorts,
        risk_params=risk_params,
        current_position=current_position,
    )


def liquidate_position(
    symbol: str,
    *,
    decision_id: Optional[str] = None,
    run_id: Optional[str] = None,
    db_path: Optional[str] = None,
    broker_factory: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    svc = _service(db_path, broker_factory)
    return svc.liquidate(symbol, decision_id=decision_id, run_id=run_id)
