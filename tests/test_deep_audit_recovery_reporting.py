"""Recovery authority and corrupted local evidence, without network calls."""
import json
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from test_ab_frozen_evidence import _valid_packet
from tradingagents.long_run_support import reporting, round_support, state
from tradingagents.llm_clients import usage


def test_run_log_recovery_requires_the_pinned_packet(monkeypatch):
    packet = _valid_packet()
    monkeypatch.setattr("tradingagents.run_logger.load_final_state_snapshot", lambda *a, **k: {
        "analysis_evidence": packet, "final_trade_intent": {"action": "BUY"}})
    kwargs = dict(observation_id="audit", results_dir="unused", _normalize_intent=lambda x: x)
    assert round_support._recover_intent_from_run_log(
        "NVDA", "2026-09-21", expected_evidence_sha256="0" * 64, **kwargs) is None
    assert round_support._recover_intent_from_run_log(
        "NVDA", "2026-09-21", expected_evidence_sha256=packet["sha256"], **kwargs) == {"action": "BUY"}


@pytest.mark.parametrize("status", ["ANALYZING", "ANALYZED", "EXECUTING"])
def test_resumed_symbol_checks_persisted_evidence_before_recovery_or_execution(monkeypatch, tmp_path, status):
    from unittest.mock import Mock
    from tradingagents import long_run as lr

    packet = _valid_packet()
    path = tmp_path / "packet.json"
    path.write_text(json.dumps({**packet, "symbol": "WRONG"}))
    journal = lr.new_round_journal("2026-09-21", ["NVDA"])
    journal["symbols"]["NVDA"].update(status=status, evidence_packet_path=str(path),
        evidence_packet_sha256=packet["sha256"], trade_intent={"action": "BUY"})
    service = Mock()
    execute = Mock()
    monkeypatch.setattr(lr, "_execute_intent", execute)
    monkeypatch.setattr(lr, "_build_graph_config", lambda *a, **k: {})
    prepared = lr._PreparedDailyRound(
        run_id="audit-resume", session_date="2026-09-21", long_cfg={},
        runtime={"shared_evidence_dir": str(tmp_path)}, schedule_info={}, deps=lr.LongRunDeps(),
        ends_at=None, journal=journal, service=service, graph_factory=None, broker_factory=None,
        recovery_can_submit=lambda: True, session_submit_allowed=lambda: True)
    with pytest.raises(lr.LongRunStop, match="STATE_CORRUPT"):
        lr._run_prepared_round_symbols(prepared)
    execute.assert_not_called()
    service.startup_recover.assert_not_called()


def test_round_journal_redacts_source_errors_before_durable_write(monkeypatch, tmp_path):
    fake = "FAKE-JOURNAL-SECRET-0123456789"
    monkeypatch.setenv("TRADINGBUFFETT_AUDIT_API_KEY", fake)
    path = tmp_path / "round.json"
    state.save_round_journal("audit", {"session_date": "2026-09-21", "error": f"provider {fake}"},
                             atomic_write_json=state.atomic_write_json, round_path=lambda *a: path)
    assert fake not in path.read_text()


@pytest.mark.parametrize("run_id", ["..", ".", "../escape", "/tmp/escape", "a/b"])
def test_run_identity_cannot_escape_its_storage_namespace(run_id, tmp_path):
    with pytest.raises(ValueError):
        state.run_dir(run_id, base_dir=lambda: tmp_path)


@pytest.mark.parametrize("bad", ["{broken", "null", "[]", '{"equity": "NaN"}'])
def test_corrupt_snapshots_are_visible_and_disable_performance_claims(tmp_path, bad):
    path = tmp_path / "account_snapshots.jsonl"
    path.write_text('{"equity": 100, "phase": "pre_round"}\n' + bad + '\n'
                    '{"equity": 110, "phase": "final"}\n')
    rows = reporting._load_snapshots("audit", run_dir=lambda _: tmp_path)
    report = reporting.aggregate_final_report(
        {"run_id": "audit", "started_at": "2026-09-21T00:00:00+00:00"}, {}, {},
        _load_snapshots=lambda _: rows, _load_rounds=lambda _: ([], []),
        compute_drawdown=reporting.compute_drawdown, _sum_unrealized=reporting._sum_unrealized,
        run_dir=lambda _: tmp_path, SYMBOL_DONE="DONE", datetime=datetime)
    assert report["evidence"]["snapshot_errors"]
    assert report["account"]["total_return"] is None
    assert report["account"]["max_drawdown"] is None
    assert bad in path.read_text()


@pytest.mark.parametrize("bad", [None, "NaN", "oops"])
def test_unrealized_pnl_never_reports_partial_positions_as_total(bad):
    assert reporting._sum_unrealized([{"unrealized_pl": 10}, {"unrealized_pl": bad}]) is None


def test_usage_overflow_is_unavailable_and_callback_always_cleans_start():
    assert usage.normalize_usage_map({"total_tokens": float("inf")})["total_tokens"] == 0
    callback = usage.UsageAccountingCallback()
    callback.on_llm_start({}, [], run_id="audit-call")
    response = NS(generations=[NS(message=NS(additional_kwargs={"usage_accounted_by_adapter": True}))])
    callback.on_llm_end(response, run_id="audit-call")
    assert "audit-call" not in usage._START_TIMES
