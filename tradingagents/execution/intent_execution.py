"""Intent execution: sizing, outbox, replay and per-spec dispatch.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from pathlib import Path
from tradingagents.execution.authority import BrokerSnapshot


def _completed_replay_result(
    self,
    *,
    decision_id: str,
    intent_dict: dict[str, Any],
    json_module: Any,
) -> dict[str, Any] | None:
    """Return success only for a proven, identity-matching FILLED replay.

    This narrow read-only path must run before fresh-position and entry-policy
    gates.  It does not infer success from the broker position and never
    treats non-terminal or uncertain order states as completed.
    """
    existing = self._store.get_intent_by_decision(decision_id)
    if existing is None:
        return None
    orders = self._store.list_orders_for_intent(existing["intent_id"])
    if not orders or {
        str(order.get("status") or "").upper() for order in orders
    } != {"FILLED"}:
        return None
    try:
        stored_payload = json_module.loads(existing.get("payload_json") or "")
    except (TypeError, ValueError):
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "replay_identity_mismatch": True,
            "decision_id": decision_id,
            "error": "completed replay payload is unreadable",
        }
    if stored_payload != intent_dict:
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "replay_identity_mismatch": True,
            "decision_id": decision_id,
            "error": "completed replay payload does not match decision identity",
        }
    return {
        "success": True,
        "deduped": True,
        "broker_attempted": False,
        "broker_calls": 0,
        "intent_id": existing["intent_id"],
        "decision_id": decision_id,
        "trade_intent": intent_dict,
        "orders": orders,
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
    _evaluate_opening_caps,
    _planned_order_specs,
    _resolve_protective_prices,
    _resolve_qty,
    canonical_decision_id,
    client_order_id_for,
    json,
    validate_trade_intent,
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
    replay = _completed_replay_result(
        self,
        decision_id=did,
        intent_dict=intent_dict,
        json_module=json,
    )
    if replay is not None:
        return replay
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
