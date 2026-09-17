"""U02/U11 regression tests: loop-reset must clear update counters and debate state.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4:
- U02: reset_for_loop does not clear ``<report>_update_count`` keys, so the
  >15 hard block in process_chunk_updates trips during loop round 17 and
  every later round reports nothing (reproduced in audit: count 16 ->
  report=None).
- U11: reset_for_loop does not clear the investment debate state, so the
  UI shows the previous round's bull/bear history after reset
  (reproduced: history=OLD).
"""

from webui.utils.state import AppState


def _seed_round_one(state, symbol="AAPL"):
    state.init_symbol_state(symbol)
    state.symbol_states[symbol]["current_reports"]["market_report"] = "round one"
    state.symbol_states[symbol]["market_report_update_count"] = 16
    state.symbol_states[symbol]["investment_debate_state"] = {
        "bull_history": "OLD",
        "bear_history": "OLD",
        "judge_decision": "OLD",
    }
    state.symbol_states[symbol]["current_reports"]["bull_report"] = "OLD bull"
    state.symbol_states[symbol]["current_reports"]["bear_report"] = "OLD bear"
    state.symbol_states[symbol]["risk_debate_state"] = {"history": "OLD risk"}
    state.analyzing_symbol = symbol
    state.current_symbol = symbol


def test_u02_reset_clears_report_update_counts():
    state = AppState()
    _seed_round_one(state)
    state.reset_for_loop()

    assert state.symbol_states["AAPL"]["market_report_update_count"] == 0
    for key in state.symbol_states["AAPL"]:
        assert not key.endswith("_update_count") or state.symbol_states["AAPL"][key] == 0

    # Round 2: the scheduler re-queues the symbol and dispatches it, then
    # the same analyst can still publish — the >15 hard block in
    # process_chunk_updates must not trip on the previous round's counter.
    state.add_symbols_to_queue(["AAPL"])
    assert state.get_next_symbol() == "AAPL"
    state.symbol_states["AAPL"]["agent_statuses"]["Market Analyst"] = "in_progress"
    state.process_chunk_updates(
        {"market_report": "round two report"},
        symbol="AAPL",
        run_generation=state.run_generation,
    )
    assert state.symbol_states["AAPL"]["current_reports"]["market_report"] == "round two report"


def test_u11_reset_clears_debate_state():
    state = AppState()
    _seed_round_one(state)
    state.reset_for_loop()

    assert state.symbol_states["AAPL"]["investment_debate_state"] is None
    assert "risk_debate_state" not in state.symbol_states["AAPL"]
    assert state.symbol_states["AAPL"]["current_reports"]["bull_report"] is None
    assert state.symbol_states["AAPL"]["current_reports"]["bear_report"] is None


def test_reset_preserves_pagination_fields():
    state = AppState()
    _seed_round_one(state)
    state.symbol_states["AAPL"]["ticker_symbol"] = "AAPL"
    state.symbol_states["AAPL"]["chart_data"] = {"closes": [1, 2]}
    state.symbol_states["AAPL"]["chart_period"] = "6mo"
    state.reset_for_loop()

    kept = state.symbol_states["AAPL"]
    assert kept["ticker_symbol"] == "AAPL"
    assert kept["chart_data"] == {"closes": [1, 2]}
    assert kept["chart_period"] == "6mo"
