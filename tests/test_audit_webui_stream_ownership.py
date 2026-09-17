import ast
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

import pytest

from webui.utils.state import AppState


def make_state():
    state = AppState()
    state.init_symbol_state("AAPL")
    state.init_symbol_state("MSFT")
    state.current_symbol = "AAPL"
    state.analyzing_symbol = "MSFT"
    return state


def test_stopped_run_cannot_update_reports_after_restart():
    state = make_state()
    dispatched_generation = state.run_generation
    state.request_stop()
    state.stop_requested = False
    before = deepcopy(state.symbol_states)
    state.needs_ui_update = False

    state.process_chunk_updates(
        {"market_report": "stale MSFT report"},
        symbol="MSFT",
        run_generation=dispatched_generation,
    )

    assert state.symbol_states == before
    assert state.generated_reports_count == 0
    assert state.needs_ui_update is False


def test_chunk_cannot_follow_mutable_analyzing_symbol():
    state = make_state()
    dispatched_generation = state.run_generation
    state.analyzing_symbol = "AAPL"
    before = deepcopy(state.symbol_states)

    state.process_chunk_updates(
        {"market_report": "MSFT report"},
        symbol="MSFT",
        run_generation=dispatched_generation,
    )

    assert state.symbol_states == before
    assert state.generated_reports_count == 0


def test_current_run_updates_analysis_symbol_not_display_symbol():
    state = make_state()

    state.process_chunk_updates(
        {"market_report": "current MSFT report"},
        symbol="MSFT",
        run_generation=state.run_generation,
    )

    assert state.symbol_states["MSFT"]["current_reports"]["market_report"] == "current MSFT report"
    assert state.symbol_states["AAPL"]["current_reports"]["market_report"] is None
    assert state.generated_reports_count == 1


def test_startup_does_not_relabel_market_evidence(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "webui/app_dash.py"
    tree = ast.parse(source.read_text())
    patch_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "apply_sequential_mode_fix"]
    monkeypatch.setattr(AppState, "process_chunk_updates", AppState.process_chunk_updates)
    monkeypatch.setattr(AppState, "_mapping_fix_applied", False, raising=False)
    monkeypatch.delattr(AppState, "_mapping_fix_applied")
    if patch_nodes:
        namespace = {}
        exec(compile(ast.Module(body=patch_nodes, type_ignores=[]), str(source), "exec"), namespace)
        namespace["apply_sequential_mode_fix"]()
    state = make_state()
    state.symbol_states["AAPL"]["agent_statuses"]["Social Analyst"] = "in_progress"
    chunk = {"market_report": "MSFT market evidence"}
    state.process_chunk_updates(chunk)
    assert chunk == {"market_report": "MSFT market evidence"}
    assert state.symbol_states["MSFT"]["current_reports"]["market_report"] == "MSFT market evidence"
    assert state.symbol_states["MSFT"]["current_reports"]["sentiment_report"] is None


@pytest.mark.parametrize("transition", ["request_stop", "stop_loop_mode", "stop_market_hour_mode", "get_next_symbol"])
def test_ownership_transition_waits_for_chunk_update(transition):
    state = make_state()
    state.analysis_queue = ["AAPL"]
    entered = Event()
    release = Event()
    transitioned = Event()
    original = state.get_state

    def blocking_get(symbol):
        entered.set()
        assert release.wait(3)
        return original(symbol)

    state.get_state = blocking_get
    updater = Thread(target=lambda: state.process_chunk_updates(
        {"market_report": "current"}, symbol="MSFT", run_generation=0))
    def change_owner():
        getattr(state, transition)()
        transitioned.set()
    changer = Thread(target=change_owner)
    updater.start()
    try:
        assert entered.wait(3)
        changer.start()
        assert not transitioned.wait(0.1)
    finally:
        release.set()
        updater.join(3)
        if changer.ident is not None:
            changer.join(3)
    assert transitioned.is_set()
