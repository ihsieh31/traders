"""Phase A durable execution and broker-authority boundary (paper-only).

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
    resolve_execution_db_path,
)
from tradingagents.execution.authority import (
    AccountExecutionLock,
    AccountLockBusy,
    BrokerAuthorityError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
    Reconciler,
    ReconciliationResult,
    capture_broker_snapshot,
    capture_quote,
    get_with_retry,
    validate_freshness,
    validate_quote,
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
    "AccountExecutionLock",
    "AccountLockBusy",
    "BrokerAuthorityError",
    "BrokerFill",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerQuote",
    "BrokerSnapshot",
    "Reconciler",
    "ReconciliationResult",
    "capture_broker_snapshot",
    "capture_quote",
    "get_with_retry",
    "validate_freshness",
    "validate_quote",
]
