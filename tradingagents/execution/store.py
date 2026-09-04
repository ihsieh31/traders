"""SQLite durable Phase A ledger. Stdlib sqlite3 only, schema version 2.

Three tables only; execution_intents PENDING row + orders PENDING row is the
durable outbox. No fourth generic outbox table (three tables already give
atomic commit-before-submit).

Ponytail ceiling note: the account CLEAN/PAUSED audit record reuses the
execution_intents table, keeping the original three-table design. Do not add
ORM/migrations/queues without evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 2

ORDER_STATUSES = frozenset(
    {
        "PENDING",
        "SUBMITTING",
        "ACCEPTED",
        "PARTIAL",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    }
)

TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})

_ALLOWED_ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"SUBMITTING", "CANCELED"}),
    "SUBMITTING": frozenset(
        {"ACCEPTED", "PARTIAL", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "UNKNOWN"}
    ),
    "ACCEPTED": frozenset({"PARTIAL", "FILLED", "CANCELED", "REJECTED", "EXPIRED"}),
    "PARTIAL": frozenset({"PARTIAL", "FILLED", "CANCELED", "REJECTED", "EXPIRED"}),
    "UNKNOWN": frozenset(
        {"SUBMITTING", "ACCEPTED", "PARTIAL", "FILLED", "CANCELED", "REJECTED", "EXPIRED"}
    ),
    # Terminal states: no outgoing edges (same-state treated as idempotent no-op).
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_order_transition(frm: str, to: str) -> bool:
    """Central state-machine validator. Same-state is an idempotent no-op."""
    frm_u = (frm or "").upper()
    to_u = (to or "").upper()
    if frm_u not in ORDER_STATUSES or to_u not in ORDER_STATUSES:
        return False
    if frm_u == to_u:
        return True
    if frm_u in TERMINAL_STATUSES:
        return False
    return to_u in _ALLOWED_ORDER_TRANSITIONS.get(frm_u, frozenset())


def _canonical_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("/", "").replace("-", "").replace(" ", "")


def canonical_decision_id(trade_intent: dict[str, Any]) -> str:
    """Stable decision_id from canonical TradeIntent fields only.

    No timestamps/randomness: same analysis result -> same ID across
    retries/restarts. Excludes generated_at/rationale/confidence text.
    """
    planned = []
    for a in trade_intent.get("planned_actions") or []:
        if isinstance(a, dict):
            planned.append(
                {
                    "action": a.get("action"),
                    "order_type": a.get("order_type"),
                    "side": a.get("side"),
                    "sizing_basis": a.get("sizing_basis"),
                }
            )
    order_intent = trade_intent.get("order_intent") or {}
    canonical = {
        "symbol": (trade_intent.get("symbol") or "").upper(),
        "action": trade_intent.get("action"),
        "trading_mode": (trade_intent.get("trading_mode") or "").lower(),
        "current_position": trade_intent.get("current_position"),
        "target_position": trade_intent.get("target_position"),
        "position_transition": trade_intent.get("position_transition"),
        "trade_date": trade_intent.get("trade_date"),
        "order_intent": {
            "order_type": order_intent.get("order_type"),
            "side": order_intent.get("side"),
            "sizing_basis": order_intent.get("sizing_basis"),
        },
        "planned_actions": planned,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    return f"dec-{digest}"


def intent_id_for_decision(decision_id: str) -> str:
    digest = hashlib.sha256(f"intent|{decision_id}".encode("utf-8")).hexdigest()[:24]
    return f"intent-{digest}"


def client_order_id_for(
    decision_id: str,
    symbol: str,
    side: str,
    role: str = "open",
    seq: int = 0,
) -> str:
    """Deterministic Alpaca client_order_id (<=48 chars, [a-z0-9-]).

    Only canonical stable fields enter the hash: no time/randomness, so
    retries/restarts reuse the same ID. role/seq separates close-then-open
    legs and protective children so they never collide.
    """
    base = "|".join(
        [
            decision_id,
            _canonical_symbol(symbol),
            (side or "").lower(),
            (role or "open").lower(),
            str(int(seq)),
        ]
    )
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]
    return f"ta-{digest}"


def order_id_for_client(client_order_id: str) -> str:
    digest = hashlib.sha256(f"order|{client_order_id}".encode("utf-8")).hexdigest()[:24]
    return f"ord-{digest}"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS execution_intents (
  intent_id TEXT PRIMARY KEY,
  decision_id TEXT UNIQUE NOT NULL,
  run_id TEXT,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  target_position TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  intent_id TEXT NOT NULL REFERENCES execution_intents(intent_id),
  client_order_id TEXT UNIQUE NOT NULL,
  broker_order_id TEXT UNIQUE,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity REAL,
  notional REAL,
  status TEXT NOT NULL,
  filled_qty REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
  execution_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  qty REAL NOT NULL,
  price REAL NOT NULL,
  filled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent_id);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
"""


class ExecutionStore:
    """Single SQLite store for intents/orders/fills."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        parent = str(Path(self.db_path).parent)
        if parent and parent != ".":
            Path(parent).mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA_SQL.strip().split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(s)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) < SCHEMA_VERSION:
                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # -- durable outbox boundary -----------------------------------------

    def create_outbox(
        self,
        *,
        decision_id: str,
        run_id: Optional[str],
        symbol: str,
        action: str,
        target_position: str,
        payload_json: str,
        orders: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """Atomically insert intent + PENDING orders. Returns (intent, orders, created).

        created=True when this call inserted the intent; False when the
        decision_id already existed (idempotent replay returns existing rows
        without inserting duplicates). Unique constraints are the last line
        of defense, not application-level ifs.
        """
        intent_id = intent_id_for_decision(decision_id)
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO execution_intents
                (intent_id, decision_id, run_id, symbol, action, target_position,
                 payload_json, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    intent_id,
                    decision_id,
                    run_id,
                    symbol,
                    action,
                    target_position,
                    payload_json,
                    now,
                    now,
                ),
            )
            intent_row = conn.execute(
                "SELECT * FROM execution_intents WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            # If a concurrent insert won with a different intent_id mapping,
            # adopt the stored intent_id (first writer wins).
            stored_intent_id = intent_row["intent_id"]
            created = intent_row["decision_id"] == decision_id and intent_row["created_at"] == now

            stored_orders: list[dict[str, Any]] = []
            for spec in orders:
                client_oid = spec["client_order_id"]
                order_id = order_id_for_client(client_oid)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO orders
                    (order_id, intent_id, client_order_id, symbol, side,
                     quantity, notional, status, filled_qty, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
                    """,
                    (
                        order_id,
                        stored_intent_id,
                        client_oid,
                        spec.get("symbol", symbol),
                        spec.get("side", ""),
                        spec.get("quantity"),
                        spec.get("notional"),
                        now,
                        now,
                    ),
                )
                orow = conn.execute(
                    "SELECT * FROM orders WHERE client_order_id = ?",
                    (client_oid,),
                ).fetchone()
                stored_orders.append(dict(orow))
            conn.execute("COMMIT")
            return dict(intent_row), stored_orders, created
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # -- reads ------------------------------------------------------------

    def get_intent_by_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM execution_intents WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_order_by_client(self, client_order_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_orders_for_intent(self, intent_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE intent_id = ? ORDER BY rowid",
                (intent_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_pending_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'PENDING' ORDER BY created_at LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_all_orders(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY rowid")]
        finally:
            conn.close()

    def list_recoverable_orders(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status IN ('PENDING','SUBMITTING','UNKNOWN','PARTIAL') "
                "ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_intent_for_order(self, order_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT i.* FROM execution_intents i JOIN orders o ON o.intent_id=i.intent_id "
                "WHERE o.order_id=?",
                (order_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -- state machine ----------------------------------------------------

    def transition_order(
        self,
        order_id: str,
        to_status: str,
        *,
        filled_qty: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Validate + persist a single order transition.

        Returns (applied, row). Illegal transitions return (False, current
        row) and preserve the original state.
        """
        to_u = (to_status or "").upper()
        if to_u not in ORDER_STATUSES:
            raise ValueError(f"unknown order status: {to_status!r}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(f"order not found: {order_id}")
            current = dict(row)
            frm = (current["status"] or "").upper()
            if not is_valid_order_transition(frm, to_u):
                conn.execute("ROLLBACK")
                return False, current
            now = utcnow_iso()
            new_filled = current["filled_qty"] if current["filled_qty"] is not None else 0.0
            if filled_qty is not None:
                new_filled = float(filled_qty)
            updates: list[str] = ["status = ?", "updated_at = ?", "filled_qty = ?"]
            params: list[Any] = [to_u, now, new_filled]
            if broker_order_id is not None:
                updates.append("broker_order_id = ?")
                params.append(broker_order_id)
            params.append(order_id)
            try:
                conn.execute(
                    f"UPDATE orders SET {', '.join(updates)} WHERE order_id = ?",
                    tuple(params),
                )
            except sqlite3.IntegrityError:
                # e.g. duplicate broker_order_id adopt race: keep original.
                conn.execute("ROLLBACK")
                return False, current
            conn.execute("COMMIT")
            updated = self.get_order(order_id)
            assert updated is not None
            return True, updated
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def sync_order_from_broker(
        self,
        order_id: str,
        status: str,
        *,
        broker_order_id: str,
        filled_qty: float,
    ) -> dict[str, Any]:
        """Authoritatively project broker facts without weakening normal transitions."""
        status_u = status.upper()
        if status_u not in ORDER_STATUSES:
            raise ValueError(f"unknown broker order status: {status!r}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"order not found: {order_id}")
            existing = dict(row)
            if existing.get("broker_order_id") not in (None, broker_order_id):
                raise ValueError("broker order identity conflict")
            # Broker authority may resolve a process that died while PENDING;
            # terminal local rows never regress to non-terminal state.
            current = str(existing["status"]).upper()
            target = current if current in TERMINAL_STATUSES and status_u not in TERMINAL_STATUSES else status_u
            conn.execute(
                "UPDATE orders SET status=?, broker_order_id=?, filled_qty=?, updated_at=? WHERE order_id=?",
                (target, broker_order_id, float(filled_qty), utcnow_iso(), order_id),
            )
            conn.execute("COMMIT")
            updated = self.get_order(order_id)
            assert updated is not None
            return updated
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def adopt_broker_order(
        self, client_order_id: str, broker_order_id: str, to_status: str = "ACCEPTED"
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        """Adopt a broker-observed order for an existing logical order.

        Never creates a second logical order: lookup by client_order_id,
        attach broker_order_id, move to the broker-observed state.
        """
        existing = self.get_order_by_client(client_order_id)
        if existing is None:
            return False, None
        if existing.get("broker_order_id") and existing["broker_order_id"] != broker_order_id:
            # Conflicting broker mapping: refuse to overwrite; keep original.
            return False, existing
        ok, row = self.transition_order(
            existing["order_id"], to_status, broker_order_id=broker_order_id
        )
        if not ok and existing.get("broker_order_id") == broker_order_id:
            # Already adopted (idempotent replay): report success without move.
            return True, self.get_order(existing["order_id"])
        return ok, row

    def update_intent_state(self, intent_id: str, to_state: str) -> bool:
        to_u = (to_state or "").upper()
        if to_u not in ORDER_STATUSES and to_u not in ("IN_PROGRESS", "COMPLETED", "FAILED"):
            raise ValueError(f"unknown intent state: {to_state!r}")
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT state FROM execution_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if cur is None:
                return False
            frm = (cur["state"] or "").upper()
            # Intent-level guard: terminal states never go back to nonterminal.
            if frm in TERMINAL_STATUSES and to_u not in TERMINAL_STATUSES and frm != to_u:
                return False
            conn.execute(
                "UPDATE execution_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
                (to_u, utcnow_iso(), intent_id),
            )
            return True
        finally:
            conn.close()

    # -- fills ------------------------------------------------------------

    def record_fill(
        self,
        *,
        execution_id: str,
        order_id: str,
        qty: float,
        price: float,
        filled_at: Optional[str] = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Persist one broker fill. Duplicate execution_id never double-counts.

        Returns (is_new, order_row). On duplicate, filled_qty is untouched.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            orow = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if orow is None:
                conn.execute("ROLLBACK")
                raise KeyError(f"order not found: {order_id}")
            cur = conn.execute(
                "SELECT execution_id FROM fills WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if cur is not None:
                conn.execute("ROLLBACK")
                existing = self.get_order(order_id)
                assert existing is not None
                return False, existing
            conn.execute(
                "INSERT INTO fills (execution_id, order_id, qty, price, filled_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    execution_id,
                    order_id,
                    float(qty),
                    float(price),
                    filled_at or utcnow_iso(),
                ),
            )
            total = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS total FROM fills WHERE order_id = ?",
                (order_id,),
            ).fetchone()["total"]
            current_status = (orow["status"] or "").upper()
            # Advance lifecycle on fills without auto-creating補单 (no auto補单).
            if current_status in ("ACCEPTED", "PARTIAL", "SUBMITTING", "UNKNOWN", "PENDING"):
                next_status = "PARTIAL"
                # Caller decides FILLED via explicit transition once broker
                # reports completion; but a fill that completes the order may
                # be marked FILLED by the fill-sync path below.
                conn.execute(
                    "UPDATE orders SET filled_qty = ?, status = ?, updated_at = ?"
                    " WHERE order_id = ?",
                    (float(total), next_status, utcnow_iso(), order_id),
                )
            else:
                conn.execute(
                    "UPDATE orders SET filled_qty = ?, updated_at = ? WHERE order_id = ?",
                    (float(total), utcnow_iso(), order_id),
                )
            conn.execute("COMMIT")
            updated = self.get_order(order_id)
            assert updated is not None
            return True, updated
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_fills_since(self, timestamp: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT f.*, o.client_order_id FROM fills f JOIN orders o ON o.order_id=f.order_id "
                "WHERE f.filled_at > ? ORDER BY f.filled_at, f.execution_id",
                (timestamp,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_account_state(self, account_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM execution_intents WHERE decision_id=?",
                (f"account-state-{hashlib.sha256(account_id.encode()).hexdigest()[:24]}",),
            ).fetchone()
            if row is None:
                return None
            raw = dict(row)
            payload = json.loads(raw["payload_json"])
            return {
                "account_id": account_id,
                "state": raw["state"],
                "reasons_json": json.dumps(payload["reasons"]),
                "snapshot_version": payload["snapshot_version"],
                "baseline_positions_json": json.dumps(payload["baseline_positions"]),
                "baseline_at": payload["baseline_at"],
                "updated_at": raw["updated_at"],
            }
        finally:
            conn.close()

    def save_account_state(
        self,
        *,
        account_id: str,
        state: str,
        reasons: tuple[str, ...],
        snapshot_version: str,
        baseline_positions: dict[str, float],
    ) -> None:
        now = utcnow_iso()
        decision_id = f"account-state-{hashlib.sha256(account_id.encode()).hexdigest()[:24]}"
        intent_id = intent_id_for_decision(decision_id)
        existing = self.get_account_state(account_id)
        baseline_at = existing["baseline_at"] if existing else now
        baseline = (
            json.loads(existing["baseline_positions_json"])
            if existing else baseline_positions
        )
        payload = json.dumps(
            {
                "account_id": account_id,
                "reasons": list(reasons),
                "snapshot_version": snapshot_version,
                "baseline_positions": baseline,
                "baseline_at": baseline_at,
            },
            sort_keys=True,
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO execution_intents
                (intent_id,decision_id,run_id,symbol,action,target_position,payload_json,state,created_at,updated_at)
                VALUES (?,?,NULL,'__ACCOUNT__','RECONCILE','AUTHORITY',?,?,?,?)
                ON CONFLICT(decision_id) DO UPDATE SET
                  state=excluded.state,
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at
                """,
                (
                    intent_id,
                    decision_id,
                    payload,
                    state,
                    now,
                    now,
                ),
            )
        finally:
            conn.close()
