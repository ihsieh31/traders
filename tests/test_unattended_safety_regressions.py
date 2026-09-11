"""Unattended-trading safety regressions (minimal-fix round 2026-09-10).

One focused offline reproduction per finding: F01 deadline SHORT signed
qty, F02 Stop→Start stale generation, F03A/F03B outstanding exposure,
F04 recovery stop-on-anomaly, F05 reversal close-only, F06 DB account
binding, F07 SUBMITTING lookup, F08 same-host account lock, F09 snapshot
freshness, F10 cumulative fill economics. No real Alpaca mutation, no
paid LLM call, no network — every transport is faked in-process.
"""

import json
import math
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tradingagents.agents  # noqa: F401  (production-safe import order)
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
    capture_broker_snapshot,
)
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import ExecutionStore, client_order_id_for
from tradingagents.risk.exposure import evaluate_opening_exposure

PAPER = "paper-1"


def _now():
    return datetime.now(timezone.utc)


def _fresh_quote(symbol, price=100.0):
    return BrokerQuote(symbol.replace("/", ""), price - 0.1, price + 0.1, _now())


def _snapshot(positions=None, orders=None, fills=None, *, equity=100000.0,
              cash=80000.0, account_id=PAPER, observed_at=None):
    positions = tuple(positions or ())
    orders = tuple(orders or ())
    return BrokerSnapshot(
        observed_at=observed_at or _now(),
        version="v-regr",
        account_id=account_id,
        equity=equity,
        last_equity=equity,
        cash=cash,
        buying_power=equity * 2,
        positions=positions,
        orders=orders,
        fills=tuple(fills or ()),
        gross_exposure=sum(abs(p.market_value) for p in positions),
    )


def _pos(symbol, qty, mv):
    return BrokerPosition(symbol, qty, mv)


def _order(symbol, side, *, qty=10, notional=None, status="new",
           client_id="ta-x", filled_qty=0.0, filled_avg_price=None):
    return BrokerOrder(
        broker_order_id=f"b-{client_id}",
        client_order_id=client_id,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        updated_at=_now(),
        notional=notional,
    )


def _caps_config(**overrides):
    config = {"max_symbol_concentration_pct": 20.0, "sector_mapping": {}}
    config.update(overrides)
    return config


class _GuardIsolated(unittest.TestCase):
    """Isolate process-global guard/config state around every test."""

    def setUp(self):
        import tradingagents.dataflows.config as _cfgmod
        import tradingagents.long_run as _lr

        self._cfgmod = _cfgmod
        self._lr = _lr
        self._saved_config = _cfgmod.get_config()
        guard = MagicMock()
        guard.enabled = False
        self._guard_patch = patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        )
        self._guard_patch.start()
        super().setUp()

    def tearDown(self):
        self._guard_patch.stop()
        self._lr._stop_requested = False
        self._cfgmod._config = dict(self._saved_config)
        super().tearDown()


# ---------------------------------------------------------------------------
# F03 — outstanding exposure accounting
# ---------------------------------------------------------------------------


class F03ExposureAccountingTests(unittest.TestCase):
    def test_f3_t1_sector_headroom_counts_pending_orders_of_flat_symbols(self):
        # MSFT is FLAT but already carries a pending BUY of $25,000; the
        # sector cap is 30% of $100,000. AAPL (same sector) may add at most
        # $5,000 — the sector symbol set comes from the sector_mapping, not
        # from currently held symbols.
        snapshot = _snapshot(
            orders=[_order("MSFT", "buy", notional=25000.0, client_id="ta-msft")]
        )
        result = evaluate_opening_exposure(
            symbol="AAPL",
            proposed_notional=25000.0,
            snapshot=snapshot,
            quote_price=100.0,
            symbol_cap_pct=25.0,
            sector_cap_pct=30.0,
            gross_cap_pct=100.0,
            sector_mapping={"MSFT": "TECH", "AAPL": "TECH"},
        )
        self.assertTrue(result.approved, result.reason)
        self.assertLessEqual(result.notional, 5000.0 + 1e-6)

    def test_f3_t2_flat_opening_short_sell_counts_as_increasing(self):
        # A SELL against a flat position opens a short: it is exposure.
        snapshot = _snapshot(
            orders=[_order("XYZ", "sell", qty=200, client_id="ta-xyz")]
        )
        total, estimated = _outstanding(snapshot, prices={"XYZ": 100.0})
        self.assertEqual(total, 20000.0)
        self.assertTrue(estimated)

    def test_f3_t3_long_close_sell_is_not_new_exposure(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", 10, 1000.0)],
            orders=[_order("AAPL", "sell", qty=10, client_id="ta-close")],
        )
        total, estimated = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertEqual(total, 0.0)
        self.assertTrue(estimated)

    def test_f3_t4_short_close_buy_is_not_new_exposure(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", -10, -1000.0)],
            orders=[_order("AAPL", "buy", qty=10, client_id="ta-close")],
        )
        total, estimated = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertEqual(total, 0.0)
        self.assertTrue(estimated)

    def test_f3_t5_overflipping_sell_counts_only_the_excess(self):
        # LONG 10 with a pending SELL of 15: at least 5 shares must count.
        snapshot = _snapshot(
            positions=[_pos("AAPL", 10, 1000.0)],
            orders=[_order("AAPL", "sell", qty=15, client_id="ta-flip")],
        )
        total, estimated = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertEqual(total, 500.0)  # 5 excess shares @ $100
        self.assertTrue(estimated)

    def test_f3_t6_two_reducing_orders_cannot_double_consume_capacity(self):
        # LONG 10 with two SELLs of 10 each: the first is provably reducing,
        # the second must be counted as exposure-increasing.
        snapshot = _snapshot(
            positions=[_pos("AAPL", 10, 1000.0)],
            orders=[
                _order("AAPL", "sell", qty=10, client_id="ta-a"),
                _order("AAPL", "sell", qty=10, client_id="ta-b"),
            ],
        )
        total, estimated = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertEqual(total, 1000.0)  # the second sell: 10 shares @ $100
        self.assertTrue(estimated)

    def test_f3_t7_missing_quote_for_increasing_order_fails_closed(self):
        # Another symbol's quantity-only increasing order without its own
        # validated quote keeps the fail-closed (fully_estimated=False) rule.
        snapshot = _snapshot(
            orders=[_order("TSLA", "buy", qty=10, client_id="ta-tsla")]
        )
        total, estimated = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertFalse(estimated)
        result = evaluate_opening_exposure(
            symbol="AAPL",
            proposed_notional=1000.0,
            snapshot=snapshot,
            quote_price=100.0,
            symbol_cap_pct=25.0,
            sector_cap_pct=None,
            gross_cap_pct=100.0,
        )
        self.assertFalse(result.approved)

    def test_f3_t8_partial_fill_only_counts_remaining_qty(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", -10, -1000.0)],
            orders=[_order("AAPL", "buy", qty=10, filled_qty=4,
                           client_id="ta-close")],
        )
        total, _ = _outstanding(snapshot, prices={"AAPL": 100.0})
        self.assertEqual(total, 0.0)  # remaining 6 still provably reducing


def _outstanding(snapshot, *, prices):
    from tradingagents.risk.exposure import outstanding_increasing_notional

    return outstanding_increasing_notional(snapshot, reference_prices=prices)


# ---------------------------------------------------------------------------
# F09 — snapshot freshness stamps the capture START
# ---------------------------------------------------------------------------


class _SimpleAuthorityBroker:
    """Minimal broker fake whose GETs return instantly."""

    def __init__(self, account_id=PAPER):
        self._account_id = account_id

    def get_account(self):
        return SimpleNamespace(
            id=self._account_id, equity="100000", last_equity="100000",
            cash="80000", buying_power="160000",
        )

    def get_all_positions(self):
        return []

    def get_orders(self, request=None):
        return []


class F09SnapshotFreshnessTests(unittest.TestCase):
    def test_f9_t1_slow_capture_is_stale_even_before_the_first_read_ages(self):
        # The capture "starts" 60s ago (equiv: the GET sequence consumed
        # 60s); TTL is 30s => fail closed, regardless of when capture ended.
        real_now = _now()
        broker = _SimpleAuthorityBroker()
        with self.assertRaises(BrokerAuthorityError) as ctx:
            capture_broker_snapshot(
                broker, now=lambda: real_now - timedelta(seconds=60)
            )
        self.assertIn("stale", str(ctx.exception))

    def test_f9_t2_fast_capture_is_fresh_and_stamps_the_start(self):
        real_now = _now()
        ticks = iter([real_now - timedelta(seconds=2), real_now])

        def fake_now():
            return next(ticks)

        broker = _SimpleAuthorityBroker()
        snapshot = capture_broker_snapshot(broker, now=fake_now)
        self.assertEqual(snapshot.observed_at, real_now - timedelta(seconds=2))
        self.assertLessEqual(
            (_now() - snapshot.observed_at).total_seconds(), 30.0
        )

    def test_f9_t3_quote_freshness_semantics_unchanged(self):
        stale = BrokerQuote(
            "AAPL", 99.0, 101.0, _now() - timedelta(seconds=60)
        )
        with self.assertRaises(BrokerAuthorityError):
            from tradingagents.execution.authority import validate_quote

            validate_quote(stale, "AAPL")


# ---------------------------------------------------------------------------
# F10 — cumulative fill economics
# ---------------------------------------------------------------------------


class F10FillEconomicsTests(unittest.TestCase):
    def _store_with_order(self, tmp):
        store = ExecutionStore(Path(tmp) / "execution.db")
        store.ensure_account_binding(PAPER)
        coid = "ta-f10"
        _, orders, _ = store.create_outbox(
            decision_id="dec-f10", run_id=None, symbol="AAPL", action="BUY",
            target_position="LONG", payload_json="{}",
            orders=[{"client_order_id": coid, "symbol": "AAPL", "side": "buy",
                     "quantity": None, "notional": 1000.0}],
        )
        return store, orders[0]

    def _snapshot_with_fills(self, cum_qty, cum_avg, broker_id="b-f10",
                             coid="ta-f10"):
        order = _order("AAPL", "buy", qty=cum_qty, status="filled",
                       client_id=coid, filled_qty=cum_qty,
                       filled_avg_price=cum_avg)
        fills = ()
        if cum_qty > 0:
            fills = (
                BrokerFill(
                    execution_id=f"{broker_id}:{cum_qty:.12g}",
                    broker_order_id=broker_id,
                    client_order_id=coid,
                    qty=cum_qty,
                    price=cum_avg,
                    filled_at=_now(),
                ),
            )
        return _snapshot(orders=[order], fills=fills)

    def test_f10_t1_two_stage_fill_records_incremental_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row = self._store_with_order(tmp)
            reconciler = Reconciler(store)

            reconciler.reconcile(self._snapshot_with_fills(1, 100.0))
            self.assertEqual(store.recorded_fill_cost(row["order_id"]), 100.0)

            reconciler.reconcile(self._snapshot_with_fills(2, 150.0))
            fills = [f for f in store.list_fills_since("") if f["order_id"] == row["order_id"]]
            self.assertEqual(len(fills), 2)
            self.assertEqual(fills[0]["qty"], 1.0)
            self.assertEqual(fills[0]["price"], 100.0)
            # Second fill: (2*150 - 1*100) / 1 = 200, NOT the cumulative 150.
            self.assertEqual(fills[1]["qty"], 1.0)
            self.assertAlmostEqual(fills[1]["price"], 200.0)
            self.assertAlmostEqual(store.recorded_fill_cost(row["order_id"]), 300.0)

    def test_f10_t2_same_cumulative_snapshot_never_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row = self._store_with_order(tmp)
            reconciler = Reconciler(store)
            reconciler.reconcile(self._snapshot_with_fills(2, 150.0))
            reconciler.reconcile(self._snapshot_with_fills(2, 150.0))
            fills = [f for f in store.list_fills_since("") if f["order_id"] == row["order_id"]]
            self.assertEqual(len(fills), 1)
            self.assertAlmostEqual(store.recorded_fill_cost(row["order_id"]), 300.0)

    def test_f10_t3_third_stage_lands_on_the_broker_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row = self._store_with_order(tmp)
            reconciler = Reconciler(store)
            reconciler.reconcile(self._snapshot_with_fills(1, 100.0))
            reconciler.reconcile(self._snapshot_with_fills(2, 150.0))
            reconciler.reconcile(self._snapshot_with_fills(3, 140.0))
            # Broker cumulative cost 3*140 = 420; third share cost 120.
            self.assertAlmostEqual(store.recorded_fill_cost(row["order_id"]), 420.0)
            fills = [f for f in store.list_fills_since("") if f["order_id"] == row["order_id"]]
            self.assertEqual(len(fills), 3)
            self.assertAlmostEqual(fills[2]["price"], 120.0)

    def test_f10_t4_invalid_cumulative_economics_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row = self._store_with_order(tmp)
            reconciler = Reconciler(store)
            reconciler.reconcile(self._snapshot_with_fills(2, 150.0))
            # Cum qty grows but cumulative cost DROPS: impossible economics.
            with self.assertRaises(BrokerAuthorityError):
                reconciler.reconcile(self._snapshot_with_fills(3, 20.0))
            fills = [f for f in store.list_fills_since("") if f["order_id"] == row["order_id"]]
            self.assertEqual(len(fills), 1)  # no guessed fill was recorded


# ---------------------------------------------------------------------------
# F08 — one same-host lock location per account
# ---------------------------------------------------------------------------


class F08AccountLockTests(unittest.TestCase):
    def test_f8_t1_same_account_different_db_dirs_share_one_lock(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                tempfile.TemporaryDirectory() as locks, \
                patch.dict(os.environ, {"TRADINGAGENTS_EXECUTION_LOCK_DIR": locks}):
            lock_a = AccountExecutionLock(str(Path(a) / "execution.db"), PAPER)
            lock_b = AccountExecutionLock(str(Path(b) / "execution.db"), PAPER)
            self.assertEqual(lock_a.path, lock_b.path)
            with lock_a:
                with self.assertRaises(AccountLockBusy):
                    with lock_b:
                        pass

    def test_f8_t2_different_accounts_use_different_locks(self):
        with tempfile.TemporaryDirectory() as locks, \
                patch.dict(os.environ, {"TRADINGAGENTS_EXECUTION_LOCK_DIR": locks}):
            lock_a = AccountExecutionLock("whatever.db", "paper-A")
            lock_b = AccountExecutionLock("whatever.db", "paper-B")
            self.assertNotEqual(lock_a.path, lock_b.path)
            with lock_a:
                with lock_b:
                    pass

    def test_f8_t3_env_override_controls_the_lock_directory(self):
        with tempfile.TemporaryDirectory() as locks, \
                patch.dict(os.environ, {"TRADINGAGENTS_EXECUTION_LOCK_DIR": locks}):
            lock = AccountExecutionLock("/any/db/path.db", PAPER)
            self.assertEqual(lock.path.parent, Path(locks))
            self.assertTrue(str(lock.path).startswith(str(locks)))

    def test_f8_t4_default_location_is_home_scoped(self):
        env = {k: v for k, v in os.environ.items()
               if k != "TRADINGAGENTS_EXECUTION_LOCK_DIR"}
        with patch.dict(os.environ, env, clear=True):
            lock = AccountExecutionLock("/any/db/path.db", PAPER)
            expected = Path.home() / ".tradingagents" / "execution-locks"
            self.assertEqual(lock.path.parent, expected)


# ---------------------------------------------------------------------------
# F06 — one execution DB belongs to exactly one broker account
# ---------------------------------------------------------------------------


class F06AccountBindingTests(unittest.TestCase):
    def _broker(self, account_id):
        return _SimpleAuthorityBroker(account_id=account_id)

    def test_f6_t1_empty_db_binds_the_first_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.db")
            store.ensure_account_binding(PAPER)
            self.assertEqual(store.account_binding_owner(), PAPER)
            # Idempotent for the same account.
            store.ensure_account_binding(PAPER)

    def test_f6_t2_reopening_the_same_db_for_the_same_account_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            ExecutionService(db_path=db, broker_factory=lambda: self._broker(PAPER))
            service = ExecutionService(
                db_path=db, broker_factory=lambda: self._broker(PAPER)
            )
            result = service.startup_recover()
            self.assertTrue(result["success"], result)

    def test_f6_t3_reusing_the_db_for_another_account_fails_before_any_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.db")
            store.ensure_account_binding(PAPER)
            broker = self._broker("paper-2")
            service = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
            )
            result = service.startup_recover()
            self.assertFalse(result["success"])
            self.assertIn("bound", " ".join(result["reconciliation_reasons"]))
            submit = getattr(broker, "submit_order", None)
            if submit is not None:  # the fake never even offers a POST path
                submit.assert_not_called()

    def test_f6_t4_legacy_db_with_single_provable_account_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.db")
            store.save_account_state(
                account_id=PAPER, state="CLEAN", reasons=(),
                snapshot_version="v", baseline_positions={},
            )
            store.ensure_account_binding(PAPER)
            self.assertEqual(store.account_binding_owner(), PAPER)

    def test_f6_t5_legacy_db_of_another_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.db")
            store.save_account_state(
                account_id="paper-old", state="CLEAN", reasons=(),
                snapshot_version="v", baseline_positions={},
            )
            with self.assertRaises(ValueError) as ctx:
                store.ensure_account_binding(PAPER)
            self.assertIn("paper-old", str(ctx.exception))

    def test_f6_t6_legacy_unbound_trading_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / "execution.db")
            store.create_outbox(
                decision_id="dec-legacy", run_id=None, symbol="AAPL",
                action="BUY", target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-legacy", "symbol": "AAPL",
                         "side": "buy", "quantity": 1, "notional": None}],
            )
            with self.assertRaises(ValueError) as ctx:
                store.ensure_account_binding(PAPER)
            self.assertIn("unbound trading records", str(ctx.exception))
            self.assertIsNone(store.account_binding_owner())


# ---------------------------------------------------------------------------
# F07 — unresolved SUBMITTING enters the read-only recovery lookup
# ---------------------------------------------------------------------------


class _RecoveryBroker:
    """Broker fake for recovery scenarios with call accounting."""

    def __init__(self, *, orders=None, positions=None, submit_status="accepted"):
        self.orders = list(orders or [])
        self.positions = list(positions or [])
        self.submit_calls = []
        self.lookup_calls = []
        self._submit_status = submit_status

    def get_account(self):
        return SimpleNamespace(
            id=PAPER, equity="100000", last_equity="100000",
            cash="80000", buying_power="160000",
        )

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, request=None):
        return list(self.orders)

    def get_order_by_client_order_id(self, cid):
        self.lookup_calls.append(cid)
        return next((o for o in self.orders if o.client_order_id == cid), None)

    def get_order_by_id(self, order_id, filter=None):
        return next((o for o in self.orders if o.id == order_id), None)

    def submit_order(self, request):
        self.submit_calls.append(request)
        order = SimpleNamespace(
            id=f"broker-{len(self.submit_calls)}",
            client_order_id=request.client_order_id,
            symbol=str(getattr(request, "symbol", "AAPL")),
            side=str(getattr(getattr(request, "side", None), "value",
                             getattr(request, "side", "buy"))),
            qty=str(getattr(request, "qty", 0) or 0),
            notional=getattr(request, "notional", None),
            filled_qty="0", filled_avg_price=None,
            status=self._submit_status, updated_at=_now(), legs=[],
        )
        self.orders.append(order)
        return order

    def cancel_order_by_id(self, order_id):
        pass


class F07SubmittingLookupTests(_GuardIsolated):
    def _seed_submitting(self, svc, coid="ta-submitting"):
        svc.store.ensure_account_binding(PAPER)
        _, orders, _ = svc.store.create_outbox(
            decision_id="dec-f7", run_id=None, symbol="AAPL", action="BUY",
            target_position="LONG", payload_json=json.dumps({}),
            orders=[{"client_order_id": coid, "symbol": "AAPL", "side": "buy",
                     "quantity": None, "notional": 1000.0}],
        )
        ok, row = svc.store.transition_order(orders[0]["order_id"], "SUBMITTING")
        assert ok
        return row

    def test_f7_t1_submitting_order_found_on_broker_is_adopted(self):
        broker = _RecoveryBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: _fresh_quote(s),
            )
            row = self._seed_submitting(svc)
            broker.orders.append(SimpleNamespace(
                id="adopt-1", client_order_id=row["client_order_id"],
                symbol="AAPL", side="buy", qty="10", notional=1000.0,
                filled_qty="0", filled_avg_price=None, status="accepted",
                updated_at=_now(), legs=[],
            ))
            result = svc.startup_recover()
            self.assertTrue(result["success"], result)
            adopted = svc.store.get_order(row["order_id"])
            self.assertEqual(adopted["status"], "ACCEPTED")
            self.assertEqual(adopted["broker_order_id"], "adopt-1")
            self.assertEqual(broker.submit_calls, [])

    def test_f7_t2_submitting_not_on_broker_stays_paused_without_resubmit(self):
        broker = _RecoveryBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: _fresh_quote(s),
            )
            row = self._seed_submitting(svc)
            result = svc.startup_recover()
            self.assertFalse(result["success"])
            # The read-only lookup DID run...
            self.assertEqual(broker.lookup_calls, [row["client_order_id"]] * 3)
            # ...but no resubmit was posted and the row stays unresolved.
            self.assertEqual(broker.submit_calls, [])
            self.assertEqual(
                svc.store.get_order(row["order_id"])["status"], "SUBMITTING"
            )

    def test_f7_t3_uncertain_lookup_pauses_with_zero_posts(self):
        broker = _RecoveryBroker()

        def flaky(cid):
            broker.lookup_calls.append(cid)
            raise ConnectionError("connection reset")

        broker.get_order_by_client_order_id = flaky
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=lambda s: _fresh_quote(s),
            )
            row = self._seed_submitting(svc)
            result = svc.startup_recover()
            self.assertFalse(result["success"])
            self.assertEqual(broker.submit_calls, [])
            self.assertEqual(
                svc.store.get_order(row["order_id"])["status"], "SUBMITTING"
            )


# ---------------------------------------------------------------------------
# F04 — recovery stops at the first non-CLEAN fresh reconcile
# ---------------------------------------------------------------------------


def _buy_intent_json(symbol="AAPL", price=100.0):
    """Minimal schema-valid opening intent payload for recovery resubmits."""
    now = datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "action": "BUY",
        "confidence": "medium",
        "trading_mode": "investment",
        "current_position": "NEUTRAL",
        "target_position": "LONG",
        "generated_at": now.isoformat(),
        "trade_date": now.date().isoformat(),
        "entry_policy": {
            "status": "READY", "minimum_price": price - 1,
            "maximum_price": price + 1,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "exit_by": (now + timedelta(days=5)).isoformat(),
            "confirmation": "fixture observed setup",
        },
        "risk_controls": {"stop_loss_price": price - 5},
        "order_intent": {"order_type": "market", "side": "buy",
                         "sizing_basis": "configured_notional"},
    }


class F04RecoveryStopTests(_GuardIsolated):
    def _seed_two_pendings(self, svc):
        rows = []
        for tag in ("a", "b"):
            did = f"dec-f4-{tag}"
            coid = client_order_id_for(did, "AAPL", "buy", role="open", seq=0)
            _, orders, _ = svc.store.create_outbox(
                decision_id=did, run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG",
                payload_json=json.dumps(_buy_intent_json()),
                orders=[{"client_order_id": coid, "symbol": "AAPL",
                         "side": "buy", "quantity": None, "notional": 1000.0}],
            )
            rows.append(orders[0])
        return rows

    def _service(self, tmp, broker):
        svc = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: _fresh_quote(s),
        )
        svc.store.ensure_account_binding(PAPER)
        svc._quarantine_rejection = MagicMock(return_value=None)
        return svc

    def _run_recovery(self, svc):
        with patch("tradingagents.execution.service._get_execution_config",
                   return_value=_caps_config(
                       max_symbol_concentration_pct=25.0)), patch(
            "tradingagents.screening.gate.check_entry_allowed", return_value=None
        ):
            return svc.startup_recover()

    def test_f4_t1_partial_after_first_mutation_stops_before_the_second(self):
        broker = _RecoveryBroker(submit_status="partially_filled")

        def partial_submit(request):
            order = _RecoveryBroker.submit_order(broker, request)
            order.status = "partially_filled"
            order.filled_qty = "3"
            order.filled_avg_price = "100"
            broker.orders[-1] = order
            return order

        broker.submit_order = partial_submit
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            rows = self._seed_two_pendings(svc)
            result = self._run_recovery(svc)
            self.assertFalse(result["success"], result)
            self.assertEqual(len(broker.submit_calls), 1)
            self.assertEqual(
                svc.store.get_order(rows[1]["order_id"])["status"], "PENDING"
            )

    def test_f4_t2_position_mismatch_after_first_mutation_stops(self):
        broker = _RecoveryBroker()
        original = _RecoveryBroker.submit_order

        def polluting_submit(request):
            # The resubmit "fills" and the broker now reports an unexplained
            # position: fresh facts are not CLEAN.
            order = original(broker, request)
            order.status = "filled"
            order.filled_qty = order.qty
            order.filled_avg_price = "100"
            broker.orders[-1] = order
            broker.positions = [SimpleNamespace(
                symbol="MSFT", qty="5", market_value="500")]
            return order

        broker.submit_order = polluting_submit
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            rows = self._seed_two_pendings(svc)
            result = self._run_recovery(svc)
            self.assertFalse(result["success"], result)
            self.assertEqual(len(broker.submit_calls), 1)
            self.assertEqual(
                svc.store.get_order(rows[1]["order_id"])["status"], "PENDING"
            )

    def test_f4_t3_clean_first_mutation_allows_the_second(self):
        broker = _RecoveryBroker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            self._seed_two_pendings(svc)
            result = self._run_recovery(svc)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(broker.submit_calls), 2)


# ---------------------------------------------------------------------------
# F01 — deadline close for SHORT uses absolute quantities
# ---------------------------------------------------------------------------


class _DeadlineBroker:
    """Paper broker holding a SHORT AAPL position with a live protective BUY."""

    def __init__(self, *, position_qty=-10, on_cancel=None):
        self.qty = float(position_qty)
        self.submits = []
        self.cancels = []
        self._on_cancel = on_cancel
        stamp = _now()
        parent = SimpleNamespace(
            id="b-parent", client_order_id="parent-1", symbol="AAPL",
            side="sell", qty="10", notional=None, filled_qty="10",
            filled_avg_price="100", status="filled", updated_at=stamp,
            legs=[],
        )
        self.stop_child = SimpleNamespace(
            id="b-stop", client_order_id="stop-1", symbol="AAPL", side="buy",
            qty="10", notional=None, filled_qty="0", filled_avg_price=None,
            status="new", updated_at=stamp, legs=[],
        )
        parent.legs = [self.stop_child]
        self.orders = [parent, self.stop_child]

    # -- authority GETs ----------------------------------------------------

    def get_account(self):
        return SimpleNamespace(
            id=PAPER, equity="100000", last_equity="100000",
            cash="90000", buying_power="160000",
        )

    def get_all_positions(self):
        if abs(self.qty) <= 1e-9:
            return []
        return [SimpleNamespace(symbol="AAPL", qty=str(self.qty),
                                market_value=str(self.qty * 100))]

    def get_orders(self, request=None):
        return list(self.orders)

    def get_order_by_id(self, order_id, filter=None):
        return next(o for o in self.orders if o.id == order_id)

    def get_order_by_client_order_id(self, cid):
        return next((o for o in self.orders if o.client_order_id == cid), None)

    # -- mutations ----------------------------------------------------------

    def cancel_order_by_id(self, order_id):
        self.cancels.append(order_id)
        self.get_order_by_id(order_id).status = "canceled"
        if self._on_cancel is not None:
            self._on_cancel(self)

    def submit_order(self, request):
        self.submits.append(request)
        side = str(getattr(getattr(request, "side", None), "value",
                           getattr(request, "side", "")))
        qty = float(getattr(request, "qty", 0) or 0)
        self.qty += qty if side == "buy" else -qty
        order = SimpleNamespace(
            id=f"broker-{len(self.submits)}",
            client_order_id=request.client_order_id, symbol="AAPL",
            side=side, qty=str(qty), notional=getattr(request, "notional", None),
            filled_qty=str(qty), filled_avg_price="100", status="filled",
            updated_at=_now(), legs=[],
        )
        self.orders.append(order)
        return order


class F01DeadlineShortTests(_GuardIsolated):
    def _seed_short_lot(self, svc, *, qty=10, exit_by="2026-09-01T14:00:00+00:00"):
        did = "dec-short-due"
        coid = client_order_id_for(did, "AAPL", "sell", role="open", seq=0)
        _, orders, _ = svc.store.create_outbox(
            decision_id=did, run_id=None, symbol="AAPL", action="SELL",
            target_position="SHORT",
            payload_json=json.dumps({
                "symbol": "AAPL", "action": "SELL", "target_position": "SHORT",
                "entry_policy": {"exit_by": exit_by},
            }),
            orders=[{"client_order_id": coid, "symbol": "AAPL", "side": "sell",
                     "quantity": qty, "notional": None}],
        )
        svc.store.sync_order_from_broker(
            orders[0]["order_id"], "FILLED", broker_order_id="b-parent",
            filled_qty=qty,
        )
        svc.store.record_fill(
            execution_id=f"b-parent:{qty:.12g}",
            order_id=orders[0]["order_id"], qty=qty, price=100.0,
            filled_at="2026-08-01T14:00:00+00:00",
        )
        return orders[0]

    def _service(self, tmp, broker):
        svc = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: _fresh_quote(s),
        )
        svc.store.ensure_account_binding(PAPER)
        return svc

    def test_f1_t2_short_deadline_close_is_not_rejected_by_signed_qty(self):
        broker = _DeadlineBroker(position_qty=-10)
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            self._seed_short_lot(svc)
            result = svc.enforce_exit_deadlines()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["deadline_exits"]), 1)
        self.assertNotIn(
            "no longer matches", str(result.get("error", "")), result
        )
        # One cancel (the protective BUY) + one BUY close of 10 shares.
        self.assertEqual(len(broker.cancels), 1)
        self.assertEqual(len(broker.submits), 1)
        close = broker.submits[0]
        self.assertEqual(
            str(getattr(getattr(close, "side", None), "value",
                        getattr(close, "side", ""))), "buy"
        )
        self.assertEqual(float(getattr(close, "qty", 0)), 10.0)
        self.assertEqual(broker.qty, 0)

    def test_f1_t3_short_deadline_with_owned_protection_submits_buy_close(self):
        # Same flow as T2 but the protective child stays live until the
        # deadline flow cancels it: post-cancel quantities are unchanged and
        # the close must proceed.
        broker = _DeadlineBroker(position_qty=-10)
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            self._seed_short_lot(svc)
            result = svc.enforce_exit_deadlines()
        self.assertTrue(result["success"], result)
        self.assertNotIn(
            "Deadline close quantity no longer matches the position",
            str(result.get("error", "")),
        )
        self.assertEqual(broker.cancels, ["b-stop"])
        self.assertEqual(len(broker.submits), 1)

    def test_f1_t4_position_changed_after_cancel_pauses_with_protection_gap(self):
        # During the cancellation the protective stop partially fills and
        # the broker position shrinks to -4 while durable lots still say -10:
        # never send a wrong-quantity close; pause with PROTECTION_GAP.
        broker = _DeadlineBroker(
            position_qty=-10,
            on_cancel=lambda b: setattr(b, "qty", -4.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            self._seed_short_lot(svc)
            result = svc.enforce_exit_deadlines()
            self.assertFalse(result["success"], result)
            self.assertTrue(result.get("paused"), result)
            self.assertTrue(result.get("protection_gap"), result)
            self.assertEqual(broker.submits, [])  # no close was sent
            state = svc.store.get_account_state(PAPER)
            assert state is not None
            self.assertEqual(state["state"], "PAUSED")
            self.assertTrue(
                any(r.startswith("PROTECTION_GAP:")
                    for r in json.loads(state["reasons_json"]))
            )

    def test_f1_t1_long_deadline_close_still_works(self):
        broker = _DeadlineBroker(position_qty=10)

        def flip_to_long(b):
            # keep position semantics long
            pass

        broker._on_cancel = None
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            # Seed a LONG due lot instead of a short one.
            did = "dec-long-due"
            coid = client_order_id_for(did, "AAPL", "buy", role="open", seq=0)
            _, orders, _ = svc.store.create_outbox(
                decision_id=did, run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG",
                payload_json=json.dumps({
                    "symbol": "AAPL", "action": "BUY",
                    "target_position": "LONG",
                    "entry_policy": {"exit_by": "2026-09-01T14:00:00+00:00"},
                }),
                orders=[{"client_order_id": coid, "symbol": "AAPL",
                         "side": "buy", "quantity": 10, "notional": None}],
            )
            svc.store.sync_order_from_broker(
                orders[0]["order_id"], "FILLED", broker_order_id="b-parent",
                filled_qty=10,
            )
            svc.store.record_fill(
                execution_id="b-parent:10", order_id=orders[0]["order_id"],
                qty=10, price=100.0,
                filled_at="2026-08-01T14:00:00+00:00",
            )
            # The protective child of a LONG is a SELL.
            broker.stop_child.side = "sell"
            result = svc.enforce_exit_deadlines()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(broker.submits), 1)
        close = broker.submits[0]
        self.assertEqual(
            str(getattr(getattr(close, "side", None), "value",
                        getattr(close, "side", ""))), "sell"
        )
        self.assertEqual(float(getattr(close, "qty", 0)), 10.0)


# ---------------------------------------------------------------------------
# F05 — a reversal completes only its close phase in one execution call
# ---------------------------------------------------------------------------


class _PositionedBroker:
    """Paper broker with one AAPL position; records POST/cancel calls."""

    def __init__(self, position_qty=5.0):
        self.qty = float(position_qty)
        self.submits = []
        self.cancels = []
        self._fail = False

    def get_account(self):
        return SimpleNamespace(
            id=PAPER, equity="100000", last_equity="100000",
            cash="90000", buying_power="160000",
        )

    def get_all_positions(self):
        if abs(self.qty) <= 1e-9:
            return []
        return [SimpleNamespace(symbol="AAPL", qty=str(self.qty),
                                market_value=str(self.qty * 100))]

    def get_orders(self, request=None):
        return []

    def get_order_by_id(self, order_id, filter=None):
        return None

    def get_order_by_client_order_id(self, cid):
        return None

    def submit_order(self, request):
        self.submits.append(request)
        if self._fail:
            raise RuntimeError("422 unprocessable: rejected")
        side = str(getattr(getattr(request, "side", None), "value",
                           getattr(request, "side", "")))
        qty = float(getattr(request, "qty", 0) or 0)
        self.qty += qty if side == "buy" else -qty
        return SimpleNamespace(
            id=f"broker-{len(self.submits)}",
            client_order_id=request.client_order_id, symbol="AAPL",
            side=side, qty=str(qty), notional=getattr(request, "notional", None),
            filled_qty="0", filled_avg_price=None, status="accepted",
            updated_at=_now(), legs=[],
        )

    def cancel_order_by_id(self, order_id):
        self.cancels.append(order_id)


def _flip_intent(*, to_short=True):
    from tradingagents.agents.schemas import (
        ExecutableAction,
        RiskDecision,
        build_trade_intent_from_risk_decision,
    )
    now = datetime.now(timezone.utc)
    return build_trade_intent_from_risk_decision(
        symbol="AAPL",
        trading_mode="trading",
        current_position="LONG" if to_short else "SHORT",
        allow_shorts=True,
        trade_date=now.date().isoformat(),
        decision=RiskDecision(
            action=ExecutableAction.SHORT if to_short else ExecutableAction.LONG,
            confidence="medium",
            risk_rationale="regr flip",
            required_controls="None.",
            entry_policy={
                "status": "READY", "minimum_price": 99.0,
                "maximum_price": 101.0,
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "exit_by": (now + timedelta(days=5)).isoformat(),
                "confirmation": "fixture observed setup",
            },
            stop_loss_price=105.0 if to_short else 95.0,
        ),
    ).model_dump(mode="json")


class F05ReversalCloseOnlyTests(_GuardIsolated):
    def _service(self, tmp, broker):
        svc = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: _fresh_quote(s),
        )
        svc._quarantine_rejection = MagicMock(return_value=None)
        return svc

    def _execute_flip(self, tmp, broker, *, to_short=True, intent=None,
                      **kwargs):
        with patch("tradingagents.execution.service._get_execution_config",
                   return_value=_caps_config(
                       max_symbol_concentration_pct=25.0)), patch(
            "tradingagents.screening.gate.check_entry_allowed", return_value=None
        ):
            return self._service(tmp, broker).execute(
                trade_intent=intent or _flip_intent(to_short=to_short),
                dollar_amount=5000.0,
                allow_shorts=True,
                **kwargs,
            )

    def _open_rows(self, svc, intent):
        did = intent.get("decision_id") or None
        from tradingagents.execution.store import canonical_decision_id

        did = canonical_decision_id(intent)
        return svc.store.list_orders_for_intent(
            svc.store.get_intent_by_decision(did)["intent_id"]
        )

    def test_f5_t1_long_to_short_close_accepted_never_opens_the_short(self):
        broker = _PositionedBroker(position_qty=5.0)
        intent = _flip_intent(to_short=True)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._execute_flip(tmp, broker, to_short=True,
                                        intent=intent)
            rows = self._open_rows(self._service(tmp, broker), intent)
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("reanalysis_required"), result)
        self.assertTrue(result.get("hold"), result)
        self.assertIn("fresh analysis", str(result.get("reason", "")))
        # Exactly one POST: the SELL close. No opening SHORT was sent.
        self.assertEqual(len(broker.submits), 1)
        first = broker.submits[0]
        self.assertEqual(
            str(getattr(getattr(first, "side", None), "value",
                        getattr(first, "side", ""))), "sell"
        )
        self.assertEqual(broker.qty, 0.0)
        # The durable open leg is CANCELED, never left replayable-PENDING.
        from tradingagents.execution.store import client_order_id_for

        open_coid = client_order_id_for(result["decision_id"], "AAPL",
                                        "sell", role="open", seq=1)
        open_rows = [r for r in rows if r["client_order_id"] == open_coid]
        self.assertTrue(open_rows)
        self.assertTrue(
            all(r["status"] == "CANCELED" for r in open_rows), rows
        )

    def test_f5_t2_short_to_long_close_accepted_never_opens_the_long(self):
        broker = _PositionedBroker(position_qty=-5.0)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._execute_flip(tmp, broker, to_short=False)
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("reanalysis_required"), result)
        self.assertEqual(len(broker.submits), 1)
        first = broker.submits[0]
        self.assertEqual(
            str(getattr(getattr(first, "side", None), "value",
                        getattr(first, "side", ""))), "buy"
        )
        self.assertEqual(broker.qty, 0.0)

    def test_f5_t3_close_safety_blocked_means_zero_open_posts(self):
        broker = _PositionedBroker(position_qty=5.0)
        guard = MagicMock()
        guard.enabled = True
        guard.check_order.return_value = SimpleNamespace(
            allowed=False, reasons=["kill switch"]
        )
        with patch("tradingagents.safety.get_safety_guard", return_value=guard):
            with tempfile.TemporaryDirectory() as tmp:
                result = self._execute_flip(tmp, broker, to_short=True)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("safety_blocked"))
        self.assertEqual(broker.submits, [])

    def test_f5_t4_close_rejected_means_zero_open_posts(self):
        broker = _PositionedBroker(position_qty=-5.0)  # SHORT -> LONG flip
        broker._fail = True
        with tempfile.TemporaryDirectory() as tmp:
            result = self._execute_flip(tmp, broker, to_short=False)
        self.assertFalse(result["success"])
        # Only the failed close POST happened; the open leg never did.
        self.assertEqual(len(broker.submits), 1)
        self.assertEqual(broker.qty, -5.0)  # position was never reduced
        from tradingagents.execution.store import client_order_id_for

        open_coid = client_order_id_for(result["decision_id"], "AAPL",
                                        "buy", role="open", seq=1)
        rows = result["orders"]
        open_rows = [r for r in rows if r["client_order_id"] == open_coid]
        self.assertTrue(open_rows)
        self.assertTrue(
            all(r["status"] == "CANCELED" for r in open_rows), rows
        )

    def test_f5_t5_close_filled_immediately_still_requires_fresh_analysis(self):
        broker = _PositionedBroker(position_qty=5.0)
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            original = broker.submit_order

            def filling_submit(request):
                resp = original(request)
                resp.status = "filled"
                return resp

            broker.submit_order = filling_submit
            with patch("tradingagents.execution.service._get_execution_config",
                       return_value=_caps_config(
                           max_symbol_concentration_pct=25.0)), patch(
                "tradingagents.screening.gate.check_entry_allowed",
                return_value=None,
            ):
                result = svc.execute(
                    trade_intent=_flip_intent(to_short=True),
                    dollar_amount=5000.0,
                    allow_shorts=True,
                )
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("reanalysis_required"), result)
        self.assertEqual(len(broker.submits), 1)

    def test_f5_t6_next_round_can_open_the_new_direction_normally(self):
        broker = _PositionedBroker(position_qty=0.0)
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            from tradingagents.agents.schemas import (
                ExecutableAction,
                RiskDecision,
                build_trade_intent_from_risk_decision,
            )
            now = datetime.now(timezone.utc)
            intent = build_trade_intent_from_risk_decision(
                symbol="AAPL", trading_mode="trading",
                current_position="NEUTRAL", allow_shorts=True,
                trade_date=now.date().isoformat(),
                decision=RiskDecision(
                    action=ExecutableAction.SHORT, confidence="medium",
                    risk_rationale="fresh round", required_controls="None.",
                    entry_policy={
                        "status": "READY", "minimum_price": 99.0,
                        "maximum_price": 101.0,
                        "expires_at": (now + timedelta(hours=1)).isoformat(),
                        "exit_by": (now + timedelta(days=5)).isoformat(),
                        "confirmation": "fixture observed setup",
                    },
                    stop_loss_price=105.0,
                ),
            ).model_dump(mode="json")
            with patch("tradingagents.execution.service._get_execution_config",
                       return_value=_caps_config(
                           max_symbol_concentration_pct=25.0)), patch(
                "tradingagents.screening.gate.check_entry_allowed",
                return_value=None,
            ):
                result = svc.execute(
                    trade_intent=intent, dollar_amount=5000.0,
                    allow_shorts=True,
                )
        self.assertTrue(result["success"], result)
        self.assertEqual(len(broker.submits), 1)
        self.assertEqual(broker.qty, -float(broker.submits[0].qty))


# ---------------------------------------------------------------------------
# F02 — Stop invalidates dispatched work; Start never restores it
# ---------------------------------------------------------------------------


class F02GenerationTests(unittest.TestCase):
    def setUp(self):
        from webui.utils.state import AppState

        self.state = AppState()

    def test_f2_t3_stop_bumps_generation_and_start_never_restores_it(self):
        captured_old = self.state.run_generation
        self.state.start_loop(["AAPL"], {})
        self.state.stop_loop_mode()
        captured_invalid = self.state.run_generation
        self.assertEqual(captured_invalid, captured_old + 1)
        # Operator immediately Starts again: stop flags clear, generation
        # must NOT be restored.
        self.state.start_loop(["AAPL"], {})
        self.assertFalse(self.state.stop_loop)
        self.assertFalse(self.state.stop_requested)
        self.assertEqual(self.state.run_generation, captured_invalid)
        # An old scheduler holding `captured_old` must see its loop
        # condition fail even though stop_loop is False again.
        old_scheduler_alive = (
            not self.state.stop_loop
            and captured_old == self.state.run_generation
        )
        self.assertFalse(old_scheduler_alive)
        new_scheduler_alive = (
            not self.state.stop_loop
            and self.state.run_generation == self.state.run_generation
        )
        self.assertTrue(new_scheduler_alive)
        # Same contract for market-hour mode.
        self.state.stop_market_hour_mode()
        self.assertEqual(self.state.run_generation, captured_invalid + 1)
        self.state.start_market_hour_mode(["AAPL"], {}, [10, 15])
        self.assertEqual(self.state.run_generation, captured_invalid + 1)

    def test_f2_t4_stop_without_restart_keeps_stop_requested(self):
        self.state.request_stop()
        self.assertTrue(self.state.is_stop_requested())
        self.assertTrue(self.state.stop_loop)
        self.assertTrue(self.state.stop_market_hour)


class F02StaleRunTests(unittest.TestCase):
    """Stale analysis runs finish their LLM work but never trade."""

    def _analysis_env(self, gate, blocked):
        graph = MagicMock()
        graph._resolve_memory_log_outcomes.return_value = None
        graph.propagator.create_initial_state.return_value = {}
        graph._graph_args_for_run.return_value = {"config": {}}
        graph.process_signal.return_value = "HOLD"

        def stream(init_state, **kwargs):
            blocked.set()
            gate.wait(timeout=10)
            yield {
                "final_trade_decision": "HOLD (fixture)",
                "final_trade_intent": None,
                "trading_mode": "investment",
            }

        graph._graph_for_run.return_value = (SimpleNamespace(stream=stream), None)
        return graph

    def _run_analysis(self, ticker, gate, blocked, patches):
        from webui.components.analysis import run_analysis

        with patches:
            return run_analysis(
                ticker,
                ["market"],
                {"rounds": 1, "level": "Shallow"},
                False,
                "quick-model",
                "deep-model",
            )

    def test_f2_t1_and_t2_stale_run_never_trades_or_clears_new_run_flag(self):
        import webui.components.analysis as analysis_mod
        from webui.utils.state import AppState

        app_state = AppState()
        app_state.init_symbol_state("AAPL")
        app_state.trade_enabled = True

        gate_a = threading.Event()
        blocked_a = threading.Event()
        graph = self._analysis_env(gate_a, blocked_a)
        logger = MagicMock()
        logger.start_run.return_value = "run-1"
        execute_mock = MagicMock()

        patches = patch.multiple(
            analysis_mod,
            app_state=app_state,
            TradingAgentsGraph=lambda *a, **k: graph,
            get_run_audit_logger=lambda: logger,
            create_chart=lambda *a, **k: None,
            execute_trade_after_analysis=execute_mock,
        )

        outcome = {}

        def run_a():
            outcome["a"] = self._run_analysis("AAPL", gate_a, blocked_a, patches)

        thread = threading.Thread(target=run_a)
        thread.start()
        self.assertTrue(blocked_a.wait(timeout=10))
        # Operator Stop: bump generation, then a NEW Start clears the flags
        # (as the start callback does) and a new run claims the symbol.
        app_state.request_stop()
        app_state.stop_requested = False
        app_state.get_state("AAPL")["analysis_running"] = True  # run B owns it
        gate_a.set()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())

        # T1: the stale run never reached execution and never persisted its
        # executable result.
        self.assertNotIn("trading_results", app_state.get_state("AAPL"))
        execute_mock.assert_not_called()
        self.assertIsNone(app_state.get_state("AAPL")["recommended_action"])
        # T2: the stale run's finally did not clear run B's running flag.
        self.assertTrue(app_state.get_state("AAPL")["analysis_running"])

    def test_f2_t1b_current_generation_run_trades_normally(self):
        import webui.components.analysis as analysis_mod
        from webui.utils.state import AppState

        app_state = AppState()
        app_state.init_symbol_state("AAPL")
        app_state.trade_enabled = True

        gate = threading.Event()
        blocked = threading.Event()
        gate.set()  # never blocks
        graph = self._analysis_env(gate, blocked)
        logger = MagicMock()
        logger.start_run.return_value = "run-2"
        execute_mock = MagicMock()

        patches = patch.multiple(
            analysis_mod,
            app_state=app_state,
            TradingAgentsGraph=lambda *a, **k: graph,
            get_run_audit_logger=lambda: logger,
            create_chart=lambda *a, **k: None,
            execute_trade_after_analysis=execute_mock,
        )
        self._run_analysis("AAPL", gate, blocked, patches)
        execute_mock.assert_called_once()
        self.assertEqual(
            app_state.get_state("AAPL")["recommended_action"], "HOLD"
        )
        # The current run's finally DID clear the running flag.
        self.assertFalse(app_state.get_state("AAPL")["analysis_running"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
