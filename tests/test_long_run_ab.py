import copy
import hashlib
import json
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def _ab_round_setup(tmp_path):
    root = tmp_path / "ab-results" / "lr-ab-fixture"
    config = _screening_config(tmp_path)
    selection = _selection(config)
    initial = _plan(selection, extras=["AAPL"])
    deps = lr.LongRunDeps(screening_fn=lambda *_a, **_k: initial)
    return root, config, selection, initial, deps


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
        stack.enter_context(patch.object(
            lr, "utc_now_iso", side_effect=lambda: clock[0].isoformat()
        ))
        stack.enter_context(patch(
            "tradingagents.long_run_support.symbols._prepare_symbol_graph_config",
            side_effect=lambda graph_config, runtime, **_kwargs: (
                dict(graph_config),
                {"path": str(root / "shared" / "evidence" / SESSION / "T00" / "evidence_packet.json"),
                 "sha256": "e" * 64},
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
        ("analysis_started", "traders", "T00"),
        ("analysis_finished", "traders", "T00"),
        ("analysis_started", "berkshire", "T00"),
        ("analysis_finished", "berkshire", "T00"),
        ("execution", "traders", "T00"),
        ("execution", "berkshire", "T00"),
    ]
    # Preparation installs each arm; these later calls prove A→B→A→B
    # switching before the first pair's analysis and execution phases.
    assert runtime_switches[4:8] == [
        "traders", "berkshire", "traders", "berkshire",
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


def test_ab_resume_after_traders_execution_only_executes_berkshire(tmp_path):
    events = []
    try:
        _run_interleaved_ab(
            tmp_path, events=events,
            crash_at=("berkshire", "execution", "T00"),
        )
    except RuntimeError as exc:
        assert "simulated coordinator crash" in str(exc)
    else:
        raise AssertionError("expected simulated crash after Traders execution")

    before_resume = list(events)
    result, _, _ = _run_interleaved_ab(tmp_path, events=events)
    t00_after = [event for event in events[len(before_resume):] if event[2] == "T00"]
    assert result["status"] == "COMPLETED"
    assert not [event for event in t00_after if event[0].startswith("analysis_")]
    assert not [event for event in t00_after if event[0] == "execution" and event[1] == "traders"]
    assert len([event for event in t00_after if event[0] == "execution" and event[1] == "berkshire"]) == 1


def test_ab_resume_mid_analysis_pair_reuses_traders_intent(tmp_path):
    result, events, _ = _run_interleaved_ab(
        tmp_path, preanalyzed_traders=("T00",),
    )
    t00 = [event for event in events if event[2] == "T00"]
    assert result["status"] == "COMPLETED"
    assert [(event[0], event[1]) for event in t00] == [
        ("analysis_started", "berkshire"),
        ("analysis_finished", "berkshire"),
        ("execution", "traders"),
        ("execution", "berkshire"),
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


def test_ab_report_compares_accounts_and_marks_unreliable_metrics_unavailable(tmp_path):
    state = {
        "run_id": "lr-ab-report", "mode": "ab", "status": "COMPLETED",
        "started_at": "2026-09-23T00:00:00+00:00", "ends_at": "2026-10-23T00:00:00+00:00",
        "expected_sessions": [SESSION], "restart_count": 1,
        "arms": {"traders": {"account_ref": "account-a"}, "berkshire": {"account_ref": "account-b"}},
    }
    root_round = lr.new_round_journal(SESSION, [])
    root_round.update({
        "mode": "ab", "status": "COMPLETED",
        "shared_screening": {"selection_hash": "f" * 64},
        "screening": {"top20": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]},
    })
    lr.save_round_journal(state["run_id"], root_round)
    for backend, signal in (("traders", "BUY"), ("berkshire", "HOLD")):
        arm_id = lr._ab_arm_run_id(state["run_id"], backend)
        arm_round = lr.new_round_journal(SESSION, ["AAPL", "MSFT"])
        arm_round["status"] = "COMPLETED"
        arm_round["symbols"]["AAPL"].update({"status": "DONE", "signal": signal})
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
