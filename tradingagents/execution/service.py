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

from tradingagents.app_identity import app_home, get_env, validate_app_path

from tradingagents.execution import protection as _protection
from tradingagents.execution import recovery as _recovery
from tradingagents.execution import dispatch as _dispatch
from tradingagents.execution import exits as _exits
from tradingagents.execution import intent_execution as _intent_execution
from tradingagents.execution import order_planning as _order_planning
from tradingagents.execution import requests as _requests
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

_DEFAULT_DB = str(app_home() / "execution" / "execution.sqlite3")
_TIMEOUT_MARKERS = _requests._TIMEOUT_MARKERS


def _exception_http_status(exc: BaseException) -> Optional[int]:
    """Extract an HTTP status from an exception chain without text guessing (R07)."""
    return _requests._exception_http_status(exc)


def _is_ambiguous_error(exc: BaseException) -> bool:
    """True when a submit outcome cannot be proven either way (R07)."""
    return _requests._is_ambiguous_error(
        exc, _exception_http_status=_exception_http_status, _TIMEOUT_MARKERS=_TIMEOUT_MARKERS
    )


def _definitive_rejection(exc: BaseException) -> bool:
    """N11: True only for a provable broker rejection after a POST was sent."""
    return _requests._definitive_rejection(exc, _exception_http_status=_exception_http_status)


StaleRecoveryPositionError = _protection.StaleRecoveryPositionError


def _new_maintenance_collector() -> dict[str, Any]:
    """N15: mutation collector for one round-maintenance pass.

    Counts every POST/DELETE attempt exactly once at the broker boundary;
    adopting an existing broker order is never a mutation.
    """
    return _exits._new_maintenance_collector(
    )


def _maintenance_summary(
    collector: dict[str, Any], *, paused: bool, error: str = "",
) -> dict[str, Any]:
    return _exits._maintenance_summary(
        collector=collector,
        paused=paused,
        error=error,
    )


def resolve_execution_db_path(explicit: Optional[str | Path] = None) -> str:
    """Single execution-DB path resolver (F13): service and reports share it.

    Precedence: 1) explicit caller/runtime path, 2) the current
    ``TRADINGBUFFETT_EXECUTION_DB`` environment value read at call time
    (never frozen at import), 3) the literal default. No default DB is
    created or opened here — callers decide when to open the store.
    """
    if explicit:
        return str(validate_app_path(explicit, field="execution_db"))
    env_value = str(get_env("EXECUTION_DB", "")).strip()
    if env_value:
        return str(validate_app_path(env_value, field="execution_db"))
    return str(validate_app_path(app_home() / "execution" / "execution.sqlite3", field="execution_db"))


def _default_db_path() -> str:
    return resolve_execution_db_path()


RequestBuildError = _requests.RequestBuildError


def _submit_authority_error(
    *, risk_reducing: bool, can_submit: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """Return the one final-boundary reason that forbids a broker POST."""
    return _dispatch._submit_authority_error(
        risk_reducing=risk_reducing, can_submit=can_submit
    )


def _build_protective_request(
    symbol: str,
    side: str,
    qty: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    client_order_id: str,
):
    """Build a broker-side bracket/OTO market order. Equities only, GTC."""
    return _requests._build_protective_request(
        symbol, side, qty, stop_loss_price, take_profit_price, client_order_id,
        RequestBuildError=RequestBuildError,
    )


def _build_market_request(symbol: str, side: str, notional, quantity, client_order_id: str):
    return _requests._build_market_request(symbol, side, notional, quantity, client_order_id)


def _get_execution_config() -> dict:
    return _dispatch._get_execution_config(
    )


def _resolve_qty(symbol: str, amount: float) -> Optional[int]:
    """Return integer share qty from the latest quote, or None."""
    return _requests._resolve_qty(symbol, amount)


def validate_trade_intent(trade_intent: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return (validated_dict, error). Invalid => (None, reason), zero broker calls."""
    return _order_planning.validate_trade_intent(trade_intent)


def _planned_order_specs(intent: dict[str, Any], dollar_amount: Optional[float]):
    """Map TradeIntent.planned_actions to logical order specs."""
    return _order_planning._planned_order_specs(intent, dollar_amount)


def _resolve_protective_prices(
    intent_dict: dict[str, Any], signal: str, is_crypto: bool, warnings: list
) -> Optional[dict[str, float]]:
    """Decide which protective price levels can be submitted to the broker."""
    return _order_planning._resolve_protective_prices(
        intent_dict, signal, is_crypto, warnings
    )


def _position_unchanged(after_qty: float, before_qty: float) -> bool:
    """Same side and same absolute quantity (NEW-R1)."""
    return _order_planning._position_unchanged(after_qty, before_qty)


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
    return _recovery._evaluate_opening_caps(
        symbol=symbol,
        specs=specs,
        snapshot=snapshot,
        quote=quote,
        intent_dict=intent_dict,
        quote_factory=quote_factory,
        BrokerAuthorityError=BrokerAuthorityError,
        _get_execution_config=_get_execution_config,
        broker_status_to_local=broker_status_to_local,
        validate_quote=validate_quote,
    )


_DeadlineGapOutcome = _exits._DeadlineGapOutcome


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
        """Delegate to protection; preserve the original method seam."""
        return _protection._verify_owned_close_protections(
            self,
            broker=broker,
            snapshot=snapshot,
            symbol=symbol,
            BrokerAuthorityError=BrokerAuthorityError,
            broker_status_to_local=broker_status_to_local,
        )

    def _abandon_prepared_rows(self, prepared: dict[str, Any]) -> None:
        """Delegate to exits; preserve the original method seam."""
        return _exits._abandon_prepared_rows(
            self,
            prepared=prepared,
        )

    @staticmethod
    def _broker_order_live(snapshot: BrokerSnapshot, broker_order_id: str) -> bool:
        """R03: is this broker order still live according to fresh facts?"""
        return _protection._broker_order_live(
            snapshot=snapshot,
            broker_order_id=broker_order_id,
            broker_status_to_local=broker_status_to_local,
        )

    def _cancel_protection_with_race_check(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        order: Any,
        *,
        account_id: str,
    ) -> tuple[BrokerSnapshot, int]:
        """Delegate to protection; preserve the original method seam."""
        return _protection._cancel_protection_with_race_check(
            self,
            broker=broker,
            snapshot=snapshot,
            order=order,
            account_id=account_id,
            capture_broker_snapshot=capture_broker_snapshot,
        )

    def _cancel_owned_close_protections(self, broker, snapshot, symbol):
        """Delegate to protection; preserve the original method seam."""
        return _protection._cancel_owned_close_protections(
            self,
            broker=broker,
            snapshot=snapshot,
            symbol=symbol,
            BrokerAuthorityError=BrokerAuthorityError,
            broker_status_to_local=broker_status_to_local,
        )

    # -- protection-gap invariant (F04) ------------------------------------

    def _program_owned_live_reducing_qty(
        self, snapshot: BrokerSnapshot, symbol: str, reducing_side: str
    ) -> float:
        """Delegate to protection; preserve the original method seam."""
        return _protection._program_owned_live_reducing_qty(
            self,
            snapshot=snapshot,
            symbol=symbol,
            reducing_side=reducing_side,
            broker_status_to_local=broker_status_to_local,
        )

    def _evaluate_protection_gap(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        symbol: str,
        *,
        canceled_protections: int,
    ) -> Optional[dict[str, Any]]:
        """Delegate to protection; preserve the original method seam."""
        return _protection._evaluate_protection_gap(
            self,
            broker=broker,
            snapshot=snapshot,
            symbol=symbol,
            canceled_protections=canceled_protections,
        )

    def _recover_persisted_protection_gaps(
        self, snapshot: BrokerSnapshot, result: Any, prior_reasons: list[str],
    ) -> Any:
        """Delegate to protection; preserve the original method seam."""
        return _protection._recover_persisted_protection_gaps(
            self,
            snapshot=snapshot,
            result=result,
            prior_reasons=prior_reasons,
            ReconciliationResult=ReconciliationResult,
        )

    # -- general protection coverage invariant (R09) ------------------------

    def _protection_coverage_gaps(self, snapshot: BrokerSnapshot) -> list[str]:
        """Delegate to protection; preserve the original method seam."""
        return _protection._protection_coverage_gaps(
            self,
            snapshot=snapshot,
            json=json,
        )

    def _apply_protection_coverage(
        self, snapshot: BrokerSnapshot, result: Any,
    ) -> Any:
        """Delegate to protection; preserve the original method seam."""
        return _protection._apply_protection_coverage(
            self,
            snapshot=snapshot,
            result=result,
            ReconciliationResult=ReconciliationResult,
        )


    def enforce_exit_deadlines(
        self,
        can_submit: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        """Exit due, fill-proven positions on scheduled checks under the account lock.

        Offline time, market closure and ambiguous broker state can delay exits.
        Any ownership/quantity conflict pauses instead of closing an unrelated lot.
        """
        from .lifecycle import due_positions, next_deadline_decision_id
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
                snapshot, reconciliation = self._recover_locked(
                    broker,
                    snapshot,
                    can_submit=can_submit,
                )
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
                    decision_id = next_deadline_decision_id(
                        self._store, due["decision_id"], symbol,
                        lambda client_id: self._lookup_for_recovery(broker, client_id),
                    )
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
                    if conflicting:
                        market_closed = self._market_clock_closed(broker)
                        if market_closed is not None:
                            raise BrokerAuthorityError(
                                f"Deadline protection cancellation blocked: {market_closed}"
                            )
                    # F04 sequencing: durably commit the deadline close BEFORE
                    # canceling any protection, so a crash after cancellation
                    # leaves a recorded intent to complete the exit.
                    prepared_outbox = self._prepare_liquidation_outbox(
                        symbol,
                        decision_id=decision_id,
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
                    try:
                        for order in conflicting:
                            snapshot, calls = self._cancel_protection_with_race_check(
                                broker, snapshot, order,
                                account_id=identity.account_id,
                            )
                            canceled_here += calls
                    except Exception:
                        cancellation_calls += canceled_here
                        if canceled_here:
                            gap = self._evaluate_protection_gap(
                                broker, snapshot, symbol,
                                canceled_protections=canceled_here,
                            )
                            if gap is not None:
                                raise _DeadlineGapOutcome(gap)
                        raise
                    cancellation_calls += canceled_here
                    if canceled_here:
                        market_closed = self._market_clock_closed(broker)
                        if market_closed is not None:
                            gap = self._evaluate_protection_gap(
                                broker, snapshot, symbol,
                                canceled_protections=canceled_here,
                            )
                            if gap is not None:
                                raise _DeadlineGapOutcome(gap)
                            raise BrokerAuthorityError(
                                f"Deadline close blocked after broker DELETE: {market_closed}"
                            )
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
                    result = self._liquidate_core(symbol, decision_id=decision_id, _broker=broker,
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
        replay = self._completed_replay_result(
            intent_dict=intent_dict,
            decision_id=decision_id,
        )
        if replay is not None:
            return replay
        is_crypto = "/" in str(intent_dict["symbol"]).upper()
        if str(intent_dict["action"]).upper() == "SHORT" and (is_crypto or not allow_shorts):
            # Reject before maintenance or cancellation; existing outbox rows
            # may belong to an earlier authorized attempt and must stay intact.
            return {
                "success": False, "fail_closed": True,
                "broker_attempted": False, "broker_calls": 0,
                "trade_intent": intent_dict,
                "error": (
                    "Crypto short exposure is not supported by Alpaca spot trading"
                    if is_crypto else "Short exposure is disabled for this session"
                ),
            }
        deadlines = self.enforce_exit_deadlines(can_submit=can_submit)
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
                snapshot, reconciliation = self._recover_locked(
                    broker,
                    snapshot,
                    can_submit=can_submit,
                )
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
                "broker_attempted": bool(getattr(exc, "broker_calls", 0)),
                "broker_calls": getattr(exc, "broker_calls", 0), "error": str(exc),
            }
        except Exception as exc:
            return {
                "success": False, "paused": True, "fail_closed": True,
                "broker_attempted": bool(getattr(exc, "broker_calls", 0)),
                "broker_calls": getattr(exc, "broker_calls", 0),
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
        return _intent_execution._execute_core(
            self,
            trade_intent=trade_intent,
            decision_id=decision_id,
            run_id=run_id,
            dollar_amount=dollar_amount,
            allow_shorts=allow_shorts,
            risk_params=risk_params,
            current_position=current_position,
            can_submit=can_submit,
            _broker=_broker,
            _snapshot=_snapshot,
            _quote=_quote,
            _reversal_close_only=_reversal_close_only,
            _evaluate_opening_caps=_evaluate_opening_caps,
            _planned_order_specs=_planned_order_specs,
            _resolve_protective_prices=_resolve_protective_prices,
            _resolve_qty=_resolve_qty,
            canonical_decision_id=canonical_decision_id,
            client_order_id_for=client_order_id_for,
            json=json,
            validate_trade_intent=validate_trade_intent,
        )

    def _completed_replay_result(
        self,
        *,
        intent_dict: dict[str, Any],
        decision_id: Optional[str],
    ) -> dict[str, Any] | None:
        did = decision_id or canonical_decision_id(intent_dict)
        return _intent_execution._completed_replay_result(
            self,
            decision_id=did,
            intent_dict=intent_dict,
            json_module=json,
        )

    @staticmethod
    def _paused_result(snapshot: BrokerSnapshot, reasons: Any) -> dict[str, Any]:
        return _exits._paused_result(
            snapshot=snapshot,
            reasons=reasons,
        )

    @staticmethod
    def _verified_reducing_exit(
        snapshot: BrokerSnapshot, symbol: str, specs: list[dict[str, Any]]
    ) -> bool:
        return _protection._verified_reducing_exit(
            snapshot=snapshot,
            symbol=symbol,
            specs=specs,
            broker_status_to_local=broker_status_to_local,
        )

    def _lookup_for_recovery(self, broker: Any, client_order_id: str) -> Any:
        return _recovery._lookup_for_recovery(
            self,
            broker=broker,
            client_order_id=client_order_id,
            BrokerAuthorityError=BrokerAuthorityError,
        )

    def _adopt_recovery_order(self, local: dict[str, Any], found: Any) -> None:
        return _recovery._adopt_recovery_order(
            self,
            local=local,
            found=found,
            BrokerAuthorityError=BrokerAuthorityError,
            broker_status_to_local=broker_status_to_local,
        )

    @staticmethod
    def _recovery_crossing_block(
        snapshot: BrokerSnapshot, symbol: str, opening_side: str,
        authorized_close: bool,
    ) -> Optional[str]:
        """Delegate to recovery; preserve the original method seam."""
        return _recovery._recovery_crossing_block(
            snapshot=snapshot,
            symbol=symbol,
            opening_side=opening_side,
            authorized_close=authorized_close,
        )

    def _resubmit_recovered(
        self, broker: Any, local: dict[str, Any], snapshot: BrokerSnapshot,
        can_submit: Optional[Callable[[], bool]] = None,
        maintenance: Optional[dict[str, Any]] = None,
    ) -> None:
        return _recovery._resubmit_recovered(
            self,
            broker=broker,
            local=local,
            snapshot=snapshot,
            can_submit=can_submit,
            maintenance=maintenance,
            BrokerAuthorityError=BrokerAuthorityError,
            StaleRecoveryPositionError=StaleRecoveryPositionError,
            _build_market_request=_build_market_request,
            _build_protective_request=_build_protective_request,
            _definitive_rejection=_definitive_rejection,
            _evaluate_opening_caps=_evaluate_opening_caps,
            _get_execution_config=_get_execution_config,
            _planned_order_specs=_planned_order_specs,
            _resolve_protective_prices=_resolve_protective_prices,
            _submit_authority_error=_submit_authority_error,
            client_order_id_for=client_order_id_for,
            json=json,
            validate_quote=validate_quote,
        )

    def _reconcile_snapshot(self, broker: Any, snapshot: BrokerSnapshot):
        """Delegate to recovery; preserve the original method seam."""
        return _recovery._reconcile_snapshot(
            self,
            broker=broker,
            snapshot=snapshot,
            BrokerAuthorityError=BrokerAuthorityError,
            Reconciler=Reconciler,
            json=json,
        )

    def _recover_locked(
        self,
        broker: Any,
        snapshot: BrokerSnapshot,
        can_submit: Optional[Callable[[], bool]] = None,
        maintenance: Optional[dict[str, Any]] = None,
    ) -> tuple[BrokerSnapshot, Any]:
        """Delegate to recovery; preserve the original method seam."""
        return _recovery._recover_locked(
            self,
            broker=broker,
            snapshot=snapshot,
            can_submit=can_submit,
            maintenance=maintenance,
            BrokerAuthorityError=BrokerAuthorityError,
            capture_broker_snapshot=capture_broker_snapshot,
            re=re,
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
        """Delegate to dispatch; preserve the original method seam."""
        return _dispatch._market_clock_closed(
            self,
            broker=broker,
        )

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
        """Delegate to dispatch; preserve the original method seam."""
        return _dispatch._validate_opening_dispatch(
            self,
            intent_dict=intent_dict,
            symbol=symbol,
            spec=spec,
            quote=quote,
            snapshot=snapshot,
            broker=broker,
            BrokerAuthorityError=BrokerAuthorityError,
            SNAPSHOT_TTL_SECONDS=SNAPSHOT_TTL_SECONDS,
            os=os,
            validate_freshness=validate_freshness,
            validate_quote=validate_quote,
        )

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
        return _dispatch._submit_one(
            self,
            order_row=order_row,
            spec=spec,
            symbol=symbol,
            intent_dict=intent_dict,
            broker=broker,
            _snapshot=_snapshot,
            _quote=_quote,
            can_submit=can_submit,
            RequestBuildError=RequestBuildError,
            _build_market_request=_build_market_request,
            _build_protective_request=_build_protective_request,
            _definitive_rejection=_definitive_rejection,
            _submit_authority_error=_submit_authority_error,
            broker_status_to_local=broker_status_to_local,
        )

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
                "broker_attempted": bool(getattr(exc, "broker_calls", 0)),
                "broker_calls": getattr(exc, "broker_calls", 0),
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
        """Delegate to exits; preserve the original method seam."""
        return _exits._prepare_liquidation_outbox(
            self,
            symbol=symbol,
            decision_id=decision_id,
            run_id=run_id,
            quantity=quantity,
            side=side,
            client_order_id_for=client_order_id_for,
            json=json,
            utc_now=utc_now,
        )

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
        """Delegate to exits; preserve the original method seam."""
        return _exits._liquidate_core(
            self,
            symbol=symbol,
            decision_id=decision_id,
            run_id=run_id,
            _broker=_broker,
            _quantity=_quantity,
            _side=_side,
            _outbox=_outbox,
            _build_market_request=_build_market_request,
            _definitive_rejection=_definitive_rejection,
            _submit_authority_error=_submit_authority_error,
            broker_status_to_local=broker_status_to_local,
        )

    # -- bounded UNKNOWN lookup/adopt ---------------------------------------

    def lookup_unknown(self, client_order_id: str) -> dict[str, Any]:
        """Query the broker by client_order_id and adopt the observed state.

        No second POST happens here. Returns adopted status or NOT_FOUND.
        Uses startup recovery's bounded lookup and verified-account lock.
        Re-read the ledger under the lock before deciding whether to adopt.
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
            identity = capture_broker_snapshot(broker)
            with AccountExecutionLock(self.db_path, identity.account_id):
                existing = self._store.get_order_by_client(client_order_id)
                if existing is None:
                    return {"found": False, "error": "unknown client_order_id"}
                if (existing.get("status") or "").upper() != "UNKNOWN":
                    return {"found": True, "adopted": False, "order": existing}
                snapshot = capture_broker_snapshot(
                    broker, expected_account_id=identity.account_id
                )
                try:
                    self._store.ensure_account_binding(snapshot.account_id)
                except Exception as exc:
                    raise BrokerAuthorityError(
                        f"execution DB account binding check failed: {exc}"
                    ) from exc
                broker_order = self._lookup_for_recovery(broker, client_order_id)
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
        except (BrokerAuthorityError, AccountLockBusy, OSError) as exc:
            return {
                "found": False, "uncertain": True, "paused": True,
                "busy": isinstance(exc, AccountLockBusy),
                "order": existing, "error": str(exc),
            }


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
