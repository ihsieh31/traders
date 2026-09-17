"""Durable close preparation, close submission and maintenance evidence.

The coordinator supplies named call-time collaborators. Cross-responsibility
calls use the original service instance; this module owns no service or lock.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from pathlib import Path
from tradingagents.execution.authority import BrokerSnapshot


class _DeadlineGapOutcome(Exception):
    """Carries a protection-gap result out of the deadline loop (F01)."""

    def __init__(self, gap: dict[str, Any]):
        super().__init__(str(gap.get("error") or "protection gap"))
        self.gap = gap


def _abandon_prepared_rows(self, prepared: dict[str, Any]) -> None:
    """Mark a durably-committed close intent CANCELED without a broker call.

        Used when the position proved closed/changed during the protection
        cancellation race, so the committed rows cannot be replayed later as
        a new submit against facts that no longer hold.
        """
    order_rows = prepared.get("order_rows") or []
    intent_row = prepared.get("intent_row") or {}
    for row in order_rows:
        status = str((row.get("status") or "")).upper()
        if status == "PENDING":
            try:
                self._store.transition_order(row["order_id"], "CANCELED")
            except Exception:
                pass
    try:
        self._store.update_intent_state(intent_row["intent_id"], "CANCELED")
    except Exception:
        pass


def _prepare_liquidation_outbox(
    self,
    symbol: str,
    *,
    decision_id: Optional[str] = None,
    run_id: Optional[str] = None,
    quantity: Optional[float] = None,
    side: str = 'sell',
    client_order_id_for,
    json,
    utc_now,
) -> dict[str, Any]:
    """Durably commit the close intent BEFORE any protection is canceled.

        F04 sequencing: if this durable commit fails, the caller must return
        with the original protections untouched — a close that only exists
        in memory must never be allowed to strip a position's stop first.
        """
    sym = (symbol or "").upper()
    # Each liquidation is its own operator decision: without an explicit
    # decision_id the default is per-call, so a later legitimate exit of
    # the same symbol is never silently deduped against an older one.
    # Repeat/concurrent safety comes from _verified_reducing_exit (fresh
    # broker position + no conflicting live close order), not from ID reuse.
    did = decision_id or f"liq-{sym}-{utc_now().strftime('%Y%m%dT%H%M%S%f')}"
    client_oid = client_order_id_for(did, sym, side, role="close", seq=0)
    try:
        intent_row, order_rows, created = self._store.create_outbox(
            decision_id=did,
            run_id=run_id,
            symbol=sym,
            action="SELL" if side == "sell" else "BUY",
            target_position="NEUTRAL",
            payload_json=json.dumps(
                {
                    "symbol": sym,
                    "action": "SELL" if side == "sell" else "BUY",
                    "kind": "liquidation",
                },
                sort_keys=True,
            ),
            orders=[
                {
                    "client_order_id": client_oid,
                    "symbol": sym,
                    "side": side,
                    "quantity": quantity,
                    "notional": None,
                }
            ],
        )
    except Exception as exc:
        return {
            "ok": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": f"durable commit failed: {exc}",
        }
    return {
        "ok": True,
        "decision_id": did,
        "client_order_id": client_oid,
        "intent_row": intent_row,
        "order_rows": order_rows,
        "created": created,
    }


def _liquidate_core(
    self,
    symbol: str,
    *,
    decision_id: Optional[str] = None,
    run_id: Optional[str] = None,
    _broker: Any = None,
    _quantity: Optional[float] = None,
    _side: str = 'sell',
    _outbox: Optional[dict[str, Any]] = None,
    _build_market_request,
    _definitive_rejection,
    _submit_authority_error,
    broker_status_to_local,
) -> dict[str, Any]:
    """Risk-reducing exit through the same durable boundary.

        When ``_outbox`` is supplied (the F04 pre-cancel durable commit), the
        already-committed intent is submitted instead of creating another.
        """
    sym = (symbol or "").upper()
    if not sym:
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": "missing symbol for liquidation",
        }
    prepared = _outbox or self._prepare_liquidation_outbox(
        sym, decision_id=decision_id, run_id=run_id,
        quantity=_quantity, side=_side,
    )
    if not prepared.get("ok"):
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": prepared.get("error", "durable commit failed"),
        }
    did = prepared["decision_id"]
    client_oid = prepared["client_order_id"]
    intent_row = prepared["intent_row"]
    order_rows = prepared["order_rows"]
    created = prepared["created"]
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
    authority_error = _submit_authority_error(risk_reducing=True)
    if authority_error:
        self._store.transition_order(orow["order_id"], "CANCELED")
        return {
            "success": False,
            "status": "CANCELED",
            "broker_attempted": False,
            "broker_calls": 0,
            "error": f"{authority_error} (no broker POST was made)",
            "intent_id": intent_row["intent_id"],
            "decision_id": did,
        }
    posted = False
    try:
        request = _build_market_request(
            sym, _side, None, _quantity, client_oid
        )
        if request is None:
            raise ValueError("verified close quantity is unavailable")
        posted = True
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
        if not broker_oid:
            self._store.transition_order(orow["order_id"], "UNKNOWN")
            return {"success": False, "status": "UNKNOWN", "broker_attempted": True,
                    "broker_calls": 1, "error": "Close response has no broker order identity"}
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
        # N11: only a structured HTTP 4xx (never 408) proves the broker
        # read and refused the POST. A local pre-POST failure (request
        # construction) is a terminal REJECTED with zero broker calls;
        # anything after the request left stays UNKNOWN.
        if not posted and not _definitive_rejection(exc):
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
        if posted and not _definitive_rejection(exc):
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
            "broker_attempted": posted,
            "broker_calls": 1 if posted else 0,
            "error": str(exc),
            "intent_id": intent_row["intent_id"],
            "decision_id": did,
        }


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


def _new_maintenance_collector() -> dict[str, Any]:
    """N15: mutation collector for one round-maintenance pass.

    Counts every POST/DELETE attempt exactly once at the broker boundary;
    adopting an existing broker order is never a mutation.
    """
    return {"submit_calls": 0, "cancel_calls": 0, "submitted_symbols": set(),
            "has_unknown": False}


def _maintenance_summary(
    collector: dict[str, Any],
    *,
    paused: bool,
    error: str = '',
) -> dict[str, Any]:
    submits = int(collector.get("submit_calls") or 0)
    cancels = int(collector.get("cancel_calls") or 0)
    return {
        "broker_calls": submits + cancels,
        "submit_calls": submits,
        "cancel_calls": cancels,
        "submitted_symbols": sorted(
            str(s).upper() for s in (collector.get("submitted_symbols") or set())
        ),
        "has_unknown": bool(collector.get("has_unknown")),
        "paused": bool(paused),
        "error": str(error or "")[:300],
    }
