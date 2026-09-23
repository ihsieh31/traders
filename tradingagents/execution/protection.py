"""Protection ownership, cancel races, stop coverage and persistent gaps.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from pathlib import Path
from tradingagents.execution.authority import BrokerSnapshot


from tradingagents.execution.authority import BrokerAuthorityError

class StaleRecoveryPositionError(BrokerAuthorityError):
    """N02: a recovery resubmit was blocked before any POST because the
    persisted position assumption would trade through the fresh broker
    position. Carries the flag so callers can surface
    ``stale_position_transition`` instead of a generic authority failure."""

    stale_position_transition = True


class ProtectionCancellationInterrupted(BrokerAuthorityError):
    def __init__(self, message: str, broker_calls: int):
        super().__init__(message)
        self.broker_calls = broker_calls


def _verify_owned_close_protections(
    self,
    broker,
    snapshot,
    symbol,
    *,
    BrokerAuthorityError,
    broker_status_to_local,
):
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
    market_closed = self._market_clock_closed(broker)
    if market_closed is not None:
        raise BrokerAuthorityError(
            f"Protection cancellation blocked: {market_closed}"
        )


def _broker_order_live(
    snapshot: BrokerSnapshot,
    broker_order_id: str,
    *,
    broker_status_to_local,
) -> bool:
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
    capture_broker_snapshot,
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
    # An equity DAY market close submitted after hours queues for the next
    # session. Keep the existing stop live unless the broker proves this
    # session is open immediately before the protection DELETE.
    market_closed = self._market_clock_closed(broker)
    if market_closed is not None:
        raise BrokerAuthorityError(
            f"Protection cancellation blocked: {market_closed}"
        )
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


def _cancel_owned_close_protections(
    self,
    broker,
    snapshot,
    symbol,
    *,
    BrokerAuthorityError,
    broker_status_to_local,
):
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
    try:
        for order in orders:
            snapshot, calls = self._cancel_protection_with_race_check(
                broker, snapshot, order, account_id=snapshot.account_id,
            )
            canceled_calls += calls
    except Exception as exc:
        if canceled_calls:
            self._evaluate_protection_gap(
                broker, snapshot, symbol, canceled_protections=canceled_calls,
            )
            raise ProtectionCancellationInterrupted(str(exc), canceled_calls) from exc
        raise
    if canceled_calls:
        market_closed = self._market_clock_closed(broker)
        if market_closed is not None:
            self._evaluate_protection_gap(
                broker, snapshot, symbol, canceled_protections=canceled_calls,
            )
            raise ProtectionCancellationInterrupted(
                f"Protection cancellation blocked after broker DELETE: {market_closed}",
                canceled_calls,
            )
    return snapshot, canceled_calls


def _program_owned_live_reducing_qty(
    self,
    snapshot: BrokerSnapshot,
    symbol: str,
    reducing_side: str,
    *,
    broker_status_to_local,
) -> float:
    """Proven stop coverage plus live program-owned market closes.

        Siblings cover one lot only. A take-profit child is not downside
        protection, and an unreadable parent relation cannot prove a close.
        """
    covered = 0.0
    group_best: dict[str, float] = {}
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
        remaining = max(0.0, float(order.qty or 0) - float(order.filled_qty or 0))
        try:
            parent_id = self._store.protective_parent(local["order_id"])
        except Exception:
            continue
        if parent_id:
            if order.order_type not in {"stop", "stop_limit", "trailing_stop"}:
                continue
            if str(order.status).lower() == "held":
                parent = self._store.get_order(parent_id)
                if not parent or str(parent.get("status") or "").upper() != "FILLED":
                    continue
            group_best[parent_id] = max(group_best.get(parent_id, 0.0), remaining)
        elif order.order_type == "market":
            covered += remaining
    return covered + sum(group_best.values())


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
    self,
    snapshot: BrokerSnapshot,
    result: Any,
    prior_reasons: list[str],
    *,
    ReconciliationResult,
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


def _protection_coverage_gaps(self, snapshot: BrokerSnapshot, *, json) -> list[str]:
    """Require coverage only for unspent, protected program opening lots.

    Parent terminal status never erases a filled lot. Historical parents and
    children whose opening lots were consumed cannot bind later manual lots.
    An unreadable ledger is an inability to prove safety, not an empty book.
    """
    from .lifecycle import remaining_lots

    try:
        lots_by_symbol = remaining_lots(self._store)
        protected_parents = {
            parent for row in self._store.list_all_orders()
            if (parent := self._store.protective_parent(row["order_id"]))
        }
    except Exception as exc:
        return [f"PROTECTION_GAP: durable protection ledger is unreadable: {exc}"]
    gaps: list[str] = []
    for position in snapshot.positions:
        if abs(position.qty) <= 1e-9:
            continue
        symbol = position.symbol
        expected_target = "LONG" if position.qty > 0 else "SHORT"
        required = 0.0
        try:
            for lot in lots_by_symbol.get(symbol, []):
                if float(lot["qty"]) * position.qty <= 0:
                    continue
                payload = json.loads(lot["intent"].get("payload_json") or "{}")
                controls = payload.get("risk_controls") or {}
                protected = lot["order"]["order_id"] in protected_parents or (
                    payload.get("kind") != "liquidation"
                    and str(payload.get("target_position") or "").upper() == expected_target
                    and bool(controls.get("stop_loss_price"))
                )
                if protected:
                    required += abs(float(lot["qty"]))
        except Exception as exc:
            gaps.append(f"PROTECTION_GAP: {symbol} protection intent is unreadable: {exc}")
            continue
        required = min(required, abs(position.qty))
        if required <= 1e-9:
            continue
        reducing_side = "sell" if position.qty > 0 else "buy"
        covered = self._program_owned_live_reducing_qty(snapshot, symbol, reducing_side)
        if covered < required - 1e-8:
            gaps.append(
                f"PROTECTION_GAP: {symbol} remains exposed with no proven live "
                f"protection or program-owned close covering the "
                f"{required:g}-share position; operator review required"
            )
    return gaps


def _apply_protection_coverage(
    self,
    snapshot: BrokerSnapshot,
    result: Any,
    *,
    ReconciliationResult,
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


def _verified_reducing_exit(
    snapshot: BrokerSnapshot,
    symbol: str,
    specs: list[dict[str, Any]],
    *,
    broker_status_to_local,
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
