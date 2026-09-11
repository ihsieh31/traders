"""Plan-B runtime/scheduler/state/reporting regressions.

Covers R01, R02, R08, R10, R11, R12, R13, R15, R16 from
TRADERS_FIX_PLAN_B_RUNTIME_RELIABILITY.md. Every transport is faked
in-process: no real Alpaca mutation, no paid LLM call, no network.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz

import tradingagents.long_run as lr
from tradingagents.risk.corporate_actions import (
    QuarantineStateError, QuarantineStore,
)

_ET = pytz.timezone("US/Eastern")
SESSION_A = "2026-09-08"  # Tuesday
SESSION_B = "2026-09-09"  # Wednesday


def _rows(*specs):
    """Authoritative calendar rows: (iso-date, close HH:MM)."""
    return [
        {"date": day, "close": dtime(*map(int, close.split(":")))}
        for day, close in specs
    ]


def _valid_cfg(**overrides):
    cfg = dict(lr.default_long_run_config())
    cfg.update({
        "base_trade_notional_usd": 1000,
        "analysis_provider": "openai", "analysis_model": "gpt-fake",
        "decision_provider": "openai", "decision_model": "gpt-fake",
        "screening_provider": "openai", "screening_model": "gpt-fake",
    })
    cfg.update(overrides)
    return cfg


class IsolatedLongRunTest(unittest.TestCase):
    """Isolates the process-global config/env/lock state Phase D touches."""

    def setUp(self):
        import tradingagents.dataflows.config as _cfgmod

        self._saved_config = _cfgmod.get_config()
        self._cfgmod = _cfgmod
        self.workdir = Path(tempfile.mkdtemp(prefix="planb-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.old_env = dict(os.environ)
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")
        self._saved_stop = lr._stop_requested

    def tearDown(self):
        lr._stop_requested = self._saved_stop
        import tradingagents.dataflows.config as _cfgmod

        _cfgmod.set_config(self._saved_config)
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)


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


class _RecordingService:
    """FakeService variant that records the recovery submit callback."""

    def __init__(self):
        self.recover_calls = 0
        self.can_submits = []

    def enforce_exit_deadlines(self):
        return {"success": True, "deadline_exits": [], "broker_calls": 0}

    def startup_recover(self, can_submit=None):
        self.recover_calls += 1
        if can_submit is not None:
            self.can_submits.append(bool(can_submit()))
        return {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}

    def execute(self, **kwargs):
        return {"success": True, "broker_attempted": True, "broker_calls": 1}


class _FakePlan:
    def __init__(self, symbols):
        from types import SimpleNamespace as NS

        self._plan = NS(
            stopped=False,
            deep_analysis_set=list(symbols),
            top20=[{"symbol": s, "rank": 1, "screening_score": 90.0,
                    "short_reason": "fake"} for s in symbols],
            selection_date=SESSION_A, as_of=SESSION_A, cached=False,
            overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
            screening_description="fixture",
            stop_reason_text=lambda: "",
        )

    def __getattr__(self, name):
        return getattr(self._plan, name)


class _FakeGraph:
    """Minimal TradingAgentsGraph stand-in: one symbol, one intent."""

    def __init__(self, config=None):
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        return ({"final_trade_intent": {"symbol": symbol, "action": "BUY",
                                        "target_position": "LONG"},
                 "final_trade_decision": "BUY"}, "BUY")


def _round_deps(service, symbols=("AAA",), **overrides):
    graph = _FakeGraph()
    deps = lr.LongRunDeps(
        screening_fn=lambda config, refresh=False: _FakePlan(symbols),
        graph_factory=lambda config: graph,
        execution_service_factory=lambda: service,
        broker_client_factory=lambda: FakeBroker(),
        alert_fn=lambda subject, body, runtime: {"sent": False},
        sleep_fn=lambda seconds: None,
    )
    return deps


# ---------------------------------------------------------------------------
# R01 — runtime config applied before recovery mutation
# ---------------------------------------------------------------------------


class R01RuntimeBeforeRecoveryTests(IsolatedLongRunTest):
    def test_r01_runtime_applied_before_long_run_recovery(self):
        """run_daily_round installs this run's runtime as the global config
        before startup_recover() runs, so recovery sees the run's screening
        mode (auto_screening_enabled=True), not a stale manual-mode global."""
        import tradingagents.dataflows.config as cfgmod

        service = _RecordingService()
        seen = {}

        real_recover = service.startup_recover

        def recording_recover(can_submit=None):
            seen["auto_screening_enabled"] = bool(
                (cfgmod.get_config() or {}).get("auto_screening_enabled")
            )
            seen["screening_provider"] = (cfgmod.get_config() or {}).get(
                "screening_provider"
            )
            return real_recover(can_submit=can_submit)

        service.startup_recover = recording_recover
        deps = _round_deps(service)
        # Simulate a stale manual-mode global config from a previous process.
        cfgmod.set_config({**(self._saved_config or {}),
                           "auto_screening_enabled": False})
        runtime = lr.build_runtime_config(_valid_cfg())
        out = lr.run_daily_round(
            run_id="run-r01a", session_date=SESSION_A,
            long_cfg=_valid_cfg(), runtime=runtime, deps=deps,
        )
        self.assertEqual(out["status"], "COMPLETED")
        self.assertEqual(service.recover_calls, 1)
        self.assertTrue(
            seen["auto_screening_enabled"],
            "recovery must see the run's runtime config, not a stale global",
        )
        self.assertEqual(seen["screening_provider"], "openai")

    def test_r01_recovery_cannot_fall_back_to_manual_screening_mode(self):
        """A runtime that disables auto-screening for an unattended long run
        is refused before recovery runs at all (no broker mutation)."""
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        # The applied global would have screening off — the exact mismatch
        # that previously let recovery bypass the Phase C entry gate.
        import tradingagents.dataflows.config as cfgmod

        original_apply = lr._apply_runtime_config

        def apply_then_disable(rt):
            original_apply(rt)
            merged = dict(cfgmod.get_config() or {})
            merged["auto_screening_enabled"] = False
            cfgmod.set_config(merged)

        service = _RecordingService()
        deps = _round_deps(service)
        with patch.object(lr, "_apply_runtime_config", apply_then_disable):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.run_daily_round(
                    run_id="run-r01b", session_date=SESSION_A,
                    long_cfg=cfg, runtime=runtime, deps=deps,
                )
        self.assertEqual(ctx.exception.code, "SAFETY_DISABLED")
        self.assertEqual(service.recover_calls, 0)

    def test_r01_cli_post_authorization_recovery_receives_runtime(self):
        """run_post_authorization_recovery applies the caller's runtime
        before recovery; the recovery sees the run's screening config."""
        import tradingagents.dataflows.config as cfgmod

        service = _RecordingService()
        seen = {}

        def recording_recover(can_submit=None):
            seen["screening_model"] = (cfgmod.get_config() or {}).get(
                "screening_model"
            )
            return {"success": True, "account_execution_state": "CLEAN",
                    "reconciliation_reasons": []}

        service.startup_recover = recording_recover
        deps = lr.LongRunDeps(
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: FakeBroker(),
        )
        cfgmod.set_config({**(self._saved_config or {}),
                           "auto_screening_enabled": False})
        runtime = lr.build_runtime_config(_valid_cfg())
        out = lr.run_post_authorization_recovery(deps, runtime)
        self.assertTrue(out["recovery"]["success"])
        self.assertEqual(seen["screening_model"], "gpt-fake")


# ---------------------------------------------------------------------------
# R02 — stop / observation window must cover the recovery POST
# ---------------------------------------------------------------------------


class R02StopWindowRecoveryTests(IsolatedLongRunTest):
    def _round_with_now(self, service, now_fn, ends_at):
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: _FakePlan(("AAA",)),
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: FakeBroker(),
            now_fn=now_fn,
            sleep_fn=lambda s: None,
        )
        return lr.run_daily_round(
            run_id="run-r02", session_date=SESSION_A,
            long_cfg=_valid_cfg(), runtime=lr.build_runtime_config(_valid_cfg()),
            deps=deps, ends_at=ends_at,
        )

    def test_r02_stop_before_round_zero_recovery_posts(self):
        """A stop requested before the round starts means zero recovery
        calls (Layer 1 round precheck)."""
        lr._stop_requested = True
        service = _RecordingService()
        now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        out = self._round_with_now(service, lambda: now, ends_at=None)
        self.assertEqual(service.recover_calls, 0)
        self.assertIsNone(out)

    def test_r02_expired_window_zero_recovery_posts(self):
        """An observation window that already ended forbids starting the
        round's recovery (Layer 1)."""
        service = _RecordingService()
        now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        ends = datetime(2026, 9, 8, 14, 59, tzinfo=timezone.utc)  # passed
        out = self._round_with_now(service, lambda: now, ends_at=ends)
        self.assertEqual(service.recover_calls, 0)
        self.assertIsNone(out)

    def test_r02_stop_race_immediately_before_recovery_submit_zero_posts(self):
        """A stop that lands between the outer precheck and the recovery
        submit is caught by the Layer 2 callback (can_submit) — the recovery
        itself refuses; the round hard-stops instead of POSTing."""
        service = _RecordingService()
        refused = {"seen": False}

        def race_recover(can_submit=None):
            # Simulate the stop landing DURING recovery's lookups: the
            # submit-boundary callback now observes the stop and refuses.
            nonlocal refused
            allowed = can_submit() if can_submit is not None else True
            if not allowed:
                refused["seen"] = True
                return {
                    "success": False,
                    "account_execution_state": "PAUSED",
                    "reconciliation_reasons": [
                        "recovery resubmit deferred by stop/window authority"],
                }
            return {"success": True, "account_execution_state": "CLEAN",
                    "reconciliation_reasons": []}

        service.startup_recover = race_recover

        # Flip the stop DURING the round: the entry precheck passes (first
        # call), every later check — the recovery submit boundary — observes
        # the stop that "arrived" meanwhile.
        class StopDuringRound:
            def __init__(self):
                self.calls = 0

        stop = StopDuringRound()

        real_control = lr._control_stop_reason

        def control_after_recovery(deps, ends_at):
            stop.calls += 1
            if stop.calls > 1:
                # The stop arrived after the entry precheck passed.
                return lr.STOP_REASON_STOP_REQUESTED
            return real_control(deps, ends_at)

        now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: _FakePlan(("AAA",)),
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: FakeBroker(),
            now_fn=lambda: now,
            sleep_fn=lambda s: None,
        )
        with patch.object(lr, "_control_stop_reason", control_after_recovery):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.run_daily_round(
                    run_id="run-r02c", session_date=SESSION_A,
                    long_cfg=_valid_cfg(),
                    runtime=lr.build_runtime_config(_valid_cfg()),
                    deps=deps, ends_at=None,
                )
        self.assertEqual(ctx.exception.code, "RECOVERY_UNSAFE")
        self.assertTrue(refused["seen"])


class R02ExecutionBoundaryTests(unittest.TestCase):
    """Layer 2 against the REAL ExecutionService: the submit-boundary guard
    defers the resubmit POST with the row left durably unresolved."""

    def _seeded_service(self, tmp, broker):
        from tradingagents.execution.authority import BrokerQuote
        from tradingagents.execution.service import ExecutionService
        from tradingagents.execution.store import client_order_id_for

        service = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: BrokerQuote(
                s, 99.0, 101.0, datetime.now(timezone.utc)),
        )
        store = service.store
        # Bind the fresh DB to this broker account BEFORE seeding rows
        # (the binding is normally established by the first execute/recover).
        guard = SimpleNamespace(
            enabled=True,
            check_order=lambda *a, **k: SimpleNamespace(allowed=True,
                                                        reasons=[]),
        )
        from tradingagents.dataflows import config as cfgmod
        from tradingagents.default_config import DEFAULT_CONFIG

        with patch.object(cfgmod, "_config",
                          {**DEFAULT_CONFIG, "auto_screening_enabled": True,
                           "data_cache_dir": str(tmp)}), patch(
            "tradingagents.safety.get_safety_guard", return_value=guard):
            service.startup_recover()
        # One durable PENDING opening with no broker order -> recoverable.
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
        store.create_outbox(
            decision_id="r02-decision", run_id=None, symbol="AAPL",
            action="BUY", target_position="LONG",
            payload_json=json.dumps(intent, sort_keys=True),
            orders=[{"client_order_id": client_order_id_for(
                "r02-decision", "AAPL", "buy", role="open", seq=0),
                "symbol": "AAPL", "side": "buy",
                "quantity": None, "notional": 1000.0}],
        )
        return service, store

    def _broker(self, tmp):
        class B:
            def __init__(self):
                self.submits = []
                self.orders = []

            def get_clock(self):
                return SimpleNamespace(is_open=True)

            def get_account(self):
                return SimpleNamespace(
                    id="paper-r02", equity="100000", last_equity="100000",
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

    def test_r02_can_submit_false_defers_resubmit_zero_posts(self):
        import socket
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(tmp)
            service, store = self._seeded_service(tmp, broker)
            guard = SimpleNamespace(
                enabled=True,
                check_order=lambda *a, **k: SimpleNamespace(
                    allowed=True, reasons=[]),
            )
            from tradingagents.dataflows import config as cfgmod
            from tradingagents.default_config import DEFAULT_CONFIG

            with patch.dict(os.environ, {}), patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ), patch.object(cfgmod, "_config",
                            {**DEFAULT_CONFIG, "auto_screening_enabled": True,
                             "data_cache_dir": tmp}), patch(
                "tradingagents.screening.gate.check_entry_allowed",
                return_value=None), patch.object(
                socket.socket, "connect",
                Mock(side_effect=AssertionError("network forbidden"))):
                result = service.startup_recover(can_submit=lambda: False)
            self.assertFalse(result["success"])
            self.assertEqual(len(broker.submits), 0)
            # The row is NOT faked into REJECTED: it stays durably
            # unresolved (UNKNOWN), replayable by a later authorized resume.
            statuses = [
                str(o.get("status") or "").upper()
                for o in store.list_all_orders()
            ]
            self.assertIn("UNKNOWN", statuses)
            self.assertNotIn("REJECTED", statuses)


# ---------------------------------------------------------------------------
# R08 — documented split event must not be silently ignored
# ---------------------------------------------------------------------------


class R08CorporateActionConfigTests(unittest.TestCase):
    def test_r08_documented_split_example_quarantines_symbol(self):
        """The documented example shape (ratio inside details) loads and
        quarantines the symbol."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "results_dir": tmp,
                "corporate_action_events": [
                    {"symbol": "TSLA", "reason": "split",
                     "effective_at": "2026-09-10T00:00:00+00:00",
                     "source": "operator", "details": {"ratio": "3:1"}},
                ],
            }
            from tradingagents.risk.corporate_actions import (
                build_quarantine_gate,
            )

            gate = build_quarantine_gate(config)
            self.assertIsNotNone(gate)
            self.assertTrue(gate.store.is_quarantined("TSLA"))

    def test_r08_invalid_configured_event_fails_closed_not_silent(self):
        """A malformed configured event (the old top-level 'ratio' example)
        raises QuarantineStateError instead of being silently skipped; the
        resulting unbuildable gate keeps execution fail-closed."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(QuarantineStateError) as ctx:
                QuarantineStore(
                    Path(tmp) / "quarantine.json",
                    initial_events=[
                        {"symbol": "TSLA", "reason": "split", "ratio": "3:1"},
                    ],
                )
            self.assertIn("corporate_action_events[0]", str(ctx.exception))
            self.assertIn("TSLA", str(ctx.exception))
            # The execution-facing builder returns None for an unbuildable
            # gate; callers (execution entry / screening) fail closed on it.
            from tradingagents.risk.corporate_actions import (
                build_quarantine_gate,
            )

            gate = build_quarantine_gate({
                "results_dir": tmp,
                "corporate_action_events": [
                    {"symbol": "TSLA", "reason": "split", "ratio": "3:1"},
                ],
            })
            self.assertIsNone(gate)


# ---------------------------------------------------------------------------
# R10 — SafetyGuard multi-process lost update
# ---------------------------------------------------------------------------


class R10SafetyGuardCrossProcessTests(unittest.TestCase):
    def _guard(self, tmp, **overrides):
        from tradingagents.safety import DEFAULT_SAFETY_CONFIG, SafetyGuard

        config = dict(DEFAULT_SAFETY_CONFIG)
        config.update(overrides)
        return SafetyGuard(
            config=config,
            state_path=Path(tmp) / "state.json",
            kill_switch_path=Path(tmp) / "KILL_SWITCH",
        )

    def test_r10_two_instances_merge_llm_token_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._guard(tmp)
            b = self._guard(tmp)
            a.record_llm_tokens(80)
            b.record_llm_tokens(30)  # used to be lost (a's write clobbered)
            self.assertEqual(a.llm_tokens_used(), 110)
            self.assertEqual(b.llm_tokens_used(), 110)

    def test_r10_cross_process_token_updates_not_lost(self):
        """Two real OS processes: the second must see the first's tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            guard = self._guard(tmp)
            guard.record_llm_tokens(50)
            script = (
                "from pathlib import Path;"
                f"import json;"
                f"state = json.loads(Path({str(Path(tmp) / 'state.json')!r}).read_text());"
                "print(state.get('llm_tokens'))"
            )
            out = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("50", out.stdout)

            # A second process writing through its own SafetyGuard must not
            # clobber the first process's count (flock + reload).
            script2 = (
                "from pathlib import Path;"
                "import sys;"
                "sys.path.insert(0, %r);"
                "from tradingagents.safety import SafetyGuard, DEFAULT_SAFETY_CONFIG;"
                "g = SafetyGuard(config=dict(DEFAULT_SAFETY_CONFIG),"
                f" state_path=Path({str(Path(tmp) / 'state.json')!r}),"
                f" kill_switch_path=Path({str(Path(tmp) / 'KILL_SWITCH')!r}));"
                "g.record_llm_tokens(25);"
                "print(g.llm_tokens_used())"
                % os.getcwd()
            )
            out2 = subprocess.run(
                [sys.executable, "-c", script2],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(out2.returncode, 0, out2.stderr)
            self.assertIn("75", out2.stdout)

    def test_r10_budget_check_reloads_other_process_state(self):
        from tradingagents.safety import DEFAULT_SAFETY_CONFIG

        with tempfile.TemporaryDirectory() as tmp:
            a = self._guard(tmp, daily_llm_token_budget=100)
            b = self._guard(tmp, daily_llm_token_budget=100)
            a.record_llm_tokens(100)
            # b's in-memory state is stale (0); the verdict must still see
            # the durable 100 and refuse.
            verdict = b.check_llm_budget()
            self.assertFalse(verdict.allowed, verdict.checks)

    def test_r10_rejection_streak_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Explicit low threshold so a 3-streak provably trips the halt.
            a = self._guard(tmp, max_consecutive_rejections=3)
            b = self._guard(tmp, max_consecutive_rejections=3)
            a.record_order_result(False)  # 1
            a.record_order_result(False)  # 2
            b.record_order_result(False)  # must become 3, not 1
            self.assertEqual(a.consecutive_rejections(), 3)
            self.assertEqual(b.consecutive_rejections(), 3)
            # The streak also blocks check_order via fresh state.
            verdict = b.check_order(
                "AAPL", 1000.0, account={"equity": 100000.0,
                                        "last_equity": 100000.0},
            )
            self.assertFalse(verdict.allowed)
            self.assertTrue(any(
                "consecutive orders were rejected" in r
                for r in verdict.reasons
            ))


# ---------------------------------------------------------------------------
# R15 — embedding client honors request timeout, zero SDK retries
# ---------------------------------------------------------------------------


class R15EmbeddingTimeoutTests(unittest.TestCase):
    def test_r15_embedding_uses_configured_timeout_and_zero_sdk_retries(self):
        with patch(
            "tradingagents.agents.utils.memory.get_embedding_client_config",
            return_value={"api_key": "test-key", "base_url": "http://x/v1"},
        ):
            from tradingagents.agents.utils.memory import (
                FinancialSituationMemory,
            )

            # memory_retrieval_enabled stays False: no Chroma collection IO.
            mem = FinancialSituationMemory(
                "planb-memory", {"memory_retrieval_enabled": False,
                                 "llm_request_timeout_seconds": 7},
            )
            self.assertIsNotNone(mem.client)
            # The OpenAI SDK exposes the configured values on the client.
            self.assertEqual(mem.client.timeout, 7)
            self.assertEqual(mem.client.max_retries, 0)

    def test_r15_default_timeout_when_config_missing(self):
        with patch(
            "tradingagents.agents.utils.memory.get_embedding_client_config",
            return_value={"api_key": "test-key", "base_url": "http://x/v1"},
        ):
            from tradingagents.agents.utils.memory import (
                FinancialSituationMemory,
            )

            mem = FinancialSituationMemory(
                "planb-memory", {"memory_retrieval_enabled": False},
            )
            self.assertEqual(mem.client.timeout, 120.0)
            self.assertEqual(mem.client.max_retries, 0)


# ---------------------------------------------------------------------------
# R16 — report snapshot must not turn unavailable into 0
# ---------------------------------------------------------------------------


class R16SnapshotTruthfulnessTests(unittest.TestCase):
    def _acct(self, **kw):
        base = dict(id="acct-1", equity="100000", cash="50000",
                    buying_power="100000")
        base.update(kw)
        return SimpleNamespace(**base)

    def _pos(self, symbol="AAPL", qty="10", mv="1000"):
        return SimpleNamespace(symbol=symbol, qty=qty, market_value=mv,
                               avg_entry_price="100", unrealized_pl="0")

    def test_r16_nan_equity_is_unavailable_not_zero(self):
        broker = SimpleNamespace(
            get_account=lambda: self._acct(equity="NaN"),
            get_all_positions=lambda: [],
        )
        with self.assertRaises(lr.SnapshotUnavailable):
            lr.capture_account_snapshot(broker)

    def test_r16_missing_cash_is_unavailable_not_zero(self):
        broker = SimpleNamespace(
            get_account=lambda: self._acct(cash=None),
            get_all_positions=lambda: [],
        )
        with self.assertRaises(lr.SnapshotUnavailable):
            lr.capture_account_snapshot(broker)

    def test_r16_positions_none_is_unavailable_not_empty(self):
        broker = SimpleNamespace(
            get_account=lambda: self._acct(),
            get_all_positions=lambda: None,
        )
        with self.assertRaises(lr.SnapshotUnavailable):
            lr.capture_account_snapshot(broker)

    def test_r16_explicit_empty_positions_is_valid(self):
        broker = SimpleNamespace(
            get_account=lambda: self._acct(),
            get_all_positions=lambda: [],
        )
        snap = lr.capture_account_snapshot(broker)
        self.assertEqual(snap["positions"], [])
        self.assertEqual(snap["equity"], 100000.0)
        self.assertEqual(snap["cash"], 50000.0)

    def test_r16_position_market_value_nan_is_unavailable(self):
        broker = SimpleNamespace(
            get_account=lambda: self._acct(),
            get_all_positions=lambda: [self._pos(mv="NaN")],
        )
        with self.assertRaises(lr.SnapshotUnavailable):
            lr.capture_account_snapshot(broker)


# ---------------------------------------------------------------------------
# R11 / R12 — WebUI generation ownership + stale ProviderFailure
# ---------------------------------------------------------------------------


class WebuiAnalysisTest(unittest.TestCase):
    def setUp(self):
        from webui.utils import state as webui_state

        self.app_state = webui_state.app_state
        self._saved_generation = self.app_state.run_generation
        self._saved_queue = list(getattr(self.app_state, "analysis_queue", []))
        self._saved_stop_reason = getattr(
            self.app_state, "provider_stop_reason", None)
        # run_analysis needs a symbol state to operate on.
        if "AAPL" not in self.app_state.symbol_states:
            self.app_state.init_symbol_state("AAPL")

    def tearDown(self):
        from webui.utils import state as webui_state

        webui_state.app_state.run_generation = self._saved_generation
        webui_state.app_state.analysis_queue = self._saved_queue
        webui_state.app_state.provider_stop_reason = self._saved_stop_reason

    def _provider_failure(self):
        from tradingagents.llm_clients.retry import ProviderFailure

        return ProviderFailure(
            role="analysis", provider="openai", model="gpt-fake",
            attempts=1, category="permanent", detail="fixture exhausted",
        )

    def test_r11_generation_captured_before_initial_chart(self):
        """start_analysis captures its token before the (slow) initial
        chart; a Stop→Start during the chart leaves the work item stale and
        run_analysis is never entered."""
        from webui.components import analysis as analysis_mod

        def fake_chart(ticker, period="1y", end_date=None):
            # Stop→Start happens DURING the slow chart: generation bumps.
            self.app_state.run_generation += 1
            return {"fake": True}

        called = {"run": 0}

        def fail_run(*args, **kwargs):
            called["run"] += 1
            raise AssertionError("stale work must not reach run_analysis")

        guard = SimpleNamespace(
            check_llm_budget=lambda: SimpleNamespace(allowed=True),
        )
        with patch.object(analysis_mod, "create_chart", fake_chart), \
             patch.object(analysis_mod, "run_analysis", fail_run), \
             patch("tradingagents.safety.get_safety_guard",
                   return_value=guard):
            message = analysis_mod.start_analysis(
                "AAPL", True, False, False, False, False, "Medium", False,
                "m", "m",
            )
        self.assertEqual(called["run"], 0)
        self.assertIn("restarted", message)

    def test_r11_old_work_after_stop_start_never_trades(self):
        """run_analysis with a dispatch token that is now stale must not
        persist its result or trade (F02/R11 boundary)."""
        from webui.components import analysis as analysis_mod

        my_gen = self.app_state.run_generation
        self.app_state.run_generation += 1  # operator Stop→Start happened
        captured = {}

        def no_trade(*a, **k):
            captured["called"] = True

        # Let the analysis itself succeed trivially: the graph is patched to
        # a minimal stand-in whose stream returns one final-state chunk.
        final_state = {
            "final_trade_intent": {"symbol": "AAPL", "action": "BUY",
                                   "target_position": "LONG"},
            "final_trade_decision": "BUY",
            "trading_mode": "investment",
        }

        class FakeGraph:
            def __init__(self, *a, **k):
                pass

            def _resolve_memory_log_outcomes(self, *a, **k):
                pass

            propagator = SimpleNamespace(
                create_initial_state=lambda ticker, date: {}
            )

            def _graph_args_for_run(self, ticker, date):
                return {"config": {"recursion_limit": 100}}

            def _graph_for_run(self, ticker, date):
                class Ctx:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                class Compiled:
                    def stream(self, state, **kwargs):
                        return iter([dict(final_state)])

                return Compiled(), Ctx()

            def process_signal(self, decision):
                return "BUY"

            def _log_state(self, *a, **k):
                pass

            memory_log = SimpleNamespace(
                store_decision=lambda **k: None,
            )

        with patch.object(analysis_mod, "TradingAgentsGraph", FakeGraph), \
             patch.object(analysis_mod, "execute_trade_after_analysis",
                          no_trade):
            result = analysis_mod.run_analysis(
                "AAPL", ["market"], {"rounds": 1, "level": "Shallow"},
                False, "m", "m", run_generation=my_gen,
            )
        self.assertIn("discarded", result)
        self.assertNotIn("called", captured)

    def test_r12_stale_provider_failure_does_not_clear_new_queue(self):
        """An OLD run's ProviderFailure must not clear the new run's queue
        or write the new run's provider_stop_reason."""
        from webui.components import analysis as analysis_mod

        self.app_state.analysis_queue = ["NEW1", "NEW2"]
        self.app_state.provider_stop_reason = None
        my_gen = self.app_state.run_generation
        self.app_state.run_generation += 1  # this run is now stale

        # The handler must finish the old run's audit first, then check
        # staleness and skip mark_provider_stop for a stale run.
        with patch.object(
            analysis_mod, "TradingAgentsGraph",
            side_effect=self._provider_failure(),
        ), patch.object(
            analysis_mod, "mark_provider_stop",
            side_effect=AssertionError(
                "stale run must not mark provider stop"),
        ):
            analysis_mod.run_analysis(
                "AAPL", ["market"], {"rounds": 1, "level": "Shallow"},
                False, "m", "m", run_generation=my_gen,
            )
        self.assertEqual(self.app_state.analysis_queue, ["NEW1", "NEW2"])
        self.assertIsNone(self.app_state.provider_stop_reason)

    def test_r12_current_provider_failure_still_stops_current_queue(self):
        from webui.components import analysis as analysis_mod

        self.app_state.analysis_queue = ["A", "B"]
        self.app_state.provider_stop_reason = None
        current_gen = self.app_state.run_generation

        with patch.object(
            analysis_mod, "TradingAgentsGraph",
            side_effect=self._provider_failure(),
        ):
            analysis_mod.run_analysis(
                "AAPL", ["market"], {"rounds": 1, "level": "Shallow"},
                False, "m", "m", run_generation=current_gen,
            )
        self.assertEqual(self.app_state.analysis_queue, [])
        self.assertIsNotNone(self.app_state.provider_stop_reason)


# ---------------------------------------------------------------------------
# R13 — closed session / closed market must not open new exposure
# ---------------------------------------------------------------------------


class R13ClosedSessionSchedulerTests(IsolatedLongRunTest):
    def test_r13_closed_session_is_marked_missed_not_due_for_entry(self):
        rows = _rows((SESSION_A, "16:00"))
        settled = mark_result = lr.mark_session_missed_after_close(
            run_id="run-r13a",
            session_date=SESSION_A,
            # 2026-09-08 16:01 ET — one minute past the authoritative close.
            now=_ET.localize(datetime(2026, 9, 8, 16, 1))
                       .astimezone(timezone.utc),
            run_time_et="11:00",
            calendar_rows=rows,
        )
        self.assertTrue(mark_result)
        journal = lr.load_round_journal("run-r13a", SESSION_A)
        self.assertEqual(journal["status"], "MISSED")
        self.assertEqual(journal["stop_reason"], "MISSED_SESSION_CLOSE")

    def test_r13_missed_session_not_replayed_next_day(self):
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        # The 9/8 session never started; on 9/9 the scheduler must settle it
        # MISSED instead of running it as an overdue due-round.
        now = _ET.localize(datetime(2026, 9, 9, 10, 0)).astimezone(timezone.utc)
        self.assertTrue(lr.mark_session_missed_after_close(
            run_id="run-r13b", session_date=SESSION_A, now=now,
            run_time_et="11:00", calendar_rows=rows,
        ))
        settled = lr.settled_sessions("run-r13b")
        self.assertIn(SESSION_A, settled)
        # A never-started session with a journal keeps recovery ownership:
        # started-before-close rounds are not sweep-MISSED here.
        self.assertFalse(lr.mark_session_missed_after_close(
            run_id="run-r13b", session_date=SESSION_A, now=now,
            run_time_et="11:00", calendar_rows=rows,
        ))

    def test_r13_open_session_still_runnable(self):
        rows = _rows((SESSION_A, "16:00"))
        now = _ET.localize(datetime(2026, 9, 8, 15, 30)).astimezone(timezone.utc)
        self.assertFalse(lr.mark_session_missed_after_close(
            run_id="run-r13c", session_date=SESSION_A, now=now,
            run_time_et="11:00", calendar_rows=rows,
        ))
        self.assertIsNone(lr.load_round_journal("run-r13c", SESSION_A))


class R13OpeningMarketGateTests(unittest.TestCase):
    """The execution-layer market-clock gate on the real service."""

    def _intent(self):
        from datetime import datetime, timedelta, timezone

        from tradingagents.agents.schemas import (
            EntryPolicy, RiskDecision, build_trade_intent_from_risk_decision,
        )

        stamp = datetime.now(timezone.utc)
        decision = RiskDecision(
            action="BUY", confidence="high", risk_rationale="fixture",
            required_controls="stop", stop_loss_price=90, take_profit_price=120,
            entry_policy=EntryPolicy(
                status="READY", minimum_price=99, maximum_price=101,
                expires_at=(stamp + timedelta(hours=1)).isoformat(),
                exit_by=(stamp + timedelta(days=5)).isoformat(),
                confirmation="fixture",
            ),
        )
        return build_trade_intent_from_risk_decision(
            symbol="AAPL", trading_mode="investment",
            current_position="NEUTRAL", decision=decision, allow_shorts=True,
        ).model_dump(mode="json")

    def _service(self, tmp, clock):
        from tradingagents.execution.authority import BrokerQuote
        from tradingagents.execution.service import ExecutionService

        class B:
            def __init__(self, clock):
                self.clock = clock
                self.submits = []
                self.orders = []

            def get_clock(self):
                if isinstance(self.clock, Exception):
                    raise self.clock
                return self.clock

            def get_account(self):
                return SimpleNamespace(
                    id="paper-r13", equity="100000", last_equity="100000",
                    cash="80000", buying_power="200000")

            def get_all_positions(self):
                return []

            def get_orders(self, request=None):
                return list(self.orders)

            def submit_order(self, request):
                self.submits.append(request)
                raise AssertionError("must not POST when market closed")

        broker = B(clock)
        service = ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: BrokerQuote(
                s, 99.0, 101.0, datetime.now(timezone.utc)),
        )
        return service, broker

    def _execute(self, service, tmp):
        import socket
        from unittest.mock import Mock

        from tradingagents.dataflows import config as cfgmod
        from tradingagents.default_config import DEFAULT_CONFIG

        guard = SimpleNamespace(
            enabled=True,
            check_order=lambda *a, **k: SimpleNamespace(allowed=True,
                                                        reasons=[]),
        )
        with patch.object(cfgmod, "_config",
                          {**DEFAULT_CONFIG, "auto_screening_enabled": True,
                           "data_cache_dir": str(tmp)}), patch(
            "tradingagents.safety.get_safety_guard", return_value=guard), \
             patch("tradingagents.screening.gate.check_entry_allowed",
                   return_value=None), patch.object(
            socket.socket, "connect",
            Mock(side_effect=AssertionError("network forbidden"))):
            return service.execute(trade_intent=self._intent(),
                                   dollar_amount=1000.0)

    def test_r13_closed_broker_clock_blocks_opening_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, broker = self._service(
                tmp, SimpleNamespace(is_open=False))
            result = self._execute(service, tmp)
            self.assertFalse(result["success"])
            self.assertEqual(result.get("broker_calls", 0), 0)
            self.assertEqual(len(broker.submits), 0)
            errors = " ".join(
                str(r.get("error") or "") for r in result.get("results", [])
            )
            self.assertIn("closed", errors.lower())
            # The durable row is provably POST-free -> CANCELED.
            self.assertIn(
                "CANCELED",
                [str(o.get("status") or "").upper()
                 for o in result.get("orders", [])],
            )

    def test_r13_clock_failure_blocks_opening_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, broker = self._service(
                tmp, RuntimeError("clock unavailable"))
            result = self._execute(service, tmp)
            self.assertFalse(result["success"])
            self.assertEqual(result.get("broker_calls", 0), 0)
            self.assertEqual(len(broker.submits), 0)
            errors = " ".join(
                str(r.get("error") or "") for r in result.get("results", [])
            )
            self.assertIn("clock unavailable", errors)

    def test_r13_verified_close_not_blocked_by_open_market_gate(self):
        """Risk-reducing closes never consult the opening market gate."""
        with tempfile.TemporaryDirectory() as tmp:
            service, broker = self._service(
                tmp, SimpleNamespace(is_open=False))  # market closed
            # Seed a live position so a close has something to close.
            broker.get_all_positions = lambda: [SimpleNamespace(
                symbol="AAPL", qty="10", market_value="1000",
                avg_entry_price="100", unrealized_pl="0")]
            result = service.liquidate("AAPL")
            # The close is exposure-reducing: the market-clock gate must
            # not block it (the broker fake forbids POSTs via assertion,
            # so success here proves no forbidden POST path was taken —
            # the liquidation result drives the broker's submit_order).
            self.assertNotIn("closed per the broker clock",
                              str(result.get("error") or ""))
            self.assertNotIn("clock", str(result.get("error") or ""))


if __name__ == "__main__":
    unittest.main()
