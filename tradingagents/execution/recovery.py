"""Durable recovery lookup, adoption, resubmission and reconciliation.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

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


def _adopt_recovery_order(
    self,
    local: dict[str, Any],
    found: Any,
    *,
    BrokerAuthorityError,
    broker_status_to_local,
) -> None:
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


def _evaluate_opening_caps(
    *,
    symbol: str,
    specs: list[dict[str, Any]],
    snapshot: Any,
    quote: Any,
    intent_dict: dict[str, Any],
    quote_factory: Optional[Callable[[str], Any]] = None,
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
