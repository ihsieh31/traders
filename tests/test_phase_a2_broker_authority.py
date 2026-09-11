"""Phase A.2 deterministic broker authority and recovery evidence (no network)."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.safety import SafetyVerdict

from tradingagents.execution import (
    AccountExecutionLock,
    AccountLockBusy,
    BrokerAuthorityError,
    BrokerQuote,
    ExecutionService,
    ExecutionStore,
    Reconciler,
    capture_broker_snapshot,
    get_with_retry,
    validate_freshness,
)


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


def _now():
    return datetime.now(timezone.utc)


def _quote(symbol="AAPL", *, at=None):
    return BrokerQuote(symbol.replace("/", ""), 100.0, 100.2, at or _now())


def _intent(action="BUY", current="NEUTRAL"):
    from tradingagents.agents.schemas import (
        ExecutableAction,
        RiskDecision,
        build_trade_intent_from_risk_decision,
    )

    return build_trade_intent_from_risk_decision(
        symbol="AAPL",
        trading_mode="investment",
        current_position=current,
        allow_shorts=False,
        trade_date="2026-09-03",
        decision=RiskDecision(
            action=ExecutableAction(action),
            confidence="medium",
            risk_rationale="A2 test",
            required_controls="strict",
            entry_policy=_ready_entry_policy(), stop_loss_price=95.0,
        ),
    ).model_dump(mode="json")


class FakeBroker:
    def __init__(self, *, account_id="paper-1", positions=None):
        self.account = SimpleNamespace(
            id=account_id, equity="100000", last_equity="100000", cash="80000", buying_power="160000"
        )
        self.positions = list(positions or [])
        self.orders = []
        self.submit_calls = 0
        self.close_calls = 0
        self.submit_error = None
        self.get_account_calls = 0

    def get_clock(self):
        # R13: the opening gate proves the regular session from the broker's
        # own clock before any exposure-adding POST; the fixture keeps it open.
        return SimpleNamespace(is_open=True)

    def get_account(self):
        self.get_account_calls += 1
        return self.account

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, request=None):
        return list(self.orders)

    def get_order_by_client_order_id(self, client_order_id):
        return next((o for o in self.orders if o.client_order_id == client_order_id), None)

    def submit_order(self, request):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        get = request.get if isinstance(request, dict) else lambda name: getattr(request, name, None)
        side = get("side")
        side = getattr(side, "value", side)
        order = SimpleNamespace(
            id=f"broker-{self.submit_calls}",
            client_order_id=get("client_order_id"),
            symbol=str(get("symbol")),
            side=str(side),
            status="accepted",
            qty=str(get("qty") or 0),
            notional=get("notional"),
            filled_qty="0",
            filled_avg_price=None,
            updated_at=_now(),
        )
        self.orders.append(order)
        return order

    def close_position(self, symbol):
        self.close_calls += 1
        return SimpleNamespace(id=f"close-{self.close_calls}", status="accepted")


def _position(qty=5, market_value=500):
    return SimpleNamespace(symbol="AAPL", qty=str(qty), market_value=str(market_value))


def _service(tmp, broker):
    return ExecutionService(
        db_path=str(Path(tmp) / "execution.db"),
        broker_factory=lambda: broker,
        quote_factory=lambda symbol: _quote(symbol),
    )


def _hold_lock(db_path, ready, release):
    with AccountExecutionLock(db_path, "paper-1"):
        ready.set()
        release.wait(10)


def _try_lock(db_path, result):
    try:
        with AccountExecutionLock(db_path, "paper-1"):
            result.put("acquired")
    except AccountLockBusy:
        result.put("busy")


class SnapshotAndRetryTests(unittest.TestCase):
    def test_snapshot_is_typed_utc_account_bound_and_complete(self):
        broker = FakeBroker(positions=[_position()])
        snapshot = capture_broker_snapshot(broker)
        self.assertEqual(snapshot.account_id, "paper-1")
        self.assertIs(snapshot.observed_at.tzinfo, timezone.utc)
        self.assertEqual(snapshot.gross_exposure, 500.0)
        self.assertEqual(snapshot.positions[0].qty, 5.0)
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker, expected_account_id="wrong")

    def test_get_retry_is_bounded_and_non_mutating(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("outage")
            return "ok"

        self.assertEqual(get_with_retry(flaky, sleep=lambda _: None), "ok")
        self.assertEqual(len(calls), 3)
        with self.assertRaises(BrokerAuthorityError):
            get_with_retry(lambda: (_ for _ in ()).throw(ConnectionError("down")), sleep=lambda _: None)

    def test_orders_request_covers_all_status_never_open_only(self):
        from tradingagents.execution.authority import _recent_all_orders_request

        request = _recent_all_orders_request()
        status = getattr(request, "status", None)
        self.assertEqual(getattr(status, "value", status), "all")

    def test_open_request_covers_open_status(self):
        from tradingagents.execution.authority import _open_orders_request

        request = _open_orders_request()
        status = getattr(request, "status", None)
        self.assertEqual(getattr(status, "value", status), "open")

    def test_snapshot_passes_all_request_to_broker(self):
        seen: dict = {}

        class StrictBroker(FakeBroker):
            def get_orders(self, request=None):
                seen["request"] = request
                status = getattr(request, "status", None)
                # The snapshot proves live orders with both the recent ALL
                # listing and the authoritative OPEN listing (F09); neither
                # may be dropped.
                if getattr(status, "value", status) not in ("all", "open"):
                    raise AssertionError(
                        f"snapshot must request ALL/OPEN orders, got {request!r}"
                    )
                return list(self.orders)

        snapshot = capture_broker_snapshot(StrictBroker())
        self.assertIsNotNone(seen.get("request"))
        self.assertEqual(snapshot.orders, ())

    def test_malformed_required_numeric_and_time_fail_closed(self):
        broker = FakeBroker()
        broker.account.cash = "<html>bad gateway</html>"
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker, sleep=lambda _: None)

        broker = FakeBroker()
        broker.orders = [SimpleNamespace(
            id="bad-time", client_order_id="ta-bad-time", symbol="AAPL",
            side="buy", status="accepted", qty="1", filled_qty="0",
            filled_avg_price=None, updated_at="not-a-time",
        )]
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker, sleep=lambda _: None)


class RecoveryAndReconciliationTests(unittest.TestCase):
    def test_account_status_uses_ledger_with_protective_parent_links(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            result = svc.startup_recover()
            self.assertTrue(result["success"])
            state = svc.store.get_account_state("paper-1")
            self.assertEqual(state["state"], "CLEAN")
            conn = sqlite3.connect(str(Path(tmp) / "execution.db"))
            try:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'"
                    )
                }
            finally:
                conn.close()
            self.assertEqual(tables, {"execution_intents", "orders", "fills", "protective_children"})

    def test_post_timeout_is_unknown_without_retry_then_lookup_adopts(self):
        broker = FakeBroker()
        broker.submit_error = TimeoutError("timed out")
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            with patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(enabled=False)):
                result = svc.execute(trade_intent=_intent(), dollar_amount=1000)
            self.assertEqual(broker.submit_calls, 1)
            self.assertEqual(result["orders"][0]["status"], "UNKNOWN")
            client_id = result["orders"][0]["client_order_id"]
            broker.submit_error = None
            broker.orders.append(SimpleNamespace(
                id="adopt-1", client_order_id=client_id, symbol="AAPL", side="buy",
                status="accepted", qty="0", filled_qty="0", filled_avg_price=None,
                updated_at=_now(),
            ))
            recovered = svc.startup_recover()
            self.assertTrue(recovered["success"])
            self.assertEqual(svc.store.get_order_by_client(client_id)["broker_order_id"], "adopt-1")
            self.assertEqual(broker.submit_calls, 1)

    def test_startup_recovers_pending_with_same_client_id_once(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            svc.store.ensure_account_binding("paper-1")
            _, orders, _ = svc.store.create_outbox(
                decision_id="dec-restart", run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG", payload_json=json.dumps(_intent()),
                orders=[{"client_order_id": "ta-restart", "symbol": "AAPL", "side": "buy", "quantity": None, "notional": 1000}],
            )
            result = svc.startup_recover()
            self.assertTrue(result["success"])
            self.assertEqual(broker.submit_calls, 1)
            self.assertEqual(broker.orders[0].client_order_id, orders[0]["client_order_id"])

    def test_unknown_resubmits_once_only_after_three_explicit_not_found_lookups(self):
        broker = FakeBroker()
        broker.submit_error = TimeoutError("connection lost")
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            with patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(enabled=False)):
                timed_out = svc.execute(trade_intent=_intent(), dollar_amount=1000)
            client_id = timed_out["orders"][0]["client_order_id"]
            broker.submit_error = None
            recovered = svc.startup_recover()
            self.assertTrue(recovered["success"])
            self.assertEqual(broker.submit_calls, 2)
            self.assertEqual(broker.orders[0].client_order_id, client_id)

    def test_unresolved_submitting_startup_stays_paused_without_post(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            _, orders, _ = svc.store.create_outbox(
                decision_id="dec-submitting", run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-submitting", "symbol": "AAPL", "side": "buy", "quantity": 1, "notional": None}],
            )
            svc.store.transition_order(orders[0]["order_id"], "SUBMITTING")
            result = svc.startup_recover()
            self.assertFalse(result["success"])
            self.assertEqual(result["account_execution_state"], "PAUSED")
            self.assertEqual(broker.submit_calls, 0)

    def test_duplicate_unknown_partial_and_position_mismatch_pause(self):
        broker = FakeBroker(positions=[_position()])
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(str(Path(tmp) / "execution.db"))
            clean = Reconciler(store).reconcile(capture_broker_snapshot(broker))
            self.assertTrue(clean.clean)
            broker.positions = [_position(qty=7, market_value=700)]
            mismatch = Reconciler(store).reconcile(capture_broker_snapshot(broker))
            self.assertIn("broker/local position mismatch", mismatch.reasons)

            duplicate = SimpleNamespace(
                id="o1", client_order_id="ta-dupe", symbol="AAPL", side="buy",
                status="accepted", qty="1", filled_qty="0", filled_avg_price=None,
                updated_at=_now(),
            )
            broker.orders = [duplicate, SimpleNamespace(**{**duplicate.__dict__, "id": "o2"})]
            paused = Reconciler(store).reconcile(capture_broker_snapshot(broker))
            self.assertEqual(paused.state, "PAUSED")
            self.assertTrue(any("duplicate" in reason for reason in paused.reasons))

    def test_live_unknown_order_pauses_but_terminal_unknown_is_history(self):
        live = SimpleNamespace(
            id="live-1", client_order_id="ta-live-unknown", symbol="AAPL", side="buy",
            status="accepted", qty="1", filled_qty="0", filled_avg_price=None,
            updated_at=_now(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(str(Path(tmp) / "execution.db"))
            broker = FakeBroker()
            broker.orders = [live]
            paused = Reconciler(store).reconcile(capture_broker_snapshot(broker))
            self.assertEqual(paused.state, "PAUSED")
            self.assertTrue(any("unknown broker order" in r for r in paused.reasons))
        for terminal in ("canceled", "filled", "rejected", "expired"):
            with tempfile.TemporaryDirectory() as tmp:
                store = ExecutionStore(str(Path(tmp) / "execution.db"))
                broker = FakeBroker()
                broker.orders = [SimpleNamespace(
                    id=f"term-{terminal}", client_order_id=f"ta-term-{terminal}",
                    symbol="AAPL", side="buy", status=terminal, qty="1",
                    filled_qty="1" if terminal == "filled" else "0",
                    filled_avg_price="100" if terminal == "filled" else None,
                    updated_at=_now(),
                )]
                result = Reconciler(store).reconcile(capture_broker_snapshot(broker))
                self.assertTrue(result.clean, f"{terminal} history must not pause")

    def test_fill_replay_is_idempotent(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            _, orders, _ = svc.store.create_outbox(
                decision_id="dec-fill", run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-fill", "symbol": "AAPL", "side": "buy", "quantity": 2, "notional": None}],
            )
            svc.store.transition_order(orders[0]["order_id"], "SUBMITTING")
            broker.orders = [SimpleNamespace(
                id="fill-order", client_order_id="ta-fill", symbol="AAPL", side="buy",
                status="partially_filled", qty="2", filled_qty="1", filled_avg_price="100",
                updated_at=_now(),
            )]
            snapshot = capture_broker_snapshot(broker)
            Reconciler(svc.store).reconcile(snapshot)
            paused = Reconciler(svc.store).reconcile(snapshot)
            self.assertEqual(svc.store.get_order(orders[0]["order_id"])["filled_qty"], 1.0)
            self.assertEqual(len(svc.store.list_fills_since("1970-01-01T00:00:00+00:00")), 1)
            self.assertEqual(paused.state, "PAUSED")
            self.assertTrue(any("partial" in reason for reason in paused.reasons))


class FreshnessAndLockTests(unittest.TestCase):
    def test_missing_stale_and_future_timestamps_fail(self):
        now = _now()
        for value in (None, now - timedelta(minutes=2), now + timedelta(minutes=2)):
            with self.assertRaises(BrokerAuthorityError):
                validate_freshness(value, ttl_seconds=30, now=now, label="fact")

    def test_wrong_symbol_quote_blocks_before_post(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"), broker_factory=lambda: broker,
                quote_factory=lambda symbol: _quote("MSFT"),
            )
            result = svc.execute(trade_intent=_intent(), dollar_amount=1000)
            self.assertTrue(result["paused"])
            self.assertEqual(broker.submit_calls, 0)

    def test_stale_and_future_quotes_block_before_post(self):
        for stamp in (_now() - timedelta(minutes=2), _now() + timedelta(minutes=2)):
            broker = FakeBroker()
            with tempfile.TemporaryDirectory() as tmp:
                svc = ExecutionService(
                    db_path=str(Path(tmp) / "execution.db"), broker_factory=lambda: broker,
                    quote_factory=lambda symbol, stamp=stamp: _quote(symbol, at=stamp),
                )
                result = svc.execute(trade_intent=_intent(), dollar_amount=1000)
                self.assertTrue(result["paused"])
                self.assertEqual(broker.submit_calls, 0)

    def test_same_account_lock_has_one_owner_and_recovers_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            with AccountExecutionLock(db, "paper-1"):
                with self.assertRaises(AccountLockBusy):
                    with AccountExecutionLock(db, "paper-1"):
                        pass
            with AccountExecutionLock(db, "paper-1"):
                pass

    def test_two_processes_compete_and_crashed_owner_releases_lock(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            ready = ctx.Event()
            release = ctx.Event()
            owner = ctx.Process(target=_hold_lock, args=(db, ready, release))
            owner.start()
            self.assertTrue(ready.wait(5))
            result = ctx.Queue()
            contender = ctx.Process(target=_try_lock, args=(db, result))
            contender.start()
            contender.join(5)
            self.assertEqual(result.get(timeout=2), "busy")
            owner.terminate()  # OS closes the fd on process exit; no stale lease.
            owner.join(5)
            recovered = ctx.Process(target=_try_lock, args=(db, result))
            recovered.start()
            recovered.join(5)
            self.assertEqual(result.get(timeout=2), "acquired")


class CallerAndExitPolicyTests(unittest.TestCase):
    def test_execute_uses_authority_snapshot_and_post_reconcile(self):
        broker = FakeBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp, broker)
            with patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(enabled=False)):
                result = svc.execute(trade_intent=_intent(), dollar_amount=1000)
            self.assertTrue(result["success"])
            self.assertIn("preflight_snapshot_version", result)
            self.assertEqual(result["account_execution_state"], "CLEAN")
            self.assertGreaterEqual(broker.get_account_calls, 3)

    def test_liquidation_requires_verified_position_and_no_conflicting_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            no_position = FakeBroker()
            blocked = _service(tmp, no_position).liquidate("AAPL")
            self.assertFalse(blocked["success"])
            self.assertEqual(no_position.submit_calls, 0)

        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(positions=[_position()])
            allowed = _service(tmp, broker).liquidate("AAPL", decision_id="liq-1")
            self.assertTrue(allowed["success"])
            self.assertEqual(broker.submit_calls, 1)

        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(positions=[_position()])
            broker.orders = [SimpleNamespace(
                id="close-open", client_order_id="manual-close", symbol="AAPL", side="sell",
                status="accepted", qty="5", filled_qty="0", filled_avg_price=None,
                updated_at=_now(),
            )]
            blocked = _service(tmp, broker).liquidate("AAPL", decision_id="liq-2")
            self.assertFalse(blocked["success"])
            self.assertEqual(broker.submit_calls, 0)

    def test_liquidation_still_obeys_kill_switch(self):
        broker = FakeBroker(positions=[_position()])
        guard = SimpleNamespace(
            enabled=True,
            check_order=lambda *args, **kwargs: SafetyVerdict(
                allowed=False, reasons=["operator kill switch"]
            ),
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        ):
            blocked = _service(tmp, broker).liquidate("AAPL", decision_id="liq-kill")
        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["safety_blocked"])
        self.assertEqual(broker.submit_calls, 0)

    def test_scheduler_has_startup_gate_and_execution_has_periodic_gate(self):
        repo = Path(__file__).resolve().parent.parent
        control = (repo / "webui/callbacks/control_callbacks.py").read_text()
        service = (repo / "tradingagents/execution/service.py").read_text()
        self.assertIn("ExecutionService().startup_recover()", control)
        self.assertIn("self._recover_locked(broker, snapshot)", service)
        self.assertIn("post-order reconciliation failed", service)


class RemediationRegressionTests(unittest.TestCase):
    def test_repeat_liquidation_without_decision_id_is_not_silently_deduped(self):
        # A terminal (rejected) first liquidation must not make a later
        # legitimate exit of the same symbol a no-op "success".
        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(positions=[_position()])
            broker.submit_error = Exception("order rejected: market closed")
            svc = _service(tmp, broker)
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=SimpleNamespace(enabled=False),
            ):
                first = svc.liquidate("AAPL")
                self.assertEqual(broker.submit_calls, 1)
                broker.submit_error = None
                second = svc.liquidate("AAPL")
            self.assertFalse(first["success"])
            self.assertTrue(second["success"])
            self.assertNotIn("deduped", second)
            self.assertEqual(broker.submit_calls, 2)
            # Each call is its own decision: distinct durable client_order_ids.
            persisted = {o["client_order_id"] for o in svc.store.list_all_orders()}
            self.assertEqual(len(persisted), 2)

    def test_second_liquidation_with_conflicting_live_close_still_blocked(self):
        # The per-call decision_id must not bypass the verified-exit gate:
        # an un-resolved prior close order on the same side still blocks.
        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(positions=[_position()])
            broker.submit_error = TimeoutError("timed out")
            svc = _service(tmp, broker)
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=SimpleNamespace(enabled=False),
            ):
                first = svc.liquidate("AAPL")
                self.assertEqual(first.get("status"), "UNKNOWN")
                broker.submit_error = None
                second = svc.liquidate("AAPL")
            self.assertFalse(second["success"])
            self.assertTrue(second.get("paused"))
            self.assertEqual(broker.submit_calls, 1)

    def test_unknown_broker_status_stays_live_not_terminal(self):
        from tradingagents.execution.authority import broker_status_to_local

        for raw in ("pending_new", "held", "done_for_day", "pending_cancel", "mystery-status"):
            self.assertEqual(broker_status_to_local(raw), "ACCEPTED")

    def test_pre_submit_ambiguous_failure_marks_unknown_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker()
            svc = _service(tmp, broker)
            _, rows, _ = svc.store.create_outbox(
                decision_id="d-presub", run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-presub", "symbol": "AAPL", "side": "buy",
                         "quantity": 1, "notional": None}],
            )
            svc.store.transition_order(rows[0]["order_id"], "SUBMITTING")
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=SimpleNamespace(enabled=False),
            ), patch(
                "tradingagents.execution.service._build_market_request",
                side_effect=TimeoutError("timed out"),
            ):
                outcome = svc._submit_one(
                    order_row=rows[0],
                    spec={"role": "open", "side": "buy", "order_type": "market"},
                    symbol="AAPL",
                    intent_dict={},
                    broker=broker,
                )
            self.assertEqual(outcome["status"], "UNKNOWN")
            self.assertEqual(outcome["broker_calls"], 0)
            self.assertEqual(broker.submit_calls, 0)
            self.assertEqual(
                svc.store.get_order(rows[0]["order_id"])["status"], "UNKNOWN"
            )


if __name__ == "__main__":
    unittest.main()
