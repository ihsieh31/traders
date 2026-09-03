"""Single execution entry for Phase A.1. All production order/close paths go here.

Trust boundary: validate TradeIntent -> durable outbox COMMIT -> safety ->
broker submit. DB commit failure => zero broker calls. Missing/invalid
intent => fail-closed with zero broker calls.

Ponytail ceiling note: one service + one SQLite store only. No facade/
adapter/repository layers. BrokerSnapshot/reconciliation/retry orchestration
belong to Phase A.2; here only order-level lookup/adopt seam.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from tradingagents.execution.store import (
    ExecutionStore,
    canonical_decision_id,
    client_order_id_for,
    is_valid_order_transition,  # re-export for tests/callers
)

__all__ = [
    "ExecutionService",
    "execute_trade_intent",
    "liquidate_position",
    "is_valid_order_transition",
]

_DEFAULT_DB = os.getenv("TRADINGAGENTS_EXECUTION_DB", "eval_results/execution.db")

_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connectionerror",
    "socket closed",
    "temporarily unavailable",
)


def _is_ambiguous_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _TIMEOUT_MARKERS)


def _broker_status_to_local(status: Any) -> str:
    s = str(status or "").lower()
    if "fill" in s and "partial" in s:
        return "PARTIAL"
    if s in ("filled", "fill"):
        return "FILLED"
    if s in ("canceled", "cancelled"):
        return "CANCELED"
    if s in ("rejected", "reject"):
        return "REJECTED"
    if s in ("expired",):
        return "EXPIRED"
    return "ACCEPTED"


def _default_db_path() -> str:
    return _DEFAULT_DB


def validate_trade_intent(trade_intent: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return (validated_dict, error). Invalid => (None, reason), zero broker calls."""
    if trade_intent is None:
        return None, "missing trade_intent: strict boundary requires schema-valid TradeIntent"
    try:
        from tradingagents.agents.schemas import TradeIntent as _TI
    except Exception as exc:  # pragma: no cover - import should exist
        return None, f"TradeIntent schema unavailable: {exc}"
    try:
        if isinstance(trade_intent, _TI):
            return trade_intent.model_dump(mode="json"), None
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


class ExecutionService:
    """Unique production execution entry (paper-only)."""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        broker_factory: Optional[Callable[[], Any]] = None,
    ):
        self.db_path = str(db_path or _default_db_path())
        self._store = ExecutionStore(self.db_path)
        self._broker_factory = broker_factory or self._default_broker_factory

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @staticmethod
    def _default_broker_factory():
        from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client

        return get_alpaca_trading_client()

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
        opens_new_exposure = bool(
            target_position_open
            and str(live_position or "NEUTRAL").upper() != target_position_open
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
                )
            except Exception as exc:
                warnings.append(
                    f"Risk sizing unavailable ({exc}); falling back to configured notional."
                )
                risk_sizing_info = {"applied": False, "error": str(exc)}
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

        specs = _planned_order_specs(intent_dict, effective_amount)
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
        except Exception:
            guard = None

        broker_calls = 0
        results: list[dict[str, Any]] = []
        close_leg_failed = False
        for spec, orow in zip(specs, order_rows):
            if (orow.get("status") or "").upper() != "PENDING":
                results.append(
                    {"client_order_id": orow["client_order_id"], "deduped": True}
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
            if guard is not None and getattr(guard, "enabled", True):
                try:
                    verdict = guard.check_order(
                        symbol, amount, risk_reducing=risk_reducing
                    )
                except Exception:
                    verdict = None
                if verdict is not None and not verdict.allowed:
                    self._store.transition_order(orow["order_id"], "CANCELED")
                    results.append(
                        {
                            "client_order_id": orow["client_order_id"],
                            "safety_blocked": True,
                            "error": " ".join(getattr(verdict, "reasons", ["blocked"])),
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
                order_row=orow, spec=spec, symbol=symbol, intent_dict=intent_dict
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
        success = all(
            r.get("ok") or r.get("deduped") for r in results
        ) and not any(r.get("safety_blocked") for r in results)
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

    def _submit_one(
        self, *, order_row: dict[str, Any], spec: dict[str, Any], symbol: str, intent_dict: dict[str, Any]
    ) -> dict[str, Any]:
        client_oid = order_row["client_order_id"]
        order_id = order_row["order_id"]
        side = spec.get("side", "")
        order_type = spec.get("order_type", "market")
        try:
            broker = self._broker_factory()
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
                resp = broker.close_position((symbol or "").upper().replace("/", ""))
                broker_calls = 1
                broker_oid = getattr(resp, "id", None) or (
                    resp.get("order_id") if isinstance(resp, dict) else None
                )
                status_raw = getattr(resp, "status", None) or (
                    resp.get("status") if isinstance(resp, dict) else "accepted"
                )
                # Explicit broker failure dict (e.g. {"success": False}) is terminal.
                if isinstance(resp, dict) and resp.get("success") is False:
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": broker_calls,
                        "error": str(resp.get("error", "broker rejected close")),
                    }
                local = _broker_status_to_local(status_raw)
                self._store.transition_order(
                    order_id, local, broker_order_id=str(broker_oid) if broker_oid else None
                )
                return {
                    "ok": True,
                    "status": local,
                    "client_order_id": client_oid,
                    "broker_order_id": broker_oid,
                    "broker_calls": broker_calls,
                }
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
            # separate child POSTs): try bracket/OTO first, fall back to plain
            # market if the broker rejects the protective legs.
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
            resp = None
            broker_calls = 0
            protective_error = None
            order_class: Optional[str] = None
            if protective_request is not None:
                try:
                    resp = broker.submit_order(protective_request)
                    broker_calls = 1
                except Exception as exc:
                    if _is_ambiguous_error(exc):
                        # Ambiguous bracket outcome: the parent may have been
                        # accepted, so never fall through to a second POST.
                        self._store.transition_order(order_id, "UNKNOWN")
                        return {
                            "ok": False,
                            "status": "UNKNOWN",
                            "client_order_id": client_oid,
                            "broker_calls": 0,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    # A protective-leg rejection is not ambiguous: the parent
                    # was not accepted, so fall through to a plain market
                    # order instead of marking UNKNOWN.
                    protective_error = str(exc)
                    resp = None
            if resp is None and protective_request is not None and protective_error is not None:
                try:
                    resp = broker.submit_order(request)
                    broker_calls += 1
                except Exception as exc:
                    if _is_ambiguous_error(exc):
                        self._store.transition_order(order_id, "UNKNOWN")
                        return {
                            "ok": False,
                            "status": "UNKNOWN",
                            "client_order_id": client_oid,
                            "broker_calls": 0,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": 0,
                        "error": str(exc),
                    }
            if resp is None:
                try:
                    resp = broker.submit_order(request)
                    broker_calls = 1
                except Exception as exc:
                    if _is_ambiguous_error(exc):
                        # Ambiguous POST outcome: mark UNKNOWN, never retry here.
                        # Lookup by client_order_id (A2 orchestration) resolves later.
                        self._store.transition_order(order_id, "UNKNOWN")
                        return {
                            "ok": False,
                            "status": "UNKNOWN",
                            "client_order_id": client_oid,
                            "broker_calls": 0,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": 0,
                        "error": str(exc),
                    }
            if protective_request is not None and protective_error is None:
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
            local = _broker_status_to_local(status_raw)
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
            if protective_error is not None:
                submitted["protective_fallback"] = True
                submitted["protective_error"] = protective_error
            return submitted
        except Exception as exc:
            if _is_ambiguous_error(exc):
                # Ambiguous POST outcome: mark UNKNOWN, never retry here.
                # Lookup by client_order_id (A2 orchestration) resolves later.
                self._store.transition_order(order_id, "UNKNOWN")
                return {
                    "ok": False,
                    "status": "UNKNOWN",
                    "client_order_id": client_oid,
                    "broker_calls": 0,
                    "error": f"ambiguous submit outcome: {exc}",
                }
            self._store.transition_order(order_id, "REJECTED")
            return {
                "ok": False,
                "status": "REJECTED",
                "client_order_id": client_oid,
                "broker_calls": 0,
                "error": str(exc),
            }

    # -- liquidation (same trust boundary) ---------------------------------

    def liquidate(
        self, symbol: str, *, decision_id: Optional[str] = None, run_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Risk-reducing exit through the same durable boundary."""
        sym = (symbol or "").upper()
        if not sym:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": "missing symbol for liquidation",
            }
        did = decision_id or f"liq-{sym}-{client_order_id_for('liq', sym, 'sell', role='close')[-8:]}"
        # Deterministic liquidation decision: reuse close role so retries
        # map to the same client_order_id.
        client_oid = client_order_id_for(did, sym, "sell", role="close", seq=0)
        try:
            intent_row, order_rows, created = self._store.create_outbox(
                decision_id=did,
                run_id=run_id,
                symbol=sym,
                action="SELL",
                target_position="NEUTRAL",
                payload_json=json.dumps(
                    {"symbol": sym, "action": "SELL", "kind": "liquidation"},
                    sort_keys=True,
                ),
                orders=[
                    {
                        "client_order_id": client_oid,
                        "symbol": sym,
                        "side": "sell",
                        "quantity": None,
                        "notional": None,
                    }
                ],
            )
        except Exception as exc:
            return {
                "success": False,
                "fail_closed": True,
                "broker_attempted": False,
                "broker_calls": 0,
                "error": f"durable commit failed: {exc}",
            }
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
            broker = self._broker_factory()
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
        try:
            resp = broker.close_position(sym.replace("/", ""))
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
            local = _broker_status_to_local(status_raw)
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
            if _is_ambiguous_error(exc):
                self._store.transition_order(orow["order_id"], "UNKNOWN")
                return {
                    "success": False,
                    "status": "UNKNOWN",
                    "broker_attempted": False,
                    "broker_calls": 0,
                    "error": f"ambiguous close outcome: {exc}",
                    "intent_id": intent_row["intent_id"],
                    "decision_id": did,
                }
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

    # -- UNKNOWN lookup/adopt seam (minimal for A1) -------------------------

    def lookup_unknown(self, client_order_id: str) -> dict[str, Any]:
        """Query the broker by client_order_id and adopt the observed state.

        No second POST happens here. Returns adopted status or NOT_FOUND.
        Full account-wide reconciliation stays in Phase A.2.
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
        broker_order = None
        broker_oid = None
        status_raw = None
        lookup_errors: list[str] = []
        for attr in ("get_order_by_client_order_id", "get_order_by_client_id"):
            getter = getattr(broker, attr, None)
            if callable(getter):
                try:
                    broker_order = getter(client_order_id)
                    break
                except Exception as exc:  # keep trying other seams
                    lookup_errors.append(f"{attr}: {exc}")
        if broker_order is None:
            lister = getattr(broker, "list_orders_by_client_ids", None)
            if callable(lister):
                try:
                    found = lister([client_order_id])
                    if found:
                        broker_order = found[0]
                except Exception as exc:
                    lookup_errors.append(f"list_orders_by_client_ids: {exc}")
        if broker_order is None:
            return {
                "found": False,
                "not_found": True,
                "order": existing,
                "detail": "; ".join(lookup_errors) or "broker has no such order",
            }
        if isinstance(broker_order, dict):
            broker_oid = broker_order.get("id") or broker_order.get("broker_order_id")
            status_raw = broker_order.get("status")
            filled = broker_order.get("filled_qty")
        else:
            broker_oid = getattr(broker_order, "id", None)
            status_raw = getattr(broker_order, "status", None)
            filled = getattr(broker_order, "filled_qty", None)
        local = _broker_status_to_local(status_raw)
        kwargs: dict[str, Any] = {}
        if broker_oid:
            kwargs["broker_order_id"] = str(broker_oid)
        if filled is not None:
            try:
                kwargs["filled_qty"] = float(filled)
            except (TypeError, ValueError):
                pass
        ok, row = self._store.transition_order(existing["order_id"], local, **kwargs)
        return {"found": True, "adopted": ok, "order": row, "status": local}


def _service(db_path=None, broker_factory=None) -> ExecutionService:
    return ExecutionService(db_path=db_path, broker_factory=broker_factory)


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
) -> dict[str, Any]:
    """Compatibility wrapper keeping the legacy call shape.

    Production callers should prefer ExecutionService.execute(); this wrapper
    is the single durable trust boundary for intent-based execution, including
    deterministic sizing (risk_params) and broker-side protective legs.
    """
    svc = _service(db_path, broker_factory)
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
