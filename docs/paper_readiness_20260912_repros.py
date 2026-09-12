"""Offline defect evidence for the 2026-09-12 review at 56b86dc.

Passing assertions demonstrate defects, NOT corrected behavior. No production
code is patched on disk. Transports and all persistent state are isolated.
Run: .venv-p2/bin/python -m pytest docs/paper_readiness_20260912_repros.py -q -s
"""
import importlib.util
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import tradingagents.agents
from tradingagents.agents.schemas import RiskDecision, build_trade_intent_from_risk_decision
from tradingagents.execution.authority import BrokerQuote, capture_broker_snapshot
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import client_order_id_for

# Reuse the existing audited deterministic broker fixture (not its test cases).
_spec = importlib.util.spec_from_file_location(
    "review_20260912_fixture", Path(__file__).resolve().parents[1] / "tests/test_execution_safety_plan_a.py"
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
Broker, opening, now = _fixture.Broker, _fixture.opening, _fixture.now


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from tradingagents.dataflows import config as cfg
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.safety.guardrails import SafetyGuard
    import tradingagents.long_run as lr
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("TRADINGAGENTS_LONG_RUN_DIR", str(tmp_path / "long_run"))
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_DB", str(tmp_path / "execution.db"))
    monkeypatch.setattr(lr, "_stop_requested", False)
    config = {**DEFAULT_CONFIG, "auto_screening_enabled": False, "allow_shorts": False,
              "data_cache_dir": str(tmp_path / "cache"), "alerts_enabled": False,
              "results_dir": str(tmp_path / "results")}
    monkeypatch.setattr(cfg, "_config", config)
    guard = SafetyGuard(config=config, state_path=tmp_path / "safety.json",
                        kill_switch_path=tmp_path / "kill")
    monkeypatch.setattr("tradingagents.safety.get_safety_guard", lambda: guard)
    broker = Broker()
    service = ExecutionService(db_path=tmp_path / "execution.db", broker_factory=lambda: broker,
                               quote_factory=lambda s: BrokerQuote(s, 99.9, 100.1, now()))
    return NS(service=service, broker=broker, config=config, guard=guard, root=tmp_path)


def seed_pending(env, payload, *, quantity=10, decision_id="pending"):
    side = "sell" if payload["action"] == "SHORT" else "buy"
    env.service.store.ensure_account_binding("audit-fixture")
    _, rows, _ = env.service.store.create_outbox(
        decision_id=decision_id, run_id=None, symbol="AAPL", action=payload["action"],
        target_position=payload["target_position"], payload_json=json.dumps(payload),
        orders=[{"client_order_id": client_order_id_for(decision_id, "AAPL", side),
                 "symbol": "AAPL", "side": side, "quantity": quantity, "notional": None}],
    )
    return rows[0]


def close_intent():
    return build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment", current_position="LONG", allow_shorts=False,
        decision=RiskDecision(action="SELL", confidence="high", risk_rationale="exit",
                              required_controls="close"),
    ).model_dump(mode="json")


def test_N01_recovery_ignores_current_short_opt_out(isolated):
    e = isolated
    seed_pending(e, opening("SHORT"))
    assert e.config["allow_shorts"] is False
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty < 0
    print("N01 recovery posts SHORT despite current allow_shorts=False:", e.broker.qty, result)


def test_N02_recovery_crosses_changed_position_and_overprotects(isolated):
    e = isolated
    seed_pending(e, opening("BUY", "NEUTRAL"))
    e.broker.qty = -4  # Fresh account state differs from the persisted decision.
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == 5
    assert e.broker.orders[0].legs[0].qty == 9
    assert result["success"], result
    print("N02 stale NEUTRAL BUY reverses SHORT -4 to LONG +5, stop qty=9, recovery=CLEAN")


def test_control_engaged_kill_switch_preserves_protection(isolated):
    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    e.guard.kill_switch_path.write_text("operator halt")
    result = e.service.execute(trade_intent=close_intent(), dollar_amount=1000)
    assert len(e.broker.cancels) == 0, result
    assert e.broker.qty == 9 and len(e.broker.submits) == 1
    assert all(leg.status == "new" for leg in e.broker.orders[0].legs)
    print("CONTROL: kill switch already engaged correctly preserves existing protection")


def test_N04_kill_switch_during_final_clock_get_does_not_block_post(isolated):
    e = isolated
    def clock_then_halt():
        e.guard.kill_switch_path.write_text("halt while final GET is running")
        return NS(is_open=True)
    e.broker.get_clock = clock_then_halt
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert e.guard.kill_switch_active()
    assert len(e.broker.submits) == 1 and e.broker.qty == 9, result
    print("N04 final clock GET engages kill switch; opening still POSTed")


def test_N05_terminal_unfilled_order_still_demands_quote(isolated):
    e = isolated
    e.broker.orders.append(NS(id="old", client_order_id="manual-old", symbol="DELISTED",
        side="buy", status="canceled", qty=10, filled_qty=0, filled_avg_price=None,
        updated_at=now(), legs=[], notional=None))
    original = e.service._quote_factory
    def quote(symbol):
        if symbol == "DELISTED":
            raise ValueError("no current quote for delisted symbol")
        return original(symbol)
    e.service._quote_factory = quote
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert len(e.broker.submits) == 0
    assert "DELISTED" in result.get("error", ""), result
    print("N05 canceled historical order blocks unrelated AAPL opening:", result["error"])


def test_N06_bracket_siblings_count_as_extra_opening_exposure(isolated):
    from tradingagents.risk.exposure import outstanding_increasing_notional
    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    snap = capture_broker_snapshot(e.broker)
    amount, complete = outstanding_increasing_notional(snap, reference_prices={"AAPL": 100})
    assert amount == 900 and complete
    print("N06 position=$900 plus linked stop/target => conservatively counts $900 increasing orders")


@pytest.mark.parametrize("control", ["stop", "window", "recovery_stop"])
def test_N03_stop_or_window_during_final_get_still_posts(isolated, monkeypatch, control):
    import tradingagents.long_run as lr
    from zoneinfo import ZoneInfo
    from tradingagents.screening.selection_store import SelectionStore
    e = isolated
    stamp = now()
    clock_time = [stamp]
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime = {**e.config, "auto_screening_enabled": True,
               "calendar_rows": [{"date": day, "open": "09:30", "close": "16:00"}]}
    # The current selection is an injected external fact. The gate itself runs.
    monkeypatch.setattr(SelectionStore, "load_valid", lambda *a, **k: {"top20": [{"symbol": "AAPL"}]})
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **k: 1.0)
    monkeypatch.setattr("tradingagents.portfolio.adjust_new_position_notional", lambda s, a, amount, **k: amount)
    def final_clock():
        if control == "window":
            clock_time[0] = stamp + timedelta(minutes=2)
        else:
            lr._stop_requested = True
        return NS(is_open=True)
    e.broker.get_clock = final_clock
    deps = lr.LongRunDeps(execution_service_factory=lambda: e.service,
        broker_client_factory=lambda: e.broker, now_fn=lambda: clock_time[0],
        screening_fn=Mock(side_effect=AssertionError("no new LLM work on execution-only resume")))
    if control == "recovery_stop":
        seed_pending(e, opening())
    else:
        journal = lr.new_round_journal(day, ["AAPL"])
        journal["status"] = "RUNNING"
        journal["screening"] = {"cached": True}
        journal["symbols"]["AAPL"].update(status=lr.SYMBOL_ANALYZED, trade_intent=opening())
        lr.save_round_journal("observation", journal)
    lr.run_daily_round(run_id="observation", session_date=day, long_cfg={"base_trade_notional_usd": 1000},
                       runtime=runtime, deps=deps, ends_at=stamp+timedelta(minutes=1))
    assert len(e.broker.submits) == 1 and e.broker.qty == 9
    assert lr._control_stop_reason(deps, stamp+timedelta(minutes=1)) is not None
    print(f"N03 {control}: control revoked during broker clock GET, opening POST=1")


def test_N07_filled_parent_without_any_protective_children_is_clean(isolated):
    e = isolated
    submit = e.broker.submit_order
    def accepted_but_no_child_facts(request):
        parent = submit(request)
        e.broker.orders = [parent]
        parent.legs = []
        return parent
    e.broker.submit_order = accepted_but_no_child_facts
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    recovery = e.service.startup_recover()
    assert e.broker.qty == 9
    assert len(e.service.store.list_all_orders()) == 1
    assert result["account_execution_state"] == "CLEAN" and recovery["success"]
    print("N07 filled entry, zero child facts ever observed, execute+recovery both CLEAN")


def test_N08_daily_loss_block_does_not_stop_observation(isolated):
    import tradingagents.long_run as lr
    e = isolated
    e.broker.get_account = lambda: NS(id="audit-fixture", equity=89000, last_equity=100000,
                                    cash=89000, buying_power=89000)
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result.get("safety_blocked") and "Daily loss" in result.get("error", ""), result
    lr._check_execution_hard_stop("AAPL", result)  # no LongRunStop raised
    assert not e.broker.submits
    print("N08 daily loss=-11%, order correctly blocked, observation hard-stop ignores result")


def test_N09_schema_accepts_contradictory_action_and_broker_side(isolated):
    from tradingagents.execution.service import validate_trade_intent
    e = isolated
    payload = opening("SHORT")
    payload["action"] = "BUY"
    payload["risk_controls"]["take_profit_price"] = None
    payload["risk_controls"]["take_profit"] = None
    validated, error = validate_trade_intent(payload)
    assert error is None
    result = e.service.execute(trade_intent=validated, dollar_amount=1000, allow_shorts=False)
    assert len(e.broker.submits) == 1 and e.broker.qty == -9, result
    print("N09 action=BUY but target/planned side=SHORT passes strict boundary and opens SHORT with opt-out")


def test_N10_auto_trade_never_supplies_portfolio_gather_symbol(isolated, monkeypatch, capsys):
    from tradingagents.execution.auto_trade import execute_auto_trade
    e = isolated
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **k: 1.0)
    broker_factory = Mock(side_effect=AssertionError("would be called by a correctly bound gather"))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client", broker_factory)
    sink = NS(execute=Mock(return_value={"success": True}))
    execute_auto_trade(ticker="AAPL", trade_intent=opening(), base_trade_notional_usd=1000,
                       allow_shorts=False, config=e.config, execution_service=sink)
    output = capsys.readouterr().out
    assert "missing 1 required positional argument: 'symbol'" in output
    assert broker_factory.call_count == 0
    assert sink.execute.call_args.kwargs["dollar_amount"] == 1000
    print("N10 portfolio hook always raises missing-symbol TypeError and silently keeps full amount")


@pytest.mark.parametrize("response_body", ['{"id":', '{"id":"unexpected-only-field"}'])
def test_N11_real_sdk_response_decode_failure_is_marked_rejected(isolated, response_body):
    from alpaca.trading.client import TradingClient
    from requests import Response
    e = isolated
    sdk = TradingClient("offline-key", "offline-secret", paper=True)
    response = Response()
    response.status_code = 200
    response._content = response_body.encode()
    sdk._session.request = Mock(return_value=response)
    e.broker.submit_order = sdk.submit_order
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert sdk._session.request.call_count == 1
    assert result["orders"][0]["status"] == "REJECTED", result
    assert result["account_execution_state"] == "CLEAN" and not result.get("has_unknown"), result
    print("N11 actual Alpaca SDK gets HTTP 200 unreadable/invalid body; outcome=REJECTED, account=CLEAN")


def test_N12_new_cli_runner_overwrites_active_state_before_lock(isolated, monkeypatch):
    from contextlib import contextmanager
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr
    e = isolated
    cfg = lr.default_long_run_config()
    cfg.update(base_trade_notional_usd=1000, analysis_provider="openai", analysis_model="fixture",
               decision_provider="openai", decision_model="fixture",
               screening_provider="openai", screening_model="fixture")
    monkeypatch.setattr(cli, "collect_long_run_config", lambda _: (cfg, {}))
    monkeypatch.setattr(lr, "run_preflight", lambda *a: {"ok": True})
    monkeypatch.setattr(lr, "run_post_authorization_recovery", lambda *a: {"snapshot": {}})
    monkeypatch.setattr(lr, "fetch_session_dates", lambda *a, **k: [now().date()])
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    def competing_runner_starts_during_setup(_):
        # Both commands originally observed no active.json. The first has now
        # started and holds the runner lock while this command is still in setup.
        lr.save_active_state({"run_id": "already-running-observation", "status": "RUNNING"})
        return cfg, {}
    monkeypatch.setattr(cli, "collect_long_run_config", competing_runner_starts_during_setup)
    @contextmanager
    def busy():
        raise lr.RunnerLockBusy("another runner holds the lock")
        yield
    monkeypatch.setattr(lr, "runner_lock", busy)
    result = CliRunner().invoke(cli.app, ["long-run"])
    saved = json.loads(lr.active_path().read_text())
    assert result.exit_code != 0 and isinstance(result.exception, lr.RunnerLockBusy)
    assert saved["run_id"] != "already-running-observation"
    assert saved["status"] == "RUNNING"
    print("N12 second runner fails lock after replacing first runner's active.json with a new RUNNING run")


def test_N13_checkpoint_restart_reexecutes_completed_nodes(isolated):
    from typing import TypedDict
    from langgraph.graph import StateGraph, START, END
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.graph.checkpointer import has_checkpoint
    e = isolated
    class State(TypedDict, total=False):
        value: int
        final_trade_decision: str
        final_trade_intent: dict
    calls = {"first": 0, "second": 0}
    def first(state):
        calls["first"] += 1
        return {"value": state["value"] + 1}
    def second(state):
        calls["second"] += 1
        if calls["second"] == 1:
            raise RuntimeError("injected failure after first node is checkpointed")
        return {"final_trade_decision": "HOLD", "final_trade_intent": None}
    workflow = StateGraph(State)
    workflow.add_node("first", first)
    workflow.add_node("second", second)
    workflow.add_edge(START, "first")
    workflow.add_edge("first", "second")
    workflow.add_edge("second", END)
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**e.config, "checkpoint_enabled": True}
    graph.workflow = workflow
    graph.debug = False
    graph.propagator = NS(create_initial_state=lambda *a: {"value": 0}, get_graph_args=lambda: {"config": {}})
    graph._resolve_memory_log_outcomes = lambda *a: None
    graph._log_state = lambda *a: None
    graph.process_signal = lambda *a: "HOLD"
    graph.memory_log = NS(store_decision=lambda **k: None)
    day = now().date().isoformat()
    with pytest.raises(RuntimeError, match="injected failure"):
        graph.propagate("AAPL", day)
    assert calls == {"first": 1, "second": 1}
    assert has_checkpoint(graph.config["data_cache_dir"], "AAPL", day)
    graph.propagate("AAPL", day)
    assert calls == {"first": 2, "second": 2}
    print("N13 real LangGraph SQLite checkpoint exists; propagate restart reruns completed first node")


def test_N14_webui_broker_outage_is_rendered_as_empty_portfolio(isolated, monkeypatch):
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils
    from webui.components.alpaca_account import render_positions_table
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
                        Mock(side_effect=RuntimeError("broker unavailable")))
    positions = render_positions_table()
    account = AlpacaUtils.get_account_info()
    orders = AlpacaUtils.get_recent_orders_page()
    assert "Your portfolio is currently empty" in str(positions)
    assert account["cash"] == 0 and account["buying_power"] == 0
    assert orders["orders"] == [] and orders["total_orders"] == 0
    print("N14 broker outage => UI claims empty portfolio, cash=$0, zero orders")


def test_N15_recovery_posts_absent_from_observation_execution_tally(isolated, monkeypatch):
    import tradingagents.long_run as lr
    from zoneinfo import ZoneInfo
    from tradingagents.screening.selection_store import SelectionStore
    e = isolated
    stamp = now()
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime = {**e.config, "auto_screening_enabled": True,
               "calendar_rows": [{"date": day, "open": "09:30", "close": "16:00"}]}
    monkeypatch.setattr(SelectionStore, "load_valid", lambda *a, **k: {"top20": [{"symbol": "AAPL"}]})
    seed_pending(e, opening())
    journal = lr.new_round_journal(day, [])
    journal["status"] = "RUNNING"
    journal["screening"] = {"cached": True}
    lr.save_round_journal("observation", journal)
    deps = lr.LongRunDeps(execution_service_factory=lambda: e.service,
        broker_client_factory=lambda: e.broker, now_fn=lambda: stamp,
        screening_fn=Mock(side_effect=AssertionError("no new LLM work")))
    cfg = {"base_trade_notional_usd": 1000, "duration_calendar_days": 30}
    lr.run_daily_round(run_id="observation", session_date=day, long_cfg=cfg,
                       runtime=runtime, deps=deps, ends_at=stamp+timedelta(days=1))
    report = lr.aggregate_final_report({"run_id": "observation", "started_at": stamp.isoformat(),
        "ends_at": (stamp+timedelta(days=1)).isoformat(), "expected_sessions": [day], "status": "RUNNING"}, cfg, runtime)
    assert len(e.broker.submits) == 1 and e.broker.qty == 9
    assert report["execution"]["broker_calls"] == 0
    assert report["execution"]["submitted_symbols"] == 0
    assert report["execution_db"]["orders_total"] == 3
    print("N15 recovery opens 9 shares; report execution broker_calls=0, submitted_symbols=0 (ledger has 3 rows)")


def test_N16_technical_brief_accepts_old_bars_as_current(isolated, monkeypatch):
    import pandas as pd
    from tradingagents.dataflows.technical_brief import build_technical_brief
    old = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=220, tz="UTC"),
                        "open": [100+i*0.1 for i in range(220)],
                        "high": [101+i*0.1 for i in range(220)],
                        "low": [99+i*0.1 for i in range(220)],
                        "close": [100+i*0.1 for i in range(220)], "volume": [1000000]*220})
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        lambda *a, **k: old.copy())
    brief = build_technical_brief("AAPL", now().date().isoformat())
    assert len(brief.timeframes) == 3
    assert brief.raw_prices["last_close"] == 121.9
    assert str(brief.generated_at).startswith(str(now().year))
    assert "2024" not in brief.model_dump_json()
    print("N16 2024 bars accepted into 3 technical timeframes, current generated_at, no source date/stale marker")
