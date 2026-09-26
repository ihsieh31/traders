"""Initial submission and the final opening dispatch proof.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional
from pathlib import Path
from tradingagents.execution.authority import (
    BrokerAuthorityError, BrokerSnapshot, SNAPSHOT_TTL_SECONDS, validate_freshness,
)
from tradingagents.app_identity import get_env


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
    stamp = getattr(clock, "timestamp", None)
    if stamp is None and isinstance(clock, dict):
        stamp = clock.get("timestamp")
    try:
        validate_freshness(
            stamp,
            ttl_seconds=float(get_env("SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)),
            label="broker clock",
        )
    except (BrokerAuthorityError, TypeError, ValueError) as exc:
        return f"broker market clock unavailable ({exc}); refusing to open exposure"
    return None


def _short_opening_rejection(
    broker: Any, symbol: str, snapshot: BrokerSnapshot
) -> Optional[str]:
    """Fail closed unless account and Alpaca asset facts authorize a short."""
    if snapshot.shorting_enabled is not True:
        return (
            "short opening rejected: broker account shorting_enabled is false "
            "or unavailable"
        )
    get_asset = getattr(broker, "get_asset", None)
    if not callable(get_asset):
        return "short opening rejected: broker asset lookup is unavailable"
    try:
        asset = get_asset(symbol)
    except Exception:
        return "short opening rejected: broker asset lookup failed"
    if asset is None:
        return "short opening rejected: broker asset lookup returned no asset"

    def field(name: str, alias: Optional[str] = None) -> Any:
        try:
            if isinstance(asset, dict):
                value = asset.get(name)
                return asset.get(alias) if value is None and alias else value
            value = getattr(asset, name, None)
            return getattr(asset, alias, None) if value is None and alias else value
        except Exception:
            return None

    def normalized(value: Any) -> str:
        if value is None:
            return ""
        try:
            return str(getattr(value, "value", value)).strip().lower()
        except Exception:
            return ""

    if normalized(field("symbol")) != str(symbol).strip().lower():
        return "short opening rejected: broker asset symbol does not match the order"
    if normalized(field("asset_class", "class")) != "us_equity":
        return "short opening rejected: asset is not a US equity"
    if normalized(field("status")) != "active":
        return "short opening rejected: asset is not active"
    if field("tradable") is not True:
        return "short opening rejected: asset is not tradable"
    if field("shortable") is not True:
        return "short opening rejected: asset is not marked shortable"

    borrow_status = normalized(field("borrow_status"))
    if borrow_status == "hard_to_borrow":
        return "short opening rejected: asset is hard_to_borrow"
    if borrow_status != "easy_to_borrow":
        return "short opening rejected: asset borrow_status is unavailable or unsupported"

    try:
        equity = float(snapshot.equity)
    except (TypeError, ValueError, OverflowError):
        return "short opening rejected: authoritative account equity is unavailable"
    if not math.isfinite(equity) or equity < 2000.0:
        return "short opening rejected: authoritative account equity is below $2,000"
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
    BrokerAuthorityError,
    SNAPSHOT_TTL_SECONDS,
    os,
    validate_freshness,
    validate_quote,
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
    if spec.get("role") == "open" and spec.get("side") == "sell":
        short_rejection = _short_opening_rejection(broker, symbol, snapshot)
        if short_rejection is not None:
            return f"dispatch revalidation failed: {short_rejection}"
    # Final time-sensitive proof, after every blocking GET above.
    try:
        validate_freshness(
            snapshot.observed_at,
            ttl_seconds=float(
                get_env("SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)
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
    RequestBuildError,
    _build_market_request,
    _build_protective_request,
    _definitive_rejection,
    _submit_authority_error,
    broker_status_to_local,
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
                    "protective_required_rejected": True,
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
        authority_error = _submit_authority_error(
            risk_reducing=spec.get("role") != "open", can_submit=can_submit
        )
        if authority_error:
            self._store.transition_order(order_id, "CANCELED")
            return {
                "ok": False,
                "status": "CANCELED",
                "pre_submit_blocked": True,
                "fail_closed": True,
                "client_order_id": client_oid,
                "broker_calls": 0,
                "error": f"{authority_error} (no broker POST was made)",
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
    except RequestBuildError as exc:
        self._store.transition_order(order_id, "REJECTED")
        return {
            "ok": False,
            "status": "REJECTED",
            "client_order_id": client_oid,
            "broker_calls": 0,
            "error": str(exc),
        }
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


class ExecutionConfigUnavailable(RuntimeError):
    """Opening authority cannot read the effective execution configuration."""


def _get_execution_config() -> dict:
    try:
        from tradingagents.dataflows.config import get_config

        return get_config() or {}
    except Exception as exc:
        raise ExecutionConfigUnavailable(f"execution configuration unavailable: {exc}") from exc



def _submit_authority_error(
    *, risk_reducing: bool, can_submit: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """Return the one final-boundary reason that forbids a broker POST."""
    if not risk_reducing and can_submit is not None:
        try:
            if not can_submit():
                return "stop/window authority revoked before the final submit"
        except Exception as exc:
            return f"stop/window authority unavailable before the final submit: {exc}"
    # Fail closed: an unavailable safety guard must never widen the final
    # submit boundary (N08 sibling _execute_core already refuses on the same
    # condition). Recovery resubmits reach this helper without that earlier
    # check, so this is the only guard standing there.
    try:
        from tradingagents.safety import get_safety_guard

        guard = get_safety_guard()
    except Exception as exc:
        return f"safety guard unavailable before the final submit: {exc}"
    if (
        callable(getattr(guard, "kill_switch_active", None))
        and guard.kill_switch_active() is True
    ):
        # The flag file is an operator control: it binds even when the safety
        # layer's config disables the guard itself.
        return "kill switch engaged before the final submit"
    return None
