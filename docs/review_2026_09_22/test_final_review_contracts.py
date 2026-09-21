"""Offline review probes: assertions describe required behavior, not current bugs.

Run explicitly (outside the normal tests/ discovery):
PYTHONPATH=. .venv-p2/bin/python -m pytest docs/review_2026_09_22/test_final_review_contracts.py -q
All broker/model boundaries are fakes; sockets are disabled.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from scripts import run_analysis_ab as ab
from scripts.summarize_analysis_ab import summarize
from tradingagents.dataflows.config import get_config, replace_config, set_config
from tradingagents.experiments.evidence_snapshot import (
    _capture_source, evidence_packet_sha256, validate_evidence_completeness,
    EvidenceIntegrityError,
)
from tradingagents.safety import get_safety_guard, reset_safety_guard


DAY = "2026-09-21"


def packet(path, *, symbol, trade_date, **kwargs):
    value = {
        "schema_version": 1, "symbol": symbol, "trade_date": trade_date,
        "captured_at": "2026-09-21T14:00:00+00:00", "sources": [], "errors": [],
        **{key: {"source": {"status": "available", "value": "observed data"}}
           for key in ("market", "fundamentals", "news", "macro", "social")},
    }
    value["sha256"] = evidence_packet_sha256(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


class Graph:
    def __init__(self, selected_analysts, debug, config):
        self.config = config
        set_config(config)  # Same global installation as TradingAgentsGraph.

    def propagate(self, symbol, trade_date):
        return {"final_trade_intent": {"symbol": symbol}, "report_context": {}}, "HOLD"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    previous = get_config()
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setenv("TRADINGBUFFETT_EXECUTION_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(ab, "build_or_load_evidence_packet", packet)
    monkeypatch.setattr(ab, "_implementation_fingerprint", lambda: "f" * 64)
    monkeypatch.setattr(ab, "_paper_account_preflight", lambda **kw: {
        "traders": {"account_ref": "account-a"}, "berkshire": {"account_ref": "account-b"},
    })
    monkeypatch.setattr(ab, "_execution_order", lambda *a: ["traders", "berkshire"])
    reset_safety_guard()
    yield
    replace_config(previous)
    reset_safety_guard()


def run(root, **kwargs):
    return ab.run_analysis_ab(
        symbol=kwargs.pop("symbol", "AAPL"), trade_date=kwargs.pop("trade_date", DAY),
        results_root=root, base_config=kwargs.pop("base_config", deepcopy(ab.DEFAULT_CONFIG)),
        graph_cls=kwargs.pop("graph_cls", Graph), **kwargs,
    )


def test_R01_guard_switches_with_arm(tmp_path):
    seen = {}

    class RecordingGraph(Graph):
        def propagate(self, symbol, trade_date):
            guard = get_safety_guard()
            seen[self.config["analysis_backend"]] = str(guard.state_path)
            guard.record_llm_tokens(10)
            return super().propagate(symbol, trade_date)

    run(tmp_path, graph_cls=RecordingGraph)
    assert seen["traders"] != seen["berkshire"], seen


def test_R02_resume_cannot_change_paper_notional(tmp_path, monkeypatch):
    calls = []
    interrupted = False

    class FlakyGraph(Graph):
        def propagate(self, symbol, trade_date):
            nonlocal interrupted
            if self.config["analysis_backend"] == "berkshire" and not interrupted:
                interrupted = True
                raise TimeoutError("temporary interruption")
            return super().propagate(symbol, trade_date)

    def execute(backend, config, result, **kwargs):
        calls.append((backend, kwargs["paper_notional_usd"]))
        return {"success": True}

    monkeypatch.setattr(ab, "_execute_paper_arm", execute)
    assert run(tmp_path, graph_cls=FlakyGraph, execute_paper=True, paper_notional_usd=500)["status"] == "PARTIAL"
    try:
        run(tmp_path, graph_cls=FlakyGraph, execute_paper=True, paper_notional_usd=5000)
    except (RuntimeError, ValueError):
        return
    pytest.fail(f"changed ceiling accepted on resume: {calls}")


def test_R03_paused_execution_cannot_complete_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_execute_paper_arm", lambda *a, **k: {
        "success": True, "paused": True, "has_unknown": False,
        "error": "post-order reconciliation is PAUSED",
    })
    result = run(tmp_path, execute_paper=True, paper_notional_usd=500)
    assert result.get("status") == "FAILED_TERMINAL", result


def test_R04_invalid_risk_is_not_valid_shadow_hold(tmp_path):
    class InvalidRiskGraph(Graph):
        def propagate(self, symbol, trade_date):
            return {"final_trade_intent": None, "risk_invalid_reason": "schema_error"}, "HOLD"

    result = run(tmp_path, graph_cls=InvalidRiskGraph)
    assert result.get("status") == "FAILED_TERMINAL", result


def test_R05_summary_includes_failed_observations(tmp_path):
    run(tmp_path)

    class BrokenGraph(Graph):
        def propagate(self, symbol, trade_date):
            raise ValueError("invalid role JSON")

    assert run(tmp_path, symbol="MSFT", graph_cls=BrokenGraph)["status"] == "FAILED_TERMINAL"
    summary = summarize(tmp_path)
    assert summary["backends"]["traders"]["failed"] == 1, summary


def test_R06_successful_cli_returns_zero(tmp_path, monkeypatch):
    summary = run(tmp_path)
    monkeypatch.setattr(ab, "run_analysis_ab", lambda **kw: summary)
    assert ab.main(["--symbol", "AAPL", "--date", DAY]) == 0


def test_R07_berkshire_timeout_remains_retryable(tmp_path):
    from tradingagents.analysis_backends.berkshire.coordinator import create_berkshire_analysis_team

    class TimeoutLLM:
        def invoke(self, *args, **kwargs):
            raise TimeoutError("provider timed out")

    evidence_path = tmp_path / "evidence.json"
    evidence = packet(evidence_path, symbol="AAPL", trade_date=DAY)
    config = dict(ab.DEFAULT_CONFIG, results_dir=str(tmp_path),
                  evidence_packet_path=str(evidence_path), evidence_packet_sha256=evidence["sha256"])

    class BerkshireGraph(Graph):
        def propagate(self, symbol, trade_date):
            create_berkshire_analysis_team(TimeoutLLM(), self.config)({
                "company_of_interest": symbol, "trade_date": trade_date,
            })

    result = ab._run_one("berkshire", config, symbol="AAPL", trade_date=DAY,
                         selected_analysts=ab.FORMAL_ANALYSTS, debug=False, graph_cls=BerkshireGraph)
    assert result["status"] == "failed_retryable", result


@pytest.mark.parametrize("returned", ["Error: Tool timed out", {}, {"error": "unavailable"}])
def test_R08_error_response_is_not_usable_evidence(returned):
    section = {}
    toolkit = SimpleNamespace(source=lambda **kwargs: returned)
    _capture_source(toolkit, section=section, section_name="market", source_name="source",
                    tool_name="source", args={}, capability=None, live_only=False,
                    trade_date=DAY, sources=[], errors=[])
    with pytest.raises(EvidenceIntegrityError):
        validate_evidence_completeness({"market": section}, sections=("market",))


def test_R09_campaign_does_not_advance_past_partial_pair(tmp_path, monkeypatch):
    class FlakyGraph(Graph):
        def propagate(self, symbol, trade_date):
            if self.config["analysis_backend"] == "berkshire":
                raise TimeoutError("provider outage")
            return super().propagate(symbol, trade_date)

    monkeypatch.setattr(ab, "_execute_paper_arm", lambda *a, **k: {"success": True})
    assert run(tmp_path, graph_cls=FlakyGraph, execute_paper=True,
               paper_notional_usd=500)["status"] == "PARTIAL"
    with pytest.raises(RuntimeError):
        run(tmp_path, trade_date="2026-09-22", execute_paper=True,
            paper_notional_usd=500)


def test_R10_pair_state_is_fsynced(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ab.os, "fsync", lambda fd: calls.append(fd))
    ab._write_json_atomic(tmp_path / "pair_state.json", {"status": "analysis_completed"})
    assert len(calls) >= 2, "file and parent directory were not fsynced"


def test_R11_third_successful_analysis_can_resume_execution(tmp_path, monkeypatch):
    attempts = 0

    class ThirdTryGraph(Graph):
        def propagate(self, symbol, trade_date):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError("temporary outage")
            return super().propagate(symbol, trade_date)

    monkeypatch.setattr(ab, "_execute_paper_arm", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    for _ in range(2):
        assert run(tmp_path, graph_cls=ThirdTryGraph, execute_paper=True, paper_notional_usd=500)["status"] == "PARTIAL"
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, graph_cls=ThirdTryGraph, execute_paper=True, paper_notional_usd=500)
    monkeypatch.setattr(ab, "_execute_paper_arm", lambda *a, **k: {"success": True})
    result = run(tmp_path, graph_cls=ThirdTryGraph, execute_paper=True, paper_notional_usd=500)
    assert result.get("status") != "FAILED_TERMINAL", result


def test_R12_effective_endpoint_is_pinned(tmp_path, monkeypatch):
    from tradingagents.llm_clients.roles import resolve_role_config
    monkeypatch.setitem(ab.DEFAULT_CONFIG, "analysis_provider", "local_openai")
    monkeypatch.setenv("TRADINGBUFFETT_OPENAI_BASE_URL", "http://localhost:9875/v1")
    before = resolve_role_config(ab.DEFAULT_CONFIG)["analysis"].backend_url
    run(tmp_path)
    monkeypatch.setenv("TRADINGBUFFETT_OPENAI_BASE_URL", "http://localhost:9876/v1")
    assert resolve_role_config(ab.DEFAULT_CONFIG)["analysis"].backend_url != before
    with pytest.raises(RuntimeError):
        run(tmp_path, symbol="MSFT")


def test_R13_dedup_precedes_fresh_position_rules(tmp_path):
    from tradingagents.agents.schemas import RiskDecision, build_trade_intent_from_risk_decision
    from tradingagents.execution import ExecutionService, BrokerQuote, BrokerSnapshot
    from tradingagents.execution.authority import BrokerPosition
    from tradingagents.execution.store import client_order_id_for
    now = datetime.now(timezone.utc)
    intent = build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment", current_position="NEUTRAL", allow_shorts=False,
        trade_date=DAY, decision=RiskDecision(
            action="BUY", confidence="medium", risk_rationale="test", required_controls="stop",
            stop_loss_price=95, entry_policy={"status": "READY", "minimum_price": 99,
                "maximum_price": 101, "expires_at": (now + timedelta(hours=1)).isoformat(),
                "exit_by": (now + timedelta(days=5)).isoformat(), "confirmation": "observed"},
        ),
    ).model_dump(mode="json")
    service = ExecutionService(db_path=tmp_path / "execution.db")
    _, orders, _ = service.store.create_outbox(
        decision_id="same-decision", run_id="audit", symbol="AAPL", action="BUY", target_position="LONG",
        payload_json=json.dumps(intent), orders=[{
            "client_order_id": client_order_id_for("same-decision", "AAPL", "buy", role="open", seq=0),
            "symbol": "AAPL", "side": "buy", "quantity": 9, "notional": None,
        }],
    )
    for status in ("SUBMITTING", "ACCEPTED", "FILLED"):
        service.store.transition_order(orders[0]["order_id"], status, broker_order_id="order-1")
    snapshot = BrokerSnapshot(now, "snapshot", "paper-a", 100000, 100000, 99100, 99100,
                              (BrokerPosition("AAPL", 9, 900),), (), (), 900)
    result = service._execute_core(trade_intent=intent, decision_id="same-decision", dollar_amount=1000,
                                   _snapshot=snapshot, _quote=BrokerQuote("AAPL", 100, 100.2, now))
    assert result.get("deduped") is True, json.dumps(result)


def test_R14_ab_obeys_daily_llm_budget(tmp_path):
    from tradingagents.safety.guardrails import SafetyGuard
    base = dict(ab.DEFAULT_CONFIG, daily_llm_token_budget=10)
    configs = ab.build_ab_configs(base, symbol="AAPL", trade_date=DAY,
                                  pair_dir=tmp_path / DAY / "AAPL", experiment_root=tmp_path)
    for config in configs.values():
        guard = SafetyGuard(config=config, state_path=Path(config["safety_state_path"]),
                            kill_switch_path=Path(config["safety_kill_switch_path"]))
        guard.record_llm_tokens(11)
        assert guard.check_llm_budget().allowed is False
    calls = []

    class CountingGraph(Graph):
        def propagate(self, symbol, trade_date):
            calls.append(self.config["analysis_backend"])
            return super().propagate(symbol, trade_date)

    try:
        run(tmp_path, base_config=base, graph_cls=CountingGraph)
    except RuntimeError:
        pass
    assert calls == [], f"analysis started despite exhausted budgets: {calls}"


def test_R15_early_close_equal_target_adjusts_before_close():
    from datetime import date, time
    from tradingagents.long_run import effective_target_for_session
    result = effective_target_for_session(
        date(2026, 11, 27), "13:00",
        calendar_rows=[{"date": date(2026, 11, 27), "open": time(9, 30), "close": time(13, 0)}],
    )
    assert result["effective_target"] == "12:30", result
