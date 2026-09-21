import unittest

from tradingagents.prompts import render_prompt


class BerkshirePromptTests(unittest.TestCase):
    _names = ("fundamentals", "news", "macro", "social")

    def _values(self):
        return {
            "asset_focus": "the company NVDA",
            "current_date": "2026-09-21",
            "ticker": "NVDA",
            "global_news_guidance": "Global news is unavailable.",
            "source_guidance": "Active sources: SEC/IR, Finnhub.",
        }

    def test_all_profiles_render_and_keep_shared_contract(self):
        required = (
            "As-of and sources used",
            "Verified observations / supplied evidence",
            "Bullish implications",
            "Bearish implications",
            "Missing or conflicting evidence",
            "Horizon relevance",
        )
        for name in self._names:
            with self.subTest(name=name):
                rendered = render_prompt(f"berkshire/{name}_system", **self._values())
                lowered = rendered.lower()
                self.assertIn("as-of", lowered)
                self.assertIn("source", lowered)
                self.assertIn("missing or conflicting evidence", lowered)
                self.assertIn("horizon", lowered)
                for section in required:
                    self.assertIn(section, rendered)
                self.assertNotIn("FINAL TRANSACTION PROPOSAL", rendered)

    def test_prompts_forbid_unsupported_precision(self):
        fundamentals = render_prompt(
            "berkshire/fundamentals_system", **self._values()
        )
        social = render_prompt("berkshire/social_system", **self._values())
        self.assertIn("single-source", fundamentals)
        self.assertIn("source conflict", fundamentals)
        self.assertIn("never invent sentiment percentages", social.lower())


if __name__ == "__main__":
    unittest.main()
