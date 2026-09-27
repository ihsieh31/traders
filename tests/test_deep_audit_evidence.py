from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_ab_frozen_evidence import _valid_packet
from tradingagents.agents.analysts.frozen import create_frozen_analyst
from tradingagents.analysis_backends.berkshire.coordinator import _packet_from_state
from tradingagents.experiments import evidence_snapshot as evidence
from tradingagents.redaction import sanitize_for_log


def test_diagnostic_report_redacts_exception_and_context(monkeypatch):
    from tradingagents.error_diagnostics import ErrorDiagnostics

    fake_key = "FAKE-DIAGNOSTIC-KEY-0123456789"
    monkeypatch.setenv("TRADINGBUFFETT_AUDIT_API_KEY", fake_key)
    report = ErrorDiagnostics.generate_error_report(f"provider refused {fake_key}",
                                                    context={"api_key": "short-secret"})
    assert fake_key not in report
    assert "short-secret" not in report


def test_provider_failure_is_redacted_on_console(monkeypatch, capsys):
    from tradingagents.dataflows import alpaca_utils

    fake_key = "FAKE-CONSOLE-KEY-0123456789"
    monkeypatch.setenv("TRADINGBUFFETT_AUDIT_API_KEY", fake_key)
    monkeypatch.setattr(alpaca_utils, "get_alpaca_trading_client",
                        Mock(side_effect=RuntimeError(f"provider failed {fake_key}")))
    alpaca_utils.AlpacaUtils.get_company_name("AUDIT")
    assert fake_key not in capsys.readouterr().out


@pytest.mark.parametrize("value", [" \n\t", "[]", "{}", "null",
    "UNAVAILABLE_FOR_HISTORICAL_AS_OF: source has no verified point-in-time cutoff"])
def test_unavailable_evidence_never_counts_as_a_complete_section(value):
    packet = _valid_packet()
    packet["macro"]["fixture"]["value"] = value
    with pytest.raises(evidence.EvidenceIntegrityError, match="macro"):
        evidence.validate_evidence_completeness(packet)


@pytest.mark.parametrize("backend", ["traders", "berkshire"])
@pytest.mark.parametrize("corruption", ["content", "symbol", "pin", "completeness"])
def test_in_memory_evidence_is_checked_before_any_llm_call(backend, corruption):
    packet = _valid_packet()
    pinned = packet["sha256"]
    if corruption == "content":
        packet["market"]["ohlcv"]["value"] = "altered without resealing"
    elif corruption == "symbol":
        packet["symbol"] = "AAPL"
        packet["sha256"] = evidence.evidence_packet_sha256(packet)
    elif corruption == "pin":
        packet["market"]["ohlcv"]["value"] = "altered and resealed"
        packet["sha256"] = evidence.evidence_packet_sha256(packet)
    else:
        packet["macro"] = {}
        packet["sha256"] = evidence.evidence_packet_sha256(packet)
        pinned = packet["sha256"]
    state = dict(company_of_interest="NVDA", trade_date="2026-09-21", analysis_evidence=packet)
    config = {"evidence_packet_sha256": pinned}
    llm = Mock()
    with pytest.raises((evidence.EvidenceIntegrityError, RuntimeError)):
        if backend == "traders":
            create_frozen_analyst(llm, NS(config=config), "market", "market_report")(state)
        else:
            _packet_from_state(state, config)
    llm.invoke.assert_not_called()


def test_rotated_and_runtime_credentials_are_redacted(monkeypatch):
    from tradingagents.dataflows.config import set_runtime_api_keys, clear_runtime_api_keys

    sanitize_for_log("prime cache before credentials exist")
    fake_env = "FAKE-ROTATED-KEY-0123456789"
    fake_runtime = "FAKE-RUNTIME-KEY-9876543210"
    monkeypatch.setenv("TRADINGBUFFETT_AUDIT_API_KEY", fake_env)
    set_runtime_api_keys({"audit_api_key": fake_runtime})
    try:
        result = sanitize_for_log({"error": f"request rejected: {fake_env} {fake_runtime}"})
        assert fake_env not in result["error"]
        assert fake_runtime not in result["error"]
    finally:
        clear_runtime_api_keys()


def test_source_error_is_redacted_before_evidence_hash_and_persistence(monkeypatch, tmp_path):
    fake_key = "FAKE-EVIDENCE-KEY-0123456789"
    monkeypatch.setenv("TRADINGBUFFETT_AUDIT_API_KEY", fake_key)

    class Toolkit:
        config = {"online_tools": False}

        def __getattr__(self, name):
            if name.startswith("has_"):
                return lambda: True
            if name == "get_sec_ir_source":
                def fail(**kwargs):
                    raise RuntimeError(f"transport included {fake_key}")
                return fail
            return lambda **kwargs: "known evidence"

    monkeypatch.setattr(evidence, "_market_today_iso", lambda: "2026-09-21")
    path = tmp_path / "evidence.json"
    packet = evidence.build_or_load_evidence_packet(
        path, symbol="NVDA", trade_date="2026-09-21", toolkit=Toolkit())
    assert fake_key not in path.read_text()
    assert packet["sha256"] == evidence.evidence_packet_sha256(packet)
    assert evidence.load_evidence_packet(path, symbol="NVDA", trade_date="2026-09-21") == packet
