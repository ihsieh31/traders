import os
import string
import tempfile
import unittest
from pathlib import Path

from tradingagents.prompts import (
    PromptTemplateError,
    list_prompt_templates,
    load_prompt,
    render_prompt,
)


class DefaultPromptValues(dict):
    def __missing__(self, key):
        value = f"<{key}>"
        self[key] = value
        return value


class PromptTemplateTests(unittest.TestCase):
    expected_groups = {
        "analysts",
        "graph",
        "managers",
        "researchers",
        "risk",
        "screening",
        "shared",
        "trader",
        "trading_modes",
    }

    def test_core_prompt_templates_are_available(self):
        templates = set(list_prompt_templates())
        expected = {
            "shared/analyst_tool_system.md",
            "analysts/market_system.md",
            "managers/research_manager.md",
            "trader/trader_system.md",
            "managers/risk_manager.md",
            "graph/signal_extraction_system.md",
            "graph/reflection_system.md",
        }
        self.assertTrue(expected.issubset(templates))

    def test_templates_are_grouped_for_searchability(self):
        templates = list_prompt_templates()
        root_files = {template for template in templates if "/" not in template}
        grouped = {template.split("/", 1)[0] for template in templates if "/" in template}

        self.assertEqual(root_files, {"README.md"})
        self.assertEqual(grouped, self.expected_groups)

    def test_every_model_template_loads_and_renders_with_sample_values(self):
        values = DefaultPromptValues(
            {
                "actions": "BUY, HOLD, or SELL",
                "agent_context": "Agent context.",
                "analysis_content": "Analysis content.",
                "analysis_context": "Analysis packet.",
                "asset_context": "The company we want to look at is NVDA",
                "base_context": "Base trading context.",
                "current_date": "2026-05-03",
                "current_position": "NEUTRAL",
                "decision_format": "BUY/HOLD/SELL",
                "final_decision": "FINAL TRANSACTION PROPOSAL: **HOLD**",
                "final_format": "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**",
                "mode_name": "SWING TRADING INVESTMENT MODE",
                "raw_return": "+1.0%",
                "ticker": "NVDA",
                "tool_names": "tool_a, tool_b",
            }
        )

        for template_name in list_prompt_templates():
            if template_name == "README.md":
                continue
            with self.subTest(template=template_name):
                template = load_prompt(template_name)
                rendered = template.format_map(values)
                self.assertIsInstance(rendered, str)
                self.assertGreater(len(rendered.strip()), 20)

    def test_template_placeholders_are_parseable(self):
        formatter = string.Formatter()
        for template_name in list_prompt_templates():
            with self.subTest(template=template_name):
                fields = [
                    field_name
                    for _, field_name, _, _ in formatter.parse(load_prompt(template_name))
                    if field_name
                ]
                self.assertTrue(all(" " not in field for field in fields))

    def test_render_prompt_substitutes_values(self):
        rendered = render_prompt(
            "shared/analyst_final_recommendation",
            analysis_label="market analysis",
            subject="NVDA",
            request="provide a final recommendation.",
            analysis_content="Technical evidence here.",
            closing_instruction="Conclude with the required final line.",
        )
        self.assertIn("NVDA", rendered)
        self.assertIn("Technical evidence here.", rendered)
        self.assertNotIn("{subject}", rendered)

    def test_missing_render_value_raises_clear_error(self):
        with self.assertRaisesRegex(PromptTemplateError, "Missing prompt value"):
            render_prompt("shared/analyst_final_recommendation", subject="NVDA")

    def test_rejects_path_traversal(self):
        with self.assertRaises(PromptTemplateError):
            load_prompt("../secrets")

    def test_prompt_dir_override(self):
        original = os.environ.get("TRADINGAGENTS_PROMPT_DIR")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                Path(temp_dir, "custom.md").write_text("Ticker: {ticker}", encoding="utf-8")
                Path(temp_dir, "analysts").mkdir()
                Path(temp_dir, "analysts", "market_system.md").write_text(
                    "Custom analyst prompt for {ticker}",
                    encoding="utf-8",
                )
                os.environ["TRADINGAGENTS_PROMPT_DIR"] = temp_dir
                self.assertEqual(render_prompt("custom", ticker="BTC/USD"), "Ticker: BTC/USD")
                self.assertEqual(
                    render_prompt("analysts/market_system", ticker="NVDA"),
                    "Custom analyst prompt for NVDA",
                )
                self.assertIn("investment decision", load_prompt("graph/signal_extraction_system"))
        finally:
            if original is None:
                os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)
            else:
                os.environ["TRADINGAGENTS_PROMPT_DIR"] = original

    # --- DMC-1 A03/C01: decision templates consume the heuristic guide and
    # stop asking agents to trust scores or expect trailing stops. ---

    _OLD_SCORE_TRUST_PHRASES = (
        "Evidence-scored decision claim matrix",
        "Full untruncated analyst reports",
        "Full Untruncated Analyst Reports",
        "high evidence, freshness, numeric support",
        "scoreboard is mixed",
        "evidence scoreboard is mixed",
        "high-score claims should be discounted",
        "Emphasize upside only when the supporting claims",
        "Press hardest on stale",
        "Balance the bull and bear evidence by comparing freshness",
        "Prefer cited claim IDs with high evidence",
        "Treat high contradiction or low freshness scores as reasons",
    )

    @classmethod
    def _guide_values(cls):
        from tradingagents.agents.utils.report_context import get_agent_context_bundle

        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-05-08",
            "market_report": (
                "# Setup\n"
                "Breakout above $950 resistance with volume expansion and bullish momentum."
            ),
            "news_report": (
                "# Demand Risk\n"
                "Demand is weakening after cancellations, creating revenue risk -10%."
            ),
        }
        bundle = get_agent_context_bundle(
            state,
            agent_role="managers/research_manager",
            objective="Adjudicate NVDA evidence quality.",
        )
        return bundle["decision_claim_matrix"], bundle["all_reports_text"]

    def test_a03_decision_templates_render_heuristic_guide(self):
        # Use the built-in templates: drop any external override for this test.
        original = os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)
        try:
            matrix, reports = self._guide_values()
            values = DefaultPromptValues(
                {
                    "claim_matrix": matrix,
                    "all_reports_text": reports,
                    "decision_format": "BUY/HOLD/SELL",
                    "final_format": "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**",
                    "output_language": "English",
                    "actions": "BUY, HOLD, or SELL",
                }
            )
            templates = (
                "researchers/bull_researcher.md",
                "researchers/bear_researcher.md",
                "risk/aggressive_debator.md",
                "risk/conservative_debator.md",
                "risk/neutral_debator.md",
                "managers/research_manager.md",
                "trader/trader_context.md",
            )
            for template in templates:
                with self.subTest(template=template):
                    rendered = load_prompt(template).format_map(values)

                    # Every decision agent receives the new heuristic guide.
                    self.assertIn("Heuristic Claim Reading Guide", rendered)
                    self.assertIn(
                        "Direction labels are keyword heuristics, not trade recommendations.",
                        rendered,
                    )
                    self.assertIn(
                        "Overlap hints may reflect compatible facts", rendered
                    )
                    self.assertIn(
                        "Use claim IDs to locate the supplied excerpts", rendered
                    )

                    # No legacy score-trust instruction survives.
                    for phrase in self._OLD_SCORE_TRUST_PHRASES:
                        self.assertNotIn(phrase, rendered)
        finally:
            if original is None:
                os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)
            else:
                os.environ["TRADINGAGENTS_PROMPT_DIR"] = original

    def test_c01_capability_boundary_rendered_in_both_modes(self):
        from tradingagents.agents.utils.agent_trading_modes import (
            get_agent_specific_context,
            get_trading_mode_context,
        )

        original = os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)
        try:
            matrix, reports = self._guide_values()
            modes = (
                ("investment", False, "BUY/HOLD/SELL"),
                ("trading", True, "LONG/NEUTRAL/SHORT"),
            )
            for mode_label, allow_shorts, decision_format in modes:
                with self.subTest(mode=mode_label):
                    trading_context = get_trading_mode_context(
                        {"allow_shorts": allow_shorts}, "LONG"
                    )
                    trader_prompt = load_prompt("trader/trader_context").format_map(
                        DefaultPromptValues(
                            {
                                "agent_context": get_agent_specific_context(
                                    "trader", trading_context
                                ),
                                "open_pos_desc": "We currently have an open LONG position in NVDA.",
                                "position_stats_desc": "qty=100",
                                "account_status_desc": "equity $100k",
                                "claim_matrix": matrix,
                                "all_reports_text": reports,
                                "debate_digest": "",
                                "decision_format": trading_context["decision_format"],
                                "final_format": trading_context["final_format"],
                                "output_language": "English",
                            }
                        )
                    )
                    risk_prompt = load_prompt("managers/risk_manager").format_map(
                        DefaultPromptValues(
                            {
                                "agent_context": get_agent_specific_context(
                                    "manager", trading_context
                                ),
                                "decision_format": trading_context["decision_format"],
                                "decision_time_utc": "2026-09-08 10:00 UTC",
                                "analysis_date": "2026-09-08",
                                "open_pos_desc": "We currently have an open LONG position in NVDA.",
                                "position_stats_desc": "qty=100",
                                "account_status_desc": "equity $100k",
                                "trader_plan": "trader plan",
                                "claim_matrix": matrix,
                                "all_reports_text": reports,
                                "risk_debate_digest": "",
                                "history": "",
                                "past_memory_str": "",
                                "decision_memory_str": "",
                                "actions": trading_context["actions"],
                                "final_format": trading_context["final_format"],
                                "output_language": "English",
                            }
                        )
                    )

                    for prompt in (trader_prompt, risk_prompt):
                        # No automatic trailing-stop promise remains.
                        self.assertNotIn("Trail stops", prompt)
                        self.assertNotIn("trailing stops", prompt)
                        self.assertIn(
                            "does not automatically trail or replace protective orders",
                            prompt,
                        )
                        # Maintain-position capability boundary.
                        self.assertIn(
                            "does not add, trim, trail or replace broker orders",
                            prompt,
                        )
                        self.assertIn(
                            "partial resizing is not an executable action", prompt
                        )
                        self.assertIn(
                            "mark them unavailable; do not infer them from unrealized P&L",
                            prompt,
                        )

                    # Existing hard constraints are still present (risk judge).
                    self.assertIn("READY requires ALL non-price conditions", risk_prompt)
                    self.assertIn("exit_by must be within 30 calendar days", risk_prompt)
                    self.assertIn(
                        "NEUTRAL in trading mode closes existing exposure", risk_prompt
                    )

                    if mode_label == "trading":
                        self.assertIn("Request a full close of the LONG position", prompt)
                        self.assertIn("Protected reversal boundary", prompt)
                    else:
                        self.assertIn(
                            "When a LONG position already exists in this symbol", prompt
                        )
                        self.assertIn("not a partial trim", prompt)
        finally:
            if original is None:
                os.environ.pop("TRADINGAGENTS_PROMPT_DIR", None)
            else:
                os.environ["TRADINGAGENTS_PROMPT_DIR"] = original


if __name__ == "__main__":
    unittest.main()
