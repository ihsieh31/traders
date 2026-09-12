"""Broker authority, reconciliation, freshness, and single-account locking.

Phase A.2 deliberately keeps these concerns in one stdlib-only module: one
immutable snapshot, one reconciler, and one crash-safe OS file lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


GET_ATTEMPTS = 3
GET_BACKOFF_SECONDS = 0.05
SNAPSHOT_TTL_SECONDS = 30.0
QUOTE_TTL_SECONDS = 15.0
MAX_FUTURE_SKEW_SECONDS = 2.0


class BrokerAuthorityError(RuntimeError):
    """Required broker facts are missing, malformed, stale, or conflicting."""


class AccountLockBusy(BrokerAuthorityError):
    """Another process currently owns this account's execution critical section."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrokerAuthorityError(f"invalid {field}: {value!r}") from exc
    else:
        raise BrokerAuthorityError(f"missing {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BrokerAuthorityError(f"{field} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _number(value: Any, *, field: str, minimum: Optional[float] = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BrokerAuthorityError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise BrokerAuthorityError(f"invalid {field}: {value!r}")
    return result


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return getattr(value, "value", value)
    return default


def _symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "")


def get_with_retry(
    operation: Callable[[], Any],
    *,
    attempts: int = GET_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry an idempotent broker GET at most three times, then fail closed."""
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # broker SDK exception families vary by version
            last = exc
            if attempt + 1 < attempts:
                sleep(GET_BACKOFF_SECONDS * (2**attempt))
    raise BrokerAuthorityError(
        f"broker GET failed after {attempts} attempts: {last}"
    ) from last


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    market_value: float
    # Optional context fields (Phase B). They come from the same broker
    # response in the same capture call; None means the broker did not
    # supply the value, never "zero". Prompt rendering shows such fields as
    # unavailable instead of fabricating numbers.
    avg_entry_price: Optional[float] = None
    unrealized_pl: Optional[float] = None
    current_price: Optional[float] = None


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    qty: float
    filled_qty: float
    filled_avg_price: Optional[float]
    updated_at: datetime
    notional: Optional[float] = None


@dataclass(frozen=True)
class BrokerFill:
    execution_id: str
    broker_order_id: str
    client_order_id: str
    qty: float
    price: float
    filled_at: datetime


@dataclass(frozen=True)
class BrokerQuote:
    symbol: str
    bid_price: Optional[float]
    ask_price: Optional[float]
    observed_at: datetime

    @property
    def price(self) -> float:
        prices = [p for p in (self.bid_price, self.ask_price) if p is not None and p > 0]
        if not prices:
            raise BrokerAuthorityError(f"quote has no positive price for {self.symbol}")
        return sum(prices) / len(prices)


@dataclass(frozen=True)
class BrokerSnapshot:
    observed_at: datetime
    version: str
    account_id: str
    equity: float
    # Prior-session close equity from the broker account object. This is the
    # daily-loss baseline. Broker last_equity can still be moved by deposits
    # and withdrawals; a cash-flow adjusted TWR is NOT implemented this round.
    last_equity: float
    cash: float
    buying_power: float
    positions: tuple[BrokerPosition, ...]
    orders: tuple[BrokerOrder, ...]
    fills: tuple[BrokerFill, ...]
    gross_exposure: float

    def position(self, symbol: str) -> Optional[BrokerPosition]:
        key = _symbol(symbol)
        return next((p for p in self.positions if p.symbol == key), None)


def validate_quote(quote: Any, symbol: str) -> BrokerQuote:
    """Validate injected or production quote facts at the execution boundary."""
    if not isinstance(quote, BrokerQuote):
        raise BrokerAuthorityError("execution quote must be a typed BrokerQuote")
    if quote.symbol != _symbol(symbol):
        raise BrokerAuthorityError("quote symbol does not match execution symbol")
    validate_freshness(
        quote.observed_at,
        ttl_seconds=float(os.getenv("TRADINGAGENTS_QUOTE_TTL_SECONDS", QUOTE_TTL_SECONDS)),
        label="quote",
    )
    quote.price
    return quote


def validate_freshness(
    observed_at: Any,
    *,
    ttl_seconds: float,
    now: Optional[datetime] = None,
    label: str,
) -> datetime:
    stamp = _utc(observed_at, field=f"{label} timestamp")
    current = (now or utc_now()).astimezone(timezone.utc)
    if stamp > current + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise BrokerAuthorityError(f"{label} timestamp is in the future")
    if current - stamp > timedelta(seconds=ttl_seconds):
        raise BrokerAuthorityError(f"{label} is stale")
    return stamp


API_ORDER_LIMIT = 500


def _recent_all_orders_request() -> Any:
    try:
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=API_ORDER_LIMIT, direction=Sort.DESC, nested=False
        )
    except Exception as exc:
        # Never silently degrade to an open-only listing: closed/canceled/
        # filled orders would become invisible and local terminal states
        # could never reconcile. Fail closed instead.
        raise BrokerAuthorityError(
            f"cannot build broker ALL-orders request: {exc}"
        ) from exc


def _open_orders_request() -> Any:
    try:
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=API_ORDER_LIMIT, direction=Sort.DESC, nested=False
        )
    except Exception as exc:
        raise BrokerAuthorityError(
            f"cannot build broker OPEN-orders request: {exc}"
        ) from exc


def _merge_raw_orders(raw_recent: Any, raw_open: Any) -> list[Any]:
    """Merge recent all-status history with authoritative current OPEN orders.

    Deduplicate by broker order ID. Duplicate occurrences must agree on
    client order ID, symbol, side, and status compatibility; conflicting
    identity is an authority failure, never a silent overwrite. Every OPEN
    order survives the merge even when it was absent from the recent ALL
    list (500 newer terminal orders can hide an older live order otherwise).
    """
    STATUS_INCOMPATIBLE = (
        lambda a, b: str(a).lower() != str(b).lower()
        and "partial" not in str(a).lower() + str(b).lower()
    )
    merged: dict[str, Any] = {}

    def _add(raw: Any, *, require_live: bool) -> None:
        broker_id = str(_value(raw, "id", "broker_order_id") or "").strip()
        if not broker_id:
            return
        existing = merged.get(broker_id)
        if existing is None:
            merged[broker_id] = raw
            return
        # Verify the duplicate occurrence agrees on identity facts.
        for field_name in ("client_order_id", "symbol", "side"):
            left = _value(existing, field_name)
            right = _value(raw, field_name)
            if str(left or "").lower() != str(right or "").lower():
                raise BrokerAuthorityError(
                    f"conflicting broker order identity for {broker_id}: "
                    f"{field_name} {left!r} vs {right!r}"
                )
        left_status = _value(existing, "status")
        right_status = _value(raw, "status")
        if left_status is not None and right_status is not None and STATUS_INCOMPATIBLE(
            left_status, right_status
        ):
            # The order moved between the two reads (e.g. filled just after
            # the OPEN listing). Keep the more recent occurrence as-is: the
            # reconciler judges facts, we only refuse silent identity swaps.
            merged[broker_id] = raw
            return
        merged[broker_id] = raw

    for raw in raw_open or []:
        _add(raw, require_live=True)
    for raw in raw_recent or []:
        _add(raw, require_live=False)
    return list(merged.values())


def capture_broker_snapshot(
    broker: Any,
    *,
    expected_account_id: Optional[str] = None,
    now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> BrokerSnapshot:
    """Fetch one complete account/position/order/fill authority snapshot."""
    # F09: stamp the capture START, not the end. Every fact below was read
    # at or after this instant, so the snapshot's age must include the
    # whole capture duration (a slow GET sequence must not masquerade as a
    # fresh snapshot).
    capture_started_at = now().astimezone(timezone.utc)
    account = get_with_retry(broker.get_account, sleep=sleep)
    account_id = str(_value(account, "id", "account_id") or "").strip()
    if not account_id:
        raise BrokerAuthorityError("broker account ID is unavailable")
    if expected_account_id and account_id != expected_account_id:
        raise BrokerAuthorityError(
            f"broker account mismatch: expected {expected_account_id}, got {account_id}"
        )
    equity = _number(_value(account, "equity"), field="account equity", minimum=0)
    # Daily-loss baseline: never backfilled from equity, cash, buying power
    # or HWM. Missing/non-finite/zero/negative last_equity fails closed.
    # Broker last_equity can still be moved by deposits and withdrawals; a
    # cash-flow adjusted TWR is NOT implemented this round.
    last_equity = _number(
        _value(account, "last_equity"),
        field="account last equity",
        minimum=0,
    )
    if last_equity <= 0:
        raise BrokerAuthorityError("account last equity must be positive")
    cash = _number(_value(account, "cash"), field="account cash")
    buying_power = _number(
        _value(account, "buying_power"), field="account buying power", minimum=0
    )

    raw_positions = get_with_retry(broker.get_all_positions, sleep=sleep)
    if raw_positions is None:
        raise BrokerAuthorityError("broker positions are unavailable")
    positions: list[BrokerPosition] = []
    for raw in raw_positions:
        symbol = _symbol(_value(raw, "symbol"))
        if not symbol:
            raise BrokerAuthorityError("broker position has no symbol")
        qty = _number(_value(raw, "qty"), field=f"{symbol} position qty")
        market_value = _number(
            _value(raw, "market_value"), field=f"{symbol} market value"
        )

        def _optional(name: str) -> Optional[float]:
            value = _value(raw, name)
            if value is None:
                return None
            return _number(value, field=f"{symbol} {name}")

        positions.append(
            BrokerPosition(
                symbol,
                qty,
                market_value,
                avg_entry_price=_optional("avg_entry_price"),
                unrealized_pl=_optional("unrealized_pl"),
                current_price=_optional("current_price"),
            )
        )

    # Two bounded GETs prove current live orders (F09): the recent ALL list
    # alone can hide an older still-live order behind 500 newer terminal
    # rows, so the authoritative OPEN listing is fetched too. A full OPEN
    # page means completeness is unprovable and the snapshot fails closed.
    raw_open = get_with_retry(
        lambda: broker.get_orders(_open_orders_request()), sleep=sleep
    )
    if raw_open is None:
        raise BrokerAuthorityError("broker OPEN orders are unavailable")
    if len(list(raw_open)) >= API_ORDER_LIMIT:
        raise BrokerAuthorityError(
            "open order list reached API limit; completeness cannot be proven"
        )
    request = _recent_all_orders_request()
    raw_orders = get_with_retry(lambda: broker.get_orders(request), sleep=sleep)
    if raw_orders is None:
        raise BrokerAuthorityError("broker orders are unavailable")
    raw_orders = _merge_raw_orders(raw_orders, raw_open)

    orders: list[BrokerOrder] = []
    fills: list[BrokerFill] = []
    for raw in raw_orders:
        broker_id = str(_value(raw, "id", "broker_order_id") or "").strip()
        client_id = str(_value(raw, "client_order_id") or "").strip()
        symbol = _symbol(_value(raw, "symbol"))
        side = str(_value(raw, "side") or "").lower()
        status = str(_value(raw, "status") or "").lower()
        if not all((broker_id, client_id, symbol, side, status)):
            raise BrokerAuthorityError("broker order identity is incomplete")
        qty = _number(_value(raw, "qty", default=0), field=f"{client_id} order qty", minimum=0)
        filled_qty = _number(
            _value(raw, "filled_qty", default=0),
            field=f"{client_id} filled qty",
            minimum=0,
        )
        stamp_value = _value(raw, "updated_at", "filled_at", "submitted_at", "created_at")
        stamp = _utc(stamp_value, field=f"{client_id} order timestamp")
        price_value = _value(raw, "filled_avg_price")
        price = (
            _number(price_value, field=f"{client_id} fill price", minimum=0)
            if price_value is not None
            else None
        )
        order = BrokerOrder(
            broker_id, client_id, symbol, side, status, qty, filled_qty, price, stamp,
            notional=(
                _number(_value(raw, "notional"), field=f"{client_id} notional", minimum=0)
                if _value(raw, "notional") is not None
                else None
            ),
        )
        orders.append(order)
        if filled_qty > 0:
            if price is None or price <= 0:
                raise BrokerAuthorityError(f"{client_id} filled order has no valid price")
            fills.append(
                BrokerFill(
                    execution_id=f"{broker_id}:{filled_qty:.12g}",
                    broker_order_id=broker_id,
                    client_order_id=client_id,
                    qty=filled_qty,
                    price=price,
                    filled_at=stamp,
                )
            )

    observed_at = capture_started_at
    canonical = json.dumps(
        {
            "observed_at": observed_at.isoformat(),
            "account_id": account_id,
            "equity": equity,
            "last_equity": last_equity,
            "cash": cash,
            "buying_power": buying_power,
            "positions": [(p.symbol, p.qty, p.market_value) for p in positions],
            "orders": [
                (o.broker_order_id, o.client_order_id, o.status, o.filled_qty)
                for o in orders
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    version = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    snapshot = BrokerSnapshot(
        observed_at=observed_at,
        version=version,
        account_id=account_id,
        equity=equity,
        last_equity=last_equity,
        cash=cash,
        buying_power=buying_power,
        positions=tuple(positions),
        orders=tuple(orders),
        fills=tuple(fills),
        gross_exposure=sum(abs(p.market_value) for p in positions),
    )
    validate_freshness(
        snapshot.observed_at,
        ttl_seconds=float(os.getenv("TRADINGAGENTS_SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)),
        label="broker snapshot",
    )
    return snapshot


def capture_quote(symbol: str) -> BrokerQuote:
    """Fetch and validate the execution quote through the existing data client."""
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils

    def fetch_and_validate() -> BrokerQuote:
        raw = AlpacaUtils.get_latest_quote(symbol)
        if not isinstance(raw, dict) or _symbol(raw.get("symbol")) != _symbol(symbol):
            raise BrokerAuthorityError(
                "quote symbol is missing or does not match execution symbol"
            )
        stamp = validate_freshness(
            raw.get("timestamp"),
            ttl_seconds=float(
                os.getenv("TRADINGAGENTS_QUOTE_TTL_SECONDS", QUOTE_TTL_SECONDS)
            ),
            label="quote",
        )
        bid = raw.get("bid_price")
        ask = raw.get("ask_price")
        return validate_quote(
            BrokerQuote(
                symbol=_symbol(symbol),
                bid_price=_number(bid, field="quote bid", minimum=0)
                if bid is not None
                else None,
                ask_price=_number(ask, field="quote ask", minimum=0)
                if ask is not None
                else None,
                observed_at=stamp,
            ),
            symbol,
        )

    return get_with_retry(fetch_and_validate)


class AccountExecutionLock:
    """Non-blocking, crash-released process lock keyed by verified account ID.

    F08: the lock lives in ONE fixed same-host location (env
    ``TRADINGAGENTS_EXECUTION_LOCK_DIR`` override, else
    ``~/.tradingagents/execution-locks``), never beside the DB — two
    worktrees pointing at different DB paths but the same broker account
    must contend for the same lock file. Same host/filesystem only; no
    multi-host (distributed) claim is made. An unbuildable lock directory
    fails closed instead of falling back to a per-DB lock.
    """

    def __init__(self, db_path: str, account_id: str):
        digest = hashlib.sha256(account_id.encode()).hexdigest()[:20]
        lock_dir = os.getenv("TRADINGAGENTS_EXECUTION_LOCK_DIR", "").strip()
        if not lock_dir:
            lock_dir = str(Path.home() / ".tradingagents" / "execution-locks")
        lock_path = Path(lock_dir)
        lock_path.mkdir(parents=True, exist_ok=True)
        self.path = lock_path / f"account-{digest}.lock"
        self._file: Any = None

    def __enter__(self) -> "AccountExecutionLock":
        self._file = self.path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise AccountLockBusy("account execution lock is busy; execution paused") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    reasons: tuple[str, ...]
    snapshot_version: str

    @property
    def clean(self) -> bool:
        return self.state == "CLEAN"


class Reconciler:
    """Make local order/fill facts follow the broker and report CLEAN/PAUSED."""

    def __init__(self, store: Any):
        self.store = store

    def reconcile(self, snapshot: BrokerSnapshot) -> ReconciliationResult:
        reasons: list[str] = []
        try:
            validate_freshness(
                snapshot.observed_at,
                ttl_seconds=float(os.getenv("TRADINGAGENTS_SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)),
                label="broker snapshot",
            )
        except BrokerAuthorityError as exc:
            reasons.append(str(exc))

        by_client: dict[str, BrokerOrder] = {}
        broker_ids: set[str] = set()
        duplicate_clients: set[str] = set()
        duplicate_broker_ids: set[str] = set()
        for order in snapshot.orders:
            if order.client_order_id in by_client:
                reasons.append(f"duplicate broker client_order_id: {order.client_order_id}")
                duplicate_clients.add(order.client_order_id)
            if order.broker_order_id in broker_ids:
                reasons.append(f"duplicate broker order identity: {order.broker_order_id}")
                duplicate_broker_ids.add(order.broker_order_id)
            by_client[order.client_order_id] = order
            broker_ids.add(order.broker_order_id)

        local_orders = self.store.list_all_orders()
        local_by_client = {o["client_order_id"]: o for o in local_orders}
        for broker_order in snapshot.orders:
            if (
                broker_order.client_order_id in duplicate_clients
                or broker_order.broker_order_id in duplicate_broker_ids
            ):
                continue
            local = local_by_client.get(broker_order.client_order_id)
            if local is None:
                # Only our deterministic namespace is execution-owned. Manual
                # paper orders remain visible but cannot silently become local.
                # A broker-terminal unknown order (canceled/filled/rejected/
                # expired) is settled history: it cannot create future exposure
                # and must not pause the account forever with no clearance
                # path. Only a live unknown order blocks new risk.
                if broker_order.client_order_id.startswith("ta-") and broker_status_to_local(
                    broker_order.status
                ) not in ("CANCELED", "FILLED", "REJECTED", "EXPIRED"):
                    reasons.append(f"unknown broker order: {broker_order.client_order_id}")
                continue
            existing_broker_id = local.get("broker_order_id")
            if existing_broker_id and existing_broker_id != broker_order.broker_order_id:
                reasons.append(f"broker identity conflict: {broker_order.client_order_id}")
                continue
            local_status = broker_status_to_local(broker_order.status)
            self.store.sync_order_from_broker(
                local["order_id"],
                local_status,
                broker_order_id=broker_order.broker_order_id,
                filled_qty=float(local.get("filled_qty") or 0),
            )

        for fill in snapshot.fills:
            local = local_by_client.get(fill.client_order_id)
            if local is None:
                continue
            current = self.store.get_order(local["order_id"])
            already = float(current.get("filled_qty") or 0) if current else 0.0
            delta = fill.qty - already
            if delta > 1e-9:
                # F10: broker filled_qty/filled_avg_price are CUMULATIVE.
                # The incremental fill price is the cost delta over the qty
                # delta, never the cumulative average itself.
                broker_cumulative_cost = float(fill.qty) * float(fill.price)
                recorded_cost = float(self.store.recorded_fill_cost(local["order_id"]))
                delta_cost = broker_cumulative_cost - recorded_cost
                incremental_price = delta_cost / delta
                if (
                    delta_cost <= 0
                    or not math.isfinite(incremental_price)
                    or incremental_price <= 0
                ):
                    raise BrokerAuthorityError(
                        f"invalid cumulative fill economics for "
                        f"{fill.client_order_id}: cumulative qty {fill.qty:g} "
                        f"at avg {fill.price:g} vs recorded cost "
                        f"{recorded_cost:.8f}; refusing to record a guessed fill"
                    )
                self.store.record_fill(
                    execution_id=fill.execution_id,
                    order_id=local["order_id"],
                    qty=delta,
                    price=incremental_price,
                    filled_at=fill.filled_at.isoformat(),
                )

        # Apply the final cumulative broker quantities after individual fill
        # deltas have been persisted idempotently.
        for broker_order in snapshot.orders:
            if (
                broker_order.client_order_id in duplicate_clients
                or broker_order.broker_order_id in duplicate_broker_ids
            ):
                continue
            local = local_by_client.get(broker_order.client_order_id)
            if local is not None:
                self.store.sync_order_from_broker(
                    local["order_id"],
                    broker_status_to_local(broker_order.status),
                    broker_order_id=broker_order.broker_order_id,
                    filled_qty=broker_order.filled_qty,
                )

        for local in self.store.list_all_orders():
            status = str(local.get("status") or "").upper()
            broker_order = by_client.get(local["client_order_id"])
            if status in {"PENDING", "SUBMITTING", "ACCEPTED", "UNKNOWN", "PARTIAL"} and broker_order is None:
                reasons.append(
                    f"unresolved {status} order: {local['client_order_id']}"
                )
            if status == "PARTIAL":
                reasons.append(f"unresolved partial fill: {local['client_order_id']}")

        state = self.store.get_account_state(snapshot.account_id)
        broker_positions = {p.symbol: p.qty for p in snapshot.positions if abs(p.qty) > 1e-9}
        if state is None:
            baseline = broker_positions
        else:
            baseline = json.loads(state["baseline_positions_json"])
            expected = dict(baseline)
            for fill in self.store.list_fills_since(state["baseline_at"]):
                order = local_by_client.get(fill["client_order_id"])
                if order is None:
                    continue
                sign = -1.0 if str(order["side"]).lower() == "sell" else 1.0
                sym = _symbol(order["symbol"])
                expected[sym] = float(expected.get(sym, 0.0)) + sign * float(fill["qty"])
            keys = set(expected) | set(broker_positions)
            if any(abs(float(expected.get(k, 0)) - float(broker_positions.get(k, 0))) > 1e-8 for k in keys):
                reasons.append("broker/local position mismatch")

        result = ReconciliationResult(
            state="PAUSED" if reasons else "CLEAN",
            reasons=tuple(dict.fromkeys(reasons)),
            snapshot_version=snapshot.version,
        )
        self.store.save_account_state(
            account_id=snapshot.account_id,
            state=result.state,
            reasons=result.reasons,
            snapshot_version=snapshot.version,
            baseline_positions=baseline,
        )
        return result

    def rebase_baseline(
        self, snapshot: BrokerSnapshot, *, reason: str
    ) -> ReconciliationResult:
        """Explicit operator maintenance: accept the broker's current
        positions as the new reconciliation baseline (e.g. a verified stock
        split). This is a manual maintenance action, never recovery: nothing
        in startup_recover(), the observation loop, or quarantine may call
        it — a position mismatch must keep hard-stopping unattended runs.

        Every precondition is mandatory; any failure raises
        BrokerAuthorityError and leaves the stored baseline untouched:
        - non-empty operator reason (acknowledged maintenance action);
        - fresh snapshot under the existing snapshot TTL rules;
        - an existing account state (a rebase never creates a first
          baseline — normal reconciliation does);
        - a normal reconcile() runs first so broker order/fill state is
          settled per the existing rules;
        - the ONLY remaining anomaly is exactly one reason, broker/local
          position mismatch — no other reason is whitelisted away;
        - no local non-terminal order (it could still fill and move the
          position after the rebase);
        - no live broker order of ANY namespace (manual orders included).
        """
        if not str(reason or "").strip():
            raise BrokerAuthorityError(
                "baseline rebase requires a non-empty operator reason"
            )
        validate_freshness(
            snapshot.observed_at,
            ttl_seconds=float(
                os.getenv("TRADINGAGENTS_SNAPSHOT_TTL_SECONDS", SNAPSHOT_TTL_SECONDS)
            ),
            label="broker snapshot",
        )
        if self.store.get_account_state(snapshot.account_id) is None:
            raise BrokerAuthorityError(
                f"no existing account state for {snapshot.account_id!r}; "
                "rebase cannot create the first baseline"
            )
        current = self.reconcile(snapshot)
        if current.state != "PAUSED" or set(current.reasons) != {
            "broker/local position mismatch"
        }:
            raise BrokerAuthorityError(
                "baseline rebase requires the only anomaly to be broker/local "
                f"position mismatch, got state={current.state} "
                f"reasons={list(current.reasons)}"
            )
        terminal_local = {"CANCELED", "FILLED", "REJECTED", "EXPIRED"}
        for local in self.store.list_all_orders():
            if str(local.get("status") or "").upper() not in terminal_local:
                raise BrokerAuthorityError(
                    "baseline rebase refused: local order "
                    f"{local.get('client_order_id')} is still "
                    f"{local.get('status')}"
                )
        for broker_order in snapshot.orders:
            if broker_status_to_local(broker_order.status) not in terminal_local:
                raise BrokerAuthorityError(
                    "baseline rebase refused: broker order "
                    f"{broker_order.client_order_id} is still live "
                    f"({broker_order.status})"
                )
        broker_positions = {
            p.symbol: p.qty for p in snapshot.positions if abs(p.qty) > 1e-9
        }
        self.store.rebase_account_state(
            account_id=snapshot.account_id,
            snapshot_version=snapshot.version,
            baseline_positions=broker_positions,
            baseline_at=snapshot.observed_at.isoformat(),
        )
        verified = self.reconcile(snapshot)
        if not verified.clean:
            raise BrokerAuthorityError(
                f"baseline rebase verification failed: {verified.reasons}"
            )
        return verified


def broker_status_to_local(status: Any) -> str:
    """Map a broker order status to the local state machine.

    Known live broker statuses (new, accepted, pending_new, held,
    done_for_day, pending_cancel/replace, stopped, ...) all land in the
    ACCEPTED fallback below. Unrecognized statuses must stay live too: a
    broker state this build does not know can never look terminal locally,
    so the row remains resolvable and the account pauses instead of
    silently clearing.
    """
    value = str(status or "").lower()
    if "partial" in value:
        return "PARTIAL"
    if value in {"filled", "fill"}:
        return "FILLED"
    if value in {"canceled", "cancelled"}:
        return "CANCELED"
    if value in {"rejected", "reject"}:
        return "REJECTED"
    if value == "expired":
        return "EXPIRED"
    return "ACCEPTED"
