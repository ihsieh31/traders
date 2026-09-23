"""Nasdaq market-cap enrichment and once-per-day cache tests."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.screening import universe as universe_module
from tradingagents.screening.universe import fetch_nasdaq_market_caps, fetch_us_equity_universe


_TODAY = date(2026, 9, 23)


def _nasdaq_payload(rows):
    return {
        "data": {
            "totalrecords": len(rows),
            "table": {"rows": rows},
        }
    }


class NasdaqMarketCapTests(unittest.TestCase):
    def test_downloads_and_caches_once_per_day(self):
        rows = [
            {"symbol": "AAA", "marketCap": "300,000,000"},
            {"symbol": "BBB", "marketCap": "1,250,000,000"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with patch.object(
                universe_module,
                "_download_nasdaq_stock_data",
                return_value=_nasdaq_payload(rows),
            ) as download:
                first = fetch_nasdaq_market_caps(cache_dir=cache_dir, today=_TODAY)
                second = fetch_nasdaq_market_caps(cache_dir=cache_dir, today=_TODAY)

            expected = {"AAA": 300_000_000.0, "BBB": 1_250_000_000.0}
            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            download.assert_called_once_with()
            cache_path = cache_dir / f"nasdaq_{_TODAY.isoformat()}.json"
            self.assertTrue(cache_path.is_file())
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cached["date"], _TODAY.isoformat())
            self.assertEqual(cached["source"], "nasdaq_screener")
            self.assertEqual(cached["market_caps"], expected)

    def test_next_day_gets_a_new_daily_cache(self):
        payloads = [
            _nasdaq_payload([{"symbol": "AAA", "marketCap": "300000000"}]),
            _nasdaq_payload([{"symbol": "AAA", "marketCap": "400000000"}]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                universe_module, "_download_nasdaq_stock_data", side_effect=payloads
            ) as download:
                today = fetch_nasdaq_market_caps(cache_dir=Path(tmp), today=_TODAY)
                tomorrow = fetch_nasdaq_market_caps(
                    cache_dir=Path(tmp), today=_TODAY + timedelta(days=1)
                )
            self.assertEqual(today["AAA"], 300_000_000.0)
            self.assertEqual(tomorrow["AAA"], 400_000_000.0)
            self.assertEqual(download.call_count, 2)

    def test_failed_download_is_not_retried_and_returns_no_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                universe_module,
                "_download_nasdaq_stock_data",
                side_effect=OSError("offline"),
            ) as download:
                first = fetch_nasdaq_market_caps(cache_dir=Path(tmp), today=_TODAY)
                second = fetch_nasdaq_market_caps(cache_dir=Path(tmp), today=_TODAY)
            self.assertEqual(first, {})
            self.assertEqual(second, {})
            download.assert_called_once_with()
            self.assertTrue((Path(tmp) / f"nasdaq_{_TODAY.isoformat()}.attempted").is_file())

    def test_payload_rejects_incomplete_rows_and_invalid_caps(self):
        caps, count = universe_module._market_caps_from_nasdaq_payload(
            _nasdaq_payload(
                [
                    {"symbol": "OK", "marketCap": "$300,000,000"},
                    {"symbol": "MISSING", "marketCap": None},
                    {"symbol": "NAN", "marketCap": "NaN"},
                    {"symbol": "INFINITY", "marketCap": "Inf"},
                    {"symbol": "NEGATIVE", "marketCap": "-1"},
                    {"symbol": "ZERO", "marketCap": "0"},
                ]
            )
        )
        self.assertEqual(count, 6)
        self.assertEqual(caps, {"OK": 300_000_000.0})
        with self.assertRaises(ValueError):
            universe_module._market_caps_from_nasdaq_payload(
                {"data": {"totalrecords": 2, "table": {"rows": [{"symbol": "OK"}]}}}
            )

    def test_alpaca_universe_is_enriched_by_symbol_and_missing_stays_missing(self):
        def asset(symbol):
            return SimpleNamespace(
                symbol=symbol,
                name=f"Company {symbol}",
                status="active",
                asset_class=SimpleNamespace(value="us_equity"),
                tradable=True,
                exchange="NASDAQ",
            )

        broker = SimpleNamespace(get_all_assets=lambda _request: [asset("AAA"), asset("BBB")])
        with patch(
            "tradingagents.screening.universe.fetch_nasdaq_market_caps",
            return_value={"AAA": 500_000_000.0},
        ):
            result = fetch_us_equity_universe(broker)

        self.assertEqual(result[0]["symbol"], "AAA")
        self.assertEqual(result[0]["market_cap"], 500_000_000.0)
        self.assertEqual(result[1]["symbol"], "BBB")
        self.assertIsNone(result[1]["market_cap"])


if __name__ == "__main__":
    unittest.main()
