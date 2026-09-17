"""D10 regression: ticker identity must be preserved.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D10:
- Stock share-class separators are stripped (BRK.B -> BRKB), changing the
  vendor identity of the symbol.
- Crypto quote currency is forced to USD (BTC/USDC reported as BTC/USD),
  misreporting the actual pair.

Supported behavior: USD-quoted pairs keep working; other quote currencies
and share-class dots are preserved, not guessed away.
"""

import unittest

from tradingagents.dataflows.ticker_utils import TickerUtils


class D10TickerIdentityTests(unittest.TestCase):
    def test_d10_share_class_separator_is_preserved(self):
        result = TickerUtils.standardize_ticker("BRK.B")
        self.assertEqual(result["alpaca_format"], "BRK.B")
        self.assertEqual(result["yahoo_format"], "BRK-B")

    def test_d10_crypto_quote_currency_is_preserved(self):
        result = TickerUtils.standardize_ticker("BTC/USDC")
        self.assertTrue(result["is_crypto"])
        self.assertEqual(result["alpaca_format"], "BTC/USDC")
        self.assertEqual(result["base_symbol"], "BTC")

    def test_d10_usd_pair_unchanged(self):
        result = TickerUtils.standardize_ticker("BTC/USD")
        self.assertEqual(result["alpaca_format"], "BTC/USD")
        self.assertEqual(result["yahoo_format"], "BTC-USD")

    def test_d10_plain_stock_unchanged(self):
        result = TickerUtils.standardize_ticker("AAPL")
        self.assertEqual(result["alpaca_format"], "AAPL")
        self.assertFalse(result["is_crypto"])


if __name__ == "__main__":
    unittest.main()
