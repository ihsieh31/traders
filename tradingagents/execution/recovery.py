"""Durable recovery lookup, adoption, resubmission and reconciliation.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional
from pathlib import Path
from tradingagents.execution.authority import BrokerSnapshot


def _lookup_for_recovery(self, broker: Any, client_order_id: str, *, BrokerAuthorityError) -> Any:
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
            # R07: message text never proves an HTTP status. Only structured
            # 404 evidence (status_code, or its literal in a stringified
            # error when no structured code exists) may classify a lookup as
            # an explicit not-found; prose like "not found" on a 5xx must
            # stay uncertain and fail closed after the bounded retries.
            if status_code == 404 or (status_code is None and "404" in text):
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


def _adopt_recovery_order(
    self,
    local: dict[str, Any],
    found: Any,
    *,
    BrokerAuthorityError,
    broker_status_to_local,
) -> None:
    from .authority import _value, _number, _utc, _symbol
    import math

    broker_id = str(_value(found, "id") or "")
    status = _value(found, "status")
    side = str(_value(found, "side") or "").lower()
    if (not broker_id or not status
            or _value(found, "client_order_id") != local["client_order_id"]
            or _symbol(_value(found, "symbol")) != local["symbol"]
            or side != local["side"]
            or (local.get("broker_order_id") and local["broker_order_id"] != broker_id)):
        raise BrokerAuthorityError("recovered broker order identity does not match the durable order")
    filled = _number(_value(found, "filled_qty", default=0), field="recovered filled qty", minimum=0)
    recorded = float((self._store.get_order(local["order_id"]) or local).get("filled_qty") or 0)
    if local.get("quantity") is not None and filled > float(local["quantity"]) + 1e-9:
        raise BrokerAuthorityError("recovered fill exceeds the durable order quantity")
    if filled < recorded - 1e-9:
        raise BrokerAuthorityError("recovered cumulative fill quantity regressed")
    delta = filled - recorded
    if delta > 1e-9:
        average = _number(_value(found, "filled_avg_price"), field="recovered fill price", minimum=0)
        delta_cost = filled * average - self._store.recorded_fill_cost(local["order_id"])
        price = delta_cost / delta
        if not math.isfinite(price) or price <= 0:
            raise BrokerAuthorityError("recovered cumulative fill economics are invalid")
        stamp = _utc(_value(found, "updated_at", "filled_at", "submitted_at"),
                     field="recovered fill timestamp")
        self._store.record_fill(execution_id=f"{broker_id}:{filled:.12g}",
                               order_id=local["order_id"], qty=delta, price=price,
                               filled_at=stamp.isoformat())
    self._store.sync_order_from_broker(
        local["order_id"], broker_status_to_local(status), broker_order_id=broker_id,
        filled_qty=filled,
    )


def _recovery_crossing_block(
    snapshot: BrokerSnapshot,
    symbol: str,
    opening_side: str,
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
    self,
    broker: Any,
    local: dict[str, Any],
    snapshot: BrokerSnapshot,
    can_submit: Optional[Callable[[], bool]] = None,
    maintenance: Optional[dict[str, Any]] = None,
    *,
    BrokerAuthorityError,
    StaleRecoveryPositionError,
    _build_market_request,
    _build_protective_request,
    _definitive_rejection,
    _evaluate_opening_caps,
    _get_execution_config,
    _planned_order_specs,
    _resolve_protective_prices,
    _submit_authority_error,
    client_order_id_for,
    json,
    validate_quote,
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
            intent = self._store.get_intent_for_order(local["order_id"])
            payload = json.loads((intent or {}).get("payload_json") or "{}")
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
                intent_dict=payload,
                quote_factory=self._quote_factory,
                execution_store=self._store,
                candidate_order_id=local["order_id"],
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
    # Phase B note: the Phase A "recovery target already exists" block is
    # superseded — an increase onto an existing same-side position is
    # allowed and clipped by the recomputed exposure caps above.
    recovery_controls = None
    if not risk_reducing:
        from .policy import entry_check
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
            spec={
                "role": "open",
                "side": side,
                "notional": effective_notional,
                "quantity": effective_quantity,
            },
            quote=quote,
            snapshot=snapshot,
            broker=broker,
        )
        if blocked:
            self._store.transition_order(local["order_id"], "CANCELED")
            raise BrokerAuthorityError(blocked)
    # R02 Layer 2 / N03: the submit-boundary authority check, run AFTER
    # every blocking GET and immediately before the PENDING/UNKNOWN ->
    # SUBMITTING transition. Refusal is NOT a rejection and NOT a CLEAN
    # outcome — and it provably made no POST, so the row keeps its exact
    # pre-transition status (PENDING stays PENDING, UNKNOWN stays
    # UNKNOWN): a later authorized resume re-enters the normal
    # adopt-or-resubmit path, and this round's reconciliation reports it
    # unresolved.
    authority_error = _submit_authority_error(
        risk_reducing=risk_reducing, can_submit=can_submit
    )
    if authority_error:
        raise BrokerAuthorityError(
            f"recovery resubmit blocked: {authority_error}: "
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


def _reconcile_snapshot(
    self,
    broker: Any,
    snapshot: BrokerSnapshot,
    *,
    BrokerAuthorityError,
    Reconciler,
    json,
):
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
    *,
    BrokerAuthorityError,
    capture_broker_snapshot,
    re,
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
    # Read-only adoption can explain an apparent position mismatch caused
    # by a fill outside the listing window. Prove those facts before the
    # gate that prevents any recovery POST; missing facts still pause.
    listed_clients = {order.client_order_id for order in snapshot.orders}
    adopted = False
    lookup_results = {}
    for local in self._store.list_recoverable_orders():
        if (str(local["status"]).upper() not in {"ACCEPTED", "SUBMITTING", "PARTIAL"}
                or local["client_order_id"] in listed_clients
                or self._store.protective_parent(local["order_id"])):
            continue
        found = self._lookup_for_recovery(broker, local["client_order_id"])
        lookup_results[local["client_order_id"]] = found
        if found is not None:
            self._adopt_recovery_order(local, found)
            adopted = True
    if adopted:
        snapshot = capture_broker_snapshot(broker, expected_account_id=snapshot.account_id)
    initial = self._apply_protection_coverage(
        snapshot, self._reconcile_snapshot(broker, snapshot)
    )
    recoverable_reasons = (
        "unresolved PENDING order:",
        "unresolved UNKNOWN order:",
        # F07: a crash mid-submit leaves a SUBMITTING row the broker may
        # or may not have accepted. It may enter the read-only
        # client-order-id lookup (adopt if found); it is never
        # auto-resubmitted — without a broker fact it stays unresolved
        # and the account stays paused.
        "unresolved SUBMITTING order:",
        # E04: a crash between broker accept and journal write leaves an
        # ACCEPTED row the broker snapshot may no longer list. It gets the
        # same read-only client-ID lookup as SUBMITTING: a broker terminal
        # fact (filled/canceled/expired) is adopted, anything else stays
        # unresolved and PAUSED. Never auto-resubmitted.
        "unresolved ACCEPTED order:",
    )
    def _explained_pending_close_gap() -> str | None:
        """Identify the sole durable, full-position close that explains a gap."""
        rows = self._store.list_recoverable_orders()
        if len(rows) != 1 or str(rows[0]["status"]).upper() != "PENDING":
            return None
        row = rows[0]
        position = snapshot.position(row["symbol"])
        if (position is None or row.get("notional") is not None
                or row.get("quantity") is None
                or not math.isfinite(float(row["quantity"]))
                or abs(float(row["quantity"]) - abs(position.qty)) > 1e-8
                or row["side"] != ("sell" if position.qty > 0 else "buy")):
            return None
        intent = self._store.get_intent_for_order(row["order_id"]) or {}
        try:
            payload = json.loads(intent.get("payload_json") or "{}")
        except (TypeError, ValueError):
            return None
        from .store import client_order_id_for
        if (payload.get("kind") != "liquidation"
                or intent.get("symbol") != row["symbol"]
                or intent.get("target_position") != "NEUTRAL"
                or row["client_order_id"] != client_order_id_for(
                    intent.get("decision_id", ""), row["symbol"], row["side"],
                    role="close", seq=0)):
            return None
        gap_prefix = f"PROTECTION_GAP: {row['symbol']} "
        if not any(reason.startswith(gap_prefix) for reason in initial.reasons):
            return None
        return row["client_order_id"]

    explained_close = _explained_pending_close_gap()
    explained_symbol = self._store.list_recoverable_orders()[0]["symbol"] if explained_close else None
    if any(not reason.startswith(recoverable_reasons)
           and not (explained_symbol and reason.startswith(f"PROTECTION_GAP: {explained_symbol} "))
           for reason in initial.reasons):
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
                r"unresolved (PENDING|UNKNOWN|SUBMITTING|ACCEPTED|PARTIAL) order: (\S+)$",
                reason,
            )
            if match and match.group(2) in (queued - processed):
                continue
            blocking.append(reason)
        return blocking
    for local in list(self._store.list_recoverable_orders()):
        if explained_close and local["client_order_id"] != explained_close:
            break
        if local["client_order_id"] in _broker_clients(snapshot):
            continue
        if self._store.protective_parent(local["order_id"]):
            raise BrokerAuthorityError("Missing protective child must be reconciled; never resubmit it as a market order")
        status = str(local["status"]).upper()
        found = (lookup_results[local["client_order_id"]]
                 if local["client_order_id"] in lookup_results
                 else self._lookup_for_recovery(broker, local["client_order_id"]))
        if found is not None:
            self._adopt_recovery_order(local, found)
            changed = True
        elif status == "PENDING":
            self._resubmit_recovered(broker, local, snapshot, can_submit=can_submit,
                                     maintenance=maintenance)
            changed = True
        else:
            # UNKNOWN means a previous POST may have reached the broker. A
            # bounded series of 404s cannot prove that it did not. Keep the
            # original identity unresolved and the account paused.
            # SUBMITTING/ACCEPTED/PARTIAL have the same no-replay rule.
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
            step_result = self._apply_protection_coverage(
                snapshot, self._reconcile_snapshot(broker, snapshot)
            )
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


def _stop_risk_number(value: Any, label: str, *, BrokerAuthorityError, allow_zero=False) -> float:
    if isinstance(value, bool):
        raise BrokerAuthorityError(f"invalid {label}: boolean is not a risk value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrokerAuthorityError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise BrokerAuthorityError(f"invalid {label}: {value!r}")
    return number


def _signed_risk_number(value: Any, label: str, *, BrokerAuthorityError, allow_zero=False) -> float:
    if isinstance(value, bool):
        raise BrokerAuthorityError(f"invalid {label}: boolean is not a risk value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrokerAuthorityError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(number) or (number == 0 and not allow_zero):
        raise BrokerAuthorityError(f"invalid {label}: {value!r}")
    return number


def _risk_terms_for_plan(payload: dict, *, BrokerAuthorityError, require_two_to_one: bool):
    from .policy import worst_case_risk_reward

    controls = payload.get("risk_controls") or {}
    policy = payload.get("entry_policy") or {}
    terms = worst_case_risk_reward(
        target_position=payload.get("target_position"),
        minimum_price=policy.get("minimum_price"),
        maximum_price=policy.get("maximum_price"),
        stop_loss_price=controls.get("stop_loss_price"),
        take_profit_price=controls.get("take_profit_price"),
    )
    if terms is None:
        raise BrokerAuthorityError("durable opening plan has invalid stop/target geometry")
    if require_two_to_one and terms.ratio < 2.0:
        raise BrokerAuthorityError(
            f"durable opening plan violates minimum 2:1 R/R ({terms.ratio:.2f}:1)"
        )
    return terms


def _portfolio_stop_risk_totals(
    *, execution_store: Any, snapshot: Any, candidate_order_id: Optional[str],
    broker_status_to_local, BrokerAuthorityError,
) -> tuple[float, float]:
    """Return active and pending stop-risk reservations."""
    from .lifecycle import remaining_lots
    from .order_planning import _planned_order_specs
    from .store import client_order_id_for
    from tradingagents.risk.exposure import remaining_position_stop_risk

    if execution_store is None:
        raise BrokerAuthorityError("durable execution ledger unavailable; refusing new exposure")
    if execution_store.account_binding_owner() != snapshot.account_id:
        raise BrokerAuthorityError("execution ledger account binding does not match broker snapshot")

    local_orders = execution_store.list_all_orders()
    local_by_client = {row["client_order_id"]: row for row in local_orders}
    broker_by_client: dict[str, Any] = {}
    for broker_order in snapshot.orders:
        client_id = str(broker_order.client_order_id or "")
        if client_id in broker_by_client:
            raise BrokerAuthorityError(f"duplicate broker order identity for {client_id}")
        broker_by_client[client_id] = broker_order
        status = broker_status_to_local(broker_order.status)
        if status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            continue
        local = local_by_client.get(client_id)
        if local is None:
            raise BrokerAuthorityError(
                f"unowned live broker order {client_id or '<missing client id>'}; refusing new exposure"
            )
        if local["order_id"] != candidate_order_id and execution_store.protective_parent(local["order_id"]) is None:
            if str(local.get("status") or "").upper() in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
                raise BrokerAuthorityError(f"broker order {client_id} is live but durable order is terminal")

    lots = remaining_lots(execution_store)
    lots_by_symbol: dict[str, list[dict]] = {}
    normalize = lambda value: str(value or "").upper().replace("/", "").replace("-", "")
    for raw_symbol, symbol_lots in lots.items():
        key = normalize(raw_symbol)
        lots_by_symbol.setdefault(key, []).extend(symbol_lots)

    broker_positions = {
        normalize(position.symbol): position
        for position in snapshot.positions
        if abs(float(position.qty)) > 1e-9
    }
    all_symbols = set(broker_positions) | set(lots_by_symbol)
    existing_risk = 0.0
    for symbol in all_symbols:
        position = broker_positions.get(symbol)
        symbol_lots = [lot for lot in lots_by_symbol.get(symbol, [])
                       if abs(_signed_risk_number(lot.get("qty"), "active lot quantity",
                                                  BrokerAuthorityError=BrokerAuthorityError)) > 1e-9]
        if position is None or not symbol_lots:
            raise BrokerAuthorityError(
                f"broker/ledger active position mismatch for {symbol}; refusing new exposure"
            )
        signed_broker_qty = _signed_risk_number(position.qty, f"{symbol} broker quantity",
                                                BrokerAuthorityError=BrokerAuthorityError)
        lot_qty = sum(float(lot["qty"]) for lot in symbol_lots)
        if (not math.isfinite(lot_qty) or not math.isclose(lot_qty, signed_broker_qty,
                                                           rel_tol=0, abs_tol=1e-6)):
            raise BrokerAuthorityError(
                f"broker/ledger quantity mismatch for {symbol}; refusing new exposure"
            )
        mark_value = position.current_price
        if mark_value is None:
            market_value = _signed_risk_number(
                position.market_value, f"{symbol} market value",
                BrokerAuthorityError=BrokerAuthorityError,
            )
            if market_value * signed_broker_qty <= 0:
                raise BrokerAuthorityError(f"cannot prove a current mark for {symbol}")
            mark_value = abs(market_value / signed_broker_qty)
        mark = _stop_risk_number(mark_value, f"{symbol} current mark",
                                 BrokerAuthorityError=BrokerAuthorityError)
        for lot in symbol_lots:
            quantity = float(lot["qty"])
            if (quantity > 0) != (signed_broker_qty > 0):
                raise BrokerAuthorityError(f"active lot direction mismatch for {symbol}")
            intent = lot.get("intent") or {}
            if normalize(intent.get("symbol")) != symbol:
                raise BrokerAuthorityError(f"active lot intent symbol mismatch for {symbol}")
            payload = json.loads(intent.get("payload_json") or "")
            target = str(payload.get("target_position") or "").upper()
            if target != ("LONG" if quantity > 0 else "SHORT"):
                raise BrokerAuthorityError(f"active lot direction is not proven by its TradeIntent for {symbol}")
            controls = payload.get("risk_controls") or {}
            stop = _stop_risk_number(
                controls.get("stop_loss_price"), f"{symbol} durable stop",
                BrokerAuthorityError=BrokerAuthorityError,
            )
            existing_risk += remaining_position_stop_risk(
                qty=quantity, current_mark=mark, stop_loss_price=stop,
            )
            if not math.isfinite(existing_risk):
                raise BrokerAuthorityError("aggregate active position stop risk is invalid")

    # A durable opening intent and its original stop/target define the
    # reservation. Protective children are not separate openings.
    fill_totals: dict[str, list[float]] = {}
    for fill in execution_store.list_fills_since(""):
        qty = _stop_risk_number(fill.get("qty"), "durable fill quantity",
                                BrokerAuthorityError=BrokerAuthorityError)
        price = _stop_risk_number(fill.get("price"), "durable fill price",
                                  BrokerAuthorityError=BrokerAuthorityError)
        bucket = fill_totals.setdefault(fill["order_id"], [0.0, 0.0])
        bucket[0] += qty
        bucket[1] += qty * price
        if not all(math.isfinite(value) and value >= 0 for value in bucket):
            raise BrokerAuthorityError("durable fill totals are invalid")

    pending_risk = 0.0
    recoverable = {"PENDING", "SUBMITTING", "UNKNOWN", "PARTIAL", "ACCEPTED"}
    terminal = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
    for row in local_orders:
        if row.get("order_id") == candidate_order_id:
            continue
        if normalize(row.get("symbol")) == "__ACCOUNT__":
            continue
        row_status = str(row.get("status") or "").upper()
        if row_status in terminal:
            continue
        if row_status not in recoverable:
            raise BrokerAuthorityError(f"unknown durable order status {row_status!r}; refusing new exposure")
        if execution_store.protective_parent(row["order_id"]) is not None:
            continue
        broker_order = broker_by_client.get(row["client_order_id"])
        if (broker_order is not None
                and broker_status_to_local(broker_order.status) in terminal):
            continue
        intent = execution_store.get_intent_for_order(row["order_id"])
        if not intent:
            raise BrokerAuthorityError("recoverable durable order has no parent TradeIntent")
        payload = json.loads(intent.get("payload_json") or "")
        planned = _planned_order_specs(payload, None)
        matches = []
        for spec in planned:
            expected_client_id = client_order_id_for(
                intent.get("decision_id") or "", row["symbol"], spec["side"],
                role=spec["role"], seq=spec["seq"],
            )
            if expected_client_id == row["client_order_id"]:
                matches.append(spec)
        if not matches:
            side_matches = [spec for spec in planned if str(spec.get("side") or "").lower() == str(row.get("side") or "").lower()]
            if len(side_matches) == 1:
                matches = side_matches
            elif len(planned) == 1 and str(planned[0].get("side") or "").lower() == str(row.get("side") or "").lower():
                matches = planned
        if len(matches) != 1:
            raise BrokerAuthorityError(f"cannot prove opening/closing role for durable order {row['client_order_id']}")
        spec = matches[0]
        if spec.get("role") == "close":
            continue
        if spec.get("role") != "open":
            raise BrokerAuthorityError("durable order has an unknown exposure role")
        terms = _risk_terms_for_plan(
            payload, BrokerAuthorityError=BrokerAuthorityError, require_two_to_one=True,
        )
        if normalize(payload.get("symbol")) != normalize(row.get("symbol")):
            raise BrokerAuthorityError("pending opening order symbol conflicts with its TradeIntent")

        if broker_order is not None:
            broker_status = broker_status_to_local(broker_order.status)
            if broker_status in terminal:
                continue
            if (normalize(broker_order.symbol) != normalize(row.get("symbol"))
                    or str(broker_order.side).lower() != str(row.get("side") or "").lower()
                    or (row.get("broker_order_id") and row["broker_order_id"] != broker_order.broker_order_id)):
                raise BrokerAuthorityError(f"pending opening order identity mismatch for {row['client_order_id']}")

        filled_qty, filled_cost = fill_totals.get(row["order_id"], [0.0, 0.0])
        local_filled = _stop_risk_number(
            row.get("filled_qty"), "durable filled quantity",
            BrokerAuthorityError=BrokerAuthorityError, allow_zero=True,
        )
        fills_proven = (broker_order is not None
                        and broker_status_to_local(broker_order.status) not in terminal)
        if fills_proven:
            broker_filled = _stop_risk_number(
                broker_order.filled_qty, "broker filled quantity",
                BrokerAuthorityError=BrokerAuthorityError, allow_zero=True,
            )
            fills_proven = (math.isclose(filled_qty, local_filled, rel_tol=0, abs_tol=1e-8)
                            and math.isclose(filled_qty, broker_filled, rel_tol=0, abs_tol=1e-8))
        else:
            broker_filled = 0.0

        quantity = row.get("quantity")
        notional = row.get("notional")
        quantity_risk = 0.0
        notional_risk = 0.0
        if quantity is not None:
            planned_qty = _stop_risk_number(quantity, "durable opening quantity",
                                            BrokerAuthorityError=BrokerAuthorityError)
            if fills_proven and broker_order is not None:
                broker_qty = _stop_risk_number(broker_order.qty, "broker order quantity",
                                               BrokerAuthorityError=BrokerAuthorityError)
                if not math.isclose(planned_qty, broker_qty, rel_tol=0, abs_tol=1e-8):
                    fills_proven = False
            remaining_qty = max(planned_qty - filled_qty, 0.0) if fills_proven else planned_qty
            quantity_risk = remaining_qty * terms.risk_per_share
        if notional is not None:
            planned_notional = _stop_risk_number(notional, "durable opening notional",
                                                 BrokerAuthorityError=BrokerAuthorityError)
            remaining_notional = max(planned_notional - filled_cost, 0.0) if fills_proven else planned_notional
            notional_risk = remaining_notional * terms.risk_fraction
        reservation = max(quantity_risk, notional_risk)
        if not math.isfinite(reservation) or reservation < 0:
            raise BrokerAuthorityError("pending opening stop-risk reservation is invalid")
        pending_risk += reservation
        if not math.isfinite(pending_risk):
            raise BrokerAuthorityError("aggregate pending stop risk is invalid")

    return existing_risk, pending_risk


def _evaluate_opening_caps(
    *,
    symbol: str,
    specs: list[dict[str, Any]],
    snapshot: Any,
    quote: Any,
    intent_dict: dict[str, Any],
    quote_factory: Optional[Callable[[str], Any]] = None,
    execution_store: Any = None,
    candidate_order_id: Optional[str] = None,
    BrokerAuthorityError,
    _get_execution_config,
    broker_status_to_local,
    validate_quote,
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
        ExposureDecision,
        clip_to_portfolio_stop_risk,
        evaluate_opening_exposure,
        outstanding_increasing_notional,
    )

    config = _get_execution_config()
    existing_risk, pending_risk = _portfolio_stop_risk_totals(
        execution_store=execution_store,
        snapshot=snapshot,
        candidate_order_id=candidate_order_id,
        broker_status_to_local=broker_status_to_local,
        BrokerAuthorityError=BrokerAuthorityError,
    )
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

    cap_result = evaluate_opening_exposure(
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
    if not cap_result.approved:
        return cap_result
    terms = _risk_terms_for_plan(
        intent_dict, BrokerAuthorityError=BrokerAuthorityError, require_two_to_one=True,
    )
    stop_result = clip_to_portfolio_stop_risk(
        proposed_notional=cap_result.notional,
        equity=snapshot.equity,
        max_stop_risk_pct=config.get("portfolio_max_stop_risk_pct", 5.0),
        existing_position_risk=existing_risk,
        reserved_pending_risk=pending_risk,
        candidate_risk_fraction=terms.risk_fraction,
    )
    if not stop_result.approved:
        return stop_result
    return ExposureDecision(
        approved=True,
        notional=stop_result.notional,
        reason=(cap_result.reason if stop_result.notional >= cap_result.notional
                else f"{cap_result.reason}; clipped by portfolio stop-risk budget"),
        details={**cap_result.details, **stop_result.details,
                 "portfolio_stop_risk_cap": True},
    )
