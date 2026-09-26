"""Broker request construction and submit-outcome classification.

Moved verbatim from service.py; service.py retains explicit-signature
wrappers so its module attributes stay the patch seams tests resolve.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from .policy import canonical_protective_price

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


def _is_ambiguous_error(exc: BaseException, *, _exception_http_status, _TIMEOUT_MARKERS) -> bool:
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


def _definitive_rejection(exc: BaseException, *, _exception_http_status) -> bool:
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


class RequestBuildError(ValueError):
    """A locally constructed broker request failed before any POST."""




def _build_protective_request(
    symbol: str,
    side: str,
    qty: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    client_order_id: str,
    *,
    RequestBuildError,
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
    except (ImportError, ModuleNotFoundError):
        return None

    try:
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        stop_loss = (
            StopLossRequest(stop_price=canonical_protective_price(stop_loss_price))
            if stop_loss_price
            else None
        )
        take_profit = (
            TakeProfitRequest(limit_price=canonical_protective_price(take_profit_price))
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
    except Exception as exc:
        raise RequestBuildError(
            f"protective request construction failed for {symbol}: {exc}"
        ) from exc


def _build_market_request(symbol: str, side: str, notional, quantity, client_order_id: str):
    is_crypto = "/" in (symbol or "").upper()
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
    except (ImportError, ModuleNotFoundError):
        # Alpaca SDK unavailable (offline unit tests use mock brokers that
        # accept any request object). Return a minimal dict-like payload.
        tif_value = "gtc" if is_crypto else "day"
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
