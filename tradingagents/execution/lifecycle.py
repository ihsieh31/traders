"""Reconstruct remaining lots only from durable fills, never from LLM prose."""
from __future__ import annotations
from collections import defaultdict
import json
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
        while book and qty * book[0]["qty"] < 0:
            consume = min(abs(qty), abs(book[0]["qty"]))
            direction = 1 if qty > 0 else -1
            qty -= direction * consume
            book[0]["qty"] += direction * consume
            if abs(book[0]["qty"]) < 1e-8:
                book.pop(0)
        if abs(qty) > 1e-8:
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
