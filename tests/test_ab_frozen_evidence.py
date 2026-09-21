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
        "fundamentals": {}, "news": {}, "macro": {}, "social": {},
        "sources": [], "errors": [],
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    return packet


class FrozenEvidenceTests(unittest.TestCase):
    def test_builder_captures_sources_once_then_only_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            first_toolkit = CountingEvidenceToolkit()
            first = build_or_load_evidence_packet(
                path, symbol="NVDA", trade_date="2026-09-21", toolkit=first_toolkit
            )
            call_count = len(first_toolkit.calls)
            second = build_or_load_evidence_packet(
                path, symbol="NVDA", trade_date="2026-09-21",
                toolkit=NoNetworkToolkit(path),
            )
        self.assertGreater(call_count, 0)
        self.assertEqual(first["sha256"], second["sha256"])

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
