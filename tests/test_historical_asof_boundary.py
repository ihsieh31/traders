"""PIT-1 boundary tests: day-level point-in-time cutoff for historical analysis.

Covers the analysis-date helpers, the Alpaca window cutoff, the regime
as-of wiring, live-only source rejection (OpenAI/Google/CryptoCompare/
DeFiLlama), the historical broker guard on Trader/Risk Manager, and the
removal of the hardcoded 2024 FOMC schedule. All offline: mocked
transports, injected loaders, no real brokers or HTTP.
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from langchain_core.runnables import RunnableLambda

from tradingagents.dataflows.interface_utils import (
    HISTORICAL_SOURCE_UNAVAILABLE,
    analysis_date_mode,
    parse_analysis_date,
)

NY_TZ = ZoneInfo("America/New_York")


class _ScriptedLLM(RunnableLambda):
    """Minimal Runnable chat-model stand-in for `prompt | llm` composition."""

    def __init__(self, content="FINAL TRANSACTION PROPOSAL: **HOLD**"):
        super().__init__(
            lambda prompt, *args, **kwargs: SimpleNamespace(
                content=content, additional_kwargs={}
            )
        )

    def bind_tools(self, tools, **kwargs):
        return self


class _ToolSpyLLM(_ScriptedLLM):
    """Scripted LLM recording the tool lists it binds."""

    def __init__(self, bound):
        super().__init__()
        self._bound = bound

    def bind_tools(self, tools, **kwargs):
        self._bound.append([t.name for t in tools])
        return self


def _today_ny() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d")


def _past_date(days_ago: int = 5) -> str:
    return (datetime.now(NY_TZ) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _now_ny(offset_days: int = 0) -> datetime:
    return datetime.now(NY_TZ) + timedelta(days=offset_days)


def _bars(dates, close=100.0):
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(dates, utc=True),
            "open": close,
            "high": close,
            "low": close,
            "close": [close + i for i in range(len(dates))],
            "volume": 1000,
        }
    )


class AnalysisDateModeTests(unittest.TestCase):
    def test_past_today_future(self):
        self.assertEqual(analysis_date_mode("2020-01-01", now=_now_ny()), "historical")
        today = _today_ny()
        self.assertEqual(analysis_date_mode(today, now=_now_ny()), "live")
        with self.assertRaisesRegex(ValueError, "future"):
            analysis_date_mode(
                (_now_ny() + timedelta(days=1)).strftime("%Y-%m-%d"), now=_now_ny()
            )

    def test_malformed_dates_raise(self):
        for bad in ("", None, "2026/09/07", "2026-09-07 10:00", "2026-9-7", "09-07-2026"):
            with self.assertRaises(ValueError):
                parse_analysis_date(bad)
            with self.assertRaises(ValueError):
                analysis_date_mode(bad, now=_now_ny())

    def test_naive_injected_now_raises(self):
        with self.assertRaises(ValueError):
            analysis_date_mode("2020-01-01", now=datetime(2026, 9, 7, 10, 0))

    def test_parse_is_strict_full_date(self):
        self.assertEqual(parse_analysis_date("2026-09-07").isoformat(), "2026-09-07")
        with self.assertRaises(ValueError):
            parse_analysis_date("2099-12-31")  # future

    def test_unavailable_constant_format(self):
        self.assertTrue(HISTORICAL_SOURCE_UNAVAILABLE.startswith("UNAVAILABLE_FOR_HISTORICAL_AS_OF:"))


class AlpacaWindowCutoffTests(unittest.TestCase):
    def _bars_with_future_row(self):
        # A provider misbehaving: returns a bar after the requested cutoff.
        return _bars(
            ["2026-08-25T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-06T16:00:00Z"]
        )

    def test_historical_passes_end_date_to_vendor(self):
        from tradingagents.dataflows import interface

        captured = {}

        def fake_get_stock_data(symbol, start_date, end_date=None, timeframe="1Day", **kw):
            captured["start"] = start_date
            captured["end"] = end_date
            return self._bars_with_future_row()

        with patch.object(
            interface.AlpacaUtils, "get_stock_data", staticmethod(fake_get_stock_data)
        ):
            report = interface.get_alpaca_data_window(
                "AAPL", curr_date=_past_date(5), look_back_days=14
            )
        self.assertEqual(captured["end"], _past_date(5))
        self.assertIn("to " + _past_date(5), report)
        self.assertNotIn("to present", report)

    def test_historical_excludes_rows_after_cutoff(self):
        from tradingagents.dataflows import interface

        with patch.object(
            interface.AlpacaUtils,
            "get_stock_data",
            staticmethod(lambda *a, **k: self._bars_with_future_row()),
        ):
            report = interface.get_alpaca_data_window(
                "AAPL", curr_date=_past_date(5), look_back_days=30
            )
        future_date = (datetime.now(NY_TZ) - timedelta(days=4)).strftime("%Y-%m-%d")
        self.assertNotIn(future_date, report)
        self.assertIn("2026-08-25", report)

    def test_historical_never_calls_latest_quote(self):
        from tradingagents.dataflows import interface

        quote_calls = []

        with patch.object(
            interface.AlpacaUtils,
            "get_stock_data",
            staticmethod(lambda *a, **k: self._bars_with_future_row()),
        ), patch.object(
            interface.AlpacaUtils,
            "get_latest_quote",
            staticmethod(lambda s: quote_calls.append(s) or {}),
        ):
            report = interface.get_alpaca_data_window(
                "AAPL", curr_date=_past_date(5), look_back_days=30
            )
        self.assertEqual(quote_calls, [])
        self.assertNotIn("Bid", report)
        self.assertNotIn("Ask", report)

    def test_live_calls_latest_quote_once(self):
        from tradingagents.dataflows import interface

        quote_calls = []
        bars = _bars(
            [
                (datetime.now(NY_TZ) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"),
                (datetime.now(NY_TZ) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
            ]
        )

        with patch.object(
            interface.AlpacaUtils,
            "get_stock_data",
            staticmethod(lambda *a, **k: bars),
        ), patch.object(
            interface.AlpacaUtils,
            "get_latest_quote",
            staticmethod(
                lambda s: quote_calls.append(s)
                or {
                    "bid_price": 1,
                    "bid_size": 1,
                    "ask_price": 2,
                    "ask_size": 1,
                    "timestamp": "now",
                }
            ),
        ):
            report = interface.get_alpaca_data_window(
                "AAPL", curr_date=_today_ny(), look_back_days=30
            )
        self.assertEqual(quote_calls, ["AAPL"])
        self.assertIn("Latest Quote", report)

    def test_unparseable_timestamps_never_pass_through(self):
        from tradingagents.dataflows import interface

        bad_bars = _bars(["2026-09-01T00:00:00Z"])
        bad_bars["timestamp"] = "not-a-timestamp"

        with patch.object(
            interface.AlpacaUtils,
            "get_stock_data",
            staticmethod(lambda *a, **k: bad_bars),
        ):
            report = interface.get_alpaca_data_window(
                "AAPL", curr_date=_past_date(5), look_back_days=30
            )
        self.assertTrue(report.startswith("UNAVAILABLE"))
        self.assertNotIn("close", report.lower().split("unavailable")[0])

    def test_toolkit_wrappers_route_through_the_same_function(self):
        from tradingagents.agents.utils.agent_utils import Toolkit
        from tradingagents.dataflows import interface

        with patch.object(
            interface,
            "get_alpaca_data_window",
            wraps=interface.get_alpaca_data_window,
        ) as spy, patch.object(
            interface.AlpacaUtils,
            "get_stock_data",
            staticmethod(lambda *a, **k: self._bars_with_future_row()),
        ):
            Toolkit.get_alpaca_data_report.invoke(
                {"symbol": "AAPL", "curr_date": _past_date(5), "look_back_days": 30}
            )
            Toolkit.get_stock_data_table.invoke(
                {"symbol": "AAPL", "curr_date": _past_date(5), "look_back_days": 30}
            )
        self.assertEqual(spy.call_count, 2)


class RegimeAsOfTests(unittest.TestCase):
    def _loader_factory(self, calls):
        def loader(symbol, start, end):
            calls.append((start, end))
            return _bars(
                pd.bdate_range("2024-06-03", periods=300).strftime("%Y-%m-%d")
            )

        return loader

    def test_historical_loader_receives_start_and_as_of_end(self):
        from tradingagents.regime import regime_report_block

        calls = []
        as_of = _past_date(5)
        block = regime_report_block(
            "SPY", price_loader=self._loader_factory(calls), as_of=as_of
        )
        self.assertIn("Market Regime", block)
        start, end = calls[-1]
        self.assertEqual(end, as_of)
        self.assertEqual(
            (pd.Timestamp(end) - pd.Timestamp(start)).days, 550
        )

    def test_invalid_or_future_as_of_never_falls_back_to_today(self):
        from tradingagents.regime import regime_report_block

        calls = []
        self.assertEqual(
            regime_report_block(
                "SPY", price_loader=self._loader_factory(calls), as_of="not-a-date"
            ),
            "",
        )
        self.assertEqual(
            regime_report_block(
                "SPY", price_loader=self._loader_factory(calls), as_of="2099-01-01"
            ),
            "",
        )
        self.assertEqual(calls, [])  # loader never called with a fallback

    def test_no_as_of_bounds_end_at_today_not_none(self):
        from tradingagents.regime import _load_assessment

        calls = []
        _load_assessment("SPY", price_loader=self._loader_factory(calls))
        start, end = calls[-1]
        self.assertEqual(end, _today_ny())
        self.assertIsNotNone(end)

    def test_market_analyst_passes_trade_date_to_regime(self):
        from tradingagents.agents.analysts.market_analyst import create_market_analyst
        from tradingagents.agents.utils.agent_utils import Toolkit

        captured_as_of = []

        def fake_regime_block(ticker, config=None, as_of=None):
            captured_as_of.append(as_of)
            return ""

        trade_date = _past_date(5)
        toolkit = SimpleNamespace(
            config={
                "online_tools": False,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_alpaca_credentials=lambda: False,
            get_stockstats_indicators_report=Toolkit.get_stockstats_indicators_report,
        )
        node = create_market_analyst(_ScriptedLLM(), toolkit)
        with patch(
            "tradingagents.agents.analysts.market_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), patch(
            "tradingagents.regime.regime_report_block",
            side_effect=fake_regime_block,
        ):
            node(
                {
                    "company_of_interest": "NVDA",
                    "trade_date": trade_date,
                    "messages": [],
                }
            )
        self.assertEqual(captured_as_of, [trade_date])


class LiveOnlySourceRejectTests(unittest.TestCase):
    def test_openai_web_search_tools_reject_historical_before_client(self):
        from tradingagents.dataflows import interface

        with patch.object(interface, "get_api_key") as key_lookup, patch.object(
            interface, "get_openai_client_with_timeout"
        ) as client_factory:
            for fn, args in (
                (interface.get_stock_news_openai, ("AAPL", _past_date(5))),
                (interface.get_global_news_openai, (_past_date(5),)),
                (interface.get_fundamentals_openai, ("AAPL", _past_date(5))),
            ):
                self.assertEqual(fn(*args), HISTORICAL_SOURCE_UNAVAILABLE)
            self.assertEqual(key_lookup.call_count, 0)
            self.assertEqual(client_factory.call_count, 0)

    def test_google_news_rejects_historical_before_http(self):
        from tradingagents.dataflows import interface

        with patch.object(interface, "getNewsData") as fetch:
            self.assertEqual(
                interface.get_google_news("AAPL", _past_date(5), 7),
                HISTORICAL_SOURCE_UNAVAILABLE,
            )
        self.assertEqual(fetch.call_count, 0)

    def test_crypto_sources_reject_historical_before_http(self):
        from tradingagents.dataflows import coindesk_utils, defillama_utils, interface

        with patch.object(coindesk_utils, "get_api_key") as cd_key, patch.object(
            coindesk_utils.requests, "get"
        ) as cd_http, patch.object(defillama_utils.requests, "get") as dl_http:
            self.assertEqual(
                interface.get_coindesk_news("BTC/USD", curr_date=_past_date(5)),
                HISTORICAL_SOURCE_UNAVAILABLE,
            )
            self.assertEqual(
                interface.get_defillama_fundamentals("BTC", curr_date=_past_date(5)),
                HISTORICAL_SOURCE_UNAVAILABLE,
            )
            self.assertEqual(
                coindesk_utils.get_news("BTC", curr_date=_past_date(5)),
                HISTORICAL_SOURCE_UNAVAILABLE,
            )
            self.assertEqual(
                defillama_utils.get_fundamentals("BTC", curr_date=_past_date(5)),
                HISTORICAL_SOURCE_UNAVAILABLE,
            )
            self.assertEqual(cd_key.call_count, 0)
            self.assertEqual(cd_http.call_count, 0)
            self.assertEqual(dl_http.call_count, 0)

    def test_live_crypto_path_uses_injected_transport(self):
        from tradingagents.dataflows import coindesk_utils

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"Type": 100, "Data": []},
        )
        with patch.object(
            coindesk_utils, "get_api_key", return_value="fixture-key"
        ), patch.object(
            coindesk_utils.requests, "get", return_value=response
        ) as http:
            coindesk_utils.get_news("BTC", curr_date=_today_ny())
        self.assertEqual(http.call_count, 1)

    def test_coindesk_http_call_carries_finite_timeout(self):
        # P2-02: every CryptoCompare HTTP call must be bounded so an
        # external hang cannot pin the analysis thread forever.
        from tradingagents.dataflows import coindesk_utils

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"Type": 100, "Data": []},
        )
        with patch.object(
            coindesk_utils, "get_api_key", return_value="fixture-key"
        ), patch.object(
            coindesk_utils.requests, "get", return_value=response
        ) as http:
            coindesk_utils.get_news("BTC", curr_date=_today_ny())
        (args, kwargs), = http.call_args_list
        timeout = kwargs.get("timeout", args[1] if len(args) > 1 else None)
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)
        self.assertTrue(timeout != float("inf"))

    def test_coindesk_timeout_returns_error_string_without_crash(self):
        import requests as _requests
        from tradingagents.dataflows import coindesk_utils

        with patch.object(
            coindesk_utils, "get_api_key", return_value="fixture-key"
        ), patch.object(
            coindesk_utils.requests, "get",
            side_effect=_requests.exceptions.Timeout("timed out"),
        ) as http:
            result = coindesk_utils.get_news("BTC", curr_date=_today_ny())
        self.assertEqual(http.call_count, 1)
        self.assertIsInstance(result, str)
        self.assertIn("CryptoCompare", result)
        # Same bounded timeout present on the raising call too.
        (_args, kwargs), = http.call_args_list
        self.assertGreater(float(kwargs.get("timeout")), 0)

    def test_news_analyst_binds_no_live_only_tools_historical(self):
        from tradingagents.agents.analysts.news_analyst import create_news_analyst
        from tradingagents.agents.utils.agent_utils import Toolkit

        bound = []

        toolkit = SimpleNamespace(
            config={
                "online_tools": True,
                "news_global_openai_enabled": True,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_openai_web_search=lambda: True,
            has_finnhub=lambda: True,
            has_coindesk=lambda: True,
            get_google_news=Toolkit.get_google_news,
            get_global_news_openai=Toolkit.get_global_news_openai,
            get_coindesk_news=Toolkit.get_coindesk_news,
            get_finnhub_news_recent=Toolkit.get_finnhub_news_recent,
        )
        with patch(
            "tradingagents.agents.analysts.news_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            create_news_analyst(_ToolSpyLLM(bound), toolkit)(
                {
                    "company_of_interest": "NVDA",
                    "trade_date": _past_date(5),
                    "messages": [],
                }
            )
        names = bound[0]
        for live_only in ("get_google_news", "get_global_news_openai", "get_coindesk_news"):
            self.assertNotIn(live_only, names)
        self.assertIn("get_finnhub_news_recent", names)  # dated Finnhub retained

    def test_social_analyst_excludes_openai_sentiment_historical(self):
        from tradingagents.agents.analysts.social_media_analyst import (
            create_social_media_analyst,
        )
        from tradingagents.agents.utils.agent_utils import Toolkit

        bound = []

        toolkit = SimpleNamespace(
            config={
                "online_tools": True,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_openai_web_search=lambda: True,
            get_reddit_stock_info=Toolkit.get_reddit_stock_info,
            get_stock_news_openai=Toolkit.get_stock_news_openai,
        )
        with patch(
            "tradingagents.agents.analysts.social_media_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            create_social_media_analyst(_ToolSpyLLM(bound), toolkit)(
                {
                    "company_of_interest": "NVDA",
                    "trade_date": _past_date(5),
                    "messages": [],
                }
            )
        self.assertNotIn("get_stock_news_openai", bound[0])

    def test_fundamentals_analyst_excludes_openai_and_defillama_historical(self):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )
        from tradingagents.agents.utils.agent_utils import Toolkit

        bound = []

        toolkit = SimpleNamespace(
            config={
                "online_tools": True,
                "sec_ir_enabled": True,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_openai_web_search=lambda: True,
            has_finnhub=lambda: False,
            has_simfin_data=lambda: False,
            get_fundamentals_openai=Toolkit.get_fundamentals_openai,
            get_defillama_fundamentals=Toolkit.get_defillama_fundamentals,
            get_sec_ir_source=Toolkit.get_sec_ir_source,
        )
        with patch(
            "tradingagents.agents.analysts.fundamentals_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            create_fundamentals_analyst(_ToolSpyLLM(bound), toolkit)(
                {
                    "company_of_interest": "NVDA",
                    "trade_date": _past_date(5),
                    "messages": [],
                }
            )
        names = bound[0]
        self.assertNotIn("get_fundamentals_openai", names)
        self.assertNotIn("get_defillama_fundamentals", names)
        self.assertIn("get_sec_ir_source", names)  # point-in-time capable

    def test_macro_analyst_excludes_openai_web_search_historical(self):
        from tradingagents.agents.analysts.macro_analyst import create_macro_analyst
        from tradingagents.agents.utils.agent_utils import Toolkit

        bound = []

        toolkit = SimpleNamespace(
            config={
                "online_tools": True,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_fred=lambda: True,
            has_openai_web_search=lambda: True,
            get_macro_analysis=Toolkit.get_macro_analysis,
            get_economic_indicators=Toolkit.get_economic_indicators,
            get_yield_curve_analysis=Toolkit.get_yield_curve_analysis,
            get_macro_news_openai=Toolkit.get_macro_news_openai,
        )
        with patch(
            "tradingagents.agents.analysts.macro_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            create_macro_analyst(_ToolSpyLLM(bound), toolkit)(
                {
                    "company_of_interest": "NVDA",
                    "trade_date": _past_date(5),
                    "messages": [],
                }
            )
        self.assertNotIn("get_macro_news_openai", bound[0])
        self.assertIn("get_macro_analysis", bound[0])  # FRED retained


class HistoricalBrokerGuardTests(unittest.TestCase):
    def _historical_state(self, symbol="NVDA", **extra):
        state = {
            "company_of_interest": symbol,
            "trade_date": _past_date(5),
            "trader_investment_plan": "Trader plan requiring confirmed entry and a protective stop",
            "investment_plan": "FINAL TRANSACTION PROPOSAL: **BUY** — a plan " + "x" * 200,
            "investment_debate_state": {
                "bull_history": "bull", "bear_history": "bear", "history": "h",
                "current_response": "", "judge_decision": "plan",
            },
            "risk_debate_state": {
                "risky_history": "r", "safe_history": "s", "neutral_history": "n",
                "history": "h", "judge_decision": "", "count": 3,
                "current_risky_response": "", "current_safe_response": "",
                "current_neutral_response": "",
            },
            "market_report": "market", "sentiment_report": "sentiment",
            "news_report": "news", "fundamentals_report": "fundamentals",
            "macro_report": "macro", "messages": [],
        }
        state.update(extra)
        return state

    def _trader_llm(self):
        class FakeLLM:
            def with_structured_output(self, schema, **kw):
                return SimpleNamespace(
                    invoke=lambda p: SimpleNamespace(
                        content="FINAL TRANSACTION PROPOSAL: **BUY**"
                    )
                )

            def invoke(self, p):
                return SimpleNamespace(content="FINAL TRANSACTION PROPOSAL: **BUY**")

        return FakeLLM()

    def test_historical_trader_makes_zero_broker_calls(self):
        from tradingagents.agents.trader.trader import create_trader

        with patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            side_effect=AssertionError("broker must not be called for historical dates"),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ), patch(
            "tradingagents.agents.trader.trader.TradingMemoryLog"
        ) as log_cls:
            log_cls.return_value.get_past_context.return_value = ""
            node = create_trader(
                self._trader_llm(),
                SimpleNamespace(get_memories=lambda *a, **k: []),
                config={},
            )
            out = node(self._historical_state())
        self.assertEqual(
            out["current_position"], HISTORICAL_SOURCE_UNAVAILABLE
        )

    def test_historical_trader_prompt_labels_unavailable_context(self):
        from tradingagents.agents.trader.trader import create_trader

        captured = []

        with patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            side_effect=AssertionError("broker must not be called for historical dates"),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=lambda t, c, symbol=None: captured.append((t, c)),
        ), patch(
            "tradingagents.agents.trader.trader.TradingMemoryLog"
        ) as log_cls:
            log_cls.return_value.get_past_context.return_value = ""
            node = create_trader(
                self._trader_llm(),
                SimpleNamespace(get_memories=lambda *a, **k: []),
                config={},
            )
            node(self._historical_state())

        trader_prompt = next(
            content for report, content in captured
            if report == "trader_investment_plan"
        )
        self.assertIn(HISTORICAL_SOURCE_UNAVAILABLE, trader_prompt)

    def test_historical_trader_uses_injected_portfolio_context(self):
        from tradingagents.agents.trader.trader import create_trader

        captured = []
        injected = {
            "current_position": "LONG",
            "position_stats": "Broker position for NVDA: LONG, qty=100 (as of 2026-09-01)",
            "account_status": "Equity: $100,000 | Cash: $80,000 (as of 2026-09-01)",
        }

        with patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            side_effect=AssertionError("broker must not be called for historical dates"),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=lambda t, c, symbol=None: captured.append((t, c)),
        ), patch(
            "tradingagents.agents.trader.trader.TradingMemoryLog"
        ) as log_cls:
            log_cls.return_value.get_past_context.return_value = ""
            node = create_trader(
                self._trader_llm(),
                SimpleNamespace(get_memories=lambda *a, **k: []),
                config={},
            )
            out = node(self._historical_state(**injected))

        trader_prompt = next(
            content for report, content in captured
            if report == "trader_investment_plan"
        )
        self.assertIn("qty=100", trader_prompt)
        self.assertIn("$80,000", trader_prompt)
        self.assertEqual(out["current_position"], "LONG")

    def _risk_llm(self):
        class FakeStructured:
            def invoke(self, p):
                # A historical run must fail closed BEFORE the LLM decides:
                # any structured output here would signal the guard was bypassed.
                raise AssertionError(
                    "historical guard must short-circuit before LLM decision"
                )

        class FakeLLM:
            def with_structured_output(self, schema, **kw):
                return FakeStructured()

            def invoke(self, p):
                raise AssertionError("historical guard must short-circuit before LLM")

        return FakeLLM()

    def test_missing_historical_portfolio_context_never_opens_ready(self):
        from tradingagents.agents.managers.risk_manager import create_risk_manager

        with patch(
            "tradingagents.agents.managers.risk_manager.capture_position_context",
            side_effect=AssertionError("broker must not be called for historical dates"),
        ), patch(
            "tradingagents.agents.managers.risk_manager.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            node = create_risk_manager(
                self._risk_llm(),
                SimpleNamespace(get_memories=lambda *a, **k: []),
                config={},
            )
            out = node(self._historical_state())

        decision = out["final_trade_decision"]
        self.assertIn(HISTORICAL_SOURCE_UNAVAILABLE, decision)
        self.assertIn("FINAL TRANSACTION PROPOSAL: **HOLD**", decision)
        self.assertIsNone(out["final_trade_intent"])
        self.assertEqual(out["risk_invalid_reason"], "historical_portfolio_context_unavailable")

    def test_injected_historical_portfolio_context_reaches_decision(self):
        from tradingagents.agents.managers.risk_manager import create_risk_manager

        # With trusted injected context the node proceeds to the structured
        # decision; a malformed LLM output then fails closed as INVALID.
        class FakeStructured:
            def invoke(self, p):
                raise ValueError("structured unavailable in fixture")

        class FakeLLM:
            def with_structured_output(self, schema, **kw):
                return FakeStructured()

        captured = []
        injected = {
            "current_position": "LONG",
            "position_stats": "Broker position for NVDA: LONG, qty=100 (as of 2026-09-01)",
            "account_status": "Equity: $100,000 | Cash: $80,000 (as of 2026-09-01)",
        }
        with patch(
            "tradingagents.agents.managers.risk_manager.capture_position_context",
            side_effect=AssertionError("broker must not be called for historical dates"),
        ), patch(
            "tradingagents.agents.managers.risk_manager.capture_agent_prompt",
            side_effect=lambda t, c, symbol=None: captured.append((t, c)),
        ):
            node = create_risk_manager(
                FakeLLM(), SimpleNamespace(get_memories=lambda *a, **k: []), config={}
            )
            out = node(self._historical_state(**injected))

        prompt = captured[0][1]
        self.assertIn("qty=100", prompt)
        self.assertIn("$80,000", prompt)
        self.assertIn("2026-09-01", prompt)
        # LLM failed in fixture -> strict INVALID path, but not the
        # historical-context-unavailable reason.
        self.assertEqual(out["risk_invalid_reason"], "structured_invoke_failed: structured unavailable in fixture")


class FomcScheduleRemovalTests(unittest.TestCase):
    def test_no_2024_fomc_schedule_in_fed_calendar_block(self):
        from tradingagents.dataflows.macro_utils import get_fed_calendar_and_minutes

        text = get_fed_calendar_and_minutes(_past_date(5))
        self.assertNotIn("2024 FOMC", text)
        self.assertNotIn("FOMC Meeting Schedule", text)
        self.assertIn(
            "FOMC event calendar unavailable: no authoritative point-in-time "
            "calendar source is configured.",
            text,
        )


if __name__ == "__main__":
    unittest.main()
