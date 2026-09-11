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
        self.clock_calls = 0
        self.clock_delay = clock_delay  # timedelta advanced on each GET
        self.is_open = is_open

    def get_account(self):
        return NS(id="clock-fixture", equity=100000, last_equity=100000,
                  cash=100000, buying_power=100000)

    def get_clock(self):
        self.clock_calls += 1
        if self.clock_delay is not None:
            import tradingagents.execution.authority as authority

            authority.utc_now = lambda: now() + self.clock_delay
        return NS(is_open=self.is_open)

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
        env = {"TRADINGAGENTS_EXECUTION_LOCK_DIR": str(Path(tmp) / "locks")}
        if quote_ttl is not None:
            env["TRADINGAGENTS_QUOTE_TTL_SECONDS"] = quote_ttl
        if snapshot_ttl is not None:
            env["TRADINGAGENTS_SNAPSHOT_TTL_SECONDS"] = snapshot_ttl
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
                return NS(is_open=True)

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
            patch.dict("os.environ", {"TRADINGAGENTS_EXECUTION_LOCK_DIR":
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
                return NS(is_open=True)

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
                self.qty += 9
                order = NS(id=f"recovered-{len(self.submits)}",
                           client_order_id=request.client_order_id,
                           symbol="AAPL", side="buy", qty=9, filled_qty=9,
                           filled_avg_price=100, status="filled",
                           updated_at=now(), legs=[], notional=None)
                self.orders.append(order)
                return order

        broker = AcceptingBroker()
        service, store = self._seeded_service(tmp, broker, status="PENDING")
        result = self._recover(service, tmp, can_submit=lambda: True)
        self.assertEqual(len(broker.submits), 1, "the authorized resume POSTs")
        row = next(o for o in store.list_all_orders()
                   if o["client_order_id"].startswith("ta-"))
        # The broker immediately filled the resubmitted parent; the durable
        # row adopts the broker-derived terminal status via reconciliation.
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


# ---------------------------------------------------------------------------
# R01 / R02-A — WebUI scheduler: config before recovery + generation guard
# ---------------------------------------------------------------------------


class WebUISchedulerFixture(unittest.TestCase):
    """Shared scaffolding to drive the extracted _scheduler_thread body."""

    def _sched_kwargs(self, **overrides):
        kwargs = dict(
            symbols=["AAPL"],
            market_hour_enabled=False,
            market_hours_list=[],
            loop_enabled=False,
            analysts_market=True,
            analysts_social=False,
            analysts_news=False,
            analysts_fundamentals=False,
            analysts_macro=False,
            research_depth="Shallow",
            allow_shorts=False,
            quick_llm="quick-model",
            deep_llm="deep-model",
            quick_llm_params={},
            deep_llm_params={},
            llm_provider="openai",
            backend_url="",
            output_language="en",
            checkpoint_enabled=False,
            provider_settings={},
            trade_enabled=False,
            trade_amount=1000,
            auto_screening_on=False,
        )
        kwargs.update(overrides)
        return kwargs

    def _run_scheduler(self, state, exec_service_factory, *, dispatch_generation=None,
                       dispatch_analysis=None, **overrides):
        import webui.callbacks.control_callbacks as cc

        if dispatch_generation is None:
            dispatch_generation = state.run_generation
        if dispatch_analysis is not None:
            dispatches = {"fn": dispatch_analysis}
        else:
            dispatches = {"fn": Mock()}
        patches = [patch.object(cc, "app_state", state),
                   patch.object(cc, "ExecutionService", exec_service_factory),
                   patch.object(cc, "start_analysis", lambda *a, **k: dispatches["fn"](*a, **k))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        kwargs = self._sched_kwargs(
            scheduler_generation=dispatch_generation, **overrides
        )
        cc._scheduler_thread(**kwargs)
        return dispatches["fn"]


class R01WebUIConfigBeforeRecoveryTests(WebUISchedulerFixture):
    def setUp(self):
        import tradingagents.dataflows.config as cfgmod
        from tradingagents.default_config import DEFAULT_CONFIG

        self.saved_config = cfgmod.get_config()
        # Simulate a stale manual-mode global from a previous process.
        cfgmod.set_config({**DEFAULT_CONFIG, "auto_screening_enabled": False})
        self.cfgmod = cfgmod
        self.addCleanup(cfgmod.set_config, self.saved_config)

    def test_r01_webui_applies_auto_screening_config_before_recovery(self):
        """Global manual, run auto: recovery must observe the run's
        auto_screening_enabled=True, and the Top20 gate (no valid selection)
        blocks the recovery opening with zero broker POSTs."""
        from webui.utils.state import AppState

        state = AppState()
        state.trade_enabled = True
        seen = {}
        submits = []

        class RecordingService:
            def startup_recover(self, can_submit=None):
                from tradingagents.dataflows.config import get_config

                seen["auto_screening_enabled"] = bool(
                    (get_config() or {}).get("auto_screening_enabled")
                )
                seen["can_submit"] = (
                    bool(can_submit()) if can_submit is not None else None
                )
                # The gate is active in auto mode: with no valid Top20
                # selection the recovery opening is refused (zero POST).
                from tradingagents.screening.gate import check_entry_allowed

                gate_reason = check_entry_allowed(
                    "AAPL", config=get_config() or {})
                seen["gate_reason"] = gate_reason
                if gate_reason:
                    return {
                        "success": False,
                        "account_execution_state": "PAUSED",
                        "reconciliation_reasons": [gate_reason],
                    }
                return {"success": True, "account_execution_state": "CLEAN",
                        "reconciliation_reasons": []}

        self._run_scheduler(
            state, lambda: RecordingService(),
            trade_enabled=True,
            provider_settings={"auto_screening_enabled": True,
                               "screening_provider": "openai"},
        )
        self.assertTrue(
            seen["auto_screening_enabled"],
            "recovery must see the run's runtime config, not the stale global",
        )
        self.assertTrue(seen["can_submit"], "current generation may submit")
        self.assertEqual(len(submits), 0)
        self.assertIsNotNone(
            seen["gate_reason"],
            "auto-mode recovery must hit the Top20 gate (no valid selection)",
        )

    def test_r01_webui_config_failure_prevents_recovery(self):
        """A failing config application stops the scheduler before any
        recovery, broker mutation, or analysis dispatch."""
        import webui.callbacks.control_callbacks as cc
        from webui.utils.state import AppState

        state = AppState()
        state.trade_enabled = True

        def failing_set_config(config):
            raise RuntimeError("config authority unavailable")

        executed = Mock()
        with patch.object(cc, "app_state", state), patch.object(
            cc, "start_analysis", executed
        ), patch.object(cc, "ExecutionService") as exec_svc, patch.object(
            self.cfgmod, "set_config", failing_set_config
        ):
            kwargs = self._sched_kwargs(
                trade_enabled=True,
                provider_settings={"auto_screening_enabled": True},
                scheduler_generation=state.run_generation,
            )
            cc._scheduler_thread(**kwargs)

        exec_svc.assert_not_called()
        executed.assert_not_called()
        self.assertFalse(state.trade_enabled,
                         "trade must be disabled on config failure")

    def test_r01_webui_call_order_config_applies_before_recovery(self):
        """Call-order recorder: apply_config strictly precedes
        startup_recover (never the reverse)."""
        from webui.utils.state import AppState

        state = AppState()
        state.trade_enabled = True
        order = []
        real_set_config = self.cfgmod.set_config

        def recording_set_config(config):
            order.append("apply_config")
            real_set_config(config)

        service = Mock()
        service.startup_recover = lambda can_submit=None: (
            order.append("startup_recover")
            or {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}
        )
        with patch.object(self.cfgmod, "set_config", recording_set_config):
            self._run_scheduler(
                state, lambda: service,
                trade_enabled=True,
                provider_settings={"auto_screening_enabled": True},
            )

        self.assertEqual(order, ["apply_config", "startup_recover"],
                         "config application must precede recovery")


class R01WebUIRuntimeCompletenessTests(WebUISchedulerFixture):
    """R01 runtime completeness: recovery must see THIS run's full
    execution runtime — auto_screening_enabled, allow_shorts and the
    trading_mode derived from it — under strict types, with every
    contract violation fail-closed before recovery runs."""

    def setUp(self):
        import tradingagents.dataflows.config as cfgmod

        self.saved_config = cfgmod.get_config()
        self.cfgmod = cfgmod
        self.addCleanup(cfgmod.set_config, self.saved_config)

    def _recording_service(self):
        class _Recorder:
            def __init__(self):
                self.recover_calls = 0
                self.seen = {}

            def startup_recover(self, can_submit=None):
                from tradingagents.dataflows.config import get_config

                self.recover_calls += 1
                cfg = get_config() or {}
                self.seen.update({
                    "auto_screening_enabled": cfg.get("auto_screening_enabled"),
                    "allow_shorts": cfg.get("allow_shorts"),
                    "trading_mode": cfg.get("trading_mode"),
                    "runtime_marker": cfg.get("runtime_marker"),
                    "can_submit": (
                        bool(can_submit()) if can_submit is not None else None
                    ),
                })
                return {"success": True, "account_execution_state": "CLEAN",
                        "reconciliation_reasons": []}

        return _Recorder()

    def _stale_global(self, **overrides):
        from tradingagents.default_config import DEFAULT_CONFIG

        stale = {**DEFAULT_CONFIG, "runtime_marker": "stale-global"}
        stale.update(overrides)
        self.cfgmod.set_config(stale)
        return stale

    def test_r01_long_only_run_overrides_stale_short_enabled_global(self):
        """Global short-enabled, run long-only: recovery must see the run's
        allow_shorts=False and the trading_mode re-derived from it."""
        from webui.utils.state import AppState

        self._stale_global(allow_shorts=True, trading_mode="trading",
                           auto_screening_enabled=True)
        state = AppState()
        state.trade_enabled = True
        service = self._recording_service()
        self._run_scheduler(
            state, lambda: service,
            trade_enabled=True,
            allow_shorts=False,
            provider_settings={"auto_screening_enabled": True},
        )
        self.assertEqual(service.recover_calls, 1)
        self.assertIs(service.seen["allow_shorts"], False)
        self.assertEqual(service.seen["trading_mode"], "investment")
        self.assertIs(service.seen["auto_screening_enabled"], True)
        self.assertEqual(service.seen["can_submit"], True)

    def test_r01_short_enabled_run_overrides_stale_long_only_global(self):
        """Global long-only, run short-enabled: recovery must see the run's
        allow_shorts=True and trading_mode='trading'."""
        from webui.utils.state import AppState

        self._stale_global(allow_shorts=False, trading_mode="investment",
                           auto_screening_enabled=False)
        state = AppState()
        state.trade_enabled = True
        service = self._recording_service()
        self._run_scheduler(
            state, lambda: service,
            trade_enabled=True,
            allow_shorts=True,
            provider_settings={"auto_screening_enabled": False},
        )
        self.assertEqual(service.recover_calls, 1)
        self.assertIs(service.seen["allow_shorts"], True)
        self.assertEqual(service.seen["trading_mode"], "trading")
        self.assertIs(service.seen["auto_screening_enabled"], False)

    def test_r01_provider_settings_cannot_override_explicit_allow_shorts(self):
        """The scheduler's explicit allow_shorts is the authority:
        provider_settings' allow_shorts/trading_mode lose to it."""
        from webui.utils.state import AppState

        self._stale_global(allow_shorts=False, trading_mode="investment")
        state = AppState()
        state.trade_enabled = True
        service = self._recording_service()
        self._run_scheduler(
            state, lambda: service,
            trade_enabled=True,
            allow_shorts=False,
            provider_settings={"allow_shorts": True, "trading_mode": "trading"},
        )
        self.assertEqual(service.recover_calls, 1)
        self.assertIs(service.seen["allow_shorts"], False,
                      "explicit scheduler argument must override provider_settings")
        self.assertEqual(service.seen["trading_mode"], "investment")

    def _fail_closed_case(self, *, allow_shorts=True, provider_settings=None):
        from webui.utils.state import AppState

        stale = self._stale_global(allow_shorts=True, trading_mode="trading",
                                   auto_screening_enabled=True)
        state = AppState()
        state.trade_enabled = True
        service = self._recording_service()
        kwargs = dict(trade_enabled=True, allow_shorts=allow_shorts)
        if provider_settings is not None or allow_shorts is True:
            kwargs["provider_settings"] = provider_settings
        self._run_scheduler(state, lambda: service, **kwargs)
        self.assertEqual(service.recover_calls, 0,
                         "a config contract violation must stop before recovery")
        self.assertFalse(state.trade_enabled, "trade must be disabled")
        self.assertEqual(self.cfgmod.get_config().get("allow_shorts"), True,
                         "the stale global config must be left untouched")
        self.assertEqual(self.cfgmod.get_config().get("runtime_marker"),
                         "stale-global")
        return service

    def test_r01_allow_shorts_string_false_fails_closed(self):
        """allow_shorts='false' is a contract violation: no bool() coercion
        that would silently flip the run to short-enabled."""
        self._fail_closed_case(allow_shorts="false")

    def test_r01_allow_shorts_none_fails_closed(self):
        self._fail_closed_case(allow_shorts=None)

    def test_r01_invalid_provider_settings_type_fails_closed(self):
        self._fail_closed_case(provider_settings=[])

    def test_r01_invalid_auto_screening_type_fails_closed(self):
        self._fail_closed_case(
            provider_settings={"auto_screening_enabled": "false"})

    def test_r01_provider_settings_none_is_legal_and_recovery_runs(self):
        """provider_settings=None is a legal value: the old global config is
        the base, allow_shorts/trading_mode are still overridden, and the
        scheduler proceeds into recovery."""
        from webui.utils.state import AppState

        self._stale_global(allow_shorts=False, trading_mode="investment",
                           auto_screening_enabled=False)
        state = AppState()
        state.trade_enabled = True
        service = self._recording_service()
        self._run_scheduler(
            state, lambda: service,
            trade_enabled=True,
            allow_shorts=False,
            provider_settings=None,
        )
        self.assertEqual(service.recover_calls, 1)
        self.assertIs(service.seen["allow_shorts"], False)
        self.assertEqual(service.seen["trading_mode"], "investment")
        self.assertIs(service.seen["auto_screening_enabled"], False)
        self.assertEqual(service.seen["runtime_marker"], "stale-global",
                         "the old global config must remain the merge base")
        self.assertEqual(service.seen["can_submit"], True)


class R02WebUIGenerationRecoveryGuardTests(WebUISchedulerFixture):
    def test_r02_webui_old_generation_cannot_resubmit_during_recovery(self):
        """Stop→Start during recovery's slow lookups: the stop flag is
        cleared by the new Start, so only the generation token can refuse
        the old run's recovery POST."""
        import webui.callbacks.control_callbacks as cc
        from webui.utils.state import AppState

        state = AppState()
        state.trade_enabled = True
        dispatch_generation = state.run_generation

        service = NS()
        service.can_submits = []

        def recover(can_submit=None):
            # The broker lookup is slow: the operator Stops (generation += 1,
            # stop_requested=True) then Starts a new run (stop_requested
            # cleared again) between the dispatch and the submit boundary.
            state.request_stop()
            state.stop_requested = False  # Start cleared the stop flag
            state.run_generation = dispatch_generation + 1  # new run lives
            if can_submit is not None:
                service.can_submits.append(bool(can_submit()))
            return {"success": False, "account_execution_state": "PAUSED",
                    "reconciliation_reasons": [
                        "recovery resubmit deferred by stop/window authority"]}

        service.startup_recover = recover
        with patch.object(cc, "app_state", state), patch.object(
            cc, "ExecutionService", lambda: service
        ):
            kwargs = self._sched_kwargs(
                trade_enabled=True,
                scheduler_generation=dispatch_generation,
            )
            cc._scheduler_thread(**kwargs)

        self.assertEqual(service.can_submits, [False],
                         "the old generation's can_submit must refuse")

    def test_r02_webui_current_generation_can_recover(self):
        """No generation bump, no stop: the submit guard allows the normal
        recovery resubmit path."""
        from webui.utils.state import AppState

        state = AppState()
        state.trade_enabled = True
        dispatch_generation = state.run_generation
        service = NS()
        service.can_submits = []

        def recover(can_submit=None):
            if can_submit is not None:
                service.can_submits.append(bool(can_submit()))
            return {"success": True, "account_execution_state": "CLEAN",
                    "reconciliation_reasons": []}

        service.startup_recover = recover
        self._run_scheduler(
            state, lambda: service,
            trade_enabled=True,
            dispatch_generation=dispatch_generation,
        )
        self.assertEqual(service.can_submits, [True],
                         "the current generation's can_submit must allow")


if __name__ == "__main__":
    unittest.main()
