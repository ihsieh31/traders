import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from tradingagents.analysis_backends.berkshire.coordinator import (
    BerkshireAnalysisError,
    create_berkshire_analysis_team,
)
from tradingagents.experiments.evidence_snapshot import evidence_packet_sha256
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup


def _packet(symbol="NVDA", trade_date="2026-09-21"):
    packet = {
        "schema_version": 1,
        "symbol": symbol,
        "trade_date": trade_date,
        "captured_at": "2026-09-21T00:00:00+00:00",
        "market": {}, "fundamentals": {}, "news": {}, "macro": {}, "social": {},
        "sources": [], "errors": [],
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    return packet


class FakeRoleLLM:
    def __init__(self, fail_role=None):
        self.fail_role = fail_role
        self.calls = []

    def invoke(self, messages):
        text = str(messages[-1].content)
        self.calls.append(text)
        if "exactly these non-empty string keys" in text:
            return AIMessage(content=json.dumps({
                "market_report": "price and market structure",
                "sentiment_report": "insufficient sentiment evidence",
                "news_report": "recent events and competitive moves",
                "fundamentals_report": "business, financial quality, and valuation",
                "macro_report": "rates, cycle, and structural trend",
            }))
        for role in ("business_analyst", "financial_analyst", "industry_researcher", "risk_assessor"):
            if f"Role name: {role}" in text:
                if role == self.fail_role:
                    raise RuntimeError("provider fixture failure")
                return AIMessage(content=json.dumps({
                    "role": role,
                    "thesis": f"{role} evidence thesis",
                    "key_facts": [f"{role} fact"],
                    "uncertainties": [],
                    "risks": [f"{role} risk"],
                    "valuation_notes": [f"{role} valuation note"],
                    "evidence_refs": ["market"],
                }))
        raise AssertionError("unknown Berkshire prompt")


class BerkshireAnalysisBackendTests(unittest.TestCase):
    def test_backend_switch_changes_only_upstream_topology(self):
        class LLM:
            pass

        selected = ["market", "social", "news", "fundamentals", "macro"]
        node_sets = {}
        for backend in ("traders", "berkshire"):
            config = {
                "analysis_backend": backend,
                "analysis_profile": "traders",
                "parallel_analysts": True,
                "parallel_risk_first_round": True,
            }
            toolkit = type("ToolkitFixture", (), {"config": config})()
            setup = GraphSetup(
                LLM(), LLM(), toolkit,
                {name: object() for name in selected},
                None, None, None, None, None,
                ConditionalLogic(), config,
            )
            node_sets[backend] = set(setup.setup_graph(selected).nodes)

        self.assertIn("Berkshire Analysis Team", node_sets["berkshire"])
        self.assertNotIn("Market Analyst", node_sets["berkshire"])
        downstream = {
            "Build Report Context", "Bull Researcher", "Bear Researcher",
            "Research Manager", "Trader", "Risky Analyst", "Safe Analyst",
            "Neutral Analyst", "Risk Judge",
        }
        self.assertTrue(downstream <= node_sets["traders"])
        self.assertTrue(downstream <= node_sets["berkshire"])

    def test_four_roles_and_team_lead_produce_canonical_bundle(self):
        llm = FakeRoleLLM()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            path.write_text(json.dumps(_packet()), encoding="utf-8")
            node = create_berkshire_analysis_team(llm, {"evidence_packet_path": str(path)})
            result = node({"company_of_interest": "NVDA", "trade_date": "2026-09-21"})

        self.assertEqual(len(llm.calls), 5)
        self.assertEqual(result["analysis_backend"], "berkshire")
        self.assertTrue(all(result[key] for key in (
            "market_report", "sentiment_report", "news_report",
            "fundamentals_report", "macro_report",
        )))
        self.assertEqual(
            set(result["analysis_backend_meta"]) - {"backend", "evidence_sha256", "team_lead"},
            {"business_analyst", "financial_analyst", "industry_researcher", "risk_assessor"},
        )
        self.assertFalse("TradeIntent" in json.dumps({key: value for key, value in result.items() if key != "messages"}))

    def test_any_role_failure_blocks_team_lead(self):
        llm = FakeRoleLLM(fail_role="risk_assessor")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence_packet.json"
            path.write_text(json.dumps(_packet()), encoding="utf-8")
            node = create_berkshire_analysis_team(llm, {"evidence_packet_path": str(path)})
            with self.assertRaises(BerkshireAnalysisError):
                node({"company_of_interest": "NVDA", "trade_date": "2026-09-21"})
        self.assertEqual(len(llm.calls), 4)


if __name__ == "__main__":
    unittest.main()
