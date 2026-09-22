"""Offline boundary regressions for the unfinished 2026-09-17 repairs."""
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from test_execution_safety_plan_a import env, opening, now
from tradingagents.execution.authority import BrokerAuthorityError, capture_broker_snapshot
from tradingagents.execution.lifecycle import next_deadline_decision_id


def seed_attempt(service, did, status):
    prepared = service._prepare_liquidation_outbox("AAPL", decision_id=did, quantity=9, side="sell")
    row = prepared["order_rows"][0]
    service.store.sync_order_from_broker(row["order_id"], status, broker_order_id=None, filled_qty=0)
    return service.store.get_order(row["order_id"])


def test_deadline_rejected_exit_retries_once_then_closes(env, monkeypatch):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    future = now() + timedelta(days=6)
    monkeypatch.setattr("tradingagents.execution.service.utc_now", lambda: future)
    submit = broker.submit_order
    def reject(request):
        broker.submits.append(request)
        return {"success": False, "error": "definitive fixture rejection"}
    broker.submit_order = reject
    first = service.enforce_exit_deadlines()
    assert not first["success"] and broker.qty == 9
    first_id = broker.submits[-1].client_order_id
    broker.submit_order = submit
    second = service.enforce_exit_deadlines()
    assert second["success"], second
    assert broker.qty == 0 and len(broker.submits) == 3
    assert broker.submits[-1].client_order_id != first_id
    assert service.enforce_exit_deadlines()["deadline_exits"] == []


@pytest.mark.parametrize("status", ["UNKNOWN", "ACCEPTED", "PARTIAL", "SUBMITTING", "PENDING"])
def test_ambiguous_or_live_attempt_keeps_identity(env, status):
    service, _ = env
    seed_attempt(service, "deadline-fixture", status)
    def no_lookup(_):
        pytest.fail("live/ambiguous attempt must retain identity")
    assert next_deadline_decision_id(service.store, "deadline-fixture", "AAPL", no_lookup) == "deadline-fixture"


def test_terminal_attempt_requires_lookup_and_has_bounded_budget(env):
    service, _ = env
    for did in ("deadline-fixture", "deadline-fixture-attempt-1", "deadline-fixture-attempt-2"):
        seed_attempt(service, did, "REJECTED")
    with pytest.raises(BrokerAuthorityError, match="budget exhausted"):
        next_deadline_decision_id(service.store, "deadline-fixture", "AAPL", lambda _: None)
    assert len(service.store.list_all_orders()) == 3


@pytest.mark.parametrize("status,filled", [("accepted", 0), ("canceled", float("nan")), ("canceled", 1)])
def test_retry_refuses_live_or_unreconciled_terminal_facts(env, status, filled):
    service, _ = env
    row = seed_attempt(service, "deadline-fixture", "CANCELED")
    found = NS(id="prior", client_order_id=row["client_order_id"], symbol="AAPL", status=status, filled_qty=filled)
    with pytest.raises(BrokerAuthorityError):
        next_deadline_decision_id(service.store, "deadline-fixture", "AAPL", lambda _: found)


def test_recovery_records_missing_accepted_fill_idempotently(env):
    service, broker = env
    service.store.ensure_account_binding("audit-fixture")
    service.startup_recover()  # establish flat baseline
    row = seed_attempt(service, "accepted-short-fill", "ACCEPTED")
    found = NS(id="aged-order", client_order_id=row["client_order_id"], symbol="AAPL", side="sell",
               qty=9, status="filled", filled_qty=9, filled_avg_price=100, updated_at=now())
    broker.qty = -9
    broker.get_order_by_client_id = lambda _: found
    recovered = service.startup_recover()
    assert recovered["success"], recovered
    assert service.store.get_order(row["order_id"])["filled_qty"] == 9
    assert service.startup_recover()["success"]
    assert len(service.store.list_fills_since("")) == 1
    assert not broker.submits


def test_protection_is_required_per_remaining_lot_not_historic_symbol(env):
    service, broker = env
    def fill(did, side, stop, qty):
        payload = {"target_position": "LONG" if side == "buy" else "NEUTRAL",
                   "risk_controls": {"stop_loss_price": stop}}
        _, rows, _ = service.store.create_outbox(decision_id=did, run_id=None, symbol="AAPL",
            action="BUY" if side == "buy" else "SELL", target_position=payload["target_position"],
            payload_json=json.dumps(payload), orders=[dict(client_order_id="ta-"+did, symbol="AAPL",
                                                         side=side, quantity=qty, notional=None)])
        service.store.record_fill(execution_id=did, order_id=rows[0]["order_id"], qty=qty, price=100)
    fill("old-protected", "buy", 90, 4)
    fill("closed-old", "sell", None, 4)
    fill("new-unprotected", "buy", None, 4)
    broker.qty = 4
    assert service._protection_coverage_gaps(capture_broker_snapshot(broker)) == []


def test_responses_preserves_roles_call_ids_and_outputs():
    from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel
    model = GPT5ChatModel.__new__(GPT5ChatModel)
    items = model._convert_messages_to_input([SystemMessage(content="rules"), HumanMessage(content="query"),
        AIMessage(content="", tool_calls=[{"name": "prices", "args": {"ticker": "AAPL"}, "id": "call_1"}]),
        ToolMessage(content="100", tool_call_id="call_1")])
    assert items[0]["role"] == "developer" and items[1]["role"] == "user"
    assert items[2] == {"type": "function_call", "call_id": "call_1", "name": "prices", "arguments": '{"ticker": "AAPL"}'}
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "100"}


def test_final_marker_replaces_conflicting_old_decision():
    from tradingagents.agents.utils.agent_trading_modes import ensure_final_transaction_proposal, extract_recommendation
    text = "Reasoning\nFINAL TRANSACTION PROPOSAL: **BUY**\nFINAL TRANSACTION PROPOSAL: **SELL**"
    assert extract_recommendation(text, "investment") == "SELL"
    result = ensure_final_transaction_proposal(text, "SELL", "investment")
    assert result.count("FINAL TRANSACTION PROPOSAL:") == 1
    assert extract_recommendation(result, "investment") == "SELL"


@pytest.mark.parametrize("column,value", [("close", np.nan), ("open", np.inf), ("volume", -1), ("low", 999)])
def test_backtest_refuses_invalid_prices(column, value):
    from test_backtest_engine import make_prices
    from tradingagents.backtest import normalize_price_frame
    prices = make_prices([100, 101, 102]); prices.loc[1, column] = value
    with pytest.raises(ValueError):
        normalize_price_frame(prices)


def test_nonfinite_equity_and_trade_pnls_refused():
    from tradingagents.backtest import summarize_performance, win_rate
    with pytest.raises(ValueError): summarize_performance([100, np.nan])
    with pytest.raises(ValueError): win_rate([10, np.inf])


def test_offline_indicator_is_computed_at_requested_date(tmp_path):
    from tradingagents.dataflows.stockstats_utils import StockstatsUtils
    from test_backtest_engine import make_prices
    data = make_prices(list(range(100, 250)))
    cutoff = str(data.timestamp.iloc[110].date())
    data.loc[111:, "close"] = 100000  # future values must not affect the indicator
    data.to_csv(tmp_path / "AAPL-Alpaca-data-2015-01-01-2025-03-25.csv", index=False)
    result = StockstatsUtils.get_stock_stats("AAPL", "close_50_sma", cutoff, str(tmp_path), online=False)
    assert result is not None and not str(result).startswith("N/A"), result
    assert "185.5" in str(result), result








def test_report_coverage_refs_and_chunk_limit_are_truthful():
    from tradingagents.agents.utils.report_context import build_report_context_index, get_agent_context_bundle
    state = {"market_report": "\n".join(f"# Section {i}\nEntry price ${100+i} has risk target {120+i}% and strong trend.\nSecond detail is less relevant and longer than twenty characters." for i in range(5)),
             "news_report": "# News\nEarnings growth guidance offers +20% upside tomorrow.",
             "macro_report": "# Macro\nYield risk increases downside exposure by 10%."}
    config = {"report_context_max_chunks": 1, "report_context_chunk_chars": 100,
              "report_context_chunk_overlap": 20, "report_context_max_points_per_report": 5}
    context = build_report_context_index(state, config)
    assert len(context["reports"]["market_report"]["coverage_points"]) == 5
    assert all(f"Section {i}:" in context["reports"]["market_report"]["summary"] for i in range(5))
    chunks = {c["id"]: c for c in context["chunks"]}
    for claim in context["evidence_claims"]:
        assert claim["evidence_refs"]
        assert all(chunks[r]["section_title"] == claim["section_title"] for r in claim["evidence_refs"])
    bundle = get_agent_context_bundle(state, "default", "risk", config)
    assert len(bundle["selected_chunk_ids"]) == 1
    assert "refs=" in bundle["decision_claim_matrix"]


def test_screening_override_must_match_calendar():
    from test_phase_c_screening import _base_config, _NOW
    from tradingagents.screening.metrics import resolve_as_of
    config = _base_config()
    assert str(resolve_as_of(config, now=_NOW)) == "2026-09-04"
    config["screening_as_of_override"] = "2026-09-03"
    with pytest.raises(ValueError, match="authoritative"):
        resolve_as_of(config, now=_NOW)


def test_constant_price_regime_does_not_become_hostile():
    from tradingagents.regime import classify_regime
    from test_backtest_engine import make_prices
    regime = classify_regime(make_prices([100]*300))
    assert regime.metrics["vol_percentile"] == 0
    assert regime.volatility_state == "calm" and regime.label != "hostile"


@pytest.mark.parametrize("end", ["2026-01-06", "2026-01-06T10:00:00+00:00"])
def test_historical_request_preserves_exact_instant_or_includes_date(monkeypatch, end):
    from tradingagents.dataflows import alpaca_utils as mod
    seen = []
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2026-01-06T09:00:00Z")],
                          "open": [100], "high": [101], "low": [99], "close": [100], "volume": [10]})
    def fetch(request):
        seen.append(request)
        return NS(df=frame.set_index("timestamp"))
    monkeypatch.setattr(mod, "get_alpaca_stock_client", lambda: NS(get_stock_bars=fetch))
    assert not mod.AlpacaUtils.get_stock_data("AAPL", "2026-01-05", end).empty
    expected = pd.Timestamp(end) + pd.Timedelta(days=1) if len(end) == 10 else pd.Timestamp(end)
    actual = pd.Timestamp(seen[0].end)
    if expected.tzinfo is not None and actual.tzinfo is None:
        actual = actual.tz_localize("UTC")  # SDK serializes UTC as a naive timestamp
    assert actual == expected


def test_historical_window_passes_end_and_never_requests_today_quote(monkeypatch):
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils
    from tradingagents.dataflows.interface import get_alpaca_data
    from test_backtest_engine import make_prices
    calls = []
    def bars(**kwargs):
        calls.append(kwargs)
        return make_prices([100, 101, 102])
    monkeypatch.setattr(AlpacaUtils, "get_stock_data", bars)
    monkeypatch.setattr(AlpacaUtils, "get_latest_quote", lambda _: pytest.fail("historical quote lookahead"))
    AlpacaUtils.get_stock_data_window("AAPL", "2026-01-07", 5)
    assert calls[-1]["end_date"] == "2026-01-07"
    report = get_alpaca_data("AAPL", "2026-01-05", "2026-01-07")
    assert "Latest Real-Time Quote" not in report
