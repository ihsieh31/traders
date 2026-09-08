import unittest

from tradingagents.agents.utils.report_context import (
    build_report_context_index,
    get_agent_context_bundle,
)


class ReportContextEvidenceScoringTests(unittest.TestCase):
    def test_builds_structured_evidence_claims_and_scoreboard(self):
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-05-08",
            "fundamentals_report": (
                "# Earnings\n"
                "2026-05-07 revenue growth accelerated +45% and gross margin expanded to 76%, "
                "showing strong demand after latest earnings."
            ),
            "market_report": (
                "# Technical Setup\n"
                "Price broke above $950 resistance on rising volume with bullish momentum and "
                "a swing target near $1,020."
            ),
            "macro_report": (
                "# Macro Risk\n"
                "2026-05-06 yields moved higher, creating valuation pressure for growth stocks."
            ),
        }

        context = build_report_context_index(state)
        claims = context["evidence_claims"]
        scoreboard = context["evidence_scoreboard"]

        self.assertEqual(context["schema_version"], "1.1")
        self.assertGreaterEqual(len(claims), 3)
        self.assertGreater(scoreboard["bullish_score"], scoreboard["bearish_score"])
        self.assertEqual(scoreboard["net_direction"], "bullish")
        self.assertTrue(scoreboard["key_bullish_claim_ids"])

        scored_claim = next(
            claim for claim in claims if claim["source_report"] == "fundamentals_report"
        )
        self.assertEqual(scored_claim["source_type"], "fundamental")
        self.assertEqual(scored_claim["timestamp"], "2026-05-07")
        self.assertTrue(scored_claim["numeric_support"])
        self.assertGreater(scored_claim["scores"]["numeric_support"], 0)
        self.assertGreater(scored_claim["confidence"], 0)

    def test_detects_material_contradictions_between_opposing_claims(self):
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-05-08",
            "fundamentals_report": (
                "# Demand\n"
                "2026-05-07 demand remains strong with revenue growth +30% and expanding backlog."
            ),
            "news_report": (
                "# Demand Risk\n"
                "2026-05-08 demand is weakening after customer cancellations, creating revenue risk -10%."
            ),
        }

        context = build_report_context_index(state)
        scoreboard = context["evidence_scoreboard"]

        self.assertGreater(scoreboard["contradiction_score"], 0)
        self.assertTrue(scoreboard["major_contradictions"])
        contradiction = scoreboard["major_contradictions"][0]
        self.assertIn("demand", contradiction["shared_terms"])

    def test_agent_context_includes_scored_matrix_and_structured_data(self):
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-05-08",
            "market_report": (
                "# Swing Setup\n"
                "Breakout above $950 support confirms bullish trend, entry trigger $955, "
                "stop $920, target $1,020."
            ),
        }

        bundle = get_agent_context_bundle(
            state,
            agent_role="managers/research_manager",
            objective="Adjudicate evidence quality for NVDA.",
        )

        self.assertIn("Heuristic Claim Reading Guide", bundle["decision_claim_matrix"])
        self.assertIn("priority=", bundle["decision_claim_matrix"])
        self.assertTrue(bundle["evidence_claims"])
        self.assertEqual(bundle["evidence_scoreboard_data"]["claim_count"], 1)

    # --- DMC-1 A01/A02: rendered text is heuristic-only; structured
    # metadata and calculations are unchanged. ---

    def _bull_bear_state(self):
        return {
            "company_of_interest": "NVDA",
            "trade_date": "2026-05-08",
            "fundamentals_report": (
                "# Earnings\n"
                "2026-05-07 revenue growth accelerated +45% and gross margin expanded "
                "to 76%, showing strong demand after latest earnings."
            ),
            "news_report": (
                "# Demand Risk\n"
                "2026-05-08 demand is weakening after customer cancellations, creating "
                "revenue risk -10% for the stock."
            ),
        }

    def test_a01_rendered_guide_keeps_claims_and_drops_aggregates(self):
        bundle = get_agent_context_bundle(
            self._bull_bear_state(),
            agent_role="managers/research_manager",
            objective="Adjudicate NVDA evidence quality.",
        )
        guide = bundle["evidence_scoreboard"]
        matrix = bundle["decision_claim_matrix"]

        # New heuristic-only framing with claim IDs and rendered scores.
        self.assertIn("Heuristic Claim Reading Guide:", guide)
        self.assertIn("Bullish-tagged excerpts (heuristic labels)", guide)
        self.assertIn("Bearish-tagged excerpts (heuristic labels)", guide)
        self.assertIn(
            "Potential claim overlaps to review (not verified contradictions)", guide
        )
        self.assertIn("overlap_score=", guide)
        self.assertIn("priority=", matrix)
        self.assertIn("date_hint=", matrix)
        self.assertIn("numeric_hint=", matrix)
        self.assertIn("overlap_hint=", matrix)
        self.assertIn("fundamentals_", matrix)  # claim IDs preserved
        self.assertIn("news_", matrix)
        self.assertIn("45%", matrix)  # numeric evidence preserved

        # No aggregate verdicts, confidence labels or manager guidance.
        self.assertNotIn("Net:", guide)
        self.assertNotIn("confidence)", guide)
        self.assertNotIn("Manager guidance", guide)
        self.assertNotIn("Key bullish claims", guide)
        self.assertNotIn("[Bullish]:", matrix)
        self.assertNotIn("[Bearish]:", matrix)
        self.assertNotIn("[Mixed]:", matrix)

        # Direction/overlap disclaimers present.
        self.assertIn(
            "Direction labels are keyword heuristics, not trade recommendations.",
            matrix,
        )
        self.assertIn(
            "Overlap hints may reflect compatible facts or different horizons, "
            "not factual contradictions.",
            matrix,
        )

        # Retrieved excerpts remain available downstream.
        self.assertTrue(bundle["all_reports_text"].startswith(
            "Retrieved analyst excerpts"
        ))
        self.assertIn("Role-Relevant Evidence Excerpts", bundle["analysis_context"])

    def test_a02_structured_scoreboard_metadata_is_unchanged(self):
        context = build_report_context_index(self._bull_bear_state())
        scoreboard = context["evidence_scoreboard"]

        # The structured calculation and keys survive the render change.
        self.assertIn(
            scoreboard["net_direction"], ("bullish", "bearish", "mixed")
        )
        self.assertIn(scoreboard["net_confidence"], ("low", "medium", "high"))
        self.assertIsInstance(scoreboard["bullish_score"], float)
        self.assertIsInstance(scoreboard["bearish_score"], float)
        self.assertEqual(scoreboard["claim_count"], len(context["evidence_claims"]))
        self.assertTrue(scoreboard["key_bullish_claim_ids"])
        self.assertTrue(scoreboard["key_bearish_claim_ids"])
        self.assertTrue(scoreboard["major_contradictions"])
        self.assertTrue(scoreboard["manager_guidance"])


if __name__ == "__main__":
    unittest.main()
