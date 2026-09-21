import unittest

from tradingagents.analysis_backends import resolve_analysis_backend
from tradingagents.analysis_backends.berkshire.schemas import validate_canonical_reports
from tradingagents.default_config import DEFAULT_CONFIG


class AnalysisBackendResolutionTests(unittest.TestCase):
    def test_default_is_native_traders(self):
        self.assertEqual(resolve_analysis_backend(DEFAULT_CONFIG), "traders")

    def test_valid_backends_resolve(self):
        self.assertEqual(resolve_analysis_backend({"analysis_backend": "berkshire"}), "berkshire")

    def test_invalid_backend_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Invalid analysis_backend"):
            resolve_analysis_backend({"analysis_backend": "prompt-only"})

    def test_canonical_schema_requires_distinct_contexts(self):
        value = {key: key for key in (
            "market_report", "sentiment_report", "news_report",
            "fundamentals_report", "macro_report",
        )}
        self.assertEqual(validate_canonical_reports(value)["market_report"], "market_report")
        with self.assertRaisesRegex(ValueError, "distinct canonical"):
            validate_canonical_reports({key: "same" for key in value})


if __name__ == "__main__":
    unittest.main()
