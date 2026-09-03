"""Phase A.1 durable execution foundation (paper-only).

Single execution trust boundary + SQLite ledger. Stdlib only.
Ponytail note (ceiling): no ORM/migration/queue; SQLite + sqlite3 is
sufficient for single-process single-account Paper execution. Revisit
storage only with measured multi-process/multi-account evidence.
"""

from tradingagents.execution.store import (
    ExecutionStore,
    canonical_decision_id,
    client_order_id_for,
    intent_id_for_decision,
    is_valid_order_transition,
    order_id_for_client,
)

from tradingagents.execution.service import (
    ExecutionService,
    execute_trade_intent,
    liquidate_position,
)

__all__ = [
    "ExecutionStore",
    "ExecutionService",
    "canonical_decision_id",
    "client_order_id_for",
    "intent_id_for_decision",
    "is_valid_order_transition",
    "order_id_for_client",
    "execute_trade_intent",
    "liquidate_position",
]
