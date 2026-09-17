"""U15 regression: auto-screening Start must not require a manual ticker.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4 U15: the Start callback
rejects an empty ticker before it ever looks at auto-screening, so an
auto-screening run (symbols come from the validated scan plan, not from
the input box) cannot start with an empty ticker field. Manual mode must
still require at least one symbol. The gate lives in the module-level
helper ``_resolve_start_symbols`` so it can be tested without a Dash app.
"""

import unittest
from unittest.mock import patch


class U15SymbolGateTests(unittest.TestCase):
    def test_u15_auto_screening_allows_empty_tickers(self):
        from webui.callbacks.control_callbacks import _resolve_start_symbols

        symbols, error = _resolve_start_symbols(
            tickers="   ", auto_screening_enabled=True
        )
        self.assertIsNone(error)
        self.assertEqual(symbols, [])

    def test_u15_manual_mode_rejects_empty_tickers(self):
        from webui.callbacks.control_callbacks import _resolve_start_symbols

        symbols, error = _resolve_start_symbols(
            tickers="", auto_screening_enabled=False
        )
        self.assertIsNone(symbols)
        self.assertIn("Please enter at least one stock symbol", error)

    def test_u15_manual_mode_normalizes_symbols(self):
        from webui.callbacks.control_callbacks import _resolve_start_symbols

        symbols, error = _resolve_start_symbols(
            tickers=" aapl , msft,,", auto_screening_enabled=False
        )
        self.assertIsNone(error)
        self.assertEqual(symbols, ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
