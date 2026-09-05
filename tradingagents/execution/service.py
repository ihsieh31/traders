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
    broker_status_to_local,
    capture_broker_snapshot,
    capture_quote,
    utc_now,
    validate_quote,
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


def _get_execution_config() -> dict:
    try:
        from tradingagents.dataflows.config import get_config

        return get_config() or {}
    except Exception:
        return {}


def _evaluate_opening_caps(
    *,
    symbol: str,
    specs: list[dict[str, Any]],
    snapshot: Any,
    quote: Any,
    intent_dict: dict[str, Any],
):
    """Run the Phase B deterministic exposure evaluator for opening legs.

    A verified close leg in the same intent (close-then-open flip) credits
    its freed market value so only the increasing part is clipped.
    Quantity-based legs are valued with the execution quote.
    """
    from tradingagents.risk.exposure import evaluate_opening_exposure

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
    return evaluate_opening_exposure(
        symbol=symbol,
        proposed_notional=proposed_notional,
        snapshot=snapshot,
        quote_price=quote_price,
        symbol_cap_pct=float(config.get("max_symbol_concentration_pct", 25.0) or 0),
        sector_cap_pct=config.get("max_sector_exposure_pct", 30.0),
        gross_cap_pct=config.get("portfolio_max_gross_exposure_pct", 100.0),
        sector_mapping=dict(config.get("sector_mapping") or {}),
        planned_close_reduction=planned_close_reduction,
    )


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
        """Execute under one verified account lock and one authority snapshot."""
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
            )
        specs = _planned_order_specs(intent_dict, dollar_amount)
        if not specs:  # HOLD has no execution facts to gate.
            return self._execute_core(
                trade_intent=trade_intent,
                decision_id=decision_id,
                run_id=run_id,
                dollar_amount=dollar_amount,
                allow_shorts=allow_shorts,
                risk_params=risk_params,
                current_position=current_position,
            )
        # Phase B corporate-action quarantine: a quarantined symbol takes no
        # new exposure (zero broker calls). Verified reducing exits keep the
        # Phase A path and are checked below under the account lock.
        if any(spec.get("role") == "open" for spec in specs):
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
                }
        try:
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                snapshot, reconciliation = self._recover_locked(broker, snapshot)
                opening = any(spec.get("role") == "open" for spec in specs)
                closing_specs = [spec for spec in specs if spec.get("role") == "close"]
                if not reconciliation.clean and opening:
                    return self._paused_result(snapshot, reconciliation.reasons)
                if closing_specs and not self._verified_reducing_exit(
                    snapshot, intent_dict.get("symbol", ""), closing_specs
                ):
                    return self._paused_result(
                        snapshot,
                        list(reconciliation.reasons)
                        + ["close requires a fresh matching broker position and no conflicting close order"],
                    )
                # Phase C entry gate: in auto-screening mode only today's
                # validated Top20 may open exposure. Program-derived and
                # re-verified inside the single execution entry, so direct
                # callers and checkpoint resumes cannot bypass it. HOLD and
                # verified reducing exits are untouched; recovery of already
                # authorized orders has already happened above.
                if opening:
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
                    if opening else None
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
                    _broker=broker,
                    _snapshot=snapshot,
                    _quote=quote,
                )
                result["preflight_snapshot_version"] = snapshot.version
                try:
                    after = capture_broker_snapshot(
                        broker, expected_account_id=snapshot.account_id
                    )
                    post = Reconciler(self._store).reconcile(after)
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
        _broker: Any = None,
        _snapshot: Optional[BrokerSnapshot] = None,
        _quote: Any = None,
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
                    authoritative_snapshot=_snapshot,
                    authoritative_quote=_quote,
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
        if _snapshot is not None:
            verified = _snapshot.position(symbol)
            for spec in specs:
                if spec.get("role") == "close" and verified is not None:
                    spec["quantity"] = abs(verified.qty)
                    spec["notional"] = None
                    spec["side"] = "sell" if verified.qty > 0 else "buy"

        # Phase B deterministic exposure caps: clip the increasing leg of
        # every exposure-adding order (fresh opens AND increases of an
        # existing position) to the single canonical symbol cap, the sector
        # cap, the gross cap and cash — with outstanding open orders counted
        # against headroom. Verified reducing exits never pass through here.
        exposure_check_info: Optional[dict] = None
        adds_exposure = target_position_open is not None
        if adds_exposure and _snapshot is not None and any(
            spec.get("role") == "open" for spec in specs
        ):
            cap_result = _evaluate_opening_caps(
                symbol=symbol,
                specs=specs,
                snapshot=_snapshot,
                quote=_quote,
                intent_dict=intent_dict,
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
                    qty_int = int(float(s.get("notional") or effective_amount or 0.0) / _quote.price)
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
                        account={"equity": _snapshot.equity} if _snapshot else None,
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
                broker=_broker,
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
        live_statuses = {"new", "accepted", "pending_new", "partially_filled", "partial"}
        return not any(
            order.symbol == position.symbol
            and order.side == expected_side
            and order.status in live_statuses
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
            filled_qty=float(filled or 0),
        )

    def _resubmit_recovered(
        self, broker: Any, local: dict[str, Any], snapshot: BrokerSnapshot
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
        risk_reducing = bool(
            position
            and ((position.qty > 0 and side == "sell") or (position.qty < 0 and side == "buy"))
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
                )
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
                account={"equity": snapshot.equity},
                position_value=abs(position.market_value) if position else 0.0,
                risk_reducing=risk_reducing,
            )
        except Exception as exc:
            raise BrokerAuthorityError(f"recovery safety policy unavailable: {exc}") from exc
        if verdict is not None and not verdict.allowed:
            self._store.transition_order(local["order_id"], "CANCELED")
            return
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
        if request is None:
            self._store.transition_order(current["order_id"], "REJECTED")
            raise BrokerAuthorityError(
                f"recovery order has no valid size: {local['client_order_id']}"
            )
        try:
            response = broker.submit_order(request)
        except Exception as exc:
            terminal = not _is_ambiguous_error(exc)
            self._store.transition_order(
                current["order_id"], "REJECTED" if terminal else "UNKNOWN"
            )
            raise BrokerAuthorityError(
                f"recovery submit {'rejected' if terminal else 'outcome is uncertain'}: "
                f"{local['client_order_id']}: {exc}"
            ) from exc
        self._adopt_recovery_order(current, response)

    def _recover_locked(
        self, broker: Any, snapshot: BrokerSnapshot
    ) -> tuple[BrokerSnapshot, Any]:
        """Resolve durable nonterminal rows before permitting new exposure."""
        initial = Reconciler(self._store).reconcile(snapshot)
        recoverable_reasons = (
            "unresolved PENDING order:",
            "unresolved UNKNOWN order:",
        )
        if any(not reason.startswith(recoverable_reasons) for reason in initial.reasons):
            return snapshot, initial
        broker_clients = {order.client_order_id for order in snapshot.orders}
        changed = False
        for local in self._store.list_recoverable_orders():
            if local["client_order_id"] in broker_clients:
                continue
            status = str(local["status"]).upper()
            found = self._lookup_for_recovery(broker, local["client_order_id"])
            if found is not None:
                self._adopt_recovery_order(local, found)
                changed = True
            elif status in {"PENDING", "UNKNOWN"}:
                self._resubmit_recovered(broker, local, snapshot)
                changed = True
            else:
                # SUBMITTING/PARTIAL without a broker fact is not safe to replay.
                continue
        if not changed:
            return snapshot, initial
        refreshed = capture_broker_snapshot(broker, expected_account_id=snapshot.account_id)
        return refreshed, Reconciler(self._store).reconcile(refreshed)

    def startup_recover(self) -> dict[str, Any]:
        """Scheduler/startup gate: recover first; only CLEAN may auto-trade."""
        try:
            broker = self._broker_factory()
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                snapshot, result = self._recover_locked(broker, snapshot)
                return {
                    "success": result.clean,
                    "account_execution_state": result.state,
                    "reconciliation_reasons": list(result.reasons),
                    "snapshot_version": snapshot.version,
                    "account_id": snapshot.account_id,
                }
        except (BrokerAuthorityError, AccountLockBusy) as exc:
            return {
                "success": False,
                "account_execution_state": "PAUSED",
                "reconciliation_reasons": [str(exc)],
                "error": str(exc),
            }

    def account_status(self) -> dict[str, Any]:
        """Return the last durable CLEAN/PAUSED status for operator displays."""
        result: dict[str, Any]
        try:
            broker = self._broker_factory()
            snapshot = capture_broker_snapshot(broker)
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

    def _submit_one(
        self, *, order_row: dict[str, Any], spec: dict[str, Any], symbol: str,
        intent_dict: dict[str, Any], broker: Any = None,
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
                            "broker_calls": 1,
                            "error": f"ambiguous submit outcome: {exc}",
                        }
                    # An explicit validation/rejection is terminal. Do not
                    # turn it into a second, less-protected POST.
                    self._store.transition_order(order_id, "REJECTED")
                    return {
                        "ok": False,
                        "status": "REJECTED",
                        "client_order_id": client_oid,
                        "broker_calls": 1,
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
            if _is_ambiguous_error(exc):
                # Ambiguous POST outcome: mark UNKNOWN, never retry here.
                # Lookup by client_order_id (A2 orchestration) resolves later.
                # Report the real POST count: the inner submit handlers account
                # for their own attempts; this handler also serves pre-submit
                # failures, where zero POSTs happened.
                self._store.transition_order(order_id, "UNKNOWN")
                return {
                    "ok": False,
                    "status": "UNKNOWN",
                    "client_order_id": client_oid,
                    "broker_calls": broker_calls,
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
        """Execute a broker-verified, exposure-reducing close under the account lock."""
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
                spec = [{"role": "close", "side": side, "quantity": abs(position.qty) if position else None}]
                if not self._verified_reducing_exit(snapshot, sym, spec):
                    reasons = list(reconciliation.reasons) + [
                        "liquidation requires a fresh broker position and no conflicting close order"
                    ]
                    return self._paused_result(snapshot, reasons)
                try:
                    from tradingagents.safety import get_safety_guard

                    guard = get_safety_guard()
                    verdict = (
                        guard.check_order(
                            sym,
                            abs(position.market_value),
                            account={"equity": snapshot.equity},
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
                result = self._liquidate_core(
                    sym,
                    decision_id=decision_id,
                    run_id=run_id,
                    _broker=broker,
                    _quantity=abs(position.qty),
                    _side=side,
                )
                try:
                    after = capture_broker_snapshot(
                        broker, expected_account_id=snapshot.account_id
                    )
                    post = Reconciler(self._store).reconcile(after)
                    result["account_execution_state"] = post.state
                    result["reconciliation_reasons"] = list(post.reasons)
                    result["snapshot_version"] = after.version
                    if not post.clean:
                        result["paused"] = True
                except BrokerAuthorityError as exc:
                    result["account_execution_state"] = "PAUSED"
                    result["reconciliation_reasons"] = [str(exc)]
                    result["paused"] = True
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

    def _liquidate_core(
        self,
        symbol: str,
        *,
        decision_id: Optional[str] = None,
        run_id: Optional[str] = None,
        _broker: Any = None,
        _quantity: Optional[float] = None,
        _side: str = "sell",
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
        # Each liquidation is its own operator decision: without an explicit
        # decision_id the default is per-call, so a later legitimate exit of
        # the same symbol is never silently deduped against an older one.
        # Repeat/concurrent safety comes from _verified_reducing_exit (fresh
        # broker position + no conflicting live close order), not from ID reuse.
        did = decision_id or f"liq-{sym}-{utc_now().strftime('%Y%m%dT%H%M%S%f')}"
        client_oid = client_order_id_for(did, sym, _side, role="close", seq=0)
        try:
            intent_row, order_rows, created = self._store.create_outbox(
                decision_id=did,
                run_id=run_id,
                symbol=sym,
                action="SELL" if _side == "sell" else "BUY",
                target_position="NEUTRAL",
                payload_json=json.dumps(
                    {
                        "symbol": sym,
                        "action": "SELL" if _side == "sell" else "BUY",
                        "kind": "liquidation",
                    },
                    sort_keys=True,
                ),
                orders=[
                    {
                        "client_order_id": client_oid,
                        "symbol": sym,
                        "side": _side,
                        "quantity": _quantity,
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
        try:
            request = _build_market_request(
                sym, _side, None, _quantity, client_oid
            )
            if request is None:
                raise ValueError("verified close quantity is unavailable")
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
            if _is_ambiguous_error(exc):
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
                "broker_attempted": True,
                "broker_calls": 1,
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
