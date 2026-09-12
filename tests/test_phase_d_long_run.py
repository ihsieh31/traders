"""Phase D tests: 30-day Paper observation orchestration (offline fakes).

Covers config/state primitives, the single-runner lock, early-close
scheduling, one daily round, the crash/resume state machine (no duplicate
broker POST), session idempotency, hard stops, a deterministic fake-clock
full simulation, final reports, and secret hygiene. No network, no LLM.
"""

import json
import os
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz

from tradingagents import long_run as lr
from tradingagents.llm_clients.retry import ProviderFailure

_ET = pytz.timezone("US/Eastern")

SESSION_A = "2026-09-08"  # Tuesday
SESSION_B = "2026-09-09"  # Wednesday

FAKE_SECRET = "sk-FAKESECRET-0123456789abcdef"


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


def _buy_intent(symbol):
    return {"symbol": symbol, "action": "BUY", "target_position": "LONG"}


class FakeBroker:
    def __init__(self, equity=100000.0, cash=50000.0, positions=()):
        self._account = SimpleNamespace(
            id="acct-fake-1", equity=equity, cash=cash, buying_power=cash,
        )
        self._positions = list(positions)

    def get_clock(self):
        # R13: the opening gate proves the regular session from the broker's
        # own clock before any exposure-adding POST; the fixture keeps it open.
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return list(self._positions)


class FakeService:
    """Durable-outbox stand-in: same decision_id never POSTs twice."""

    def enforce_exit_deadlines(self, can_submit=None):
        return {"success": True, "deadline_exits": [], "broker_calls": 0}

    def __init__(self):
        self.recover_calls = 0
        self.execute_calls = []
        self.posts = {}  # decision_id -> broker_calls

    def startup_recover(self, can_submit=None):
        # R02: long-run callers pass their stop/window authority as a
        # submit-boundary callback; the fixture accepts it and fails the
        # recovery the same way the real service would when it refuses.
        if can_submit is not None and not can_submit():
            return {"success": False, "account_execution_state": "PAUSED",
                    "reconciliation_reasons": ["stop/window authority refused recovery submit"]}
        self.recover_calls += 1
        return {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        decision_id = kwargs.get("decision_id")
        if decision_id in self.posts:
            return {"success": True, "deduped": True, "broker_attempted": False,
                    "broker_calls": 0, "decision_id": decision_id}
        self.posts[decision_id] = 1
        return {"success": True, "broker_attempted": True, "broker_calls": 1,
                "decision_id": decision_id}


class FakeGraph:
    def __init__(self, intents=None, exc=None):
        self.intents = intents or {}
        self.exc = exc
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        if self.exc is not None:
            raise self.exc
        intent = self.intents.get(symbol, _buy_intent(symbol))
        return ({"final_trade_intent": intent,
                 "final_trade_decision": "BUY"}, "BUY")


def _fake_plan(symbols, **overrides):
    plan = SimpleNamespace(
        stopped=False,
        deep_analysis_set=list(symbols),
        top20=[{"symbol": s, "rank": i + 1, "screening_score": 90.0 - i,
                "short_reason": "fake reason"} for i, s in enumerate(symbols)],
        selection_date=SESSION_A, as_of=SESSION_A, cached=False,
        overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
        screening_description="Screening=openai/gpt-fake-screening",
        stop_reason_text=lambda: "",
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def _deps(service=None, graph=None, symbols=("AAA", "BBB"), **overrides):
    service = service or FakeService()
    graph = graph or FakeGraph()
    kwargs = {
        "screening_fn": lambda config, refresh=False: _fake_plan(symbols),
        "graph_factory": lambda config: graph,
        "execution_service_factory": lambda: service,
        "broker_client_factory": lambda: FakeBroker(),
        "alert_fn": lambda subject, body, runtime: {"sent": False},
        "sleep_fn": lambda seconds: None,
    }
    kwargs.update(overrides)
    return lr.LongRunDeps(**kwargs), service, graph


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        # Snapshot process-global state: rounds call set_config() (same as
        # the TradingAgentsGraph constructor) and may construct the safety
        # singleton; none of it may leak into other test files.
        import tradingagents.dataflows.config as _cfgmod

        self._saved_config = _cfgmod.get_config()
        self._cfgmod = _cfgmod
        self.workdir = Path(tempfile.mkdtemp(prefix="phased-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.old_env = dict(os.environ)
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")
        # Fast deterministic sizing (no network) for every round test.
        self._regime = patch(
            "tradingagents.regime.regime_risk_multiplier", return_value=1.0)
        self._portfolio = patch(
            "tradingagents.portfolio.adjust_new_position_notional",
            side_effect=lambda s, a, amount, gather_state=None, config=None: amount)
        self._regime.start()
        self._portfolio.start()
        # Isolate the process-global safety guard: the real one persists a
        # kill-switch flag and equity high-water mark under
        # ~/.tradingagents/safety, so leftover operator state would fail
        # every execution-path test with a stale KILL_SWITCH.
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
        self._regime.stop()
        self._portfolio.stop()
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


class ConfigValidationTest(IsolatedTest):
    def test_valid_config_passes(self):
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        self.assertEqual(lr.validate_long_run_config(cfg, runtime), [])

    def test_runtime_forces_full_system_mode(self):
        runtime = lr.build_runtime_config(_valid_cfg())
        self.assertTrue(runtime["auto_screening_enabled"])
        self.assertFalse(runtime["allow_shorts"])
        self.assertEqual(runtime["trading_mode"], "investment")

    def test_safety_enabled_true_is_the_only_allowed_value(self):
        # P2-01: unattended long-run runs only with the safety layer on.
        self.assertEqual(lr.validate_long_run_config(
            _valid_cfg(), lr.build_runtime_config(_valid_cfg())), [])
        for bad in (False, 0, "false", None):
            runtime = lr.build_runtime_config(_valid_cfg())
            runtime["safety_enabled"] = bad
            errors = lr.validate_long_run_config(_valid_cfg(), runtime)
            self.assertTrue(any("safety_enabled" in e for e in errors),
                            f"safety_enabled={bad!r} must be rejected")
        # Missing key keeps the default (on).
        runtime = lr.build_runtime_config(_valid_cfg())
        runtime.pop("safety_enabled", None)
        self.assertEqual(
            lr.validate_long_run_config(_valid_cfg(), runtime), [])

    def test_preflight_refuses_disabled_safety_before_any_probe(self):
        # P2-01 defense layer 2: run_preflight itself fails closed before
        # any LLM probe or broker access, even without a prior validation.
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        runtime["safety_enabled"] = False
        deps = lr.LongRunDeps(
            broker_client_factory=lambda: (_ for _ in ()).throw(
                AssertionError("no broker access when safety is disabled")),
            llm_probe_fn=lambda **k: (_ for _ in ()).throw(
                AssertionError("no LLM probe when safety is disabled")),
        )
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_preflight(cfg, runtime, deps)
        self.assertEqual(ctx.exception.code, "SAFETY_DISABLED")

    def test_missing_values_reported(self):
        cfg = lr.default_long_run_config()
        self.assertIn("base_trade_notional_usd", lr.missing_config_fields(cfg))
        self.assertIn("analysis_provider", lr.missing_config_fields(cfg))
        errors = lr.validate_long_run_config(cfg)
        self.assertTrue(any("notional" in e for e in errors))

    def test_impossible_schedule_time_rejected(self):
        cfg = _valid_cfg(run_time_et="18:00")
        errors = lr.validate_long_run_config(cfg)
        self.assertTrue(any("regular-session" in e for e in errors))
        with self.assertRaises(ValueError):
            lr.parse_run_time_et("not-a-time")

    def test_config_roundtrip_without_secrets(self):
        cfg = _valid_cfg()
        lr.save_long_run_config(cfg)
        loaded = lr.load_long_run_config()
        for key in ("analysis_provider", "base_trade_notional_usd", "run_time_et"):
            self.assertEqual(loaded[key], cfg[key])

    def test_secret_looking_value_refused(self):
        cfg = _valid_cfg(analysis_model=FAKE_SECRET)
        with self.assertRaises(ValueError):
            lr.save_long_run_config(cfg)

    def test_sanitize_redacts_keys_and_url_credentials(self):
        cleaned = lr.sanitize_for_log({
            "ALPACA_API_KEY": "secret",
            "analysis_backend_url": "https://user:pass@host/v1?token=abc",
            "nested": [{"SCREENING_OPENAI_API_KEY": "secret2"}],
            "symbol": "AAPL",
        })
        self.assertEqual(cleaned["ALPACA_API_KEY"], "***")
        self.assertEqual(
            cleaned["nested"][0]["SCREENING_OPENAI_API_KEY"], "***")
        self.assertNotIn("user", cleaned["analysis_backend_url"])
        self.assertNotIn("token", cleaned["analysis_backend_url"])
        self.assertEqual(cleaned["symbol"], "AAPL")


class LockAndAtomicTest(IsolatedTest):
    def test_second_runner_blocked_while_first_holds(self):
        with lr.runner_lock():
            with self.assertRaises(lr.RunnerLockBusy):
                with lr.runner_lock():
                    pass

    def test_lock_released_after_exit(self):
        with lr.runner_lock():
            pass
        with lr.runner_lock():
            pass

    def test_missing_state_means_no_active_run(self):
        self.assertFalse(lr.active_path().exists())
        self.assertIsNone(lr.load_active_state())

    def test_valid_state_round_trips_unchanged(self):
        state = {"run_id": "run-x", "status": "RUNNING"}
        lr.save_active_state(state)
        before = lr.active_path().read_bytes()
        self.assertEqual(lr.load_active_state(), state)
        self.assertEqual(lr.active_path().read_bytes(), before)

    def test_truncated_json_fails_closed(self):
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "x", "status":', encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_non_dict_root_fails_closed(self):
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[{"run_id": "x"}]', encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_missing_run_id_fails_closed(self):
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status": "RUNNING"}', encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_blank_run_id_fails_closed(self):
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in ('{"run_id": ""}', '{"run_id": "   "}'):
            path.write_text(bad, encoding="utf-8")
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.load_active_state()
            self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_cli_corrupt_active_state_creates_no_new_run_and_no_mutation(self):
        # P1-01: a corrupt active.json must stop the CLI before any setup
        # wizard / observation creation / broker access. Simulate the broker
        # layer with counting fakes; if the CLI tried to create a run or
        # reach Alpaca at all, these record it.
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "lr-old", "status":', encoding="utf-8")

        import cli.main as cli_main
        from typer.testing import CliRunner

        submit_calls, cancel_calls = [], []

        def _broker(_calls={"submit": submit_calls, "cancel": cancel_calls}):
            def submit_order(request):
                _calls["submit"].append(request)
                raise AssertionError("no broker submit may happen")

            def cancel_order(*a, **k):
                _calls["cancel"].append((a, k))
                raise AssertionError("no broker cancel may happen")

            def get_account():
                raise AssertionError("no broker access may happen")

            return SimpleNamespace(
                submit_order=submit_order, cancel_order=cancel_order,
                get_account=get_account, get_all_positions=lambda: [],
            )

        exec_calls = []

        runner = CliRunner()
        with patch("tradingagents.long_run._default_broker_client",
                   side_effect=_broker), \
             patch("tradingagents.long_run._default_execution_service",
                   side_effect=lambda: exec_calls.append("constructed")), \
             patch("cli.main.collect_long_run_config",
                   side_effect=AssertionError("setup wizard must not run")), \
             patch("tradingagents.long_run.run_observation_loop",
                   side_effect=AssertionError("observation loop must not run")):
            result = runner.invoke(cli_main.app, ["long-run"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(len(submit_calls), 0)
        self.assertEqual(len(cancel_calls), 0)
        self.assertEqual(exec_calls, [])
        # Corrupt file preserved untouched; no new run was created anywhere.
        self.assertEqual(path.read_text(encoding="utf-8"),
                         '{"run_id": "lr-old", "status":')
        self.assertFalse((lr.base_dir() / "runs").exists())


    def test_typo_status_fails_closed(self):
        # P1: "RUNNIG" must not silently read as "no active run".
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "existing-run", "status": "RUNNIG"}',
                        encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_terminal_completed_state_fails_closed(self):
        # finalize_observation() persists COMPLETED before final reports and
        # clear_active_state(); a crash in that window must not look like a
        # fresh install.
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "existing-run", "status": "COMPLETED"}',
                        encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_terminal_stopped_state_fails_closed(self):
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "existing-run", "status": "STOPPED"}',
                        encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.load_active_state()
        self.assertEqual(ctx.exception.code, "ACTIVE_STATE_CORRUPT")

    def test_cli_non_resumable_active_state_creates_no_new_run_and_no_mutation(self):
        # P1: an existing but non-resumable active.json (terminal status)
        # must stop the CLI before the setup wizard / observation creation /
        # preflight / recovery / broker access — same contract as corrupt JSON.
        path = lr.active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "lr-old", "status": "COMPLETED"}',
                        encoding="utf-8")

        import cli.main as cli_main
        from typer.testing import CliRunner

        submit_calls, cancel_calls, recovery_calls = [], [], []

        def _broker(_calls={"submit": submit_calls, "cancel": cancel_calls}):
            def submit_order(request):
                _calls["submit"].append(request)
                raise AssertionError("no broker submit may happen")

            def cancel_order(*a, **k):
                _calls["cancel"].append((a, k))
                raise AssertionError("no broker cancel may happen")

            def get_account():
                raise AssertionError("no broker access may happen")

            return SimpleNamespace(
                submit_order=submit_order, cancel_order=cancel_order,
                get_account=get_account, get_all_positions=lambda: [],
            )

        exec_calls = []

        runner = CliRunner()
        with patch("tradingagents.long_run._default_broker_client",
                   side_effect=_broker), \
             patch("tradingagents.long_run._default_execution_service",
                   side_effect=lambda: exec_calls.append("constructed")), \
             patch("tradingagents.long_run.run_post_authorization_recovery",
                   side_effect=lambda *a, **k: recovery_calls.append("recovery")), \
             patch("cli.main.collect_long_run_config",
                   side_effect=AssertionError("setup wizard must not run")), \
             patch("tradingagents.long_run.run_preflight",
                   side_effect=AssertionError("preflight must not run")), \
             patch("tradingagents.long_run.new_observation_state",
                   side_effect=AssertionError("no new observation may be created")), \
             patch("tradingagents.long_run.run_observation_loop",
                   side_effect=AssertionError("observation loop must not run")):
            result = runner.invoke(cli_main.app, ["long-run"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(len(submit_calls), 0)
        self.assertEqual(len(cancel_calls), 0)
        self.assertEqual(recovery_calls, [])
        self.assertEqual(exec_calls, [])
        # Existing state preserved untouched; no new run was created anywhere.
        self.assertEqual(path.read_text(encoding="utf-8"),
                         '{"run_id": "lr-old", "status": "COMPLETED"}')
        self.assertFalse((lr.base_dir() / "runs").exists())


class SchedulingTest(IsolatedTest):
    def test_normal_day_uses_configured_target(self):
        info = lr.effective_target_for_session(
            lr.date(2026, 9, 8), "11:00",
            calendar_rows=_rows((SESSION_A, "16:00")),
        )
        self.assertEqual(info["effective_target"], "11:00")
        self.assertEqual(info["schedule_adjustment"], "NONE")
        self.assertEqual(info["authoritative_close"], "16:00")

    def test_early_close_moves_before_close(self):
        info = lr.effective_target_for_session(
            lr.date(2026, 9, 8), "14:00",
            calendar_rows=_rows((SESSION_A, "13:00")),
        )
        self.assertEqual(info["configured_target"], "14:00")
        self.assertEqual(info["effective_target"], "12:30")
        self.assertEqual(info["schedule_adjustment"], "EARLY_CLOSE")

    def test_unprovable_calendar_fails_closed(self):
        from tradingagents.dataflows.market_calendar import CalendarError

        with self.assertRaises(CalendarError):
            lr.effective_target_for_session(lr.date(2026, 9, 8), "11:00",
                                            calendar_rows=[])

    def test_next_due_prefers_overdue_session(self):
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        started = _ET.localize(datetime(2026, 9, 8, 0, 0)).astimezone(timezone.utc)
        ends = started + lr.timedelta(days=30)
        now = _ET.localize(datetime(2026, 9, 8, 15, 0)).astimezone(timezone.utc)
        target = lr.next_due_session(
            now=now, run_time_et="11:00", started_at=started, ends_at=ends,
            settled=[], calendar_rows=rows,
        )
        self.assertEqual(target["session_date"], SESSION_A)
        self.assertTrue(target["due"])

    def test_completed_session_skipped(self):
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        started = _ET.localize(datetime(2026, 9, 8, 0, 0)).astimezone(timezone.utc)
        ends = started + lr.timedelta(days=30)
        now = _ET.localize(datetime(2026, 9, 9, 15, 0)).astimezone(timezone.utc)
        target = lr.next_due_session(
            now=now, run_time_et="11:00", started_at=started, ends_at=ends,
            settled=[SESSION_A], calendar_rows=rows,
        )
        self.assertEqual(target["session_date"], SESSION_B)
        self.assertTrue(target["due"])

    def test_missed_session_is_never_rescheduled(self):
        # A MISSED round is terminal: on a late resume the scheduler must not
        # hand the missed session back as due (stale backfill, A18).
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        started = _ET.localize(datetime(2026, 9, 8, 0, 0)).astimezone(timezone.utc)
        ends = started + lr.timedelta(days=30)
        now = _ET.localize(datetime(2026, 9, 9, 15, 0)).astimezone(timezone.utc)
        missed_journal = lr.new_round_journal(SESSION_A, [])
        missed_journal["status"] = "MISSED"
        lr.save_round_journal("run-sched", missed_journal)
        self.assertEqual(lr.settled_sessions("run-sched"), [SESSION_A])
        target = lr.next_due_session(
            now=now, run_time_et="11:00", started_at=started, ends_at=ends,
            settled=lr.settled_sessions("run-sched"), calendar_rows=rows,
        )
        self.assertEqual(target["session_date"], SESSION_B)
        self.assertTrue(target["due"])

    def test_missed_session_recorded_not_backfilled(self):
        missed = lr.sweep_missed_sessions(
            run_id="run-x", expected=[SESSION_A, SESSION_B],
            today=SESSION_B,
        )
        self.assertEqual(missed, [SESSION_A])
        journal = lr.load_round_journal("run-x", SESSION_A)
        self.assertEqual(journal["status"], "MISSED")
        self.assertEqual(journal["stop_reason"], "MISSED_PROCESS_DOWN")


class DailyRoundTest(IsolatedTest):
    def _run(self, session=SESSION_A, symbols=("AAA", "BBB"), **kw):
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        deps, service, graph = _deps(symbols=symbols, **kw)
        journal = lr.run_daily_round(
            run_id="run-1", session_date=session, long_cfg=cfg,
            runtime=runtime,
            schedule_info={"effective_at": f"{session}T11:00:00-04:00",
                           "schedule_adjustment": "NONE"},
            deps=deps,
        )
        return journal, service, graph, cfg, runtime

    def test_happy_path_completes_session_once(self):
        journal, service, graph, _, _ = self._run()
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertFalse(journal["screening"]["cached"])
        self.assertEqual(len(journal["screening"]["top20"]), 2)
        for symbol in ("AAA", "BBB"):
            entry = journal["symbols"][symbol]
            self.assertEqual(entry["status"], "DONE")
            self.assertEqual(entry["signal"], "BUY")
            self.assertIsNotNone(entry["trade_intent"])
            self.assertEqual(entry["execution_result_summary"]["broker_calls"], 1)
        self.assertEqual(len(service.execute_calls), 2)
        # Session idempotency: a second pass never re-analyzes or re-executes.
        deps2, service2, graph2 = _deps(symbols=("AAA", "BBB"))
        journal3 = lr.run_daily_round(
            run_id="run-1", session_date=SESSION_A,
            long_cfg=_valid_cfg(), runtime=lr.build_runtime_config(_valid_cfg()),
            deps=deps2,
        )
        self.assertEqual(journal3["status"], "COMPLETED")
        self.assertEqual(graph2.calls, [])
        self.assertEqual(service2.execute_calls, [])

    def test_crash_at_executing_resumes_without_duplicate_post(self):
        # Simulate a crash after EXECUTING was persisted but before execute ran.
        journal = lr.new_round_journal(SESSION_A, ["AAA", "BBB"])
        journal["status"] = "RUNNING"
        journal["symbols"]["AAA"] = {
            "status": "DONE", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("AAA"),
            "execution_result_summary": {"broker_calls": 1},
        }
        journal["symbols"]["BBB"] = {
            "status": "EXECUTING", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("BBB"),
            "execution_result_summary": None,
        }
        lr.save_round_journal("run-2", journal)
        graph = FakeGraph()
        service = FakeService()

        def _boom(symbol, trade_date):
            raise AssertionError("must not re-analyze on EXECUTING resume")

        graph.propagate = _boom
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: _fake_plan(("AAA", "BBB")),
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: FakeBroker(),
            alert_fn=lambda s, b, r: {},
            sleep_fn=lambda s: None,
        )
        out = lr.run_daily_round(
            run_id="run-2", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps,
        )
        self.assertEqual(out["symbols"]["BBB"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)
        self.assertEqual(
            service.execute_calls[0]["decision_id"], f"run-2-{SESSION_A}-BBB")
        # Resume once more: the identical decision identity dedupes, no POST.
        out2 = lr.run_daily_round(
            run_id="run-2", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps,
        )
        self.assertEqual(len(service.execute_calls), 1)

    def test_analyzing_without_proof_reanalyzes(self):
        journal = lr.new_round_journal(SESSION_A, ["AAA"])
        journal["status"] = "RUNNING"
        journal["symbols"]["AAA"]["status"] = "ANALYZING"
        lr.save_round_journal("run-3", journal)
        deps, service, graph = _deps(symbols=("AAA",))
        lr.run_daily_round(
            run_id="run-3", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps,
        )
        self.assertEqual(graph.calls, [("AAA", SESSION_A)])
        self.assertEqual(len(service.execute_calls), 1)

    def test_analyzing_with_completed_run_log_recovers_intent(self):
        runs_dir = (Path("eval_results") / "AAA" / "TradingAgentsStrategy_logs"
                    / "runs")
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "r1.json").write_text(json.dumps({
            "run_id": "r1", "symbol": "AAA", "trade_date": SESSION_A,
            "status": "completed", "started_at": f"{SESSION_A}T15:00:00+00:00",
            # F12: long-run run logs carry their observation identity;
            # recovery only accepts an exact metadata match. The keys mirror
            # what propagate() actually writes (config "_analysis_source" /
            # "_long_run_observation_id" with the leading underscore stripped).
            "metadata": {"analysis_source": "long_run",
                         "long_run_observation_id": "run-4"},
            "snapshots": {"final_state": {"final_trade_intent":
                                          _buy_intent("AAA")}},
            "summary": {},
        }), encoding="utf-8")
        journal = lr.new_round_journal(SESSION_A, ["AAA"])
        journal["status"] = "RUNNING"
        journal["symbols"]["AAA"]["status"] = "ANALYZING"
        lr.save_round_journal("run-4", journal)
        graph = FakeGraph()
        service = FakeService()

        def _boom(symbol, trade_date):
            raise AssertionError("proven intent must not be re-analyzed")

        graph.propagate = _boom
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: _fake_plan(("AAA",)),
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: FakeBroker(),
            sleep_fn=lambda s: None,
        )
        out = lr.run_daily_round(
            run_id="run-4", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps,
        )
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(out["symbols"]["AAA"]["signal"], "BUY")
        self.assertEqual(len(service.execute_calls), 1)

    def test_provider_failure_hard_stops(self):
        exc = ProviderFailure(role="analysis", provider="openai",
                              model="gpt-x", attempts=4,
                              category="transient", detail="overloaded")
        deps, service, graph = _deps(graph=FakeGraph(exc=exc),
                                     symbols=("AAA",))
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-5", session_date=SESSION_A,
                long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "PROVIDER_FAILURE")
        journal = lr.load_round_journal("run-5", SESSION_A)
        self.assertEqual(journal["symbols"]["AAA"]["status"], "FAILED")

    def test_screening_stop_hard_stops(self):
        plan = _fake_plan(("AAA",), stopped=True)
        plan.stop_reason_text = lambda: "CALENDAR_UNAVAILABLE: down"
        deps, _, _ = _deps(symbols=("AAA",))
        deps.screening_fn = lambda config, refresh=False: plan
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-6", session_date=SESSION_A,
                long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "SCREENING_STOPPED")

    def test_paused_account_hard_stops(self):
        service = FakeService()

        def _paused(**kwargs):
            return {"success": False, "paused": True, "broker_attempted": False,
                    "broker_calls": 0, "error": "PAUSED: unknown order"}

        service.execute = _paused
        deps, _, _ = _deps(service=service, symbols=("AAA",))
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-7", session_date=SESSION_A,
                long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "ACCOUNT_PAUSED")

    def test_unknown_outcome_stays_executing_and_stops(self):
        service = FakeService()

        def _unknown(**kwargs):
            return {"success": False, "broker_attempted": True,
                    "broker_calls": 1, "has_unknown": True}

        service.execute = _unknown
        deps, _, _ = _deps(service=service, symbols=("AAA",))
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-8", session_date=SESSION_A,
                long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "EXECUTION_AMBIGUOUS")
        journal = lr.load_round_journal("run-8", SESSION_A)
        self.assertEqual(journal["symbols"]["AAA"]["status"], "EXECUTING")

    def test_recovery_refusal_hard_stops(self):
        service = FakeService()
        service.startup_recover = lambda can_submit=None: {
            "success": False, "reconciliation_reasons": ["unresolved UNKNOWN"]}
        deps, _, _ = _deps(service=service, symbols=("AAA",))
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-9", session_date=SESSION_A,
                long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "RECOVERY_UNSAFE")


class FakeClockSimulationTest(IsolatedTest):
    def test_full_window_simulation_finalizes_with_reports(self):
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        started = _ET.localize(
            datetime(2026, 9, 8, 10, 0)).astimezone(timezone.utc)
        cfg = _valid_cfg(duration_calendar_days=30, run_time_et="11:00")
        runtime = lr.build_runtime_config(cfg)
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A, SESSION_B], now=started)
        lr.save_active_state(state)
        times = [
            _ET.localize(datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            # R02 Layer 1 reads now_fn once at round entry (stop precheck);
            # F10 checkpoints then read now_fn three more times inside the
            # round (pre-screening, per-symbol loop, pre-execution) — all
            # must land inside the window.
            _ET.localize(datetime(2026, 9, 8, 11, 1)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 2)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 3)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 4)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 5)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 6)).astimezone(timezone.utc),
            started + lr.timedelta(days=30, hours=1),
        ]
        calls = {"n": 0}

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        alerts = []
        deps, service, graph = _deps(
            symbols=("AAA",),
            calendar_rows=rows,
            now_fn=_now,
            alert_fn=lambda s, b, r: alerts.append(s) or {},
        )
        with lr.runner_lock():
            result = lr.run_observation_loop(state, cfg, runtime, deps)
        self.assertEqual(result["outcome"], "completed")
        run_id = state["run_id"]
        md_path = Path(result["md_path"])
        json_path = Path(result["json_path"])
        self.assertTrue(md_path.is_file())
        self.assertTrue(json_path.is_file())
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["coverage"]["expected_trading_sessions"], 2)
        self.assertEqual(payload["coverage"]["completed_sessions"], 1)
        self.assertEqual(payload["coverage"]["missed_sessions"], 1)
        self.assertAlmostEqual(
            payload["coverage"]["completion_rate"], 0.5)
        for section in ("account", "decisions", "screening", "execution",
                        "llm_operations", "reliability", "evidence"):
            self.assertIn(section, payload)
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("Raw evidence index", text)
        # Secret hygiene: no fake marker anywhere in generated files.
        markers = ["sk-FAKESECRET", "FAKESECRET"]
        hits = lr.scan_files_for_secrets(
            [p for p in (lr.run_dir(run_id)).rglob("*") if p.is_file()],
            markers,
        )
        self.assertEqual(hits, [])
        self.assertFalse(lr.active_path().exists())
        self.assertTrue(alerts)

    def test_resume_continues_original_window(self):
        rows = _rows((SESSION_A, "16:00"), (SESSION_B, "16:00"))
        started = _ET.localize(
            datetime(2026, 9, 8, 10, 0)).astimezone(timezone.utc)
        cfg = _valid_cfg(duration_calendar_days=30, run_time_et="11:00")
        runtime = lr.build_runtime_config(cfg)
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A, SESSION_B], now=started)
        lr.save_active_state(state)
        times = [
            _ET.localize(datetime(2026, 9, 8, 11, 0)).astimezone(timezone.utc),
            # F10 intra-round checkpoint reads must stay inside the window.
            _ET.localize(datetime(2026, 9, 8, 11, 2)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 3)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 4)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 5)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 8, 11, 6)).astimezone(timezone.utc),
            started + lr.timedelta(days=30, hours=1),
        ]
        calls = {"n": 0}

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        deps, _, _ = _deps(symbols=("AAA",), calendar_rows=rows,
                           now_fn=_now,
                           alert_fn=lambda s, b, r: {})
        result = lr.setup_or_resume(long_cfg=cfg, runtime=runtime, deps=deps)
        self.assertEqual(result["outcome"], "completed")
        reloaded_manifest = json.loads(
            (lr.run_dir(result["run_id"]) / "manifest.json").read_text(
                encoding="utf-8"))
        self.assertEqual(reloaded_manifest["run_id"], state["run_id"])
        self.assertEqual(reloaded_manifest["status"], "COMPLETED")

    def test_no_active_observation_refuses_resume(self):
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.setup_or_resume(deps=lr.LongRunDeps())
        self.assertEqual(ctx.exception.code, "NO_ACTIVE_OBSERVATION")

    def test_second_runner_refused(self):
        cfg = _valid_cfg()
        state = lr.new_observation_state(cfg, expected_sessions=[SESSION_A])
        lr.save_active_state(state)
        with lr.runner_lock():
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.setup_or_resume(deps=lr.LongRunDeps())
        self.assertEqual(ctx.exception.code, "ALREADY_RUNNING")


class TerminalJournalGateTest(IsolatedTest):
    """Settled sessions are never re-run; corrupt journals fail closed."""

    def _save_journal(self, run_id, session, status):
        journal = lr.new_round_journal(session, ["AAA"])
        journal["status"] = status
        lr.save_round_journal(run_id, journal)

    def test_completed_journal_short_circuits_without_side_effects(self):
        self._save_journal("run-g1", SESSION_A, "COMPLETED")
        screening_calls = []

        def _screening(config, refresh=False):
            screening_calls.append(1)
            return _fake_plan(("AAA",))

        deps, service, graph = _deps(symbols=("AAA",), screening_fn=_screening)
        out = lr.run_daily_round(
            run_id="run-g1", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps,
        )
        self.assertEqual(out["status"], "COMPLETED")
        self.assertEqual(screening_calls, [])
        self.assertEqual(service.recover_calls, 0)
        self.assertEqual(service.execute_calls, [])
        self.assertEqual(graph.calls, [])

    def test_missed_and_stopped_journals_refuse_to_rerun(self):
        for status in ("MISSED", "STOPPED"):
            with self.subTest(status=status):
                self._save_journal("run-g2", SESSION_A, status)
                deps, _, _ = _deps(symbols=("AAA",))
                with self.assertRaises(lr.LongRunStop) as ctx:
                    lr.run_daily_round(
                        run_id="run-g2", session_date=SESSION_A,
                        long_cfg=_valid_cfg(),
                        runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
                self.assertEqual(ctx.exception.code, "SESSION_SETTLED")

    def test_unknown_status_and_unreadable_journal_fail_closed(self):
        deps, _, _ = _deps(symbols=("AAA",))
        self._save_journal("run-g3", SESSION_A, "SOMEHOW_CORRUPT")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-g3", session_date=SESSION_A, long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "STATE_CORRUPT")

        rounds_dir = lr.run_dir("run-g3") / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        (rounds_dir / f"{SESSION_B}.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-g3", session_date=SESSION_B, long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "STATE_CORRUPT")

    def test_analyzed_resume_executes_saved_intent_without_reanalysis(self):
        # Case C: the exact persisted intent is executed; the LLM is never
        # re-asked for a symbol already analyzed (A25).
        journal = lr.new_round_journal(SESSION_A, ["AAA"])
        journal["status"] = "RUNNING"
        journal["symbols"]["AAA"] = {
            "status": "ANALYZED", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("AAA"),
            "execution_result_summary": None,
        }
        lr.save_round_journal("run-g4", journal)
        deps, service, graph = _deps(symbols=("AAA",))

        def _boom(symbol, trade_date):
            raise AssertionError("ANALYZED resume must not re-ask the LLM")

        graph.propagate = _boom
        out = lr.run_daily_round(
            run_id="run-g4", session_date=SESSION_A, long_cfg=_valid_cfg(),
            runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)
        self.assertEqual(
            service.execute_calls[0]["decision_id"], f"run-g4-{SESSION_A}-AAA")

    def test_engaged_kill_switch_stops_the_round(self):
        # The isolated guard proves the KILL_SWITCH hard-stop wiring end to
        # end without touching machine-global operator state (A32).
        self._isolated_guard.engage_kill_switch("acceptance drill")
        deps, _, _ = _deps(symbols=("AAA",))
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-g5", session_date=SESSION_A, long_cfg=_valid_cfg(),
                runtime=lr.build_runtime_config(_valid_cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "KILL_SWITCH")


class LateResumeTest(IsolatedTest):
    def test_late_resume_never_backfills_a_missed_session(self):
        # Day 1 completed; the process was down for day 2; resume on day 3
        # before the target time. Day 2 must stay MISSED (no analysis, no
        # stale order) and day 3 must run exactly once (A18).
        rows = _rows(("2026-09-08", "16:00"), ("2026-09-09", "16:00"),
                     ("2026-09-10", "16:00"))
        started = _ET.localize(
            datetime(2026, 9, 8, 10, 0)).astimezone(timezone.utc)
        cfg = _valid_cfg(run_time_et="11:00")
        runtime = lr.build_runtime_config(cfg)
        state = lr.new_observation_state(
            cfg, expected_sessions=["2026-09-08", "2026-09-09", "2026-09-10"],
            now=started)
        state["run_id"] = "run-a18"
        lr.save_active_state(state)
        done = lr.new_round_journal("2026-09-08", ["AAA"])
        done["status"] = "COMPLETED"
        lr.save_round_journal("run-a18", done)

        times = [
            _ET.localize(datetime(2026, 9, 10, 10, 0)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 0)).astimezone(timezone.utc),
            # R02 Layer 1 reads now_fn once at round entry (stop precheck);
            # F10 intra-round checkpoint reads must stay inside the window:
            # pre-screening, per-symbol loop, post-analysis, pre-execution.
            _ET.localize(datetime(2026, 9, 10, 11, 1)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 2)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 3)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 4)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 5)).astimezone(timezone.utc),
            _ET.localize(datetime(2026, 9, 10, 11, 6)).astimezone(timezone.utc),
            started + lr.timedelta(days=30, hours=1),
        ]
        calls = {"n": 0}
        sleeps = []

        def _now():
            idx = min(calls["n"], len(times) - 1)
            calls["n"] += 1
            return times[idx]

        deps, service, graph = _deps(
            symbols=("AAA",), calendar_rows=rows, now_fn=_now,
            sleep_fn=lambda s: sleeps.append(s),
        )
        with lr.runner_lock():
            result = lr.run_observation_loop(state, cfg, runtime, deps)
        self.assertEqual(result["outcome"], "completed")
        # Scheduler waits are bounded sleeps, never a busy spin (A47).
        self.assertTrue(sleeps)
        self.assertTrue(all(0 < s <= 60 for s in sleeps))
        # Day 2 stayed missed and was never re-analyzed.
        self.assertEqual(
            lr.load_round_journal("run-a18", "2026-09-09")["status"], "MISSED")
        self.assertEqual(graph.calls, [("AAA", "2026-09-10")])
        self.assertEqual(len(service.execute_calls), 1)
        self.assertEqual(
            service.execute_calls[0]["decision_id"], "run-a18-2026-09-10-AAA")
        self.assertEqual(
            lr.load_round_journal("run-a18", "2026-09-10")["status"], "COMPLETED")

    def test_sweep_settles_stale_running_journal_and_keeps_evidence(self):
        # A round that aged past its session date unfinished (process died
        # mid-round) is settled as missed WITHOUT discarding the partial
        # per-symbol evidence.
        journal = lr.new_round_journal(SESSION_A, ["AAA", "BBB"])
        journal["status"] = "RUNNING"
        journal["started_at"] = "2026-09-08T15:00:00+00:00"
        journal["symbols"]["AAA"] = {
            "status": "DONE", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("AAA"),
            "execution_result_summary": {"broker_calls": 1},
        }
        lr.save_round_journal("run-sweep", journal)
        missed = lr.sweep_missed_sessions(
            run_id="run-sweep", expected=[SESSION_A], today=SESSION_B)
        self.assertEqual(missed, [SESSION_A])
        settled = lr.load_round_journal("run-sweep", SESSION_A)
        self.assertEqual(settled["status"], "MISSED")
        self.assertEqual(settled["stop_reason"], "MISSED_PROCESS_DOWN")
        self.assertEqual(settled["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(settled["symbols"]["AAA"]["execution_result_summary"],
                         {"broker_calls": 1})
        self.assertEqual(settled["symbols"]["BBB"]["status"], "PENDING")
        self.assertIn(SESSION_A, lr.settled_sessions("run-sweep"))

    def test_finalize_records_missed_sessions_without_losing_evidence(self):
        cfg = _valid_cfg()
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A, SESSION_B])
        state["run_id"] = "run-fin"
        partial = lr.new_round_journal(SESSION_A, ["AAA", "BBB"])
        partial["status"] = "RUNNING"
        partial["symbols"]["AAA"] = {
            "status": "DONE", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": _buy_intent("AAA"),
            "execution_result_summary": {"broker_calls": 1},
        }
        lr.save_round_journal("run-fin", partial)
        deps = lr.LongRunDeps(alert_fn=lambda s, b, r: {}, sleep_fn=lambda s: None)
        out = lr.finalize_observation(
            state, cfg, lr.build_runtime_config(cfg), deps,
            final_status="COMPLETED")
        self.assertEqual(out["outcome"], "completed")
        kept = lr.load_round_journal("run-fin", SESSION_A)
        self.assertEqual(kept["status"], "MISSED")
        self.assertEqual(kept["stop_reason"], "MISSED_PROCESS_DOWN")
        self.assertEqual(kept["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(kept["symbols"]["BBB"]["status"], "PENDING")
        payload = json.loads(Path(out["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["coverage"]["expected_trading_sessions"], 2)
        self.assertEqual(payload["coverage"]["completed_sessions"], 0)
        self.assertEqual(payload["coverage"]["missed_sessions"], 2)
        executed = [row for row in payload["decisions"]["per_symbol"]
                    if row["symbol"] == "AAA" and row["status"] == "DONE"]
        self.assertEqual(len(executed), 1)

    def test_interrupted_observation_stays_resumable(self):
        # A controlled interruption leaves the window INTERRUPTED and
        # resumable with the same run_id (A34).
        cfg = _valid_cfg()
        state = lr.new_observation_state(cfg, expected_sessions=[SESSION_A])
        state["run_id"] = "run-int"
        lr.save_active_state(state)
        deps = lr.LongRunDeps(alert_fn=lambda s, b, r: {}, sleep_fn=lambda s: None)
        lr._stop_requested = True
        try:
            result = lr.run_observation_loop(
                state, cfg, lr.build_runtime_config(cfg), deps)
        finally:
            lr._stop_requested = False
        self.assertEqual(result["outcome"], "interrupted")
        saved = lr.load_active_state()
        self.assertIsNotNone(saved)
        self.assertEqual(saved["run_id"], "run-int")
        self.assertEqual(saved["status"], "INTERRUPTED")


class SchedulerStopFinalizationTest(IsolatedTest):
    """Hard stops at the scheduling stage finalize the observation exactly
    like mid-round stops: STOPPED state, partial report, active closed."""

    def test_calendar_unavailable_finalizes_stopped_with_report(self):
        started = _ET.localize(
            datetime(2026, 9, 8, 15, 0)).astimezone(timezone.utc)
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A], now=started)
        state["run_id"] = "run-cal-stop"
        lr.save_active_state(state)
        # No authoritative calendar rows: scheduling cannot be proven.
        deps, service, graph = _deps(
            symbols=("AAA",), calendar_rows=[],
            now_fn=lambda: _ET.localize(
                datetime(2026, 9, 8, 15, 0)).astimezone(timezone.utc))
        with lr.runner_lock():
            result = lr.run_observation_loop(state, cfg, runtime, deps)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["final_status"], "STOPPED")
        self.assertEqual(payload["stop"]["code"], "CALENDAR_UNAVAILABLE")
        self.assertTrue(Path(result["md_path"]).is_file())
        self.assertIn("CALENDAR_UNAVAILABLE",
                      Path(result["md_path"]).read_text(encoding="utf-8"))
        self.assertFalse(lr.active_path().exists())
        # The due session was classified STOPPED with the observation reason,
        # and nothing was analyzed or executed.
        journal = lr.load_round_journal("run-cal-stop", SESSION_A)
        self.assertEqual(journal["status"], "STOPPED")
        self.assertIn("CALENDAR_UNAVAILABLE", journal["stop_reason"])
        self.assertEqual(graph.calls, [])
        self.assertEqual(service.execute_calls, [])
        self.assertEqual(service.recover_calls, 0)

    def test_sweep_corrupt_journal_finalizes_and_preserves_evidence(self):
        started = _ET.localize(
            datetime(2026, 9, 8, 10, 0)).astimezone(timezone.utc)
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A, SESSION_B], now=started)
        state["run_id"] = "run-corrupt-stop"
        lr.save_active_state(state)
        rounds_dir = lr.run_dir("run-corrupt-stop") / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        corrupt = rounds_dir / f"{SESSION_A}.json"
        corrupt.write_text("{broken json", encoding="utf-8")
        # Resume on day 2 after target time: the past corrupt journal is
        # discovered by the missed-session sweep, not by a due round.
        deps, service, graph = _deps(
            symbols=("AAA",),
            calendar_rows=_rows((SESSION_A, "16:00"), (SESSION_B, "16:00")),
            now_fn=lambda: _ET.localize(
                datetime(2026, 9, 9, 15, 0)).astimezone(timezone.utc))
        with lr.runner_lock():
            result = lr.run_observation_loop(state, cfg, runtime, deps)
        self.assertEqual(result["outcome"], "stopped")
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["final_status"], "STOPPED")
        self.assertEqual(payload["stop"]["code"], "STATE_CORRUPT")
        # The original corrupt bytes are preserved, never overwritten with a
        # synthesized classification, and the session stays in the audit via
        # the unreadable-journal bucket.
        self.assertEqual(corrupt.read_text(encoding="utf-8"), "{broken json")
        self.assertEqual(payload["coverage"]["unreadable_journals"], [SESSION_A])
        self.assertEqual(payload["coverage"]["stopped_sessions"], 1)
        self.assertEqual(payload["coverage"]["missed_sessions"], 0)
        self.assertFalse(lr.active_path().exists())
        self.assertEqual(graph.calls, [])
        self.assertEqual(service.execute_calls, [])

    def test_finalize_stopped_labels_unrun_sessions_stopped(self):
        # A stopped observation did not lose sessions to downtime: unrun
        # sessions carry the observation's stop reason (B02 vocabulary).
        cfg = _valid_cfg()
        state = lr.new_observation_state(
            cfg, expected_sessions=[SESSION_A, SESSION_B])
        state["run_id"] = "run-stop-labels"
        deps = lr.LongRunDeps(alert_fn=lambda s, b, r: {}, sleep_fn=lambda s: None)
        out = lr.finalize_observation(
            state, cfg, lr.build_runtime_config(cfg), deps,
            final_status="STOPPED", stop_code="KILL_SWITCH", stop_detail="drill")
        journal = lr.load_round_journal("run-stop-labels", SESSION_A)
        self.assertEqual(journal["status"], "STOPPED")
        self.assertEqual(journal["stop_reason"], "KILL_SWITCH: drill")
        payload = json.loads(
            Path(out["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["coverage"]["stopped_sessions"], 2)
        self.assertEqual(payload["coverage"]["missed_sessions"], 0)
        markdown = Path(out["md_path"]).read_text(encoding="utf-8")
        self.assertIn("status: **STOPPED**", markdown)
        self.assertIn("KILL_SWITCH", markdown)


class FinalReportMathTest(IsolatedTest):
    def test_coverage_return_drawdown_and_turnover_math(self):
        # A37: expected=20, completed=19, missed=1 -> completion 95%.
        # A38/A39: return and max drawdown match hand calculation.
        # A43: turnover matches the Top20 sets it is computed from.
        dates = [f"2026-10-{day:02d}" for day in range(1, 21)]
        state = lr.new_observation_state(_valid_cfg(), expected_sessions=dates)
        state["run_id"] = "run-math"
        for session in dates[:19]:
            journal = lr.new_round_journal(session, ["AAA"])
            journal["status"] = "COMPLETED"
            journal["screening"]["top20"] = [
                {"symbol": s} for s in ("BBB", "CCC", "DDD")]
            if session == dates[0]:
                journal["screening"]["top20"] = [
                    {"symbol": s} for s in ("AAA", "BBB", "CCC")]
            lr.save_round_journal("run-math", journal)
        missed = lr.new_round_journal(dates[19], [])
        missed["status"] = "MISSED"
        lr.save_round_journal("run-math", missed)

        snapshots_path = lr.run_dir("run-math") / "account_snapshots.jsonl"
        equities = [100000, 110000, 105000, 90000, 95000, 120000]
        with open(snapshots_path, "w", encoding="utf-8") as handle:
            for index, equity in enumerate(equities):
                # F14: the last snapshot carries phase="final" so the report's
                # ending equity keeps coming from a fresh end-of-observation
                # capture; the others are round boundaries.
                phase = "final" if index == len(equities) - 1 else "pre_round"
                handle.write(json.dumps({
                    "at": "2026-10-15T00:00:00+00:00", "phase": phase,
                    "session": dates[0], "equity": float(equity),
                    "cash": 0.0,
                }) + "\n")

        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        runtime["execution_db_path"] = str(self.workdir / "execution.db")
        report = lr.aggregate_final_report(state, cfg, runtime)

        self.assertEqual(report["coverage"]["expected_trading_sessions"], 20)
        self.assertEqual(report["coverage"]["completed_sessions"], 19)
        self.assertEqual(report["coverage"]["missed_sessions"], 1)
        self.assertAlmostEqual(report["coverage"]["completion_rate"], 0.95)
        self.assertEqual(report["account"]["starting_equity"], 100000.0)
        self.assertEqual(report["account"]["ending_equity"], 120000.0)
        self.assertEqual(report["account"]["absolute_pl"], 20000.0)
        self.assertAlmostEqual(report["account"]["total_return"], 0.20)
        # Worst segment: (90000 - 110000) / 110000 = -18.1818...%
        self.assertAlmostEqual(
            report["account"]["max_drawdown"], (90000 - 110000) / 110000)
        self.assertEqual(report["account"]["trough_equity"], 90000.0)
        # Hand-calculated churn: day 2 entered DDD / exited AAA; the Top20
        # then stays stable, so every later day reports no change.
        turnover = report["screening"]["turnover"]
        self.assertEqual(len(turnover), 18)
        self.assertEqual(turnover[0], {
            "session": dates[1], "entered": ["DDD"], "exited": ["AAA"]})
        self.assertTrue(
            all(t["entered"] == [] and t["exited"] == [] for t in turnover[1:]))
        markdown = lr.render_final_markdown(report)
        self.assertIn("completion: +95.00%", markdown)
        self.assertIn("maximum drawdown: -18.18%", markdown)


if __name__ == "__main__":
    unittest.main()
