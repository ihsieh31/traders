"""Reconstruct remaining lots only from durable fills, never from LLM prose."""
from __future__ import annotations
from collections import defaultdict
import json
import math
from .policy import utc_timestamp


def remaining_lots(store):
    lots = defaultdict(list)
    for fill in store.list_fills_since(""):
        order = store.get_order(fill["order_id"])
        if not order:
            raise ValueError("Fill without an order")
        qty = float(fill["qty"]) * (1 if order["side"] == "buy" else -1)
        symbol = order["symbol"]
        book = lots[symbol]
        parent_id = store.protective_parent(order["order_id"])
        if parent_id and not any(lot["order"]["order_id"] == parent_id for lot in book):
            raise ValueError("Protective fill has no remaining parent lot")
        while abs(qty) > 1e-8:
            if parent_id:
                index = next((i for i, lot in enumerate(book)
                              if qty * lot["qty"] < 0
                              and lot["order"]["order_id"] == parent_id), None)
            else:
                index = 0 if book and qty * book[0]["qty"] < 0 else None
            if index is None:
                break
            consume = min(abs(qty), abs(book[index]["qty"]))
            direction = 1 if qty > 0 else -1
            qty -= direction * consume
            book[index]["qty"] += direction * consume
            if abs(book[index]["qty"]) < 1e-8:
                book.pop(index)
        if abs(qty) > 1e-8:
            if parent_id:
                raise ValueError("Protective fill exceeds its parent lot")
            intent = store.get_intent_for_order(order["order_id"]) or {}
            payload = json.loads(intent.get("payload_json") or "{}")
            book.append({"qty": qty, "exit_by": (payload.get("entry_policy") or {}).get("exit_by"),
                         "order": order, "intent": intent})
    return lots


def due_positions(store, now):
    result = {}
    for symbol, lots in remaining_lots(store).items():
        due = [lot for lot in lots if lot["exit_by"] and utc_timestamp(lot["exit_by"]) <= now]
        if due:
            result[symbol] = {"qty": sum(lot["qty"] for lot in lots), "lots": lots,
                              "decision_id": "deadline-" + due[0]["order"]["order_id"]}
    return result


MAX_DEADLINE_EXIT_ATTEMPTS = 3


def next_deadline_decision_id(store, base_id, symbol, lookup):
    """Select one durable attempt under the account lock, never replay an
    ambiguous close with a new identity. Terminal facts must be re-proven
    by bounded broker lookup; each deadline gets at most three attempts.
    """
    from .authority import BrokerAuthorityError, broker_status_to_local

    def field(order, name):
        return order.get(name) if isinstance(order, dict) else getattr(order, name, None)

    for attempt in range(MAX_DEADLINE_EXIT_ATTEMPTS):
        decision_id = base_id if attempt == 0 else f"{base_id}-attempt-{attempt}"
        intent = store.get_intent_by_decision(decision_id)
        if intent is None:
            return decision_id
        rows = store.list_orders_for_intent(intent["intent_id"])
        if len(rows) != 1:
            raise BrokerAuthorityError("Deadline close attempt has an invalid durable order set")
        row = rows[0]
        status = str(row.get("status") or "").upper()
        if status not in {"REJECTED", "CANCELED", "EXPIRED", "FILLED"}:
            return decision_id  # original PENDING/live/UNKNOWN ID remains its owner
        found = lookup(row["client_order_id"])
        if found is None:
            if status != "REJECTED" or row.get("broker_order_id"):
                raise BrokerAuthorityError("Previous deadline close cannot be proven terminal")
            continue  # definitive rejection plus authoritative absence, no broker order
        found_status = broker_status_to_local(field(found, "status"))
        if (field(found, "client_order_id") != row["client_order_id"]
                or str(field(found, "symbol") or "").upper().replace("/", "")
                != symbol.upper().replace("/", "")
                or not field(found, "id")
                or (row.get("broker_order_id") and str(field(found, "id")) != row["broker_order_id"])
                or found_status not in {"REJECTED", "CANCELED", "EXPIRED", "FILLED"}):
            raise BrokerAuthorityError("Previous deadline close is live or its terminal identity is uncertain")
        broker_filled = float(field(found, "filled_qty") or 0)
        local_filled = float(row.get("filled_qty") or 0)
        if (not math.isfinite(broker_filled) or broker_filled < 0
                or not math.isfinite(local_filled)
                or abs(broker_filled - local_filled) > 1e-8):
            raise BrokerAuthorityError("Previous deadline close fills require reconciliation before retry")
        store.sync_order_from_broker(row["order_id"], found_status,
                                    broker_order_id=str(field(found, "id")),
                                    filled_qty=float(row.get("filled_qty") or 0))
    raise BrokerAuthorityError("Deadline exit retry budget exhausted; operator review required")
