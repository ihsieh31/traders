import unittest

from tradingagents.analysis_profiles import (
    PROFILE_PROMPTS,
    analyst_prompt,
    resolve_analysis_profile,
)
from tradingagents.default_config import DEFAULT_CONFIG


class AnalysisProfileTests(unittest.TestCase):
    def test_default_profile_is_traders(self):
        self.assertEqual(resolve_analysis_profile(DEFAULT_CONFIG), "traders")
        self.assertEqual(analyst_prompt({}, "news"), "analysts/news_system")

    def test_berkshire_profile_resolves_only_non_market_prompts(self):
        config = {"analysis_profile": "berkshire"}
        self.assertEqual(
            analyst_prompt(config, "fundamentals"),
            "berkshire/fundamentals_system",
        )
        self.assertEqual(
            analyst_prompt(config, "social"),
            "berkshire/social_system",
        )
        self.assertEqual(set(PROFILE_PROMPTS["berkshire"]), {"fundamentals", "news", "macro", "social"})

    def test_invalid_profile_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Invalid analysis_profile"):
            resolve_analysis_profile({"analysis_profile": "unknown"})

    def test_market_uses_shared_prompt(self):
        self.assertEqual(
            analyst_prompt({"analysis_profile": "berkshire"}, "market"),
            "analysts/market_system",
        )


if __name__ == "__main__":
    unittest.main()
