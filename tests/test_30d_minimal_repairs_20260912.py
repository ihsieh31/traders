"""Regression tests for the 30-day CLI paper-test minimal repairs
(R1-R10 of TRADERS_30D_MINIMAL_REPAIR_IMPLEMENTATION_2026-09-12).

Every transport is faked in-process: no real broker POST, no real LLM API,
no network, no user ~/.tradingagents durable state.
"""

import importlib.util
import inspect
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import tradingagents.agents  # noqa: F401  (production-safe import order)
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from tradingagents.execution.authority import BrokerQuote
from tradingagents.execution.service import ExecutionService, _build_market_request
from tradingagents.execution.store import client_order_id_for
from tradingagents.llm_clients.retry import ProviderFailure

# Reuse the existing audited deterministic broker fixture (not its test cases).
_spec = importlib.util.spec_from_file_location(
    "repairs_20260912_fixture",
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


# ---------------------------------------------------------------------------
# R1 (B-01) — execute()/deadline recovery inherits can_submit
# ---------------------------------------------------------------------------

def test_R01_execute_recovery_defers_resubmit_when_can_submit_false(isolated):
    e = isolated
    row = seed_pending(e, opening())
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000,
                               can_submit=lambda: False)
    assert len(e.broker.submits) == 0, result
    assert e.broker.qty == 0
    assert result["success"] is False
    assert result["broker_calls"] == 0
    assert "stop/window authority" in result.get("error", ""), result
    # No fake REJECTED/CANCELED: the row keeps its durable pre-POST state.
    assert e.service.store.get_order(row["order_id"])["status"] == "PENDING"


def test_R01_execute_recovery_resubmits_when_can_submit_true(isolated):
    e = isolated
    seed_pending(e, opening())
    result = e.service.execute(trade_intent=opening(), dollar_amount=1000,
                               can_submit=lambda: True)
    # Control for the False case: with authority granted, the recoverable
    # order really is resubmitted (the R1 refusal is the can_submit wiring,
    # not a fixture that could never POST).
    assert len(e.broker.submits) >= 1, result


def test_R01_run_daily_round_passes_can_submit_to_deadline_recovery(isolated, monkeypatch):
    import tradingagents.long_run as lr
    from tradingagents.screening.selection_store import SelectionStore

    e = isolated
    stamp = now()
    day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    runtime = {**e.config, "auto_screening_enabled": True,
               "calendar_rows": [{"date": day, "open": "09:30", "close": "16:00"}]}
    monkeypatch.setattr(SelectionStore, "load_valid", lambda *a, **k: {"top20": [{"symbol": "AAPL"}]})
    journal = lr.new_round_journal(day, [])
    journal["status"] = "RUNNING"
    journal["screening"] = {"cached": True}
    lr.save_round_journal("observation", journal)

    captured = {}
    original = e.service.enforce_exit_deadlines

    def spying_deadlines(*args, **kwargs):
        captured["can_submit"] = kwargs.get("can_submit")
        return original(*args, **kwargs)

    e.service.enforce_exit_deadlines = spying_deadlines
    deps = lr.LongRunDeps(execution_service_factory=lambda: e.service,
                          broker_client_factory=lambda: e.broker, now_fn=lambda: stamp,
                          screening_fn=Mock(side_effect=AssertionError("no new LLM work")))
    lr.run_daily_round(run_id="observation", session_date=day,
                       long_cfg={"base_trade_notional_usd": 1000, "duration_calendar_days": 30},
                       runtime=runtime, deps=deps, ends_at=stamp + timedelta(days=1))
    assert callable(captured["can_submit"])
    assert captured["can_submit"]() is True  # no stop, no window end


def test_R01_enforce_exit_deadlines_signature_accepts_can_submit():
    sig = inspect.signature(ExecutionService.enforce_exit_deadlines)
    assert "can_submit" in sig.parameters
    assert sig.parameters["can_submit"].default is None


# ---------------------------------------------------------------------------
# R2 (H-07) — macro analyst must not swallow ProviderFailure
# ---------------------------------------------------------------------------

class FakeTool:
    def __init__(self, name, result="fake tool evidence"):
        self.name = name
        self._result = result

    def invoke(self, args):
        return self._result


class ScriptedLLM(Runnable):
    """bind_tools-aware LLM stub serving scripted per-invoke outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.invocations = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.invocations += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def tool_calls_message(name):
    return AIMessage(content="", additional_kwargs={
        "tool_calls": [{"name": name, "args": {}, "id": "call-1", "type": "tool_call"}],
    })


def _macro_toolkit():
    return NS(
        config={"online_tools": False, "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1},
        has_fred=lambda: True,
        has_openai_web_search=lambda: False,
        get_macro_analysis=FakeTool("get_macro_analysis"),
        get_economic_indicators=FakeTool("get_economic_indicators"),
        get_yield_curve_analysis=FakeTool("get_yield_curve_analysis"),
    )


def _run_analyst(monkeypatch, factory_path, llm, toolkit, state=None):
    module_name, factory_name = factory_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(f"{module_name}.capture_agent_prompt", lambda *a, **k: None)
    node = getattr(module, factory_name)(llm, toolkit)
    return node(state or {"company_of_interest": "AAPL", "trade_date": "2026-09-11",
                          "messages": []})


def _provider_failure(detail="provider outage"):
    return ProviderFailure(role="analysis", provider="openai", model="fixture-model",
                           attempts=1, category="permanent", detail=detail)


def test_R02_macro_tool_loop_provider_failure_stops_the_round(isolated, monkeypatch):
    llm = ScriptedLLM([
        tool_calls_message("get_macro_analysis"),  # first response demands tools
        _provider_failure(),                       # the loop's next LLM call fails
    ])
    with pytest.raises(ProviderFailure):
        _run_analyst(monkeypatch, "tradingagents.agents.analysts.macro_analyst.create_macro_analyst",
                     llm, _macro_toolkit())
    assert llm.invocations == 2  # raised out, not eaten as a generic iteration error


def test_R02_macro_fallback_provider_failure_stops_the_round(isolated, monkeypatch):
    llm = ScriptedLLM([
        tool_calls_message("no_such_tool"),            # all tools fail -> fallback
        AIMessage(content="interim"),                  # loop exits normally
        _provider_failure("provider outage in fallback"),
    ])
    with pytest.raises(ProviderFailure):
        _run_analyst(monkeypatch, "tradingagents.agents.analysts.macro_analyst.create_macro_analyst",
                     llm, _macro_toolkit())
    assert llm.invocations == 3  # the fallback LLM call failed closed


# ---------------------------------------------------------------------------
# R5 (H-08) — tool-loop exhaustion is a failed analyst, never a fake report
# ---------------------------------------------------------------------------

def _market_toolkit():
    return NS(
        config={"online_tools": False, "max_tool_iterations_per_agent": 1,
                "max_same_tool_call_repeats": 1},
        has_alpaca_credentials=lambda: False,
        get_stockstats_indicators_report=FakeTool("get_stockstats_indicators_report"),
    )


def _news_toolkit(live_date):
    return NS(
        config={"online_tools": False, "max_tool_iterations_per_agent": 1,
                "max_same_tool_call_repeats": 1},
        has_openai_web_search=lambda: False,
        has_finnhub=lambda: False,
        has_coindesk=lambda: False,
        get_google_news=FakeTool("get_google_news"),
    )


def _social_toolkit():
    return NS(
        config={"online_tools": False, "max_tool_iterations_per_agent": 1,
                "max_same_tool_call_repeats": 1},
        has_openai_web_search=lambda: False,
        get_reddit_news=FakeTool("get_reddit_news"),
        get_reddit_stock_info=FakeTool("get_reddit_stock_info"),
    )


def _fundamentals_toolkit():
    return NS(
        config={"online_tools": False, "max_tool_iterations_per_agent": 1,
                "max_same_tool_call_repeats": 1, "sec_ir_enabled": True},
        has_openai_web_search=lambda: False,
        has_finnhub=lambda: False,
        has_simfin_data=lambda: False,
        get_sec_ir_source=FakeTool("get_sec_ir_source"),
    )


def _macro_exhaust_toolkit():
    toolkit = _macro_toolkit()
    toolkit.config["max_tool_iterations_per_agent"] = 1
    return toolkit


_R5_CASES = [
    ("market", "tradingagents.agents.analysts.market_analyst.create_market_analyst",
     _market_toolkit, "get_stockstats_indicators_report", "market_report", "market"),
    ("news", "tradingagents.agents.analysts.news_analyst.create_news_analyst",
     None, "get_google_news", "news_report", "news"),  # toolkit needs the live date
    ("social", "tradingagents.agents.analysts.social_media_analyst.create_social_media_analyst",
     _social_toolkit, "get_reddit_stock_info", "sentiment_report", "social"),
    ("fundamentals", "tradingagents.agents.analysts.fundamentals_analyst.create_fundamentals_analyst",
     _fundamentals_toolkit, "get_sec_ir_source", "fundamentals_report", "fundamentals"),
    ("macro", "tradingagents.agents.analysts.macro_analyst.create_macro_analyst",
     _macro_exhaust_toolkit, "get_macro_analysis", "macro_report", "macro"),
]


@pytest.mark.parametrize("label,factory_path,toolkit_fn,tool_name,report_key,status_key",
                         _R5_CASES, ids=[c[0] for c in _R5_CASES])
def test_R05_tool_loop_exhaustion_yields_failed_analyst(
        isolated, monkeypatch, label, factory_path, toolkit_fn, tool_name,
        report_key, status_key):
    live_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    toolkit = (_news_toolkit(live_date) if label == "news" else toolkit_fn())
    # The model never stops demanding tool calls; max_iterations=1 exhausts it.
    llm = ScriptedLLM([
        tool_calls_message(tool_name),
        tool_calls_message(tool_name),
        tool_calls_message(tool_name),
    ])
    out = _run_analyst(monkeypatch, factory_path, llm, toolkit,
                       state={"company_of_interest": "AAPL", "trade_date": live_date,
                              "messages": []})
    assert out[report_key] == "", out[report_key]
    assert out["analysis_status"][status_key] == "failed"
    assert out["analysis_errors"][status_key]  # existing empty-report error
    assert "Tool-loop halted" not in str(out["messages"][-1].content)


# ---------------------------------------------------------------------------
# R3 (B-02) — unattended long-run never runs with an unlimited token budget
# ---------------------------------------------------------------------------

@pytest.fixture
def long_run_config(monkeypatch, tmp_path):
    from tradingagents.dataflows import config as cfg
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.long_run as lr

    monkeypatch.setenv("TRADINGAGENTS_LONG_RUN_DIR", str(tmp_path / "long_run"))
    monkeypatch.setattr("tradingagents.safety.guardrails._SAFETY_HOME",
                        tmp_path / "safety_home")
    config = {**DEFAULT_CONFIG, "auto_screening_enabled": True,
              "daily_llm_token_budget": 0,
              "data_cache_dir": str(tmp_path / "cache"), "alerts_enabled": False,
              "results_dir": str(tmp_path / "results")}
    monkeypatch.setattr(cfg, "_config", config)
    return NS(cfg=cfg, lr=lr, config=config)


@pytest.mark.parametrize("raw", [0, 0.0, 250_000, 20_000_000])
def test_R03_budget_zero_normalizes_to_hard_cap_and_values_pass(long_run_config, raw):
    ns = long_run_config
    ns.config["daily_llm_token_budget"] = raw
    ns.lr._validate_long_run_execution_config(dict(ns.config))
    stored = ns.cfg.get_config()["daily_llm_token_budget"]
    assert stored == (20_000_000 if raw == 0 else raw)
    assert stored > 0  # an unattended long run is never unlimited


@pytest.mark.parametrize("raw,code", [
    (-1, "LLM_BUDGET_INVALID"),
    (-0.5, "LLM_BUDGET_INVALID"),
    (20_000_001, "LLM_BUDGET_TOO_HIGH"),
    (1_000_000_000, "LLM_BUDGET_TOO_HIGH"),
    ("1000", "LLM_BUDGET_INVALID"),
    (None, "LLM_BUDGET_INVALID"),
    (True, "LLM_BUDGET_INVALID"),
    (float("nan"), "LLM_BUDGET_INVALID"),
    (float("inf"), "LLM_BUDGET_INVALID"),
])
def test_R03_budget_out_of_range_refuses_startup(long_run_config, raw, code):
    ns = long_run_config
    ns.config["daily_llm_token_budget"] = raw
    with pytest.raises(ns.lr.LongRunStop) as excinfo:
        ns.lr._validate_long_run_execution_config(dict(ns.config))
    assert excinfo.value.code == code
    assert "LLM_BUDGET" in excinfo.value.code


def test_R03_budget_zero_normalization_is_persisted_for_check_llm_budget(long_run_config):
    from tradingagents.safety import reset_safety_guard

    ns = long_run_config
    reset_safety_guard()
    ns.lr._validate_long_run_execution_config(dict(ns.config))
    from tradingagents.safety import get_safety_guard

    verdict = get_safety_guard().check_llm_budget()
    assert verdict.allowed
    # The lazily built guard sees the 20M cap, not 0=unlimited.
    assert get_safety_guard().config["daily_llm_token_budget"] == 20_000_000


# ---------------------------------------------------------------------------
# R4 (H-04) — a corrupt round journal is STATE_CORRUPT, never MISSED
# ---------------------------------------------------------------------------

def test_R04_corrupt_round_journal_stops_missed_settlement(isolated):
    import tradingagents.long_run as lr

    stamp = datetime(2026, 9, 8, 20, 1, tzinfo=ZoneInfo("UTC"))  # 16:01 ET, past close
    corrupt = b'{"status": "PENDING", "trunc'
    path = lr.round_path("run-r4", "2026-09-08")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(corrupt)
    with pytest.raises(lr.LongRunStop) as excinfo:
        lr.mark_session_missed_after_close(
            run_id="run-r4", session_date="2026-09-08", now=stamp,
            run_time_et="11:00",
            calendar_rows=[{"date": "2026-09-08", "open": "09:30", "close": "16:00"}],
        )
    assert excinfo.value.code == "STATE_CORRUPT"
    assert path.read_bytes() == corrupt  # the evidence was not overwritten


def test_R04_missing_journal_still_settles_missed(isolated):
    import tradingagents.long_run as lr

    stamp = datetime(2026, 9, 8, 20, 1, tzinfo=ZoneInfo("UTC"))
    settled = lr.mark_session_missed_after_close(
        run_id="run-r4b", session_date="2026-09-08", now=stamp,
        run_time_et="11:00",
        calendar_rows=[{"date": "2026-09-08", "open": "09:30", "close": "16:00"}],
    )
    assert settled is True
    assert lr.load_round_journal("run-r4b", "2026-09-08")["status"] == "MISSED"


# ---------------------------------------------------------------------------
# R6 (H-09) — run-log fallback recovery matches the metadata actually written
# ---------------------------------------------------------------------------

def test_R06_run_log_fallback_queries_analysis_source_key(isolated, monkeypatch):
    import tradingagents.long_run as lr

    captured = {}

    def fake_snapshot(symbol, session_date, *, eval_results_dir, metadata_match):
        captured["metadata_match"] = metadata_match
        return {"final_trade_intent": None}

    monkeypatch.setattr("tradingagents.run_logger.load_final_state_snapshot", fake_snapshot)
    lr._recover_intent_from_run_log(
        "AAPL", "2026-09-11", observation_id="obs-1", results_dir="eval_results",
    )
    assert captured["metadata_match"] == {
        "analysis_source": "long_run",
        "long_run_observation_id": "obs-1",
    }


# ---------------------------------------------------------------------------
# R7 (M-03) — run_time_et 16:00 is invalid; 09:30 and 15:59 stay valid
# ---------------------------------------------------------------------------

def test_R07_run_time_1600_is_rejected(isolated):
    import tradingagents.long_run as lr

    cfg = lr.default_long_run_config()
    cfg["run_time_et"] = "16:00"
    errors = lr.validate_long_run_config(cfg)
    assert any("09:30 <= run_time_et < 16:00 ET" in e for e in errors), errors


@pytest.mark.parametrize("run_time", ["09:30", "15:59", "11:00"])
def test_R07_valid_run_times_still_accepted(isolated, run_time):
    import tradingagents.long_run as lr

    cfg = lr.default_long_run_config()
    cfg["run_time_et"] = run_time
    errors = lr.validate_long_run_config(cfg)
    assert not any("run_time_et" in e for e in errors), errors


# ---------------------------------------------------------------------------
# R8 (M-04) — equity 0.0 is real data: fail-closed, not skipped
# ---------------------------------------------------------------------------

def test_R08_zero_equity_blocks_opening_on_concentration(isolated):
    e = isolated
    e.guard.config["max_symbol_concentration_pct"] = 10
    verdict = e.guard.check_order("AAPL", 1000.0,
                                  account={"equity": 0.0, "last_equity": None})
    assert not verdict.allowed
    assert "MAX_SYMBOL_CONCENTRATION" in verdict.reason_codes
    assert verdict.checks["concentration"]["status"] == "fail"


def test_R08_zero_equity_fires_drawdown_breaker(isolated):
    e = isolated
    e.guard.config["max_drawdown_halt_pct"] = 20
    e.guard.state_path.write_text(json.dumps(
        {"high_water_mark": 100000.0, "consecutive_rejections": 0, "llm_tokens": {}}))
    verdict = e.guard.check_order("AAPL", 1000.0,
                                  account={"equity": 0.0, "last_equity": None})
    assert not verdict.allowed
    assert "MAX_DRAWDOWN_HALT" in verdict.reason_codes


def test_R08_zero_last_equity_keeps_daily_loss_skipped(isolated):
    e = isolated
    e.guard.config["daily_loss_halt_pct"] = 5
    verdict = e.guard.check_order("AAPL", 1000.0,
                                  account={"equity": 90000.0, "last_equity": 0.0})
    assert verdict.allowed
    assert verdict.checks["daily_loss"]["status"] == "skipped"


def test_R08_none_equity_still_reports_unavailable(isolated):
    e = isolated
    e.guard.config["max_symbol_concentration_pct"] = 10
    verdict = e.guard.check_order("AAPL", 1000.0,
                                  account={"equity": None, "last_equity": None})
    assert verdict.allowed
    assert verdict.checks["concentration"]["status"] == "skipped"


def test_R08_risk_reducing_exit_still_bypasses_breakers(isolated):
    e = isolated
    e.guard.state_path.write_text(json.dumps(
        {"high_water_mark": 100000.0, "consecutive_rejections": 0, "llm_tokens": {}}))
    verdict = e.guard.check_order("AAPL", 1000.0, risk_reducing=True,
                                  account={"equity": 0.0, "last_equity": 100000.0})
    assert verdict.allowed  # deliberate policy: breakers never trap risk exits


# ---------------------------------------------------------------------------
# R9 (M-01) — token accounting counters survive the secret sanitizer
# ---------------------------------------------------------------------------

def test_R09_token_count_keys_are_not_masked():
    import tradingagents.long_run as lr

    payload = {
        "total_tokens": 12345, "input_tokens": 1, "output_tokens": 2,
        "prompt_tokens": 3, "completion_tokens": 4, "unpriced_tokens": 5,
        "llm_tokens": {"2026-09-12": 100},
    }
    out = lr.sanitize_for_log(payload)
    assert out == payload


@pytest.mark.parametrize("key", [
    "token", "api_token", "access_token", "refresh_token",
    "alert_telegram_bot_token", "OPENAI_API_KEY", "ALPACA_SECRET_KEY",
    "alpaca_api_key", "password",
])
def test_R09_secret_keys_still_masked(key):
    import tradingagents.long_run as lr

    assert lr.sanitize_for_log({key: "leak-me"})[key] == "***"


def test_R09_url_credentials_still_stripped():
    import tradingagents.long_run as lr

    out = lr.sanitize_for_log({"backend_url": "https://user:pass@example.com/x"})
    assert "pass" not in str(out) and "user" not in str(out)


# ---------------------------------------------------------------------------
# R10 (M-06) — only a missing SDK may fall back to the dict payload
# ---------------------------------------------------------------------------

def test_R10_sdk_present_construction_failure_propagates(isolated, monkeypatch):
    import alpaca.trading.requests as requests_mod

    class ExplodingRequest:
        def __init__(self, **kwargs):
            raise ValueError("deterministic request validation failure")

    monkeypatch.setattr(requests_mod, "MarketOrderRequest", ExplodingRequest)
    with pytest.raises(ValueError):
        _build_market_request("AAPL", "buy", 1000.0, None, "cid-1")


def test_R10_sdk_missing_returns_dict_fallback(isolated, monkeypatch):
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", None)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", None)
    payload = _build_market_request("AAPL", "buy", 1000.0, None, "cid-1")
    assert isinstance(payload, dict)
    assert payload["symbol"] == "AAPL" and payload["side"] == "buy"
    assert payload["time_in_force"] == "day" and payload["notional"] == 1000.0
    assert payload["client_order_id"] == "cid-1"


def test_R10_sdk_present_builds_real_request(isolated):
    from alpaca.trading.enums import OrderSide, TimeInForce

    request = _build_market_request("AAPL", "buy", 1000.0, None, "cid-1")
    assert request.symbol == "AAPL"
    assert request.side == OrderSide.BUY
    assert request.time_in_force == TimeInForce.DAY
    assert request.notional == 1000.0


def test_R10_no_size_yields_none(isolated):
    assert _build_market_request("AAPL", "buy", None, None, "cid-1") is None
