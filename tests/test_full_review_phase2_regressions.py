"""Phase 2 full-review regression tests (F08/F13/F14/F15/F16/F17/F18/F19).

Behavioral tests over the real production layers; external transports are
faked. No real Alpaca mutation and no paid LLM call anywhere in this file.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from tradingagents.safety import DEFAULT_SAFETY_CONFIG, SafetyGuard


def _make_guard(tmp, **overrides):
    config = dict(DEFAULT_SAFETY_CONFIG)
    config.update(overrides)
    return SafetyGuard(
        config=config,
        state_path=Path(tmp) / "safety" / "state.json",
        kill_switch_path=Path(tmp) / "safety" / "KILL_SWITCH",
    )


def _run_log_dir(results_dir: Path, symbol: str) -> Path:
    return results_dir / symbol / "TradingAgentsStrategy_logs" / "runs"


def _write_run_log(
    results_dir: Path,
    symbol: str,
    run_id: str,
    *,
    metadata: dict,
    llm_calls: list,
    status: str = "completed",
) -> Path:
    path = _run_log_dir(results_dir, symbol)
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "symbol": symbol,
        "trade_date": "2026-09-08",
        "started_at": "2026-09-08T14:30:00+00:00",
        "ended_at": "2026-09-08T15:00:00+00:00",
        "status": status,
        "metadata": metadata,
        "events": [
            {"timestamp": "2026-09-08T14:31:00+00:00", "type": "llm_call",
             "payload": call}
            for call in llm_calls
        ],
        "summary": {"total_llm_tokens": sum(
            (c.get("usage") or {}).get("total_tokens", 0) for c in llm_calls)},
        "snapshots": {},
    }
    target = path / f"{run_id}.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


class _Isolated:
    """Snapshot process-global state; offline, fake-broker only."""

    def setUp(self):
        import tradingagents.dataflows.config as _cfgmod

        self._cfgmod = _cfgmod
        self._saved_config = _cfgmod.get_config()
        self.workdir = Path(tempfile.mkdtemp(prefix="phase2-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.old_env = dict(os.environ)
        self._guard = _make_guard(self.workdir)
        self._guard_patch = patch(
            "tradingagents.safety.get_safety_guard", return_value=self._guard
        )
        self._guard_patch.start()
        self._audit_patch = patch(
            "tradingagents.run_logger._RUN_AUDIT_LOGGER",
            __import__("tradingagents.run_logger", fromlist=["RunAuditLogger"]).RunAuditLogger(),
        )
        self._audit_patch.start()
        super().setUp()

    def tearDown(self):
        self._audit_patch.stop()
        self._guard_patch.stop()
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)
        self._cfgmod._config = dict(self._saved_config)
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)
        super().tearDown()


# ---------------------------------------------------------------------------
# F08 — llm_request_timeout_seconds reaches every client construction path
# ---------------------------------------------------------------------------


class F08TimeoutTests(_Isolated, unittest.TestCase):
    def _graph_with_roles(self, timeout=7):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        captured = []

        def _fake_create_llm_client(provider, model, base_url=None, **kwargs):
            captured.append({"provider": provider, "model": model, **kwargs})
            return MagicMock(get_llm=MagicMock(return_value=MagicMock()))

        config = {
            "llm_provider": "openai",
            "quick_think_llm": "gpt-fake-quick",
            "deep_think_llm": "gpt-fake-deep",
            "llm_request_timeout_seconds": timeout,
            "llm_max_retries": 0,
            "results_dir": str(self.workdir / "results"),
            "data_cache_dir": str(self.workdir / "cache"),
            "analysis_provider": "openai",
            "analysis_model": "gpt-fake-analysis",
            "decision_provider": "anthropic",
            "decision_model": "claude-fake-decision",
            "memory_outcome_holding_days": 5,
            "reflection_on_outcome_enabled": False,
        }
        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            side_effect=_fake_create_llm_client,
        ), patch(
            "tradingagents.llm_clients.roles._resolve_provider_key",
            return_value="fake-key",
        ):
            TradingAgentsGraph(["market"], config=config, debug=False)
        return captured

    def test_role_clients_receive_configured_timeout(self):
        captured = self._graph_with_roles(timeout=7)
        by_model = {c["model"]: c for c in captured}
        self.assertEqual(by_model["gpt-fake-analysis"]["timeout"], 7)
        self.assertEqual(by_model["claude-fake-decision"]["timeout"], 7)
        # Provider-generic kwargs (both OpenAI and Anthropic paths) preserved.
        self.assertEqual(by_model["claude-fake-decision"]["provider"], "anthropic")

    def test_analysis_fallback_receives_configured_timeout(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        captured = []

        def _fake_create_llm_client(provider, model, base_url=None, **kwargs):
            captured.append({"provider": provider, "model": model, **kwargs})
            return MagicMock(get_llm=MagicMock(return_value=MagicMock()))

        config = {
            "llm_provider": "openai",
            "quick_think_llm": "gpt-fake-quick",
            "deep_think_llm": "gpt-fake-deep",
            "llm_request_timeout_seconds": 9,
            "llm_max_retries": 0,
            "results_dir": str(self.workdir / "results"),
            "data_cache_dir": str(self.workdir / "cache"),
            "analysis_provider": "openai",
            "analysis_model": "gpt-fake-analysis",
            "analysis_fallback_provider": "openai",
            "analysis_fallback_model": "gpt-fake-fallback",
            "memory_outcome_holding_days": 5,
            "reflection_on_outcome_enabled": False,
        }
        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            side_effect=_fake_create_llm_client,
        ), patch(
            "tradingagents.llm_clients.roles._resolve_provider_key",
            return_value="fake-key",
        ):
            TradingAgentsGraph(["market"], config=config, debug=False)
        by_model = {c["model"]: c for c in captured}
        self.assertEqual(by_model["gpt-fake-fallback"]["timeout"], 9)
        self.assertEqual(by_model["gpt-fake-analysis"]["timeout"], 9)

    def test_screening_llm_receives_configured_timeout(self):
        from tradingagents.screening.llm import build_screening_llm, resolve_screening_config

        captured = []

        def _fake_create_llm_client(provider, model, base_url=None, **kwargs):
            captured.append({"provider": provider, "model": model, **kwargs})
            return MagicMock(get_llm=MagicMock(return_value=MagicMock()))

        config = {
            "auto_screening_enabled": True,
            "screening_provider": "google",
            "screening_model": "gemini-fake-screening",
            "llm_request_timeout_seconds": 11,
        }
        resolved = resolve_screening_config(config)
        resolved["api_key"] = "fake-key"
        with patch(
            "tradingagents.llm_clients.create_llm_client",
            side_effect=_fake_create_llm_client,
        ):
            build_screening_llm(resolved, config)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["timeout"], 11)
        self.assertEqual(captured[0]["provider"], "google")

    def test_gpt5_factory_and_bound_clone_preserve_timeout_and_single_callback(self):
        from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel, get_chat_model
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        model = get_chat_model(
            "gpt-5-mini",
            api_key="k",
            model_role="quick",
            timeout=13.0,
            callbacks=[UsageAccountingCallback()],
        )
        self.assertIsInstance(model, GPT5ChatModel)
        self.assertEqual(model.timeout, 13.0)
        # Exactly one usage callback survives construction.
        self.assertEqual(
            sum(1 for c in model.callbacks if isinstance(c, UsageAccountingCallback)), 1
        )
        bound = model.bind_tools([_FakeScreeningSchema], tool_choice="required")
        self.assertEqual(bound.timeout, 13.0)
        self.assertEqual(
            sum(1 for c in bound.callbacks if isinstance(c, UsageAccountingCallback)), 1
        )


from pydantic import BaseModel, ConfigDict


class _FakeScreeningSchema(BaseModel):
    """Minimal Pydantic schema bindable as a Responses function tool."""

    model_config = ConfigDict(extra="forbid")

    answer: str


# ---------------------------------------------------------------------------
# F15 — common usage path: budget-before-attribution, exactly once
# ---------------------------------------------------------------------------


class F15UsageTests(_Isolated, unittest.TestCase):
    def _logger(self):
        from tradingagents.run_logger import get_run_audit_logger

        return get_run_audit_logger()

    def test_budget_increments_without_active_run(self):
        before = self._guard.llm_tokens_used()
        self._logger().log_event(
            "llm_call",
            payload={"usage": {"total_tokens": 100}, "model": "gpt-5-mini"},
        )
        self.assertEqual(self._guard.llm_tokens_used() - before, 100)

    def test_budget_increments_exactly_once_with_active_run(self):
        run_id = self._logger().start_run(symbol="AAPL", trade_date="2026-09-08")
        before = self._guard.llm_tokens_used()
        self._logger().log_event(
            "llm_call", symbol="AAPL", run_id=run_id,
            payload={"usage": {"total_tokens": 80}, "model": "m"},
        )
        self.assertEqual(self._guard.llm_tokens_used() - before, 80)
        self._logger().finish_run(symbol="AAPL", run_id=run_id)

    def test_zero_or_missing_usage_never_increments(self):
        before = self._guard.llm_tokens_used()
        self._logger().log_event("llm_call", payload={"usage": {"total_tokens": 0}})
        self._logger().log_event("llm_call", payload={})
        self._logger().log_event("llm_call", payload={"usage": {"total_tokens": "junk"}})
        self.assertEqual(self._guard.llm_tokens_used(), before)

    def test_langchain_usage_callback_records_generic_provider_usage_once(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        callback = UsageAccountingCallback()
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 30, "output_tokens": 50, "total_tokens": 80},
            response_metadata={"model_name": "gemini-fake"},
        )
        callback.on_llm_end(ChatResult(generations=[ChatGeneration(message=message)]))
        run_id = self._logger().start_run(symbol="AAPL", trade_date="2026-09-08")
        before = self._guard.llm_tokens_used()
        callback.on_llm_end(ChatResult(generations=[ChatGeneration(message=message)]),
                            run_id=__import__("uuid").uuid4())
        self.assertEqual(self._guard.llm_tokens_used() - before, 80)
        self._logger().finish_run(symbol="AAPL", run_id=run_id)

    def test_langchain_response_metadata_usage_extracted(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from tradingagents.llm_clients.usage import extract_langchain_usage

        message = AIMessage(
            content="ok",
            response_metadata={
                "model_name": "claude-fake",
                "token_usage": {"input_tokens": 12, "output_tokens": 34},
            },
        )
        usage, model = extract_langchain_usage(
            ChatResult(generations=[ChatGeneration(message=message)])
        )
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46})
        self.assertEqual(model, "claude-fake")

    def test_usage_metadata_keeps_response_metadata_model_attribution(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult
        from tradingagents.llm_clients.usage import extract_langchain_usage

        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            response_metadata={"model_name": "gemini-fake"},
        )
        usage, model = extract_langchain_usage(
            LLMResult(generations=[[ChatGeneration(message=message)]])
        )

        self.assertEqual(usage["total_tokens"], 7)
        self.assertEqual(model, "gemini-fake")

    def test_google_shaped_usage_normalized(self):
        from tradingagents.llm_clients.usage import normalize_usage_map

        usage = normalize_usage_map(
            {"prompt_token_count": 10, "candidates_token_count": 15}
        )
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 15, "total_tokens": 25})
        anthropic = normalize_usage_map({"input_tokens": 7, "output_tokens": 9})
        self.assertEqual(anthropic["total_tokens"], 16)

    def test_missing_usage_is_never_invented(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        callback = UsageAccountingCallback()
        message = AIMessage(content="no usage anywhere")
        before = self._guard.llm_tokens_used()
        callback.on_llm_end(ChatResult(generations=[ChatGeneration(message=message)]))
        self.assertEqual(self._guard.llm_tokens_used(), before)

    def test_callback_skips_adapter_marked_results(self):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        callback = UsageAccountingCallback()
        message = AIMessage(
            content="x",
            additional_kwargs={"usage_accounted_by_adapter": True},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        before = self._guard.llm_tokens_used()
        callback.on_llm_end(ChatResult(generations=[ChatGeneration(message=message)]))
        self.assertEqual(self._guard.llm_tokens_used(), before)

    def test_gpt5_adapter_accounts_exactly_once(self):
        """Fake Responses transport returns usage 123: budget +123, one event,
        UI counter may update but never a second SafetyGuard increment."""
        from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel

        usage_obj = SimpleNamespace(
            input_tokens=100, output_tokens=23, total_tokens=123)
        response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(
                type="output_text", text="hello")])],
            output_text="hello", usage=usage_obj,
        )
        model = GPT5ChatModel(model="gpt-5-mini", api_key="test-key")
        object.__setattr__(
            model, "_client", SimpleNamespace(
                responses=SimpleNamespace(create=MagicMock(return_value=response)))
        )

        before = self._guard.llm_tokens_used()
        ui_calls = []

        class _FakeAppState:
            llm_calls_log = []
            llm_calls_count = 0
            needs_ui_update = False

            @staticmethod
            def register_llm_call(**kwargs):
                ui_calls.append(kwargs)

        audit = self._logger()
        run_id = audit.start_run(symbol="AAPL", trade_date="2026-09-08")
        with patch("webui.utils.state.app_state", _FakeAppState):
            model.invoke("hi")
        audit.finish_run(symbol="AAPL", run_id=run_id)

        self.assertEqual(self._guard.llm_tokens_used() - before, 123)
        self.assertEqual(len(ui_calls), 1)
        self.assertFalse(ui_calls[0].get("write_audit", True))

        # Exactly one audit event carrying the usage in the run log.
        payloads = [
            json.loads(p.read_text())
            for p in Path("eval_results").glob("AAPL/TradingAgentsStrategy_logs/runs/*.json")
        ]
        usage_events = [
            e for p in payloads for e in p["events"]
            if e["type"] == "llm_call" and e["payload"].get("purpose") == "gpt5_responses"
        ]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0]["payload"]["usage"]["total_tokens"], 123)

    def test_direct_openai_tool_usage_recorded_once(self):
        from tradingagents.llm_clients.usage import record_direct_openai_usage

        before = self._guard.llm_tokens_used()
        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=70, output_tokens=7, total_tokens=77))
        record_direct_openai_usage(
            response, model="gpt-5.4-nano", purpose="tool:stock_news_openai")
        self.assertEqual(self._guard.llm_tokens_used() - before, 77)

        run_id = self._logger().start_run(symbol="AAPL", trade_date="2026-09-08")
        record_direct_openai_usage(
            response, model="gpt-5.4-nano", purpose="tool:stock_news_openai")
        self._logger().finish_run(symbol="AAPL", run_id=run_id)
        self.assertEqual(self._guard.llm_tokens_used() - before, 154)

    def test_interface_web_search_tool_records_usage_via_fake_transport(self):
        """Exercise the real get_stock_news_openai layer with a fake OpenAI
        transport: usage 77 is recorded exactly once."""
        import tradingagents.dataflows.interface as interface
        from tradingagents.dataflows.interface_utils import current_analysis_date

        response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(
                type="output_text", text="news")])],
            output_text="news",
            usage=SimpleNamespace(input_tokens=70, output_tokens=7, total_tokens=77),
        )
        fake_client = SimpleNamespace(
            responses=SimpleNamespace(create=MagicMock(return_value=response)),
            chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock())),
        )
        before = self._guard.llm_tokens_used()
        with patch.object(
            interface, "get_openai_client_with_timeout", return_value=fake_client
        ), patch.object(interface, "get_api_key", return_value="fake-key"):
            # Live-mode gate compares against real "today" (ET); a hard-coded
            # date turns historical the day after it is written and the tool
            # short-circuits before recording usage.
            interface.get_stock_news_openai("AAPL", current_analysis_date())
        self.assertEqual(self._guard.llm_tokens_used() - before, 77)
        self.assertEqual(fake_client.responses.create.call_count, 1)
        self.assertEqual(fake_client.chat.completions.create.call_count, 0)

    # -- F15 remediation: real nested LLMResult callback shapes ------------

    def _nested_result(self, message, llm_output=None):
        from langchain_core.outputs import ChatGeneration, LLMResult

        return LLMResult(
            generations=[[ChatGeneration(message=message)]],
            llm_output=llm_output if llm_output is not None else {},
        )

    def test_nested_llm_result_generations_accounted(self):
        """F15-A: the real on_llm_end shape is LLMResult.generations=[[gen]];
        provider-reported usage must reach the budget with no active run."""
        from langchain_core.messages import AIMessage
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 30, "output_tokens": 50,
                            "total_tokens": 80},
        )
        before = self._guard.llm_tokens_used()
        UsageAccountingCallback().on_llm_end(self._nested_result(message))
        self.assertEqual(self._guard.llm_tokens_used() - before, 80)

    def test_nested_result_recorded_exactly_once_in_run(self):
        """F15-B: under an active audit run the nested result increments the
        budget exactly once, emits exactly one llm_call event, and the run
        summary carries 80 (never 160)."""
        import uuid
        from langchain_core.messages import AIMessage
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 30, "output_tokens": 50,
                            "total_tokens": 80},
        )
        audit = self._logger()
        run_id = audit.start_run(symbol="AAPL", trade_date="2026-09-08")
        before = self._guard.llm_tokens_used()
        UsageAccountingCallback().on_llm_end(
            self._nested_result(message), run_id=uuid.uuid4())
        self.assertEqual(self._guard.llm_tokens_used() - before, 80)
        audit.finish_run(symbol="AAPL", run_id=run_id)

        payloads = [
            json.loads(p.read_text())
            for p in Path("eval_results").glob(
                "AAPL/TradingAgentsStrategy_logs/runs/*.json")
        ]
        events = [
            e for p in payloads for e in p["events"]
            if e["type"] == "llm_call"
            and e["payload"].get("purpose") == "langchain_provider"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["usage"]["total_tokens"], 80)
        self.assertEqual(
            sum(p["summary"]["total_llm_tokens"] for p in payloads), 80)

    def test_adapter_marker_skips_nested_result(self):
        """F15-C: the Responses-adapter marker must be honored inside the
        nested shape — llm_output usage must not leak a second count."""
        from langchain_core.messages import AIMessage
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        message = AIMessage(
            content="x",
            additional_kwargs={"usage_accounted_by_adapter": True},
            usage_metadata={"input_tokens": 30, "output_tokens": 50,
                            "total_tokens": 80},
        )
        result = self._nested_result(
            message,
            llm_output={"token_usage": {"input_tokens": 30, "output_tokens": 50,
                                        "total_tokens": 80}},
        )
        before = self._guard.llm_tokens_used()
        UsageAccountingCallback().on_llm_end(result)
        self.assertEqual(self._guard.llm_tokens_used(), before)

    def test_google_shaped_nested_usage_accounted(self):
        """F15-D: message.usage_metadata is the source for Google/Gemini
        results; llm_output with only prompt_feedback must not lose tokens
        and must not depend on llm_output['token_usage']."""
        from langchain_core.messages import AIMessage
        from tradingagents.llm_clients.usage import UsageAccountingCallback

        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 10, "output_tokens": 15,
                            "total_tokens": 25},
        )
        before = self._guard.llm_tokens_used()
        UsageAccountingCallback().on_llm_end(self._nested_result(
            message, llm_output={"prompt_feedback": {}}))
        self.assertEqual(self._guard.llm_tokens_used() - before, 25)

    def test_nested_usage_kept_without_model_attribution(self):
        """F15: usage present but no model anywhere — tokens are still
        counted; model attribution stays unknown and is never guessed."""
        from langchain_core.messages import AIMessage
        from tradingagents.llm_clients.usage import extract_langchain_usage

        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 3, "output_tokens": 4,
                            "total_tokens": 7},
        )
        usage, model = extract_langchain_usage(self._nested_result(message))
        self.assertEqual(usage["total_tokens"], 7)
        self.assertIsNone(model)


# ---------------------------------------------------------------------------
# F15.6 — long-run screening is attributed to the exact observation
# ---------------------------------------------------------------------------


class ScreeningAttributionTests(_Isolated, unittest.TestCase):
    def test_screening_audit_run_carries_observation_metadata(self):
        import tradingagents.long_run as lr

        plan = SimpleNamespace(stopped=False, stop_reason_text=lambda: "")
        plan_result = lr._screening_with_audit_scope(
            lambda config, refresh=False: plan,
            {"results_dir": "eval_results"},
            "lr-obs-1", "2026-09-08",
        )
        self.assertFalse(plan_result.stopped)

        matches = list(
            Path("eval_results/__SCREENING__/TradingAgentsStrategy_logs/runs").glob("*.json"))
        self.assertEqual(len(matches), 1)
        payload = json.loads(matches[0].read_text())
        self.assertEqual(payload["metadata"]["source"], "long_run_screening")
        self.assertEqual(payload["metadata"]["long_run_observation_id"], "lr-obs-1")
        self.assertEqual(payload["status"], "completed")

    def test_screening_failure_finishes_run_failed(self):
        import tradingagents.long_run as lr

        def _boom(config, refresh=False):
            raise RuntimeError("screening exploded")

        with self.assertRaises(RuntimeError):
            lr._screening_with_audit_scope(
                _boom, {"results_dir": "eval_results"}, "lr-obs-2", "2026-09-08")
        matches = list(
            Path("eval_results/__SCREENING__/TradingAgentsStrategy_logs/runs").glob("*.json"))
        self.assertEqual(len(matches), 1)
        payload = json.loads(matches[0].read_text())
        self.assertEqual(payload["status"], "failed")


# ---------------------------------------------------------------------------
# F19 — long-run daily LLM budget gate
# ---------------------------------------------------------------------------


class _BudgetRoundFixture(_Isolated):
    def _setup_long_run(self):
        import tradingagents.long_run as lr

        self._lr = lr
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")
        self._regime = patch(
            "tradingagents.regime.regime_risk_multiplier", return_value=1.0)
        self._portfolio = patch(
            "tradingagents.portfolio.adjust_new_position_notional",
            side_effect=lambda s, a, amount, gather_state=None, config=None: amount)
        self._regime.start()
        self._portfolio.start()

    def tearDown(self):
        if hasattr(self, "_regime"):
            self._regime.stop()
            self._portfolio.stop()
        super().tearDown()

    def _cfg(self):
        cfg = self._lr.default_long_run_config()
        cfg.update({
            "run_time_et": "11:00",
            "base_trade_notional_usd": 1000.0,
            "analysts": ["market"],
        })
        return cfg

    def _plan(self, symbols):
        return SimpleNamespace(
            stopped=False,
            deep_analysis_set=list(symbols),
            top20=[{"symbol": s, "rank": i + 1, "screening_score": 90.0,
                    "short_reason": "r"} for i, s in enumerate(symbols)],
            selection_date="2026-09-08", as_of="2026-09-08", cached=False,
            overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
            screening_description="s", stop_reason_text=lambda: "",
        )

    def _deps(self, service, graph, plan):
        return self._lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: plan,
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )


class _FakeBrokerForBudget:
    def get_account(self):
        return SimpleNamespace(
            id="acct", equity=100000.0, cash=50000.0, buying_power=50000.0)

    def get_all_positions(self):
        return []


class _FakeBudgetService:
    def __init__(self):
        self.execute_calls = []

    def enforce_exit_deadlines(self):
        return {"success": True, "deadline_exits": [], "broker_calls": 0}

    def startup_recover(self):
        return {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        return {"success": True, "broker_attempted": True, "broker_calls": 1,
                "decision_id": kwargs.get("decision_id")}


class _CountingGraph:
    def __init__(self):
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append(symbol)
        intent = {"symbol": symbol, "action": "BUY", "target_position": "LONG"}
        return {"final_trade_intent": intent, "final_trade_decision": "BUY"}, "BUY"


class _SilentGraph:
    def __init__(self):
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append(symbol)
        raise AssertionError("no LLM analysis may run under an exhausted budget")


class F19BudgetGateTests(_BudgetRoundFixture, unittest.TestCase):
    def test_exhausted_budget_before_round_stops_with_zero_llm_work(self):
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard.record_llm_tokens(2)
        plan = self._plan(["AAA"])
        graph = _SilentGraph()
        deps = self._deps(_FakeBudgetService(), graph, plan)
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-budget-1", session_date="2026-09-08",
                long_cfg=self._cfg(),
                runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(len(graph.calls), 0)

    def test_screening_consuming_remainder_blocks_first_analysis(self):
        self._setup_long_run()
        lr = self._lr
        # Budget 1 used 0; screening scope accounts the screening call (+1)
        # so the first fresh symbol must be blocked.
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard._save_state()

        real_scope = lr._screening_with_audit_scope

        def _scope_with_usage(screening_fn, runtime, run_id, session_date):
            self._guard.record_llm_tokens(1)
            return real_scope(screening_fn, runtime, run_id, session_date)

        plan = self._plan(["AAA"])
        graph = _SilentGraph()
        deps = self._deps(_FakeBudgetService(), graph, plan)
        with patch.object(lr, "_screening_with_audit_scope",
                          side_effect=_scope_with_usage):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.run_daily_round(
                    run_id="run-budget-2", session_date="2026-09-08",
                    long_cfg=self._cfg(),
                    runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(len(graph.calls), 0)

    def test_analyzed_intent_executes_without_new_llm_work(self):
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard.record_llm_tokens(5)  # far over budget
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        # A journal that reached symbol analysis always carries the persisted
        # screening summary — the execution-only resume's proof of selection.
        journal["screening"] = {
            "selection_date": "2026-09-08", "as_of": "2026-09-08",
            "cached": False, "top20": [{"symbol": "AAA", "rank": 1}],
            "overlap_holdings": [], "extra_holdings": [],
            "blocked_holdings": [], "deep_analysis_set": ["AAA"],
            "description": "s",
        }
        journal["symbols"]["AAA"] = {
            "status": "ANALYZED", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": {"symbol": "AAA", "action": "BUY",
                             "target_position": "LONG"},
            "execution_result_summary": None,
        }
        lr.save_round_journal("run-budget-3", journal)
        graph = _SilentGraph()
        service = _FakeBudgetService()
        deps = self._deps(service, graph, self._plan(["AAA"]))
        out = lr.run_daily_round(
            run_id="run-budget-3", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)
        self.assertEqual(len(graph.calls), 0)

    def test_budget_zero_is_unlimited(self):
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 0
        self._guard.record_llm_tokens(10_000_000)
        plan = self._plan(["AAA"])
        graph = _CountingGraph()
        deps = self._deps(_FakeBudgetService(), graph, plan)
        out = lr.run_daily_round(
            run_id="run-budget-4", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(graph.calls), 1)


class _ScreeningForbidden:
    """screening_fn/graph that must never be reached on an execution-only
    resume (F19 remediation)."""

    def _forbidden_screening(self, config, refresh=False):
        raise AssertionError("screening must not run on execution-only resume")

    def _forbidden_graph_factory(self, config):
        raise AssertionError("no LLM analysis may run on execution-only resume")


class F19ExecutionOnlyResumeTests(_ScreeningForbidden, _BudgetRoundFixture,
                                  unittest.TestCase):
    def _journal_with_screening(self, run_id, status, intent):
        lr = self._lr
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        journal["screening"] = {
            "selection_date": "2026-09-08", "as_of": "2026-09-08",
            "cached": False, "top20": [{"symbol": "AAA", "rank": 1}],
            "overlap_holdings": [], "extra_holdings": [],
            "blocked_holdings": [], "deep_analysis_set": ["AAA"],
            "description": "s",
        }
        journal["symbols"]["AAA"] = {
            "status": status, "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": intent, "execution_result_summary": None,
        }
        lr.save_round_journal(run_id, journal)
        return journal

    def _intent(self, symbol="AAA"):
        return {"symbol": symbol, "action": "BUY",
                "target_position": "LONG"}

    def test_analyzed_only_resume_skips_screening_pipeline(self):
        """F19-A: ANALYZED-only resume must not call screening_fn nor the
        analysis graph; the persisted intent still reaches execution."""
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 0  # budget exhausted
        self._guard.record_llm_tokens(1_000_000)
        self._journal_with_screening(
            "run-resume-a", "ANALYZED", self._intent())
        graph_calls = []

        class _Graph:
            def propagate(self, symbol, trade_date):
                graph_calls.append(symbol)
                raise AssertionError("no LLM analysis on execution-only resume")

        service = _FakeBudgetService()
        deps = self._lr.LongRunDeps(
            screening_fn=self._forbidden_screening,
            graph_factory=self._forbidden_graph_factory,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        out = lr.run_daily_round(
            run_id="run-resume-a", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)
        self.assertEqual(graph_calls, [])
        # The persisted screening summary was not rewritten/replaced.
        self.assertEqual(
            out["screening"]["top20"], [{"symbol": "AAA", "rank": 1}])

    def test_executing_only_resume_skips_screening_and_recovers(self):
        """F19-B: EXECUTING-only resume goes straight to startup recovery +
        deterministic execution re-entry, with zero screening/analysis."""
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 0
        self._guard.record_llm_tokens(1_000_000)
        self._journal_with_screening(
            "run-resume-b", "EXECUTING", self._intent())
        service = _FakeBudgetService()
        deps = self._lr.LongRunDeps(
            screening_fn=self._forbidden_screening,
            graph_factory=self._forbidden_graph_factory,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        out = lr.run_daily_round(
            run_id="run-resume-b", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)

    def test_execution_only_resume_with_exhausted_budget_and_no_cache(self):
        """F19-C: budget exhausted AND no selection cache anywhere — the
        journal itself authorizes the resume; screening must never run."""
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard.record_llm_tokens(50)
        self._journal_with_screening(
            "run-resume-c", "ANALYZED", self._intent())
        service = _FakeBudgetService()
        deps = self._lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: (_ for _ in ()).throw(
                AssertionError("screening must not run")),
            graph_factory=self._forbidden_graph_factory,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        out = lr.run_daily_round(
            run_id="run-resume-c", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")
        self.assertEqual(len(service.execute_calls), 1)

    def test_pending_resume_with_exhausted_budget_still_gated(self):
        """F19-D: a journal that still needs fresh analysis (PENDING) is
        never skipped — exhausted budget stops with LLM_BUDGET_EXHAUSTED,
        zero screening and zero analysis requests."""
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard.record_llm_tokens(50)
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        journal["screening"] = {
            "selection_date": "2026-09-08", "as_of": "2026-09-08",
            "cached": False, "top20": [{"symbol": "AAA", "rank": 1}],
            "overlap_holdings": [], "extra_holdings": [],
            "blocked_holdings": [], "deep_analysis_set": ["AAA"],
            "description": "s",
        }
        journal["symbols"]["AAA"] = {
            "status": "PENDING", "analysis_run_ref": None, "signal": None,
            "trade_intent": None, "execution_result_summary": None,
        }
        lr.save_round_journal("run-resume-d", journal)
        screening_calls = []

        def _counting_screening(config, refresh=False):
            screening_calls.append(config)
            return self._plan(["AAA"])

        graph = _SilentGraph()
        deps = self._lr.LongRunDeps(
            screening_fn=_counting_screening,
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: _FakeBudgetService(),
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-resume-d", session_date="2026-09-08",
                long_cfg=self._cfg(),
                runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(len(screening_calls), 0)
        self.assertEqual(len(graph.calls), 0)

    def test_journal_without_screening_summary_never_skips(self):
        """Fail-closed: a journal lacking its screening summary cannot prove
        the round selection; resume must re-enter screening behind the
        budget gate (exhausted here → LLM_BUDGET_EXHAUSTED)."""
        self._setup_long_run()
        lr = self._lr
        self._guard.config["daily_llm_token_budget"] = 1
        self._guard.record_llm_tokens(50)
        journal = lr.new_round_journal("2026-09-08", ["AAA"])
        journal["status"] = "RUNNING"
        journal["screening"] = {}
        journal["symbols"]["AAA"] = {
            "status": "ANALYZED", "analysis_run_ref": "x", "signal": "BUY",
            "trade_intent": self._intent(),
            "execution_result_summary": None,
        }
        lr.save_round_journal("run-resume-e", journal)
        service = _FakeBudgetService()
        deps = self._lr.LongRunDeps(
            screening_fn=self._forbidden_screening,
            graph_factory=self._forbidden_graph_factory,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        with self.assertRaises(lr.LongRunStop) as ctx:
            lr.run_daily_round(
                run_id="run-resume-e", session_date="2026-09-08",
                long_cfg=self._cfg(),
                runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(ctx.exception.code, "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(len(service.execute_calls), 0)

    def test_fresh_round_still_runs_screening(self):
        """Gate B: the skip is resume-only — a fresh round still screens and
        analyzes normally."""
        self._setup_long_run()
        lr = self._lr
        plan = self._plan(["AAA"])
        screening_calls = []

        def _counting_screening(config, refresh=False):
            screening_calls.append(config)
            return plan

        graph = _CountingGraph()
        service = _FakeBudgetService()
        deps = self._lr.LongRunDeps(
            screening_fn=_counting_screening,
            graph_factory=lambda config: graph,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBrokerForBudget(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=lambda seconds: None,
        )
        out = lr.run_daily_round(
            run_id="run-fresh-1", session_date="2026-09-08",
            long_cfg=self._cfg(),
            runtime=lr.build_runtime_config(self._cfg()), deps=deps)
        self.assertEqual(len(screening_calls), 1)
        self.assertEqual(len(graph.calls), 1)
        self.assertEqual(out["symbols"]["AAA"]["status"], "DONE")


# ---------------------------------------------------------------------------
# F13 — observation-scoped costs + shared execution DB resolver
# ---------------------------------------------------------------------------


class F13CostScopingTests(_Isolated, unittest.TestCase):
    def test_scan_run_costs_metadata_filter_scopes_to_observation(self):
        results = self.workdir / "results"
        # 1) old/manual run: 1,000,000 tokens, no observation metadata.
        _write_run_log(
            results, "OLD", "run-old-1",
            metadata={"source": "manual"},
            llm_calls=[{"model": "gpt-5", "usage": {
                "input_tokens": 500_000, "output_tokens": 500_000,
                "total_tokens": 1_000_000}}],
        )
        # 2) current observation OBS-A: 100 analysis + 50 screening.
        _write_run_log(
            results, "AAA", "run-obs-a-1",
            metadata={"source": "long_run", "long_run_observation_id": "OBS-A"},
            llm_calls=[{"model": "gpt-5", "usage": {
                "input_tokens": 60, "output_tokens": 40, "total_tokens": 100}}],
        )
        _write_run_log(
            results, "__SCREENING__", "run-obs-a-screen",
            metadata={"source": "long_run_screening",
                      "long_run_observation_id": "OBS-A"},
            llm_calls=[{"model": "gpt-5", "usage": {
                "input_tokens": 30, "output_tokens": 20, "total_tokens": 50}}],
        )
        # 3) concurrent observation OBS-B: 200 tokens.
        _write_run_log(
            results, "BBB", "run-obs-b-1",
            metadata={"source": "long_run", "long_run_observation_id": "OBS-B"},
            llm_calls=[{"model": "gpt-5", "usage": {
                "input_tokens": 120, "output_tokens": 80, "total_tokens": 200}}],
        )

        from tradingagents.llm_cost import aggregate_costs, scan_run_costs

        records = scan_run_costs(
            str(results), metadata_match={"long_run_observation_id": "OBS-A"})
        self.assertEqual(len(records), 2)
        totals = aggregate_costs(records)["totals"]
        self.assertEqual(totals["total_tokens"], 150)

        # Unfiltered scan keeps the global-report behavior (all four runs).
        everything = scan_run_costs(str(results))
        self.assertEqual(len(everything), 4)

    def test_execution_db_resolver_precedence(self):
        from tradingagents.execution.service import resolve_execution_db_path

        os.environ.pop("TRADINGAGENTS_EXECUTION_DB", None)
        self.assertEqual(resolve_execution_db_path(), "eval_results/execution.db")
        os.environ["TRADINGAGENTS_EXECUTION_DB"] = "/tmp/custom.db"
        self.assertEqual(resolve_execution_db_path(), "/tmp/custom.db")
        self.assertEqual(
            resolve_execution_db_path("/explicit.db"), "/explicit.db")

    def test_service_and_report_resolve_same_env_db(self):
        from tradingagents.execution import ExecutionStore
        from tradingagents.execution.service import ExecutionService, resolve_execution_db_path

        custom = self.workdir / "custom-execution.db"
        os.environ["TRADINGAGENTS_EXECUTION_DB"] = str(custom)
        store = ExecutionStore(str(custom))
        orders = store.list_all_orders()  # initializes the store schema
        self.assertIsInstance(orders, list)

        service = ExecutionService()
        self.assertEqual(
            Path(service.db_path).resolve(), custom.resolve())
        self.assertEqual(
            Path(resolve_execution_db_path()).resolve(), custom.resolve())
        # The final report path uses the same resolver (no default file).
        default_db = self.workdir / "eval_results" / "execution.db"
        self.assertFalse(default_db.exists())


# ---------------------------------------------------------------------------
# F14 — fresh final snapshot
# ---------------------------------------------------------------------------


class F14FinalSnapshotTests(_Isolated):
    def _setup_long_run(self):
        import tradingagents.long_run as lr

        self._lr = lr
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")

    def _fake_broker(self, equity, positions):
        return lambda: SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="acct", equity=equity, cash=equity * 0.5,
                buying_power=equity * 0.5),
            get_all_positions=lambda: positions,
        )

    def test_final_report_uses_fresh_final_snapshot(self):
        self._setup_long_run()
        lr = self._lr
        cfg = lr.default_long_run_config()
        cfg["base_trade_notional_usd"] = 1000.0
        state = lr.new_observation_state(
            cfg, expected_sessions=["2026-09-08"])
        state["run_id"] = "run-final-1"
        # Last post-round snapshot: equity 100,000, AAA=10.
        with open(lr.run_dir("run-final-1") / "account_snapshots.jsonl", "w") as f:
            f.write(json.dumps({
                "at": "2026-09-05T20:00:00+00:00", "phase": "post_round",
                "session": "2026-09-05", "equity": 100000.0, "cash": 50000.0,
                "positions": [{"symbol": "AAA", "qty": 10.0, "side": "long",
                               "market_value": 50000.0}],
            }) + "\n")
        # Fresh final broker: equity 101,500, no AAA.
        deps = lr.LongRunDeps(
            broker_client_factory=self._fake_broker(101500.0, []),
            alert_fn=lambda s, b, r: ({}), sleep_fn=lambda s: None,
        )
        lr.finalize_observation(
            state, cfg, lr.build_runtime_config(cfg), deps,
            final_status="COMPLETED")
        report = json.loads(
            (lr.run_dir("run-final-1") / "final_report.json").read_text())
        self.assertEqual(report["account"]["ending_equity"], 101500.0)
        self.assertTrue(report["account"]["ending_snapshot_available"])
        self.assertEqual(report["account"]["ending_positions"], [])
        rows = [json.loads(line) for line in
                (lr.run_dir("run-final-1") / "account_snapshots.jsonl")
                .read_text().splitlines() if line.strip()]
        self.assertEqual(rows[-1]["phase"], "final")

    def test_final_snapshot_failure_reports_unknown(self):
        self._setup_long_run()
        lr = self._lr
        cfg = lr.default_long_run_config()
        state = lr.new_observation_state(cfg, expected_sessions=["2026-09-08"])
        state["run_id"] = "run-final-2"
        with open(lr.run_dir("run-final-2") / "account_snapshots.jsonl", "w") as f:
            f.write(json.dumps({
                "at": "2026-09-05T20:00:00+00:00", "phase": "post_round",
                "session": "2026-09-05", "equity": 100000.0, "cash": 50000.0,
                "positions": [{"symbol": "AAA", "qty": 10.0, "side": "long",
                               "market_value": 50000.0}],
            }) + "\n")

        def _boom():
            raise RuntimeError("broker GET failed")

        deps = lr.LongRunDeps(
            broker_client_factory=_boom,
            alert_fn=lambda s, b, r: ({}), sleep_fn=lambda s: None,
        )
        lr.finalize_observation(
            state, cfg, lr.build_runtime_config(cfg), deps,
            final_status="COMPLETED")
        report = json.loads(
            (lr.run_dir("run-final-2") / "final_report.json").read_text())
        self.assertFalse(report["account"]["ending_snapshot_available"])
        self.assertIsNone(report["account"]["ending_equity"])
        self.assertIsNone(report["account"]["ending_positions"])
        # Stale post-round value is kept only as a diagnostic, never final.
        self.assertEqual(report["account"]["last_observed_equity"], 100000.0)
        self.assertNotEqual(report["account"]["ending_equity"], 100000.0)
        events_path = lr.run_dir("run-final-2") / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()
                  if line.strip()]
        self.assertTrue(any(e.get("type") == "final_snapshot_unavailable"
                            for e in events))


# ---------------------------------------------------------------------------
# F16 — Treasury spread basis points
# ---------------------------------------------------------------------------


class F16SpreadTests(_Isolated, unittest.TestCase):
    def _curve(self, ten, two):
        import tradingagents.dataflows.macro_utils as macro

        values = {"DGS10": ten, "DGS2": two}

        def _fake_fetch(series_id, start_date, end_date):
            value = values.get(series_id)
            if value is None:
                return {"observations": [{"date": "2026-09-01", "value": "."}]}
            return {"observations": [
                {"date": "2026-09-01", "value": str(value)}]}

        with patch.object(macro, "get_fred_data", side_effect=_fake_fetch), patch(
            "tradingagents.dataflows.macro_utils.get_fred_api_key",
            return_value="fake-key",
        ):
            return macro.get_treasury_yield_curve("2026-09-08")

    def test_normal_100bps(self):
        report = self._curve(4.00, 3.00)
        self.assertIn("100.00 basis points", report)
        self.assertIn("NORMAL YIELD CURVE", report)
        self.assertNotIn("1.00 basis points", report)

    def test_flat_25bps(self):
        report = self._curve(3.25, 3.00)
        self.assertIn("25.00 basis points", report)
        self.assertIn("FLAT YIELD CURVE", report)

    def test_inverted_minus10bps(self):
        report = self._curve(2.90, 3.00)
        self.assertIn("-10.00 basis points", report)
        self.assertIn("INVERTED YIELD CURVE", report)


# ---------------------------------------------------------------------------
# F17 — America/New_York analysis date
# ---------------------------------------------------------------------------


class F17AnalysisDateTests(_Isolated, unittest.TestCase):
    def test_taipei_after_midnight_uses_new_york_date(self):
        from tradingagents.dataflows.interface_utils import (
            current_analysis_date,
            parse_analysis_date,
        )

        # 2026-09-09 00:30 Taipei = 2026-09-08 12:30 New York (EDT).
        taipei = datetime(2026, 9, 9, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        self.assertEqual(current_analysis_date(taipei), "2026-09-08")
        self.assertEqual(current_analysis_date(), parse_analysis_date(
            current_analysis_date()).isoformat())
        self.assertTrue(parse_analysis_date("2026-09-08"))

    def test_naive_injected_now_rejected(self):
        from tradingagents.dataflows.interface_utils import current_analysis_date

        with self.assertRaises(ValueError):
            current_analysis_date(datetime(2026, 9, 8, 12, 0))

    def test_webui_uses_helper_not_local_now(self):
        import tradingagents.dataflows.interface_utils as iu
        import webui.components.analysis as analysis

        source = Path(analysis.__file__).read_text()
        self.assertIn("current_analysis_date", source)
        self.assertNotIn('datetime.now().strftime("%Y-%m-%d")', source)
        self.assertTrue(callable(iu.current_analysis_date))

    def test_cli_uses_helper_not_local_now(self):
        source = Path(
            __import__("cli.main", fromlist=["main"]).__file__).read_text()
        self.assertIn("current_analysis_date", source)
        self.assertNotIn('datetime.datetime.now().strftime("%Y-%m-%d")', source)


# ---------------------------------------------------------------------------
# F18 — packaged Web entry point
# ---------------------------------------------------------------------------


class F18EntryPointTests(_Isolated, unittest.TestCase):
    def test_webui_cli_imports_and_parses(self):
        import webui.cli

        self.assertTrue(callable(webui.cli.main))

    def test_entry_point_target_resolves(self):
        source = (Path(__file__).resolve().parents[1] / "setup.py").read_text()
        # setup.py must point at the packaged webui.cli module.
        self.assertIn("tradingagents-web=webui.cli:main", source)
        self.assertNotIn("web_ui:main", source)

    def test_root_script_delegates_to_packaged_module(self):
        root = Path(__file__).resolve().parents[1] / "run_webui_dash.py"
        source = root.read_text()
        self.assertIn("from webui.cli import main", source)

    def test_installed_wheel_entry_point_smoke(self):
        """Build a wheel, import its webui.cli module from the wheel contents
        in an isolated path, and prove the entry target resolves without
        contacting any external service."""
        import subprocess
        import sys
        import zipfile

        repo = Path(__file__).resolve().parents[1]
        wheel_dir = self.workdir / "wheel"
        wheel_dir.mkdir()
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", ".",
             "-w", str(wheel_dir)],
            cwd=repo, check=True, capture_output=True, timeout=300,
        )
        wheels = list(wheel_dir.glob("tradingagents-*.whl"))
        self.assertTrue(wheels)
        with zipfile.ZipFile(wheels[0]) as zf:
            names = zf.namelist()
            self.assertIn("webui/cli.py", names)
            entry = zf.read("tradingagents-0.1.0.dist-info/entry_points.txt").decode()
            self.assertIn("tradingagents-web", entry)
            self.assertIn("webui.cli:main", entry.replace(" ", ""))
            # The packaged module must be importable from the wheel alone.
            extracted = self.workdir / "wheel-extract"
            extracted.mkdir()
            zf.extractall(extracted)
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "wheel_webui_cli",
            extracted / "webui" / "cli.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))

    def test_run_webui_dash_help_exits_without_server(self):
        import subprocess
        import sys

        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "run_webui_dash.py", "--help"],
            cwd=repo, capture_output=True, timeout=60, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--port", result.stdout)


if __name__ == "__main__":
    unittest.main()
