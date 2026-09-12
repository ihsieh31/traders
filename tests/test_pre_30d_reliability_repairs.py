"""Pre-30-day reliability repairs (offline fakes).

F-03: bounded scheduler retry (3 attempts, 5.0 s apart, via deps.sleep_fn;
LongRunStop never retried; persistent failure still fails closed).
F-04: an ordinary exception inside run_daily_round finalizes the observation
STOPPED with UNEXPECTED_ROUND_ERROR evidence instead of escaping the process;
KeyboardInterrupt still propagates.
F-01: the reconciliation baseline stays frozen under normal
save_account_state(); only the explicit operator rebase API
(Reconciler.rebase_baseline -> ExecutionStore.rebase_account_state) may
replace it, and only under strict preconditions.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz

from tradingagents import long_run as lr
from tradingagents.execution import (
    BrokerAuthorityError,
    ExecutionStore,
    Reconciler,
    capture_broker_snapshot,
)

_ET = pytz.timezone("US/Eastern")

SESSION_A = "2026-09-08"  # Tuesday

ACCOUNT_ID = "paper-rebase"


def _rows(*specs):
    """Calendar rows as dicts: (date, close_hhmm)."""
    return [
        {"date": day, "open": "09:30", "close": close}
        for day, close in specs
    ]


def _valid_cfg(**overrides):
    cfg = lr.default_long_run_config()
    cfg.update({
        "duration_calendar_days": 30,
        "run_time_et": "11:00",
        "base_trade_notional_usd": 1000.0,
        "analysts": ["market", "social", "news", "fundamentals"],
        "research_depth": 3,
        "output_language": "English",
        "analysis_provider": "openai",
        "analysis_model": "gpt-fake-analysis",
        "analysis_backend_url": None,
        "decision_provider": "openai",
        "decision_model": "gpt-fake-decision",
        "decision_backend_url": None,
        "screening_provider": "openai",
        "screening_model": "gpt-fake-screening",
        "screening_backend_url": None,
    })
    cfg.update(overrides)
    return cfg


class FakeBroker:
    def __init__(self, equity=100000.0, cash=50000.0, positions=()):
        self._account = SimpleNamespace(
            id="acct-fake-1", equity=equity, cash=cash, buying_power=cash,
        )
        self._positions = list(positions)

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return list(self._positions)


class FakeService:
    def enforce_exit_deadlines(self):
        return {"success": True, "deadline_exits": [], "broker_calls": 0}

    def startup_recover(self, can_submit=None):
        return {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}


class FakeGraph:
    def __init__(self):
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        return ({"final_trade_intent": {"symbol": symbol, "action": "BUY"},
                 "final_trade_decision": "BUY"}, "BUY")


def _fake_plan(symbols):
    return SimpleNamespace(
        stopped=False,
        deep_analysis_set=list(symbols),
        top20=[{"symbol": s, "rank": i + 1, "screening_score": 90.0 - i,
                "short_reason": "fake reason"} for i, s in enumerate(symbols)],
        selection_date=SESSION_A, as_of=SESSION_A, cached=False,
        overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
        screening_description="Screening=openai/gpt-fake-screening",
        stop_reason_text=lambda: "",
    )


def _deps(**overrides):
    kwargs = {
        "screening_fn": lambda config, refresh=False: _fake_plan(("AAA",)),
        "graph_factory": lambda config: FakeGraph(),
        "execution_service_factory": lambda: FakeService(),
        "broker_client_factory": lambda: FakeBroker(),
        "alert_fn": lambda subject, body, runtime: {"sent": False},
        "sleep_fn": lambda seconds: None,
    }
    kwargs.update(overrides)
    return lr.LongRunDeps(**kwargs)


class IsolatedTest(unittest.TestCase):
    """Same isolation contract as the Phase-D tests: private long-run dir,
    private cwd, isolated safety guard, cleared calendar cache."""

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="pre30d-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.old_env = dict(os.environ)
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")
        # Snapshot process-global config: rounds call set_config() via
        # _apply_runtime_config(); none of it may leak into other test files.
        import tradingagents.dataflows.config as _cfgmod

        self._saved_config = _cfgmod.get_config()
        self._cfgmod = _cfgmod
        from tradingagents.safety import SafetyGuard

        self._isolated_guard = SafetyGuard(
            state_path=self.workdir / "safety" / "state.json",
            kill_switch_path=self.workdir / "safety" / "KILL_SWITCH",
        )
        self._safety = patch(
            "tradingagents.safety.get_safety_guard",
            return_value=self._isolated_guard,
        )
        self._safety.start()

    def tearDown(self):
        self._safety.stop()
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)
        self._cfgmod._config = dict(self._saved_config)
        try:
            from tradingagents.safety import reset_safety_guard

            reset_safety_guard()
        except Exception:
            pass
        try:
            from tradingagents.dataflows.market_calendar import (
                clear_calendar_cache,
            )

            clear_calendar_cache()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    def _loop_state(self, run_id):
        cfg = _valid_cfg()
        started = _ET.localize(datetime(2026, 9, 8, 10, 0)).astimezone(timezone.utc)
        state = lr.new_observation_state(cfg, expected_sessions=[SESSION_A], now=started)
        state["run_id"] = run_id
        lr.save_active_state(state)
        return cfg, state, started

    def _run(self, state, cfg, deps):
        runtime = lr.build_runtime_config(cfg)
        with lr.runner_lock():
            return lr.run_observation_loop(state, cfg, runtime, deps)


# ---------------------------------------------------------------------------
# F-03: bounded scheduler retry
# ---------------------------------------------------------------------------


class SchedulerBoundedRetryTest(IsolatedTest):
    def test_t03_1_transient_failure_retries_then_continues(self):
        cfg, state, started = self._loop_state("run-f03-one-flap")
        end = started + lr.timedelta(days=30, hours=1)
        times = [
            _ET.localize(datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            end,
        ]
        calls = {"n": 0}

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        attempts = {"n": 0}

        def flaky_next_due(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("calendar flap")
            return None  # nothing left to schedule

        sleeps = []
        deps = _deps(calendar_rows=_rows((SESSION_A, "16:00")), now_fn=_now,
                     sleep_fn=sleeps.append)
        with patch.object(lr, "next_due_session", flaky_next_due):
            result = self._run(state, cfg, deps)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(sleeps.count(lr.SCHEDULER_RETRY_DELAY_SECONDS), 1)
        self.assertEqual(result["outcome"], "completed")
        self.assertFalse(lr.active_path().exists())

    def test_t03_2_two_failures_third_attempt_succeeds(self):
        cfg, state, started = self._loop_state("run-f03-two-flaps")
        end = started + lr.timedelta(days=30, hours=1)
        times = [
            _ET.localize(datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            end,
        ]
        calls = {"n": 0}

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        attempts = {"n": 0}

        def flaky_next_due(**kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("calendar flap")
            return None

        sleeps = []
        deps = _deps(calendar_rows=_rows((SESSION_A, "16:00")), now_fn=_now,
                     sleep_fn=sleeps.append)
        with patch.object(lr, "next_due_session", flaky_next_due):
            result = self._run(state, cfg, deps)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(sleeps.count(lr.SCHEDULER_RETRY_DELAY_SECONDS), 2)
        self.assertEqual(result["outcome"], "completed")

    def test_t03_3_three_failures_fail_closed_calendar_unavailable(self):
        cfg, state, _ = self._loop_state("run-f03-persistent")
        attempts = {"n": 0}

        def always_flaps(**kwargs):
            attempts["n"] += 1
            raise RuntimeError("calendar down")

        sleeps = []
        deps = _deps(now_fn=lambda: _ET.localize(
            datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            sleep_fn=sleeps.append)
        with patch.object(lr, "next_due_session", always_flaps):
            result = self._run(state, cfg, deps)
        self.assertEqual(attempts["n"], 3)  # exactly 3, never a 4th
        self.assertEqual(sleeps.count(lr.SCHEDULER_RETRY_DELAY_SECONDS), 2)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["final_status"], "STOPPED")
        self.assertEqual(payload["stop"]["code"], "CALENDAR_UNAVAILABLE")
        self.assertIn("RuntimeError: calendar down", payload["stop"]["detail"])
        self.assertIn("3 attempts", payload["stop"]["detail"])

    def test_t03_4_long_run_stop_is_never_retried(self):
        cfg, state, _ = self._loop_state("run-f03-hard-stop")
        attempts = {"n": 0}

        def hard_stop(**kwargs):
            attempts["n"] += 1
            raise lr.LongRunStop("STATE_CORRUPT", "journal unreadable")

        sleeps = []
        deps = _deps(now_fn=lambda: _ET.localize(
            datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            sleep_fn=sleeps.append)
        with patch.object(lr, "next_due_session", hard_stop):
            result = self._run(state, cfg, deps)
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(sleeps.count(lr.SCHEDULER_RETRY_DELAY_SECONDS), 0)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["stop"]["code"], "STATE_CORRUPT")

    def test_t03_5_mark_session_missed_after_close_retries_transient(self):
        cfg, state, started = self._loop_state("run-f03-mark")
        end = started + lr.timedelta(days=30, hours=1)
        times = [
            # After the regular close: the session is settleable as MISSED.
            _ET.localize(datetime(2026, 9, 8, 20, 0)).astimezone(timezone.utc),
            end,
        ]
        calls = {"n": 0}

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        mark_calls = {"n": 0}
        real_mark = lr.mark_session_missed_after_close

        def flaky_mark(**kwargs):
            mark_calls["n"] += 1
            if mark_calls["n"] == 1:
                raise RuntimeError("close proof flap")
            return real_mark(**kwargs)

        sleeps = []
        deps = _deps(calendar_rows=_rows((SESSION_A, "16:00")), now_fn=_now,
                     sleep_fn=sleeps.append)
        with patch.object(lr, "mark_session_missed_after_close", flaky_mark):
            result = self._run(state, cfg, deps)
        self.assertEqual(mark_calls["n"], 2)
        self.assertEqual(sleeps.count(lr.SCHEDULER_RETRY_DELAY_SECONDS), 1)
        self.assertEqual(result["outcome"], "completed")
        journal = lr.load_round_journal(state["run_id"], SESSION_A)
        self.assertEqual(journal["status"], "MISSED")
        self.assertEqual(journal["stop_reason"], "MISSED_SESSION_CLOSE")


# ---------------------------------------------------------------------------
# F-04: ordinary round exception fence
# ---------------------------------------------------------------------------


class RoundExceptionFenceTest(IsolatedTest):
    def _boom_deps(self, exc):
        def _screening(config, refresh=False):
            raise exc

        return _deps(
            screening_fn=_screening,
            calendar_rows=_rows((SESSION_A, "16:00")),
            now_fn=lambda: _ET.localize(
                datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
        )

    def test_t04_1_ordinary_round_exception_finalizes_stopped(self):
        cfg, state, _ = self._loop_state("run-f04-boom")
        deps = self._boom_deps(RuntimeError("boom"))
        result = self._run(state, cfg, deps)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["final_status"], "STOPPED")
        self.assertEqual(payload["stop"]["code"], "UNEXPECTED_ROUND_ERROR")
        self.assertIn("RuntimeError", payload["stop"]["detail"])
        self.assertIn("boom", payload["stop"]["detail"])
        self.assertFalse(lr.active_path().exists())

    def test_t04_2_existing_round_journal_keeps_stop_evidence(self):
        cfg, state, _ = self._loop_state("run-f04-journal")
        journal = lr.new_round_journal(SESSION_A, [])
        lr.save_round_journal(state["run_id"], journal)
        deps = self._boom_deps(RuntimeError("boom"))
        result = self._run(state, cfg, deps)
        self.assertEqual(result["outcome"], "stopped")
        stored = lr.load_round_journal(state["run_id"], SESSION_A)
        self.assertEqual(stored["status"], "STOPPED")
        self.assertIn("UNEXPECTED_ROUND_ERROR", stored["stop_reason"])
        self.assertIn("RuntimeError", stored["stop_reason"])
        self.assertIn("boom", stored["stop_reason"])
        self.assertTrue(stored["finished_at"])

    def test_t04_3_keyboard_interrupt_is_never_swallowed(self):
        cfg, state, _ = self._loop_state("run-f04-kbd")
        deps = self._boom_deps(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self._run(state, cfg, deps)
        # Not finalized: the window is still resumable, no STOPPED evidence.
        self.assertIsNotNone(lr.load_active_state())

    def test_t04_4_long_run_stop_semantics_unchanged(self):
        cfg, state, _ = self._loop_state("run-f04-hard-stop")

        def paused_round(**kwargs):
            raise lr.LongRunStop("ACCOUNT_PAUSED", "paper account paused")

        deps = _deps(calendar_rows=_rows((SESSION_A, "16:00")),
                     now_fn=lambda: _ET.localize(
                         datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc))
        with patch.object(lr, "run_daily_round", paused_round):
            result = self._run(state, cfg, deps)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["stop"]["code"], "ACCOUNT_PAUSED")
        journal = lr.load_round_journal(state["run_id"], SESSION_A)
        self.assertEqual(journal["status"], "STOPPED")
        self.assertTrue(journal["stop_reason"].startswith("ACCOUNT_PAUSED:"))


# ---------------------------------------------------------------------------
# F-01: frozen baseline + explicit manual rebase
# ---------------------------------------------------------------------------


class RebaseBroker:
    def __init__(self, positions=None, orders=None):
        self.account = SimpleNamespace(
            id=ACCOUNT_ID, equity="100000", last_equity="100000",
            cash="80000", buying_power="160000",
        )
        self.positions = list(positions or [])
        self.orders = list(orders or [])

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, request=None):
        return list(self.orders)


def _position(qty=10, market_value=1000.0):
    return SimpleNamespace(symbol="AAPL", qty=str(qty),
                           market_value=str(market_value))


def _broker_order(client_order_id, status, *, filled_qty="0",
                  filled_avg_price=None):
    return SimpleNamespace(
        id=f"broker-{client_order_id}", client_order_id=client_order_id,
        symbol="AAPL", side="buy", status=status, qty="2",
        filled_qty=filled_qty, filled_avg_price=filled_avg_price,
        updated_at=datetime.now(timezone.utc),
    )


class BaselineFreezeAndRebaseTest(unittest.TestCase):
    def _store(self, tmp):
        return ExecutionStore(str(Path(tmp) / "execution.db"))

    def _mismatch_setup(self, tmp):
        """Baseline AAPL=10 proven by a CLEAN reconcile, then broker AAPL=20
        producing exactly one PAUSED anomaly: position mismatch."""
        store = self._store(tmp)
        reconciler = Reconciler(store)
        broker = RebaseBroker(positions=[_position(10, 1000)])
        initial = reconciler.reconcile(capture_broker_snapshot(broker))
        self.assertTrue(initial.clean)
        broker.positions = [_position(20, 2000)]
        snapshot = capture_broker_snapshot(broker)
        paused = reconciler.reconcile(snapshot)
        self.assertEqual(paused.state, "PAUSED")
        self.assertEqual(set(paused.reasons), {"broker/local position mismatch"})
        return store, reconciler, snapshot

    def test_t01_1_normal_save_account_state_keeps_freezing_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.save_account_state(
                account_id=ACCOUNT_ID, state="CLEAN", reasons=(),
                snapshot_version="v1", baseline_positions={"AAPL": 10.0})
            store.save_account_state(
                account_id=ACCOUNT_ID, state="CLEAN", reasons=(),
                snapshot_version="v2", baseline_positions={"AAPL": 20.0})
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_2_explicit_rebase_with_only_position_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            verified = reconciler.rebase_baseline(
                snapshot, reason="verified 2:1 stock split")
            self.assertTrue(verified.clean)
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 20.0})
            self.assertEqual(state["baseline_at"],
                             snapshot.observed_at.isoformat())

    def test_t01_3_post_rebase_reconcile_of_same_snapshot_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            reconciler.rebase_baseline(snapshot, reason="verified 2:1 stock split")
            again = reconciler.reconcile(snapshot)
            self.assertTrue(again.clean)
            self.assertEqual(again.state, "CLEAN")

    def test_t01_4_partial_order_refuses_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            _, orders, _ = store.create_outbox(
                decision_id="dec-partial", run_id=None, symbol="AAPL",
                action="BUY", target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-partial", "symbol": "AAPL",
                         "side": "buy", "quantity": 2, "notional": None}])
            store.transition_order(orders[0]["order_id"], "SUBMITTING")
            broker = RebaseBroker(
                positions=[_position(20, 2000)],
                orders=[_broker_order("ta-partial", "partially_filled",
                                      filled_qty="1", filled_avg_price="100")],
            )
            fresh = capture_broker_snapshot(broker)
            paused = reconciler.reconcile(fresh)
            self.assertEqual(paused.state, "PAUSED")
            with self.assertRaises(BrokerAuthorityError):
                reconciler.rebase_baseline(
                    fresh, reason="verified 2:1 stock split")
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_5a_local_nonterminal_accepted_order_refuses_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            _, orders, _ = store.create_outbox(
                decision_id="dec-live", run_id=None, symbol="AAPL",
                action="BUY", target_position="LONG", payload_json="{}",
                orders=[{"client_order_id": "ta-live", "symbol": "AAPL",
                         "side": "buy", "quantity": 1, "notional": None}])
            store.transition_order(orders[0]["order_id"], "SUBMITTING")
            broker = RebaseBroker(
                positions=[_position(20, 2000)],
                orders=[_broker_order("ta-live", "accepted")],
            )
            fresh = capture_broker_snapshot(broker)
            paused = reconciler.reconcile(fresh)
            self.assertEqual(set(paused.reasons),
                             {"broker/local position mismatch"})
            with self.assertRaises(BrokerAuthorityError):
                reconciler.rebase_baseline(
                    fresh, reason="verified 2:1 stock split")
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_5b_live_manual_broker_order_refuses_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            # A manual (non-ta-) live broker order: invisible to reconcile
            # reasons, but it could still move the position after a rebase.
            broker = RebaseBroker(
                positions=[_position(20, 2000)],
                orders=[_broker_order("manual-xyz", "accepted")],
            )
            fresh = capture_broker_snapshot(broker)
            paused = reconciler.reconcile(fresh)
            self.assertEqual(set(paused.reasons),
                             {"broker/local position mismatch"})
            with self.assertRaises(BrokerAuthorityError):
                reconciler.rebase_baseline(
                    fresh, reason="verified 2:1 stock split")
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_6_stale_snapshot_refuses_rebase(self):
        from tradingagents.execution import authority as exec_authority

        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            stale = replace(
                snapshot,
                observed_at=snapshot.observed_at - timedelta(
                    seconds=float(exec_authority.SNAPSHOT_TTL_SECONDS) * 10),
            )
            with self.assertRaises(BrokerAuthorityError):
                reconciler.rebase_baseline(
                    stale, reason="verified 2:1 stock split")
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_7_blank_reason_refuses_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, reconciler, snapshot = self._mismatch_setup(tmp)
            for reason in ("", "   "):
                with self.subTest(reason=reason):
                    with self.assertRaises(BrokerAuthorityError):
                        reconciler.rebase_baseline(snapshot, reason=reason)
            state = store.get_account_state(ACCOUNT_ID)
            self.assertEqual(json.loads(state["baseline_positions_json"]),
                             {"AAPL": 10.0})

    def test_t01_8_missing_account_state_refuses_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            reconciler = Reconciler(store)
            snapshot = capture_broker_snapshot(RebaseBroker())
            with self.assertRaises(BrokerAuthorityError):
                reconciler.rebase_baseline(
                    snapshot, reason="verified 2:1 stock split")
            # A rebase must never become a shortcut to the first baseline.
            self.assertIsNone(store.get_account_state(ACCOUNT_ID))


if __name__ == "__main__":
    unittest.main()
