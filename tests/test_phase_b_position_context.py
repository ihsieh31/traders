"""Phase B3 tests: broker position context for Trader/Decision prompts,
flat-vs-unknown separation, and prompt-injection boundaries."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.execution.authority import (
    BrokerAuthorityError,
    BrokerSnapshot,
    capture_broker_snapshot,
)
from tradingagents.execution.context import (
    build_position_context,
    capture_position_context,
    render_account_context,
    render_position_context,
)

_NOW = datetime.now(timezone.utc)


def _snapshot(positions=None, *, account_id="paper-1", equity=100000.0, cash=80000.0,
              observed_at=None):
    from tradingagents.execution.authority import BrokerPosition

    observed = observed_at or datetime.now(timezone.utc)
    position_objects = tuple(
        p if isinstance(p, BrokerPosition) else BrokerPosition(*p[:3], **(p[3] if len(p) > 3 else {}))
        for p in positions or []
    )
    return BrokerSnapshot(
        observed_at=observed,
        version="test-version",
        account_id=account_id,
        equity=equity,
        cash=cash,
        buying_power=160000.0,
        positions=position_objects,
        orders=(),
        fills=(),
        gross_exposure=sum(abs(p.market_value) for p in position_objects),
    )


class PositionContextMathTests(unittest.TestCase):
    def test_weight_gross_and_details_are_derived_from_snapshot(self):
        snapshot = _snapshot(
            positions=[
                ("NVDA", 100.0, 28000.0, {"avg_entry_price": 240.0, "unrealized_pl": 4000.0, "current_price": 280.0}),
                ("AAPL", 50.0, 9000.0, {}),
                ("MSFT", -10.0, -3000.0, {}),
            ]
        )
        context = build_position_context("NVDA", snapshot)
        self.assertEqual(context.side, "LONG")
        self.assertEqual(context.qty, 100.0)
        # weight_pct = abs(market_value)/equity*100 -> 28% of 100k.
        self.assertAlmostEqual(context.position_weight_pct, 28.0)
        # gross_pct = sum(abs(mv))/equity*100 -> 40% of 100k.
        self.assertAlmostEqual(context.gross_exposure_pct, 40.0)
        self.assertAlmostEqual(context.unrealized_pl_pct, 4000.0 / 24000.0 * 100.0)
        # Broker sign conventions are preserved for shorts.
        msft = build_position_context("MSFT", snapshot)
        self.assertEqual(msft.side, "SHORT")
        self.assertEqual(msft.market_value, -3000.0)
        self.assertAlmostEqual(msft.position_weight_pct, 3.0)

    def test_render_marks_missing_optional_fields_as_unavailable(self):
        snapshot = _snapshot(positions=[("NVDA", 100.0, 28000.0, {})])
        context = build_position_context("NVDA", snapshot)
        text = render_position_context(context)
        self.assertIn("unavailable", text)
        self.assertNotIn("Average entry price: $0", text)

    def test_flat_only_when_positions_response_is_complete(self):
        snapshot = _snapshot(positions=[])
        context = build_position_context("NVDA", snapshot)
        self.assertEqual(context.side, "FLAT")
        self.assertEqual(context.qty, 0.0)
        self.assertIn("FLAT (qty=0)", render_position_context(context))

    def test_zero_equity_fails_closed(self):
        snapshot = _snapshot(positions=[], equity=0.0)
        with self.assertRaises(BrokerAuthorityError):
            build_position_context("NVDA", snapshot)

    def test_account_context_renders_the_same_snapshot(self):
        snapshot = _snapshot()
        context = build_position_context("NVDA", snapshot)
        account_text = render_account_context(context)
        self.assertIn("$100,000.00", account_text)
        self.assertIn("Gross exposure", account_text)


class CaptureFailureTests(unittest.TestCase):
    """Timeout / wrong account / NaN / stale must stop, never read as flat."""

    def _broker(self):
        return SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", cash="80000", buying_power="160000"
            ),
            get_all_positions=lambda: [],
            get_orders=lambda request=None: [],
        )

    def test_capture_uses_the_strict_snapshot_path(self):
        broker = self._broker()
        context = capture_position_context("NVDA", broker)
        self.assertEqual(context.account_id, "paper-1")

    def test_wrong_account_is_rejected(self):
        broker = self._broker()
        broker.get_account = lambda: SimpleNamespace(
            id="paper-OTHER", equity="100000", cash="80000", buying_power="160000"
        )
        with self.assertRaises(BrokerAuthorityError):
            capture_position_context(
                "NVDA", broker, expected_account_id="paper-1"
            )

    def test_nan_equity_is_rejected(self):
        broker = self._broker()
        broker.get_account = lambda: SimpleNamespace(
            id="paper-1", equity="nan", cash="80000", buying_power="160000"
        )
        with self.assertRaises(BrokerAuthorityError):
            capture_position_context("NVDA", broker)

    def test_future_timestamp_is_rejected(self):
        stale = datetime.now(timezone.utc) + timedelta(hours=2)
        broker = self._broker()
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker, now=lambda: stale)

    def test_stale_snapshot_is_rejected(self):
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        stale_snapshot = BrokerSnapshot(
            observed_at=stale,
            version="v",
            account_id="paper-1",
            equity=100000.0,
            cash=80000.0,
            buying_power=160000.0,
            positions=(),
            orders=(),
            fills=(),
            gross_exposure=0.0,
        )
        with self.assertRaises(BrokerAuthorityError):
            build_position_context("NVDA", stale_snapshot)


class NodePromptInjectionTests(unittest.TestCase):
    """Position context must reach exactly the Trader and Risk Manager
    prompts — never the analysts/bull/bear/research-manager side."""

    def _state(self, symbol="NVDA"):
        return {
            "company_of_interest": symbol,
            "trade_date": "2026-09-05",
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

    def _context(self):
        return build_position_context("NVDA", _snapshot(
            positions=[("NVDA", 100.0, 28000.0, {"avg_entry_price": 240.0})]
        ))

    def test_trader_prompt_contains_broker_context(self):
        from tradingagents.agents.trader.trader import create_trader

        class FakeLLM:
            def with_structured_output(self, schema, **kw):
                return SimpleNamespace(
                    invoke=lambda p: SimpleNamespace(
                        content="FINAL TRANSACTION PROPOSAL: **BUY**"
                    )
                )

            def invoke(self, p):
                return SimpleNamespace(content="FINAL TRANSACTION PROPOSAL: **BUY**")

        captured_prompts = []

        def fake_capture(report_type, prompt_content, symbol=None):
            captured_prompts.append((report_type, prompt_content))

        with patch(
            "tradingagents.agents.trader.trader.capture_position_context",
            return_value=self._context(),
        ), patch(
            "tradingagents.agents.trader.trader.capture_agent_prompt",
            side_effect=fake_capture,
        ), patch(
            "tradingagents.agents.trader.trader.TradingMemoryLog"
        ) as log_cls:
            log_cls.return_value.get_past_context.return_value = ""
            node = create_trader(FakeLLM(), SimpleNamespace(get_memories=lambda *a, **k: []), config={})
            out = node(self._state())

        trader_prompt = next(
            content for report, content in captured_prompts if report == "trader_investment_plan"
        )
        self.assertIn("Broker position context", trader_prompt)
        self.assertIn("28.00%", trader_prompt)
        self.assertEqual(out["current_position"], "LONG")

    def test_risk_manager_recaptures_fresh_context_each_run(self):
        from tradingagents.agents.managers.risk_manager import create_risk_manager

        contexts = [
            build_position_context("NVDA", _snapshot(
                positions=[("NVDA", 100.0, 20000.0, {})],
                observed_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            )),
            build_position_context("NVDA", _snapshot(
                positions=[("NVDA", 100.0, 28000.0, {})],
                observed_at=datetime.now(timezone.utc),
            )),
        ]

        class FakeStructured:
            def invoke(self, p):
                return SimpleNamespace(
                    content="FINAL TRANSACTION PROPOSAL: **HOLD**", action="HOLD",
                    confidence="medium", risk_rationale="r", required_controls="c",
                )

        class FakeLLM:
            def with_structured_output(self, schema, **kw):
                return FakeStructured()

        captured_prompts = []
        with patch(
            "tradingagents.agents.managers.risk_manager.capture_position_context",
            side_effect=contexts,
        ) as capture_mock, patch(
            "tradingagents.agents.managers.risk_manager.capture_agent_prompt",
            side_effect=lambda t, c, symbol=None: captured_prompts.append((t, c)),
        ), patch(
            "tradingagents.agents.managers.risk_manager.TradingMemoryLog"
        ) as log_cls:
            log_cls.return_value.get_past_context.return_value = ""
            node = create_risk_manager(
                FakeLLM(), SimpleNamespace(get_memories=lambda *a, **k: []), config={}
            )
            node(self._state())
            node(self._state())

        self.assertEqual(capture_mock.call_count, 2)
        first_prompt = captured_prompts[0][1]
        second_prompt = captured_prompts[1][1]
        # Each node start re-renders the context it captured: the second run
        # shows the newer 28% weight, not the older 20%.
        self.assertIn("20.00%", first_prompt)
        self.assertIn("28.00%", second_prompt)
        self.assertNotIn("28.00%", first_prompt)

    def test_capture_failure_stops_the_node(self):
        from tradingagents.agents.managers.risk_manager import create_risk_manager

        with patch(
            "tradingagents.agents.managers.risk_manager.capture_position_context",
            side_effect=BrokerAuthorityError("broker GET failed after 3 attempts"),
        ):
            node = create_risk_manager(
                SimpleNamespace(), SimpleNamespace(), config={}
            )
            with self.assertRaises(BrokerAuthorityError):
                node(self._state())

    def test_analyst_side_never_receives_the_account_dump(self):
        # The trader/risk prompt templates are the only ones carrying the
        # broker context block; analyst templates have no such placeholder.
        from tradingagents.prompts import list_prompt_templates, load_prompt

        for template in list_prompt_templates():
            text = load_prompt(template)
            if template.startswith(("analysts/", "researchers/", "managers/research_manager")):
                self.assertNotIn("position_stats_desc", text, template)
                self.assertNotIn("Broker position context", text, template)


if __name__ == "__main__":
    unittest.main()
