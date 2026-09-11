"""Offline evidence for the 2026-09-11 paper-readiness review.

Run: .venv-p2/bin/python -m pytest docs/paper_readiness_20260911_repros.py -q -s

These assertions demonstrate the CURRENT DEFECTS, rather than asserting the
desired fixed behavior. A passing test here is evidence of the named defect.
They are deliberately outside tests/. No real broker, LLM, or network calls.
"""
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import tradingagents.agents
from tradingagents.agents.schemas import (
    EntryPolicy, RiskDecision, build_trade_intent_from_risk_decision,
)
from tradingagents.execution.authority import (
    BrokerOrder, BrokerPosition, BrokerQuote, BrokerSnapshot,
    capture_broker_snapshot,
)
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import client_order_id_for


def now():
    return datetime.now(timezone.utc)


def opening(action="BUY", current="NEUTRAL", *, stamp=None):
    stamp = stamp or now()
    short = action == "SHORT"
    decision = RiskDecision(
        action=action, confidence="high", risk_rationale="fixture", required_controls="stop",
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


class Broker:
    def __init__(self):
        self.qty = 0
        self.orders = []
        self.submits = []
        self.cancels = []

    def get_account(self):
        return NS(id="audit-fixture", equity=100000, last_equity=100000,
                  cash=100000, buying_power=100000)

    def get_all_positions(self):
        return [NS(symbol="AAPL", qty=self.qty, market_value=self.qty * 100)] if self.qty else []

    def get_orders(self, request):
        if str(getattr(request.status, "value", request.status)) == "open":
            return [o for o in self.orders if o.status not in {"filled", "canceled", "expired", "rejected"}]
        return list(reversed(self.orders))[:500]

    def get_order_by_id(self, order_id, filter=None):
        return next(o for o in self.orders if o.id == order_id)

    def get_order_by_client_id(self, client_id):
        return next((o for o in self.orders if o.client_order_id == client_id), None)

    def submit_order(self, request):
        self.submits.append(request)
        qty = float(request.qty)
        side = request.side.value
        self.qty += qty if side == "buy" else -qty
        order = NS(id=f"parent-{len(self.submits)}", client_order_id=request.client_order_id,
                   symbol="AAPL", side=side, qty=qty, filled_qty=qty, filled_avg_price=100,
                   status="filled", updated_at=now(), legs=[], notional=None)
        self.orders.append(order)
        if request.stop_loss:
            for kind in (["stop", "target"] if request.take_profit else ["stop"]):
                child = NS(id=f"{kind}-{len(self.submits)}",
                           client_order_id=f"broker-{kind}-{len(self.submits)}", symbol="AAPL",
                           side="sell" if side == "buy" else "buy", qty=qty, filled_qty=0,
                           filled_avg_price=None, status="new", updated_at=now(), legs=[], notional=None)
                order.legs.append(child)
                self.orders.append(child)
        return order

    def cancel_order_by_id(self, order_id):
        self.cancels.append(order_id)
        child = self.get_order_by_id(order_id)
        if child.status == "canceled":
            raise RuntimeError("422 order is already canceled")
        # Alpaca bracket cancellation also cancels the other live child.
        parent = next((o for o in self.orders if child in o.legs), None)
        for leg in (parent.legs if parent else [child]):
            leg.status = "canceled"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tradingagents.dataflows import config as cfg
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.long_run as lr
    monkeypatch.setattr(cfg, "_config", {**DEFAULT_CONFIG, "auto_screening_enabled": False,
                                       "data_cache_dir": str(tmp_path), "alerts_enabled": False})
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("TRADINGAGENTS_LONG_RUN_DIR", str(tmp_path / "long-run"))
    monkeypatch.setattr(lr, "_stop_requested", False)
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network forbidden")))
    guard = NS(enabled=False, check_order=lambda *a, **k: NS(allowed=True, reasons=[]),
               check_llm_budget=lambda: NS(allowed=True, reasons=[]), status=lambda: {})
    monkeypatch.setattr("tradingagents.safety.get_safety_guard", lambda: guard)
    broker = Broker()
    service = ExecutionService(db_path=tmp_path / "execution.db", broker_factory=lambda: broker,
        quote_factory=lambda s: BrokerQuote(s, 99.9, 100.1, now()))
    service._quarantine_rejection = lambda symbol: None
    return service, broker


def seed_pending(service, data, *, decision_id="pending", quantity=10):
    service.store.ensure_account_binding("audit-fixture")
    side = "sell" if data["action"] == "SHORT" else "buy"
    _, rows, _ = service.store.create_outbox(
        decision_id=decision_id, run_id=None, symbol="AAPL", action=data["action"],
        target_position=data["target_position"], payload_json=json.dumps(data),
        orders=[{"client_order_id": client_order_id_for(decision_id, "AAPL", side),
                 "symbol": "AAPL", "side": side, "quantity": quantity, "notional": None}],
    )
    return rows[0]


def test_R01_long_run_recovery_uses_manual_global_config(env, monkeypatch):
    import tradingagents.long_run as lr
    from tradingagents.dataflows.config import get_config
    service, broker = env
    seed_pending(service, opening())
    seen = []
    def stopped_screening(runtime, refresh=False):
        seen.append((runtime["auto_screening_enabled"], get_config()["auto_screening_enabled"], len(broker.submits)))
        return NS(stopped=True, stop_reason_text=lambda: "fixture stops after recovery")
    deps = lr.LongRunDeps(execution_service_factory=lambda: service,
        broker_client_factory=lambda: broker, screening_fn=stopped_screening)
    with pytest.raises(lr.LongRunStop, match="SCREENING_STOPPED"):
        lr.run_daily_round(run_id="obs", session_date=now().date().isoformat(),
            long_cfg={"base_trade_notional_usd": 1000}, runtime={"auto_screening_enabled": True,
            "results_dir": str(Path(service.db_path).parent / "results")}, deps=deps)
    assert seen == [(True, False, 1)], seen
    print("R01 runtime screening=True, execution global=False, broker POST before screening=1")


@pytest.mark.parametrize("control", ["stop", "expired"])
def test_R02_recovery_precedes_round_stop_and_window_check(env, monkeypatch, control):
    import tradingagents.long_run as lr
    service, broker = env
    seed_pending(service, opening())
    if control == "stop":
        monkeypatch.setattr(lr, "_stop_requested", True)
    deps = lr.LongRunDeps(execution_service_factory=lambda: service,
        broker_client_factory=lambda: broker, screening_fn=Mock(side_effect=AssertionError("no screening")))
    lr.run_daily_round(run_id="obs", session_date=now().date().isoformat(),
        long_cfg={"base_trade_notional_usd": 1000}, runtime={}, deps=deps,
        ends_at=now() - timedelta(seconds=1) if control == "expired" else None)
    assert len(broker.submits) == 1
    print(f"R02 {control} already active, recovery nevertheless POSTed 1 opening order")


def test_R03_bracket_cancel_cascade_aborts_liquidation(env):
    service, broker = env
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result["success"] and not result.get("paused"), result
    result = service.liquidate("AAPL")
    assert not result["success"] and "already canceled" in result["error"], result
    assert broker.qty > 0 and all(o.status == "canceled" for o in broker.orders[0].legs)
    assert len(broker.submits) == 1  # only entry, no close POST
    assert result["broker_calls"] == 0  # false mutation count despite two DELETE attempts
    print("R03", {"remaining_qty": broker.qty, "canceled_children": broker.cancels,
                  "close_posts": len(broker.submits)-1, "reported_broker_calls": result["broker_calls"]})


def test_R04_protected_reversal_cannot_execute_its_close(env):
    service, broker = env
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result["success"] and not result.get("paused"), result
    reversal = opening("SHORT", "LONG")
    assert len(reversal["planned_actions"]) == 2
    result = service.execute(trade_intent=reversal, dollar_amount=1000, allow_shorts=True)
    assert not result["success"] and "no conflicting close order" in result["error"], result
    assert len(broker.submits) == 1 and not broker.cancels
    print("R04 valid reversal blocked by its own protective orders; no close submitted")


def test_R05_expired_quote_and_decision_can_reach_submit(env, monkeypatch):
    service, broker = env
    data = opening()
    future = now() + timedelta(hours=2)
    original = service.store.create_outbox
    def persist_then_delay(**kwargs):
        result = original(**kwargs)
        # Simulates a slow durable commit / process suspension after all preflights.
        monkeypatch.setattr("tradingagents.execution.authority.utc_now", lambda: future)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return future if tz else future.replace(tzinfo=None)
        monkeypatch.setattr("tradingagents.execution.policy.datetime", Later)
        return result
    monkeypatch.setattr(service.store, "create_outbox", persist_then_delay)
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert len(broker.submits) == 1, result
    from tradingagents.execution.policy import entry_check
    assert "expired" in entry_check(data)[1]
    print("R05 policy is expired at dispatch, but protected opening POST still occurs")


def test_R06_done_for_day_exposure_is_ignored():
    from tradingagents.risk.exposure import outstanding_increasing_notional
    order = BrokerOrder("b", "ta-b", "AAPL", "buy", "done_for_day", 100, 0, None, now(), None)
    snap = BrokerSnapshot(now(), "v", "fixture", 100000, 100000, 100000, 100000, (), (order,), (), 0)
    amount, complete = outstanding_increasing_notional(snap, reference_prices={"AAPL": 100})
    assert amount == 0 and complete
    print("R06 $10,000 live done_for_day order consumes $0 headroom")


def test_R07_server_error_is_terminal_rejection(env):
    from alpaca.common.exceptions import APIError
    from requests import HTTPError, Response
    service, broker = env
    response = Response()
    response.status_code = 500
    error = APIError('{"code":50010000,"message":"Internal Server Error"}',
                     HTTPError(response=response))
    broker.submit_order = Mock(side_effect=error)
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert broker.submit_order.call_count == 1
    assert result["orders"][0]["status"] == "REJECTED", result
    assert not result.get("paused") and not result.get("has_unknown"), result
    print("R07 HTTP 500 after submit => REJECTED, account CLEAN, no UNKNOWN recovery")


def test_R08_documented_quarantine_example_is_silently_dropped(tmp_path):
    from tradingagents.risk.corporate_actions import QuarantineStore
    store = QuarantineStore(tmp_path / "quarantine.json", initial_events=[{
        "symbol": "TSLA", "reason": "split", "ratio": "3:1",
        "effective_at": "2026-09-10T00:00:00+00:00", "source": "operator",
    }])
    assert not store.is_quarantined("TSLA", now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    print("R08 default_config documented split example => no quarantine")


def test_R09_missing_protective_orders_can_be_clean(env):
    service, broker = env
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result["success"] and not result.get("paused"), result
    # Broker/exchange cancels both children while filled exposure remains.
    for child in broker.orders[0].legs:
        child.status = "canceled"
    result = service.startup_recover()
    assert result["success"] and result["account_execution_state"] == "CLEAN", result
    assert broker.qty > 0
    print("R09 parent filled, all protective children canceled, remaining exposure still CLEAN")


def test_R10_safety_state_lost_update_between_instances(tmp_path):
    from tradingagents.safety.guardrails import SafetyGuard
    path = tmp_path / "state.json"
    a = SafetyGuard(state_path=path, kill_switch_path=tmp_path / "kill")
    b = SafetyGuard(state_path=path, kill_switch_path=tmp_path / "kill")
    a.record_llm_tokens(80, when="2026-09-11")
    b.record_llm_tokens(30, when="2026-09-11")
    actual = json.loads(path.read_text())["llm_tokens"]["2026-09-11"]
    assert actual == 30
    print("R10 two process-equivalent guard instances report 110 tokens, persist only 30")


def ui_fixture(monkeypatch):
    import webui.components.analysis as ui
    state = {"agent_statuses": {"market": "idle"}, "current_reports": {}}
    app = NS(run_generation=0, stop_requested=False, trade_enabled=True, trade_amount=1000,
             provider_stop_reason=None, analysis_queue=["NEW-RUN"], tool_calls_log=[],
             llm_calls_count=0, tool_calls_count=0, get_state=lambda s: state,
             process_chunk_updates=Mock(), update_agent_status=Mock())
    final = {"final_trade_intent": opening(), "final_trade_decision": "BUY", "trading_mode": "investment"}
    graph = Mock()
    graph._graph_args_for_run.return_value = {"config": {}}
    compiled = Mock()
    compiled.stream.return_value = iter([final])
    graph._graph_for_run.return_value = (compiled, None)
    monkeypatch.setattr(ui, "app_state", app)
    monkeypatch.setattr(ui, "TradingAgentsGraph", Mock(return_value=graph))
    monkeypatch.setattr(ui, "get_run_audit_logger", Mock(return_value=Mock()))
    monkeypatch.setattr(ui, "create_chart", Mock(return_value=None))
    execute = Mock()
    monkeypatch.setattr(ui, "execute_trade_after_analysis", execute)
    return ui, app, compiled, execute


def test_R11_stop_start_during_initial_chart_revives_old_analysis(env, monkeypatch):
    ui, app, compiled, execute = ui_fixture(monkeypatch)
    count = 0
    def chart(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            app.run_generation += 1  # operator Stop
            app.stop_requested = False  # operator's new Start
        return None
    monkeypatch.setattr(ui, "create_chart", chart)
    ui.start_analysis("AAPL", True, False, False, False, False,
                      "Shallow", False, "fixture", "fixture")
    assert execute.call_count == 1
    print("R11 old start_analysis passes slow chart after Stop/Start and dispatches a trade")


def test_R12_stale_provider_failure_clears_new_run_queue(env, monkeypatch):
    ui, app, compiled, execute = ui_fixture(monkeypatch)
    def stream(*args, **kwargs):
        app.run_generation += 1
        app.stop_requested = False
        app.analysis_queue = ["NEW-RUN"]
        raise ui.ProviderFailure(role="analysis", provider="fixture", model="fixture",
            attempts=4, category="transient", detail="old request timed out after new Start")
    compiled.stream.side_effect = stream
    ui.run_analysis("AAPL", ["market"], 1, False, "fixture", "fixture")
    assert app.analysis_queue == [] and app.provider_stop_reason
    assert execute.call_count == 0
    print("R12 old generation ProviderFailure empties the new generation's queue")


def test_R13_closed_session_still_due_and_execution_has_no_clock_gate(env):
    from tradingagents.long_run import next_due_session
    service, broker = env
    target = next_due_session(
        now=datetime(2026, 9, 11, 21, tzinfo=timezone.utc), run_time_et="11:00",
        started_at=datetime(2026, 9, 11, 12, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 11, 12, tzinfo=timezone.utc), settled=[],
        calendar_rows=[{"date": "2026-09-11", "open": "09:30", "close": "16:00"}],
    )
    assert target["due"] and target["session_date"] == "2026-09-11"
    broker.get_clock = Mock(return_value=NS(is_open=False))
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result["success"], result
    assert len(broker.submits) == 1 and broker.get_clock.call_count == 0
    print("R13 17:00 ET session remains due; execution submits GTC market parent without market-clock proof")


def test_R14_stale_position_plan_can_create_oversized_opposite_stop(env):
    service, broker = env
    data = opening("LONG", "NEUTRAL")
    broker.qty = -5  # position changed after the analysis produced its FLAT plan
    result = service.execute(trade_intent=data, dollar_amount=1000, allow_shorts=True)
    assert result["success"] and not result.get("paused"), result
    assert broker.qty == 4 and broker.orders[0].legs[0].qty == 9
    print("R14 stale FLAT->LONG plan on SHORT 5 buys 9: LONG 4 with SELL 9 stop; account CLEAN")


def test_R15_embedding_client_ignores_configured_request_timeout(env, monkeypatch):
    import tradingagents.agents.utils.memory as memory
    monkeypatch.setattr(memory, "get_embedding_client_config", lambda: {"api_key": "fixture-only"})
    monkeypatch.setattr(memory.chromadb, "Client", Mock())
    obj = memory.FinancialSituationMemory("fixture", {
        "memory_retrieval_enabled": True, "llm_request_timeout_seconds": 7,
    })
    assert obj.client.timeout.read == 600 and obj.client.max_retries == 2
    obj.client.close()
    print("R15 configured timeout=7s, embedding SDK read timeout=600s with 2 retries")


def test_R16_report_snapshot_fabricates_zero_on_malformed_broker_facts(env):
    from tradingagents.long_run import capture_account_snapshot
    broken = NS(get_account=lambda: NS(id="fixture", equity="NaN", cash=None),
                get_all_positions=lambda: None)
    result = capture_account_snapshot(broken)
    assert result["equity"] == 0 and result["cash"] == 0 and result["positions"] == []
    print("R16 invalid equity/missing cash/unavailable positions become equity=0,cash=0,positions=[]")
