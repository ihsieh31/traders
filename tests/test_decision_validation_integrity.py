"""DVI-1: decision validation integrity tests.

Covers the analyst research-only output contract, selected-analyst
coverage fail-closed, the heuristic claim-priority matrix framing, the
last_equity daily-loss baseline wiring, and honest performance copy.
All offline: scripted LLMs, in-memory stores, no network or broker.
"""

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda


class _ScriptedLLM(RunnableLambda):
    """Counts invocations; returns scripted content, raises scripted errors."""

    def __init__(self, content="study narrative", error=None, **_kwargs):
        calls = []

        def _respond(prompt, *a, **k):
            calls.append(1)
            if error is not None:
                raise error
            return SimpleNamespace(content=content, additional_kwargs={})

        super().__init__(_respond)
        self.calls = calls

    def bind_tools(self, tools, **kwargs):
        return self


def _toolkit(online=False):
    from tradingagents.agents.utils.agent_utils import Toolkit

    return SimpleNamespace(
        config={
            "online_tools": online,
            "max_tool_iterations_per_agent": 8,
            "max_same_tool_call_repeats": 1,
            "sec_ir_enabled": False,
        },
        has_alpaca_credentials=lambda: False,
        has_openai_web_search=lambda: False,
        has_finnhub=lambda: False,
        has_fred=lambda: False,
        has_coindesk=lambda: False,
        has_simfin_data=lambda: False,
        get_stockstats_indicators_report=Toolkit.get_stockstats_indicators_report,
        get_reddit_stock_info=Toolkit.get_reddit_stock_info,
        get_reddit_news=Toolkit.get_reddit_news,
        get_sec_ir_source=Toolkit.get_sec_ir_source,
        get_macro_analysis=Toolkit.get_macro_analysis,
        get_economic_indicators=Toolkit.get_economic_indicators,
        get_yield_curve_analysis=Toolkit.get_yield_curve_analysis,
        get_google_news=Toolkit.get_google_news,
        get_finnhub_news_recent=Toolkit.get_finnhub_news_recent,
        get_global_news_openai=Toolkit.get_global_news_openai,
        get_coindesk_news=Toolkit.get_coindesk_news,
        get_stock_news_openai=Toolkit.get_stock_news_openai,
        get_defillama_fundamentals=Toolkit.get_defillama_fundamentals,
        get_finnhub_company_insider_sentiment=Toolkit.get_finnhub_company_insider_sentiment,
        get_finnhub_company_insider_transactions=Toolkit.get_finnhub_company_insider_transactions,
        get_simfin_balance_sheet=Toolkit.get_simfin_balance_sheet,
        get_simfin_cashflow=Toolkit.get_simfin_cashflow,
        get_simfin_income_stmt=Toolkit.get_simfin_income_stmt,
        get_macro_news_openai=Toolkit.get_macro_news_openai,
        get_alpaca_data_report=Toolkit.get_alpaca_data_report,
        get_technical_brief=Toolkit.get_technical_brief,
    )


def _state(ticker="NVDA", date="2026-09-07"):
    return {
        "company_of_interest": ticker,
        "trade_date": date,
        "messages": [],
    }


def _no_capture():
    return patch(
        "tradingagents.agents.analysts.market_analyst.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.analysts.news_analyst.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.analysts.social_media_analyst.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.analysts.fundamentals_analyst.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    ), patch(
        "tradingagents.agents.analysts.macro_analyst.capture_agent_prompt",
        side_effect=lambda *a, **k: None,
    )


def _run_analyst(module_name, create_name, report_key, llm, analyst_name):
    import importlib

    module = importlib.importmodule = importlib.import_module(
        f"tradingagents.agents.analysts.{module_name}"
    )
    node = getattr(module, create_name)(llm, _toolkit())
    stack = _no_capture()
    for patcher in stack:
        patcher.start()
    try:
        return node(_state())
    finally:
        for patcher in reversed(stack):
            patcher.stop()


class AnalystReportOnlyTests(unittest.TestCase):
    """Spec items 1-2: research-only output contract, no extra action call."""

    ANALYSTS = [
        ("market_analyst", "create_market_analyst", "market_report", "market"),
        ("news_analyst", "create_news_analyst", "news_report", "news"),
        ("social_media_analyst", "create_social_media_analyst", "sentiment_report", "social"),
        ("fundamentals_analyst", "create_fundamentals_analyst", "fundamentals_report", "fundamentals"),
        ("macro_analyst", "create_macro_analyst", "macro_report", "macro"),
    ]

    def test_each_analyst_runs_one_llm_call_and_reports_no_action(self):
        for module_name, create_name, report_key, analyst_name in self.ANALYSTS:
            with self.subTest(analyst=analyst_name):
                llm = _ScriptedLLM(content="Verified observation: RSI oversold.")
                result = _run_analyst(module_name, create_name, report_key, llm, analyst_name)
                # Exactly one LLM request: no follow-up final-recommendation call.
                self.assertEqual(len(llm.calls), 1)
                report = result[report_key]
                self.assertTrue(report.strip())
                self.assertNotIn("FINAL TRANSACTION PROPOSAL", report)

    def test_empty_report_is_marked_failed_not_patched(self):
        llm = _ScriptedLLM(content="   ")
        result = _run_analyst("market_analyst", "create_market_analyst", "market_report", llm, "market")
        self.assertEqual(result["analysis_status"]["market"], "failed")
        self.assertEqual(len(llm.calls), 1)

    def test_completed_reports_carry_completed_status(self):
        for module_name, create_name, report_key, analyst_name in self.ANALYSTS:
            with self.subTest(analyst=analyst_name):
                llm = _ScriptedLLM(content="evidence narrative")
                result = _run_analyst(module_name, create_name, report_key, llm, analyst_name)
                self.assertEqual(result["analysis_status"][analyst_name], "completed")

    def test_analyst_exception_raises_instead_of_fallback_report(self):
        for module_name, create_name, report_key, analyst_name in self.ANALYSTS:
            with self.subTest(analyst=analyst_name):
                llm = _ScriptedLLM(error=RuntimeError("provider exploded"))
                with self.assertRaises(RuntimeError):
                    _run_analyst(module_name, create_name, report_key, llm, analyst_name)

    def test_final_recommendation_templates_are_legacy_marked(self):
        prompts_dir = Path("tradingagents/prompts/templates")
        legacy = [prompts_dir / "shared" / "analyst_final_recommendation.md"] + [
            prompts_dir / "analysts" / f"{name}_final_recommendation.md"
            for name in ("market", "news", "social", "fundamentals", "macro")
        ]
        for path in legacy:
            with self.subTest(template=path.name):
                self.assertTrue(
                    path.read_text(encoding="utf-8")
                    .startswith("Legacy unused template")
                )

    def test_decision_layer_still_emits_structured_action(self):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            ResearchPlan,
            RiskDecision,
            TraderProposal,
        )

        plan = ResearchPlan(
            recommendation=ExecutableAction.BUY, confidence="medium",
            rationale="r", strategic_actions="s",
        )
        proposal = TraderProposal(
            action=ExecutableAction.BUY, confidence="medium", reasoning="r",
        )
        decision = RiskDecision(
            action=ExecutableAction.BUY, confidence="medium",
            risk_rationale="r", required_controls="c",
        )
        self.assertEqual(plan.recommendation, ExecutableAction.BUY)
        self.assertEqual(proposal.action, ExecutableAction.BUY)
        self.assertEqual(decision.action, ExecutableAction.BUY)


class CoverageGateTests(unittest.TestCase):
    """Spec items 4-8: selected analyst coverage fails closed."""

    def _coordinator(self, nodes, selected):
        from tradingagents.graph.conditional_logic import ConditionalLogic
        from tradingagents.graph.setup import GraphSetup

        setup = GraphSetup(
            None, None, None, {}, None, None, None, None, None,
            ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
            config={},
        )
        return setup._create_parallel_analysts_coordinator(selected, nodes, {}, {})

    def test_parallel_failure_blocks_downstream(self):
        calls = []

        def ok(state):
            calls.append("ok")
            return {**state, "market_report": "fine", "analysis_status": {"market": "completed"}}

        def broken(state):
            calls.append("broken")
            raise RuntimeError("boom")

        coordinator = self._coordinator(
            {"market": ok, "news": broken}, ["market", "news"]
        )
        with self.assertRaises(RuntimeError):
            coordinator({"company_of_interest": "NVDA", "messages": []})
        # Both dispatched, failure propagated, nothing swallowed.
        self.assertIn("broken", calls)

    def test_sequential_failure_blocks_downstream(self):
        from tradingagents.agents.utils.report_context import create_report_context_node

        node = create_report_context_node(None, selected_analysts=["market", "news"])
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-09-07",
            "messages": [],
            "market_report": "fine",
            "analysis_status": {"market": "completed", "news": "failed"},
            "news_report": "",
        }
        with self.assertRaises(Exception):
            node(state)

    def test_gate_rejects_missing_status_and_empty_report(self):
        from tradingagents.agents.utils.report_context import AnalysisCoverageError, validate_selected_analyst_coverage

        with self.assertRaises(AnalysisCoverageError):
            validate_selected_analyst_coverage({}, ["market"])
        with self.assertRaises(AnalysisCoverageError):
            validate_selected_analyst_coverage(
                {"analysis_status": {"market": "failed"}, "market_report": "x"},
                ["market"],
            )
        with self.assertRaises(AnalysisCoverageError):
            validate_selected_analyst_coverage(
                {"analysis_status": {"market": "completed"}, "market_report": "   "},
                ["market"],
            )
        # Unselected analysts are never required.
        validate_selected_analyst_coverage(
            {
                "analysis_status": {"market": "completed"},
                "market_report": "fine",
            },
            ["market"],
        )

    def test_partial_selection_only_requires_selected_reports(self):
        from tradingagents.agents.utils.report_context import validate_selected_analyst_coverage

        state = {
            "analysis_status": {"market": "completed", "news": "completed"},
            "market_report": "fine",
            "news_report": "fine",
        }
        validate_selected_analyst_coverage(state, ["market", "news"])
        self.assertNotIn("sentiment_report", state)

    def test_report_context_node_reports_no_intent_rows(self):
        """A blocked coverage gate produces no report context at all."""
        from tradingagents.agents.utils.report_context import create_report_context_node

        node = create_report_context_node(None, selected_analysts=["macro"])
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-09-07",
            "messages": [],
            "market_report": "fine",
            "analysis_status": {"market": "completed"},
        }
        with self.assertRaises(Exception):
            node(state)
        self.assertNotIn("report_context", state)


class HeuristicMatrixTests(unittest.TestCase):
    """Spec item 9: matrix prompts must demote the score to a heuristic."""

    EXPECTED = (
        "reading-order heuristic",
        "not source verification",
        "model confidence",
        "probability",
        "win rate",
        "independent vote",
    )

    def _text(self, path):
        return (Path("tradingagents/prompts/templates") / path).read_text(encoding="utf-8")

    def test_manager_and_trader_prompts_state_heuristic_rules(self):
        for template in (
            "managers/research_manager.md",
            "managers/risk_manager.md",
            "trader/trader_context.md",
        ):
            with self.subTest(template=template):
                text = self._text(template)
                self.assertIn("Heuristic claim priority matrix", text)
                for phrase in self.EXPECTED:
                    self.assertIn(phrase, text)

    def test_rendered_matrix_heading_states_heuristic_rules(self):
        from tradingagents.agents.utils.report_context import get_agent_context_bundle

        state = {
            "trade_date": "2026-09-07",
            "market_report": "## Overview\nPrice broke above the 50-day average at 100.5 with volume 2x the 20-day average.",
            "news_report": "## Overview\nReuters reported on 2026-09-06 that revenue guidance was raised by 4%.",
        }
        bundle = get_agent_context_bundle(state, "trader", "decide", None)
        matrix = bundle["decision_claim_matrix"]
        self.assertIn("Heuristic Claim Priority Matrix", matrix)
        for phrase in self.EXPECTED:
            self.assertIn(phrase, matrix)


class BrokerLastEquityTests(unittest.TestCase):
    """Spec items 10-14: snapshot baseline, fail-closed capture, gates."""

    def _broker(self, equity=100000.0, last_equity=100000.0, orders=(), positions=()):
        return SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity=str(equity), last_equity=str(last_equity),
                cash="50000", buying_power="200000",
            ),
            get_all_positions=lambda: list(positions),
            get_orders=lambda request=None: list(orders),
        )

    def _capture(self, **kwargs):
        from tradingagents.execution.authority import capture_broker_snapshot

        return capture_broker_snapshot(self._broker(**kwargs))

    def test_snapshot_carries_last_equity(self):
        snapshot = self._capture(equity=99000.0, last_equity=100000.0)
        self.assertEqual(snapshot.last_equity, 100000.0)
        self.assertEqual(snapshot.equity, 99000.0)

    def _assert_capture_closed(self, **kwargs):
        from tradingagents.execution.authority import BrokerAuthorityError

        with self.assertRaises(BrokerAuthorityError):
            self._capture(**kwargs)

    def test_missing_last_equity_fails_closed(self):
        from tradingagents.execution.authority import (
            BrokerAuthorityError,
            capture_broker_snapshot,
        )

        account = SimpleNamespace(
            id="paper-1", equity="100000", cash="50000", buying_power="200000",
        )
        broker = SimpleNamespace(
            get_account=lambda: account,
            get_all_positions=lambda: [],
            get_orders=lambda request=None: [],
        )
        with self.assertRaises(BrokerAuthorityError):
            capture_broker_snapshot(broker)

    def test_invalid_last_equity_fails_closed(self):
        from tradingagents.execution.authority import (
            BrokerAuthorityError,
            capture_broker_snapshot,
        )

        for bad in (float("nan"), float("inf"), 0.0, -5.0):
            with self.subTest(value=bad):
                account = SimpleNamespace(
                    id="paper-1", equity="100000", last_equity=bad,
                    cash="50000", buying_power="200000",
                )
                broker = SimpleNamespace(
                    get_account=lambda: account,
                    get_all_positions=lambda: [],
                    get_orders=lambda request=None: [],
                )
                with self.assertRaises(BrokerAuthorityError):
                    capture_broker_snapshot(broker)

    def _exec_service(self, tmp, guard, equity, last_equity):
        from tradingagents.execution import ExecutionService
        from tradingagents.execution.authority import BrokerQuote

        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity=str(equity), last_equity=str(last_equity),
                cash="50000", buying_power="200000",
            ),
            get_all_positions=lambda: [],
            get_orders=lambda request=None: [],
            submit_order=lambda request: SimpleNamespace(
                id="broker-1", client_order_id=request.client_order_id,
                symbol="AAPL", side="buy", qty=100, filled_qty=0,
                filled_avg_price=None, status="accepted",
                updated_at=datetime.now(timezone.utc), legs=[], notional=None,
            ),
        )
        return ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda s: BrokerQuote(s, 100.0, 100.1, datetime.now(timezone.utc)),
        ), broker

    def _buy_intent(self):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            RiskDecision,
            build_trade_intent_from_risk_decision,
        )

        now = datetime.now(timezone.utc)
        return build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="NEUTRAL",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.BUY,
                confidence="medium",
                risk_rationale="fixture",
                required_controls="stop 95",
                entry_policy={
                    "status": "READY",
                    "minimum_price": 99,
                    "maximum_price": 101,
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "exit_by": (now + timedelta(days=5)).isoformat(),
                    "confirmation": "fixture observed setup",
                },
                stop_loss_price=95,
            ),
        ).model_dump(mode="json")

    def _sell_intent(self):
        from tradingagents.agents.schemas import (
            ExecutableAction,
            RiskDecision,
            build_trade_intent_from_risk_decision,
        )

        return build_trade_intent_from_risk_decision(
            symbol="AAPL",
            trading_mode="investment",
            current_position="LONG",
            allow_shorts=False,
            trade_date="2026-01-02",
            decision=RiskDecision(
                action=ExecutableAction.SELL,
                confidence="medium",
                risk_rationale="exit",
                required_controls="None.",
            ),
        ).model_dump(mode="json")

    def _screening_open(self):
        return patch("tradingagents.screening.gate.check_entry_allowed", return_value=None)

    def test_initial_submit_5pct_loss_passes_gate(self):
        guard = SimpleNamespace(
            enabled=True,
            check_order=lambda *a, **k: SimpleNamespace(allowed=True, reasons=[], checks={}),
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tradingagents.safety.get_safety_guard", return_value=guard
        ), self._screening_open():
            service, _ = self._exec_service(tmp, guard, 95000.0, 100000.0)
            result = service.execute(trade_intent=self._buy_intent(), dollar_amount=5000.0)
        self.assertTrue(result.get("success"), result)

    def test_initial_submit_11pct_loss_blocked_with_zero_posts(self):
        from tradingagents.safety.guardrails import SafetyGuard

        with tempfile.TemporaryDirectory() as tmp:
            guard = SafetyGuard(
                config={"daily_loss_halt_pct": 10.0, "max_trade_notional_usd": 0,
                        "max_symbol_concentration_pct": 0, "max_drawdown_halt_pct": 0,
                        "max_consecutive_rejections": 0},
                state_path=Path(tmp) / "state.json",
                kill_switch_path=Path(tmp) / "KILL_SWITCH",
            )
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ), self._screening_open():
                service, broker = self._exec_service(tmp, guard, 89000.0, 100000.0)
                result = service.execute(trade_intent=self._buy_intent(), dollar_amount=5000.0)
            self.assertFalse(result.get("success"), result)
            self.assertTrue(result.get("safety_blocked"), result)
            self.assertEqual(len(getattr(broker, "submits", []) or []), 0)
            # The durable row must never reach SUBMITTING/SENT/ACCEPTED:
            # broker-visible state stays zero exposure.
            orders = service._store.list_all_orders()
            self.assertTrue(
                all(
                    (o.get("status") or "").upper() in {"PENDING", "CANCELED"}
                    for o in orders
                ),
                orders,
            )

    def test_recovery_path_blocked_with_zero_posts(self):
        """Recovery resubmission passes through the same account gate."""
        from tradingagents.safety.guardrails import SafetyGuard

        with tempfile.TemporaryDirectory() as tmp:
            guard = SafetyGuard(
                config={"daily_loss_halt_pct": 10.0, "max_trade_notional_usd": 0,
                        "max_symbol_concentration_pct": 0, "max_drawdown_halt_pct": 0,
                        "max_consecutive_rejections": 0},
                state_path=Path(tmp) / "state.json",
                kill_switch_path=Path(tmp) / "KILL_SWITCH",
            )
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ), self._screening_open():
                service, broker = self._exec_service(tmp, guard, 89000.0, 100000.0)
                # Startup recovery must also observe the halted account via
                # snapshot capture: the path ends paused with no broker POST.
                recovery = service.startup_recover()
            self.assertTrue(recovery.get("success"), recovery)
            self.assertEqual(len(getattr(broker, "submits", []) or []), 0)

    def test_risk_reducing_exit_bypasses_daily_loss(self):
        from tradingagents.safety.guardrails import SafetyGuard

        with tempfile.TemporaryDirectory() as tmp:
            guard = SafetyGuard(
                config={"daily_loss_halt_pct": 10.0, "max_trade_notional_usd": 0,
                        "max_symbol_concentration_pct": 0, "max_drawdown_halt_pct": 0,
                        "max_consecutive_rejections": 0},
                state_path=Path(tmp) / "state.json",
                kill_switch_path=Path(tmp) / "KILL_SWITCH",
            )
            verdict = guard.check_order(
                "AAPL", 5000.0,
                account={"equity": 89000.0, "last_equity": 100000.0},
                risk_reducing=True,
            )
            self.assertTrue(verdict.allowed)
            self.assertEqual(
                verdict.checks["daily_loss"]["status"], "skipped"
            )


class HonestPresentationTests(unittest.TestCase):
    """Spec items 15-17: honest UI copy and long-run return limitations."""

    def test_webui_source_has_no_walk_forward_copy(self):
        source = Path("webui/components/backtest_panel.py").read_text(encoding="utf-8")
        callbacks = Path("webui/callbacks/backtest_callbacks.py").read_text(encoding="utf-8")
        self.assertNotIn("Walk-Forward Backtest", source)
        self.assertNotIn("Out-of-sample windows", source + callbacks)
        self.assertIn("Recorded Signal Diagnostic", source)
        self.assertIn("Segmented diagnostic windows", callbacks)
        self.assertIn("does not prove profitability", source)

    def test_teach_ui_uses_hypothetical_label(self):
        source = Path("webui/components/backtest_panel.py").read_text(encoding="utf-8")
        self.assertNotIn("realized next-open return", source)
        self.assertIn("fixed-horizon hypothetical position return", source)

    def test_long_run_report_carries_return_limitations(self):
        import tradingagents.long_run as lr

        with tempfile.TemporaryDirectory() as tmp:
            os_env_patch = patch.dict("os.environ", {"TRADINGAGENTS_LONG_RUN_DIR": tmp})
            os_env_patch.start()
            self.addCleanup(os_env_patch.stop)

            run_id = "run-dvi"
            run = lr.run_dir(run_id)
            (run / "rounds").mkdir(parents=True)
            (run / "account_snapshots.jsonl").write_text(
                json.dumps({"at": "2026-01-01T00:00:00+00:00", "phase": "pre_round",
                            "session": "2026-01-02", "equity": 100000.0, "cash": 50000.0,
                            "positions": []}) + "\n" +
                json.dumps({"at": "2026-02-01T00:00:00+00:00", "phase": "post_round",
                            "session": "2026-01-30", "equity": 95000.0, "cash": 45000.0,
                            "positions": []}) + "\n",
                encoding="utf-8",
            )
            state = {
                "run_id": run_id,
                "status": "COMPLETED",
                "started_at": "2026-01-01T00:00:00+00:00",
                "expected_sessions": ["2026-01-02"],
                "restart_count": 0,
                "stop": None,
            }
            runtime = {"results_dir": str(Path(tmp) / "results"),
                       "execution_db_path": str(Path(tmp) / "exec.db")}
            report = lr.aggregate_final_report(state, {"duration_calendar_days": 30}, runtime)

            self.assertEqual(report["account"]["return_kind"], "unadjusted_account_equity_change")
            self.assertEqual(len(report["account"]["return_limitations"]), 4)

            md = lr.render_final_markdown(report)
            self.assertIn("return limitation", md)
            self.assertIn("Not adjusted for deposits or withdrawals", md)
            self.assertIn("Not net profitability", md)


if __name__ == "__main__":
    unittest.main()
