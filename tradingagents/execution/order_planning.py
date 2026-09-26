"""TradeIntent validation and deterministic order planning.

Moved verbatim from service.py; service.py retains explicit-signature
wrappers delegating with named collaborators at call time.
"""

from __future__ import annotations

from typing import Any, Optional


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

    from .policy import canonical_protective_price
    out: dict[str, float] = {}
    if stop_price:
        out["stop_loss_price"] = canonical_protective_price(stop_price)
    if target_price:
        out["take_profit_price"] = canonical_protective_price(target_price)
    return out or None


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
