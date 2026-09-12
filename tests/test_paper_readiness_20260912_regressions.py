"""Formal corrected-behavior regressions for the 2026-09-12 paper-readiness
review (N01-N05, N07-N12, N14-N16).

Inverted from the defect evidence in ``docs/paper_readiness_20260912_repros.py``:
passing here proves the FIXED behavior. Every transport is faked in-process —
no network, no real Alpaca mutation, no paid LLM call, no sleeping.
"""

import importlib.util
import json
import socket
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import tradingagents.agents  # noqa: F401  (production-safe import order)
from tradingagents.agents.schemas import (
    EntryPolicy,
    RiskDecision,
    TradeIntent,
    build_trade_intent_from_risk_decision,
)
from tradingagents.execution.authority import BrokerQuote, capture_broker_snapshot
from tradingagents.execution.service import ExecutionService, validate_trade_intent
from tradingagents.execution.store import canonical_decision_id, client_order_id_for

# Reuse the existing audited deterministic broker fixture (not its test cases).
_spec = importlib.util.spec_from_file_location(
    "readiness_20260912_fixture",
    Path(__file__).resolve().parents[1] / "tests/test_execution_safety_plan_a.py",
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
Broker, opening, now = _fixture.Broker, _fixture.opening, _fixture.now


@pytest.fixture
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


def seed_pending(env, payload, *, quantity=10, decision_id="pending", side=None, role="open", seq=0):
    if side is None:
        side = "sell" if payload["action"] == "SHORT" else "buy"
    env.service.store.ensure_account_binding("audit-fixture")
    _, rows, _ = env.service.store.create_outbox(
        decision_id=decision_id, run_id=None, symbol="AAPL", action=payload["action"],
        target_position=payload["target_position"], payload_json=json.dumps(payload),
        orders=[{"client_order_id": client_order_id_for(decision_id, "AAPL", side, role=role, seq=seq),
                 "symbol": "AAPL", "side": side, "quantity": quantity, "notional": None}],
    )
    return rows[0]


def close_intent(*, current="LONG"):
    return build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment", current_position=current, allow_shorts=False,
        decision=RiskDecision(action="SELL", confidence="high", risk_rationale="exit",
                              required_controls="close"),
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# N01 — recovery obeys the CURRENT short-exposure policy
# ---------------------------------------------------------------------------

def test_N01_recovery_blocks_short_opening_under_current_opt_out(isolated):
    e = isolated
    row = seed_pending(e, opening("SHORT"))
    assert e.config["allow_shorts"] is False
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 0, result
    assert e.broker.qty == 0
    assert result["success"] is False and result["account_execution_state"] == "PAUSED"
    assert e.service.store.get_order(row["order_id"])["status"] == "CANCELED"
    assert "short-exposure" in result["error"]


def test_N01_recovery_submits_short_opening_under_current_opt_in(isolated):
    e = isolated
    e.config["allow_shorts"] = True  # the CURRENT policy, read live by recovery
    seed_pending(e, opening("SHORT"))
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == -9  # protective resubmit sizes 9 shares from the quote
    assert result["success"] is True, result


def test_N01_recovery_allows_close_buy_for_existing_short(isolated):
    e = isolated
    e.broker.qty = -4  # fresh broker holds a SHORT; the pending close buy covers it
    payload = close_intent(current="SHORT")  # canonical close of a SHORT is a buy
    row = seed_pending(e, payload, quantity=4, side="buy", role="close", seq=0)
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == 0
    assert e.service.store.get_order(row["order_id"])["status"] != "CANCELED"


# ---------------------------------------------------------------------------
# N02 — recovery never trades through the fresh broker position
# ---------------------------------------------------------------------------

def test_N02_recovery_blocks_crossing_buy_over_fresh_short(isolated):
    e = isolated
    row = seed_pending(e, opening("BUY", "NEUTRAL"))
    e.broker.qty = -4  # fresh account state differs from the persisted decision
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 0, result
    assert e.broker.qty == -4, "no POST, no rewrite, no reversal"
    assert result["stale_position_transition"] is True
    assert "fresh analysis" in result["error"]
    assert result["success"] is False and result["account_execution_state"] == "PAUSED"
    assert e.service.store.get_order(row["order_id"])["status"] == "CANCELED"
    # No protective child was ever created for the blocked opening.
    assert len(e.service.store.list_all_orders()) == 1


def test_N02_same_direction_buy_onto_fresh_long_is_not_blocked_by_crossing(isolated):
    e = isolated
    seed_pending(e, opening("BUY", "NEUTRAL"))
    e.broker.qty = 5
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == 14  # same-direction increase: 5 held + 9 resubmitted
    assert "stale position transition" not in result.get("error", "")


def test_N02_same_direction_sell_onto_fresh_short_is_not_blocked_by_crossing(isolated):
    e = isolated
    e.config["allow_shorts"] = True  # current policy permits the increase
    seed_pending(e, opening("SHORT"))
    e.broker.qty = -2
    result = e.service.startup_recover()
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == -11
    assert "stale position transition" not in result.get("error", "")


# ---------------------------------------------------------------------------
# N03 — stop/window authority holds the final opening-POST boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("control", ["stop", "window", "recovery_stop"])
def test_N03_stop_or_window_during_final_get_blocks_the_post(isolated, monkeypatch, control):
    import tradingagents.long_run as lr
    from tradingagents.screening.selection_store import SelectionStore

    e = isolated
    stamp = now()
    clock_time = [stamp]
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime = {**e.config, "auto_screening_enabled": True,
               "calendar_rows": [{"date": day, "open": "09:30", "close": "16:00"}]}
    monkeypatch.setattr(SelectionStore, "load_valid", lambda *a, **k: {"top20": [{"symbol": "AAPL"}]})
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **k: 1.0)
    monkeypatch.setattr("tradingagents.portfolio.adjust_new_position_notional", lambda s, a, amount, **k: amount)

    def final_clock():
        # Authority is revoked WHILE the last blocking broker GET is running.
        if control == "window":
            clock_time[0] = stamp + timedelta(minutes=2)
        else:
            lr._stop_requested = True
        return NS(is_open=True)

    e.broker.get_clock = final_clock
    ends_at = stamp + timedelta(minutes=1)
    deps = lr.LongRunDeps(execution_service_factory=lambda: e.service,
                          broker_client_factory=lambda: e.broker,
                          now_fn=lambda: clock_time[0],
                          screening_fn=Mock(side_effect=AssertionError("no new LLM work on execution-only resume")))
    if control == "recovery_stop":
        seed_pending(e, opening())
        with pytest.raises(lr.LongRunStop):
            lr.run_daily_round(run_id="observation", session_date=day,
                               long_cfg={"base_trade_notional_usd": 1000}, runtime=runtime,
                               deps=deps, ends_at=ends_at)
        assert lr._control_stop_reason(deps, ends_at) is not None
        assert len(e.broker.submits) == 0
        return

    journal = lr.new_round_journal(day, ["AAPL"])
    journal["status"] = "RUNNING"
    journal["screening"] = {"cached": True}
    journal["symbols"]["AAPL"].update(status=lr.SYMBOL_ANALYZED, trade_intent=opening())
    lr.save_round_journal("observation", journal)
    lr.run_daily_round(run_id="observation", session_date=day,
                       long_cfg={"base_trade_notional_usd": 1000}, runtime=runtime,
                       deps=deps, ends_at=ends_at)
    assert lr._control_stop_reason(deps, ends_at) is not None
    assert len(e.broker.submits) == 0 and e.broker.qty == 0


# ---------------------------------------------------------------------------
# N04 — kill switch holds the final POST and DELETE boundaries
# ---------------------------------------------------------------------------

def test_N04_kill_switch_during_final_clock_get_blocks_the_post(isolated):
    e = isolated

    def clock_then_halt():
        e.guard.kill_switch_path.write_text("halt while final GET is running")
        return NS(is_open=True)

    e.broker.get_clock = clock_then_halt
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert e.guard.kill_switch_active()
    assert len(e.broker.submits) == 0 and e.broker.qty == 0, result
    assert result["success"] is False
    row = next(o for o in e.service.store.list_all_orders())
    assert row["status"] == "CANCELED"


def test_N04_kill_switch_during_protection_race_get_blocks_the_delete(isolated, monkeypatch):
    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    import tradingagents.execution.service as svc_module

    original = svc_module.capture_broker_snapshot
    state = {"count": 0}

    def counting_capture(broker, **kwargs):
        snap = original(broker, **kwargs)
        state["count"] += 1
        if state["count"] == 3:  # the fresh GET inside _cancel_protection_with_race_check
            e.guard.kill_switch_path.write_text("engaged during the fresh GET")
        return snap

    monkeypatch.setattr(svc_module, "capture_broker_snapshot", counting_capture)
    result = e.service.execute(trade_intent=close_intent(), dollar_amount=1000)
    assert len(e.broker.cancels) == 0, result
    assert all(leg.status == "new" for leg in e.broker.orders[0].legs)
    assert e.broker.qty == 9
    assert result.get("paused") is True


def test_N04_control_kill_switch_already_engaged_blocks_close_and_keeps_protection(isolated):
    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    e.guard.kill_switch_path.write_text("operator halt")
    result = e.service.execute(trade_intent=close_intent(), dollar_amount=1000)
    assert len(e.broker.cancels) == 0, result
    assert e.broker.qty == 9 and len(e.broker.submits) == 1
    assert all(leg.status == "new" for leg in e.broker.orders[0].legs)


# ---------------------------------------------------------------------------
# N05 — terminal historical orders never demand a quote
# ---------------------------------------------------------------------------

def _seed_delisted(env, status):
    env.broker.orders.append(NS(id="old", client_order_id="manual-old", symbol="DELISTED",
                                side="buy", status=status, qty=10, filled_qty=0,
                                filled_avg_price=None, updated_at=now(), legs=[], notional=None))
    original = env.service._quote_factory

    def quote(symbol):
        if symbol == "DELISTED":
            raise ValueError("no current quote for delisted symbol")
        return original(symbol)

    env.service._quote_factory = quote


@pytest.mark.parametrize("status", ["canceled", "rejected", "expired", "filled"])
def test_N05_terminal_historical_orders_do_not_block_new_opening(isolated, status):
    e = isolated
    _seed_delisted(e, status)
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert len(e.broker.submits) == 1, result
    assert e.broker.qty == 9


def test_N05_live_order_without_quote_still_fails_closed(isolated):
    e = isolated
    _seed_delisted(e, "new")
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert len(e.broker.submits) == 0
    assert "DELISTED" in result.get("error", ""), result


# ---------------------------------------------------------------------------
# N07 — a filled protected opening is judged even with zero child facts
# ---------------------------------------------------------------------------

def test_N07_filled_parent_without_any_child_facts_pauses_execute_and_recovery(isolated):
    e = isolated
    submit = e.broker.submit_order

    def accepted_but_no_child_facts(request):
        parent = submit(request)
        e.broker.orders = [parent]
        parent.legs = []
        return parent

    e.broker.submit_order = accepted_but_no_child_facts
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert e.broker.qty == 9
    assert len(e.service.store.list_all_orders()) == 1
    assert result["account_execution_state"] == "PAUSED"
    assert any(r.startswith("PROTECTION_GAP:") for r in result["reconciliation_reasons"])
    recovery = e.service.startup_recover()
    assert recovery["success"] is False
    assert any(r.startswith("PROTECTION_GAP:") for r in recovery["reconciliation_reasons"])


def test_N07_adequate_live_child_stop_is_clean(isolated):
    e = isolated
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result["success"] and result["account_execution_state"] == "CLEAN"
    recovery = e.service.startup_recover()
    assert recovery["success"] is True, recovery


def test_N07_terminal_children_leave_a_protection_gap(isolated):
    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    for order in e.broker.orders[1:]:  # both protective children hit a terminal state
        order.status = "canceled"
    recovery = e.service.startup_recover()
    assert recovery["success"] is False
    assert any(r.startswith("PROTECTION_GAP:") for r in recovery["reconciliation_reasons"])


def test_N07_position_closed_clears_the_pause(isolated):
    e = isolated
    submit = e.broker.submit_order

    def accepted_but_no_child_facts(request):
        parent = submit(request)
        e.broker.orders = [parent]
        parent.legs = []
        return parent

    e.broker.submit_order = accepted_but_no_child_facts
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["paused"] is True
    liquidation = e.service.liquidate("AAPL")
    assert e.broker.qty == 0
    recovery = e.service.startup_recover()
    assert recovery["success"] is True, (liquidation, recovery)


def test_N07_manual_position_is_never_paused_by_the_coverage_rule(isolated):
    e = isolated
    e.broker.qty = 5  # a holding with no durable program ledger rows at all
    recovery = e.service.startup_recover()
    assert all("PROTECTION_GAP" not in r for r in recovery["reconciliation_reasons"]), recovery


# ---------------------------------------------------------------------------
# N08 — circuit breakers stop the observation; single-order refusals do not
# ---------------------------------------------------------------------------

def test_N08_daily_loss_breaker_stops_the_observation(isolated):
    import tradingagents.long_run as lr

    e = isolated
    e.broker.get_account = lambda: NS(id="audit-fixture", equity=89000, last_equity=100000,
                                      cash=89000, buying_power=89000)
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result.get("safety_blocked") and "Daily loss" in result.get("error", ""), result
    assert "DAILY_LOSS_HALT" in result.get("safety_reason_codes", [])
    with pytest.raises(lr.LongRunStop) as excinfo:
        lr._check_execution_hard_stop("AAPL", result)
    assert excinfo.value.code == "SAFETY_CIRCUIT_BREAKER"
    assert not e.broker.submits


def test_N08_drawdown_breaker_stops_the_observation(isolated):
    import tradingagents.long_run as lr

    e = isolated
    e.guard.state_path.write_text(json.dumps(
        {"high_water_mark": 100000.0, "consecutive_rejections": 0, "llm_tokens": {}}))
    e.broker.get_account = lambda: NS(id="audit-fixture", equity=84000, last_equity=84000,
                                      cash=84000, buying_power=84000)
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result.get("safety_blocked"), result
    assert "MAX_DRAWDOWN_HALT" in result.get("safety_reason_codes", [])
    with pytest.raises(lr.LongRunStop) as excinfo:
        lr._check_execution_hard_stop("AAPL", result)
    assert excinfo.value.code == "SAFETY_CIRCUIT_BREAKER"


def test_N08_rejection_streak_breaker_stops_the_observation(isolated):
    import tradingagents.long_run as lr

    e = isolated
    e.guard.state_path.write_text(json.dumps(
        {"high_water_mark": None, "consecutive_rejections": 5, "llm_tokens": {}}))
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert result.get("safety_blocked"), result
    assert "CONSECUTIVE_REJECTIONS_HALT" in result.get("safety_reason_codes", [])
    with pytest.raises(lr.LongRunStop) as excinfo:
        lr._check_execution_hard_stop("AAPL", result)
    assert excinfo.value.code == "SAFETY_CIRCUIT_BREAKER"
    assert not e.broker.submits


def test_N08_notional_cap_refuses_one_order_without_stopping(isolated):
    import tradingagents.long_run as lr

    e = isolated
    result = e.service.execute(trade_intent=opening(), dollar_amount=26000)
    assert result.get("safety_blocked"), result
    assert "MAX_TRADE_NOTIONAL" in result.get("safety_reason_codes", [])
    lr._check_execution_hard_stop("AAPL", result)  # must NOT raise
    assert not e.broker.submits


def test_N08_concentration_cap_refuses_one_order_without_stopping(isolated):
    import tradingagents.long_run as lr

    e = isolated
    e.guard.config["max_trade_notional_usd"] = 0  # isolate the concentration check
    payload = opening()
    payload["entry_policy"]["risk_fraction"] = 0.03  # let 26000 pass entry policy
    result = e.service.execute(trade_intent=payload, dollar_amount=26000)
    assert result.get("safety_blocked"), result
    assert "MAX_SYMBOL_CONCENTRATION" in result.get("safety_reason_codes", [])
    lr._check_execution_hard_stop("AAPL", result)  # must NOT raise
    assert not e.broker.submits


# ---------------------------------------------------------------------------
# N09 — TradeIntent fields must match one canonical plan
# ---------------------------------------------------------------------------

def test_N09_contradictory_payload_is_rejected_at_the_schema_boundary(isolated):
    e = isolated
    payload = opening("SHORT")
    payload["action"] = "BUY"  # contradicts the SHORT target/planned sell
    payload["risk_controls"]["take_profit_price"] = None
    payload["risk_controls"]["take_profit"] = None
    validated, error = validate_trade_intent(payload)
    assert validated is None and error is not None
    assert "inconsistent TradeIntent" in error
    result = e.service.execute(trade_intent=payload, dollar_amount=1000, allow_shorts=False)
    assert len(e.broker.submits) == 0
    assert result.get("fail_closed") is True
    assert len(e.broker.orders) == 0  # not even a broker GET mismatch marker


def test_N09_prebuilt_model_cannot_bypass_consistency(isolated):
    from tradingagents.agents.schemas import ExecutableAction

    model = TradeIntent.model_validate(opening("SHORT"))
    model.action = ExecutableAction.BUY  # contradicts the SHORT target/planned sell
    validated, error = validate_trade_intent(model)
    assert validated is None and "inconsistent TradeIntent" in error


@pytest.mark.parametrize("action,current,mode", [
    ("BUY", "NEUTRAL", "investment"),
    ("SELL", "LONG", "investment"),
    ("HOLD", "LONG", "investment"),
    ("LONG", "NEUTRAL", "trading"),
    ("SHORT", "NEUTRAL", "trading"),
    ("NEUTRAL", "SHORT", "trading"),
    ("SHORT", "LONG", "trading"),    # reversal to short
    ("LONG", "SHORT", "trading"),    # reversal to long
])
def test_N09_canonical_builder_payloads_pass_the_validator(action, current, mode):
    payload = build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode=mode, current_position=current,
        decision=RiskDecision(action=action, confidence="high", risk_rationale="fixture",
                              required_controls="stop", stop_loss_price=90, take_profit_price=120),
        allow_shorts=True,
    ).model_dump(mode="json")
    validated, error = validate_trade_intent(payload)
    assert error is None, error
    assert validated["target_position"] == payload["target_position"]


# ---------------------------------------------------------------------------
# N10 — the portfolio gather hook is bound to the ticker
# ---------------------------------------------------------------------------

def test_N10_portfolio_gather_receives_ticker_and_adjusts_amount(isolated, monkeypatch):
    from tradingagents.execution.auto_trade import execute_auto_trade

    e = isolated
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **k: 1.0)
    gather = Mock(return_value=(10.0, {}, {}))  # $10 equity clips the $1000 request
    monkeypatch.setattr("tradingagents.portfolio.gather_portfolio_state_via_alpaca", gather)
    sink = NS(execute=Mock(return_value={"success": True}))
    execute_auto_trade(ticker="AAPL", trade_intent=opening(), base_trade_notional_usd=1000,
                       allow_shorts=False, config=e.config, execution_service=sink)
    assert gather.call_count == 1
    assert gather.call_args.args[0] == "AAPL"
    assert sink.execute.call_args.kwargs["dollar_amount"] == 10.0


def test_N10_gather_failure_keeps_the_original_amount(isolated, monkeypatch):
    from tradingagents.execution.auto_trade import execute_auto_trade

    e = isolated
    monkeypatch.setattr("tradingagents.regime.regime_risk_multiplier", lambda *a, **k: 1.0)
    gather = Mock(side_effect=RuntimeError("gather down"))
    monkeypatch.setattr("tradingagents.portfolio.gather_portfolio_state_via_alpaca", gather)
    sink = NS(execute=Mock(return_value={"success": True}))
    execute_auto_trade(ticker="AAPL", trade_intent=opening(), base_trade_notional_usd=1000,
                       allow_shorts=False, config=e.config, execution_service=sink)
    assert gather.call_count == 1
    assert sink.execute.call_args.kwargs["dollar_amount"] == 1000


# ---------------------------------------------------------------------------
# N11 — an unreadable HTTP-200 submit outcome is UNKNOWN, never REJECTED
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response_body", ['{"id":', '{"id":"unexpected-only-field"}'])
def test_N11_real_sdk_http200_decode_failure_is_unknown(isolated, response_body):
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
    assert result["orders"][0]["status"] == "UNKNOWN", result
    assert result.get("has_unknown") is True
    assert result["account_execution_state"] == "PAUSED"
    assert result.get("safety_blocked") is not True
    # The same client order id remains the reconciliation handle.
    lookup = e.service.lookup_unknown(result["orders"][0]["client_order_id"])
    assert lookup["found"] is False and lookup.get("not_found") is True


def test_N11_structured_422_is_still_a_definitive_rejection(isolated):
    from alpaca.common.exceptions import APIError
    from requests import HTTPError, Response

    e = isolated
    response = Response()
    response.status_code = 422
    e.broker.submit_order = Mock(side_effect=APIError(
        '{"code":42210000,"message":"unprocessable"}', HTTPError(response=response)))
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000)
    assert e.broker.submit_order.call_count == 1
    assert result["orders"][0]["status"] == "REJECTED", result
    assert not result.get("has_unknown")


def test_N11_request_construction_failure_makes_zero_broker_calls(isolated):
    e = isolated
    e.broker.submit_order = Mock(side_effect=AssertionError("must never be called"))
    result = e.service._liquidate_core("AAPL", _quantity=None)  # no provable size
    assert result["status"] == "REJECTED"
    assert result["broker_calls"] == 0
    assert e.broker.submit_order.assert_not_called() is None


# ---------------------------------------------------------------------------
# N12 — the runner lock guards the active-state decision
# ---------------------------------------------------------------------------

def _cli_cfg(lr):
    cfg = lr.default_long_run_config()
    cfg.update(base_trade_notional_usd=1000, analysis_provider="openai", analysis_model="fixture",
               decision_provider="openai", decision_model="fixture",
               screening_provider="openai", screening_model="fixture")
    return cfg


def test_N12_second_runner_never_overwrites_the_active_state(isolated, monkeypatch):
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr

    e = isolated
    cfg = _cli_cfg(lr)
    recorded = {}

    def competing_runner_starts_during_setup(_):
        lr.save_active_state({"run_id": "already-running-observation", "status": "RUNNING"})
        recorded["bytes"] = lr.active_path().read_bytes()
        return cfg, {}

    monkeypatch.setattr(cli, "collect_long_run_config", competing_runner_starts_during_setup)
    monkeypatch.setattr(lr, "run_preflight", lambda *a: {"ok": True})
    monkeypatch.setattr(lr, "run_post_authorization_recovery",
                        Mock(side_effect=AssertionError("recovery must not run")))
    monkeypatch.setattr(lr, "fetch_session_dates", lambda *a, **k: [now().date()])
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)

    @contextmanager
    def busy():
        raise lr.RunnerLockBusy("another runner holds the lock")
        yield

    monkeypatch.setattr(lr, "runner_lock", busy)
    result = CliRunner().invoke(cli.app, ["long-run"])
    assert result.exit_code == 2, result.output
    assert lr.active_path().read_bytes() == recorded["bytes"]


def test_N12_race_inside_the_lock_refuses_without_recovery_or_writes(isolated, monkeypatch):
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr

    e = isolated
    cfg = _cli_cfg(lr)
    recorded = {}

    def competing_runner_starts_during_setup(_):
        # Another runner created an observation while this process was in setup.
        lr.save_active_state({"run_id": "already-running-observation", "status": "RUNNING"})
        recorded["bytes"] = lr.active_path().read_bytes()
        return cfg, {}

    monkeypatch.setattr(cli, "collect_long_run_config", competing_runner_starts_during_setup)
    monkeypatch.setattr(lr, "run_preflight", lambda *a: {"ok": True})
    monkeypatch.setattr(lr, "run_post_authorization_recovery",
                        Mock(side_effect=AssertionError("recovery must not run")))
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    result = CliRunner().invoke(cli.app, ["long-run"])
    assert result.exit_code == 2, result.output
    assert lr.active_path().read_bytes() == recorded["bytes"]


def test_N12_new_path_acquires_the_lock_exactly_once(isolated, monkeypatch):
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr

    e = isolated
    cfg = _cli_cfg(lr)
    entries = []
    real_lock = lr.runner_lock

    @contextmanager
    def counting_lock():
        entries.append("enter")
        try:
            with real_lock():
                yield
        finally:
            entries.append("exit")

    monkeypatch.setattr(lr, "runner_lock", counting_lock)
    monkeypatch.setattr(cli, "collect_long_run_config", lambda _: (cfg, {}))
    monkeypatch.setattr(lr, "run_preflight", lambda *a: {"ok": True})
    monkeypatch.setattr(lr, "run_post_authorization_recovery", lambda *a: {"snapshot": {"equity": 100000}})
    monkeypatch.setattr(lr, "fetch_session_dates", lambda *a, **k: [now().date()])
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    loop = Mock(return_value={"outcome": "interrupted"})
    monkeypatch.setattr(lr, "run_observation_loop", loop)
    result = CliRunner().invoke(cli.app, ["long-run"])
    assert loop.call_count == 1
    assert entries.count("enter") == 1, entries
    saved = json.loads(lr.active_path().read_text())
    assert saved["status"] == "RUNNING" and saved["run_id"]


def test_N12_resume_acquires_the_lock_once_and_bumps_restart_count(isolated, monkeypatch):
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr

    e = isolated
    stamp = now().isoformat()
    lr.save_active_state({"run_id": "lr-resume", "status": "RUNNING", "restart_count": 0,
                          "config": {}, "started_at": stamp, "ends_at": stamp})
    entries = []
    real_lock = lr.runner_lock

    @contextmanager
    def counting_lock():
        entries.append("enter")
        try:
            with real_lock():
                yield
        finally:
            entries.append("exit")

    monkeypatch.setattr(lr, "runner_lock", counting_lock)
    loop = Mock(return_value={"outcome": "completed"})
    monkeypatch.setattr(lr, "run_observation_loop", loop)
    result = CliRunner().invoke(cli.app, ["long-run"])
    assert result.exit_code == 0, result.output
    assert loop.call_count == 1
    assert entries.count("enter") == 1, entries
    saved = json.loads(lr.active_path().read_text())
    assert saved["restart_count"] == 1


def test_N12_busy_lock_on_resume_mutates_nothing(isolated, monkeypatch):
    from typer.testing import CliRunner
    import cli.main as cli
    import tradingagents.long_run as lr

    e = isolated
    stamp = now().isoformat()
    lr.save_active_state({"run_id": "lr-resume", "status": "RUNNING", "restart_count": 2,
                          "config": {}, "started_at": stamp, "ends_at": stamp})
    recorded = lr.active_path().read_bytes()

    @contextmanager
    def busy():
        raise lr.RunnerLockBusy("another runner holds the lock")
        yield

    monkeypatch.setattr(lr, "runner_lock", busy)
    result = CliRunner().invoke(cli.app, ["long-run"])
    assert result.exit_code == 2, result.output
    assert lr.active_path().read_bytes() == recorded


# ---------------------------------------------------------------------------
# N14 — broker outages are error states, never an empty portfolio
# ---------------------------------------------------------------------------

def test_N14_broker_outage_renders_error_states(isolated, monkeypatch):
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils
    from webui.components.alpaca_account import (
        render_account_summary, render_orders_table, render_orders_table_error,
        render_positions_table,
    )

    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
                        Mock(side_effect=RuntimeError("broker unavailable")))
    positions = render_positions_table()
    assert "Unable to Load Positions" in str(positions)
    assert "broker unavailable" in str(positions)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        AlpacaUtils.get_positions_data()
    with pytest.raises(RuntimeError, match="account info unavailable"):
        AlpacaUtils.get_account_info()
    assert "Unable to Load Account Summary" in str(render_account_summary())
    with pytest.raises(RuntimeError, match="orders unavailable"):
        AlpacaUtils.get_recent_orders_page()
    assert "Unable to Load Orders" in str(render_orders_table())
    # The callback path's catch must land on the error renderer too.
    try:
        AlpacaUtils.get_recent_orders_page()
    except Exception as exc:
        assert "Unable to Load Orders" in str(render_orders_table_error(exc))


def test_N14_legal_empty_account_still_renders_empty_and_zeros(isolated, monkeypatch):
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils
    from webui.components.alpaca_account import render_account_summary, render_positions_table

    client = Mock()
    client.get_all_positions.return_value = []
    client.get_account.return_value = NS(buying_power=0, cash=0, equity=0, last_equity=0)
    client.get_orders.return_value = []
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
                        lambda: client)
    assert "Your portfolio is currently empty" in str(render_positions_table())
    account = AlpacaUtils.get_account_info()
    assert account["cash"] == 0 and account["buying_power"] == 0
    assert "$0.00" in str(render_account_summary())
    orders = AlpacaUtils.get_recent_orders_page()
    assert orders["orders"] == [] and orders["total_orders"] == 0


# ---------------------------------------------------------------------------
# N15 — recovery/deadline mutations land in the round journal and final report
# ---------------------------------------------------------------------------

def _round_env(e, monkeypatch, stamp, day):
    import tradingagents.long_run as lr
    from tradingagents.screening.selection_store import SelectionStore

    runtime = {**e.config, "auto_screening_enabled": True,
               "calendar_rows": [{"date": day, "open": "09:30", "close": "16:00"}]}
    monkeypatch.setattr(SelectionStore, "load_valid", lambda *a, **k: {"top20": [{"symbol": "AAPL"}]})
    journal = lr.new_round_journal(day, [])
    journal["status"] = "RUNNING"
    journal["screening"] = {"cached": True}
    lr.save_round_journal("observation", journal)
    deps = lr.LongRunDeps(execution_service_factory=lambda: e.service,
                          broker_client_factory=lambda: e.broker, now_fn=lambda: stamp,
                          screening_fn=Mock(side_effect=AssertionError("no new LLM work")))
    return runtime, deps


def test_N15_recovery_bracket_post_counts_once_in_the_final_report(isolated, monkeypatch):
    import tradingagents.long_run as lr

    e = isolated
    stamp = now()
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime, deps = _round_env(e, monkeypatch, stamp, day)
    seed_pending(e, opening())
    cfg = {"base_trade_notional_usd": 1000, "duration_calendar_days": 30}
    lr.run_daily_round(run_id="observation", session_date=day, long_cfg=cfg,
                       runtime=runtime, deps=deps, ends_at=stamp + timedelta(days=1))
    saved = lr.load_round_journal("observation", day)
    maintenance = saved["maintenance_execution"]["recovery"]
    assert maintenance["submit_calls"] == 1
    assert maintenance["broker_calls"] == 1
    assert maintenance["submitted_symbols"] == ["AAPL"]
    report = lr.aggregate_final_report(
        {"run_id": "observation", "started_at": stamp.isoformat(),
         "ends_at": (stamp + timedelta(days=1)).isoformat(),
         "expected_sessions": [day], "status": "RUNNING"}, cfg, runtime)
    assert len(e.broker.submits) == 1 and e.broker.qty == 9
    assert report["execution"]["broker_calls"] == 1
    assert report["execution"]["maintenance_broker_calls"] == 1
    assert report["execution"]["submitted_symbols"] == 1
    assert report["execution_db"]["orders_total"] == 3  # one POST, parent + 2 children


def test_N15_adoption_only_recovery_counts_zero_mutations(isolated):
    e = isolated
    row = seed_pending(e, opening())
    e.broker.qty = 10
    # The broker already holds the parent AND its live protective children.
    children = [
        NS(id="child-stop", client_order_id="broker-stop-1", symbol="AAPL", side="sell",
           qty=10, filled_qty=0, filled_avg_price=None, status="new", updated_at=now(),
           legs=[], notional=None),
        NS(id="child-target", client_order_id="broker-target-1", symbol="AAPL", side="sell",
           qty=10, filled_qty=0, filled_avg_price=None, status="new", updated_at=now(),
           legs=[], notional=None),
    ]
    parent = NS(id="broker-1", client_order_id=row["client_order_id"], symbol="AAPL",
                side="buy", qty=10, filled_qty=10, filled_avg_price=100, status="filled",
                updated_at=now(), legs=list(children), notional=None)
    e.broker.orders.extend([parent, *children])
    # Realistic adoption race: the entry authority snapshot runs just BEFORE
    # the broker surfaces the accepted parent (capture #2's all-orders call,
    # i.e. get_orders invocation #4), so recovery adopts the parent via the
    # client-order-id lookup instead of re-POSTing.
    original_get_orders = e.broker.get_orders
    calls = {"n": 0}

    def get_orders(request):
        calls["n"] += 1
        result = original_get_orders(request)
        if calls["n"] == 4:
            return []
        return result

    e.broker.get_orders = get_orders
    result = e.service.startup_recover()
    assert result["success"] is True, result
    maintenance = result["recovery_maintenance"]
    assert maintenance["broker_calls"] == 0
    assert maintenance["submit_calls"] == 0
    assert maintenance["submitted_symbols"] == []
    assert e.service.store.get_order(row["order_id"])["status"] == "FILLED"


def test_N15_deadline_close_cancel_and_post_counts_match_the_report(isolated, monkeypatch):
    import tradingagents.long_run as lr

    e = isolated
    stamp = now()
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime, deps = _round_env(e, monkeypatch, stamp, day)
    intent = opening()
    assert e.service.execute(trade_intent=intent, dollar_amount=1000)["success"]
    # Seed a due exit: move the durable exit deadline into the past.
    did = canonical_decision_id(intent)
    conn = sqlite3.connect(e.root / "execution.db")
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM execution_intents WHERE decision_id = ?", (did,)).fetchone()[0])
    payload["entry_policy"]["exit_by"] = (stamp - timedelta(days=1)).isoformat()
    conn.execute("UPDATE execution_intents SET payload_json = ? WHERE decision_id = ?",
                 (json.dumps(payload), did))
    conn.commit()
    conn.close()
    cfg = {"base_trade_notional_usd": 1000, "duration_calendar_days": 30}
    lr.run_daily_round(run_id="observation", session_date=day, long_cfg=cfg,
                       runtime=runtime, deps=deps, ends_at=stamp + timedelta(days=1))
    saved = lr.load_round_journal("observation", day)
    maintenance = saved["maintenance_execution"]["deadline"]
    assert maintenance["cancel_calls"] == 1  # one DELETE; the broker cascade canceled the sibling
    assert maintenance["submit_calls"] == 1
    assert maintenance["broker_calls"] == 2
    assert maintenance["submitted_symbols"] == ["AAPL"]
    report = lr.aggregate_final_report(
        {"run_id": "observation", "started_at": stamp.isoformat(),
         "ends_at": (stamp + timedelta(days=1)).isoformat(),
         "expected_sessions": [day], "status": "RUNNING"}, cfg, runtime)
    assert report["execution"]["broker_calls"] == maintenance["broker_calls"]
    assert report["execution"]["submitted_symbols"] == 1
    assert len(e.broker.cancels) == maintenance["cancel_calls"]
    assert len(e.broker.submits) == 2  # the original entry + the deadline close


# ---------------------------------------------------------------------------
# N16 — technical brief proves data as-of and refuses stale/unavailable bars
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")


def _et(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)


def _frame(stamps):
    n = len(stamps)
    close = [100 + i * 0.1 for i in range(n)]
    return pd.DataFrame({
        "timestamp": [s.astimezone(timezone.utc) for s in stamps],
        "open": [c - 0.5 for c in close],
        "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": [1_000_000] * n,
    })


def _equity_frames(sessions):
    hourly, h4, daily = [], [], []
    for day in sessions:
        for hour in range(7):
            hourly.append(_et(day, 9, 30) + timedelta(hours=hour))
        h4.append(_et(day, 9, 30))
        h4.append(_et(day, 13, 30))
        daily.append(datetime(day.year, day.month, day.day, 5, 0, tzinfo=timezone.utc))
    return {"1Hour": _frame(hourly), "4Hour": _frame(h4), "1Day": _frame(daily)}


def _dispatcher(frames):
    def fake_get_stock_data(*args, symbol=None, start_date=None, end_date=None,
                            timeframe=None, **kwargs):
        return frames[timeframe].copy()

    return fake_get_stock_data


def _calendar_rows(days, close="16:00"):
    return [{"date": d.isoformat(), "open": "09:30", "close": close} for d in days]


def _weekdays(start, end):
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def test_N16_stale_2024_bars_are_reported_stale_and_excluded(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    old = _frame([datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i) for i in range(220)])
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        lambda *a, **k: old.copy())
    ref = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)
    rows = _calendar_rows(_weekdays(date(2026, 5, 1), date(2026, 9, 11)))
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    assert brief.timeframes == []
    assert [q.status for q in brief.data_quality] == ["stale", "stale", "stale"]
    assert all(q.as_of and q.as_of.startswith("2024") for q in brief.data_quality)
    assert brief.raw_prices == {"last_close": None, "prev_close": None, "daily_change_pct": None}
    payload = json.loads(brief.model_dump_json())
    assert payload["generated_at"].startswith("2026")
    assert payload["signal_summary"]["confidence"] == "low"


def test_N16_current_session_intraday_and_previous_session_higher_tf_are_fresh(
        isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2026, 7, 13), date(2026, 9, 11))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    rows = _calendar_rows(sessions)
    ref = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)  # 12:00 ET Friday
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    quality = {q.timeframe: q for q in brief.data_quality}
    assert quality["1h"].status == "fresh"
    assert quality["4h"].status == "fresh" and quality["1d"].status == "fresh"
    # The 11:30 bar has not completed by 12:00 ET; the 10:30 bar has.
    assert quality["1h"].as_of == (_et(date(2026, 9, 11), 10, 30)).astimezone(timezone.utc).isoformat()
    # 4h/daily completed bars only exist for the PREVIOUS session this early.
    assert quality["4h"].as_of == (_et(date(2026, 9, 10), 13, 30)).astimezone(timezone.utc).isoformat()
    assert quality["1d"].as_of == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc).isoformat()
    assert len(brief.timeframes) == 3
    assert brief.raw_prices["last_close"] is not None


def test_N16_future_and_incomplete_bars_are_dropped(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2026, 7, 13), date(2026, 9, 11))
    frames = _equity_frames(sessions)
    frames["1Hour"] = pd.concat([
        frames["1Hour"],
        _frame([_et(date(2026, 9, 14), 9, 30), _et(date(2026, 9, 14), 10, 30)]),
    ]).reset_index(drop=True)
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(frames))
    rows = _calendar_rows(sessions)
    ref = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    quality = {q.timeframe: q for q in brief.data_quality}
    assert quality["1h"].status == "fresh"
    assert quality["1h"].as_of == (_et(date(2026, 9, 11), 10, 30)).astimezone(timezone.utc).isoformat()


def test_N16_weekend_reference_judges_against_the_last_completed_session(
        isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2026, 7, 13), date(2026, 9, 11))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    rows = _calendar_rows(sessions)
    ref = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)  # Saturday
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    assert all(q.status == "fresh" for q in brief.data_quality)


def test_N16_holiday_session_is_skipped_without_a_false_stale(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = [d for d in _weekdays(date(2026, 7, 13), date(2026, 9, 11)) if d != date(2026, 9, 11)]
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    rows = _calendar_rows(sessions)
    ref = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)  # exchange closed this Friday
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    assert all(q.status == "fresh" for q in brief.data_quality)


def test_N16_early_close_session_completes_at_the_actual_close(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2026, 7, 13), date(2026, 9, 11))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    rows = _calendar_rows(sessions, close="13:00")
    ref = datetime(2026, 9, 11, 17, 10, tzinfo=timezone.utc)  # 13:10 ET after the early close
    brief = build_technical_brief("AAPL", "2026-09-11", now=ref, calendar_rows=rows)
    quality = {q.timeframe: q for q in brief.data_quality}
    assert all(q.status == "fresh" for q in brief.data_quality)
    # The 09:30 4h bar completed at the 13:00 early close, not 13:30.
    assert quality["4h"].as_of == (_et(date(2026, 9, 11), 9, 30)).astimezone(timezone.utc).isoformat()
    # Today's daily bar completed at 13:00 as well.
    assert quality["1d"].as_of == datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc).isoformat()


def test_N16_calendar_outage_makes_equity_timeframes_unavailable(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2026, 7, 13), date(2026, 9, 11))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    brief = build_technical_brief("AAPL", "2026-09-11", now=datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc))
    assert all(q.status == "unavailable" for q in brief.data_quality)
    assert all("calendar unavailable" in (q.reason or "") for q in brief.data_quality)
    assert brief.timeframes == []
    assert brief.raw_prices["last_close"] is None


def test_N16_historical_request_is_judged_point_in_time(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    sessions = _weekdays(date(2025, 4, 21), date(2025, 6, 13))
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(_equity_frames(sessions)))
    rows = _calendar_rows(sessions)
    brief = build_technical_brief("AAPL", "2025-06-13", now=now(), calendar_rows=rows)
    assert all(q.status == "fresh" for q in brief.data_quality)
    assert brief.data_quality[0].as_of.startswith("2025-06-13")
    assert len(brief.timeframes) == 3


def _crypto_frames(*, tf_hours, end_utc, count=40):
    stamps = [end_utc - timedelta(hours=tf_hours * i) for i in range(count - 1, -1, -1)]
    return _frame(stamps)


@pytest.mark.parametrize("tf_hours,tf_key,ttl_hours,alpaca_tf", [
    (1, "1h", 2, "1Hour"), (4, "4h", 8, "4Hour"), (24, "1d", 36, "1Day"),
])
def test_N16_crypto_ttl_boundaries(isolated, monkeypatch, tf_hours, tf_key, ttl_hours, alpaca_tf):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    end = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)
    frames = {tf: _crypto_frames(tf_hours=hours, end_utc=end)
              for tf, hours in (("1Hour", 1), ("4Hour", 4), ("1Day", 24))}
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        _dispatcher(frames))
    # The last bar completes at end + tf_hours; the TTL runs from completion.
    # curr_date follows each reference's ET date so a same-day reference is
    # used (never the historical fallback).
    fresh_ref = end + timedelta(hours=tf_hours + ttl_hours)
    fresh_day = fresh_ref.astimezone(ET).date().isoformat()
    brief = build_technical_brief("BTC/USD", fresh_day, now=fresh_ref)
    quality = {q.timeframe: q for q in brief.data_quality}
    assert quality[tf_key].status == "fresh", quality[tf_key]
    stale_ref = end + timedelta(hours=tf_hours + ttl_hours, seconds=1)
    stale_day = stale_ref.astimezone(ET).date().isoformat()
    brief = build_technical_brief("BTC/USD", stale_day, now=stale_ref)
    quality = {q.timeframe: q for q in brief.data_quality}
    assert quality[tf_key].status == "stale", quality[tf_key]
    assert quality[tf_key].as_of == end.isoformat()


def test_N16_data_quality_always_covers_all_three_timeframes(isolated, monkeypatch):
    from tradingagents.dataflows.technical_brief import build_technical_brief

    e = isolated
    monkeypatch.setattr("tradingagents.dataflows.alpaca_utils.AlpacaUtils.get_stock_data",
                        lambda *a, **k: None)  # no data anywhere
    brief = build_technical_brief("AAPL", "2026-09-11", now=now(),
                                  calendar_rows=_calendar_rows(_weekdays(date(2026, 8, 1), date(2026, 9, 11))))
    assert [q.timeframe for q in brief.data_quality] == ["1h", "4h", "1d"]
    assert all(q.status == "unavailable" and q.as_of is None for q in brief.data_quality)


# ---------------------------------------------------------------------------
# N06 — characterization: conservative OCO sibling exposure is preserved
# ---------------------------------------------------------------------------

def test_N06_bracket_siblings_still_count_as_extra_opening_exposure(isolated):
    from tradingagents.risk.exposure import outstanding_increasing_notional

    e = isolated
    assert e.service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    snap = capture_broker_snapshot(e.broker)
    amount, complete = outstanding_increasing_notional(snap, reference_prices={"AAPL": 100})
    assert amount == 900 and complete
