import copy
import hashlib
import json
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from tradingagents import long_run as lr
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.long_run_support.config import apply_ab_backend_runtime_paths
from tradingagents.screening.pipeline import prepare_screening_round_from_selection
from tradingagents.screening.selection_store import (
    SCHEMA_VERSION,
    SCREENING_DATA_FEED,
    SelectionStore,
    default_selection_cache_path,
)
from tradingagents.screening.llm import resolve_screening_config


SESSION = "2026-09-23"


def _screening_config(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update({
        "auto_screening_enabled": True,
        "allow_shorts": True,
        "screening_provider": "openai",
        "screening_model": "test-screening-model",
        "screening_backend_url": None,
        "screening_selection_cache_path": str(tmp_path / "shared" / "cache.json"),
        "screening_select_n": 20,
        "screening_top_k": 40,
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "results"),
        "sector_mapping": {},
        "corporate_action_events": [],
    })
    return config


def _selection(config, *, session=SESSION, as_of="2026-09-22"):
    resolved = resolve_screening_config(config)
    store = SelectionStore(default_selection_cache_path(config))
    top40 = [
        {
            "symbol": f"T{i:02d}", "score": float(100 - i),
            "candidate_lane": "positive_trend", "positive_score": 90.0,
            "negative_score": 10.0, "momentum": 1.0, "trend": 1.0,
            "liquidity": 1.0, "quality": 1.0,
        }
        for i in range(40)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trading_date": session,
        "as_of": as_of,
        "generated_at": "2026-09-23T10:30:00-04:00",
        "data_feed": SCREENING_DATA_FEED,
        "role": {"provider": resolved["spec"].provider,
                 "model": resolved["spec"].model,
                 "endpoint": resolved["spec"].display_endpoint()},
        "config_fingerprint": store.config_fingerprint(config, resolved["spec"]),
        "adjustment_policy": "split",
        "sector_mode": {"applied": True, "max_per_sector": 5, "missing_sectors": []},
        "stats": {"universe_total": 40, "eligible": 40, "excluded": {}},
        "top40": top40,
        "top20": [
            {"rank": i + 1, "symbol": f"T{i:02d}",
             "screening_score": float(100 - i), "short_reason": "shared reason"}
            for i in range(20)
        ],
    }
    store.save(payload)
    return store.load_raw()


def _plan(selection, *, extras=(), cached=False):
    return SimpleNamespace(
        stopped=False,
        reason=None,
        selection=selection,
        selection_date=selection["trading_date"],
        as_of=selection["as_of"],
        cached=cached,
        entry_allowed=True,
        top20=list(selection["top20"]),
        deep_analysis_set=[row["symbol"] for row in selection["top20"]] + list(extras),
        overlap_holdings=[],
        extra_holdings=list(extras),
        blocked_holdings=[],
        screening_description="shared test screening",
        selection_hash=lr._ab_selection_hash(selection),
        stop_reason_text=lambda: "",
    )


def _complete_arm(**kwargs):
    arm_id = kwargs["run_id"]
    session = kwargs["session_date"]
    plan = kwargs["long_cfg"]["_ab_screening_plan"]
    journal = lr.new_round_journal(session, list(plan.deep_analysis_set))
    journal["status"] = "COMPLETED"
    journal["screening"] = {
        "selection_hash": plan.selection_hash,
        "top20": [{"symbol": row["symbol"], "rank": row["rank"]}
                  for row in plan.top20],
        "deep_analysis_set": plan.deep_analysis_set,
    }
    for symbol in journal["symbols"]:
        journal["symbols"][symbol]["status"] = lr.SYMBOL_DONE
        journal["symbols"][symbol]["evidence_packet_sha256"] = "e" * 64
    journal["symbols"]["T00"]["signal"] = (
        "BUY" if kwargs["runtime"]["analysis_backend"] == "traders" else "HOLD"
    )
    lr.save_round_journal(arm_id, journal)
    return journal


def test_shared_selection_plan_adds_only_arm_holdings_without_screening(tmp_path):
    config = _screening_config(tmp_path)
    selection = _selection(config)
    calls = {"screening": 0, "positions": 0}

    def positions():
        calls["positions"] += 1
        return [{"symbol": "AAPL", "qty": 2, "asset_class": "us_equity"}]

    deps = SimpleNamespace(
        positions_fn=positions,
        quarantine_fn=lambda _cfg: lambda _symbol: None,
        asset_fn=lambda _symbol: SimpleNamespace(tradable=True, status="active"),
        screening_invoke_fn=lambda *_a, **_k: calls.__setitem__("screening", calls["screening"] + 1),
    )
    plan = prepare_screening_round_from_selection(
        config, selection, deps=deps, session_date=SESSION
    )

    assert not plan.stopped
    assert plan.deep_analysis_set == [*[row["symbol"] for row in selection["top20"]], "AAPL"]
    assert plan.selection_hash == lr._ab_selection_hash(selection)
    assert calls == {"screening": 0, "positions": 1}


def test_shared_selection_rejects_future_as_of_and_tampering(tmp_path):
    config = _screening_config(tmp_path)
    future = _selection(config, as_of="2026-09-24")
    plan = prepare_screening_round_from_selection(config, future, deps=SimpleNamespace(positions_fn=lambda: []), session_date=SESSION)
    assert plan.stopped
    assert plan.reason == "FROZEN_SELECTION_INVALID"

    tampered = copy.deepcopy(_selection(config))
    tampered["top20"][0]["symbol"] = "FUTURE"
    plan = prepare_screening_round_from_selection(config, tampered, deps=SimpleNamespace(positions_fn=lambda: []), session_date=SESSION)
    assert plan.stopped
    assert "integrity" in plan.detail


def test_ab_runtime_namespaces_are_isolated_but_screening_is_shared(tmp_path):
    base = copy.deepcopy(DEFAULT_CONFIG)
    a = apply_ab_backend_runtime_paths(base, "traders", tmp_path / "ab")
    b = apply_ab_backend_runtime_paths(base, "berkshire", tmp_path / "ab")

    assert a["analysis_backend"] == "traders"
    assert b["analysis_backend"] == "berkshire"
    assert a["_alpaca_account_profile"] == "A"
    assert b["_alpaca_account_profile"] == "B"
    for key in ("execution_db_path", "recovery_ledger_db_path", "results_dir",
                "memory_log_path", "agent_memory_dir", "safety_state_path",
                "safety_kill_switch_path", "data_cache_dir"):
        assert a[key] != b[key], key
    assert a["screening_selection_cache_path"] == b["screening_selection_cache_path"]
    assert a["shared_evidence_dir"] == b["shared_evidence_dir"]


def test_both_backends_bind_to_the_same_frozen_evidence_identity(tmp_path):
    from tradingagents.analysis_backends.resolver import resolve_analysis_backend
    from tradingagents.long_run_support.symbols import _prepare_symbol_graph_config

    root = tmp_path / "shared" / "evidence"
    runtimes = {
        backend: {"analysis_backend": backend, "shared_evidence_dir": str(root),
                  "results_dir": str(tmp_path / backend / "results")}
        for backend in lr.AB_BACKENDS
    }
    with patch(
        "tradingagents.experiments.evidence_snapshot.build_or_load_evidence_packet",
        return_value={"sha256": "a" * 64},
    ), patch("tradingagents.experiments.evidence_snapshot.validate_evidence_completeness"):
        a_config, a_identity = _prepare_symbol_graph_config(
            {}, runtimes["traders"], run_id="lr-fixture", session_date=SESSION, symbol="AAPL"
        )
        b_config, b_identity = _prepare_symbol_graph_config(
            {}, runtimes["berkshire"], run_id="lr-fixture", session_date=SESSION, symbol="AAPL"
        )
    assert a_identity == b_identity
    assert a_config["evidence_packet_path"] == b_config["evidence_packet_path"]
    assert a_config["evidence_packet_sha256"] == b_config["evidence_packet_sha256"] == "a" * 64
    assert a_config["analysis_input_mode"] == b_config["analysis_input_mode"] == "frozen_evidence"
    assert resolve_analysis_backend(a_config) == "traders"
    assert resolve_analysis_backend(b_config) == "berkshire"


def test_ab_completion_waits_for_both_execution_ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "_apply_runtime_config", lambda runtime: None)
    state = {"run_id": "settlement-gate", "mode": "ab", "status": "RUNNING",
             "expected_sessions": []}
    lr.save_active_state(state)
    services = iter([
        SimpleNamespace(startup_recover=lambda **kwargs: {"success": True, "account_execution_state": "CLEAN"},
                        store=SimpleNamespace(list_recoverable_orders=lambda: [])),
        SimpleNamespace(startup_recover=lambda **kwargs: {"success": False, "account_execution_state": "PAUSED"},
                        store=SimpleNamespace(list_recoverable_orders=lambda: [{"status": "UNKNOWN"}])),
    ])
    deps = lr.LongRunDeps(execution_service_factory=lambda: next(services))
    with pytest.raises(lr.LongRunStop, match="SETTLEMENT_UNRESOLVED"):
        lr.finalize_observation(
            state, {"ab_results_root": str(tmp_path / "ab")}, dict(DEFAULT_CONFIG),
            deps, final_status="COMPLETED")
    assert lr.load_active_state()["status"] == "RUNNING"
    assert not (lr.run_dir("settlement-gate") / "final_report.json").exists()


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN", "SUBMITTING", "ACCEPTED", "PARTIAL"])
def test_single_completion_rejects_nonterminal_order(tmp_path, monkeypatch, status):
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "_apply_runtime_config", lambda runtime: None)
    state = {"run_id": "single-settlement", "status": "RUNNING", "expected_sessions": []}
    lr.save_active_state(state)
    service = SimpleNamespace(
        startup_recover=lambda **_kw: {"success": True, "account_execution_state": "CLEAN"},
        store=SimpleNamespace(list_recoverable_orders=lambda: [{"status": status}]),
    )
    with pytest.raises(lr.LongRunStop, match="SETTLEMENT_UNRESOLVED"):
        lr.finalize_observation(
            state, {}, dict(DEFAULT_CONFIG),
            lr.LongRunDeps(execution_service_factory=lambda: service), final_status="COMPLETED")
    assert lr.load_active_state()["status"] == "RUNNING"


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN", "SUBMITTING", "ACCEPTED", "PARTIAL"])
def test_ab_completion_rejects_each_nonterminal_order(tmp_path, monkeypatch, status):
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "_apply_runtime_config", lambda runtime: None)
    state = {"run_id": "nonterminal-gate", "mode": "ab", "status": "RUNNING",
             "expected_sessions": []}
    lr.save_active_state(state)
    calls = iter([0, 1])
    def service():
        arm = next(calls)
        return SimpleNamespace(
            startup_recover=lambda **_kw: {"success": True, "account_execution_state": "CLEAN"},
            store=SimpleNamespace(list_recoverable_orders=lambda: [] if arm == 0 else [{"status": status}]),
        )
    with pytest.raises(lr.LongRunStop, match="SETTLEMENT_UNRESOLVED"):
        lr.finalize_observation(
            state, {"ab_results_root": str(tmp_path / "ab")}, dict(DEFAULT_CONFIG),
            lr.LongRunDeps(execution_service_factory=service), final_status="COMPLETED")
    assert lr.load_active_state()["status"] == "RUNNING"


@pytest.mark.parametrize("key,value", [
    ("max_trade_notional_usd", 0),
    ("max_symbol_concentration_pct", float("nan")),
    ("daily_loss_halt_pct", -1),
    ("max_drawdown_halt_pct", float("inf")),
    ("max_consecutive_rejections", 0),
])
def test_unattended_invalid_safety_limits_fail_preflight(key, value):
    with pytest.raises(lr.LongRunStop, match="SAFETY_LIMIT_INVALID"):
        lr._validate_unattended_safety({"safety_enabled": True, key: value})


def test_wrong_date_round_journal_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    path = lr.round_path("wrong-date", SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_date": "2026-09-24", "status": "COMPLETED"}))
    with pytest.raises(lr.LongRunStop, match="STATE_CORRUPT"):
        lr.load_round_journal("wrong-date", SESSION)


def test_terminal_parent_still_finalizes_running_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "_apply_runtime_config", lambda runtime: None)
    parent = lr.new_round_journal(SESSION, [])
    parent["status"] = "STOPPED"
    lr.save_round_journal("parent-terminal", parent)
    arm_id = lr._ab_arm_run_id("parent-terminal", "traders")
    arm = lr.new_round_journal(SESSION, [])
    arm["status"] = "RUNNING"
    lr.save_round_journal(arm_id, arm)
    other_id = lr._ab_arm_run_id("parent-terminal", "berkshire")
    other = lr.new_round_journal(SESSION, [])
    other["status"] = "COMPLETED"
    lr.save_round_journal(other_id, other)
    monkeypatch.setattr(lr, "aggregate_ab_final_report", lambda *_a: {})
    monkeypatch.setattr(lr, "write_ab_final_report", lambda *_a: ("report.md", "report.json"))
    monkeypatch.setattr(lr, "send_long_run_alert", lambda *_a, **_kw: None)
    state = {"run_id": "parent-terminal", "mode": "ab", "status": "RUNNING",
             "expected_sessions": [SESSION], "started_at": "2026-09-23T00:00:00+00:00",
             "ends_at": "2026-09-24T00:00:00+00:00"}
    lr.finalize_observation(state, {"ab_results_root": str(tmp_path / "ab")},
                            dict(DEFAULT_CONFIG), final_status="STOPPED")
    assert lr.load_round_journal(arm_id, SESSION)["status"] == "STOPPED"
    assert lr.load_round_journal(other_id, SESSION)["status"] == "COMPLETED"


def _ab_round_setup(tmp_path):
    root = tmp_path / "ab-results" / "lr-ab-fixture"
    config = _screening_config(tmp_path)
    selection = _selection(config)
    initial = _plan(selection, extras=["AAPL"])
    deps = lr.LongRunDeps(screening_fn=lambda *_a, **_k: initial,
                          now_fn=lambda: datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc))
    return root, config, selection, initial, deps


def test_ab_shared_screening_skips_after_submission_deadline(tmp_path):
    root, config, _selection_value, initial, _deps = _ab_round_setup(tmp_path)
    calls = []
    deps = lr.LongRunDeps(
        screening_fn=lambda *_a, **_k: calls.append(1) or initial,
        now_fn=lambda: datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc),
    )
    journal = lr.run_ab_daily_round(
        run_id="lr-ab-deadline", session_date=SESSION,
        long_cfg={"ab_results_root": str(root)}, runtime=config, deps=deps,
        schedule_info={"effective_target": "11:00"},
    )
    assert calls == []
    assert journal["shared_screening"]["reason"] == "SESSION_SUBMISSION_DEADLINE"
    assert journal["status"] == "MISSED"
    assert lr.settled_sessions("lr-ab-deadline") == [SESSION]


def test_ab_shared_screening_runs_between_deadline_and_close(tmp_path, monkeypatch):
    # The A/B submission fence is the authoritative close minus 15 minutes,
    # NOT target+30min: a full-market pair runs up to 40 sequential analyses
    # between 11:00 and the fence, so a 30-minute fence silently no-trades
    # the whole campaign. A start at 14:00 ET (16:00 close) was settled
    # MISSED under the old fence before any screening ran; it must screen.
    # Arm preparation is stubbed here — arm execution has its own coverage
    # and this test pins only the coordinator's fence decision.
    root, config, _selection_value, initial, _deps = _ab_round_setup(tmp_path)
    calls = []
    deps = lr.LongRunDeps(
        screening_fn=lambda *_a, **_k: calls.append(1) or initial,
        now_fn=lambda: datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(lr, "run_daily_round", lambda **_kwargs: lr.new_round_journal(SESSION, []))
    # The Berkshire arm's held-review plan preparation consults broker
    # positions; stub it so this test never constructs a real broker client.
    monkeypatch.setattr(
        "tradingagents.screening.pipeline.prepare_screening_round_from_selection",
        lambda _runtime, selection, **_kwargs: _plan(selection),
    )
    journal = lr.run_ab_daily_round(
        run_id="lr-ab-mid-window", session_date=SESSION,
        long_cfg={"ab_results_root": str(root)}, runtime=config, deps=deps,
        schedule_info={"effective_target": "11:00"},
    )
    assert calls == [1]
    assert journal["shared_screening"]["status"] == "SCREENING_COMPLETE"
    assert journal["status"] != "MISSED"


@pytest.mark.parametrize("field,value", [("schema_version", 999), ("status", "BROKEN")])
def test_ab_resume_rejects_invalid_coordinator_before_screening(tmp_path, field, value):
    journal = lr.new_round_journal(SESSION, [])
    journal.update({"mode": "ab", field: value})
    lr.save_round_journal("corrupt-ab", journal)
    before = lr.round_path("corrupt-ab", SESSION).read_bytes()
    with patch.object(lr, "_read_ab_selection_artifact", side_effect=AssertionError("screening reached")):
        with pytest.raises(lr.LongRunStop, match="STATE_CORRUPT"):
            lr.run_ab_daily_round(
                run_id="corrupt-ab", session_date=SESSION,
                long_cfg={"ab_results_root": str(tmp_path / "ab")},
                runtime=_screening_config(tmp_path),
            )
    assert lr.round_path("corrupt-ab", SESSION).read_bytes() == before


def test_ab_resume_rejects_completed_arm_with_unknown_schema(tmp_path):
    _run_interleaved_ab(tmp_path)
    rounds = tmp_path / "state" / "runs"
    for run_id, field, value in (("lr-ab-fixture", "status", "RUNNING"),
                                 ("lr-ab-fixture-traders", "schema_version", 999)):
        path = rounds / run_id / "rounds" / f"{SESSION}.json"
        journal = lr.read_json(path)
        journal[field] = value
        lr.atomic_write_json(path, journal)
    with pytest.raises(lr.LongRunStop, match="STATE_CORRUPT"):
        _run_interleaved_ab(tmp_path)


def test_ab_analysis_recovery_does_not_accept_invalid_intent():
    journal = lr.new_round_journal(SESSION, ["AAPL"])
    journal["symbols"]["AAPL"]["status"] = lr.SYMBOL_ANALYZING
    prepared = lr._PreparedDailyRound(
        run_id="ab-recovery-traders", session_date=SESSION, long_cfg={},
        runtime={"shared_evidence_dir": "unused"}, schedule_info={},
        deps=lr.LongRunDeps(), ends_at=None, journal=journal, service=None,
        graph_factory=None, broker_factory=None,
        recovery_can_submit=lambda: False, session_submit_allowed=lambda: False,
    )
    with patch.object(lr, "_build_graph_config", return_value={}), \
         patch.object(lr, "_recover_intent_from_run_log", return_value={"action": "BUY"}):
        lr._run_prepared_round_symbols(prepared, phase="analysis")
    entry = journal["symbols"]["AAPL"]
    assert entry["status"] == lr.SYMBOL_DONE
    assert entry["trade_intent"] is None
    assert entry["execution_result_summary"]["error"] == "SESSION_SUBMISSION_DEADLINE"


@pytest.mark.parametrize("persistent", [False, True])
def test_continuous_extension_retries_only_calendar_before_mutating_window(tmp_path, persistent):
    state = {"run_id": "extension-retry", "mode": "ab", "status": "RUNNING",
             "continuous": True, "ends_at": "2026-09-24T18:00:00+00:00",
             "expected_sessions": [SESSION]}
    original = copy.deepcopy(state)
    sleeps = []
    deps = lr.LongRunDeps(sleep_fn=sleeps.append)
    results = ([RuntimeError("temporary calendar outage")] * 3 if persistent else
               [RuntimeError("temporary calendar outage"), [date(2026, 9, 24)]])
    with patch.object(lr, "fetch_session_dates", side_effect=results) as fetch:
        if persistent:
            with pytest.raises(lr.LongRunStop, match="CALENDAR_UNAVAILABLE"):
                lr.extend_continuous_window(state, {"duration_calendar_days": 1}, deps)
            assert state == original
        else:
            lr.extend_continuous_window(state, {"duration_calendar_days": 1}, deps)
            assert state["ends_at"] == "2026-09-25T18:00:00+00:00"
            assert state["expected_sessions"] == [SESSION, "2026-09-24"]
            assert lr.load_active_state() == state
    assert fetch.call_count == (3 if persistent else 2)
    assert sleeps == [lr.SCHEDULER_RETRY_DELAY_SECONDS] * (fetch.call_count - 1)


def test_unresolvable_session_target_does_not_silently_skip_the_round(
    tmp_path, monkeypatch
):
    """An unknown submission window is not proof that the window closed.

    The F15 gate once treated "no effective target" as "deadline passed" and
    returned SESSION_SUBMISSION_DEADLINE, turning a calendar outage into a
    silent no-trade session. Only a provably closed window may skip.
    """
    root, config, selection, initial, deps = _ab_round_setup(tmp_path)
    screen_calls = []
    deps = lr.LongRunDeps(
        screening_fn=lambda *_a, **_k: screen_calls.append(1) or initial,
        now_fn=lambda: datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
    )
    # No calendar authority, and no schedule_info carrying an effective target.
    monkeypatch.setattr(
        lr, "effective_target_for_session",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("calendar unavailable")),
    )
    with patch.object(lr, "_apply_runtime_config"), \
         patch.object(lr, "_validate_long_run_execution_config"), \
         patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(
             check_llm_budget=lambda: SimpleNamespace(allowed=True, reasons=[]))), \
         patch("tradingagents.screening.pipeline.prepare_screening_round_from_selection",
               side_effect=lambda *_a, **_kw: _plan(selection)), \
         patch.object(lr, "run_daily_round", side_effect=_complete_arm):
        journal = lr.run_ab_daily_round(
            run_id="lr-ab-fixture", session_date=SESSION,
            long_cfg={"ab_results_root": str(root)}, runtime=config, deps=deps,
        )

    assert screen_calls, "screening must still run when the window is unknown"
    assert journal["shared_screening"].get("reason") != "SESSION_SUBMISSION_DEADLINE"


def test_scheduler_uses_frozen_unsettled_session_without_history_calendar_gets(monkeypatch):
    monkeypatch.setattr("tradingagents.dataflows.market_calendar.is_us_trading_day_auth",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("history re-queried")))
    calls = []
    monkeypatch.setattr(lr, "effective_target_for_session",
                        lambda day, *_a, **_kw: calls.append(day) or
                        {"session_date": day.isoformat(), "effective_target": "11:00"})
    sessions = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(250)]
    result = lr.next_due_session(
        now=datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc),
        run_time_et="11:00",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        settled=sessions[:-1], expected_sessions=sessions,
    )
    assert result["session_date"] == sessions[-1]
    assert len(calls) == 1


def test_experiment_fingerprint_tracks_behavior_not_api_keys():
    config = lr.default_long_run_config()
    runtime = dict(DEFAULT_CONFIG)
    baseline = lr.experiment_fingerprint(config, runtime)
    assert lr.experiment_fingerprint(config, {**runtime, "max_trade_notional_usd": 500}) != baseline
    assert lr.experiment_fingerprint(config, {**runtime, "analysis_model": "changed-model"}) != baseline
    assert lr.experiment_fingerprint(config, {**runtime, "openai_api_key": "changed-secret"}) == baseline
    nested = {**runtime, "deep_llm_params": {**runtime["deep_llm_params"], "auth_token": "secret"}}
    assert lr.experiment_fingerprint(config, nested) == baseline


def test_experiment_fingerprint_survives_the_active_state_sanitize_round_trip():
    """The resume path rebuilds its config from the sanitized active state.

    ``sanitize_for_log`` rewrites every ``*_url`` key through ``sanitize_url``,
    which turns an unset ``None`` into ``""``. If the fingerprint hashed the
    raw ``None``, every crash/resume of a live observation would be refused as
    CONFIG_DRIFT — the exact interruption the gate exists to prevent.
    """
    created_cfg = dict(lr.default_long_run_config())
    created_cfg.update({
        "duration_calendar_days": 30,
        "base_trade_notional_usd": 50_000,
        "analysis_provider": "openai",
        "analysis_model": "test-analysis-model",
    })
    created_cfg["ab_results_root"] = "/tmp/ab-root/lr-fixture"
    created_runtime = lr.build_runtime_config(created_cfg)
    stored = json.loads(json.dumps(lr.sanitize_for_log(created_cfg)))
    recorded = lr.experiment_fingerprint(created_cfg, created_runtime)

    resumed_cfg = dict(lr.default_long_run_config())
    resumed_cfg.update(stored)
    assert lr.experiment_fingerprint(
        resumed_cfg, lr.build_runtime_config(resumed_cfg)) == recorded

    # A real behavioral change must still be refused on that same round trip.
    drifted = dict(resumed_cfg)
    drifted["base_trade_notional_usd"] = 10_000
    assert lr.experiment_fingerprint(drifted, lr.build_runtime_config(drifted)) != recorded
    assert lr.experiment_fingerprint(
        resumed_cfg,
        {**lr.build_runtime_config(resumed_cfg), "openai_api_key": "rotated"},
    ) == recorded


def test_ab_pair_order_is_deterministic_and_balanced():
    """F14: neither arm may always take the first quote/execution window."""
    def first_arm(session, symbol):
        parity = int.from_bytes(
            hashlib.sha256(f"{session}:{symbol}".encode()).digest()[:8], "big") % 2
        return "traders" if parity == 0 else "berkshire"

    symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "SPY", "QQQ", "IWM"]
    sessions = [f"2026-09-{day:02d}" for day in range(1, 21)]
    firsts = [first_arm(session, symbol) for session in sessions for symbol in symbols]
    assert set(firsts) == {"traders", "berkshire"}
    assert abs(firsts.count("traders") - firsts.count("berkshire")) <= 0.1 * len(firsts)


def test_ab_report_reads_each_arms_own_kill_switch(tmp_path, monkeypatch):
    """F18-D: the two arms keep independent kill-switch files under their arm dir."""
    monkeypatch.setenv("TRADINGBUFFETT_LONG_RUN_DIR", str(tmp_path / "state"))
    root = str(tmp_path / "ab" / "lr-kill-fixture")
    runtimes = {backend: apply_ab_backend_runtime_paths(
        dict(DEFAULT_CONFIG), backend, root) for backend in lr.AB_BACKENDS}
    assert runtimes["traders"]["safety_kill_switch_path"] != \
        runtimes["berkshire"]["safety_kill_switch_path"]

    halted = Path(runtimes["traders"]["safety_kill_switch_path"])
    halted.parent.mkdir(parents=True, exist_ok=True)
    halted.write_text("operator halt", encoding="utf-8")

    base_state = {"run_id": "kill-fixture", "expected_sessions": [],
                  "started_at": "2026-09-01T00:00:00+00:00",
                  "ends_at": "2026-10-01T00:00:00+00:00"}
    reports = {backend: lr.aggregate_final_report(dict(base_state), {}, arm_runtime)
               for backend, arm_runtime in runtimes.items()}
    assert reports["traders"]["safety"]["kill_switch_active"] is True
    assert reports["traders"]["safety"]["kill_switch_reason"] == "operator halt"
    assert reports["berkshire"]["safety"]["kill_switch_active"] is False


def test_ab_startup_requires_flat_comparable_accounts():
    snapshots = {
        "traders": {"account_ref": "account-a", "equity": 100_000, "positions": []},
        "berkshire": {"account_ref": "account-b", "equity": 100_500, "positions": []},
    }
    lr.validate_ab_startup_snapshots(snapshots)
    with pytest.raises(lr.LongRunStop, match="AB_BASELINE_INVALID"):
        lr.validate_ab_startup_snapshots({**snapshots, "traders": {
            **snapshots["traders"], "positions": [{"symbol": "AAPL", "qty": 1}]
        }})
    with pytest.raises(lr.LongRunStop, match="AB_BASELINE_INVALID"):
        lr.validate_ab_startup_snapshots({**snapshots, "berkshire": {
            **snapshots["berkshire"], "equity": 90_000
        }})


def test_selected_local_openai_adapter_keeps_supported_model_params():
    from tradingagents.llm_clients.factory import create_llm_client
    llm = create_llm_client(
        "local_openai", "agnes-3.0-flash", "http://localhost:1234/v1",
        api_key="offline-test", model_role="deep", max_output_tokens=16000,
        timeout=9,
    ).get_llm()
    assert llm.max_tokens == 16000
    assert llm.request_timeout == 9


def _run_interleaved_ab(
    tmp_path,
    *,
    events=None,
    clock=None,
    deadline=None,
    analysis_duration=timedelta(0),
    execution_duration=timedelta(0),
    extras=None,
    preanalyzed_traders=(),
    crash_at=None,
    invalid_arm=None,
    mismatched_evidence=False,
):
    """Exercise the real A/B coordinator and symbol state machine with fakes."""
    root, config, selection, _initial, _deps = _ab_round_setup(tmp_path)
    initial = _plan(selection, extras=(extras or {}).get("traders", ()), cached=False)
    events = events if events is not None else []
    clock = clock or [datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)]
    extras = extras or {"traders": [], "berkshire": []}
    applied = {}
    runtime_switches = []
    crash_state = {"pending": crash_at is not None}

    def apply_runtime(runtime):
        applied.clear()
        applied.update(runtime)
        runtime_switches.append(runtime["analysis_backend"])

    def prepare_arm(**kwargs):
        backend = kwargs["runtime"]["analysis_backend"]
        apply_runtime(kwargs["runtime"])
        plan = kwargs["long_cfg"]["_ab_screening_plan"]
        arm_id = kwargs["run_id"]
        journal = lr.load_round_journal(arm_id, SESSION)
        if journal is None:
            journal = lr.new_round_journal(SESSION, list(plan.deep_analysis_set))
            journal["screening"] = {
                "selection_hash": plan.selection_hash,
                "top20": [{"symbol": row["symbol"], "rank": row["rank"]}
                          for row in plan.top20],
                "deep_analysis_set": list(plan.deep_analysis_set),
            }
            for symbol in preanalyzed_traders:
                if backend == "traders" and symbol in journal["symbols"]:
                    journal["symbols"][symbol].update({
                        "status": lr.SYMBOL_ANALYZED,
                        "evidence_packet_sha256": "e" * 64,
                        "trade_intent": {"symbol": symbol, "action": "BUY",
                                         "target_position": "LONG"},
                        "signal": "BUY",
                    })
        journal["status"] = "RUNNING"
        lr.save_round_journal(arm_id, journal)

        class Service:
            pass

        service = Service()
        service.backend = backend
        service.execution_db_path = kwargs["runtime"]["execution_db_path"]

        class Graph:
            def propagate(self, symbol, _trade_date):
                events.append(("analysis_started", backend, symbol, clock[0]))
                clock[0] += analysis_duration
                events.append(("analysis_finished", backend, symbol, clock[0]))
                if backend == invalid_arm and symbol == "T00":
                    return ({"final_trade_intent": None}, "HOLD")
                return ({"final_trade_intent": {
                    "symbol": symbol, "action": "BUY", "target_position": "LONG",
                }}, "BUY")

        graph_factory = lambda _graph_config: Graph()
        long_cfg = {key: value for key, value in kwargs["long_cfg"].items()
                    if not key.startswith("_ab_")}
        prepared = lr._PreparedDailyRound(
            run_id=arm_id, session_date=SESSION, long_cfg=long_cfg,
            runtime=kwargs["runtime"], schedule_info=kwargs.get("schedule_info"),
            deps=kwargs["deps"], ends_at=kwargs.get("ends_at"), journal=journal,
            service=service, graph_factory=graph_factory,
            broker_factory=lambda: None,
            recovery_can_submit=lambda: deadline is None or clock[0] < deadline,
            session_submit_allowed=lambda: deadline is None or clock[0] < deadline,
        )
        return prepared

    def make_plan(runtime, _selection, **_kwargs):
        backend = runtime["analysis_backend"]
        return _plan(selection, extras=extras.get(backend, ()), cached=True)

    def execute(_deps, service, symbol, _intent, _notional, *, run_id, **kwargs):
        backend = service.backend
        expected_profile = "A" if backend == "traders" else "B"
        assert applied.get("_alpaca_account_profile") == expected_profile
        assert applied.get("execution_db_path") == service.execution_db_path
        assert run_id.endswith(f"-{backend}")
        allowed = kwargs["can_submit"]()
        events.append(("execution", backend, symbol, clock[0], allowed,
                       service.execution_db_path))
        if not allowed:
            return {"success": False, "entry_gate_blocked": True,
                    "broker_attempted": False, "broker_calls": 0}
        clock[0] += execution_duration
        return {"success": True, "broker_attempted": True, "broker_calls": 1}

    def finish(prepared, _stop_reason, *, reapply_runtime=False):
        if reapply_runtime:
            apply_runtime(prepared.runtime)
        prepared.journal["status"] = "COMPLETED"
        lr.save_round_journal(prepared.run_id, prepared.journal)
        return prepared.journal

    def maybe_crash(prepared, **kwargs):
        if (
            crash_state["pending"]
            and prepared.run_id.endswith(f"-{crash_at[0]}")
            and kwargs.get("phase") == crash_at[1]
            and kwargs.get("symbols") == [crash_at[2]]
        ):
            crash_state["pending"] = False
            raise RuntimeError("simulated coordinator crash")
        return original_worker(prepared, **kwargs)

    original_worker = lr._run_prepared_round_symbols
    with ExitStack() as stack:
        stack.enter_context(patch.object(lr, "base_dir", return_value=tmp_path / "state"))
        stack.enter_context(patch.object(lr, "_apply_runtime_config", side_effect=apply_runtime))
        stack.enter_context(patch.object(lr, "_validate_long_run_execution_config"))
        stack.enter_context(patch.object(lr, "_build_graph_config", side_effect=lambda runtime, cfg, run_id=None: dict(runtime)))
        stack.enter_context(patch.object(lr, "_screening_with_audit_scope", return_value=initial))
        stack.enter_context(patch(
            "tradingagents.safety.get_safety_guard",
            return_value=SimpleNamespace(
                check_llm_budget=lambda: SimpleNamespace(allowed=True, reasons=[])
            ),
        ))
        stack.enter_context(patch(
            "tradingagents.screening.pipeline.prepare_screening_round_from_selection",
            side_effect=make_plan,
        ))
        stack.enter_context(patch.object(lr, "run_daily_round", side_effect=prepare_arm))
        stack.enter_context(patch.object(lr, "_finish_prepared_daily_round", side_effect=finish))
        stack.enter_context(patch.object(lr, "_execute_intent", side_effect=execute))
        stack.enter_context(patch("tradingagents.execution.service.validate_trade_intent",
                                  side_effect=lambda intent: (intent, None)))
        stack.enter_context(patch.object(
            lr, "utc_now_iso", side_effect=lambda: clock[0].isoformat()
        ))
        stack.enter_context(patch(
            "tradingagents.long_run_support.symbols._prepare_symbol_graph_config",
            side_effect=lambda graph_config, runtime, **_kwargs: (
                dict(graph_config),
                {"path": str(root / "shared" / "evidence" / SESSION / "T00" / "evidence_packet.json"),
                 "sha256": ("f" if mismatched_evidence and runtime["analysis_backend"] == "berkshire" else "e") * 64},
            ),
        ))
        if crash_at is not None:
            stack.enter_context(patch.object(
                lr, "_run_prepared_round_symbols", side_effect=maybe_crash
            ))
        result = lr.run_ab_daily_round(
            run_id="lr-ab-fixture", session_date=SESSION,
            long_cfg={"ab_results_root": str(root), "base_trade_notional_usd": 1000},
            runtime=config,
            deps=lr.LongRunDeps(now_fn=lambda: clock[0]),
            schedule_info={"effective_at": f"{SESSION}T11:00:00-04:00",
                           "effective_target": "11:00"},
            ends_at=clock[0] + timedelta(days=1),
        )
    return result, events, runtime_switches


def test_ab_round_screens_once_shares_hash_and_allows_different_signals(tmp_path):
    root, config, selection, initial, deps = _ab_round_setup(tmp_path)
    screen_calls = []
    arm_plans = {}

    def from_selection(_config, _selection, **_kwargs):
        backend = _config["analysis_backend"]
        extras = ["AAPL"] if backend == "traders" else ["MSFT"]
        plan = _plan(selection, extras=extras, cached=True)
        arm_plans[backend] = plan
        return plan

    with patch.object(lr, "_apply_runtime_config"), \
         patch.object(lr, "_validate_long_run_execution_config"), \
         patch.object(lr, "_screening_with_audit_scope", side_effect=lambda fn, *_a: screen_calls.append(fn) or initial), \
         patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(check_llm_budget=lambda: SimpleNamespace(allowed=True, reasons=[]))), \
         patch("tradingagents.screening.pipeline.prepare_screening_round_from_selection", side_effect=from_selection), \
         patch.object(lr, "run_daily_round", side_effect=_complete_arm):
        result = lr.run_ab_daily_round(
            run_id="lr-ab-fixture", session_date=SESSION,
            long_cfg={"ab_results_root": str(root), "base_trade_notional_usd": 1000},
            runtime=config, deps=deps,
            schedule_info={"effective_at": f"{SESSION}T11:00:00-04:00", "effective_target": "11:00"},
        )

    assert len(screen_calls) == 1
    assert result["status"] == "COMPLETED"
    assert result["shared_screening"]["status"] == "SCREENING_COMPLETE"
    assert result["arms"]["traders"]["selection_hash"] == result["arms"]["berkshire"]["selection_hash"]
    assert initial.deep_analysis_set[-1] == "AAPL"
    assert arm_plans["berkshire"].deep_analysis_set[-1] == "MSFT"
    assert lr.load_round_journal("lr-ab-fixture-traders", SESSION)["symbols"]["T00"]["signal"] == "BUY"
    assert lr.load_round_journal("lr-ab-fixture-berkshire", SESSION)["symbols"]["T00"]["signal"] == "HOLD"


def test_ab_interleaves_each_symbol_and_reapplies_account_runtime(tmp_path):
    result, events, runtime_switches = _run_interleaved_ab(tmp_path)

    assert result["status"] == "COMPLETED"
    assert [(event[0], event[1], event[2]) for event in events[:6]] == [
        ("analysis_started", "berkshire", "T00"),
        ("analysis_finished", "berkshire", "T00"),
        ("analysis_started", "traders", "T00"),
        ("analysis_finished", "traders", "T00"),
        ("execution", "berkshire", "T00"),
        ("execution", "traders", "T00"),
    ]
    # Preparation installs each arm; the first pair's order is deterministic.
    assert runtime_switches[4:8] == [
        "berkshire", "traders", "berkshire", "traders",
    ]
    assert events[4][5] != events[5][5]  # execution databases remain arm-local

    traders = json.loads((tmp_path / "state" / "runs" / "lr-ab-fixture-traders"
                          / "rounds" / f"{SESSION}.json").read_text())
    berkshire = json.loads((tmp_path / "state" / "runs" / "lr-ab-fixture-berkshire"
                            / "rounds" / f"{SESSION}.json").read_text())
    for arm in (traders, berkshire):
        entry = arm["symbols"]["T00"]
        assert entry["analysis_started_at"]
        assert entry["analysis_finished_at"]
        assert entry["execution_started_at"]
        assert entry["execution_finished_at"]
        assert entry["evidence_packet_sha256"] == "e" * 64
    assert traders["symbols"]["T00"]["evidence_packet_sha256"] == berkshire["symbols"]["T00"]["evidence_packet_sha256"]


def test_ab_resume_after_first_arm_execution_only_executes_second(tmp_path):
    events = []
    try:
        _run_interleaved_ab(
            tmp_path, events=events,
            crash_at=("traders", "execution", "T00"),
        )
    except RuntimeError as exc:
        assert "simulated coordinator crash" in str(exc)
    else:
        raise AssertionError("expected simulated crash after first-arm execution")

    before_resume = list(events)
    result, _, _ = _run_interleaved_ab(tmp_path, events=events)
    t00_after = [event for event in events[len(before_resume):] if event[2] == "T00"]
    assert result["status"] == "COMPLETED"
    assert not [event for event in t00_after if event[0].startswith("analysis_")]
    assert not [event for event in t00_after if event[0] == "execution" and event[1] == "berkshire"]
    assert len([event for event in t00_after if event[0] == "execution" and event[1] == "traders"]) == 1


def test_ab_resume_mid_analysis_pair_reuses_traders_intent(tmp_path):
    result, events, _ = _run_interleaved_ab(
        tmp_path, preanalyzed_traders=("T00",),
    )
    t00 = [event for event in events if event[2] == "T00"]
    assert result["status"] == "COMPLETED"
    assert [(event[0], event[1]) for event in t00] == [
        ("analysis_started", "berkshire"),
        ("analysis_finished", "berkshire"),
        ("execution", "berkshire"),
        ("execution", "traders"),
    ]


def test_ab_held_symbols_run_after_shared_pairs_in_their_own_arm(tmp_path):
    result, events, _ = _run_interleaved_ab(
        tmp_path, extras={"traders": ["AAPL"], "berkshire": ["NVDA"]},
    )
    top20_events = [event for event in events if event[2].startswith("T")]
    held_events = [event for event in events if event[2] in {"AAPL", "NVDA"}]
    assert result["status"] == "COMPLETED"
    assert held_events
    assert min(events.index(event) for event in held_events) > max(
        events.index(event) for event in top20_events
    )
    assert {(event[1], event[2]) for event in held_events} == {
        ("traders", "AAPL"), ("berkshire", "NVDA"),
    }


def test_ab_fake_clock_keeps_both_arms_inside_submission_window(tmp_path):
    clock = [datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)]
    deadline = clock[0] + timedelta(minutes=10)
    result, events, _ = _run_interleaved_ab(
        tmp_path,
        clock=clock,
        deadline=deadline,
        analysis_duration=timedelta(minutes=2),
        execution_duration=timedelta(seconds=15),
    )

    berkshire_posts = [
        event for event in events
        if event[0] == "execution" and event[1] == "berkshire" and event[4]
    ]
    assert result["status"] == "COMPLETED"
    assert len(berkshire_posts) == 2
    assert all(event[3] < deadline for event in berkshire_posts)
    # Berkshire starts each paired analysis before Traders has worked through
    # the portfolio, and its first execution lands inside the 30-minute gate.
    first_berkshire_analysis = next(
        event for event in events if event[0] == "analysis_started" and event[1] == "berkshire"
    )
    assert first_berkshire_analysis[3] < deadline


def test_invalid_intent_blocks_both_arms_for_symbol(tmp_path):
    result, events, _ = _run_interleaved_ab(tmp_path, invalid_arm="traders")
    assert result["status"] == "COMPLETED"
    assert not any(event[0] == "execution" and event[2] == "T00" for event in events)
    assert result["pair_evidence"]["T00"]["status"] == "FAILED"
    counterpart = lr.read_json(tmp_path / "state" / "runs" / "lr-ab-fixture-berkshire"
                               / "rounds" / f"{SESSION}.json")
    assert counterpart["symbols"]["T00"]["status"] == lr.SYMBOL_DONE
    assert counterpart["symbols"]["T00"]["execution_result_summary"]["no_trade"]


def test_deadline_between_paired_analyses_blocks_unpaired_execution(tmp_path):
    now = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
    result, events, _ = _run_interleaved_ab(
        tmp_path, clock=[now], deadline=now + timedelta(seconds=30),
        analysis_duration=timedelta(minutes=1),
    )
    assert result["status"] == "COMPLETED"
    assert not any(event[0] == "execution" for event in events)
    assert result["pair_evidence"]["T00"]["status"] == "FAILED"


def test_pair_evidence_hash_mismatch_blocks_execution(tmp_path):
    events = []
    with pytest.raises(lr.LongRunStop, match="STATE_CORRUPT"):
        _run_interleaved_ab(tmp_path, events=events, mismatched_evidence=True)
    assert not any(event[0] == "execution" for event in events)


def test_resume_after_shared_screening_does_not_rescreen(tmp_path):
    root, config, selection, initial, deps = _ab_round_setup(tmp_path)
    screen_calls = []
    crashes = {"first": True}

    def run_arm(**kwargs):
        if crashes["first"]:
            crashes["first"] = False
            raise RuntimeError("simulated crash after screening artifact")
        return _complete_arm(**kwargs)

    with patch.object(lr, "_apply_runtime_config"), \
         patch.object(lr, "_validate_long_run_execution_config"), \
         patch.object(lr, "_screening_with_audit_scope", side_effect=lambda *_a, **_k: screen_calls.append(1) or initial), \
         patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(check_llm_budget=lambda: SimpleNamespace(allowed=True, reasons=[]))), \
         patch("tradingagents.screening.pipeline.prepare_screening_round_from_selection", side_effect=lambda *_a, **_k: _plan(selection)), \
         patch.object(lr, "run_daily_round", side_effect=run_arm):
        kwargs = dict(
            run_id="lr-ab-fixture", session_date=SESSION,
            long_cfg={"ab_results_root": str(root), "base_trade_notional_usd": 1000},
            runtime=config, deps=deps,
        )
        try:
            lr.run_ab_daily_round(**kwargs)
        except RuntimeError:
            pass
        else:
            raise AssertionError("first arm should simulate a crash")
        result = lr.run_ab_daily_round(**kwargs)

    assert len(screen_calls) == 1
    assert result["status"] == "COMPLETED"
    assert result["shared_screening"]["selection_hash"] == lr._ab_selection_hash(selection)


def test_resume_between_arms_does_not_repeat_completed_arm(tmp_path):
    root, config, selection, initial, deps = _ab_round_setup(tmp_path)
    screen_calls = []
    calls = {"traders": 0, "berkshire": 0}

    def run_arm(**kwargs):
        backend = kwargs["runtime"]["analysis_backend"]
        calls[backend] += 1
        if backend == "berkshire" and calls[backend] == 1:
            raise RuntimeError("simulated crash between arms")
        return _complete_arm(**kwargs)

    with patch.object(lr, "_apply_runtime_config"), \
         patch.object(lr, "_validate_long_run_execution_config"), \
         patch.object(lr, "_screening_with_audit_scope", side_effect=lambda *_a, **_k: screen_calls.append(1) or initial), \
         patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(check_llm_budget=lambda: SimpleNamespace(allowed=True, reasons=[]))), \
         patch("tradingagents.screening.pipeline.prepare_screening_round_from_selection", side_effect=lambda *_a, **_k: _plan(selection)), \
         patch.object(lr, "run_daily_round", side_effect=run_arm):
        kwargs = dict(
            run_id="lr-ab-fixture", session_date=SESSION,
            long_cfg={"ab_results_root": str(root), "base_trade_notional_usd": 1000},
            runtime=config, deps=deps,
        )
        try:
            lr.run_ab_daily_round(**kwargs)
        except RuntimeError:
            pass
        else:
            raise AssertionError("first Berkshire pass should simulate a crash")
        result = lr.run_ab_daily_round(**kwargs)

    assert result["status"] == "COMPLETED"
    assert calls == {"traders": 1, "berkshire": 2}
    assert len(screen_calls) == 1


@pytest.mark.parametrize("completed", [True, False])
def test_ab_report_compares_accounts_and_marks_unreliable_metrics_unavailable(tmp_path, completed):
    state = {
        "run_id": "lr-ab-report", "mode": "ab", "status": "COMPLETED",
        "started_at": "2026-09-23T00:00:00+00:00", "ends_at": "2026-10-23T00:00:00+00:00",
        "expected_sessions": [SESSION], "restart_count": 1,
        "arms": {"traders": {"account_ref": "account-a"}, "berkshire": {"account_ref": "account-b"}},
    }
    root_round = lr.new_round_journal(SESSION, [])
    root_round.update({
        "mode": "ab", "status": "COMPLETED" if completed else "STOPPED",
        "shared_screening": {"selection_hash": "f" * 64, "top20": ["AAPL", "MSFT"]},
        "screening": {"top20": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]} if completed else {},
    })
    lr.save_round_journal(state["run_id"], root_round)
    for backend, signal in (("traders", "BUY"), ("berkshire", "HOLD")):
        arm_id = lr._ab_arm_run_id(state["run_id"], backend)
        arm_round = lr.new_round_journal(SESSION, ["AAPL", "MSFT"])
        arm_round["status"] = "COMPLETED"
        arm_round["symbols"]["AAPL"].update({"status": "DONE", "signal": signal})
        # Shared holdings outside Top20 are account management, not paired inputs.
        arm_round["symbols"]["HELD"] = {"status": "DONE", "signal": signal}
        arm_round["symbols"]["AAPL"]["execution_started_at"] = (
            "2026-09-23T15:00:00+00:00" if backend == "traders"
            else "2026-09-23T15:00:02+00:00"
        )
        lr.save_round_journal(arm_id, arm_round)

    def fake_report(arm_state, _long_cfg, _runtime):
        equity = 100_000 if arm_state["run_id"].endswith("traders") else 101_000
        return {
            "run_id": arm_state["run_id"],
            "account": {"starting_equity": equity, "ending_equity": equity + 100,
                        "absolute_pl": 100, "total_return": 0.001,
                        "max_drawdown": -0.01, "daily_equity_series": [],
                        "ending_unrealized_pl": 50, "ending_positions": []},
            "decisions": {"signal_counts": {"BUY": 1}},
            "execution": {"broker_calls": 1},
            "execution_db": {"available": True, "orders_total": 1, "by_status": {"FILLED": 1}},
            "llm_operations": {"available": True, "totals": {"total_tokens": 10, "cost_usd": 0.1}},
            "safety": {},
        }

    with patch.object(lr, "aggregate_final_report", side_effect=fake_report):
        report = lr.aggregate_ab_final_report(
            state,
            {"ab_results_root": str(tmp_path / "ab"), "duration_calendar_days": 30},
            copy.deepcopy(DEFAULT_CONFIG),
        )

    assert report["shared_screening"]["selection_hashes"][SESSION] == "f" * 64
    assert report["shared_screening"]["top20_by_day"][SESSION] == ["AAPL", "MSFT"]
    assert report["signal_disagreement"]["shared_symbol_session_count"] == 1
    assert report["comparison"]["traders"]["account_ref"] == "account-a"
    assert report["comparison"]["berkshire"]["account_ref"] == "account-b"
    assert report["signal_disagreement"]["disagreements"] == [
        {"session": SESSION, "symbol": "AAPL", "traders": "BUY", "berkshire": "HOLD"}
    ]
    assert report["timing_fairness"]["paired_execution_count"] == 1
    assert report["timing_fairness"]["mean_execution_time_gap_seconds"] == 2
    assert report["timing_fairness"]["max_execution_time_gap_seconds"] == 2
    assert next(
        row for row in report["timing_fairness"]["by_symbol_session"]
        if row["symbol"] == "AAPL"
    )["execution_time_gap_seconds"] == 2
    assert report["metrics_unavailable"]["realized_pl"]


def test_ab_report_accounts_for_shared_screening_cost_once(tmp_path):
    root = tmp_path / "ab"
    runtime = dict(DEFAULT_CONFIG)
    traders = apply_ab_backend_runtime_paths(runtime, "traders", root)
    log_dir = Path(traders["results_dir"]) / "__SCREENING__" / "TradingAgentsStrategy_logs" / "runs"
    log_dir.mkdir(parents=True)
    for observation, tokens in (("ab-cost", 123), ("ab-cost-traders", 7), ("other-run", 999)):
        (log_dir / f"{observation}.json").write_text(json.dumps({
            "symbol": "__SCREENING__", "trade_date": SESSION,
            "metadata": {"long_run_observation_id": observation},
            "summary": {"total_llm_tokens": tokens}, "events": [],
        }))
    report = lr.aggregate_ab_final_report(
        {"run_id": "ab-cost", "expected_sessions": [],
         "started_at": "2026-09-23T00:00:00+00:00", "ends_at": "2026-09-24T00:00:00+00:00"},
        {"ab_results_root": str(root)}, runtime,
    )
    shared = report["shared_screening"]["llm_operations"]
    assert shared["available"]
    assert shared["totals"]["total_tokens"] == 123
    assert report["comparison"]["traders"]["llm_operations"]["totals"]["total_tokens"] == 7
    assert report["comparison"]["berkshire"]["llm_operations"]["totals"]["total_tokens"] == 0
    md_path, _ = lr.write_ab_final_report(report)
    assert "Shared LLM tokens: 123" in Path(md_path).read_text()
