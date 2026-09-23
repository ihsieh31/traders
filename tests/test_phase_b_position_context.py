"""Phase B3 tests: broker position context for Trader/Decision prompts,
flat-vs-unknown separation, and prompt-injection boundaries."""

import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tradingagents.execution.authority import (
    BrokerAuthorityError,
    BrokerSnapshot,
    capture_broker_snapshot,
)
from tradingagents.execution.context import (
    ActiveTradePlanContext,
    build_position_context,
    capture_position_context,
    load_active_trade_plan_context,
    render_account_context,
    render_position_context,
    render_trade_plan_context,
)
from tradingagents.execution.store import ExecutionStore

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
        last_equity=equity if equity > 0 else 100000.0,
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


class ActiveTradePlanTests(unittest.TestCase):
    def _write_lots(self, path, plans, *, account_id="paper-1"):
        store = ExecutionStore(path)
        store.ensure_account_binding(account_id)
        for index, (decision_id, thesis, quantity) in enumerate(plans):
            payload = {
                "symbol": "NVDA", "trade_date": "2026-09-22",
                "rationale_summary": thesis, "time_horizon": "5 days",
                "risk_controls": {
                    "invalidation": "daily close below 90",
                    "stop_loss_price": 90.0, "take_profit_price": 120.0,
                },
                "entry_policy": {
                    "status": "READY", "minimum_price": 98.0,
                    "maximum_price": 100.0,
                    "expires_at": "2026-09-23T15:00:00+00:00",
                    "exit_by": "2026-09-29T15:00:00+00:00",
                    "risk_fraction": 0.01,
                },
            }
            _, orders, _ = store.create_outbox(
                decision_id=decision_id, run_id=None, symbol="NVDA", action="BUY",
                target_position="LONG", payload_json=json.dumps(payload),
                orders=[{"client_order_id": f"trade-{index}", "symbol": "NVDA",
                         "side": "buy", "quantity": quantity, "notional": None}],
            )
            store.record_fill(
                execution_id=f"fill-{index}", order_id=orders[0]["order_id"],
                qty=quantity, price=99.0,
                filled_at="2026-09-22T14:00:00+00:00",
            )
        return store

    def _position(self, qty=10, *, account_id="paper-1"):
        return build_position_context(
            "NVDA", _snapshot(
                positions=[("NVDA", qty, qty * 99.0, {"current_price": 99.0})],
                account_id=account_id,
            ),
        )

    def test_filled_lot_recovers_original_plan_and_context_is_read_only(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "execution.sqlite3"
            store = self._write_lots(path, [("decision-original", "original earnings thesis", 10)])
            plan = load_active_trade_plan_context(self._position(), {"execution_db_path": str(path)})
            self.assertEqual(plan.availability, "available")
            self.assertEqual(plan.original_thesis, "original earnings thesis")
            self.assertEqual(plan.original_stop_loss_price, 90.0)
            self.assertEqual(plan.original_take_profit_price, 120.0)
            self.assertEqual(plan.exit_by, "2026-09-29T15:00:00+00:00")
            self.assertEqual(plan.active_lot_qty, 10.0)
            self.assertIn("Original trade plan: available", render_trade_plan_context(plan))
            readonly = ExecutionStore(path, read_only=True)
            with self.assertRaises(sqlite3.OperationalError):
                with readonly._connect() as conn:
                    conn.execute("INSERT INTO schema_version(version) VALUES (999)")
            self.assertEqual(store.account_binding_owner(), "paper-1")

    def test_flat_and_broker_ledger_mismatch_are_unavailable(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "execution.sqlite3"
            self._write_lots(path, [("decision-original", "original thesis", 10)])
            flat = load_active_trade_plan_context(
                build_position_context("NVDA", _snapshot()),
                {"execution_db_path": str(Path(directory) / "missing.db")},
            )
            self.assertEqual(flat.availability, "unavailable")
            self.assertIn("flat", flat.reason)
            mismatch = load_active_trade_plan_context(
                self._position(qty=11), {"execution_db_path": str(path)}
            )
            self.assertEqual(mismatch.availability, "unavailable")
            self.assertIn("quantity", mismatch.reason)

    def test_multiple_active_plans_are_ambiguous_and_bad_payload_is_unavailable(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "execution.sqlite3"
            store = self._write_lots(path, [
                ("decision-one", "first thesis", 10),
                ("decision-two", "second thesis", 10),
            ])
            plan = load_active_trade_plan_context(
                self._position(qty=20), {"execution_db_path": str(path)}
            )
            self.assertEqual(plan.availability, "ambiguous")
            intent = store.get_intent_by_decision("decision-one")
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE execution_intents SET payload_json='{' WHERE decision_id=?", ("decision-one",))
            malformed = load_active_trade_plan_context(
                self._position(qty=20), {"execution_db_path": str(path)}
            )
            self.assertEqual(malformed.availability, "unavailable")
            self.assertIn("evidence unavailable", malformed.reason)

    def test_profile_execution_paths_are_isolated(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            arm_a = Path(directory) / "traders" / "execution.sqlite3"
            arm_b = Path(directory) / "berkshire" / "execution.sqlite3"
            self._write_lots(arm_a, [("decision-a", "Traders thesis", 10)])
            self._write_lots(arm_b, [("decision-b", "Berkshire thesis", 10)])
            a = load_active_trade_plan_context(self._position(), {"execution_db_path": str(arm_a)})
            b = load_active_trade_plan_context(self._position(), {"execution_db_path": str(arm_b)})
            self.assertEqual(a.original_thesis, "Traders thesis")
            self.assertEqual(b.original_thesis, "Berkshire thesis")


class CaptureFailureTests(unittest.TestCase):
    """Timeout / wrong account / NaN / stale must stop, never read as flat."""

    def _broker(self):
        return SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity="100000", last_equity="100000", cash="80000", buying_power="160000"
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
            id="paper-OTHER", equity="100000", last_equity="100000", cash="80000", buying_power="160000"
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
            last_equity=100000.0,
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
            # Live-mode fixture: today's Eastern date. A past date would be
            # classified as a historical as-of and the (correct) PIT guard
            # would skip the broker capture these tests exercise.
            "trade_date": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
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

    def _context(self):
        return build_position_context("NVDA", _snapshot(
            positions=[("NVDA", 100.0, 28000.0, {"avg_entry_price": 240.0})]
        ))

    def _trade_plan(self):
        return ActiveTradePlanContext(
            symbol="NVDA", availability="available", decision_id="decision-original",
            trade_date="2026-09-22", original_thesis="original earnings thesis",
            invalidation="close below 90", original_stop_loss_price=90.0,
            original_take_profit_price=120.0, entry_minimum_price=98.0,
            entry_maximum_price=100.0, entry_expires_at="2026-09-23T15:00:00+00:00",
            exit_by="2026-09-29T15:00:00+00:00", risk_fraction=0.01,
            time_horizon="5 days", active_lot_qty=100.0,
        )

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
            "tradingagents.agents.trader.trader.load_active_trade_plan_context",
            return_value=self._trade_plan(),
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
        self.assertIn("original earnings thesis", trader_prompt)
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
        ), patch(
            "tradingagents.agents.managers.risk_manager.load_active_trade_plan_context",
            return_value=self._trade_plan(),
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
        self.assertIn("original earnings thesis", first_prompt)
        self.assertIn("original earnings thesis", second_prompt)

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

    def test_historical_and_shadow_nodes_never_read_current_trade_plan_ledger(self):
        from tradingagents.agents.managers.risk_manager import create_risk_manager
        from tradingagents.agents.trader.trader import create_trader
        from tradingagents.agents.schemas import ExecutableAction

        class FakeStructured:
            def invoke(self, prompt):
                return SimpleNamespace(
                    content="FINAL TRANSACTION PROPOSAL: **HOLD**",
                    action=ExecutableAction.HOLD, confidence="medium",
                    risk_rationale="maintain", required_controls="strict",
                )

        class FakeLLM:
            def with_structured_output(self, schema, **kwargs):
                return FakeStructured()

            def invoke(self, prompt):
                return SimpleNamespace(content="FINAL TRANSACTION PROPOSAL: **HOLD**")

        for mode in ("historical", "shadow"):
            with self.subTest(mode=mode):
                state = self._state()
                config = {"execution_db_path": "/path/that/must/not/be/read.sqlite3"}
                if mode == "historical":
                    state.update(
                        trade_date="2020-01-02", current_position="LONG",
                        position_stats="Historical as-of position: LONG",
                        account_status="Historical as-of account",
                    )
                else:
                    config["auto_trade"] = False
                with patch(
                    "tradingagents.agents.trader.trader.capture_position_context",
                    side_effect=AssertionError("historical/shadow must not query broker"),
                ), patch(
                    "tradingagents.agents.managers.risk_manager.capture_position_context",
                    side_effect=AssertionError("historical/shadow must not query broker"),
                ), patch(
                    "tradingagents.agents.trader.trader.load_active_trade_plan_context",
                    side_effect=AssertionError("historical/shadow must not query ledger"),
                ) as trader_plan, patch(
                    "tradingagents.agents.managers.risk_manager.load_active_trade_plan_context",
                    side_effect=AssertionError("historical/shadow must not query ledger"),
                ) as risk_plan, patch(
                    "tradingagents.agents.trader.trader.capture_agent_prompt"
                ), patch(
                    "tradingagents.agents.managers.risk_manager.capture_agent_prompt"
                ), patch(
                    "tradingagents.agents.trader.trader.TradingMemoryLog"
                ) as trader_log, patch(
                    "tradingagents.agents.managers.risk_manager.TradingMemoryLog"
                ) as risk_log:
                    trader_log.return_value.get_past_context.return_value = ""
                    risk_log.return_value.get_past_context.return_value = ""
                    memory = SimpleNamespace(get_memories=lambda *args, **kwargs: [])
                    create_trader(FakeLLM(), memory, config=config)(state)
                    create_risk_manager(FakeLLM(), memory, config=config)(state)
                trader_plan.assert_not_called()
                risk_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
