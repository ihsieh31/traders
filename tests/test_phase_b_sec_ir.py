"""Phase B4 tests: SEC/IR primary source — official CIK mapping, honest
dates/freshness, bounded transport, and explicit unavailability markers."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tradingagents.dataflows.sec_ir import (
    DEFAULT_FRESHNESS_DAYS,
    SecIrClient,
    SecIrError,
    SourceRecord,
    render_sec_ir_report,
)

_NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)

_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
}

_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["10-K", "10-Q", "8-K", "S-1"],
            "accessionNumber": [
                "0000320193-26-000001",
                "0000320193-26-000002",
                "0000320193-26-000003",
                "0000320193-26-000004",
            ],
            "filingDate": ["2025-11-01", "2026-08-01", "2026-09-01", "2026-01-01"],
            "primaryDocument": ["a10k.htm", "a10q.htm", "a8k.htm", "s1.htm"],
            "reportDate": ["2025-09-27", "2026-06-27", None, None],
        }
    }
}


def _fetch_from_fixtures(responses):
    def fetch(url, headers, timeout):
        if url.endswith(".json") and "company_tickers" in url:
            return json.dumps(_TICKERS).encode(), {}
        if "submissions" in url:
            return json.dumps(_SUBMISSIONS).encode(), {}
        for matcher, body in responses:
            if matcher(url):
                return body
        raise SecIrError(f"unexpected URL in fixture: {url}")

    return fetch


def _client(tmp, responses=None, **kwargs):
    return SecIrClient(
        cache_dir=Path(tmp) / "cache",
        fetch=_fetch_from_fixtures(responses or []),
        now=lambda: _NOW,
        **kwargs,
    )


class CikMappingTests(unittest.TestCase):
    def test_official_mapping_resolves_cik(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            self.assertEqual(client.resolve_cik("AAPL"), 320193)

    def test_unknown_symbol_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            self.assertIsNone(client.resolve_cik("ZZZZ"))
            records = client.latest_filings("ZZZZ")
            self.assertTrue(all(r.error for r in records))
            report = render_sec_ir_report("ZZZZ", records, now=_NOW)
            self.assertIn("no entry in the official SEC ticker mapping", report)

    def test_crypto_symbols_have_no_cik(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            self.assertIsNone(client.resolve_cik("BTC/USD"))

    def test_mapping_failure_is_explicit(self):
        def failing(url, headers, timeout):
            raise SecIrError("HTTP 503")

        with tempfile.TemporaryDirectory() as tmp:
            client = SecIrClient(
                cache_dir=Path(tmp) / "cache", fetch=failing, now=lambda: _NOW
            )
            with self.assertRaises(SecIrError):
                client.resolve_cik("AAPL")


class FilingMetadataTests(unittest.TestCase):
    def test_filings_carry_source_url_published_retrieved(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = _client(tmp).latest_filings("AAPL")
            by_form = {r.form_type: r for r in records}
            ten_q = by_form["10-Q"]
            self.assertEqual(ten_q.source, "SEC EDGAR")
            self.assertEqual(
                ten_q.url,
                "https://www.sec.gov/Archives/edgar/data/320193/"
                "000032019326000002/a10q.htm",
            )
            self.assertEqual(ten_q.published_at, "2026-08-02T00:00:00+00:00")
            # retrieved_at is a real UTC timestamp (never a fake date).
            self.assertIn("T", ten_q.retrieved_at)
            self.assertIn("+00:00", ten_q.retrieved_at)
            # S-1 is not among the supported forms.
            self.assertEqual(
                sorted(r.form_type for r in records), ["10-K", "10-Q", "8-K"]
            )

    def test_published_date_governs_freshness_not_retrieval_time(self):
        # Freshness is evaluated against the real clock (is_fresh has no
        # injection point), so fixture dates are relative to "now" to stay
        # valid regardless of when the suite runs.
        now = datetime.now(timezone.utc)
        fresh = SourceRecord(
            kind="sec_filing", symbol="AAPL", source="SEC EDGAR",
            url="https://www.sec.gov/x.htm",
            published_at=(now - timedelta(days=300)).isoformat(),
            retrieved_at="",  # retrieval time must not matter
            form_type="10-K",
            freshness_days=DEFAULT_FRESHNESS_DAYS["10-K"],
        )
        self.assertTrue(fresh.is_fresh)
        old_10k = SourceRecord(
            kind="sec_filing", symbol="AAPL", source="SEC EDGAR",
            url="https://www.sec.gov/x.htm",
            published_at=(now - timedelta(days=600)).isoformat(),
            retrieved_at=now.isoformat(),  # retrieved seconds ago
            form_type="10-K",
            freshness_days=DEFAULT_FRESHNESS_DAYS["10-K"],
        )
        self.assertFalse(old_10k.is_fresh)

    def test_missing_future_and_unparseable_dates_are_never_fresh(self):
        base = dict(kind="sec_filing", symbol="AAPL", source="SEC EDGAR",
                    url="https://www.sec.gov/x.htm", form_type="10-K",
                    freshness_days=500)
        missing = SourceRecord(published_at=None, retrieved_at="", **base)
        unparseable = SourceRecord(published_at="not-a-date", retrieved_at="", **base)
        # Relative to the real clock: a fixed "future" date stops being
        # future as the calendar advances and the assertion silently flips.
        future = SourceRecord(
            published_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            retrieved_at="", **base
        )
        for record in (missing, unparseable, future):
            with self.subTest(record=record.published_at):
                self.assertFalse(record.is_fresh)
        report = render_sec_ir_report("AAPL", [missing], now=_NOW)
        self.assertIn("STALE", report)


class IrPageTests(unittest.TestCase):
    def test_missing_ir_url_is_reported_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp, ir_pages={})
            record = client.ir_page("AAPL")
            self.assertIn("no IR page configured", record.error)
            report = render_sec_ir_report("AAPL", [record], now=_NOW)
            self.assertIn("UNAVAILABLE", report)

    def test_configured_ir_page_is_fetched_with_last_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(
                tmp,
                responses=[
                    (
                        lambda url: "ir.example.com" in url,
                        (
                            b"<html><title>Apple IR</title>press releases</html>",
                            {"Last-Modified": "Fri, 28 Aug 2026 12:00:00 GMT"},
                        ),
                    )
                ],
                ir_pages={"AAPL": "https://ir.example.com/apple"},
                # The fixed fixture date ages past the default 14-day IR
                # window; widen it — this test pins Last-Modified parsing,
                # not the freshness window.
                freshness_days={"ir": 36500},
            )
            record = client.ir_page("AAPL")
            self.assertEqual(record.published_at, "2026-08-28T12:00:00+00:00")
            self.assertTrue(record.is_fresh)


class TransportSafetyTests(unittest.TestCase):
    def test_non_https_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            with self.assertRaises(SecIrError):
                client._fetch("http://www.sec.gov/x")

    def test_cross_host_redirect_rejected(self):
        called = []

        def fetch(url, headers, timeout):
            called.append(url)
            return b"ok", {}

        with tempfile.TemporaryDirectory() as tmp:
            client = SecIrClient(
                cache_dir=Path(tmp) / "cache", fetch=fetch, now=lambda: _NOW
            )
            # Simulate the redirect check at the URL level: a configured
            # SEC fetch to a non-official host is refused.
            with self.assertRaises(SecIrError):
                client._fetch("https://evil.example.com/files/x",
                              expect_host="www.sec.gov")
            self.assertEqual(called, [])

    def test_user_agent_header_is_sent(self):
        seen = {}

        def fetch(url, headers, timeout):
            seen.update(headers)
            return json.dumps(_TICKERS).encode(), {}

        with tempfile.TemporaryDirectory() as tmp:
            client = SecIrClient(
                cache_dir=Path(tmp) / "cache", fetch=fetch, now=lambda: _NOW,
                user_agent="TestAgent contact@example.com",
            )
            client.resolve_cik("AAPL")
        self.assertEqual(seen.get("User-Agent"), "TestAgent contact@example.com")


class ToolkitWiringTests(unittest.TestCase):
    def test_toolkit_exposes_sec_ir_source_for_fundamentals(self):
        from tradingagents.agents.utils.agent_utils import Toolkit

        toolkit = Toolkit(config={})
        self.assertTrue(hasattr(toolkit, "get_sec_ir_source"))
        name = getattr(toolkit.get_sec_ir_source, "name", None) or getattr(
            toolkit.get_sec_ir_source, "func", None
        ).__name__
        self.assertEqual(name, "get_sec_ir_source")

    def test_disabled_config_reports_honest_status(self):
        from tradingagents.dataflows.interface import get_sec_ir_primary_source

        with patch(
            "tradingagents.dataflows.interface.get_config",
            return_value={"sec_ir_enabled": False},
        ):
            report = get_sec_ir_primary_source("AAPL", "2026-09-05")
        self.assertIn("disabled by configuration", report)

    def test_fundamentals_toolnode_includes_the_tool(self):
        import inspect

        from tradingagents.graph.trading_graph import TradingAgentsGraph

        source = inspect.getsource(TradingAgentsGraph._create_tool_nodes)
        self.assertIn("get_sec_ir_source", source)


if __name__ == "__main__":
    unittest.main()
