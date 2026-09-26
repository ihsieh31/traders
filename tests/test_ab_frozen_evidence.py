import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from tradingagents.agents.analysts.frozen import create_frozen_analyst
from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    build_or_load_evidence_packet,
    evidence_packet_sha256,
    load_evidence_packet,
    validate_evidence_completeness,
    classify_source_value,
)


class NoNetworkToolkit:
    def __init__(self, path):
        self.config = {"evidence_packet_path": str(path)}

    def __getattr__(self, name):
        raise AssertionError(f"network/tool access during frozen analysis: {name}")


class FakeLLM:
    def invoke(self, messages):
        return AIMessage(content="evidence-only report")


class CountingEvidenceToolkit:
    config = {"online_tools": False}

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("has_"):
            return lambda: False

        def source(**_kwargs):
            self.calls.append(name)
            return f"fixture:{name}"

        return source


def _valid_packet(symbol="NVDA", trade_date="2026-09-21"):
    packet = {
        "schema_version": 1,
        "symbol": symbol,
        "trade_date": trade_date,
        "captured_at": "2026-09-21T00:00:00+00:00",
        "market": {"ohlcv": {"status": "available", "value": "fixture"}},
        "fundamentals": {"fixture": {"status": "available", "value": "fixture"}},
        "news": {"fixture": {"status": "available", "value": "fixture"}},
        "macro": {"fixture": {"status": "available", "value": "fixture"}},
        "social": {"fixture": {"status": "available", "value": "fixture"}},
        "sources": [], "errors": [],
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    return packet


class FrozenEvidenceTests(unittest.TestCase):
    def test_adapter_failure_envelopes_are_not_available(self):
        for value in ('{"error":"provider failed"}', 'Error getting indicators',
                      'No indicator data available for AAPL', '**Error** FRED unavailable'):
            self.assertNotEqual(classify_source_value(value), "available")

    def test_macro_report_error_line_is_not_available(self):
        # The FRED adapter appends one "### <indicator>\n**Error**: ..." line
        # per failed series, so the marker sits mid-document, not at the front.
        report = "### Real GDP\nvalue 2.1\n\n### CPI\n**Error**: 503 from FRED\n"
        self.assertEqual(classify_source_value(report), "error")
        # A fully populated report of the same shape is real evidence.
        self.assertEqual(
            classify_source_value("### Real GDP\n2.1\n\n### CPI\n3.1\n"), "available")
        # A bolded word inside prose is content, not a transport failure.
        self.assertEqual(
            classify_source_value("# Notes\nThe vendor raised an **Error** today.\n"),
            "available")

    def test_genuinely_empty_search_result_is_available(self):
        """Zero hits is a real answer, not a provider outage."""
        for value in ("No results found for 'acme corp'", '{"results": []}',
                      {"data": [], "count": 0}):
            self.assertEqual(classify_source_value(value), "available")

    def test_incomplete_capture_is_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            first_toolkit = CountingEvidenceToolkit()
            with self.assertRaisesRegex(EvidenceIntegrityError, "completeness"):
                build_or_load_evidence_packet(
                    path, symbol="NVDA", trade_date="2026-09-21", toolkit=first_toolkit
                )
            self.assertGreater(len(first_toolkit.calls), 0)
            self.assertFalse(path.exists())

    def test_packet_round_trip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            packet = _valid_packet()
            path.write_text(json.dumps(packet), encoding="utf-8")
            loaded = build_or_load_evidence_packet(
                path, symbol="NVDA", trade_date="2026-09-21",
                config={"evidence_packet_sha256": packet["sha256"]},
            )
            self.assertEqual(loaded["sha256"], packet["sha256"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["market"]["ohlcv"]["value"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(EvidenceIntegrityError):
                build_or_load_evidence_packet(path, symbol="NVDA", trade_date="2026-09-21")

    def test_pinned_hash_rejects_tamper_even_when_attacker_recomputes_self_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            packet = _valid_packet()
            pinned = packet["sha256"]
            packet["market"]["ohlcv"]["value"] = "tampered"
            packet["sha256"] = evidence_packet_sha256(packet)
            path.write_text(json.dumps(packet), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceIntegrityError, "pinned"):
                load_evidence_packet(
                    path,
                    symbol="NVDA",
                    trade_date="2026-09-21",
                    expected_sha256=pinned,
                )

    def test_completeness_requires_a_usable_source_in_every_section(self):
        packet = _valid_packet()
        packet["fundamentals"] = {}
        with self.assertRaisesRegex(EvidenceIntegrityError, "fundamentals"):
            validate_evidence_completeness(packet)

    def test_frozen_analyst_never_accesses_toolkit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            path.write_text(json.dumps(_valid_packet()), encoding="utf-8")
            node = create_frozen_analyst(FakeLLM(), NoNetworkToolkit(path), "market", "market_report")
            result = node({"company_of_interest": "NVDA", "trade_date": "2026-09-21"})
        self.assertEqual(result["analysis_status"]["market"], "completed")
        self.assertTrue(result["market_report"])

    def test_packet_identity_cannot_cross_symbol_or_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            path.write_text(json.dumps(_valid_packet()), encoding="utf-8")
            with self.assertRaises(EvidenceIntegrityError):
                build_or_load_evidence_packet(path, symbol="AAPL", trade_date="2026-09-21")
            with self.assertRaises(EvidenceIntegrityError):
                build_or_load_evidence_packet(path, symbol="NVDA", trade_date="2026-09-22")


if __name__ == "__main__":
    unittest.main()
