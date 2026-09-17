"""U12/U13 regression tests for webui.components.analysis.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4:
- U12: run_analysis reads ``current_state = app_state.get_state(ticker)``
  inside the try; when no symbol state exists it returns early, and the
  finally block then raises NameError (UnboundLocalError) on
  ``current_state`` — masking the original error path.
- U13: run_analysis swallows ordinary exceptions and still returns
  "Real-time analysis complete"; start_analysis ignores run_analysis's
  return value, so a failed analysis reports success to the operator.
"""

import unittest
from unittest.mock import patch


class U12MissingSymbolStateTests(unittest.TestCase):
    def test_u12_run_analysis_without_symbol_state_returns_failure_not_name_error(self):
        from webui.components import analysis as analysis_mod
        from webui.utils import state as webui_state

        app_state = webui_state.app_state
        app_state.symbol_states.pop("NOPE", None)
        saved_gen = app_state.run_generation
        try:
            result = analysis_mod.run_analysis(
                "NO-SUCH-SYMBOL", ["market"], {"rounds": 1, "level": "Shallow"},
                False, "m", "m", run_generation=app_state.run_generation,
            )
        finally:
            app_state.run_generation = saved_gen
        self.assertIsNotNone(result)
        self.assertIn("fail", result.get("status", ""))


class U13FailurePropagationTests(unittest.TestCase):
    def setUp(self):
        from webui.utils import state as webui_state

        self.app_state = webui_state.app_state
        if "AAPL" not in self.app_state.symbol_states:
            self.app_state.init_symbol_state("AAPL")
        self.app_state.get_state("AAPL")["analysis_running"] = False

    def test_u13_run_analysis_reports_failed_result_dict_on_exception(self):
        from webui.components import analysis as analysis_mod
        from tradingagents.llm_clients.retry import ProviderFailure

        failure = ProviderFailure(
            role="analysis", provider="openai", model="gpt-fake",
            attempts=1, category="permanent", detail="fixture exhausted",
        )
        with patch.object(
            analysis_mod, "TradingAgentsGraph", side_effect=failure
        ):
            result = analysis_mod.run_analysis(
                "AAPL", ["market"], {"rounds": 1, "level": "Shallow"},
                False, "m", "m", run_generation=self.app_state.run_generation,
            )
        self.assertIsInstance(result, dict)
        self.assertIn(result["status"], ("failed", "stopped"))
        self.assertIn("fixture exhausted", result.get("error", ""))

    def test_u13_start_analysis_surfaces_failure_from_run_analysis(self):
        from webui.components import analysis as analysis_mod

        guard = SimpleNamespace_guard = type("G", (), {})()
        guard.check_llm_budget = lambda: type("V", (), {"allowed": True})()

        def failing_run(*a, **k):
            return {"status": "failed", "error": "boom", "symbol": a[0]}

        with patch.object(analysis_mod, "create_chart", lambda *a, **k: {}), \
             patch.object(analysis_mod, "run_analysis", failing_run), \
             patch("tradingagents.safety.get_safety_guard", return_value=guard):
            message = analysis_mod.start_analysis(
                "AAPL", True, False, False, False, False, "Medium", False,
                "m", "m",
            )
        self.assertIn("failed", message.lower())
        self.assertIn("boom", message)


if __name__ == "__main__":
    unittest.main()
