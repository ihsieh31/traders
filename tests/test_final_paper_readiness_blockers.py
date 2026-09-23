"""Final paper-readiness blocker regressions (R05 / R01 / R02).

Derived from TRADERS_FINAL_BLOCKERS_FIX_AND_ACCEPTANCE.md: the three
remaining P1 blockers after Plan A and Plan B. Every transport is faked
in-process: no real Alpaca mutation, no paid LLM call, no network.
"""

import json
import socket
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import tradingagents.agents  # noqa: F401  (production-safe import order)
from tradingagents.execution.authority import BrokerQuote
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import client_order_id_for


def now():
    return datetime.now(timezone.utc)


def opening(action="BUY", current="NEUTRAL", *, stamp=None):
    from tradingagents.agents.schemas import (
        EntryPolicy, RiskDecision, build_trade_intent_from_risk_decision,
    )

    stamp = stamp or now()
    short = action == "SHORT"
    decision = RiskDecision(
        action=action, confidence="high", risk_rationale="fixture",
        required_controls="stop",
        stop_loss_price=110 if short else 90,
        take_profit_price=80 if short else 120,
        entry_policy=EntryPolicy(
            status="READY", minimum_price=99, maximum_price=101,
            expires_at=(stamp + timedelta(hours=1)).isoformat(),
            exit_by=(stamp + timedelta(days=5)).isoformat(), confirmation="fixture",
        ),
    )
    data = build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment" if action == "BUY" else "trading",
        current_position=current, decision=decision, allow_shorts=True,
    ).model_dump(mode="json")
    data["generated_at"] = stamp.isoformat()
    return data


class ClockBroker:
    """Plan-A style broker whose get_clock() GET advances the wall clock.

    R05: the clock GET is the potentially blocking network call between
    the earlier freshness approval and the POST — a slow clock response
    must make the (already captured) facts stale BEFORE the POST boundary
    re-check, not after it.
    """

    def __init__(self, *, clock_delay=None, is_open=True):
        self.qty = 0
        self.orders = []
        self.submits = []
        self.cancels = []
        self.client_lookups = []
        self.clock_calls = 0
        self.clock_delay = clock_delay  # timedelta advanced on each GET
        self.is_open = is_open

    def get_account(self):
        return NS(id="clock-fixture", equity=100000, last_equity=100000,
                  cash=100000, buying_power=100000)

    def get_clock(self):
        self.clock_calls += 1
        import tradingagents.execution.authority as authority
        if self.clock_delay is not None:
            authority.utc_now = lambda: now() + self.clock_delay
        return NS(is_open=self.is_open, timestamp=authority.utc_now())

    def get_all_positions(self):
        return [NS(symbol="AAPL", qty=self.qty, market_value=self.qty * 100)] if self.qty else []

    def get_orders(self, request):
        if str(getattr(request.status, "value", request.status)) == "open":
            return [o for o in self.orders if o.status not in
                    {"filled", "canceled", "expired", "rejected"}]
        return list(reversed(self.orders))[:500]

    def get_order_by_id(self, order_id, filter=None):
        return next(o for o in self.orders if o.id == order_id)

    def get_order_by_client_id(self, client_id):
        self.client_lookups.append(client_id)
        return next((o for o in self.orders if o.client_order_id == client_id), None)

    def submit_order(self, request):
        self.submits.append(request)
        qty = float(getattr(request, "qty", 0) or 0)
        side = str(getattr(getattr(request, "side", None), "value",
                           getattr(request, "side", "")))
        self.qty += qty if side == "buy" else -qty
        order = NS(id=f"parent-{len(self.submits)}",
                   client_order_id=request.client_order_id,
                   symbol="AAPL", side=side, qty=qty, filled_qty=qty,
                   filled_avg_price=100, status="filled", updated_at=now(),
                   legs=[], notional=None)
        self.orders.append(order)
        return order


class _Guard:
    enabled = False

    def check_order(self, *a, **k):
        return NS(allowed=True, reasons=[])

    def check_llm_budget(self):
        return NS(allowed=True, reasons=[])


def network_forbidden():
    return (
        patch.object(socket.socket, "connect",
                     Mock(side_effect=AssertionError("network forbidden"))),
        patch.object(socket, "create_connection",
                     Mock(side_effect=AssertionError("network forbidden"))),
    )


def guard_patch():
    return patch("tradingagents.safety.get_safety_guard", return_value=_Guard())


def _first_error(result):
    """First error text from either the per-order results or the top level."""
    if result.get("results"):
        return str(result["results"][0].get("error") or "")
    return str(result.get("error") or "")


# ---------------------------------------------------------------------------
# R05 — the broker clock GET must precede the final freshness proof
# ---------------------------------------------------------------------------


class R05ClockBeforeFreshnessTests(unittest.TestCase):
    def _run(self, broker, *, quote_ttl=None, snapshot_ttl=None):
        import tempfile

        from tradingagents.dataflows import config as cfg
        from tradingagents.default_config import DEFAULT_CONFIG

        tmp = tempfile.mkdtemp(prefix="r05-")
        cfg.set_config({**DEFAULT_CONFIG, "data_cache_dir": tmp})
        self.addCleanup(cfg.set_config, cfg.get_config())
        quote_factory = lambda s: BrokerQuote(s, 99.9, 100.1, now())
        if snapshot_ttl is not None and quote_ttl == "3600":
            # R05: the isolated snapshot-stale case advances the authority
            # clock 20s inside get_clock(); the quote is re-stamped by the
            # SAME advanced authority clock so it stays fresh while the
            # snapshot (observed before the clock GET) reads as stale.
            def quote_factory(s, _broker=broker):
                import tradingagents.execution.authority as authority

                return BrokerQuote(s, 99.9, 100.1, authority.utc_now())
        service = ExecutionService(
            db_path=Path(tmp) / "execution.db",
            broker_factory=lambda: broker,
            quote_factory=quote_factory,
        )
        service._quarantine_rejection = lambda symbol: None
        env = {"TRADINGBUFFETT_EXECUTION_LOCK_DIR": str(Path(tmp) / "locks")}
        if quote_ttl is not None:
            env["TRADINGBUFFETT_QUOTE_TTL_SECONDS"] = quote_ttl
        if snapshot_ttl is not None:
            env["TRADINGBUFFETT_SNAPSHOT_TTL_SECONDS"] = snapshot_ttl
        patches = [patch.dict("os.environ", env, clear=False), guard_patch(),
                   *network_forbidden()]
        for p in patches:
            p.start()
        # Restore the authority clock AFTER execute() returns: a delayed
        # ClockBroker monkeypatches authority.utc_now DURING the call, so a
        # cleanup registered before that moment would capture the patched
        # function and leak the +20s clock into every later test.
        import tradingagents.execution.authority as authority

        real_utc_now = authority.utc_now
        self.addCleanup(
            lambda: (
                setattr(authority, "utc_now", real_utc_now),
                [p.stop() for p in reversed(patches)],
            )
        )
        return service.execute(trade_intent=opening(), dollar_amount=1000)

    def test_r05_clock_delay_rechecks_quote_freshness_before_post(self):
        """A slow clock GET stales the quote: the POST-boundary re-check must
        notice (zero POST, CANCELED row), never send the stale-facts order."""
        # Quote TTL 15s; snapshot TTL long enough to isolate the quote.
        broker = ClockBroker(clock_delay=timedelta(seconds=20))
        result = self._run(broker, quote_ttl="15", snapshot_ttl="3600")
        self.assertEqual(len(broker.submits), 0)
        self.assertFalse(result["success"])
        first = result["results"][0]
        self.assertTrue(first.get("pre_submit_blocked"))
        self.assertEqual(first.get("broker_calls"), 0)
        error = first["error"].lower()
        self.assertIn("quote", error)
        self.assertIn("stale", error)
        self.assertEqual(result["orders"][0]["status"], "CANCELED")

    def test_r05_clock_delay_rechecks_snapshot_freshness_before_post(self):
        """A slow clock GET stales the snapshot: the same fail-closed proof
        (zero POST, no submit-time POST, durable row canceled)."""
        # Snapshot TTL 15s; quote TTL long enough to isolate the snapshot.
        broker = ClockBroker(clock_delay=timedelta(seconds=20))
        result = self._run(broker, quote_ttl="3600", snapshot_ttl="15")
        self.assertEqual(len(broker.submits), 0)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("pre_submit_blocked") or result.get("paused"))
        self.assertEqual(result.get("broker_calls", 0), 0)
        error = _first_error(result).lower()
        self.assertIn("snapshot", error)
        self.assertIn("stale", error)
        # The durable row is provably POST-free -> CANCELED.
        statuses = [str(o.get("status") or "").upper() for o in result.get("orders", [])]
        self.assertIn("CANCELED", statuses)

    def test_r05_fast_clock_keeps_normal_opening_working(self):
        """An open market with fresh facts still POSTs the normal opening."""
        broker = ClockBroker()  # no clock delay: facts stay fresh
        result = self._run(broker)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(broker.submits), 1)
        self.assertGreaterEqual(broker.clock_calls, 1)
        self.assertEqual(broker.qty, 9)
        self.assertNotIn("stale", _first_error(result).lower())


# ---------------------------------------------------------------------------
# R02-B — a refused recovery resubmit keeps the row's exact prior status
# ---------------------------------------------------------------------------


class R02NoPostStateSemanticsTests(unittest.TestCase):
    """Real-service recovery: can_submit=False must refuse BEFORE the
    PENDING/UNKNOWN -> SUBMITTING transition, so no-POST rows are not
    masked as ambiguous-outcome UNKNOWN."""

    def _seeded_service(self, tmp, broker, *, status="PENDING"):
        from tradingagents.dataflows import config as cfgmod
        from tradingagents.default_config import DEFAULT_CONFIG

        service = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: BrokerQuote(
                s, 99.0, 101.0, datetime.now(timezone.utc)),
        )
        store = service.store
        guard = NS(enabled=True, check_order=lambda *a, **k: NS(allowed=True, reasons=[]))
        with patch.object(cfgmod, "_config",
                          {**DEFAULT_CONFIG, "auto_screening_enabled": True,
                           "data_cache_dir": str(tmp)}), patch(
                "tradingagents.safety.get_safety_guard", return_value=guard):
            service.startup_recover()  # bind the DB to this broker account
        stamp = datetime.now(timezone.utc)
        intent = {
            "symbol": "AAPL", "action": "BUY", "target_position": "LONG",
            "trading_mode": "investment",
            "entry_policy": {
                "status": "READY", "minimum_price": 99, "maximum_price": 101,
                "expires_at": (stamp + timedelta(hours=1)).isoformat(),
                "exit_by": (stamp + timedelta(days=5)).isoformat(),
                "confirmation": "fixture",
            },
            "risk_controls": {"required_controls": "stop",
                              "stop_loss_price": 90.0,
                              "take_profit_price": 120.0},
            "generated_at": stamp.isoformat(),
        }
        coid = client_order_id_for("r02-decision", "AAPL", "buy", role="open", seq=0)
        store.create_outbox(
            decision_id="r02-decision", run_id=None, symbol="AAPL",
            action="BUY", target_position="LONG",
            payload_json=json.dumps(intent, sort_keys=True),
            orders=[{"client_order_id": coid, "symbol": "AAPL", "side": "buy",
                     "quantity": None, "notional": 1000.0}],
        )
        if status != "PENDING":
            # Build a genuinely UNKNOWN row through the legal state machine:
            # PENDING -> SUBMITTING -> UNKNOWN (a crash mid-submit).
            row = store.get_order_by_client(coid)
            ok, _ = store.transition_order(row["order_id"], "SUBMITTING")
            assert ok
            ok, _ = store.transition_order(row["order_id"], status)
            assert ok, f"cannot seed status {status}"
        return service, store

    def _refusing_broker(self):
        class B:
            def __init__(self):
                self.submits = []
                self.orders = []

            def get_clock(self):
                return NS(is_open=True, timestamp=datetime.now(timezone.utc))

            def get_account(self):
                return NS(id="paper-r02", equity="100000", last_equity="100000",
                          cash="80000", buying_power="200000")

            def get_all_positions(self):
                return []

            def get_orders(self, request=None):
                return list(self.orders)

            def get_order_by_client_order_id(self, cid):
                return None

            def get_order_by_id(self, order_id, filter=None):
                raise RuntimeError("lookup uncertain")

            def submit_order(self, request):
                self.submits.append(request)
                raise RuntimeError("must not POST: stop authority refused")

        return B()

    def _recover(self, service, tmp, can_submit):
        from tradingagents.dataflows import config as cfgmod
        from tradingagents.default_config import DEFAULT_CONFIG

        guard = NS(enabled=True, check_order=lambda *a, **k: NS(allowed=True, reasons=[]))
        patches = [
            patch.dict("os.environ", {"TRADINGBUFFETT_EXECUTION_LOCK_DIR":
                                      str(Path(tmp) / "locks")}, clear=False),
            patch("tradingagents.safety.get_safety_guard", return_value=guard),
            patch.object(cfgmod, "_config",
                         {**DEFAULT_CONFIG, "auto_screening_enabled": True,
                          "data_cache_dir": str(tmp)}),
            patch("tradingagents.screening.gate.check_entry_allowed", return_value=None),
            *network_forbidden(),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return service.startup_recover(can_submit=can_submit)

    def _statuses(self, store):
        return [str(o.get("status") or "").upper() for o in store.list_all_orders()]

    def test_r02_pending_row_stays_pending_when_submit_guard_refuses(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="r02-pending-")
        broker = self._refusing_broker()
        service, store = self._seeded_service(tmp, broker, status="PENDING")
        result = self._recover(service, tmp, can_submit=lambda: False)
        self.assertFalse(result["success"])
        self.assertEqual(len(broker.submits), 0)
        statuses = self._statuses(store)
        self.assertIn("PENDING", statuses, "a no-POST refusal must keep PENDING")
        self.assertNotIn("UNKNOWN", statuses)
        self.assertNotIn("REJECTED", statuses)
        self.assertNotIn("SUBMITTING", statuses)

    def test_r02_unknown_row_stays_unknown_when_submit_guard_refuses(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="r02-unknown-")
        broker = self._refusing_broker()
        # A genuinely UNKNOWN row (ambiguous earlier submit outcome).
        service, store = self._seeded_service(tmp, broker, status="UNKNOWN")
        result = self._recover(service, tmp, can_submit=lambda: False)
        self.assertFalse(result["success"])
        self.assertEqual(len(broker.submits), 0)
        statuses = self._statuses(store)
        self.assertIn("UNKNOWN", statuses, "a no-POST refusal must keep UNKNOWN")
        self.assertNotIn("PENDING", statuses)
        self.assertNotIn("REJECTED", statuses)
        self.assertNotIn("SUBMITTING", statuses)

    def test_r02_allowed_recovery_transitions_to_submitting_and_posts(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="r02-allowed-")

        class AcceptingBroker:
            def __init__(self):
                self.submits = []
                self.orders = []
                self.qty = 0

            def get_clock(self):
                return NS(is_open=True, timestamp=datetime.now(timezone.utc))

            def get_account(self):
                return NS(id="paper-r02", equity=100000, last_equity=100000,
                          cash=80000, buying_power=200000)

            def get_all_positions(self):
                return ([NS(symbol="AAPL", qty=self.qty, market_value=self.qty * 100)]
                        if self.qty else [])

            def get_orders(self, request=None):
                # The accepted parent shows up as broker history so the
                # post-recovery reconcile can adopt it and report CLEAN.
                return list(self.orders)

            def get_order_by_client_order_id(self, cid):
                return next((o for o in self.orders if o.client_order_id == cid), None)

            def get_order_by_id(self, order_id, filter=None):
                raise RuntimeError("lookup uncertain")

            def submit_order(self, request):
                self.submits.append(request)
                # The resubmit stays LIVE (accepted, unfilled): a filled
                # protected opening without broker-side child facts is a
                # N07 protection gap and must pause, so this fake keeps the
                # parent unfilled for the state-transition assertions.
                order = NS(id=f"recovered-{len(self.submits)}",
                           client_order_id=request.client_order_id,
                           symbol="AAPL", side="buy", qty=9, filled_qty=0,
                           filled_avg_price=None, status="accepted",
                           updated_at=now(), legs=[], notional=None)
                self.orders.append(order)
                return order

        broker = AcceptingBroker()
        service, store = self._seeded_service(tmp, broker, status="PENDING")
        result = self._recover(service, tmp, can_submit=lambda: True)
        self.assertEqual(len(broker.submits), 1, "the authorized resume POSTs")
        row = next(o for o in store.list_all_orders()
                   if o["client_order_id"].startswith("ta-"))
        # The broker accepted the resubmitted parent (live, unfilled); the
        # durable row adopts the broker-derived status via reconciliation.
        self.assertIn(str(row["status"]).upper(), {"FILLED", "ACCEPTED"})
        self.assertTrue(result["success"], result)

    def test_r02_guard_flip_before_transition_never_enters_submitting(self):
        """The guard flips between the outer precheck and the submit
        boundary: deterministic callback, row never enters SUBMITTING."""
        import tempfile

        tmp = tempfile.mkdtemp(prefix="r02-flip-")
        broker = self._refusing_broker()
        service, store = self._seeded_service(tmp, broker, status="PENDING")
        transitions = []
        original = store.transition_order

        def recording_transition(order_id, to_status, **kwargs):
            transitions.append(str(to_status).upper())
            return original(order_id, to_status, **kwargs)

        store.transition_order = recording_transition
        self._recover(service, tmp, can_submit=lambda: False)
        self.assertNotIn("SUBMITTING", transitions,
                         "the refused resubmit never entered SUBMITTING")

    def test_deadline_recovery_looks_up_unknown_order_without_duplicate_post(self):
        """An expired entry window blocks resubmission, not existing-order
        lookup/adoption by the original deterministic client order ID."""
        import tempfile

        from tradingagents.long_run_support.sessions import make_session_submit_guard

        tmp = tempfile.mkdtemp(prefix="deadline-unknown-recovery-")
        class LookupOnlyBroker(ClockBroker):
            lookup_completed = False

            def get_orders(self, request):
                # Model an accepted order omitted from the first account
                # snapshot but returned by deterministic client-ID lookup.
                return list(self.orders) if self.lookup_completed else []

            def get_order_by_client_id(self, client_id):
                found = super().get_order_by_client_id(client_id)
                self.lookup_completed = True
                return found

        broker = LookupOnlyBroker()
        service, store = self._seeded_service(tmp, broker, status="UNKNOWN")
        original = store.list_all_orders()[0]
        broker.orders.append(NS(
            id="broker-existing-order",
            client_order_id=original["client_order_id"],
            symbol="AAPL", side="buy", qty=10, filled_qty=0,
            filled_avg_price=None, status="accepted", updated_at=now(),
            legs=[], notional=1000.0,
        ))
        after_deadline = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)
        can_submit = make_session_submit_guard(
            session_date="2026-09-08", effective_target="11:00",
            now_fn=lambda: after_deadline,
        )
        self.assertFalse(can_submit())

        result = self._recover(service, tmp, can_submit=can_submit)

        self.assertTrue(result["success"], result)
        self.assertIn(original["client_order_id"], broker.client_lookups)
        self.assertEqual(broker.submits, [])
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(store.list_all_orders()), 1)
        self.assertEqual(store.get_intent_by_decision("r02-decision")["decision_id"],
                         "r02-decision")
        recovered = store.get_order_by_client(original["client_order_id"])
        self.assertEqual(recovered["broker_order_id"], "broker-existing-order")


# ---------------------------------------------------------------------------
# R01 / R02-A — WebUI scheduler: config before recovery + generation guard
# ---------------------------------------------------------------------------










if __name__ == "__main__":
    unittest.main()
